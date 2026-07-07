"""tests/exec/test_render.py —— Jinja2 渲染共享层（SPEC §7.9 / 计划 B.3）。

覆盖：
  - ``render_template``：inputs 取值 / 嵌套 output 取值 / 未定义变量 raise
  - ``render_command``：ScriptNode command 渲染
  - ``render_prompt``：内联 prompt / agents/<name>.md 加载 / md 不存在 raise
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orca.exec.context import RunContext
from orca.exec.error import ExecError
from orca.exec.render import render_command, render_prompt, render_template
from orca.schema import AgentNode


def _ctx(inputs=None, outputs=None) -> RunContext:
    return RunContext(inputs=inputs or {}, outputs=outputs or {}, run_id="r1")


# ── render_template ──────────────────────────────────────────────────────────


def test_render_template_inputs_value():
    out = render_template("hello {{ inputs.x }}", _ctx(inputs={"x": "world"}))
    assert out == "hello world"


def test_render_template_nested_output_via_dotted_path():
    """{{ optimizer.output.structure }} 从 ctx.outputs['optimizer'] 取（嵌套，SPEC §7.9）。"""
    ctx = _ctx(outputs={"optimizer": {"output": {"structure": "tree"}}})
    out = render_template("{{ optimizer.output.structure }}", ctx)
    assert out == "tree"


def test_render_template_node_output_top_level():
    """node 名做顶层变量：{{ finder.output }} 取整个 output dict。"""
    ctx = _ctx(outputs={"finder": {"output": {"found": 3}}})
    out = render_template("{{ finder.output.found }}", ctx)
    assert out == "3"


def test_render_template_undefined_variable_raises_exec_error():
    """未定义变量 fail loud（StrictUndefined → ExecError phase=render，SPEC §6）。"""
    with pytest.raises(ExecError) as ei:
        render_template("{{ undefined_var }}", _ctx())
    assert ei.value.phase == "render"
    assert ei.value.error_type == "RenderError"


def test_render_template_undefined_attribute_raises():
    """引用存在的 node 但字段不存在 → fail loud。"""
    ctx = _ctx(outputs={"finder": {"output": {"found": 3}}})
    with pytest.raises(ExecError, match="render"):
        render_template("{{ finder.output.nonexistent }}", ctx)


def test_render_template_literal_passthrough():
    out = render_template("just literal text, no vars", _ctx())
    assert out == "just literal text, no vars"


# ── render_command ───────────────────────────────────────────────────────────


def test_render_command_substitutes_inputs():
    out = render_command("echo {{ inputs.path }}", _ctx(inputs={"path": "/tmp/x"}))
    assert out == "echo /tmp/x"


# ── render_prompt ────────────────────────────────────────────────────────────


def test_render_prompt_inline_prompt_rendered():
    node = AgentNode(name="a", prompt="Summarize: {{ inputs.text }}")
    out = render_prompt(node, _ctx(inputs={"text": "hi"}))
    assert out == "Summarize: hi"


def test_render_prompt_materialized_agent_md_renders():
    """phase-14：agent md 由 compile 期 AgentResolver 物化进 node.prompt（含 Jinja2）。

    render 层只渲染已物化的字符串（删了旧 ``_load_agent_md`` 文件 I/O，消除双加载债）。
    验证物化后的 agent prompt 渲染正确（md 加载本身的测试在 tests/compile/test_agents.py）。
    """
    # 模拟 compile 物化：node.prompt 已是 agent md 内容（含 {{ inputs.role }}）
    node = AgentNode(name="writer", prompt="You are {{ inputs.role }}.")
    out = render_prompt(node, _ctx(inputs={"role": "a poet"}))
    assert out == "You are a poet."


def test_render_prompt_none_raises():
    """phase-14：node.prompt=None → ExecError（防御性 fail loud）。

    agent 引用必须在 compile 期经 AgentResolver 物化进 node.prompt；render 拿到 None
    说明绕过了 load_workflow（程序化 ``Workflow(**raw)`` 构造），清晰归因提示。
    """
    node = AgentNode(name="ghost", prompt=None)
    with pytest.raises(ExecError) as ei:
        render_prompt(node, _ctx())
    assert ei.value.phase == "render"
    assert "未物化" in ei.value.message


def test_render_prompt_empty_raises():
    """phase-14 C7：node.prompt=''（空串）→ ExecError（空 prompt 给 claude 行为未定义）。"""
    node = AgentNode(name="empty", prompt="")
    with pytest.raises(ExecError) as ei:
        render_prompt(node, _ctx())
    assert ei.value.phase == "render"


# ── locals 注入（foreach body 用，phase 5 扩展）───────────────────────────────


def test_render_template_resolves_locals_item():
    """{{ item }} 取 ctx.locals['item']（foreach body 裸引用，无 inputs. 前缀）。"""
    ctx = RunContext(inputs={}, outputs={}, run_id="r1", locals={"item": "apple"})
    assert render_template("process {{ item }}", ctx) == "process apple"


# ── phase-10 技术债回填：setup phase outputs 渲染 ────────────────────────────


def test_render_template_setup_output_via_dotted_path():
    """{{ setup.<agent>.output.<field> }} 取 ctx.setup（MCP setup_outputs 注入路径）。"""
    ctx = RunContext(
        inputs={}, outputs={}, run_id="r1",
        setup={"collector": {"output": {"host": "example.com"}}},
    )
    assert render_template("host={{ setup.collector.output.host }}", ctx) == (
        "host=example.com"
    )


def test_render_template_setup_empty_when_no_setup_phase():
    """无 setup phase → ctx.setup 空 dict，setup 根不污染现有模板（向后兼容）。"""
    ctx = RunContext(inputs={"x": "1"}, outputs={}, run_id="r1")
    # 现有模板照常渲染
    assert render_template("x={{ inputs.x }}", ctx) == "x=1"
    # setup 根存在但为空 dict（不 raise）
    assert render_template("{% if setup %}has{% else %}none{% endif %}", ctx) == "none"


# ── phase 11 §4：guidance section 拼接 ──────────────────────────────────────


def test_render_prompt_appends_guidance_section():
    """ctx 含 user_guidance → 渲染后 prompt 末尾有 [User Guidance] 段 + 每条 guidance。"""
    ctx = RunContext(
        inputs={}, outputs={}, run_id="r1",
        user_guidance=("用更保守的方案", "省 token"),
    )
    node = AgentNode(name="x", prompt="任务 X")
    rendered = render_prompt(node, ctx)
    assert rendered.startswith("任务 X")
    assert "[User Guidance]" in rendered
    assert "- 用更保守的方案" in rendered
    assert "- 省 token" in rendered
    assert "Incorporate this guidance" in rendered


def test_render_prompt_empty_guidance_no_section():
    """ctx 无 guidance（空 tuple）→ 渲染后无 [User Guidance] 段（向后兼容）。"""
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")  # user_guidance 默认 ()
    node = AgentNode(name="x", prompt="任务 X")
    rendered = render_prompt(node, ctx)
    assert rendered == "任务 X"
    assert "[User Guidance]" not in rendered


def test_render_prompt_single_guidance():
    """单条 guidance 也正确拼接。"""
    ctx = RunContext(inputs={}, outputs={}, run_id="r1", user_guidance=("only one",))
    node = AgentNode(name="x", prompt="do thing")
    rendered = render_prompt(node, ctx)
    assert "[User Guidance]" in rendered
    assert "- only one" in rendered


def test_guidance_prompt_section_format_exact():
    """guidance_prompt_section 逐字对齐 Conductor（SPEC §4.1 / §1.2 实证）。"""
    ctx = RunContext(
        inputs={}, outputs={}, run_id="r1",
        user_guidance=("g1", "g2"),
    )
    section = ctx.guidance_prompt_section()
    assert section == (
        "\n\n[User Guidance]\n"
        "The following guidance was provided by the user during workflow execution. "
        "Incorporate this guidance into your response:\n"
        "- g1\n- g2"
    )


def test_guidance_prompt_section_none_when_empty():
    """无 guidance → guidance_prompt_section 返回 None。"""
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")
    assert ctx.guidance_prompt_section() is None


def test_context_with_guidance_immutable():
    """with_guidance 返回新 frozen 实例，原 ctx 不变（frozen 语义，SPEC §4.1）。"""
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")
    new_ctx = ctx.with_guidance("用更保守的方案")
    assert ctx.user_guidance == ()  # 原实例不变
    assert new_ctx.user_guidance == ("用更保守的方案",)
    assert new_ctx is not ctx


def test_context_with_guidance_accumulates():
    """多次 with_guidance 累积（不覆盖）。"""
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")
    ctx = ctx.with_guidance("g1").with_guidance("g2")
    assert ctx.user_guidance == ("g1", "g2")


def test_context_with_guidance_empty_ignored():
    """空 / 全空白 guidance 不累积（防 prompt 末尾空 guidance 段）。"""
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")
    assert ctx.with_guidance("").user_guidance == ()
    assert ctx.with_guidance("   ").user_guidance == ()
    assert ctx.with_guidance("real").user_guidance == ("real",)



def test_render_template_resolves_locals_index():
    """{{ _index }} 取 ctx.locals['_index']（foreach 索引变量）。"""
    ctx = RunContext(inputs={}, outputs={}, run_id="r1", locals={"item": "x", "_index": 2})
    assert render_template("#{{ _index }}: {{ item }}", ctx) == "#2: x"


def test_render_template_locals_compose_with_inputs_and_outputs():
    """locals 与 inputs / outputs 同层共存（三者摊到 Jinja2 顶层命名空间）。

    注意：``task`` 字段不摊顶层（它仅作为 ctx 数据字段供日志/事件用，渲染时走
    ``inputs.task``，由 orchestrator 把位置参数 task 注入 inputs）。此处验证 locals
    与 inputs / outputs 的共存，``item`` 来自 locals，``prev.output.v`` 来自 outputs。
    """
    ctx = RunContext(
        inputs={"task": "T"},
        outputs={"prev": {"output": {"v": 1}}},
        run_id="r1",
        locals={"item": "y"},
    )
    out = render_template(
        "{{ inputs.task }}/{{ prev.output.v }}/{{ item }}", ctx
    )
    assert out == "T/1/y"


def test_render_template_empty_locals_no_effect():
    """locals 默认空 dict，对普通 node 渲染无影响（零回归）。"""
    ctx = RunContext(inputs={"x": "1"}, outputs={}, run_id="r1", locals={})
    assert render_template("{{ inputs.x }}", ctx) == "1"

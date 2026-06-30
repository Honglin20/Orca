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


def test_render_prompt_none_loads_agents_md(tmp_path, monkeypatch):
    """node.prompt=None → 从 agents/<name>.md 加载（cwd 相对路径，SPEC §4.6）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "writer.md").write_text(
        "You are {{ inputs.role }}.", encoding="utf-8"
    )
    node = AgentNode(name="writer", prompt=None)
    out = render_prompt(node, _ctx(inputs={"role": "a poet"}))
    assert out == "You are a poet."


def test_render_prompt_none_missing_md_raises(tmp_path, monkeypatch):
    """prompt=None 且 agents/<name>.md 不存在 → ExecError(phase=render)（fail loud）。"""
    monkeypatch.chdir(tmp_path)
    node = AgentNode(name="ghost", prompt=None)
    with pytest.raises(ExecError) as ei:
        render_prompt(node, _ctx())
    assert ei.value.phase == "render"
    assert "agents/ghost.md" in ei.value.message


def test_render_prompt_none_md_with_jinja_render_error(tmp_path, monkeypatch):
    """agents/<name>.md 存在但内部 Jinja2 引用未定义变量 → ExecError(phase=render)。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "bad.md").write_text(
        "uses {{ undefined_thing }}", encoding="utf-8"
    )
    node = AgentNode(name="bad", prompt=None)
    with pytest.raises(ExecError, match="render"):
        render_prompt(node, _ctx())


# ── locals 注入（foreach body 用，phase 5 扩展）───────────────────────────────


def test_render_template_resolves_locals_item():
    """{{ item }} 取 ctx.locals['item']（foreach body 裸引用，无 inputs. 前缀）。"""
    ctx = RunContext(inputs={}, outputs={}, run_id="r1", locals={"item": "apple"})
    assert render_template("process {{ item }}", ctx) == "process apple"


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

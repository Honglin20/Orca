"""render.py —— Jinja2 渲染共享层（agent / script / set 三种 executor 共用）。

回答「prompt / command / values 怎么填上下文？」：把 ``RunContext`` 暴露成 Jinja2
命名空间，统一渲染入口，失败 fail loud（SPEC §4.7 / §6 phase=render）。

Jinja2 命名空间（SPEC §4.7）：
  - ``{{ inputs.x }}`` → ``ctx.inputs["x"]``
  - ``{{ node_name.output.field }}`` → ``ctx.outputs["node_name"]["output"]["field"]``
    （已完成 node 的 output 累积在 ``ctx.outputs``，存 ``{"output": raw}`` 包装）
  - ``{{ item }}`` / ``{{ _index }}`` → ``ctx.locals["item"]``（foreach body 注入，
    由 phase 5 orchestrator 经 ``RunContext.with_locals`` 派生实例）

设计：
  - **UndefinedError 显式开**（``StrictUndefined``）：引用未定义变量 fail loud，
    不静默渲染成空串（防 prompt 漂移）。
  - **trim_blocks/lstrip_block**：YAML 多行模板更友好（不强制，但减少意外空白）。
  - 三种 executor 的渲染需求收敛到此：``render_prompt``（agent）/ ``render_command``
    （script）/ ``render_template``（set 的 values + 通用）。

依赖单向：本模块依赖 ``orca.exec.context``（RunContext 类型）+ ``orca.exec.error``
（ExecError），不依赖 schema/events/profiles 之外的层。
"""

from __future__ import annotations

from typing import Any

from jinja2 import Environment, StrictUndefined, TemplateError

from orca.exec.context import RunContext
from orca.exec.error import ExecError

# 单例 Environment：StrictUndefined 让未定义变量 fail loud（SPEC §6 phase=render）。
# trim_blocks/lstrip_block 让 YAML 多行模板（agent inline prompt）渲染更干净。
_ENV = Environment(
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,  # prompt/command 是给 agent/shell 的，非 HTML，不需转义
)


def _namespace(ctx: RunContext) -> dict[str, Any]:
    """把 RunContext 摊成 Jinja2 顶层命名空间（SPEC §4.7）。

    - ``inputs``：直接放 ``ctx.inputs``
    - 每个 node 的 output：以 node 名为 key 放顶层（``ctx.outputs`` 展开），
      支持 ``{{ optimizer.output.structure }}`` 这种点路径（``outputs["optimizer"]
      = {"output": {...}}``，故 ``{{ optimizer.output.structure }}`` 取得到）
    - ``locals``：foreach body 注入的局部变量（``{{ item }}`` / ``{{ _index }}``）
      摊到顶层；普通 node ``locals`` 为空 dict（无影响）。
    """
    ns: dict[str, Any] = {"inputs": dict(ctx.inputs)}
    # ctx.outputs 的 key（node 名）直接做顶层变量，value（{"output": raw} dict）原样暴露
    ns.update(ctx.outputs)
    # ctx.locals 摊顶层（foreach body 的 item / _index；普通 node 为空，update 无影响）
    ns.update(ctx.locals)
    return ns


def render_template(template: str, ctx: RunContext) -> str:
    """渲染 Jinja2 模板（通用入口，SPEC §7.9）。

    失败（未定义变量 / 语法错）raise ``ExecError(phase="render")``（fail loud，SPEC §6）。
    """
    try:
        tpl = _ENV.from_string(template)
        return tpl.render(**_namespace(ctx))
    except TemplateError as e:
        raise ExecError(
            phase="render",
            message=f"Jinja2 渲染失败：{e.__class__.__name__}: {e}",
        ) from e


def render_command(command: str, ctx: RunContext) -> str:
    """渲染 ScriptNode.command（SPEC §4.6）。同 ``render_template``，仅语义命名区分。"""
    return render_template(command, ctx)


def render_prompt(node, ctx: RunContext) -> str:
    """组装 agent prompt（SPEC §4.6 / §7.9 / phase-14）。

    phase-14：prompt 在 compile 期已由 ``AgentResolver`` 物化进 ``node.prompt``（agent 引用
    ``agent: <name>`` 或旧约定 name-fallback），render 层**零文件 I/O**（删了旧的
    ``_load_agent_md`` 双加载债——它用 cwd 相对路径，与 compile 期 yaml 父目录不一致）。

    ``node.prompt`` 为 None 或空串 → 防御性 fail loud（C7：空 prompt 给 claude 行为未定义；
    也用于归因「绕过 load_workflow 直接构造 Workflow」的程序化构造误用）。

    phase 11 §4：渲染完 base prompt 后，若 ``ctx.user_guidance`` 非空，拼 ``[User Guidance]``
    段到末尾（``ctx.guidance_prompt_section()``）。这是用户 Ctrl+G + CONTINUE 注入纠偏话的
    落地点——重 spawn 的 agent 看到 prompt 末尾的 guidance 段。无 guidance 时返回 base 原样。
    """
    # C7：None 或空串 "" 都防（空 prompt 给 claude 行为未定义）。
    if not node.prompt:
        raise ExecError(
            phase="render",
            message=(
                f"agent {node.name!r} 的 prompt 未物化或为空（node.prompt={node.prompt!r}）。"
                "是否绕过了 load_workflow 直接构造 Workflow？"
                "agent 引用必须在 compile 期经 AgentResolver 解析物化进 node.prompt。"
            ),
        )
    base = render_template(node.prompt, ctx)

    # phase 11 §4：拼 guidance section（无 guidance 时 section=None，原样返回）。
    guidance_section = ctx.guidance_prompt_section()
    if guidance_section:
        return base + guidance_section
    return base


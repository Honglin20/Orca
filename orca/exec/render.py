"""render.py —— Jinja2 渲染共享层（agent / script / set 三种 executor 共用）。

回答「prompt / command / values 怎么填上下文？」：把 ``RunContext`` 暴露成 Jinja2
命名空间，统一渲染入口，失败 fail loud（SPEC §4.7 / §6 phase=render）。

Jinja2 命名空间（SPEC §4.7）：
  - ``{{ inputs.x }}`` → ``ctx.inputs["x"]``
  - ``{{ node_name.output.field }}`` → ``ctx.outputs["node_name"]["output"]["field"]``
    （已完成 node 的 output 累积在 ``ctx.outputs``）
  - ``{{ item }}`` / ``{{ _index }}`` → foreach body 注入（phase 5，本阶段 foreach 不做）

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

from pathlib import Path
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
      支持 ``{{ optimizer.output.structure }}`` 这种点路径
    """
    ns: dict[str, Any] = {"inputs": dict(ctx.inputs)}
    # ctx.outputs 的 key（node 名）直接做顶层变量，value（output dict）原样暴露
    ns.update(ctx.outputs)
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
    """组装 agent prompt（SPEC §4.6 / §7.9）。

    - ``node.prompt`` 非空 → 内联短 prompt，渲染后返回
    - ``node.prompt is None`` → 从 ``agents/<node.name>.md`` 加载（与 compile 约定一致），
      文件不存在 → ``ExecError(phase="render")``（fail loud）

    agents/<name>.md 的内容经 Jinja2 渲染（支持 ``{{ inputs.x }}`` 引用）。
    """
    if node.prompt is not None:
        return render_template(node.prompt, ctx)

    # None → 约定加载 agents/<name>.md（cwd 相对路径，与 compile/ 的引用解析一致）
    md_path = Path("agents") / f"{node.name}.md"
    if not md_path.is_file():
        raise ExecError(
            phase="render",
            message=(
                f"agent {node.name!r} 的 prompt 为空且找不到约定文件 {md_path}（cwd="
                f"{Path.cwd()!s}）；要么在 node 里内联 prompt，要么提供该 md 文件"
            ),
        )
    try:
        md_text = md_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ExecError(
            phase="render",
            message=f"读取 agent prompt 文件 {md_path} 失败：{e}",
        ) from e
    return render_template(md_text, ctx)

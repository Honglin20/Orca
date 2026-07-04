"""orca.iface.cli.widgets.tool_render —— render layer v1（render-layer-design-draft）。

回答「工具调用怎么渲染？」：在 canonical Event 之上加一层 iface 内的纯函数::

    Event ──[normalize]──▶ RenderItem ──[registry]──▶ Rich renderable
            (backend 工具差异归一)                    (TUI)

模块布局（spec §7.1）：
  - normalize.py：``(executor, tool, args, result, status) → RenderItem`` 纯函数
  - kinds.py：per-kind Rich renderer（file_read/file_write/.../unknown + thinking/message）
  - registry.py：kind → renderer 派发表
  - reduce.py：RenderState + Event 流累积 reducer

依赖单向（§7.2）：本包只依赖 ``orca.schema`` + ``textual``/``rich`` + stdlib，
**禁止** import ``orca.exec`` / ``orca.run`` / ``orca.events.bus``。
"""

from __future__ import annotations

from orca.iface.cli.widgets.tool_render.normalize import (
    NormalizeError,
    describe_tool_event,
    normalize_tool,
)
from orca.iface.cli.widgets.tool_render.reduce import (
    RenderState,
    reduce_event,
)
from orca.iface.cli.widgets.tool_render.registry import render_tool

__all__ = [
    "NormalizeError",
    "describe_tool_event",
    "normalize_tool",
    "RenderState",
    "reduce_event",
    "render_tool",
]

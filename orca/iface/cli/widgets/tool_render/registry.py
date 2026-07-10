"""tool_render/registry.py —— kind → Rich renderer 派发表（render-layer §3.1 / §7）。

回答「给定 RenderItem 怎么找到对应 renderer？」：一张静态表，O(1) 派发。

OCP 扩展点：新 kind 只需在 ``_RENDERERS`` 加一行，不改核心路径（render_layer §4.3）。
"""

from __future__ import annotations

from typing import Callable

from rich.console import RenderableType

from orca.schema import RenderItem, RenderToolKind

from orca.iface.cli.widgets.tool_render.kinds import (
    render_file_edit,
    render_file_read,
    render_file_write,
    render_glob,
    render_grep,
    render_shell,
    render_unknown,
)

_RKindRenderer = Callable[[RenderItem], RenderableType]

# kind → renderer 注册表（§3.1 第四层派发，对齐 normalize._PAYLOAD_NORMALIZERS）。
_RENDERERS: dict[RenderToolKind, _RKindRenderer] = {
    "file_read": render_file_read,
    "file_write": render_file_write,
    "file_edit": render_file_edit,
    "shell": render_shell,
    "glob": render_glob,
    "grep": render_grep,
    "unknown": render_unknown,
}


def render_tool(item: RenderItem) -> RenderableType:
    """kind 派发（fail loud：未知 kind 走 unknown renderer，不应静默丢失）。

    防御性：kind 不在表内（schema extra="forbid" 已防，但代码兜底）→ fallback unknown。
    """
    renderer = _RENDERERS.get(item.kind, render_unknown)
    return renderer(item)


__all__ = ["render_tool", "_RENDERERS"]

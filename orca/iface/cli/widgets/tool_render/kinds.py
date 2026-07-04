"""tool_render/kinds.py —— per-kind Rich renderer（render-layer-design-draft §8）。

回答「RenderItem 怎么变成像素？」：每个 kind 一个纯函数
``render_<kind>(item: RenderItem) -> RichRenderable``，由 registry 派发。

视觉意图对齐 claude-code（§8 / §12）：
  - 折叠 Panel 包裹所有 kind（共性规则 §8.2）
  - 状态色体现在 Panel 标题（completed 绿 / running 灰 / error 红 / interrupted 黄）
  - file_edit：unified diff（+绿 / -红 / ctx 灰）
  - shell：终端块（等宽）
  - unknown：args JSON 美化（§12.9）
  - agent_thinking 在 ``render_thinking`` 单独处理（§12.8 dim+italic 纯文本）

依赖单向：仅 ``orca.schema`` + ``rich`` + stdlib。
"""

from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from rich.tree import Tree

from orca.schema import RenderItem, ToolStatus

# ── 状态色（§8.2 共性规则：体现在 Panel border / icon）────────────────────────
_STATUS_COLOR: dict[ToolStatus, str] = {
    "running": "cyan",
    "completed": "green",
    "error": "red",
    "interrupted": "yellow",
}

_STATUS_ICON: dict[ToolStatus, str] = {
    "running": "…",
    "completed": "✓",
    "error": "✗",
    "interrupted": "⏸",
}

# 单 kind body 行数上限（>折叠 + 显示"展开更多"；千行虚拟化延后 v2，§8.2）。
_BODY_LINE_LIMIT = 200


# ── 公共 helpers ───────────────────────────────────────────────────────────────


def _status_prefix(item: RenderItem) -> str:
    """``[✓]`` / ``[…]`` / ... 状态前缀，附在 Panel title 前。"""
    return f"[{_STATUS_ICON[item.status]}]"


def _make_panel(item: RenderItem, body: Any, *, kind_icon: str = "") -> Panel:
    """构建标准折叠 Panel（共性规则 §8.2）。

    title：``[status] kind_icon title  subtitle``（status 用色 + icon）
    body：任意 Rich renderable
    """
    color = _STATUS_COLOR[item.status]
    parts = [_status_prefix(item)]
    if kind_icon:
        parts.append(kind_icon)
    parts.append(item.title)
    if item.subtitle:
        parts.append(f"({item.subtitle})")
    title = " ".join(parts)
    return Panel(
        body,
        title=f"[{color}]{title}[/{color}]",
        title_align="left",
        border_style=color,
    )


def _truncate_body_lines(text: str) -> str:
    """body > _BODY_LINE_LIMIT 行 → 截断 + 提示（§8.2）。"""
    lines = text.splitlines()
    if len(lines) <= _BODY_LINE_LIMIT:
        return text
    head = "\n".join(lines[:_BODY_LINE_LIMIT])
    return f"{head}\n…（共 {len(lines)} 行，v1 截断显示前 {_BODY_LINE_LIMIT} 行；v2 加虚拟化）"


# ── per-kind renderer ─────────────────────────────────────────────────────────


def render_file_read(item: RenderItem) -> Panel:
    """file_read：目录树 / 行号化代码（§8.1）。"""
    payload = item.payload
    if payload.get("is_dir"):
        entries = payload.get("entries", [])
        tree = Tree(f"[bold]{payload.get('path', '')}[/bold]")
        for entry in entries:
            tree.add(entry)
        return _make_panel(item, tree, kind_icon="📂")

    # 文件：行号化代码块（rich.syntax 的 line_numbers=True）
    content = payload.get("content", [])
    full_text = "\n".join(line.get("text", "") for line in content)
    body_text = _truncate_body_lines(full_text)
    body = Syntax(
        body_text, lexer="python", line_numbers=True, theme="ansi_dark", word_wrap=True,
    )
    return _make_panel(item, body, kind_icon="📄")


def render_file_write(item: RenderItem) -> Panel:
    """file_write：行号化新文件内容（§8.1）。"""
    payload = item.payload
    content = payload.get("content", [])
    text = "\n".join(line.get("text", "") for line in content)
    body = _truncate_body_lines(text)
    syntax = Syntax(body, lexer="python", line_numbers=True, theme="ansi_dark", word_wrap=True)
    return _make_panel(item, syntax, kind_icon="✏")


def render_file_edit(item: RenderItem) -> Panel:
    """file_edit：unified diff（+ 绿 / - 红 / ctx 灰底，§8.1 / §12.12）。"""
    payload = item.payload
    hunks = payload.get("hunks", [])
    chunks: list[Text] = []
    for hunk in hunks:
        start = hunk.get("start", 1)
        chunks.append(Text(f"@@ @@ +{start}", style="dim cyan"))
        for line in hunk.get("lines", []):
            t = line.get("type", "ctx")
            text = line.get("text", "")
            marker = {"add": "+", "del": "-", "ctx": " "}.get(t, " ")
            style = {"add": "green", "del": "red", "ctx": "dim"}.get(t, "dim")
            chunks.append(Text(f"{marker}{text}", style=style))
    body = Group(*chunks) if chunks else Text("(空 diff)", style="dim")
    return _make_panel(item, body, kind_icon="✏")


def render_shell(item: RenderItem) -> Panel:
    """shell：终端块（等宽，§8.1）。"""
    payload = item.payload
    output = payload.get("output", "")
    body = _truncate_body_lines(output)
    text = Text(body, style="white on #1a1a1a")
    return _make_panel(item, text, kind_icon="▶")


def render_glob(item: RenderItem) -> Panel:
    """glob：路径列表（§8.1）。"""
    payload = item.payload
    matches = payload.get("matches", [])
    if not matches:
        body = Text("(无匹配)", style="dim")
    else:
        body = Text("\n".join(matches))
    return _make_panel(item, body, kind_icon="༚")


def render_grep(item: RenderItem) -> Panel:
    """grep：按文件分组，命中行展示（§8.1）。

    v1 不高亮 hit 区间（spec §5.2 grep payload 有 hit_start/end，但 normalizer v1 未解析）。
    """
    payload = item.payload
    matches = payload.get("matches", [])
    chunks: list[Text] = []
    for group in matches:
        path = group.get("path", "?")
        chunks.append(Text(path, style="bold cyan"))
        for line in group.get("lines", []):
            n = line.get("n", 0)
            text = line.get("text", "")
            chunks.append(Text(f"  {n}:{text}"))
    body = Group(*chunks) if chunks else Text("(无匹配)", style="dim")
    return _make_panel(item, body, kind_icon="🔍")


def render_unknown(item: RenderItem) -> Panel:
    """unknown：args JSON 美化 + result 截断预览（§8.1 / §12.9）。"""
    payload = item.payload
    args_preview = payload.get("args_preview", "")
    result_preview = payload.get("result_preview", "")
    chunks: list[Any] = []
    if args_preview:
        chunks.append(Text("args:", style="dim bold"))
        chunks.append(Syntax(args_preview, lexer="json", theme="ansi_dark", word_wrap=True))
    if result_preview:
        chunks.append(Text("result:", style="dim bold"))
        chunks.append(Text(result_preview))
    if not chunks:
        chunks.append(Text("(无 args/result)", style="dim"))
    body = Group(*chunks)
    return _make_panel(item, body, kind_icon="?")


# ── agent_thinking / agent_message（不是 RenderItem.kind，由 reducer 单独累积）──


def render_thinking(text: str) -> Text:
    """agent_thinking：dim + italic 纯文本（§12.8）。

    **不渲染 markdown**（claude-code 对齐）。多行文本按 ``\\n`` 拼接，整段包成 Text。
    """
    return Text(text, style="dim italic")


def render_message(text: str) -> Markdown:
    """agent_message：Rich Markdown（含代码块 Syntax 高亮，§8.3 / §12.12）。

    Rich Markdown 默认开启代码块语法高亮，零额外工作（spec §12.12 acceptance）。
    """
    return Markdown(text)


__all__ = [
    "render_file_read",
    "render_file_write",
    "render_file_edit",
    "render_shell",
    "render_glob",
    "render_grep",
    "render_unknown",
    "render_thinking",
    "render_message",
]

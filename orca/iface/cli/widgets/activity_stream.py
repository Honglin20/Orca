"""activity_stream.py —— Activity Stream widget（tui-redesign-draft v1.1 §5）。

回答「发生过什么？」：双行 entry（summary 行 + meta 行）+ 折叠详情（spec §5.3 / §5.4）。

设计：
  - ``entries: list[ActivityEntry]``：内部维护，按 seq 排序（reducer 派生 fold）
  - ``filter_mode``：``all``（默认）/ ``node``（仅选中节点）
  - 选中 entry：``j/k`` 切换；选中时 detail_view 自动展开折叠详情
  - detail 内容：phase-15 ``render_tool`` / ``render_message`` / ``render_thinking``

依赖单向（spec §8.2）：仅 import ``orca.schema`` + ``textual`` + ``rich`` + stdlib +
本包 ``tool_render`` + ``_event_filter``；禁止 ``orca.exec`` / ``orca.run`` / ``orca.events.bus``。

注意：``filter_mode`` 是 UI 交互态（不持久化、不进 tape，spec §4.4.1）。
重启清零，重放同 tape 不重建。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from rich.console import Group, RenderableType
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import RichLog, Static

from orca.iface.cli.widgets._event_filter import visibility_of
from orca.iface.cli.widgets.tool_render import (
    NormalizeError,
    normalize_tool,
    render_tool,
)
from orca.iface.cli.widgets.tool_render.kinds import render_message, render_thinking
from orca.schema import RenderItem

logger = logging.getLogger(__name__)

# spec §5.5：折叠块内部 VerticalScroll 上限（防爆卡顿）
_DETAIL_LINE_CAP = 200
# 摘要行 / 标题最大字符数（spec §5.4 字段级定义里 ``data.text[:50]`` 等）
_TITLE_LIMIT = 50


@dataclass
class ActivityEntry:
    """单条 Activity Stream entry（spec §5.4 per-type entry 结构表）。

    每条 entry 对应一个 Event；持有：
      - ``seq``：Event seq（排序用，reducer 派生 fold）
      - ``event_type``：EventType 名（派发用，未直接 import EventType 避免循环）
      - ``node``：节点名（filter ``node`` 模式用）
      - ``visibility``：visibility tag（spec §6.4）
      - ``summary_line``：双行 entry 第 1 行（type_icon + title）
      - ``meta_line``：双行 entry 第 2 行（meta，可空）
      - ``detail_renderable``：折叠详情 Rich renderable（None = 无详情，如 route_taken）
    """

    seq: int
    event_type: str
    node: str | None
    timestamp: float
    visibility: str
    summary_line: str
    meta_line: str = ""
    detail_renderable: RenderableType | None = None


# ── per-type entry 渲染（spec §5.4 字段级定义）─────────────────────────────


def _truncate(s: Any, limit: int = _TITLE_LIMIT) -> str:
    """字符串截断 + ``…``。"""
    text = str(s) if s is not None else ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _arg_title(tool: str, args: Any) -> str:
    """per-tool 一句话标题（spec §5.4 ``_arg_title``）。"""
    if not isinstance(args, dict):
        return _truncate(args, 40)
    tool_lower = (tool or "").lower()
    if tool_lower in ("read",):
        return str(args.get("filePath") or args.get("file_path") or args.get("path") or "")
    if tool_lower in ("bash",):
        return _truncate(args.get("command", ""), 50)
    if tool_lower in ("glob",):
        return str(args.get("pattern", ""))
    if tool_lower in ("grep",):
        return str(args.get("pattern", ""))
    if tool_lower in ("write",):
        path = args.get("filePath") or args.get("path") or ""
        return f"{path} (new)" if path else ""
    if tool_lower in ("edit",):
        return str(args.get("filePath") or args.get("path") or "")
    # 其他工具：json.dumps 前 40 字符
    import json

    return _truncate(json.dumps(args, ensure_ascii=False, default=str), 40)


def _type_icon(event_type: str, data: dict) -> str:
    """per-type icon（spec §5.4 type_icon 列）。"""
    icons = {
        "agent_message":            "💬",
        "agent_thinking":           "🤔",
        "agent_tool_call":          "▶",
        "agent_tool_result":        "✓",
        "node_started":             "▶",
        "node_completed":           "✓",
        "node_failed":              "!",
        "node_skipped":             "⏭",
        "error":                    "!",
        "route_taken":              "→",
        "foreach_started":          "▶",
        "foreach_item_started":     "▸",
        "foreach_item_completed":   "✓",
        "foreach_completed":        "✓",
        "human_decision_requested": "⏸",
        "human_decision_resolved":  "✓",
        "interrupt_requested":      "⏸",
        "interrupt_resolved":       "✓",
        "retry_started":            "↻",
        "retry_succeeded":          "✓",
        "retry_exhausted":          "!",
        "wait_started":             "⏱",
        "wait_completed":           "✓",
        "validator_started":        "🔍",
        "validator_passed":         "✓",
        "validator_failed":         "!",
        "dialog_started":           "💬",
        "dialog_message":           "💬",
        "dialog_ended":             "✓",
        "workflow_started":         "🚀",
        "workflow_completed":       "✓",
        "workflow_failed":          "!",
        "workflow_cancelled":       "⏹",
        "workflow_resumed":         "↻",
    }
    return icons.get(event_type, "·")


def _build_summary_line(event_type: str, data: dict) -> str:
    """spec §5.4 title source 列：每个 EventType 一句话标题。

    文本类（message / thinking）按 spec §5.4 字段级定义：``data.text[:50].replace("\\n"," ")``
    （双行 entry 的 summary 行不能含换行，否则破坏排版）。
    """
    if event_type == "agent_message":
        return _truncate(data.get("text", "")).replace("\n", " ")
    if event_type == "agent_thinking":
        return _truncate(data.get("text", "")).replace("\n", " ")
    if event_type in ("agent_tool_call", "agent_tool_result"):
        tool = data.get("tool", "?")
        args = data.get("args", {}) or {}
        if event_type == "agent_tool_call":
            return f"{tool}  {_arg_title(tool, args)}"
        return f"{tool}  {_arg_title(tool, args)}"  # result 复用 call 的 title
    if event_type == "node_started":
        return f"node started (kind={data.get('kind', '?')})"
    if event_type == "node_completed":
        return f"node completed ({data.get('elapsed', '?')}s)"
    if event_type == "node_failed":
        return f"node FAILED: {data.get('message', data.get('error_type', '?'))}"
    if event_type == "node_skipped":
        return f"node skipped: {data.get('reason', '?')}"
    if event_type == "error":
        return f"error: {data.get('message', data.get('error_type', '?'))}"
    if event_type == "route_taken":
        return f"route: {data.get('from', '?')} → {data.get('to', '?')}"
    if event_type == "foreach_started":
        return (
            f"foreach: {data.get('item_count', '?')} items "
            f"· max_concurrent={data.get('max_concurrent', '?')}"
        )
    if event_type == "foreach_item_started":
        return f"item #{data.get('index', '?')}: {data.get('item_key', '?')}"
    if event_type == "foreach_item_completed":
        return f"item #{data.get('index', '?')} completed"
    if event_type == "foreach_completed":
        return f"foreach: {data.get('count', '?')}/{data.get('succeeded', '?')}"
    if event_type == "human_decision_requested":
        return f"gate: {_truncate(data.get('prompt', ''))}"
    if event_type == "human_decision_resolved":
        return f"gate resolved: {data.get('answer', '?')}"
    if event_type == "interrupt_requested":
        return f"interrupt requested at {data.get('node', '?')}"
    if event_type == "interrupt_resolved":
        return f"interrupt: {data.get('action', '?')}"
    if event_type == "retry_started":
        return f"retry #{data.get('attempt', '?')}/{data.get('max_attempts', '?')}"
    if event_type == "retry_succeeded":
        return f"retry succeeded (after {data.get('attempt_total', '?')} attempts)"
    if event_type == "retry_exhausted":
        return f"retry exhausted ({data.get('attempts', '?')})"
    if event_type == "wait_started":
        return f"wait {data.get('duration_seconds', 0):.1f}s"
    if event_type == "wait_completed":
        return f"wait completed ({data.get('elapsed_seconds', 0):.1f}s)"
    if event_type == "validator_started":
        return f"validator: {_truncate(data.get('criteria_preview', ''))}"
    if event_type == "validator_passed":
        return "validator passed"
    if event_type == "validator_failed":
        return f"validator failed: {_truncate((data.get('issues') or ['?'])[0])}"
    if event_type == "dialog_started":
        return "dialog started"
    if event_type == "dialog_message":
        return f"{data.get('role', '?')}: {_truncate(data.get('text', ''))}"
    if event_type == "dialog_ended":
        return f"dialog ended ({data.get('total_turns', 0)} turns)"
    if event_type == "workflow_started":
        return f"workflow started: {data.get('workflow_name', '?')}"
    if event_type == "workflow_completed":
        return f"workflow completed ({data.get('elapsed', '?')}s)"
    if event_type == "workflow_failed":
        return f"workflow FAILED: {data.get('error_type', '?')}"
    if event_type == "workflow_cancelled":
        return f"workflow cancelled: {data.get('reason', '?')}"
    if event_type == "workflow_resumed":
        return f"workflow resumed from {data.get('resumed_node', '?')}"
    if event_type == "custom":
        return f"custom({data.get('kind', '?')})"
    return event_type


def _build_meta_line(event_type: str, data: dict) -> str:
    """spec §5.4 meta source 列：每事件第 2 行元信息（可空）。"""
    if event_type == "agent_tool_call":
        return "running..."
    if event_type == "agent_tool_result":
        result = data.get("result", "")
        lines = str(result).count("\n") + 1 if result else 0
        exit_code = data.get("exit_code")
        elapsed = data.get("elapsed")
        parts = [f"{lines} lines"]
        if exit_code is not None:
            parts.append(f"exit {exit_code}")
        if elapsed is not None:
            parts.append(f"{elapsed}s")
        return " · ".join(parts)
    if event_type == "agent_message":
        text = data.get("text", "")
        lines = text.count("\n") + 1 if text else 0
        return f"{lines} lines markdown"
    if event_type == "agent_thinking":
        text = data.get("text", "")
        lines = text.count("\n") + 1 if text else 0
        return f"{lines} lines (dim)"
    if event_type in ("node_failed", "error"):
        return f"phase={data.get('phase', '?')}"
    if event_type == "interrupt_requested":
        return f"elapsed={data.get('elapsed_at_request', 0):.1f}s"
    if event_type == "interrupt_resolved":
        return f"by={data.get('resolved_by', '?')}"
    if event_type == "retry_started":
        return f"delay={data.get('delay_seconds', 0)}s"
    if event_type == "retry_exhausted":
        return f"last_error={data.get('last_error_type', '?')}"
    if event_type == "validator_failed":
        return f"retrying={data.get('retrying', False)}"
    if event_type == "wait_completed":
        return f"interrupted={data.get('interrupted', False)}"
    if event_type == "dialog_message":
        return f"turn={data.get('turn', '?')}"
    if event_type == "workflow_started":
        return f"node_count={data.get('node_count', '?')}"
    if event_type == "workflow_failed":
        return f"node={data.get('node', '?')}"
    if event_type == "workflow_resumed":
        return f"replayed={data.get('replayed_events', 0)}"
    if event_type == "human_decision_requested":
        return f"gate_id={data.get('gate_id', '?')}"
    return ""


def _build_detail_renderable(
    event_type: str,
    data: dict,
    *,
    executor: str = "claude",
) -> RenderableType | None:
    """spec §5.4 detail source 列：折叠块内容（None = 无折叠）。

    工具事件 → phase-15 ``render_tool(normalize_tool(...))``；
    agent_message → ``render_message``；agent_thinking → ``render_thinking``；
    失败类 → 完整 message（含 stack trace 若有）。
    """
    if event_type in ("agent_tool_call", "agent_tool_result"):
        try:
            if event_type == "agent_tool_call":
                item: RenderItem = normalize_tool(
                    executor=executor,
                    tool_name=str(data.get("tool", "")),
                    args=data.get("args", {}) or {},
                    result=None,
                    status="running",
                )
            else:
                result = data.get("result")
                result_str = result if isinstance(result, str) else (
                    "" if result is None else str(result)
                )
                item = normalize_tool(
                    executor=executor,
                    tool_name=str(data.get("tool", "")),
                    args=data.get("args", {}) or {},
                    result=result_str,
                    status="completed",
                )
            return render_tool(item)
        except NormalizeError:
            logger.warning("Activity Stream detail normalize 失败 type=%s", event_type)
            return None
    if event_type == "agent_message":
        return render_message(str(data.get("text", "")))
    if event_type == "agent_thinking":
        return render_thinking(str(data.get("text", "")))
    if event_type in ("node_failed", "error", "workflow_failed", "retry_exhausted",
                      "validator_failed"):
        msg = data.get("message") or data.get("error_type") or ""
        stack = data.get("stack") or data.get("traceback")
        if stack:
            return Static(f"{msg}\n\n{stack}")
        return Static(str(msg)) if msg else None
    if event_type == "human_decision_requested":
        prompt = data.get("prompt", "")
        options = data.get("options") or []
        opts_text = "\n".join(f"  - {o}" for o in options) if options else ""
        return Static(f"{prompt}\n{opts_text}") if opts_text else None
    return None


def build_entry(
    seq: int,
    event_type: str,
    data: dict,
    *,
    node: str | None = None,
    timestamp: float | None = None,
    executor: str = "claude",
) -> ActivityEntry | None:
    """从 Event 派生 ``ActivityEntry``（spec §5.4 字段级定义）。

    返 None = 该事件 visibility 是 ``hide_main`` / ``hide_all``（不进 Stream）。
    调用者（OrcaApp）应已经过 ``visibility_of`` 过滤；本函数做防御性二次过滤。
    """
    try:
        vis = visibility_of(event_type)
    except KeyError:
        # 未知 EventType（schema 加新 type 漏登记）：fail visible 走 show
        vis = "show"
    if vis in ("hide_main", "hide_all"):
        return None
    ts = timestamp if timestamp is not None else time.time()
    summary = _build_summary_line(event_type, data)
    meta = _build_meta_line(event_type, data)
    detail = _build_detail_renderable(event_type, data, executor=executor)
    return ActivityEntry(
        seq=seq,
        event_type=event_type,
        node=node,
        timestamp=ts,
        visibility=vis,
        summary_line=summary,
        meta_line=meta,
        detail_renderable=detail,
    )


# ── ActivityStream widget（双行 entry + 折叠详情）─────────────────────────


class ActivityStream(Static):
    """Activity Stream widget（spec §5）：双行 entry + 折叠详情 + filter 模式。

    用法（由 OrcaApp 驱动）::

        stream = app.query_one(ActivityStream)
        stream.set_executor("claude")           # normalize_tool 查表用
        stream.append_event(seq, etype, data, node=node, timestamp=ts)
        stream.set_filter_node("analyzer")      # filter 到选中节点（``f`` 键）
        stream.clear_filter()                   # 切回全事件
    """

    DEFAULT_CSS = """
    ActivityStream {
        width: 1fr;
        height: 1fr;
        border: round $success;
        padding: 0 1;
        background: $surface;
    }
    ActivityStream VerticalScroll {
        height: 1fr;
    }
    #activity-detail {
        height: auto;
        max-height: 50%;
        border-top: solid $accent;
        padding: 0 1;
        background: $panel;
    }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "下条", show=False),
        Binding("k", "cursor_up", "上条", show=False),
        Binding("tab", "toggle_expand", "展开/收起", show=False),
    ]

    def __init__(self) -> None:
        super().__init__("", id="activity-stream")
        # 全部 entry（按 seq 排序，reducer 派生 fold——重放同 tape 必产相同列表）
        self._entries: list[ActivityEntry] = []
        # 当前 executor（normalize_tool 查表用，OrcaApp 在 workflow_started 时 set）
        self._executor: str = "claude"
        # filter 模式（UI 交互态，spec §4.4.1）：None=all / str=仅该节点
        self._filter_node: str | None = None
        # 当前选中 entry seq（None=未选中）
        self._selected_seq: int | None = None
        # 折叠详情展开状态（UI 交互态）
        self._expanded: bool = False
        # 内部 RichLog 实例（compose 后挂载）
        self._log: RichLog | None = None
        self._detail_view: Static | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield RichLog(id="activity-log", markup=False, wrap=True, auto_scroll=True)
            yield Static("", id="activity-detail")

    def on_mount(self) -> None:
        self._log = self.query_one("#activity-log", RichLog)
        self._detail_view = self.query_one("#activity-detail", Static)

    # ── 配置 ──────────────────────────────────────────────────────────

    def set_executor(self, executor: str) -> None:
        """设置当前 backend（``claude`` / ``opencode`` / ``codex``）。"""
        self._executor = executor or "claude"

    @property
    def filter_node(self) -> str | None:
        """当前 filter 节点（None=all；测试断言用）。"""
        return self._filter_node

    @property
    def entries(self) -> list[ActivityEntry]:
        """entries 只读视图（测试用）。"""
        return list(self._entries)

    # ── 事件追加（由 app 调）─────────────────────────────────────────

    def append_event(
        self,
        seq: int,
        event_type: str,
        data: dict,
        *,
        node: str | None = None,
        timestamp: float | None = None,
    ) -> None:
        """追加一条事件（spec §5）。``hide_main`` / ``hide_all`` 不进 Stream。"""
        entry = build_entry(
            seq, event_type, data,
            node=node, timestamp=timestamp, executor=self._executor,
        )
        if entry is None:
            return  # visibility 过滤掉
        self._entries.append(entry)
        # 保持 seq 排序（防御：事件乱序到达，reducer 重放必产相同序列）
        if len(self._entries) >= 2 and self._entries[-2].seq > self._entries[-1].seq:
            self._entries.sort(key=lambda e: e.seq)
        # 仅当符合 filter 时才写入 log（避免双重渲染）
        if self._matches_filter(entry):
            self._write_entry(entry)

    def _matches_filter(self, entry: ActivityEntry) -> bool:
        """filter_mode=all → True；filter_mode=node → entry.node == filter_node。"""
        if self._filter_node is None:
            return True
        return entry.node == self._filter_node

    def _write_entry(self, entry: ActivityEntry) -> None:
        """写一条 entry 到 RichLog（双行 + 缩进）。"""
        if self._log is None:
            return
        ts_str = time.strftime("%H:%M:%S", time.localtime(entry.timestamp))
        node_str = entry.node or "-"
        sel = "▶" if entry.seq == self._selected_seq else " "
        # spec §5.3 双行 entry
        line1 = f"{sel}{ts_str}  {node_str:<14} {_type_icon(entry.event_type, {})}  {entry.summary_line}"
        self._log.write(line1)
        if entry.meta_line:
            line2 = f"  {'':18}    {entry.meta_line}"
            self._log.write(line2)

    # ── filter 模式（f 键）────────────────────────────────────────────

    def set_filter_node(self, node: str | None) -> None:
        """f 键：toggle filter 模式（None=全事件 / str=仅 node）。"""
        if self._filter_node == node:
            # 同 node 再按 f → 清 filter（toggle 语义）
            self._filter_node = None
        else:
            self._filter_node = node
        self._reflow()

    def clear_filter(self) -> None:
        """清 filter（切回全事件）。"""
        self._filter_node = None
        self._reflow()

    def _reflow(self) -> None:
        """清空 RichLog 重写全部 entries（filter 切换 / cursor 变更后）。"""
        if self._log is None:
            return
        self._log.clear()
        for entry in self._entries:
            if self._matches_filter(entry):
                self._write_entry(entry)
        self._refresh_detail()

    # ── 选中（j/k）+ 折叠详情 ─────────────────────────────────────────

    def _visible_entries(self) -> list[ActivityEntry]:
        """当前 filter 下可见的 entries（seq 排序）。"""
        return [e for e in self._entries if self._matches_filter(e)]

    def _select_entry(self, seq: int | None) -> None:
        """设选中 entry + 自动展开折叠详情（spec §5.5：当前选中 entry 自动展开）。"""
        self._selected_seq = seq
        self._expanded = seq is not None  # 选中 = 展开
        self._reflow()

    def action_cursor_down(self) -> None:
        entries = self._visible_entries()
        if not entries:
            return
        if self._selected_seq is None:
            self._select_entry(entries[0].seq)
            return
        # 找当前选中之后的第一个
        for i, e in enumerate(entries):
            if e.seq == self._selected_seq and i + 1 < len(entries):
                self._select_entry(entries[i + 1].seq)
                return

    def action_cursor_up(self) -> None:
        entries = self._visible_entries()
        if not entries:
            return
        if self._selected_seq is None:
            self._select_entry(entries[-1].seq)
            return
        for i, e in enumerate(entries):
            if e.seq == self._selected_seq and i > 0:
                self._select_entry(entries[i - 1].seq)
                return

    def action_toggle_expand(self) -> None:
        """Tab：切换折叠详情展开/收起（仅在选中状态下生效）。"""
        if self._selected_seq is None:
            return
        self._expanded = not self._expanded
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        """刷新底部 detail_view（选中 entry 的折叠详情）。"""
        if self._detail_view is None:
            return
        if not self._expanded or self._selected_seq is None:
            self._detail_view.update("")
            return
        # 找当前选中 entry
        entry = next(
            (e for e in self._entries if e.seq == self._selected_seq), None,
        )
        if entry is None or entry.detail_renderable is None:
            self._detail_view.update("(此事件无折叠详情)")
            return
        self._detail_view.update(entry.detail_renderable)

    # ── 兼容旧 LogStream API（fallback）──────────────────────────────

    def write(self, text: str) -> None:
        """兼容旧 LogStream.write（hint 行 / filter 占位提示等）。直接写 RichLog。"""
        if self._log is not None:
            self._log.write(text)


__all__ = ["ActivityStream", "ActivityEntry", "build_entry"]

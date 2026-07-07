"""log_stream.py —— v2 右下 30% Log Stream widget（spec §2.4，Conductor Log View 风格）。

回答「workflow 整体怎么走的？哪里出错了？」：高层节点事件流（**不是** ``agent_*``，
那些归 Agent History）。5 level icon 区分 info/success/error/warn/debug；
debug 默认隐藏（L 键 toggle）；``node_failed`` / ``error`` / ``workflow_failed`` /
``retry_exhausted`` 显示完整失败原因（**不截断**，reviewer P1-10）。

设计原则：
  - **壳无真相**：widget 只渲染注入的事件描述，不订阅 bus。
  - **高层事件过滤**：``EVENT_LEVEL`` 表（37 EventType → 5 level + None），
    ``agent_*`` / ``prompt_rendered`` / ``custom`` 显式 None 不进 Log Stream
    （归 Agent History / Header footer / ChartPanel 路径）。
  - **fail loud 完整性**：``EVENT_LEVEL`` 必须覆盖全 37 EventType（reviewer P0-1）；
    未登记 type → LEVEL_INFO 兜底（fail visible，spec §11.5 #6）。
  - **debug toggle**：默认隐藏 ``route_taken`` / ``foreach_*`` / ``wait_*`` /
    ``validator_started``/``validator_passed`` / ``dialog_*``；L 键 toggle 显示。
  - **node_failed 不截断**（reviewer P1-10）：完整 ``data.message`` 写入，
    Textual ``RichLog(wrap=True)`` 处理换行。
"""

from __future__ import annotations

import time
from typing import Any, Literal

from textual.binding import Binding
from textual.widgets import RichLog

# ── 5 level（spec §2.4 + Conductor 借鉴）──────────────────────────────

LEVEL_INFO = "info"        # ›  blue   — start / completed / route
LEVEL_SUCCESS = "success"  # ✓  green  — node_completed / workflow_completed / retry_succeeded
LEVEL_ERROR = "error"      # ✗  red    — node_failed / workflow_failed / retry_exhausted / error
LEVEL_WARN = "warn"        # ⚠  amber  — gate / interrupt / retry_started / validator_failed / skipped
LEVEL_DEBUG = "debug"      # ·  dim    — route_taken / foreach_* / wait_* / validator_started/passed / dialog_*

Level = Literal["info", "success", "error", "warn", "debug"]

_LEVEL_ICONS: dict[str, str] = {
    LEVEL_INFO: "›",
    LEVEL_SUCCESS: "✓",
    LEVEL_ERROR: "✗",
    LEVEL_WARN: "⚠",
    LEVEL_DEBUG: "·",
}

# 7 个事件显式 None：设计上不进 Log Stream（agent_history / header footer / chart 路径消费）
EVENTS_NOT_IN_LOG_STREAM = frozenset({
    "agent_message", "agent_thinking", "agent_tool_call", "agent_tool_result",
    "agent_usage", "prompt_rendered", "custom",
})

# spec §2.4：37 EventType → level | None
# reviewer P0-1：表内全 37 EventType，含 7 个显式 None（防 self-fail 完整性测试）
EVENT_LEVEL: dict[str, str | None] = {
    # ── workflow 生命周期 ──
    "workflow_started":            LEVEL_INFO,
    "workflow_completed":          LEVEL_SUCCESS,
    "workflow_failed":             LEVEL_ERROR,
    "workflow_cancelled":          LEVEL_WARN,
    "workflow_resumed":            LEVEL_INFO,
    # ── node 生命周期 ──
    "node_started":                LEVEL_INFO,
    "node_completed":              LEVEL_SUCCESS,
    "node_failed":                 LEVEL_ERROR,
    "node_skipped":                LEVEL_WARN,
    # ── HMIL gates / 中断 ──
    "human_decision_requested":    LEVEL_WARN,
    "human_decision_resolved":     LEVEL_INFO,
    "interrupt_requested":         LEVEL_WARN,
    "interrupt_resolved":          LEVEL_INFO,
    # ── Retry Policy ──
    "retry_started":               LEVEL_WARN,
    "retry_succeeded":             LEVEL_SUCCESS,
    "retry_exhausted":             LEVEL_ERROR,
    # ── Validator ──
    "validator_failed":            LEVEL_WARN,
    # ── 错误 ──
    "error":                       LEVEL_ERROR,
    # ── debug 默认隐藏（L 键 toggle）──
    "route_taken":                 LEVEL_DEBUG,
    "foreach_started":             LEVEL_DEBUG,
    "foreach_item_started":        LEVEL_DEBUG,
    "foreach_item_completed":      LEVEL_DEBUG,
    "foreach_completed":           LEVEL_DEBUG,
    "wait_started":                LEVEL_DEBUG,
    "wait_completed":              LEVEL_DEBUG,
    "validator_started":           LEVEL_DEBUG,
    "validator_passed":            LEVEL_DEBUG,
    "dialog_started":              LEVEL_DEBUG,
    "dialog_message":              LEVEL_DEBUG,
    "dialog_ended":                LEVEL_DEBUG,
    # ── 显式 None（设计上不进 Log Stream）──
    "agent_message":               None,
    "agent_thinking":              None,
    "agent_tool_call":             None,
    "agent_tool_result":           None,
    "agent_usage":                 None,
    "prompt_rendered":             None,
    "custom":                      None,
}


def level_of(event_type: str) -> str | None:
    """查 ``EVENT_LEVEL``；返回 level 字符串或 None（不进 Log Stream）。

    spec §11.5 #6 三态语义：
      - **合法 level**：表内已登记的 EventType（30 个），返回对应 level 字符串。
      - **None (explicit skip)**：``EVENTS_NOT_IN_LOG_STREAM`` 白名单 7 个
        （agent_* / prompt_rendered / custom），设计上归 Agent History / Header 路径。
      - **未登记 type**：返回 LEVEL_INFO 兜底（fail visible，让用户看到，不静默吞）。

    调用方（``LogStream.append_event`` / ``app._dispatch_to_widgets``）按返回值
    派发：None 跳过；DEBUG 默认隐藏；其他 level 写入。
    """
    if event_type in EVENTS_NOT_IN_LOG_STREAM:
        return None  # 显式 skip
    level = EVENT_LEVEL.get(event_type)
    if level is None:
        # 未登记 type（schema 加新 type 漏 mapping）→ LEVEL_INFO 兜底
        return LEVEL_INFO
    return level


def format_event(
    event_type: str,
    data: dict[str, Any],
    *,
    node: str | None = None,
    session_id: str | None = None,  # 保留接口签名兼容，v2 不再用（spec §2.4 改用 node）
    timestamp: float | None = None,
) -> str:
    """格式化事件为日志行（spec §2.4，Conductor 风格）。

    格式：``HH:MM:SS  {level_icon}  {node:<14}  {message}``

    message 派生按 event_type 分派（``_build_message``）：
      - ``node_failed`` / ``error`` / ``workflow_failed`` / ``retry_exhausted``：
        **不截断**（reviewer P1-10），完整 ``data.message`` 写入
      - 其他：截断到 80 字符（保持单行紧凑）

    timestamp=None 时用当前时间（测试可注入固定时间）。
    返回空串表示该事件不进 Log Stream（``level_of() is None``）。
    """
    level = level_of(event_type)
    if level is None:
        return ""  # 不进 Log Stream
    ts = time.localtime(timestamp) if timestamp is not None else time.localtime()
    hh_mm_ss = time.strftime("%H:%M:%S", ts)
    icon = _LEVEL_ICONS.get(level, "·")
    msg = _build_message(event_type, data, level=level)
    node_str = (node or "-")[:14].ljust(14)
    return f"{hh_mm_ss}  {icon}  {node_str}  {msg}"


# ── message 派生（spec §2.4 + reviewer P1-10 不截断）───────────────────

_TRUNCATE_LIMIT = 80  # 非 error/warn 路径的截断长度（保持单行紧凑）


def _truncate(s: Any, limit: int = _TRUNCATE_LIMIT) -> str:
    """字符串截断 + ``…``（log stream 单行紧凑）。"""
    text = str(s) if s is not None else ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _build_message(event_type: str, data: dict[str, Any], *, level: str) -> str:
    """spec §2.4 message 模板。

    error / warn 路径**不截断**（reviewer P1-10）：完整 ``data.message`` 写入，
    Textual ``RichLog(wrap=True)`` 处理换行。其他路径截断到 80 字符。
    """
    no_truncate = level in (LEVEL_ERROR, LEVEL_WARN)

    if event_type == "workflow_started":
        return f"workflow started: {data.get('workflow_name', '?')}"
    if event_type == "workflow_completed":
        return f"workflow completed ({data.get('elapsed', '?')}s)"
    if event_type == "workflow_failed":
        # error_type + 完整 message（reviewer P1-10）
        err_type = data.get("error_type", "?")
        msg = data.get("message", "")
        text = f"workflow FAILED: {err_type}"
        if msg:
            text += f" — {msg}"  # 完整不截
        return text
    if event_type == "workflow_cancelled":
        return f"workflow cancelled: {data.get('reason', '?')}"
    if event_type == "workflow_resumed":
        return (
            f"workflow resumed from {data.get('resumed_node', '?')} "
            f"(replayed {data.get('replayed_events', 0)} events)"
        )
    if event_type == "node_started":
        return f"node started (kind={data.get('kind', '?')})"
    if event_type == "node_completed":
        return f"node completed ({data.get('elapsed', '?')}s)"
    if event_type == "node_failed":
        # reviewer P1-10：完整 message 不截断
        msg = data.get("message", data.get("error_type", "?"))
        return f"node FAILED: {msg}"
    if event_type == "node_skipped":
        return f"node skipped: {data.get('reason', '?')}"
    if event_type == "human_decision_requested":
        prompt = data.get("prompt", "")
        prompt_text = prompt if no_truncate else _truncate(prompt)
        return f"gate: {prompt_text}"
    if event_type == "human_decision_resolved":
        return f"gate resolved: {data.get('answer', '?')}"
    if event_type == "interrupt_requested":
        return f"interrupt requested at {data.get('node', '?')}"
    if event_type == "interrupt_resolved":
        action = data.get("action", "?")
        guidance = data.get("guidance")
        text = f"interrupt: {action}"
        if guidance:
            guidance_text = guidance if no_truncate else _truncate(guidance)
            text += f": {guidance_text}"
        return text
    if event_type == "retry_started":
        return (
            f"retry #{data.get('attempt', '?')}/{data.get('max_attempts', '?')} "
            f"(delay {data.get('delay_seconds', 0):.1f}s)"
        )
    if event_type == "retry_succeeded":
        return f"retry succeeded after {data.get('attempt_total', '?')} attempts"
    if event_type == "retry_exhausted":
        # 完整 last_error_type 不截断（reviewer P1-10）
        return f"retry exhausted: {data.get('last_error_type', '?')}"
    if event_type == "validator_failed":
        issues = data.get("issues") or ["?"]
        first = issues[0] if issues else "?"
        first_text = first if no_truncate else _truncate(first)
        return f"validator failed: {first_text}"
    if event_type == "error":
        # 完整 message 不截断（reviewer P1-10）
        return f"error: {data.get('message', data.get('error_type', '?'))}"
    # ── debug 路径（默认隐藏，L 键 toggle）──
    if event_type == "route_taken":
        return f"route: {data.get('from', '?')} → {data.get('to', '?')}"
    if event_type == "foreach_started":
        return (
            f"foreach: {data.get('item_count', '?')} items "
            f"(concurrency={data.get('max_concurrent', '?')})"
        )
    if event_type == "foreach_item_started":
        return f"  item[{data.get('index', '?')}] started: {data.get('item_key', '?')}"
    if event_type == "foreach_item_completed":
        return f"  item[{data.get('index', '?')}] completed"
    if event_type == "foreach_completed":
        return f"foreach done: {data.get('count', '?')}/{data.get('succeeded', '?')}"
    if event_type == "wait_started":
        return f"wait {data.get('duration_seconds', 0):.1f}s"
    if event_type == "wait_completed":
        return f"wait done ({data.get('elapsed_seconds', 0):.1f}s)"
    if event_type == "validator_started":
        return f"validator: {_truncate(data.get('criteria_preview', ''))}"
    if event_type == "validator_passed":
        return "validator passed"
    if event_type == "dialog_started":
        return f"dialog started at {data.get('node', '?')}"
    if event_type == "dialog_message":
        return (
            f"{data.get('role', '?')} (turn {data.get('turn', '?')}): "
            f"{_truncate(data.get('text', ''))}"
        )
    if event_type == "dialog_ended":
        return f"dialog ended: {data.get('total_turns', 0)} turns"
    # 兜底（未登记 type，level_of 已 fail visible 给 info）
    return event_type


class LogStream(RichLog):
    """v2 右下高层事件日志流（spec §2.4，Conductor Log View 风格）。

    包装 Textual ``RichLog``：``append_event`` 把事件格式化为字符串后 ``write``。
    debug 事件（``route_taken`` / ``foreach_*`` / ``wait_*`` / ``validator_started`` /
    ``validator_passed`` / ``dialog_*``）默认隐藏，L 键 toggle 显示。
    """

    DEFAULT_CSS = """
    LogStream {
        width: 1fr;
        height: 3fr;
        border: round $warning;
        padding: 0 1;
        background: $surface;
    }
    """

    BINDINGS = [
        # spec v2 §2.4：L 键切 debug 显示。但 ``LogStream`` 是 ``RichLog`` 子类
        # （``can_focus=True`` 默认），自己拿焦点时 RichLog 会吞 ``L`` 字符。
        # 同时 widget 级 BINDINGS 优先级高于 App 级，会让本 widget 抢先处理。
        # 解决：widget BINDINGS 不绑 L，由 OrcaApp 级 BINDINGS 上提（spec §2.4）。
        # 单测通道保留：``test_log_stream.py`` 直接调 ``action_toggle_debug``。
    ]

    def __init__(self) -> None:
        # markup=False：日志行含字面量 ``[session]``，开 markup 会被 Rich 当样式标签吞掉。
        # wrap=True：node_failed 等长 message 自动换行（reviewer P1-10）。
        super().__init__(id="log-stream", markup=False, wrap=True, auto_scroll=True)
        self._show_debug: bool = False
        # phase-16 §5.1 行 L：debug 事件缓冲。``show_debug=False`` 时到达的 debug 事件
        # 暂存这里（formatted 字符串），用户按 L 开 debug 后**回放**已发生的 debug 事件——
        # 否则用户在 run 结束后按 L 只看到「debug log: ON」一行空提示，看不到任何历史
        # debug 事件（real-execution 发现的 UX gap，SPEC §5.1「前无后有」要求）。
        # FIFO 上限保护（防长 run 内存膨胀；与 _NODE_EVENTS_CAP 同语义）。
        self._debug_buffer: list[str] = []
        self._debug_buffer_cap: int = 500

    def append_event(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        node: str | None = None,
        session_id: str | None = None,
        timestamp: float | None = None,
    ) -> None:
        """格式化事件 + 写入日志流（spec §2.4）。

        - ``level is None`` 的事件（``agent_*`` / ``prompt_rendered`` / ``custom``）不写
          （归 Agent History / Header footer / ChartPanel 路径）。
        - debug 事件（``level == LEVEL_DEBUG``）默认不写但**缓冲**到 ``_debug_buffer``；
          L 键 toggle ON 时回放（phase-16 §5.1「前无后有」）。
        """
        level = level_of(event_type)
        if level is None:
            return  # 不进 Log Stream
        line = format_event(
            event_type, data, node=node, session_id=session_id, timestamp=timestamp,
        )
        if not line:
            return  # format_event 可能返回空（防御性）
        if level == LEVEL_DEBUG and not self._show_debug:
            # 缓冲：用户后续按 L 开 debug 时回放（phase-16 §5.1「前无后有」AC）
            self._debug_buffer.append(line)
            if len(self._debug_buffer) > self._debug_buffer_cap:
                self._debug_buffer.pop(0)  # FIFO 丢最旧
            return
        self.write(line)

    def action_toggle_debug(self) -> None:
        """L 键：toggle debug 事件显示（spec §2.4 + Binding ``action_*`` 约定）。"""
        self.toggle_debug()

    def toggle_debug(self) -> None:
        """Toggle debug 显示；写一行提示让用户感知当前状态。

        phase-16 §5.1：开 debug 时回放 ``_debug_buffer`` 中累积的历史 debug 事件
        （否则用户在 run 结束后按 L 只看到空提示，SPEC「前无后有」AC 要求实际内容）。
        """
        self._show_debug = not self._show_debug
        status = "ON" if self._show_debug else "OFF"
        self.write(f"── debug log: {status} ──")
        if self._show_debug and self._debug_buffer:
            # 回放历史 debug 事件（用户开 debug 前到达的）
            for buffered in self._debug_buffer:
                self.write(buffered)
            self._debug_buffer.clear()

    @property
    def show_debug(self) -> bool:
        """当前 debug 显示状态（测试用 + UI 反馈）。"""
        return self._show_debug

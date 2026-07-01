"""log_stream.py —— 右下滚动日志流（SPEC §4.3）。

回答「发生过什么？」：Textual 原生 ``RichLog`` widget，格式化事件为
``HH:MM:SS [session] <描述>`` 后写入，自动滚动。

设计原则：
  - **壳无真相**：widget 只渲染注入的事件描述，不订阅 bus、不存业务状态。
  - **格式可测**：``format_event`` 是纯函数，单测直接断言格式（SPEC §6.5）。
  - **session 截短**：完整 session_id 太长（uuid），取前 8 字符显示（仿 agent view）。
"""

from __future__ import annotations

import time
from typing import Any

from textual.widgets import RichLog

# 日志行里 session_id 的显示长度（uuid4 hex 截前 8 字符，足够区分且省空间）。
_SESSION_DISPLAY_LEN = 8


def format_event(event_type: str, data: dict[str, Any], *, node: str | None = None,
                 session_id: str | None = None, timestamp: float | None = None) -> str:
    """格式化事件为日志行（纯函数，SPEC §4.3）。

    格式：``HH:MM:SS [session_short] <描述>``

    描述按事件类型派生（agent_message/tool_call/node_*/gate 等）：
      - agent_message      → data["text"]
      - agent_thinking     → (thinking) data["text"]
      - agent_tool_call    → tool: data["tool"](<args 摘要>)
      - agent_tool_result  → → <result 摘要>
      - node_started       → node started (kind=<...>)
      - node_completed     → node completed (<elapsed>s)
      - node_failed        → node FAILED: <message>
      - human_decision_*   → gate <prompt 摘要>
      - 其他               → <event_type>

    timestamp=None 时用当前时间（测试可注入固定时间）。
    """
    ts = time.localtime(timestamp) if timestamp is not None else time.localtime()
    hh_mm_ss = time.strftime("%H:%M:%S", ts)
    short_session = _short_session(session_id) if session_id else "-"
    desc = _describe(event_type, data)
    if node:
        return f"{hh_mm_ss} [{short_session}] {node} · {desc}"
    return f"{hh_mm_ss} [{short_session}] {desc}"


def _short_session(session_id: str) -> str:
    return session_id[:_SESSION_DISPLAY_LEN]


def _truncate(s: Any, limit: int = 60) -> str:
    """字符串截短（带 …），用于日志行不挤爆宽度。"""
    text = str(s)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _describe(event_type: str, data: dict[str, Any]) -> str:
    """事件类型 → 日志描述（SPEC §4.3）。"""
    if event_type == "agent_message":
        return _truncate(data.get("text", ""))
    if event_type == "agent_thinking":
        return f"(thinking) {_truncate(data.get('text', ''))}"
    if event_type == "agent_tool_call":
        tool = data.get("tool", "?")
        args = data.get("args", {})
        return f"tool: {tool}({_truncate(args)})"
    if event_type == "agent_tool_result":
        result = data.get("result", "")
        return f"→ {_truncate(result)}"
    if event_type == "agent_usage":
        # token 摘要：in/out/cache + cost（SPEC §3.3 payload）。让用户在 LogStream 直接看消耗，
        # 不必去 tape 翻 jsonl。零值字段也显示（对比 cache hit/miss 更直观）。
        return (
            f"usage: in={data.get('input_tokens', 0)} out={data.get('output_tokens', 0)}"
            f" cache={data.get('cache_tokens', 0)} cost=${data.get('cost_usd', 0):.4f}"
        )
    if event_type == "node_started":
        return f"node started (kind={data.get('kind', '?')})"
    if event_type == "node_completed":
        return f"node completed ({data.get('elapsed', '?')}s)"
    if event_type == "node_failed":
        return f"node FAILED: {data.get('message', data.get('error_type', '?'))}"
    if event_type == "human_decision_requested":
        return f"gate: {_truncate(data.get('prompt', ''))}"
    if event_type == "human_decision_resolved":
        return f"gate resolved by {data.get('resolved_by', '?')}: {_truncate(data.get('answer', ''))}"
    if event_type == "workflow_started":
        return f"workflow started: {data.get('workflow_name', '?')}"
    if event_type == "workflow_completed":
        return "workflow completed"
    if event_type == "workflow_failed":
        return f"workflow FAILED: {data.get('error_type', '?')}"
    if event_type == "route_taken":
        return f"route: {data.get('from', '?')} → {data.get('to', '?')}"
    # phase 11 §3 / §4：中断 + Guidance 可观测事件。
    if event_type == "interrupt_requested":
        return (
            f"⏸ interrupt requested at {data.get('node', '?')} "
            f"({data.get('elapsed_at_request', 0):.1f}s)"
        )
    if event_type == "interrupt_resolved":
        action = data.get("action", "?")
        guidance = data.get("guidance")
        text = f"interrupt {action}"
        if guidance:
            text += f": {_truncate(guidance)}"
        return text
    if event_type == "prompt_rendered":
        # preview 是 prompt 末尾 ~200 字符（含 [User Guidance] 段时直观可见）。
        return f"prompt rendered: {_truncate(data.get('preview', ''), limit=80)}"
    if event_type == "workflow_resumed":
        # phase 11 §7：从 Tape 重放恢复后 emit。让用户看到「从哪个 node 续跑 + 重放了多少事件」。
        return (
            f"↻ resumed from {data.get('from_tape', '?')}: "
            f"node={data.get('resumed_node', '?')} "
            f"(replayed {data.get('replayed_events', 0)} events)"
        )
    # phase 11 §9.5.3：Retry Policy 可观测事件（让用户看到「第 N 次重试 / 重试后成功 / 用尽」）。
    if event_type == "retry_started":
        return (
            f"↻ retry #{data.get('attempt', '?')}/{data.get('max_attempts', '?')} "
            f"after {data.get('error_type', '?')} "
            f"(wait {data.get('delay_seconds', 0):.1f}s)"
        )
    if event_type == "retry_succeeded":
        return f"✓ retry succeeded after {data.get('attempt_total', '?')} attempt(s)"
    if event_type == "retry_exhausted":
        return (
            f"✗ retry exhausted after {data.get('attempts', '?')} attempt(s) "
            f"(last: {data.get('last_error_type', '?')})"
        )
    # phase 11 §9.7：Wait Node 可观测事件（让用户看到「wait 开始 + 时长 / wait 结束 + 是否被打断」）。
    if event_type == "wait_started":
        secs = data.get("duration_seconds", 0)
        reason = data.get("reason")
        text = f"⏳ wait {secs:.1f}s"
        if reason:
            text += f": {_truncate(reason)}"
        return text
    if event_type == "wait_completed":
        secs = data.get("elapsed_seconds", 0)
        interrupted = data.get("interrupted", False)
        suffix = " (interrupted)" if interrupted else ""
        return f"⏳ wait done{suffix} ({secs:.1f}s)"
    # phase 11 §9.6：Semantic Output Validator 可观测事件（让用户看到「校验开始 / 通过 / 失败+是否重试」）。
    if event_type == "validator_started":
        return f"🔍 validating: {_truncate(data.get('criteria_preview', ''))}"
    if event_type == "validator_passed":
        return "✓ validator passed"
    if event_type == "validator_failed":
        retrying = data.get("retrying", False)
        issues = data.get("issues", [])
        summary = "; ".join(str(i) for i in issues[:2])  # 最多 2 条，防爆宽
        if len(issues) > 2:
            summary += f" (+{len(issues) - 2} more)"
        suffix = " (retrying)" if retrying else " (exhausted)"
        return f"✗ validator failed{suffix}: {summary}"
    return event_type


class LogStream(RichLog):
    """滚动日志流 widget（SPEC §4.3）。

    包装 Textual ``RichLog``：``append_event`` 把事件格式化为字符串后 ``write``。
    ``RichLog`` 自带自动滚动 + 行缓冲 + 主题着色。
    """

    DEFAULT_CSS = """
    LogStream {
        width: 3fr;
        height: 1fr;
        border: round $success;
        padding: 0 1;
        background: $surface;
    }
    """

    def __init__(self) -> None:
        # markup=False：日志行含字面量 ``[session]``，开 markup 会被 Rich 当样式标签
        # 吞掉。关闭后 ``write`` 把字符串按原样渲染（[session] 字面保留）。
        super().__init__(id="log-stream", markup=False, wrap=True, auto_scroll=True)

    def append_event(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        node: str | None = None,
        session_id: str | None = None,
        timestamp: float | None = None,
    ) -> None:
        """格式化事件 + 写入日志流（SPEC §4.3）。"""
        self.write(
            format_event(
                event_type, data, node=node, session_id=session_id, timestamp=timestamp,
            )
        )

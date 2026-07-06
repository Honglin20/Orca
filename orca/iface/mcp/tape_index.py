"""tape_index.py —— tape 事件索引（SPEC phase-10 §5.10 / History 工具依赖）。

回答「``get_task_history`` 从哪读 task 的历史事件？」：``RunManager.get_run_events``
已经从 tape ``replay()`` 出全部事件（tape-only query path，§3.1）。本模块做一层
**摘要投影**：把原始 ``Event`` 列表折成 MCP 友好的 ``[{seq, type, node, summary, ts}]``。

设计约束：
  - 纯函数（tape → list[dict]），无 I/O，无状态。
  - 调用方（``server.tool_get_task_history``）从 ``RunManager.get_run_events`` 取 events，
    传给本函数做摘要。
  - ``_summary`` 字段：``agent_message`` / ``node_completed`` 等取核心字段（output 前 200 字）；
    ``node_failed`` 取 ``error.message``；其它 type 直返 ``{type, node}``。

依赖单向：本模块依赖 ``orca.schema.event``（Event 类型）。不依赖 run/exec/gates。
"""

from __future__ import annotations

from typing import Any

from orca.schema.event import Event


def summarize_events(events: list[Event], *, limit: int = 50) -> list[dict[str, Any]]:
    """把 ``Event`` 列表折成 MCP 友好的历史摘要（SPEC §2.4 / §3.1）。

    Args:
        events: ``tape.replay()`` 出的全部事件（调用方负责取）。
        limit: 最多返多少条（默认 50；超出取**最后** N 条——最近的更有意义）。

    每项字段：``{seq, type, node, summary, session_id?}``。``summary`` 是 type-specific
    摘要（output 前 200 字 / error message / gate prompt 等）。
    """
    items = [_summarize_one(e) for e in events]
    if len(items) > limit:
        items = items[-limit:]
    return items


def _summarize_one(event: Event) -> dict[str, Any]:
    """单个 Event → 摘要 dict（type-specific 抽取核心字段）。"""
    out: dict[str, Any] = {
        "seq": getattr(event, "seq", None),
        "type": event.type,
        "node": event.node,
    }
    data = event.data if isinstance(event.data, dict) else {}

    # 带 session_id 的事件透传（debugging 用）
    sid = data.get("session_id")
    if isinstance(sid, str):
        out["session_id"] = sid

    out["summary"] = _extract_summary(event.type, data)
    return out


def _extract_summary(event_type: str, data: dict) -> str:
    """按 EventType 抽取核心摘要（前 200 字，给主 session 快速扫描用）。"""
    # agent 输出
    if event_type == "agent_message":
        text = data.get("message") or data.get("text") or ""
        return _truncate(text, 200)
    if event_type == "node_completed":
        output = data.get("output")
        if isinstance(output, str):
            return _truncate(output, 200)
        if isinstance(output, dict):
            return _truncate(str(output), 200)
        return "node completed"
    if event_type == "node_failed":
        msg = data.get("message") or data.get("kind") or "node failed"
        return _truncate(str(msg), 200)
    if event_type == "workflow_completed":
        return "workflow completed"
    if event_type == "workflow_failed":
        msg = data.get("message") or "workflow failed"
        return _truncate(str(msg), 200)
    if event_type == "workflow_cancelled":
        reason = data.get("reason") or "cancelled"
        return f"cancelled: {reason}"
    if event_type == "workflow_started":
        return "workflow started"
    if event_type in ("retry_started", "retry_succeeded", "retry_exhausted"):
        return event_type.replace("_", " ")
    # 默认：type 即摘要
    return event_type


def _truncate(text: str, max_len: int) -> str:
    """截断 + 省略号（保前 N 字，超长加 ``…``）。"""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"

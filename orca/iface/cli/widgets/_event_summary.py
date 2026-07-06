"""_event_summary.py —— v2 共享事件派生纯函数（spec v2 §2.3 / §2.4）。

**目的**：v2 AgentHistory（Step 3）+ LogStream（Step 4）共享事件派生函数——
``AgentHistory`` 双行 entry（summary 行 + meta 行 + 折叠详情）；
``LogStream`` 高层节点事件（message 模板，部分需 elapsed / issue 等派生）。

**迁入历史**：6 个 module-level 纯函数源自从前 v1.1.1 ``activity_stream.py``
（commit `225933e`），v2 Step 1b（迁移并删该文件后）保留这些函数供两 widget
共享，避免重复实现（DRY）。

**frozen 性质**：纯函数 + 无全局状态，便于单测，不依赖 textual 事件循环。

**依赖单向**：仅 import ``orca.schema`` + ``rich`` + stdlib + 本包 ``tool_render``；
禁止 import ``orca.exec`` / ``orca.run`` / ``textual``（保持纯函数性质）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from rich.console import RenderableType
from textual.widgets import Static

from orca.iface.cli.widgets.tool_render import (
    NormalizeError,
    normalize_tool,
    render_tool,
)
from orca.iface.cli.widgets.tool_render.kinds import render_message, render_thinking
from orca.schema import RenderItem

logger = logging.getLogger(__name__)

# spec §5.4 字段级定义里 ``data.text[:50]`` 等。被 AgentHistory / LogStream 共享。
_TITLE_LIMIT = 50


# ── per-type entry 渲染（spec §5.4 字段级定义）─────────────────────────────


def _truncate(s: Any, limit: int = _TITLE_LIMIT) -> str:
    """字符串截断 + ``…``。"""
    text = str(s) if s is not None else ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_elapsed_sec(elapsed: float) -> str:
    """秒数格式化（``0.8s`` / ``12s`` / ``1m30s``）—— tool_result meta 用。

    精度比 node elapsed 更细（tool_result 普遍 < 1s，需保留 1 位小数；node elapsed
    普遍整秒）。
    """
    if elapsed < 0:
        return "0s"
    if elapsed < 60:
        # < 10s 显 1 位小数（0.8s）；否则整数（避免 12.345s 噪音）
        return f"{elapsed:.1f}s" if elapsed < 10 else f"{elapsed:.0f}s"
    minutes = int(elapsed // 60)
    secs = elapsed - minutes * 60
    return f"{minutes}m{secs:.0f}s"


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
    return _truncate(json.dumps(args, ensure_ascii=False, default=str), 40)


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
    """spec §5.4 meta source 列：每事件第 2 行元信息（可空）。

    spec v1.1.1 GAP-C 修订：tool_result meta 主路径为 ``<N> lines · <elapsed>s``。
    ``elapsed`` 从 ``agent_tool_call.timestamp`` + ``agent_tool_result.timestamp``
    派生（顶层 Event 字段，spec §3）。``exit_code`` 是 canonical Event 不支持字段
    （spec §11 裁决 12.8 不动 schema），故主路径不显示；若未来 translator 补了
    exit_code（如 codex shell tool），则追加 ``· exit <code>``。
    """
    if event_type == "agent_tool_call":
        return "running..."
    if event_type == "agent_tool_result":
        result = data.get("result", "")
        lines = str(result).count("\n") + 1 if result else 0
        exit_code = data.get("exit_code")
        elapsed = data.get("elapsed")
        parts = [f"{lines} lines"]
        if elapsed is not None:
            parts.append(f"{_format_elapsed_sec(elapsed)}")
        if exit_code is not None:
            parts.append(f"exit {exit_code}")
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
            logger.warning("detail normalize 失败 type=%s", event_type)
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


__all__ = [
    "_truncate",
    "_format_elapsed_sec",
    "_arg_title",
    "_build_summary_line",
    "_build_meta_line",
    "_build_detail_renderable",
]

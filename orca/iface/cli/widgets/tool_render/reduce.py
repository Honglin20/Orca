"""tool_render/reduce.py —— RenderState + Event 流累积 reducer（render-layer §9）。

回答「流式事件如何累积成可渲染的状态？」：``RenderState`` 是 widget 内存态，
**不持久化、不进 tape**（§3.2 唯一真相源链 + §3.3 派生投影纯函数性）。

reducer 规则（§9.2 事件处理表）：
  - agent_message      → messages[key] += text
  - agent_thinking     → thinking[key] += text（thinking_visible=False 仍累积，保可重建性）
  - agent_tool_call    → normalize(status=running) → tool_cards[id]
  - agent_tool_result  → 取 tool_cards[id]，重新 normalize(status=completed) 覆盖
  - agent_usage        → 不归 RenderState（footer 由 widget 自取 last 直接渲染）
  - error              → 标记对应 tool_card 状态 error

排序（§9.3 / §12.11）：``order`` 三元组 ``(seq, kind, key)`` 按 seq 单调递增（Tape 不变量）。

依赖单向：仅 ``orca.schema`` + stdlib + ``.normalize``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from orca.schema import Event, RenderItem

from orca.iface.cli.widgets.tool_render.normalize import normalize_tool

logger = logging.getLogger(__name__)

# 累积键：``session_id|node``（thinking/message 按 session+node 分组，spec §9.1）。
# node=None（workflow 级事件，不应到 agent_*）兜底 "" 防 KeyError。
_OrderKind = Literal["message", "thinking", "tool"]


def _msg_key(event: Event) -> str:
    """session_id|node 复合 key（spec §9.1）。

    None 字段兜底空串（agent 流式事件按 phase-3 SPEC 必带 session_id + node，
    防御性兜底防 KeyError）。
    """
    return f"{event.session_id or ''}|{event.node or ''}"


@dataclass
class RenderState:
    """widget 内部累积态（render layer reducer 输出，§9.1）。

    字段：
      messages: session|node → 累积 message 文本（agent_message 流式拼接）
      thinking: session|node → 累积 thinking 文本
      thinking_visible: ``/thinking`` 命令切换的全局可见性（默认 True）
      tool_cards: tool_call_id → RenderItem（call 创建 running，result 覆盖 completed）
      order: ``[(seq, kind, key), ...]`` 三元组按 seq 单调（Tape 不变量保证全序）
      errored_tools: tool_call_id set（error 事件标记；渲染时变红边框）
    """

    messages: dict[str, str] = field(default_factory=dict)
    thinking: dict[str, str] = field(default_factory=dict)
    thinking_visible: bool = True
    tool_cards: dict[str, RenderItem] = field(default_factory=dict)
    order: list[tuple[int, _OrderKind, str]] = field(default_factory=list)
    errored_tools: set[str] = field(default_factory=set)

    # ── query helpers（widget 渲染用）────────────────────────────────────────

    def ordered_entries(self) -> list[tuple[int, _OrderKind, str]]:
        """按 seq 排序后的 entries（Tape seq 全序保证；spec §12.11 acceptance）。

        Tape 不变量：seq 单调递增（phase-3 SPEC §3.2 Lock 覆盖），故 ``sorted`` 即可，
        无需稳定排序兜底。返回新 list（不改 self.order）。
        """
        return sorted(self.order, key=lambda x: x[0])


# ── reducer ─────────────────────────────────────────────────────────────────


def reduce_event(state: RenderState, event: Event, *, executor: str) -> RenderState:
    """ ``(state, event) → state`` 纯函数 reducer（§9.4 幂等性）。

    给定相同 Event 序列必产相同 RenderState（与 phase-3 SPEC reducer 模式一致）。

    executor 透传给 normalizer（查 §6.1 表用）；reducer 自身不感知 backend。

    实现策略：直接 mutate state 并返回（reducer 调用方约定不共享 state 实例）。
    """
    etype = event.type
    data = event.data or {}
    seq = event.seq

    if etype == "agent_message":
        key = _msg_key(event)
        text = str(data.get("text", ""))
        state.messages[key] = state.messages.get(key, "") + text
        state.order.append((seq, "message", key))
        return state

    if etype == "agent_thinking":
        key = _msg_key(event)
        text = str(data.get("text", ""))
        # thinking_visible=False 时**仍累积**（保可重建性：切换可见性后立即出现）
        state.thinking[key] = state.thinking.get(key, "") + text
        state.order.append((seq, "thinking", key))
        return state

    if etype == "agent_tool_call":
        tool = str(data.get("tool", ""))
        args = data.get("args", {})
        tool_call_id = str(data.get("tool_call_id", ""))
        item = normalize_tool(
            executor=executor,
            tool_name=tool,
            args=args,
            result=None,
            status="running",
        )
        state.tool_cards[tool_call_id] = item
        state.order.append((seq, "tool", tool_call_id))
        return state

    if etype == "agent_tool_result":
        tool_call_id = str(data.get("tool_call_id", ""))
        result = data.get("result")
        result_str = result if isinstance(result, str) else ("" if result is None else str(result))
        existing = state.tool_cards.get(tool_call_id)
        if existing is None:
            # tool_result 无对应 tool_call（不应到，但防御：translator 漏 call 时跳过，
            # 不静默丢——记 logger.warning 让上游暴露问题）。
            logger.warning(
                "tool_result 无对应 tool_call（tool_call_id=%s）；translator 漏 call？跳过",
                tool_call_id,
            )
            return state
        # 重新 normalize 覆盖（status=completed；result 填充 payload）。
        # tool_name / args 从 existing.raw 取（normalizer 把它们存进 raw，§5.1）。
        item = normalize_tool(
            executor=executor,
            tool_name=str(existing.raw.get("tool_name", "")),
            args=existing.raw.get("args", {}) if isinstance(existing.raw.get("args"), dict) else {},
            result=result_str,
            status="completed",
        )
        state.tool_cards[tool_call_id] = item
        # order 不变（位置由 call 时的 seq 决定，§9.2）
        return state

    if etype == "error":
        # error 事件带 tool_call_id 时标记对应 tool_card（v1 简单策略：标 errored_tools）
        tool_call_id = str(data.get("tool_call_id", ""))
        if tool_call_id and tool_call_id in state.tool_cards:
            state.errored_tools.add(tool_call_id)
        return state

    # 其他事件类型（agent_usage / node_* / foreach_* 等）不归 RenderState
    return state


__all__ = ["RenderState", "reduce_event"]

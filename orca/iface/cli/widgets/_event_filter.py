"""_event_filter.py —— TUI 事件可见性集中表（v2 §11.1 + §2.4）。

回答「这个 EventType 在 TUI 怎么显示？」：v2 三块布局的分派规则。

v2 语义重映射（spec §11.1）：
  - ``show`` / ``show_dim`` → Agent History（业务核心：message/tool_call/tool_result/...）
  - ``show_compact`` / ``show_warn`` / ``show_error`` → Log Stream（高层节点事件）
  - ``hide_main`` → 不进主流，写到 Header footer（agent_usage 收敛到 per-node；
    ``custom`` 走 NodeDetail ChartPanel 路径）
  - ``hide_all`` → 完全不显示，仅写 tape（prompt_rendered 调试事件）

设计原则：
  - **fail loud**：表必须覆盖全部 37 EventType（``test_event_visibility_completeness``
    守门）；schema 加新 type 漏 mapping 时测试立即 fail。
  - **依赖单向**：本模块只 import ``typing``（Literal），无 textual/rich 依赖（纯数据）。
  - **OCP**：消费者按 tag 派发（``if vis == "show"``）；新增 tag 改消费者单点，不动表。

取舍：``EVENT_VISIBILITY`` 是 dict 而非 Enum。理由：消费者只判字符串等价（``vis == "show"``），
Enum 反而多一层 ``.value``；dict + Literal 是 SPEC §6.4 显式契约（逐字）。
"""

from __future__ import annotations

from typing import Literal

# 7 个 visibility tag（消费者派发用；新增 tag 必须改全部消费者）。
Visibility = Literal[
    "show",          # Agent History 主流双行 entry（业务核心）
    "show_dim",      # Agent History 主流 dim 行（thinking）
    "show_compact",  # Log Stream 主流单行（node/route/foreach 等结构性事件）
    "show_warn",     # Log Stream + Amber 强调（gate/interrupt/retry）
    "show_error",    # Log Stream + Red 强调（error/failed/exhausted）
    "hide_main",     # 不进主流，写到 Header footer（agent_usage / custom chart）
    "hide_all",      # 完全不显示，仅写 tape（prompt_rendered）
]

# 全 37 EventType → visibility tag（spec §11.1 v2 重映射）。
# 新增 EventType 必须在此登记（否则 ``test_event_visibility_completeness`` fail loud）。
EVENT_VISIBILITY: dict[str, Visibility] = {
    # ── workflow 生命周期（→ Log Stream）──
    "workflow_started":            "show_compact",
    "workflow_completed":          "show_compact",
    "workflow_failed":             "show_error",
    "workflow_cancelled":          "show_compact",
    "workflow_resumed":            "show_compact",
    # ── node 生命周期（→ Log Stream）──
    "node_started":                "show_compact",
    "node_completed":              "show_compact",
    "node_failed":                 "show_error",
    "node_skipped":                "show_compact",
    # ── agent 流式（→ Agent History）──
    "agent_message":               "show",
    "agent_thinking":              "show_dim",
    "agent_tool_call":             "show",
    "agent_tool_result":           "show",
    "agent_usage":                 "hide_main",      # 收敛到 Header footer
    # ── 路由（→ Log Stream debug）──
    "route_taken":                 "show_compact",
    # ── 并发（→ Log Stream debug）──
    "foreach_started":             "show_compact",
    "foreach_item_started":        "show_compact",
    "foreach_item_completed":      "show_compact",
    "foreach_completed":           "show_compact",
    # ── HMIL gates（→ Log Stream warn）──
    "human_decision_requested":    "show_warn",
    "human_decision_resolved":     "show_compact",
    # ── 中断（→ Log Stream warn）──
    "interrupt_requested":         "show_warn",
    "interrupt_resolved":          "show_compact",
    # ── prompt 调试（→ 仅 tape）──
    "prompt_rendered":             "hide_all",
    # ── Retry Policy（→ Log Stream warn/error）──
    "retry_started":               "show_warn",
    "retry_succeeded":             "show_compact",
    "retry_exhausted":             "show_error",
    # ── Wait Node（→ Log Stream debug）──
    "wait_started":                "show_compact",
    "wait_completed":              "show_compact",
    # ── Validator（→ Log Stream debug + warn）──
    "validator_started":           "show_compact",
    "validator_passed":            "show_compact",
    "validator_failed":            "show_warn",  # spec §2.4: warn（与 EVENT_LEVEL 一致）
    # ── Dialog（→ Log Stream debug）──
    "dialog_started":              "show_compact",
    "dialog_message":              "show_compact",
    "dialog_ended":                "show_compact",
    # ── 自定义（chart 等）── 走 ChartPanel 路径，不进 Log Stream / Agent History
    "custom":                      "hide_main",
    # ── 错误（→ Log Stream error）──
    "error":                       "show_error",
}


def visibility_of(event_type: str) -> Visibility:
    """查 ``EVENT_VISIBILITY``；未登记抛 ``KeyError``（fail loud，不静默兜底）。

    消费者（AgentHistory / LogStream / Header footer）应优先用本函数而非直接 dict index，
    便于未来加默认值/告警等横切逻辑（DRY）。
    """
    return EVENT_VISIBILITY[event_type]


# 便利谓词（消费者按 tag 派发用）──────────────────────────────────────────


def goes_to_agent_history(event_type: str) -> bool:
    """是否进 Agent History（``show`` / ``show_dim``）。

    业务核心流式事件：``agent_message`` / ``agent_thinking`` / ``agent_tool_call`` /
    ``agent_tool_result``。
    """
    return EVENT_VISIBILITY.get(event_type) in ("show", "show_dim")


def goes_to_log_stream(event_type: str) -> bool:
    """是否进 Log Stream（``show_compact`` / ``show_warn`` / ``show_error``）。

    高层节点事件：``node_*`` / ``workflow_*`` / ``route_taken`` / ``foreach_*`` /
    ``human_decision_*`` / ``interrupt_*`` / ``retry_*`` / ``wait_*`` /
    ``validator_*`` / ``dialog_*``。

    调用方仍需查 ``log_stream.EVENT_LEVEL`` 决定具体 level（debug 默认隐藏）。
    """
    return EVENT_VISIBILITY.get(event_type) in ("show_compact", "show_warn", "show_error")


def goes_to_header_footer(event_type: str) -> bool:
    """是否进 Header footer（``hide_main``，如 ``agent_usage`` / ``custom`` chart）。"""
    return EVENT_VISIBILITY.get(event_type) == "hide_main"


__all__ = [
    "EVENT_VISIBILITY",
    "Visibility",
    "visibility_of",
    "goes_to_agent_history",
    "goes_to_log_stream",
    "goes_to_header_footer",
]

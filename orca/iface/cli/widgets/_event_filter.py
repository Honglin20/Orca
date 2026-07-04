"""_event_filter.py —— TUI 事件可见性集中表（tui-redesign-draft v1.1 §6.4）。

回答「这个 EventType 在 TUI 怎么显示？」：5 个 visibility tag 集中映射，消费者按
tag 派发（避免每个 widget 重复 if-else 同一份分类逻辑，DRY）。

5 个 visibility tag（spec §6.4）：
  - ``show``：Activity Stream 主流双行 entry（业务核心：message/tool_call/tool_result/...）
  - ``show_dim``：Activity Stream 主流 dim 行（thinking，弱化但仍可见）
  - ``show_compact``：Activity Stream 主流单行（node/route/foreach 等结构性事件）
  - ``show_warn``：主流 + DAG 节点框双重显示（warn=黄，gate/interrupt/retry）
  - ``show_error``：主流 + DAG 节点框双重显示（error=红，error/failed/exhausted）
  - ``hide_main``：不进 Activity Stream 主流，但写到 Header footer（agent_usage 收敛到 per-node）
  - ``hide_all``：完全不显示，仅写 tape（prompt_rendered 调试事件）

设计原则：
  - **fail loud**：表必须覆盖全部 32 EventType（``test_event_visibility_completeness`` 守门）；
    schema 加新 type 漏 mapping 时测试立即 fail。
  - **依赖单向**：本模块只 import ``orca.schema.event.EventType``（只读 Literal 元信息），
    无 textual/rich 依赖（纯数据）。
  - **OCP**：消费者按 tag 派发（``if vis == "show"``），新增 tag 改消费者单点，不动表。

取舍：``EVENT_VISIBILITY`` 是 dict 而非 Enum。理由：消费者只判字符串等价（``vis == "show"``），
Enum 反而多一层 ``.value``；dict + Literal 是 SPEC §6.4 显式契约（逐字）。
"""

from __future__ import annotations

from typing import Literal

# 7 个 visibility tag（消费者派发用；新增 tag 必须改全部消费者）。
Visibility = Literal[
    "show",
    "show_dim",
    "show_compact",
    "show_warn",
    "show_error",
    "hide_main",
    "hide_all",
]

# 全 32 EventType → visibility tag（spec §6.4 字段级定义）。
# 新增 EventType 必须在此登记（否则 ``test_event_visibility_completeness`` fail loud）。
EVENT_VISIBILITY: dict[str, Visibility] = {
    # ── workflow 生命周期 ──
    "workflow_started":         "show_compact",
    "workflow_completed":       "show_compact",
    "workflow_failed":          "show_error",
    "workflow_cancelled":       "show_compact",
    "workflow_resumed":         "show_compact",
    # ── node 生命周期 ──
    "node_started":             "show_compact",
    "node_completed":           "show_compact",
    "node_failed":              "show_error",
    "node_skipped":             "show_compact",
    # ── agent 流式 ──
    "agent_message":            "show",
    "agent_thinking":           "show_dim",
    "agent_tool_call":          "show",
    "agent_tool_result":        "show",
    "agent_usage":              "hide_main",      # spec §6.2：收敛到 Header footer
    # ── 路由 ──
    "route_taken":              "show_compact",
    # ── 并发 ──
    "foreach_started":          "show_compact",
    "foreach_item_started":     "show_compact",
    "foreach_item_completed":   "show_compact",
    "foreach_completed":        "show_compact",
    # ── HMIL gates ──
    "human_decision_requested": "show_warn",
    "human_decision_resolved":  "show_compact",
    # ── 中断 ──
    "interrupt_requested":      "show_warn",
    "interrupt_resolved":       "show_compact",
    # ── prompt 调试 ──
    "prompt_rendered":          "hide_all",       # spec §6.1：仅 tape，TUI 不显示
    # ── Retry Policy ──
    "retry_started":            "show_warn",
    "retry_succeeded":          "show_compact",
    "retry_exhausted":          "show_error",
    # ── Wait Node ──
    "wait_started":             "show_compact",
    "wait_completed":           "show_compact",
    # ── Validator ──
    "validator_started":        "show_compact",
    "validator_passed":         "show_compact",
    "validator_failed":         "show_error",
    # ── Dialog ──
    "dialog_started":           "show_compact",
    "dialog_message":           "show",
    "dialog_ended":             "show_compact",
    # ── 自定义（chart 等）──
    "custom":                   "show",           # 按 data.kind 分发渲染（chart 入 ChartPanel）
    # ── 错误 ──
    "error":                    "show_error",
}


def visibility_of(event_type: str) -> Visibility:
    """查 ``EVENT_VISIBILITY``；未登记抛 ``KeyError``（fail loud，不静默兜底）。

    消费者（Activity Stream / Header footer / DAG）应优先用本函数而非直接 dict index，
    便于未来加默认值/告警等横切逻辑（DRY）。
    """
    return EVENT_VISIBILITY[event_type]


__all__ = ["EVENT_VISIBILITY", "Visibility", "visibility_of"]

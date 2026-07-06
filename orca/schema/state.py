"""state.py —— 运行时状态（event tape 的派生视图）。

回答「现在到哪了？」：RunState / Status / UsageSummary。

关键定位（SPEC §4.1）：RunState 不是另一份真相，而是编排器运行时的内存状态，
是 event tape 的派生物——任何时刻都能从 tape replay 重建。这个区分是避免
「两份状态不一致」的根本。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

# 单个 node 的状态（注意：用 "done"，与 RunState.status 的 "completed" 区分，SPEC §4.2 有意为之）。
#
# ``blocked``（ADR §4.3 / 接口收敛 v2 §4.3）：reducer fold 派生态，不入 tape。派生条件 =
# 该 node 当前 ``running`` 且有未 resolved 的 ``human_decision_requested`` /
# ``interrupt_requested`` 事件。``projections.node_status`` 与 ``apply_event`` 同源派生
# （P4：消费层不许自造 blocked 字符串）。
Status = Literal["pending", "running", "done", "failed", "skipped", "blocked"]


class UsageSummary(BaseModel):
    """token / 成本用量汇总。

    node_breakdown 为每 node 的用量（递归自引用），支持按 node 下钻。
    """

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    cost_usd: float = 0.0
    node_breakdown: dict[str, "UsageSummary"] = {}  # 每 node 的 usage（递归）


# 解析 node_breakdown 的自引用前向引用（fail loud / clean，避免运行时 rebuild warning）。
UsageSummary.model_rebuild()


class RunState(BaseModel):
    """编排器运行时内存状态（tape 的派生视图，非真相源）。

    status 为 workflow 级状态（"completed"）；node_status 为每 node 状态（用 "done"）。
    context 累积所有已完成 node 的输出。
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_name: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"] = "pending"
    current_node: str | None = None
    node_status: dict[str, Status] = {}  # 每个 node 的状态
    context: dict[str, Any] = {}  # 所有已完成 node 的输出（accumulate）
    usage: UsageSummary | None = None

"""event.py —— 事件契约（唯一真相源的元素）。

回答「产出了什么？」：Event / EventType。

事件 tape 是 Orca 的唯一真相源；RunState 是 tape 的派生物（见 state.py）。
本模块只定义事件数据结构，零逻辑：EventBus + tape 持久化在 events/ 阶段做。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

# 事件类型全集（Literal 联合体，非 Enum：更兼容 pydantic + IDE，typo 编译期捕获）。
# 每个 type 旁注释其 data payload 字段。新增类型需改此定义（SPEC §3.4 权衡：可接受的小代价）。
#
# 身份维度（顶层字段，非 data）：node = DAG 步骤；session_id = 一次 agent 调用（独立 context）。
# retry / for_each / parallel 每次调用都产生新 session_id；attempt（第几次重试）reducer 派生，不入库。
EventType = Literal[
    # ── workflow 生命周期（node=None, session_id=None）──
    "workflow_started",  # data: {inputs, node_count, entry}
    "workflow_completed",  # data: {elapsed, outputs}
    "workflow_failed",  # data: {error_type, message, node}  # node=导致失败的 node（payload）
    # ── node 生命周期（顶层 node + session_id 标识本次调用；attempt 派生）──
    "node_started",  # 本次调用开始（顶层 node + session_id 标识）
    "node_completed",  # data: {elapsed, output}
    "node_failed",  # data: {error_type, message}
    "node_skipped",  # data: {reason}
    # ── agent 流式（claude stream-json 翻译产出；均带 session_id）──
    "agent_message",  # data: {text}
    "agent_thinking",  # data: {text}
    "agent_tool_call",  # data: {tool, args, tool_call_id}
    "agent_tool_result",  # data: {tool_call_id, result}
    "agent_usage",  # data: {input_tokens, output_tokens, cache_tokens, cost_usd}
    # ── 路由 ──
    "route_taken",  # data: {from, to}
    # ── 并发 ──
    "foreach_started",  # data: {item_count, max_concurrent}
    "foreach_item_started",  # data: {index, item_key}
    "foreach_item_completed",  # data: {index, output}
    "foreach_completed",  # data: {count, succeeded}
    # ── HMIL（gates extension 产出；核心只认这个事件，不认 gate 实体）──
    "human_decision_requested",  # data: {gate_id, prompt, options?, source, context}
    "human_decision_resolved",  # data: {gate_id, answer}
    # ── 自定义（MCP 工具产出，前端按 data.kind 分发渲染）──
    "custom",  # data: {kind: "chart"|"table"|"image"|..., ...}
    # ── 错误 ──
    "error",  # data: {error_type, message, phase?}
]


class Event(BaseModel):
    """单个事件。seq 全局单调递增（不变量）；timestamp 为 epoch 秒。

    type 决定 data 的 payload 结构（见 EventType 注释）。data 为自由 dict，
    schema 层不校验各 type 的 payload 字段（由产出方约定）。

    身份维度（顶层）：node = DAG 步骤；session_id = 一次 agent 调用（独立 context）。
    workflow 级事件两者皆 None；agent 流式事件两者皆有。详见 phase-3 SPEC 身份模型。
    """

    model_config = ConfigDict(extra="forbid")

    seq: int  # 单调递增序号（全局唯一递增）
    type: EventType
    timestamp: float  # epoch 秒
    node: str | None = None  # 哪个 node 产出；workflow 级为 None
    session_id: str | None = None  # 哪次 agent 调用（独立 context）；workflow/node 级生命周期可为 None
    data: dict = {}  # 各 type 特定 payload

"""replay.py —— 从 Tape 重建 RunState（纯 reducer fold）。

回答「如何从事件重建状态？」：``replay_state`` 把 tape fold 出 RunState，``apply_event``
是单一 reducer。**一条读路径**：live 消费和 replay 走同一个 ``apply_event``（反模式④）。

幂等硬约束（反模式③，SPEC §3.4 / §11 决策 4、8）：
  - streaming text 用 ``text@seq``（keyed by seq，last-writer-wins），**绝不字符串拼接**。
    一旦 reducer 幂等，所有 dedup set / watermark / per-node cursor 都是死代码。
  - 同一事件应用 N 次 = 应用 1 次（有测试覆盖）。
  - **分歧 = 错误**：如果 replay 产出的 state 和 live 不一致应 raise（SPEC §3.4 规则 8）。

session_id 与 RunState（SPEC §3.4）：
  - reducer 对同 node 多 session 事件，``node_status``/``context`` 取最后写入
    （last-writer-wins = 该 node 最终状态/输出）。
  - session 级流式细节（message/thinking text）不进 RunState，留给前端 reducer 按
    ``session_id`` 分组（phase 6）。``session_id`` 在事件顶层保留，replay 不丢失。

依赖单向：本模块依赖 ``orca.schema``（Event/RunState）+ ``orca.events.tape``（Tape）。
"""

from __future__ import annotations

import logging

from orca.schema import Event, RunState

from orca.events.tape import Tape

logger = logging.getLogger(__name__)


def replay_state(tape: Tape, since_seq: int = 0) -> RunState:
    """从 tape 重放事件，fold 出 RunState。纯函数，重放两次结果相同（规则 8）。

    ``since_seq`` 起始 seq（不含本身），用于增量重放。
    """
    state = RunState(run_id=tape.run_id, workflow_name="", status="pending")
    for event in tape.replay(since_seq):
        state = apply_event(state, event)
    return state


def apply_event(state: RunState, event: Event) -> RunState:
    """单一 reducer。幂等：同一事件应用两次 = 一次（SPEC §3.4）。

    每个 EventType 一个分支。streaming text（agent_message/agent_thinking）用
    ``text@seq`` keyed by seq（last-writer-wins），绝不字符串拼接。

    所有分支都是**对 state 字段的覆盖语义**（last-writer-wins），不是累加 —— 这是幂等的根。
    对 node_status / context：同 node 多 session 取最后写入（该 node 最终状态/输出）。

    实现：``RunState.model_copy(update={...})`` 返回新实例（reducer 纯函数语义）。
    """
    t = event.type
    node = event.node
    data = event.data

    if t == "workflow_started":
        # workflow_name 来自 payload 的 workflow_name 字段（**非 entry** —— entry 是入口
        # node 名，不是 workflow 名）。仅在 state.workflow_name 为空时填入（首次；
        # 后续 workflow_started 不覆盖，保留最早值）。
        update = {"status": "running"}
        wf_name = data.get("workflow_name")
        if not state.workflow_name and wf_name:
            update["workflow_name"] = wf_name
        return state.model_copy(update=update)

    if t == "workflow_completed":
        return state.model_copy(update={"status": "completed", "current_node": None})

    if t == "workflow_failed":
        # node 是 payload（导致失败的 node，可能为 None = workflow 级失败）。
        # 仅当 node 非 None 时覆盖 current_node；workflow 级失败保留最近已知位置
        # （避免 clobber 掉 node_started/route_taken 建立的 current_node）。
        update: dict = {"status": "failed"}
        if node is not None:
            update["current_node"] = node
        return state.model_copy(update=update)

    if t == "workflow_cancelled":
        # 用户取消（MCP cancel_task / RunManager.cancel_run）。语义与 failed 类似（终态），
        # 但 status 区分 cancelled，让壳 / MCP 客户端能渲染不同的"已取消"UI。current_node
        # 不覆盖（保留最近已知位置，便于事后追查在哪被取消）。
        return state.model_copy(update={"status": "cancelled"})

    if t == "node_started":
        # last-writer-wins：同 node 多 session（retry）取最后写入的 running
        node_status = {**state.node_status, node: "running"}
        return state.model_copy(
            update={"node_status": node_status, "current_node": node}
        )

    if t == "node_completed":
        node_status = {**state.node_status, node: "done"}
        context = {**state.context, node: data.get("output")}
        return state.model_copy(update={"node_status": node_status, "context": context})

    if t == "node_failed":
        node_status = {**state.node_status, node: "failed"}
        return state.model_copy(update={"node_status": node_status})

    if t == "node_skipped":
        node_status = {**state.node_status, node: "skipped"}
        return state.model_copy(update={"node_status": node_status})

    if t in ("agent_message", "agent_thinking", "agent_tool_call",
             "agent_tool_result", "agent_usage"):
        # session 级流式细节不进 RunState（留给前端按 session_id 分组，phase 6）。
        # agent_usage 可聚合进 usage（覆盖语义，幂等），但累加会破坏幂等（同事件应用两次=翻倍），
        # 故此处仅在顶层 usage 存在时覆盖为最新一次的快照；phase 5 的 orchestrator 负责
        # 跨 session 聚合。phase 3 不进 RunState，保持 reducer 纯粹幂等。
        # → 显式 no-op：不修改 state（session 细节不入顶层状态）。
        return state

    if t == "route_taken":
        # 路由事件：更新 current_node（last-writer-wins）
        to = data.get("to")
        return state.model_copy(update={"current_node": to})

    # 已知但顶层 RunState 不投影的事件：foreach_* / human_decision_* / custom / error /
    # phase 11 中断 + resume + retry 可观测事件。
    # 这些事件的语义留给前端 reducer（按 session_id 分组 / 自定义渲染）：
    # - foreach 输出随 node_completed 进 context；
    # - gate 决策、custom 渲染不进顶层状态；
    # - error 细节由 workflow_failed 分支承担状态转换（已处理）；
    # - interrupt_*/prompt_rendered/workflow_resumed/retry_* 是可观测标记，不改顶层状态
    #   （resume 后状态由已落盘的 node_completed/route_taken 重建，resumed 事件本身
    #   不推进 node_status / current_node —— drive_loop 后续 dispatch 才推进；
    #   retry 的最终成败由它包裹的 node_completed/node_failed 承担，retry_started/
    #   retry_succeeded/retry_exhausted 本身不推进 node_status —— 否则同 node 多 attempt
    #   会让 running/done 状态反复跳）。
    # 保持 reducer 幂等 + 最小。
    if t in (
        "foreach_started", "foreach_item_started", "foreach_item_completed",
        "foreach_completed", "human_decision_requested", "human_decision_resolved",
        "interrupt_requested", "interrupt_resolved", "prompt_rendered",
        "workflow_resumed", "retry_started", "retry_succeeded", "retry_exhausted",
        "custom", "error",
    ):
        return state

    # fail loud（SPEC §6.0 铁律4）：未知事件类型不静默丢弃。
    # 新增 EventType 时若忘了加 reducer 分支，这里会 warning（可见，不阻断 replay）。
    logger.warning(
        "reducer 无 %s 分支（seq=%d），事件未投影进 RunState", t, event.seq
    )
    return state

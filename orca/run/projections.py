"""projections.py —— tape event 派生视图的单一算法源（ADR §4.3.1 / 接口收敛 v2 §4.3）。

回答「如何从事件流派生节点状态/用量/session 序列？」：本模块是 ``node_status`` /
``node_usage`` / ``node_session_ids`` / ``node_iter`` 的**唯一**派生算法。RunState
（经 ``apply_event``）+ TUI fold + 未来 Web/MCP 都消费同一份 reducer（DRY，P3）。

设计原则（ADR §3 P1/P3 + §4.3）：
  - **node_status 含 blocked 派生**（ADR §4.3）：node 当前 ``running`` 且有未 resolved
    的 ``human_decision_requested`` / ``interrupt_requested`` → ``blocked``。不入 tape，
    fold 派生；旧 tape 重放遇 gate/interrupt 事件自然派生 blocked（不引入新 EventType）。
  - **node_status 经 apply_event fold**（DRY 单一算法源）：本函数 fold 出 RunState 再读
    ``node_status``，与 ``replay_state`` 的 incremental reducer 同源——保证增量消费
    （live TUI）与 batch 重放（replay_state）输出一致。
  - **其他 projection 独立 fold**：``node_usage`` / ``node_session_ids`` / ``node_iter``
    是 RunState 之外的派生视图（RunState 顶层只有单 ``usage: UsageSummary``，无 per-node
    breakdown / session 序列），故本模块独立 fold；算法仍是单一权威（消费层不许复制）。
  - **纯函数**：同样输入同样输出，不依赖全局状态 / 不发事件 / 不改状态。

依赖单向：本模块依赖 ``orca.schema`` + ``orca.events.replay``；不依赖 ``orca.run.*``
其他子模块（避免 ``run/`` 内部循环）、不依赖 ``orca.iface`` （消费层）。
"""

from __future__ import annotations

from collections.abc import Iterable

from orca.schema import Event, RunState, Status, UsageSummary


def node_status(events: Iterable[Event]) -> dict[str, Status]:
    """Batch fold：events → ``dict[node, Status]``（含 ``blocked`` 派生，ADR §4.3）。

    纯函数，重放一致。blocked 派生规则：node 当前 ``running`` 且收到未 resolved 的
    ``human_decision_requested`` / ``interrupt_requested`` → ``blocked``；对应
    ``*_resolved`` 时回 ``running``。

    实现委托 ``apply_event``（reducer 单一算法源），与 ``replay_state`` 增量消费产出的
    ``RunState.node_status`` 一致——保证 RunState + TUI + 未来 Web/MCP 三处同源。
    """
    # Local import：events.replay 仅依赖 schema（无循环）。
    from orca.events.replay import apply_event

    state = RunState(run_id="_proj", workflow_name="", status="pending")
    for event in events:
        state = apply_event(state, event)
    return dict(state.node_status)


def node_usage(events: Iterable[Event]) -> dict[str, UsageSummary]:
    """Batch fold：events → ``dict[node, UsageSummary]``。

    每 node 取 ``seq`` 最大的一条 ``agent_usage`` 事件（opencode translator per-step 是
    累积值，故 last-wins；claude translator 每 step 独立计 -> 累加场景由调用方处理）。

    幂等：同事件序列重放两次产出相同 dict（按 seq 严格比较，避免乱序污染）。
    """
    usage: dict[str, UsageSummary] = {}
    last_seq: dict[str, int] = {}
    for event in events:
        if event.type != "agent_usage" or not event.node:
            continue
        data = event.data or {}
        prev = last_seq.get(event.node, -1)
        # 严格 >= ：允许同 seq 重放（幂等）；< 表示乱序到达，跳过。
        if event.seq < prev:
            continue
        cache = data.get("cache_tokens")
        usage[event.node] = UsageSummary(
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            cache_tokens=int(cache) if cache is not None else 0,
            cost_usd=float(data.get("cost_usd", 0.0)),
        )
        last_seq[event.node] = event.seq
    return usage


def node_session_ids(events: Iterable[Event]) -> dict[str, list[str]]:
    """Batch fold：events → ``dict[node, list[session_id]]``（按 ``node_started`` 顺序）。

    ``session_id`` 为空（``""`` / ``None``）的 ``node_started`` 不入列表（防御非法事件）。
    重放同一 session_id 不重复 append（幂等）。retry 时新 session_id 触发 append；
    skip / interrupt 不 append（它们不 emit ``node_started``）。
    """
    sessions: dict[str, list[str]] = {}
    for event in events:
        if event.type != "node_started" or not event.node:
            continue
        sid = event.session_id or ""
        if not sid:
            continue
        lst = sessions.setdefault(event.node, [])
        if sid not in lst:
            lst.append(sid)
    return sessions


def node_iter(events: Iterable[Event]) -> dict[str, int]:
    """Batch fold：events → ``dict[node, iter_n]``。

    ``iter_n`` = 该 node 的 ``session_id`` 列表长度（最后一个是当前 session）。
    与 TUI 既有 iter 派生（``session_list.index(session_id) + 1``）一致——
    当前 session 是列表最后一条时，``len == index + 1``。
    """
    sessions = node_session_ids(events)
    return {node: len(sids) for node, sids in sessions.items()}


__all__: list[str] = [
    "node_status",
    "node_usage",
    "node_session_ids",
    "node_iter",
]

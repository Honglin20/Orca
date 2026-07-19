"""tests/events/test_replay.py —— replay reducer 幂等性（核心）+ 各 EventType 分支。

覆盖 SPEC §6.0 铁律 2（幂等）+ SPEC §6.4：
  - **reducer 幂等性（核心）**：apply_event 应用同一事件 N 次 = 1 次（streaming text
    用 text@seq 不拼接；node_status/context 取 last-writer-wins）。
  - 各 EventType 分支（workflow/node 生命周期 / route_taken）。
  - 同 node 多 session_id 分组（retry 场景）：node_status/context 取最后写入。
  - 一条读路径：live 和 replay 走同一个 apply_event（直接调 apply_event 即 live 路径）。

注：reducer 幂等是消灭 dedup 层的根本（反模式③）。若设计需要 dedup set / watermark，
设计就是错的。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orca.events.replay import (
    _replay_state_and_inputs,
    apply_event,
    replay_state,
)
from orca.events.tape import Tape
from orca.run.orchestrator import Orchestrator
from orca.schema import Event, RunState


def _run(coro):
    return asyncio.run(coro)


def _evt(type, seq, node=None, session_id=None, **data) -> Event:
    return Event(
        seq=seq, type=type, timestamp=float(seq), node=node,
        session_id=session_id, data=data,
    )


def _state() -> RunState:
    return RunState(run_id="r1", workflow_name="", status="pending")


# ── reducer 幂等性（核心，SPEC §6.0 铁律 2）──────────────────────────────────


def test_idempotent_node_completed_applied_twice_equals_once():
    """node_completed 应用两次 = 一次（覆盖语义，不累加 / 不翻倍）。"""
    s = _state()
    ev = _evt("node_completed", seq=1, node="a", output={"x": 1})
    once = apply_event(s, ev)
    twice = apply_event(once, ev)
    assert twice == once  # 应用两次结果相同
    assert twice.context["a"] == {"x": 1}  # 不重复累加


def test_idempotent_node_started_does_not_accumulate():
    """node_started 应用多次：node_status 仍为 running（不计数、不累加）。"""
    s = _state()
    ev = _evt("node_started", seq=1, node="a")
    once = apply_event(s, ev)
    many = apply_event(apply_event(apply_event(once, ev), ev), ev)
    assert once == many
    assert many.node_status["a"] == "running"


def test_idempotent_full_tape_replayed_twice_equals_once(tmp_path):
    """整段 tape replay 两次结果相同（reducer 纯函数，SPEC §3.4 规则 8）。"""
    path = tmp_path / "events.jsonl"
    tape = Tape(path, run_id="r1")
    try:
        _run(tape.append({"type": "workflow_started", "timestamp": 1.0,
                          "node": None, "session_id": None, "data": {"entry": "a"}}))
        _run(tape.append({"type": "node_started", "timestamp": 1.0,
                          "node": "a", "session_id": "s1", "data": {}}))
        _run(tape.append({"type": "node_completed", "timestamp": 1.0,
                          "node": "a", "session_id": "s1", "data": {"output": {"x": 1}}}))
        _run(tape.append({"type": "workflow_completed", "timestamp": 1.0,
                          "node": None, "session_id": None, "data": {}}))
    finally:
        tape.close()

    tape2 = Tape(path, run_id="r1")
    try:
        s1 = replay_state(tape2)
        s2 = replay_state(tape2)
        assert s1 == s2  # 重放两次完全一致
        assert s1.status == "completed"
        assert s1.node_status == {"a": "done"}
        assert s1.context == {"a": {"x": 1}}
    finally:
        tape2.close()


# ── streaming text 幂等（text@seq，不拼接 —— 反模式③核心）──────────────────


def test_streaming_agent_message_does_not_concat():
    """agent_message reducer 不进 RunState（session 细节留给前端 reducer 按 session_id 分组）。

    这保证了「应用两次 = 一次」：streaming text 根本不累加进顶层状态，无拼接风险。
    即便前端按 session_id 做 text@seq 投影，也是 last-writer-wins（keyed by seq），不拼接。
    """
    s = _state()
    e1 = apply_event(s, _evt("agent_message", seq=1, node="a", session_id="s1", text="hello"))
    e2 = apply_event(e1, _evt("agent_message", seq=2, node="a", session_id="s1", text="world"))
    # 多次应用同一个 agent_message：状态不变（no-op）
    e3 = apply_event(e2, _evt("agent_message", seq=2, node="a", session_id="s1", text="world"))
    assert e2 == e3  # 幂等
    # agent_message 不污染顶层 context（session 细节不入 RunState）
    assert "a" not in e3.context


@pytest.mark.parametrize("etype,payload", [
    ("agent_message", {"text": "hi"}),
    ("agent_thinking", {"text": "thinking"}),
    ("agent_tool_call", {"tool": "bash", "args": {}, "tool_call_id": "t1"}),
    ("agent_tool_result", {"tool_call_id": "t1", "result": "ok"}),
    ("agent_usage", {"input_tokens": 100, "output_tokens": 50, "cache_tokens": 0, "cost_usd": 0.01}),
])
def test_streaming_event_types_idempotent(etype, payload):
    """所有 streaming 事件类型应用 N 次 = 1 次（SPEC §6.4 核心幂等）。

    agent_usage 尤其关键：累加会翻倍，必须 no-op（phase 5 orchestrator 负责跨 session 聚合）。
    """
    s = _state()
    ev = _evt(etype, seq=1, node="a", session_id="s1", **payload)
    once = apply_event(s, ev)
    many = apply_event(apply_event(apply_event(once, ev), ev), ev)
    assert once == many
    # streaming 事件不污染顶层状态
    assert many.context == {}
    assert many.node_status == {}


def test_route_taken_idempotent_on_reapplication():
    """route_taken 应用多次：current_node 取最后写入（幂等，SPEC §6.4）。"""
    s = _state()
    ev = _evt("route_taken", seq=1, **{"from": "a", "to": "b"})
    once = apply_event(s, ev)
    many = apply_event(apply_event(once, ev), ev)
    assert once == many
    assert many.current_node == "b"


# ── 各 EventType 分支 ────────────────────────────────────────────────────────


def test_workflow_lifecycle_branches():
    s = _state()
    s = apply_event(s, _evt("workflow_started", seq=1, entry="a"))
    assert s.status == "running"
    s = apply_event(s, _evt("workflow_completed", seq=2))
    assert s.status == "completed"
    assert s.current_node is None


def test_workflow_started_recovers_workflow_name_not_entry():
    """workflow_started 的 workflow_name 字段正确恢复进 RunState（**非 entry**）。

    回归防护：旧 reducer 误把 entry（入口 node 名）赋给 workflow_name。
    entry 是 node 名（如 "start_node"），workflow_name 是 workflow 名（如 "nas-review"）。
    """
    s = _state()
    s = apply_event(
        s,
        _evt("workflow_started", seq=1, entry="start_node", workflow_name="nas-review"),
    )
    assert s.status == "running"
    assert s.workflow_name == "nas-review"  # workflow 名
    assert s.workflow_name != "start_node"  # 不是入口 node 名


def test_workflow_failed_sets_status_and_current_node():
    s = _state()
    s = apply_event(s, _evt("workflow_started", seq=1))
    s = apply_event(s, _evt("node_started", seq=2, node="b"))
    s = apply_event(s, _evt("workflow_failed", seq=3, node="b"))
    assert s.status == "failed"
    assert s.current_node == "b"  # 导致失败的 node


def test_workflow_failed_without_node_preserves_current_node():
    """workflow_failed 且 node=None（workflow 级失败）：保留最近已知 current_node。

    不 clobber 掉 node_started/route_taken 建立的位置（review M1 修复）。
    """
    s = _state()
    s = apply_event(s, _evt("workflow_started", seq=1))
    s = apply_event(s, _evt("node_started", seq=2, node="b"))
    s = apply_event(s, _evt("workflow_failed", seq=3, node=None))  # workflow 级
    assert s.status == "failed"
    assert s.current_node == "b"  # 保留，不被 None 覆盖


def test_node_lifecycle_branches():
    s = _state()
    s = apply_event(s, _evt("node_started", seq=1, node="a"))
    assert s.node_status == {"a": "running"}
    assert s.current_node == "a"
    s = apply_event(s, _evt("node_completed", seq=2, node="a", output={"r": 42}))
    assert s.node_status == {"a": "done"}
    assert s.context == {"a": {"r": 42}}


def test_node_failed_and_skipped_branches():
    s = _state()
    s = apply_event(s, _evt("node_failed", seq=1, node="a"))
    assert s.node_status == {"a": "failed"}
    s = apply_event(s, _evt("node_skipped", seq=2, node="b", reason="cond"))
    assert s.node_status == {"a": "failed", "b": "skipped"}


def test_route_taken_updates_current_node():
    s = _state()
    s = apply_event(s, _evt("route_taken", seq=1, **{"from": "a", "to": "b"}))
    assert s.current_node == "b"


# ── ADR §4.3：blocked 派生（不入 tape，由 gate/interrupt 事件 fold）─────────


def test_human_decision_derives_blocked():
    """ADR §4.3：human_decision_requested → node_status=blocked（None 或 running 时覆盖）。

    blocked 是 fold 派生态，不入 tape；reducer 据 gate 事件直接派生。
    """
    s = _state()
    # node 未 started（None）+ gate requested → blocked（对齐既有 TUI 行为）
    s = apply_event(s, _evt("human_decision_requested", seq=1, node="a", gate_id="g"))
    assert s.node_status.get("a") == "blocked"
    # gate resolved → blocked 回 running（claude resume 继续）
    s = apply_event(s, _evt("human_decision_resolved", seq=2, node="a", gate_id="g"))
    assert s.node_status.get("a") == "running"


def test_interrupt_derives_blocked_same_as_gate():
    """ADR §4.3：interrupt_requested/resolved 与 gate 同源派生 blocked。"""
    s = _state()
    s = apply_event(s, _evt("node_started", seq=1, node="a"))
    assert s.node_status.get("a") == "running"
    s = apply_event(s, _evt("interrupt_requested", seq=2, node="a"))
    assert s.node_status.get("a") == "blocked"
    s = apply_event(s, _evt("interrupt_resolved", seq=3, node="a"))
    assert s.node_status.get("a") == "running"


def test_blocked_does_not_override_terminal_status():
    """终态（done/failed/skipped）不被 blocked 覆盖（node 已完成时 gate 无意义）。"""
    s_done = apply_event(_state(), _evt("node_completed", seq=1, node="a"))
    s_done = apply_event(s_done, _evt("human_decision_requested", seq=2, node="a"))
    assert s_done.node_status.get("a") == "done"

    s_failed = apply_event(_state(), _evt("node_failed", seq=1, node="a"))
    s_failed = apply_event(s_failed, _evt("interrupt_requested", seq=2, node="a"))
    assert s_failed.node_status.get("a") == "failed"


def test_known_noop_events_dont_mutate_state():
    """foreach_* / custom / error：不修改 RunState（保持 reducer 幂等 + 最小）。

    这些事件的语义投影留给前端 reducer（按 session_id 分组 / 自定义渲染）。

    注意：``human_decision_*`` / ``interrupt_*`` 不在此列——它们经 ADR §4.3 blocked
    派生修改 ``node_status``（见 ``test_human_decision_derives_blocked``）。
    """
    s = _state()
    for t, data in [
        ("foreach_started", {"item_count": 3, "max_concurrent": 2}),
        ("foreach_item_started", {"index": 0, "item_key": "k"}),
        ("foreach_item_completed", {"index": 0, "output": "x"}),
        ("foreach_completed", {"count": 3, "succeeded": 3}),
        ("prompt_rendered", {"preview": "..."}),
        ("workflow_resumed", {"from_tape": "x", "replayed_events": 0}),
        ("retry_started", {"attempt": 1, "max_attempts": 3}),
        ("retry_succeeded", {"attempt_total": 1}),
        ("retry_exhausted", {"attempts": 3}),
        ("validator_started", {"criteria_preview": "..."}),
        ("validator_passed", {"issues": []}),
        ("validator_failed", {"issues": ["x"], "retrying": False}),
        ("wait_started", {"duration_seconds": 1}),
        ("wait_completed", {"elapsed_seconds": 1, "interrupted": False}),
        ("dialog_started", {"node": "a"}),
        ("dialog_message", {"role": "user", "text": "hi", "turn": 1}),
        ("dialog_ended", {"total_turns": 1}),
        # web-shell-v2 §3.2 B1 / D8：agent_step_started / unknown_event MUST no-op
        # （agent_step_started 是 liveness 心跳；unknown_event 是 tape escape hatch。
        # 两者绝不投影进 RunState，否则 backend 协议变化会触发非幂等状态变更）。
        ("agent_step_started", {"step_reason": "tool-calls"}),
        ("agent_step_started", {}),
        ("unknown_event", {"raw": {"type": "experimental"}, "source": "opencode"}),
        ("custom", {"kind": "chart"}),
        ("error", {"error_type": "ValueError", "message": "boom"}),
    ]:
        # ``_evt`` 签名 ``_evt(type, seq, node=, session_id=, **data)``：剔除 data 中与
        # ``node`` / ``session_id`` 顶层参数同名的 key（如 dialog_started.data.node），避免冲突。
        spread = {k: v for k, v in data.items() if k not in ("node", "session_id")}
        s2 = apply_event(s, _evt(t, seq=1, node="a", **spread))
        assert s2 == s, f"{t} 不应改 RunState（reducer MUST no-op）"


def test_unknown_event_type_warns_fail_loud(caplog):
    """未知事件类型（未来新增 EventType 忘加 reducer 分支）：记 warning，不静默丢。

    fail loud（SPEC §6.0 铁律4）：reducer 不应静默吞未知类型。
    注意：Event 的 type 是 Literal，构造未知 type 会抛 ValidationError —— 此测试用
    model_construct 绕过校验，模拟「reducer 收到 schema 未声明但 tape 里有」的防御场景。
    """
    import logging

    s = _state()
    # 绕过 Literal 校验构造一个未知 type（模拟未来 schema 扩展但 reducer 未跟上的场景）
    ghost = Event.model_construct(seq=1, type="future_unknown_type",
                                  timestamp=1.0, node="a", session_id="s", data={})
    with caplog.at_level(logging.WARNING):
        s2 = apply_event(s, ghost)
    assert s2 == s  # 不改 state
    assert any("无 future_unknown_type 分支" in r.message for r in caplog.records)


# ── 同 node 多 session_id 分组（retry 场景）──────────────────────────────────


def test_same_node_multiple_sessions_last_writer_wins():
    """同 node 多 session（retry）：node_status/context 取最后写入（SPEC §3.4）。

    retry 场景：node a 第一次 session=s1 失败，重试 session=s2 成功。
    node_status 最终为 done，context 为 s2 的输出（last-writer-wins）。
    """
    s = _state()
    s = apply_event(s, _evt("node_started", seq=1, node="a", session_id="s1"))
    s = apply_event(s, _evt("node_failed", seq=2, node="a", session_id="s1"))
    # retry：新 session_id
    s = apply_event(s, _evt("node_started", seq=3, node="a", session_id="s2"))
    s = apply_event(s, _evt("node_completed", seq=4, node="a", session_id="s2", output={"final": True}))
    assert s.node_status["a"] == "done"  # 最后写入
    assert s.context["a"] == {"final": True}  # s2 的输出，非 s1


def test_replay_preserves_session_id_for_grouping(tmp_path):
    """replay 保留事件 session_id；同 node 不同 session 可区分（SPEC §6.4）。

    session 级细节不进 RunState，但 session_id 在事件顶层保留 —— 前端可按它分组。
    """
    path = tmp_path / "events.jsonl"
    tape = Tape(path, run_id="r1")
    try:
        _run(tape.append({"type": "agent_message", "timestamp": 1.0,
                          "node": "a", "session_id": "s1", "data": {"text": "first"}}))
        _run(tape.append({"type": "agent_message", "timestamp": 1.0,
                          "node": "a", "session_id": "s2", "data": {"text": "retry"}}))
    finally:
        tape.close()

    tape2 = Tape(path, run_id="r1")
    try:
        events = list(tape2.replay())
        # 按 session_id 分组可区分
        by_session = {}
        for e in events:
            by_session.setdefault(e.session_id, []).append(e.data["text"])
        assert by_session == {"s1": ["first"], "s2": ["retry"]}
    finally:
        tape2.close()


# ── 一条读路径（streaming = replay = apply_event）────────────────────────────


def test_single_read_path_apply_event_is_the_reducer():
    """live 和 replay 走同一个 apply_event（SPEC §6.0 铁律 3）。

    无第二份 live/replay 分支代码：replay_state 内部就是循环调 apply_event；
    live 消费（EventBus 订阅）同样应调 apply_event 投影状态。
    """
    # 模拟 live 路径：直接逐个 apply_event（这就是 live reducer 的等价物）
    s = _state()
    s = apply_event(s, _evt("workflow_started", seq=1))
    s = apply_event(s, _evt("node_started", seq=2, node="a"))
    s = apply_event(s, _evt("node_completed", seq=3, node="a", output="ok"))
    s = apply_event(s, _evt("workflow_completed", seq=4))

    # 与 replay 一致（同一段事件序列，同 reducer）
    # （构造等价 tape 验证 replay 产出相同状态）
    import tempfile
    path = Path(tempfile.mkdtemp()) / "e.jsonl"
    tape = Tape(path, run_id="r1")
    try:
        _run(tape.append({"type": "workflow_started", "timestamp": 1.0,
                          "node": None, "session_id": None, "data": {}}))
        _run(tape.append({"type": "node_started", "timestamp": 1.0,
                          "node": "a", "session_id": None, "data": {}}))
        _run(tape.append({"type": "node_completed", "timestamp": 1.0,
                          "node": "a", "session_id": None, "data": {"output": "ok"}}))
        _run(tape.append({"type": "workflow_completed", "timestamp": 1.0,
                          "node": None, "session_id": None, "data": {}}))
    finally:
        tape.close()
    tape2 = Tape(path, run_id="r1")
    try:
        replayed = replay_state(tape2)
    finally:
        tape2.close()
    assert replayed == s  # live（逐 apply_event）== replay（fold apply_event）


# ── phase 11 收官 e2e sweep：reducer 穷尽性 + 富 phase-11 tape 回放 ──────────────
#
# 这两个测试是 phase 11 final sweep 的回归守门：
#   1. **穷尽性**：遍历 EventType Literal 全集，断言每个 type 要么有 reducer 分支（投影状态）
#      要么在显式 no-op 集合里（且**不**触发 fail-loud warning）。回归场景：未来新增
#      EventType 忘了加 reducer 分支 → 现有 test_known_noop_events_dont_mutate_state
#      （硬编码 8 个）抓不到，test_unknown_event_type_warns_fail_loud 只测一个虚构 type；
#      本测试遍历真 Literal 全集，新增 type 漏 reducer 分支时立即可见（warning = 漏分支）。
#   2. **富 phase-11 tape**：构造含所有 phase 11 事件类型（interrupt_* / prompt_rendered /
#      workflow_resumed / retry_* / wait_* / validator_* / dialog_*）交错的 tape，replay 不崩
#      + 终态正确。回归场景：未来 reducer 改动让富 phase-11 tape 回放挂掉（reducer 不幂等 /
#      分支顺序依赖 / 顶层 RunState schema 变）会被此测试抓住。


def test_every_event_type_has_reducer_branch_or_explicit_noop(caplog):
    """穷尽性守门：遍历 EventType Literal 全集，每个 type 应用都不触发 fail-loud warning。

    INTENT（SPEC §6.0 铁律 4 / final sweep item8 等价 reducer 侧）：reducer 对每个 EventType
    要么投影状态（有专属分支），要么显式 no-op（在 apply_event 的 no-op 集合里）。触发 warning
    路径意味着「新增了 EventType 但 reducer 没跟上」——单 Tape 唯一真相源会静默丢该事件的
    状态投影，违反 fail-loud。

    与既有测试的区别：
      - test_known_noop_events_dont_mutate_state：硬编码 8 个 type，新增 type 漏分支抓不到。
      - test_unknown_event_type_warns_fail_loud：用 model_construct 造虚构 type，只验 warning
        路径本身；不验「真 Literal 全集都不走 warning」。
      - 本测试：遍历真 Literal，新增 type 漏 reducer 分支时立即可见。
    """
    import logging
    import typing

    from orca.schema import EventType

    types = typing.get_args(EventType)
    assert len(types) > 0  # sanity：Literal 非空（防止 import 漂移）

    s = _state()
    with caplog.at_level(logging.WARNING):
        for i, t in enumerate(types):
            # 用合理 node/session/data 构造（reducer 只读 type + node + data.get(...)，对 payload
            # 形状不敏感；workflow 级事件 node=None）。
            is_workflow_level = t.startswith("workflow_")
            s = apply_event(
                s, _evt(
                    t, seq=i + 1,
                    node=None if is_workflow_level else "a",
                    session_id="s1" if not is_workflow_level else None,
                ),
            )

    # 无任何 type 走到 fail-loud warning 分支（即无「reducer 无 X 分支」记录）。
    leaked = [r.message for r in caplog.records if "无 " in r.message and "分支" in r.message]
    assert leaked == [], (
        f"以下 EventType 触发了 fail-loud warning（reducer 漏分支 / 漏 no-op 集合）：{leaked}"
    )


def test_replay_tape_rich_with_all_phase11_event_types_no_crash(tmp_path):
    """富 phase-11 tape 回放守门：含全部 phase 11 事件类型交错，replay 不崩 + 终态正确。

    INTENT（final sweep C 项）：phase 11 引入 14 个新事件类型（interrupt_* / prompt_rendered /
    workflow_resumed / retry_* / wait_* / validator_* / dialog_*）。一个真实 phase-11 workflow
    的 tape 会交错这些类型（node 重试时 retry_started 夹 node_started 之间；wait node 夹
    interrupt_resolved 之间；validator 夹 node_completed 之间）。本测试构造这样一个交错
    tape，断言：
      1. replay_state 不崩（未来 reducer 改动让富 tape 回放挂掉会被抓）。
      2. 终态正确：最后一个 route_taken 的 to 字段 = current_node；node_status 反映各 node
         最终状态（done / skipped / failed）。
      3. 幂等：replay 两次结果相同（SPEC §6.0 铁律 2）。

    回归场景：
      - 未来 reducer 给 retry_started/validator_started 加 node_status 推进（让同 node 多 attempt
        状态反复跳）→ 终态 node_status 会错（取到中间 attempt 而非最终）。
      - 未来 reducer 给 wait_started 加 current_node 覆盖 → current_node 会被 wait 事件污染。
      - 未来 RunState schema 变（加必填字段）→ 老 tape 回放挂。
    """
    path = tmp_path / "rich.jsonl"
    tape = Tape(path, run_id="r1")
    try:
        # workflow 起 + 入口 node a（agent，配 retry + validator）。
        _run(tape.append({"type": "workflow_started", "timestamp": 1.0,
                          "node": None, "session_id": None,
                          "data": {"workflow_name": "rich_wf", "entry": "a"}}))
        # node a 第一次 attempt：started → prompt → 失败（spawn_error，可重试）。
        _run(tape.append({"type": "node_started", "timestamp": 2.0,
                          "node": "a", "session_id": "s1", "data": {"kind": "agent"}}))
        _run(tape.append({"type": "prompt_rendered", "timestamp": 2.1,
                          "node": "a", "session_id": "s1",
                          "data": {"node": "a", "session_id": "s1", "preview": "do A"}}))
        _run(tape.append({"type": "retry_started", "timestamp": 2.5,
                          "node": "a", "session_id": "s1",
                          "data": {"attempt": 2, "max_attempts": 3, "error_type": "spawn_error",
                                   "delay_seconds": 0.0}}))
        _run(tape.append({"type": "node_failed", "timestamp": 2.4,
                          "node": "a", "session_id": "s1",
                          "data": {"error_type": "spawn_error", "message": "boom",
                                   "phase": "spawn"}}))
        # node a 第二次 attempt：started → prompt → completed（output 写入）。
        _run(tape.append({"type": "node_started", "timestamp": 3.0,
                          "node": "a", "session_id": "s2", "data": {"kind": "agent"}}))
        _run(tape.append({"type": "prompt_rendered", "timestamp": 3.1,
                          "node": "a", "session_id": "s2",
                          "data": {"node": "a", "session_id": "s2", "preview": "do A (retry)"}}))
        _run(tape.append({"type": "retry_succeeded", "timestamp": 3.2,
                          "node": "a", "session_id": "s2",
                          "data": {"attempt_total": 2, "node": "a"}}))
        # validator：started → failed（重试）→ started → passed。
        _run(tape.append({"type": "validator_started", "timestamp": 3.3,
                          "node": "a", "session_id": "s2",
                          "data": {"node": "a", "criteria_preview": "must be valid"}}))
        _run(tape.append({"type": "validator_failed", "timestamp": 3.4,
                          "node": "a", "session_id": "s2",
                          "data": {"node": "a", "issues": ["bad"], "retrying": True}}))
        _run(tape.append({"type": "validator_started", "timestamp": 3.5,
                          "node": "a", "session_id": "s2",
                          "data": {"node": "a", "criteria_preview": "must be valid"}}))
        _run(tape.append({"type": "validator_passed", "timestamp": 3.6,
                          "node": "a", "session_id": "s2",
                          "data": {"node": "a", "issues": []}}))
        _run(tape.append({"type": "node_completed", "timestamp": 3.7,
                          "node": "a", "session_id": "s2",
                          "data": {"output": {"result": "ok-a"}, "elapsed": 0.7}}))
        # route a → b（wait node）。
        _run(tape.append({"type": "route_taken", "timestamp": 4.0,
                          "node": None, "session_id": None,
                          "data": {"from": "a", "to": "b"}}))
        # node b：wait node，被 Ctrl+G 打断。
        _run(tape.append({"type": "node_started", "timestamp": 4.1,
                          "node": "b", "session_id": "s3", "data": {"kind": "wait"}}))
        _run(tape.append({"type": "wait_started", "timestamp": 4.2,
                          "node": "b", "session_id": "s3",
                          "data": {"duration_seconds": 60.0, "reason": "rate limit"}}))
        _run(tape.append({"type": "interrupt_requested", "timestamp": 4.3,
                          "node": "b", "session_id": None,
                          "data": {"interrupt_id": "i1", "node": "b", "run_id": "r1",
                                   "elapsed_at_request": 0.1, "source": "cli"}}))
        _run(tape.append({"type": "wait_completed", "timestamp": 4.4,
                          "node": "b", "session_id": "s3",
                          "data": {"elapsed_seconds": 0.1, "interrupted": True}}))
        _run(tape.append({"type": "interrupt_resolved", "timestamp": 4.5,
                          "node": "b", "session_id": None,
                          "data": {"interrupt_id": "i1", "action": "continue",
                                   "guidance": "skip weights", "resolved_by": "cli"}}))
        _run(tape.append({"type": "node_completed", "timestamp": 4.6,
                          "node": "b", "session_id": "s3",
                          "data": {"output": {"interrupted": True}, "elapsed": 0.1}}))
        # route b → c。
        _run(tape.append({"type": "route_taken", "timestamp": 5.0,
                          "node": None, "session_id": None,
                          "data": {"from": "b", "to": "c"}}))
        # node c：skipped（用户 P4 显式 skip 到下游）。
        _run(tape.append({"type": "node_skipped", "timestamp": 5.1,
                          "node": "c", "session_id": None,
                          "data": {"reason": "user_interrupt_skip"}}))
        _run(tape.append({"type": "interrupt_resolved", "timestamp": 5.2,
                          "node": "c", "session_id": None,
                          "data": {"interrupt_id": "i2", "action": "skip",
                                   "skip_target": "$end", "resolved_by": "cli"}}))
        _run(tape.append({"type": "route_taken", "timestamp": 5.3,
                          "node": None, "session_id": None,
                          "data": {"from": "c", "to": "$end"}}))
        # dialog（post-run，node a 上多轮）。
        _run(tape.append({"type": "dialog_started", "timestamp": 6.0,
                          "node": "a", "session_id": "dlg1",
                          "data": {"node": "a", "session_id": "dlg1",
                                   "initial_prompt": "why X?"}}))
        _run(tape.append({"type": "dialog_message", "timestamp": 6.1,
                          "node": "a", "session_id": "dlg1",
                          "data": {"role": "user", "text": "why?", "turn": 1}}))
        _run(tape.append({"type": "dialog_message", "timestamp": 6.2,
                          "node": "a", "session_id": "dlg1",
                          "data": {"role": "agent", "text": "because", "turn": 1}}))
        _run(tape.append({"type": "dialog_ended", "timestamp": 6.3,
                          "node": "a", "session_id": "dlg1",
                          "data": {"node": "a", "total_turns": 1, "conclusion": "done"}}))
        _run(tape.append({"type": "workflow_resumed", "timestamp": 7.0,
                          "node": None, "session_id": None,
                          "data": {"from_tape": "rich.jsonl", "resumed_node": "c",
                                   "replayed_events": 20}}))
        _run(tape.append({"type": "workflow_completed", "timestamp": 8.0,
                          "node": None, "session_id": None,
                          "data": {"elapsed": 7.0, "outputs": {}}}))
    finally:
        tape.close()

    # replay 两次（验幂等：富 tape 重放两次结果相同）。
    t1 = Tape(path, run_id="r1")
    t2 = Tape(path, run_id="r1")
    try:
        state1 = replay_state(t1)
        state2 = replay_state(t2)
    finally:
        t1.close()
        t2.close()

    assert state1 == state2  # 幂等（SPEC §6.0 铁律 2）

    # 终态断言（reducer 投影的正确性，可观测结果）。
    assert state1.status == "completed"
    assert state1.workflow_name == "rich_wf"
    # current_node = 最后一个 route_taken 的 to（c → $end 之间 reducer 把 to=$end 写入；
    # workflow_completed 把 current_node 置 None）。
    assert state1.current_node is None
    # node_status：a 最终 done（重试 + validator 后）；b done；c skipped。
    assert state1.node_status.get("a") == "done"
    assert state1.node_status.get("b") == "done"
    assert state1.node_status.get("c") == "skipped"
    # context：a 的最终 output 是 s2 的（last-writer-wins，retry 后的正确输出）。
    assert state1.context.get("a") == {"result": "ok-a"}


# ── SPEC §3 O1a：_replay_state_and_inputs 单次遍历合并 ──────────────────────


def _write_tape(path: Path, events: list[dict]) -> Tape:
    """构造 tape：依次 append 事件（payload 形态，自动补 seq）。测试 helper。"""
    tape = Tape(path, run_id="r1")
    try:
        for ev in events:
            _run(tape.append(ev))
    finally:
        tape.close()
    return tape


def test_replay_state_and_inputs_empty_tape(tmp_path):
    """空 tape（文件不存在 / 无事件）→ 初始 state + 空 inputs（不 WARN）。"""
    path = tmp_path / "empty.jsonl"
    # 不创建文件 → replay() yield 不到任何事件。
    tape = Tape(path, run_id="r1")
    try:
        state, inputs = _replay_state_and_inputs(tape)
    finally:
        tape.close()

    # state ≡ replay_state(tape)（初始 pending，空字段）
    assert state == RunState(run_id="r1", workflow_name="", status="pending")
    # inputs 静默返 {}（无 workflow_started = bootstrap 首调正常态）
    assert inputs == {}


def test_replay_state_and_inputs_no_workflow_started(tmp_path, caplog):
    """tape 有事件但无 workflow_started → 静默返 {}（不 WARN，bootstrap 噪声修复）。"""
    path = tmp_path / "no_ws.jsonl"
    _write_tape(path, [
        {"type": "node_started", "timestamp": 1.0, "node": "a",
         "session_id": "s1", "data": {}},
    ])
    tape = Tape(path, run_id="r1")
    try:
        with caplog.at_level("WARNING"):
            state, inputs = _replay_state_and_inputs(tape)
    finally:
        tape.close()

    # state 部分：node a → running（reducer fold 正确）
    assert state.node_status == {"a": "running"}
    assert state.status == "pending"  # 无 workflow_started → 不转 running
    # inputs 静默返 {} + 无 WARNING（无 ws = bootstrap 首调正常态，非异常）
    assert inputs == {}
    assert not any("workflow_started.data.inputs" in rec.message for rec in caplog.records), (
        "无 workflow_started 时不应 WARN（bootstrap 首调噪声修复）"
    )


def test_replay_state_and_inputs_dict_inputs(tmp_path):
    """workflow_started.data.inputs 为 dict → 返回该 dict（与 _inputs_from_tape 等价）。"""
    path = tmp_path / "ws_inputs.jsonl"
    _write_tape(path, [
        {"type": "workflow_started", "timestamp": 1.0, "node": None,
         "session_id": None,
         "data": {"workflow_name": "wf_a", "inputs": {"x": 1, "y": "foo"}}},
        {"type": "node_started", "timestamp": 2.0, "node": "a",
         "session_id": "s1", "data": {}},
    ])
    tape = Tape(path, run_id="r1")
    try:
        state, inputs = _replay_state_and_inputs(tape)
    finally:
        tape.close()

    # state ≡ replay_state(tape)（workflow_name 设 + status=running）
    assert state.workflow_name == "wf_a"
    assert state.status == "running"
    assert state.node_status == {"a": "running"}
    # inputs ≡ Orchestrator._inputs_from_tape(tape)（首条 ws 的 data.inputs）
    assert inputs == {"x": 1, "y": "foo"}


def test_replay_state_and_inputs_non_dict_inputs_warns(tmp_path, caplog):
    """workflow_started.data.inputs 非 dict（真异常）→ 返 {} + WARN（不静默吞）。"""
    path = tmp_path / "ws_bad_inputs.jsonl"
    _write_tape(path, [
        {"type": "workflow_started", "timestamp": 1.0, "node": None,
         "session_id": None,
         "data": {"workflow_name": "wf_a", "inputs": "not-a-dict-string"}},
    ])
    tape = Tape(path, run_id="r1")
    try:
        with caplog.at_level("WARNING"):
            state, inputs = _replay_state_and_inputs(tape)
    finally:
        tape.close()

    # state 部分：reducer 仍 fold workflow_started（status=running, workflow_name 设）
    assert state.status == "running"
    assert state.workflow_name == "wf_a"
    # inputs：坏 → 返 {}（与 _inputs_from_tape 一致）
    assert inputs == {}
    # WARN 触发（真异常，归因可见）
    assert any("workflow_started.data.inputs" in rec.message for rec in caplog.records), (
        "workflow_started 存在但 inputs 非 dict 应 WARN（真异常归因）"
    )


def test_replay_state_and_inputs_missing_inputs_key_warns(tmp_path, caplog):
    """workflow_started 存在但 data 完全缺 inputs 字段 → 返 {} + WARN（与 _inputs_from_tape 一致）。"""
    path = tmp_path / "ws_no_inputs_key.jsonl"
    _write_tape(path, [
        {"type": "workflow_started", "timestamp": 1.0, "node": None,
         "session_id": None,
         "data": {"workflow_name": "wf_a"}},  # 无 inputs 键
    ])
    tape = Tape(path, run_id="r1")
    try:
        with caplog.at_level("WARNING"):
            _, inputs = _replay_state_and_inputs(tape)
    finally:
        tape.close()

    # inputs 缺失 → dict.get 返 None → 非 dict → WARN + {}
    assert inputs == {}
    assert any("workflow_started.data.inputs" in rec.message for rec in caplog.records)


def test_replay_state_and_inputs_only_first_workflow_started_inputs(tmp_path):
    """多条 workflow_started（罕见 retry 场景）→ 取首条 inputs（mirror _inputs_from_tape 早返）。"""
    path = tmp_path / "multi_ws.jsonl"
    _write_tape(path, [
        {"type": "workflow_started", "timestamp": 1.0, "node": None,
         "session_id": None,
         "data": {"workflow_name": "wf_a", "inputs": {"first": True}}},
        {"type": "workflow_started", "timestamp": 2.0, "node": None,
         "session_id": None,
         "data": {"workflow_name": "wf_a", "inputs": {"second": True}}},
    ])
    tape = Tape(path, run_id="r1")
    try:
        _, inputs = _replay_state_and_inputs(tape)
    finally:
        tape.close()

    # 取首条 ws 的 inputs（与 _inputs_from_tape 在首条 ws 即 return 等价）
    assert inputs == {"first": True}


def test_replay_state_and_inputs_snapshot_equivalence(tmp_path):
    """SPEC §3 O1a 核心 AC：单次遍历 (state, inputs) 与拆分调用**逐字相等**（pure refactor）。

    构造富 tape（含 workflow_started + inputs + node 生命周期 + route），断言：
      - ``state`` 部分 ≡ ``replay_state(tape)``（旧路径 reducer fold，未受本 PR 影响）
      - ``inputs`` 部分 = 固定 expected dict（不通过 wrapper 计算，避免循环自证）
    """
    path = tmp_path / "snapshot.jsonl"
    expected_inputs = {"task": "demo", "count": 3}
    _write_tape(path, [
        {"type": "workflow_started", "timestamp": 1.0, "node": None,
         "session_id": None,
         "data": {"workflow_name": "snap_wf", "entry": "a",
                  "inputs": expected_inputs}},
        {"type": "node_started", "timestamp": 2.0, "node": "a",
         "session_id": "s1", "data": {"kind": "agent"}},
        {"type": "node_completed", "timestamp": 3.0, "node": "a",
         "session_id": "s1",
         "data": {"output": {"result": "ok"}, "elapsed": 1.0}},
        {"type": "route_taken", "timestamp": 4.0, "node": None,
         "session_id": None, "data": {"from": "a", "to": "$end"}},
        {"type": "workflow_completed", "timestamp": 5.0, "node": None,
         "session_id": None,
         "data": {"elapsed": 3.0, "outputs": {"final": "ok"}}},
    ])

    # state 旧路径：replay_state（未受本 PR 影响，独立 reducer fold）
    tape_old = Tape(path, run_id="r1")
    try:
        expected_state = replay_state(tape_old)
    finally:
        tape_old.close()

    # 合并调用（新路径）
    tape_new = Tape(path, run_id="r1")
    try:
        actual_state, actual_inputs = _replay_state_and_inputs(tape_new)
    finally:
        tape_new.close()

    # state 逐字相等（state 半侧的 pure refactor 守门；replay_state 是独立函数，
    # 不调用 _replay_state_and_inputs，故对比有意义）
    assert actual_state == expected_state, (
        f"state 部分 mismatch：\nactual={actual_state}\nexpected={expected_state}"
    )
    # inputs 用固定 expected 值（不通过 Orchestrator._inputs_from_tape 计算 —— 后者现已
    # 是 _replay_state_and_inputs 的薄封装，对比会循环自证）。固定值守门确保 inputs
    # 抽取的"首条 ws.data.inputs"语义不被回归。
    assert actual_inputs == expected_inputs, (
        f"inputs 部分 mismatch：\nactual={actual_inputs}\nexpected={expected_inputs}"
    )

    # 终态断言（确认 snapshot 不是 trivially 空）
    assert actual_state.status == "completed"
    assert actual_state.workflow_name == "snap_wf"
    assert actual_state.node_status == {"a": "done"}


def test_replay_state_and_inputs_idempotent(tmp_path):
    """重放两次结果相同（reducer 纯函数幂等，SPEC §6.0 铁律 2）。"""
    path = tmp_path / "idem.jsonl"
    _write_tape(path, [
        {"type": "workflow_started", "timestamp": 1.0, "node": None,
         "session_id": None,
         "data": {"workflow_name": "idem_wf", "inputs": {"k": "v"}}},
        {"type": "node_started", "timestamp": 2.0, "node": "a",
         "session_id": "s1", "data": {}},
    ])
    t1 = Tape(path, run_id="r1")
    t2 = Tape(path, run_id="r1")
    try:
        s1, i1 = _replay_state_and_inputs(t1)
        s2, i2 = _replay_state_and_inputs(t2)
    finally:
        t1.close()
        t2.close()

    assert (s1, i1) == (s2, i2)  # 重放两次完全一致


def test_replay_state_and_inputs_first_ws_bad_second_ws_good(tmp_path, caplog):
    """首条 ws inputs 坏 + 后续 ws inputs 好 → 返 {}（mirror _inputs_from_tape 早返语义）。

    ``_inputs_from_tape`` 原实现：首条 ws 坏即早返 {}，看不到后续 ws。
    ``_replay_state_and_inputs``：``ws_seen`` flag 在首条 ws 即锁定，后续 ws 不再读 inputs。
    两者语义一致 —— 此测试锁住该 invariant，防 ``ws_seen`` flag 写错（如忘记加 not）。
    """
    path = tmp_path / "ws_first_bad.jsonl"
    _write_tape(path, [
        {"type": "workflow_started", "timestamp": 1.0, "node": None,
         "session_id": None,
         "data": {"workflow_name": "wf_a", "inputs": "first-bad-string"}},
        {"type": "workflow_started", "timestamp": 2.0, "node": None,
         "session_id": None,
         "data": {"workflow_name": "wf_a", "inputs": {"second": "good"}}},
    ])
    tape = Tape(path, run_id="r1")
    try:
        with caplog.at_level("WARNING"):
            _, inputs = _replay_state_and_inputs(tape)
    finally:
        tape.close()

    # 首条 ws 坏 → 返 {}（即使后续 ws 有 dict inputs；mirror _inputs_from_tape 早返）
    assert inputs == {}
    # 首条 ws 的 WARN 触发一次（不应因后续 ws 再次 WARN）
    ws_warns = [r for r in caplog.records if "workflow_started.data.inputs" in r.message]
    assert len(ws_warns) == 1, (
        f"首条 ws 坏应 WARN 一次（ws_seen 锁定后续不再判），实得 {len(ws_warns)} 次"
    )


# ── Orchestrator._inputs_from_tape wrapper parity（SPEC §3 O1a 薄封装契约）────


def test_inputs_from_tape_wrapper_parity_with_replay_helper(tmp_path):
    """SPEC §3 O1a：``Orchestrator._inputs_from_tape`` 薄封装返 ``_replay_state_and_inputs[1]``。

    参数化覆盖三种 edge case（非 dict / 缺 key / 多 ws first-wins），锁住 wrapper
    不会偏离 helper 行为。若有人误改 wrapper（如直接读最后一行 ws 而非首条），本测试会失败。
    """
    cases: list[tuple[str, list[dict], dict]] = [
        (
            "non_dict_inputs",
            [{"type": "workflow_started", "timestamp": 1.0, "node": None,
              "session_id": None,
              "data": {"workflow_name": "wf", "inputs": "bad-string"}}],
            {},
        ),
        (
            "missing_inputs_key",
            [{"type": "workflow_started", "timestamp": 1.0, "node": None,
              "session_id": None,
              "data": {"workflow_name": "wf"}}],
            {},
        ),
        (
            "multiple_ws_first_wins",
            [
                {"type": "workflow_started", "timestamp": 1.0, "node": None,
                 "session_id": None,
                 "data": {"workflow_name": "wf", "inputs": {"first": True}}},
                {"type": "workflow_started", "timestamp": 2.0, "node": None,
                 "session_id": None,
                 "data": {"workflow_name": "wf", "inputs": {"second": True}}},
            ],
            {"first": True},
        ),
        (
            "no_workflow_started",
            [{"type": "node_started", "timestamp": 1.0, "node": "a",
              "session_id": "s1", "data": {}}],
            {},
        ),
    ]

    for name, events, expected in cases:
        path = tmp_path / f"{name}.jsonl"
        _write_tape(path, events)
        tape = Tape(path, run_id="r1")
        try:
            wrapper_inputs = Orchestrator._inputs_from_tape(tape)
        finally:
            tape.close()

        tape2 = Tape(path, run_id="r1")
        try:
            helper_inputs = _replay_state_and_inputs(tape2)[1]
        finally:
            tape2.close()

        assert wrapper_inputs == expected, (
            f"[{name}] wrapper 返 {wrapper_inputs}，期望 {expected}"
        )
        assert wrapper_inputs == helper_inputs, (
            f"[{name}] wrapper 与 helper 结果不一致：{wrapper_inputs} vs {helper_inputs}"
        )


# ── SPEC §7 O1a AC3：_inputs_from_tape 调用方 grep 守门 ─────────────────────


def test_inputs_from_tape_callers_are_bounded():
    """SPEC §7 O1a AC3 自动化守门：``_inputs_from_tape`` 生产调用点 ≤ 1（仅 ``_bare_instance``）。

    ``advance_step`` 已直调 ``_replay_state_and_inputs`` 不再走 wrapper；wrapper 仅供
    ``Orchestrator.from_tape`` 经 ``_bare_instance`` 使用。若未来 orca/ 内出现新调用点，
    本测试 fail loud，提示 reviewer 确认是否：(a) 改直调 ``_replay_state_and_inputs``
    合并遍历；或 (b) 显式接受新调用点（更新本测试上限）。

    范围：仅扫 ``orca/`` 生产代码（tests/ 不计；spec-reviewer S2 同款 AST grep 守门模式）。
    """
    import ast
    from pathlib import Path

    orca_root = Path(__file__).resolve().parents[2] / "orca"
    callers: list[str] = []
    for py in orca_root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # 捕获两种形态：Orchestrator._inputs_from_tape(...) / cls._inputs_from_tape(...)
            # / self._inputs_from_tape(...) —— 任何 ``X._inputs_from_tape`` attribute call。
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_inputs_from_tape"):
                callers.append(f"{py.name}:{node.lineno}")

    # 上限 = 1（``_bare_instance`` 的 ``Orchestrator._inputs_from_tape(bus.tape)``）。
    # ``advance_step`` 应直调 ``_replay_state_and_inputs``，**不**经 wrapper。
    assert len(callers) <= 1, (
        f"`_inputs_from_tape` 生产调用点应为 ≤1（仅 _bare_instance），实得 {callers}。"
        f"新调用点应直调 `_replay_state_and_inputs` 合并遍历，或显式更新本测试上限。"
    )

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

from orca.events.replay import apply_event, replay_state
from orca.events.tape import Tape
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
        ("custom", {"kind": "chart"}),
        ("error", {"error_type": "ValueError", "message": "boom"}),
    ]:
        s2 = apply_event(s, _evt(t, seq=1, node="a"))
        assert s2 == s  # 这些事件不进 RunState（投影留给前端 reducer）


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

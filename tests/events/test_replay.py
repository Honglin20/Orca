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


def test_known_noop_events_dont_mutate_state():
    """foreach_* / human_decision_* / custom / error：不修改 RunState（保持 reducer 幂等 + 最小）。

    这些事件的语义投影留给前端 reducer（按 session_id 分组 / 自定义渲染）。
    """
    s = _state()
    for t, data in [
        ("foreach_started", {"item_count": 3, "max_concurrent": 2}),
        ("foreach_item_started", {"index": 0, "item_key": "k"}),
        ("foreach_item_completed", {"index": 0, "output": "x"}),
        ("foreach_completed", {"count": 3, "succeeded": 3}),
        ("human_decision_requested", {"gate_id": "g", "prompt": "p"}),
        ("human_decision_resolved", {"gate_id": "g", "answer": "yes"}),
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

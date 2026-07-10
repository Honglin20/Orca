"""test_projections.py —— ADR §4.3.1 batch projection 单测。

验证 ``orca.run.projections`` 是 node_status / node_usage / node_session_ids /
node_iter 的单一算法源：

- node_status：含 blocked 派生（gate / interrupt 两类事件，覆盖 None / running /
  terminal-state 三种 current status 路径）。
- node_usage：last-wins（按 seq），opencode per-step 累积值语义。
- node_session_ids：retry 时新 session_id append；空 session_id 跳过；重复不 append。
- node_iter：= len(session_ids[node])。
- 重放一致性（同事件序列两次产出相同 dict）。
- 与 ``apply_event`` incremental reducer 输出一致（RunState.node_status）。
"""

from __future__ import annotations

from orca.run import projections
from orca.schema import Event


def _ev(
    type_: str, seq: int, *, node: str | None = "a",
    session_id: str | None = None, **data,
) -> Event:
    return Event(
        seq=seq, type=type_,  # type: ignore[arg-type]
        timestamp=float(seq), node=node,
        session_id=session_id, data=dict(data) if data else {},
    )


# ── node_status（含 blocked 派生）───────────────────────────────────────────


class TestNodeStatus:
    def test_empty_events_returns_empty_dict(self):
        assert projections.node_status([]) == {}

    def test_basic_lifecycle_running_done_failed_skipped(self):
        events = [
            _ev("node_started", 1, node="a"),
            _ev("node_completed", 2, node="a"),
            _ev("node_started", 3, node="b"),
            _ev("node_failed", 4, node="b"),
            _ev("node_started", 5, node="c"),
            _ev("node_skipped", 6, node="c"),
        ]
        status = projections.node_status(events)
        assert status == {"a": "done", "b": "failed", "c": "skipped"}

    def test_gate_derives_blocked_from_running(self):
        """node running + human_decision_requested → blocked（ADR §4.3 典型路径）。"""
        events = [
            _ev("node_started", 1, node="a", session_id="s1"),
            _ev("human_decision_requested", 2, node="a", gate_id="g1"),
        ]
        status = projections.node_status(events)
        assert status.get("a") == "blocked"

    def test_gate_derives_blocked_from_none(self):
        """node 未 started（None）+ gate requested → blocked（对齐既有 TUI 行为）。"""
        events = [
            _ev("human_decision_requested", 1, node="a", gate_id="g1"),
        ]
        status = projections.node_status(events)
        assert status.get("a") == "blocked"

    def test_gate_resolved_reverts_blocked_to_running(self):
        """gate resolved → blocked 回 running（claude resume 继续）。"""
        events = [
            _ev("node_started", 1, node="a", session_id="s1"),
            _ev("human_decision_requested", 2, node="a", gate_id="g1"),
            _ev("human_decision_resolved", 3, node="a", gate_id="g1"),
        ]
        status = projections.node_status(events)
        assert status.get("a") == "running"

    def test_interrupt_derives_blocked_same_as_gate(self):
        """ADR §4.3：interrupt 事件与 gate 同源派生 blocked。"""
        events = [
            _ev("node_started", 1, node="a", session_id="s1"),
            _ev("interrupt_requested", 2, node="a"),
            _ev("interrupt_resolved", 3, node="a"),
        ]
        # requested 后 blocked；resolved 后 running
        assert projections.node_status(events[:2]).get("a") == "blocked"
        assert projections.node_status(events).get("a") == "running"

    def test_blocked_does_not_override_terminal_status(self):
        """终态（done/failed/skipped）不被 blocked 覆盖。"""
        events_done = [
            _ev("node_completed", 1, node="a"),
            _ev("human_decision_requested", 2, node="a"),
        ]
        assert projections.node_status(events_done).get("a") == "done"

        events_failed = [
            _ev("node_failed", 1, node="a"),
            _ev("interrupt_requested", 2, node="a"),
        ]
        assert projections.node_status(events_failed).get("a") == "failed"

    def test_node_completed_after_gate_resolved(self):
        """完整序列：started → gate → resolved → completed。终态 = done。"""
        events = [
            _ev("node_started", 1, node="a", session_id="s1"),
            _ev("human_decision_requested", 2, node="a", gate_id="g"),
            _ev("human_decision_resolved", 3, node="a", gate_id="g"),
            _ev("node_completed", 4, node="a"),
        ]
        assert projections.node_status(events).get("a") == "done"


# ── node_usage ─────────────────────────────────────────────────────────────


class TestNodeUsage:
    def test_empty_events_returns_empty_dict(self):
        assert projections.node_usage([]) == {}

    def test_single_agent_usage(self):
        events = [_ev("agent_usage", 1, node="a", input_tokens=100, output_tokens=50,
                      cost_usd=0.01)]
        usage = projections.node_usage(events)
        assert usage["a"].input_tokens == 100
        assert usage["a"].output_tokens == 50
        assert usage["a"].cost_usd == 0.01

    def test_last_seq_wins_per_node(self):
        """opencode per-step 累积值语义：取最后一条（按 seq）。"""
        events = [
            _ev("agent_usage", 1, node="a", input_tokens=100, output_tokens=50),
            _ev("agent_usage", 2, node="a", input_tokens=200, output_tokens=100),
            _ev("agent_usage", 3, node="a", input_tokens=300, output_tokens=150),
        ]
        usage = projections.node_usage(events)
        # 最后一条覆盖前两条
        assert usage["a"].input_tokens == 300
        assert usage["a"].output_tokens == 150

    def test_multi_nodes_isolated(self):
        events = [
            _ev("agent_usage", 1, node="a", input_tokens=100),
            _ev("agent_usage", 2, node="b", input_tokens=200),
            _ev("agent_usage", 3, node="a", input_tokens=150),  # 覆盖 a 的
        ]
        usage = projections.node_usage(events)
        assert usage["a"].input_tokens == 150
        assert usage["b"].input_tokens == 200

    def test_out_of_order_seq_skipped(self):
        """乱序（小 seq 后到）跳过，不覆盖大 seq 的值。"""
        events = [
            _ev("agent_usage", seq=10, node="a", input_tokens=100),
            _ev("agent_usage", seq=5, node="a", input_tokens=999),  # 老 seq，跳过
        ]
        usage = projections.node_usage(events)
        assert usage["a"].input_tokens == 100

    def test_cache_tokens_optional(self):
        events = [_ev("agent_usage", 1, node="a", input_tokens=100, cache_tokens=42)]
        assert projections.node_usage(events)["a"].cache_tokens == 42

    def test_cache_tokens_missing_defaults_zero(self):
        events = [_ev("agent_usage", 1, node="a", input_tokens=100)]
        assert projections.node_usage(events)["a"].cache_tokens == 0


# ── node_session_ids / node_iter ────────────────────────────────────────────


class TestNodeSessionIds:
    def test_empty_events(self):
        assert projections.node_session_ids([]) == {}

    def test_single_session(self):
        events = [_ev("node_started", 1, node="a", session_id="s1")]
        assert projections.node_session_ids(events) == {"a": ["s1"]}

    def test_retry_appends_new_session(self):
        """retry 时新 session_id 触发 append。"""
        events = [
            _ev("node_started", 1, node="a", session_id="s1"),
            _ev("node_started", 2, node="a", session_id="s2"),  # retry
        ]
        assert projections.node_session_ids(events) == {"a": ["s1", "s2"]}

    def test_replay_same_session_id_dedup(self):
        """重放同 session_id 不重复 append（幂等）。"""
        events = [
            _ev("node_started", 1, node="a", session_id="s1"),
            _ev("node_started", 2, node="a", session_id="s1"),  # 同 sid 重放
        ]
        assert projections.node_session_ids(events) == {"a": ["s1"]}

    def test_empty_session_id_skipped(self):
        """空 session_id 跳过（防御非法事件）。"""
        events = [
            _ev("node_started", 1, node="a", session_id=""),
            _ev("node_started", 2, node="a", session_id=None),
            _ev("node_started", 3, node="a", session_id="s1"),
        ]
        assert projections.node_session_ids(events) == {"a": ["s1"]}

    def test_multi_nodes_isolated(self):
        events = [
            _ev("node_started", 1, node="a", session_id="a1"),
            _ev("node_started", 2, node="b", session_id="b1"),
            _ev("node_started", 3, node="a", session_id="a2"),
        ]
        sessions = projections.node_session_ids(events)
        assert sessions == {"a": ["a1", "a2"], "b": ["b1"]}


class TestNodeIter:
    def test_empty_events(self):
        assert projections.node_iter([]) == {}

    def test_single_session_iter_one(self):
        events = [_ev("node_started", 1, node="a", session_id="s1")]
        assert projections.node_iter(events) == {"a": 1}

    def test_retry_increments_iter(self):
        events = [
            _ev("node_started", 1, node="a", session_id="s1"),
            _ev("node_started", 2, node="a", session_id="s2"),
            _ev("node_started", 3, node="a", session_id="s3"),
        ]
        assert projections.node_iter(events) == {"a": 3}


# ── 重放一致性 + apply_event 一致性 ─────────────────────────────────────────


class TestReplayConsistency:
    def test_node_status_idempotent_replay(self):
        """同一事件序列两次产出相同 dict（幂等）。"""
        events = [
            _ev("node_started", 1, node="a", session_id="s1"),
            _ev("human_decision_requested", 2, node="a", gate_id="g"),
            _ev("human_decision_resolved", 3, node="a", gate_id="g"),
            _ev("node_completed", 4, node="a"),
        ]
        first = projections.node_status(events)
        second = projections.node_status(events)
        assert first == second

    def test_node_usage_idempotent_replay(self):
        events = [_ev("agent_usage", 1, node="a", input_tokens=100)]
        assert projections.node_usage(events) == projections.node_usage(events)

    def test_node_session_ids_idempotent_replay(self):
        events = [
            _ev("node_started", 1, node="a", session_id="s1"),
            _ev("node_started", 2, node="a", session_id="s2"),
        ]
        assert projections.node_session_ids(events) == projections.node_session_ids(events)

    def test_node_status_matches_replay_state(self):
        """projections.node_status 与 replay_state().node_status 输出一致（DRY 单一算法源）"""
        import asyncio
        import tempfile
        from pathlib import Path

        from orca.events.replay import replay_state
        from orca.events.tape import Tape

        # 写临时 tape，重放后比对。
        events = [
            _ev("node_started", 1, node="a", session_id="s1"),
            _ev("human_decision_requested", 2, node="a", gate_id="g"),
        ]

        def write_and_replay():
            with tempfile.TemporaryDirectory() as d:
                tape = Tape(Path(d) / "t.jsonl", run_id="r")
                # tape.append 是 async（asyncio），同步 wrapper。
                for e in events:
                    asyncio.run(tape.append(e.model_dump()))
                tape.close()

                tape2 = Tape(Path(d) / "t.jsonl", run_id="r")
                state = replay_state(tape2)
                tape2.close()
                return state

        state = write_and_replay()
        # replay_state.node_status 应等于 projections.node_status(events)
        assert state.node_status == projections.node_status(events)

    def test_node_status_matches_replay_state_with_interrupt_path(self):
        """DRY 一致性扩展：interrupt 路径也匹配（gate + interrupt 同源派生）。"""
        import asyncio
        import tempfile
        from pathlib import Path

        from orca.events.replay import replay_state
        from orca.events.tape import Tape

        events = [
            _ev("node_started", 1, node="x", session_id="s1"),
            _ev("interrupt_requested", 2, node="x"),
            _ev("node_completed", 3, node="x"),  # 罕见：interrupt 后仍 completed
            _ev("node_started", 4, node="y", session_id="s1"),
            _ev("human_decision_requested", 5, node="y", gate_id="g"),
        ]

        def write_and_replay():
            with tempfile.TemporaryDirectory() as d:
                tape = Tape(Path(d) / "t.jsonl", run_id="r")
                for e in events:
                    asyncio.run(tape.append(e.model_dump()))
                tape.close()
                tape2 = Tape(Path(d) / "t.jsonl", run_id="r")
                state = replay_state(tape2)
                tape2.close()
                return state

        state = write_and_replay()
        assert state.node_status == projections.node_status(events)

    def test_node_usage_same_seq_replay_idempotent(self):
        """同 seq 重放（事件重放）last-wins 语义幂等（projections.node_usage 注释说 >= 允许）。"""
        events = [
            _ev("agent_usage", seq=5, node="a", input_tokens=100),
            _ev("agent_usage", seq=5, node="a", input_tokens=200),  # 同 seq，后到覆盖
        ]
        usage = projections.node_usage(events)
        # 同 seq：第二个覆盖第一个（last-wins）
        assert usage["a"].input_tokens == 200

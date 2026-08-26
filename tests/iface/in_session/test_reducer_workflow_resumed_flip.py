"""tests/iface/in_session/test_reducer_workflow_resumed_flip.py —— AC4 reducer 翻转守门。

覆盖 SPEC ``docs/specs/2026-08-11-resume-failed-and-configurable-escalation.md`` §1.3：
  - AC4：``workflow_resumed`` 的 reducer 语义——failed → running 翻转；其余状态 no-op。

幂等验证（SPEC §3.4 规则 8）：同一 ``workflow_resumed`` 应用 N 次 = 1 次。
"""
from __future__ import annotations

from orca.events.replay import apply_event
from orca.schema import Event, RunState


def _state(status: str) -> RunState:
    return RunState(run_id="r1", workflow_name="wf", status=status)


def _evt(status_for: str = "failed") -> Event:
    """构造一条 workflow_resumed 事件。status_for 仅用来让 data 内容有意义，reducer 不读。"""
    return Event(
        seq=1, type="workflow_resumed", timestamp=1.0,
        node=None, session_id=None,
        data={
            "from_tape": "/tmp/tape.jsonl",
            "resumed_node": "a",
            "reason": "recovered_from_failure",
            "replayed_events": 0,
        },
    )


# ── AC4：全状态矩阵 ─────────────────────────────────────────────────────────


def test_workflow_resumed_flips_failed_to_running():
    """AC4 核心：failed state → workflow_resumed → running（resume-failed 的 reducer 翻转）。"""
    s_before = _state("failed")
    s_after = apply_event(s_before, _evt())
    assert s_after.status == "running"


def test_workflow_resumed_on_running_is_noop():
    """AC4：running state → workflow_resumed → running（崩溃-resume 的 no-op，不破坏既有语义）。"""
    s_before = _state("running")
    s_after = apply_event(s_before, _evt())
    assert s_after.status == "running"
    assert s_after == s_before


def test_workflow_resumed_on_pending_is_noop():
    """AC4：pending state → workflow_resumed → pending（不翻转非终态）。"""
    s_before = _state("pending")
    s_after = apply_event(s_before, _evt())
    assert s_after.status == "pending"
    assert s_after == s_before


def test_workflow_resumed_on_completed_is_noop():
    """AC4：completed state → workflow_resumed → completed（不翻 completed）。"""
    s_before = _state("completed")
    s_after = apply_event(s_before, _evt())
    assert s_after.status == "completed"
    assert s_after == s_before


def test_workflow_resumed_on_cancelled_is_noop():
    """AC4：cancelled state → workflow_resumed → cancelled（cancelled 保持终态，不可 resume）。"""
    s_before = _state("cancelled")
    s_after = apply_event(s_before, _evt())
    assert s_after.status == "cancelled"
    assert s_after == s_before


# ── 幂等（SPEC §3.4 规则 8）─────────────────────────────────────────────────


def test_workflow_resumed_idempotent_on_failed():
    """幂等：failed → workflow_resumed → running → workflow_resumed → running（第二次 no-op）。"""
    s_failed = _state("failed")
    s1 = apply_event(s_failed, _evt())
    assert s1.status == "running"
    s2 = apply_event(s1, _evt())
    assert s2.status == "running"
    assert s2 == s1


# ── 集成：tape fold（replay_state）对 failed run resume 翻转 ─────────────────


def test_replay_state_flips_failed_to_running_after_resume(tmp_path):
    """AC4 集成：手写 failed tape + workflow_resumed → replay_state(tape).status == 'running'。

    这是 cli._next_in_critical_section 重建 marker 时 ``replay_state(tape).status == 'failed'``
    判定的前提：resume emit 落 tape 后，replay_state 翻转为 running（AC4）。
    但在 marker 重建条件检查时（resume emit 落 tape **之前**），status 仍是 failed。
    本测试验落 tape **之后**的 fold 结果 = running。
    """
    import json

    from orca.events.replay import replay_state
    from orca.events.tape import Tape

    events = [
        {"type": "workflow_started", "node": None, "session_id": None,
         "data": {"workflow_name": "wf"}},
        {"type": "node_started", "node": "a", "session_id": None, "data": {}},
        {"type": "node_failed", "node": "a", "session_id": None,
         "data": {"kind": "output_schema_mismatch"}},
        {"type": "node_started", "node": "a", "session_id": None, "data": {}},
        {"type": "workflow_failed", "node": "a", "session_id": None,
         "data": {"kind": "output_schema_mismatch"}},
    ]
    path = tmp_path / "tape.jsonl"
    lines = []
    for seq, evt in enumerate(events, start=1):
        lines.append(json.dumps({**evt, "seq": seq, "timestamp": 0.0}, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tape = Tape(path, run_id="r1", resume=True)
    assert replay_state(tape).status == "failed"

    # 追加 workflow_resumed（模拟 resume emit 落 tape）
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "seq": len(events) + 1, "type": "workflow_resumed", "timestamp": 0.0,
            "node": None, "session_id": None,
            "data": {"reason": "recovered_from_failure", "resumed_node": "a"},
        }, ensure_ascii=False) + "\n")

    tape2 = Tape(path, run_id="r1", resume=True)
    state = replay_state(tape2)
    assert state.status == "running", (
        f"workflow_resumed 落 tape 后 replay_state 应翻 running，实得 {state.status!r}"
    )

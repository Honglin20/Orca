"""test_resume_crash_window.py —— SPEC B B1 resume 起点判据（NEW-1 双终态覆盖）。

覆盖 SPEC §6.1 / §8.1：
  - **done 窗口**：``[node_completed(A), route_taken(A→B))`` 崩溃 → A 不重跑，B 被推进。
  - **skipped 窗口**：``[node_skipped(A),  route_taken(A→B))`` 崩溃 → A 不重 dispatch，
    router skip_tolerant 命中兜底 route 后的下一 node 被推进（NEW-1 验收盲区 b）。
  - **复合序列**：``done A → skip B → route_taken(B→C) → 崩在 C running`` → A/B 都不重跑，
    仅 C 被 dispatch（NEW-1 验收盲区 a）。
  - **幂等回归**：同 tape 两次 ``from_tape + run_from_state`` 的 dispatch 序列相等。

策略：手构造 tape fixtures（确定性，零 spawn），断言 ``_RecordingAgentExecutor.spawned``
序列 + ``state.context`` 未被覆盖 + ``workflow_resumed.data`` 诊断字段。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from orca.events.bus import EventBus
from orca.events.replay import replay_state
from orca.events.tape import Tape
from orca.exec.context import RunContext
from orca.run.orchestrator import Orchestrator
from orca.schema import AgentNode, Event, Route, Workflow


def run_async(coro):
    return asyncio.run(coro)


# ── helpers ──────────────────────────────────────────────────────────────────


def _linear_wf(*, a_routes: list[Route] | None = None) -> Workflow:
    """entry a → b → c → $end（全 agent，便于 _RecordingAgentExecutor 记录 spawn）。

    ``a_routes`` 默认 ``[Route(to="b")]``；skipped-window 测试改成 ``[Route(when=None,
    to="b")]`` 让 router skip_tolerant 命中兜底 route。
    """
    if a_routes is None:
        a_routes = [Route(to="b")]
    return Workflow(
        name="crash_window",
        entry="a",
        nodes=[
            AgentNode(name="a", prompt="do A", routes=a_routes),
            AgentNode(name="b", prompt="do B", routes=[Route(to="c")]),
            AgentNode(name="c", prompt="do C", routes=[Route(to="$end")]),
        ],
        outputs={},
    )


def _resume_bus(tmp_path: Path, tape_path: Path) -> EventBus:
    run_id = tape_path.stem or "r1"
    tape = Tape(tape_path, run_id=run_id, resume=True)
    return EventBus(tape)


def _write_tape(tmp_path: Path, name: str, events: list[dict]) -> Path:
    tape_path = tmp_path / name
    tape_path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8",
    )
    return tape_path


def _ws(seq: int) -> dict:
    return {"seq": seq, "type": "workflow_started", "timestamp": 1.0,
            "node": None, "session_id": None,
            "data": {"inputs": {}, "node_count": 3, "entry": "a",
                     "workflow_name": "crash_window", "topology": {}}}


def _ns(seq: int, node: str, sid: str = "s1") -> dict:
    return {"seq": seq, "type": "node_started", "timestamp": 1.0,
            "node": node, "session_id": sid, "data": {"kind": "script"}}


def _nc(seq: int, node: str, output: Any, sid: str = "s1") -> dict:
    return {"seq": seq, "type": "node_completed", "timestamp": 1.0,
            "node": node, "session_id": sid,
            "data": {"output": output, "elapsed": 0.01}}


def _nskip(seq: int, node: str, reason: str = "user_skip", sid: str = "s1") -> dict:
    return {"seq": seq, "type": "node_skipped", "timestamp": 1.0,
            "node": node, "session_id": sid, "data": {"reason": reason}}


def _rt(seq: int, frm: str, to: str) -> dict:
    return {"seq": seq, "type": "route_taken", "timestamp": 1.0,
            "node": None, "session_id": None, "data": {"from": frm, "to": to}}


class _RecordingAgentExecutor:
    """记录 spawn 的 fake agent executor（同 test_skip_to_agent.py 实现）。"""

    def __init__(self) -> None:
        self.spawned: list[str] = []

    async def exec(self, node, ctx: RunContext):
        from orca.exec.render import render_prompt

        self.spawned.append(node.name)
        session_id = uuid.uuid4().hex
        prompt = render_prompt(node, ctx)
        output = {"stdout": f"step_{node.name}\n", "exit_code": 0, "prompt": prompt}

        def _ev(t: str, data: dict) -> Event:
            return Event(seq=0, type=t, timestamp=time.time(),  # type: ignore[arg-type]
                         node=node.name, session_id=session_id, data=data)

        yield _ev("node_started", {"executor": "fake", "kind": "agent"})
        yield _ev("prompt_rendered", {
            "node": node.name, "session_id": session_id, "preview": prompt[-200:],
        })
        yield _ev("node_completed", {"output": output, "elapsed": 0.01})


def _patch_factory(fake_exec: _RecordingAgentExecutor):
    import orca.exec.factory as factory_mod

    orig = factory_mod.make_executor
    factory_mod.make_executor = (
        lambda node, agent_tools_server=None, bus=None, **kwargs: fake_exec
    )
    return orig


def _restore_factory(orig):
    import orca.exec.factory as factory_mod

    factory_mod.make_executor = orig


# ── B1 done 窗口：[node_completed(A), route_taken(A→B)) ────────────────────────


def test_resume_after_node_completed_before_route_taken(tmp_path):
    """SPEC B B1 / I-RESUME-1（done 终态）：tape 末尾 ``[ws, ns(A), nc(A){X}]``（缺
    route_taken）→ ``from_tape`` → ``run_from_state`` → 断言：
      - ``spawned == ["B"]``（A 不重跑，B 是 route 求值结果）
      - ``state.context[A] == X``（未被覆盖）
      - ``workflow_resumed.data["current_node_at_crash"] == "A"``（NEW-6 诊断字段）
    """
    wf = _linear_wf()
    output_a = {"stdout": "step_a\n", "exit_code": 0}
    tape_path = _write_tape(tmp_path, "events.jsonl", [
        _ws(1), _ns(2, "a"), _nc(3, "a", output_a),
    ])

    bus = _resume_bus(tmp_path, tape_path)
    orch = Orchestrator.from_tape(tape_path, bus, wf)
    # B1 判据：A 处于 done 终态 → fallback 推断 a 的下一 node = b。
    assert orch._resume_start_node == "b"
    # NEW-6：诊断字段 = state.current_node（= a）。
    assert orch._resume_current_node_at_crash == "a"
    # outputs_acc 含 a 的原始 output（包壳）。
    assert orch._resume_initial_outputs["a"] == {"output": output_a}

    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory(fake_exec)
    try:
        state = run_async(orch.run_from_state())
        # NEW-6 端到端 AC：emit 的 workflow_resumed event.data 含 current_node_at_crash
        # （= state.current_node 字面快照，additive 字段）。reducer 对 workflow_resumed
        # no-op，但事件落 tape；从 tape 读回验证。
        resumed_events = [
            e for e in bus.tape.replay() if e.type == "workflow_resumed"
        ]
    finally:
        _restore_factory(orig)
        bus.close()

    # A 不重 dispatch；B 被 dispatch；C 推进到完成（linear wf）。
    assert fake_exec.spawned == ["b", "c"], (
        f"spawned 应为 [b, c]（a 不重跑），实得 {fake_exec.spawned}"
    )
    # A 的 context 未被二次覆盖（仍是原 output_a）。
    assert state.context["a"] == output_a
    # b/c 由新 dispatch 写入。
    assert "b" in state.context
    assert "c" in state.context
    # NEW-6 / §6.1 末条 AC：workflow_resumed.data["current_node_at_crash"] == "a"
    # （诊断字段，= reducer state 字面快照，非 resume_node）。
    assert len(resumed_events) == 1, (
        f"应 emit 一条 workflow_resumed，实得 {len(resumed_events)}"
    )
    assert resumed_events[0].data["current_node_at_crash"] == "a", (
        f"current_node_at_crash 应为 a（state.current_node），实得 "
        f"{resumed_events[0].data.get('current_node_at_crash')!r}"
    )
    assert resumed_events[0].data["resumed_node"] == "b"


# ── B1 skipped 窗口：[node_skipped(A), route_taken(A→B)) ───────────────────────


def test_resume_after_node_skipped_before_route_taken(tmp_path):
    """SPEC B B1（NEW-1 验收盲区 b）：tape 末尾 ``[ws, ns(A), nskip(A)]``（缺 route_taken）
    → ``from_tape`` → ``run_from_state`` → 断言：
      - ``spawned`` **不含** ``A``（A 是 skipped 终态，不重 dispatch）
      - ``spawned`` 含 router skip_tolerant 命中兜底 route 后的下一 node（= b）

    前置：A 的 routes 必须有兜底（``when=None``）才能让 ``output=None`` 经 router
    skip_tolerant 命中（F4-b）；否则 RouteError fail loud（声明 limitation）。
    """
    wf = _linear_wf(a_routes=[Route(when=None, to="b")])  # 兜底 route
    tape_path = _write_tape(tmp_path, "events.jsonl", [
        _ws(1), _ns(2, "a"), _nskip(3, "a"),
    ])

    bus = _resume_bus(tmp_path, tape_path)
    orch = Orchestrator.from_tape(tape_path, bus, wf)
    # B1 skipped-window：A 处于 skipped 终态 → fallback；outputs_acc[a] = {"output": None}
    # （B2 派生）→ router skip_tolerant 命中兜底 route → b。
    assert orch._resume_start_node == "b"
    # B2 live/resume 一致：outputs_acc[a] = {"output": None}。
    assert orch._resume_initial_outputs["a"] == {"output": None}

    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory(fake_exec)
    try:
        state = run_async(orch.run_from_state())
    finally:
        _restore_factory(orig)
        bus.close()

    assert "a" not in fake_exec.spawned, (
        f"A 是 skipped 终态，不应重 dispatch，实得 spawned={fake_exec.spawned}"
    )
    assert "b" in fake_exec.spawned
    # 续跑至完成。
    assert state.status == "completed"


# ── B1 复合序列：done A → skip B → route B→C → 崩 C running ───────────────────


def test_resume_after_skip_then_crash(tmp_path):
    """SPEC B B1（NEW-1 验收盲区 a）：复合序列
    ``[ws, ns(A), nc(A), rt(A→B), ns(B), interrupt_*, nskip(B), rt(B→C), ns(C)]``
    （崩在 C running）→ ``from_tape`` → ``run_from_state`` → 断言：
      - ``spawned == ["C"]``（A done、B skipped 均为终态，不重跑）
      - A/B 的 context 未被覆盖

    本测试实际测的是 R5 limitation 不重跑 done/skipped 终态；不测 progressed-fallback
    （因 tape 含完整 route_taken(B→C)，current_node 已翻到 C）。
    """
    wf = Workflow(
        name="crash_window_compose",
        entry="a",
        nodes=[
            AgentNode(name="a", prompt="do A", routes=[Route(to="b")]),
            # b 兜底 route（when=None）让 skip 容错能命中 → c
            AgentNode(name="b", prompt="do B", routes=[Route(when=None, to="c")]),
            AgentNode(name="c", prompt="do C", routes=[Route(to="$end")]),
        ],
        outputs={},
    )
    output_a = {"stdout": "step_a\n", "exit_code": 0}
    tape_path = _write_tape(tmp_path, "events.jsonl", [
        _ws(1),
        _ns(2, "a", "sa"), _nc(3, "a", output_a, "sa"), _rt(4, "a", "b"),
        _ns(5, "b", "sb"),
        {"seq": 6, "type": "interrupt_requested", "timestamp": 1.0,
         "node": "b", "session_id": "sb", "data": {"id": "i1", "node": "b"}},
        {"seq": 7, "type": "interrupt_resolved", "timestamp": 1.0,
         "node": "b", "session_id": "sb",
         "data": {"id": "i1", "action": "skip", "guidance": None, "source": "test"}},
        _nskip(8, "b", "user_interrupt_skip", "sb"),
        _rt(9, "b", "c"),
        _ns(10, "c", "sc"),
        # 崩在 c running：无 nc(c)，无 workflow_failed（模拟 kill -9）。
    ])

    bus = _resume_bus(tmp_path, tape_path)
    orch = Orchestrator.from_tape(tape_path, bus, wf)
    # current_node = c（route_taken(B→C) 已落盘），status=running → 不走 fallback。
    # c 是 running（非 done/skipped 终态），按 R5 limitation 重 dispatch（at-least-once）。
    assert orch._resume_start_node == "c"

    fake_exec = _RecordingAgentExecutor()
    orig = _patch_factory(fake_exec)
    try:
        state = run_async(orch.run_from_state())
    finally:
        _restore_factory(orig)
        bus.close()

    # 仅 c 被 dispatch（a done、b skipped 均不重跑）。
    assert fake_exec.spawned == ["c"], (
        f"spawned 应为 [c]（a/b 不重跑），实得 {fake_exec.spawned}"
    )
    # A/B 的 context 未被覆盖。
    assert state.context["a"] == output_a
    assert state.context["b"] is None  # B2：skipped node 的 context 是 raw None
    # c 由新 dispatch 写入。
    assert "c" in state.context
    assert state.status == "completed"


# ── B1 幂等回归（铁律 2）───────────────────────────────────────────────────────


def test_resume_idempotent_reapplication(tmp_path):
    """SPEC B 铁律 2：同 tape 跑两次 ``from_tape + run_from_state`` 的 dispatch
    序列相等（幂等）。

    每次迭代用独立的 tape_path（``run_from_state`` 会向 tape 追加 workflow_completed，
    第二次 ``from_tape`` 会 AlreadyCompletedError——故每次写一份等价的 fresh tape）。
    """
    wf = _linear_wf()
    output_a = {"stdout": "step_a\n", "exit_code": 0}
    base_events = [_ws(1), _ns(2, "a"), _nc(3, "a", output_a)]

    spawned_runs: list[list[str]] = []
    for i in range(2):
        tape_path = _write_tape(tmp_path, f"events_{i}.jsonl", base_events)
        bus = _resume_bus(tmp_path, tape_path)
        orch = Orchestrator.from_tape(tape_path, bus, wf)
        fake_exec = _RecordingAgentExecutor()
        orig = _patch_factory(fake_exec)
        try:
            run_async(orch.run_from_state())
        finally:
            _restore_factory(orig)
            bus.close()
        spawned_runs.append(list(fake_exec.spawned))

    assert spawned_runs[0] == spawned_runs[1], (
        f"两次 resume 的 dispatch 序列应相等（幂等），实得 {spawned_runs}"
    )

"""test_run_manager_usage.py —— SPEC B B3 / E1：``_extract_cost`` 切 projections。

验证：
  - ``RunState.usage`` 字段删除后，``_extract_cost`` 改读 ``projections.node_usage``
    （batch fold），从 tape 的 ``agent_usage`` 事件正确累加 ``cost_usd``。
  - **E1**：原 ``getattr(usage, "cost", ...)`` typo（应 ``cost_usd``）被 no-op reducer
    掩盖恒返 0.0；切 projections 后该 bug 自动消失。本测试断言**非零 cost**（构造
    ``agent_usage`` 含 ``cost_usd>0`` 的 demo），不与坏基线对齐。
  - 无 ``agent_usage`` 事件的纯 script run → cost = 0.0（向后兼容）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.iface.web.run_manager import _extract_cost
from orca.run.orchestrator import Orchestrator
from orca.schema import AgentNode, Route, Workflow


def _wf_with_agent() -> Workflow:
    """单 agent node → $end（用 fake executor 跑出不 spawn 的真实 tape）。"""
    return Workflow(
        name="usage_test",
        entry="a",
        nodes=[
            AgentNode(name="a", prompt="do A", routes=[Route(to="$end")]),
        ],
        outputs={},
    )


def _write_tape_with_usage(tmp_path: Path, *, cost_usd: float) -> Tape:
    """手构造含 ``agent_usage`` 事件的 tape（确定性，零 spawn）。"""
    path = tmp_path / "events.jsonl"
    tape = Tape(path, run_id="r1")
    bus = EventBus(tape)
    import asyncio

    async def _write():
        await bus.emit("workflow_started", {
            "inputs": {}, "node_count": 1, "entry": "a",
            "workflow_name": "usage_test", "topology": {},
        })
        await bus.emit("node_started", {"kind": "agent"}, node="a", session_id="s1")
        await bus.emit(
            "agent_usage",
            {"input_tokens": 100, "output_tokens": 50, "cache_tokens": 10,
             "cost_usd": cost_usd},
            node="a", session_id="s1",
        )
        await bus.emit(
            "node_completed",
            {"output": {"result": "done"}, "elapsed": 0.01},
            node="a", session_id="s1",
        )
        await bus.emit("route_taken", {"from": "a", "to": "$end"})
        await bus.emit("workflow_completed", {"outputs": {}, "elapsed": 0.5})

    asyncio.run(_write())
    bus.close()
    return Tape(path, run_id="r1")


def test_extract_cost_returns_nonzero_from_projections(tmp_path):
    """SPEC B B3 / E1：``agent_usage`` 含 ``cost_usd=0.05`` → ``_extract_cost`` 返 0.05。

    原 ``RunState.usage`` 路径因 reducer no-op 恒返 0.0（且 ``getattr(usage,"cost",...)``
    typo 进一步掩盖）；切 projections 后 ``cost_usd`` 字段正确读出，非零 cost 可见。
    """
    tape = _write_tape_with_usage(tmp_path, cost_usd=0.05)
    try:
        cost = _extract_cost(tape)
    finally:
        tape.close()
    assert cost == pytest.approx(0.05), (
        f"含 agent_usage(cost_usd=0.05) 的 tape 应返 0.05，实得 {cost}"
    )
    assert cost > 0.0, "E1：非零 cost 锚点（不与坏基线 0.0 对齐）"


def test_extract_cost_zero_when_no_agent_usage(tmp_path):
    """无 ``agent_usage`` 事件（纯 script run 无 token）→ cost = 0.0（向后兼容）。"""
    path = tmp_path / "no_usage.jsonl"
    tape = Tape(path, run_id="r1")
    bus = EventBus(tape)
    import asyncio

    async def _write():
        await bus.emit("workflow_started", {
            "inputs": {}, "node_count": 0, "entry": "a",
            "workflow_name": "empty", "topology": {},
        })
        await bus.emit("workflow_completed", {"outputs": {}, "elapsed": 0.1})

    asyncio.run(_write())
    bus.close()
    tape2 = Tape(path, run_id="r1")
    try:
        assert _extract_cost(tape2) == 0.0
    finally:
        tape2.close()


def test_extract_cost_sums_multiple_nodes(tmp_path):
    """多 node usage 求和：a(cost=0.01) + b(cost=0.02) → 0.03。"""
    path = tmp_path / "multi.jsonl"
    tape = Tape(path, run_id="r1")
    bus = EventBus(tape)
    import asyncio

    async def _write():
        await bus.emit("workflow_started", {
            "inputs": {}, "node_count": 2, "entry": "a",
            "workflow_name": "multi", "topology": {},
        })
        await bus.emit("node_started", {"kind": "agent"}, node="a", session_id="sa")
        await bus.emit(
            "agent_usage",
            {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01},
            node="a", session_id="sa",
        )
        await bus.emit("node_completed", {"output": {}, "elapsed": 0.01},
                       node="a", session_id="sa")
        await bus.emit("route_taken", {"from": "a", "to": "b"})
        await bus.emit("node_started", {"kind": "agent"}, node="b", session_id="sb")
        await bus.emit(
            "agent_usage",
            {"input_tokens": 20, "output_tokens": 10, "cost_usd": 0.02},
            node="b", session_id="sb",
        )
        await bus.emit("node_completed", {"output": {}, "elapsed": 0.01},
                       node="b", session_id="sb")
        await bus.emit("workflow_completed", {"outputs": {}, "elapsed": 0.2})

    asyncio.run(_write())
    bus.close()
    tape2 = Tape(path, run_id="r1")
    try:
        cost = _extract_cost(tape2)
    finally:
        tape2.close()
    assert cost == pytest.approx(0.03)

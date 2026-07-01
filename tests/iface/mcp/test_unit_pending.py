"""test_unit_pending.py —— pending_gates_from_tape 单元测试（SPEC phase-10 §3.2 / §D1.4）。

覆盖意图（非仅行为）：
  - **tape-only**：派生只读 ``tape.replay()``，不读 handler 内存（反例断言）。
  - **requested/resolved 集合相减**：1 requested 无 resolved → 1 / 同 gate_id 配对 → 0。
  - **多 gate 部分解决**：2 requested + 1 resolved → 1 个（未 resolved 的那个）。
  - **幂等**：连调两次结果相同（纯函数，SPEC §11 决策 4/8）。
  - **字段全**：重建的 HumanGate 所有字段从 event.data + event 顶层取（含 session_id）。
  - **空 tape**：返回 ``[]``（无 requested）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from orca.gates.pending import pending_gates_from_tape
from orca.gates.types import HumanGate

from tests.iface.mcp.conftest import make_tape, run_async


# ── helpers ──────────────────────────────────────────────────────────────────


async def _emit_requested(
    tape: Tape,
    *,
    gate_id: str = "g1",
    prompt: str = "批准？",
    options: list[str] | None = None,
    context: dict | None = None,
    source: str = "tool_permission",
    run_id: str = "r1",
    node: str | None = "n1",
    session_id: str | None = "sess-1",
    timestamp: float = 0.0,
) -> int:
    """直接 tape.append 写 human_decision_requested（绕过 handler，纯 tape 测试）。"""
    return await tape.append(
        {
            "type": "human_decision_requested",
            "timestamp": timestamp,
            "node": node,
            "session_id": session_id,
            "data": {
                "gate_id": gate_id,
                "prompt": prompt,
                "options": options if options is not None else ["allow", "deny"],
                "context": context if context is not None else {"tool": "Bash"},
                "source": source,
                "run_id": run_id,
                "node": node,
            },
        }
    )


async def _emit_resolved(
    tape: Tape,
    *,
    gate_id: str = "g1",
    answer: str = "allow",
    resolved_by: str = "cli",
    node: str | None = None,
    session_id: str | None = None,
    timestamp: float = 0.0,
) -> int:
    """直接 tape.append 写 human_decision_resolved。"""
    return await tape.append(
        {
            "type": "human_decision_resolved",
            "timestamp": timestamp,
            "node": node,
            "session_id": session_id,
            "data": {"gate_id": gate_id, "answer": answer, "resolved_by": resolved_by},
        }
    )


# ── 5 例 + 反例（SPEC D1.4）─────────────────────────────────────────────────


def test_empty_tape_returns_empty_list(tmp_path):
    """空 tape（无任何事件）→ ``[]``（base case）。"""
    tape = make_tape(tmp_path)
    assert pending_gates_from_tape(tape) == []


def test_one_requested_no_resolved_returns_one_gate(tmp_path):
    """1 个 requested（无 resolved）→ 返回 1 个 HumanGate，所有字段重建正确。"""
    tape = make_tape(tmp_path)

    async def go():
        await _emit_requested(
            tape,
            gate_id="g_abc",
            prompt="批准部署吗？",
            options=["yes", "no"],
            context={"tool": "Bash", "args": "rm -rf"},
            source="tool_permission",
            run_id="run-xyz",
            node="deploy",
            session_id="sess-42",
        )

    run_async(go())

    gates = pending_gates_from_tape(tape)
    assert len(gates) == 1
    g = gates[0]
    # 字段全：HumanGate 所有字段从 event.data + event 顶层取
    assert g.id == "g_abc"
    assert g.prompt == "批准部署吗？"
    assert g.options == ["yes", "no"]
    assert g.context == {"tool": "Bash", "args": "rm -rf"}
    assert g.source == "tool_permission"
    assert g.run_id == "run-xyz"
    assert g.node == "deploy"
    assert g.session_id == "sess-42"


def test_requested_plus_resolved_same_gate_returns_empty(tmp_path):
    """requested + resolved（同 gate_id）→ 返回 ``[]``（gate 已答，不再 pending）。"""
    tape = make_tape(tmp_path)

    async def go():
        await _emit_requested(tape, gate_id="g1")
        await _emit_resolved(tape, gate_id="g1", answer="allow", resolved_by="web")

    run_async(go())

    assert pending_gates_from_tape(tape) == []


def test_two_requested_one_resolved_returns_unresolved_one(tmp_path):
    """2 个 requested + 1 个 resolved → 返回 1 个（未 resolved 的那个）。"""
    tape = make_tape(tmp_path)

    async def go():
        await _emit_requested(tape, gate_id="g1", prompt="第一个 gate")
        await _emit_requested(tape, gate_id="g2", prompt="第二个 gate")
        await _emit_resolved(tape, gate_id="g1", answer="allow")

    run_async(go())

    gates = pending_gates_from_tape(tape)
    assert len(gates) == 1
    assert gates[0].id == "g2"  # 未 resolved 的是 g2
    assert gates[0].prompt == "第二个 gate"


def test_derive_is_idempotent(tmp_path):
    """幂等：连调两次相同结果（纯函数，SPEC §11 决策 4/8）。"""
    tape = make_tape(tmp_path)

    async def go():
        await _emit_requested(tape, gate_id="g1")
        await _emit_requested(tape, gate_id="g2")

    run_async(go())

    first = pending_gates_from_tape(tape)
    second = pending_gates_from_tape(tape)
    # HumanGate 是 frozen dataclass，== 比较字段值
    assert first == second
    assert len(first) == 2


def test_does_not_touch_handler_internal_state(tmp_path):
    """**反例断言**：pending_gates_from_tape 对 HumanGateHandler 一无所知仍能正确派生。

    SPEC phase-10 §0.1 第二条 / §3.6 review 检查项：查询路径**禁止**读 handler
    ``_pending`` / ``_gates_meta``。本测试 mock 一个 handler（设任意内部状态），
    断言 ``pending_gates_from_tape`` 不读它，仅靠 tape 派生。

    做法：构造一个 mock handler，把 ``_pending`` 设成完全乱来的内容（与 tape 不一致）；
    断言函数返回的 gates **只反映 tape**，不被 handler 内部污染。
    """
    tape = make_tape(tmp_path)

    async def go():
        await _emit_requested(tape, gate_id="tape_gate")

    run_async(go())

    # mock 一个 handler，内部状态故意与 tape 不一致（_pending 有别的 gate_id）
    fake_handler = MagicMock()
    fake_handler._pending = {"phantom_gate": MagicMock(done=lambda: False)}
    fake_handler._gates_meta = {
        "phantom_gate": HumanGate(
            id="phantom_gate",
            prompt="不应出现",
            context={},
            source="tool_permission",
            run_id="r1",
            node=None,
        )
    }

    # 不把 handler 传给函数（函数签名压根不收 handler），仅断言它对 handler 一无所知
    gates = pending_gates_from_tape(tape)
    # 函数返回的 gates 来自 tape，不含 phantom_gate（handler 内部的）
    assert len(gates) == 1
    assert gates[0].id == "tape_gate"
    # 反例：handler._pending 的内容没污染结果
    assert all(g.id != "phantom_gate" for g in gates)

"""tests/events/test_sidechain_ingestor.py —— RawAgentEvent → agent_* ingestor（SPEC-B v4 §3/§6/§7）。

覆盖意图（SPEC Rule 9：测意图非仅行为）：
  - R2 1:1 透传：每种 kind → 对应 EventType + payload 1:1（无内部 rename）。
  - R3 source_id 查重：相同 source_id 两次 ingest → 只 emit 一次（O(1) 命中 skip）。
  - R3 crash rebuild：tape 已有 agent_* 事件 → rebuild_from_tape 重建 source_id set，
    再 ingest 同 source_id 的事件 → skip。
  - §6 U1 node 派生：emit 前增量扫 tape 取最后 node_started.node；
    多 node_started → 取最后；tape 新增 node_started（cli next 推进）→ 增量反映。
  - source_id 进 data.source_id（schema 不破；前端 pairToolEvents 不读 source_id）。
  - step_boundary → agent_step_started {step_reason}（schema 对齐变换，R2 唯一例外）。
  - session_id = child_id（前端按 session_id 归组）。
  - partial-line 防护：tape 末尾 partial node_started → 沿用上次 node。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from orca.events.bus import EventBus
from orca.events.raw_agent_event import RawAgentEvent
from orca.events.sidechain_ingestor import SidechainIngestor
from orca.events.tape import Tape


# ── helpers ─────────────────────────────────────────────────────────────────


def _run(coro):
    """统一 asyncio.run（仓库约定：不用 pytest-asyncio）。"""
    return asyncio.run(coro)


def _make_bus(tmp_path: Path, run_id: str = "demo-test") -> tuple[EventBus, Tape, Path]:
    """构造 EventBus + Tape + tape_path。"""
    tape_path = tmp_path / "runs" / f"{run_id}.jsonl"
    tape = Tape(tape_path, run_id=run_id)
    bus = EventBus(tape)
    return bus, tape, tape_path


def _append_raw_line(tape_path: Path, obj: dict) -> None:
    """直接 append 一行到 tape 文件（绕过 Tape.append，模拟 cli.next 写入）。"""
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tape_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def _append_event(tape_path: Path, etype: str, *, seq: int, node: str | None = None,
                  session_id: str | None = None, data: dict | None = None) -> None:
    """append 一条完整 Event 行到 tape。"""
    _append_raw_line(tape_path, {
        "seq": seq, "type": etype, "timestamp": 0.0,
        "node": node, "session_id": session_id,
        "data": data or {},
    })


# ── R2: 1:1 透传 ──────────────────────────────────────────────────────────────


def test_ingest_thinking_1to1(tmp_path):
    """thinking kind → agent_thinking，payload 1:1 + source_id 进 data + session_id=child_id。"""
    bus, tape, tape_path = _make_bus(tmp_path)
    try:
        ing = SidechainIngestor(bus, tape_path)
        raw = RawAgentEvent(
            child_id="task-aaa", source_id="task-aaa:0:0",
            kind="thinking", payload={"text": "let me think"},
        )
        emitted = _run(ing.ingest(raw))
        assert emitted is True

        events = list(tape.replay())
        assert len(events) == 1
        e = events[0]
        assert e.type == "agent_thinking"
        assert e.data["text"] == "let me think"
        assert e.data["source_id"] == "task-aaa:0:0"  # R3.1: source_id 进 data
        assert e.session_id == "task-aaa"  # 前端按 session_id 归组
        assert e.node is None  # tape 无 node_started
    finally:
        bus.close()


def test_ingest_each_kind_maps_to_correct_event_type(tmp_path):
    """所有 kind → 对应 EventType；payload 透传（仅 step_boundary 有 schema 对齐变换）。"""
    bus, tape, tape_path = _make_bus(tmp_path)
    try:
        ing = SidechainIngestor(bus, tape_path)
        cases = [
            (RawAgentEvent("c", "s1", "thinking", {"text": "T"}),
             "agent_thinking", {"text": "T", "source_id": "s1"}),
            (RawAgentEvent("c", "s2", "text", {"text": "M"}),
             "agent_message", {"text": "M", "source_id": "s2"}),
            (RawAgentEvent("c", "s3", "tool_call",
              {"tool": "Read", "args": {"path": "/x"}, "tool_call_id": "tc1"}),
             "agent_tool_call",
             {"tool": "Read", "args": {"path": "/x"}, "tool_call_id": "tc1", "source_id": "s3"}),
            (RawAgentEvent("c", "s4", "tool_result",
              {"tool_call_id": "tc1", "result": "ok"}),
             "agent_tool_result",
             {"tool_call_id": "tc1", "result": "ok", "source_id": "s4"}),
            (RawAgentEvent("c", "s5", "step_boundary", {"phase": "start"}),
             "agent_step_started", {"step_reason": "start", "source_id": "s5"}),
        ]
        for raw, expected_type, expected_data in cases:
            emitted = _run(ing.ingest(raw))
            assert emitted is True, f"{raw.kind} 应 emit"

        events = list(tape.replay())
        assert len(events) == len(cases)
        for e, (_, expected_type, expected_data) in zip(events, cases):
            assert e.type == expected_type
            assert e.data == expected_data
    finally:
        bus.close()


def test_step_boundary_with_missing_phase_emits_empty_data(tmp_path):
    """step_boundary 缺 phase → agent_step_started data 仅含 source_id（无 step_reason）。

    对齐 opencode_translator._translate_step_start：首个 step_start 无 reason 时 data={}。
    """
    bus, tape, tape_path = _make_bus(tmp_path)
    try:
        ing = SidechainIngestor(bus, tape_path)
        raw = RawAgentEvent("c", "s1", "step_boundary", {})
        _run(ing.ingest(raw))
        events = list(tape.replay())
        assert len(events) == 1
        assert events[0].type == "agent_step_started"
        assert "step_reason" not in events[0].data
        assert events[0].data["source_id"] == "s1"
    finally:
        bus.close()


# ── R3: source_id 查重 ─────────────────────────────────────────────────────────


def test_ingest_dedup_by_source_id(tmp_path):
    """相同 source_id 第二次 ingest → False（skip）；tape 只 1 行。"""
    bus, tape, tape_path = _make_bus(tmp_path)
    try:
        ing = SidechainIngestor(bus, tape_path)
        raw = RawAgentEvent("c", "dup-id", "thinking", {"text": "T"})
        assert _run(ing.ingest(raw)) is True
        assert _run(ing.ingest(raw)) is False  # dedup

        events = list(tape.replay())
        assert len(events) == 1, "dedup 后 tape 只 1 行"
        assert ing.seen_source_ids == frozenset({"dup-id"})
    finally:
        bus.close()


def test_ingest_different_source_ids_both_emit(tmp_path):
    """不同 source_id → 都 emit（哪怕同 child_id / 同 kind / 同 payload）。"""
    bus, tape, tape_path = _make_bus(tmp_path)
    try:
        ing = SidechainIngestor(bus, tape_path)
        r1 = RawAgentEvent("c", "s1", "thinking", {"text": "T"})
        r2 = RawAgentEvent("c", "s2", "thinking", {"text": "T"})
        assert _run(ing.ingest(r1)) is True
        assert _run(ing.ingest(r2)) is True
        events = list(tape.replay())
        assert len(events) == 2
    finally:
        bus.close()


def test_rebuild_from_tape_reconstructs_source_id_set(tmp_path):
    """crash 重启：tape 已有 agent_* → rebuild 重建 set → 再 ingest 同 source_id skip。

    R3.3 闭环：crash 后 daemon 重启，ingestor.rebuild_from_tape 扫 tape 一次性重建 set。
    """
    bus, tape, tape_path = _make_bus(tmp_path)
    try:
        ing1 = SidechainIngestor(bus, tape_path)
        _run(ing1.ingest(RawAgentEvent("c", "old-id", "thinking", {"text": "T"})))
        # 模拟 crash：丢 ingestor（set 丢失）。
        del ing1

        # 新 ingestor（模拟重启后构造）。
        ing2 = SidechainIngestor(bus, tape_path)
        assert len(ing2.seen_source_ids) == 0  # rebuild 前
        ing2.rebuild_from_tape()
        assert "old-id" in ing2.seen_source_ids  # rebuild 后

        # 再 ingest 同 source_id → skip（dedup 兜底）。
        emitted = _run(ing2.ingest(RawAgentEvent("c", "old-id", "thinking", {"text": "T"})))
        assert emitted is False
        events = list(tape.replay())
        assert len(events) == 1, "rebuild 后 dedup 生效，tape 仍 1 行"
    finally:
        bus.close()


def test_rebuild_skips_non_agent_events(tmp_path):
    """rebuild 只扫 agent_* 事件的 source_id；其它事件（node_* / workflow_*）不影响 set。"""
    bus, tape, tape_path = _make_bus(tmp_path)
    try:
        # 直接写 tape：模拟 cli.next 已写了一些事件（无 source_id 字段）。
        _append_event(tape_path, "workflow_started", seq=1)
        _append_event(tape_path, "node_started", seq=2, node="A")
        _append_event(tape_path, "agent_thinking", seq=3, data={"source_id": "x:0:0"})
        _append_event(tape_path, "node_completed", seq=4, node="A", data={"output": "ok"})

        ing = SidechainIngestor(bus, tape_path)
        ing.rebuild_from_tape()
        assert ing.seen_source_ids == frozenset({"x:0:0"})
        # node_started 也被扫到 → current_node 派生
        assert ing.current_node == "A"
    finally:
        bus.close()


# ── §6 U1: node 派生 ──────────────────────────────────────────────────────────


def test_derive_current_node_from_last_node_started(tmp_path):
    """emit 前增量扫 tape，取最后一条 node_started 的 node。"""
    bus, tape, tape_path = _make_bus(tmp_path)
    try:
        # 模拟 cli.next 已写 ws + ns[nodeA]。
        _append_event(tape_path, "workflow_started", seq=1)
        _append_event(tape_path, "node_started", seq=2, node="A")
        ing = SidechainIngestor(bus, tape_path)
        ing.rebuild_from_tape()
        assert ing.current_node == "A"

        _run(ing.ingest(RawAgentEvent("c", "s1", "thinking", {"text": "T"})))
        events = list(tape.replay())
        agent_events = [e for e in events if e.type == "agent_thinking"]
        assert len(agent_events) == 1
        assert agent_events[0].node == "A", "agent_* 应挂在最后 node_started 的 node"
    finally:
        bus.close()


def test_derive_current_node_updates_incrementally(tmp_path):
    """cli.next 推进到下一节点（append node_completed + node_started）→ ingest 增量反映新 node。

    U1 关键：daemon ingestor 不重扫全文，只读「上次 offset → EOF」新字节。
    """
    bus, tape, tape_path = _make_bus(tmp_path)
    try:
        _append_event(tape_path, "workflow_started", seq=1)
        _append_event(tape_path, "node_started", seq=2, node="A")
        ing = SidechainIngestor(bus, tape_path)
        ing.rebuild_from_tape()
        assert ing.current_node == "A"

        # ingest 一条（挂在 A）。
        _run(ing.ingest(RawAgentEvent("c", "s1", "thinking", {"text": "on A"})))

        # 模拟 cli.next 推进：append node_completed[A] + node_started[B]。
        _append_event(tape_path, "node_completed", seq=4, node="A", data={"output": "ok"})
        _append_event(tape_path, "node_started", seq=5, node="B")

        # 再 ingest → 应挂在 B（增量扫到新 node_started）。
        _run(ing.ingest(RawAgentEvent("c", "s2", "thinking", {"text": "on B"})))

        events = list(tape.replay())
        agent_events = [e for e in events if e.type == "agent_thinking"]
        assert len(agent_events) == 2
        assert agent_events[0].node == "A"
        assert agent_events[1].node == "B", "增量扫到 node_started[B] → 后续事件挂 B"
    finally:
        bus.close()


def test_derive_current_node_handles_partial_line(tmp_path):
    """partial node_started 行（无 \\n）→ _derive_current_node 沿用上次 node（下次重读）。

    生产中 daemon 的 _FlockSafeTape.append 与 cli.next flock 互斥 → 永远不会看到 partial；
    此测试**直接调 _derive_current_node**（绕过 bus.emit 的 tape.append），隔离验证派生逻辑。
    """
    bus, _, tape_path = _make_bus(tmp_path)
    try:
        _append_event(tape_path, "workflow_started", seq=1)
        _append_event(tape_path, "node_started", seq=2, node="A")
        ing = SidechainIngestor(bus, tape_path)
        ing.rebuild_from_tape()
        assert ing.current_node == "A"

        # 写 partial 行（无 \n）：模拟 cli.next 的 write 被打断（生产 flock 不会让 daemon 看到）。
        with open(tape_path, "ab") as f:
            f.write(b'{"seq": 3, "type": "node_started", "node": "B", ')  # partial JSON

        # 直接调 _derive_current_node → 沿用 A（partial 不解析）。
        assert ing._derive_current_node() == "A", "partial 行未推进 _current_node"

        # 补完 partial → 下次调 _derive_current_node 应反映 B。
        with open(tape_path, "ab") as f:
            f.write(b'"timestamp": 0.0, "session_id": null, "data": {}}\n')
        assert ing._derive_current_node() == "B", "partial 补完后增量扫到 → 派生 B"
    finally:
        bus.close()


def test_derive_current_node_none_when_no_node_started(tmp_path):
    """tape 无 node_started → ingest 的事件 node=None（前端 LogStream 仍能渲染）。"""
    bus, tape, tape_path = _make_bus(tmp_path)
    try:
        ing = SidechainIngestor(bus, tape_path)
        ing.rebuild_from_tape()
        assert ing.current_node is None

        _run(ing.ingest(RawAgentEvent("c", "s1", "thinking", {"text": "T"})))
        events = list(tape.replay())
        assert events[0].node is None
    finally:
        bus.close()


# ── 边界 ──────────────────────────────────────────────────────────────────────


def test_rebuild_handles_missing_tape(tmp_path):
    """tape 不存在 → rebuild 静默返（首次启动等价；不抛）。"""
    tape_path = tmp_path / "runs" / "missing.jsonl"
    bus = EventBus(Tape(tape_path, run_id="r"))
    try:
        ing = SidechainIngestor(bus, tape_path)
        ing.rebuild_from_tape()
        assert len(ing.seen_source_ids) == 0
        assert ing.current_node is None
    finally:
        bus.close()


def test_ingest_payload_not_mutated(tmp_path):
    """ingestor 不应改 raw.payload（R2 零 rename，含不修改入参 dict）。"""
    bus, tape, tape_path = _make_bus(tmp_path)
    try:
        ing = SidechainIngestor(bus, tape_path)
        raw = RawAgentEvent("c", "s1", "tool_call",
                            {"tool": "Read", "args": {"x": 1}, "tool_call_id": "t1"})
        original_payload = dict(raw.payload)
        _run(ing.ingest(raw))
        assert raw.payload == original_payload, "入参 payload 不应被修改"
    finally:
        bus.close()

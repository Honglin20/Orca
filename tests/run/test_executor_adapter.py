"""tests/run/test_executor_adapter.py —— executor→bus 桥接（计划 R3.2）。

覆盖：
  - FakeExecutor 产出 [node_started, agent_message, node_completed(output)] →
    adapter 调 bus.emit 3 次，返回 raw output
  - node_failed → adapter raise ExecError（透传 phase / error_type）
  - bus.emit 次数 == 事件数（无丢失）
  - 生命周期违约（既无 completed 也无 failed）→ raise ExecError

用 FakeExecutor 注入确定 output 流，验证桥接逻辑（不 spawn claude）。
真 claude 在 R5 demo 端到端覆盖。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.exec.context import RunContext
from orca.exec.error import ExecError
from orca.exec.interface import Executor
from orca.run.executor_adapter import execute_and_emit
from orca.schema import Event, ScriptNode


def _run(coro):
    """本仓库约定：异步测试统一 asyncio.run（无 pytest-asyncio）。"""
    return asyncio.run(coro)


def _bus(tmp_path: Path, run_id: str = "r1") -> tuple[EventBus, Tape]:
    tape = Tape(tmp_path / "events.jsonl", run_id=run_id)
    return EventBus(tape), tape


class FakeExecutor(Executor):
    """注入确定事件流的假 executor（不 spawn）。

    每个事件 yield 出来；adapter 应逐个 bus.emit。
    """

    def __init__(self, events: list[Event]):
        self._events = events

    async def exec(self, node, ctx) -> AsyncIterator[Event]:  # type: ignore[override]
        for ev in self._events:
            yield ev


def _ev(type_: str, data: dict, node: str = "n", session_id: str = "s1") -> Event:
    """构造占位 Event（seq=0，bus.emit 内部会重分配 seq）。"""
    return Event(seq=0, type=type_, timestamp=0.0, node=node, session_id=session_id, data=data)


def _ctx() -> RunContext:
    return RunContext(inputs={}, outputs={}, run_id="r1")


# ── happy path ────────────────────────────────────────────────────────────────


def test_execute_and_emit_emits_all_events_and_returns_output(tmp_path):
    """3 个事件全 emit；node_completed 的 output 被返回（raw，未包装）。"""
    events = [
        _ev("node_started", {"kind": "script"}),
        _ev("agent_message", {"text": "hi"}),  # 模拟中间流式事件
        _ev("node_completed", {"output": {"x": 1}, "elapsed": 0.1}),
    ]
    bus, tape = _bus(tmp_path)
    node = ScriptNode(name="n", command="echo", routes=[])

    output = _run(execute_and_emit(FakeExecutor(events), node, _ctx(), bus))

    assert output == {"x": 1}  # raw output（adapter 不包 {"output": raw}）
    # Tape 写了 3 行（emit 次数 == 事件数，无丢失）
    seqs = [e.seq for e in tape.replay()]
    assert seqs == [1, 2, 3]
    types = [e.type for e in tape.replay()]
    assert types == ["node_started", "agent_message", "node_completed"]


def test_execute_and_emit_session_id_passthrough(tmp_path):
    """session_id 透传到 bus.emit（reducer / 前端按它分组）。"""
    events = [
        _ev("node_started", {}, session_id="sess-xyz"),
        _ev("node_completed", {"output": None}, session_id="sess-xyz"),
    ]
    bus, tape = _bus(tmp_path)
    node = ScriptNode(name="n", command="echo", routes=[])
    _run(execute_and_emit(FakeExecutor(events), node, _ctx(), bus))
    events_replayed = list(tape.replay())
    assert all(e.session_id == "sess-xyz" for e in events_replayed)


# ── node_failed ───────────────────────────────────────────────────────────────


def test_execute_and_emit_node_failed_raises_exec_error(tmp_path):
    """executor yield node_failed → adapter raise ExecError（透传 phase/error_type）。"""
    events = [
        _ev("node_started", {}),
        _ev("node_failed", {
            "error_type": "ExecTimeout",
            "message": "超时",
            "phase": "timeout",
        }),
    ]
    bus, _ = _bus(tmp_path)
    node = ScriptNode(name="n", command="echo", routes=[])

    with pytest.raises(ExecError) as ei:
        _run(execute_and_emit(FakeExecutor(events), node, _ctx(), bus))
    assert ei.value.phase == "timeout"
    assert ei.value.error_type == "ExecTimeout"
    assert "超时" in ei.value.message


def test_execute_and_emit_node_failed_writes_tape_before_raising(tmp_path):
    """node_failed 事件本身应先 emit 落 Tape，再 raise（事件流完整）。"""
    events = [
        _ev("node_started", {}),
        _ev("node_failed", {"error_type": "X", "message": "m", "phase": "spawn"}),
    ]
    bus, tape = _bus(tmp_path)
    node = ScriptNode(name="n", command="echo", routes=[])

    with pytest.raises(ExecError):
        _run(execute_and_emit(FakeExecutor(events), node, _ctx(), bus))

    types = [e.type for e in tape.replay()]
    assert types == ["node_started", "node_failed"]  # 失败事件已落 Tape


# ── 生命周期违约 ──────────────────────────────────────────────────────────────


def test_execute_and_emit_no_terminal_event_raises(tmp_path):
    """executor 既无 completed 也无 failed → raise（生命周期违约，fail loud）。"""
    events = [_ev("node_started", {})]  # 没有 node_completed
    bus, _ = _bus(tmp_path)
    node = ScriptNode(name="n", command="echo", routes=[])

    with pytest.raises(ExecError, match="生命周期违约"):
        _run(execute_and_emit(FakeExecutor(events), node, _ctx(), bus))


def test_execute_and_emit_empty_stream_raises(tmp_path):
    """空事件流（连 node_started 都没有）→ raise（违约）。"""
    bus, _ = _bus(tmp_path)
    node = ScriptNode(name="n", command="echo", routes=[])

    with pytest.raises(ExecError):
        _run(execute_and_emit(FakeExecutor([]), node, _ctx(), bus))

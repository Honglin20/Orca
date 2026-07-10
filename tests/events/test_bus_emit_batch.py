"""tests/events/test_bus_emit_batch.py —— EventBus.emit_batch 透传 + fan-out（B1）。

覆盖：透传 ``Tape.append_batch``（seq 连续 / 单次 write）+ 逐条 fan-out 订阅者
（顺序与 items 一致）+ 空 list no-op + timestamp 默认填充。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from orca.events.bus import EventBus
from orca.events.tape import Tape


def _run(coro):
    return asyncio.run(coro)


def _bus(tmp_path: Path, run_id: str = "r1") -> tuple[EventBus, Tape]:
    tape = Tape(tmp_path / "events.jsonl", run_id=run_id)
    return EventBus(tape), tape


def _item(type: str = "node_started", **payload) -> dict:
    return {"type": type, "data": payload, "node": payload.pop("_node", "n")}


def test_emit_batch_writes_all_to_tape(tmp_path):
    bus, tape = _bus(tmp_path)
    try:
        events = _run(bus.emit_batch([
            _item("node_started", x=1),
            _item("route_taken", x=2),
            _item("node_completed", x=3),
        ]))
        assert [e.seq for e in events] == [1, 2, 3]
        assert [e.type for e in events] == ["node_started", "route_taken", "node_completed"]
        assert tape.last_seq() == 3
    finally:
        bus.close()


def test_emit_batch_fans_out_in_order(tmp_path):
    bus, tape = _bus(tmp_path)
    try:
        sub = bus.subscribe()
        events = _run(bus.emit_batch([
            _item("node_started"),
            _item("route_taken"),
            _item("node_completed"),
        ]))

        async def collect():
            out = []
            async for e in sub.events():
                out.append(e)
                if len(out) == 3:
                    break
            return out
        received = _run(collect())
        assert [r.seq for r in received] == [e.seq for e in events]
        assert [r.type for r in received] == ["node_started", "route_taken", "node_completed"]
    finally:
        bus.close()


def test_emit_batch_empty_list_noop(tmp_path):
    bus, tape = _bus(tmp_path)
    try:
        events = _run(bus.emit_batch([]))
        assert events == []
        assert tape.last_seq() == 0
    finally:
        bus.close()


def test_emit_batch_default_timestamp_filled(tmp_path):
    bus, tape = _bus(tmp_path)
    try:
        events = _run(bus.emit_batch([_item("node_started")]))
        assert events[0].timestamp > 0
    finally:
        bus.close()

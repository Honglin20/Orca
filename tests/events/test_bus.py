"""tests/events/test_bus.py —— EventBus：持有 Tape + 异步 fan-out + session_id 透传。

覆盖 SPEC §6.3：emit 构造 Event + 写 Tape + 通知订阅者 / session_id 透传到事件顶层 /
异步：慢订阅者不阻塞 emit / per-cursor（订阅者 A 落后不影响 B）/ 队列满丢最老 + warning。

注：本仓库 dev 依赖仅 pytest（无 pytest-asyncio），异步测试统一用 ``asyncio.run``。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from orca.events.bus import EventBus, Subscription
from orca.events.tape import Tape


def _run(coro):
    return asyncio.run(coro)


def _bus(tmp_path: Path, run_id: str = "r1") -> tuple[EventBus, Tape]:
    tape = Tape(tmp_path / "events.jsonl", run_id=run_id)
    return EventBus(tape), tape


# ── emit 构造 Event + 写 Tape + 通知订阅者 ────────────────────────────────────


def test_emit_writes_tape_and_returns_event(tmp_path):
    bus, tape = _bus(tmp_path)
    try:
        ev = _run(bus.emit("node_started", {"x": 1}, node="a", session_id="s1"))
        assert ev.seq == 1
        assert ev.type == "node_started"
        assert ev.node == "a"
        assert ev.session_id == "s1"
        assert ev.data == {"x": 1}
        # Tape 有 1 行（唯一真相源已写）
        assert tape.last_seq() == 1
    finally:
        bus.close()


def test_emit_first_action_writes_tape(tmp_path):
    """emit 第一动作 = Tape.append（唯一真相强制副作用，SPEC §3.3）。

    即使没有订阅者，事件也落盘（订阅者非必需）。
    """
    bus, tape = _bus(tmp_path)
    try:
        # 不订阅，直接 emit
        _run(bus.emit("workflow_started", {"entry": "a"}))
        assert tape.last_seq() == 1
    finally:
        bus.close()


# ── session_id 透传 ──────────────────────────────────────────────────────────


def test_session_id_transparent_passthrough(tmp_path):
    """emit(..., session_id=s) 写入事件顶层 session_id（SPEC §6.3 / §6.4）。

    reducer/前端按 session_id 分组；retry 场景同 node 不同 session_id 可区分。
    """
    bus, tape = _bus(tmp_path)
    try:
        _run(bus.emit("agent_message", {"text": "hi"}, node="a", session_id="s1"))
        _run(bus.emit("agent_message", {"text": "retry"}, node="a", session_id="s2"))
        events = list(tape.replay())
        assert [e.session_id for e in events] == ["s1", "s2"]
        # 同 node 不同 session_id —— replay 可区分
        assert all(e.node == "a" for e in events)
    finally:
        bus.close()


def test_session_id_none_for_workflow_events(tmp_path):
    """workflow 级事件 session_id=None（身份模型：workflow 级两者皆 None）。"""
    bus, tape = _bus(tmp_path)
    try:
        _run(bus.emit("workflow_started", {"entry": "a"}))
        events = list(tape.replay())
        assert events[0].session_id is None
        assert events[0].node is None
    finally:
        bus.close()


# ── 异步：慢订阅者不阻塞 emit / per-cursor ───────────────────────────────────


def test_slow_subscriber_does_not_block_emit(tmp_path):
    """慢订阅者不阻塞 emitter（SPEC §6.3 / §11 决策 8 / §6.8）。

    fan-out 是 put_nowait（非阻塞）。慢订阅者（每事件 sleep）不影响 emit 速率。
    emit 100 个事件到慢订阅者（未消费），emit 必须快速完成（不阻塞），
    tape 100 条立即落盘。
    """
    import time

    bus, tape = _bus(tmp_path)
    try:
        sub = bus.subscribe()  # 慢订阅者：从不消费（cursor 不动）

        async def scenario():
            t0 = time.monotonic()
            for i in range(100):
                await bus.emit("node_started", {"i": i}, node="n")
            elapsed = time.monotonic() - t0
            return elapsed

        elapsed = _run(scenario())
        # 100 个 put_nowait 极快（< 1s）；若是阻塞 fan-out，订阅者不消费会卡死
        assert elapsed < 1.0, f"emit 被慢订阅者阻塞：{elapsed:.2f}s"
        # tape（唯一真相）立即有 100 条
        assert tape.last_seq() == 100
        assert len(list(tape.replay())) == 100
    finally:
        bus.close()


def test_per_consumer_cursor_isolation(tmp_path):
    """per-consumer cursor：订阅者 A 落后时，订阅者 B 不受影响（SPEC §3.3 规则 5）。

    A 的队列满（小 maxsize + 不消费），继续 emit；B 应仍能收到所有事件
    （各自独立 queue，A 丢老不影响 B）。
    """
    bus, tape = _bus(tmp_path)
    try:
        sub_a = bus.subscribe(queue_max=2)  # A 小队列 + 不消费 → 很快满
        sub_b = bus.subscribe()  # B 正常

        async def scenario():
            # emit 5 个：A 队列满后丢老，B 应全收到
            for i in range(5):
                await bus.emit("node_started", {"i": i}, node="n")
            # B 排空全部
            received_b = await _drain_n(sub_b, 5)
            return received_b

        received_b = _run(scenario())
        # B 不受 A 落后影响，收到全部 5 个
        assert [e.seq for e in received_b] == [1, 2, 3, 4, 5]
        # A 的 cursor 因丢老策略仍推进到最新（5），但 queue 里只剩最近 2 个
        assert sub_a.cursor == 5
    finally:
        bus.close()


async def _drain_n(sub: Subscription, n: int) -> list:
    """从 subscription 收 n 个事件。"""
    out = []
    async for ev in sub.events():
        out.append(ev)
        if len(out) >= n:
            break
    return out


def test_subscriber_receives_events(tmp_path):
    """订阅者通过 events() 异步迭代收到 emit 的事件。"""
    bus, tape = _bus(tmp_path)
    try:
        sub = bus.subscribe()

        async def scenario():
            await bus.emit("node_started", {"i": 0}, node="n")
            await bus.emit("node_completed", {"output": "ok"}, node="n")
            received = await _drain_n(sub, 2)
            return received

        received = _run(scenario())
        assert [e.type for e in received] == ["node_started", "node_completed"]
    finally:
        bus.close()


# ── 队列满：丢最老 + warning ──────────────────────────────────────────────────


def test_queue_full_drops_oldest_with_warning(tmp_path, caplog):
    """队列满：丢最老事件 + warning，不抛异常、不阻塞 emitter（SPEC §3.3 / §6.3）。

    订阅者靠 replay 补全（唯一真相源保证不丢真相）。
    """
    bus, tape = _bus(tmp_path)
    try:
        sub = bus.subscribe(queue_max=2)

        async def scenario():
            # 不消费，emit 超过容量 2 个 → 第 3 个触发丢老
            await bus.emit("node_started", {"i": 0}, node="n")
            await bus.emit("node_started", {"i": 1}, node="n")
            await bus.emit("node_started", {"i": 2}, node="n")  # 触发丢老

        with caplog.at_level(logging.WARNING):
            _run(scenario())
        # 不抛异常，且记 warning
        assert any("队列满" in r.message for r in caplog.records)
        # tape（唯一真相）仍完整保留 3 条 —— 订阅者可经 replay 补全
        assert tape.last_seq() == 3
        assert len(list(tape.replay())) == 3
    finally:
        bus.close()


# ── close ────────────────────────────────────────────────────────────────────


def test_close_terminates_subscription_iteration(tmp_path):
    """bus close 投递哨兵，订阅者 events() 迭代正常终止。"""
    bus, _ = _bus(tmp_path)
    sub = bus.subscribe()

    async def scenario():
        await bus.emit("node_started", {}, node="n")
        bus.close()
        out = []
        async for ev in sub.events():
            out.append(ev)
        return out

    received = _run(scenario())
    assert len(received) == 1  # 哨兵前收到 1 个，之后终止


def test_cancelled_subscription_not_enqueued(tmp_path):
    """cancelled 订阅者：emit 不再向其投递（_enqueue 早退）。"""
    bus, _ = _bus(tmp_path)
    sub = bus.subscribe()
    sub.cancel()

    async def scenario():
        await bus.emit("node_started", {}, node="n")
        # cancelled 后 cursor 不更新（_enqueue 直接 return）
        return sub.cursor

    cursor = _run(scenario())
    assert cursor == 0  # 未投递


# ── fail loud：非法 type 在 emit 时报错（不延迟到 replay）────────────────────


def test_emit_invalid_type_fails_loud(tmp_path):
    """非法 type 在 emit 时（Tape.append 校验）即报错，不延迟到 replay（SPEC §6.0 铁律4）。

    review m1 修复：append 落盘前构造 Event 校验，type 不在 EventType → 立即抛。
    """
    import pytest
    from pydantic import ValidationError

    bus, tape = _bus(tmp_path)
    try:

        async def scenario():
            # type='bogus' 不在 EventType Literal → ValidationError
            await bus.emit("bogus_type", {}, node="n")

        with pytest.raises((ValidationError, ValueError)):
            _run(scenario())
        # 坏事件未落盘（tape 仍空）
        assert tape.last_seq() == 0
    finally:
        bus.close()


def test_session_id_end_to_end_emit_tape_replay(tmp_path):
    """session_id 端到端：bus.emit → tape 落盘 → replay 按 session_id 分组（SPEC §6.8）。

    整条链路（emit 写 session_id → tape 存 → replay 读）一气呵成，捕获任何中间丢字段。
    """
    bus, tape = _bus(tmp_path)
    try:

        async def scenario():
            # 同 node 两个 session（retry 场景）
            await bus.emit("agent_message", {"text": "first"}, node="a", session_id="s1")
            await bus.emit("agent_message", {"text": "retry"}, node="a", session_id="s2")

        _run(scenario())
    finally:
        bus.close()

    tape2 = Tape(tape.path, run_id="r1")
    try:
        events = list(tape2.replay())
        # 按 session_id 分组可区分（端到端未丢 session_id）
        by_session = {}
        for e in events:
            by_session.setdefault(e.session_id, []).append(e.data["text"])
        assert by_session == {"s1": ["first"], "s2": ["retry"]}
    finally:
        tape2.close()


def test_close_with_full_queue_warns(tmp_path, caplog):
    """close 时队列满：丢弃未消费事件 + warning（review M3 修复，SPEC §6.0 铁律4）。"""
    bus, _ = _bus(tmp_path)
    sub = bus.subscribe(queue_max=1)

    async def scenario():
        # 填满队列（1 个），再 close 时哨兵投递会触发丢老 + warning
        await bus.emit("node_started", {"i": 0}, node="n")
        with caplog.at_level(logging.WARNING):
            bus.close()

    _run(scenario())
    assert any("close 时订阅者队列满" in r.message for r in caplog.records)

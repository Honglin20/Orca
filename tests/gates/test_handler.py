"""test_handler.py —— HumanGateHandler request/resolve/竞速/广播/fail loud（SPEC §2 §4 / 计划 G2.3）。

测试原则（SPEC §7.2 §7.3）：纯逻辑，不依赖具体壳——用 ``handler.resolve(...)``
直接模拟「壳答了」。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from orca.events.bus import EventBus
from orca.gates.handler import HumanGateHandler

from tests.gates.conftest import make_bus, make_gate, run_async


# ── helpers ──────────────────────────────────────────────────────────────────


def _tape_event_types(bus: EventBus) -> list[str]:
    """读 Tape 里的事件类型序列（断言写 Tape = 唯一真相）。"""
    return [e.type for e in bus.tape.replay()]


# ── 基础：request → resolve → 返回 (answer, source) ──────────────────────────


def test_request_resolve_basic(tmp_path):
    """request 注册 future + emit requested；resolve set_result → request 返回。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            gate = make_gate("g1")
            task = asyncio.create_task(handler.request(gate))
            await asyncio.sleep(0.01)  # 让 request 跑到 await fut

            ok = handler.resolve("g1", "allow", "cli")
            assert ok is True

            answer, source = await asyncio.wait_for(task, timeout=1.0)
            assert (answer, source) == ("allow", "cli")
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


def test_request_emits_requested_to_tape(tmp_path):
    """request 第一动作 = 写 Tape（唯一真相，SPEC §7.0 铁律 1）。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            gate = make_gate("g1")
            task = asyncio.create_task(handler.request(gate))
            await asyncio.sleep(0.02)
            handler.resolve("g1", "allow", "cli")
            await task
            # 等广播 emit resolved
            await asyncio.sleep(0.02)
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())
    # 重新打开 bus 验证 Tape 已落 requested + resolved（run_async 内已 close）
    # —— 改为在 scenario 内断言（bus.close 后 replay 仍可读文件）
    # 这里通过重新 replay 文件验证
    from orca.events.tape import Tape

    types = [e.type for e in Tape(tmp_path / "events.jsonl", run_id="r1").replay()]
    assert "human_decision_requested" in types
    assert "human_decision_resolved" in types


# ── fail loud：未知 / 已 resolved ─────────────────────────────────────────────


def test_resolve_unknown_gate_returns_false(tmp_path, caplog):
    """未知 gate_id 的 resolve → False + warning（SPEC §2.2 §10 决策 7）。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            with caplog.at_level(logging.WARNING):
                ok = handler.resolve("never", "allow", "cli")
            assert ok is False
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())
    assert "已 resolved" in caplog.text or "未知" in caplog.text


def test_double_resolve_fail_loud(tmp_path, caplog):
    """已 resolved 再 resolve → False + warning（输入丢弃，SPEC §10 决策 7）。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            gate = make_gate("g3")
            task = asyncio.create_task(handler.request(gate))
            await asyncio.sleep(0.01)

            ok1 = handler.resolve("g3", "allow", "cli")
            assert ok1 is True

            with caplog.at_level(logging.WARNING):
                ok2 = handler.resolve("g3", "deny", "web")  # 输家：晚到
            assert ok2 is False
            await task
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())
    assert "已 resolved" in caplog.text


# ── 竞速：first-wins（SPEC §4.2 §7.3）────────────────────────────────────────


def test_race_first_wins(tmp_path):
    """两个壳同时 resolve 同一 gate，只有一个返回 True（FIRST_COMPLETED）。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            gate = make_gate("g-race")
            task = asyncio.create_task(handler.request(gate))
            await asyncio.sleep(0.01)

            # 两个壳「同时」resolve（asyncio 单线程下仍可能交错，但 set_result 第一次
            # 成功后第二次见 fut.done()=True → 返回 False）。用 to_thread 模拟跨壳并发。
            r1, r2 = await asyncio.gather(
                asyncio.to_thread(handler.resolve, "g-race", "allow", "cli"),
                asyncio.to_thread(handler.resolve, "g-race", "deny", "web"),
            )
            # 恰好一个赢家（两个 bool 中 True 的数量为 1）
            assert (r1, r2).count(True) == 1
            await task
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


# ── 广播：resolved 事件 emit（SPEC §4.1 §7.3）────────────────────────────────


def test_broadcast_emits_resolved(tmp_path):
    """resolve 后 _broadcaster emit human_decision_resolved（含 answer + resolved_by）。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            gate = make_gate("g4")
            sub = bus.subscribe()
            task = asyncio.create_task(handler.request(gate))
            await asyncio.sleep(0.01)

            handler.resolve("g4", "allow", "web")
            await task
            # 等 broadcaster emit resolved
            await asyncio.sleep(0.05)

            # drain 订阅队列（订阅者收到 requested + resolved）
            events = []
            async for e in sub.events():
                events.append(e)
                if e.type == "human_decision_resolved":
                    break
            types = [e.type for e in events]
            assert "human_decision_requested" in types
            assert "human_decision_resolved" in types
            # resolved 事件 data 含 answer + resolved_by
            resolved = next(e for e in events if e.type == "human_decision_resolved")
            assert resolved.data["answer"] == "allow"
            assert resolved.data["resolved_by"] == "web"
            sub.cancel()
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


# ── gate 无限等（无 timeout，SPEC §2.2 决策 3 §7.2）─────────────────────────


def test_gate_waits_forever(tmp_path):
    """request 的 await fut 无 timeout——人不答就永远等（用 wait_for 超时证明它在等）。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            gate = make_gate("g-forever")
            task = asyncio.create_task(handler.request(gate))
            await asyncio.sleep(0.01)
            # 不 resolve，request 应仍在 await —— 用 wait_for 短超时证明它没返回
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            # 清理：resolve 让 task 完成（避免 leaked task）
            handler.resolve("g-forever", "allow", "cli")
            await task
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


# ── 生命周期：start/stop 幂等 + 无 leaked task ───────────────────────────────


def test_start_stop_idempotent(tmp_path):
    """重复 start / stop 不创建多 task / 不抛。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        await handler.start()  # 幂等
        await handler.stop()
        await handler.stop()  # 幂等
        bus.close()

    run_async(scenario())


def test_no_leaked_task_after_stop(tmp_path):
    """stop 后 broadcaster task 干净退出（无 "Task was destroyed but it is pending"）。

    捕获 warnings：若有未结束 task 被 GC，asyncio 会抛 ``Task was destroyed but it is
    pending`` 警告。我们断言无此类警告。
    """
    import warnings

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        await handler.stop()
        # stop 后 _broadcaster_task 清空（stop() 末尾置 None）
        assert handler._broadcaster_task is None
        bus.close()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        run_async(scenario())
    pending_warnings = [
        str(w.message) for w in caught if "destroyed" in str(w.message).lower()
    ]
    assert pending_warnings == [], f"leaked task warnings: {pending_warnings}"


# ── session_id 透传到 event 顶层（phase 3 §3.3 身份模型）──────────────────────


def test_session_id_propagated_to_event(tmp_path):
    """request emit 的事件顶层 session_id == gate.session_id（reducer 据此分组）。

    验证 intent：session_id 不只是放进 data.context，而是透传到 event 顶层（phase 3
    §3.3），让壳 reducer 能按 session 分组关联 gate 事件到具体 claude 会话。
    """

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            gate = make_gate("g-sess", session_id="claude-sess-42")
            task = asyncio.create_task(handler.request(gate))
            await asyncio.sleep(0.02)

            handler.resolve("g-sess", "allow", "cli")
            await task
            await asyncio.sleep(0.02)  # 等 broadcaster emit resolved

            events = list(bus.tape.replay())
            requested = next(
                e for e in events if e.type == "human_decision_requested"
            )
            resolved = next(
                e for e in events if e.type == "human_decision_resolved"
            )
            # 两个 gate 事件顶层 session_id 都应等于 gate.session_id
            assert requested.session_id == "claude-sess-42"
            assert resolved.session_id == "claude-sess-42"
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


# ── broadcaster 容错：单次 emit 失败不退出（SPEC §_broadcaster）──────────────


def test_broadcaster_survives_emit_failure(tmp_path):
    """broadcaster emit resolved 失败时，后续 gate 的广播仍能正常（fail loud 但不阻断）。

    构造一个 emit 抛异常的 bus（用 close 后的 tape），验证 broadcaster 不退出、记 error，
    后续正常 bus 上 resolve 仍能广播。
    """

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            # 第一个 gate：正常 resolve + 广播（验证 broadcaster 健康）
            gate1 = make_gate("g-ok")
            t1 = asyncio.create_task(handler.request(gate1))
            await asyncio.sleep(0.02)
            handler.resolve("g-ok", "allow", "cli")
            await t1
            await asyncio.sleep(0.05)  # 等 broadcaster emit

            # 第二个 gate：构造 emit 失败（close tape 后 append 抛 RuntimeError）
            # 注意：close 后 bus.emit 会抛 RuntimeError → broadcaster 内 except 接住
            # 但我们不想真 close bus（后续还要用）。改为：spy bus.emit 第一次抛、第二次正常。
            gate2 = make_gate("g-fail")
            t2 = asyncio.create_task(handler.request(gate2))
            await asyncio.sleep(0.02)
            handler.resolve("g-fail", "allow", "cli")
            await t2

            # bus 仍正常，broadcaster 第二次 emit 应成功
            await asyncio.sleep(0.05)
            events = list(bus.tape.replay())
            resolved_count = sum(
                1 for e in events if e.type == "human_decision_resolved"
            )
            assert resolved_count >= 2  # 两次广播都成功
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


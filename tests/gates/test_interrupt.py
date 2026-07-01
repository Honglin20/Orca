"""test_interrupt.py —— InterruptHandler request/resolve/竞速/广播/fail loud（SPEC §3.3）。

测试原则（同 test_handler.py）：纯逻辑，不依赖具体壳——用 ``handler.resolve(...)``
直接模拟「壳答了」。覆盖：
  - request → 不返回 → resolve → 返回 (action, guidance)
  - first-wins race（双 source 并发 resolve）
  - broadcaster emit ``interrupt_resolved`` 写 Tape
  - idempotent start/stop
  - 线程安全（多线程并发 resolve 同 interrupt_id）
"""

from __future__ import annotations

import asyncio
import threading

from orca.events.bus import EventBus
from orca.gates.interrupt import InterruptHandler
from orca.gates.types import InterruptRequest

from tests.gates.conftest import make_bus, run_async


# ── helpers ──────────────────────────────────────────────────────────────────


def make_ireq(
    interrupt_id: str = "i1",
    *,
    node: str = "cfg",
    run_id: str = "r1",
    session_id: str | None = "sess-test",
    source: str = "cli",
    elapsed: float = 12.5,
) -> InterruptRequest:
    """构造测试用 InterruptRequest（默认 cli source + 中断 node=cfg）。"""
    return InterruptRequest(
        id=interrupt_id,
        node=node,
        run_id=run_id,
        session_id=session_id,
        source=source,  # type: ignore[arg-type]
        elapsed_at_request=elapsed,
    )


# ── 基础：request → resolve → 返回 (action, guidance) ──────────────────────


def test_request_blocks_until_resolved(tmp_path):
    """request 注册 future + emit requested；resolve set_result → request 返回。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = InterruptHandler(bus)
        await handler.start()
        try:
            ireq = make_ireq("i1")
            task = asyncio.create_task(handler.request(ireq))
            await asyncio.sleep(0.01)  # 让 request 跑到 await fut

            ok = handler.resolve("i1", "continue", "用更保守的方案", "cli")
            assert ok is True

            action, guidance = await asyncio.wait_for(task, timeout=1.0)
            assert action == "continue"
            assert guidance == "用更保守的方案"
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


def test_request_emits_requested_to_tape(tmp_path):
    """request 第一动作 = 写 Tape（唯一真相，SPEC §7.0 铁律 1）。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = InterruptHandler(bus)
        await handler.start()
        try:
            ireq = make_ireq("i1")
            task = asyncio.create_task(handler.request(ireq))
            await asyncio.sleep(0.02)
            handler.resolve("i1", "skip", None, "cli")
            await task
            await asyncio.sleep(0.02)  # 等广播 emit resolved
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())
    # 验证 Tape 已落 requested + resolved
    from orca.events.tape import Tape

    types = [e.type for e in Tape(tmp_path / "events.jsonl", run_id="r1").replay()]
    assert "interrupt_requested" in types
    assert "interrupt_resolved" in types


# ── first-wins race（SPEC §4.2 同款，interrupt 版）──────────────────────────


def test_resolve_first_wins(tmp_path, caplog):
    """双 source 并发 resolve 同 interrupt_id → 只第一个赢，第二个返回 False。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = InterruptHandler(bus)
        await handler.start()
        try:
            ireq = make_ireq("i1")
            task = asyncio.create_task(handler.request(ireq))
            await asyncio.sleep(0.01)

            ok1 = handler.resolve("i1", "continue", "g1", "cli")
            ok2 = handler.resolve("i1", "skip", None, "web")  # 第二个 source
            assert ok1 is True
            assert ok2 is False  # first-wins

            action, guidance = await asyncio.wait_for(task, timeout=1.0)
            assert action == "continue"
            assert guidance == "g1"  # 赢家的 guidance 生效
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


def test_resolve_unknown_interrupt_returns_false(tmp_path, caplog):
    """未知 interrupt_id 的 resolve → False + warning（fail loud）。"""
    caplog.set_level("WARNING")

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = InterruptHandler(bus)
        await handler.start()
        try:
            ok = handler.resolve("nonexistent", "continue", None, "cli")
            assert ok is False
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())
    assert "nonexistent" in caplog.text


# ── broadcaster emit resolved（SPEC §3.3 broadcaster）────────────────────────


def test_broadcaster_emits_resolved_to_tape(tmp_path):
    """resolve → broadcaster emit interrupt_resolved（含正确 payload）写 Tape。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = InterruptHandler(bus)
        await handler.start()
        try:
            ireq = make_ireq("i1", node="runner", session_id="sess-x")
            task = asyncio.create_task(handler.request(ireq))
            await asyncio.sleep(0.02)
            handler.resolve("i1", "continue", "skip weights", "cli")
            await task
            await asyncio.sleep(0.05)  # 等 broadcaster emit
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())
    from orca.events.tape import Tape

    events = list(Tape(tmp_path / "events.jsonl", run_id="r1").replay())
    resolved = next(e for e in events if e.type == "interrupt_resolved")
    assert resolved.data["interrupt_id"] == "i1"
    assert resolved.data["action"] == "continue"
    assert resolved.data["guidance"] == "skip weights"
    assert resolved.data["resolved_by"] == "cli"
    # node / session_id 透传到 event 顶层（与 requested 一致）
    assert resolved.node == "runner"
    assert resolved.session_id == "sess-x"


# ── idempotent start/stop ────────────────────────────────────────────────────


def test_idempotent_start(tmp_path):
    """重复 start 不创建第二个 task（幂等）。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = InterruptHandler(bus)
        await handler.start()
        task1 = handler._broadcaster_task
        await handler.start()  # 幂等
        task2 = handler._broadcaster_task
        assert task1 is task2  # 同一个 task，没创建新的
        await handler.stop()
        bus.close()

    run_async(scenario())


def test_idempotent_stop(tmp_path):
    """未 start / 已 stop 直接 stop 不报错（幂等）。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = InterruptHandler(bus)
        # 未 start 就 stop
        await handler.stop()
        # start → stop → 再 stop
        await handler.start()
        await handler.stop()
        await handler.stop()
        bus.close()

    run_async(scenario())


# ── 线程安全（threading.Lock，跨线程 resolve 并发）──────────────────────────


def test_concurrent_resolve_from_threads_only_one_wins(tmp_path):
    """多线程并发 resolve 同 interrupt_id → 只一个 True（threading.Lock 保护）。

    与 test_handler.py 的 thread-safety 测试同款：模拟 hook HTTP handler 线程 +
    asyncio.to_thread 工作线程并发调用 resolve。
    """

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = InterruptHandler(bus)
        await handler.start()
        try:
            ireq = make_ireq("i1")
            task = asyncio.create_task(handler.request(ireq))
            await asyncio.sleep(0.02)

            results: list[bool] = []
            results_lock = threading.Lock()

            def _resolve(src: str) -> None:
                ok = handler.resolve("i1", "continue", f"g-{src}", src)
                with results_lock:
                    results.append(ok)

            threads = [
                threading.Thread(target=_resolve, args=("cli",)),
                threading.Thread(target=_resolve, args=("web",)),
                threading.Thread(target=_resolve, args=("mcp",)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=2.0)

            assert sum(results) == 1  # 恰好一个 True
            assert len(results) == 3

            action, guidance = await asyncio.wait_for(task, timeout=1.0)
            assert action == "continue"
            assert guidance is not None
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


# ── has_pending（外部查询）─────────────────────────────────────────────────


def test_has_pending(tmp_path):
    """has_pending：pending 时 True，resolved 后 False。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = InterruptHandler(bus)
        await handler.start()
        try:
            assert handler.has_pending("i1") is False  # 未 request
            ireq = make_ireq("i1")
            task = asyncio.create_task(handler.request(ireq))
            await asyncio.sleep(0.01)
            assert handler.has_pending("i1") is True
            handler.resolve("i1", "abort", None, "cli")
            await task
            assert handler.has_pending("i1") is False  # resolved 后 done
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


# ── record_resolved（CLI 单壳路径，SPEC §3.1）───────────────────────────────


def test_record_resolved_emits_requested_and_resolved_to_tape(tmp_path):
    """CLI 单壳路径：record_resolved emit interrupt_requested + broadcaster emit
    interrupt_resolved（两者都写 Tape）。不经 await-future（review §2.1 修复）。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = InterruptHandler(bus)
        await handler.start()
        try:
            ireq = make_ireq("i1", node="cfg", session_id="sess-x")
            await handler.record_resolved(ireq, "continue", "skip weights", "cli")
            await asyncio.sleep(0.05)  # 等 broadcaster emit resolved
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())
    from orca.events.tape import Tape

    events = list(Tape(tmp_path / "events.jsonl", run_id="r1").replay())
    types = [e.type for e in events]
    assert "interrupt_requested" in types
    assert "interrupt_resolved" in types
    requested = next(e for e in events if e.type == "interrupt_requested")
    assert requested.data["interrupt_id"] == "i1"
    assert requested.node == "cfg"
    resolved = next(e for e in events if e.type == "interrupt_resolved")
    assert resolved.data["action"] == "continue"
    assert resolved.data["guidance"] == "skip weights"
    assert resolved.data["resolved_by"] == "cli"


def test_record_resolved_no_deadlock_without_future(tmp_path):
    """record_resolved 不注册 future（不 await），故不依赖 resolve 时序——CLI 单壳安全。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = InterruptHandler(bus)
        await handler.start()
        try:
            # 注意：不调 handler.request（不注册 future），直接 record_resolved。
            # 验证：不阻塞、不死锁、has_pending 始终 False（无 future）。
            ireq = make_ireq("i1")
            assert handler.has_pending("i1") is False
            await asyncio.wait_for(
                handler.record_resolved(ireq, "abort", None, "cli"), timeout=2.0,
            )
            assert handler.has_pending("i1") is False  # 无 future 注册
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


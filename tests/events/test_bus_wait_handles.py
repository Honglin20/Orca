"""tests/events/test_bus_wait_handles.py —— EventBus wait-handle API（SPEC §9.7.6）。

覆盖 phase 11 §9.7.6 公开契约：
  - register/unregister 幂等
  - notify_all_waits 把所有已注册 handle set() + 返回数量
  - 线程安全（并发 register + notify 不崩）

约定（同 tests/events/test_bus.py）：本仓库不用 pytest-asyncio，异步统一 ``asyncio.run``。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from orca.events.bus import EventBus
from orca.events.tape import Tape


def _bus(tmp_path: Path, run_id: str = "r1") -> EventBus:
    return EventBus(Tape(tmp_path / "events.jsonl", run_id=run_id))


# ── register / unregister 幂等 ────────────────────────────────────────────────


def test_register_wait_handle_idempotent(tmp_path):
    """同一 handle 多次 register 不重复（set 语义）。"""
    bus = _bus(tmp_path)
    try:
        evt = asyncio.Event()
        bus.register_wait_handle(evt)
        bus.register_wait_handle(evt)  # 幂等：重复不报错也不重复
        # notify 后返 1（去重），handle 被 set
        assert bus.notify_all_waits() == 1
        assert evt.is_set()
    finally:
        bus.close()


def test_unregister_wait_handle_idempotent_absent(tmp_path):
    """unregister 未注册的 handle 不报错（幂等 no-op）。"""
    bus = _bus(tmp_path)
    try:
        evt = asyncio.Event()
        bus.unregister_wait_handle(evt)  # 未注册 —— no-op，不抛
        evt2 = asyncio.Event()
        bus.register_wait_handle(evt2)
        bus.unregister_wait_handle(evt2)
        bus.unregister_wait_handle(evt2)  # 重复注销 —— no-op
        assert bus.notify_all_waits() == 0
    finally:
        bus.close()


def test_unregister_removes_from_notify(tmp_path):
    """注销后的 handle 不再被 notify 唤醒。"""
    bus = _bus(tmp_path)
    try:
        evt = asyncio.Event()
        bus.register_wait_handle(evt)
        bus.unregister_wait_handle(evt)
        assert bus.notify_all_waits() == 0
        assert not evt.is_set()
    finally:
        bus.close()


# ── notify_all_waits ─────────────────────────────────────────────────────────


def test_notify_all_waits_sets_all_and_returns_count(tmp_path):
    """notify 把所有已注册 handle set()，返回被唤醒的数量。"""
    bus = _bus(tmp_path)
    try:
        a, b, c = asyncio.Event(), asyncio.Event(), asyncio.Event()
        for e in (a, b, c):
            bus.register_wait_handle(e)
        count = bus.notify_all_waits()
        assert count == 3
        assert a.is_set() and b.is_set() and c.is_set()
        # 二次 notify：handle 已 set（注册表未清，notify 仍返 3，set 幂等）
        assert bus.notify_all_waits() == 3
    finally:
        bus.close()


def test_notify_all_waits_empty_returns_zero(tmp_path):
    """无注册 handle 时 notify 返 0（幂等，不抛）。"""
    bus = _bus(tmp_path)
    try:
        assert bus.notify_all_waits() == 0
    finally:
        bus.close()


# ── 线程安全 ─────────────────────────────────────────────────────────────────


def test_notify_all_waits_thread_safe_under_concurrent_register(tmp_path):
    """并发 register + notify 不损坏集合迭代（``threading.Lock`` 保护 set 的 add/迭代）。

    意图：SPEC §9.7.6 要求 wait-handle 集合的并发访问安全。本测试验证的是 **集合操作
    的并发安全**（Lock 保护 add/discard/迭代，防「Set changed size during iteration」），
    **不**验证 ``asyncio.Event.set()`` 本身的跨线程语义（那是调用方在 loop 线程上的
    契约，见 ``bus.notify_all_waits`` docstring）。不断言精确计数（竞态下数量不定），
    只断言「不崩 + 最终所有 handle 都被 set」。
    """
    bus = _bus(tmp_path)
    try:
        handles = [asyncio.Event() for _ in range(50)]
        errors: list[Exception] = []

        def register_batch(start: int) -> None:
            try:
                for i in range(start, start + 25):
                    bus.register_wait_handle(handles[i])
            except Exception as e:  # pragma: no cover - 失败即记录
                errors.append(e)

        t1 = threading.Thread(target=register_batch, args=(0,))
        t2 = threading.Thread(target=register_batch, args=(25,))
        # 并发 notify（在 register 进行中）
        notifier = threading.Thread(target=bus.notify_all_waits, daemon=True)
        t1.start()
        notifier.start()
        t2.start()
        t1.join()
        t2.join()
        notifier.join()
        assert not errors
        # 最终：所有 handle 都注册过（notify 可能早于部分 register，再 notify 一次兜底）
        bus.notify_all_waits()
        for h in handles:
            assert h.is_set()
    finally:
        bus.close()

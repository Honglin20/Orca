"""bus.py —— EventBus：持有 Tape + 异步 fan-out（编排事件总线）。

回答「事件如何分发？」：``emit`` 第一动作永远是写 Tape（唯一真相），再异步通知订阅者。

设计规则（SPEC §3.3 / §11 决策 5、6、8）：
  - **EventBus 持有 Tape**（emit 第一动作 = ``Tape.append``）。Tape 不是「可选订阅者」，
    是 emit 的强制副作用。
  - **emit 透传 session_id**：调用方可带 ``session_id``（标识本次 agent 调用）；流式事件
    必须带，reducer/前端按它分组（SPEC §3.5 身份模型）。
  - **异步 fan-out**：``put_nowait`` 到每个订阅者的 ``asyncio.Queue``，**不阻塞** emitter
    （反 Conductor 同步 fan-out：慢订阅者阻塞 emitter）。
  - **per-consumer cursor**：每个 Subscription 自带 cursor，慢订阅者不拖累快订阅者。
  - **队列满策略**：``put_nowait`` 抛 ``QueueFull`` → 丢最老事件 + 记 warning（实时性优先；
    订阅者靠 replay 补全 —— 规则 1 的唯一真相源保证不丢真相）。
  - **单点有序**：seq 单调由 ``Tape.append`` 内部 Lock 保证；fan-out 在锁外（bus 不再加锁）。

依赖单向：本模块依赖 ``orca.schema``（Event/EventType）+ ``orca.events.tape``（Tape）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import AsyncIterator

from orca.schema import Event, EventType

from orca.events.tape import Tape

logger = logging.getLogger(__name__)

# 订阅者队列默认容量（有界内存：反模式① 的有界在订阅者，不在日志）。
_DEFAULT_QUEUE_MAX = 1024


class Subscription:
    """订阅者句柄。自带 ``asyncio.Queue`` + cursor，生产者不阻塞消费者。

    消费者通过 ``events()`` 异步迭代拉取事件；``cancel()`` 退出。慢订阅者队列满时，
    bus 端丢最老事件（实时性优先），订阅者可经 ``Tape.replay`` 补全（唯一真相源保证不丢）。
    """

    def __init__(self, queue_max: int = _DEFAULT_QUEUE_MAX):
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=queue_max)
        self.cursor: int = 0  # 已投递到的 seq（per-consumer cursor，规则 5）
        self._cancelled = False

    async def events(self) -> AsyncIterator[Event]:
        """drain 队列。bus close 时投递 None 哨兵 → 终止迭代。"""
        while True:
            item = await self._queue.get()
            if item is None:
                # 哨兵：bus 已 close，正常退出
                return
            yield item

    def cancel(self) -> None:
        """取消订阅。仅标记；队列清理由 bus close 或 GC 回收。"""
        self._cancelled = True

    def _enqueue(self, event: Event) -> None:
        """bus 端调用：投递事件，队列满时丢最老 + warning（不抛，不阻塞 emitter）。"""
        if self._cancelled:
            return
        try:
            self._queue.put_nowait(event)
            self.cursor = event.seq
        except asyncio.QueueFull:
            # 实时性优先：丢最老事件腾位（订阅者靠 replay 补全）
            try:
                dropped = self._queue.get_nowait()
                self._queue.put_nowait(event)
                self.cursor = event.seq
                logger.warning(
                    "订阅者队列满，丢弃旧事件 seq=%d（type=%s），订阅者可经 replay 补全",
                    dropped.seq,
                    dropped.type,
                )
            except asyncio.QueueFull:
                # 极端：连丢一个都进不去（理论不可达，maxsize>=1）—— fail loud 记 error
                logger.error(
                    "订阅者队列满且无法腾位，丢弃当前事件 seq=%d", event.seq
                )

    def _close(self) -> None:
        """bus close 时投递哨兵，通知所有消费者终止。

        若队列已满（慢订阅者未消费），丢弃一个已缓冲事件腾位投递哨兵 —— 这会丢失
        慢订阅者尚未读到的最后几条事件。事件本身在 Tape（唯一真相源）中完好，
        订阅者可经 ``Tape.replay`` 补全，故此处不致命；记 warning 以可见。
        """
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                dropped = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                dropped = None
            self._queue.put_nowait(None)
            if dropped is not None:
                logger.warning(
                    "close 时订阅者队列满，丢弃未消费事件 seq=%s "
                    "（type=%s）；订阅者可经 Tape.replay 补全",
                    getattr(dropped, "seq", "?"),
                    getattr(dropped, "type", "?"),
                )


class EventBus:
    """编排事件总线。emit 第一动作永远是写 Tape（唯一真相），再异步通知订阅者。"""

    def __init__(self, tape: Tape):
        self.tape = tape
        self._subs: list[Subscription] = []
        self._closed = False
        # phase 11 §9.7.6：wait-handle 注册表。WaitExecutor 进入 interruptible sleep 前
        # register 一个 asyncio.Event；InterruptHandler 收到 Ctrl+G 时 notify_all_waits
        # 把它们全 set()，让 interruptible wait node 立即结束。``threading.Lock`` 保护
        # 集合的 add/discard/迭代（防并发 register/unregister 损坏 set 迭代器）。
        # 注：当前所有 resolve/notify 调用点都在事件循环线程（HTTP/WS/MCP/CLI 端点均
        # async def），``asyncio.Event.set`` 的跨线程安全不由本 Lock 保证；未来若引入
        # 线程化 resolve 路径，需经 ``loop.call_soon_threadsafe(handle.set)``。
        self._wait_handles: set[asyncio.Event] = set()
        self._wait_handles_lock = threading.Lock()

    async def emit(
        self,
        type: EventType,
        data: dict,
        node: str | None = None,
        session_id: str | None = None,
    ) -> Event:
        """构造 Event（seq 由 Tape 分配）→ Tape.append（强制，唯一真相）→ 异步通知订阅者。

        1. 构造不含 seq 的 event 字段 dict（type/timestamp/node/session_id/data）
        2. ``tape.append``：写时分配 seq + 落盘（唯一真相，强制副作用）
        3. 异步 fan-out：``put_nowait`` 到每个订阅者（锁外，不阻塞 emitter）

        透传 session_id 到事件顶层（reducer/前端按它分组）。
        """
        event_data = {
            "type": type,
            "timestamp": time.time(),
            "node": node,
            "session_id": session_id,
            "data": data,
        }
        seq = await self.tape.append(event_data)
        event = Event(seq=seq, **event_data)

        # fan-out 在锁外（asyncio，非阻塞）。慢订阅者靠 per-queue 满策略保护。
        for sub in self._subs:
            sub._enqueue(event)
        return event

    def subscribe(self, queue_max: int = _DEFAULT_QUEUE_MAX) -> Subscription:
        """返回一个 Subscription，自带 ``asyncio.Queue`` + cursor。

        新订阅者只收**订阅之后** emit 的事件（历史经 ``Tape.replay`` 补全 —— 唯一真相源）。
        """
        sub = Subscription(queue_max=queue_max)
        self._subs.append(sub)
        return sub

    # ── phase 11 §9.7.6：wait-handle API（WaitExecutor ↔ InterruptHandler 协同）──

    def register_wait_handle(self, handle: asyncio.Event) -> None:
        """WaitExecutor 进入 interruptible sleep 前注册一个 handle（幂等）。

        InterruptHandler 收到 Ctrl+G 时调 ``notify_all_waits`` 把所有已注册 handle
        ``set()``，让 interruptible wait node 立即结束（``wait_completed.interrupted=True``）。
        """
        with self._wait_handles_lock:
            self._wait_handles.add(handle)

    def unregister_wait_handle(self, handle: asyncio.Event) -> None:
        """sleep 结束（正常完成 / 被打断）后注销 handle（幂等：未注册不报错）。"""
        with self._wait_handles_lock:
            self._wait_handles.discard(handle)

    def notify_all_waits(self) -> int:
        """set 所有已注册 wait handle，返回被唤醒的数量。

        InterruptHandler 收到 Ctrl+G 时调（``resolve`` / ``record_resolved`` 路径都调）。
        返回值用于日志可观测（被唤醒的 wait node 数）。

        线程安全范围：``threading.Lock`` 保护 wait-handle 集合的 snapshot（防并发
        register/unregister 损坏迭代）；snapshot 后在锁外逐个 ``handle.set()``。
        ``asyncio.Event.set`` 本身的跨线程安全不由本 Lock 保证 —— 调用方应在事件循环
        线程上调用（当前所有 resolve 路径均满足：HTTP/WS/MCP/CLI 端点都是 async def）。
        """
        with self._wait_handles_lock:
            handles = list(self._wait_handles)
        for handle in handles:
            handle.set()
        return len(handles)

    def close(self) -> None:
        """关闭 bus：关闭 Tape 句柄 + 通知所有订阅者终止。

        幂等（与 ``Tape.close`` 对齐）：重复调用直接返回。RunManager 的 ``_teardown_handle``
        可能在 run 终态 + shutdown 两次执行 close，幂等 guard 防重复操作埋雷。
        """
        if self._closed:
            return
        self._closed = True
        for sub in self._subs:
            sub._close()
        self._subs.clear()
        self.tape.close()

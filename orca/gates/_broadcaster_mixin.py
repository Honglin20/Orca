"""_broadcaster_mixin.py —— 共享的 broadcaster 生命周期 pattern（DRY，SPEC §3.3）。

回答「为什么 HumanGateHandler 和 InterruptHandler 都要 start/stop/_broadcaster？」：
两者都是「resolve 是同步非阻塞（不阻塞在 emit 上）→ 投 resolved 入队 → 后台
_broadcaster 协程出队 emit resolved 写 Tape 广播给三壳」的同款 pattern（SPEC §2.2
决策 2 / §10 决策 11）。抽成 mixin 避免三处重复（DRY 铁律 6）。

共享的「形状」：
  - ``_resolved_queue``：``asyncio.Queue[object]``，resolve 入队（同步非阻塞），
    ``_broadcaster`` 出队 emit。
  - ``_broadcaster_task``：后台 ``asyncio.Task``，``start`` 起 / ``stop`` 投哨兵退出。
  - ``start`` / ``stop``：幂等生命周期（重复 start 直接 return；stop 投 ``_STOP`` 哨兵
    + await task 5s，超时 cancel 兜底）。
  - ``_broadcaster``：循环出队 → 哨兵 return → 否则交子类 ``_emit_resolved(item)``。

子类契约（abstract hook）：
  - ``_emit_resolved(self, item) -> Awaitable[None]``：出队的 resolved payload 如何
    翻译成 bus.emit（event type / data / node / session_id 各子类自定）。**子类必须实现**。

**为什么用 mixin 而非基类**：mixin 表达「能力混入」（HumanGateHandler / InterruptHandler
各自有独立的 request/resolve 业务逻辑，仅共享生命周期样板）；基类会暗示「is-a」层级，
语义错位（interrupt 不是 gate）。Mixin + abstract hook = OCP 局部扩展点。

依赖单向：本模块依赖 ``orca.events.bus``（EventBus 类型）+ stdlib，不依赖 run/exec/iface。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

if TYPE_CHECKING:
    from orca.events.bus import EventBus

logger = logging.getLogger(__name__)

# broadcaster 退出哨兵：``stop()`` 投此值入队，``_broadcaster`` 收到即 return。
# 私有单例 sentinel 而非 None，避免与子类入队的 resolved payload 形态冲突。
_STOP: Final = object()


@runtime_checkable
class _ResolvedEmitter(Protocol):
    """子类契约：``_broadcaster`` 出队非哨兵 item 时调此 hook emit resolved。

    子类（HumanGateHandler / InterruptHandler）实现此方法决定如何把 item 翻译成
    ``bus.emit(...)``（event type / data / node / session_id 各异）。
    """

    def _emit_resolved(self, item: object) -> "asyncio.Future[None]": ...  # pragma: no cover


class BroadcasterMixin:
    """共享的 broadcaster pattern：asyncio.Queue + 后台 task + start/stop 生命周期。

    子类必须：
      1. 在 ``__init__`` 里设 ``self._resolved_queue: asyncio.Queue | None = None``
         + ``self._broadcaster_task: asyncio.Task | None = None``（惰性，绑定 start 所在 loop）。
      2. 实现 ``_emit_resolved(self, item) -> Awaitable[None]``（出队 item → bus.emit）。
      3. 持有 ``self._bus: EventBus``（emit 入口）。

    生命周期约定：
      - ``start()`` 必须在 running event loop 内调（request 前）。
      - ``stop()`` 幂等（未 start / 已 stop 直接 return）。
      - 测试必须 ``start()`` + ``stop()`` 配对，否则 asyncio 报「Task was destroyed」。
    """

    # 类型注解（子类 __init__ 赋值；mixin 自身不 __init__，避免与子类构造签名冲突）。
    _bus: "EventBus"
    _resolved_queue: asyncio.Queue[object] | None
    _broadcaster_task: asyncio.Task[None] | None
    # 子类提供 logger（用各自模块 logger，错误日志归属正确模块）。
    _broadcaster_logger: logging.Logger

    async def start(self) -> None:
        """启动 ``_broadcaster`` 后台协程。

        必须在有 running event loop 时调用（request 前）。重复 start 是幂等的
        （已运行则直接返回，不创建第二个 task）。
        """
        if self._broadcaster_task is not None and not self._broadcaster_task.done():
            return  # 幂等：已启动
        if self._resolved_queue is None:
            self._resolved_queue = asyncio.Queue()
        self._broadcaster_task = asyncio.create_task(
            self._broadcaster(), name="orca-gates-broadcaster"
        )

    async def stop(self) -> None:
        """停止 ``_broadcaster``：投哨兵 + await task 干净退出。

        幂等：未 start / 已 stop 直接返回。调用后 resolved 事件不再被 emit（request
        已完成的 gate/interrupt 仍能返回；后续 resolve 仍 set_result，只是广播丢失 ——
        通常在 handler 生命周期收尾时调用，已无在途请求）。
        """
        task = self._broadcaster_task
        queue = self._resolved_queue
        if task is None or queue is None:
            return  # 幂等：未 start
        if not task.done():
            await queue.put(_STOP)
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                # broadcaster 卡住：cancel 兜底（不应发生，fail loud 记 error）
                self._broadcaster_logger.error(
                    "%s broadcaster 5s 内未退出，强制 cancel",
                    self.__class__.__name__,
                )
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    # cancel 后 task 抛 CancelledError 是预期路径，正常吞
                    pass
        self._broadcaster_task = None

    async def _broadcaster(self) -> None:
        """后台协程：从 ``_resolved_queue`` 取 resolved item → 调子类 ``_emit_resolved``。

        resolve() 只唤醒 request()；广播由本协程统一 emit（避免 resolve 阻塞在 emit 上，
        SPEC §10 决策 11）。三壳订阅 bus 收到 resolved 事件 → 同步关闭各自的 UI。

        退出：``stop()`` 投 ``_STOP`` 哨兵入队，本协程收到即 return。

        emit 失败不阻断 broadcaster（后续 resolved 仍能广播）；记 exception 让其可见
        （fail loud 记 error 暴露问题，不静默吞）。
        """
        assert self._resolved_queue is not None  # start() 保证
        queue = self._resolved_queue
        while True:
            item = await queue.get()
            if item is _STOP:
                return
            try:
                await self._emit_resolved(item)  # type: ignore[attr-defined]
            except Exception:
                self._broadcaster_logger.exception(
                    "%s broadcaster _emit_resolved 失败（item=%r）",
                    self.__class__.__name__, item,
                )

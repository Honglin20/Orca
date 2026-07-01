"""interrupt.py —— InterruptHandler：用户中断 workflow 的暂停 / 等意图 / 广播（SPEC §3.3）。

回答「用户 Ctrl+G 想中断 workflow 纠偏时，怎么暂停编排、等用户选 continue/skip/abort、
把结果广播给三壳？」：与 ``HumanGateHandler`` 同款 pattern（暂停 await future + resolve
set_result + 后台 broadcaster emit resolved），但语义不同——

  - **HumanGateHandler**：等「决策」（allow/deny 或自由文本），驱动工具权限 / agent 问答。
  - **InterruptHandler**：等「用户意图」（continue + guidance / skip / abort），驱动
    workflow 中断纠偏。返回 ``(action, guidance)``：action 决定编排下一步（continue 重跑
    当前 node 含 guidance / skip 跳过 / abort 中止），guidance 是用户给 agent 的纠偏话。

共享的「形状」（DRY，SPEC §3.3）：继承 ``BroadcasterMixin``——``start``/``stop``/
``_broadcaster`` 生命周期 + ``_emit_resolved`` hook（emit ``interrupt_resolved``）。

设计规则（SPEC §3.3）：
  - **request 是 async**：emit ``interrupt_requested`` 写 Tape + ``await fut`` 等用户答
    （无 timeout，与 gate 同：用户可以慢慢想）。
  - **resolve 是同步非阻塞**：壳调它喂答案立即返回（是否赢家 first-wins）。
    resolve 不直接 emit——广播由 ``_broadcaster`` 异步负责（避免 resolve 阻塞在 emit 上，
    SPEC §2.2 决策 2 / §10 决策 11）。
  - **record_resolved 是 CLI 单壳路径**：``_handle_interrupt`` 在 node 边界直接调它。
    与 ``resolve`` 不同，``record_resolved`` **同步 await bus.emit("interrupt_resolved")``**
    写 Tape（关键正确性修复，见方法 docstring），async broadcaster 仅负责通知订阅者刷新 UI。
  - **first-wins race**：多壳并发 resolve 同 interrupt_id → ``_resolve_lock``（threading.Lock）
    保护「get + done check + set_result + put_nowait」原子段，只第一个赢家返回 True。
  - **晚到 / 未知 resolve = fail loud**：返回 False + warning（SPEC §10 决策 7）。

线程安全（与 HumanGateHandler 同）：``_resolve_lock`` 是 ``threading.Lock``，因 resolve
可能从 hook HTTP handler 线程 / ``asyncio.to_thread`` 工作线程并发调用——必须跨线程串行，
否则 GIL 释放点交错触发 ``asyncio.InvalidStateError``。

生命周期：
  - ``start()``：启动 ``_broadcaster``（继承自 mixin，幂等）。
  - ``stop()``：投哨兵 + await task 退出（继承自 mixin，幂等）。
  - 测试必须 start + stop 配对（否则 asyncio 报「Task was destroyed」）。

依赖单向：本模块依赖 ``orca.events``（EventBus）+ ``orca.gates.{_broadcaster_mixin, types}``，
不依赖 run/exec/iface（SPEC §2.2 决策 5、§10 决策 8）。orchestrator 调 interrupt，interrupt
不知道 orchestrator 存在。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

from orca.gates._broadcaster_mixin import BroadcasterMixin
from orca.gates.types import InterruptAction, InterruptRequest, InterruptSource

if TYPE_CHECKING:
    from orca.events.bus import EventBus

logger = logging.getLogger(__name__)


class InterruptHandler(BroadcasterMixin):
    """用户中断 workflow 的暂停 / 等意图 / 广播（SPEC §3.3）。

    用法（orchestrator 在 node 边界 ``_handle_interrupt`` 内调）::

        handler = InterruptHandler(bus)
        await handler.start()
        try:
            action, guidance = await handler.request(ireq)
        finally:
            await handler.stop()
    """

    def __init__(self, bus: EventBus):
        self._bus = bus
        # interrupt_id → Future[(action, guidance)]。request 注册，resolve set_result。
        self._pending: dict[str, asyncio.Future[tuple[str, str | None]]] = {}
        # interrupt_id → InterruptRequest 元信息（_emit_resolved 需要 node / session_id 透传）。
        self._interrupts_meta: dict[str, InterruptRequest] = {}
        # 保护 request 的 _pending 注册段（asyncio 路径，跨 task 串行化）。
        self._lock = asyncio.Lock()
        # 保护 resolve 的原子段（跨线程，与 HumanGateHandler 同理）。
        self._resolve_lock = threading.Lock()
        # BroadcasterMixin 共享状态（start/stop/_broadcaster 在 mixin）。
        self._resolved_queue: asyncio.Queue[object] | None = None
        self._broadcaster_task: asyncio.Task[None] | None = None
        self._broadcaster_logger = logger

    # ── 公开 API ─────────────────────────────────────────────────────────────
    # start / stop 继承自 BroadcasterMixin（共享生命周期 pattern）。

    async def request(self, ireq: InterruptRequest) -> tuple[str, str | None]:
        """emit ``interrupt_requested`` 写 Tape + 暂停 + 等任一壳 resolve。

        返回 ``(action, guidance)``：action 是 ``"continue"``/``"skip"``/``"abort"``，
        guidance 是用户给后续 agent 的纠偏话（``"continue"`` 时可能非 None，其余 None）。

        interrupt 无限等（``await fut`` 无 timeout，SPEC §2.2 决策 3，与 gate 同）。
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[str, str | None]] = loop.create_future()
        async with self._lock:
            self._pending[ireq.id] = fut
            self._interrupts_meta[ireq.id] = ireq

        # emit 第一动作 = 写 Tape（唯一真相）。三壳从 Tape / 订阅双通道收到
        # interrupt_requested，各自弹 InterruptModal 参与竞速。
        await self._bus.emit(
            "interrupt_requested",
            data={
                "interrupt_id": ireq.id,
                "node": ireq.node,
                "run_id": ireq.run_id,
                "session_id": ireq.session_id,
                "elapsed_at_request": ireq.elapsed_at_request,
                "source": ireq.source,
            },
            node=ireq.node,
            session_id=ireq.session_id,
        )

        try:
            return await fut
        finally:
            # 清 _pending（防泄漏）；_interrupts_meta 保留至 _emit_resolved 取出时 pop，
            # 保证晚到的 broadcaster 仍能取 node/session_id。
            async with self._lock:
                self._pending.pop(ireq.id, None)
                if self._resolved_queue is None:
                    self._interrupts_meta.pop(ireq.id, None)

    def has_pending(self, interrupt_id: str) -> bool:
        """该 interrupt_id 是否在 pending（未 resolved、未取消）。外部查询用。"""
        fut = self._pending.get(interrupt_id)
        return fut is not None and not fut.done()

    async def record_resolved(
        self,
        ireq: InterruptRequest,
        action: InterruptAction,
        guidance: str | None,
        source: InterruptSource,
    ) -> None:
        """CLI 单壳路径：用户已在 modal 答完，**同步** emit ``interrupt_requested`` +
        ``interrupt_resolved`` 写 Tape（含订阅者 fan-out）。

        与 ``request``/``resolve`` 的区别（SPEC §3.1 时序）：
          - 多壳路径（web/mcp）：``request`` emit requested + await future；壳 ``resolve`` 同步
            set_result + 入队；broadcaster 异步 emit resolved。resolve 必须在 request 之后
            （future 先注册）。broadcaster 在此路径是必须的：``resolve`` 是同步方法不能
            ``await emit``，只能投队列让 async broadcaster 代发。
          - CLI 单壳路径（本方法）：用户在 InterruptModal 里答完，``action``/``guidance`` 已知，
            Orchestrator ``_handle_interrupt`` 在 node 边界直接调本方法——不经 future 机制
            （CLI 不需要竞速，且 modal dismiss 发生在 node 边界**之前**，时序不匹配 await-future）。

        **关键正确性（tape 写同步，不经 broadcaster）**：``interrupt_resolved`` 必须**同步**
        await bus.emit 写 Tape（SPEC §4.1 ``_handle_interrupt`` 内同步 emit 的契约描述）。
        绝不能把「写 Tape」这一步交给 async broadcaster 异步负责——否则 abort/skip 分支
        ``_drive_loop`` 立即结束 → ``run()`` 的 ``bus.close()`` 早于 broadcaster flush，
        ``emit`` 撞 close 抛 RuntimeError 被吞 → ``interrupt_resolved`` 永久丢失，违反单
        Tape 配对不变量 + 唯一真相源铁律（wave-1 e2e 审计发现的 critical bug）。

        本方法是 async，可直接 ``await emit``，故**不经 broadcaster**——``bus.emit`` 第一动作
        写 Tape、第二动作同步 ``put_nowait`` 给所有订阅者（bus.py fan-out），Tape 写 + 订阅者
        通知在一次 await 内全部完成，与后续 ``bus.close()`` 无竞态。broadcaster 仅留给同步
        ``resolve()`` 入口（它无法 await emit，必须靠 async 代发）。

        **不登记 ``_interrupts_meta``**：该 dict 仅供 ``_emit_resolved``（broadcaster 出队时）
        取 node/session_id。本同步路径已直接带 node/session_id 调 emit，无需登记 meta——
        后续维护者勿误以为 record_resolved 会写 meta 而依赖它。
        """
        await self._bus.emit(
            "interrupt_requested",
            data={
                "interrupt_id": ireq.id,
                "node": ireq.node,
                "run_id": ireq.run_id,
                "session_id": ireq.session_id,
                "elapsed_at_request": ireq.elapsed_at_request,
                "source": ireq.source,
            },
            node=ireq.node,
            session_id=ireq.session_id,
        )
        # 同步写 Tape + fan-out 订阅者（关键正确性：resolved 必须在返回前落盘，不交给
        # async broadcaster——避免与 run() 的 bus.close() 竞态丢事件）。
        await self._bus.emit(
            "interrupt_resolved",
            data={
                "interrupt_id": ireq.id,
                "action": action,
                "guidance": guidance,
                "resolved_by": source,
            },
            node=ireq.node,
            session_id=ireq.session_id,
        )
        # phase 11 §9.7.6：打断所有 interruptible wait node（Ctrl+G 应让正在 sleep 的
        # wait 立即结束，而非等它睡满）。bus.notify_all_waits 幂等（无 handle 返 0）。
        woken = self._bus.notify_all_waits()
        if woken:
            logger.info("interrupt %s 打断了 %d 个 wait node", ireq.id, woken)

    def resolve(
        self,
        interrupt_id: str,
        action: InterruptAction,
        guidance: str | None,
        source: InterruptSource,
    ) -> bool:
        """任一壳调它喂答案。返回是否是赢家（FIRST_COMPLETED）。

        同步 + 非阻塞：``set_result`` + 入 ``_resolved_queue``（不 emit，广播由
        ``_broadcaster`` 异步负责，SPEC §2.2 决策 2）。

        - 已 resolved / 未知 interrupt_id → 返回 False + warning（fail loud）。
        - 赢家 → 返回 True（其 (action, guidance) 生效，编排 resume）。

        线程安全：``_resolve_lock``（threading.Lock）保护原子段，与 HumanGateHandler 同。
        """
        with self._resolve_lock:
            fut = self._pending.get(interrupt_id)
            if fut is None or fut.done():
                logger.warning(
                    "interrupt %s 已 resolved 或未知，source=%s 的输入被丢弃（fail loud）",
                    interrupt_id,
                    source,
                )
                return False

            fut.set_result((action, guidance))  # 唤醒 request() 的 await fut
            if self._resolved_queue is not None:
                self._resolved_queue.put_nowait((interrupt_id, action, guidance, source))
            else:
                # 未 start 就 resolve：记 warning（调用方应先 start；测试外不应触发）
                logger.warning(
                    "interrupt %s resolved 但 broadcaster 未启动，resolved 事件未广播",
                    interrupt_id,
                )
            # phase 11 §9.7.6：打断所有 interruptible wait node（Ctrl+G 应让正在 sleep 的
            # wait 立即结束）。放锁外避免持锁调 bus；notify 自带锁。先 set_result 再 notify：
            # wait node 不依赖 future，set 顺序无强约束，但 future 路径优先保编排唤醒。
            woken = self._bus.notify_all_waits()
            if woken:
                logger.info(
                    "interrupt %s 打断了 %d 个 wait node", interrupt_id, woken
                )
            return True

    # ── 内部 ─────────────────────────────────────────────────────────────────

    async def _emit_resolved(self, item: object) -> None:
        """``_broadcaster`` 出队的 resolved item → emit ``interrupt_resolved``（SPEC §3.2）。

        item 形态：``(interrupt_id, action, guidance, source)``（resolve 入队时定型）。
        从 ``_interrupts_meta`` 取 node / session_id 透传到 event 顶层（与 requested 一致），
        emit 后清 meta（防泄漏）。

        emit 失败由 ``BroadcasterMixin._broadcaster`` 捕获记 exception（不阻断后续广播）。
        """
        interrupt_id, action, guidance, source = item  # type: ignore[misc]
        ireq = self._interrupts_meta.pop(interrupt_id, None)
        node = ireq.node if ireq is not None else None
        session_id = ireq.session_id if ireq is not None else None
        await self._bus.emit(
            "interrupt_resolved",
            data={
                "interrupt_id": interrupt_id,
                "action": action,
                "guidance": guidance,
                "resolved_by": source,
            },
            node=node,
            session_id=session_id,
        )

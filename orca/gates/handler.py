"""handler.py —— HumanGateHandler：暂停 / 三通道竞速 / 广播 / 恢复（SPEC §2 §4）。

回答「workflow 需要人决策时，怎么暂停、把决策广播给三壳、等任一壳回答、广播结果、
恢复？」：``request(gate)`` emit ``human_decision_requested`` 写 Tape + 暂停 await；
任一壳调 ``resolve(gate_id, answer, source)`` 唤醒 request + 入队；后台 ``_broadcaster``
协程从队列取出 emit ``human_decision_resolved``（广播给所有订阅的壳）。

设计规则（SPEC §2.2 §4.2 §10 决策 3/6/7/11）：
  - **request 是 async**：``await bus.emit``（emit 是 async，写 Tape 强制副作用）+
    ``await fut``（等人，**无 timeout**——gate 语义层无限等，SPEC §2.2 决策 3）。
  - **resolve 是同步非阻塞**：壳调它喂答案立即返回（是否赢家）。resolve **不直接 emit**——
    广播由 ``_broadcaster`` 协程统一负责，避免 resolve 阻塞在 emit 上（SPEC §2.2 决策 2、
    §10 决策 11）。
  - **race = first-wins**：多个壳同时 resolve 同一 gate → ``_lock`` 保护，只有第一个
    ``set_result`` 成功的返回 True，其余返回 False（SPEC §4.2）。
  - **晚到 resolve = fail loud**：未知 gate_id / 已 resolved 的 resolve → 返回 False +
    记 warning（SPEC §10 决策 7，不静默吞）。
  - **广播语义**：``_broadcaster`` 把 resolved 事件 emit 到 bus → 三壳从同一份 Tape 读，
    视觉同步（SPEC §4.1 §4.2）。

生命周期：
  - ``start()``：启动 ``_broadcaster`` 后台 task（必须在 request 前 / 同 event loop 调）。
  - ``stop()``：投 ``_STOP`` 哨兵入队 + ``await task`` 干净退出（无 leaked task 警告）。
  - 测试必须 ``start()`` + ``stop()`` 配对，否则 ``asyncio`` 报「Task was destroyed but
    it is pending」。

依赖单向：本模块依赖 ``orca.events``（EventBus）+ ``orca.gates.types``（HumanGate），
不依赖 run/exec/iface（SPEC §2.2 决策 5、§10 决策 8）。orchestrator 调 gates，gates
不知道 orchestrator 存在。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Final

from orca.gates.types import HumanGate

if TYPE_CHECKING:
    from orca.events.bus import EventBus

logger = logging.getLogger(__name__)

# broadcaster 退出哨兵：``stop()`` 投此值入队，``_broadcaster`` 收到即 return。
# 用私有单例 sentinel 而非 None，避免与 (gate_id, answer, source) 元组形态冲突。
_STOP: Final = object()


class HumanGateHandler:
    """暂停 / 竞速 / 广播 / 恢复（SPEC §2 §4）。

    用法（orchestrator 或壳侧）::

        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            answer, source = await handler.request(gate)
        finally:
            await handler.stop()
    """

    def __init__(self, bus: EventBus):
        self._bus = bus
        # gate_id → Future[(answer, source)]。request 注册，resolve set_result。
        self._pending: dict[str, asyncio.Future[tuple[str, str]]] = {}
        # gate_id → gate 元信息（广播 emit 需要 node + session_id；SPEC §4.1 broadcaster 用）。
        self._gates_meta: dict[str, HumanGate] = {}
        # 保护 request 的 _pending 注册段（asyncio 路径，跨 task 串行化）。
        self._lock = asyncio.Lock()
        # 保护 resolve 的「get + done check + set_result + put_nowait」原子段。
        # resolve 是同步方法，但可能从 hook HTTP handler 线程或 asyncio.to_thread
        # 工作线程并发调用——必须用 threading.Lock（不是 asyncio.Lock）跨线程串行，
        # 否则两个线程在 GIL 释放点交错会触发 asyncio.InvalidStateError（race first-wins
        # 失效）。与 context_registry.py 的 threading.Lock 用法一致。
        self._resolve_lock = threading.Lock()
        # resolved gate 队列：resolve 入队（不阻塞），_broadcaster 出队 emit。
        # 惰性创建：绑定到 start() 所在 event loop（与 Tape.Lock 同理，Python 3.12 绑 loop）。
        self._resolved_queue: asyncio.Queue[object] | None = None
        self._broadcaster_task: asyncio.Task[None] | None = None

    # ── 公开 API ─────────────────────────────────────────────────────────────

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
        已完成的 gate 仍能返回；后续 resolve 仍 set_result，只是广播丢失 —— 通常在
        orchestrator 收尾时调用，已无在途 gate）。
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
                logger.error("HumanGateHandler broadcaster 5s 内未退出，强制 cancel")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    # cancel 后 task 抛 CancelledError 是预期路径，正常吞
                    pass
        self._broadcaster_task = None

    async def request(self, gate: HumanGate) -> tuple[str, str]:
        """emit ``human_decision_requested`` 写 Tape + 暂停 + 等任一壳 resolve。

        返回 ``(answer, source)``——source 是哪个壳答的（``"cli"``/``"web"``/``"mcp"``）。

        gate 无限等（``await fut`` 无 timeout，SPEC §2.2 决策 3）；超时只在 hook 桥
        传输层（见 ``hook_script.py``），不在 gate 语义层。
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[str, str]] = loop.create_future()
        async with self._lock:
            self._pending[gate.id] = fut
            self._gates_meta[gate.id] = gate

        # emit 第一动作 = 写 Tape（唯一真相，phase 3 §3.3）。三壳订阅者从 Tape / 订阅
        # 双通道收到 human_decision_requested，各自渲染 gate UI。session_id 透传到 event
        # 顶层（phase 3 §3.3 身份模型——壳 reducer 按 session 分组关联到 claude 会话）。
        await self._bus.emit(
            "human_decision_requested",
            data={
                "gate_id": gate.id,
                "prompt": gate.prompt,
                "options": gate.options,
                "source": gate.source,
                "context": gate.context,
                "run_id": gate.run_id,
                "node": gate.node,
            },
            node=gate.node,
            session_id=gate.session_id,
        )

        try:
            return await fut
        finally:
            # 无论正常 resolve 还是 cancel，清 _pending / _gates_meta（防内存泄漏）。
            # _lock 内做：与 resolve 的 set_result 串行，避免 race（resolve 已 set_result
            # 后此处 pop 不会影响赢家判定——赢家判定在 set_result 之前）。
            async with self._lock:
                self._pending.pop(gate.id, None)
                # _gates_meta 保留至广播 emit 后清理（_broadcaster 取出时 pop），
                # 保证晚到的 broadcaster 仍能取 node。若 broadcaster 已停则可能残留，
                # 属 handler 生命周期收尾，可接受。
                if self._resolved_queue is None:
                    self._gates_meta.pop(gate.id, None)

    def has_pending(self, gate_id: str) -> bool:
        """该 gate_id 是否在 pending（未 resolved、未取消）。

        多 run 分发（phase 9a web）从外部查询「哪个 run 持有此 gate」时用，避免直接
        访问私有 ``_pending``。返回 True = 存在且未 done。
        """
        fut = self._pending.get(gate_id)
        return fut is not None and not fut.done()

    def resolve(self, gate_id: str, answer: str, source: str) -> bool:
        """任一壳调它喂答案。返回是否是赢家（FIRST_COMPLETED）。

        同步 + 非阻塞：``set_result`` + 入 ``_resolved_queue``（不 emit，广播由
        ``_broadcaster`` 异步负责，SPEC §2.2 决策 2）。

        - 已 resolved / 未知 gate_id → 返回 False + 记 warning（fail loud，§10 决策 7）。
        - 赢家 → 返回 True（其 answer 生效，引擎 resume）。

        线程安全：``_resolve_lock``（threading.Lock）保护「get + done check + set_result
        + put_nowait」原子段。resolve 可能从 hook HTTP handler 线程或 ``asyncio.to_thread``
        工作线程并发调用——必须显式锁，否则两个线程在 GIL 释放点交错会触发
        ``asyncio.InvalidStateError``（race first-wins 失效）。
        """
        with self._resolve_lock:
            fut = self._pending.get(gate_id)
            if fut is None or fut.done():
                logger.warning(
                    "gate %s 已 resolved 或未知，source=%s 的输入被丢弃（fail loud）",
                    gate_id,
                    source,
                )
                return False

            fut.set_result((answer, source))  # 唤醒 request() 的 await fut
            # 入队让 broadcaster emit resolved（广播）。Queue 未创建（未 start）时丢弃，
            # 但 request 仍能返回（语义：无 broadcaster 时退化成无广播 resolve）。
            if self._resolved_queue is not None:
                self._resolved_queue.put_nowait((gate_id, answer, source))
            else:
                # 未 start 就 resolve：记 warning（调用方应先 start；测试外不应触发）
                logger.warning(
                    "gate %s resolved 但 broadcaster 未启动，resolved 事件未广播",
                    gate_id,
                )
            return True

    # ── 内部 ─────────────────────────────────────────────────────────────────

    async def _broadcaster(self) -> None:
        """后台协程：从 ``_resolved_queue`` 取 resolved gate → emit ``human_decision_resolved``。

        resolve() 只唤醒 request()；广播由本协程统一 emit（避免 resolve 阻塞在 emit 上，
        SPEC §10 决策 11）。三壳订阅 bus 收到 resolved 事件 → 同步关闭各自的 gate UI。

        退出：``stop()`` 投 ``_STOP`` 哨兵入队，本协程收到即 return。
        """
        assert self._resolved_queue is not None  # start() 保证
        queue = self._resolved_queue
        while True:
            item = await queue.get()
            if item is _STOP:
                return
            gate_id, answer, source = item  # type: ignore[misc]
            gate = self._gates_meta.pop(gate_id, None)
            node = gate.node if gate is not None else None
            session_id = gate.session_id if gate is not None else None
            # emit resolved（写 Tape + 三壳订阅者收到广播，视觉同步）。session_id 透传
            # 到 event 顶层（与 requested 一致，phase 3 §3.3 身份模型）。
            try:
                await self._bus.emit(
                    "human_decision_resolved",
                    data={
                        "gate_id": gate_id,
                        "answer": answer,
                        "resolved_by": source,
                    },
                    node=node,
                    session_id=session_id,
                )
            except Exception:
                # emit 失败不阻断 broadcaster（后续 gate 仍能广播）；记 error 让其可见。
                # 注意：emit 失败意味着 Tape 可能未落 resolved 行（罕见，如 Tape 已 close），
                # 三壳读不到 resolved → 各自 UI 不会自动关。fail loud 记 error 暴露问题。
                logger.exception(
                    "broadcaster emit human_decision_resolved 失败（gate=%s）",
                    gate_id,
                )

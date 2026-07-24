"""ws_handler.py —— WebSocket 单通道 + 按 run 订阅 + gate_response 反向（SPEC §4）。

回答「事件怎么按需推给前端？」：单条 ``/ws``。前端发 ``subscribe(run_id)`` → 后端为该
WS 起一个 pump task，把**该 run 的 bus 订阅事件**推给 WS（带 ``run_id`` 标签）。切 run
（再 subscribe / unsubscribe）→ cancel 旧 pump。反向通道：同 WS 收 ``gate_response`` →
对应 run 的 ``gate_handler.resolve``。

设计规则（SPEC §0.1 铁律 3 / §4.2 / §9 决策 4）：
  - **单通道**：所有事件/gate 走一条 ``/ws``（反双 WS）。
  - **按需订阅**：subscribe(A) 后只推 A 的事件（**不推所有 run 洪流**，断言覆盖）。
  - **切 run**：unsubscribe / 再 subscribe → cancel 旧 pump（无 leaked task）。
  - **反向 gate_response**：同 WS 收 ``{type:gate_response, ...}`` → resolve 当前订阅 run
    的 gate_handler。
  - **断开清理**：WS 断开 → cancel 当前 pump + 清 _subs（无 leaked task/coroutine）。
  - **无并行内存事件 list**：pump 直接转发 bus 订阅事件到 WS，不缓存（铁律 1）。

依赖单向：本模块依赖 ``orca.iface.web.run_manager``（同层）+ ``fastapi``（WS 框架），
不依赖 run/exec 内部（不含编排逻辑——纯转发）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect

from orca.events.bus import Subscription

if TYPE_CHECKING:
    from orca.iface.web.run_manager import RunHandle, RunManager

logger = logging.getLogger(__name__)


@dataclass
class _RunSubscription:
    """一个 WS 当前订阅某 run 的状态：handle + bus subscription + pump task。

    切 run / 断开时 cancel pump（无 leaked task）。
    """

    handle: RunHandle
    sub: Subscription
    pump: asyncio.Task


@dataclass
class _WSConnection:
    """单条 WS 的出站 queue + writer task（SPEC §13.1 U-3 / §13.2 B-4）。

    解决 FastAPI 单 WS 不支持并发 send 的 ``RuntimeError``：所有出站帧（bus 事件 pump +
    控制帧广播）经 ``queue`` 串行化，由单 writer task ``send_json``。

    控制帧（``run_changed``）与 bus 事件经同一 queue 出站，前端 ``processEvent`` 见
    ``kind==="control"`` 即拒（不进 reducer）。
    """

    ws: WebSocket
    queue: asyncio.Queue
    writer: asyncio.Task | None = None
    subscription: _RunSubscription | None = None


class WebServer:
    """WS 单通道端点 + 按 run 订阅管理（SPEC §4.1）。

    用法（``server.py`` 挂载）::

        web_server = WebServer(manager)
        app.websocket("/ws")(web_server.ws_endpoint)
    """

    def __init__(self, manager: RunManager):
        self._manager = manager
        # WS → 当前订阅。一个 WS 同时只订阅一个 run（subscribe 切换覆盖旧订阅）。
        self._subs: dict[WebSocket, _RunSubscription] = {}
        # SPEC §13.2 B-4 WS connection registry：独立于 ``_subs``（控制帧广播遍历此 registry）。
        # 控制帧 ``run_changed`` 要广播给**所有 WS**（即便未订阅某 run），故单独持表。
        self._connections: dict[WebSocket, _WSConnection] = {}
        # D4 WS 事件驱动 auto-exit：任一 WS connect/disconnect 重置计时；
        # ``now - last_ws_activity_at > N AND run.terminal AND 无活跃 WS`` → 进程退出
        # （``orca run`` web 默认路径用，SPEC §0 D4 / §4 step4 / §8 AC5 负向）。
        # 初值 = 构造时刻，让无 WS 接入的窗口也计时。单调钟（不受系统时间回拨影响）。
        self.last_ws_activity_at: float = time.monotonic()
        # **活跃 WS 连接计数**（SPEC §8 AC5 负向「有活跃 WS 不退」）：connect++ / disconnect--。
        # 仅当 ``active_ws_count == 0`` 时 auto-exit 计时窗口才生效——长存活 WS 不会因为
        # 「connect 时刻已超过 N 秒」被误判为可退（修复旧版只 touch 不计数的缺陷）。
        self.active_ws_count: int = 0
        # SPEC §13.2 B-4：注册 run_changed 广播回调（manager delete/cancel/attach 时调）。
        self._run_changed_cb = self._on_run_changed
        self._manager.add_run_changed_listener(self._run_changed_cb)

    def _on_run_changed(self, run_id: str, action: str) -> None:
        """``manager`` 触发 run_changed → enqueue 到每条 WS 的出站 queue（控制帧广播）。

        SPEC §13.4 M-8：帧 ``{kind:"control", type:"run_changed", run_id, action}``；
        前端 ``processEvent`` 见 ``kind==="control"`` 即拒（不进 reducer）。

        本回调由 manager 在 delete/cancel/attach 后**同步**调用——直接 put_nowait 入各 WS
        queue（writer task 异步消费）。queue 满或 WS 已断 → 静默丢（best-effort，轮询兜底）。
        """
        frame = {
            "kind": "control",
            "type": "run_changed",
            "run_id": run_id,
            "action": action,
        }
        for conn in list(self._connections.values()):
            try:
                conn.queue.put_nowait(frame)
            except asyncio.QueueFull:
                # 队列满 → 静默丢（前端轮询兜底，SPEC D9 / §13.1 U-3）
                pass

    def _touch_ws_activity(self) -> None:
        """重置 WS 活动计时（connect/disconnect 调用）。"""
        self.last_ws_activity_at = time.monotonic()

    async def ws_endpoint(self, ws: WebSocket) -> None:
        """单通道 WS 端点：accept → 循环 receive → 分派 subscribe/unsubscribe/gate_response。

        断开（WebSocketDisconnect / 异常）→ 清理当前订阅（cancel pump），无 leaked task。

        SPEC §13.2 B-4：每条 WS 起独立 queue + writer task 串行化出站帧（bus 事件 + 控制帧）。
        """
        await ws.accept()
        # 建 connection（queue + writer task）—— 控制帧广播也走此 queue。
        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        conn = _WSConnection(ws=ws, queue=queue)
        conn.writer = asyncio.create_task(
            self._writer_loop(ws, queue), name="orca-web-ws-writer",
        )
        self._connections[ws] = conn
        try:
            # connect → 计数 +1 + 重置 auto-exit 计时（D4 / §8 AC5 负向：active WS → no exit）。
            self.active_ws_count += 1
            self._touch_ws_activity()
            while True:
                msg = await ws.receive_json()
                await self._dispatch(ws, msg)
        except WebSocketDisconnect:
            pass  # 正常断开
        except Exception:  # noqa: BLE001 — 任何异常都走清理路径（fail loud 记 warning）
            logger.warning("ws_endpoint 异常，连接将清理", exc_info=True)
        finally:
            await self._cleanup(ws)
            # disconnect → 计数 -1 + 重置 auto-exit 计时（D4）。
            if self.active_ws_count > 0:
                self.active_ws_count -= 1
            self._touch_ws_activity()

    async def _writer_loop(self, ws: WebSocket, queue: asyncio.Queue) -> None:
        """SPEC §13.2 B-4：单 writer task 串行化 ``ws.send_json``（解决并发 send RuntimeError）。

        从 queue 取帧 → ``send_json``。``queue.put(None)`` 哨兵 / WebSocketDisconnect → 退出。
        """
        try:
            while True:
                frame = await queue.get()
                if frame is None:
                    return
                try:
                    await ws.send_json(frame)
                except WebSocketDisconnect:
                    return
                except Exception:  # noqa: BLE001
                    logger.warning("ws writer send_json 失败", exc_info=True)
                    return
        except asyncio.CancelledError:
            raise

    async def _dispatch(self, ws: WebSocket, msg: dict) -> None:
        """分派一条客户端消息。未知 type 记 warning（fail loud），不崩连接。"""
        mtype = msg.get("type")
        if mtype == "subscribe":
            await self._handle_subscribe(ws, msg.get("run_id"))
        elif mtype == "unsubscribe":
            await self._cancel_sub(ws)
        elif mtype == "gate_response":
            await self._handle_gate_response(ws, msg)
        elif mtype == "resume":
            # web-shell-v2 §0 D6：client 重连发 resume(run_id, since=last_seq_seen)；
            # server 重放 seq>since 的历史事件，再 subscribe（接 live 流）。
            await self._handle_resume(ws, msg.get("run_id"), msg.get("since"))
        else:
            logger.warning("ws 收到未知消息 type=%s（忽略）", mtype)

    async def _handle_resume(
        self, ws: WebSocket, run_id: object | None, since: object | None
    ) -> None:
        """D6 resume：重放 run 的 tape 中 seq > since 的事件，然后 subscribe 接 live 流。

        - run_id 未知 / since 非数字 → 记 warning + 不崩，回退到 subscribe（live 流接上）。
        - 否则：把 tape 中 seq>since 的事件按 seq 升序发给 WS（带 run_id 标签），再 subscribe。
        - resume 失败（tape 读异常等）→ 记 warning，回退 subscribe（live 流不丢）。
        - **resume_ok ack**（D4 watchdog 配套）：重放完毕后发 ``{type:"resume_ok", run_id,
          last_seq}`` 帧，client 据此清 resume-fallback watchdog（避免 idle 场景误触发
          全量重拉——SPEC §0 D6 真义是「resume 失败」非「无事件」）。
        """
        if not isinstance(run_id, str):
            logger.warning("ws resume 缺 run_id（回退 subscribe）")
            return
        since_seq: int | None
        if isinstance(since, (int, float)) and not isinstance(since, bool):
            since_seq = int(since)
        else:
            since_seq = None
            logger.warning("ws resume since 非数字 run_id=%s（回退 subscribe）", run_id)
        handle = self._manager.get_handle(run_id)
        if handle is None:
            logger.warning("ws resume 未知 run_id=%s（回退 subscribe）", run_id)
            return
        last_seq = 0
        replayed_ok = False
        conn = self._connections.get(ws)
        try:
            if since_seq is not None:
                # 按 seq 升序重放历史（Tape.replay(since_seq) 已保证 seq>since 升序）。
                for event in handle.tape.replay(since_seq=since_seq):
                    payload = event.model_dump()
                    payload["run_id"] = run_id
                    if conn is not None:
                        try:
                            conn.queue.put_nowait(payload)
                        except asyncio.QueueFull:
                            logger.warning(
                                "ws resume queue full run_id=%s — dropping replay event",
                                run_id,
                            )
                    if event.seq > last_seq:
                        last_seq = event.seq
                replayed_ok = True
        except Exception:  # noqa: BLE001 — resume 失败 fail loud，不阻断后续 subscribe
            logger.warning(
                "ws resume 重放失败 run_id=%s since=%s（回退 subscribe）",
                run_id,
                since_seq,
                exc_info=True,
            )
        # 重放完毕（或失败）→ subscribe 接 live 流（与初始 subscribe 共用路径）
        await self._handle_subscribe(ws, run_id)
        # D4 watchdog ack：**仅当 resume 协议真正执行**（since_seq 合法 + 重放无异常）才发
        # resume_ok。invalid since / unknown run 等回退 subscribe 路径不发——避免误升级 client
        # 状态。type 故意不进 EventType（控制平面帧，不进 tape）；前端 onmessage 见
        # type="resume_ok" 即清 watchdog（不 processEvent）。
        if replayed_ok and conn is not None:
            try:
                # §13.4 M-8：控制帧统一带 ``kind:"control"``（``run_changed`` 已有；``resume_ok`` 补齐）。
                conn.queue.put_nowait(
                    {
                        "kind": "control",
                        "type": "resume_ok",
                        "run_id": run_id,
                        "last_seq": last_seq,
                    }
                )
            except asyncio.QueueFull:  # noqa: BLE001 — queue 满 → client 会经 onclose 重连
                logger.warning(
                    "ws resume_ok enqueue 失败 run_id=%s（queue full）", run_id,
                )

    async def _handle_subscribe(self, ws: WebSocket, run_id: object | None) -> None:
        """订阅某 run：ensure_attached → cancel 旧订阅 → 订阅 handle.bus → 起 pump task。

        SPEC §13 §5.3 D7 / §13.2 I-3：未知 run_id 先 ``manager.ensure_attached``（懒挂载
        discovery 出来的 tape）。ensure_attached 失败（FileNotFoundError / RuntimeError /
        PermissionError）→ 记 warning 不崩，前端收不到该 run 事件但 WS 仍可用。
        """
        if not isinstance(run_id, str):
            logger.warning("ws subscribe 缺 run_id（忽略）")
            return
        # 懒挂载：unknown run_id 先尝试 ensure_attached。
        if self._manager.get_handle(run_id) is None:
            try:
                await self._manager.ensure_attached(run_id)
            except FileNotFoundError:
                logger.warning("ws subscribe ensure_attached 0 命中 run_id=%s", run_id)
                return
            except RuntimeError as e:
                logger.warning(
                    "ws subscribe ensure_attached 多命中 run_id=%s: %s", run_id, e
                )
                return
            except PermissionError as e:
                logger.warning(
                    "ws subscribe ensure_attached 拒绝 run_id=%s: %s", run_id, e
                )
                return
        handle = self._manager.get_handle(run_id)
        if handle is None:
            logger.warning("ws subscribe 未知 run_id=%s（忽略）", run_id)
            return
        # 切 run：先 cancel 旧订阅（无论新旧是否同 run，语义统一）。
        await self._cancel_sub(ws)
        sub = handle.bus.subscribe()
        conn = self._connections.get(ws)
        pump = asyncio.create_task(
            self._pump(conn, sub, run_id),
            name=f"orca-web-ws-pump-{run_id}",
        )
        run_sub = _RunSubscription(handle=handle, sub=sub, pump=pump)
        self._subs[ws] = run_sub
        if conn is not None:
            conn.subscription = run_sub

    async def _handle_gate_response(self, ws: WebSocket, msg: dict) -> None:
        """反向通道：gate_response → 当前订阅 run 的 gate_handler.resolve。

        ``resolve`` 同步返回是否赢家（False = 晚到，fail loud 已在 handler 内记 warning）。
        未订阅 run 时无 gate_handler 可 resolve → 记 warning。

        **attached run 无 gate_handler**（read-only，SPEC §8 AC11）：前端 ``writable=false``
        时 gate 模态禁提交，正常不会发 gate_response；防御性记 warning（不崩）。
        """
        run_sub = self._subs.get(ws)
        if run_sub is None:
            logger.warning("ws gate_response 但未订阅任何 run（忽略）")
            return
        gate_id = msg.get("gate_id")
        answer = msg.get("answer")
        if not gate_id or answer is None:
            logger.warning("ws gate_response 缺 gate_id/answer（忽略）")
            return
        # attached run read-only（无 gate_handler）；前端按理不会发，防御性记 warning。
        gate_handler = getattr(run_sub.handle, "gate_handler", None)
        if gate_handler is None:
            logger.warning(
                "ws gate_response 但当前订阅 run 无 gate_handler（attached run read-only）"
            )
            return
        gate_handler.resolve(str(gate_id), str(answer), "web")

    async def _pump(
        self, conn: _WSConnection | None, sub: Subscription, run_id: str
    ) -> None:
        """把某 run 的 bus 事件 enqueue 出站（SPEC §13.2 B-4 经 queue 串行化）。

        正常退出：bus close（sub.events 收到 None 哨兵）或 queue writer 已停。
        不直接 ``ws.send_json``——所有出站经 queue，由 ``_writer_loop`` 串行化发送，
        解决 FastAPI 单 WS 并发 send RuntimeError。
        """
        try:
            async for event in sub.events():
                payload = event.model_dump()
                payload["run_id"] = run_id  # 标签：让前端区分来源 run
                if conn is None:
                    return
                try:
                    conn.queue.put_nowait(payload)
                except asyncio.QueueFull:
                    # 慢消费者 → drop（前端 resume 机制兜底，SPEC D6）
                    logger.warning(
                        "ws pump queue full (run=%s) — dropping event", run_id,
                    )
        except WebSocketDisconnect:
            pass  # WS 断开，正常退出
        except Exception:  # noqa: BLE001 — pump 异常 fail loud 记 warning，不 crash server
            logger.warning("ws pump（run=%s）异常退出", run_id, exc_info=True)

    async def _cancel_sub(self, ws: WebSocket) -> None:
        """cancel 当前 WS 的订阅 pump（若有）。幂等。"""
        run_sub = self._subs.pop(ws, None)
        if run_sub is None:
            return
        run_sub.sub.cancel()
        if not run_sub.pump.done():
            run_sub.pump.cancel()
            try:
                await run_sub.pump
            except asyncio.CancelledError:
                pass

    async def _cleanup(self, ws: WebSocket) -> None:
        """WS 断开时的清理：cancel pump + 停 writer task + 移出 _subs/_connections。幂等。"""
        await self._cancel_sub(ws)
        conn = self._connections.pop(ws, None)
        if conn is not None:
            # 停 writer task：put None 哨兵 + cancel 兜底。
            try:
                conn.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            if conn.writer is not None and not conn.writer.done():
                conn.writer.cancel()
                try:
                    await conn.writer
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    pass

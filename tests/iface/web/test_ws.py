"""test_ws.py —— WebSocket 单通道 + 按 run 订阅（SPEC §6.4 / 计划 A2.2）。

覆盖意图：
  - subscribe(A) → 推 A 的事件（带 run_id 标签）。
  - **不推未订阅 run**：subscribe(A)，B 的 emit 收不到（断言，SPEC §0.1 铁律 3）。
  - 切 run：subscribe(A) → unsubscribe → subscribe(B) → 只收 B。
  - gate_response → 对应 run 的 handler.resolve 被调（返回 True = 赢家）。
  - 连接断开 → _subs 清空（无 leaked pump task）。

测试策略：直接构造 ``WebServer`` + 模拟 WebSocket（``asyncio.Queue`` 桥），手动驱动
``_dispatch`` / ``_pump``，避免 TestClient 跨线程 + 真事件的复杂时序（intent-first）。
WS 跨线程真连由 ``test_integration`` 覆盖。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocket

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.gates.handler import HumanGateHandler
from orca.gates.types import HumanGate
from orca.iface.web.run_manager import RunHandle, RunManager
from orca.iface.web.ws_handler import WebServer

from tests.iface.web.conftest import run_async


class FakeWebSocket:
    """模拟 WebSocket：accept/send_json/receive_json/disconnect。

    用 ``asyncio.Queue`` 桥：server ``send_json`` 入 client 队列（client 读收到的事件）；
    client ``receive_json``（经 ``feed``）入 server 队列。断开 = raise WebSocketDisconnect。
    """

    def __init__(self):
        self._sent: asyncio.Queue[dict] = asyncio.Queue()  # server → client
        self._recv: asyncio.Queue[dict | Exception] = asyncio.Queue()  # client → server
        self.accepted = False
        self.closed = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        if self.closed:
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect()
        await self._sent.put(data)

    async def receive_json(self) -> dict:
        item = await self._recv.get()
        if isinstance(item, Exception):
            raise item
        return item

    # client-side helpers（测试驱动用）
    def feed(self, msg: dict) -> None:
        """client 发消息给 server。"""
        self._recv.put_nowait(msg)

    def feed_disconnect(self) -> None:
        from fastapi import WebSocketDisconnect
        self._recv.put_nowait(WebSocketDisconnect())

    async def client_recv(self, timeout: float = 1.0) -> dict:
        return await asyncio.wait_for(self._sent.get(), timeout=timeout)


def _make_handle(tmp_path, run_id: str) -> RunHandle:
    """构造一个真实隔离的 RunHandle（bus + tape + gate_handler）。不启动 run。"""
    from orca.compile import load_workflow
    from pathlib import Path
    import yaml

    p = tmp_path / f"{run_id}.yaml"
    p.write_text(yaml.safe_dump({
        "name": run_id, "entry": "a",
        "nodes": [{"name": "a", "kind": "script", "command": "echo hi",
                   "routes": [{"to": "$end"}]}],
    }))
    wf = load_workflow(p)
    tape = Tape(tmp_path / f"{run_id}.jsonl", run_id=run_id)
    bus = EventBus(tape)
    gate_handler = HumanGateHandler(bus)
    return RunHandle(run_id=run_id, wf=wf, bus=bus, tape=tape, gate_handler=gate_handler)


def _manager_with_handles(tmp_path, *run_ids: str) -> tuple[RunManager, dict[str, RunHandle]]:
    """构造 RunManager + 注入若干 handle（不经 start_run，避免真 run 时序）。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    handles = {}
    for rid in run_ids:
        h = _make_handle(tmp_path, rid)
        manager._runs[rid] = h
        handles[rid] = h
    return manager, handles


async def _close_handles(manager: RunManager) -> None:
    """关掉 manager 内所有测试注入 handle 的 tape/bus（避免 ResourceWarning: unclosed file）。

    测试注入的 handle 不经 start_run → 不经 _teardown_handle，须手动关。
    幂等：bus.close 内部 _closed guard。
    """
    for h in manager._runs.values():
        try:
            h.bus.close()
        except Exception:  # noqa: BLE001
            pass


# ── subscribe 推该 run 事件（SPEC §6.4）───────────────────────────────────


def test_subscribe_pushes_that_run_events(tmp_path):
    """subscribe(A) → A 的 emit 收到（带 run_id 标签）。"""
    manager, handles = _manager_with_handles(tmp_path, "runA")

    async def go():
        server = WebServer(manager)
        ws = FakeWebSocket()
        # 驱动 ws_endpoint 在后台，client 在主线发消息
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)  # 让 accept
        ws.feed({"type": "subscribe", "run_id": "runA"})
        await asyncio.sleep(0.02)  # 让 subscribe 建订阅 + 起 pump
        # emit A 的事件
        await handles["runA"].bus.emit("node_started", {"node": "a"}, node="a")
        msg = await ws.client_recv(timeout=1.0)
        assert msg["type"] == "node_started"
        assert msg["run_id"] == "runA"  # 标签
        # 清理
        ws.feed_disconnect()
        await asyncio.sleep(0.02)
        await server._cleanup(ws)
        endpoint_task.cancel()
        try:
            await endpoint_task
        except (asyncio.CancelledError, Exception):
            pass
        await _close_handles(manager)

    run_async(go())


# ── 不推未订阅 run（SPEC §0.1 铁律 3，核心断言）──────────────────────────


def test_subscribe_a_does_not_receive_b_events(tmp_path):
    """subscribe(A) 后，B 的 emit 收不到（SPEC §0.1 铁律 3 反洪流）。"""
    manager, handles = _manager_with_handles(tmp_path, "runA", "runB")

    async def go():
        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        ws.feed({"type": "subscribe", "run_id": "runA"})
        await asyncio.sleep(0.02)
        # B emit——A 的订阅不该收到
        await handles["runB"].bus.emit("node_started", {"node": "x"}, node="x")
        # A 也 emit 一条（这条该收到）
        await handles["runA"].bus.emit("node_started", {"node": "a"}, node="a")
        msg = await ws.client_recv(timeout=0.5)
        assert msg["run_id"] == "runA", f"收到非订阅 run 事件: {msg}"
        # 再收应该阻塞（B 的事件没推过来）
        with pytest.raises(asyncio.TimeoutError):
            await ws.client_recv(timeout=0.2)
        ws.feed_disconnect()
        await server._cleanup(ws)
        endpoint_task.cancel()
        try:
            await endpoint_task
        except (asyncio.CancelledError, Exception):
            pass
        await _close_handles(manager)

    run_async(go())


# ── 切 run（SPEC §6.4）────────────────────────────────────────────────────


def test_switch_run_unsubscribes_old(tmp_path):
    """subscribe(A) → unsubscribe → subscribe(B) → 只收 B（旧 A pump 已 cancel）。"""
    manager, handles = _manager_with_handles(tmp_path, "runA", "runB")

    async def go():
        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        ws.feed({"type": "subscribe", "run_id": "runA"})
        await asyncio.sleep(0.02)
        ws.feed({"type": "unsubscribe"})
        await asyncio.sleep(0.02)
        ws.feed({"type": "subscribe", "run_id": "runB"})
        await asyncio.sleep(0.02)
        # A emit → 不该收到（已 unsubscribe）
        await handles["runA"].bus.emit("node_started", {"node": "a"}, node="a")
        # B emit → 该收到
        await handles["runB"].bus.emit("node_started", {"node": "b"}, node="b")
        msg = await ws.client_recv(timeout=0.5)
        assert msg["run_id"] == "runB"
        ws.feed_disconnect()
        await server._cleanup(ws)
        endpoint_task.cancel()
        try:
            await endpoint_task
        except (asyncio.CancelledError, Exception):
            pass
        await _close_handles(manager)

    run_async(go())


def test_subscribe_switch_overwrites(tmp_path):
    """subscribe(A) → subscribe(B)（无 unsubscribe）→ 只收 B（subscribe 内部 cancel 旧）。"""
    manager, handles = _manager_with_handles(tmp_path, "runA", "runB")

    async def go():
        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        ws.feed({"type": "subscribe", "run_id": "runA"})
        await asyncio.sleep(0.02)
        ws.feed({"type": "subscribe", "run_id": "runB"})
        await asyncio.sleep(0.02)
        await handles["runA"].bus.emit("node_started", {"node": "a"}, node="a")
        await handles["runB"].bus.emit("node_started", {"node": "b"}, node="b")
        msg = await ws.client_recv(timeout=0.5)
        assert msg["run_id"] == "runB"
        ws.feed_disconnect()
        await server._cleanup(ws)
        endpoint_task.cancel()
        try:
            await endpoint_task
        except (asyncio.CancelledError, Exception):
            pass
        await _close_handles(manager)

    run_async(go())


# ── gate_response 反向通道（SPEC §6.4）────────────────────────────────────


def test_gate_response_resolves_handler(tmp_path):
    """gate_response → 当前订阅 run 的 gate_handler.resolve 被调（赢家返回 True）。"""
    manager, handles = _manager_with_handles(tmp_path, "runA")

    async def go():
        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        ws.feed({"type": "subscribe", "run_id": "runA"})
        await asyncio.sleep(0.02)
        # 起 gate_handler + 注册一个 pending gate
        await handles["runA"].gate_handler.start()
        gate = HumanGate(
            id="g1", prompt="?", context={}, options=["yes", "no"],
            source="tool_permission", run_id="runA", node="a", session_id=None,
        )
        # request 在后台（它会 await fut 直到 resolve）
        req_task = asyncio.create_task(handles["runA"].gate_handler.request(gate))
        await asyncio.sleep(0.02)
        # client 发 gate_response
        ws.feed({"type": "gate_response", "gate_id": "g1", "answer": "yes"})
        answer, source = await asyncio.wait_for(req_task, timeout=1.0)
        assert answer == "yes"
        assert source == "web"
        await handles["runA"].gate_handler.stop()
        ws.feed_disconnect()
        await server._cleanup(ws)
        endpoint_task.cancel()
        try:
            await endpoint_task
        except (asyncio.CancelledError, Exception):
            pass
        await _close_handles(manager)

    run_async(go())


def test_gate_response_without_subscription_ignored(tmp_path):
    """未订阅任何 run 时 gate_response 被忽略（fail loud 记 warning，不崩）。"""
    manager, _ = _manager_with_handles(tmp_path, "runA")

    async def go():
        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        ws.feed({"type": "gate_response", "gate_id": "g1", "answer": "yes"})
        await asyncio.sleep(0.02)
        # 未崩、未订阅
        assert ws not in server._subs
        ws.feed_disconnect()
        await server._cleanup(ws)
        endpoint_task.cancel()
        try:
            await endpoint_task
        except (asyncio.CancelledError, Exception):
            pass
        await _close_handles(manager)

    run_async(go())


# ── 断开清理（SPEC §6.4，无 leaked task）──────────────────────────────────


def test_disconnect_cleans_subscription(tmp_path):
    """WS 断开 → _subs 清空 + pump cancel（无 leaked task，SPEC §6.4）。"""
    manager, handles = _manager_with_handles(tmp_path, "runA")

    async def go():
        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        ws.feed({"type": "subscribe", "run_id": "runA"})
        await asyncio.sleep(0.02)
        assert ws in server._subs
        # 断开
        ws.feed_disconnect()
        await asyncio.sleep(0.05)
        # endpoint_task 已退出（WebSocketDisconnect → finally _cleanup）
        assert endpoint_task.done()
        assert ws not in server._subs, "断开后 _subs 未清空（leak）"
        # pump task 已 cancel/done
        # （_subs 已清，无法直接查 pump；通过 _subs 空验证清理完成）
        await _close_handles(manager)

    run_async(go())


def test_subscribe_unknown_run_ignored(tmp_path):
    """subscribe 未知 run_id → 不订阅（fail loud，连接保持）。"""
    manager, _ = _manager_with_handles(tmp_path, "runA")

    async def go():
        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        ws.feed({"type": "subscribe", "run_id": "nope"})
        await asyncio.sleep(0.02)
        assert ws not in server._subs  # 未订阅
        ws.feed_disconnect()
        await server._cleanup(ws)
        endpoint_task.cancel()
        try:
            await endpoint_task
        except (asyncio.CancelledError, Exception):
            pass
        await _close_handles(manager)

    run_async(go())

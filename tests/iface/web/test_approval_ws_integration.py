"""test_approval_ws_integration.py —— WS approval 第二条 pump + broker 路由（SPEC §4.3 P4）。

覆盖：
  - WS subscribe(run) → broker 自动推 approval_snapshot。
  - broker.publish 经 ws_handler ``_approval_pump`` 推到 WS 出站 queue（``kind:"approval"``）。
  - WS 反向消息 ``request_approval_snapshot`` / ``approval_respond`` / ``approval_yolo`` 正确分派。
  - 切 run：旧 approval pump cancel + unsubscribe（无 leak）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.gates.context_registry import SessionContextRegistry
from orca.iface.web.approval_broker import ApprovalBroker, ApprovalEvent
from orca.iface.web.run_manager import RunHandle, RunManager
from orca.iface.web.ws_handler import WebServer

from tests.iface.web.conftest import FakeWebSocket, run_async


def _make_handle(tmp_path, run_id: str) -> RunHandle:
    from orca.compile import load_workflow
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
    return RunHandle(run_id=run_id, wf=wf, bus=bus, tape=tape)


@pytest.fixture(autouse=True)
def _isolate_yolo_persist(tmp_path: Path, monkeypatch):
    """每测试隔离持久 yolo 文件（防 ``~/.orca/approval-yolo.json`` 残留 yolo=True 污染）。"""
    import orca.iface.web.approval_broker as mod
    monkeypatch.setattr(mod, "_YOLO_PATH", tmp_path / "approval-yolo.json")


def _manager_with_handles(tmp_path, *run_ids: str) -> RunManager:
    manager = RunManager(runs_dir=tmp_path / "runs")
    for rid in run_ids:
        manager._runs[rid] = _make_handle(tmp_path, rid)
    return manager


async def _close_handles(manager: RunManager) -> None:
    for h in manager._runs.values():
        try:
            h.bus.close()
        except Exception:  # noqa: BLE001
            pass


def test_subscribe_emits_initial_approval_snapshot(tmp_path):
    """subscribe(run) → ws_handler 自动推 approval_snapshot（含 pending+yolo）。"""
    manager = _manager_with_handles(tmp_path, "runA")
    broker = ApprovalBroker(manager.registry, timeout=5.0)

    async def go():
        server = WebServer(manager, approval_broker=broker)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        ws.feed({"type": "subscribe", "run_id": "runA"})
        await asyncio.sleep(0.05)
        msg = await ws.client_recv(timeout=1.0)
        assert msg["kind"] == "approval"
        assert msg["type"] == "approval_snapshot"
        assert msg["run_id"] == "runA"
        assert "approvals" in msg
        assert "yolo" in msg
        ws.feed_disconnect()
        await server._cleanup(ws)
        endpoint_task.cancel()
        try:
            await endpoint_task
        except (asyncio.CancelledError, Exception):
            pass
        await broker.shutdown()
        await _close_handles(manager)

    run_async(go())


def test_broker_publish_produces_approval_frame_on_ws(tmp_path):
    """broker ``_publish`` → ws_handler ``_approval_pump`` → 出站 ``kind:"approval"`` 帧。"""
    manager = _manager_with_handles(tmp_path, "runA")
    broker = ApprovalBroker(manager.registry, timeout=5.0)

    async def go():
        server = WebServer(manager, approval_broker=broker)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        ws.feed({"type": "subscribe", "run_id": "runA"})
        await asyncio.sleep(0.05)
        # 排掉初始 snapshot。
        _ = await ws.client_recv(timeout=1.0)
        # broker publish 一条 approval_requested。
        broker._publish(
            "runA",
            ApprovalEvent(
                "approval_requested",
                {"approval_id": "aid-1", "run_id": "runA", "tool": "Bash"},
            ),
        )
        msg = await ws.client_recv(timeout=1.0)
        assert msg["kind"] == "approval"
        assert msg["type"] == "approval_requested"
        assert msg["approval_id"] == "aid-1"
        assert msg["run_id"] == "runA"
        ws.feed_disconnect()
        await server._cleanup(ws)
        endpoint_task.cancel()
        try:
            await endpoint_task
        except (asyncio.CancelledError, Exception):
            pass
        await broker.shutdown()
        await _close_handles(manager)

    run_async(go())


def test_approval_respond_ws_dispatches_to_broker(tmp_path):
    """WS ``approval_respond`` → broker.resolve（first-wins，与 /approval/respond 等价）。"""
    manager = _manager_with_handles(tmp_path, "runA")
    broker = ApprovalBroker(manager.registry, timeout=5.0)
    broker.registry.register("sid-ws", "runA", None)

    async def go():
        server = WebServer(manager, approval_broker=broker)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        ws.feed({"type": "subscribe", "run_id": "runA"})
        await asyncio.sleep(0.05)
        _ = await ws.client_recv(timeout=1.0)  # snapshot
        # 起 broker.request（pending）。
        req_task = asyncio.create_task(
            broker.request({"session_id": "sid-ws", "tool": "Bash", "tool_input": {}}),
        )
        await asyncio.sleep(0.05)
        aid = next(iter(broker._pending.keys()))
        # 通过 WS 反向 resolve。
        ws.feed({"type": "approval_respond", "approval_id": aid, "answer": "allow"})
        result = await asyncio.wait_for(req_task, timeout=2.0)
        assert result["behavior"] == "allow"
        assert result["resolved_by"] == "web"
        ws.feed_disconnect()
        await server._cleanup(ws)
        endpoint_task.cancel()
        try:
            await endpoint_task
        except (asyncio.CancelledError, Exception):
            pass
        await broker.shutdown()
        await _close_handles(manager)

    run_async(go())


def test_switch_run_unsubscribes_old_approval_pump(tmp_path):
    """subscribe(A) → subscribe(B)：旧 A 的 approval pump cancel + unsubscribe（无 leak）。"""
    manager = _manager_with_handles(tmp_path, "runA", "runB")
    broker = ApprovalBroker(manager.registry, timeout=5.0)

    async def go():
        server = WebServer(manager, approval_broker=broker)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        ws.feed({"type": "subscribe", "run_id": "runA"})
        await asyncio.sleep(0.05)
        _ = await ws.client_recv(timeout=1.0)  # A snapshot
        ws.feed({"type": "subscribe", "run_id": "runB"})
        await asyncio.sleep(0.05)
        _ = await ws.client_recv(timeout=1.0)  # B snapshot
        # 切到 B 后，A 的 broker.publish 不该再推到 WS。
        broker._publish(
            "runA",
            ApprovalEvent("approval_requested", {"approval_id": "stale", "run_id": "runA"}),
        )
        await asyncio.sleep(0.05)
        # 期望：A publish 不达；B publish 达。
        broker._publish(
            "runB",
            ApprovalEvent("approval_requested", {"approval_id": "live", "run_id": "runB"}),
        )
        msg = await ws.client_recv(timeout=1.0)
        assert msg["approval_id"] == "live"
        # 不该再收到 stale（A）。
        with pytest.raises(asyncio.TimeoutError):
            await ws.client_recv(timeout=0.2)
        ws.feed_disconnect()
        await server._cleanup(ws)
        endpoint_task.cancel()
        try:
            await endpoint_task
        except (asyncio.CancelledError, Exception):
            pass
        await broker.shutdown()
        await _close_handles(manager)

    run_async(go())


def test_approval_yolo_ws_dispatches_to_broker(tmp_path):
    """WS ``approval_yolo`` → broker.set_yolo。"""
    manager = _manager_with_handles(tmp_path, "runA")
    broker = ApprovalBroker(manager.registry, timeout=5.0)

    async def go():
        server = WebServer(manager, approval_broker=broker)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        ws.feed({"type": "subscribe", "run_id": "runA"})
        await asyncio.sleep(0.05)
        _ = await ws.client_recv(timeout=1.0)  # snapshot
        assert broker.yolo is False
        ws.feed({"type": "approval_yolo", "yolo": True})
        await asyncio.sleep(0.05)
        assert broker.yolo is True
        ws.feed_disconnect()
        await server._cleanup(ws)
        endpoint_task.cancel()
        try:
            await endpoint_task
        except (asyncio.CancelledError, Exception):
            pass
        await broker.shutdown()
        await _close_handles(manager)

    run_async(go())


def test_request_approval_snapshot_message_returns_snapshot(tmp_path):
    """WS ``request_approval_snapshot`` → ws_handler 推一份 snapshot（双保险）。"""
    manager = _manager_with_handles(tmp_path, "runA")
    broker = ApprovalBroker(manager.registry, timeout=5.0)

    async def go():
        server = WebServer(manager, approval_broker=broker)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        ws.feed({"type": "subscribe", "run_id": "runA"})
        await asyncio.sleep(0.05)
        _ = await ws.client_recv(timeout=1.0)  # 初始 snapshot
        # 显式再请求一次。
        ws.feed({"type": "request_approval_snapshot"})
        await asyncio.sleep(0.05)
        msg = await ws.client_recv(timeout=1.0)
        assert msg["type"] == "approval_snapshot"
        ws.feed_disconnect()
        await server._cleanup(ws)
        endpoint_task.cancel()
        try:
            await endpoint_task
        except (asyncio.CancelledError, Exception):
            pass
        await broker.shutdown()
        await _close_handles(manager)

    run_async(go())

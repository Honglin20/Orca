"""test_ws_resume.py —— Web Shell v2 §0 D6 server-side WS resume 单测。

SPEC §0 D6：client 重连发 ``{type:"resume",run_id,since:last_seq_seen}``；server 重放
``tape.replay(since_seq=N)`` 中 seq>N 的事件（按 seq 升序 + 带 run_id 标签），再 subscribe
接 live 流。

覆盖意图（4 条分支）：
  1. happy：tape 有 N 条事件，resume(since=K) → 收到 seq>K 的 (N-K) 条事件，按 seq 升序，
     带 run_id 标签，之后 subscribe 接 live。
  2. run_id 未知 → 记 warning + 回退 subscribe（不重放，不崩）。
  3. since 非法（非数字）→ 记 warning + 回退 subscribe。
  4. tape.replay 抛异常 → 记 warning + 回退 subscribe（live 流不丢）。

测试策略：复用 test_ws.py 的 FakeWebSocket + _make_handle 模式，手动驱动 _dispatch。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.gates.handler import HumanGateHandler
from orca.iface.web.run_manager import RunHandle, RunManager
from orca.iface.web.ws_handler import WebServer

from tests.iface.web.conftest import run_async


def _make_handle(tmp_path, run_id: str) -> RunHandle:
    from pathlib import Path
    import yaml
    from orca.compile import load_workflow

    p = tmp_path / f"{run_id}.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "name": run_id,
                "entry": "a",
                "nodes": [
                    {"name": "a", "kind": "script", "command": "echo hi",
                     "routes": [{"to": "$end"}]},
                ],
            }
        )
    )
    wf = load_workflow(p)
    tape = Tape(tmp_path / f"{run_id}.jsonl", run_id=run_id)
    bus = EventBus(tape)
    gate_handler = HumanGateHandler(bus)
    return RunHandle(run_id=run_id, wf=wf, bus=bus, tape=tape, gate_handler=gate_handler)


def _manager_with_handles(tmp_path, *run_ids: str) -> tuple[RunManager, dict[str, RunHandle]]:
    manager = RunManager(runs_dir=tmp_path / "runs")
    handles = {}
    for rid in run_ids:
        h = _make_handle(tmp_path, rid)
        manager._runs[rid] = h
        handles[rid] = h
    return manager, handles


async def _close_handles(manager: RunManager) -> None:
    for h in list(manager._runs.values()):
        try:
            h.bus.close()
        except Exception:
            pass
        try:
            h.tape.close()
        except Exception:
            pass


# 复用 test_ws.py 的 FakeWebSocket（同实现，避免跨文件 import 私有）
class FakeWebSocket:
    def __init__(self):
        self._sent: asyncio.Queue[dict] = asyncio.Queue()
        self._recv: asyncio.Queue[dict | Exception] = asyncio.Queue()
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

    def feed(self, msg: dict) -> None:
        self._recv.put_nowait(msg)

    def feed_disconnect(self) -> None:
        from fastapi import WebSocketDisconnect
        self._recv.put_nowait(WebSocketDisconnect())

    async def client_recv(self, timeout: float = 1.0) -> dict:
        return await asyncio.wait_for(self._sent.get(), timeout=timeout)


# ── happy path：resume(since=K) → 重放 seq>K ─────────────────────────────────

def test_resume_replays_events_after_since(tmp_path):
    """resume(since=2) → 收到 seq=3,4 的事件（升序 + run_id 标签），之后 subscribe 接 live。"""
    manager, handles = _manager_with_handles(tmp_path, "runA")
    handle = handles["runA"]

    async def go():
        # 直接 append 4 条事件到 tape（不经 bus，模拟历史 tape）
        for i in range(1, 5):
            await handle.tape.append(
                {
                    "type": "node_started",
                    "node": f"n{i}",
                    "session_id": None,
                    "data": {},
                    "timestamp": float(i),
                }
            )

        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        # 直接 dispatch resume（绕过 pump，专注 resume 重放路径）
        await server._dispatch(
            ws, {"type": "resume", "run_id": "runA", "since": 2}
        )
        # 应收到 seq=3,4 两条事件（按 seq 升序）
        msg3 = await ws.client_recv(timeout=1.0)
        msg4 = await ws.client_recv(timeout=1.0)
        assert msg3["seq"] == 3
        assert msg3["run_id"] == "runA"
        assert msg3["type"] == "node_started"
        assert msg4["seq"] == 4
        assert msg4["run_id"] == "runA"
        # resume 之后应自动 subscribe（接 live）；不立即有事件，但 _subs 应有 entry
        assert ws in server._subs

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


# ── D4 watchdog 配套：resume_ok ack 帧（重放完毕后发，client 据此清 watchdog）─────

def test_resume_emits_resume_ok_after_replay(tmp_path):
    """resume(since=K) → 重放完毕 → 发 ``{type:"resume_ok", last_seq}`` ack。

    D4 watchdog 真义：idle 场景（重放零事件 / client 已 caught-up）也发 ack，让 client
    清 watchdog，避免误触发全量重拉。
    """
    manager, handles = _manager_with_handles(tmp_path, "runAck")
    handle = handles["runAck"]

    async def go():
        # 注入 2 条历史事件
        for i in range(1, 3):
            await handle.tape.append(
                {
                    "type": "node_started",
                    "node": f"n{i}",
                    "session_id": None,
                    "data": {},
                    "timestamp": float(i),
                }
            )

        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        # resume(since=1) → 重放 seq=2 → resume_ok(last_seq=2)
        await server._dispatch(
            ws, {"type": "resume", "run_id": "runAck", "since": 1}
        )
        # 第一条：seq=2 业务事件
        msg_event = await ws.client_recv(timeout=1.0)
        assert msg_event["seq"] == 2
        assert msg_event["run_id"] == "runAck"
        # 第二条：resume_ok ack（控制平面帧，不进 EventType）
        msg_ack = await ws.client_recv(timeout=1.0)
        assert msg_ack["type"] == "resume_ok"
        assert msg_ack["run_id"] == "runAck"
        assert msg_ack["last_seq"] == 2

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


def test_resume_zero_replay_still_emits_resume_ok(tmp_path):
    """client 已 caught-up（seq > since 无事件）→ resume_ok(last_seq=0) ack。

    D4 BLOCKER fix：idle 场景（重放零事件）也必须发 ack，否则 client 误判 resume 失败。
    """
    manager, handles = _manager_with_handles(tmp_path, "runIdle")
    handle = handles["runIdle"]

    async def go():
        await handle.tape.append(
            {
                "type": "node_started",
                "node": "n1",
                "session_id": None,
                "data": {},
                "timestamp": 1.0,
            }
        )

        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        # resume(since=5) → tape 中无 seq>5 → 零重放，但仍发 ack
        await server._dispatch(
            ws, {"type": "resume", "run_id": "runIdle", "since": 5}
        )
        msg = await ws.client_recv(timeout=1.0)
        assert msg["type"] == "resume_ok"
        assert msg["run_id"] == "runIdle"
        assert msg["last_seq"] == 0

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


# ── fallback 1：run_id 未知 → 回退 subscribe ──────────────────────────────────

def test_resume_unknown_run_falls_back_to_subscribe(tmp_path):
    manager, _ = _manager_with_handles(tmp_path, "runA")

    async def go():
        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        # resume 未知 run_id
        await server._dispatch(
            ws, {"type": "resume", "run_id": "NONEXISTENT", "since": 0}
        )
        # 不应崩，subscribe 应被调用（_subs 有 entry，挂的是 NONEXISTENT 的 pump——但
        # get_handle 返回 None 时 _handle_subscribe 自身会拒绝；这里只断言不崩 + ws 健在）
        # 由于 _handle_subscribe(NONEXISTENT) 也走 unknown run 路径，_subs 不会加 entry。
        # 断言：没有事件被发送（resume 重放 + subscribe 都没东西发）
        with pytest.raises(asyncio.TimeoutError):
            await ws.client_recv(timeout=0.2)

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


# ── fallback 2：since 非数字 → 回退 subscribe ────────────────────────────────

def test_resume_invalid_since_falls_back_to_subscribe(tmp_path, caplog):
    """since 非数字（None / 字符串）→ 记 warning + 不重放（直接 subscribe）。"""
    import logging
    manager, handles = _manager_with_handles(tmp_path, "runA")
    handle = handles["runA"]

    async def go():
        for i in range(1, 3):
            await handle.tape.append(
                {
                    "type": "node_started",
                    "node": f"n{i}",
                    "data": {},
                    "session_id": None,
                    "timestamp": float(i),
                }
            )

        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        with caplog.at_level(logging.WARNING):
            # since 是字符串（非法）
            await server._dispatch(
                ws, {"type": "resume", "run_id": "runA", "since": "not-a-number"}
            )
        # 应有 warning 记录
        assert any(
            "since" in rec.message and "非数字" in rec.message
            for rec in caplog.records
        ), f"未记录 since 非数字 warning；实际 records={[r.message for r in caplog.records]}"
        # 不重放（since 视为 None）；之后 subscribe（runA 有 handle）→ _subs 有 entry
        assert ws in server._subs
        # 不该有事件送达（subscribe 后等待 live，没有历史 push）
        with pytest.raises(asyncio.TimeoutError):
            await ws.client_recv(timeout=0.2)

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


# ── fallback 3：tape.replay 抛异常 → 回退 subscribe ──────────────────────────

def test_resume_tape_exception_falls_back_to_subscribe(tmp_path, caplog):
    """tape.replay 抛异常 → 记 warning + 回退 subscribe（live 流不丢）。"""
    import logging
    manager, handles = _manager_with_handles(tmp_path, "runA")
    handle = handles["runA"]

    async def go():
        await handle.tape.append(
            {
                "type": "node_started",
                "node": "n1",
                "data": {},
                "session_id": None,
                "timestamp": 1.0,
            }
        )

        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)

        # patch tape.replay 抛异常
        with patch.object(
            type(handle.tape), "replay", side_effect=RuntimeError("simulated tape read fail")
        ):
            with caplog.at_level(logging.WARNING):
                await server._dispatch(
                    ws, {"type": "resume", "run_id": "runA", "since": 0}
                )
        assert any(
            "resume 重放失败" in rec.message for rec in caplog.records
        ), f"未记录 tape 异常 warning；实际 records={[r.message for r in caplog.records]}"
        # 回退 subscribe（runA 有 handle）→ _subs 有 entry
        assert ws in server._subs

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


# ── resume after since=0 等价于全量重放 ──────────────────────────────────────

def test_resume_since_zero_replays_all(tmp_path):
    """since=0 → 重放全部历史事件（等价于初次全量拉取）。"""
    manager, handles = _manager_with_handles(tmp_path, "runA")
    handle = handles["runA"]

    async def go():
        for i in range(1, 4):
            await handle.tape.append(
                {
                    "type": "node_completed",
                    "node": f"n{i}",
                    "data": {"elapsed": 0.1 * i},
                    "session_id": None,
                    "timestamp": float(i),
                }
            )

        server = WebServer(manager)
        ws = FakeWebSocket()
        endpoint_task = asyncio.create_task(server.ws_endpoint(ws))
        await asyncio.sleep(0.01)
        await server._dispatch(
            ws, {"type": "resume", "run_id": "runA", "since": 0}
        )
        # 应收到全部 3 条
        seqs = []
        for _ in range(3):
            msg = await ws.client_recv(timeout=1.0)
            seqs.append(msg["seq"])
        assert seqs == [1, 2, 3]  # 升序

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

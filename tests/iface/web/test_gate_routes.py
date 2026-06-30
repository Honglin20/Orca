"""test_gate_routes.py —— 多 run gate 端点分发（SPEC §5 §6.4 / 计划 A3.4）。

覆盖意图（review blocker 修复：routes/gate.py 此前零覆盖）：
  - ``POST /gate`` session_id 已注册 → 精确路由到该 run handler。
  - ``POST /gate`` 未注册 session_id + 恰好一个活跃 run → fallback 到它。
  - ``POST /gate`` 未注册 + 多个活跃 run → 400（fail loud，避免错路由）。
  - ``POST /gate`` 无活跃 run → 400。
  - ``POST /gate/respond`` 有 run_id → 该 run handler.resolve。
  - ``POST /gate/respond`` 无 run_id + gate_id 在某 run pending → 反查路由。
  - ``POST /gate/respond`` 无匹配 → 404。
  - ``POST /gate/respond`` 缺 gate_id/answer → 400。

真实流程：hook POST /gate → server ``handler.request``（阻塞）→ shell resolve → POST 返回。
测试并发驱动 POST（task）+ 找到 pending gate_id + resolve + await POST。
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from orca.gates.handler import HumanGateHandler
from orca.iface.web.run_manager import RunHandle, RunManager
from orca.iface.web.server import create_app

from tests.iface.web.conftest import run_async


def _client(manager: RunManager) -> AsyncClient:
    app = create_app(manager)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _holding_handle(tmp_path, run_id: str, status: str = "running") -> RunHandle:
    """构造一个 holding handle：status=running，handler 已就绪（不 start）。"""
    import yaml
    from orca.compile import load_workflow
    from orca.events.bus import EventBus
    from orca.events.tape import Tape

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
    return RunHandle(
        run_id=run_id, wf=wf, bus=bus, tape=tape,
        gate_handler=gate_handler, status=status,
    )


def _inject(manager: RunManager, *handles: RunHandle) -> None:
    for h in handles:
        manager._runs[h.run_id] = h


async def _post_gate_and_resolve(client: AsyncClient, handler: HumanGateHandler,
                                 payload: dict) -> tuple[int, dict]:
    """并发：POST /gate（阻塞）+ 找到 server 创建的 pending gate + resolve → 返回 (status, body)。"""
    post_task = asyncio.create_task(client.post("/gate", json=payload))
    # 等 server 端 handler.request 注册 pending gate（轮询 _pending）
    gate_id = None
    for _ in range(50):  # ~0.5s
        await asyncio.sleep(0.01)
        pending = list(handler._pending.keys())
        if pending:
            gate_id = pending[0]
            break
    assert gate_id is not None, "POST /gate 未在 server 端创建 pending gate"
    # shell 侧 resolve（赢家）
    handler.resolve(gate_id, "allow", "test")
    resp = await asyncio.wait_for(post_task, timeout=2.0)
    return resp.status_code, resp.json()


# ── POST /gate 多 run 分发（SPEC §5）──────────────────────────────────────


def test_gate_routes_to_registered_session(tmp_path):
    """session_id 已注册 → 精确路由到该 run 的 handler（而非 fallback 到别的 run）。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    hA = _holding_handle(tmp_path, "runA")
    hB = _holding_handle(tmp_path, "runB")
    _inject(manager, hA, hB)

    async def go():
        await hA.gate_handler.start()
        await hB.gate_handler.start()
        manager.registry.register("sess-A", "runA", "a")
        async with _client(manager) as client:
            status, body = await _post_gate_and_resolve(
                client, hA.gate_handler,
                {"session_id": "sess-A", "tool_name": "Bash",
                 "tool_input": {"cmd": "ls"}, "tool_use_id": "tu1"},
            )
        assert status == 200
        assert body["decision"] == "allow"
        # 确认路由到了 runA（hB 的 handler 无 pending 残留）
        assert not hB.gate_handler._pending
        await hA.gate_handler.stop()
        await hB.gate_handler.stop()
        manager.registry.unregister("sess-A")
        await manager._teardown_handle(hA)
        await manager._teardown_handle(hB)

    run_async(go())


def test_gate_fallback_single_active_run(tmp_path):
    """未注册 session_id + 恰好一个活跃 run → fallback 到它。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    hA = _holding_handle(tmp_path, "runA", status="running")
    _inject(manager, hA)

    async def go():
        await hA.gate_handler.start()
        async with _client(manager) as client:
            status, body = await _post_gate_and_resolve(
                client, hA.gate_handler,
                {"session_id": "unregistered", "tool_name": "Bash"},
            )
        assert status == 200
        await hA.gate_handler.stop()
        await manager._teardown_handle(hA)

    run_async(go())


def test_gate_multiple_active_runs_no_session_400(tmp_path):
    """未注册 session_id + 多个活跃 run → 400（fail loud，避免错路由）。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    hA = _holding_handle(tmp_path, "runA")
    hB = _holding_handle(tmp_path, "runB")
    _inject(manager, hA, hB)

    async def go():
        await hA.gate_handler.start()
        await hB.gate_handler.start()
        async with _client(manager) as client:
            resp = await client.post("/gate", json={
                "session_id": "unregistered", "tool_name": "Bash",
            })
        assert resp.status_code == 400
        await hA.gate_handler.stop()
        await hB.gate_handler.stop()
        await manager._teardown_handle(hA)
        await manager._teardown_handle(hB)

    run_async(go())


def test_gate_no_active_run_400(tmp_path):
    """无活跃 run → 400（fail loud）。"""
    manager = RunManager(runs_dir=tmp_path / "runs")

    async def go():
        async with _client(manager) as client:
            resp = await client.post("/gate", json={
                "session_id": "x", "tool_name": "Bash",
            })
        assert resp.status_code == 400
        await manager.shutdown()

    run_async(go())


# ── POST /gate/respond 多 run 分发（SPEC §5）─────────────────────────────


def test_gate_respond_with_run_id(tmp_path):
    """POST /gate/respond 有 run_id → 该 run handler.resolve（赢家）。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    hA = _holding_handle(tmp_path, "runA")
    _inject(manager, hA)

    async def go():
        await hA.gate_handler.start()
        async with _client(manager) as client:
            # 并发：先建一个 pending gate（模拟 server 端 handler.request），再 respond
            post_task = asyncio.create_task(client.post("/gate", json={
                "session_id": None, "tool_name": "Bash",
            }))
            gate_id = None
            for _ in range(50):
                await asyncio.sleep(0.01)
                pending = list(hA.gate_handler._pending.keys())
                if pending:
                    gate_id = pending[0]
                    break
            assert gate_id is not None
            # respond 带 run_id（这会 resolve，post_task 也返回）
            # 注意：先 respond 还是先等 post？respond 让 post 的 request 返回 → post 完成。
            # 但 respond 本身不阻塞，可并发。
            resp = await client.post("/gate/respond", json={
                "run_id": "runA", "gate_id": gate_id, "answer": "deny",
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is True
            assert body["run_id"] == "runA"
            post_resp = await asyncio.wait_for(post_task, timeout=2.0)
            assert post_resp.json()["decision"] == "deny"
        await hA.gate_handler.stop()
        await manager._teardown_handle(hA)

    run_async(go())


def test_gate_respond_without_run_id_finds_pending(tmp_path):
    """无 run_id + gate_id 在某 run pending → 反查路由（has_pending）。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    hA = _holding_handle(tmp_path, "runA")
    hB = _holding_handle(tmp_path, "runB")
    _inject(manager, hA, hB)

    async def go():
        await hA.gate_handler.start()
        await hB.gate_handler.start()
        async with _client(manager) as client:
            # 注册 session 到 runB，POST /gate 让 runB 建 pending gate
            manager.registry.register("sess-B", "runB", "b")
            post_task = asyncio.create_task(client.post("/gate", json={
                "session_id": "sess-B", "tool_name": "Bash",
            }))
            gate_id = None
            for _ in range(50):
                await asyncio.sleep(0.01)
                if hB.gate_handler._pending:
                    gate_id = next(iter(hB.gate_handler._pending))
                    break
            assert gate_id is not None
            # respond 无 run_id → 应反查到 runB
            resp = await client.post("/gate/respond", json={
                "gate_id": gate_id, "answer": "allow",
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is True
            assert body["run_id"] == "runB"
            await asyncio.wait_for(post_task, timeout=2.0)
        manager.registry.unregister("sess-B")
        await hA.gate_handler.stop()
        await hB.gate_handler.stop()
        await manager._teardown_handle(hA)
        await manager._teardown_handle(hB)

    run_async(go())


def test_gate_respond_no_match_404(tmp_path):
    """无匹配 gate → 404。"""
    manager = RunManager(runs_dir=tmp_path / "runs")

    async def go():
        async with _client(manager) as client:
            resp = await client.post("/gate/respond", json={
                "gate_id": "nope", "answer": "x",
            })
        assert resp.status_code == 404
        await manager.shutdown()

    run_async(go())


def test_gate_respond_missing_fields_400(tmp_path):
    """缺 gate_id/answer → 400（fail loud）。"""
    manager = RunManager(runs_dir=tmp_path / "runs")

    async def go():
        async with _client(manager) as client:
            resp = await client.post("/gate/respond", json={"gate_id": "g"})
        assert resp.status_code == 400
        await manager.shutdown()

    run_async(go())

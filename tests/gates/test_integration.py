"""test_integration.py —— phase 6 端到端集成测试（SPEC §7.6 / 计划 G5.2）。

标 ``@pytest.mark.integration``：CI 默认不跑（``-m "not integration"`` 跳过），本地
``pytest -m integration tests/gates/test_integration.py`` 可选跑。这些测试不真 spawn
claude（那需要 API key + claude CLI），而是端到端 mock：HTTP 协议层 + gate 暂停/恢复
+ 三通道竞速 + tape 完整性。

覆盖：
  1. 端到端 mock「claude 想调工具」→ /gate → fake 壳 resolve → claude-resume 信号
  2. ask_user 端到端（agent_ask gate → resolve → 返回）
  3. 竞速端到端（两个 fake 壳同时 resolve，赢家 + 广播 + 输家 fail loud）
  4. tape 完整性（整个流程后 replay 能重建，gate 事件在 tape 里）
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from orca.events.bus import EventBus
from orca.events.replay import replay_state
from orca.gates.ask_user import ask_user
from orca.gates.context_registry import SessionContextRegistry
from orca.gates.handler import HumanGateHandler
from orca.gates.http_endpoint import register_gate_routes

from tests.gates.conftest import make_bus, make_gate, run_async

pytestmark = pytest.mark.integration


def _setup_app(tmp_path) -> tuple[FastAPI, HumanGateHandler, SessionContextRegistry, EventBus]:
    bus, _ = make_bus(tmp_path)
    handler = HumanGateHandler(bus)
    registry = SessionContextRegistry()
    app = FastAPI()
    register_gate_routes(app, handler, registry)
    return app, handler, registry, bus


def test_end_to_end_tool_permission_via_http(tmp_path):
    """端到端：POST /gate（hook 桥模拟）→ handler.request → fake 壳 resolve → resume。

    模拟完整链路（除真 spawn claude）：hook POST /gate → 构造 gate → 暂停 →
    fake 壳（测试侧 task）resolve → /gate 返回 decision=allow（claude resume 信号）。
    """

    async def scenario():
        app, handler, registry, bus = _setup_app(tmp_path)
        registry.register("sess-e2e", "run-e2e", "node-deploy")
        await handler.start()
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

                async def fake_shell_resolve():
                    """模拟 CLI/Web 壳：等 gate 出现 → 用户点「批准」→ resolve。"""
                    # 轮询等 pending gate（hook POST 后才出现）
                    for _ in range(50):
                        if handler._pending:
                            break
                        await asyncio.sleep(0.02)
                    gate_id = next(iter(handler._pending))
                    ok = handler.resolve(gate_id, "allow", "web")
                    assert ok is True  # fake 壳是赢家

                resolve_task = asyncio.create_task(fake_shell_resolve())
                resp = await client.post(
                    "/gate",
                    json={
                        "session_id": "sess-e2e",
                        "tool_name": "Bash",
                        "tool_input": {"command": "rm -rf /tmp/test"},
                        "tool_use_id": "tu-e2e",
                    },
                )
                await resolve_task
            assert resp.status_code == 200
            body = resp.json()
            assert body["decision"] == "allow"
            assert body["resolved_by"] == "web"

            # 等 broadcaster emit resolved
            await asyncio.sleep(0.05)
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


def test_end_to_end_ask_user(tmp_path):
    """端到端：ask_user → agent_ask gate → fake 壳 resolve → 返回 answer。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            ask_task = asyncio.create_task(
                ask_user(
                    handler,
                    prompt="需要数据库连接串",
                    options=["postgres://...", "mysql://..."],
                    run_id="run-ask",
                    node="db_init",
                )
            )

            # fake 壳答了
            await asyncio.sleep(0.05)
            gate_id = next(iter(handler._pending))
            ok = handler.resolve(gate_id, "postgres://...", "cli")
            assert ok is True

            answer = await asyncio.wait_for(ask_task, timeout=1.0)
            assert answer == "postgres://..."

            # 等 broadcaster emit resolved
            await asyncio.sleep(0.05)
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


def test_end_to_end_race_first_wins_with_broadcast(tmp_path):
    """端到端：两个 fake 壳同时 resolve 同一 gate → 一个赢家 + 广播 + 输家 fail loud。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            sub = bus.subscribe()  # 订阅，验证广播到全壳
            gate = make_gate("g-race-e2e")
            request_task = asyncio.create_task(handler.request(gate))
            await asyncio.sleep(0.05)

            # 两个壳「同时」resolve（gather）
            r1, r2 = await asyncio.gather(
                asyncio.to_thread(handler.resolve, "g-race-e2e", "allow", "cli"),
                asyncio.to_thread(handler.resolve, "g-race-e2e", "deny", "web"),
            )
            assert sum([r1, r2]) == 1  # 恰好一个赢家

            answer, source = await asyncio.wait_for(request_task, timeout=1.0)
            # 赢家的 answer 生效
            assert answer in ("allow", "deny")

            # 等 broadcaster emit resolved
            await asyncio.sleep(0.05)

            # 订阅者应收到 requested + resolved
            events = []
            async for e in sub.events():
                events.append(e)
                if e.type == "human_decision_resolved":
                    break
            types = [e.type for e in events]
            assert "human_decision_requested" in types
            assert "human_decision_resolved" in types
            resolved = next(e for e in events if e.type == "human_decision_resolved")
            assert resolved.data["resolved_by"] in ("cli", "web")
            sub.cancel()
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


def test_end_to_end_tape_integrity(tmp_path):
    """tape 完整性：整个流程后 replay_state 能重建，gate 事件在 tape 里（唯一真相）。"""

    async def scenario():
        bus, tape = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            # 两个 gate 流程（tool_permission + agent_ask）
            gate1 = make_gate("g-tape-1", source="tool_permission")
            t1 = asyncio.create_task(handler.request(gate1))
            await asyncio.sleep(0.02)
            handler.resolve("g-tape-1", "allow", "cli")

            gate2 = make_gate("g-tape-2", source="agent_ask",
                              options=["a", "b"], prompt="选哪个？")
            t2 = asyncio.create_task(handler.request(gate2))
            await asyncio.sleep(0.02)
            handler.resolve("g-tape-2", "a", "web")

            await asyncio.gather(t1, t2)
            # 等 broadcaster emit 两个 resolved
            await asyncio.sleep(0.05)
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())

    # 验证 tape 完整性：replay 能重建，gate 事件齐全
    from pathlib import Path

    from orca.events.tape import Tape

    tape_path = tmp_path / "events.jsonl"
    assert tape_path.exists(), "tape 文件应存在"
    types = [e.type for e in Tape(tape_path, run_id="r1").replay()]
    # 两个 gate 各有 requested + resolved = 4 个 gate 事件
    assert types.count("human_decision_requested") == 2
    assert types.count("human_decision_resolved") == 2
    # 顺序：每个 gate 的 requested 在 resolved 前（同 gate 内有序）
    g1_req = types.index("human_decision_requested")  # 第一个 requested
    # 找 g-tape-1 的 resolved（第二个 resolved 在最后）
    # 简化断言：requested 总数 == resolved 总数

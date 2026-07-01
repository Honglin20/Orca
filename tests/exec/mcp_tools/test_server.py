"""tests/exec/mcp_tools/test_server.py —— AgentToolsMcpServer（phase 11 §5）。

覆盖 INTENT（不仅行为）：
  - SSE server 在空闲 loopback port 起 / 幂等 stop。
  - write_config 产出合法 SSE mcp-config JSON（claude -p ``--mcp-config`` 可读）。
  - ask_user 工具经 in-memory MCP ``ClientSession`` 调通 → 确定性路由参（orca_run_id /
    orca_node）→ 触发 HumanGateHandler.request → 壳答 → 返回 answer。
  - 路由参缺失 → RuntimeError（fail loud，决策 D4）。
  - register_session 路由登记（phase 6 register debt，review B2）。

SSE spike 前置已 PASS（2026-07-02，in-memory + real claude 双通）。
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest
from mcp import ClientSession
from mcp.client.sse import sse_client

from orca.exec.mcp_tools.server import AgentToolsMcpServer
from orca.gates.handler import HumanGateHandler

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_handler(bus) -> HumanGateHandler:
    return HumanGateHandler(bus)


async def _settle_server_start() -> None:
    """等 uvicorn bind port（实测 ~0.3s，给 1.0s 余量）。"""
    await asyncio.sleep(1.0)


# ── 生命周期：start / stop / port ────────────────────────────────────────────


def test_server_starts_on_free_port(tmp_path):
    """start() 返回的 port 在合法范围（1024-65535），且 ``port`` 属性同步。"""

    async def scenario():
        from orca.gates.context_registry import SessionContextRegistry
        from tests.gates.conftest import make_bus

        bus, _ = make_bus(tmp_path)
        handler = _make_handler(bus)
        srv = AgentToolsMcpServer(handler, SessionContextRegistry(), runs_dir=tmp_path)
        try:
            port = await srv.start()
            assert isinstance(port, int)
            assert 1024 < port < 65536
            assert srv.port == port
        finally:
            await srv.stop()
            bus.close()

    asyncio.run(scenario())


def test_server_start_is_idempotent(tmp_path):
    """重复 start 返回同一 port（不重启第二个 server task）。"""

    async def scenario():
        from tests.gates.conftest import make_bus
        from orca.gates.context_registry import SessionContextRegistry

        bus, _ = make_bus(tmp_path)
        handler = _make_handler(bus)
        srv = AgentToolsMcpServer(handler, SessionContextRegistry(), runs_dir=tmp_path)
        try:
            p1 = await srv.start()
            p2 = await srv.start()
            assert p1 == p2
        finally:
            await srv.stop()
            bus.close()

    asyncio.run(scenario())


def test_server_stop_idempotent(tmp_path):
    """stop 幂等：未 start 调 stop / 重复 stop 都不抛。"""
    from tests.gates.conftest import make_bus
    from orca.gates.context_registry import SessionContextRegistry

    bus, _ = make_bus(tmp_path)
    handler = _make_handler(bus)
    srv = AgentToolsMcpServer(handler, SessionContextRegistry(), runs_dir=tmp_path)
    # 未 start → stop 不抛
    asyncio.run(srv.stop())
    asyncio.run(srv.stop())
    bus.close()


# ── write_config ─────────────────────────────────────────────────────────────


def test_write_config_creates_valid_sse_json(tmp_path):
    """write_config 产出 runs/<run_id>/mcp_<session>.json，含 SSE url + server 名。"""
    from tests.gates.conftest import make_bus
    from orca.gates.context_registry import SessionContextRegistry

    bus, _ = make_bus(tmp_path)
    handler = _make_handler(bus)
    srv = AgentToolsMcpServer(handler, SessionContextRegistry(), runs_dir=tmp_path)

    async def scenario():
        port = await srv.start()
        try:
            path = srv.write_config(session_id="sess-1", run_id="run-1", node="cfg")
            assert path.exists()
            assert path.parent == tmp_path / "run-1"
            assert path.name == "mcp_sess-1.json"
            config = json.loads(path.read_text())
            assert "mcpServers" in config
            assert "orca-agent-tools" in config["mcpServers"]
            entry = config["mcpServers"]["orca-agent-tools"]
            assert entry["type"] == "sse"
            assert entry["url"] == f"http://127.0.0.1:{port}/sse"
        finally:
            await srv.stop()
            bus.close()

    asyncio.run(scenario())


def test_write_config_before_start_raises(tmp_path):
    """write_config 在 start() 之前调 → RuntimeError（fail loud，port 未绑定）。"""
    from tests.gates.conftest import make_bus
    from orca.gates.context_registry import SessionContextRegistry

    bus, _ = make_bus(tmp_path)
    handler = _make_handler(bus)
    srv = AgentToolsMcpServer(handler, SessionContextRegistry(), runs_dir=tmp_path)
    with pytest.raises(RuntimeError, match="start\\(\\) 之前"):
        srv.write_config(session_id="s", run_id="r", node="n")
    bus.close()


# ── register_session / lookup（phase 6 register debt，review B2）──────────────


def test_register_and_lookup_session(tmp_path):
    """register_session 登记 → registry.lookup 取回 SessionLoc(run_id, node)。"""
    from orca.gates.context_registry import SessionContextRegistry

    handler = _make_handler(None)  # registry 测试不需真 bus
    reg = SessionContextRegistry()
    srv = AgentToolsMcpServer(handler, reg, runs_dir=tmp_path)
    srv.register_session(session_id="claude-sid-1", run_id="run-1", node="cfg")
    loc = reg.lookup("claude-sid-1")
    assert loc is not None
    assert loc.run_id == "run-1"
    assert loc.node == "cfg"
    # unregister 幂等
    srv.unregister_session("claude-sid-1")
    assert reg.lookup("claude-sid-1") is None
    # 重复 unregister 不抛
    srv.unregister_session("claude-sid-1")


def test_unregister_run_clears_all_sessions_for_run(tmp_path):
    """unregister_run(run_id) 清空该 run 的全部 session 路由（SPEC §6 清理契约，防泄漏）。

    INTENT：长跑 workflow 多 agent node 累积多条 register；run 结束时 orchestrator 调
    unregister_run 批清（session_id 由 executor 内部 uuid 生成，orchestrator 不持有，故按 run 批清）。
    验证：(a) 指定 run 的条目全清；(b) 其它 run 的条目保留；(c) 返回清理条目数。
    """
    from orca.gates.context_registry import SessionContextRegistry

    reg = SessionContextRegistry()
    srv = AgentToolsMcpServer(_make_handler(None), reg, runs_dir=tmp_path)
    # run-A 两条，run-B 一条
    srv.register_session(session_id="sid-a1", run_id="run-A", node="n1")
    srv.register_session(session_id="sid-a2", run_id="run-A", node="n2")
    srv.register_session(session_id="sid-b1", run_id="run-B", node="n1")

    cleared = srv.unregister_run("run-A")
    assert cleared == 2
    assert reg.lookup("sid-a1") is None
    assert reg.lookup("sid-a2") is None
    # run-B 保留
    assert reg.lookup("sid-b1") is not None
    assert reg.lookup("sid-b1").run_id == "run-B"
    # 未注册的 run 幂等返回 0
    assert srv.unregister_run("never") == 0


# start() 的 bind 失败探测：uvicorn bind 失败调 ``sys.exit(1)``（SystemExit）会撕裂
# asyncio loop，无法在 task 内干净捕获，故 ``start()`` 不做同步探测（详见 server.py
# ``start`` docstring 的 TOCTOU 说明）。fail loud 由 orchestrator 的 ``_start_agent_tools``
# 包 try/except 兜底——见 ``tests/run/test_orchestrator_agent_tools.py::
# test_orchestrator_propagates_agent_tools_start_failure``。


# ── ask_user 工具：in-memory Client round-trip（INTENT 核心）──────────────────


def test_ask_user_tool_routes_via_params_and_calls_handler(tmp_path):
    """ask_user 经 in-memory ClientSession 调通 → 路由参 → HumanGateHandler.request → 壳答 → 返回。

    INTENT（决策 D4）：路由靠 ``orca_run_id`` / ``orca_node`` 工具参（**不**依赖 MCP session
    反查）。验证：handler.request 被调时拿到正确的 run_id / node / session_id（=run:node）。

    **SPEC §10.2 item4 验收**：tape 含恰好一对 ``human_decision_requested(source=agent_ask)`` +
    ``human_decision_resolved``（gate_id 一致）——单 tape 真相源契约（CLAUDE.md 底线）。
    """
    from tests.gates.conftest import make_bus
    from orca.gates.context_registry import SessionContextRegistry

    bus, _ = make_bus(tmp_path)
    handler = _make_handler(bus)
    srv = AgentToolsMcpServer(handler, SessionContextRegistry(), runs_dir=tmp_path)

    async def scenario():
        await handler.start()
        port = await srv.start()
        await _settle_server_start()
        try:
            # 开一个 ask_user 协程（会阻塞在 handler.request 等壳答）
            ask_task = asyncio.create_task(_call_ask_user_via_client(port))
            # 等 handler 收到 request（gate 入 _pending）
            gate_id = await _wait_for_pending(handler, timeout=5.0)
            # 验证路由参确实透传到了 gate（INTENT：确定性路由）
            gate = handler._gates_meta[gate_id]
            assert gate.run_id == "run-xyz"
            assert gate.node == "agent-1"
            assert gate.session_id == "run-xyz:agent-1"
            # 壳答
            assert handler.resolve(gate_id, "Alice", "cli") is True
            # 拿工具返回
            result_text = await asyncio.wait_for(ask_task, timeout=5.0)
            assert result_text == "Alice"

            # SPEC §10.2 item4：tape 落 requested + resolved 配对（单 tape 真相源）。
            # resolved 由 broadcaster 异步 emit，等队列消费完。
            await asyncio.sleep(0.1)
            events = list(bus.tape.replay())
            requested = [e for e in events if e.type == "human_decision_requested"]
            resolved = [e for e in events if e.type == "human_decision_resolved"]
            assert len(requested) == 1, f"requested 应恰好 1 个，实际 {len(requested)}"
            assert len(resolved) == 1, f"resolved 应恰好 1 个，实际 {len(resolved)}"
            # requested source=agent_ask（ask_user 触发的 gate 来源）
            assert requested[0].data["source"] == "agent_ask"
            assert requested[0].data["prompt"] == "What's your name?"
            # resolved 答案 + resolved_by
            assert resolved[0].data["answer"] == "Alice"
            assert resolved[0].data["resolved_by"] == "cli"
            # 配对契约：gate_id 一致
            assert requested[0].data["gate_id"] == resolved[0].data["gate_id"]
        finally:
            await srv.stop()
            await handler.stop()
            bus.close()

    asyncio.run(scenario())


def test_ask_user_missing_routing_params_raises(tmp_path):
    """路由参缺失（orca_run_id / orca_node 空）→ 工具返回 isError=True 含 routing params 消息（fail loud，D4）。

    INTENT：路由参是强制的，claude 没带（prompt instruction 没生效 / 被绕过）必须 fail loud，
    不能静默走错 run。FastMCP 把工具抛的异常包装成 ``CallToolResult(isError=True, content=
    [TextContent("Error executing tool ...: <msg>")])`` 返回给客户端（不连 tear down）。
    断言 ``isError=True`` + 文本含 ``routing params``——证明 fail loud 的 RoutingError 被触达。
    """
    from tests.gates.conftest import make_bus
    from orca.gates.context_registry import SessionContextRegistry

    bus, _ = make_bus(tmp_path)
    handler = _make_handler(bus)
    srv = AgentToolsMcpServer(handler, SessionContextRegistry(), runs_dir=tmp_path)

    async def scenario():
        await handler.start()
        port = await srv.start()
        await _settle_server_start()
        try:
            # 三种缺失情况都应 fail loud（isError=True + routing params 消息）。
            for bad_args in (
                {"prompt": "q", "orca_run_id": "r"},            # 缺 orca_node
                {"prompt": "q", "orca_node": "n"},              # 缺 orca_run_id
                {"prompt": "q"},                                # 两个都缺
            ):
                result = await _call_ask_user_raw_result(port, bad_args)
                assert result.isError, f"{bad_args} 未触发 isError"
                text = result.content[0].text  # type: ignore[union-attr]
                assert "routing params" in text.lower(), f"{bad_args} 错误文本缺 routing params：{text}"
        finally:
            await srv.stop()
            await handler.stop()
            bus.close()

    asyncio.run(scenario())


# ── ask_user 工具签名：路由参在 schema 里（claude 可见 / 可填）─────────────────


def test_ask_user_tool_signature_has_routing_params(tmp_path):
    """ask_user 工具的函数签名含 orca_run_id / orca_node（FastMCP 据此生成 inputSchema）。

    INTENT：claude 经 MCP list_tools 看到的 schema 必须含这两个参，否则它无法填。
    验证源码签名（FastMCP 用 inspect.signature 解析）。
    """
    # 找到注册到 FastMCP 的 ask_user 闭包——它定义在 _register_tools 内。
    # 通过 list_tools（不连 server，直接调 FastMCP 内部）拿到 tool 的 fn。
    from orca.gates.context_registry import SessionContextRegistry

    handler = _make_handler(None)
    srv = AgentToolsMcpServer(handler, SessionContextRegistry(), runs_dir=tmp_path)
    # FastMCP 的 _tool_manager 持有注册的 Tool；取其 fn 检签名。
    tools = srv._mcp._tool_manager._tools  # type: ignore[attr-defined]
    assert "ask_user" in tools
    fn = tools["ask_user"].fn
    sig = inspect.signature(fn)
    params = set(sig.parameters)
    assert "prompt" in params
    assert "options" in params
    assert "orca_run_id" in params
    assert "orca_node" in params


# ── 并发隔离：两个 ask_user 各自有独立 gate_id，答案不串台 ─────────────────────


def test_concurrent_ask_user_calls_get_distinct_gates_no_crosstalk(tmp_path):
    """两个并发 ask_user（不同 run:node）拿独立 gate_id；答 gate-A 不送达 gate-B。

    INTENT（隔离不变量 + 路由确定性）：session_id=f"{run_id}:{node}" 是确定性派生。
    两个不同的 (run, node) 组合 → 两个独立 HumanGate（独立 gate_id / 独立 future）。
    resolve(gate_A) 只唤醒 A 的 await，B 仍 pending；resolve(gate_B) 才唤醒 B。
    若路由串台（如两个调用共享一个 future / gate_id 碰撞），workflow 会把 A 的答案送给
    B 的 agent——正确性 + 安全漏洞。本测试是 ask_user 路由可靠性的核心契约。

    验证 SPEC §10.2 item4 的并发面：两对独立的 (requested, resolved) 各自 gate_id 一致。
    """
    from tests.gates.conftest import make_bus
    from orca.gates.context_registry import SessionContextRegistry

    bus, _ = make_bus(tmp_path)
    handler = _make_handler(bus)
    srv = AgentToolsMcpServer(handler, SessionContextRegistry(), runs_dir=tmp_path)

    async def scenario():
        await handler.start()
        port = await srv.start()
        await _settle_server_start()
        try:
            # 两个并发 ask_user（不同 run + node）
            task_a = asyncio.create_task(
                _call_ask_user_with_args(
                    port, prompt="A?", orca_run_id="run-A", orca_node="agent-1"
                )
            )
            task_b = asyncio.create_task(
                _call_ask_user_with_args(
                    port, prompt="B?", orca_run_id="run-B", orca_node="agent-2"
                )
            )

            # 等两个 gate 都进 pending（两个独立 gate_id）
            gate_a, gate_b = await _wait_for_two_pending(handler, timeout=5.0)
            assert gate_a != gate_b, (
                f"两个并发 ask_user 拿到相同 gate_id（{gate_a}）——路由碰撞，答案会串台"
            )

            # 校验路由参确实各不相同（meta 里 run/node 独立）
            meta_a = handler._gates_meta[gate_a]
            meta_b = handler._gates_meta[gate_b]
            assert (meta_a.run_id, meta_a.node) == ("run-A", "agent-1")
            assert (meta_b.run_id, meta_b.node) == ("run-B", "agent-2")
            assert meta_a.session_id == "run-A:agent-1"
            assert meta_b.session_id == "run-B:agent-2"

            # 先答 A，B 仍 pending（隔离：A 的 resolve 不唤醒 B）
            assert handler.resolve(gate_a, "answer-for-A", "cli") is True
            # B 的 resolve 必须返 False（已 resolved 的 gate 才返 False；B 还没 resolve，
            # 这里测的是「A 的 resolve 不影响 B 的 future」——验证 B 仍在 pending）
            assert handler.has_pending(gate_b), (
                "答 gate-A 后 gate-B 不应被唤醒（隔离不变量）——B 应仍在 pending"
            )
            # B 的 task 此刻不应完成（仍阻塞等答案）
            done, _pending = await asyncio.wait({task_a, task_b}, timeout=0.3)
            assert task_a in done and task_b not in done, (
                "答 A 后只有 A 的 task 应完成；B 应仍阻塞"
            )
            assert await task_a == "answer-for-A"

            # 再答 B
            assert handler.resolve(gate_b, "answer-for-B", "web") is True
            assert await asyncio.wait_for(task_b, timeout=5.0) == "answer-for-B"

            # SPEC §10.2 item4 并发面：tape 上两对独立 (requested, resolved)，gate_id 各自配对
            await asyncio.sleep(0.1)  # 等 broadcaster emit resolved 落 tape
            events = list(bus.tape.replay())
            requested = [e for e in events if e.type == "human_decision_requested"]
            resolved = [e for e in events if e.type == "human_decision_resolved"]
            assert len(requested) == 2
            assert len(resolved) == 2
            req_ids = {e.data["gate_id"] for e in requested}
            res_ids = {e.data["gate_id"] for e in resolved}
            assert req_ids == res_ids == {gate_a, gate_b}, (
                f"requested/resolved gate_id 集合应一致且 = {{{gate_a}, {gate_b}}}，"
                f"实际 req={req_ids} res={res_ids}"
            )
            # 答案不串台：A 的 resolved 答案是 answer-for-A，B 的是 answer-for-B
            ans_by_gate = {e.data["gate_id"]: e.data["answer"] for e in resolved}
            assert ans_by_gate[gate_a] == "answer-for-A"
            assert ans_by_gate[gate_b] == "answer-for-B"
        finally:
            await srv.stop()
            await handler.stop()
            bus.close()

    asyncio.run(scenario())


# ── 异步 helpers ──────────────────────────────────────────────────────────────


async def _call_ask_user_via_client(port: int) -> str:
    """经 in-memory MCP Client（SSE transport）调 ask_user，返回 result 文本。"""
    url = f"http://127.0.0.1:{port}/sse"
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "ask_user",
                {
                    "prompt": "What's your name?",
                    "options": ["Alice", "Bob"],
                    "orca_run_id": "run-xyz",
                    "orca_node": "agent-1",
                },
            )
            # result.content 是 list[TextContent]，取第一项 .text
            assert result.content, "ask_user returned empty content"
            return result.content[0].text  # type: ignore[union-attr]


async def _call_ask_user_raw_result(port: int, arguments: dict):
    """经 in-memory Client 调 ask_user，返回 CallToolResult（含 isError + content）。

    用于测 fail loud：工具抛异常时 FastMCP 包装成 ``isError=True`` 的 result 返回（不抛）。
    """
    url = f"http://127.0.0.1:{port}/sse"
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool("ask_user", arguments)


async def _wait_for_pending(handler: HumanGateHandler, timeout: float = 5.0) -> str:
    """等 handler._pending 出现第一个 gate_id（ask_user 调通后 request 入 pending）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if handler._pending:
            return next(iter(handler._pending))
        await asyncio.sleep(0.02)
    raise AssertionError(f"handler._pending 在 {timeout}s 内未出现（ask_user 未到 request）")


async def _wait_for_two_pending(
    handler: HumanGateHandler, timeout: float = 5.0
) -> tuple[str, str]:
    """等 handler._pending 出现至少 2 个 gate_id（两个并发 ask_user 都进 pending）。

    返回 (first, second)（插入序）。用于并发隔离测试——验证两个 ask_user 拿到独立 gate。
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if len(handler._pending) >= 2:
            ids = list(handler._pending)
            return ids[0], ids[1]
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"handler._pending 在 {timeout}s 内未出现 2 个 gate（并发 ask_user 未都到 request）"
    )


async def _call_ask_user_with_args(
    port: int, *, prompt: str, orca_run_id: str, orca_node: str, options: list[str] | None = None
) -> str:
    """经 in-memory Client 调 ask_user，路由参可配置（并发隔离测试用）。"""
    url = f"http://127.0.0.1:{port}/sse"
    args: dict = {"prompt": prompt, "orca_run_id": orca_run_id, "orca_node": orca_node}
    if options is not None:
        args["options"] = options
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("ask_user", args)
            assert result.content, "ask_user returned empty content"
            return result.content[0].text  # type: ignore[union-attr]

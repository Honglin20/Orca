"""test_hook_bridge.py —— hook 脚本 + /gate 端点 + 安全语义（SPEC §3 / 计划 G4.4）。

测试分两组：
  1. **hook_script 子进程 exit code**（安全语义）：真跑 ``orca/gates/hook_script.py``
     子进程，对 in-thread mock HTTP server，断言 exit code（unreachable→2, timeout→2,
     deny→2, allow→0）。这是 HMIL 底线，必须有测试（SPEC §7.4 / 验收总则铁律 4）。
  2. **/gate + /gate/respond 端点**：用 ``httpx.AsyncClient`` + ASGI transport 直接驱动
     FastAPI app（单 asyncio loop，``handler.request`` 的 ``await fut`` 与外部
     ``handler.resolve`` 同 loop，无跨线程 race）。这是端到端 mock：HTTP 协议层 + gate
     暂停/恢复 + session_id 映射。
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from orca.gates.context_registry import SessionContextRegistry
from orca.gates.handler import HumanGateHandler
from orca.gates.http_endpoint import register_gate_routes

from tests.gates.conftest import make_bus, run_async

# hook_script.py 路径：从 orca.gates 包定位（不依赖 tests 目录深度，避免脆弱）。
import orca.gates as _orca_gates_pkg

HOOK_SCRIPT = Path(_orca_gates_pkg.__file__).resolve().parent / "hook_script.py"


# ── mock HTTP server（in-thread，给 hook_script 子进程 POST）──────────────────


class _MockHandler(BaseHTTPRequestHandler):
    """可配置响应的 mock HTTP handler（类变量注入行为）。"""

    # 类变量：测试用例配置（response_body / delay / status / drop_connection / send_raw）
    response_body: object = {"decision": "allow"}
    send_raw: bytes | None = None  # 非空时直接发原始字节（测试非法响应）
    status: int = 200
    delay: float = 0.0

    def log_message(self, *args, **kwargs):  # 静默（不污染测试输出）
        pass

    def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler 协议名)
        if self.delay > 0:
            time.sleep(self.delay)
        if self.send_raw is not None:
            self.send_response(self.status)
            self.send_header("Content-Length", str(len(self.send_raw)))
            self.end_headers()
            self.wfile.write(self.send_raw)
            return
        body = json.dumps(self.response_body).encode("utf-8")
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_mock_server(response_body: object = {"decision": "allow"}, *,
                       send_raw: bytes | None = None, delay: float = 0.0,
                       status: int = 200) -> tuple[ThreadingHTTPServer, int]:
    """启动 in-thread mock HTTP server，返回 (server, port)。"""
    _MockHandler.response_body = response_body
    _MockHandler.send_raw = send_raw
    _MockHandler.delay = delay
    _MockHandler.status = status
    # port=0 让 OS 分配空闲端口（避免端口冲突）
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _run_hook(port: str, timeout: str = "60",
              stdin_payload: dict | None = None) -> int:
    """跑 hook_script.py 子进程（stdin 注入 JSON），返回 exit code。"""
    payload = json.dumps(stdin_payload or {
        "session_id": "sess-1",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_use_id": "tu-1",
    })
    env = {
        "ORCA_PORT": port,
        "ORCA_HOST": "127.0.0.1",
        "ORCA_GATE_TIMEOUT": timeout,
        # 继承最小 PATH（python3 子进程需要）
        "PATH": "/usr/bin:/bin",
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,  # 测试自身保护超时（远大于 hook 的 ORCA_GATE_TIMEOUT）
    )
    return proc.returncode


# ── 1. hook_script exit code 安全语义（SPEC §3.3 §7.4 铁律 4）─────────────────


def test_hook_allow_exits_0():
    """server 返回 decision=allow → exit 0（正常放行）。"""
    server, port = _start_mock_server({"decision": "allow"})
    try:
        assert _run_hook(str(port)) == 0
    finally:
        server.shutdown()


def test_hook_deny_exits_2():
    """server 返回 decision=deny → exit 2。"""
    server, port = _start_mock_server({"decision": "deny"})
    try:
        assert _run_hook(str(port)) == 2
    finally:
        server.shutdown()


def test_hook_unreachable_exits_2():
    """server 不可达（端口没人监听）→ exit 2（HMIL 底线）。"""
    # 找一个肯定没监听的端口：bind 后立即 close，端口通常仍空闲
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    # 短 timeout 加快失败（不需要等默认 60s）
    assert _run_hook(str(port), timeout="2") == 2


def test_hook_timeout_exits_2():
    """server 响应超时（>ORCA_GATE_TIMEOUT）→ exit 2（安全优先，绝不放行）。"""
    # mock server sleep 3s，hook timeout=1s → 超时 → exit 2
    server, port = _start_mock_server({"decision": "allow"}, delay=3.0)
    try:
        assert _run_hook(str(port), timeout="1") == 2
    finally:
        server.shutdown()


def test_hook_malformed_response_exits_2():
    """响应非合法 JSON → exit 2（安全优先）。"""
    server, port = _start_mock_server(send_raw=b"not-json<<<")
    try:
        assert _run_hook(str(port)) == 2
    finally:
        server.shutdown()


def test_hook_missing_decision_field_exits_2():
    """响应是合法 JSON 但缺 decision 字段 → exit 2（仅 allow 放行）。"""
    server, port = _start_mock_server({"foo": "bar"})
    try:
        assert _run_hook(str(port)) == 2
    finally:
        server.shutdown()


def test_hook_unknown_decision_exits_2():
    """decision 非 allow/deny（如 "maybe"）→ exit 2（仅 allow 放行）。"""
    server, port = _start_mock_server({"decision": "maybe"})
    try:
        assert _run_hook(str(port)) == 2
    finally:
        server.shutdown()


def test_hook_empty_stdin_still_safety_first():
    """stdin 为空 → 仍走 POST 流程，安全语义由 server 响应决定。

    注意：``sys.stdin.read()`` 对空 stdin 返回 ``""``（不抛），故 hook 会 POST 空 body。
    安全语义由 server 端的 decision 决定（hook 本身的 fail-loud 分支只在真异常时触发，
    如 stdin pipe 被强制关闭）。此处验证：空 stdin + server allow → exit 0；
    空 stdin + server deny → exit 2（server 仍是安全决策点）。
    """
    # allow
    server, port = _start_mock_server({"decision": "allow"})
    try:
        proc = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input="",
            capture_output=True,
            text=True,
            env={
                "ORCA_PORT": str(port),
                "ORCA_HOST": "127.0.0.1",
                "ORCA_GATE_TIMEOUT": "5",
                "PATH": "/usr/bin:/bin",
            },
            timeout=15,
        )
        assert proc.returncode == 0
    finally:
        server.shutdown()

    # deny
    server, port = _start_mock_server({"decision": "deny"})
    try:
        proc = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input="",
            capture_output=True,
            text=True,
            env={
                "ORCA_PORT": str(port),
                "ORCA_HOST": "127.0.0.1",
                "ORCA_GATE_TIMEOUT": "5",
                "PATH": "/usr/bin:/bin",
            },
            timeout=15,
        )
        assert proc.returncode == 2
    finally:
        server.shutdown()


def test_hook_invalid_timeout_env_falls_back():
    """ORCA_GATE_TIMEOUT 非法（如 "abc"）→ 回退默认，仍按响应 decision exit。"""
    server, port = _start_mock_server({"decision": "allow"})
    try:
        assert _run_hook(str(port), timeout="not-a-number") == 0
    finally:
        server.shutdown()


# ── 2. /gate + /gate/respond 端点（httpx.AsyncClient + ASGI transport）─────
#
# 用 ``httpx.AsyncClient(transport=ASGITransport(app=app))`` 在单一 asyncio loop 内
# 驱动 FastAPI app。这样 ``handler.request`` 的 ``await fut``（端点内）与
# ``handler.resolve``（测试侧 task / 第二个 HTTP 请求）天然同 loop，无跨线程 race。
# 所有端点测试用 ``run_async(scenario())`` 跑。


def _setup_app(tmp_path) -> tuple[FastAPI, HumanGateHandler, SessionContextRegistry]:
    """构造挂了 gate 路由的 FastAPI app（handler 尚未 start，scenario 内 start）。"""
    bus, _ = make_bus(tmp_path)
    handler = HumanGateHandler(bus)
    registry = SessionContextRegistry()
    app = FastAPI()
    register_gate_routes(app, handler, registry)
    return app, handler, registry


def test_gate_endpoint_resolves(tmp_path):
    """/gate：hook payload → handler.request 被调 → fake 壳 resolve → 返回 allow。"""

    async def scenario():
        app, handler, registry = _setup_app(tmp_path)
        registry.register("sess-1", "run-1", "node-1")
        await handler.start()
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # /gate 会阻塞在 handler.request；并发跑一个 task 在 0.05s 后 resolve
                async def delayed_resolve():
                    await asyncio.sleep(0.05)
                    gate_id = next(iter(handler._pending))
                    handler.resolve(gate_id, "allow", "test")

                resolve_task = asyncio.create_task(delayed_resolve())
                resp = await client.post(
                    "/gate",
                    json={
                        "session_id": "sess-1",
                        "tool_name": "Bash",
                        "tool_input": {"command": "ls"},
                        "tool_use_id": "tu-1",
                    },
                )
                await resolve_task
            assert resp.status_code == 200
            body = resp.json()
            assert body["decision"] == "allow"
            assert body["resolved_by"] == "test"
            assert "gate_id" in body
        finally:
            await handler.stop()
            handler._bus.close()

    run_async(scenario())


def test_gate_endpoint_session_id_injects_run_and_node(tmp_path):
    """/gate 从 registry 查到 run_id/node 注入 gate（SPEC §6 §7.4 末条）。"""

    async def scenario():
        app, handler, registry = _setup_app(tmp_path)
        registry.register("sess-42", "run-xyz", "node-deploy")

        captured: dict = {}
        original_request = handler.request

        async def spy_request(gate):
            captured.update(
                run_id=gate.run_id, node=gate.node,
                source=gate.source, tool=gate.context.get("tool"),
            )
            return await original_request(gate)

        handler.request = spy_request  # type: ignore[assignment]
        await handler.start()
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

                async def delayed_resolve():
                    await asyncio.sleep(0.05)
                    gate_id = next(iter(handler._pending))
                    handler.resolve(gate_id, "deny", "web")

                resolve_task = asyncio.create_task(delayed_resolve())
                resp = await client.post(
                    "/gate",
                    json={
                        "session_id": "sess-42",
                        "tool_name": "Bash",
                        "tool_input": {},
                        "tool_use_id": "tu-1",
                    },
                )
                await resolve_task
            assert resp.status_code == 200
            assert resp.json()["decision"] == "deny"
            # 验证 registry 注入到 gate（captured 在 spy_request 内填充）
            assert captured["run_id"] == "run-xyz"
            assert captured["node"] == "node-deploy"
            assert captured["source"] == "tool_permission"
            assert captured["tool"] == "Bash"
        finally:
            await handler.stop()
            handler._bus.close()

    run_async(scenario())


def test_gate_endpoint_unregistered_session_falls_back(tmp_path):
    """未注册 session_id → fallback workflow 级 gate（node=None），不 500。"""

    async def scenario():
        app, handler, registry = _setup_app(tmp_path)  # registry 空
        await handler.start()
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

                async def delayed_resolve():
                    await asyncio.sleep(0.05)
                    gate_id = next(iter(handler._pending))
                    handler.resolve(gate_id, "allow", "test")

                resolve_task = asyncio.create_task(delayed_resolve())
                resp = await client.post(
                    "/gate",
                    json={
                        "session_id": "never-registered",
                        "tool_name": "Bash",
                        "tool_input": {},
                    },
                )
                await resolve_task
            # 不 500，返回 decision（fallback 到 workflow 级 gate）
            assert resp.status_code == 200
            assert resp.json()["decision"] == "allow"
        finally:
            await handler.stop()
            handler._bus.close()

    run_async(scenario())


def test_gate_respond_endpoint(tmp_path):
    """/gate/respond：壳 POST {gate_id, answer, source} → handler.resolve → {ok}。"""

    async def scenario():
        app, handler, registry = _setup_app(tmp_path)
        await handler.start()
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # 先制造一个 pending gate（直接调 handler.request，不经 HTTP）
                from tests.gates.conftest import make_gate

                gate = make_gate("g-resp")
                request_task = asyncio.create_task(handler.request(gate))
                await asyncio.sleep(0.05)  # 等 request 跑到 await fut

                # 第一次 respond → ok=True（赢家）
                resp = await client.post(
                    "/gate/respond",
                    json={"gate_id": "g-resp", "answer": "allow", "source": "web"},
                )
                assert resp.status_code == 200
                assert resp.json() == {"ok": True, "gate_id": "g-resp"}

                # request 应被唤醒
                await asyncio.wait_for(request_task, timeout=1.0)

                # 第二次 respond → ok=False（已 resolved，fail loud）
                resp2 = await client.post(
                    "/gate/respond",
                    json={"gate_id": "g-resp", "answer": "deny", "source": "cli"},
                )
                assert resp2.status_code == 200
                assert resp2.json()["ok"] is False
        finally:
            await handler.stop()
            handler._bus.close()

    run_async(scenario())


def test_gate_respond_missing_fields_400(tmp_path):
    """/gate/respond 缺 gate_id/answer → 400（fail loud，不静默）。"""

    async def scenario():
        app, handler, registry = _setup_app(tmp_path)
        await handler.start()
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/gate/respond", json={"gate_id": "g1"}  # 缺 answer
                )
            assert resp.status_code == 400
        finally:
            await handler.stop()
            handler._bus.close()

    run_async(scenario())

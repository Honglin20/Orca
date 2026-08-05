"""test_orca_permission_hook.py —— PermissionRequest hook stdlib 行为单测（SPEC §3.1）。

覆盖各分支 emit（透传 / timeout-allow / timeout-ask / 网络失败-ask / HTTP 错-deny /
非 JSON-deny / stdin 非 JSON-deny）。用 stdlib http.server 起本地 broker mock。

约定：直接 import hook 模块（py 路径加 sys.path）。hook 仅 stdlib，故测试也仅 stdlib。
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest

HOOK_PATH = (
    Path(__file__).resolve().parents[3]
    / "orca" / "iface" / "in_session" / "templates" / "orca-permission-hook.py"
)


def _load_hook_module():
    """以独立模块名加载 hook 脚本（避免与 templates 包冲突）。"""
    spec = importlib.util.spec_from_file_location(
        "orca_permission_hook_test_only", HOOK_PATH,
    )
    assert spec and spec.loader, "hook 模块加载失败"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook_module()


@pytest.fixture(autouse=True)
def _reload_hook():
    """每测试重新加载 hook 模块（防模块状态 / sys.path 残留）。"""
    global hook
    hook = _load_hook_module()


def _emit_to_dict(capsys_or_mock):
    """辅助：从 mock stdout 取 emit JSON。"""


def test_emit_writes_decision_json(capsys):
    hook._emit("allow", "ok")
    out = capsys.readouterr().out.strip()
    obj = json.loads(out)
    assert obj["decision"]["behavior"] == "allow"
    assert obj["decision"]["reason"] == "ok"


def test_emit_no_reason_omitted(capsys):
    hook._emit("deny")
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["decision"]["behavior"] == "deny"
    assert "reason" not in obj["decision"]


def test_pick_returns_first_match():
    assert hook._pick({"tool_name": "Bash"}, hook._TOOL_NAME_KEYS) == "Bash"
    assert hook._pick({"toolName": "Edit"}, hook._TOOL_NAME_KEYS) == "Edit"
    assert hook._pick({"input": {"x": 1}}, hook._TOOL_INPUT_KEYS) == {"x": 1}
    assert hook._pick({}, hook._TOOL_NAME_KEYS, default="x") == "x"


def test_resolve_session_id_prefers_env(monkeypatch):
    """ORCA_HOST_SESSION_ID > CLAUDE_CODE_SESSION_ID > stdin。"""
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-cc")
    assert hook._resolve_session_id({}) == "from-cc"
    monkeypatch.setenv("ORCA_HOST_SESSION_ID", "from-host")
    assert hook._resolve_session_id({}) == "from-host"
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert hook._resolve_session_id({"sessionId": "from-stdin"}) == "from-stdin"
    assert hook._resolve_session_id({}) is None


# ── broker mock：用 stdlib http.server 起 ThreadingHTTPServer ─────────────────


class _BrokerMock:
    """线程化的 HTTP broker mock。

    handler 按 ``status``, ``body``, ``delay`` 控制响应；测试可注入自定义 handler。
    """

    def __init__(self, status: int = 200, body: str = '{"behavior":"allow"}', delay: float = 0.0):
        self.status = status
        self.body = body
        self.delay = delay
        self.received: list[dict] = []
        self._lock = threading.Lock()
        handler_self = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):  # 静音
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {"_raw": raw}
                with handler_self._lock:
                    handler_self.received.append(payload)
                if handler_self.delay:
                    time.sleep(handler_self.delay)
                self.send_response(handler_self.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(handler_self.body.encode("utf-8"))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def broker_allow():
    b = _BrokerMock(status=200, body='{"behavior":"allow","approval_id":"a1","resolved_by":"user"}')
    yield b
    b.stop()


def _run_hook_with_stdin(stdin_data: str, env: dict, capsys) -> int:
    """模拟 CC spawn 子进程：注入 stdin + env + 调 main()。"""
    with mock.patch.object(sys, "stdin", _StringIOWrapper(stdin_data)):
        for k, v in env.items():
            os.environ[k] = v
        try:
            return hook.main()
        finally:
            for k in env:
                os.environ.pop(k, None)


class _StringIOWrapper:
    """模拟 sys.stdin.read()。"""
    def __init__(self, data: str):
        self._data = data
    def read(self) -> str:
        return self._data


def test_main_passes_through_allow(broker_allow, capsys, monkeypatch):
    """响应 allow → emit allow（透传）。"""
    env = {
        "ORCA_HOST": "127.0.0.1",
        "ORCA_PORT": str(broker_allow.port),
        "ORCA_APPROVAL_TIMEOUT": "5",
    }
    stdin = json.dumps({"tool_name": "Bash", "tool_input": {"cmd": "ls"}})
    code = _run_hook_with_stdin(stdin, env, capsys)
    assert code == 0
    out = capsys.readouterr().out.strip()
    obj = json.loads(out)
    assert obj["decision"]["behavior"] == "allow"
    # broker 收到请求。
    assert len(broker_allow.received) == 1
    assert broker_allow.received[0]["tool"] == "Bash"
    assert broker_allow.received[0]["hook_event"] == "PermissionRequest"


def test_main_deny_passthrough(capsys):
    body = '{"behavior":"deny"}'
    b = _BrokerMock(status=200, body=body)
    try:
        env = {"ORCA_HOST": "127.0.0.1", "ORCA_PORT": str(b.port), "ORCA_APPROVAL_TIMEOUT": "5"}
        code = _run_hook_with_stdin(
            json.dumps({"tool_name": "Write", "tool_input": {}}), env, capsys,
        )
        assert code == 0
        obj = json.loads(capsys.readouterr().out.strip())
        assert obj["decision"]["behavior"] == "deny"
    finally:
        b.stop()


def test_main_timeout_policy_allow(capsys):
    """stdlib timeout（broker delay > approval timeout）→ 按 policy=allow（默认）。"""
    b = _BrokerMock(status=200, body='{"behavior":"allow"}', delay=2.0)
    try:
        env = {
            "ORCA_HOST": "127.0.0.1", "ORCA_PORT": str(b.port),
            "ORCA_APPROVAL_TIMEOUT": "0.5",  # 比 broker delay 短
            "ORCA_APPROVAL_TIMEOUT_POLICY": "allow",
        }
        code = _run_hook_with_stdin(
            json.dumps({"tool": "Bash", "tool_input": {}}), env, capsys,
        )
        assert code == 0
        captured = capsys.readouterr()
        obj = json.loads(captured.out.strip())
        assert obj["decision"]["behavior"] == "allow"
        assert "超时" in captured.err
    finally:
        b.stop()


def test_main_timeout_policy_ask(capsys):
    b = _BrokerMock(status=200, body='{"behavior":"allow"}', delay=2.0)
    try:
        env = {
            "ORCA_HOST": "127.0.0.1", "ORCA_PORT": str(b.port),
            "ORCA_APPROVAL_TIMEOUT": "0.5",
            "ORCA_APPROVAL_TIMEOUT_POLICY": "ask",
        }
        _run_hook_with_stdin(json.dumps({"tool": "Bash"}), env, capsys)
        obj = json.loads(capsys.readouterr().out.strip())
        assert obj["decision"]["behavior"] == "ask"
    finally:
        b.stop()


def test_main_broker_unreachable_returns_ask(capsys):
    """连接失败（端口未监听）→ ask（fail-open to native，SPEC §7）。"""
    # 取一个必然未监听的端口：先 bind + close 拿到 free port，但无 server。
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = {
        "ORCA_HOST": "127.0.0.1", "ORCA_PORT": str(port),
        "ORCA_APPROVAL_TIMEOUT": "1",
    }
    _run_hook_with_stdin(json.dumps({"tool": "Bash"}), env, capsys)
    captured = capsys.readouterr()
    obj = json.loads(captured.out.strip())
    assert obj["decision"]["behavior"] == "ask"
    assert "不可达" in captured.err


def test_main_http_error_returns_deny(capsys):
    """HTTP 4xx/5xx → deny + warn（SPEC §7 / §3.1）。"""
    b = _BrokerMock(status=500, body='{"detail":"boom"}')
    try:
        env = {
            "ORCA_HOST": "127.0.0.1", "ORCA_PORT": str(b.port),
            "ORCA_APPROVAL_TIMEOUT": "5",
        }
        _run_hook_with_stdin(json.dumps({"tool": "Bash"}), env, capsys)
        out = capsys.readouterr()
        obj = json.loads(out.out.strip())
        assert obj["decision"]["behavior"] == "deny"
        assert "HTTP 500" in out.err
    finally:
        b.stop()


def test_main_non_json_response_returns_deny(capsys):
    """响应非 JSON → deny + warn（SPEC §7）。"""
    b = _BrokerMock(status=200, body="not-json{")
    try:
        env = {
            "ORCA_HOST": "127.0.0.1", "ORCA_PORT": str(b.port),
            "ORCA_APPROVAL_TIMEOUT": "5",
        }
        _run_hook_with_stdin(json.dumps({"tool": "Bash"}), env, capsys)
        out = capsys.readouterr()
        obj = json.loads(out.out.strip())
        assert obj["decision"]["behavior"] == "deny"
        assert "非 JSON" in out.err
    finally:
        b.stop()


def test_main_invalid_behavior_returns_deny(capsys):
    """响应 behavior 非法 → deny + warn。"""
    b = _BrokerMock(status=200, body='{"behavior":"bogus"}')
    try:
        env = {"ORCA_HOST": "127.0.0.1", "ORCA_PORT": str(b.port), "ORCA_APPROVAL_TIMEOUT": "5"}
        _run_hook_with_stdin(json.dumps({"tool": "Bash"}), env, capsys)
        out = capsys.readouterr()
        obj = json.loads(out.out.strip())
        assert obj["decision"]["behavior"] == "deny"
    finally:
        b.stop()


def test_main_stdin_non_json_returns_deny(capsys):
    """stdin 非 JSON → deny + warn（fail loud）。"""
    b = _BrokerMock(status=200, body='{"behavior":"allow"}')
    try:
        env = {"ORCA_HOST": "127.0.0.1", "ORCA_PORT": str(b.port), "ORCA_APPROVAL_TIMEOUT": "5"}
        _run_hook_with_stdin("not-json{", env, capsys)
        out = capsys.readouterr()
        obj = json.loads(out.out.strip())
        assert obj["decision"]["behavior"] == "deny"
        assert "非 JSON" in out.err
    finally:
        b.stop()


def test_main_invalid_policy_falls_back_default(capsys, monkeypatch):
    """ORCA_APPROVAL_TIMEOUT_POLICY 非法 → 默认 allow + warn（policy warn + 超时 emit allow）。"""
    b = _BrokerMock(status=200, body='{"behavior":"allow"}', delay=2.0)
    try:
        env = {
            "ORCA_HOST": "127.0.0.1", "ORCA_PORT": str(b.port),
            "ORCA_APPROVAL_TIMEOUT": "0.5",
            "ORCA_APPROVAL_TIMEOUT_POLICY": "bogus",
        }
        _run_hook_with_stdin(json.dumps({"tool": "Bash"}), env, capsys)
        out = capsys.readouterr()
        # 非法 policy → stderr 必报 policy 非法 warn（fail loud 可见）。
        assert "ORCA_APPROVAL_TIMEOUT_POLICY" in out.err
        # 默认 policy=allow；超时路径触发（broker delay > approval timeout）。
        obj = json.loads(out.out.strip())
        assert obj["decision"]["behavior"] == "allow"
    finally:
        b.stop()


def test_main_session_id_taken_from_stdin(capsys):
    """stdin 字段名 sessionId / toolName / toolUseInput 容错读取（N6 fallback）。"""
    b = _BrokerMock(status=200, body='{"behavior":"allow"}')
    try:
        env = {"ORCA_HOST": "127.0.0.1", "ORCA_PORT": str(b.port), "ORCA_APPROVAL_TIMEOUT": "5"}
        # 模拟 stdin：旧式字段名。
        stdin = json.dumps({
            "toolName": "Bash",
            "toolUseInput": {"cmd": "ls"},
            "sessionId": "stdin-sid-1",
        })
        # 清 env 让 stdin 是唯一来源。
        os.environ.pop("ORCA_HOST_SESSION_ID", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        _run_hook_with_stdin(stdin, env, capsys)
        assert b.received[0]["session_id"] == "stdin-sid-1"
        assert b.received[0]["tool"] == "Bash"
        assert b.received[0]["tool_input"] == {"cmd": "ls"}
    finally:
        b.stop()

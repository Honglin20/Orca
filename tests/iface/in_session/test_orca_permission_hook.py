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
    """ORCA_HOST_SESSION_ID > CLAUDE_CODE_SESSION_ID > CAC PID 回溯 > stdin。

    PID 回溯在此 mock 为 None（确定性——不依赖宿主 ``~/.cac/sessions`` 是否存在）。
    """
    with mock.patch.object(hook, "_cac_session_id_from_pid", return_value=None):
        monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-cc")
        assert hook._resolve_session_id({}) == "from-cc"
        monkeypatch.setenv("ORCA_HOST_SESSION_ID", "from-host")
        assert hook._resolve_session_id({}) == "from-host"
        monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        # env 空 + PID 回溯 None → 落 stdin。
        assert hook._resolve_session_id({"sessionId": "from-stdin"}) == "from-stdin"
        assert hook._resolve_session_id({}) is None


def test_resolve_session_id_cac_pid_walk_when_env_absent(monkeypatch):
    """env 两键皆空 → CAC PID 回溯取值（CAC 不注入 CLAUDE_CODE_SESSION_ID 的兜底）。

    覆盖 CAC 修复的核心：无 env 时 hook 仍能拿到 session_id → broker 双键命中 → yolo 可达。
    """
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with mock.patch.object(hook, "_cac_session_id_from_pid", return_value="cac-sid-1"):
        assert hook._resolve_session_id({}) == "cac-sid-1"


def test_resolve_session_id_cac_pid_walk_beats_stdin(monkeypatch):
    """PID 回溯优先于 stdin（与 host_session_from_env 对齐：进程身份 > payload）。

    保证 hook 取值与 tape ``data.host_session``（同款 PID 回溯写出）一致 → broker 命中。
    """
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with mock.patch.object(hook, "_cac_session_id_from_pid", return_value="cac-sid-1"):
        assert hook._resolve_session_id({"sessionId": "stdin-sid"}) == "cac-sid-1"


def test_resolve_session_id_env_beats_cac_pid_walk(monkeypatch):
    """CC 路径：env 第二键短路，PID 回溯根本不触达。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-cc")
    with mock.patch.object(
        hook, "_cac_session_id_from_pid", return_value="should-not-be-used",
    ) as m:
        assert hook._resolve_session_id({}) == "from-cc"
        m.assert_not_called()


def test_cac_session_id_from_pid_no_sessions_dir(monkeypatch, tmp_path):
    """``~/.cac/sessions`` 不存在 → 立即 None（跨平台确定性守卫，不触 ``/proc``）。

    非 CAC 环境（含 Windows）的确定性兜底；真机 codeagentcli 路径靠行为等价
    ``_hostenv.cac_session_id_from_pid`` / ``cc_nudge.sh`` 保证（无 CAC 环境无法 e2e）。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows expanduser 兜底
    assert hook._cac_session_id_from_pid() is None


def test_cac_pid_walk_drift_gate_against_canonical():
    """DRY 漂移闸门：hook 的 ``_cac_session_id_from_pid`` 守恒常量必须与基准 ``_hostenv`` 同步。

    这是第三份副本（``_hostenv.py`` / ``cc_nudge.sh`` / hook 模板），三者皆 stdlib-only /
    跑在无 Orca venv 的子进程，不能 import 共享——改基准忘改 hook → hook 取值 ≠ tape
    ``data.host_session`` → broker 双键 miss → **yolo 静默失效（本次 bug 原样复发）**，且无
    CAC 真机能抓。本闸门把"守恒常量"从 inspection 物化为可执行断言：改基准常量而忘改 hook → fail。
    范式仿 ``test_host_session_binding.py`` 的 cc_nudge DRY 漂移闸门。不断言风格（pathlib vs
    os.path），只断言语义不变量。
    """
    hostenv_path = (
        Path(__file__).resolve().parents[3]
        / "orca" / "iface" / "in_session" / "_hostenv.py"
    )
    hostenv_src = hostenv_path.read_text(encoding="utf-8")
    hook_src = HOOK_PATH.read_text(encoding="utf-8")
    # 守恒不变量：改任一都意味着 CAC 身份解析语义变了，hook 必须同步。
    invariants = [
        "codeagentcli",                                                # exe 精确匹配串
        "range(20)",                                                   # PID 链回溯上界
        "(FileNotFoundError, PermissionError, ValueError, IndexError)",  # status 异常元组
        "(json.JSONDecodeError, KeyError)",                            # session 文件异常元组
    ]
    for inv in invariants:
        assert inv in hostenv_src, (
            f"基准 _hostenv 不再含守恒常量 {inv!r}——闸门自身需更新（确认 hook 是否也要跟改）"
        )
        assert inv in hook_src, (
            f"hook 模板与 _hostenv 漂移：缺守恒常量 {inv!r}（hook 取值将 ≠ tape，yolo 会静默失效）"
        )


def test_resolve_session_id_cac_pid_walk_exception_falls_back_to_stdin(monkeypatch):
    """PID 回溯抛任意异常（含 UnicodeDecodeError）→ 回退 stdin，绝不让 hook 崩溃。

    fail-safety：``_resolve_session_id`` 位于 main() 无保护区，PID 回溯 best-effort 包裹后
    异常必须降级为 None → 走 stdin → 最终 None 时 main 仍能 emit ask（而非 exit 1 无 decision）。
    """
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    def _boom():
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "simulated non-utf8 session file")

    with mock.patch.object(hook, "_cac_session_id_from_pid", side_effect=_boom):
        # PID 回溯炸 → 回退 stdin。
        assert hook._resolve_session_id({"sessionId": "stdin-sid"}) == "stdin-sid"
        # PID 回溯炸 + 无 stdin → None（main 会落 ask，不崩）。
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
        # 清 env 让 stdin 是唯一来源；mock PID 回溯为 None（确定性，隔离宿主 ~/.cac 状态）。
        os.environ.pop("ORCA_HOST_SESSION_ID", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        with mock.patch.object(hook, "_cac_session_id_from_pid", return_value=None):
            _run_hook_with_stdin(stdin, env, capsys)
        assert b.received[0]["session_id"] == "stdin-sid-1"
        assert b.received[0]["tool"] == "Bash"
        assert b.received[0]["tool_input"] == {"cmd": "ls"}
    finally:
        b.stop()

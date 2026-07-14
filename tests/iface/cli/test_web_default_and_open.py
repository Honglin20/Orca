"""test_web_default_and_open.py —— ``orca run`` web 默认 + ``orca open`` CLI 测试。

SPEC web-attach-and-default §4 / §5 / §8 AC5-7 / 11。

覆盖：
  - ``orca run <wf>``（默认 web）：``webbrowser.open`` 收 ``/runs/<id>``；run 终态后
    auto-exit；负向：活跃 WS 不退；``ORCA_WEB_AUTOEXIT_SECONDS=1`` 加速。
  - ``orca run --tui`` → Textual TUI（opt-in 保留）。
  - ``orca run --background`` → detached + run_id + pid（既有行为不变）。
  - ``orca open <id>``：probe / spawn serve / attach / browser open；负向：tape 缺失 exit 2。
  - ``/orca open`` slash：plugin ``orca.ts`` 加 ``open`` dispatch（signature-contract）。
  - 铁律：单 store/registry grep unchanged；TUI 代码仍在。
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from orca.iface.cli.commands import (
    DEFAULT_WEB_AUTOEXIT_SECONDS,
    DEFAULT_WEB_PORT,
    EXIT_ARG_OR_VALIDATE,
    EXIT_OK,
    EXIT_RUN_FAILED,
    app,
    _find_free_port,
    _is_port_free,
    _probe_orca_server,
    _web_autoexit_seconds,
    resolve_web_endpoint,
)

runner = CliRunner()


# ── fixtures ────────────────────────────────────────────────────────────────


SIMPLE_WF_YAML = """\
name: web_default_wf
description: 1-agent 线性 workflow（web 默认 / open 测试）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 A。"
    routes:
      - to: $end
"""


@pytest.fixture
def wf_path(tmp_path: Path) -> Path:
    p = tmp_path / "wf.yaml"
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    return p


def _is_httpx_available() -> bool:
    return importlib.util.find_spec("httpx") is not None


# ── resolve_web_endpoint（host/port 单一真相源 + 远程可见性）──────────────────


class TestResolveWebEndpoint:
    """``resolve_web_endpoint``：serve/run/open 三路径共用的端点解析（2026-07-08 远程化）。

    覆盖意图：默认 bind 0.0.0.0（远程可见）；--host / ORCA_WEB_HOST 覆盖；display_host
    在 bind 0.0.0.0 时给实际 IP（ORCA_PUBLIC_HOST 优先）；port --port / ORCA_WEB_PORT 覆盖。
    """

    def test_defaults_bind_all_interfaces(self, monkeypatch):
        """无参 → bind 0.0.0.0（远程可访问），port 默认 7428。"""
        monkeypatch.delenv("ORCA_WEB_HOST", raising=False)
        monkeypatch.delenv("ORCA_WEB_PORT", raising=False)
        monkeypatch.delenv("ORCA_PUBLIC_HOST", raising=False)
        bind, display, port = resolve_web_endpoint(host=None, port=None)
        assert bind == "0.0.0.0"
        assert port == DEFAULT_WEB_PORT
        # display 是某 IP（探测或 loopback），非 0.0.0.0（不能点开）
        assert display != "0.0.0.0"

    def test_explicit_host_overrides_env_and_default(self, monkeypatch):
        monkeypatch.setenv("ORCA_WEB_HOST", "9.9.9.9")
        bind, display, port = resolve_web_endpoint(host="1.2.3.4", port=None)
        assert bind == "1.2.3.4"
        assert display == "1.2.3.4"  # 具体地址 → display = bind

    def test_env_host_when_no_flag(self, monkeypatch):
        monkeypatch.setenv("ORCA_WEB_HOST", "127.0.0.1")
        bind, display, port = resolve_web_endpoint(host=None, port=None)
        assert bind == "127.0.0.1"
        assert display == "127.0.0.1"

    def test_public_host_env_overrides_detection(self, monkeypatch):
        """bind 0.0.0.0 + ORCA_PUBLIC_HOST → display 用 env（容器/反代场景）。"""
        monkeypatch.setenv("ORCA_PUBLIC_HOST", "my-server.example.com")
        bind, display, port = resolve_web_endpoint(host=None, port=None)
        assert bind == "0.0.0.0"
        assert display == "my-server.example.com"

    def test_port_flag_overrides_env(self, monkeypatch):
        monkeypatch.setenv("ORCA_WEB_PORT", "8000")
        bind, display, port = resolve_web_endpoint(host=None, port=9000)
        assert port == 9000

    def test_port_env_when_no_flag(self, monkeypatch):
        monkeypatch.setenv("ORCA_WEB_PORT", "8000")
        _, _, port = resolve_web_endpoint(host=None, port=None)
        assert port == 8000

    def test_invalid_port_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("ORCA_WEB_PORT", "not-a-number")
        _, _, port = resolve_web_endpoint(host=None, port=None)
        assert port == DEFAULT_WEB_PORT


# ── _web_autoexit_seconds env 解析（AC5 测试加速前置）────────────────────────


class TestWebAutoexitSeconds:
    """``ORCA_WEB_AUTOEXIT_SECONDS`` env 解析（SPEC §0 D4 + §4 step4）。"""

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("ORCA_WEB_AUTOEXIT_SECONDS", raising=False)
        assert _web_autoexit_seconds() == DEFAULT_WEB_AUTOEXIT_SECONDS

    def test_override(self, monkeypatch):
        monkeypatch.setenv("ORCA_WEB_AUTOEXIT_SECONDS", "1")
        assert _web_autoexit_seconds() == 1.0

    def test_non_numeric_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("ORCA_WEB_AUTOEXIT_SECONDS", "abc")
        assert _web_autoexit_seconds() == DEFAULT_WEB_AUTOEXIT_SECONDS

    def test_zero_or_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv("ORCA_WEB_AUTOEXIT_SECONDS", "0")
        assert _web_autoexit_seconds() == DEFAULT_WEB_AUTOEXIT_SECONDS
        monkeypatch.setenv("ORCA_WEB_AUTOEXIT_SECONDS", "-5")
        assert _web_autoexit_seconds() == DEFAULT_WEB_AUTOEXIT_SECONDS


# ── 端口工具函数 ─────────────────────────────────────────────────────────────


class TestPortHelpers:
    """``_find_free_port`` / ``_is_port_free`` / ``_probe_orca_server``。"""

    def test_find_free_port_returns_int(self):
        port = _find_free_port()
        assert isinstance(port, int) and 1024 < port < 65536

    def test_find_free_port_preferred_when_free(self):
        # 挑一个空闲端口作 preferred，验证返回同值。
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        preferred = s.getsockname()[1]
        s.close()
        # 释放后立即 find（race 窗口小）。
        assert _find_free_port(preferred=preferred) == preferred

    def test_is_port_free_for_unbound(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        # 释放后（race 小）应判空闲。
        assert _is_port_free("127.0.0.1", port) is True

    def test_is_port_free_false_when_bound(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            assert _is_port_free("127.0.0.1", port) is False
        finally:
            s.close()

    def test_probe_orca_server_unreachable_returns_none(self):
        # 选一个几乎肯定没监听的端口（不与 default 7428 冲突）。
        port = _find_free_port()
        # _find_free_port 刚释放，重新 bind 占用让探测失败。
        assert _probe_orca_server("127.0.0.1", port, timeout=0.2) is None

    def test_probe_orca_server_non_orca_returns_none(self):
        # 启一个只回 "not orca" 的假 server，探测应判非 orca → None。
        import httpx
        from fastapi import FastAPI

        fake = FastAPI()

        @fake.get("/api/health")
        def _h():
            return {"app": "not-orca"}

        # uvicorn 没装则 skip。
        pytest.importorskip("uvicorn")
        import uvicorn

        port = _find_free_port()
        config = uvicorn.Config(fake, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)

        async def _run():
            t = asyncio.create_task(server.serve())
            await asyncio.sleep(0.6)
            result = _probe_orca_server("127.0.0.1", port, timeout=1.0)
            server.should_exit = True
            await asyncio.sleep(0.3)
            return result

        try:
            result = asyncio.run(_run())
        except RuntimeError:
            return
        assert result is None


# ── orca run --tui / --background 既有行为保留（AC6）─────────────────────────


class TestRunFlagsPreserved:
    """``--tui`` opt-in / ``--background`` 不变（SPEC §4 D5 + AC6）。"""

    def test_run_tui_invokes_run_workflow(self, wf_path, monkeypatch):
        """``--tui`` → ``_run_workflow`` 调用（旧 TUI 路径，D5 opt-in）。"""
        called = {"n": 0}

        def _fake_run_workflow(config):
            called["n"] += 1
            return 0

        monkeypatch.setattr(
            "orca.iface.cli.commands._run_workflow", _fake_run_workflow,
        )
        result = runner.invoke(app, ["run", str(wf_path), "--tui"])
        assert result.exit_code == EXIT_OK
        assert called["n"] == 1, "—-tui 应走 _run_workflow（旧 TUI 路径）"

    def test_run_background_unaffected(self, wf_path, monkeypatch):
        """``--background`` 仍走 daemonize（不受 web 默认改动影响）。"""
        called = {"n": 0}

        def _fake_start_background(yaml, task, i_args, max_iter):
            called["n"] += 1
            return 0

        monkeypatch.setattr(
            "orca.iface.cli.commands._start_background", _fake_start_background,
        )
        result = runner.invoke(app, ["run", str(wf_path), "--background"])
        assert result.exit_code == EXIT_OK
        assert called["n"] == 1


# ── orca run web 默认（AC5）─────────────────────────────────────────────────


class TestRunWebDefault:
    """``orca run <wf>``（默认 web）：probe / spawn-serve / browser / auto-exit（SPEC §4 + §8 AC5）。"""

    def test_web_default_calls_web_path(self, wf_path, monkeypatch):
        """默认（无 flag）→ ``_run_web_default`` 调用（非 _run_workflow）。"""
        called = {"web": 0, "tui": 0}

        def _fake_web(config, *, host, port, stay):
            called["web"] += 1
            return 0

        def _fake_tui(config):
            called["tui"] += 1
            return 0

        monkeypatch.setattr(
            "orca.iface.cli.commands._run_web_default", _fake_web,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._run_workflow", _fake_tui,
        )
        result = runner.invoke(app, ["run", str(wf_path)])
        assert result.exit_code == EXIT_OK
        assert called["web"] == 1
        assert called["tui"] == 0

    def test_web_default_reuse_existing_server(self, wf_path, monkeypatch):
        """既有 orca server（探测 hit）→ 走 ``_post_run_to_existing`` + 轮询。"""
        calls = {"post": 0, "poll": 0, "browser": 0}

        def _probe(host, port, timeout=0.5):
            return {"app": "orca", "version": "x", "pid": 1}

        def _post(host, port, config):
            calls["post"] += 1
            return "run-xyz"

        def _poll(host, port, run_id, timeout=None):
            calls["poll"] += 1
            return "completed"

        def _open_browser(url):
            calls["browser"] += 1
            assert "/runs/run-xyz" in url, f"webbrowser.open 应收 /runs/<id>，收到 {url}"

        monkeypatch.setattr("orca.iface.cli.commands._probe_orca_server", _probe)
        monkeypatch.setattr(
            "orca.iface.cli.commands._post_run_to_existing", _post,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._poll_run_terminal", _poll,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._open_browser_or_print", _open_browser,
        )
        rc = runner.invoke(app, ["run", str(wf_path)]).exit_code
        assert rc == EXIT_OK
        assert calls["post"] == 1 and calls["poll"] == 1 and calls["browser"] == 1

    def test_web_default_reuse_failed_run_exit_one(self, wf_path, monkeypatch):
        """复用既有 server，run 终态 failed → exit 1（SPEC §4 step5 退出码）。"""
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: {"app": "orca", "version": "x", "pid": 1},
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._post_run_to_existing",
            lambda host, port, config: "run-x",
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._poll_run_terminal",
            lambda host, port, run_id, timeout=None: "failed",
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._open_browser_or_print", lambda url: None,
        )
        rc = runner.invoke(app, ["run", str(wf_path)]).exit_code
        assert rc == EXIT_RUN_FAILED

    def test_web_default_invalid_yaml_exits_two(self, tmp_path, monkeypatch):
        """yaml 不存在 / 校验失败 → exit 2（前置校验，不进入 web 路径）。"""
        # 不存在文件。
        rc = runner.invoke(app, ["run", str(tmp_path / "nope.yaml")]).exit_code
        assert rc == EXIT_ARG_OR_VALIDATE

    def test_port_occupied_by_non_orca_with_explicit_port_exits_two(
        self, wf_path, monkeypatch,
    ):
        """``--port`` 显式 + 被 non-orca 占 → fail loud exit 2（SPEC §7「--port 被占」）。"""
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: None,  # 探测到非 orca（None）
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._is_port_free", lambda host, port: False,
        )
        rc = runner.invoke(
            app, ["run", str(wf_path), "--port", "9999"],
        ).exit_code
        assert rc == EXIT_ARG_OR_VALIDATE

    def test_web_default_in_process_branch_invoked(
        self, wf_path, monkeypatch,
    ):
        """无既有 orca server（probe None）→ 进入 ``_serve_and_run_inprocess`` 分支。"""
        calls = {"serve": 0, "args": None}

        async def _fake_serve(config, wf, *, bind_host, display_host, port, stay):
            calls["serve"] += 1
            calls["args"] = {
                "bind_host": bind_host, "display_host": display_host,
                "port": port, "stay": stay,
            }
            return 0

        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: None,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._is_port_free", lambda host, port: True,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._serve_and_run_inprocess", _fake_serve,
        )
        rc = runner.invoke(app, ["run", str(wf_path)]).exit_code
        assert rc == EXIT_OK
        assert calls["serve"] == 1
        # 默认端口空闲 → 用默认端口。
        assert calls["args"]["port"] == DEFAULT_WEB_PORT
        assert calls["args"]["stay"] is False

    def test_web_default_post_run_runtime_error_exits_one(
        self, wf_path, monkeypatch,
    ):
        """复用既有 server 时 POST /api/run 抛 RuntimeError → exit 1（fail loud，B3 闭环）。"""
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: {"app": "orca", "version": "x", "pid": 1},
        )

        def _raise(host, port, config):
            raise RuntimeError("POST /api/run HTTP 500: internal")

        monkeypatch.setattr(
            "orca.iface.cli.commands._post_run_to_existing", _raise,
        )
        rc = runner.invoke(app, ["run", str(wf_path)]).exit_code
        assert rc == EXIT_RUN_FAILED

    def test_web_default_stay_warn_in_reuse_mode(self, wf_path, monkeypatch):
        """``--stay`` 在复用既有 server 模式下提示用户（不静默忽略）。"""
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: {"app": "orca", "version": "x", "pid": 1},
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._post_run_to_existing",
            lambda host, port, config: "rid",
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._poll_run_terminal",
            lambda host, port, run_id, timeout=None: "completed",
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._open_browser_or_print", lambda url: None,
        )
        result = runner.invoke(app, ["run", str(wf_path), "--stay"])
        assert result.exit_code == EXIT_OK
        assert "--stay" in result.output and "不适用" in result.output


# ── _serve_and_run_inprocess 直接单测（mock RunManager + uvicorn.Server）────


class TestServeAndRunInprocess:
    """``_serve_and_run_inprocess`` 各退出分支单测（M1 闭环）。"""

    def test_start_run_configuration_error_returns_two(self, wf_path, monkeypatch):
        """start_run 抛 ConfigurationError → exit 2（不进入 serve 主循环）。"""
        from orca.compile import ConfigurationError
        from orca.iface.cli.commands import (
            RunConfig,
            _serve_and_run_inprocess,
            _wait_server_started,
        )

        # mock uvicorn + create_app + RunManager：manager.start_run 抛 ConfigurationError。
        class _FakeServer:
            def __init__(self, config):
                self.started = True
                self.should_exit = False

            async def serve(self):
                await asyncio.sleep(100)  # 永不自然返回（should_exit 控制）

        class _FakeManager:
            async def start_run(self, *a, **kw):
                raise ConfigurationError("bad config", [])

            async def shutdown(self):
                pass

        async def _run():
            # 直接 await（不 asyncio.run，避免 double-wrap）。
            return await _serve_and_run_inprocess(
                RunConfig(yaml_path=wf_path),
                wf=None,
                bind_host="127.0.0.1",
                display_host="127.0.0.1",
                port=12345,
                stay=False,
            )

        # Patch deps inside the function.
        import sys
        import types

        fake_uvicorn_mod = types.ModuleType("uvicorn")
        fake_uvicorn_mod.Config = lambda *a, **kw: None
        fake_uvicorn_mod.Server = _FakeServer
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn_mod)

        fake_web_mod = types.ModuleType("orca.iface.web")
        fake_web_mod.RunManager = lambda **kw: _FakeManager()
        monkeypatch.setitem(sys.modules, "orca.iface.web", fake_web_mod)

        fake_server_mod = types.ModuleType("orca.iface.web.server")
        fake_server_mod.create_app = lambda manager: types.SimpleNamespace(
            state=types.SimpleNamespace(web_server=types.SimpleNamespace(
                last_ws_activity_at=time.monotonic(),
            ))
        )
        monkeypatch.setitem(sys.modules, "orca.iface.web.server", fake_server_mod)

        # Avoid _wait_server_started 真等。
        monkeypatch.setattr(
            "orca.iface.cli.commands._wait_server_started",
            lambda server, timeout: asyncio.sleep(0),
        )

        rc = asyncio.run(_run())
        assert rc == EXIT_ARG_OR_VALIDATE

    def test_keyboard_interrupt_returns_via_outer_layer(self, monkeypatch, wf_path):
        """Ctrl-C 路径：CancelledError 捕获 → exit code 保留 → 外层映射 130。

        本测试只验证 _serve_and_run_inprocess 内部 CancelledError 不裸抛；
        外层 130 映射在 _run_web_default 测试覆盖。
        """
        from orca.iface.cli.commands import (
            RunConfig,
            _serve_and_run_inprocess,
        )

        class _FakeServer:
            def __init__(self, config):
                self.started = True
                self.should_exit = False

            async def serve(self):
                await asyncio.sleep(100)

        class _FakeManager:
            async def start_run(self, *a, **kw):
                return "rid"

            def get_handle(self, run_id):
                return None  # 无 handle → run_task = None → 立即进 autoexit wait

            async def shutdown(self):
                pass

        import sys
        import types

        fake_uvicorn_mod = types.ModuleType("uvicorn")
        fake_uvicorn_mod.Config = lambda *a, **kw: None
        fake_uvicorn_mod.Server = _FakeServer
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn_mod)

        fake_web_mod = types.ModuleType("orca.iface.web")
        fake_web_mod.RunManager = lambda **kw: _FakeManager()
        monkeypatch.setitem(sys.modules, "orca.iface.web", fake_web_mod)

        fake_server_mod = types.ModuleType("orca.iface.web.server")
        fake_server_mod.create_app = lambda manager: types.SimpleNamespace(
            state=types.SimpleNamespace(web_server=types.SimpleNamespace(
                last_ws_activity_at=time.monotonic(),
            ))
        )
        monkeypatch.setitem(sys.modules, "orca.iface.web.server", fake_server_mod)

        # _wait_ws_autoexit 抛 CancelledError 模拟 Ctrl-C。
        async def _raise_cancel(web_server, n):
            raise asyncio.CancelledError()

        monkeypatch.setattr(
            "orca.iface.cli.commands._wait_server_started",
            lambda server, timeout: asyncio.sleep(0),
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._wait_ws_autoexit", _raise_cancel,
        )

        async def _run():
            return await _serve_and_run_inprocess(
                RunConfig(yaml_path=wf_path),
                wf=None,
                bind_host="127.0.0.1",
                display_host="127.0.0.1",
                port=12345,
                stay=False,
            )

        # CancelledError 被内部捕获，函数应正常返回（exit_code = default failed = 1）
        rc = asyncio.run(_run())
        assert rc == EXIT_RUN_FAILED  # 无 handle → exit_code 默认 failed


# ── _spawn_background_serve / _wait_for_health / _attach_and_get_error ──────


class TestSpawnAndAttachHelpers:
    """``_spawn_background_serve`` / ``_wait_for_health`` / ``_attach_and_get_error``。"""

    def test_spawn_background_serve_returns_false_when_orca_missing(self, monkeypatch):
        """``orca`` 不在 PATH → FileNotFoundError 捕获 → 返回 False（B3 闭环）。"""
        import subprocess
        from unittest.mock import patch

        from orca.iface.cli.commands import _spawn_background_serve

        def _raise(*a, **kw):
            raise FileNotFoundError("[Errno 2] No such file: orca")

        with patch.object(subprocess, "Popen", _raise):
            assert _spawn_background_serve("127.0.0.1", 7428) is False

    def test_wait_for_health_returns_false_on_timeout(self, monkeypatch):
        """超时无 orca ready → False（M6 闭环）。"""
        from orca.iface.cli.commands import _wait_for_health

        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=1.0: None,
        )
        # 缩短 timeout 让测试快跑。
        assert _wait_for_health("127.0.0.1", 12345, timeout=0.3) is False

    def test_attach_and_get_error_returns_code_on_http_exception(self, monkeypatch):
        """httpx 抛异常 → 返回 EXIT_RUN_FAILED（M7 闭环）。"""
        import httpx
        from unittest.mock import patch

        from orca.iface.cli.commands import _attach_and_get_error

        def _raise(*a, **kw):
            raise httpx.ConnectError("connection refused")

        with patch.object(httpx, "post", _raise):
            rc = _attach_and_get_error("127.0.0.1", 7428, "/tape.jsonl", "rid")
        assert rc == EXIT_RUN_FAILED

    def test_attach_and_get_error_returns_none_on_success(self, monkeypatch):
        """HTTP 200 → None（成功）。"""
        import httpx
        from unittest.mock import patch, MagicMock

        from orca.iface.cli.commands import _attach_and_get_error

        fake_resp = MagicMock()
        fake_resp.status_code = 200

        with patch.object(httpx, "post", lambda *a, **kw: fake_resp):
            rc = _attach_and_get_error("127.0.0.1", 7428, "/tape.jsonl", "rid")
        assert rc is None

    def test_open_browser_failure_prints_url(self, monkeypatch, capsys):
        """webbrowser.open 失败 → 打印 URL 不抛（SPEC §7，m1 闭环）。"""
        from unittest.mock import patch
        import webbrowser

        from orca.iface.cli.commands import _open_browser_or_print

        with patch.object(webbrowser, "open", lambda url: False):
            _open_browser_or_print("http://127.0.0.1:7428/runs/x")
        out = capsys.readouterr().out
        assert "http://127.0.0.1:7428/runs/x" in out


# ── WS 驱动 auto-exit（AC5 负向）────────────────────────────────────────────


class TestWSAutoexit:
    """WS 活动计时器：``_wait_ws_autoexit`` 单元测试（SPEC §0 D4 / §4 step4 / §8 AC5 负向）。"""

    def test_autoexit_returns_when_window_elapsed(self):
        """无活跃 WS + window 过 → 返回（允许 caller 退出）。"""
        from orca.iface.cli.commands import _wait_ws_autoexit

        class _FakeWebServer:
            def __init__(self, offset: float):
                # last_ws_activity_at 设为 offset 秒前；无活跃 WS。
                self.last_ws_activity_at = time.monotonic() - offset
                self.active_ws_count = 0

        # window=0.05s + last 1s 前 → 立即满足条件返回。
        async def _go():
            await _wait_ws_autoexit(_FakeWebServer(offset=1.0), 0.05)

        start = time.monotonic()
        asyncio.run(_go())
        assert time.monotonic() - start < 1.0  # 远小于 window

    def test_autoexit_blocks_when_within_window(self):
        """窗口内（last 刚 touch，无活跃 WS）→ 至少等 window 才返回。"""
        from orca.iface.cli.commands import _wait_ws_autoexit

        class _Fresh:
            def __init__(self):
                self.last_ws_activity_at = time.monotonic()
                self.active_ws_count = 0

        async def _go():
            await _wait_ws_autoexit(_Fresh(), 0.4)

        start = time.monotonic()
        asyncio.run(_go())
        elapsed = time.monotonic() - start
        assert elapsed >= 0.3  # 至少等了 ~window（不立即退）

    def test_autoexit_blocks_while_ws_active(self):
        """SPEC §8 AC5 负向「有活跃 WS 不退」：``active_ws_count > 0`` → 永不退。

        even if last_ws_activity_at 远在过去（修复旧版只 touch 不计数的缺陷）。
        """
        from orca.iface.cli.commands import _wait_ws_autoexit

        class _ActiveWS:
            def __init__(self):
                # 旧 bug 复现条件：last 远在过去（窗口显然已过），但有活跃 WS 连接。
                self.last_ws_activity_at = time.monotonic() - 100.0
                self.active_ws_count = 1

        async def _go():
            await asyncio.wait_for(
                _wait_ws_autoexit(_ActiveWS(), 0.05), timeout=1.0,
            )

        # 活跃 WS → 永不自然返回 → asyncio.wait_for 超时抛 TimeoutError。
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(_go())

    def test_autoexit_fires_after_ws_disconnect(self):
        """SPEC §8 AC5：活跃 WS 断开后 + window 过 → 退（负向 AC 的正面：可退条件）。"""
        from orca.iface.cli.commands import _wait_ws_autoexit

        # 模拟「WS 刚断开」：count 回到 0，last_ws_activity_at 在 window 之前。
        class _JustDisconnected:
            def __init__(self):
                self.last_ws_activity_at = time.monotonic() - 1.0
                self.active_ws_count = 0

        async def _go():
            await _wait_ws_autoexit(_JustDisconnected(), 0.05)

        start = time.monotonic()
        asyncio.run(_go())
        assert time.monotonic() - start < 1.0  # 立即满足条件返回


# ── orca open（AC7）─────────────────────────────────────────────────────────


class TestOrcaOpen:
    """``orca open <id>``：probe / spawn / attach / browser open（SPEC §5 + §8 AC7）。"""

    def _write_tape(self, runs_dir: Path, run_id: str) -> Path:
        """写一个最小合法 tape（workflow_started + workflow_completed）。"""
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{run_id}.jsonl"
        path.write_text(
            '{"seq":1,"type":"workflow_started","timestamp":1.0,"data":'
            '{"run_id":"RID","workflow_name":"wf","inputs":{},'
            '"topology":{"a":{"to":["$end"]}}}}\n'
            '{"seq":2,"type":"workflow_completed","timestamp":2.0,"data":{}}\n',
            encoding="utf-8",
        )
        return path

    def test_open_missing_tape_exits_two(self, tmp_path, monkeypatch):
        """tape 不存在 → exit 2（fail loud）。"""
        monkeypatch.chdir(tmp_path)
        rc = runner.invoke(app, ["open", "no-such-run"]).exit_code
        assert rc == EXIT_ARG_OR_VALIDATE

    def test_open_reuses_existing_server(self, tmp_path, monkeypatch):
        """既有 orca server → 不起后台 serve；attach + browser open。"""
        runs_dir = tmp_path / "runs"
        self._write_tape(runs_dir, "run-abc")
        monkeypatch.chdir(tmp_path)

        calls = {"spawn": 0, "attach": 0, "browser_url": None}

        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: {"app": "orca", "version": "x", "pid": 1},
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._spawn_background_serve",
            lambda host, port: calls.__setitem__("spawn", calls["spawn"] + 1),
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._attach_and_get_error",
            lambda host, port, tape, run_id: calls.__setitem__(
                "attach", calls["attach"] + 1,
            ) or None,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._open_browser_or_print",
            lambda url: calls.__setitem__("browser_url", url),
        )
        rc = runner.invoke(app, ["open", "run-abc"]).exit_code
        assert rc == EXIT_OK
        assert calls["spawn"] == 0  # 复用 → 不起后台 serve
        assert calls["attach"] == 1
        assert calls["browser_url"] is not None
        assert "run-abc" in calls["browser_url"]

    def test_open_spawns_serve_when_no_existing(self, tmp_path, monkeypatch):
        """无既有 server → 起 background ``orca serve`` + 等健康。"""
        runs_dir = tmp_path / "runs"
        self._write_tape(runs_dir, "run-spawn")
        monkeypatch.chdir(tmp_path)

        calls = {"spawn": 0, "wait": 0}

        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: None,  # 无既有
        )
        # 默认端口"被占"（非 orca）→ 走 _find_free_port 分支。
        monkeypatch.setattr(
            "orca.iface.cli.commands._is_port_free",
            lambda host, port: False,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._find_free_port",
            lambda preferred=None, bind_host="127.0.0.1": 12345,
        )

        def _spawn(host, port):
            calls["spawn"] = port
            return True  # success（_spawn_background_serve 现在返回 bool）

        def _wait(host, port, *, timeout):
            calls["wait"] = timeout
            return True

        monkeypatch.setattr(
            "orca.iface.cli.commands._spawn_background_serve", _spawn,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._wait_for_health", _wait,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._attach_and_get_error",
            lambda host, port, tape, run_id: None,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._open_browser_or_print", lambda url: None,
        )
        rc = runner.invoke(app, ["open", "run-spawn"]).exit_code
        assert rc == EXIT_OK
        assert calls["spawn"] == 12345  # spawn 收到 find_free_port 选的端口
        assert calls["wait"] == 10.0

    def test_open_attach_failure_exits_one(self, tmp_path, monkeypatch):
        """attach 失败（4xx）→ exit 1（fail loud）。"""
        runs_dir = tmp_path / "runs"
        self._write_tape(runs_dir, "run-fail")
        monkeypatch.chdir(tmp_path)

        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: {"app": "orca", "version": "x", "pid": 1},
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._attach_and_get_error",
            lambda host, port, tape, run_id: EXIT_RUN_FAILED,  # 模拟 attach 失败
        )
        rc = runner.invoke(app, ["open", "run-fail"]).exit_code
        assert rc == EXIT_RUN_FAILED

    def test_open_explicit_port_non_orca_occupied_exits_two(
        self, tmp_path, monkeypatch,
    ):
        """``--port`` 显式 + 非 orca 占 → exit 2。"""
        runs_dir = tmp_path / "runs"
        self._write_tape(runs_dir, "run-x")
        monkeypatch.chdir(tmp_path)

        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: None,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._is_port_free", lambda host, port: False,
        )
        rc = runner.invoke(
            app, ["open", "run-x", "--port", "9999"],
        ).exit_code
        assert rc == EXIT_ARG_OR_VALIDATE

    def test_open_with_explicit_tape_flag(self, tmp_path, monkeypatch):
        """``--tape <path>`` 显式指定 tape 路径（不依赖 runs/<id>.jsonl 约定）。"""
        tape_path = tmp_path / "custom" / "my-run.jsonl"
        self._write_tape(tmp_path / "custom", "my-run")
        monkeypatch.chdir(tmp_path)

        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: {"app": "orca", "version": "x", "pid": 1},
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._attach_and_get_error",
            lambda host, port, tape, run_id: None,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._open_browser_or_print", lambda url: None,
        )
        rc = runner.invoke(
            app, ["open", "my-run", "--tape", str(tape_path)],
        ).exit_code
        assert rc == EXIT_OK


# ── /orca open slash command（signature-contract，AC7）─────────────────────
#
# **v5 §8 step 4**：``/orca open <run_id>`` 的 plugin 侧 marker 派发（transform →
# spawnTopLevelCli(["open", rid])）随 transform 整删下线——入口统一切到 orca skill。
# 旧的 ``TestOrcaOpenSlashContract``（plugin-side marker dispatch 守门）已无承载对象，
# 整删。``orca open`` CLI 命令本身仍在（见 ``test_web_open_command_*`` + ``TestIronLaws``），
# 由 CLI 行为契约覆盖（非 plugin TS 源码 grep）。


# ── Iron laws（铁律守门，SPEC §1 / §8 AC12-13）──────────────────────────────


class TestIronLaws:
    """Web attach + Step2 后铁律不破：单 store / 单 registry / TUI 未删 / attacher 只读。"""

    def test_tui_code_still_present(self):
        """``--tui`` opt-in 保留（AC6）：_run_workflow 函数仍在。"""
        from orca.iface.cli import commands

        assert hasattr(commands, "_run_workflow")
        assert callable(commands._run_workflow)

    def test_single_runs_registry_unchanged(self):
        """``_runs: dict[str, RunView]`` 单 registry（grep AC12）。"""
        import re
        from pathlib import Path

        rm_path = (
            Path(__file__).resolve().parents[3]
            / "orca" / "iface" / "web" / "run_manager.py"
        )
        text = rm_path.read_text(encoding="utf-8")
        # 单 _runs dict 声明（无新增并行 dict）。
        declarations = re.findall(r"self\._\w*runs\w*\s*:\s*dict", text)
        assert len(declarations) <= 1, f"发现多个 runs dict 声明：{declarations}"

    def test_attacher_no_tape_resume_true(self):
        """attacher 路径无 ``Tape(resume=True)`` 实际调用（AC13）。

        文档/docstring 里会出现反引号包裹的 ``Tape(resume=True)``（"为什么不用"的说明），
        本测试只断言**代码调用**（非 docstring）：``Tape(...resume=True...)`` 作为实际
        构造器调用，而非双反引号包裹的文档字面。
        """
        import re
        from pathlib import Path

        tr_path = (
            Path(__file__).resolve().parents[3]
            / "orca" / "events" / "tape_reader.py"
        )
        text = tr_path.read_text(encoding="utf-8")
        # 排除双反引号包裹的 docstring 字面（``...``），只查实际代码调用。
        # ``Tape(...resume=True...)`` 不在反引号内的形态。
        code_text = re.sub(r"``[^`]*``", "", text)
        # 注释行也排除（# 开头的）。
        code_text = "\n".join(
            ln for ln in code_text.splitlines()
            if not ln.lstrip().startswith("#")
        )
        # 实际调用形态：Tape(... , resume=True ...)
        assert not re.search(r"Tape\([^)]*resume\s*=\s*True", code_text), (
            "tape_reader 实际调用 Tape(resume=True) 违反 read-only 铁律 6"
        )

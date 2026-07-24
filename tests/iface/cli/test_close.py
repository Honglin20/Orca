"""test_close.py —— ``tars close`` 命令单测（SPEC tars-close §4-5 / AC1-8）。

覆盖意图（非仅行为）：
  - **AC1 endpoint 路径必走**：默认 close 经 ``POST /api/shutdown`` 关闭，``_kill_pid_on_port``
    未被调用 + lifespan 完整跑完。
  - **AC2 ``--all`` 指纹隔离**：双 ``ORCA_HOME`` 起 2 server（指纹不同）→ 只关自己，别用户存活。
    + 非默认端口发现（``ORCA_WEB_PORT=9999``）。
  - **AC3 无 server → exit 0**：「no orca server found」。
  - **AC7 found-but-failed**：endpoint 返 5xx → exit 1 + 端口入 stderr。
  - **B4 re-probe**：PID 兜底返 False + re-probe 空 → ``"none"``（并发 winner 语义）。
  - **守门**：``tars close`` 在 ``tars --help``。

mock 边界（计划 §5）：``_kill_pid_on_port`` / ``_local_listening_ports`` /
``_probe_orca_server`` / ``httpx.post`` 一律 mock，不依赖真起 server（平台无关，win32 绿）。
仅 ``test_close_lifespan_completes_via_endpoint`` 起真 in-process uvicorn（验 lifespan）。
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from orca.iface.cli.commands import (
    EXIT_OK,
    EXIT_RUN_FAILED,
    app,
)


runner = CliRunner()


# ── 共享 helpers / fixtures ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_orca_home(tmp_path: Path, monkeypatch):
    """隔离 ``ORCA_HOME`` —— ``~/.orca/.orca-web.json`` 不污染真实用户全局登记。

    同时让 ``_runs_dir_fp`` 拿到确定性指纹（基于此 tmp 路径）。
    """
    home = tmp_path / "orca-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("ORCA_HOME", str(home))
    yield home


def _my_fp() -> str:
    """当前隔离 ``ORCA_HOME`` 下的指纹（与 ``_runs_dir_fp`` 同源）。"""
    from orca.iface.cli.commands import _runs_dir_fp
    return _runs_dir_fp(Path("runs"))


def _my_fp_health() -> dict:
    """返回本项目指纹的 health body。"""
    return {"app": "orca", "version": "x", "pid": 1, "orca_home_fp": _my_fp(), "runs_dir_fp": _my_fp()}


def _foreign_fp_health() -> dict:
    """返回别用户指纹的 health body（确保与 my_fp 不同）。"""
    fp = "deadbeefdead"  # 12 字符；几乎不可能与 sha1 hash 碰撞
    assert fp != _my_fp(), "测试前提：foreign fp 必须与 my_fp 不同"
    return {"app": "orca", "version": "x", "pid": 1, "orca_home_fp": fp, "runs_dir_fp": fp}


class _FakeResponse:
    """``httpx.post`` 返回的假 Response（status_code + json）。"""

    def __init__(self, status_code: int, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text or ""

    def json(self) -> dict:
        return self._body


# ── 守门：``tars close`` 在 ``tars --help`` ─────────────────────────────────────


class TestCloseCommandGate:
    """``tars close`` 命令注册 + help 可见。"""

    def test_close_in_tars_help(self):
        """``tars --help`` 输出含 ``close`` 命令（用户可发现）。"""
        r = runner.invoke(app, ["--help"])
        assert r.exit_code == EXIT_OK
        assert "close" in r.output, "`tars close` 未出现在 tars --help"

    def test_close_help_shows_options(self):
        """``tars close --help`` 含 ``--all`` / ``--port`` / ``--host``。"""
        r = runner.invoke(app, ["close", "--help"])
        assert r.exit_code == EXIT_OK
        assert "--all" in r.output
        assert "--port" in r.output
        assert "--host" in r.output


# ── AC1：默认 endpoint 路径必走 + lifespan 完整 ──────────────────────────────────


class TestCloseDefaultEndpointPath:
    """默认 close（非 --all）经 ``/api/shutdown`` 关闭，PID 兜底不动（AC1 (a)）。"""

    def test_close_default_uses_endpoint_and_skips_pid(self, monkeypatch):
        """默认候选 = [7428]（registry 空）；probe my → POST 200 → re-probe None → ``endpoint``。

        断言 ``_kill_pid_on_port`` **未被调用**（AC1：endpoint 路径必走，PID 兜底是遗留老 server
        的兜底分支，不应在健康 server 上触发）。
        """
        # probe 序列：第 1 次（_shutdown_server_on_port 入口过滤）→ my；后续 re-probe → None。
        probe_call_count = {"n": 0}

        def _probe(host, port, timeout=0.5):
            probe_call_count["n"] += 1
            # 第 1 次是 _shutdown_server_on_port 入口 → my；后续 re-probe → None（已关）
            return _my_fp_health() if probe_call_count["n"] == 1 else None

        kill_calls = {"n": 0}

        def _kill(port):
            kill_calls["n"] += 1
            return False

        post_calls = {"n": 0, "urls": []}

        def _post(url, **kw):
            post_calls["n"] += 1
            post_calls["urls"].append(url)
            return _FakeResponse(200, body={"shutting_down": True, "pid": 1})

        monkeypatch.setattr("orca.iface.cli.commands._probe_orca_server", _probe)
        monkeypatch.setattr("orca.iface.cli.commands._kill_pid_on_port", _kill)
        import httpx
        monkeypatch.setattr(httpx, "post", _post)

        # 避免 _safe_lookup_registry_port 调真 filesystem（直接 None）
        monkeypatch.setattr(
            "orca.iface.cli.commands._safe_lookup_registry_port",
            lambda *a, **kw: None,
        )

        result = runner.invoke(app, ["close"])
        assert result.exit_code == EXIT_OK, result.output
        assert "closed 1 orca server" in result.output
        assert "7428" in result.output
        assert "endpoint" in result.output
        # 关键断言（AC1）：endpoint 路径跑通，PID 兜底未触发
        assert kill_calls["n"] == 0, f"AC1：_kill_pid_on_port 不应被调用（实际 {kill_calls['n']} 次）"
        assert post_calls["n"] == 1
        assert "/api/shutdown" in post_calls["urls"][0]

    def test_close_lifespan_completes_via_endpoint(self, tmp_path):
        """AC1 (b)：真 in-process uvicorn → POST /api/shutdown → server task 退出 +
        manager.shutdown 跑完（lifespan 完整；tape flush 等清理资源）。

        非 mockery：这是唯一真起 server 的测试，验证 ``should_exit=True`` → uvicorn lifespan
        shutdown → ``manager.shutdown()`` 的真实链路。其它测试一律 mock 此路径。
        """
        import uvicorn
        from orca.iface.web.run_manager import RunManager
        from orca.iface.web.server import create_app

        # 选空闲端口
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        runs_dir = tmp_path / "runs"
        runs_dir.mkdir(parents=True)
        manager = RunManager(max_concurrent=2, runs_dir=runs_dir)

        # 跟踪 manager.shutdown 是否被 lifespan 调用
        original_shutdown = manager.shutdown
        shutdown_called = threading.Event()

        async def _tracked_shutdown():
            await original_shutdown()
            shutdown_called.set()

        manager.shutdown = _tracked_shutdown

        app = create_app(manager)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        # B1：wire uvicorn 句柄（与 run_server / _serve_and_run_inprocess 同）
        app.state.uvicorn_server = server

        loop = asyncio.new_event_loop()

        def _serve():
            loop.run_until_complete(server.serve())

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()

        # 等端口 accept ready
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        try:
            import httpx
            r = httpx.post(f"http://127.0.0.1:{port}/api/shutdown", timeout=3.0)
            assert r.status_code == 200, f"shutdown 端点应返 200（实际 {r.status_code}）"
            # 等 lifespan shutdown 跑完（server thread 退出 + manager.shutdown 调用）
            thread.join(timeout=5.0)
            assert not thread.is_alive(), (
                "server 线程未退出 → uvicorn lifespan shutdown 未完成（AC1 (b) 违反）"
            )
            assert shutdown_called.is_set(), (
                "manager.shutdown 未被 lifespan 调用 → tape flush 等清理资源可能未跑"
            )
        finally:
            if thread.is_alive():
                server.should_exit = True
                thread.join(timeout=2.0)
            loop.close()

    def test_close_via_endpoint_preserves_terminal_tape(self, tmp_path):
        """AC1 (b) 补强：attach 一个**终态 tape**（末事件 = workflow_completed）→ POST
        /api/shutdown → server lifespan 跑完后，tape 文件末事件**仍是终态**（未被截断 /
        未 corrupt）。

        直接验证计划 §5 AC1「tape 末事件为终态，非 mid-step 截断」子属性 ——
        ``manager.shutdown`` 是机制，本测试给直接证据（tape 内容前后一致）。
        """
        import json
        import uvicorn
        from orca.iface.web.run_manager import RunManager
        from orca.iface.web.server import create_app

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        runs_dir = tmp_path / "runs"
        runs_dir.mkdir(parents=True)
        # 写一个最小终态 tape（workflow_started + workflow_completed）。
        tape_path = runs_dir / "term.jsonl"
        tape_path.write_text(
            '{"seq":1,"type":"workflow_started","timestamp":1.0,"data":'
            '{"run_id":"term-run","workflow_name":"wf","inputs":{},'
            '"topology":{"a":{"to":["$end"]}}}}\n'
            '{"seq":2,"type":"workflow_completed","timestamp":2.0,"data":{}}\n',
            encoding="utf-8",
        )
        # 记录 shutdown 前 tape 内容（断言用）
        pre_shutdown_lines = tape_path.read_text(encoding="utf-8").splitlines()
        assert pre_shutdown_lines[-1].strip().endswith("}")  # 完整行（非 partial）

        manager = RunManager(max_concurrent=2, runs_dir=runs_dir)
        # attach 这个 tape（read-only follow）
        loop = asyncio.new_event_loop()
        run_id = loop.run_until_complete(manager.attach_run(str(tape_path)))
        try:
            app = create_app(manager)
            config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
            server = uvicorn.Server(config)
            app.state.uvicorn_server = server

            def _serve():
                loop.run_until_complete(server.serve())

            thread = threading.Thread(target=_serve, daemon=True)
            thread.start()

            # 等端口 ready
            deadline = time.time() + 5.0
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.05)

            import httpx
            r = httpx.post(f"http://127.0.0.1:{port}/api/shutdown", timeout=3.0)
            assert r.status_code == 200
            thread.join(timeout=5.0)
            assert not thread.is_alive(), "server 线程未退出（lifespan 未跑完）"

            # 直接证据：tape 文件内容与 shutdown 前一致（末事件仍是 workflow_completed）。
            post_lines = tape_path.read_text(encoding="utf-8").splitlines()
            assert post_lines == pre_shutdown_lines, (
                "tape 内容在 shutdown 后发生变化（应为只读 attach，不写）"
            )
            last_event = json.loads(post_lines[-1])
            assert last_event["type"] == "workflow_completed", (
                f"tape 末事件应为 workflow_completed（实际 {last_event['type']}）"
            )
        finally:
            try:
                loop.run_until_complete(manager.shutdown())
            except Exception:  # noqa: BLE001
                pass
            loop.close()


# ── AC3：无 server → exit 0 ───────────────────────────────────────────────────


class TestCloseNoServer:
    """无 orca server → 「no orca server found」+ exit 0（AC3）。"""

    def test_close_no_server_message_and_exit_zero(self, monkeypatch):
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: None,  # 无 server
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._safe_lookup_registry_port",
            lambda *a, **kw: None,
        )
        result = runner.invoke(app, ["close"])
        assert result.exit_code == EXIT_OK, result.output
        assert "no orca server found" in result.output


# ── AC7：found-but-failed → exit 1 ────────────────────────────────────────────


class TestCloseFoundButFailed:
    """找到 orca 但关闭失败（5xx / 网络错）→ exit 1 + 端口入 stderr（AC7）。"""

    def test_close_endpoint_5xx_exits_one_with_port(self, monkeypatch):
        """probe my → POST 返 503 → ``fail``；exit 1 + 端口 7428 在 stderr。"""
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: _my_fp_health(),
        )
        import httpx
        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **kw: _FakeResponse(503, text="internal error"),
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._safe_lookup_registry_port",
            lambda *a, **kw: None,
        )
        result = runner.invoke(app, ["close"])
        assert result.exit_code == EXIT_RUN_FAILED, result.output
        # 端口 7428 应出现在输出（fail loud 指明哪个端口失败）
        assert "7428" in result.output
        assert "failed" in result.output.lower()

    def test_close_endpoint_network_error_exits_one(self, monkeypatch):
        """probe my → httpx.post 抛异常 → ``fail``；exit 1（fail loud）。"""
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: _my_fp_health(),
        )
        import httpx

        def _raise(*a, **kw):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "post", _raise)
        monkeypatch.setattr(
            "orca.iface.cli.commands._safe_lookup_registry_port",
            lambda *a, **kw: None,
        )
        result = runner.invoke(app, ["close"])
        assert result.exit_code == EXIT_RUN_FAILED


# ── B4：PID 兜底返 False → re-probe（none vs fail）──────────────────────────────


class TestClosePidFallbackReprobe:
    """PID 兜底路径：404 → kill；kill False 后必 re-probe（B4 并发语义）。"""

    def test_close_pid_fallback_kill_true_returns_pid(self, monkeypatch):
        """probe my → POST 404（老 server 无端点）→ kill True → ``pid``。"""
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: _my_fp_health(),
        )
        import httpx
        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **kw: _FakeResponse(404),
        )
        kill_calls = {"n": 0}
        monkeypatch.setattr(
            "orca.iface.cli.commands._kill_pid_on_port",
            lambda port: kill_calls.__setitem__("n", kill_calls["n"] + 1) or True,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._safe_lookup_registry_port",
            lambda *a, **kw: None,
        )
        # 避免在 win32 触发 stderr warn（与 B4 无关，测试关注 kill 返 True 路径）
        result = runner.invoke(app, ["close"])
        assert result.exit_code == EXIT_OK, result.output
        assert "closed 1" in result.output
        assert "pid" in result.output
        assert kill_calls["n"] == 1

    def test_close_pid_fallback_kill_false_reprobe_none(self, monkeypatch):
        """B4 核心契约：kill False + re-probe **空** → ``"none"``（不是 ``"fail"``）。

        场景：并发 ``tars close --all`` 的 loser 跑到 PID 兜底时，winner 已关 server，
        loser 的 kill 找不到 PID 返 False，re-probe 端口已空 → ``none``（不算失败）。
        """
        probe_call_count = {"n": 0}

        def _probe(host, port, timeout=0.5):
            probe_call_count["n"] += 1
            # 第 1 次（入口过滤）→ my；第 2 次（B4 re-probe）→ None（已被 winner 关）
            return _my_fp_health() if probe_call_count["n"] == 1 else None

        monkeypatch.setattr("orca.iface.cli.commands._probe_orca_server", _probe)
        import httpx
        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **kw: _FakeResponse(404),
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._kill_pid_on_port",
            lambda port: False,  # 模拟 loser：找不到 PID
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._safe_lookup_registry_port",
            lambda *a, **kw: None,
        )
        result = runner.invoke(app, ["close"])
        # ``none`` 不算失败 → exit 0；汇总「无 server found」（close 把 none 当未关）
        assert result.exit_code == EXIT_OK, result.output
        # re-probe 后端口已空 → 当作「无 server」（closed 列表空 + failed 列表空）
        assert "no orca server found" in result.output

    def test_close_pid_fallback_kill_false_reprobe_still_occupied(self, monkeypatch):
        """B4 反向：kill False + re-probe **仍占** → ``"fail"``（exit 1）。"""
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: _my_fp_health(),  # 入口 + re-probe 都返 my
        )
        import httpx
        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **kw: _FakeResponse(404),
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._kill_pid_on_port",
            lambda port: False,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._safe_lookup_registry_port",
            lambda *a, **kw: None,
        )
        result = runner.invoke(app, ["close"])
        assert result.exit_code == EXIT_RUN_FAILED, result.output
        assert "7428" in result.output
        assert "failed" in result.output.lower()


# ── AC5：registry 不清（B3 简化）── close 不写登记文件 ──────────────────────────


class TestCloseDoesNotTouchRegistry:
    """B3 / AC5 回归守门：``tars close`` 路径**不**调任何 registry writer。

    B3 简化后，close 不清登记（靠 ``_lookup_my_registered_port`` stale 自愈）。本测试
    patch 所有 writer，跑 close，断言 call_count == 0 —— 防止未来回归（如有人重新加
    ``clear_orca_home_registry`` 调用）破坏「无新 writer」契约。
    """

    def test_close_does_not_call_any_registry_writer(self, monkeypatch):
        """close 全路径（默认 + --all）均不调 write_orca_home_registry / write_registry。"""
        from orca.iface.cli import web_registry

        writer_calls = {"n": 0}

        def _record_call(*a, **kw):
            writer_calls["n"] += 1
            # 不真实写盘（仅记调用次数）

        # patch 所有 writer（公开 + unlocked）
        monkeypatch.setattr(web_registry, "write_orca_home_registry", _record_call)
        monkeypatch.setattr(web_registry, "write_registry", _record_call)
        monkeypatch.setattr(
            web_registry, "_write_orca_home_registry_unlocked", _record_call,
        )

        # close 探测无 server（最快路径，触发 _safe_lookup_registry_port 真跑）
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: None,
        )
        # _safe_lookup_registry_port 不 mock —— 让它真跑 _lookup_my_registered_port
        # （读 ~/.orca/.orca-web.json；隔离 ORCA_HOME fixture 保证无文件 → 返 None）

        result = runner.invoke(app, ["close"])
        assert result.exit_code == EXIT_OK, result.output
        assert writer_calls["n"] == 0, (
            f"B3：close 不应调任何 registry writer（实际调 {writer_calls['n']} 次）"
        )

    def test_close_all_does_not_call_any_registry_writer(self, monkeypatch):
        """``--all`` 路径同样不调 writer（即使扫描发现 server）。"""
        from orca.iface.cli import web_registry

        writer_calls = {"n": 0}

        def _record_call(*a, **kw):
            writer_calls["n"] += 1

        monkeypatch.setattr(web_registry, "write_orca_home_registry", _record_call)
        monkeypatch.setattr(web_registry, "write_registry", _record_call)
        monkeypatch.setattr(
            web_registry, "_write_orca_home_registry_unlocked", _record_call,
        )

        monkeypatch.setattr(
            "orca.iface.cli.commands._local_listening_ports",
            lambda: [7428, 9999],
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: None,  # 全无 server
        )

        result = runner.invoke(app, ["close", "--all"])
        assert result.exit_code == EXIT_OK, result.output
        assert writer_calls["n"] == 0, (
            f"B3：close --all 不应调任何 registry writer（实际调 {writer_calls['n']} 次）"
        )


# ── AC2：``--all`` 指纹隔离 + 非默认端口发现 ─────────────────────────────────────


class TestCloseAllFingerprintAndDiscovery:
    """``tars close --all``：指纹匹配才关 + 发现非默认/非登记端口（AC2）。"""

    def test_close_all_isolates_foreign_fingerprint(self, monkeypatch):
        """B2/AC2 核心：双 ORCA_HOME 模拟（指纹不同）→ ``--all`` 只关自己，别用户存活。

        mock 两个端口：7428 = my_fp（关），9999 = foreign_fp（不关）。
        断言 ``httpx.post`` 只在 7428 上调用（9999 被 phase-1 probe 过滤掉）。
        """
        # _local_listening_ports 返两端口
        monkeypatch.setattr(
            "orca.iface.cli.commands._local_listening_ports",
            lambda: [7428, 9999],
        )

        # probe 按端口分流 + 7428 计数（第 1-2 次 my；第 3 次起 None 表示已关）。
        call_count = {"7428": 0, "9999": 0}

        def _probe(host, port, timeout=0.5):
            if port == 7428:
                call_count["7428"] += 1
                # 第 1 次（phase-1 probe）+ 第 2 次（_shutdown_server_on_port 入口）= my
                # 第 3 次起（re-probe loop）= None（已关）
                return _my_fp_health() if call_count["7428"] <= 2 else None
            if port == 9999:
                call_count["9999"] += 1
                return _foreign_fp_health()
            return None

        monkeypatch.setattr("orca.iface.cli.commands._probe_orca_server", _probe)

        # httpx.post 记录调用 URL
        post_urls = []

        def _post(url, **kw):
            post_urls.append(url)
            return _FakeResponse(200, body={"shutting_down": True})

        import httpx
        monkeypatch.setattr(httpx, "post", _post)

        monkeypatch.setattr(
            "orca.iface.cli.commands._safe_lookup_registry_port",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._kill_pid_on_port",
            lambda port: True,  # 不会被调用（endpoint 路径）
        )

        result = runner.invoke(app, ["close", "--all"])
        assert result.exit_code == EXIT_OK, result.output
        # 7428 关闭（my_fp），9999 不动（foreign）
        assert "closed 1" in result.output
        assert "7428" in result.output
        assert "endpoint" in result.output
        # 关键断言：foreign 指纹的 9999 **不进 closed 行**（B2 隔离）
        closed_line = _extract_closed_line(result.output)
        assert "9999" not in closed_line, (
            f"B2：foreign 指纹的 9999 不应在 closed 行（实际：{closed_line}）"
        )

        # 关键断言：httpx.post 在 7428 上调过，在 9999 上**没调过**
        post_on_9999 = [u for u in post_urls if ":9999/" in u]
        post_on_7428 = [u for u in post_urls if ":7428/" in u]
        assert len(post_on_7428) == 1, f"应在 7428 上调一次 POST（实际 {post_on_7428}）"
        assert len(post_on_9999) == 0, (
            f"B2：foreign 指纹的 9999 不应被 POST /api/shutdown（实际 {post_on_9999}）"
        )

    def test_close_all_discovers_non_default_port(self, monkeypatch):
        """AC2：``ORCA_WEB_PORT=9999`` → ``--all`` 发现并关闭 9999（候选集含扫描结果）。

        断言 ``_local_listening_ports`` 被调用（``--all`` 路径）+ 9999 进 closed 列表。
        """
        monkeypatch.setattr(
            "orca.iface.cli.commands._local_listening_ports",
            lambda: [9999, 7428],  # 扫描返回（顺序无关；priority 排序后 7428 在前）
        )

        call_count = {"9999": 0}
        def _probe(host, port, timeout=0.5):
            if port == 9999:
                call_count["9999"] += 1
                # 第 1-2 次 my（phase-1 + shutdown 入口）；第 3 次起 None（已关）
                return _my_fp_health() if call_count["9999"] <= 2 else None
            return None  # 7428 无 server

        monkeypatch.setattr("orca.iface.cli.commands._probe_orca_server", _probe)

        import httpx
        post_urls = []
        def _post(url, **kw):
            post_urls.append(url)
            return _FakeResponse(200, body={"shutting_down": True})
        monkeypatch.setattr(httpx, "post", _post)

        monkeypatch.setattr(
            "orca.iface.cli.commands._safe_lookup_registry_port",
            lambda *a, **kw: None,
        )

        result = runner.invoke(app, ["close", "--all"])
        assert result.exit_code == EXIT_OK, result.output
        assert "closed 1" in result.output
        assert "9999" in result.output
        assert "endpoint" in result.output
        # 9999 上的 POST 被调用
        assert any(":9999/" in u for u in post_urls), f"9999 未被 POST（{post_urls}）"

    def test_close_all_handles_empty_list(self, monkeypatch):
        """``_local_listening_ports`` 返空列表 → 「no orca server found」+ exit 0（容错）。"""
        monkeypatch.setattr(
            "orca.iface.cli.commands._local_listening_ports",
            lambda: [],
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: None,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._safe_lookup_registry_port",
            lambda *a, **kw: None,
        )
        result = runner.invoke(app, ["close", "--all"])
        assert result.exit_code == EXIT_OK, result.output
        assert "no orca server found" in result.output

    def test_close_all_scanner_failure_exits_one(self, monkeypatch):
        """``_local_listening_ports`` raise RuntimeError（fail loud）→ exit 1 + 提示手 kill。"""
        monkeypatch.setattr(
            "orca.iface.cli.commands._local_listening_ports",
            lambda: (_ for _ in ()).throw(
                RuntimeError("无法枚举本地 LISTEN 端口（ss / netstat 都失败）")
            ),
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._safe_lookup_registry_port",
            lambda *a, **kw: None,
        )
        result = runner.invoke(app, ["close", "--all"])
        assert result.exit_code == EXIT_RUN_FAILED, result.output
        assert "无法枚举" in result.output or "pkill" in result.output or "taskkill" in result.output


# ── helpers ───────────────────────────────────────────────────────────────────


def _extract_closed_line(output: str) -> str:
    """从 ``tars close`` 输出中抽取 ``closed N orca server(s): [...]`` 行（若有）。"""
    for line in output.splitlines():
        if line.startswith("closed"):
            return line
    return ""


# ── 附加契约：close 路径选择 helpers 单元测试 ────────────────────────────────────


class TestCloseHelpers:
    """``_shutdown_server_on_port`` 各分支（直接单测，不走 CLI）。"""

    def test_shutdown_returns_none_for_foreign_fingerprint(self, monkeypatch):
        """B2：foreign 指纹 → ``"none"``（不报错、不调 POST）。"""
        from orca.iface.cli.commands import _shutdown_server_on_port
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: _foreign_fp_health(),
        )
        import httpx
        post_calls = []
        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **kw: post_calls.append(1) or _FakeResponse(200),
        )
        result = _shutdown_server_on_port("127.0.0.1", 9999, _my_fp())
        assert result == "none"
        assert len(post_calls) == 0, "foreign server 不应 POST"

    def test_shutdown_returns_none_for_non_orca_port(self, monkeypatch):
        """非 orca 端口（probe None）→ ``"none"``（不调 POST）。"""
        from orca.iface.cli.commands import _shutdown_server_on_port
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: None,
        )
        import httpx
        post_calls = []
        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **kw: post_calls.append(1) or _FakeResponse(200),
        )
        result = _shutdown_server_on_port("127.0.0.1", 12345, _my_fp())
        assert result == "none"
        assert len(post_calls) == 0

    def test_shutdown_returns_endpoint_on_200_then_health_drop(self, monkeypatch):
        """POST 200 + re-probe None → ``"endpoint"``。"""
        from orca.iface.cli.commands import _shutdown_server_on_port
        call_count = {"n": 0}

        def _probe(host, port, timeout=0.5):
            call_count["n"] += 1
            return _my_fp_health() if call_count["n"] == 1 else None

        monkeypatch.setattr("orca.iface.cli.commands._probe_orca_server", _probe)
        import httpx
        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **kw: _FakeResponse(200, body={"shutting_down": True}),
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._kill_pid_on_port",
            lambda port: pytest.fail("endpoint 路径不应调 PID 兜底"),
        )
        result = _shutdown_server_on_port("127.0.0.1", 7428, _my_fp())
        assert result == "endpoint"

    def test_shutdown_returns_fail_when_200_but_health_persists(self, monkeypatch):
        """POST 200 但 health 3s 内仍在 → ``"fail"``（shutdown 信号未生效）。"""
        from orca.iface.cli.commands import _shutdown_server_on_port
        # probe 始终返 my（health 未消失）
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: _my_fp_health(),
        )
        import httpx
        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **kw: _FakeResponse(200, body={"shutting_down": True}),
        )
        # 加速：把 re-probe deadline 缩短（直接 patch time.monotonic 不现实；用 env/默认 3s）
        # 3s 在测试可接受（CI 慢但不 hang）
        start = time.monotonic()
        result = _shutdown_server_on_port("127.0.0.1", 7428, _my_fp())
        elapsed = time.monotonic() - start
        assert result == "fail"
        # 确实等了 ~3s（不是立即返）
        assert elapsed >= 2.5, f"re-probe 应等 ~3s，实际 {elapsed:.1f}s"

    def test_probe_my_orca_ports_async_filters_by_fingerprint(self):
        """``_probe_my_orca_ports_async`` 单元：my_fp 留、foreign 过滤、非 orca 过滤。"""
        from orca.iface.cli.commands import _probe_my_orca_ports_async

        # 注意：``_probe_my_orca_ports_async`` 经 ``asyncio.to_thread(_probe_orca_server, ...)``
        # 调用 sync 函数；故 mock 必须是 **sync**（async side_effect 会返 coroutine 误判过滤）。
        def _probe_port(host, port, timeout=0.5):
            if port == 7428:
                return _my_fp_health()
            if port == 9999:
                return _foreign_fp_health()
            return None

        with patch("orca.iface.cli.commands._probe_orca_server", side_effect=_probe_port):
            result = asyncio.run(_probe_my_orca_ports_async(
                "127.0.0.1", [7428, 9999, 5555], _my_fp(), priority={7428},
            ))
        assert result == [7428], f"应只留 7428（my），实际 {result}"

    def test_probe_my_orca_ports_async_respects_deadline(self):
        """AC8 守门：5s deadline 内完成（200 端口主机不超时）。"""
        from orca.iface.cli.commands import _probe_my_orca_ports_async

        with patch(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: None,  # 都不可达
        ):
            start = time.monotonic()
            result = asyncio.run(_probe_my_orca_ports_async(
                "127.0.0.1", list(range(7430, 7630)),  # 200 端口
                _my_fp(), priority={7428},
            ))
            elapsed = time.monotonic() - start
        assert result == []
        # Semaphore(16) + 200 端口 × 0.5s = ~6.25s 理论上限；deadline 5s 截断
        # 实际经 to_thread 调度，可能略超 5s 但应在 ~6s 内（CI 慢容忍 10s）
        assert elapsed < 10.0, f"200 端口 probe 超 10s（AC8 违反）：{elapsed:.1f}s"


# ── Windows warn 路径（仅 win32 触发；其它平台 skip）────────────────────────────


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows PID 兜底 force-kill warn 仅 win32 触发",
)
class TestWindowsPidFallbackWarn:
    """Windows PID 兜底前置 stderr warn（tape leak 风险显式告知）。"""

    def test_close_pid_fallback_warns_on_windows(self, monkeypatch):
        """POST 404 + win32 → kill 前 stderr warn（B7）。"""
        monkeypatch.setattr(
            "orca.iface.cli.commands._probe_orca_server",
            lambda host, port, timeout=0.5: _my_fp_health(),
        )
        import httpx
        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **kw: _FakeResponse(404),
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._kill_pid_on_port",
            lambda port: True,
        )
        monkeypatch.setattr(
            "orca.iface.cli.commands._safe_lookup_registry_port",
            lambda *a, **kw: None,
        )
        result = runner.invoke(app, ["close"])
        assert result.exit_code == EXIT_OK
        assert "tape 可能未 flush" in result.output
        assert "force-kill" in result.output.lower() or "taskkill" in result.output.lower()

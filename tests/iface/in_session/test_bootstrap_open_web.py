"""test_bootstrap_open_web.py —— bootstrap 默认自动开 web（工作流 B）。

覆盖意图（SPEC web-attach §5a + 工作流 B）：
  - ``_bootstrap_open_web_enabled``：flag > env > 默认**开** 三层解析全分支。
  - bootstrap 真启动路径（带 ``--inputs``）默认 → 调 ``_spawn_open_web``；
    ``--no-open-web`` / ``ORCA_BOOTSTRAP_OPEN_WEB=0`` → 不调。
  - schema-only 路径（不带 ``--inputs``）→ 不调（早退）。
  - ``--format prompt`` 路径 → 同样触发（F1）。
  - **stdout 契约纯净**（H4）：``json.loads`` 成功 + schema ``{run_id,tape,done}`` + 无 web 文本。
  - ``_spawn_open_web`` 失败 soft：bootstrap 仍 exit 0 + 正常 JSON（绝不 fail bootstrap）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.iface.in_session import cli as cli_mod
from orca.iface.in_session.cli import _bootstrap_open_web_enabled, app

AGENT_WF_YAML = """\
name: bs_open_web_wf
description: 1-agent 线性 workflow（bootstrap 自动开 web 测试）。
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
    p.write_text(AGENT_WF_YAML, encoding="utf-8")
    return p


@pytest.fixture
def cwd_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _stub_daemons(monkeypatch: pytest.MonkeyPatch) -> None:
    """stub daemon spawn / env / socket wait，让 bootstrap 不起真子进程（隔离 + 快）。

    ``_spawn_open_web`` 默认**不** stub（被测对象）；个别用例按需 spy / 触发失败。
    ``_WEB_READY_TIMEOUT`` 设为 0.01 跳过信号文件轮询延迟（bootstrap 测试不走真 detach）。
    """
    monkeypatch.setattr(cli_mod, "_spawn_chart_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "_spawn_sidechain_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "_wait_for_sock", lambda *a, **kw: True)
    monkeypatch.setattr(cli_mod, "_write_orca_env", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "_WEB_READY_TIMEOUT", 0.01)


# ── _bootstrap_open_web_enabled 纯函数（flag > env > 默认开）──────────────────


class TestBootstrapOpenWebEnabled:
    def test_flag_true_wins(self):
        assert _bootstrap_open_web_enabled(True) is True

    def test_flag_false_wins(self):
        assert _bootstrap_open_web_enabled(False) is False

    def test_default_on_when_no_flag_no_env(self, monkeypatch):
        monkeypatch.delenv("ORCA_BOOTSTRAP_OPEN_WEB", raising=False)
        assert _bootstrap_open_web_enabled(None) is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "FALSE", "Off"])
    def test_env_disables(self, monkeypatch, val):
        monkeypatch.setenv("ORCA_BOOTSTRAP_OPEN_WEB", val)
        assert _bootstrap_open_web_enabled(None) is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
    def test_env_enables(self, monkeypatch, val):
        monkeypatch.setenv("ORCA_BOOTSTRAP_OPEN_WEB", val)
        assert _bootstrap_open_web_enabled(None) is True

    def test_flag_overrides_env(self, monkeypatch):
        """--open-web 显式 > env=0（flag 优先）。"""
        monkeypatch.setenv("ORCA_BOOTSTRAP_OPEN_WEB", "0")
        assert _bootstrap_open_web_enabled(True) is True


# ── bootstrap 集成：_spawn_open_web 调用条件 + stdout 契约（H4）─────────────────


def test_bootstrap_default_invokes_spawn_open_web(cwd_tmp, wf_path, monkeypatch):
    """默认（无 flag）→ ``_spawn_open_web`` 被调一次，参数是本 run 的 run_id。"""
    _stub_daemons(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(cli_mod, "_spawn_open_web", lambda run_id: calls.append(run_id))
    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    reply = json.loads(result.output.splitlines()[-1])
    assert calls[0] == reply["run_id"]


def test_bootstrap_no_open_web_disables(cwd_tmp, wf_path, monkeypatch):
    """``--no-open-web`` → 不调 ``_spawn_open_web``。"""
    _stub_daemons(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(cli_mod, "_spawn_open_web", lambda run_id: calls.append(run_id))
    runner = CliRunner()
    result = runner.invoke(
        app, ["bootstrap", str(wf_path), "--inputs", "{}", "--no-open-web"],
    )
    assert result.exit_code == 0, result.output
    assert calls == []


def test_bootstrap_env_zero_disables(cwd_tmp, wf_path, monkeypatch):
    """``ORCA_BOOTSTRAP_OPEN_WEB=0`` → 不调 ``_spawn_open_web``。"""
    _stub_daemons(monkeypatch)
    monkeypatch.setenv("ORCA_BOOTSTRAP_OPEN_WEB", "0")
    calls: list[str] = []
    monkeypatch.setattr(cli_mod, "_spawn_open_web", lambda run_id: calls.append(run_id))
    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    assert result.exit_code == 0, result.output
    assert calls == []


def test_bootstrap_schema_only_does_not_open_web(cwd_tmp, wf_path, monkeypatch):
    """不带 ``--inputs``（schema-only 早退）→ 不调 ``_spawn_open_web``。"""
    _stub_daemons(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(cli_mod, "_spawn_open_web", lambda run_id: calls.append(run_id))
    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(wf_path)])  # 无 --inputs
    assert result.exit_code == 0
    assert calls == []


def test_bootstrap_format_prompt_also_triggers(cwd_tmp, wf_path, monkeypatch):
    """``--format prompt`` 路径同样触发自动开 web（F1：command 入口多在交互终端）。"""
    _stub_daemons(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(cli_mod, "_spawn_open_web", lambda run_id: calls.append(run_id))
    runner = CliRunner()
    result = runner.invoke(
        app, ["bootstrap", str(wf_path), "--inputs", "{}", "--format", "prompt"],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1


def test_bootstrap_stdout_contract_clean(cwd_tmp, wf_path, monkeypatch):
    """H4（演进）：自动开 web 后 bootstrap stdout 仍是合法 JSON 契约，且**显式带 web_url**。

    旧版 H4 断言 stdout 不含 ``http://``（detached 子进程的 echo 进日志，不进 stdout）。
    2026-07-22 演进：detached ``orca open`` 的 URL echo 进日志文件、用户终端看不到 → bootstrap
    自身启动当下即算出 URL，显式塞进 (1) JSON ``web_url`` 字段 + (2) stderr ``Orca Web UI`` 行。
    **不进 prompt**：prompt 须与 next idempotent 重发逐字相等（见 test_f1_resume_flow）。

    2026-07-23 演进 2：``_resolve_web_url`` 改为轮询 detached 进程写出的信号文件获取**真实端口**；
    URL resolution 逻辑独立测于 ``TestResolveWebUrlSignal``，本测试聚焦 stdout 契约。

    oracle：(a) ``json.loads`` 成功；(b) schema ``{run_id:str, tape:str, done:bool, web_url:str}``；
    (c) ``web_url`` 指向 ``/runs/<run_id>``；(d) stderr 出 ``Orca Web UI`` 行；(e) 无 ``webbrowser``
    泄漏（bootstrap 自身不开浏览器，开浏览器是 detached ``orca open`` 的职责）。
    """
    _stub_daemons(monkeypatch)
    monkeypatch.setattr(cli_mod, "_spawn_open_web", lambda run_id: None)
    monkeypatch.setattr(
        cli_mod, "_resolve_web_url",
        lambda run_id: f"http://127.0.0.1:7428/runs/{run_id}",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    assert result.exit_code == 0, result.output
    last = result.output.splitlines()[-1]
    body = json.loads(last)  # (a) 合法 JSON
    assert isinstance(body["run_id"], str)   # (b) schema
    assert isinstance(body["tape"], str)
    assert isinstance(body["done"], bool)
    assert isinstance(body["web_url"], str) and body["web_url"].startswith("http://")
    assert f"/runs/{body['run_id']}" in body["web_url"]  # (c) URL 指向本 run
    assert "Orca Web UI" in result.output    # (d) stderr echo（CliRunner 默认 mix_stderr）
    assert "webbrowser" not in last          # (e) bootstrap 自身不开浏览器


def test_bootstrap_no_open_web_omits_web_url(cwd_tmp, wf_path, monkeypatch):
    """``--no-open-web`` → 不算 web_url：JSON 无 ``web_url`` 字段，stderr 无 ``Orca Web UI`` 行。

    反向契约钉住：web_url 的出现严格跟「自动开 web 是否启用」绑定，避免 disabled 路径漏吐
    一个指不到活跃 server 的链接误导用户。
    """
    _stub_daemons(monkeypatch)
    monkeypatch.setattr(cli_mod, "_spawn_open_web", lambda run_id: None)
    runner = CliRunner()
    result = runner.invoke(
        app, ["bootstrap", str(wf_path), "--inputs", "{}", "--no-open-web"],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output.splitlines()[-1])
    assert "web_url" not in body
    assert "Orca Web UI" not in result.output


def test_bootstrap_resolve_web_url_failure_is_soft(cwd_tmp, wf_path, monkeypatch):
    """``_resolve_web_url`` 内部异常（``resolve_web_endpoint`` 抛错）→ soft warn，bootstrap 仍
    exit 0 + JSON 无 ``web_url`` + stderr 无 ``Orca Web UI`` 行。

    钉住 soft-fail 契约：URL 解析是便利层，失败绝不阻断 bootstrap、也不漏吐死链接。
    monkeypatch ``orca.iface.cli.commands.resolve_web_endpoint``（``_resolve_web_url`` 函数内
    lazy import 的真源），走**真实** ``_resolve_web_url`` 的 except 分支（不 mock helper 本身）。

    设 ``_WEB_READY_TIMEOUT = 0.01`` 跳过信号文件轮询 3s 延迟，立即落入静态计算回退分支→抛异常。
    """
    import orca.iface.cli.commands as commands_mod

    _stub_daemons(monkeypatch)
    monkeypatch.setattr(cli_mod, "_spawn_open_web", lambda run_id: None)
    monkeypatch.setattr(cli_mod, "_WEB_READY_TIMEOUT", 0.01)

    def _boom(*a, **kw):
        raise RuntimeError("simulated resolve_web_endpoint failure")

    monkeypatch.setattr(commands_mod, "resolve_web_endpoint", _boom)
    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    assert result.exit_code == 0, result.output  # soft：不 fail bootstrap
    body = json.loads(result.output.splitlines()[-1])
    assert "run_id" in body
    assert "web_url" not in body              # 解析失败 → 不漏吐 URL
    assert "Orca Web UI" not in result.output


def test_bootstrap_spawn_open_web_failure_is_soft(cwd_tmp, wf_path, monkeypatch):
    """``_spawn_open_web`` 内部 OSError → soft warn，bootstrap 仍 exit 0 + 正常 JSON。

    monkeypatch ``subprocess.Popen`` 抛 OSError，走**真实** ``_spawn_open_web`` 的 except 分支
    （不 mock ``_spawn_open_web`` 本身）——验证 soft-fail 契约真生效。
    """
    _stub_daemons(monkeypatch)

    def _boom(*a, **kw):
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(cli_mod.subprocess, "Popen", _boom)
    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    assert result.exit_code == 0, result.output  # soft：不 fail bootstrap
    body = json.loads(result.output.splitlines()[-1])
    assert "run_id" in body


def test_spawn_open_web_uses_module_cmd_and_detached(cwd_tmp, wf_path, monkeypatch):
    """M-1：``_spawn_open_web`` 的 Popen cmd = ``[sys.executable, -m, orca.iface.in_session.cli,
    open, <run_id>]`` + detached（``start_new_session=True`` + ``close_fds=True``）。

    守门：cmd 拼错（如漏 ``open`` / 写成别的子命令）或丢了 detached 标志会被抓。用
    ``sys.executable -m``（非 bare ``orca``）——与 ``_spawn_chart_daemon`` 同模式，免 PATH 依赖。
    """
    import sys as _sys

    _stub_daemons(monkeypatch)
    captured: dict = {}

    def _capture(*a, **kw):
        captured["cmd"] = list(a[0]) if a else None
        captured["kw"] = kw
        return None

    monkeypatch.setattr(cli_mod.subprocess, "Popen", _capture)
    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert captured["cmd"] == [
        _sys.executable, "-m", "orca.iface.in_session.cli", "open", reply["run_id"],
    ]
    assert captured["kw"]["start_new_session"] is True
    assert captured["kw"]["close_fds"] is True


# ── _resolve_web_url 信号文件轮询（2026-07-23）────────────────────────────────


_WEB_READY_FILE = ".orca-web-ready-{run_id}.json"


class TestResolveWebUrlSignal:
    """``_resolve_web_url`` 信号文件轮询行为：优先读 detached 进程写的真实 URL；

    超时回退静态计算；异常软降级。
    """

    def test_returns_signal_url(self, cwd_tmp, monkeypatch):
        """信号文件存在 → 返回其 url 字段，不到 3s 回退。"""
        _stub_daemons(monkeypatch)
        monkeypatch.setattr(cli_mod, "_WEB_READY_TIMEOUT", 0.5)
        rid = "test-returns-signal"
        signal_path = cwd_tmp / "runs" / _WEB_READY_FILE.format(run_id=rid)
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        signal_path.write_text(
            json.dumps({"url": f"http://10.0.0.1:9999/runs/{rid}"}), encoding="utf-8",
        )
        url = cli_mod._resolve_web_url(rid)
        assert url == f"http://10.0.0.1:9999/runs/{rid}"

    def test_falls_back_on_timeout(self, cwd_tmp, monkeypatch):
        """信号文件不存在 → 超时后回退静态计算（默认端口 7428）。"""
        _stub_daemons(monkeypatch)
        monkeypatch.setattr(cli_mod, "_WEB_READY_TIMEOUT", 0.01)
        rid = "test-fallback"
        url = cli_mod._resolve_web_url(rid)
        assert url is not None
        assert f"/runs/{rid}" in url
        assert url.startswith("http://")

    def test_ignores_invalid_signal_file(self, cwd_tmp, monkeypatch):
        """信号文件存在但内容不是有效 JSON → 忽略，继续轮询到超时回退。"""
        _stub_daemons(monkeypatch)
        monkeypatch.setattr(cli_mod, "_WEB_READY_TIMEOUT", 0.01)
        rid = "test-invalid-json"
        signal_path = cwd_tmp / "runs" / _WEB_READY_FILE.format(run_id=rid)
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        signal_path.write_text("not json", encoding="utf-8")
        url = cli_mod._resolve_web_url(rid)
        assert url is not None
        assert url.startswith("http://")

    def test_ignores_signal_without_url_key(self, cwd_tmp, monkeypatch):
        """信号文件为合法 JSON 但缺 url 字段 → 忽略，继续轮询到超时回退。"""
        _stub_daemons(monkeypatch)
        monkeypatch.setattr(cli_mod, "_WEB_READY_TIMEOUT", 0.01)
        rid = "test-no-url-key"
        signal_path = cwd_tmp / "runs" / _WEB_READY_FILE.format(run_id=rid)
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        signal_path.write_text(json.dumps({"other": "value"}), encoding="utf-8")
        url = cli_mod._resolve_web_url(rid)
        assert url is not None
        assert url.startswith("http://")

    def test_returns_none_on_fatal_error(self, monkeypatch):
        """poll 循环内 fatal OSError → 外层 except 捕获，返回 None。"""
        _stub_daemons(monkeypatch)
        monkeypatch.setattr(cli_mod, "_default_rundir", lambda: None)
        url = cli_mod._resolve_web_url("test-fatal")
        assert url is None

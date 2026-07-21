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
    """
    monkeypatch.setattr(cli_mod, "_spawn_chart_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "_spawn_sidechain_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "_wait_for_sock", lambda *a, **kw: True)
    monkeypatch.setattr(cli_mod, "_write_orca_env", lambda *a, **kw: None)


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
    """H4：自动开 web 后 bootstrap stdout 仍是合法 JSON 契约，无 web 文本污染。

    oracle 精确化：(a) ``json.loads`` 成功；(b) schema ``{run_id:str, tape:str, done:bool}``；
    (c) regex 负向断言 stdout 不含 ``http://`` / ``webbrowser`` / ``Orca Web UI``。
    """
    _stub_daemons(monkeypatch)
    monkeypatch.setattr(cli_mod, "_spawn_open_web", lambda run_id: None)
    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    assert result.exit_code == 0, result.output
    last = result.output.splitlines()[-1]
    body = json.loads(last)  # (a) 合法 JSON
    assert isinstance(body["run_id"], str)   # (b) schema
    assert isinstance(body["tape"], str)
    assert isinstance(body["done"], bool)
    # (c) 无 web 文本污染（detached 子进程 stdio 重定向到日志，不进 bootstrap stdout）
    assert "http://" not in last
    assert "webbrowser" not in last
    assert "Orca Web UI" not in last


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

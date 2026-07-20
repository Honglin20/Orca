"""tests/iface/in_session/test_sidechain_cmds.py —— ``orca sidechain`` 子命令组测试。

覆盖 ``orca sidechain family`` 的 set / show / unset + 合法值校验 + ``--scope project|user``。
仿 ``test_in_session_v8.py`` 的 CliRunner + ``Path.home``/chdir 隔离模式（不污染开发机 config）。

隔离设计：home 与 cwd **分离**（home=tmp_path/home，cwd=tmp_path），使 project 级
（``<cwd>/.orca/config.json``）与 user 级（``~/.orca/config.json``）写到不同文件，可分别断言。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.iface.in_session.cli import app

runner = CliRunner()


@pytest.fixture
def iso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离 home + cwd（二者分离）；返 cwd（=tmp_path），home=tmp_path/home。"""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _read_cfg(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


PROJECT_CFG = Path(".orca") / "config.json"
USER_CFG = Path("home") / ".orca" / "config.json"


def test_family_set_writes_project_config(iso: Path) -> None:
    """``family cac`` → 写 <cwd>/.orca/config.json 的 sidechain.family=cac；回打生效值。"""
    result = runner.invoke(app, ["sidechain", "family", "cac"])
    assert result.exit_code == 0, result.output
    cfg = _read_cfg(iso / PROJECT_CFG)
    assert cfg.get("sidechain", {}).get("family") == "cac"
    assert "sidechain.family=cac" in result.output


def test_family_set_scope_user_writes_user_config(iso: Path) -> None:
    """``--scope user`` → 写 ~/.orca/config.json；project 级不被创建。"""
    result = runner.invoke(app, ["sidechain", "family", "cac", "--scope", "user"])
    assert result.exit_code == 0, result.output
    assert _read_cfg(iso / USER_CFG).get("sidechain", {}).get("family") == "cac"
    assert not (iso / PROJECT_CFG).exists()


def test_family_show_no_env(iso: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``family``（无参、无 CC/opencode env）→ 回显 family + 提示无法 resolve。"""
    # 显式清 env：开发机/Claude Code 环境可能注入 CLAUDE_CODE_SESSION_ID，使 show 走 CC 分支。
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    runner.invoke(app, ["sidechain", "family", "cac"])
    result = runner.invoke(app, ["sidechain", "family"])
    assert result.exit_code == 0, result.output
    assert "family=cac" in result.output
    assert "未检测到 CC/opencode env" in result.output


def test_family_show_with_env_resolves_root(iso: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``family``（有 CLAUDE_CODE_SESSION_ID + 已设 cac）→ 回显 source=config + resolved_root。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "host-xyz")
    runner.invoke(app, ["sidechain", "family", "cac"])
    result = runner.invoke(app, ["sidechain", "family"])
    assert result.exit_code == 0, result.output
    assert "family=cac" in result.output
    assert "source=config" in result.output
    assert "resolved_root=" in result.output


def test_family_unset_clears(iso: Path) -> None:
    """``family --unset`` → 清除 sidechain.family（回探测）。"""
    runner.invoke(app, ["sidechain", "family", "cac"])
    result = runner.invoke(app, ["sidechain", "family", "--unset"])
    assert result.exit_code == 0, result.output
    cfg = _read_cfg(iso / PROJECT_CFG)
    assert "family" not in cfg.get("sidechain", {})
    # 空 sidechain dict 应被清理（不留 {"sidechain": {}} 残留）。
    assert "sidechain" not in cfg
    assert "清除" in result.output


def test_family_unset_idempotent_when_absent(iso: Path) -> None:
    """``--unset`` 无 family 时不报错（已是探测模式）。"""
    result = runner.invoke(app, ["sidechain", "family", "--unset"])
    assert result.exit_code == 0, result.output
    assert "探测模式" in result.output


def test_family_invalid_value_exits_2(iso: Path) -> None:
    """非法 family 值 → exit 2 + 错误信息（fail loud）。"""
    result = runner.invoke(app, ["sidechain", "family", "bogus"])
    assert result.exit_code == 2
    assert "bogus" in result.output


def test_family_invalid_scope_exits_2(iso: Path) -> None:
    """非法 --scope → exit 2。"""
    result = runner.invoke(app, ["sidechain", "family", "cac", "--scope", "foo"])
    assert result.exit_code == 2
    assert "foo" in result.output

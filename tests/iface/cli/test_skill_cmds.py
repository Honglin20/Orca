"""test_skill_cmds.py —— ``orca skill install`` 子命令 + ``install_targets`` 纯函数单测。

覆盖：
  - ``install_targets``：claude / opencode / all 三态 + ``OPENCODE_CONFIG_DIR`` 覆盖 + 未知 target 抛错
  - ``skill install``：默认 all 两边都装、``--target claude`` 只装 CC、幂等重跑、fail loud（copytree
    失败 → exit 1 + stderr 报路径）
  - monkeypatch ``Path.home`` 到 tmp_path，不碰真实 ``~/.claude`` / ``~/.config/opencode``
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.iface.cli import skill_cmds
from orca.iface.cli.commands import app

runner = CliRunner()


# ── install_targets 纯函数 ────────────────────────────────────────────────────


def test_targets_all(tmp_path: Path):
    targets = skill_cmds.install_targets("all", home=tmp_path)
    labels = {label for label, _ in targets}
    assert labels == {"claude", "opencode"}
    for _, dst in targets:
        assert dst.name == skill_cmds.SKILL_NAME


def test_targets_claude_only(tmp_path: Path):
    targets = skill_cmds.install_targets("claude", home=tmp_path)
    assert [label for label, _ in targets] == ["claude"]
    assert targets[0][1] == tmp_path / ".claude" / "skills" / skill_cmds.SKILL_NAME


def test_targets_opencode_only(tmp_path: Path):
    targets = skill_cmds.install_targets("opencode", home=tmp_path)
    assert [label for label, _ in targets] == ["opencode"]
    assert targets[0][1] == tmp_path / ".config" / "opencode" / "skills" / skill_cmds.SKILL_NAME


def test_targets_opencode_config_dir_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    custom = tmp_path / "custom-oc-config"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(custom))
    targets = skill_cmds.install_targets("opencode", home=tmp_path)
    assert targets[0][1] == custom / "skills" / skill_cmds.SKILL_NAME


def test_targets_unknown_raises(tmp_path: Path):
    import typer

    with pytest.raises(typer.BadParameter):
        skill_cmds.install_targets("bogus", home=tmp_path)


# ── skill install（CliRunner + monkeypatch home）──────────────────────────────


@pytest.fixture
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把 ``Path.home`` 指到 tmp_path，隔离 ``~/.claude`` / ``~/.config/opencode``。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    return tmp_path


def _skill_file(dst_root: Path) -> Path:
    return dst_root / ".claude" / "skills" / skill_cmds.SKILL_NAME / "SKILL.md"


def test_install_both(_isolated_home: Path):
    result = runner.invoke(app, ["skill", "install"])
    assert result.exit_code == 0, result.output
    home = _isolated_home
    # CC
    assert (home / ".claude" / "skills" / skill_cmds.SKILL_NAME / "SKILL.md").is_file()
    # opencode
    assert (home / ".config" / "opencode" / "skills" / skill_cmds.SKILL_NAME / "SKILL.md").is_file()
    # reference + examples 跟着 copy
    skill_dir = home / ".claude" / "skills" / skill_cmds.SKILL_NAME
    assert (skill_dir / "reference" / "orca-workflow-contract.md").is_file()
    assert any((skill_dir / "examples").glob("*.yaml"))
    # 🔴 公平性：benchmark/（评测答案）绝不被装到用户 skill 目录
    assert not (skill_dir / "benchmark").exists(), "install 不应拷 benchmark/（会泄露评测答案）"


def test_install_target_claude_only(_isolated_home: Path):
    result = runner.invoke(app, ["skill", "install", "--target", "claude"])
    assert result.exit_code == 0, result.output
    home = _isolated_home
    assert _skill_file(home).is_file()  # CC 装了
    # opencode 没装
    assert not (home / ".config" / "opencode" / "skills" / skill_cmds.SKILL_NAME).exists()


def test_install_idempotent(_isolated_home: Path):
    home = _isolated_home
    r1 = runner.invoke(app, ["skill", "install"])
    assert r1.exit_code == 0, r1.output
    skill_md = _skill_file(home)
    first = skill_md.read_text()
    # 第二次：dirs_exist_ok=True，覆盖不报错
    r2 = runner.invoke(app, ["skill", "install"])
    assert r2.exit_code == 0, r2.output
    assert skill_md.read_text() == first


def test_install_fail_loud(_isolated_home: Path, monkeypatch: pytest.MonkeyPatch):
    """copytree 失败 → exit 1 + stderr 报路径（铁律 12，不静默吞错）。"""
    import shutil

    def _boom(*_args, **_kwargs):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(skill_cmds.shutil, "copytree", _boom)
    result = runner.invoke(app, ["skill", "install"])
    assert result.exit_code == 1
    assert "permission denied (simulated)" in result.output or "simulated" in result.output


def test_orca_and_teams_both_aliases_work():
    """``orca`` / ``teams`` 两个 entry point 同入口（pyproject 声明），skill 子命令在 app 上即可。"""
    # CliRunner 直接打 app，不依赖 binary 名；这里只确认 skill install 在顶层 app 注册。
    result = runner.invoke(app, ["skill", "--help"])
    assert result.exit_code == 0
    assert "install" in result.output

"""test_skill_cmds.py —— ``teams skill install`` 弃用别名 + ``install_targets`` 纯函数单测（v5 §4.3）。

覆盖：
  - ``install_targets``：cc / opencode / cac / nga / all 五态 + ``OPENCODE_CONFIG_DIR`` 覆盖 +
    未知 target 抛错；返 skill **base 目录**（随包所有 skill 落其下）。
  - ``skill install``（弃用别名，委托 ``teams install``）：默认 all 四前端都装、``--target cc``
    只装 cc、幂等重跑、fail loud（copytree 失败 → exit 1 + stderr 报路径）。
  - monkeypatch ``Path.home`` 到 tmp_path，不碰真实 ``~/.claude`` / ``~/.config/opencode``。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.iface.cli import skill_cmds
from orca.iface.cli.commands import app

runner = CliRunner()


# ── install_targets 纯函数（v5 §4.3：返 skill base 目录）──────────────────────


def test_targets_all(tmp_path: Path):
    targets = skill_cmds.install_targets("all", home=tmp_path)
    labels = {label for label, _ in targets}
    assert labels == {"cc", "opencode", "cac", "nga"}
    for _, dst in targets:
        assert dst.name == "skills"  # base 目录，随包所有 skill 落其下


def test_targets_cc_only(tmp_path: Path):
    targets = skill_cmds.install_targets("cc", home=tmp_path)
    assert [label for label, _ in targets] == ["cc"]
    assert targets[0][1] == tmp_path / ".claude" / "skills"


def test_targets_opencode_only(tmp_path: Path):
    targets = skill_cmds.install_targets("opencode", home=tmp_path)
    assert [label for label, _ in targets] == ["opencode"]
    assert targets[0][1] == tmp_path / ".config" / "opencode" / "skills"


def test_targets_opencode_config_dir_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    custom = tmp_path / "custom-oc-config"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(custom))
    targets = skill_cmds.install_targets("opencode", home=tmp_path)
    assert targets[0][1] == custom / "skills"


def test_targets_cac_and_nga(tmp_path: Path):
    for platform, dotdir in (("cac", ".cac"), ("nga", ".nga")):
        targets = skill_cmds.install_targets(platform, home=tmp_path)
        assert [label for label, _ in targets] == [platform]
        assert targets[0][1] == tmp_path / dotdir / "skills"


def test_targets_unknown_raises(tmp_path: Path):
    import typer

    with pytest.raises(typer.BadParameter):
        skill_cmds.install_targets("bogus", home=tmp_path)


# ── skill install（弃用别名，委托 teams install；CliRunner + monkeypatch home）──


@pytest.fixture
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把 ``Path.home`` 指到 tmp_path，隔离 ``~/.claude`` / ``~/.config/opencode``。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    return tmp_path


def _skill_file(dst_root: Path) -> Path:
    return dst_root / ".claude" / "skills" / skill_cmds.SKILL_NAME / "SKILL.md"


def _entry_skill_file(dst_root: Path) -> Path:
    """v5 in-session 入口 skill（TARS 品牌）落地路径。目录名取 ``ENTRY_SKILL_NAME`` 单一真相源。"""
    return dst_root / ".claude" / "skills" / skill_cmds.ENTRY_SKILL_NAME / "SKILL.md"


def test_install_both(_isolated_home: Path):
    result = runner.invoke(app, ["skill", "install"])
    assert result.exit_code == 0, result.output
    home = _isolated_home
    # CC（create-workflow + tars 入口 skill 都装）
    assert _skill_file(home).is_file()
    assert _entry_skill_file(home).is_file()
    # opencode（两 skill 都装）
    assert (home / ".config" / "opencode" / "skills" / skill_cmds.SKILL_NAME / "SKILL.md").is_file()
    assert (home / ".config" / "opencode" / "skills" / skill_cmds.ENTRY_SKILL_NAME / "SKILL.md").is_file()
    # v5：cac / nga 也装（四前端）
    for dotdir in (".cac", ".nga"):
        assert (home / dotdir / "skills" / skill_cmds.ENTRY_SKILL_NAME / "SKILL.md").is_file()
    # reference + examples 跟着 copy
    skill_dir = home / ".claude" / "skills" / skill_cmds.SKILL_NAME
    assert (skill_dir / "reference" / "orca-workflow-contract.md").is_file()
    assert any((skill_dir / "examples").glob("*.yaml"))
    # 🔴 公平性：benchmark/（评测答案）绝不被装到用户 skill 目录
    assert not (skill_dir / "benchmark").exists(), "install 不应拷 benchmark/（会泄露评测答案）"


def test_install_target_cc_only(_isolated_home: Path):
    result = runner.invoke(app, ["skill", "install", "--target", "cc"])
    assert result.exit_code == 0, result.output
    home = _isolated_home
    assert _skill_file(home).is_file()  # CC 装了
    assert _entry_skill_file(home).is_file()
    # opencode / cac / nga 没装
    assert not (home / ".config" / "opencode" / "skills" / skill_cmds.SKILL_NAME).exists()
    assert not (home / ".cac" / "skills" / skill_cmds.ENTRY_SKILL_NAME).exists()


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


def test_skill_install_deprecated_warns_and_delegates(_isolated_home: Path):
    """``teams skill install`` 已弃用：打印 ⚠ 警告 + 委托 ``teams install``（文件仍落地）。"""
    result = runner.invoke(app, ["skill", "install"])
    assert result.exit_code == 0, result.output
    # 弃用警告打到 stderr（CliRunner 默认 mix 进 output）
    assert "已弃用" in result.output
    # 委托执行：CC + opencode skill 仍落地
    assert _skill_file(_isolated_home).is_file()
    oc_skill = (
        _isolated_home / ".config" / "opencode" / "skills" / skill_cmds.SKILL_NAME / "SKILL.md"
    )
    assert oc_skill.is_file()
    # opencode 仍装 plugin（惰性，step 4 整删）
    assert (_isolated_home / ".config" / "opencode" / "plugins" / "orca.ts").is_file()


def test_orca_and_teams_both_aliases_work():
    """``orca`` / ``teams`` 两个 entry point 同入口（pyproject 声明），skill 子命令在 app 上即可。"""
    # CliRunner 直接打 app，不依赖 binary 名；这里只确认 skill install 在顶层 app 注册。
    result = runner.invoke(app, ["skill", "--help"])
    assert result.exit_code == 0
    assert "install" in result.output

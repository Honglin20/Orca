"""test_skill_cmds.py —— ``tars skill install`` 弃用别名 + ``install_targets`` 纯函数单测（v5 §4.3）。

覆盖：
  - ``install_targets``：cc / opencode / cac / nga / all 五态 + ``OPENCODE_CONFIG_DIR`` 覆盖 +
    未知 target 抛错；返 skill **base 目录**（随包所有 skill 落其下）。
  - ``skill install``（弃用别名，委托 ``tars install``）：默认 all 四前端都装、``--target cc``
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


# ── skill install（弃用别名，委托 tars install；CliRunner + monkeypatch home）──


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
    """``tars skill install`` 已弃用：打印 ⚠ 警告 + 委托 ``tars install``（文件仍落地）。"""
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


def test_skill_subcommand_registered_on_app():
    """skill 子命令注册在顶层 backend app 上（``tars skill --help`` 可用）。

    注：此处只验 app 内装配——不依赖 binary 名。binary entry point（``tars``）由
    ``test_backend_entry_point_is_tars_not_teams`` 读 pyproject 锁契约，真机 ``which tars``
    由 test-agent 验。
    """
    result = runner.invoke(app, ["skill", "--help"])
    assert result.exit_code == 0
    assert "install" in result.output


def test_backend_entry_point_is_tars_not_teams():
    """锁 ``[project.scripts]`` 后端入口 = ``tars``（2026-07-16 teams→tars 改名契约）。

    deterministic 读 pyproject.toml（不装包），防有人把 entry 改回 ``teams`` 或拼错而单测全绿。
    binary 真上 PATH 由 test-agent 真机验（``which tars``）；此处锁源代码契约。
    """
    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert 'tars = "orca.iface.cli.commands:main"' in text, (
        "pyproject [project.scripts] 必须有 tars 后端入口（2026-07-16 改名）"
    )
    # teams 入口已退役（保 teams_app 模块别名作向后兼容，但 binary 不再上 PATH）
    assert 'teams = "orca.iface.cli.commands:main"' not in text, (
        "pyproject [project.scripts] 不应再有 teams 入口（已改名 tars）"
    )


def test_teams_app_deprecated_alias_still_importable():
    """``teams_app`` deprecated 别名仍可 import 且 is app（向后兼容垫片，2026-07-16 保）。

    plan §1.2：``teams_app = app`` 作改名前旧别名保留，防外部代码 / notebook 陡断。
    本测试锁该契约——若将来误删 ``teams_app``，单测红。
    """
    from orca.iface.cli.commands import app as backend_app, tars_app, teams_app

    assert teams_app is tars_app is backend_app, (
        "teams_app（deprecated）/ tars_app 应都 is app（同一对象，向后兼容别名）"
    )

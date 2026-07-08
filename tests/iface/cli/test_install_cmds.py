"""test_install_cmds.py —— ``orca install`` 统一安装入口单测。

覆盖 spike-verified（2026-07-08，详见 ``docs/plans/2026-07-08-unified-install.md``）行为：
  - ``resolve_roots``：target(all/claude/opencode) × scope(user/project) 矩阵 +
    ``OPENCODE_CONFIG_DIR`` 覆盖 + 未知值 fail loud。
  - opencode 全套落地：skill + ``plugins/orca.ts`` + ``command/orca.md`` + ``opencode.json`` 声明。
  - ``opencode.json`` 合并：保已有键 / ``$schema`` / 其他 plugin 条目；去重；项目相对 vs 用户绝对。
  - 幂等：再跑不重复加声明。
  - claude target：只装 skill（无 plugin/command——CC hooks 是 per-run）。
  - project scope：``opencode.json`` 在 cwd 根 + 相对声明路径。
  - fail loud：copytree 失败 → exit 1（铁律 12）。
  - 守门：不拷 ``benchmark/``。
  - 模板内容 = 随包模板（防 install 写错版本）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from orca.iface.cli import install_cmds
from orca.iface.cli.install_cmds import app

runner = CliRunner()


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``Path.home`` → tmp_path；清 ``OPENCODE_CONFIG_DIR``。隔离 ``~/.claude`` / ``~/.config/opencode``。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    return tmp_path


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """chdir 到 tmp_path（project scope 落地用 cwd 解析 ``.opencode/`` / 根 ``opencode.json``）。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── resolve_roots（纯函数）────────────────────────────────────────────────────


def test_resolve_roots_all_user(isolated_home: Path):
    roots = install_cmds.resolve_roots("all", "user", home=isolated_home)
    assert sorted(r.host for r in roots) == ["claude", "opencode"]
    oc = next(r for r in roots if r.host == "opencode")
    assert oc.root == isolated_home / ".config" / "opencode"
    cc = next(r for r in roots if r.host == "claude")
    assert cc.root == isolated_home / ".claude"


def test_resolve_roots_opencode_user_honors_OPENCODE_CONFIG_DIR(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
):
    custom = isolated_home / "custom-oc"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(custom))
    roots = install_cmds.resolve_roots("opencode", "user", home=isolated_home)
    assert roots[0].root == custom


def test_resolve_roots_project_uses_cwd(isolated_home: Path, isolated_cwd: Path):
    roots = install_cmds.resolve_roots("opencode", "project", home=isolated_home)
    assert roots[0].root == isolated_cwd / ".opencode"
    assert roots[0].scope == "project"


def test_resolve_roots_bad_target(isolated_home: Path):
    with pytest.raises(typer.BadParameter):
        install_cmds.resolve_roots("bogus", "user", home=isolated_home)


def test_resolve_roots_bad_scope(isolated_home: Path):
    with pytest.raises(typer.BadParameter):
        install_cmds.resolve_roots("all", "galaxy", home=isolated_home)


# ── opencode 全套落地（user scope）────────────────────────────────────────────


def test_install_opencode_user_lands_all(isolated_home: Path):
    result = runner.invoke(app, ["--target", "opencode", "--scope", "user"])
    assert result.exit_code == 0, result.output
    oc = isolated_home / ".config" / "opencode"
    assert (oc / "skills" / install_cmds.SKILL_NAME / "SKILL.md").is_file()
    assert (oc / "plugins" / "orca.ts").is_file()
    # 批 B：command 是 ``orca/`` 命名空间（run/status/stop/doctor），非单 orca.md
    cmd_ns = oc / "command" / "orca"
    assert cmd_ns.is_dir()
    assert {p.name for p in cmd_ns.iterdir()} == {"run.md", "status.md", "stop.md", "doctor.md"}
    # 旧单命令模板不应残留（install 清理）
    assert not (oc / "command" / "orca.md").exists()
    cfg = json.loads((oc / "opencode.json").read_text())
    # 用户 scope：声明用绝对路径（spike：全局 config 非项目相对，必须绝对）
    assert str((oc / "plugins" / "orca.ts").resolve()) in cfg["plugin"]
    # 意图断言（review 🟢#1）：不依赖 resolve 对称性，直接断言绝对路径语义
    orca_decl = next(p for p in cfg["plugin"] if "orca.ts" in p)
    assert orca_decl.startswith("/"), f"用户 scope 声明应为绝对路径: {orca_decl}"


def test_install_opencode_json_preserves_existing_keys(isolated_home: Path):
    """合并 opencode.json：保 $schema / 其他 plugin / 自定义键；只追加 orca 声明。"""
    oc = isolated_home / ".config" / "opencode"
    oc.mkdir(parents=True)
    (oc / "opencode.json").write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "plugin": ["some-other-plugin"],
        "custom_key": "kept",
    }))
    result = runner.invoke(app, ["--target", "opencode", "--scope", "user"])
    assert result.exit_code == 0, result.output
    cfg = json.loads((oc / "opencode.json").read_text())
    assert cfg["$schema"] == "https://opencode.ai/config.json"
    assert "some-other-plugin" in cfg["plugin"]
    assert cfg["custom_key"] == "kept"
    assert any("orca.ts" in p for p in cfg["plugin"])


def test_install_opencode_idempotent_no_duplicate(isolated_home: Path):
    """再跑：opencode.json 不重复加 orca 声明。"""
    for _ in range(2):
        r = runner.invoke(app, ["--target", "opencode", "--scope", "user"])
        assert r.exit_code == 0, r.output
    cfg = json.loads(
        (isolated_home / ".config" / "opencode" / "opencode.json").read_text()
    )
    orca_entries = [p for p in cfg["plugin"] if "orca.ts" in p]
    assert len(orca_entries) == 1, f"orca 声明重复: {orca_entries}"


def test_install_opencode_json_recovers_from_corrupt(isolated_home: Path):
    """opencode.json 损坏（非 JSON）→ 从 {} 起，不崩（fail-soft 读；写仍原子）。"""
    oc = isolated_home / ".config" / "opencode"
    oc.mkdir(parents=True)
    (oc / "opencode.json").write_text("{ not valid json")
    result = runner.invoke(app, ["--target", "opencode", "--scope", "user"])
    assert result.exit_code == 0, result.output
    cfg = json.loads((oc / "opencode.json").read_text())
    assert any("orca.ts" in p for p in cfg["plugin"])


# ── project scope（相对声明 + cwd 根 opencode.json）──────────────────────────


def test_install_project_scope_relative_declaration(
    isolated_home: Path, isolated_cwd: Path
):
    """项目 scope：模板在 .opencode/，opencode.json 在 cwd 根，声明用相对路径（spike 验证）。"""
    result = runner.invoke(app, ["--target", "opencode", "--scope", "project"])
    assert result.exit_code == 0, result.output
    assert (isolated_cwd / ".opencode" / "plugins" / "orca.ts").is_file()
    cfg_path = isolated_cwd / "opencode.json"
    assert cfg_path.is_file(), "项目 scope opencode.json 应在 cwd 根"
    cfg = json.loads(cfg_path.read_text())
    assert "./.opencode/plugins/orca.ts" in cfg["plugin"]


# ── claude target（只装 skill）────────────────────────────────────────────────


def test_install_claude_only_skill(isolated_home: Path):
    result = runner.invoke(app, ["--target", "claude", "--scope", "user"])
    assert result.exit_code == 0, result.output
    cc = isolated_home / ".claude"
    assert (cc / "skills" / install_cmds.SKILL_NAME / "SKILL.md").is_file()
    # claude 不装 plugin / command（CC hooks per-run，由 in-session start 生成）
    assert not (cc / "plugins").exists()
    assert not (cc / "command").exists()


def test_install_no_benchmark(isolated_home: Path):
    """守门：benchmark/（评测答案）绝不装到用户目录。"""
    runner.invoke(app, ["--target", "claude", "--scope", "user"])
    skill = isolated_home / ".claude" / "skills" / install_cmds.SKILL_NAME
    assert not (skill / "benchmark").exists(), "install 不应拷 benchmark/"


# ── fail loud + 模板内容 ──────────────────────────────────────────────────────


def test_install_fail_loud(isolated_home: Path, monkeypatch: pytest.MonkeyPatch):
    """copytree 失败 → exit 1 + 报路径（铁律 12，不静默吞错）。"""

    def _boom(*_a, **_k):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(install_cmds.shutil, "copytree", _boom)
    result = runner.invoke(app, ["--target", "claude", "--scope", "user"])
    assert result.exit_code == 1
    assert "simulated" in result.output or "失败" in result.output


def test_install_template_content_matches_bundle(isolated_home: Path):
    """落地 plugin/command 内容 = 随包模板（防 install 写错 / 漂移版本）。"""
    runner.invoke(app, ["--target", "opencode", "--scope", "user"])
    oc = isolated_home / ".config" / "opencode"
    assert (oc / "plugins" / "orca.ts").read_text() == install_cmds._opencode_plugin_src().read_text()
    # 批 B：command 命名空间 4 个 .md，逐个比对随包模板
    cmd_ns = oc / "command" / "orca"
    bundled = {p.name: p.read_text() for p in install_cmds._opencode_command_srcs()}
    assert set(bundled) == {"run.md", "status.md", "stop.md", "doctor.md"}
    for name, content in bundled.items():
        assert (cmd_ns / name).read_text() == content


def test_install_warns_on_legacy_singular_plugin_dir(isolated_home: Path):
    """迁移友好：检测到旧 start 写的 singular ``plugin/`` 目录（无 s）→ warn（review 🟡#4）。"""
    oc = isolated_home / ".config" / "opencode"
    (oc / "plugin").mkdir(parents=True)  # 旧式 singular（无 s）
    (oc / "plugin" / "orca.ts").write_text("// legacy from old start", encoding="utf-8")
    result = runner.invoke(app, ["--target", "opencode", "--scope", "user"])
    assert result.exit_code == 0, result.output
    assert "旧式" in result.output or "singular" in result.output


def test_install_warns_on_non_array_plugin(isolated_home: Path):
    """opencode.json 的 plugin 字段非数组 → warn + 重置（不静默吞，review 🟡#2）。"""
    oc = isolated_home / ".config" / "opencode"
    oc.mkdir(parents=True)
    (oc / "opencode.json").write_text(json.dumps({"plugin": "single-string.ts"}))
    result = runner.invoke(app, ["--target", "opencode", "--scope", "user"])
    assert result.exit_code == 0, result.output
    assert "非 array" in result.output or "重置" in result.output


def test_install_cmds_has_no_orca_business_logic():
    """架构守门（D-v7-1 同源）：install_cmds 零 Orca 业务逻辑——只拷文件 + 合并 JSON。

    禁止 import ``orca.run`` / ``orca.events`` / ``orca.schema`` 或调用 advance/router/replay/
    tape 路径。让 ``install_cmds`` docstring 的「CI grep 守门」承诺成真（review 🟡#3）。
    禁词用**限定调用形态**（如 ``advance_step``），避开 docstring 里「不调 advance/router」
    这类合规描述。
    """
    src = Path(install_cmds.__file__).read_text(encoding="utf-8")
    forbidden = [
        "from orca.run", "from orca.events", "from orca.schema",
        "import orca.run", "import orca.events", "import orca.schema",
        "advance_step", "router.resolve", "replay_state", "tape.append",
        "EventBus(", "Orchestrator(",
    ]
    for kw in forbidden:
        assert kw not in src, f"install_cmds 含禁词 {kw!r}（违反零业务逻辑守门）"

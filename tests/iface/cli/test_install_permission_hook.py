"""test_install_permission_hook.py —— PermissionRequest hook install 单测（SPEC §4.4 / §11）。

覆盖：
  - ``_install_cc_nudge`` 单事务合并 ``hooks.PermissionRequest``（去重关键字 ``orca-permission``）。
  - ``orca-permission-hook.py`` 落到 ``<root>/hooks/`` 且 exec bit 755。
  - hook 命令含绝对路径 + ``timeout=86400``。
  - hook env 写入 ``ORCA_PORT`` / ``ORCA_HOST`` / ``ORCA_APPROVAL_TIMEOUT`` / ``ORCA_APPROVAL_TIMEOUT_POLICY``。
  - 幂等：再跑不重复加 PermissionRequest 声明。
  - 保已有 settings.json 键 / hooks。
  - cac 家族同款（``.cac/hooks/orca-permission-hook.py``）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.iface.cli import install_cmds
from orca.iface.cli.install_cmds import app

runner = CliRunner()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    return tmp_path


def _permission_entries(cfg: dict) -> list[dict]:
    return [
        entry for entry in cfg.get("hooks", {}).get("PermissionRequest", [])
        if isinstance(entry, dict)
        and any(
            "orca-permission" in str(h.get("command", ""))
            for h in entry.get("hooks", [])
        )
    ]


def test_install_cc_lands_permission_hook(isolated_home: Path):
    """cc target → ``.claude/hooks/orca-permission-hook.py`` + settings.json hooks.PermissionRequest。"""
    result = runner.invoke(app, ["--target", "cc", "--scope", "user"])
    assert result.exit_code == 0, result.output
    cc = isolated_home / ".claude"
    hook_path = cc / "hooks" / "orca-permission-hook.py"
    assert hook_path.is_file(), "PermissionRequest hook 脚本未落地"
    cfg = json.loads((cc / "settings.json").read_text())
    entries = _permission_entries(cfg)
    assert len(entries) == 1, f"PermissionRequest 应有 1 条 orca entry，实际 {entries}"
    cmd = entries[0]["hooks"][0]["command"]
    assert cmd.startswith("python ")
    assert "orca-permission-hook.py" in cmd
    # 绝对路径（不依赖 cwd）。
    assert os.path.isabs(cmd.split("python ", 1)[1])


def test_install_cc_permission_hook_exec_bit(isolated_home: Path):
    """SPEC §11 N8：exec bit 755（best-effort，Linux/Mac 生效）。"""
    runner.invoke(app, ["--target", "cc", "--scope", "user"])
    hook_path = isolated_home / ".claude" / "hooks" / "orca-permission-hook.py"
    # Windows FS 无 exec bit；只测 POSIX。
    if os.name == "posix":
        mode = hook_path.stat().st_mode & 0o777
        assert mode == 0o755, f"exec bit 应 755，实际 {oct(mode)}"


def test_install_cc_permission_hook_timeout_86400(isolated_home: Path):
    """SPEC §3.4 / §4.4：CC hook timeout=86400（传输层永不误杀审批 hook）。"""
    runner.invoke(app, ["--target", "cc", "--scope", "user"])
    cfg = json.loads((isolated_home / ".claude" / "settings.json").read_text())
    entries = _permission_entries(cfg)
    assert entries[0]["hooks"][0]["timeout"] == 86400


def test_install_cc_permission_hook_env_written(isolated_home: Path):
    """SPEC §4.4 末段：hook env 写入 ORCA_PORT/HOST/TIMEOUT/POLICY。"""
    runner.invoke(app, ["--target", "cc", "--scope", "user"])
    cfg = json.loads((isolated_home / ".claude" / "settings.json").read_text())
    env = _permission_entries(cfg)[0]["hooks"][0].get("env", {})
    assert env["ORCA_PORT"] == "7428"
    assert env["ORCA_HOST"] == "127.0.0.1"
    assert env["ORCA_APPROVAL_TIMEOUT"] == "600"
    assert env["ORCA_APPROVAL_TIMEOUT_POLICY"] == "allow"


def test_install_cc_permission_hook_env_honors_user_env(isolated_home, monkeypatch):
    """install 时若用户设了 ORCA_HOST/PORT 等 env，install 用用户值（N9 跨边界）。"""
    monkeypatch.setenv("ORCA_PORT", "9999")
    monkeypatch.setenv("ORCA_HOST", "10.0.0.5")
    monkeypatch.setenv("ORCA_APPROVAL_TIMEOUT", "120")
    monkeypatch.setenv("ORCA_APPROVAL_TIMEOUT_POLICY", "deny")
    runner.invoke(app, ["--target", "cc", "--scope", "user"])
    cfg = json.loads((isolated_home / ".claude" / "settings.json").read_text())
    env = _permission_entries(cfg)[0]["hooks"][0]["env"]
    assert env["ORCA_PORT"] == "9999"
    assert env["ORCA_HOST"] == "10.0.0.5"
    assert env["ORCA_APPROVAL_TIMEOUT"] == "120"
    assert env["ORCA_APPROVAL_TIMEOUT_POLICY"] == "deny"


def test_install_cc_permission_hook_idempotent(isolated_home: Path):
    """再跑：settings.json hooks.PermissionRequest 不重复加。"""
    for _ in range(3):
        r = runner.invoke(app, ["--target", "cc", "--scope", "user"])
        assert r.exit_code == 0, r.output
    cfg = json.loads((isolated_home / ".claude" / "settings.json").read_text())
    entries = _permission_entries(cfg)
    assert len(entries) == 1, f"重复 install 应幂等，实际 {len(entries)} 条"


def test_install_cc_permission_hook_preserves_existing_settings(isolated_home: Path):
    """合并 settings.json：保已有 hooks.Stop / hooks.PostToolUse / 其他键。"""
    cc = isolated_home / ".claude"
    cc.mkdir(parents=True)
    (cc / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(*)"]},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo user"}]}],
            "PermissionRequest": [
                {"hooks": [{"type": "command", "command": "echo user-pr"}]}
            ],
        },
    }))
    result = runner.invoke(app, ["--target", "cc", "--scope", "user"])
    assert result.exit_code == 0, result.output
    cfg = json.loads((cc / "settings.json").read_text())
    # 已有 Stop 保留 + orca nudge 追加。
    stop_cmds = [h["command"] for e in cfg["hooks"]["Stop"] for h in e["hooks"]]
    assert "echo user" in stop_cmds
    assert any("orca-nudge.sh" in c for c in stop_cmds)
    # 用户已有 PermissionRequest 保留 + orca permission 追加。
    pr_cmds = [h["command"] for e in cfg["hooks"]["PermissionRequest"] for h in e["hooks"]]
    assert "echo user-pr" in pr_cmds
    assert any("orca-permission-hook.py" in c for c in pr_cmds)
    # 其他键保留。
    assert cfg["permissions"]["allow"] == ["Bash(*)"]


def test_install_cac_permission_hook_symmetric(isolated_home: Path):
    """cac target → ``.cac/hooks/orca-permission-hook.py`` + settings.json（结构同 cc）。"""
    runner.invoke(app, ["--target", "cac", "--scope", "user"])
    cac = isolated_home / ".cac"
    assert (cac / "hooks" / "orca-permission-hook.py").is_file()
    cfg = json.loads((cac / "settings.json").read_text())
    assert len(_permission_entries(cfg)) == 1


def test_install_cc_permission_hook_script_matches_bundle(isolated_home: Path):
    """install 写入的 hook 脚本内容 = 随包模板（防 install 写错版本）。"""
    runner.invoke(app, ["--target", "cc", "--scope", "user"])
    src = install_cmds._cc_permission_hook_src().read_text(encoding="utf-8")
    dst = (isolated_home / ".claude" / "hooks" / "orca-permission-hook.py").read_text(
        encoding="utf-8",
    )
    assert src == dst

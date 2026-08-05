"""test_install_cmds.py —— ``tars install`` 统一安装入口单测（v5 §4.3 四前端）。

覆盖：
  - ``resolve_roots``：target(all/cc/opencode/cac/nga) × scope(user/project) 矩阵 +
    ``OPENCODE_CONFIG_DIR`` 覆盖 + 未知值 fail loud。
  - opencode 落地：随包 skill（含 orca 入口 skill）+ ``plugins/orca.ts`` + ``opencode.json`` 声明。
  - ``opencode.json`` 合并：保已有键 / ``$schema`` / 其他 plugin 条目；去重；项目相对 vs 用户绝对。
  - 幂等：再跑不重复加声明。
  - cc / cac / nga target：cc 家族（cc/cac）skill + nudge Stop-hook；opencode 家族（opencode/nga）skill + plugin + json。
  - project scope：``opencode.json`` 在 cwd 根 + 相对声明路径。
  - fail loud：copytree 失败 → exit 1（铁律 12）。
  - 守门：不拷 ``benchmark/``。
  - 模板内容 = 随包模板（防 install 写错版本）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
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
    # v5 §4.3：all → 四前端 cc/opencode/cac/nga
    assert sorted(r.host for r in roots) == ["cac", "cc", "nga", "opencode"]
    oc = next(r for r in roots if r.host == "opencode")
    assert oc.root == isolated_home / ".config" / "opencode"
    cc = next(r for r in roots if r.host == "cc")
    assert cc.root == isolated_home / ".claude"
    cac = next(r for r in roots if r.host == "cac")
    assert cac.root == isolated_home / ".cac"
    nga = next(r for r in roots if r.host == "nga")
    assert nga.root == isolated_home / ".nga"


def test_resolve_roots_project_scope_four_platforms(isolated_home: Path, isolated_cwd: Path):
    """project scope：四前端都落 cwd 下对应 dotdir。"""
    roots = install_cmds.resolve_roots("all", "project", home=isolated_home)
    by_host = {r.host: r.root for r in roots}
    assert by_host["cc"] == isolated_cwd / ".claude"
    assert by_host["opencode"] == isolated_cwd / ".opencode"
    assert by_host["cac"] == isolated_cwd / ".cac"
    assert by_host["nga"] == isolated_cwd / ".nga"


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
    # v5：所有随包 skill 都装（create-workflow + tars 入口 skill）
    assert (oc / "skills" / install_cmds.SKILL_NAME / "SKILL.md").is_file()
    assert (oc / "skills" / install_cmds.ENTRY_SKILL_NAME / "SKILL.md").is_file()
    assert (oc / "plugins" / "orca.ts").is_file()
    # v5 step 2b(5)：command 模板已删，install 不再创建 command/orca/ 命名空间
    assert not (oc / "command" / "orca").exists()
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


# ── cc 家族 / opencode 家族 target（step 6：CAC≡cc / NGA≡opencode 全套装）──────


def test_install_cc_family_full_set(isolated_home: Path):
    """cc target → cc 家族全套：skill + nudge Stop-hook（v5 §4.4 step 2b(7)）。"""
    result = runner.invoke(app, ["--target", "cc", "--scope", "user"])
    assert result.exit_code == 0, result.output
    cc = isolated_home / ".claude"
    assert (cc / "skills" / install_cmds.SKILL_NAME / "SKILL.md").is_file()
    assert (cc / "skills" / install_cmds.ENTRY_SKILL_NAME / "SKILL.md").is_file()
    # cc nudge：脚本 + settings.json Stop 声明
    assert (cc / "hooks" / "orca-nudge.sh").is_file()
    cfg = json.loads((cc / "settings.json").read_text())
    stop = cfg["hooks"]["Stop"]
    cmds = [h["command"] for entry in stop for h in entry["hooks"]]
    assert any("orca-nudge.sh" in c for c in cmds)
    # cc 家族（cc/cac）不装 plugin / command（那是 opencode 家族专属）
    assert not (cc / "plugins").exists()
    assert not (cc / "command").exists()


def test_install_cc_nudge_script_never_calls_next(isolated_home: Path):
    """v5 §4.4 铁律：nudge 脚本只 block 提醒，**绝不**执行 ``orca next``（防退化 A 路径）。

    reminder 文案里提到 ``orca next`` 是允许的（教模型去调）；脚本自身不得 spawn 或ca CLI。
    守门：脚本无 orca 子进程调用（``$(orca`` / 反引号 / 行首裸 ``orca`` 命令均不得有）。
    """
    runner.invoke(app, ["--target", "cc", "--scope", "user"])
    script = (isolated_home / ".claude" / "hooks" / "orca-nudge.sh").read_text()
    # nudge 机制：emit ``decision: "block"`` JSON。正则容许 jq 形 ``decision:"block"`` 与
    # python 形 ``"decision": "block"`` 两种字面（review NIT#1：收紧——只匹配 JSON 字段形态，
    # 不被注释 / 无关字符串里的 "decision" / "block" 字符满足）。
    assert re.search(r'"decision"\s*:\s*"block"', script), (
        "nudge 脚本必须 emit decision:block JSON（CC Stop hook 协议）"
    )
    # 提醒文案教模型调 next（允许出现，纯文本）
    assert "orca next" in script
    # 守门：脚本不得 spawn 或ca CLI。REASON 是双引号字符串——**禁用反引号**（双引号内
    # 反引号 = bash 命令替换，会误执行 ``orca next`` 退化 A 路径）。脚本全篇零反引号。
    assert "`" not in script, "nudge 脚本禁用反引号（双引号内 = 命令替换，可能误执行 orca）"
    assert "$(orca" not in script, "nudge 脚本不得 $(orca ...) 调 CLI"
    # 行首裸 ``orca`` 命令（执行 next/stop 等子命令）也禁
    exec_lines = [ln for ln in script.splitlines()
                  if ln.strip().startswith("orca ") and not ln.strip().startswith("#")]
    assert exec_lines == [], f"nudge 脚本不得直接执行 orca 命令: {exec_lines}"
    # DEFECT-1：实现必须用 python3（跨环境可靠），不得用 jq（WSL conda orca 等环境可能无 jq）。
    # 旧版 ``jq ... 2>/dev/null || true`` 在缺 jq 时静默失败 → nudge 永不触发且无报错（fail-loud 反例）。
    assert "python3" in script, "nudge 脚本必须用 python3（DEFECT-1：jq 跨环境不可靠）"
    # 守门只看**非注释行**——注释里可以提到 jq（说明为何不用），脚本执行体不得 spawn jq。
    non_comment_lines = [
        ln for ln in script.splitlines()
        if not ln.lstrip().startswith("#")
    ]
    exec_body = "\n".join(non_comment_lines)
    assert "jq " not in exec_body and "jq<" not in exec_body and "| jq" not in exec_body, (
        "nudge 脚本执行体不得 spawn jq（DEFECT-1：缺 jq 时静默失败违反 fail-loud）"
    )


def test_install_cc_nudge_idempotent_no_duplicate(isolated_home: Path):
    """再跑：settings.json Stop 不重复加 orca nudge 声明。"""
    for _ in range(2):
        r = runner.invoke(app, ["--target", "cc", "--scope", "user"])
        assert r.exit_code == 0, r.output
    cfg = json.loads((isolated_home / ".claude" / "settings.json").read_text())
    stop = cfg["hooks"]["Stop"]
    orca_entries = [
        entry for entry in stop
        if isinstance(entry, dict)
        and any("orca-nudge" in str(h.get("command", "")) for h in entry.get("hooks", []))
    ]
    assert len(orca_entries) == 1, f"orca nudge Stop 声明重复: {orca_entries}"


def test_install_cc_nudge_preserves_existing_settings(isolated_home: Path):
    """合并 settings.json：保已有 hooks / 其他键；只追加 orca nudge Stop。"""
    cc = isolated_home / ".claude"
    cc.mkdir(parents=True)
    (cc / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(*)"]},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo user-hook"}]}],
            "PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "echo"}]}],
        },
    }))
    result = runner.invoke(app, ["--target", "cc", "--scope", "user"])
    assert result.exit_code == 0, result.output
    cfg = json.loads((cc / "settings.json").read_text())
    # 已有键保留
    assert cfg["permissions"]["allow"] == ["Bash(*)"]
    assert cfg["hooks"]["PostToolUse"][0]["matcher"] == "Write"
    # 用户原有 Stop 保留 + orca nudge 追加
    stop_cmds = [h["command"] for entry in cfg["hooks"]["Stop"] for h in entry["hooks"]]
    assert "echo user-hook" in stop_cmds
    assert any("orca-nudge.sh" in c for c in stop_cmds)


def test_install_cc_nudge_recovers_malformed_settings(isolated_home: Path):
    """settings.json 的 hooks / hooks.Stop 非法形态（非 object / 非 array）→ warn + 重置 +
    加入 orca nudge（不静默吞，fail loud；与 _install_opencode 同款 recovery 对齐）。"""
    cc = isolated_home / ".claude"
    cc.mkdir(parents=True)
    (cc / "settings.json").write_text(json.dumps({
        "hooks": "not-an-object",   # 非法：hooks 应是 object
    }))
    result = runner.invoke(app, ["--target", "cc", "--scope", "user"])
    assert result.exit_code == 0, result.output
    # 非法形态 → warn 到 stderr（CliRunner mix 进 output）
    assert "非 object" in result.output or "重置" in result.output
    cfg = json.loads((cc / "settings.json").read_text())
    # hooks 被重置为 object + orca nudge Stop 加入
    assert isinstance(cfg["hooks"], dict)
    stop_cmds = [h["command"] for entry in cfg["hooks"]["Stop"] for h in entry["hooks"]]
    assert any("orca-nudge.sh" in c for c in stop_cmds)


# ── cc nudge 脚本行为（DEFECT-1：fail-loud + python3）─────────────────────────
#
# 旧版 cc_nudge.sh 用 ``jq ... 2>/dev/null || true`` 读 marker；缺 jq 时静默失败 → nudge 永
# 不触发且无报错（违反 fail-loud）。DEFECT-1 改用 python3（orca 本就依赖 python，跨环境可靠）
# + 非法 marker fail loud。下方测试用真子进程跑脚本，验证语义不变（block/pass/节流）+ fail loud。


_BASH = shutil.which("bash")
_PYTHON3 = shutil.which("python3")
_NUDGE_BEHAVIOR_OK = bool(_BASH) and bool(_PYTHON3)
pytestmark_nudge_behavior = pytest.mark.skipif(
    not _NUDGE_BEHAVIOR_OK,
    reason="跑 cc_nudge.sh 需要 bash + python3（Windows 原生缺；WSL / Linux / macOS 有）",
)


def _write_nudge_script(dst_dir: Path) -> Path:
    """拷随包 cc_nudge.sh 到 dst_dir（行为测试跑的是真脚本，非 mock）。"""
    src = install_cmds._cc_nudge_script_src()
    dst = dst_dir / "orca-nudge.sh"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


# host-session-binding v2：nudge 按 host_session 过滤。测试默认模拟「CC 注入了 session id」
# 的真实 Stop-hook env（CLAUDE_CODE_SESSION_ID）。脚本读 env 拿 current，读 tape 首行拿归属。
_NUDGE_TEST_SESSION = "cc-session-test-abc"


def _nudge_env(session: str | None = _NUDGE_TEST_SESSION) -> dict:
    """subprocess env：继承当前 + 注入 CLAUDE_CODE_SESSION_ID（模拟 CC Stop-hook 子进程）。"""
    import os
    env = dict(os.environ)
    if session is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session
    else:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("ORCA_HOST_SESSION_ID", None)
    return env


def _write_active_marker_with_tape(
    runs: Path, run_id: str, host_session: str | None = _NUDGE_TEST_SESSION,
) -> None:
    """建活跃 marker + 对应 tape（host_session-binding v2：nudge 读 tape 首行派生归属）。

    marker 只 3 字段（无归属）；tape workflow_started.data.host_session 是单一真相源。
    """
    (runs / f"orca-{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "model": "deepseek", "no_output_count": 0}),
        encoding="utf-8",
    )
    ws = {"type": "workflow_started", "data": {"host_session": host_session}}
    (runs / f"{run_id}.jsonl").write_text(
        json.dumps(ws) + "\n", encoding="utf-8",
    )


@pytestmark_nudge_behavior
def test_cc_nudge_script_blocks_when_active_run(tmp_path: Path):
    """有活跃 marker **且归属当前 session** → emit ``decision: block``（v5 §4.4 + binding v2）。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "abc", host_session=_NUDGE_TEST_SESSION)
    script = _write_nudge_script(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_nudge_env(),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["decision"] == "block"
    assert "abc" in payload["reason"]
    assert "orca next" in payload["reason"]


@pytestmark_nudge_behavior
def test_cc_nudge_script_passes_when_no_active_run(tmp_path: Path):
    """无 marker → 静默放行（exit 0，无 stdout）——nudge 不该误报。"""
    script = _write_nudge_script(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_nudge_env(),
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


@pytestmark_nudge_behavior
def test_cc_nudge_script_fails_loud_on_malformed_marker(tmp_path: Path):
    """DEFECT-1 核心回归：marker 损坏（非合法 JSON）→ **fail loud**（stderr + exit 2）。

    host_session-binding v2：需设 env 让脚本走到 scan marker 路径（current 已解析）。
    旧版 ``jq ... 2>/dev/null || true`` 在此场景静默失败 → nudge 永不触发；用户看不到任何
    信号。新版必须把错误打到 stderr、exit 非零，让用户看到 orca 状态已乱。
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "orca-run-broken.json").write_text("{not json", encoding="utf-8")
    script = _write_nudge_script(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_nudge_env(),
    )
    assert proc.returncode != 0, "marker 损坏必须 fail loud（exit 非零），不得静默"
    assert proc.stderr, "fail loud 必须把错误写到 stderr"
    assert "marker" in proc.stderr or "JSON" in proc.stderr


@pytestmark_nudge_behavior
def test_cc_nudge_script_throttles_within_60s(tmp_path: Path):
    """60s 内第二次 Stop → 放行（不重复 block，防刷屏）。节流时间戳由首次 block 写。

    host_session-binding v2：STATE 按 session 分键（``.orca-nudge-cc-<session>``）。
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "xyz", host_session=_NUDGE_TEST_SESSION)
    state_file = runs / f".orca-nudge-cc-{_NUDGE_TEST_SESSION}"
    script = _write_nudge_script(tmp_path)

    first = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_nudge_env(),
    )
    assert first.returncode == 0
    assert json.loads(first.stdout.strip())["decision"] == "block"
    # review NIT#3：直接断言首次 block 写了节流时间戳（副作用锁死，不靠第二次隐式验证）。
    assert state_file.is_file(), "首次 block 必须写节流时间戳文件（per-session 分键）"
    assert state_file.read_text(encoding="utf-8").strip().isdigit(), (
        "节流时间戳内容必须是整数（now epoch seconds）"
    )

    second = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_nudge_env(),
    )
    assert second.returncode == 0
    assert second.stdout.strip() == "", "60s 窗口内第二次 Stop 应节流放行（无 block 输出）"


@pytestmark_nudge_behavior
def test_cc_nudge_script_passes_when_throttle_state_corrupt(tmp_path: Path):
    """节流时间戳文件内容非整数（损坏）→ 视作可再次 block，不崩（与旧版 case 容错同款）。

    host_session-binding v2：STATE 文件名含 session 后缀。
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "q", host_session=_NUDGE_TEST_SESSION)
    # 写一个非数字内容的时间戳文件（损坏态；per-session 分键文件名）
    (runs / f".orca-nudge-cc-{_NUDGE_TEST_SESSION}").write_text("garbage", encoding="utf-8")
    script = _write_nudge_script(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_nudge_env(),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["decision"] == "block"


def test_install_cac_family_full_set(isolated_home: Path):
    """cac target → cc 家族全套：skill + nudge Stop-hook（step 6：CAC≡cc，结构相同）。

    step 6 前 cac 只装 skill（零 nudge 覆盖）；本步补：``.cac/hooks/orca-nudge.sh`` +
    ``.cac/settings.json`` Stop hook 声明 + 无 plugins/command（opencode 家族专属）。
    """
    result = runner.invoke(app, ["--target", "cac", "--scope", "user"])
    assert result.exit_code == 0, result.output
    cac = isolated_home / ".cac"
    # skill
    assert (cac / "skills" / install_cmds.SKILL_NAME / "SKILL.md").is_file()
    assert (cac / "skills" / install_cmds.ENTRY_SKILL_NAME / "SKILL.md").is_file()
    # cc 家族 nudge：脚本 + settings.json Stop 声明
    assert (cac / "hooks" / "orca-nudge.sh").is_file()
    cfg = json.loads((cac / "settings.json").read_text())
    stop = cfg["hooks"]["Stop"]
    cmds = [h["command"] for entry in stop for h in entry["hooks"]]
    assert any("orca-nudge.sh" in c for c in cmds)
    # 意图断言：nudge 脚本落点在 .cac（CAC≡cc 落点对称，非 .claude）
    assert str(cac) in next(c for c in cmds if "orca-nudge.sh" in c)
    # cc 家族不装 plugin / command（opencode 家族专属）
    assert not (cac / "plugins").exists()
    assert not (cac / "command").exists()


def test_install_nga_family_full_set(isolated_home: Path):
    """nga target → opencode 家族全套：skill + plugin orca.ts + opencode.json 声明（step 6：NGA≡opencode）。

    step 6 前 nga 只装 skill；本步补 plugin + json 声明（路径指 ``.nga``）。
    """
    result = runner.invoke(app, ["--target", "nga", "--scope", "user"])
    assert result.exit_code == 0, result.output
    nga = isolated_home / ".nga"
    # skill
    assert (nga / "skills" / install_cmds.SKILL_NAME / "SKILL.md").is_file()
    assert (nga / "skills" / install_cmds.ENTRY_SKILL_NAME / "SKILL.md").is_file()
    # opencode 家族 plugin + json 声明
    assert (nga / "plugins" / "orca.ts").is_file()
    cfg = json.loads((nga / "opencode.json").read_text())
    # 用户 scope 声明用绝对路径（指向 .nga，非 .opencode）
    orca_decls = [p for p in cfg["plugin"] if "orca.ts" in p]
    assert len(orca_decls) == 1, f"nga 应恰好一条 orca 声明: {orca_decls}"
    decl = orca_decls[0]
    assert decl.startswith("/"), f"用户 scope 声明应为绝对路径: {decl}"
    assert "/.nga/plugins/orca.ts" in decl, f"nga 声明应指向 .nga: {decl}"


def test_install_nga_project_scope_uses_dotnga_relative(
    isolated_home: Path, isolated_cwd: Path
):
    """nga ``--scope project``：cwd 根 ``opencode.json`` plugin 声明须含 ``./.nga/plugins/orca.ts``。

    **step 6 泛化闸门**（spec-reviewer #1/#2 关键）：``_opencode_plugin_decl`` project scope
    走 ``f"./{hr.root.name}/plugins/orca.ts"``，``hr.root.name`` 由 resolve_roots 派生。user scope
    走绝对路径（本就 root-relative），**不改泛化也能过** → 必须 project scope 测才抓得住泛化 bug
    （若泛化退回硬编码 ``.opencode``，本测试断言 ``./.nga/...`` 会 fail）。
    """
    result = runner.invoke(app, ["--target", "nga", "--scope", "project"])
    assert result.exit_code == 0, result.output
    assert (isolated_cwd / ".nga" / "plugins" / "orca.ts").is_file()
    cfg_path = isolated_cwd / "opencode.json"
    assert cfg_path.is_file(), "项目 scope opencode.json 应在 cwd 根"
    cfg = json.loads(cfg_path.read_text())
    assert "./.nga/plugins/orca.ts" in cfg["plugin"], (
        f"nga project-scope 声明应为 ./.nga/plugins/orca.ts（_opencode_plugin_decl 泛化闸门），实际: {cfg['plugin']}"
    )
    # 同时确认旧硬编码 .opencode 路径不在声明里（防泛化漏改）
    assert not any(".opencode" in p for p in cfg["plugin"]), (
        f"nga 声明不应含 .opencode（_opencode_plugin_decl 泛化应去硬编码）: {cfg['plugin']}"
    )


def test_install_cac_nudge_idempotent_no_duplicate(isolated_home: Path):
    """cac nudge 重跑：settings.json Stop 不重复加 orca nudge 声明（与 cc 同款幂等）。"""
    for _ in range(2):
        r = runner.invoke(app, ["--target", "cac", "--scope", "user"])
        assert r.exit_code == 0, r.output
    cfg = json.loads((isolated_home / ".cac" / "settings.json").read_text())
    stop = cfg["hooks"]["Stop"]
    orca_entries = [
        entry for entry in stop
        if isinstance(entry, dict)
        and any("orca-nudge" in str(h.get("command", "")) for h in entry.get("hooks", []))
    ]
    assert len(orca_entries) == 1, f"cac nudge Stop 声明重复: {orca_entries}"


def test_install_nga_idempotent_no_duplicate(isolated_home: Path):
    """nga plugin 声明重跑：opencode.json 不重复加 orca 声明。

    nga 的 plugin_decl 经 ``hr.root.name`` 派生（比 opencode 绝对路径多一层间接），
    补此测试锁死该间接层的幂等性（与 opencode 同款 dedup 对称）。
    """
    for _ in range(2):
        r = runner.invoke(app, ["--target", "nga", "--scope", "user"])
        assert r.exit_code == 0, r.output
    cfg = json.loads((isolated_home / ".nga" / "opencode.json").read_text())
    orca_entries = [p for p in cfg["plugin"] if "orca.ts" in p]
    assert len(orca_entries) == 1, f"nga orca 声明重复: {orca_entries}"


def test_install_no_benchmark(isolated_home: Path):
    """守门：benchmark/（评测答案）绝不装到用户目录。"""
    runner.invoke(app, ["--target", "cc", "--scope", "user"])
    skill = isolated_home / ".claude" / "skills" / install_cmds.SKILL_NAME
    assert not (skill / "benchmark").exists(), "install 不应拷 benchmark/"


# ── fail loud + 模板内容 ──────────────────────────────────────────────────────


def test_install_fail_loud(isolated_home: Path, monkeypatch: pytest.MonkeyPatch):
    """copytree 失败 → exit 1 + 报路径（铁律 12，不静默吞错）。"""

    def _boom(*_a, **_k):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(install_cmds.shutil, "copytree", _boom)
    result = runner.invoke(app, ["--target", "cc", "--scope", "user"])
    assert result.exit_code == 1
    assert "simulated" in result.output or "失败" in result.output


def test_install_plugin_content_matches_bundle(isolated_home: Path):
    """落地 plugin 内容 = 随包模板（防 install 写错 / 漂移版本）。

    v5 step 2b(5)：command 模板已删，不再比对 command 命名空间；只比对 plugin + skill。
    """
    runner.invoke(app, ["--target", "opencode", "--scope", "user"])
    oc = isolated_home / ".config" / "opencode"
    assert (oc / "plugins" / "orca.ts").read_text() == install_cmds._opencode_plugin_src().read_text()
    # 随包所有 skill 都落地，内容 = 源
    bundled = install_cmds._bundled_skill_sources()
    assert bundled, "随包应至少有一个 skill 源"
    for src in bundled:
        dst = oc / "skills" / src.name
        assert (dst / "SKILL.md").is_file()
        assert (dst / "SKILL.md").read_text() == (src / "SKILL.md").read_text()


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


def test_install_bundled_workflows_deploys_cwd_to_global(tmp_path, monkeypatch):
    """``_install_bundled_workflows``：CWD/workflows/*.yaml + agents/ → ~/.orca/workflows。

    部署 + 内容一致 + yaml 幂等（内容同跳过）+ 变更 refresh（覆盖）+ agents 池随 yaml 同步
    （agent 解析按 <workflow_dir>/agents/ 找，不拷会 agent not found）+ 无 CWD/workflows no-op。
    """
    cwd = tmp_path / "proj"
    (cwd / "workflows").mkdir(parents=True)
    wf_src = cwd / "workflows" / "demo-wf.yaml"
    wf_src.write_text("name: demo-wf\ndescription: test\n", encoding="utf-8")
    agent_src = cwd / "workflows" / "agents" / "demo-agent"
    agent_src.mkdir(parents=True)
    (agent_src / "agent.md").write_text("# demo-agent\n", encoding="utf-8")
    (agent_src / "__pycache__").mkdir()
    (agent_src / "__pycache__" / "x.pyc").write_text("junk", encoding="utf-8")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(fake_home))  # Path.home() 走 $HOME（POSIX）

    # 首次部署：yaml + agents 池都落地
    deployed = install_cmds._install_bundled_workflows()
    assert [p.name for p in deployed] == ["demo-wf.yaml", "agents"]
    dst = fake_home / ".orca" / "workflows" / "demo-wf.yaml"
    assert dst.is_file()
    assert dst.read_text(encoding="utf-8") == wf_src.read_text(encoding="utf-8")
    agents_dst = fake_home / ".orca" / "workflows" / "agents"
    assert (agents_dst / "demo-agent" / "agent.md").is_file()
    assert not (agents_dst / "demo-agent" / "__pycache__").exists()

    # yaml 幂等：内容同 → 跳过（agents 树仍覆盖同步）
    assert [p.name for p in install_cmds._install_bundled_workflows()] == ["agents"]

    # 变更 → refresh（覆盖）
    wf_src.write_text("name: demo-wf\ndescription: changed\n", encoding="utf-8")
    deployed3 = install_cmds._install_bundled_workflows()
    assert [p.name for p in deployed3] == ["demo-wf.yaml", "agents"]
    assert "changed" in dst.read_text(encoding="utf-8")

    # 无 CWD/workflows → no-op
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert install_cmds._install_bundled_workflows() == []


# ── _install_bundled_subagents（plan v5 §9.2 拓扑映射）────────────────────────


def test_install_bundled_subagents_maps_dirname_to_target(tmp_path, monkeypatch):
    """``_install_bundled_subagents``：workflows/_<name>_subagents/*.md → ~/.orca/<name>/subagents。

    覆盖意图（非仅行为）：
      - **拓扑映射**：``_nas-supernet_subagents`` → ``nas-supernet``（strip 首 ``_`` 尾
        ``_subagents``，plan §9.2 契约——node Bash 按 ``$HOME/.orca/nas-supernet/subagents/``
        读，映射错位会让 read+embed 协议读不到 body）。
      - 5 个真实 subagent body 全部部署（nas-supernet 的依赖完整性）。
      - 多 workflow 目录并发部署（OCP：加 ``_<other>_subagents/`` 自动捡，零核心改动）。
      - 空目录容错（无 *.md → no-op 该目录，不 fail）。
      - 幂等（内容同跳过）+ 变更 refresh（覆盖）+ 内容逐字一致（read+embed 要原文 body）。
      - 无 CWD/workflows → no-op（非仓库根跑 install 不报错）。
    """
    cwd = tmp_path / "proj"
    sa_src = cwd / "workflows" / "_nas-supernet_subagents"
    sa_src.mkdir(parents=True)
    bodies = {
        "supernet-evaluator.md": "# Supernet Evaluator\nbody A\n",
        "workflow-verifier.md": "# Workflow Verifier\nbody B\n",
        "memory-verifier.md": "# Memory Verifier\nbody C\n",
        "project-porter.md": "# Project Porter\nbody D\n",
        "project-fidelity-verifier.md": "# Project Fidelity Verifier\nbody E\n",
    }
    for name, content in bodies.items():
        (sa_src / name).write_text(content, encoding="utf-8")
    # 第二个 workflow 的 subagent 目录（验证多目录映射 + OCP 加目录零核心改动）
    other_src = cwd / "workflows" / "_other-wf_subagents"
    other_src.mkdir()
    (other_src / "helper.md").write_text("# Helper\n", encoding="utf-8")
    # 空目录（无 *.md）→ 容错 no-op
    (cwd / "workflows" / "_empty-wf_subagents").mkdir()

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.chdir(cwd)
    # Path.home 直接打桩——比 setenv HOME 更可靠（Windows 下 Path.home 走 USERPROFILE 不走 HOME）
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    deployed = install_cmds._install_bundled_subagents()
    # 5 (nas-supernet) + 1 (other-wf) = 6 文件部署；空目录 no-op
    assert len(deployed) == 6, f"期望 6 个部署，实际 {len(deployed)}: {[p.name for p in deployed]}"
    # 拓扑映射：_nas-supernet_subagents → ~/.orca/nas-supernet/subagents/
    ns_dst = fake_home / ".orca" / "nas-supernet" / "subagents"
    for name, content in bodies.items():
        dst_file = ns_dst / name
        assert dst_file.is_file(), f"{name} 应部署到 {dst_file}"
        assert dst_file.read_text(encoding="utf-8") == content, f"{name} body 应逐字一致"
    # 第二 workflow 映射：_other-wf_subagents → ~/.orca/other-wf/subagents/
    assert (fake_home / ".orca" / "other-wf" / "subagents" / "helper.md").is_file()
    # 空目录无落点
    assert not (fake_home / ".orca" / "empty-wf").exists()

    # 幂等：再跑 → 全部跳过（内容同）
    assert install_cmds._install_bundled_subagents() == [], "内容一致的 subagent 再跑应全跳过"

    # 变更 → refresh（覆盖）
    (sa_src / "supernet-evaluator.md").write_text("# Supernet Evaluator v2\n", encoding="utf-8")
    deployed3 = install_cmds._install_bundled_subagents()
    assert len(deployed3) == 1 and deployed3[0].name == "supernet-evaluator.md"
    assert "v2" in (ns_dst / "supernet-evaluator.md").read_text(encoding="utf-8")

    # 无 CWD/workflows → no-op
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert install_cmds._install_bundled_subagents() == []


def test_install_bundled_subagents_skips_degenerate_dirname(tmp_path, monkeypatch):
    """非法中间名（空 / 路径穿透片段）→ warn + skip（不中断 install，stderr 可见；无落点）。

    意图：install 是部署步骤，不该因用户随手建的怪目录硬失败；但必须 stderr warn（可见，
    符合 fail loud 铁律——不静默吞），且**绝不为非法名创建落点目录**（防 ``..`` 把落点
    逃出 ``~/.orca/<name>/`` 的路径穿透 footgun）。用 CliRunner 捕 ``typer.echo(..., err=True)``
    的输出（CliRunner mix 进 result.output）。覆盖两类非法名：
      - ``__subagents``（中间名空 ``''``——退化名）
      - ``_.._subagents``（中间名 ``'..'``——路径穿透，落点会变成 ``~/.orca/../subagents``）
    """
    cwd = tmp_path / "proj"
    empty_dir = cwd / "workflows" / "__subagents"  # strip 首 _ 尾 _subagents → 中间名空
    empty_dir.mkdir(parents=True)
    (empty_dir / "x.md").write_text("# x\n", encoding="utf-8")
    traversal_dir = cwd / "workflows" / "_.._subagents"  # 中间名 '..' → 路径穿透
    traversal_dir.mkdir()
    (traversal_dir / "y.md").write_text("# y\n", encoding="utf-8")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    result = runner.invoke(app, ["--target", "cc", "--scope", "user"])
    # install 整体不崩（exit 0），subagent 部署对两个非法目录都 warn + skip
    assert result.exit_code == 0, result.output
    assert "非法 subagent 目录名" in result.output
    assert "中间名 ''" in result.output  # __subagents → 空串
    assert "'..'" in result.output  # _.._subagents → 路径穿透片段
    # 关键安全断言：绝不为非法名创建任何 ~/.orca 落点（``.orca`` 目录都不应存在）
    assert not (fake_home / ".orca").exists(), (
        "非法中间名不应产生任何 ~/.orca 落点（防路径穿透）"
    )


def test_install_bundled_subagents_warns_on_per_file_copy_failure(
    tmp_path, monkeypatch
):
    """单文件 ``shutil.copy2`` 抛 OSError → warn + continue（per-file fail-soft 契约承重墙）。

    意图（Rule 9）：per-file fail-soft——一个 subagent copy 失败**不得**中断其他文件部署，
    也**不得**静默（stderr warn 可见 = 符合 fail loud）；失败文件不计入返回值，成功文件
    正常落地。与 ``_install_bundled_workflows`` 的 per-file OSError fail-soft 同款契约。
    """
    cwd = tmp_path / "proj"
    sa_src = cwd / "workflows" / "_nas-supernet_subagents"
    sa_src.mkdir(parents=True)
    (sa_src / "good-a.md").write_text("# A\n", encoding="utf-8")
    (sa_src / "bad.md").write_text("# B\n", encoding="utf-8")
    (sa_src / "good-c.md").write_text("# C\n", encoding="utf-8")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    real_copy2 = install_cmds.shutil.copy2

    def flaky_copy2(src, dst, *, follow_symlinks=True):
        if Path(src).name == "bad.md":
            raise OSError("simulated permission denied")
        return real_copy2(src, dst, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(install_cmds.shutil, "copy2", flaky_copy2)

    deployed = install_cmds._install_bundled_subagents()
    # bad.md 失败 warn+skip；good-a / good-c 仍部署（fail-soft：单点失败不中断）
    assert sorted(p.name for p in deployed) == ["good-a.md", "good-c.md"], (
        f"失败文件应排除，成功文件应保留: {[p.name for p in deployed]}"
    )
    dst_dir = fake_home / ".orca" / "nas-supernet" / "subagents"
    assert (dst_dir / "good-a.md").is_file()
    assert (dst_dir / "good-c.md").is_file()
    assert not (dst_dir / "bad.md").exists()


def test_install_bundled_subagents_noop_without_workflows_dir(tmp_path, monkeypatch):
    """无 CWD/workflows 目录 → no-op 返回 []（非仓库根跑 install 不报错，幂等安全）。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert install_cmds._install_bundled_subagents() == []


# ── PostToolUse 事后告警守卫（SPEC docs/specs/posttooluse-rogue-guard.md）─────────
#
# 新增能力：cc_nudge.sh 单脚本双事件（Stop + PostToolUse）+ orca.ts tool.execute.after
# 钩子 + tool-classification.json 单一真相源。下方覆盖 SPEC §11.1/§11.2/§11.4 验收。

# PostToolUse 测试专用 session id（与 _NUDGE_TEST_SESSION 同款，便于复用 marker helper）。
_GUARD_TEST_SESSION = "cc-session-guard-xyz"


def _guard_payload(tool_name: str, *, command: str | None = None,
                   session_id: str | None = _GUARD_TEST_SESSION) -> str:
    """构造 CC PostToolUse hook stdin JSON（SPEC §7.1 输入契约）。"""
    payload: dict = {"hook_event_name": "PostToolUse", "tool_name": tool_name}
    if command is not None:
        payload["tool_input"] = {"command": command}
    else:
        payload["tool_input"] = {}
    if session_id is not None:
        payload["session_id"] = session_id
    return json.dumps(payload)


def _guard_env(session: str | None = _GUARD_TEST_SESSION) -> dict:
    """subprocess env：注入 CLAUDE_CODE_SESSION_ID（模拟 CC hook 子进程 env 链，§10 R5）。"""
    import os
    env = dict(os.environ)
    if session is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session
    else:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("ORCA_HOST_SESSION_ID", None)
    return env


def _write_classification_next_to_script(script_dir: Path) -> Path:
    """拷随包 tool-classification.json 到脚本同目录（install 时 install_cmds 也这么做）。"""
    src = install_cmds._tool_classification_src()
    dst = script_dir / "tool-classification.json"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


@pytestmark_nudge_behavior
def test_cc_guard_emits_additionalcontext_for_write(tmp_path: Path):
    """SPEC §11.2：PostToolUse + 有活跃 run（本 session）+ Write → stdout 含 additionalContext，
    不含 decision/permissionDecision，exit 0（非 2）—— pure hint 契约。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "run1", host_session=_GUARD_TEST_SESSION)
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Write"), capture_output=True, text=True,
        timeout=10, env=_guard_env(),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert "hookSpecificOutput" in out
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    additional = out["hookSpecificOutput"]["additionalContext"]
    assert "run1" in additional
    assert "Write" in additional
    # pure hint：绝不 emit decision/permissionDecision（§11.4 结构化断言）
    assert "decision" not in out
    assert "permissionDecision" not in out


@pytestmark_nudge_behavior
def test_cc_guard_silent_for_readonly_bash(tmp_path: Path):
    """SPEC §11.2：只读 Bash（ls / cat / git log）→ 静默（无 stdout）。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "run1", host_session=_GUARD_TEST_SESSION)
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)
    for cmd in ("ls", "cat README.md", "git log", "git status", "git diff", "rg foo"):
        proc = subprocess.run(
            ["bash", str(script)], cwd=tmp_path,
            input=_guard_payload("Bash", command=cmd), capture_output=True, text=True,
            timeout=10, env=_guard_env(),
        )
        assert proc.returncode == 0, (cmd, proc.stderr)
        assert proc.stdout.strip() == "", f"只读 Bash {cmd!r} 应静默，实际 stdout: {proc.stdout}"


@pytestmark_nudge_behavior
def test_cc_guard_silent_for_orca_next_bash(tmp_path: Path):
    """SPEC §11.2：``orca next`` Bash（正确推进）→ 静默（不告警）。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "run1", host_session=_GUARD_TEST_SESSION)
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Bash", command="orca next --run-id run1 --output done"),
        capture_output=True, text=True, timeout=10, env=_guard_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


@pytestmark_nudge_behavior
def test_cc_guard_emits_for_writing_bash(tmp_path: Path):
    """SPEC §11.2：非只读 Bash（``python train.py``）→ stdout 含 additionalContext。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "run1", host_session=_GUARD_TEST_SESSION)
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Bash", command="python train.py"),
        capture_output=True, text=True, timeout=10, env=_guard_env(),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert "additionalContext" in out["hookSpecificOutput"]
    assert "Bash" in out["hookSpecificOutput"]["additionalContext"]


@pytestmark_nudge_behavior
@pytest.mark.parametrize("sep", [";", "&&", "||", "|", "&", "\n", ">", ">>"])
def test_cc_guard_emits_for_compound_command(tmp_path: Path, sep: str):
    """SPEC §11.2 / §5 Bash 分类 1：复合命令（含任一分隔符）→ 告警（E1 验收 + review 🟢#1 全分隔符覆盖）。

    review 🟡#2：``>`` / ``>>`` 重定向加入分隔符集——``cat foo > /etc/passwd`` 是潜在 rogue 路径，
    不该被 ``cat`` readonly 前缀放行（重定向即写文件语义）。
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "run1", host_session=_GUARD_TEST_SESSION)
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)
    cmd = f"git log{sep}python train.py"
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Bash", command=cmd),
        capture_output=True, text=True, timeout=10, env=_guard_env(),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert "additionalContext" in out["hookSpecificOutput"], (
        f"复合命令 sep={sep!r} 应告警"
    )


@pytestmark_nudge_behavior
def test_cc_guard_emits_for_edit_direct_hit(tmp_path: Path):
    """SPEC §11.2 / review §五-6：``Edit`` 工具直接命中 ``writing_tools`` ——与 Write 同语义，
    单独 case 锁住，避免集合相等测（E11）掩盖个体 bug。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "run1", host_session=_GUARD_TEST_SESSION)
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Edit"), capture_output=True, text=True,
        timeout=10, env=_guard_env(),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert "additionalContext" in out["hookSpecificOutput"]
    assert "Edit" in out["hookSpecificOutput"]["additionalContext"]


@pytestmark_nudge_behavior
def test_cc_guard_failopen_on_malformed_marker(tmp_path: Path):
    """SPEC §7.3 / §11.4：PostToolUse + malformed marker → exit 0 + 静默 + stderr warn（**绝不 exit 2**）。

    review §四-1 must-fix：原 ``_scan_my_active_run_ids`` 在 marker 损坏时 ``sys.exit(2)``，
    会经 PostToolUse 路径泄漏成 exit 2——违反 SPEC §7.3 纯提示铁律。Stop 路径仍 fail loud
    （marker 真相源契约）；guard 路径用 ``strict=False`` fail-open（与 hook 本地 best-effort 态对称）。
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "run1", host_session=_GUARD_TEST_SESSION)
    # 在合法 marker 之间混入损坏 marker（模拟 atomic_write 半成品残留）
    (runs / "orca-broken.json").write_text("{not valid json", encoding="utf-8")
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Write"), capture_output=True, text=True,
        timeout=10, env=_guard_env(),
    )
    # 纯提示铁律：guard 路径绝不 exit 2——即便 marker 损坏。
    assert proc.returncode == 0, (
        f"PostToolUse guard 必须 fail-open（exit 0），实际 exit {proc.returncode}；"
        f"stderr: {proc.stderr}"
    )
    # 仍应从其他合法 marker 命中并告警（损坏的 skip + warn，不阻断主流程）
    out = json.loads(proc.stdout.strip())
    assert "additionalContext" in out["hookSpecificOutput"]
    # stderr 应有 warn（不是静默吞错——与 hook 本地态 fail-open + warn 对称）
    assert "broken" in proc.stderr or "不可读" in proc.stderr, (
        f"malformed marker 应 stderr warn，实际 stderr: {proc.stderr!r}"
    )


@pytestmark_nudge_behavior
def test_cc_guard_word_boundary_lsof_not_mistaken_for_ls(tmp_path: Path):
    """SPEC §11.2 / §5 E6：word-boundary——``lsof`` 不该被 ``ls`` 误命中（应告警）。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "run1", host_session=_GUARD_TEST_SESSION)
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Bash", command="lsof -i"),
        capture_output=True, text=True, timeout=10, env=_guard_env(),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert "additionalContext" in out["hookSpecificOutput"]


@pytestmark_nudge_behavior
def test_cc_guard_silent_when_no_active_run(tmp_path: Path):
    """SPEC §11.2：无活跃 run + Write → 静默。"""
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)
    (tmp_path / "runs").mkdir()
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Write"), capture_output=True, text=True,
        timeout=10, env=_guard_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


@pytestmark_nudge_behavior
def test_cc_guard_silent_when_run_belongs_to_other_session(tmp_path: Path):
    """SPEC §11.2：run 归属他 session + Write → 静默（host_session 隔离）。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "run1", host_session="other-session-zzz")
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Write"), capture_output=True, text=True,
        timeout=10, env=_guard_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


@pytestmark_nudge_behavior
def test_cc_guard_throttles_within_30s(tmp_path: Path):
    """SPEC §11.2 / §4.3：30s 内重复 Write → 仅第一次有 stdout（guard 节流，与 nudge 分键）。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "run1", host_session=_GUARD_TEST_SESSION)
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)
    guard_state = runs / f".orca-guard-cc-{_GUARD_TEST_SESSION}"

    first = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Write"), capture_output=True, text=True,
        timeout=10, env=_guard_env(),
    )
    assert first.returncode == 0
    assert "additionalContext" in json.loads(first.stdout.strip())["hookSpecificOutput"]
    # 节流文件名 = runs/.orca-guard-cc-<session>.json（SPEC §4.3 分键，与 .orca-nudge-cc- 互不影响）
    assert guard_state.is_file(), "首次告警必须写 guard 节流时间戳（per-session 分键）"

    second = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Write"), capture_output=True, text=True,
        timeout=10, env=_guard_env(),
    )
    assert second.returncode == 0
    assert second.stdout.strip() == "", "30s 窗内第二次 Write 应节流静默"


@pytestmark_nudge_behavior
def test_cc_guard_throttle_key_independent_from_nudge(tmp_path: Path):
    """SPEC §4.3：guard 30s 节流与 nudge 60s 节流分键，互不影响。

    意图：guard 节流写过 .orca-guard-cc-<sid> 后，nudge 仍可触发（.orca-nudge-cc-<sid> 不存在）。
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "run1", host_session=_GUARD_TEST_SESSION)
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)

    # 先触发 guard（写 .orca-guard-cc-<sid>）
    g = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Write"), capture_output=True, text=True,
        timeout=10, env=_guard_env(),
    )
    assert "additionalContext" in json.loads(g.stdout.strip())["hookSpecificOutput"]
    # nudge 节流文件不存在（分键）
    assert not (runs / f".orca-nudge-cc-{_GUARD_TEST_SESSION}").exists()
    assert (runs / f".orca-guard-cc-{_GUARD_TEST_SESSION}").exists()


@pytestmark_nudge_behavior
def test_cc_stop_branch_byte_identical_regression(tmp_path: Path):
    """SPEC §11.2 Stop 分支回归：Stop mock stdin → stdout 字节级 == pre-change golden。

    golden 在本次改动前捕获（commit pre-posttooluse-rogue-guard，详见 _fixtures/cc_stop_golden.json）：
    单一活跃 run abc + session cc-session-test-abc → decision:block + 完整 reason 文本。
    本测锁 Stop 分支字节级不变（防 refactoring 误改）+ 不泄漏 PostToolUse 字段。
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "abc", host_session=_NUDGE_TEST_SESSION)
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)
    stop_payload = json.dumps({"hook_event_name": "Stop"})
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=stop_payload, capture_output=True, text=True,
        timeout=10, env=_nudge_env(),
    )
    assert proc.returncode == 0, proc.stderr
    # 字节级 golden（review 🟡#1）：stdout == fixture 文件内容 + 换行
    golden_path = Path(__file__).parent / "_fixtures" / "cc_stop_golden.json"
    expected = golden_path.read_text(encoding="utf-8").rstrip("\n")
    assert proc.stdout.rstrip("\n") == expected, (
        f"Stop 分支 stdout 不等于 golden：\n--got--\n{proc.stdout!r}\n--golden--\n{expected!r}\n"
    )
    # 双保险：解析后字段集 + 不泄漏 PostToolUse 字段
    out = json.loads(proc.stdout.strip())
    assert set(out.keys()) == {"decision", "reason"}
    assert out["decision"] == "block"
    # 60s 节流文件名不变（v5 §4.4 分键，guard 不改 nudge 行为）
    assert (runs / f".orca-nudge-cc-{_NUDGE_TEST_SESSION}").is_file()


@pytestmark_nudge_behavior
def test_cc_guard_silent_when_classification_missing(tmp_path: Path):
    """SPEC §10 / fail-open：tool-classification.json 缺失 → PostToolUse guard 不分类（不告警），
    stderr warn（不静默）；不影响 Stop 路径。install 出错时不应让用户 session 卡死。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "run1", host_session=_GUARD_TEST_SESSION)
    script = _write_nudge_script(tmp_path)
    # 故意不拷 classification
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Write"), capture_output=True, text=True,
        timeout=10, env=_guard_env(),
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "", "classification 缺失应不告警（fail-open）"
    assert "tool-classification" in proc.stderr or "classification" in proc.stderr, (
        "缺失应 stderr warn（不静默吞）"
    )


@pytestmark_nudge_behavior
def test_cc_guard_session_id_fallback_when_env_missing(tmp_path: Path):
    """SPEC §10 R5 fallback：env 未注入 CLAUDE_CODE_SESSION_ID → 从 stdin JSON.session_id 取
    host_session；分类命中 + 活跃 run → 仍告警。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "run1", host_session=_GUARD_TEST_SESSION)
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)
    # env 不注入 → _host_session_from_env() 返 None；fallback 取 payload.session_id
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Write", session_id=_GUARD_TEST_SESSION),
        capture_output=True, text=True, timeout=10,
        env=_guard_env(session=None),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert "additionalContext" in out["hookSpecificOutput"]


@pytestmark_nudge_behavior
def test_cc_guard_unbound_heartbeat_when_no_session_anywhere(tmp_path: Path):
    """SPEC §10 R1/R5 fail-safe：env + stdin 都取不到 session → 写 runs/.orca-guard-unbound.json
    心跳 + 放行（不告警，不抛错）。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_active_marker_with_tape(runs, "run1", host_session=_GUARD_TEST_SESSION)
    script = _write_nudge_script(tmp_path)
    _write_classification_next_to_script(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        input=_guard_payload("Write", session_id=None),
        capture_output=True, text=True, timeout=10,
        env=_guard_env(session=None),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""
    heartbeat = runs / ".orca-guard-unbound.json"
    assert heartbeat.is_file(), "无 session 时应写 unbound 心跳（doctor 诊断信号）"


# ── §11.1 四前端安装对称（PostToolUse 守卫新增）─────────────────────────────────


def test_install_cc_family_registers_posttooluse_hook(isolated_home: Path):
    """SPEC §11.1：cc/cac settings.json 注册 hooks.PostToolUse（matcher 锚定 + command 含 orca-nudge）。"""
    for target, dot in (("cc", ".claude"), ("cac", ".cac")):
        isolated_home_pkg = isolated_home if target == "cc" else isolated_home
        result = runner.invoke(app, ["--target", target, "--scope", "user"])
        assert result.exit_code == 0, result.output
        root = isolated_home_pkg / dot
        cfg = json.loads((root / "settings.json").read_text())
        ptu = cfg["hooks"]["PostToolUse"]
        orca_entry = next(
            (e for e in ptu if isinstance(e, dict)
             and any("orca-nudge" in str(h.get("command", "")) for h in e.get("hooks", []))),
            None,
        )
        assert orca_entry is not None, f"{target} PostToolUse 未注册 orca-nudge"
        assert orca_entry["matcher"] == "^(Write|Edit|NotebookEdit|Bash|PowerShell)$", (
            f"{target} PostToolUse matcher 应锚定 §7.2 工具集"
        )
        # tool-classification.json 同目录落地（PostToolUse 分支启动时 read）
        assert (root / "hooks" / "tool-classification.json").is_file()


def test_install_opencode_family_copies_tool_classification(isolated_home: Path):
    """SPEC §11.1：opencode/nga plugins/ 下 tool-classification.json 与 orca.ts 同目录。"""
    for target in ("opencode", "nga"):
        result = runner.invoke(app, ["--target", target, "--scope", "user"])
        assert result.exit_code == 0, result.output
        root = {
            "opencode": isolated_home / ".config" / "opencode",
            "nga": isolated_home / ".nga",
        }[target]
        assert (root / "plugins" / "tool-classification.json").is_file(), (
            f"{target} plugins/tool-classification.json 应与 orca.ts 同目录落地"
        )
        # 内容 = 随包单一真相源
        bundled = install_cmds._tool_classification_src().read_text(encoding="utf-8")
        assert (root / "plugins" / "tool-classification.json").read_text() == bundled


def test_install_cc_posttooluse_idempotent_no_duplicate(isolated_home: Path):
    """SPEC §11.1 幂等：重跑 cc install，PostToolUse 不重复加 orca-nudge 条目。"""
    for _ in range(2):
        r = runner.invoke(app, ["--target", "cc", "--scope", "user"])
        assert r.exit_code == 0, r.output
    cfg = json.loads((isolated_home / ".claude" / "settings.json").read_text())
    ptu = cfg["hooks"]["PostToolUse"]
    orca_entries = [
        e for e in ptu if isinstance(e, dict)
        and any("orca-nudge" in str(h.get("command", "")) for h in e.get("hooks", []))
    ]
    assert len(orca_entries) == 1, f"PostToolUse orca-nudge 条目重复: {orca_entries}"


def test_install_cc_posttooluse_preserves_user_existing(isolated_home: Path):
    """SPEC §11.1 合并友好：用户已有 PostToolUse（其他 matcher）保留 + orca 追加。"""
    cc = isolated_home / ".claude"
    cc.mkdir(parents=True)
    (cc / "settings.json").write_text(json.dumps({
        "hooks": {
            "PostToolUse": [
                {"matcher": "Read", "hooks": [{"type": "command", "command": "echo read-hook"}]},
            ],
        },
    }))
    result = runner.invoke(app, ["--target", "cc", "--scope", "user"])
    assert result.exit_code == 0, result.output
    cfg = json.loads((cc / "settings.json").read_text())
    ptu = cfg["hooks"]["PostToolUse"]
    matchers = [e.get("matcher") for e in ptu]
    assert "Read" in matchers, "用户已有 PostToolUse 应保留"
    assert "^(Write|Edit|NotebookEdit|Bash|PowerShell)$" in matchers


def test_four_frontend_trigger_tool_set_equal(isolated_home: Path, tmp_path: Path):
    """SPEC §11.1 E11：cc/cac settings.json matcher 工具集 ≡ opencode/nga orca.ts 分类工具集。

    cc/cac matcher = ^(Write|Edit|NotebookEdit|Bash|PowerShell)$
    opencode/nga orca.ts = writing_tools + bash_tools（tool-classification.json）
    两家族对「下场干活」工具的判定面应一致（CC 用 matcher 预过滤 + 脚本分类；opencode 全靠分类）。
    """
    import re as _re
    # CC matcher 工具集（来自 install 后的 settings.json）
    runner.invoke(app, ["--target", "cc", "--scope", "user"])
    cc_cfg = json.loads((isolated_home / ".claude" / "settings.json").read_text())
    matcher = next(
        e["matcher"] for e in cc_cfg["hooks"]["PostToolUse"]
        if any("orca-nudge" in str(h.get("command", "")) for h in e.get("hooks", []))
    )
    # matcher 形如 ^(Write|Edit|...|PowerShell)$ —— 剥 ^(...) $ 取 | 分隔的工具集
    inner = _re.match(r"^\^\((.+)\)\$$", matcher).group(1)
    cc_tools = set(inner.split("|"))

    # opencode 分类工具集（writing_tools + bash_tools，来自 tool-classification.json）
    cls = json.loads(install_cmds._tool_classification_src().read_text(encoding="utf-8"))
    # 仅取 opencode 小写形态（write/edit + bash），排除 CC PascalCase
    oc_writing = {t for t in cls["writing_tools"] if not t[0].isupper()}
    oc_bash = {t for t in cls["bash_tools"] if not t[0].isupper()}
    oc_tools = oc_writing | oc_bash

    # cc_tools = {Write, Edit, NotebookEdit, Bash, PowerShell}（PascalCase）
    # 对应 opencode 小写：{write, edit, bash}（NotebookEdit/PowerShell 是 CC 特有，无 opencode 对应）
    cc_lowered = {t.lower() for t in cc_tools} - {"notebookedit", "powershell"}
    assert cc_lowered == oc_tools, (
        f"四前端触发工具集不一致（E11）：cc(去 NoteBook/PS)={cc_lowered} opencode={oc_tools}"
    )


# ── §11.4 守门（架构 + 纯提示 + 单一真相源）────────────────────────────────────


def test_cc_nudge_decision_block_count_at_baseline(isolated_home: Path):
    """SPEC §11.4：模板内 ``"decision": "block"`` 总出现次数 ≤ 基线（Stop 分支合法含 1 处）。

    PostToolUse 分支不得新增 decision:block（pure hint 契约）。
    """
    runner.invoke(app, ["--target", "cc", "--scope", "user"])
    script = (isolated_home / ".claude" / "hooks" / "orca-nudge.sh").read_text()
    count = len(re.findall(r'"decision"\s*:\s*"block"', script))
    assert count == 1, f"decision:block 出现 {count} 次，基线应为 1（仅 Stop 分支）"


def test_cc_nudge_posttooluse_no_self_orca_next_call(isolated_home: Path):
    """SPEC §11.4 B 路径铁律：模板/plugin 不得自调 ``orca next``。PostToolUse 分支也不得新增。

    正则禁令（与现有 test_install_cc_nudge_script_never_calls_next 同款）：行首裸 orca 命令 /
    ``$(orca`` / 反引号。PostToolUse 分支共享同一脚本，规则继承。
    """
    runner.invoke(app, ["--target", "cc", "--scope", "user"])
    script = (isolated_home / ".claude" / "hooks" / "orca-nudge.sh").read_text()
    assert "`" not in script
    assert "$(orca" not in script
    exec_lines = [
        ln for ln in script.splitlines()
        if ln.strip().startswith("orca ") and not ln.lstrip().startswith("#")
    ]
    assert exec_lines == [], f"脚本不得行首裸执行 orca: {exec_lines}"


def test_tool_classification_is_single_source_of_truth():
    """SPEC §11.4 P5/E2：白名单字面量在 *.sh / *.ts 各出现 ≤1 次（只读 JSON 引用，非硬编码副本）。

    检查 cc_nudge.sh 与 orca.ts 不硬编码具体只读命令字面（如 'git log' / 'python'），而是通过
    读 tool-classification.json 派生。允许：JSON 文件名 / 字段名（readonly_bash_prefixes）的引用、
    注释里的提及。守门对象：代码主体（去注释后）不得硬编码具体 readonly 前缀列表（引号包裹形态）。
    """
    templates = Path(install_cmds._cc_nudge_script_src()).parent
    sh = (templates / "cc_nudge.sh").read_text(encoding="utf-8")
    ts = (templates / "opencode" / "orca.ts").read_text(encoding="utf-8")
    cls = json.loads((templates / "tool-classification.json").read_text(encoding="utf-8"))

    def _strip_sh_comments(text: str) -> str:
        return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))

    def _strip_ts_comments(text: str) -> str:
        text = re.sub(r"/\*[\s\S]*?\*/", "", text)
        out = []
        for ln in text.splitlines():
            idx = ln.find("//")
            if idx >= 0:
                ln = ln[:idx]
            out.append(ln)
        return "\n".join(out)

    sh_code = _strip_sh_comments(sh)
    ts_code = _strip_ts_comments(ts)
    # 多字 readonly 前缀（'git status' 等）不应作为引号字面出现在代码主体（容许 JSON 文件内容 /
    # 注释 / 字段名引用）。引号形态断言抓「硬编码字符串」而不误伤注释文本里的提及。
    for prefix in cls["readonly_bash_prefixes"]:
        if " " not in prefix:
            continue  # 单词前缀（orca/cat/ls）容许在 reason / 命令名等场景
        for quoted in (f"'{prefix}'", f'"{prefix}'):
            assert quoted not in sh_code, (
                f"cc_nudge.sh 代码主体不应硬编码 readonly 前缀 {quoted!r}（应读 tool-classification.json）"
            )
            assert quoted not in ts_code, (
                f"orca.ts 代码主体不应硬编码 readonly 前缀 {quoted!r}（应读 tool-classification.json）"
            )
    # 反向守门：JSON 文件存在 + 两模板均 read 它（reference 字面至少出现一次）。
    assert "tool-classification.json" in sh
    assert "tool-classification.json" in ts


def test_guard_reason_template_single_source_of_truth():
    """SPEC §6 + review 🟡#3：guard_reason_template 文案是 cc_nudge.sh 与 orca.ts 共享的 canonical
    字面，存 tool-classification.json 单一真相源。两家族读 JSON 填占位符——代码主体容许**1 份**
    内联兜底（应对 JSON 缺失，防御性双声明），但不得多处硬编码（漂移源）。

    本测断言：canonical 文案字面在 *.sh / *.ts 各出现 ≤1 次（仅内联兜底，非多处副本）。
    """
    templates = Path(install_cmds._cc_nudge_script_src()).parent
    cls = json.loads((templates / "tool-classification.json").read_text(encoding="utf-8"))
    canonical = cls["guard_reason_template"]
    sh = (templates / "cc_nudge.sh").read_text(encoding="utf-8")
    ts = (templates / "opencode" / "orca.ts").read_text(encoding="utf-8")
    # 各最多 1 份（内联兜底）；JSON 文件是真相源，不在限制内。
    assert sh.count(canonical) <= 1, (
        f"cc_nudge.sh 含 {sh.count(canonical)} 份 canonical reason（容许 ≤1 份兜底）"
    )
    assert ts.count(canonical) <= 1, (
        f"orca.ts 含 {ts.count(canonical)} 份 canonical reason（容许 ≤1 份兜底）"
    )
    # 两家族都从 JSON 读 guard_reason_template 字段（取真相源，非纯兜底）
    assert "guard_reason_template" in sh
    assert "guard_reason_template" in ts


def test_orca_ts_has_tool_execute_after_hook():
    """SPEC §11.1：orca.ts 含 tool.execute.after 钩子（grep 守门），idle event 不变。"""
    plugin = install_cmds._opencode_plugin_src()
    text = plugin.read_text(encoding="utf-8")
    assert '"tool.execute.after"' in text, "缺 tool.execute.after 钩子（SPEC §8）"
    assert "classifyTool" in text, "缺 classifyTool helper（SPEC §5 分类）"
    assert "loadClassification" in text, "缺 loadClassification（读 tool-classification.json）"
    assert "guard" in text.lower()
    # idle 钩子不变（SPEC §11.3 回归保护）
    assert 'if (event.type !== "session.idle") return' in text
    assert "Orca nudge" in text  # idle nudge 文案保留


def test_orca_ts_tool_execute_after_never_advances():
    """SPEC §11.4 B 路径铁律：tool.execute.after 钩子绝不 spawn orca / 调 advance/router/tape。"""
    plugin = install_cmds._opencode_plugin_src()
    text = plugin.read_text(encoding="utf-8")
    start = text.find('"tool.execute.after"')
    assert start >= 0
    # 钩子区段到下一个同级钩子/对象闭合。粗切到 '} finally {' 后的 '},' 闭合即可。
    end = text.find('    },', start)
    assert end >= 0
    hook = text[start:end]
    # 禁止实际 spawn/调用 advance 路径（reminder 文本里提到 ``orca next`` 是允许的——教模型去调）。
    for forbidden in ("spawnCli", "spawnTopLevelCli", "Bun.spawn", "advance_step"):
        assert forbidden not in hook, f"tool.execute.after 不得 {forbidden}"
    # 钩子内含 promptAsync（pure hint 注入路径）+ classifyTool（分类）
    assert "promptAsync" in hook
    assert "classifyTool" in hook


def test_install_cmds_has_no_orca_business_logic_posttooluse():
    """SPEC §11.4 D-v7-1：install_cmds 仍零 Orca 业务逻辑（新增 PostToolUse 条目 = 纯配置合并）。"""
    src = Path(install_cmds.__file__).read_text(encoding="utf-8")
    forbidden = [
        "from orca.run", "from orca.events", "from orca.schema",
        "advance_step", "router.resolve", "replay_state", "tape.append",
        "EventBus(", "Orchestrator(",
    ]
    for kw in forbidden:
        assert kw not in src, f"install_cmds 含禁词 {kw!r}（违反零业务逻辑守门）"

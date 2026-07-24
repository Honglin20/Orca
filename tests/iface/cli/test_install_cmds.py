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

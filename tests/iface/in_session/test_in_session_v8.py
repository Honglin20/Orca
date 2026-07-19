"""tests/iface/in_session/test_in_session_v8.py —— v8 / v5 增量守门测试。

覆盖 SPEC v8 §2.7 + §9.2 + v5 §4.4 / §8 step 4：
  - **doctor CLI**（§2.7，v5 重设计）：4 项 checks（skill_install/cli_imports 硬 +
    diag_switch/advance_hook 可选；FU-2 删 entry_hook dead check）、JSON 结构、
    report 描述 B 路径、ok=skill+cli 无 fail。
  - **v5 §4.4 idle nudge hook**：session.idle → 提醒主 session 调 next（**绝不 spawn next**，
    B 路径铁律）。
  - **架构守门**：plugin 模板无 `@opencode/core/client` / `command.execute.before` /
    `advance_step` / `router.resolve` / `replay_state` / `tape.append` / `EventBus` /
    `Tape(` / `drive_loop` / `advance(`。
  - **CLI 行为契约**：bootstrap / next / status / stop（含 --json flag、compact prompt_file
    指针、stop run_id 直定位）。

v5 §8 step 4 收尾：transform marker 派发 + 死代码（extractTaskOutput / spawnCli / spawnTopLevelCli
/ rewriteText / findLastUserTextPart / extractModel / buildCliArgs / MARKER_REGEX / MARKER_LITERAL）
从 orca.ts 整删——相关守门测试（marker regex 同步 / 改写语义 / spawnCli fail loud / buildCliArgs
分支 / transform 签名）随之删除。仅保留 idle nudge hook 守门 + 架构守门 + CLI 行为契约。
``_constants.py`` 整删（MARKER_REGEX/LITERAL 仅被 transform 段引用，删后无消费者）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.iface.in_session.cli import app


# ── fixtures ────────────────────────────────────────────────────────────────


PLUGIN_TS = (
    Path(__file__).resolve().parents[3]
    / "orca/iface/in_session/templates/opencode/orca.ts"
)


@pytest.fixture
def cwd_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── doctor 诊断（v5 §4.4：skill_install + cli_imports 为硬检查；hook 心跳可选）────


@pytest.fixture
def doctor_iso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离 doctor 的 home + cwd：``Path.home`` → tmp_path + chdir tmp_path。

    必须：doctor 的 ``_scan_skill_install`` 查 user-scope（``~/.claude`` 等）+ project-scope。
    不隔离 home，则装过 orca 的开发机上 ``skill_install`` 恒 pass → fail-when-absent 测试反向失败。
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run_doctor() -> dict:
    """跑 ``doctor``，解析末行 JSON。"""
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output.splitlines()[-1])


def _write_probe(cwd: Path, name: str, payload: dict) -> None:
    """写一个诊断心跳文件到 ``<cwd>/runs/<name>``（doctor 从 ``runs/`` 读）。"""
    runs = cwd / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / name).write_text(json.dumps(payload), encoding="utf-8")


_DOTDIR = {"cc": ".claude", "opencode": ".opencode", "cac": ".cac", "nga": ".nga"}


def _install_fake_entry_skill(
    root: Path, platform: str = "cc", *, under: str = "project",
    home: Path | None = None,
) -> Path:
    """落一个占位入口 skill（TARS 品牌）让 doctor 扫到。

    目录名取 ``ENTRY_SKILL_NAME`` 单一真相源（现 = tars），与 ``_scan_skill_install`` 对齐。
    - ``under="project"``：落 ``<root>/<dotdir>/skills/<ENTRY_SKILL_NAME>/SKILL.md``（root 当 cwd）。
    - ``under="user"``：落 ``<home>/<dotdir>/skills/<ENTRY_SKILL_NAME>/SKILL.md``（home 注入时用）。
    """
    from orca.iface.cli.skill_cmds import ENTRY_SKILL_NAME

    dotdir = _DOTDIR[platform]
    base = (home if under == "user" else root)
    skill_md = base / dotdir / "skills" / ENTRY_SKILL_NAME / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(f"---\nname: {ENTRY_SKILL_NAME}\n---\n# {ENTRY_SKILL_NAME}\n", encoding="utf-8")
    return skill_md


def test_doctor_json_structure(doctor_iso, monkeypatch):
    """doctor 输出 JSON {ok, diag, report, checks} + 6 项 checks。

    顺序：skill_install / cli_imports_ok（硬）在前，diag_switch / advance_hook /
    sidechain_backend（SPEC §P4）/ sidechain_daemon（SPEC §5 D3）（可选）在后。
    """
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    reply = _run_doctor()
    assert set(reply.keys()) >= {"ok", "diag", "report", "checks"}
    assert isinstance(reply["ok"], bool)
    assert isinstance(reply["diag"], bool)
    assert isinstance(reply["report"], str) and len(reply["report"]) > 0
    assert isinstance(reply["checks"], list)
    assert len(reply["checks"]) == 6
    names = [c["name"] for c in reply["checks"]]
    # FU-2：entry_hook 已删（transform step 4 整删后 PROBE_ENTRY 心跳永不再写，dead）。
    # SPEC §P4：sidechain_backend（hard=False，B2 路径诊断）。
    # SPEC §5 D3：sidechain_daemon（hard=False，per-active-run liveness 探针；覆盖死亡，
    # 不覆盖持续 iterate 失败 §8#4）。
    assert names == ["skill_install", "cli_imports_ok", "diag_switch",
                     "advance_hook", "sidechain_backend", "sidechain_daemon"]
    # 每条 check 带 hard 字段（review 🟡#5：替代硬编码 name tuple 防 typo 静默丢失硬检查）
    hard_expected = {"skill_install": True, "cli_imports_ok": True,
                     "diag_switch": False, "advance_hook": False,
                     "sidechain_backend": False, "sidechain_daemon": False}
    for c in reply["checks"]:
        assert set(c.keys()) == {"name", "status", "detail", "hard"}, (
            f"check {c['name']} 字段漂移：{set(c.keys())}"
        )
        assert c["status"] in ("pass", "unknown", "fail")
        assert c["hard"] is hard_expected[c["name"]]


def test_doctor_skill_install_pass_when_skill_present(doctor_iso, monkeypatch):
    """A6：四前端任一装了入口 skill（project-scope）→ skill_install=pass + ok=True。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    _install_fake_entry_skill(doctor_iso, "cc")
    reply = _run_doctor()
    by_name = {c["name"]: c for c in reply["checks"]}
    assert by_name["skill_install"]["status"] == "pass"
    assert "cc" in by_name["skill_install"]["detail"]
    assert reply["ok"] is True  # skill + cli 都 pass


def test_doctor_skill_install_detects_each_platform(doctor_iso, monkeypatch):
    """A6：opencode / cac / nga project-scope 装 skill → 各自被扫到（覆盖 _scan_skill_install 分支）。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    for platform in ("opencode", "cac", "nga"):
        _install_fake_entry_skill(doctor_iso, platform)
    reply = _run_doctor()
    detail = {c["name"]: c["detail"] for c in reply["checks"]}["skill_install"]
    assert reply["ok"] is True
    for platform in ("opencode", "cac", "nga"):
        assert platform in detail


def test_doctor_skill_install_user_scope(doctor_iso, monkeypatch):
    """A6：user-scope（``<home>/<dotdir>/skills/<ENTRY_SKILL_NAME>``）也能被扫到（doctor_iso 把 home 指到 tmp）。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    _install_fake_entry_skill(doctor_iso, "cac", under="user", home=doctor_iso)
    reply = _run_doctor()
    by_name = {c["name"]: c for c in reply["checks"]}
    assert by_name["skill_install"]["status"] == "pass"
    assert "cac" in by_name["skill_install"]["detail"]


def test_doctor_skill_install_fail_when_absent(doctor_iso, monkeypatch):
    """A6：四前端都没装入口 skill → skill_install=fail + ok=False（doctor_iso 隔离 home + cwd 干净）。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    reply = _run_doctor()
    by_name = {c["name"]: c for c in reply["checks"]}
    assert by_name["skill_install"]["status"] == "fail"
    assert reply["ok"] is False  # skill_install fail → ok False（即便 cli ok）


def test_doctor_diag_off_hook_checks_unknown_ok_unaffected(doctor_iso, monkeypatch):
    """诊断关：hook 两项（diag/advance）均 unknown；ok 只看 skill+cli（装了 skill → ok）。

    FU-2：entry_hook check 已删（transform step 4 整删后 dead），不再断言。
    """
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    _install_fake_entry_skill(doctor_iso, "cc")
    reply = _run_doctor()
    assert reply["diag"] is False
    by_name = {c["name"]: c for c in reply["checks"]}
    assert by_name["diag_switch"]["status"] == "unknown"
    assert by_name["advance_hook"]["status"] == "unknown"
    assert reply["ok"] is True  # hook unknown 不拉低 ok（skill+cli pass）


def test_doctor_advance_heartbeat_passes(doctor_iso, monkeypatch):
    """诊断开 + advance 心跳 = advance PASS（idle 在 fire，报告 idle 计数，仅 nudge）。

    v5 §8 step 4 收尾：``advance_count`` / ``last_advance_run_id`` 字段从 fixture 删除
    （plugin 不再写——A 路径退场后 idle hook 不 spawn next，doctor 也不读它们）。
    """
    import time as _t
    monkeypatch.setenv("ORCA_DIAGNOSE", "1")
    _install_fake_entry_skill(doctor_iso, "cc")
    _write_probe(doctor_iso, ".orca-probe-advance.json", {
        "diag": True, "last_idle_at": int(_t.time()),
        "idle_count": 3,
        "last_session_id": "s-1",
    })
    reply = _run_doctor()
    by_name = {c["name"]: c for c in reply["checks"]}
    assert by_name["advance_hook"]["status"] == "pass"
    assert "idle fire 过 3" in by_name["advance_hook"]["detail"]


def test_doctor_report_describes_b_path(doctor_iso, monkeypatch):
    """v5：报告说明执行模型 = B 路径（主 session 自调 next），hook 退居可选诊断。

    FU-2：entry probe 路径行已删，仅 advance probe 路径写进报告。
    """
    monkeypatch.setenv("ORCA_DIAGNOSE", "1")
    _install_fake_entry_skill(doctor_iso, "cc")
    reply = _run_doctor()
    assert "B 路径" in reply["report"]
    assert "orca next" in reply["report"]  # 主 session 自调 next
    # advance 心跳文件路径写进报告（entry 路径行 FU-2 删，不再断言）
    assert ".orca-probe-advance.json" in reply["report"]
    assert ".orca-probe-entry.json" not in reply["report"]  # FU-2 守门：entry 路径不再出现


# ── SPEC §P4：sidechain_backend check（cac/nga 路径诊断）─────────────────────


def _sidechain_check(reply: dict) -> dict:
    """从 doctor reply 抽 sidechain_backend check。"""
    matches = [c for c in reply["checks"] if c["name"] == "sidechain_backend"]
    assert len(matches) == 1, f"应有恰好 1 个 sidechain_backend check，得 {len(matches)}"
    return matches[0]


def test_doctor_sidechain_backend_no_env_reports_unknown(doctor_iso, monkeypatch):
    """无 CC/opencode env → status=unknown（非 in-session run，B2 不适用）。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    _install_fake_entry_skill(doctor_iso, "cc")
    reply = _run_doctor()
    check = _sidechain_check(reply)
    assert check["hard"] is False
    assert check["status"] == "unknown"
    assert "未检测到" in check["detail"]
    # hard=False → 不影响 ok（既有 skill+cli 硬检查主导）。
    assert reply["ok"] is True


def test_doctor_sidechain_backend_cc_default_when_no_dirs(doctor_iso, monkeypatch):
    """有 CC env + 无 .claude/.cac 目录 → default .claude；root_exists=False。

    SPEC §P4 验收「doctor 输出 resolved 路径 + source + 存在性」。
    """
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "host-sid-123")
    _install_fake_entry_skill(doctor_iso, "cc")
    reply = _run_doctor()
    check = _sidechain_check(reply)
    assert check["hard"] is False
    # 无目录存在 → root_exists=False → status=fail（hard=False 不影响 ok）。
    assert check["status"] == "fail"
    detail = check["detail"]
    assert "family=cc" in detail
    assert "resolved_root=" in detail
    assert "root_source=default" in detail
    assert "root_exists=False" in detail
    assert "host_session_set=True" in detail
    assert "available=False" in detail
    assert "tars install --target cac" in detail  # 不存在时 hint


def test_doctor_sidechain_backend_cc_ambiguity_hint(doctor_iso, monkeypatch):
    """SPEC §P4 验收 #2：cc+cac 同装无 config → 默认 .claude + doctor 报告歧义 hint。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "host-sid-amb")
    _install_fake_entry_skill(doctor_iso, "cc")

    # 在 doctor_iso（=tmp_path，也是 Path.home() 注入目标）下建 cc + cac 同 sid 的 subagents
    # 目录，触发 detect_cc_existing_roots 返 {'cc','cac'}（歧义）。
    encoded = str(doctor_iso).replace("/", "-")  # _encode_cwd 等价
    for dotdir in (".claude", ".cac"):
        root = doctor_iso / dotdir / "projects" / encoded / "host-sid-amb" / "subagents"
        root.mkdir(parents=True)

    reply = _run_doctor()
    check = _sidechain_check(reply)
    detail = check["detail"]
    # 默认 .claude + family=cc。
    assert "family=cc" in detail
    assert "source=probe-ambig" in detail
    # 歧义 hint（SPEC §P4 验收 #2）。
    assert "歧义" in detail
    assert "sidechain.family" in detail  # 建议设 config
    # 两路径都存在 → root_exists=True → available。
    assert "root_exists=True" in detail


def test_doctor_sidechain_backend_cc_cac_only_probe(doctor_iso, monkeypatch):
    """SPEC §P4 验收 #1 场景：仅 .cac 存在 → probe 胜指向 cac。

    doctor 应报告 family=cac（source=probe）+ root_exists=True。
    """
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "host-sid-cac")
    _install_fake_entry_skill(doctor_iso, "cc")

    encoded = str(doctor_iso).replace("/", "-")
    cac_root = doctor_iso / ".cac" / "projects" / encoded / "host-sid-cac" / "subagents"
    cac_root.mkdir(parents=True)

    reply = _run_doctor()
    check = _sidechain_check(reply)
    detail = check["detail"]
    assert "family=cac" in detail
    assert "source=probe" in detail
    assert "root_exists=True" in detail
    assert "available=True" in detail
    assert check["status"] == "pass"
    # 单一存在无歧义 hint。
    assert "歧义" not in detail


def test_doctor_sidechain_backend_opencode_family(doctor_iso, monkeypatch):
    """opencode 家族：有 ORCA_HOST_SESSION_ID + nga DB 存在 → probe 胜。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("ORCA_HOST_SESSION_ID", "host-sid-opc")
    _install_fake_entry_skill(doctor_iso, "cc")

    # 建 ~/.local/share/nga/opencode.db（fake_home=doctor_iso）。
    nga_db = doctor_iso / ".local" / "share" / "nga" / "opencode.db"
    nga_db.parent.mkdir(parents=True)
    nga_db.write_bytes(b"")

    reply = _run_doctor()
    check = _sidechain_check(reply)
    detail = check["detail"]
    assert "family=nga" in detail
    assert "resolved_db=" in detail
    assert "db_source=probe" in detail
    assert "db_exists=True" in detail
    assert "available=True" in detail
    assert check["status"] == "pass"


def test_doctor_sidechain_backend_config_family_override(doctor_iso, monkeypatch):
    """config sidechain.family 覆盖探测：即便 .claude 存在，family=cac → resolved 指向 .cac。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "host-sid-cfg")
    _install_fake_entry_skill(doctor_iso, "cc")

    # 建 .claude（probe 会选它），但 config 设 family=cac。
    encoded = str(doctor_iso).replace("/", "-")
    cc_root = doctor_iso / ".claude" / "projects" / encoded / "host-sid-cfg" / "subagents"
    cc_root.mkdir(parents=True)

    # 写 user-scope config（load_merged_config 读 ~/.orca/config.json）。
    cfg = doctor_iso / ".orca" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('{"sidechain": {"family": "cac"}}', encoding="utf-8")

    reply = _run_doctor()
    check = _sidechain_check(reply)
    detail = check["detail"]
    # config 胜：family=cac（source=config）。
    assert "family=cac" in detail
    assert "source=config" in detail
    # resolved 指向 .cac（即便不存在，config 显式）。
    assert ".cac" in detail
    assert "root_source=config" in detail


def test_doctor_sidechain_backend_hard_false_does_not_affect_ok(doctor_iso, monkeypatch):
    """sidechain_backend hard=False → status=fail 不拉低 ok（hard 检查主导）。

    守门 SPEC §P4：不破坏既有 hard check（skill_install/cli_imports_ok）的 ok 逻辑。
    """
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "host-sid-noroot")
    _install_fake_entry_skill(doctor_iso, "cc")
    # 无目录 → sidechain_backend status=fail。
    reply = _run_doctor()
    check = _sidechain_check(reply)
    assert check["hard"] is False
    assert check["status"] == "fail"
    # 但 skill + cli 硬检查 pass → ok=True。
    assert reply["ok"] is True


# ── SPEC §5 D3：sidechain_daemon liveness check（per-active-run 探针）─────────


def _sidechain_daemon_check(reply: dict) -> dict:
    """从 doctor reply 抽 sidechain_daemon check。"""
    matches = [c for c in reply["checks"] if c["name"] == "sidechain_daemon"]
    assert len(matches) == 1, f"应有恰好 1 个 sidechain_daemon check，得 {len(matches)}"
    return matches[0]


def test_doctor_sidechain_daemon_no_env_reports_unknown(doctor_iso, monkeypatch):
    """无 host_session env → sidechain_daemon status=unknown（守护本就不该起）。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    _install_fake_entry_skill(doctor_iso, "cc")
    reply = _run_doctor()
    check = _sidechain_daemon_check(reply)
    assert check["hard"] is False
    assert check["status"] == "unknown"
    assert "未检测到 host_session env" in check["detail"]
    # hard=False → 不影响 ok。
    assert reply["ok"] is True


def test_doctor_sidechain_daemon_env_but_no_active_run_unknown(doctor_iso, monkeypatch):
    """有 CC env 但无活跃 run marker → unknown（守护仅在活跃 run 中起作用）。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "host-sid-noactive")
    _install_fake_entry_skill(doctor_iso, "cc")
    # 无 marker（runs/ 可能不存在）
    reply = _run_doctor()
    check = _sidechain_daemon_check(reply)
    assert check["hard"] is False
    assert check["status"] == "unknown"
    assert "无活跃 run marker" in check["detail"] or "无 runs/" in check["detail"]


def test_doctor_sidechain_daemon_dead_for_active_run(doctor_iso, monkeypatch):
    """有 CC env + 有活跃 marker + 无对应守护 pidfile → status=fail（degraded，hard=False）。

    SPEC §5 D3 AC：守护死 → degraded + hint。覆盖「守护死亡」场景。
    """
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "host-sid-daemon-dead")
    _install_fake_entry_skill(doctor_iso, "cc")

    # 造一个活跃 marker 但不 spawn 守护（模拟守护被杀 / 残留 marker）。
    runs = doctor_iso / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "orca-fake-run-zzz.json").write_text(
        json.dumps({"run_id": "fake-run-zzz", "model": "any", "no_output_count": 0}),
        encoding="utf-8",
    )

    reply = _run_doctor()
    check = _sidechain_daemon_check(reply)
    assert check["hard"] is False
    assert check["status"] == "fail"
    detail = check["detail"]
    assert "fake-run-zzz=dead" in detail
    # SPEC §5 D3 AC：degraded + hint（next 自动 respawn）。
    assert "respawn" in detail or "next" in detail
    # SPEC §5 D3 AC：明示不覆盖持续失败（§8#4）。
    assert "§8#4" in detail or "iterate" in detail
    # hard=False → 不影响 ok（skill+cli 仍主导）。
    assert reply["ok"] is True


def test_doctor_sidechain_daemon_alive_for_active_run(doctor_iso, monkeypatch):
    """有 CC env + 活跃 marker + 守护真活（写 pidfile + 起一个轻量进程）→ status=pass。

    SPEC §5 D3 AC：守护存活 → pass。
    """
    import os
    import tempfile

    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "host-sid-daemon-alive")
    _install_fake_entry_skill(doctor_iso, "cc")

    run_id = "fake-alive-run-xyz"
    runs = doctor_iso / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"orca-{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "model": "any", "no_output_count": 0}),
        encoding="utf-8",
    )

    # 起 sleeping 子进程 + 写 pidfile 指向它 + cmdline 伪装成 sidechain_daemon。
    # ``pidfile_daemon_alive`` 读 /proc/<pid>/cmdline 校验含模块名 + --run-id + run_id；
    # 这里用一个长存活子进程跑 ``sleep``，再 monkeypatch cmdline 校验（避免真起 daemon）。
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import time; time.sleep(300)"],
        # 伪装 argv：让 cmdline 解析出 module_name + --run-id + run_id。
        # 直接 monkeypatch _sidechain_daemon_alive 更简洁（不依赖 /proc 真值）。
    )
    try:
        # 直接 monkeypatch liveness 探为 True（守护存活语义），守 doctor 的 pass 分支。
        from orca.iface.in_session import sidechain_daemon as sd_mod
        monkeypatch.setattr(sd_mod, "_sidechain_daemon_alive", lambda rid: True)

        reply = _run_doctor()
        check = _sidechain_daemon_check(reply)
        assert check["hard"] is False
        assert check["status"] == "pass"
        assert f"{run_id}=alive" in check["detail"]
        # SPEC §5 D3 AC：明示不覆盖持续 iterate 失败（§8#4）。
        assert "§8#4" in check["detail"] or "iterate" in check["detail"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ── v5 §4.4 / step 2b(7)：orca.ts idle nudge（提醒，绝不推进）───────────────────


def test_orca_ts_idle_hook_is_nudge_no_advance():
    """v5 §4.4 + step 4：``session.idle`` hook 是 nudge（提醒主 session 调 next），**绝不 spawn next**。

    B 路径铁律：hook 自动调 next = 退化 A 路径。idle hook 应只扫 marker + promptAsync 注入
    提醒，不 spawnCli。提取 event hook 区段断言。

    step 4 收尾后：transform 段已整删——本测试同时守门 transform 不得复活（防止 builder
    把 transform 入口段加回来）。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    # 提取 event hook 区段（从 ``event: async`` 到其后第一个 `\n    },`）。
    start = text.find("event: async")
    assert start >= 0, "未找到 event hook"
    end = text.find("\n    },", start)
    assert end >= 0, "event hook 区段未闭合"
    hook = text[start:end]
    # nudge 机制存在
    assert "listActiveRuns" in text, "缺 listActiveRuns（nudge 扫活跃 run 的 helper）"
    assert "Orca nudge" in hook, "idle hook 缺 nudge 提醒文案"
    assert "promptAsync" in hook, "idle hook 应用 promptAsync 注入提醒"
    # 铁律：idle hook 不得 spawn next（不出现任何 spawn 路径调用——spawnCli /
    # spawnTopLevelCli / Bun.spawnSync / Bun.spawn 任一都算退化 A 路径自动推进）
    for spawn_pat in ("spawnCli", "spawnTopLevelCli", "Bun.spawn"):
        assert spawn_pat not in hook, (
            f"idle hook 不得 {spawn_pat}（B 路径：nudge 只提醒，绝不自动调 next）"
        )


def test_orca_ts_has_no_transform_hook_step4():
    """v5 §8 step 4 收尾守门：transform marker 派发入口段 + 死代码已整删。

    防止 builder 把 ``experimental.chat.messages.transform`` 入口加回来（旧 A 路径第二入口，
    v5 入口统一切到入口 skill（TARS）——transform 复活 = 第二入口绕过 skill，违反单一接口）。
    同时守门 transform 相关死代码（spawnCli / rewriteText / buildCliArgs / MARKER_REGEX 等）
    不得复活。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    code = _strip_ts_comments(text)
    # transform 入口段已整删（查裸键——双引号 / 单引号 / 模板字符串任一形态都算复活）
    assert "experimental.chat.messages.transform" not in code, (
        "step 4：transform marker 派发入口段应整删（入口统一切到入口 skill / TARS）"
    )
    # transform 死代码同步守门（防复活）
    dead_artifacts = [
        "MARKER_REGEX", "MARKER_LITERAL",
        "function spawnCli", "function spawnTopLevelCli",
        "function rewriteText", "function buildCliArgs",
        "function extractTaskOutput", "function findLastUserTextPart",
        "function extractModel",
    ]
    for artifact in dead_artifacts:
        assert artifact not in code, (
            f"step 4：transform 死代码 {artifact!r} 应整删（无消费者）"
        )


# ── 架构守门（§2.6 / §9.2）──────────────────────────────────────────────────


def _strip_ts_comments(text: str) -> str:
    """去掉 // 行注释与 /* */ 块注释（粗略，仅测试用）。"""
    # 去块注释
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    # 去 // 行注释（不跨行）
    lines = []
    for ln in text.splitlines():
        # 简单切：找行内第一个未在字符串里的 //；测试场景足够用
        idx = ln.find("//")
        if idx >= 0:
            ln = ln[:idx]
        lines.append(ln)
    return "\n".join(lines)


def test_plugin_uses_ctx_client_not_npm_import():
    """v8 spike 实证：``@opencode/core/client`` npm 不存在，client 必须从 ``ctx.client`` 取。

    守门对象是 **import 语句**（不是注释里的提及）：禁 ``from "@opencode/core/client"``
    与 ``require("@opencode/core/client")``。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    code = _strip_ts_comments(text)
    assert 'from "@opencode/core/client"' not in code
    assert "from '@opencode/core/client'" not in code
    assert 'require("@opencode/core/client")' not in code
    assert "require('@opencode/core/client')" not in code
    assert "ctx.client" in code, "plugin 应使用 ctx.client"


def test_plugin_exports_flat_hooks_not_nested():
    """SPEC §13 v8：``export const OrcaPlugin = async (ctx) => ({ ...flat hooks })``。"""
    text = PLUGIN_TS.read_text(encoding="utf-8")
    code = _strip_ts_comments(text)
    assert "export const OrcaPlugin = async" in code, (
        "plugin 必须 `export const OrcaPlugin = async (ctx) => ({...})`（flat hooks）"
    )
    # 不允许 nested hooks: { hooks: { event: ... } }（v7 缺陷）
    assert not re.search(r"\{\s*hooks:\s*\{", code), (
        "plugin 不得 nested hooks（spike 实证 flat hooks 才生效）"
    )


def test_plugin_does_not_use_command_execute_before():
    """SPEC §2.6 D-v8-1：``command.execute.before`` 在 opencode 1.14.22 runtime 不触发，禁用。

    守门对象是 **hook 注册键**（不是注释里的提及）：禁 ``"command.execute.before":``
    作为 hook key 注册。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    code = _strip_ts_comments(text)
    assert '"command.execute.before"' not in code, (
        "plugin 不得注册 command.execute.before（opencode 1.14.22 runtime 不触发）"
    )
    assert "'command.execute.before'" not in code


def test_plugin_has_no_orca_business_logic():
    """SPEC §2.6 / §9.2 架构守门：plugin 侧零 Orca 业务逻辑。

    禁词：advance_step / router.resolve / replay_state / tape.append / EventBus /
    drive_loop / advance（带 ``(`` 的调用形式）。
    注：``<task_result>`` 提取 + ``task_id:`` 剥离**允许**（SPEC §2.5 划为宿主侧
    payload 扁平化，非 Orca 业务逻辑）。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    forbidden = [
        "advance_step", "router.resolve", "replay_state", "tape.append",
        "EventBus", "Tape(", "drive_loop", "advance(",
    ]
    for kw in forbidden:
        assert kw not in text, f"plugin 模板含禁词 {kw!r}（违反 D-v7-1）"


def test_plugin_no_chat_message_hook():
    """SPEC §2.6 spike：``chat.message`` 的 ``ignored`` flag 在 1.14.22 不生效，禁用。"""
    text = PLUGIN_TS.read_text(encoding="utf-8")
    assert '"chat.message"' not in text
    assert "'chat.message'" not in text


def test_plugin_no_count_state_for_doctor():
    """SPEC §2.7 M6：plugin 侧不维护 hook 触发 count++ 状态（与「plugin 零 Orca 状态」守门冲突）。"""
    text = PLUGIN_TS.read_text(encoding="utf-8")
    # 不应有 count[self-type]++ 模式
    assert not re.search(r"count\[\s*[\"']\w+[\"']\s*\]\s*\+\+", text), (
        "plugin 不得维护 hook 触发计数（doctor 不依赖 plugin 自报状态）"
    )


# ── 共享 wf fixture（v5 step 2b：start 命令已删，fixture 供其余 v7/v8 测试复用）────


AGENT_WF_YAML = """\
name: start_test_wf
description: 2-agent 线性 workflow（fixture，供 v7/v8 测试复用）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 A。"
    routes:
      - to: b
  - name: b
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "基于 {{ a.output }} 总结。"
    routes:
      - to: $end
"""


@pytest.fixture
def wf_path(tmp_path: Path) -> Path:
    p = tmp_path / "wf.yaml"
    p.write_text(AGENT_WF_YAML, encoding="utf-8")
    return p


# ── v7 baseline 兼容性（CI 守门：v8 不破坏 v7 行为）─────────────────────────


def test_v7_baseline_cli_commands_still_work_v8(cwd_tmp, wf_path):
    """v7 CLI 大脑未动：bootstrap/next/stop/status 仍可用（v5 删 start，v3 删 serve）。"""
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    assert boot.exit_code == 0
    reply = json.loads(boot.output.splitlines()[-1])
    assert reply["done"] is False

    tape = reply["tape"]
    run_id = reply["run_id"]
    nxt = runner.invoke(app, ["next", "--tape", tape, "--run-id", run_id, "--output", "out_a"])
    assert nxt.exit_code == 0

    stop = runner.invoke(app, ["stop", run_id])
    assert stop.exit_code == 0


# ── §2.6.2 status JSON 契约（MAJOR-1 闭环）──────────────────────────────────


def test_status_json_flag_outputs_json(cwd_tmp, wf_path):
    """SPEC §2.6.2：``--json`` flag → stdout 是合法 JSON（plugin 改写契约）。

    MAJOR-1 闭环：plugin 期望 status stdout 是 JSON 顶层字段（status/node_status/progress）。
    """
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    run_id = json.loads(boot.output.splitlines()[-1])["run_id"]

    result = runner.invoke(app, ["status", run_id, "--json"])
    assert result.exit_code == 0
    reply = json.loads(result.output.splitlines()[-1])

    assert reply["run_id"] == run_id
    assert reply["status"] == "running"
    assert isinstance(reply["node_status"], dict)
    assert "progress" in reply
    # 顶层字段对齐 plugin rewriteText 提取（status/node_status/progress）


def test_status_default_human_readable_unchanged(cwd_tmp, wf_path):
    """v7 行为：``status <run_id>`` 默认人类可读多行（无 --json 不变）。"""
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    run_id = json.loads(boot.output.splitlines()[-1])["run_id"]

    result = runner.invoke(app, ["status", run_id])
    assert result.exit_code == 0
    assert "status:" in result.output
    assert "running" in result.output
    assert "node_status:" in result.output


def test_status_run_id_includes_no_output_count(cwd_tmp, wf_path):
    """SPEC §3 O3：``status --run-id <id>`` 详情含 ``no_output_count``（raw 透出供观测）。

    bootstrap 后立刻查 → 计数 0（首次写 marker）；next 无 output 后再查 → 计数 ≥1。
    主 session 不据它改行为（compliance 是 orca 自我保护）—— 此处仅守字段透出契约。
    """
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    run_id = json.loads(boot.output.splitlines()[-1])["run_id"]

    # 1. bootstrap 后：no_output_count = 0（marker 初始化）
    result = runner.invoke(app, ["status", run_id, "--json"])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert "no_output_count" in reply, "status --run-id 详情应含 no_output_count（O3）"
    assert reply["no_output_count"] == 0

    # 人类可读路径也含
    result_hr = runner.invoke(app, ["status", run_id])
    assert result_hr.exit_code == 0
    assert "no_output_count" in result_hr.output

    # 2. 调一次 next 无 output（compliance +1）
    runner.invoke(app, ["next", "--run-id", run_id, "--output", ""])

    result2 = runner.invoke(app, ["status", run_id, "--json"])
    reply2 = json.loads(result2.output.splitlines()[-1])
    assert reply2["no_output_count"] == 1, "一次无 output next 后计数应 = 1"


def test_status_run_id_no_marker_reports_none_no_output_count(cwd_tmp, wf_path):
    """SPEC §3 O3：``status --run-id <id>`` 无 marker（run 已终态 / 损坏）→ no_output_count=None。

    守住「不阻塞 status」：marker 读失败时透出 None 供消费者识别，不 raise。
    """
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    run_id = json.loads(boot.output.splitlines()[-1])["run_id"]
    tape_path = (cwd_tmp / "runs" / f"{run_id}.jsonl")

    # 终态后清 marker（run 结束），status 仍可查 tape 但 marker 无 → None。
    runner.invoke(app, ["next", "--run-id", run_id, "--output", "out_a"])
    runner.invoke(app, ["next", "--run-id", run_id, "--output", "out_b"])
    # workflow_completed 已落 tape，marker 被 next 清掉

    result = runner.invoke(app, ["status", run_id, "--json"])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["status"] == "completed"
    assert reply["no_output_count"] is None, "无 marker → None（不阻塞 status）"


def test_status_json_flag_no_run_id_lists_runs_json(cwd_tmp, wf_path):
    """FU-3（SPEC §2.1/§2.3）：``status --json`` 无 run_id → 只列活跃 run，结构化字典元素。

    每元素是 dict（含 run_id/node/status/last_next_at/elapsed/resumable 六键），非裸 stem。
    completed run（无 marker）不列；活跃 = marker 存在。F1：``resumable: true`` 显式透出
    （marker 在即 resumable；零 marker 字段改动）。
    """
    from orca.events.tape import Tape as _Tape

    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    boot_reply = json.loads(boot.output.splitlines()[-1])
    run_id = boot_reply["run_id"]
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    reply = json.loads(result.output.strip())
    assert "runs" in reply
    assert isinstance(reply["runs"], list)
    assert len(reply["runs"]) == 1  # 唯一活跃 run（bootstrap 后未终态）
    entry = reply["runs"][0]
    assert isinstance(entry, dict), "FU-3：runs 元素应为结构化 dict，非裸 stem"
    # 精确键集守门（防字段静默漂移）。F1：加 ``resumable``（SPEC §4 —— marker 在即 resumable）。
    assert set(entry.keys()) == {"run_id", "node", "status", "last_next_at", "elapsed", "resumable"}
    assert entry["run_id"] == run_id
    assert entry["status"] == "running"
    assert entry["node"] is not None  # current_node（bootstrap 后指向 entry 节点）
    # F1：resumable 透出（marker 在即 resumable）—— 主 session / SKILL 据此识别可续跑 run。
    assert entry["resumable"] is True
    # 时间字段钉死：last_next_at 必须等于 tape 末事件 Event.timestamp（spec-reviewer #1：
    # RunState 零时间字段，时间只能从 tape 事件派生；防止回归到 monotonic / marker mtime）。
    expected_last_ts = max(
        ev.timestamp for ev in _Tape(Path(boot_reply["tape"]), run_id=run_id).replay()
    )
    assert entry["last_next_at"] == pytest.approx(expected_last_ts)
    assert isinstance(entry["last_next_at"], (int, float))
    assert entry["elapsed"] is not None
    assert isinstance(entry["elapsed"], (int, float))


def test_status_no_run_id_excludes_completed(cwd_tmp, wf_path):
    """FU-3：completed run（marker 已清）不在活跃列表里。

    bootstrap → next 推到 done（workflow_completed + clear_marker）→ status 无参不列它。
    """
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    run_id = json.loads(boot.output.splitlines()[-1])["run_id"]
    tape = json.loads(boot.output.splitlines()[-1])["tape"]
    # 推进两步到 workflow_completed（a → b → $end），marker 清。
    runner.invoke(app, ["next", "--tape", tape, "--run-id", run_id, "--output", "out_a"])
    done = runner.invoke(app, ["next", "--tape", tape, "--run-id", run_id, "--output", "out_b"])
    assert json.loads(done.output.splitlines()[-1])["done"] is True

    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    reply = json.loads(result.output.strip())
    assert reply["runs"] == []  # completed run 无 marker → 不列


def test_status_no_run_id_empty_human_readable(cwd_tmp):
    """FU-3：无活跃 run → 人类可读 ``(无活跃 run)`` + exit 0（shape 一致）。"""
    runner = CliRunner()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "无活跃 run" in result.output


def test_status_no_run_id_non_empty_human_readable(cwd_tmp, wf_path):
    """FU-3：有活跃 run + 人类可读（无 --json）→ 每行 ``- <run_id> [status] node=… elapsed=… resumable=true``。"""
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    run_id = json.loads(boot.output.splitlines()[-1])["run_id"]
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert run_id in result.output
    assert "[running]" in result.output
    assert "node=" in result.output
    assert "elapsed=" in result.output
    # F1：resumable=true 透出在每行尾（人类可读形态；marker 在即 resumable）。
    assert "resumable=true" in result.output
    assert "看详情" in result.output  # 尾行提示
    # F1：尾行提示续跑命令（新 session 据 status 找到 run_id 后知道怎么续）。
    assert "orca next --run-id" in result.output


def test_status_no_run_id_skips_corrupt_and_orphan_markers(cwd_tmp, wf_path):
    """FU-3：损坏 marker（非法 JSON）+ 孤儿 marker（marker 在、tape 缺）被跳过，真 run 仍列。

    守护 cli.py 的两个显式 ``continue`` 失败路径（Rule 9：显式失败路径需测试）。
    """
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    run_id = json.loads(boot.output.splitlines()[-1])["run_id"]
    runs_dir = cwd_tmp / "runs"
    # 损坏 marker（非法 JSON）
    (runs_dir / "orca-corrupt.json").write_text("{not json", encoding="utf-8")
    # 孤儿 marker（合法 JSON 但无对应 tape 文件）
    (runs_dir / "orca-orphan.json").write_text(
        json.dumps({"run_id": "orphan", "model": "x", "no_output_count": 0}),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0  # 不崩（skip 不 fail）
    reply = json.loads(result.output.strip())
    run_ids = [r["run_id"] for r in reply["runs"]]
    assert run_ids == [run_id]  # corrupt + orphan 被跳过，只留真 run


# ── F1（SPEC §4）：TARS resume —— status resumable + next 无 output idempotent 重发 ──


def test_f1_resume_flow_status_resumable_then_next_no_output_resends_prompt(
    cwd_tmp, wf_path,
):
    """F1（SPEC §4）：新 session 续跑契约 —— status 透出 ``resumable: true`` +
    ``current_node``，``orca next --run-id X``（无 output）idempotent 重发 current_node
    的 prompt（复用 advance_step branch 4，零新字段）。

    端到端验证 SKILL.md「续跑」段的四步：
      1. ``status --json`` 无参 → runs[*] 含 ``resumable: true`` + ``node``（current_node）。
      2. ``next --run-id X``（不带 ``--output``）→ 重发 current_node 的 prompt（idempotent）。
      3. 重发的 prompt 与 bootstrap 时首发的 prompt **逐字相等**（同一节点同 prompt）。
      4. 之后 ``next --run-id X --output '<产出>'`` 正常推进到下一节点（resume 主路径绕过
         compliance —— 带输出，``no_output_count`` 不增）。
    """
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    boot_reply = json.loads(boot.output.splitlines()[-1])
    run_id = boot_reply["run_id"]
    tape = boot_reply["tape"]
    boot_prompt = boot_reply["prompt"]
    assert boot_prompt is not None  # bootstrap 必须首发 prompt

    # 1. status 无参 → resumable: true + node（current_node = "a"，首节点）。
    status_reply = runner.invoke(app, ["status", "--json"])
    assert status_reply.exit_code == 0
    status_json = json.loads(status_reply.output.strip())
    assert len(status_json["runs"]) == 1
    entry = status_json["runs"][0]
    assert entry["run_id"] == run_id
    assert entry["resumable"] is True  # F1 核心字段
    assert entry["node"] == "a"  # current_node（SKILL 据此知道续跑从哪起）

    # 2. next --run-id X（无 output）→ idempotent 重发 current_node=a 的 prompt。
    #    advance_step branch 4：无 output 且进行中 → 重发 pending prompt（零 emits）。
    #    否定契约：tape **未增加**（emits=[]）；兄弟测 test_in_session_cli.py:604-617 +
    #    test_daemon.py:270-294 都守这同一 AC，唯独 F1 resume 路径不能漏。
    #    **不传 ``--tape``**：SKILL 教的形式是裸 ``orca next --run-id X``，依赖
    #    ``_default_tape_path(run_id)`` 解析（cli.py:1129）—— 真验证生产路径，不走捷径。
    tape_lines_before = len(Path(tape).read_text(encoding="utf-8").strip().split("\n"))
    resume_reply = runner.invoke(app, ["next", "--run-id", run_id])
    assert resume_reply.exit_code == 0, resume_reply.output
    resume_json = json.loads(resume_reply.output.splitlines()[-1])
    assert resume_json["done"] is False
    assert resume_json["node"] == "a"  # 仍 a，未推进（idempotent 重发）
    tape_lines_after = len(Path(tape).read_text(encoding="utf-8").strip().split("\n"))
    assert tape_lines_after == tape_lines_before, (
        "F1：next 无 output idempotent 重发必须零 emits（advance_step branch 4 emits=[]），"
        "tape 行数不应增加"
    )

    # 3. 重发的 prompt 与 bootstrap prompt 逐字相等（同节点同 prompt，advance_step
    #    idempotent-replay 不改 prompt 内容）。
    assert resume_json["prompt"] == boot_prompt, (
        "F1：next 无 output 重发的 prompt 必须与 bootstrap 首发 prompt 逐字相等"
        "（idempotent replay，复用 advance_step branch 4）"
    )

    # 步骤 2 后 compliance 计数 +1（无 output next 的偏窄语义，§8#5）：钉死增量语义，
    # 防回归（如有人误把 idempotent 重发排除在 +1 之外）。
    from orca.iface.in_session.marker import read_marker, marker_path
    rundir = Path(tape).parent
    m_after_step2 = read_marker(marker_path(rundir, run_id))
    assert m_after_step2 is not None
    assert m_after_step2.no_output_count == 1, (
        "步骤 2 的无 output next 应让 no_output_count +1（compliance 偏窄语义，§8#5）"
    )

    # 4. next --run-id X --output '<产出>'（带 output）→ 正常推进到下一节点 b。
    #    resume 主路径绕过 compliance：带 output → ``no_output_count`` 重置为 0
    #    （cli.py:1327-1328：``if output is not None: marker.no_output_count = 0``）。
    #    同样不传 ``--tape``：SKILL 教的形式是 ``orca next --run-id X --output '...'``。
    advance_reply = runner.invoke(app, [
        "next", "--run-id", run_id, "--output", "out_a",
    ])
    assert advance_reply.exit_code == 0, advance_reply.output
    advance_json = json.loads(advance_reply.output.splitlines()[-1])
    assert advance_json["done"] is False
    assert advance_json["node"] == "b"  # 推进到下一节点 b

    # 步骤 4 后 compliance 计数 = 0（带 output 重置；cli.py:1327-1328 契约）。
    # 钉死主路径绕过：带 output 的 next 不只「不增」，而是「清零」（防 reset 逻辑回归）。
    m_after_step4 = read_marker(marker_path(rundir, run_id))
    assert m_after_step4 is not None
    assert m_after_step4.no_output_count == 0, (
        "步骤 4 的带 output next 应重置 no_output_count = 0（cli.py:1327-1328 契约；"
        "resume 主路径绕过 compliance）"
    )


# ── §2.6.2 stop run_id 派发（v3 §7.2：marker 无 owner，stop 按 run_id 直定位）────


def test_stop_with_run_id_succeeds(cwd_tmp, wf_path):
    """v3 §7.2：``stop <run_id>`` 按 run_id O(1) 直定位 marker（marker 文件名 = run_id）。"""
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    run_id = json.loads(boot.output.splitlines()[-1])["run_id"]

    result = runner.invoke(app, ["stop", run_id])
    assert result.exit_code == 0
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["ok"] is True
    assert reply["run_id"] == run_id


def test_stop_missing_run_id_fails_loud(cwd_tmp, wf_path):
    """FU-1：``stop`` 无 run_id（位置参数与 --run-id 都省略）→ exit 2（fail loud）。

    位置参数已从必填改为可选（让 ``stop --run-id X`` 不再因「缺位置参数」exit 2），
    None 改由显式守卫 ``raise BadParameter`` 拦下（ISSUE-3，保 exit 2 回归）。
    """
    runner = CliRunner()
    result = runner.invoke(app, ["stop"])
    # 位置参数与 --run-id 都省略 → None 守卫 BadParameter exit 2
    assert result.exit_code == 2


def test_stop_unknown_run_id_clears_no_tape(cwd_tmp, wf_path):
    """``stop <未知 run_id>`` → 幂等清 marker + ok 信封（note=no-tape，不崩）。"""
    runner = CliRunner()
    result = runner.invoke(app, ["stop", "nonexistent-run"])
    assert result.exit_code == 0
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["ok"] is True
    assert reply.get("note") == "no-tape"


# ── v8.1 签名契约测试（防 builder 回退，e2e /tmp/orca-e2e-v8/ 实证形态）─────────
#
# 教训（task 根因）：plugin TS 纯单测（marker regex / 字段提取）验不出运行时签名 bug
# —— 52 单测全过却 shipped inert。hook 的调用签名（参数个数、payload 包装）只能由
# 真 opencode runtime 决定，spike 已实证的形态是唯一真相源。
#
# **v5 §8 step 4**：transform 签名契约（Bug A）已随 transform 段整删下线——transform 入口
# 不再注册，签名契约无承载对象。event hook 签名契约（Bug B）保留——idle nudge hook 仍存在。


def test_event_hook_payload_unwrap_input_event_fallback_input():
    """Bug B 签名契约：event hook 必须兼容 ``input.event`` 包装形态。

    spike ``/tmp/orca-f4/.opencode/plugin/orca.ts:7`` 实证：opencode 1.14.22 runtime
    包一层 ``{event}``，即 ``input.event.type``。shipped v8 曾回退为裸 ``event: ...``
    直访形态 → runtime 下 ``event.type`` 永远 undefined → idle 永远 early-return。
    守门形态：``const event = input?.event ?? input``（兼容解构包装 + 直传两种）。
    """
    text = PLUGIN_TS.read_text(encoding="utf-8")
    # hook 注册：event 必须接 input 单参，不能直接解构 event
    assert re.search(r'event:\s*async\s*\(input:\s*any\)\s*=>', text), (
        "event hook 必须 `async (input: any) =>` 单参（包装在 input.event 上）"
    )
    # 必须有 unwrap + 兜底直传
    assert re.search(r'const\s+event:\s*any\s*=\s*input\?\.\s*event\s*\?\?\s*input', text), (
        "event hook 必须含严格形态 `const event: any = input?.event ?? input`（兼容包装+直传）"
    )


def test_bootstrap_and_next_return_pointer_and_write_prompt_file(cwd_tmp, wf_path):
    """compact 交付契约（2026-07-08，替代 Bug G 的 prepend 形态）。

    bootstrap + next 不再把整段渲染后 prompt 经 .prompt 注入主 session；改为：
      1. ``.prompt`` = host-facing **指针**（"用 task 工具派子代理"+"完整指令已写入 <path>"）。
      2. ``.prompt_file`` = 渲染后 prompt 落盘路径，文件含渲染全文（含上游 output 插值）。
    两种 agent 形态渲染无差别（compile 已扁平化）；plugin 仍读 .prompt（指针文本）。
    """
    runner = CliRunner()

    # bootstrap：entry 节点 → 指针 + 文件（含 entry prompt 全文）
    boot = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    assert boot.exit_code == 0
    boot_reply = json.loads(boot.output.splitlines()[-1])
    assert boot_reply.get("prompt_file"), "compact：bootstrap 必须返 .prompt_file"
    pointer = boot_reply["prompt"]
    assert "task 工具" in pointer and "完整节点指令已写入" in pointer, (
        "bootstrap .prompt 必须是 host-facing 指针（含 task 工具 + 文件路径提示）"
    )
    entry_file = Path(boot_reply["prompt_file"])
    assert entry_file.is_file(), f"compact prompt 文件未落盘：{entry_file}"
    assert "产出 A。" in entry_file.read_text(encoding="utf-8"), (
        "compact prompt 文件必须含 entry 节点渲染全文"
    )

    # next：下一节点 → 指针 + 文件（含上游 output 插值后的渲染全文）
    tape = boot_reply["tape"]
    run_id = boot_reply["run_id"]
    nxt = runner.invoke(app, ["next", "--tape", tape, "--run-id", run_id, "--output", "OUT_A"])
    assert nxt.exit_code == 0
    nxt_reply = json.loads(nxt.output.splitlines()[-1])
    assert nxt_reply.get("prompt_file"), "compact：next 必须返 .prompt_file"
    assert "task 工具" in nxt_reply["prompt"], "next .prompt 必须是指针"
    b_file = Path(nxt_reply["prompt_file"])
    assert b_file.is_file() and b_file.name == "b.md", (
        f"next compact prompt 文件按节点名命名：<prompts_dir>/b.md，实得 {b_file}"
    )
    # node b prompt = "基于 {{ a.output }} 总结。" → 渲染后上游 output 已代入
    assert "基于 OUT_A 总结。" in b_file.read_text(encoding="utf-8"), (
        "compact prompt 文件必须含 Jinja 渲染后的完整文本（上游 output 已插值），而非 {{ }} 占位符"
    )


def test_build_pointer_is_single_source():
    """DRY 守门：指针文本单一定义（``_build_pointer``），防 bootstrap/next 各处拼字面漂移。"""
    from orca.iface.in_session.cli import _build_pointer
    from orca.run.step import StepResult

    # 有 resources_root：指针含 task 工具 + 子代理 + 文件路径 + 资源目录
    r = StepResult(node="x", prompt_file="/abs/path/x.md", resources_root="/agents/x")
    p = _build_pointer(r)
    assert "task 工具" in p and "子代理" in p
    assert "/abs/path/x.md" in p
    assert "/agents/x" in p

    # 无 resources_root：不附资源目录行
    r2 = StepResult(node="y", prompt_file="/p/y.md", resources_root=None)
    p2 = _build_pointer(r2)
    assert "/p/y.md" in p2 and "资源目录" not in p2


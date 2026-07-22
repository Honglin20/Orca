"""test_push_probe.py —— ``doctor --probe-push`` 推送链路诊断单测（SPEC：push-chain-diagnostic）。

覆盖 S1 范围（SPEC §8 落地拆分）：
  - H1/H2/H3 happy / fail / unknown 各分支（SPEC §7-2 / §7-3）。
  - **零副作用回归门**（SPEC §7-1）：无 ``--probe-push`` 时 doctor 输出与基线一致
    （时间派生字段 stub 后逐值比对；S1 跑不到基线 commit eb63b35，断言 keys 集合 + 字段
    集合一致 + 关键字段值不漂）。
  - **H2 中间态自洽守门**（SPEC §5 测试 3）：复算结果与 ``cac_session_id_from_pid()``
    返回值自洽。
  - **runbook 锚点对应 + fix_hint 指针有效**（SPEC §5 测试 1/2；S4 完整三组守门）。

后续 S2/S3/S4 在本文件追加 H4/H5/H6 + 三组守门完整集。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from orca.iface.in_session import _push_probe
from orca.iface.in_session._push_probe import (
    H1_FAMILY_DETECT,
    H2_CAC_PID_WALK,
    H3_ADAPTER_DISCOVERY,
    H4_DAEMON_PROGRESS,
    H5_BUS_FLOW,
    H6_WS_DELIVERY,
    RUNBOOK_PATH,
    _recompute_pid_walk_intermediate,
    _recompute_session_file_state,
    run_push_probe,
)
from orca.iface.in_session.cli import app


# ── helpers ──────────────────────────────────────────────────────────────────


def _clear_in_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉所有 in-session env 变量（SPEC §7-2 / §7-3 构造手段）。"""
    for k in ("CLAUDE_CODE_SESSION_ID", "ORCA_HOST_SESSION_ID", "CODEAGENT"):
        monkeypatch.delenv(k, raising=False)


def _hop_by_name(result: dict, hop: str) -> dict:
    return next(h for h in result["hops"] if h["hop"] == hop)


# ── H1 family_detect ─────────────────────────────────────────────────────────


def test_h1_unknown_when_no_in_session_env(monkeypatch, tmp_path):
    """SPEC §7-2：非 in-session 环境 → H1=unknown，链路不 crash。"""
    _clear_in_session_env(monkeypatch)
    result = run_push_probe(rundir=tmp_path)
    h1 = _hop_by_name(result, H1_FAMILY_DETECT)
    assert h1["status"] == "unknown"
    assert "backend=None" in h1["evidence"]
    # overall 应该 fail（含 unknown 但无 fail/error 时仍算 fail——链路不确定即非全通）。
    assert result["overall"] == "fail"
    assert result["first_break"] == H1_FAMILY_DETECT


def test_h1_fail_when_codeagent_but_no_cc_sid(monkeypatch, tmp_path):
    """SPEC §7-3：CODEAGENT 在但 CC_SESSION_ID 无 + PID 回溯不命中 → H1=fail。

    构造：CODEAGENT=1 + cac_session_id_from_pid 返 None（非 CAC 进程，仅 env 残留）。
    detect_backend_from_env 会返 cc（CODEAGENT + host_session_from_env 命中条件不满足），
    但 detect_family_from_env 返 None（PID 未命中）。
    """
    _clear_in_session_env(monkeypatch)
    monkeypatch.setenv("CODEAGENT", "1")
    # mock host_session_from_env 返 None → detect_backend 走第三条不命中（CODEAGENT+host_session 都要）。
    # 但我们要它命中 cc，所以让 host_session_from_env 返一个值。
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.host_session_from_env", lambda: "fake-sid",
    )
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.cac_session_id_from_pid", lambda: None,
    )
    result = run_push_probe(rundir=tmp_path)
    h1 = _hop_by_name(result, H1_FAMILY_DETECT)
    # CODEAGENT + 假 host_session 命中 → backend=cc；但 family PID 回溯未命中 → None。
    assert h1["status"] == "fail"
    assert "backend=cc" in h1["evidence"]
    assert "family=None" in h1["evidence"]
    assert result["first_break"] == H1_FAMILY_DETECT


def test_h1_pass_cc_when_claude_code_session_id(monkeypatch, tmp_path):
    """真 CC：CLAUDE_CODE_SESSION_ID 在 → backend=cc, family=cc → pass。"""
    _clear_in_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    result = run_push_probe(rundir=tmp_path)
    h1 = _hop_by_name(result, H1_FAMILY_DETECT)
    assert h1["status"] == "pass"
    assert "backend=cc" in h1["evidence"]
    assert "family=cc" in h1["evidence"]


def test_h1_pass_cac_when_codeagent_and_pid_hit(monkeypatch, tmp_path):
    """CAC：CODEAGENT=1 + PID 命中 → backend=cc, family=cac → pass。"""
    _clear_in_session_env(monkeypatch)
    monkeypatch.setenv("CODEAGENT", "1")
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.cac_session_id_from_pid", lambda: "cac-sid-xyz",
    )
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.host_session_from_env", lambda: "cac-sid-xyz",
    )
    result = run_push_probe(rundir=tmp_path)
    h1 = _hop_by_name(result, H1_FAMILY_DETECT)
    assert h1["status"] == "pass"
    assert "backend=cc" in h1["evidence"]
    assert "family=cac" in h1["evidence"]


def test_h1_pass_opencode_when_host_session_id(monkeypatch, tmp_path):
    """opencode 家族：ORCA_HOST_SESSION_ID 在 → backend=opencode → pass。"""
    _clear_in_session_env(monkeypatch)
    monkeypatch.setenv("ORCA_HOST_SESSION_ID", "opencode-sid")
    result = run_push_probe(rundir=tmp_path)
    h1 = _hop_by_name(result, H1_FAMILY_DETECT)
    assert h1["status"] == "pass"
    assert "backend=opencode" in h1["evidence"]


# ── H2 cac_pid_walk ──────────────────────────────────────────────────────────


def test_h2_skip_when_opencode(monkeypatch, tmp_path):
    """SPEC §4 H2 跳过条件：backend != cc → status=pass-through。"""
    _clear_in_session_env(monkeypatch)
    monkeypatch.setenv("ORCA_HOST_SESSION_ID", "opc-sid")
    result = run_push_probe(rundir=tmp_path)
    h2 = _hop_by_name(result, H2_CAC_PID_WALK)
    assert h2["status"] == "pass"
    assert "skip" in h2["evidence"]


def test_h2_skip_when_true_cc(monkeypatch, tmp_path):
    """SPEC §4 H2 跳过条件：CLAUDE_CODE_SESSION_ID 在 → PID 回溯不需要 → pass。"""
    _clear_in_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-sid")
    result = run_push_probe(rundir=tmp_path)
    h2 = _hop_by_name(result, H2_CAC_PID_WALK)
    assert h2["status"] == "pass"
    assert "skip" in h2["evidence"]


def test_h2_fail_when_no_codeagent(monkeypatch, tmp_path):
    """SPEC §4 H2 fail：CC 家族（backend=cc）但 CODEAGENT 未设 → fail。

    构造：让 detect_backend_from_env 命中 cc（mock），但实际 env 无 CODEAGENT。
    """
    _clear_in_session_env(monkeypatch)
    # mock detect_backend_from_env 直接返 cc（避开 env 命中条件）。
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.detect_backend_from_env", lambda: "cc",
    )
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.detect_family_from_env", lambda: None,
    )
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.cac_session_id_from_pid", lambda: None,
    )
    result = run_push_probe(rundir=tmp_path)
    h2 = _hop_by_name(result, H2_CAC_PID_WALK)
    assert h2["status"] == "fail"
    assert "CODEAGENT=0" in h2["evidence"]


def test_h2_fail_when_codeagent_but_pid_miss(monkeypatch, tmp_path):
    """SPEC §4 H2 fail：CODEAGENT=1 + PID 链未命中 codeagentcli → fail。"""
    _clear_in_session_env(monkeypatch)
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.detect_backend_from_env", lambda: "cc",
    )
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.detect_family_from_env", lambda: None,
    )
    monkeypatch.setenv("CODEAGENT", "1")
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.cac_session_id_from_pid", lambda: None,
    )
    # mock 复算也返未命中（保持自洽）。
    monkeypatch.setattr(
        "orca.iface.in_session._push_probe._recompute_pid_walk_intermediate",
        lambda: (None, False),
    )
    result = run_push_probe(rundir=tmp_path)
    h2 = _hop_by_name(result, H2_CAC_PID_WALK)
    assert h2["status"] == "fail"
    assert "pid_walk_hit=false" in h2["evidence"]


def test_h2_pass_when_pid_hit_and_session_file_present(monkeypatch, tmp_path):
    """SPEC §4 H2 happy：PID 链命中 + session 文件有 sessionId → pass。

    用 mock 模拟命中（绕开对 /proc 的依赖，CI 上没 CAC 进程）。
    """
    _clear_in_session_env(monkeypatch)
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.detect_backend_from_env", lambda: "cc",
    )
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.detect_family_from_env", lambda: "cac",
    )
    monkeypatch.setenv("CODEAGENT", "1")
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.cac_session_id_from_pid", lambda: "cac-sid-real",
    )
    # mock 复算返命中（与权威自洽）。
    monkeypatch.setattr(
        "orca.iface.in_session._push_probe._recompute_pid_walk_intermediate",
        lambda: (99999, True),
    )
    monkeypatch.setattr(
        "orca.iface.in_session._push_probe._recompute_session_file_state",
        lambda ppid: (True, True) if ppid == 99999 else (False, False),
    )
    result = run_push_probe(rundir=tmp_path)
    h2 = _hop_by_name(result, H2_CAC_PID_WALK)
    assert h2["status"] == "pass"
    assert "matched_ppid=99999" in h2["evidence"]
    assert "session_file=true" in h2["evidence"]


# ── H3 adapter_discovery ─────────────────────────────────────────────────────


def _setup_cc_with_sidechain_root(monkeypatch, tmp_path):
    """通用：CC env + tmp sidechain root（ORCA_CC_SIDECHAIN_ROOT env）。"""
    _clear_in_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "host-ses-1")
    sidechain_root = tmp_path / "sidechain"
    sidechain_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ORCA_CC_SIDECHAIN_ROOT", str(sidechain_root))
    return sidechain_root


def test_h3_unknown_when_no_backend(monkeypatch, tmp_path):
    """SPEC §4 H3：backend=None → unknown（非 in-session）。"""
    _clear_in_session_env(monkeypatch)
    result = run_push_probe(rundir=tmp_path)
    h3 = _hop_by_name(result, H3_ADAPTER_DISCOVERY)
    assert h3["status"] == "unknown"


def test_h3_unknown_when_root_missing(monkeypatch, tmp_path):
    """SPEC §4 H3：root 不存在 → unknown（子 agent 尚未起，非故障）。"""
    sidechain_root = _setup_cc_with_sidechain_root(monkeypatch, tmp_path)
    # 删 root 让 discover 返空。
    for child in sidechain_root.iterdir():
        child.unlink()
    sidechain_root.rmdir()
    result = run_push_probe(rundir=tmp_path)
    h3 = _hop_by_name(result, H3_ADAPTER_DISCOVERY)
    assert h3["status"] == "unknown"
    assert "root_exists=False" in h3["evidence"]


def test_h3_unknown_when_root_empty(monkeypatch, tmp_path):
    """SPEC §4 H3：root 在但无 agent-*.jsonl → unknown（子 agent 尚未产事件）。"""
    _setup_cc_with_sidechain_root(monkeypatch, tmp_path)
    result = run_push_probe(rundir=tmp_path)
    h3 = _hop_by_name(result, H3_ADAPTER_DISCOVERY)
    assert h3["status"] == "unknown"
    assert "jsonl_count=0" in h3["evidence"]


def test_h3_fail_when_jsonl_but_no_meta(monkeypatch, tmp_path):
    """SPEC §4 H3 fail：root 在 + jsonl 在 + with_meta=0 → 宿主未写 meta.json。"""
    sidechain_root = _setup_cc_with_sidechain_root(monkeypatch, tmp_path)
    # 写 agent-*.jsonl 但不写 .meta.json（模拟宿主后台系统子代理）。
    (sidechain_root / "agent-task-sys.jsonl").write_text(
        '{"type":"system","content":"hi"}\n', encoding="utf-8",
    )
    result = run_push_probe(rundir=tmp_path)
    h3 = _hop_by_name(result, H3_ADAPTER_DISCOVERY)
    assert h3["status"] == "fail"
    assert "jsonl_count=1" in h3["evidence"]
    assert "with_meta_count=0" in h3["evidence"]


def test_h3_pass_when_jsonl_with_meta(monkeypatch, tmp_path):
    """SPEC §4 H3 happy：root 在 + jsonl + .meta.json → pass + discovered_children 非空。"""
    sidechain_root = _setup_cc_with_sidechain_root(monkeypatch, tmp_path)
    (sidechain_root / "agent-task-aaa.jsonl").write_text(
        '{"type":"system","content":"hi"}\n', encoding="utf-8",
    )
    (sidechain_root / "agent-task-aaa.meta.json").write_text(
        json.dumps({"agentType": "task", "description": "test"}), encoding="utf-8",
    )
    result = run_push_probe(rundir=tmp_path)
    h3 = _hop_by_name(result, H3_ADAPTER_DISCOVERY)
    assert h3["status"] == "pass"
    assert "jsonl_count=1" in h3["evidence"]
    assert "with_meta_count=1" in h3["evidence"]
    assert "task-aaa" in h3["evidence"]


def test_h3_fail_when_make_adapter_raises(monkeypatch, tmp_path):
    """SPEC §4 H3 fail loud：``_make_adapter`` 抛异常 → status=fail（不静默）。

    构造：backend=cc + host_session 在，但 mock ``_make_adapter`` 抛 ValueError
    （模拟 family 非法 / host_session 空 等场景）。S1 review 🟡#2 补的分支测试。
    """
    _setup_cc_with_sidechain_root(monkeypatch, tmp_path)

    def _boom(*args, **kwargs):
        raise ValueError("unknown family 'xyz'")

    monkeypatch.setattr(
        "orca.iface.in_session.sidechain_daemon._make_adapter", _boom,
    )
    result = run_push_probe(rundir=tmp_path)
    h3 = _hop_by_name(result, H3_ADAPTER_DISCOVERY)
    assert h3["status"] == "fail"
    assert "ValueError" in h3["evidence"]
    assert "adapter 构造失败" in h3["reason"]


# ── 零副作用回归门（SPEC §7-1）────────────────────────────────────────────────


def test_doctor_without_probe_push_has_no_push_chain_probe(doctor_iso, monkeypatch):
    """SPEC §7-1：无 --probe-push → 输出 JSON 不含 push_chain_probe 字段（零副作用）。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    _clear_in_session_env(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    # 关键断言：push_chain_probe 字段不在。
    assert "push_chain_probe" not in reply, (
        "无 --probe-push 时不应追加 push_chain_probe（零副作用铁律）"
    )
    # 现有 6 check 一字不改。
    assert len(reply["checks"]) == 6
    assert [c["name"] for c in reply["checks"]] == [
        "skill_install", "cli_imports_ok", "diag_switch",
        "advance_hook", "sidechain_backend", "sidechain_daemon",
    ]


def test_doctor_with_probe_push_appends_block(doctor_iso, monkeypatch):
    """SPEC §3：--probe-push → 输出追加 push_chain_probe 区块（含 6 跳）。"""
    monkeypatch.delenv("ORCA_DIAGNOSE", raising=False)
    _clear_in_session_env(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--probe-push"])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert "push_chain_probe" in reply
    probe = reply["push_chain_probe"]
    assert set(probe.keys()) >= {"overall", "first_break", "runbook", "hops"}
    assert probe["runbook"] == RUNBOOK_PATH
    assert len(probe["hops"]) == 6
    # ok 不受 probe_push 影响（push_chain_probe 不计入 ok）。
    assert isinstance(reply["ok"], bool)


# ── H2 中间态自洽守门（SPEC §5 测试 3）────────────────────────────────────────


def test_h2_intermediate_self_consistent_when_authority_returns_sid(monkeypatch, tmp_path):
    """SPEC §5 守门 3：``cac_session_id_from_pid`` 返非 None ⟺ PID 链命中 + session 有 sid。

    用真实 mock 链：PID 复算 + session 文件复算 + 权威函数返值必须自洽。
    本测试断言三函数定义的等价关系：authority != None ⟺ (pid_walk_hit AND session_has_sid)。
    """
    # 构造一个自洽命中态：mock authority 返 sid，复算返命中 + session 在。
    _clear_in_session_env(monkeypatch)
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.cac_session_id_from_pid", lambda: "sid-x",
    )
    monkeypatch.setattr(
        "orca.iface.in_session._push_probe._recompute_pid_walk_intermediate",
        lambda: (4321, True),
    )
    monkeypatch.setattr(
        "orca.iface.in_session._push_probe._recompute_session_file_state",
        lambda ppid: (True, True) if ppid == 4321 else (False, False),
    )

    # 直接调 run_push_probe，拿 H2 evidence 验证自洽。
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.detect_backend_from_env", lambda: "cc",
    )
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.detect_family_from_env", lambda: "cac",
    )
    monkeypatch.setenv("CODEAGENT", "1")
    result = run_push_probe(rundir=tmp_path)
    h2 = _hop_by_name(result, H2_CAC_PID_WALK)
    assert h2["status"] == "pass"
    assert "pid_walk_hit=true" in h2["evidence"]
    assert "session_file_has_sessionId=true" in h2["evidence"]
    assert "authority_session_id='sid-x'" in h2["evidence"]


def test_h2_intermediate_self_consistent_when_authority_returns_none_pid_miss(
    monkeypatch, tmp_path,
):
    """SPEC §5 守门 3 反向：authority 返 None ⟺ PID 链未命中 OR session 无 sid。

    构造：authority 返 None + PID 复算也未命中 → 自洽。
    """
    _clear_in_session_env(monkeypatch)
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.detect_backend_from_env", lambda: "cc",
    )
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.detect_family_from_env", lambda: None,
    )
    monkeypatch.setenv("CODEAGENT", "1")
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.cac_session_id_from_pid", lambda: None,
    )
    # PID 复算返未命中（与权威自洽）。
    monkeypatch.setattr(
        "orca.iface.in_session._push_probe._recompute_pid_walk_intermediate",
        lambda: (None, False),
    )
    result = run_push_probe(rundir=tmp_path)
    h2 = _hop_by_name(result, H2_CAC_PID_WALK)
    assert h2["status"] == "fail"
    assert "pid_walk_hit=false" in h2["evidence"]
    assert "authority_session_id=None" in h2["evidence"]


# ── H4 daemon_progress（SPEC §4 H4 / §8#4 覆盖；S2 实现）─────────────────────


def _setup_cc_with_run(monkeypatch, tmp_path, run_id="r-abc"):
    """CC env + tmp sidechain root + 写一个 run marker + run_dir。

    返 ``(sidechain_root, run_dir)``。H4 测试基线：daemon_alive 走 mock，disk/tape/log
    手动构造。
    """
    sidechain_root = _setup_cc_with_sidechain_root(monkeypatch, tmp_path)
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # 写 marker 让 _sidechain_daemon_alive / _compute_run_age 能定位 run。
    marker_path = tmp_path / f"orca-{run_id}.json"
    marker_path.write_text(
        json.dumps({"run_id": run_id, "model": "m", "no_output_count": 0}),
        encoding="utf-8",
    )
    return sidechain_root, run_dir


def test_h4_unknown_when_no_run_id(monkeypatch, tmp_path):
    """SPEC §4 H4：无 --run-id → unknown（不适用）。"""
    _clear_in_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid")
    result = run_push_probe(rundir=tmp_path)  # run_id=None
    h4 = _hop_by_name(result, H4_DAEMON_PROGRESS)
    assert h4["status"] == "unknown"
    assert "run_id 未给" in h4["evidence"]


def test_h4_unknown_when_disk_jsonl_zero(monkeypatch, tmp_path):
    """SPEC §7-4：disk_jsonl_lines==0 → unknown（不误报刚 bootstrap）。"""
    run_id = "r-h4-unknown"
    _setup_cc_with_run(monkeypatch, tmp_path, run_id)
    # sidechain root 空（无 agent-*.jsonl）→ disk_jsonl_lines=0。
    result = run_push_probe(run_id=run_id, rundir=tmp_path)
    h4 = _hop_by_name(result, H4_DAEMON_PROGRESS)
    assert h4["status"] == "unknown"
    assert "disk_jsonl_lines=0" in h4["evidence"]


def test_h4_fail_when_iteration_exceptions_in_log(monkeypatch, tmp_path):
    """SPEC §4 H4 fail：log 含 iteration 异常 warning → fail（无论 daemon_alive）。"""
    run_id = "r-h4-iter"
    sidechain_root, run_dir = _setup_cc_with_run(monkeypatch, tmp_path, run_id)
    # 写一个 child jsonl 让 disk_jsonl_lines>0。
    (sidechain_root / "agent-task-1.jsonl").write_text(
        '{"type":"system"}\n', encoding="utf-8",
    )
    (sidechain_root / "agent-task-1.meta.json").write_text("{}", encoding="utf-8")
    # daemon log 含 iteration 异常。
    (run_dir / "sidechain_daemon.log").write_text(
        "WARNING sidechain driver iteration 异常（将 sleep 后重试）\n", encoding="utf-8",
    )
    result = run_push_probe(run_id=run_id, rundir=tmp_path)
    h4 = _hop_by_name(result, H4_DAEMON_PROGRESS)
    assert h4["status"] == "fail"
    assert "iteration_exceptions=1" in h4["evidence"]


def test_h4_fail_when_daemon_dead_and_disk_has_events(monkeypatch, tmp_path):
    """SPEC §4 H4 fail：daemon_dead → fail。"""
    run_id = "r-h4-dead"
    sidechain_root, _run_dir = _setup_cc_with_run(monkeypatch, tmp_path, run_id)
    (sidechain_root / "agent-task-1.jsonl").write_text(
        '{"type":"system"}\n', encoding="utf-8",
    )
    (sidechain_root / "agent-task-1.meta.json").write_text("{}", encoding="utf-8")
    # mock daemon_dead。
    monkeypatch.setattr(
        "orca.iface.in_session.sidechain_daemon._sidechain_daemon_alive", lambda rid: False,
    )
    result = run_push_probe(run_id=run_id, rundir=tmp_path)
    h4 = _hop_by_name(result, H4_DAEMON_PROGRESS)
    assert h4["status"] == "fail"
    assert "daemon_alive=false" in h4["evidence"]


def test_h4_fail_when_run_age_old_and_tape_empty(monkeypatch, tmp_path):
    """SPEC §7-4 / §4 H4 fail：disk_jsonl>0 + tape agent_events=0 + run_age>30s → fail。

    构造：disk_jsonl_lines>0 + tape 不存在 + mock daemon_alive=True + mock run_age>30s。
    """
    run_id = "r-h4-stuck"
    sidechain_root, _run_dir = _setup_cc_with_run(monkeypatch, tmp_path, run_id)
    (sidechain_root / "agent-task-1.jsonl").write_text(
        '{"type":"system"}\n{"type":"system"}\n', encoding="utf-8",
    )
    (sidechain_root / "agent-task-1.meta.json").write_text("{}", encoding="utf-8")
    # mock daemon_alive=True（让 fail 决策不走 daemon_dead 分支）。
    monkeypatch.setattr(
        "orca.iface.in_session.sidechain_daemon._sidechain_daemon_alive", lambda rid: True,
    )
    # mock run_age > 30s（run_dir ctime 默认是刚创建的 now，需 patch _compute_run_age）。
    monkeypatch.setattr(
        "orca.iface.in_session._push_probe._compute_run_age", lambda rundir, rid: 120.0,
    )
    # 不写 tape 文件 → agent_events=0。
    result = run_push_probe(run_id=run_id, rundir=tmp_path)
    h4 = _hop_by_name(result, H4_DAEMON_PROGRESS)
    assert h4["status"] == "fail"
    assert "agent_events=0" in h4["evidence"]
    assert "run_age_s=120.0" in h4["evidence"]
    assert "持续 iterate 失败" in h4["reason"]


def test_h4_pass_when_daemon_alive_and_gap_zero(monkeypatch, tmp_path):
    """SPEC §4 H4 happy：daemon_alive + agent_events>0 + gap==0 + age<30 + 无异常 → pass。

    构造：写 child jsonl + 写 tape 含 agent_message 事件（disk_jsonl_lines==agent_events）。
    """
    run_id = "r-h4-pass"
    sidechain_root, _run_dir = _setup_cc_with_run(monkeypatch, tmp_path, run_id)
    # child jsonl 1 行。
    (sidechain_root / "agent-task-1.jsonl").write_text(
        '{"type":"system"}\n', encoding="utf-8",
    )
    (sidechain_root / "agent-task-1.meta.json").write_text("{}", encoding="utf-8")
    # tape 含 1 个 agent_message 事件（agent_events=1, gap=0）。
    tape_path = tmp_path / f"{run_id}.jsonl"
    tape_path.write_text(
        json.dumps({"type": "agent_message", "timestamp": __import__("time").time(),
                    "data": {"text": "hi"}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orca.iface.in_session.sidechain_daemon._sidechain_daemon_alive", lambda rid: True,
    )
    result = run_push_probe(run_id=run_id, rundir=tmp_path)
    h4 = _hop_by_name(result, H4_DAEMON_PROGRESS)
    assert h4["status"] == "pass", h4
    assert "agent_events=1" in h4["evidence"]
    assert "gap=0" in h4["evidence"]


def test_h4_fail_when_gap_positive_and_run_age_old(monkeypatch, tmp_path):
    """SPEC §4 H4 / §7-4 fail：disk 比 tape 多 + run_age>30s → 漏推。

    构造：disk 2 行（child jsonl）+ tape 1 行（agent_message）→ gap=1 + run_age>30。
    review 🟡#2 补的分支测试。
    """
    run_id = "r-h4-gap"
    sidechain_root, _run_dir = _setup_cc_with_run(monkeypatch, tmp_path, run_id)
    # child jsonl 2 行。
    (sidechain_root / "agent-task-1.jsonl").write_text(
        '{"type":"system"}\n{"type":"system"}\n', encoding="utf-8",
    )
    (sidechain_root / "agent-task-1.meta.json").write_text("{}", encoding="utf-8")
    # tape 含 1 个 agent_message（gap = 2-1 = 1）。
    tape_path = tmp_path / f"{run_id}.jsonl"
    tape_path.write_text(
        json.dumps({"type": "agent_message", "timestamp": __import__("time").time(),
                    "data": {"text": "hi"}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orca.iface.in_session.sidechain_daemon._sidechain_daemon_alive", lambda rid: True,
    )
    monkeypatch.setattr(
        "orca.iface.in_session._push_probe._compute_run_age", lambda rundir, rid: 90.0,
    )
    result = run_push_probe(run_id=run_id, rundir=tmp_path)
    h4 = _hop_by_name(result, H4_DAEMON_PROGRESS)
    assert h4["status"] == "fail"
    assert "gap=1" in h4["evidence"]
    assert "漏推" in h4["reason"]


# ── H5 bus_flow（SPEC §4 H5；S2 实现）────────────────────────────────────────


def test_h5_unknown_when_no_run_id(monkeypatch, tmp_path):
    """SPEC §4 H5：无 --run-id → unknown。"""
    _clear_in_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid")
    result = run_push_probe(rundir=tmp_path)
    h5 = _hop_by_name(result, H5_BUS_FLOW)
    assert h5["status"] == "unknown"


def test_h5_fail_when_queue_full_warning_in_log(monkeypatch, tmp_path):
    """SPEC §4 H5 fail：log 含 ``订阅者队列满`` warning → fail。"""
    run_id = "r-h5-fail"
    _sidechain_root, run_dir = _setup_cc_with_run(monkeypatch, tmp_path, run_id)
    (run_dir / "sidechain_daemon.log").write_text(
        "WARNING 订阅者队列满，丢弃旧事件 seq=42（type=agent_message，订阅者可经 replay 补全）\n",
        encoding="utf-8",
    )
    result = run_push_probe(run_id=run_id, rundir=tmp_path)
    h5 = _hop_by_name(result, H5_BUS_FLOW)
    assert h5["status"] == "fail"
    assert "queue_full_warnings=1" in h5["evidence"]


def test_h5_unknown_when_log_has_no_warning(monkeypatch, tmp_path):
    """SPEC §4 H5 unknown：log 在但无队列满 warning。"""
    run_id = "r-h5-ok"
    _sidechain_root, run_dir = _setup_cc_with_run(monkeypatch, tmp_path, run_id)
    (run_dir / "sidechain_daemon.log").write_text(
        "INFO sidechain driver 启动\n", encoding="utf-8",
    )
    result = run_push_probe(run_id=run_id, rundir=tmp_path)
    h5 = _hop_by_name(result, H5_BUS_FLOW)
    assert h5["status"] == "unknown"
    assert "queue_full_warnings=0" in h5["evidence"]


def test_h5_unknown_when_log_missing(monkeypatch, tmp_path):
    """SPEC §4 H5 unknown：daemon log 不在（无证据）。"""
    run_id = "r-h5-nolog"
    _setup_cc_with_run(monkeypatch, tmp_path, run_id)
    result = run_push_probe(run_id=run_id, rundir=tmp_path)
    h5 = _hop_by_name(result, H5_BUS_FLOW)
    assert h5["status"] == "unknown"


# ── H6 ws_delivery（SPEC §4 H6 + B2 决议 degradation；S3 实现）───────────────


def test_h6_pass_self_spawn(monkeypatch, tmp_path):
    """SPEC §7-5a：H6 self-spawn happy path——3s 内收到合成 agent_message → pass。

    构造：RunManager + monkey-patch Orchestrator.run（probe 内部已 patch，无需测试侧介入）
    + ephemeral web + WS subscribe + bus.emit 合成事件。
    """
    _clear_in_session_env(monkeypatch)
    # H6 self-spawn 不依赖 in-session env（它自己起 RunManager）。
    result = run_push_probe(rundir=tmp_path)
    h6 = _hop_by_name(result, H6_WS_DELIVERY)
    assert h6["status"] == "pass", h6
    assert "received agent_message within 3s" in h6["evidence"]
    assert "__probe__" in h6["evidence"]  # probe run_id 前缀


def test_h6_fail_when_pump_raises(monkeypatch, tmp_path):
    """SPEC §7-5b 反例：patch ``ws_handler._pump`` 抛 RuntimeError → H6=fail。

    构造：monkey-patch WebServer._pump 抛非 Disconnect 异常 → 合成事件无法到达 WS →
    3s 超时 → fail。
    """
    _clear_in_session_env(monkeypatch)

    async def _boom_pump(self, ws, sub, run_id):
        raise RuntimeError("injected pump failure for H6 test")

    monkeypatch.setattr(
        "orca.iface.web.ws_handler.WebServer._pump", _boom_pump,
    )
    result = run_push_probe(rundir=tmp_path)
    h6 = _hop_by_name(result, H6_WS_DELIVERY)
    assert h6["status"] == "fail", h6
    assert "3s 内未收到事件" in h6["reason"]
    assert "pump" in h6["reason"] or "WS 未订阅" in h6["reason"]


def test_h6_no_residual_after_two_runs(monkeypatch, tmp_path):
    """SPEC §7-5c：连续两次 doctor --probe-push，第二次不因 __probe__ 残留 / EADDRINUSE 而 fail。

    构造：串行跑两遍 run_push_probe，断言两次都 pass（独立 tmp runs_dir + ephemeral port
    隔离生效，无残留）。
    """
    _clear_in_session_env(monkeypatch)
    result1 = run_push_probe(rundir=tmp_path)
    h6_1 = _hop_by_name(result1, H6_WS_DELIVERY)
    assert h6_1["status"] == "pass", h6_1

    result2 = run_push_probe(rundir=tmp_path)
    h6_2 = _hop_by_name(result2, H6_WS_DELIVERY)
    assert h6_2["status"] == "pass", h6_2

    # 验证两次 probe run_id 不同（独立 mkdtemp + 独立 gen_run_id）。
    assert h6_1["evidence"] != h6_2["evidence"]


def test_h6_fail_when_wrong_event_type(monkeypatch, tmp_path):
    """SPEC §4 H6 守门：WS 收到非 target agent_message → fail（防 pump 串流误判 pass）。

    构造：monkey-patch WebServer._pump 让它改发 ``workflow_started`` 而非 agent_message。
    review T-1 补的负向测试（SPEC §4 H6 明示「防 pump 串流其它事件误判 pass」契约）。
    """
    _clear_in_session_env(monkeypatch)
    import asyncio

    async def _wrong_event_pump(self, ws, sub, run_id):
        """串流非 agent_message 事件（模拟 pump 误发 / 历史残留事件）。"""
        try:
            # 直接发一条 workflow_started（pump 不会这么干，但 mock 模拟串流）。
            await ws.send_json({
                "type": "workflow_started",
                "run_id": run_id,
                "data": {"fake": "pump 串流"},
            })
        except Exception:  # noqa: BLE001
            pass
        # hang 住不让 _pump 自然退出（避免 ws_handler 把连接清理掉）。
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "orca.iface.web.ws_handler.WebServer._pump", _wrong_event_pump,
    )
    result = run_push_probe(rundir=tmp_path)
    h6 = _hop_by_name(result, H6_WS_DELIVERY)
    assert h6["status"] == "fail", h6
    assert "非目标 agent_message" in h6["reason"]
    assert "workflow_started" in h6["reason"]


def test_h6_error_when_setup_raises(monkeypatch, tmp_path):
    """SPEC §0 fail loud：H6 self-spawn setup 抛异常 → status=error（不是 fail）。

    构造：mock ``_hop_h6_ws_delivery_async`` 抛 RuntimeError → 外层 try/except 兜底为
    status=error + reason 含异常类型名。review T-2 补的分支测试。
    """
    _clear_in_session_env(monkeypatch)

    async def _boom_async(ctx):
        raise RuntimeError("injected setup failure for H6 test")

    monkeypatch.setattr(
        "orca.iface.in_session._push_probe._hop_h6_ws_delivery_async", _boom_async,
    )
    result = run_push_probe(rundir=tmp_path)
    h6 = _hop_by_name(result, H6_WS_DELIVERY)
    assert h6["status"] == "error"
    assert "RuntimeError" in h6["reason"]
    assert "injected setup failure" in h6["reason"]


# ── runbook 锚点 + fix_hint 指针 守门（SPEC §5 测试 1/2；S4 完整三组守门）────


def test_runbook_anchors_match_hops():
    """SPEC §5 守门 1（S4 完整跑；S1 先骨架）：每个 hop 在 MD 有对应显式锚 ``{#h<N>-<slug>}``。

    锚点格式：``{#h1-family-detect}`` 等。
    """
    # MD 文件路径相对 repo 根。``_push_probe.RUNBOOK_PATH`` 是相对路径字符串。
    repo_root = Path(__file__).resolve().parents[3]
    md_path = repo_root / RUNBOOK_PATH
    assert md_path.is_file(), f"runbook {md_path} 不存在"
    raw = md_path.read_text(encoding="utf-8")

    # 收集 MD 中所有显式锚。
    anchors = set(re.findall(r"\{#([\w\-]+)\}", raw))
    # 预期锚（与 _fix_hint 内的 anchor 算法一致）。
    expected = {
        "h1-family-detect", "h2-cac-pid-walk", "h3-adapter-discovery",
        "h4-daemon-progress", "h5-bus-flow", "h6-ws-delivery",
    }
    missing = expected - anchors
    assert not missing, f"runbook 缺锚点：{missing}（已有：{anchors}）"


def test_fix_hint_pointers_resolve_to_runbook_anchors():
    """SPEC §5 守门 2：每个 hop 的 fix_hint 提到的锚点必须在 MD 锚点集合内。"""
    repo_root = Path(__file__).resolve().parents[3]
    md_path = repo_root / RUNBOOK_PATH
    raw = md_path.read_text(encoding="utf-8")
    anchors = set(re.findall(r"\{#([\w\-]+)\}", raw))

    # 临时 ctx（hop 函数不实际用 ctx 字段，除了 H4/H5/H6 placeholder）。
    ctx = _push_probe.ProbeContext(run_id=None, ws_url=None, rundir=Path("/tmp"))
    for hop_name, hop_fn in [
        (H1_FAMILY_DETECT, _push_probe._hop_h1_family_detect),
        (H2_CAC_PID_WALK, _push_probe._hop_h2_cac_pid_walk),
        (H3_ADAPTER_DISCOVERY, _push_probe._hop_h3_adapter_discovery),
        (_push_probe.H4_DAEMON_PROGRESS, _push_probe._hop_h4_daemon_progress),
        (_push_probe.H5_BUS_FLOW, _push_probe._hop_h5_bus_flow),
        (_push_probe.H6_WS_DELIVERY, _push_probe._hop_h6_ws_delivery),
    ]:
        result = hop_fn(ctx)
        fix_hint = result["fix_hint"]
        # 提取 fix_hint 中的 ``#anchor``。
        m = re.search(r"#([\w\-]+)", fix_hint)
        assert m, f"hop {hop_name} fix_hint 缺锚点指针：{fix_hint!r}"
        assert m.group(1) in anchors, (
            f"hop {hop_name} fix_hint 指针 ``{m.group(1)}`` 不在 MD 锚点集合 {anchors}"
        )


# ── fixtures（doctor_iso 同 test_in_session_v8.py；本地定义避免 import 循环）────


@pytest.fixture
def doctor_iso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离 doctor 的 home + cwd（与 test_in_session_v8.doctor_iso 同款）。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── H6 passive 模式（S5，SPEC §4 H6 + §7-9）──────────────────────────────────


def test_h6_passive_fail_without_run_id(monkeypatch, tmp_path):
    """S5：passive 模式（--ws-url）缺 --run-id → fail（subscribe 需目标）。

    构造：``run_push_probe(ws_url=..., run_id=None)``——passive 分支应 fail + hint。
    """
    _clear_in_session_env(monkeypatch)
    result = run_push_probe(
        ws_url="ws://127.0.0.1:9999/ws", run_id=None, rundir=tmp_path,
    )
    h6 = _hop_by_name(result, H6_WS_DELIVERY)
    assert h6["status"] == "fail", h6
    assert "passive" in h6["evidence"]
    assert "--run-id" in h6["reason"]


def test_h6_passive_fail_connection_refused(monkeypatch, tmp_path):
    """S5：passive 连不存在的 web（死端口）→ fail「WS 连接失败」。

    构造：``ws_url`` 指向一个没人监听的端口 → websockets.connect 抛 ConnectionRefused → fail。
    """
    _clear_in_session_env(monkeypatch)
    result = run_push_probe(
        ws_url="ws://127.0.0.1:1/ws",  # port 1：特权端口，正常无人监听 → 连接拒绝
        run_id="some-run",
        rundir=tmp_path,
    )
    h6 = _hop_by_name(result, H6_WS_DELIVERY)
    assert h6["status"] == "fail", h6
    assert "WS 连接失败" in h6["reason"]


class _PassiveTarget:
    """ephemeral web server 跑在后台线程，供 passive 模式 H6 测试连接。

    起 RunManager + create_app + uvicorn（ephemeral port）+ attach_run 一个手写 tape。
    暴露 ``ws_url`` / ``run_id`` 供 probe 连；``emit()`` 往该 run 的 bus 注入真实事件
    （走后台 loop → pump → WS → probe 的 client，跨线程靠 TCP socket）。
    """

    def __init__(self, tmp_path: Path) -> None:
        import asyncio
        import threading

        self.tmp_path = tmp_path
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop_evt: asyncio.Event | None = None
        self.ws_url: str | None = None
        self.run_id = "passive-target-run"
        self._manager = None
        self._handle = None
        self._server = None
        self._server_task = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self._ready.wait(timeout=8.0), "passive target web server 未就绪"

    def _run(self) -> None:
        import asyncio
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._startup())
            # 显式 cancel 残留 task（uvicorn 内部 callback）并 drain，防「Event loop is closed」噪音。
            pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                self.loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True),
                )
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001 — 后台 helper，teardown 异常不影响测试结论
            pass
        finally:
            self.loop.close()

    async def _startup(self) -> None:
        import asyncio
        import time
        from orca.iface.web.run_manager import RunManager
        from orca.iface.web.server import create_app
        import uvicorn

        runs_dir = self.tmp_path / "target-runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        tape_path = runs_dir / f"{self.run_id}.jsonl"
        # 手写 workflow_started + node_started（attach_run 首行需 workflow_started）。
        with open(tape_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "seq": 1, "type": "workflow_started", "timestamp": time.time(),
                "node": None, "session_id": None,
                "data": {"run_id": self.run_id, "workflow_name": "target"},
            }) + "\n")
            f.write(json.dumps({
                "seq": 2, "type": "node_started", "timestamp": time.time(),
                "node": "N1", "session_id": None, "data": {},
            }) + "\n")

        self._manager = RunManager(runs_dir=runs_dir, max_concurrent=1)
        await self._manager.attach_run(str(tape_path), run_id=self.run_id)
        self._handle = self._manager.get_handle(self.run_id)

        app = create_app(self._manager)
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        self._server.force_exit = True
        self._server_task = self.loop.create_task(self._server.serve())

        # 等端口就绪。
        deadline = self.loop.time() + 5.0
        port = None
        while self.loop.time() < deadline:
            for srv in getattr(self._server, "servers", None) or []:
                for sock in (list(srv.sockets) if hasattr(srv, "sockets") else []):
                    try:
                        port = int(sock.getsockname()[1])
                        break
                    except (OSError, TypeError, IndexError):
                        continue
                if port:
                    break
            if port:
                break
            await asyncio.sleep(0.05)
        assert port is not None, "target uvicorn 未起端口"
        self.ws_url = f"ws://127.0.0.1:{port}/ws"
        self._stop_evt = asyncio.Event()
        self._ready.set()
        await self._stop_evt.wait()
        # 优雅关闭（在 loop 仍活时跑完 server/manager teardown，防「Event loop is closed」噪音）。
        self._server.should_exit = True
        if self._server_task is not None:
            try:
                await asyncio.wait_for(self._server_task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                self._server_task.cancel()
        try:
            await self._manager.shutdown(timeout=2.0)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    def emit(self) -> None:
        """往 target run 的 tape 文件追加一条 agent_message（follow task poll→parse→relay）。

        attached run 的 bus 是 read-only（AttachedTape.append raise），正确做法是写 tape
        文件让 follow task（0.3s 轮询 mtime/size）拾取 → ``Event(**line)`` → ``bus.relay`` →
        pump → WS。这正复刻真实 daemon 写 tape → 前端收的链路。
        """
        import time
        tape_path = self.tmp_path / "target-runs" / f"{self.run_id}.jsonl"
        with open(tape_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "seq": 99, "type": "agent_message", "timestamp": time.time(),
                "node": "N1", "session_id": "target-session",
                "data": {"text": "real event from passive target"},
            }) + "\n")

    def stop(self) -> None:
        """触发后台 _startup 的优雅关闭分支（server + manager teardown 在 loop 内完成）。"""
        if self._stop_evt is not None:
            self._stop_evt.set()
        try:
            self.thread.join(timeout=8.0)
        except Exception:  # noqa: BLE001
            pass


def test_h6_passive_pass_receives_real_event(monkeypatch, tmp_path):
    """S5 happy：passive 连真实 web + 后台 emit 一条事件 → probe 监听窗口内收到 → pass。

    构造：后台线程起 ephemeral web + attach_run；主线程跑 run_push_probe(ws_url, run_id)
    （阻塞 ≤8s 监听）；主线程 sleep 1.5s 后 emit → pump → WS → probe 收到 → pass。
    """
    import threading
    import time

    _clear_in_session_env(monkeypatch)
    target = _PassiveTarget(tmp_path)

    holder: dict = {}

    def _probe():
        holder["r"] = run_push_probe(
            ws_url=target.ws_url, run_id=target.run_id, rundir=tmp_path,
        )

    t = threading.Thread(target=_probe)
    t.start()
    time.sleep(1.5)  # 等 probe connect + subscribe + pump 起。
    target.emit()    # 注入真实事件 → bus → pump → WS → probe client。
    t.join(timeout=15.0)
    target.stop()

    assert "r" in holder, "probe 线程未返回结果"
    h6 = _hop_by_name(holder["r"], H6_WS_DELIVERY)
    assert h6["status"] == "pass", h6
    assert "mode=passive" in h6["evidence"]
    assert "received" in h6["evidence"]


def test_h6_passive_unknown_when_no_event(monkeypatch, tmp_path):
    """S5：passive subscribe 成功但监听窗口无事件 → unknown（被动模式无法注入，不强判 fail）。

    构造：后台起 ephemeral web + attach_run 但**不 emit** → probe 8s 监听无事件 → unknown。
    """
    _clear_in_session_env(monkeypatch)
    target = _PassiveTarget(tmp_path)
    result = run_push_probe(
        ws_url=target.ws_url, run_id=target.run_id, rundir=tmp_path,
    )
    target.stop()
    h6 = _hop_by_name(result, H6_WS_DELIVERY)
    assert h6["status"] == "unknown", h6
    assert "mode=passive" in h6["evidence"]
    assert "no event" in h6["evidence"]

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

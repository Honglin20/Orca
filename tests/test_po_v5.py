"""test_po_v5.py — v5 mechanism tests (sequential-gating redesign).

Script-level unit tests for the v5 mechanics: round_state (the single round
source), the frozen origin anchor (analyze --freeze-origin), the v5 history
builders (advanced / probe gap), the anchor-budget verdicts, the dual-mode
round advance ((round, mode) idempotency key, torn-write repair,
direction.json), the v5 gate decision order, the deployed-set version stamp,
the profiling-mode resolver (env -> npu-smi -> fallback, column-aware chip
parse), the accuracy-rule pool (check/seed/merge), the reuse gate's
profiling-mode consistency, and the v5 latency recheck. The smoke section
drives the real script chain end to end on one fixture workspace.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workflows" / "prof-opt" / "agents" / "_po_scripts"
sys.path.insert(0, str(_SCRIPTS))

import history_lib  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_po_scripts import _write_profile_fixture  # noqa: E402


def _run_cli(args: list[str], env: dict | None = None,
             timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], capture_output=True,
                          text=True, timeout=timeout, env=env)


def _write_anchor(artifacts: Path, baseline: int = 1000, ratio: float = 0.5,
                  budget: float = 0.1) -> Path:
    """Origin anchor on disk with the v5 schema (target per SPEC formula)."""
    anchor = artifacts / "base" / "origin_anchor.json"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(json.dumps({
        "baseline_makespan_cycles": baseline,
        "latency_reduction_min": ratio,
        "accuracy_budget": budget,
        "target_cycles": int(baseline * (1 - ratio)) + 1,
        "frozen_at_round": 0}), encoding="utf-8")
    return anchor


# ── round_state ───────────────────────────────────────────────────────────────

def _round_state(artifacts: Path, command: str
                 ) -> subprocess.CompletedProcess:
    return _run_cli([str(_SCRIPTS / "round_state.py"),
                     "--artifacts", str(artifacts), command])


def test_round_state_current_zero_pads_and_ignores_non_numeric(tmp_path):
    art = tmp_path / "ws"
    out = _round_state(art, "current")
    assert json.loads(out.stdout) == {"round": 0, "round_dir": None}

    for name in ("001", "005", "junk", "00X"):
        (art / "rounds" / name).mkdir(parents=True)
    out = _round_state(art, "current")
    assert json.loads(out.stdout) == {"round": 5, "round_dir": "rounds/005"}


def test_round_state_working_marker_linkage(tmp_path):
    art = tmp_path / "ws"
    (art / "rounds" / "001").mkdir(parents=True)
    # no marker -> max(current, 1)
    assert json.loads(_round_state(art, "working").stdout) == \
        {"round": 1, "round_dir": "rounds/001"}

    # marker at the CURRENT round -> next round
    (art / ".round_advanced").write_text(
        json.dumps({"round": 1, "mode": "latency"}), encoding="utf-8")
    assert json.loads(_round_state(art, "working").stdout) == \
        {"round": 2, "round_dir": "rounds/002"}

    # stale marker (older round) -> still max(current, 1)
    (art / ".round_advanced").write_text(
        json.dumps({"round": 0, "mode": "latency"}), encoding="utf-8")
    assert json.loads(_round_state(art, "working").stdout) == \
        {"round": 1, "round_dir": "rounds/001"}


def test_round_state_mode_two_states_and_missing_anchor_rc2(tmp_path):
    art = tmp_path / "ws"
    (art / "rounds" / "001").mkdir(parents=True)
    _write_anchor(art, baseline=1000, ratio=0.5)   # target = 501

    # no best.json -> latency
    assert json.loads(_round_state(art, "mode").stdout)["mode"] == "latency"

    (art / "best.json").write_text(json.dumps(
        {"vid": "r1-01", "makespan_cycles": 700, "proxy_acc": None,
         "round": 1, "profile_dir": "x"}), encoding="utf-8")
    state = json.loads(_round_state(art, "mode").stdout)
    assert state["mode"] == "latency" and state["target_cycles"] == 501

    (art / "best.json").write_text(json.dumps(
        {"vid": "r1-01", "makespan_cycles": 501, "proxy_acc": 0.9,
         "round": 1, "profile_dir": "x"}), encoding="utf-8")
    state = json.loads(_round_state(art, "mode").stdout)
    assert state["mode"] == "accuracy"
    assert state["best_makespan"] == 501

    # missing anchor -> exit 2 fail loud
    shutil.rmtree(art / "base")
    proc = _round_state(art, "mode")
    assert proc.returncode == 2
    assert "origin_anchor" in proc.stderr


def test_round_state_bad_command_rejected(tmp_path):
    art = tmp_path / "ws"
    art.mkdir()
    proc = _round_state(art, "bogus")
    assert proc.returncode != 0   # argparse choices fail loud


# ── analyze --freeze-origin ───────────────────────────────────────────────────

def _freeze(tmp_path: Path, ratio: str, budget: str,
            ) -> subprocess.CompletedProcess:
    profile_dir = tmp_path / "base" / "profile"
    if not (profile_dir / "profile_summary.json").is_file():
        _write_profile_fixture(profile_dir)   # fixture makespan = 310
    return _run_cli([str(_SCRIPTS / "analyze.py"),
                     "--profile-dir", str(profile_dir), "--freeze-origin",
                     "--latency-reduction-min", ratio,
                     "--accuracy-budget", budget])


def test_analyze_freeze_origin_first_write_formula(tmp_path):
    proc = _freeze(tmp_path, "0.5", "0.1")
    assert proc.returncode == 0, proc.stderr
    anchor = json.loads((tmp_path / "base" / "origin_anchor.json")
                        .read_text(encoding="utf-8"))
    # fixture baseline 310 x (1 - 0.5) + 1 = 156 (<= target <=> strictly below)
    assert anchor == {"baseline_makespan_cycles": 310,
                      "latency_reduction_min": 0.5, "accuracy_budget": 0.1,
                      "target_cycles": 156, "frozen_at_round": 0}
    assert "origin_anchor" in json.loads(proc.stdout)


def test_analyze_freeze_origin_idempotent_noop(tmp_path):
    assert _freeze(tmp_path, "0.5", "0.1").returncode == 0
    before = (tmp_path / "base" / "origin_anchor.json").read_bytes()
    proc = _freeze(tmp_path, "0.5", "0.1")
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "base" / "origin_anchor.json").read_bytes() == before
    assert "no-op" in json.loads(proc.stdout)["origin_anchor"]


def test_analyze_freeze_origin_conflict_rc2(tmp_path):
    assert _freeze(tmp_path, "0.5", "0.1").returncode == 0
    proc = _freeze(tmp_path, "0.4", "0.1")   # a different line
    assert proc.returncode == 2
    assert "IMMUTABLE" in proc.stderr
    assert "fresh_start" in proc.stderr


@pytest.mark.parametrize("ratio,budget", [("0", "0.1"), ("1", "0.1"),
                                          ("1.5", "0.1"), ("0.5", "-1")])
def test_analyze_freeze_origin_range_validation_rc2(tmp_path, ratio, budget):
    proc = _freeze(tmp_path, ratio, budget)
    assert proc.returncode == 2
    assert not (tmp_path / "base" / "origin_anchor.json").exists()


def test_analyze_freeze_origin_requires_both_params(tmp_path):
    profile_dir = tmp_path / "base" / "profile"
    _write_profile_fixture(profile_dir)
    proc = _run_cli([str(_SCRIPTS / "analyze.py"),
                     "--profile-dir", str(profile_dir), "--freeze-origin",
                     "--latency-reduction-min", "0.5"])
    assert proc.returncode == 2


def test_analyze_without_freeze_never_touches_anchor(tmp_path):
    profile_dir = tmp_path / "base" / "profile"
    _write_profile_fixture(profile_dir)
    anchor = profile_dir.parent / "origin_anchor.json"
    anchor.write_text('{"sentinel": "untouched"}', encoding="utf-8")
    mtime_before = os.stat(anchor).st_mtime_ns
    proc = _run_cli([str(_SCRIPTS / "analyze.py"),
                     "--profile-dir", str(profile_dir)])
    assert proc.returncode == 0, proc.stderr
    assert anchor.read_text(encoding="utf-8") == '{"sentinel": "untouched"}'
    assert os.stat(anchor).st_mtime_ns == mtime_before   # not rewritten


# ── history: advanced builder + probe gap ────────────────────────────────────

def test_history_append_advanced_writes_latency_field_set(tmp_path):
    hist = tmp_path / "history.jsonl"
    history_lib.append_implemented(
        hist, "r1-01", round=1, seq=1, parent_vid=None,
        change_sig="activation:gelu->relu:m", probe_epochs=1,
        probe_max_steps=None, probe_data_value=None,
        target_modules=["m"], predicted_delta_cycles=-100,
        base_at_proposal={"vid": None, "makespan_cycles": 1000})
    history_lib.append_latency(hist, "r1-01", structural_check="pass",
                               makespan_cycles=900, latency_gate="pass",
                               pred_actual_ratio=1.0, outcome="latency_pass")
    row = history_lib.append_advanced(hist, "r1-01")
    assert row["outcome"] == "advanced"
    # the marker row rides the promoted (LATENCY) field set on a full snapshot
    assert set(row) >= set(history_lib.LATENCY_FIELDS) | set(history_lib.IMPL_FIELDS)
    latest = history_lib.read_latest(hist)
    assert latest["r1-01"]["outcome"] == "advanced"
    assert latest["r1-01"]["makespan_cycles"] == 900


def test_history_permanent_set_v5(tmp_path):
    assert history_lib.PERMANENT_OUTCOMES == \
        frozenset({"advanced", "promoted", "unsupported_op"})
    # accuracy_fail is NOT permanent: a composed proposal's NEW sig passes
    # exact-match dedup by design
    hist = tmp_path / "history.jsonl"
    history_lib.append_implemented(
        hist, "r1-01", round=1, seq=1, parent_vid=None,
        change_sig="reduce_layers:2", probe_epochs=1, probe_max_steps=None,
        probe_data_value=None, target_modules=["m"], predicted_delta_cycles=-10,
        base_at_proposal={"vid": None, "makespan_cycles": 100})
    history_lib.append_latency(hist, "r1-01", structural_check="pass",
                               makespan_cycles=90, latency_gate="pass",
                               pred_actual_ratio=1.0, outcome="latency_pass")
    history_lib.append_probe(hist, "r1-01", proxy_acc=0.2,
                             promote_gate="fail", outcome="accuracy_fail",
                             gap=0.61)
    state = history_lib.dedup_state(hist, "reduce_layers:2", 1, None, None)
    assert state["blocked"] is False


def test_history_probe_gap_written_and_omitted(tmp_path):
    hist = tmp_path / "history.jsonl"
    row = history_lib.append_probe(
        hist, "r1-01", proxy_acc=0.9, promote_gate="pass",
        outcome="accuracy_pass", gap=0.05)
    assert row["gap"] == 0.05
    assert "gap" in history_lib.PROBE_FIELDS

    hist2 = tmp_path / "h2.jsonl"
    row2 = history_lib.append_probe(
        hist2, "r1-02", proxy_acc=None, promote_gate="fail",
        outcome="probe_insufficient")          # gap=None -> omitted
    assert "gap" not in row2


# ── gate CLI (v5): retired flags rejected ────────────────────────────────────

def test_gate_cli_rejects_retired_flags(tmp_path):
    art = tmp_path / "ws"
    art.mkdir()
    for flag in ("--latency-reduction-min", "--stall-rounds"):
        proc = _run_cli([str(_SCRIPTS / "gate_decide.py"),
                         "--artifacts", str(art), flag, "0.5"])
        assert proc.returncode != 0, flag
        assert flag in proc.stderr

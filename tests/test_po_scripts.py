"""test_po_scripts.py — unit tests for the prof-opt shared deterministic scripts.

Covers: history_lib (builder field sets + dedup branches + joint retry
budget + probe-row eval annotations with the gap field), gate_decide (the
sequential-gate decision order + round cap + torn-workspace invariant),
advance_round (latency/accuracy dual mode, the (round, mode) idempotency
key, torn-write repair, direction.json failed-sigs), analyze (fixture-driven
hot patterns / pipeline breakdown / cost table + strict unknown-key failure
+ the frozen origin anchor), predict_delta (per-shape-class row pricing
incl. the small-site E2E regression + params normalization idempotency),
the placeholder profiler's delta-direction guarantee, render_run (<<k>>
token chain), check_contracts (fairness-invariant token/budget enforcement),
run_baseline_chain (non-blocking baseline + finalizer guardian; profiling
mode from profile_mode.json), the po_flatten reuse gate (fresh_start
whole-workspace wipe + lock states + profiling-mode consistency), and
deploy_scripts' orphan retirement + version stamp. Shared-layer coverage:
metric_curve pinned-depth compare, stop_at_epoch (process-group kill at
epoch k), check_bottleneck, push_curves, verdict_decide (anchor-budget
promote/final-budget gates), extract_user_pkg, and po_propose
check_prerequisites. The v5 recheck section pins the mode-conditioned gate
(placeholder/mfu from profile_mode.json; strict-improvement vs incumbent in
the latency phase, the frozen target line in the recovery phase). v5
mechanics (round_state, rules_pool, resolve_profile_mode, gate_node stamp
wiring, smoke) live in test_po_v5.py.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workflows" / "prof-opt" / "agents" / "_po_scripts"
sys.path.insert(0, str(_SCRIPTS))

import history_lib  # noqa: E402
from analyze import ContractError, analyze  # noqa: E402
from gate_decide import decide  # noqa: E402
from advance_round import advance  # noqa: E402
from predict_delta import predict_delta  # noqa: E402

# v6 (prof-opt-v6 P0) retired the advance/mode/dual-gate/probe-row mechanics.
# Their v5 test cases are SKIPPED in place — not deleted — so the phase range
# stays bisectable; P5-T4 reclaims or migrates them per content class.
_RETIRED_V6 = pytest.mark.skip(
    reason="retired in v6 (prof-opt-v6 P0), cleanup in P5")


# ── history_lib ───────────────────────────────────────────────────────────────

@_RETIRED_V6
def test_history_builder_field_sets(tmp_path: Path):
    hist = tmp_path / "history.jsonl"
    history_lib.append_implemented(
        hist, "r1-01", round=1, seq=1, parent_vid=None,
        change_sig="activation:gelu->relu:blocks.0", probe_epochs=1,
        probe_max_steps=500, probe_data_value=2000, target_modules=["blocks.0"],
        predicted_delta_cycles=-100,
        base_at_proposal={"vid": None, "makespan_cycles": 15288})
    history_lib.append_latency(
        hist, "r1-01", structural_check="pass", makespan_cycles=900,
        latency_gate="pass", pred_actual_ratio=0.9, outcome="latency_pass")
    history_lib.append_probe(
        hist, "r1-01", proxy_acc=0.83,
        promote_gate="pass", outcome="accuracy_pass", gap=0.02,
        eval_skipped_no_epoch_ckpt=False, monitor_failed=False,
        eval_acc=0.91, eval_failed=False)

    rows = history_lib.read_rows(hist)
    assert len(rows) == 3
    impl, lat, probe = rows
    assert set(impl) >= set(history_lib.IMPL_FIELDS) | {"version", "ts"}
    assert set(lat) >= set(history_lib.LATENCY_FIELDS) | set(history_lib.IMPL_FIELDS)
    assert set(probe) >= set(history_lib.PROBE_FIELDS) | set(history_lib.LATENCY_FIELDS)
    assert [r["version"] for r in rows] == [1, 2, 3]

    latest = history_lib.read_latest(hist)
    assert latest["r1-01"]["outcome"] == "accuracy_pass"
    assert latest["r1-01"]["makespan_cycles"] == 900  # merged snapshot carries L0 fields
    assert latest["r1-01"]["gap"] == 0.02

    # the advance marker row keeps the promoted field set (LATENCY_FIELDS)
    advanced = history_lib.append_advanced(hist, "r1-01")
    assert advanced["outcome"] == "advanced"
    assert history_lib.read_latest(hist)["r1-01"]["outcome"] == "advanced"


def test_history_builder_rejects_unknown_fields(tmp_path: Path):
    hist = tmp_path / "history.jsonl"
    # public API: the typed signature itself rejects unknown kwargs
    with pytest.raises(TypeError):
        history_lib.append_latency(hist, "r1-01", structural_check="pass",
                                   makespan_cycles=1, latency_gate="pass",
                                   pred_actual_ratio=1.0, outcome="latency_pass",
                                   bogus_field=1)
    # internal guard: a stray field never reaches the file
    with pytest.raises(history_lib.HistoryError):
        history_lib._append(hist, "r1-01", {"bogus": 1}, history_lib.LATENCY_FIELDS,
                            "append_latency")
    assert not hist.exists()  # nothing was appended


def _write_sig_history(hist: Path, sig: str, outcomes: list[str],
                       probe_epochs: int = 1, probe_max_steps: int | None = 500,
                       probe_data_value=None):
    for i, outcome in enumerate(outcomes, 1):
        vid = f"r1-{i:02d}"
        history_lib.append_implemented(
            hist, vid, round=1, seq=i, parent_vid=None, change_sig=sig,
            probe_epochs=probe_epochs, probe_max_steps=probe_max_steps,
            probe_data_value=probe_data_value,
            target_modules=["m"], predicted_delta_cycles=-10,
            base_at_proposal={"vid": None, "makespan_cycles": 100})
        if outcome == "latency_pass":
            history_lib.append_latency(hist, vid, structural_check="pass",
                                       makespan_cycles=100, latency_gate="pass",
                                       pred_actual_ratio=1.0, outcome="latency_pass")
        elif outcome == "probe_insufficient":
            history_lib.append_latency(hist, vid, structural_check="pass",
                                       makespan_cycles=100, latency_gate="pass",
                                       pred_actual_ratio=1.0, outcome="latency_pass")
            history_lib.append_probe(hist, vid, proxy_acc=0.4,
                                     promote_gate="fail", outcome="probe_insufficient")
        elif outcome == "promoted":
            # v4 read-compat row: never written by v5, still dedup-blocks
            history_lib.append_latency(hist, vid, structural_check="pass",
                                       makespan_cycles=100, latency_gate="pass",
                                       pred_actual_ratio=1.0, outcome="latency_pass")
            history_lib.append_probe(hist, vid, proxy_acc=0.9,
                                     promote_gate="pass", outcome="promoted")
        elif outcome == "advanced":
            history_lib.append_latency(hist, vid, structural_check="pass",
                                       makespan_cycles=100, latency_gate="pass",
                                       pred_actual_ratio=1.0, outcome="latency_pass")
            history_lib.append_advanced(hist, vid)
        elif outcome in ("accuracy_pass", "accuracy_fail"):
            history_lib.append_latency(hist, vid, structural_check="pass",
                                       makespan_cycles=100, latency_gate="pass",
                                       pred_actual_ratio=1.0, outcome="latency_pass")
            history_lib.append_probe(hist, vid, proxy_acc=0.9,
                                     promote_gate="pass" if outcome == "accuracy_pass" else "fail",
                                     outcome=outcome, gap=0.05)
        else:  # structural_mismatch / variant_broken / unsupported_op
            if outcome in ("structural_mismatch", "variant_broken"):
                history_lib.append_outcome(hist, vid, outcome)
            else:
                history_lib.append_latency(hist, vid, structural_check="fail",
                                           makespan_cycles=None, latency_gate=None,
                                           pred_actual_ratio=None, outcome=outcome)


@pytest.mark.parametrize("outcomes,blocked", [
    (["promoted"], True),                      # v4 read-compat: still permanent
    (["advanced"], True),                      # permanent: a round advanced it
    (["unsupported_op"], True),                # permanent: structurally infeasible
    (["latency_pass"], False),                 # process state never blocks
    (["accuracy_fail"], False),                # composed re-proposals use NEW sigs
    (["structural_mismatch"], False),          # joint budget allows one retry
    (["variant_broken"], False),               # the other class, same budget
    (["structural_mismatch", "variant_broken"], True),   # joint budget exhausted
    (["variant_broken", "variant_broken"], True),        # same class twice: exhausted
])
@_RETIRED_V6
def test_history_dedup_branches(tmp_path: Path, outcomes, blocked):
    hist = tmp_path / "history.jsonl"
    _write_sig_history(hist, "activation:x->y", outcomes)
    state = history_lib.dedup_state(hist, "activation:x->y", 1, 500)
    assert state["blocked"] is blocked, state


@_RETIRED_V6
def test_history_dedup_probe_config_retry(tmp_path: Path):
    hist = tmp_path / "history.jsonl"
    _write_sig_history(hist, "norm:relax:m", ["probe_insufficient"],
                       probe_epochs=1, probe_max_steps=500,
                       probe_data_value=2000)
    same = history_lib.dedup_state(hist, "norm:relax:m", 1, 500, 2000)
    assert same["blocked"] is True          # identical proxy budget: no blind retry
    changed = history_lib.dedup_state(hist, "norm:relax:m", 2, 500, 2000)
    assert changed["blocked"] is False      # epochs change reopens the sig
    other = history_lib.dedup_state(hist, "norm:relax:m", 1, 1000, 2000)
    assert other["blocked"] is False        # step-cap change reopens the sig
    more_data = history_lib.dedup_state(hist, "norm:relax:m", 1, 500, 5000)
    assert more_data["blocked"] is False    # data-subset value change reopens it


@_RETIRED_V6
def test_history_dedup_null_max_steps_is_a_pinned_budget(tmp_path: Path):
    """Regression (review): a project WITHOUT a step-truncation mechanism pins
    max_steps=null. null is a budget value, not "unset" — a same-budget
    re-proposal must stay blocked even though the raw workflow input default
    is 500. Both sides read contracts.proxy_budget verbatim."""
    hist = tmp_path / "history.jsonl"
    _write_sig_history(hist, "act:swap:m", ["probe_insufficient"],
                       probe_epochs=1, probe_max_steps=None,
                       probe_data_value=None)
    same = history_lib.dedup_state(hist, "act:swap:m", 1, None, None)
    assert same["blocked"] is True   # nothing changed: no blind retry
    got_steps = history_lib.dedup_state(hist, "act:swap:m", 1, 500, None)
    assert got_steps["blocked"] is False  # a real budget change reopens it


@_RETIRED_V6
def test_history_cli_requires_and_passes_null_budget(tmp_path: Path):
    """The po_propose node drives the CLI, not the function: pin the CLI
    surface — null budgets must survive the round trip, and a missing flag
    must fail loud (a silent 500 default is the fingerprint-mismatch hole)."""
    hist = tmp_path / "history.jsonl"
    _write_sig_history(hist, "act:swap:m", ["probe_insufficient"],
                       probe_epochs=1, probe_max_steps=None,
                       probe_data_value=None)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "history_lib.py"),
         "--history", str(hist), "--sig", "act:swap:m",
         "--probe-epochs", "1", "--probe-max-steps", "null",
         "--probe-data-value", "null"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["blocked"] is True

    reopened = subprocess.run(
        [sys.executable, str(_SCRIPTS / "history_lib.py"),
         "--history", str(hist), "--sig", "act:swap:m",
         "--probe-epochs", "1", "--probe-max-steps", "500",
         "--probe-data-value", "null"],
        capture_output=True, text=True, timeout=60)
    assert json.loads(reopened.stdout)["blocked"] is False

    # missing flag -> argparse fail loud, never a silent default
    missing = subprocess.run(
        [sys.executable, str(_SCRIPTS / "history_lib.py"),
         "--history", str(hist), "--sig", "act:swap:m",
         "--probe-epochs", "1"],
        capture_output=True, text=True, timeout=60)
    assert missing.returncode != 0
    assert "--probe-max-steps" in missing.stderr


# ── gate_decide (v5: sequential gates, round cap only) ────────────────────────

_GATE_BASE_MAKESPAN = 1000  # fixture baseline; anchor ratio 0.5 -> target 501


def _gate_artifacts(tmp_path: Path, *, rounds: list[int],
                    history_rows: list[dict], best: dict | None,
                    with_anchor: bool = True) -> Path:
    art = tmp_path / "gate-artifacts"
    for rnd in rounds:
        (art / "rounds" / f"{rnd:03d}").mkdir(parents=True)
    if with_anchor:
        (art / "base").mkdir(parents=True, exist_ok=True)
        (art / "base" / "origin_anchor.json").write_text(json.dumps({
            "baseline_makespan_cycles": _GATE_BASE_MAKESPAN,
            "latency_reduction_min": 0.5, "accuracy_budget": 0.1,
            "target_cycles": 501, "frozen_at_round": 0}), encoding="utf-8")
    hist = art / "history.jsonl"
    with open(hist, "w", encoding="utf-8") as fh:
        for row in history_rows:
            fh.write(json.dumps(row) + "\n")
    if best is not None:
        art.mkdir(parents=True, exist_ok=True)
        (art / "best.json").write_text(json.dumps(best), encoding="utf-8")
    return art


# a fully-closed accuracy winner: latency_pass -> accuracy_pass -> advanced
# (the ANY-version-row rule must see the accuracy_pass under the advanced)
_ACCURACY_WINNER_R1 = [
    {"vid": "r1-01", "round": 1, "outcome": "latency_pass",
     "makespan_cycles": 450, "proxy_acc": 0.9, "promote_gate": "none"},
    {"vid": "r1-01", "round": 1, "outcome": "accuracy_pass",
     "makespan_cycles": 450, "proxy_acc": 0.9, "promote_gate": "pass",
     "gap": 0.05},
    {"vid": "r1-01", "round": 1, "outcome": "advanced",
     "makespan_cycles": 450, "proxy_acc": 0.9, "promote_gate": "pass"},
]
_NO_PASS_R1 = [
    {"vid": "r1-01", "round": 1, "outcome": "latency_fail",
     "makespan_cycles": 800},
]


@_RETIRED_V6
def test_gate_full_train_on_accuracy_pass_any_version_row(tmp_path: Path):
    """Decision 1: best under the frozen line AND an accuracy_pass in ANY
    version row (the advance's `advanced` row does not erase it)."""
    art = _gate_artifacts(tmp_path, rounds=[1], history_rows=_ACCURACY_WINNER_R1,
                          best={"vid": "r1-01", "makespan_cycles": 450,
                                "proxy_acc": 0.9})
    out = decide(art, max_rounds=5)
    assert out["decision"] == "full-train"
    assert out["mode"] == "accuracy"
    assert out["target_cycles"] == 501
    assert out["best"]["vid"] == "r1-01"


@_RETIRED_V6
def test_gate_loops_when_gates_unmet(tmp_path: Path):
    art = _gate_artifacts(tmp_path, rounds=[1], history_rows=_NO_PASS_R1,
                          best=None)
    out = decide(art, max_rounds=5)
    assert out["decision"] == "loop"
    assert out["mode"] == "latency"
    assert out["best"] is None


def test_gate_loops_across_consecutive_zero_advance_rounds(tmp_path: Path):
    """v5 has no stall/platoona exit: many zero-advance rounds still loop
    (the plateau's answer is proposal rerouting, not stopping)."""
    rows = [dict(_NO_PASS_R1[0], vid=f"r{r}-01", round=r)
            for r in range(1, 6)]
    art = _gate_artifacts(tmp_path, rounds=[1, 2, 3, 4, 5],
                          history_rows=rows, best=None)
    out = decide(art, max_rounds=100)
    assert out["decision"] == "loop"


@_RETIRED_V6
def test_gate_best_met_line_without_accuracy_pass_still_loops(tmp_path: Path):
    """Under the line but the accuracy gate never passed (accuracy_fail or
    probe pending): NOT full-train — the recovery rounds continue."""
    rows = [dict(_ACCURACY_WINNER_R1[0]),
            {"vid": "r1-01", "round": 1, "outcome": "accuracy_fail",
             "makespan_cycles": 450, "proxy_acc": 0.4,
             "promote_gate": "fail", "gap": 0.5}]
    art = _gate_artifacts(tmp_path, rounds=[1], history_rows=rows,
                          best={"vid": "r1-01", "makespan_cycles": 450,
                                "proxy_acc": 0.4})
    out = decide(art, max_rounds=5)
    assert out["decision"] == "loop"
    assert out["mode"] == "accuracy"


@_RETIRED_V6
def test_gate_hard_cap_with_best_is_best_effort(tmp_path: Path):
    art = _gate_artifacts(tmp_path, rounds=[1, 2], history_rows=_NO_PASS_R1,
                          best={"vid": "r1-01", "makespan_cycles": 800,
                                "proxy_acc": None})
    out = decide(art, max_rounds=2)
    assert out["decision"] == "full-train-best-effort"  # cap + best present


@_RETIRED_V6
def test_gate_finish_failed_when_no_best_at_cap(tmp_path: Path):
    art = _gate_artifacts(tmp_path, rounds=[1, 2],
                          history_rows=_NO_PASS_R1, best=None)
    out = decide(art, max_rounds=2)
    assert out["decision"] == "finish-failed"


@_RETIRED_V6
def test_gate_accuracy_pass_wins_over_the_cap(tmp_path: Path):
    """Decision 1 is judged FIRST: even at the round cap, an accuracy-passed
    best under the line is a clean full-train, not best-effort."""
    art = _gate_artifacts(tmp_path, rounds=[1, 2],
                          history_rows=_ACCURACY_WINNER_R1,
                          best={"vid": "r1-01", "makespan_cycles": 450,
                                "proxy_acc": 0.9})
    out = decide(art, max_rounds=2)
    assert out["decision"] == "full-train"


@_RETIRED_V6
def test_gate_hard_cap_never_loops_at_max_rounds(tmp_path: Path):
    art = _gate_artifacts(tmp_path, rounds=[1, 2],
                          history_rows=_NO_PASS_R1, best=None)
    out = decide(art, max_rounds=2)
    assert out["round"] == 2
    assert out["decision"] != "loop"
    assert out["decision"] == "finish-failed"


@_RETIRED_V6
def test_gate_invariant_accuracy_mode_without_probe_row_rc2(tmp_path: Path):
    """mode=accuracy but best.vid has no probe row at all: the workspace is
    torn — exit 2, never a guessed decision."""
    art = _gate_artifacts(tmp_path, rounds=[1], history_rows=_NO_PASS_R1,
                          best={"vid": "r1-01", "makespan_cycles": 450,
                                "proxy_acc": None})
    with pytest.raises(ValueError, match="invariant"):
        decide(art, max_rounds=5)


def test_gate_missing_origin_anchor_rc2(tmp_path: Path):
    art = _gate_artifacts(tmp_path, rounds=[1], history_rows=_NO_PASS_R1,
                          best=None, with_anchor=False)
    with pytest.raises(FileNotFoundError, match="origin anchor"):
        decide(art, max_rounds=5)


def test_gate_no_longer_reads_proposals_json(tmp_path: Path):
    """v5 gate is proposals-blind: no proposals.json anywhere is fine (the
    reverse of the v4 fail-loud — exhausted is report material, not gate
    input)."""
    art = tmp_path / "empty-artifacts"
    (art / "rounds" / "001").mkdir(parents=True)
    (art / "base").mkdir(parents=True)
    (art / "base" / "origin_anchor.json").write_text(json.dumps({
        "baseline_makespan_cycles": 1000, "latency_reduction_min": 0.5,
        "accuracy_budget": 0.1, "target_cycles": 501,
        "frozen_at_round": 0}), encoding="utf-8")
    out = decide(art, max_rounds=5)
    assert out["decision"] == "loop"


# ── advance_round (v5: dual-mode, (round, mode) key, torn repair) ─────────────

def _v5_advance_artifacts(tmp_path: Path, baseline: int = 1000,
                          ratio: float = 0.5) -> Path:
    """Workspace with an origin anchor (ratio 0.5 -> target 501) and a
    round-1 directory; incumbent before any best = the anchor baseline."""
    art = tmp_path / "advance-artifacts"
    (art / "rounds" / "001").mkdir(parents=True)
    (art / "base").mkdir(parents=True)
    (art / "base" / "model.onnx").write_text("onnx-of-round0-base", encoding="utf-8")
    (art / "base" / "origin_anchor.json").write_text(json.dumps({
        "baseline_makespan_cycles": baseline, "latency_reduction_min": ratio,
        "accuracy_budget": 0.1,
        "target_cycles": int(baseline * (1 - ratio)) + 1,
        "frozen_at_round": 0}), encoding="utf-8")
    (art / "shadow" / "pkg").mkdir(parents=True)
    (art / "shadow" / "pkg" / "model.py").write_text("# shadow round0\n", encoding="utf-8")
    return art


def _v5_variant(art: Path, vid: str, round_no: int, makespan: int, *,
                probe: str | None = None, gap: float | None = None,
                proxy_acc: float = 0.8, advanced: bool = False,
                latency: bool = True, shadow_tag: str | None = None,
                sig: str | None = None):
    """On-disk variant + its history rows: implemented -> [latency_pass] ->
    [accuracy_pass|accuracy_fail] -> [advanced]."""
    vd = art / "variants" / vid
    (vd / "onnx").mkdir(parents=True, exist_ok=True)
    (vd / "onnx" / "model.onnx").write_text(f"onnx-of-{shadow_tag or vid}", encoding="utf-8")
    (vd / "profile").mkdir(parents=True, exist_ok=True)
    (vd / "profile" / "profile_summary.json").write_text(
        json.dumps({"schema_version": 1, "onnx": "x", "makespan_cycles": makespan,
                    "op_count": 1}), encoding="utf-8")
    shadow = vd / "shadow" / "pkg"
    shadow.mkdir(parents=True, exist_ok=True)
    (shadow / "model.py").write_text(f"# shadow {shadow_tag or vid}\n", encoding="utf-8")
    (vd / "shadow" / "pkg" / "__pycache__").mkdir(exist_ok=True)
    (vd / "shadow" / "pkg" / "__pycache__" / "model.cpython-311.pyc").write_text(
        "stale bytecode", encoding="utf-8")

    hist = art / "history.jsonl"
    history_lib.append_implemented(
        hist, vid, round=round_no, seq=1, parent_vid=None,
        change_sig=sig or f"sig:{vid}", probe_epochs=1, probe_max_steps=500,
        probe_data_value=None,
        target_modules=["m"], predicted_delta_cycles=-10,
        base_at_proposal={"vid": None, "makespan_cycles": 1000})
    if latency:
        history_lib.append_latency(hist, vid, structural_check="pass",
                                   makespan_cycles=makespan, latency_gate="pass",
                                   pred_actual_ratio=1.0, outcome="latency_pass")
    if probe == "accuracy_pass":
        history_lib.append_probe(hist, vid, proxy_acc=proxy_acc,
                                 promote_gate="pass", outcome="accuracy_pass",
                                 gap=gap if gap is not None else 0.05)
    elif probe == "accuracy_fail":
        history_lib.append_probe(hist, vid, proxy_acc=proxy_acc,
                                 promote_gate="fail", outcome="accuracy_fail",
                                 gap=gap if gap is not None else 0.5)
    if advanced:
        history_lib.append_advanced(hist, vid)


def _marker(art: Path) -> dict:
    return json.loads((art / ".round_advanced").read_text(encoding="utf-8"))


def _direction(art: Path, round_no: int) -> dict:
    return json.loads((art / "rounds" / f"{round_no:03d}" / "direction.json")
                      .read_text(encoding="utf-8"))


@_RETIRED_V6
def test_advance_latency_r1_then_r2_replaces_base_and_shadow(tmp_path: Path):
    art = _v5_advance_artifacts(tmp_path)
    _v5_variant(art, "r1-01", round_no=1, makespan=900)   # < incumbent 1000

    out1 = advance(art)
    assert out1 == {"advanced": True, "round": 1, "mode": "latency",
                    "vid": "r1-01", "improved": True, "best_updated": True,
                    "reason": "winner advanced"}
    assert (art / "base" / "model.onnx").read_text(encoding="utf-8") == "onnx-of-r1-01"
    assert (art / "shadow" / "pkg" / "model.py").read_text(encoding="utf-8") == "# shadow r1-01\n"
    # __pycache__ must not leak into the global shadow
    assert not (art / "shadow" / "pkg" / "__pycache__").exists()
    assert (art / "base" / "profile" / "profile_summary.json").is_file()
    best1 = json.loads((art / "best.json").read_text(encoding="utf-8"))
    assert best1["vid"] == "r1-01" and best1["makespan_cycles"] == 900
    assert best1["proxy_acc"] is None          # latency-mode advances carry no acc
    assert best1["round"] == 1
    assert _marker(art) == {"round": 1, "mode": "latency", "vid": "r1-01",
                            "improved": True, "best_updated": True}
    assert history_lib.read_latest(art / "history.jsonl")["r1-01"]["outcome"] == "advanced"

    # idempotency key: (round, mode) match -> pure no-op even after new history
    out_again = advance(art)
    assert out_again["advanced"] is False

    # round 2 strictly better (above the line: latency phase) -> replacement
    (art / "rounds" / "002").mkdir()
    _v5_variant(art, "r2-01", round_no=2, makespan=700)
    out2 = advance(art)
    assert out2["advanced"] is True and out2["round"] == 2 and out2["vid"] == "r2-01"
    assert (art / "base" / "model.onnx").read_text(encoding="utf-8") == "onnx-of-r2-01"
    best2 = json.loads((art / "best.json").read_text(encoding="utf-8"))
    assert best2["vid"] == "r2-01" and best2["makespan_cycles"] == 700


@_RETIRED_V6
def test_advance_latency_small_strict_step_also_advances(tmp_path: Path):
    """v5 retired the absolute/relative/ratio thresholds: a 50-cycle step
    that is STRICTLY below the incumbent is a legitimate advance."""
    art = _v5_advance_artifacts(tmp_path)
    (art / "best.json").write_text(json.dumps(
        {"vid": "r0-99", "makespan_cycles": 900, "proxy_acc": None,
         "round": 0, "profile_dir": "x"}), encoding="utf-8")
    _v5_variant(art, "r1-01", round_no=1, makespan=850)   # 50 better, strict
    out = advance(art)
    assert out["advanced"] is True and out["vid"] == "r1-01"
    assert _direction(art, 1)["improved"] is True


@_RETIRED_V6
def test_advance_zero_improvement_marker_and_failed_sigs(tmp_path: Path):
    """No candidate at all (no latency_pass row under the incumbent): the
    common actions are skipped entirely — marker-only, best.json absent,
    direction.json records the fail evidence for the next round's rerouting."""
    art = _v5_advance_artifacts(tmp_path)
    _v5_variant(art, "r1-01", round_no=1, makespan=900, latency=False,
                sig="sig:worse-a")
    _v5_variant(art, "r1-02", round_no=1, makespan=800, probe="accuracy_fail",
                gap=0.6, sig="sig:accfail-b", shadow_tag="r1-02")
    out = advance(art)
    assert out["advanced"] is False and out["vid"] is None
    assert out["improved"] is False and out["best_updated"] is False
    assert not (art / "best.json").exists()
    marker = _marker(art)
    assert marker == {"round": 1, "mode": "latency", "vid": None,
                      "improved": False, "best_updated": False}
    d = _direction(art, 1)
    assert d["failed_sigs"] == ["sig:accfail-b"]   # only latest-row fails count
    assert d["improved"] is False and d["advanced_vid"] is None


@_RETIRED_V6
def test_advance_latency_fail_rows_feed_failed_sigs(tmp_path: Path):
    art = _v5_advance_artifacts(tmp_path)
    hist = art / "history.jsonl"
    # r1-01: latest row latency_fail (worse than the anchor incumbent)
    _v5_variant(art, "r1-01", round_no=1, makespan=1100, latency=False,
                sig="sig:lat-fail")
    history_lib.append_latency(hist, "r1-01", structural_check="pass",
                               makespan_cycles=1100, latency_gate="fail",
                               pred_actual_ratio=None, outcome="latency_fail")
    # r1-02: accuracy_fail probe row (no best yet -> mode stays latency)
    _v5_variant(art, "r1-02", round_no=1, makespan=800, probe="accuracy_fail",
                gap=0.4, sig="sig:acc-fail")
    out = advance(art)
    assert out["advanced"] is False
    assert _direction(art, 1)["failed_sigs"] == ["sig:acc-fail", "sig:lat-fail"]


@_RETIRED_V6
def test_advance_accuracy_mode_only_accuracy_pass_advances(tmp_path: Path):
    art = _v5_advance_artifacts(tmp_path)
    # existing best under the line -> mode accuracy (recovery phase)
    (art / "best.json").write_text(json.dumps(
        {"vid": "r0-99", "makespan_cycles": 450, "proxy_acc": 0.4,
         "round": 0, "profile_dir": "x"}), encoding="utf-8")
    # survivor within the line but FAILING the accuracy gate -> no advance
    _v5_variant(art, "r1-01", round_no=1, makespan=460, probe="accuracy_fail",
                gap=0.5)
    out = advance(art)
    assert out["advanced"] is False and out["mode"] == "accuracy"
    assert _marker(art)["mode"] == "accuracy"
    assert (art / "base" / "model.onnx").read_text(encoding="utf-8") == \
        "onnx-of-round0-base"   # base fixed: no copy without accuracy_pass


@_RETIRED_V6
def test_advance_accuracy_pass_winner_ranked_by_gap(tmp_path: Path):
    art = _v5_advance_artifacts(tmp_path)
    (art / "best.json").write_text(json.dumps(
        {"vid": "r0-99", "makespan_cycles": 450, "proxy_acc": 0.4,
         "round": 0, "profile_dir": "x"}), encoding="utf-8")
    _v5_variant(art, "r1-01", round_no=1, makespan=460, probe="accuracy_pass",
                gap=0.10)
    _v5_variant(art, "r1-02", round_no=1, makespan=480, probe="accuracy_pass",
                gap=0.05, shadow_tag="r1-02")
    out = advance(art)
    assert out["advanced"] is True and out["vid"] == "r1-02"   # smallest gap
    best = json.loads((art / "best.json").read_text(encoding="utf-8"))
    assert best["vid"] == "r1-02" and best["proxy_acc"] == 0.8  # accuracy-mode acc kept
    assert best["round"] == 1
    assert (art / "shadow" / "pkg" / "model.py").read_text(encoding="utf-8") == \
        "# shadow r1-02\n"


@_RETIRED_V6
def test_advance_accuracy_tie_break_gap_then_makespan_then_vid(tmp_path: Path):
    """The full accuracy ranking chain (gap -> makespan -> vid), direction
    already normalized by the verdict layer: equal gaps fall through to the
    smaller makespan, equal both to the vid order."""
    art = _v5_advance_artifacts(tmp_path)
    (art / "best.json").write_text(json.dumps(
        {"vid": "r0-99", "makespan_cycles": 450, "proxy_acc": 0.4,
         "round": 0, "profile_dir": "x"}), encoding="utf-8")
    # equal gap 0.05, DIFFERENT makespans: the smaller makespan wins
    _v5_variant(art, "r1-01", round_no=1, makespan=490, probe="accuracy_pass",
                gap=0.05)
    _v5_variant(art, "r1-02", round_no=1, makespan=470, probe="accuracy_pass",
                gap=0.05, shadow_tag="r1-02")
    out = advance(art)
    assert out["vid"] == "r1-02"

    # equal gap AND makespan: the vid order decides (no proxy_acc anywhere
    # in the ranking — the v4 higher-proxy hardcode is gone)
    art2 = _v5_advance_artifacts(tmp_path / "b")
    (art2 / "best.json").write_text(json.dumps(
        {"vid": "r0-99", "makespan_cycles": 450, "proxy_acc": 0.4,
         "round": 0, "profile_dir": "x"}), encoding="utf-8")
    _v5_variant(art2, "r1-02", round_no=1, makespan=470, probe="accuracy_pass",
                gap=0.05, proxy_acc=0.99)      # HIGHER acc, loses on vid order
    _v5_variant(art2, "r1-01", round_no=1, makespan=470, probe="accuracy_pass",
                gap=0.05, proxy_acc=0.10, shadow_tag="r1-01")
    out2 = advance(art2)
    assert out2["vid"] == "r1-01"              # lexicographic, acc irrelevant


@_RETIRED_V6
def test_advance_accuracy_above_line_never_a_candidate(tmp_path: Path):
    art = _v5_advance_artifacts(tmp_path)
    (art / "best.json").write_text(json.dumps(
        {"vid": "r0-99", "makespan_cycles": 450, "proxy_acc": 0.4,
         "round": 0, "profile_dir": "x"}), encoding="utf-8")
    # accuracy_pass but makespan ABOVE target: eliminated mechanically
    _v5_variant(art, "r1-01", round_no=1, makespan=600, probe="accuracy_pass",
                gap=0.02)
    out = advance(art)
    assert out["advanced"] is False
    assert (art / "base" / "model.onnx").read_text(encoding="utf-8") == \
        "onnx-of-round0-base"


@_RETIRED_V6
def test_advance_round_mode_idempotency_key_both_modes_once(tmp_path: Path):
    """Same round, both phases: the (round, mode) key admits one latency and
    one accuracy advance each; the later direction.json overwrites."""
    art = _v5_advance_artifacts(tmp_path)
    _v5_variant(art, "r1-01", round_no=1, makespan=450)     # <= 501: flips mode
    # first advance runs BEFORE any best exists -> latency mode (incumbent
    # is the anchor baseline 1000)
    out1 = advance(art)
    assert out1["mode"] == "latency" and out1["vid"] == "r1-01"
    # re-run: same (round, latency) -> no-op
    assert advance(art)["advanced"] is False
    # now best=450 <= 501 -> accuracy mode; the vid already advanced this
    # round (benign first-entry needs its accuracy row; without one there is
    # no candidate) -> marker-only for the accuracy key
    _v5_variant(art, "r1-02", round_no=1, makespan=470, probe="accuracy_fail",
                gap=0.5, shadow_tag="r1-02")
    out2 = advance(art)
    assert out2["mode"] == "accuracy" and out2["advanced"] is False
    marker = _marker(art)
    assert (marker["round"], marker["mode"]) == (1, "accuracy")
    # the SAME-round latency+accuracy sequence each ran exactly once
    assert _direction(art, 1)["mode"] == "accuracy"   # later write overwrote


@_RETIRED_V6
def test_advance_benign_first_entry_marker_only_no_recopy(tmp_path: Path):
    """The benign first-entry: the same-round latency-advanced best.vid
    passes the accuracy gate. Its advanced row already exists -> NO torn
    repair, NO copy, marker-only (vid=null, improved=false)."""
    art = _v5_advance_artifacts(tmp_path)
    _v5_variant(art, "r1-01", round_no=1, makespan=450)
    advance(art)   # latency advance: best=r1-01, advanced row written
    base_after = (art / "base" / "model.onnx").read_text(encoding="utf-8")
    # probe passes the gate on the SAME vid (first accuracy entry)
    hist = art / "history.jsonl"
    history_lib.append_probe(hist, "r1-01", proxy_acc=0.9,
                             promote_gate="pass", outcome="accuracy_pass",
                             gap=0.05)
    out = advance(art)   # mode=accuracy, winner==incumbent
    assert out["advanced"] is False and out["vid"] is None
    assert out["improved"] is False
    assert _marker(art) == {"round": 1, "mode": "accuracy", "vid": None,
                            "improved": False, "best_updated": False}
    # no re-copy happened (benign: the winner row was already advanced)
    assert (art / "base" / "model.onnx").read_text(encoding="utf-8") == base_after


@_RETIRED_V6
def test_advance_stale_marker_replays_under_current_mode(tmp_path: Path):
    """marker.round < current round (crash between rounds) -> replay the
    advance under the CURRENT mode."""
    art = _v5_advance_artifacts(tmp_path)
    _v5_variant(art, "r1-01", round_no=1, makespan=900)
    advance(art)
    (art / "rounds" / "002").mkdir()
    _v5_variant(art, "r2-01", round_no=2, makespan=700)
    # simulate a crash AFTER history write but BEFORE marker update
    (art / ".round_advanced").write_text(json.dumps(
        {"round": 1, "mode": "latency", "vid": "r1-01"}), encoding="utf-8")
    out = advance(art)
    assert out["advanced"] is True and out["round"] == 2
    assert (art / "shadow" / "pkg" / "model.py").read_text(encoding="utf-8") == \
        "# shadow r2-01\n"


@_RETIRED_V6
def test_advance_torn_repair_a_winner_recomputation_hits(tmp_path: Path):
    """Torn accuracy write: best.json written for the new winner, copy and
    advanced row missing. Recomputation re-derives the same winner -> repair
    by best.vid (copy + append_advanced + marker)."""
    art = _v5_advance_artifacts(tmp_path)
    (art / "best.json").write_text(json.dumps(
        {"vid": "r0-99", "makespan_cycles": 900, "proxy_acc": None,
         "round": 0, "profile_dir": "x"}), encoding="utf-8")
    _v5_variant(art, "r1-01", round_no=1, makespan=450,
                probe="accuracy_pass", gap=0.05)
    # crash after best.json write, before copy/advanced/marker
    (art / "best.json").write_text(json.dumps(
        {"vid": "r1-01", "makespan_cycles": 450, "proxy_acc": 0.8,
         "round": 1, "profile_dir": "x"}), encoding="utf-8")
    out = advance(art)
    assert out["advanced"] is True and out["vid"] == "r1-01"
    assert out["reason"] == "torn write repaired"
    assert (art / "base" / "model.onnx").read_text(encoding="utf-8") == "onnx-of-r1-01"
    assert history_lib.read_latest(art / "history.jsonl")["r1-01"]["outcome"] == \
        "advanced"
    # converged: a second run is a no-op
    assert advance(art)["advanced"] is False


@_RETIRED_V6
def test_advance_torn_repair_b_latency_candidate_suppressed(tmp_path: Path):
    """Torn LATENCY write: best.json names this round's winner; the strict
    improvement test can never re-admit it (incumbent == winner itself), so
    recomputation finds NO candidate — repair still completes by best.vid."""
    art = _v5_advance_artifacts(tmp_path)
    _v5_variant(art, "r1-01", round_no=1, makespan=700)   # above line: latency
    # crash after best.json write, before advanced row/copy/marker
    (art / "best.json").write_text(json.dumps(
        {"vid": "r1-01", "makespan_cycles": 700, "proxy_acc": None,
         "round": 1, "profile_dir": "x"}), encoding="utf-8")
    out = advance(art)
    assert out["advanced"] is True and out["vid"] == "r1-01"
    assert out["reason"] == "torn write repaired"
    assert (art / "shadow" / "pkg" / "model.py").read_text(encoding="utf-8") == \
        "# shadow r1-01\n"
    assert history_lib.read_latest(art / "history.jsonl")["r1-01"]["outcome"] == \
        "advanced"
    marker = _marker(art)
    assert (marker["round"], marker["mode"]) == (1, "latency")
    assert marker["improved"] is True


@_RETIRED_V6
def test_advance_worse_promotion_keeps_base(tmp_path: Path):
    art = _v5_advance_artifacts(tmp_path)
    _v5_variant(art, "r1-01", round_no=1, makespan=900)
    advance(art)
    (art / "rounds" / "002").mkdir()
    _v5_variant(art, "r2-01", round_no=2, makespan=950)  # worse than 900
    out = advance(art)
    assert out["advanced"] is False and out["vid"] is None
    assert (art / "base" / "model.onnx").read_text(encoding="utf-8") == "onnx-of-r1-01"
    best = json.loads((art / "best.json").read_text(encoding="utf-8"))
    assert best["vid"] == "r1-01" and best["round"] == 1


# ── analyze ───────────────────────────────────────────────────────────────────

def _write_profile_fixture(profile_dir: Path) -> None:
    """Chain MatMul -> Erf*3 -> Add, with a cheap Relu side-branch joining at
    Add. Critical path = the chain; Erf must cluster as the top hot pattern."""
    ops = [
        {"name": "mm1", "op_type": "MatMul", "latency": 100, "depends_on": [],
         "dims": [1, 64], "onnx_nodes": ["/fc1/MatMul"]},
        {"name": "erf1", "op_type": "Erf", "latency": 50, "depends_on": ["mm1"],
         "dims": [1, 64], "onnx_nodes": ["/act1/Erf"]},
        {"name": "relu_side", "op_type": "Relu", "latency": 20, "depends_on": ["mm1"],
         "dims": [1, 64], "onnx_nodes": ["/side/Relu"]},
        {"name": "erf2", "op_type": "Erf", "latency": 50, "depends_on": ["erf1"],
         "dims": [1, 64], "onnx_nodes": ["/act2/Erf"]},
        {"name": "erf3", "op_type": "Erf", "latency": 50, "depends_on": ["erf2"],
         "dims": [1, 64], "onnx_nodes": ["/act3/Erf"]},
        {"name": "add1", "op_type": "Add", "latency": 40,
         "depends_on": ["erf3", "relu_side"], "dims": [1, 64],
         "onnx_nodes": ["/join/Add"]},
    ]
    by_name = {o["name"]: o for o in ops}
    level = {}
    for o in ops:
        level[o["name"]] = 1 + max((level[d] for d in o["depends_on"]), default=0)

    import csv
    profile_dir.mkdir(parents=True)
    taskgraph = {"schema_version": 1, "onnx": "fixture.onnx", "operators": [
        {"name": o["name"], "op_type": o["op_type"], "task_id": f"t{i:04d}",
         "pipeline": f"p{level[o['name']]:03d}", "latency": o["latency"],
         "depends_on": o["depends_on"], "output_memory": 64 * 4,
         "output_dimensions": o["dims"], "onnx_nodes": o["onnx_nodes"]}
        for i, o in enumerate(ops)]}
    (profile_dir / "taskgraph.json").write_text(json.dumps(taskgraph), encoding="utf-8")

    with open(profile_dir / "ops.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "op_type", "task_id", "pipeline", "latency",
                    "depends_on", "output_memory", "output_dimensions", "onnx_nodes"])
        for i, o in enumerate(ops):
            w.writerow([o["name"], o["op_type"], f"t{i:04d}",
                        f"p{level[o['name']]:03d}", o["latency"],
                        ";".join(o["depends_on"]), 64 * 4, "x".join(map(str, o["dims"])),
                        ";".join(o["onnx_nodes"])])

    clock = 0
    assignments = []
    for i, o in enumerate(ops):
        assignments.append({"task_id": f"t{i:04d}", "operator": o["name"],
                            "pipeline": f"p{level[o['name']]:03d}",
                            "start_cycle": clock, "end_cycle": clock + o["latency"]})
        clock += o["latency"]
    (profile_dir / "schedule.json").write_text(json.dumps(
        {"schema_version": 1, "makespan_cycles": clock, "assignments": assignments}),
        encoding="utf-8")
    (profile_dir / "profile_summary.json").write_text(json.dumps(
        {"schema_version": 1, "onnx": "fixture.onnx", "makespan_cycles": clock,
         "op_count": len(ops)}), encoding="utf-8")


def test_analyze_hot_patterns_and_breakdown(tmp_path: Path):
    profile_dir = tmp_path / "base" / "profile"
    _write_profile_fixture(profile_dir)
    report = analyze(profile_dir)

    assert report["makespan_cycles"] == 310  # 100+50+50+50+40+20
    # critical path = mm1 -> erf1 -> erf2 -> erf3 -> add1 (relu_side is a side branch)
    assert [step["name"] for step in report["critical_path"]] == \
        ["mm1", "erf1", "erf2", "erf3", "add1"]
    assert report["critical_path_cycles"] == 290

    top = report["hot_patterns"][0]
    assert top["op_type"] == "Erf" and top["count"] == 3
    assert top["total_cycles"] == 150
    assert top["onnx_nodes"] == ["/act1/Erf", "/act2/Erf", "/act3/Erf"]
    assert top["task_ids"] == ["t0001", "t0003", "t0004"]  # relu_side is t0002
    assert top["share"] == round(150 / 290, 6)

    pipelines = {p["pipeline"]: p for p in report["pipeline_breakdown"]}
    assert pipelines["p002"]["op_count"] == 2  # erf1 + relu_side share level 2
    assert set(pipelines) == {"p001", "p002", "p003", "p004", "p005"}

    table = {(r["op_type"], r["shape_class"]): r for r in report["cost_table"]}
    assert table[("Erf", "<1e2")]["count"] == 3
    assert table[("Erf", "<1e2")]["mean_cycles"] == 50
    assert table[("MatMul", "<1e2")]["mean_cycles"] == 100


def test_analyze_fails_loud_on_unknown_key(tmp_path: Path):
    profile_dir = tmp_path / "profile"
    _write_profile_fixture(profile_dir)
    tg = json.loads((profile_dir / "taskgraph.json").read_text(encoding="utf-8"))
    tg["operators"][0]["surprise_field"] = 1
    (profile_dir / "taskgraph.json").write_text(json.dumps(tg), encoding="utf-8")
    with pytest.raises(ContractError):
        analyze(profile_dir)


def test_analyze_fails_loud_on_cross_artifact_mismatch(tmp_path: Path):
    profile_dir = tmp_path / "profile"
    _write_profile_fixture(profile_dir)
    summary = json.loads((profile_dir / "profile_summary.json").read_text(encoding="utf-8"))
    summary["makespan_cycles"] += 1  # disagree with schedule.json
    (profile_dir / "profile_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ContractError):
        analyze(profile_dir)


# ── predict_delta ─────────────────────────────────────────────────────────────

_REPORT = {
    "cost_table": [
        {"op_type": "Erf", "shape_class": "<1e2", "count": 4, "mean_cycles": 50,
         "min_cycles": 50, "max_cycles": 50},
        {"op_type": "Relu", "shape_class": "<1e2", "count": 2, "mean_cycles": 20,
         "min_cycles": 20, "max_cycles": 20},
        {"op_type": "Relu", "shape_class": "1e2-1e4", "count": 2, "mean_cycles": 80,
         "min_cycles": 80, "max_cycles": 80},
    ]
}


def test_predict_delta_arithmetic_and_params():
    # by-site pricing: each affected site at its own shape-class row
    out = predict_delta(_REPORT, {"Erf": -4, "Relu": 2}, {},
                        {"Erf": ["<1e2"] * 4, "Relu": ["<1e2", "<1e2"]})
    assert out["predicted_delta_cycles"] == -4 * 50 + 2 * 20
    assert out["params"] == "Erf-4;Relu+2"
    assert {b["op_type"] for b in out["basis"]} == {"Erf", "Relu"}
    assert out["basis"][0]["source"] == "cost_table:by-site"

    # no site info: per-site price = sum of ALL shape-class rows of the op
    out = predict_delta(_REPORT, {"Erf": -4, "Relu": 2}, {})
    assert out["predicted_delta_cycles"] == -4 * 50 + 2 * (20 + 80)
    assert out["basis"][0]["source"] == "cost_table:all-shapes"


def test_predict_delta_prices_small_site_by_its_shape_class():
    """Regression (E2E): softmax->relu at one small site predicted EXACTLY 0
    under the old whole-op count-weighted mean (the big-site rows drowned the
    small site), so admission's strictly-negative check rejected a real win.
    Per-shape-class rows must price the actual affected sites."""
    report = {
        "cost_table": [
            # one small attention-softmax site + many big softmax sites
            {"op_type": "Softmax", "shape_class": "1e2-1e4", "count": 1,
             "mean_cycles": 20, "min_cycles": 20, "max_cycles": 20},
            {"op_type": "Softmax", "shape_class": ">=1e8", "count": 9,
             "mean_cycles": 30, "min_cycles": 30, "max_cycles": 30},
            {"op_type": "Relu", "shape_class": "1e2-1e4", "count": 2,
             "mean_cycles": 10, "min_cycles": 10, "max_cycles": 10},
            {"op_type": "Relu", "shape_class": ">=1e8", "count": 2,
             "mean_cycles": 48, "min_cycles": 48, "max_cycles": 48},
        ]
    }
    # the OLD weighted-mean math: softmax (1*20+9*30)/10 == relu (2*10+2*48)/4
    # == 29 -> predicted delta exactly 0 -> the proposal was rejected
    old_softmax = (1 * 20 + 9 * 30) / 10
    old_relu = (2 * 10 + 2 * 48) / 4
    assert old_softmax == old_relu == 29

    out = predict_delta(report, {"Softmax": -1, "Relu": 1}, {},
                        {"Softmax": ["1e2-1e4"], "Relu": ["1e2-1e4"]})
    assert out["predicted_delta_cycles"] == -20 + 10  # the small rows, not 0
    assert out["predicted_delta_cycles"] < 0          # admissible again


def test_predict_delta_e2e_softmax_relu_repro():
    """The exact E2E-R1 repro numbers, locked: a softmax->relu attention
    retune removing 4 softmax sites (528 cycles each at their shape class)
    and inserting 4 relu sites (144 cycles each) must predict -1536. Under
    the old whole-op count-weighted mean the same change predicted exactly 0
    (both weighted means land on 1188) and admission rejected a real win."""
    report = {
        "cost_table": [
            {"op_type": "Softmax", "shape_class": "1e4-1e6", "count": 4,
             "mean_cycles": 528, "min_cycles": 528, "max_cycles": 528},
            {"op_type": "Softmax", "shape_class": ">=1e8", "count": 20,
             "mean_cycles": 1320, "min_cycles": 1320, "max_cycles": 1320},
            {"op_type": "Relu", "shape_class": "1e4-1e6", "count": 4,
             "mean_cycles": 144, "min_cycles": 144, "max_cycles": 144},
            {"op_type": "Relu", "shape_class": ">=1e8", "count": 32,
             "mean_cycles": 1318.5, "min_cycles": 1318.5, "max_cycles": 1318.5},
        ]
    }
    # the OLD math: both whole-op count-weighted means equal 1188 -> delta 0
    old_softmax = (4 * 528 + 20 * 1320) / 24
    old_relu = (4 * 144 + 32 * 1318.5) / 36
    assert old_softmax == old_relu == 1188
    assert -4 * old_softmax + 4 * old_relu == 0  # the old false rejection

    out = predict_delta(report, {"Softmax": -4, "Relu": 4}, {},
                        {"Softmax": ["1e4-1e6"] * 4, "Relu": ["1e4-1e6"] * 4})
    assert out["predicted_delta_cycles"] == -1536  # -4*528 + 4*144
    assert out["predicted_delta_cycles"] < 0       # admissible again


def test_predict_delta_is_idempotent():
    a = predict_delta(_REPORT, {"Relu": 2, "Erf": -4}, {},
                      {"Erf": ["<1e2"] * 4, "Relu": ["<1e2", "<1e2"]})  # flipped
    b = predict_delta(_REPORT, {"Erf": -4, "Relu": 2}, {},
                      {"Relu": ["<1e2", "<1e2"], "Erf": ["<1e2"] * 4})
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_predict_delta_missing_op_fails_loud():
    with pytest.raises(ValueError):
        predict_delta(_REPORT, {"Conv": -1}, {})
    ok = predict_delta(_REPORT, {"Conv": -1}, {"Conv": 10})
    assert ok["basis"][0]["source"] == "override"


def test_predict_delta_sites_validation_fails_loud():
    # wrong site count for the delta
    with pytest.raises(ValueError, match="one class per affected op instance"):
        predict_delta(_REPORT, {"Relu": 2}, {},
                      {"Relu": ["<1e2"]})
    # declared class has no row for an otherwise present op type
    with pytest.raises(ValueError, match="shape class"):
        predict_delta(_REPORT, {"Relu": 1}, {},
                      {"Relu": ["1e6-1e8"]})
    # the class-row miss is overridable (inserted op at a shape the base
    # model never runs at that op type)
    ok = predict_delta(_REPORT, {"Relu": 1}, {"Relu": 7},
                       {"Relu": ["1e6-1e8"]})
    assert ok["predicted_delta_cycles"] == 7
    assert ok["basis"][0]["source"] == "cost_table:by-site"
    # --sites carrying an op absent from the delta is a typo, fail loud
    with pytest.raises(ValueError, match="not in the op delta"):
        predict_delta(_REPORT, {"Relu": 1}, {}, {"Erf": ["<1e2"]})


# ── placeholder profiler: delta direction (GELU vs ReLU) ─────────────────────

def test_placeholder_profiler_delta_direction(tmp_path: Path):
    torch = pytest.importorskip("torch")
    torch_nn = torch.nn
    import placeholder_profiler  # noqa: E402

    class Tiny(torch_nn.Module):
        def __init__(self, act):
            super().__init__()
            self.fc1 = torch_nn.Linear(64, 64)
            self.act = act
            self.fc2 = torch_nn.Linear(64, 64)

        def forward(self, x):
            return self.fc2(self.act(self.fc1(x)))

    def export(model, path):
        model.eval()
        torch.onnx.export(model, torch.randn(1, 64), str(path),
                          input_names=["x"], output_names=["out"],
                          opset_version=17, do_constant_folding=True)
        return path

    torch.manual_seed(0)
    gelu_onnx = export(Tiny(torch_nn.GELU()), tmp_path / "gelu.onnx")
    relu_onnx = export(Tiny(torch_nn.ReLU()), tmp_path / "relu.onnx")

    gelu_dir = tmp_path / "p_gelu"
    relu_dir = tmp_path / "p_relu"
    g = placeholder_profiler.profile(Path(gelu_onnx), gelu_dir)
    r = placeholder_profiler.profile(Path(relu_onnx), relu_dir)

    tg = json.loads((gelu_dir / "taskgraph.json").read_text(encoding="utf-8"))
    gelu_ops = {op["op_type"] for op in tg["operators"]}
    assert "Erf" in gelu_ops          # GELU decomposes into the erf chain
    assert g["makespan_cycles"] > r["makespan_cycles"]  # direction, not ratio

    # artifacts satisfy the contract cross-checks analyze enforces
    report = analyze(gelu_dir)
    assert report["makespan_cycles"] == g["makespan_cycles"]


def test_placeholder_profiler_required_op_coverage():
    """The op set the workflow contract names explicitly must always be
    supported — a regression here turns whole variants into unsupported_op
    eliminations at verify time."""
    import placeholder_profiler  # noqa: E402

    required = {
        "Conv", "MatMul", "Gemm", "Relu", "Erf", "Tanh", "Mul", "Add", "Pow",
        "Div", "ReduceL2", "Transpose", "Reshape", "Flatten", "Softmax",
        "LayerNormalization", "Identity",
    }
    missing = required - placeholder_profiler.SUPPORTED_OPS
    assert not missing, f"required ops lost from SUPPORTED_OPS: {sorted(missing)}"


def test_placeholder_profiler_unsupported_op_fails_loud(tmp_path: Path):
    pytest.importorskip("onnx")
    import onnx
    from onnx import helper, TensorProto
    import placeholder_profiler  # noqa: E402

    node = helper.make_node("TreeEnsembleRegressor", ["x"], ["y"], name="weird",
                            domain="ai.onnx.ml")
    graph = helper.make_graph([node], "g",
                              [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 4])],
                              [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 1])])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    onnx.save(model, str(tmp_path / "weird.onnx"))
    with pytest.raises(placeholder_profiler.UnsupportedOpsError):
        placeholder_profiler.profile(tmp_path / "weird.onnx", tmp_path / "p")


# ── mfu_adapter: raw mfu_benchmark products -> contract four-piece ────────────

def _mfu_raw_fixture(tmp_path: Path) -> tuple[Path, dict]:
    """Run the deployed-shape mfu_benchmark placeholder on a real tiny onnx —
    the raw products carry exactly the documented contract shapes, so the
    adapter is tested against the same form the real remote script must keep
    (the placeholder's docstring pins the interface). Returns (profile_dir,
    schedule_result)."""
    torch = pytest.importorskip("torch")
    pytest.importorskip("onnx")

    class Tiny(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.fc1 = torch.nn.Linear(32, 32)
            self.act = torch.nn.GELU()
            self.fc2 = torch.nn.Linear(32, 32)

        def forward(self, x):
            return self.fc2(self.act(self.fc1(x)))

    onnx_path = tmp_path / "model.onnx"
    model = Tiny().eval()
    torch.onnx.export(model, torch.randn(1, 32), str(onnx_path),
                      input_names=["x"], output_names=["out"],
                      opset_version=17, do_constant_folding=True)
    profile_dir = tmp_path / "profile"
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "mfu_benchmark.py"), str(onnx_path),
         "--chip", "6613", "--precision", "INT8", "--core-num", "1",
         "-o", str(profile_dir), "--timeout", "60"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    schedule = json.loads(
        next(p for p in sorted(profile_dir.rglob("schedule_result.json")))
        .read_text(encoding="utf-8"))
    return profile_dir, schedule


def test_mfu_adapter_maps_raw_products_into_contract_four_piece(tmp_path: Path):
    """Field-by-field mapping: makespan == schedule_result.parallel_cycles
    (the canonical makespan), structural fields verbatim from the raw
    taskgraph, latency joined from the subgraph tasks, and the strict
    analyzer accepts the result (closed schema + cross-artifact agreement)."""
    import mfu_adapter  # noqa: E402

    profile_dir, schedule = _mfu_raw_fixture(tmp_path)
    raw_dir = next(p for p in sorted(profile_dir.glob("*/schedule_result.json"))).parent
    raw_tg = json.loads(
        next(raw_dir.glob("*_taskgraph.json")).read_text(encoding="utf-8"))
    raw_tasks = json.loads(
        (raw_dir / "subgraph_0_tasks.json").read_text(encoding="utf-8"))["tasks"]
    cycles_by_task = {t["task_id"]: t["cycles"] for t in raw_tasks}

    result = mfu_adapter.adapt(profile_dir, None)

    assert result["makespan_cycles"] == schedule["parallel_cycles"]
    tg = json.loads((profile_dir / "taskgraph.json").read_text(encoding="utf-8"))
    sc = json.loads((profile_dir / "schedule.json").read_text(encoding="utf-8"))
    sm = json.loads((profile_dir / "profile_summary.json").read_text(encoding="utf-8"))
    assert sm["makespan_cycles"] == schedule["parallel_cycles"] == sc["makespan_cycles"]
    assert sm["onnx"] == raw_tg["onnx"]
    assert sm["op_count"] == len(raw_tg["operators"]) == len(tg["operators"])
    for op, raw in zip(tg["operators"], raw_tg["operators"]):
        assert op["name"] == raw["name"]
        assert op["op_type"] == raw["op_type"]
        assert op["task_id"] == raw["task_id"]
        assert op["pipeline"] == raw["pipeline"]
        assert op["depends_on"] == raw["depends_on"]
        assert op["output_memory"] == raw["output_memory"]
        assert op["output_dimensions"] == raw["output_dimensions"]
        assert op["latency"] == cycles_by_task[raw["task_id"]]  # joined, not guessed
        assert op["onnx_nodes"] == [raw["name"]]                # 1:1 rule
    # derived schedule: end-start == latency and max(end) == canonical makespan
    lat = {op["task_id"]: op["latency"] for op in tg["operators"]}
    for a in sc["assignments"]:
        assert a["end_cycle"] - a["start_cycle"] == lat[a["task_id"]]
    assert sc["makespan_cycles"] == max(a["end_cycle"] for a in sc["assignments"])
    # ops.csv carries exactly the contract columns
    lines = (profile_dir / "ops.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == ("name,op_type,task_id,pipeline,latency,depends_on,"
                        "output_memory,output_dimensions,onnx_nodes")
    assert len(lines) - 1 == len(tg["operators"])
    # the strict analyzer (closed schema + cross-artifact consistency) accepts it
    report = analyze(profile_dir)
    assert report["makespan_cycles"] == schedule["parallel_cycles"]
    assert report["hot_patterns"], "the tiny GELU graph must yield hot patterns"


def test_mfu_adapter_is_idempotent_and_honors_onnx_override(tmp_path: Path):
    import mfu_adapter  # noqa: E402

    profile_dir, _ = _mfu_raw_fixture(tmp_path)
    mfu_adapter.adapt(profile_dir, None)
    before = {name: (profile_dir / name).read_bytes() for name in
              ("taskgraph.json", "ops.csv", "schedule.json", "profile_summary.json")}
    mfu_adapter.adapt(profile_dir, str(tmp_path / "elsewhere.onnx"))
    after = {name: (profile_dir / name).read_bytes() for name in before}
    # deterministic re-derivation: ONLY the onnx-bearing fields may move
    assert after["ops.csv"] == before["ops.csv"]
    assert after["schedule.json"] == before["schedule.json"]
    sm = json.loads(after["profile_summary.json"].decode("utf-8"))
    assert sm["onnx"] == str((tmp_path / "elsewhere.onnx").resolve())


def test_mfu_adapter_fails_loud_on_missing_or_inconsistent_raw(tmp_path: Path):
    """Every conversion gap is a hard error naming what is missing — the
    adapter must never fabricate a field or silently average two disagreeing
    artifacts (the whole point of routing real evaluations through it)."""
    import mfu_adapter  # noqa: E402

    profile_dir, _ = _mfu_raw_fixture(tmp_path)
    raw_dir = next(p for p in sorted(profile_dir.glob("*/schedule_result.json"))).parent

    # 1. no raw products at all
    empty = tmp_path / "empty_profile"
    empty.mkdir()
    with pytest.raises(mfu_adapter.AdapterError, match="no mfu_benchmark raw products"):
        mfu_adapter.adapt(empty, None)

    # 2. ambiguous raw dirs (two onnx stems profiled into one dir)
    shutil.copytree(raw_dir, profile_dir / "other_stem")
    with pytest.raises(mfu_adapter.AdapterError, match="ambiguous"):
        mfu_adapter.adapt(profile_dir, None)
    shutil.rmtree(profile_dir / "other_stem")

    # 3. a taskgraph field the raw products simply do not carry
    case_no = 0

    def copy_fixture() -> Path:
        nonlocal case_no
        case_no += 1
        fresh = tmp_path / f"case_{case_no}"
        shutil.copytree(profile_dir, fresh)
        return fresh

    fresh = copy_fixture()
    rd = next(p for p in sorted(fresh.glob("*/schedule_result.json"))).parent
    doc = json.loads((rd / "model_taskgraph.json").read_text(encoding="utf-8"))
    del doc["operators"][0]["output_memory"]
    (rd / "model_taskgraph.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(mfu_adapter.AdapterError,
                       match=r"missing field\(s\).*output_memory"):
        mfu_adapter.adapt(fresh, None)

    # 4. the latency csv disagrees with the subgraph tasks
    fresh = copy_fixture()
    rd = next(p for p in sorted(fresh.glob("*/schedule_result.json"))).parent
    doc = json.loads((rd / "subgraph_0_tasks.json").read_text(encoding="utf-8"))
    doc["tasks"][0]["cycles"] += 1
    (rd / "subgraph_0_tasks.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(mfu_adapter.AdapterError, match="inconsistent cycles"):
        mfu_adapter.adapt(fresh, None)

    # 5. canonical makespan below the dependency critical path
    fresh = copy_fixture()
    rd = next(p for p in sorted(fresh.glob("*/schedule_result.json"))).parent
    doc = json.loads((rd / "schedule_result.json").read_text(encoding="utf-8"))
    doc["parallel_cycles"] = 1
    (rd / "schedule_result.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(mfu_adapter.AdapterError,
                       match="below the dependency critical path"):
        mfu_adapter.adapt(fresh, None)

    # 6. a taskgraph operator with no subgraph task (graphs disagree)
    fresh = copy_fixture()
    rd = next(p for p in sorted(fresh.glob("*/schedule_result.json"))).parent
    doc = json.loads((rd / "subgraph_0_tasks.json").read_text(encoding="utf-8"))
    doc["tasks"] = doc["tasks"][1:]
    (rd / "subgraph_0_tasks.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(mfu_adapter.AdapterError, match="no task for task_id"):
        mfu_adapter.adapt(fresh, None)

    # 7. duplicate task_id in the subgraph tasks (ambiguous raw products —
    # a silent dict overwrite would pick a winner without telling anyone)
    fresh = copy_fixture()
    rd = next(p for p in sorted(fresh.glob("*/schedule_result.json"))).parent
    doc = json.loads((rd / "subgraph_0_tasks.json").read_text(encoding="utf-8"))
    doc["tasks"].append(dict(doc["tasks"][0]))
    (rd / "subgraph_0_tasks.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(mfu_adapter.AdapterError, match="duplicate task_id"):
        mfu_adapter.adapt(fresh, None)


def test_mfu_adapter_accepts_scalar_output_operator(tmp_path: Path):
    """An operator whose primary output is a SCALAR carries an empty
    output_dimensions list — a legal static shape, not a corrupt artifact
    (the validation must price it into the smallest shape bucket, never
    reject it)."""
    import mfu_adapter  # noqa: E402

    profile_dir, schedule = _mfu_raw_fixture(tmp_path)
    raw_tg = (next(p for p in sorted(profile_dir.glob("*/schedule_result.json")))
              .parent / "model_taskgraph.json")
    doc = json.loads(raw_tg.read_text(encoding="utf-8"))
    doc["operators"][0]["output_dimensions"] = []
    raw_tg.write_text(json.dumps(doc), encoding="utf-8")

    result = mfu_adapter.adapt(profile_dir, None)

    assert result["makespan_cycles"] == schedule["parallel_cycles"]
    tg = json.loads((profile_dir / "taskgraph.json").read_text(encoding="utf-8"))
    assert tg["operators"][0]["output_dimensions"] == []
    # downstream: the analyzer prices the scalar op into the smallest bucket
    report = analyze(profile_dir)
    assert report["makespan_cycles"] == schedule["parallel_cycles"]
    scalar_rows = [row for row in report["cost_table"]
                   if row["shape_class"] == "<1e2"]
    assert scalar_rows, "the scalar-output op must land in the <1e2 bucket"


def test_mfu_adapter_cli_fail_loud_exit_code(tmp_path: Path):
    """The CLI surface the chain drives: rc=2 + a stderr line naming the gap."""
    empty = tmp_path / "nope"
    empty.mkdir()
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "mfu_adapter.py"),
         "--profile-dir", str(empty)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "mfu_adapter:" in proc.stderr
    assert "raw products" in proc.stderr


# ── predict_delta.build_change_sig ────────────────────────────────────────────

def test_build_change_sig_is_canonical():
    from predict_delta import build_change_sig

    a = build_change_sig("activation", "Erf-4;Relu+2", ["blocks.1.mlp", "blocks.0.mlp"])
    b = build_change_sig("activation", "Erf-4;Relu+2", ["blocks.0.mlp", "blocks.1.mlp"])
    assert a == b == "activation:Erf-4;Relu+2:blocks.0.mlp,blocks.1.mlp"
    with pytest.raises(ValueError):
        build_change_sig("activation", "Erf-4", [])


# ── gen_export_onnx ───────────────────────────────────────────────────────────

_CONTRACTS = {
    "model_facts": {
        "module": "tiny_model",
        "factory": "build_model",
        "args": [],
        "kwargs": {},
        "dummy_inputs": [{"name": "x", "shape": [1, 8], "dtype": "float32"}],
    }
}


def test_gen_export_onnx_generates_deterministic_script(tmp_path: Path):
    from gen_export_onnx import generate

    contracts = tmp_path / "contracts.json"
    contracts.write_text(json.dumps(_CONTRACTS), encoding="utf-8")
    out_dir = tmp_path / "artifacts"

    a = generate(contracts, out_dir)
    b = generate(contracts, out_dir)
    assert a["sha256"] == b["sha256"]                       # byte-idempotent
    script = (out_dir / "export_onnx.py").read_text(encoding="utf-8")
    assert '"module": "tiny_model"' in script
    assert '"name": "x"' in script and '"shape": [' in script  # indent splits the list
    assert "opset_version=17" in script
    compile(script, "export_onnx.py", "exec")               # valid python


def test_gen_export_onnx_rejects_dynamic_axes(tmp_path: Path):
    from gen_export_onnx import generate

    facts = {"model_facts": {
        "module": "m", "factory": "f", "args": [], "kwargs": {},
        "dummy_inputs": [{"name": "x", "shape": [1, 8],
                          "dynamic_axes": {"x": {0: "batch"}}}]}}
    contracts = tmp_path / "contracts.json"
    contracts.write_text(json.dumps(facts), encoding="utf-8")
    with pytest.raises(ValueError):
        generate(contracts, tmp_path / "artifacts")


def test_gen_export_onnx_rejects_incomplete_facts(tmp_path: Path):
    from gen_export_onnx import generate

    contracts = tmp_path / "contracts.json"
    contracts.write_text(json.dumps({"model_facts": {"module": "m"}}),
                         encoding="utf-8")
    with pytest.raises(ValueError):
        generate(contracts, tmp_path / "artifacts")


# ── render_run (<<k>> token chain) ────────────────────────────────────────────
# Token syntax is <<k>> and MUST stay that way: agent.md bodies and template
# examples are Jinja2-rendered by the engine — a {{k}} token becomes an
# undeclared prompt variable (validate error + StrictUndefined crash).

_INJECT_SRC = _SCRIPTS / "orca_inject"


def _render_run_deployed(tmp_path: Path) -> Path:
    """DEPLOYED layout render_run.sh expects: $ART/scripts/render_run.sh with
    orca_inject/header.env at ../orca_inject/ (assert_shadow.py is embedded by
    path reference, so it is deployed alongside)."""
    art = tmp_path / "artifacts"
    (art / "scripts").mkdir(parents=True)
    (art / "orca_inject").mkdir(parents=True)
    shutil.copy(_SCRIPTS / "render_run.sh", art / "scripts" / "render_run.sh")
    shutil.copy(_SCRIPTS / "assert_shadow.py", art / "scripts" / "assert_shadow.py")
    shutil.copy(_INJECT_SRC / "header.env", art / "orca_inject" / "header.env")
    return art


def _render(art: Path, template: Path, out: Path, *sets: str,
            extra_env: dict[str, str] | None = None):
    env = {k: v for k, v in os.environ.items() if k != "ORCA_PYTHON"}
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(art / "scripts" / "render_run.sh"),
         "--template", str(template), "--out", str(out),
         *(arg for pair in sets for arg in ("--set", pair))],
        capture_output=True, text=True, timeout=60, env=env)


def test_render_run_substitutes_body_builtin_and_header_tokens(tmp_path: Path):
    art = _render_run_deployed(tmp_path)
    template = art / "run.template.sh"
    template.write_text(
        '"<<python>>" train.py --data <<artifacts>>/data '
        '--epochs <<epochs>> --ckpt <<ckpt>> --out-dir <<out_dir>>\n',
        encoding="utf-8")

    # poison the engine-owned env name: the renderer must NEVER take the
    # project root from it (it points at the Orca repo root, not the user's
    # project — the exact name collision that broke the baseline chain)
    proc = _render(
        art, template, art / "run.rendered.sh",
        "epochs=3", "ckpt=/tmp/a&b*c k.pth", "out_dir=contract_work/dryrun_train/",
        "shadow_dir=/tmp/shadow", "shadow_pkgs=pkg", "project_root=/tmp/proj",
        extra_env={"ORCA_PROJECT_ROOT": "/engine/repo/root"})

    # rc=0 also proves the fail-loud grep skips comment lines: header.env's
    # own header comment names <<name>> in prose and is spliced verbatim
    assert proc.returncode == 0, proc.stderr
    script = json.loads(proc.stdout.strip().splitlines()[-1])["script"]
    rendered = Path(script).read_text(encoding="utf-8")

    # body tokens: --set values (glob/&/space chars must survive literally —
    # only the KEY side of the substitution is a glob pattern) + builtins
    assert "--epochs 3 --ckpt /tmp/a&b*c k.pth" in rendered
    assert f"--data {art}/data" in rendered       # builtin <<artifacts>>
    assert "--out-dir contract_work/dryrun_train/" in rendered
    assert 'PY="python3"' in rendered             # builtin <<python>>, token gone
    # D-V4-16: the header layer forces unbuffered python — the baseline
    # finalizer re-parses the training log incrementally per poll cycle, and
    # a block-buffered epoch line would starve the live curve
    assert "export PYTHONUNBUFFERED=1" in rendered
    # header tokens rendered with the probed pathsep, none left anywhere
    assert "ORCA_SHADOW_DIR='/tmp/shadow'" in rendered
    assert "ORCA_RUN_PROJECT_ROOT='/tmp/proj'" in rendered
    assert "/engine/repo/root" not in rendered    # engine env never leaks in
    assert 'cd "$ORCA_RUN_PROJECT_ROOT"' in rendered
    pathsep = os.pathsep                          # same interpreter class as $PY
    assert f"orca_inject{pathsep}/tmp/proj" in rendered
    assert "<<python>>" not in rendered and "<<shadow_dir>>" not in rendered


def test_render_run_has_no_project_root_env_fallback(tmp_path: Path):
    """project_root must come from --set ONLY: the ORCA_PROJECT_ROOT env is
    engine-owned (= the Orca repo root) — a fallback silently anchors run
    scripts to the wrong project (the E2E baseline-chain failure)."""
    art = _render_run_deployed(tmp_path)
    template = art / "run.template.sh"
    template.write_text('"<<python>>" eval.py\n', encoding="utf-8")

    proc = _render(
        art, template, art / "run.rendered.sh",
        "shadow_dir=/tmp/shadow", "shadow_pkgs=pkg",
        extra_env={"ORCA_PROJECT_ROOT": "/engine/repo/root"})

    assert proc.returncode == 2
    assert "project_root not set" in proc.stderr
    assert "/engine/repo/root" not in proc.stderr  # the env value was ignored
    assert not (art / "run.rendered.sh").exists()


def test_render_run_fails_loud_on_unreplaced_token(tmp_path: Path):
    art = _render_run_deployed(tmp_path)
    template = art / "run.template.sh"
    template.write_text('"<<python>>" eval.py --ckpt <<ghost>>\n',
                        encoding="utf-8")

    proc = _render(
        art, template, art / "run.rendered.sh",
        "shadow_dir=/tmp/shadow", "shadow_pkgs=pkg", "project_root=/tmp/proj")

    assert proc.returncode == 2
    assert "unreplaced template tokens" in proc.stderr
    assert "<<ghost>>" in proc.stderr
    assert not (art / "run.rendered.sh").exists()  # half-rendered file removed


def test_render_run_rejects_non_identifier_set_key(tmp_path: Path):
    """A glob char in --set's key would silently match (and corrupt) OTHER
    tokens in the bash pattern substitution — it must fail loud instead."""
    art = _render_run_deployed(tmp_path)
    template = art / "run.template.sh"
    template.write_text('"<<python>>" eval.py\n', encoding="utf-8")

    proc = _render(
        art, template, art / "run.rendered.sh",
        "s*d=/tmp/x", "shadow_dir=/tmp/shadow", "shadow_pkgs=pkg",
        "project_root=/tmp/proj")

    assert proc.returncode == 2
    assert "identifier" in proc.stderr
    assert "s*d" in proc.stderr


# ── check_contracts gate (fairness-invariant token/budget enforcement) ───────

_CONTRACTS_SH = _REPO / "workflows" / "prof-opt" / "agents" / "po_contract" / "scripts" / "check_contracts.sh"


def _contracts_workspace(tmp_path: Path, *, probe_body: str | None = None,
                         full_body: str | None = None,
                         budget: dict | None = None,
                         full_train_budget: dict | None = None) -> Path:
    """Minimal workspace satisfying the po_contract v4 gate: contracts.json
    with a pinned epoch-only proxy_budget + full_train_budget fingerprint,
    four templates (probe/full byte-identical by default), measured evidence,
    real entries."""
    import hashlib

    art = tmp_path / "art"
    (art / "templates").mkdir(parents=True)
    (art / "contract_work").mkdir(parents=True)
    (art / "scripts").mkdir(parents=True)
    shutil.copy(_SCRIPTS / "metric_curve.py", art / "scripts" / "metric_curve.py")

    for name in ("train.py", "eval.py", "exporter.py"):
        (art / name).write_text("# entry\n", encoding="utf-8")

    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    budget = budget or {"epochs": 1, "dataset_knob": None,
                        "data_value": None, "max_steps": None, "seed": 0}
    contracts = {
        "viable": True,
        "reason": "tier A, measured; 训练须按给定轮数精确执行，自带 "
                  "early-stopping 项目不在范围",
        "interpreter": {"sys_executable": sys.executable, "flags_check": "pass"},
        "shadow": {"shadow_root": str(art / "shadow"), "shadow_pkgs": ["pkg"]},
        "model_facts": {"module": "pkg.model", "factory": "build",
                        "args": [], "kwargs": {},
                        "dummy_inputs": [{"name": "x", "shape": [1, 4],
                                          "dtype": "float32"}]},
        "train": {"tier": "A", "entry": str(art / "train.py"),
                  "entry_sha256": sha(art / "train.py"),
                  "flags": {"epochs": "--epochs", "out_dir": "--out-dir",
                            "seed": "--seed", "max_steps": "--max-steps",
                            "data_knob": budget["dataset_knob"]},
                  "ckpt_output_rule": "{out_dir}/epoch_*.pth",
                  "ckpt_per_epoch": True,
                  "epoch_metric_extraction": {
                      "kind": "stdout_regex",
                      "pattern": r"epoch (?P<epoch>\d+) metric=(?P<metric>[0-9.]+)"},
                  "train_epochs_full": 10},
        "full_train_budget": full_train_budget or {
            "epochs": 2, "seed": 0,
            "data": {"dataset_knob": None, "data_value": None}},
        "eval": {"tier": "A", "entry": str(art / "eval.py"),
                 "entry_sha256": sha(art / "eval.py"),
                 "flags": {"ckpt": "--ckpt"}, "ckpt_container": "bare",
                 "metric_extraction": {"kind": "stdout_regex",
                                       "pattern": "acc=([0-9.]+)"},
                 "metric_direction": "higher_better"},
        "export": {"entry": str(art / "exporter.py"),
                   "entry_sha256": sha(art / "exporter.py"),
                   "generated": False, "argv_facts": "pinned"},
        "proxy_budget": budget,
        "probe_cap_mechanism": "stop-at-k",
        "exemptions": [],
        "sitecustomize_merge": {"found": False, "path": "", "merged": False},
    }
    (art / "contracts.json").write_text(json.dumps(contracts), encoding="utf-8")

    probe = probe_body or (
        '"<<python>>" train.py --epochs <<epochs>> --out-dir <<out_dir>> '
        '--seed <<seed>> --device <<device>>\n')
    # v4 single training pipeline: the full template defaults to the SAME
    # bytes as the probe template (the gate asserts identity)
    full = probe if full_body is None else full_body
    (art / "templates" / "run_probe_finetune.template.sh").write_text(
        probe, encoding="utf-8")
    (art / "templates" / "run_full_finetune.template.sh").write_text(
        full, encoding="utf-8")
    (art / "templates" / "run_eval.template.sh").write_text(
        '"<<python>>" eval.py --ckpt <<ckpt>> > <<log>> 2>&1\n', encoding="utf-8")
    (art / "templates" / "export_onnx.template.sh").write_text(
        '"<<python>>" exporter.py --out <<out>> --seed <<seed>>\n', encoding="utf-8")

    cw = art / "contract_work"
    (cw / "quickrun_train.log").write_text(
        "epoch 1 metric=0.9\nepoch 2 metric=0.8\n", encoding="utf-8")
    (cw / "train_quickrun.json").write_text(
        json.dumps({"status": "runs_minimal_budget",
                    "train_log": str(cw / "quickrun_train.log"),
                    "epoch_metric_extraction_check": "pass"}),
        encoding="utf-8")
    (cw / "eval_dual_ckpt.json").write_text(
        json.dumps({"metric_seed0": 0.1, "metric_seed1": 0.6, "moved": True,
                    "ckpt_container": "bare"}), encoding="utf-8")
    (cw / "export_check.json").write_text(
        json.dumps({"loaded": True}), encoding="utf-8")
    (cw / "proxy_budget_selection.json").write_text(
        json.dumps({"dataset_knob": budget["dataset_knob"],
                    "data_value": budget["data_value"],
                    "rationale": "small subset, fast"}), encoding="utf-8")
    return art


def _run_contracts_gate(art: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    return subprocess.run(["bash", str(_CONTRACTS_SH)], capture_output=True,
                          text=True, timeout=60, env=env)


def test_check_contracts_gate_passes_consistent_workspace(tmp_path: Path):
    art = _contracts_workspace(tmp_path)
    proc = _run_contracts_gate(art)
    assert proc.returncode == 0, proc.stderr
    assert "PASS" in proc.stderr


def test_check_contracts_gate_rejects_retired_quickrun_status(tmp_path: Path):
    """The quick-run evidence speaks ONLY the v4 classification set: a
    downgraded epochs-zero verdict is not a completed 2-epoch measurement
    and must fail the gate (fail loud, never silently accepted)."""
    art = _contracts_workspace(tmp_path)
    (art / "contract_work" / "train_quickrun.json").write_text(
        json.dumps({"status": "runs_epochs_zero_rejected"}), encoding="utf-8")
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "train_quickrun.json" in proc.stderr


def test_check_contracts_gate_rejects_zero_based_epoch_sequence(tmp_path: Path):
    """A pattern that matches the quickrun log but extracts 0-based epochs
    (0, 1, ...) passes every syntax/boundary check yet breaks every
    downstream consumer (metric_curve extract, stop_at_epoch) only after the
    full baseline has already run - the gate must re-run the extraction on
    the REAL quickrun log and fail here, at the contract stage."""
    art = _contracts_workspace(tmp_path)
    (art / "contract_work" / "quickrun_train.log").write_text(
        "epoch 0 metric=0.9\nepoch 1 metric=0.8\n", encoding="utf-8")
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "contiguous from 1" in proc.stderr


def test_check_contracts_gate_enforces_token_budget_consistency(tmp_path: Path):
    # knob pinned but the probe template dropped the data token -> the
    # fairness invariant (same budget rendered) would silently break
    art = _contracts_workspace(
        tmp_path,
        budget={"epochs": 1, "dataset_knob": "--limit", "data_value": 2000,
                "max_steps": None, "seed": 0})
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "lacks <<data_value>>" in proc.stderr

    # max_steps pinned but the token vanished -> truncation would silently
    # disappear (render_run drops unused --set values)
    art2 = _contracts_workspace(
        tmp_path / "b",
        budget={"epochs": 1, "dataset_knob": None, "data_value": None,
                "max_steps": 500, "seed": 0})
    proc2 = _run_contracts_gate(art2)
    assert proc2.returncode == 1
    assert "lacks" in proc2.stderr and "<<max_steps>>" in proc2.stderr

    # no knob recorded (epochs-only budget) yet the template still carries the
    # data token -> every render would fail on the unreplaced token
    art3 = _contracts_workspace(
        tmp_path / "c",
        probe_body='"<<python>>" train.py --epochs <<epochs>> '
        '--out-dir <<out_dir>> --seed <<seed>> --limit <<data_value>>\n')
    proc3 = _run_contracts_gate(art3)
    assert proc3.returncode == 1
    assert "carries <<data_value>>" in proc3.stderr

    # max_steps=null but the template still carries the step-cap token ->
    # the symmetric branch (renders would fail on the unreplaced token)
    art4 = _contracts_workspace(
        tmp_path / "d",
        budget={"epochs": 1, "dataset_knob": "--limit", "data_value": 2000,
                "max_steps": None, "seed": 0},
        probe_body='"<<python>>" train.py --epochs <<epochs>> '
        '--out-dir <<out_dir>> --seed <<seed>> --limit <<data_value>> '
        '--max-steps <<max_steps>>\n')
    proc4 = _run_contracts_gate(art4)
    assert proc4.returncode == 1
    assert "carries" in proc4.stderr and "<<max_steps>>" in proc4.stderr


def test_check_contracts_gate_requires_device_token_in_training_templates(tmp_path: Path):
    # a training template without <<device>> renders a card-agnostic training
    # — the allocation ledger's mutual exclusion silently breaks (v6 §3.2)
    art = _contracts_workspace(
        tmp_path,
        probe_body='"<<python>>" train.py --epochs <<epochs>> --out-dir <<out_dir>> '
                   '--seed <<seed>>\n')
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "<<device>>" in proc.stderr

    # the symmetric half: the EVAL/EXPORT templates must NOT grow the token
    # (only the two training renders bind a claimed card)
    art2 = _contracts_workspace(tmp_path / "b")
    tpl = art2 / "templates" / "run_eval.template.sh"
    tpl.write_text(tpl.read_text(encoding="utf-8").rstrip("\n")
                   + " --device <<device>>\n", encoding="utf-8")
    assert _run_contracts_gate(art2).returncode == 0

    # the full-budget training template is pinned independently (a divergent
    # edit to its token row alone must still fail)
    art3 = _contracts_workspace(
        tmp_path / "c",
        full_body='"<<python>>" train.py --epochs <<epochs>> --out-dir <<out_dir>> '
                  '--seed <<seed>>\n')
    proc3 = _run_contracts_gate(art3)
    assert proc3.returncode == 1 and "<<device>>" in proc3.stderr


def test_check_contracts_gate_forbids_ckpt_token_in_training_templates(tmp_path: Path):
    art = _contracts_workspace(
        tmp_path,
        budget={"epochs": 1, "dataset_knob": "--limit", "data_value": 2000,
                "max_steps": 500, "seed": 0},
        probe_body='"<<python>>" train.py --epochs <<epochs>> '
        '--out-dir <<out_dir>> --seed <<seed>> --limit <<data_value>> '
        '--max-steps <<max_steps>> --resume <<ckpt>>\n')
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "<<ckpt>>" in proc.stderr and "from scratch" in proc.stderr


def test_check_contracts_gate_rejects_value_without_knob(tmp_path: Path):
    # symmetric branch: data_value recorded while dataset_knob is null — a
    # value with no knob to feed it into is a meaningless combination
    art = _contracts_workspace(
        tmp_path, budget={"epochs": 1, "dataset_knob": None, "data_value": 2000,
                          "max_steps": None, "seed": 0})
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "data_value set but dataset_knob" in proc.stderr


def test_check_contracts_gate_validates_v4_fields(tmp_path: Path):
    def rewrite(art: Path, mutate):
        contracts = json.loads((art / "contracts.json").read_text(encoding="utf-8"))
        mutate(contracts)
        (art / "contracts.json").write_text(json.dumps(contracts), encoding="utf-8")

    # full_train_budget.data must be the null pair (value-level fingerprint:
    # a recorded knob would silently change the budget meaning)
    art = _contracts_workspace(
        tmp_path / "a",
        full_train_budget={"epochs": 2, "seed": 0,
                           "data": {"dataset_knob": "--limit", "data_value": 1}})
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "full_train_budget.data" in proc.stderr

    # missing ckpt_per_epoch -> ckpt addressability undecidable downstream
    art = _contracts_workspace(tmp_path / "b")
    rewrite(art, lambda c: c["train"].pop("ckpt_per_epoch"))
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "ckpt_per_epoch" in proc.stderr

    # top-level reason must carry the admission clause (E3-07 consumer side)
    art = _contracts_workspace(tmp_path / "c")
    rewrite(art, lambda c: c.update(reason="tier A, measured"))
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "admission clause" in proc.stderr

    # probe/full templates must be byte-identical (ONE training pipeline)
    art = _contracts_workspace(
        tmp_path / "d",
        full_body='"<<python>>" train.py --epochs <<epochs>> --out-dir <<out_dir>> '
                  '--seed <<seed>> --extra-flag 1\n')
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "differ" in proc.stderr and "same source" in proc.stderr

    # wrong cap mechanism
    art = _contracts_workspace(tmp_path / "e")
    rewrite(art, lambda c: c.update(probe_cap_mechanism="epochs-only"))
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "stop-at-k" in proc.stderr

    # metric pattern that can truncate mid-number (boundary anchor check)
    art = _contracts_workspace(tmp_path / "f")
    rewrite(art, lambda c: c["train"].update(epoch_metric_extraction={
        "kind": "stdout_regex",
        "pattern": r"epoch (?P<epoch>\d+) metric=(?P<metric>[0-9.]{4})"}))
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "truncation" in proc.stderr

    # control: the unmutated v4 workspace passes (already asserted by the
    # passes-consistent test; guard the fixture itself here)
    assert _run_contracts_gate(_contracts_workspace(tmp_path / "ok")).returncode == 0


def test_check_contracts_reuse_rejects_pre_v4_contracts(tmp_path: Path):
    """A reusable workspace built before this workflow version lacks the v4
    fields — the reuse gate must fail loud with the fresh_start hint instead
    of failing cryptically downstream."""
    art = _contracts_workspace(tmp_path)
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)

    def reuse():
        return subprocess.run(["bash", str(_CONTRACTS_SH), "--reuse-check"],
                              capture_output=True, text=True, timeout=60, env=env)

    # current-version contracts + matching shas -> REUSE
    assert reuse().returncode == 0

    # strip full_train_budget: a pre-v4 contracts.json
    contracts = json.loads((art / "contracts.json").read_text(encoding="utf-8"))
    contracts.pop("full_train_budget")
    contracts["train"].pop("ckpt_per_epoch")
    (art / "contracts.json").write_text(json.dumps(contracts), encoding="utf-8")
    proc = reuse()
    assert proc.returncode == 1
    assert "predates the current workflow version" in proc.stderr
    assert "fresh_start" in proc.stderr


# ── run_baseline_chain (v4): non-blocking baseline + finalizer guardian ──────

_BASELINE_SH = _REPO / "workflows" / "prof-opt" / "agents" / "po_baseline" / "scripts" / "run_baseline_chain.sh"

_BL_MD = ("[subagent:business-logic-analyst v1 TEST]\n## 任务语义\nclassify\n"
          "## 输入输出\nx->y\n## 架构动机\nwhy\n"
          "## 逐模块职责与物理意义\nper module\n## 训练目标与指标方向\nacc higher\n")


def _baseline_ws(tmp_path: Path, *, full_epochs: int = 2, probe_k: int = 1,
                 ckpt_per_epoch: bool = True, train_body: str | None = None,
                 with_business_logic: bool = True) -> Path:
    """Deployed-layout workspace whose early chain (steps 1-3) products exist;
    the chain therefore goes straight to the full-train + finalizer launches.
    The train template's stdout IS the training log (epoch lines, matching the
    contracts extraction pattern); ckpt files land in <<out_dir>> per the
    ckpt_output_rule glob."""
    art = tmp_path / "art"
    proj = tmp_path / "proj"
    (art / "scripts").mkdir(parents=True)
    (art / "orca_inject").mkdir(parents=True)
    proj.mkdir()
    for src in ("render_run.sh", "assert_shadow.py", "metric_curve.py",
                "push_curves.py", "analyze.py", "device_alloc.py"):
        shutil.copy(_SCRIPTS / src, art / "scripts" / src)
    shutil.copy(_SCRIPTS / "orca_inject" / "header.env",
                art / "orca_inject" / "header.env")
    shutil.copy(_SCRIPTS / "orca_inject" / "sitecustomize.py",
                art / "orca_inject" / "sitecustomize.py")
    (art / "shadow" / "pkg").mkdir(parents=True)
    (art / "shadow" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (art / "base" / "profile").mkdir(parents=True)
    (art / "base" / "model.onnx").write_bytes(b"onnx-bytes")
    (art / "base" / "profile" / "profile_summary.json").write_text(
        json.dumps({"makespan_cycles": 500}), encoding="utf-8")
    (art / "base" / "bottleneck_report.json").write_text(
        json.dumps({"makespan_cycles": 500, "hot_patterns": []}), encoding="utf-8")
    (art / "baseline").mkdir()
    if with_business_logic:
        (art / "baseline" / "business_logic.md").write_text(_BL_MD, encoding="utf-8")

    rule = "{out_dir}/epoch_*.pth" if ckpt_per_epoch else "{out_dir}/model.pth"
    (art / "contracts.json").write_text(json.dumps({
        "interpreter": {"sys_executable": sys.executable},
        "shadow": {"shadow_pkgs": ["pkg"]},
        "full_train_budget": {"epochs": full_epochs, "seed": 0,
                              "data": {"dataset_knob": None, "data_value": None}},
        "proxy_budget": {"epochs": probe_k, "dataset_knob": None,
                         "data_value": None, "max_steps": None, "seed": 0},
        "train": {"ckpt_output_rule": rule, "ckpt_per_epoch": ckpt_per_epoch,
                  "epoch_metric_extraction": {
                      "kind": "stdout_regex",
                      "pattern": r"epoch (?P<epoch>\d+) loss=(?P<metric>[0-9.]+)"}},
        "eval": {"metric_extraction": {"kind": "stdout_regex",
                                       "pattern": r"acc=([0-9.]+)"}},
    }), encoding="utf-8")
    (art / "readiness").mkdir()
    (art / "readiness" / "readiness.json").write_text(
        json.dumps({"project_root": str(proj)}), encoding="utf-8")

    ckpt_line = ("for e in $(seq 1 <<epochs>>); do "
                 "touch '<<out_dir>>/epoch_'$e'.pth'; done\n" if ckpt_per_epoch
                 else "touch '<<out_dir>>/model.pth'\n")
    body = train_body or (
        "for e in $(seq 1 <<epochs>>); do echo \"epoch $e loss=0.$e\"; sleep 1.2; done\n"
        + ckpt_line)
    (art / "templates").mkdir()
    (art / "templates" / "export_onnx.template.sh").write_text("echo export\n",
                                                               encoding="utf-8")
    (art / "templates" / "run_full_finetune.template.sh").write_text(body,
                                                                     encoding="utf-8")
    (art / "templates" / "run_eval.template.sh").write_text(
        "echo 'ckpt <<ckpt>> acc=0.9'\n", encoding="utf-8")
    # the profiling mode the entry node resolved (placeholder by default)
    (art / "profile_mode.json").write_text(json.dumps(
        {"mode": "placeholder", "chip": "", "precision": None,
         "core_num": None, "resolved_by": "fallback"}), encoding="utf-8")
    # the training device backend the entry node resolved (synthetic card)
    (art / "train_device.json").write_text(
        json.dumps({"backend": "cuda", "device_count": 1,
                    "resolved_by": "test"}), encoding="utf-8")
    # the backend occupancy CLI the device ledger probes during claim (idle:
    # the only busy card in these tests is the ledger's own live lock)
    stub_dir = art / "stubbin"
    stub_dir.mkdir()
    (stub_dir / "nvidia-smi").write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  -L) printf 'GPU 0: stub\\nGPU 1: stub\\n' ;;\n"
        "  --query-gpu=*) printf '0, GPU-aa\\n1, GPU-bb\\n' ;;\n"
        "  --query-compute-apps=*) : ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n", encoding="utf-8")
    (stub_dir / "nvidia-smi").chmod(0o755)
    return art


def _chain_env(art: Path) -> dict:
    """Env for direct chain invocations: artifacts root + the fixture's
    backend-CLI stub dir in front of PATH (the device ledger's claim probes
    real occupancy through it)."""
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    stub = art / "stubbin"
    if stub.is_dir():
        env["PATH"] = str(stub) + os.pathsep + env["PATH"]
    return env


def _run_baseline_chain(art: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5", "--seed", "0"],
        capture_output=True, text=True, timeout=timeout, env=_chain_env(art))


def _wait_for(path: Path, timeout_s: float = 40) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        time.sleep(0.5)
    return False


def _train_final(art: Path) -> dict:
    return json.loads((art / "baseline" / "train_final.json").read_text(
        encoding="utf-8"))


# the po_baseline node output schema — DERIVED from workflows/prof-opt/workflow.yaml,
# never hand-copied: the chain's stdout line is the agent's final reply
# VERBATIM, so its field set must be EXACTLY the schema's in BOTH directions
# (additionalProperties:false rejects extra keys; a schema edit must not
# silently strand the chain emitter either)
def _po_baseline_schema_fields() -> set[str]:
    import yaml
    wf = yaml.safe_load(
        (_REPO / "workflows" / "prof-opt" / "workflow.yaml").read_text(encoding="utf-8"))
    schema = next(n for n in wf["nodes"]
                  if n["name"] == "po_baseline")["output_schema"]
    props = set(schema["properties"])
    assert set(schema["required"]) == props, "schema itself drifted"
    return props


def test_workflow_inputs_pin_v5_eight_input_set():
    """The input set after the sequential-gating redesign: exactly 8 inputs,
    field-for-field pinned (the retired six — npu trio / write_back /
    report_dir / probe_epochs — must stay gone; every {{ inputs.X }} Jinja
    reference that died with them must not reappear, a dangling ref crashes
    the render)."""
    import yaml
    wf = yaml.safe_load(
        (_REPO / "workflows" / "prof-opt" / "workflow.yaml").read_text(encoding="utf-8"))
    inputs = wf["inputs"]
    assert set(inputs) == {"project_root", "model_path", "latency_reduction_min",
                           "accuracy_budget", "seed", "max_rounds", "fresh_start",
                           "full_train_epoch_cap"}
    pinned = {
        "project_root": ("string", True, None),
        "model_path": ("string", True, None),
        "latency_reduction_min": ("number", True, None),
        "accuracy_budget": ("number", True, None),
        "seed": ("integer", False, 0),
        "max_rounds": ("integer", False, 100),
        "fresh_start": ("boolean", False, False),
        "full_train_epoch_cap": ("string", False, ""),
    }
    for name, (typ, required, default) in pinned.items():
        assert inputs[name]["type"] == typ, name
        assert inputs[name]["required"] is required, name
        if default is not None:
            assert inputs[name]["default"] == default, name

    for retired in ("profile_script_path", "npu_chip", "npu_precision",
                    "npu_core_num", "write_back", "report_dir", "probe_epochs"):
        assert retired not in inputs, retired

    # the anchors the freeze consumes are referenced in the baseline body
    body = (_REPO / "workflows" / "prof-opt" / "agents" / "po_baseline" / "agent.md") \
        .read_text(encoding="utf-8")
    assert "{{ inputs.latency_reduction_min }}" in body
    assert "{{ inputs.accuracy_budget }}" in body


def test_po_contract_output_schema_is_thin_envelope():
    """The contract stage's node output stays a routing envelope: contracts
    and evidence live on disk, so output must not duplicate their fields."""
    import yaml

    wf = yaml.safe_load(
        (_REPO / "workflows" / "prof-opt" / "workflow.yaml").read_text(
            encoding="utf-8"))
    node = next(n for n in wf["nodes"] if n["name"] == "po_contract")
    schema = node["output_schema"]
    props = set(schema["properties"])
    assert set(schema["required"]) == props
    assert props == {"viable", "contracts_path", "error", "generated_artifacts"}


def test_prof_opt_execution_nodes_use_thin_output_envelopes():
    """All execution-stage nodes follow the file-first output contract."""
    import yaml

    wf = yaml.safe_load(
        (_REPO / "workflows" / "prof-opt" / "workflow.yaml").read_text(
            encoding="utf-8"))
    expected = {
        "po_flatten": {"flatten_passed", "readiness_path", "error",
                       "generated_artifacts"},
        "po_baseline": {"status", "error", "generated_artifacts"},
        "po_propose": {"status", "error", "generated_artifacts"},
        "po_probe": {"status", "error", "generated_artifacts"},
        "po_full_train": {"status", "error", "generated_artifacts"},
    }
    for node in wf["nodes"]:
        if node["name"] not in expected:
            continue
        schema = node["output_schema"]
        props = set(schema["properties"])
        assert set(schema["required"]) == props
        assert props == expected[node["name"]], node["name"]


def test_check_full_train_emit_gate(tmp_path: Path):
    """The po_full_train pre-return gate checks terminal artifacts, not
    outcome quality."""
    art = tmp_path / "art"
    final = art / "final"
    final.mkdir(parents=True)
    budget = {"epochs": 1, "seed": 0,
              "data": {"dataset_knob": None, "data_value": None}}
    (art / "contracts.json").write_text(json.dumps({
        "train": {"ckpt_output_rule": "{out_dir}/ckpt.pth"},
        "full_train_budget": budget,
    }), encoding="utf-8")
    (final / "final_acc.json").write_text(json.dumps({
        "vid": "r1-01", "final_acc": 0.9, "baseline_full_acc": 0.8,
        "baseline_full_acc_source": "baseline",
        "full_train_budget": budget, "within_budget": True,
        "metric_direction": "higher_better",
    }), encoding="utf-8")
    (final / "model.onnx").write_bytes(b"onnx")
    (final / "train_status.md").write_text("training\n", encoding="utf-8")
    (final / "final_metrics.jsonl").write_text(
        '{"epoch": 1, "metric": 0.9}\n', encoding="utf-8")
    (final / "ckpt.pth").write_bytes(b"weights")

    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_full_train_emit.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr

    bad = json.loads((final / "final_acc.json").read_text(encoding="utf-8"))
    del bad["final_acc"]
    (final / "final_acc.json").write_text(json.dumps(bad), encoding="utf-8")
    proc2 = subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_full_train_emit.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60)
    assert proc2.returncode == 1
    assert "missing final_acc" in proc2.stderr


@_RETIRED_V6
def test_check_propose_emit_gate(tmp_path: Path):
    """The propose pre-return gate checks round disk closure, not verdict
    quality."""
    art = tmp_path / "art"
    (art / "scripts").mkdir(parents=True)
    shutil.copy(_SCRIPTS / "round_state.py", art / "scripts" / "round_state.py")
    shutil.copy(_SCRIPTS / "history_lib.py", art / "scripts" / "history_lib.py")
    (art / "base").mkdir(parents=True)
    (art / "base" / "origin_anchor.json").write_text(
        json.dumps({"target_cycles": 100}), encoding="utf-8")
    (art / "profile_mode.json").write_text(
        json.dumps({"mode": "placeholder"}), encoding="utf-8")
    (art / "base" / "bottleneck_analysis.json").write_text(json.dumps({
        "schema_version": 1, "base_report": "base/bottleneck_report.json",
        "summary": "s",
        "top_bottlenecks": [{"name": "P1", "op_type": "Erf", "cycles": 1,
                             "analysis": "a"}]}), encoding="utf-8")
    rd = art / "rounds" / "001"
    rd.mkdir(parents=True)
    proposals = {
        "round": 1, "exhausted": False, "filtered_count": 0,
        "exhausted_rationale": [],
        "proposals": [{
            "vid": "r1-01", "change_sig": "sig",
            "predicted_delta_cycles": -10, "edited_files": ["pkg/model.py"],
            "target_pattern_id": "P1", "predicted_acc_impact": "low",
            "sota_reference": "ref",
        }],
    }
    (rd / "proposals.json").write_text(json.dumps(proposals), encoding="utf-8")
    (art / "history.jsonl").write_text(
        '{"vid": "r1-01", "round": 1, "change_sig": "sig"}\n',
        encoding="utf-8")
    (rd / "verdicts.jsonl").write_text('{"vid": "r1-01"}\n', encoding="utf-8")
    (rd / "direction.json").write_text('{}', encoding="utf-8")
    (rd / "analysis.md").write_text("# latency\n", encoding="utf-8")
    (art / ".round_advanced").write_text(
        '{"round": 1, "mode": "latency"}', encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_propose_emit.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr

    # placeholder mode: a target_pattern_id outside the analysis list fails
    proposals["proposals"][0]["target_pattern_id"] = "P9"
    (rd / "proposals.json").write_text(json.dumps(proposals), encoding="utf-8")
    proc1b = subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_propose_emit.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60)
    assert proc1b.returncode == 1
    assert "not a name in base/bottleneck_analysis.json" in proc1b.stderr

    (rd / "proposals.json").unlink()
    proc2 = subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_propose_emit.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60)
    assert proc2.returncode == 1
    assert "proposals.json missing" in proc2.stderr


@_RETIRED_V6
def test_check_propose_emit_mfu_freeform_target(tmp_path: Path):
    """mfu mode: target_pattern_id is a free-form label and the gate never
    requires the placeholder bottleneck_analysis.json."""
    art = tmp_path / "art"
    (art / "scripts").mkdir(parents=True)
    shutil.copy(_SCRIPTS / "round_state.py", art / "scripts" / "round_state.py")
    shutil.copy(_SCRIPTS / "history_lib.py", art / "scripts" / "history_lib.py")
    (art / "base").mkdir(parents=True)
    (art / "base" / "origin_anchor.json").write_text(
        json.dumps({"target_cycles": 100}), encoding="utf-8")
    (art / "profile_mode.json").write_text(
        json.dumps({"mode": "mfu"}), encoding="utf-8")
    rd = art / "rounds" / "001"
    rd.mkdir(parents=True)
    (rd / "proposals.json").write_text(json.dumps({
        "round": 1, "exhausted": False, "filtered_count": 0,
        "exhausted_rationale": [],
        "proposals": [{
            "vid": "r1-01", "change_sig": "sig",
            "predicted_delta_cycles": -10, "edited_files": ["pkg/model.py"],
            "target_pattern_id": "dma-stall", "predicted_acc_impact": "low",
            "sota_reference": "ref",
        }],
    }), encoding="utf-8")
    (art / "history.jsonl").write_text(
        '{"vid": "r1-01", "round": 1, "change_sig": "sig"}\n',
        encoding="utf-8")
    (rd / "verdicts.jsonl").write_text('{"vid": "r1-01"}\n', encoding="utf-8")
    (rd / "direction.json").write_text('{}', encoding="utf-8")
    (rd / "analysis.md").write_text("# latency\n", encoding="utf-8")
    (art / ".round_advanced").write_text(
        '{"round": 1, "mode": "latency"}', encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_propose_emit.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


@_RETIRED_V6
def test_check_probe_emit_gate_latency_passthrough(tmp_path: Path):
    """Latency passthrough only needs the proposal node's advance marker."""
    art = tmp_path / "art"
    (art / "scripts").mkdir(parents=True)
    shutil.copy(_SCRIPTS / "round_state.py", art / "scripts" / "round_state.py")
    shutil.copy(_SCRIPTS / "history_lib.py", art / "scripts" / "history_lib.py")
    (art / "base").mkdir(parents=True)
    (art / "base" / "origin_anchor.json").write_text(
        json.dumps({"target_cycles": 100}), encoding="utf-8")
    (art / "rounds" / "001").mkdir(parents=True)
    (art / ".round_advanced").write_text(
        '{"round": 1, "mode": "latency"}', encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_probe_emit.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr

    (art / ".round_advanced").write_text(
        '{"round": 1, "mode": "accuracy"}', encoding="utf-8")
    proc2 = subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_probe_emit.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60)
    assert proc2.returncode == 1
    assert "does not record (round, latency)" in proc2.stderr


@_RETIRED_V6
def test_check_probe_emit_gate_accuracy_first_entry(tmp_path: Path):
    """Accuracy first entry requires the best vid to have a terminal probe
    row in probe_results.jsonl."""
    art = tmp_path / "art"
    (art / "scripts").mkdir(parents=True)
    shutil.copy(_SCRIPTS / "round_state.py", art / "scripts" / "round_state.py")
    shutil.copy(_SCRIPTS / "history_lib.py", art / "scripts" / "history_lib.py")
    (art / "base").mkdir(parents=True)
    (art / "base" / "origin_anchor.json").write_text(
        json.dumps({"target_cycles": 100}), encoding="utf-8")
    (art / "best.json").write_text(
        json.dumps({"vid": "r1-01", "makespan_cycles": 50}), encoding="utf-8")
    rd = art / "rounds" / "001"
    rd.mkdir(parents=True)
    (art / "history.jsonl").write_text(
        '{"vid": "r1-01", "round": 1, "outcome": "latency_pass"}\n'
        '{"vid": "r1-01", "round": 1, "outcome": "accuracy_pass"}\n',
        encoding="utf-8")
    (rd / "probe_results.jsonl").write_text(
        '{"vid": "r1-01", "outcome": "accuracy_pass"}\n', encoding="utf-8")
    (rd / "analysis.md").write_text("# accuracy\n", encoding="utf-8")
    (rd / "direction.json").write_text('{}', encoding="utf-8")
    (art / ".round_advanced").write_text(
        '{"round": 1, "mode": "accuracy"}', encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_probe_emit.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr

    (rd / "probe_results.jsonl").write_text("", encoding="utf-8")
    proc2 = subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_probe_emit.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60)
    assert proc2.returncode == 1
    assert "probe_results.jsonl missing or empty" in proc2.stderr


def test_baseline_chain_binds_training_to_claimed_device(tmp_path: Path):
    """The full training launches only on a ledger-claimed card (vid=baseline
    lock under devices/), the render binds it (--set device), and the
    finalizer's terminal state releases the claim."""
    art = _baseline_ws(
        tmp_path, full_epochs=1, probe_k=1, ckpt_per_epoch=True,
        train_body="echo device=<<device>>\n"
                   "sleep 6\n"
                   "echo 'epoch 1 loss=0.5'\n"
                   "touch '<<out_dir>>/epoch_1.pth'\n")
    proc = _run_baseline_chain(art)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["status"] == "executed"

    # the render bound the training to the ledger-claimed card
    rendered = (art / "baseline" / "train.rendered.sh").read_text(encoding="utf-8")
    assert "device=0" in rendered

    # non-blocking window: while the training runs, the claim is live
    lock = art / "devices" / "0.lock"
    assert lock.is_file()
    assert json.loads(lock.read_text(encoding="utf-8"))["vid"] == "baseline"

    # the finalizer's terminal state releases the claim (and the record)
    assert _wait_for(art / "baseline" / "train_final.json")
    assert _train_final(art)["status"] == "done"
    deadline = time.monotonic() + 15
    while lock.exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    assert not lock.exists()
    assert not (art / "baseline" / ".train_device_idx").exists()


def test_baseline_chain_lock_survives_free_while_training(tmp_path: Path):
    """Ledger intent (v6 §3.2): while the baseline training is alive, a
    `device_alloc.py free` from ANY later consumer must NOT reclaim its
    lock — the claim is adopted by the long-lived finalizer (which owns the
    terminal release), never left bound to the short-lived chain process."""
    art = _baseline_ws(
        tmp_path, full_epochs=2, probe_k=1, ckpt_per_epoch=True,
        train_body="for e in $(seq 1 <<epochs>>); do sleep 6; "
                   'echo "epoch $e loss=0.$e"; done\n'
                   "for e in $(seq 1 <<epochs>>); do "
                   "touch '<<out_dir>>/epoch_'$e'.pth'; done\n")
    proc = _run_baseline_chain(art)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["status"] == "executed"
    lock_path = art / "devices" / "0.lock"
    assert lock_path.is_file()
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    # ownership ladder landed on the finalizer (alive through the terminal
    # release), not on the chain invocation that already exited
    finalizer_pid = int((art / "baseline" / "finalizer.pid")
                        .read_text(encoding="utf-8"))
    assert lock["vid"] == "baseline" and lock["pid"] == finalizer_pid

    # the fixture's stubbed backend CLI reports every GPU idle — the ONLY
    # thing keeping device 0 busy is the adopted, live ledger lock
    stub_dir = art / "stubbin"
    env = dict(os.environ)
    env["PATH"] = str(stub_dir) + os.pathsep + env["PATH"]
    freeout = subprocess.run(
        [sys.executable, str(art / "scripts" / "device_alloc.py"),
         "free", "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60, env=env)
    assert freeout.returncode == 0, freeout.stderr
    doc = json.loads(freeout.stdout)
    assert doc["recycled"] == [] and doc["free"] == [] and doc["locked"] == [0]
    assert lock_path.is_file()   # not reclaimed under a live training


def test_baseline_chain_nonblocking_emit_and_finalizer_products(tmp_path: Path):
    """executed does NOT wait for the training: the chain emits while the
    training pid is still alive and train_final is absent; the detached
    finalizer then delivers the curve (incrementally, one poll cycle at a
    time), both accuracy anchors, and the terminal marker on its own."""
    # 6s per epoch: epoch 1 lands mid-training (before the finalizer's 10s
    # poll boundary), so the INCREMENTAL curve is observable; the whole run
    # (12s) still outlives the chain's emit (~7s)
    art = _baseline_ws(
        tmp_path, full_epochs=2, probe_k=1, ckpt_per_epoch=True,
        train_body="for e in $(seq 1 <<epochs>>); do sleep 6; "
                   'echo "epoch $e loss=0.$e"; done\n'
                   "for e in $(seq 1 <<epochs>>); do "
                   "touch '<<out_dir>>/epoch_'$e'.pth'; done\n")
    proc = _run_baseline_chain(art)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    schema_fields = _po_baseline_schema_fields()
    assert set(payload) == schema_fields
    assert payload["status"] == "executed"
    assert payload["error"] == ""
    artifacts = payload["generated_artifacts"]
    assert any(a.endswith("base/model.onnx") for a in artifacts)
    assert any(a.rstrip("/").endswith("base/profile") for a in artifacts)
    assert any(a.endswith("baseline/business_logic.md") for a in artifacts)

    # NON-BLOCKING PROOF: at emit time the training is still running
    train_pid = int((art / "baseline" / "train.pid").read_text(encoding="utf-8"))
    assert subprocess.run(["kill", "-0", str(train_pid)],
                          capture_output=True).returncode == 0
    assert not (art / "baseline" / "train_final.json").exists()

    # incremental curve: epoch 1 on disk while rc is still absent (the
    # finalizer's mid-training poll wrote it — not the final extract)
    deadline = time.monotonic() + 30
    saw_incremental = False
    while time.monotonic() < deadline:
        curve_file = art / "baseline" / "baseline_metrics.jsonl"
        if curve_file.is_file():
            points = curve_file.read_text(encoding="utf-8").splitlines()
            if points and not (art / "baseline" / "train.rc").exists():
                saw_incremental = True
                assert json.loads(points[0]) == {"epoch": 1, "metric": 0.1}
                break
        time.sleep(0.5)
    assert saw_incremental, "no incremental curve point observed mid-training"

    # the finalizer finishes the baseline on its own
    assert _wait_for(art / "baseline" / "train_final.json", timeout_s=60), \
        (art / "baseline" / "finalizer.log").read_text(encoding="utf-8")[-2000:]
    final = _train_final(art)
    assert final["status"] == "done" and final["rc"] == 0 and final["stage"] == "done"

    # rendered at FULL epochs (fairness: stop-at-k happens downstream, never
    # here) — the fixture template consumes the epoch count via `seq 1 N`
    rendered = (art / "baseline" / "train.rendered.sh").read_text(encoding="utf-8")
    assert "seq 1 2" in rendered

    # accuracy anchors carry the value-level budget fingerprint
    full_acc = json.loads((art / "baseline" / "baseline_full_acc.json").read_text(
        encoding="utf-8"))
    assert full_acc["baseline_full_acc"] == 0.9
    assert full_acc["full_train_budget"] == {"epochs": 2, "seed": 0,
                                             "data": {"dataset_knob": None,
                                                      "data_value": None}}
    k_acc = json.loads((art / "baseline" / "baseline_k_acc.json").read_text(
        encoding="utf-8"))
    assert k_acc["baseline_k_acc"] == 0.9 and k_acc["k"] == 1
    assert k_acc["full_train_budget"] == full_acc["full_train_budget"]

    # finalizer.log: every line starts ISO8601 UTC; heartbeat + stage lines
    lines = (art / "baseline" / "finalizer.log").read_text(
        encoding="utf-8").splitlines()
    assert lines
    iso = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ")
    assert all(iso.match(line) for line in lines)
    assert any("alive curve_points=" in line for line in lines)
    assert any("stage=final_check" in line for line in lines)

    # final curve: the full two-epoch curve (3 lines would mean re-parse drift)
    curve = (art / "baseline" / "baseline_metrics.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert len(curve) == 2
    assert json.loads(curve[-1]) == {"epoch": 2, "metric": 0.2}

    # re-entry: chain re-invocation with a fresh anchor is a clean executed
    proc2 = _run_baseline_chain(art)
    assert proc2.returncode == 0, proc2.stderr
    assert json.loads(proc2.stdout)["status"] == "executed"


def test_baseline_chain_running_until_business_logic_lands(tmp_path: Path):
    """business_logic.md is a HARD precondition of executed: absent -> the
    agent-internal running line; once on disk (and the finalizer terminal),
    a re-invocation emits executed."""
    art = _baseline_ws(tmp_path, full_epochs=1, with_business_logic=False)
    proc = _run_baseline_chain(art)
    payload = json.loads(proc.stdout)
    assert payload["status"] == "running"
    assert "business_logic.md not yet on disk" in payload["error"]

    (art / "baseline" / "business_logic.md").write_text(_BL_MD, encoding="utf-8")
    assert _wait_for(art / "baseline" / "train_final.json", timeout_s=60)
    proc2 = _run_baseline_chain(art)
    assert proc2.returncode == 0, proc2.stderr
    assert json.loads(proc2.stdout)["status"] == "executed"


def test_baseline_chain_rejects_stale_full_acc_fingerprint(tmp_path: Path):
    """verify_anchor_budget paradigm, v4 form: a done train_final whose
    baseline_full_acc.json was recorded under a DIFFERENT full_train_budget
    fails loud (never silently reused), with rebuild guidance."""
    art = _baseline_ws(tmp_path, full_epochs=1)
    _run_baseline_chain(art)   # launch; the finalizer delivers the terminal state
    assert _wait_for(art / "baseline" / "train_final.json", timeout_s=60)
    # rebuild the budget since the anchor was recorded (simulating a
    # contract-stage re-run with a different epoch cap)
    contracts = json.loads((art / "contracts.json").read_text(encoding="utf-8"))
    contracts["full_train_budget"]["epochs"] = 5
    (art / "contracts.json").write_text(json.dumps(contracts), encoding="utf-8")
    proc = _run_baseline_chain(art)
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["status"] == "failed"
    assert payload["error"].startswith("baseline step 6:")
    assert "stale full-training anchor" in payload["error"]
    # never auto-deleted: rebuilding is a deliberate action
    assert (art / "baseline" / "baseline_full_acc.json").is_file()


def test_baseline_chain_final_check_failure_points_at_admission_clause(tmp_path: Path):
    """A training that runs fewer epochs than rendered (the early-stopping
    breach) is a final_check FAILURE whose message points at the admission
    clause — never a silent partial comparison."""
    art = _baseline_ws(
        tmp_path, full_epochs=2, ckpt_per_epoch=False,
        train_body='echo "epoch 1 loss=0.1"\ntouch \'<<out_dir>>/model.pth\'\n')
    _run_baseline_chain(art)   # timing-dependent first line; terminal state below is what counts
    assert _wait_for(art / "baseline" / "train_final.json", timeout_s=60)
    final = _train_final(art)
    assert final["status"] == "failed"
    assert final["stage"] == "final_check"
    assert "训练须按给定轮数精确执行" in final.get("message", "")

    proc2 = _run_baseline_chain(art)
    assert proc2.returncode == 1
    payload = json.loads(proc2.stdout)
    assert payload["status"] == "failed"
    assert "baseline step 6" in payload["error"]
    assert "final_check" in payload["error"]
    assert "训练须按给定轮数精确执行" in payload["error"]
    # no anchors from a failed finalization
    assert not (art / "baseline" / "baseline_full_acc.json").exists()


def test_baseline_chain_worker_failure_recorded_as_train_stage(tmp_path: Path):
    art = _baseline_ws(
        tmp_path, full_epochs=1,
        train_body='echo "epoch 1 loss=0.1"\ntouch \'<<out_dir>>/model.pth\'\nexit 3\n')
    _run_baseline_chain(art)
    assert _wait_for(art / "baseline" / "train_final.json", timeout_s=60)
    final = _train_final(art)
    assert final["status"] == "failed" and final["stage"] == "train"
    assert final["rc"] == 3


def test_baseline_chain_relaunches_crashed_training_with_per_attempt_logs(tmp_path: Path):
    """A training group killed without an rc file (crash scene) is re-launched
    by the finalizer (<= 3 attempts), each attempt logging to its OWN file;
    the final state is still a clean done."""
    art = _baseline_ws(tmp_path, full_epochs=1)
    proc = _run_baseline_chain(art)
    assert proc.returncode == 0, proc.stderr

    # crash the training group behind the finalizer's back (no rc written)
    train_pid = int((art / "baseline" / "train.pid").read_text(encoding="utf-8"))
    subprocess.run(["kill", "-KILL", f"-{train_pid}"], capture_output=True)

    assert _wait_for(art / "baseline" / "train_final.json", timeout_s=60), \
        (art / "baseline" / "finalizer.log").read_text(encoding="utf-8")[-2000:]
    final = _train_final(art)
    assert final["status"] == "done"
    attempts = int((art / "baseline" / ".train_attempts").read_text(
        encoding="utf-8").strip())
    assert attempts == 2                                   # crash + relaunch
    assert (art / "baseline" / "train.attempt1.log").is_file()
    assert (art / "baseline" / "train.attempt2.log").is_file()
    # the curve comes from the CURRENT attempt (attempt 2), re-derived whole
    curve = (art / "baseline" / "baseline_metrics.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert len(curve) == 1 and json.loads(curve[0])["epoch"] == 1


def test_baseline_chain_relaunch_budget_is_three(tmp_path: Path):
    """Beyond 3 crash relaunches the finalizer gives up honestly:
    train_final{failed, stage: relaunch_exhausted} — never an infinite
    relaunch loop, never a fabricated terminal."""
    art = _baseline_ws(
        tmp_path, full_epochs=1,
        train_body='echo "epoch 1 loss=0.1" & sleep 300\n'
                   "touch '<<out_dir>>/model.pth'\n")

    def crash_train():
        pid_file = art / "baseline" / "train.pid"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if pid_file.is_file():
                pid = pid_file.read_text(encoding="utf-8").strip()
                if pid.isdigit() and subprocess.run(
                        ["kill", "-0", pid], capture_output=True).returncode == 0:
                    subprocess.run(["kill", "-KILL", f"-{pid}"],
                                   capture_output=True)
                    return True
            time.sleep(0.3)
        return False

    _run_baseline_chain(art)
    # kill every relaunched attempt as it comes up (1 initial + 3 relaunches)
    for _ in range(4):
        assert crash_train()
        # let the finalizer notice the crash and relaunch (poll cycle ~10s)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            attempts = int((art / "baseline" / ".train_attempts").read_text(
                encoding="utf-8").strip())
            if attempts >= 4 or (art / "baseline" / "train_final.json").is_file():
                break
            time.sleep(0.5)

    assert _wait_for(art / "baseline" / "train_final.json", timeout_s=60), \
        (art / "baseline" / "finalizer.log").read_text(encoding="utf-8")[-2000:]
    final = _train_final(art)
    assert final["status"] == "failed"
    assert final["stage"] == "relaunch_exhausted"
    assert int((art / "baseline" / ".train_attempts").read_text(
        encoding="utf-8").strip()) == 4


def _mfu_baseline_ws(tmp_path: Path) -> tuple[Path, dict]:
    """Baseline workspace in mfu mode: a REAL exported onnx (the chain skips
    its export step), no profiling products yet, and the adapter deployed.
    Returns (art, env)."""
    torch = pytest.importorskip("torch")

    class Tiny(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.fc1 = torch.nn.Linear(32, 32)
            self.act = torch.nn.GELU()
            self.fc2 = torch.nn.Linear(32, 32)

        def forward(self, x):
            return self.fc2(self.act(self.fc1(x)))

    art = _baseline_ws(tmp_path, full_epochs=1)
    (art / "profile_mode.json").write_text(json.dumps(
        {"mode": "mfu", "chip": "6613", "precision": "INT8",
         "core_num": 1, "resolved_by": "env"}), encoding="utf-8")
    shutil.copy(_SCRIPTS / "mfu_adapter.py", art / "scripts" / "mfu_adapter.py")
    model = Tiny().eval()
    torch.onnx.export(model, torch.randn(1, 32), str(art / "base" / "model.onnx"),
                      input_names=["x"], output_names=["out"],
                      opset_version=17, do_constant_folding=True)
    # strip the pre-made early-chain products: profile + analyze must be
    # re-derived through the mfu path
    for rel in ("base/profile/profile_summary.json", "base/bottleneck_report.json"):
        p = art / rel
        if p.is_file():
            p.unlink()
    return art, _chain_env(art)


def test_baseline_chain_mfu_mode_awaits_analyzer_then_adapts(tmp_path: Path):
    """mfu mode handshake: with no raw products the chain WAITS for the
    mfu-analyzer subagent (running line telling the agent to dispatch it —
    never a placeholder run); once the raw products land, the re-invoked
    chain adapts them and proceeds to executed with makespan == the raw
    parallel_cycles."""
    art, env = _mfu_baseline_ws(tmp_path)
    base_cmd = ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5",
                "--seed", "0"]

    first = subprocess.run(base_cmd, capture_output=True, text=True,
                           timeout=60, env=env)
    payload = json.loads(first.stdout)
    assert payload["status"] == "running"
    assert "awaiting mfu-analyzer" in payload["error"]
    # no placeholder fallback happened while waiting
    assert not (art / "base" / "profile" / "profile_summary.json").exists()

    # the mfu-analyzer's products: raw benchmark output + sentinel report
    # (the benchmark CLI spells its knobs --chip/--precision/--core-num)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "mfu_benchmark.py"),
         str(art / "base" / "model.onnx"),
         "--chip", "6613", "--precision", "INT8", "--core-num", "1",
         "-o", str(art / "base" / "profile"), "--timeout", "60"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    (art / "base" / "profile" / "mfu_bottleneck_report.md").write_text(
        "[subagent:mfu-analyzer v1 MBA7K2]\n\n## MFU 时延瓶颈分析报告\n",
        encoding="utf-8")
    parallel = json.loads(proc.stdout)["parallel_cycles"]

    second = subprocess.run(base_cmd, capture_output=True, text=True,
                            timeout=120, env=env)
    payload2 = json.loads(second.stdout)
    assert payload2["status"] == "executed", payload2
    assert any(a.rstrip("/").endswith("base/profile") for a in payload2["generated_artifacts"])
    # analyze.py re-derived the bottleneck report from the ADAPTED four-piece
    report = json.loads((art / "base" / "bottleneck_report.json")
                        .read_text(encoding="utf-8"))
    assert report["makespan_cycles"] == parallel
    assert "profile mode: mfu (chip=6613" in \
        (art / "baseline_status.md").read_text(encoding="utf-8")
    # let the detached finalizer reach its terminal state before tmp cleanup
    assert _wait_for(art / "baseline" / "train_final.json", timeout_s=60)


def test_baseline_chain_mfu_mode_report_without_raw_is_fatal_no_fallback(tmp_path: Path):
    """The analyzer reported (its hard rule: even a failed evaluation writes
    the report) but left no usable raw products — the chain fails loud
    pointing at the report; the placeholder estimator must NOT run."""
    art, env = _mfu_baseline_ws(tmp_path)
    (art / "base" / "profile" / "mfu_bottleneck_report.md").write_text(
        "[subagent:mfu-analyzer v1 MBA7K2]\n\n## MFU 时延瓶颈分析报告\n"
        "评测失败：远程服务不可达\n", encoding="utf-8")

    proc = subprocess.run(
        ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5",
         "--seed", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    payload = json.loads(proc.stdout)
    assert payload["status"] == "failed"
    assert "baseline step 2" in payload["error"]
    assert "no placeholder fallback" in payload["error"]
    assert not (art / "base" / "profile" / "profile_summary.json").exists()


def test_baseline_chain_mfu_adapter_failure_surfaces_in_error(tmp_path: Path):
    """Raw products present but inconsistent: the adapter's fail-loud line
    must travel into the chain's emit error (PROFILE_FAIL_DETAIL), so the
    node's final output names the actual conversion gap on the real machine."""
    art, env = _mfu_baseline_ws(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "mfu_benchmark.py"),
         str(art / "base" / "model.onnx"),
         "--chip", "6613", "--precision", "INT8", "--core-num", "1",
         "-o", str(art / "base" / "profile"), "--timeout", "60"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    raw_tg = next((art / "base" / "profile").glob("*/model_taskgraph.json"))
    doc = json.loads(raw_tg.read_text(encoding="utf-8"))
    del doc["operators"][0]["output_memory"]      # corrupt one raw field
    raw_tg.write_text(json.dumps(doc), encoding="utf-8")

    chain = subprocess.run(
        ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5",
         "--seed", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    payload = json.loads(chain.stdout)
    assert payload["status"] == "failed"
    assert "baseline step 2" in payload["error"]
    assert "mfu_adapter failed" in payload["error"]
    assert "output_memory" in payload["error"]
    assert not (art / "base" / "profile" / "profile_summary.json").exists()


def test_baseline_chain_profile_mode_file_is_the_single_source(tmp_path: Path):
    """The chain no longer takes profiling-mode arguments: the mode (and the
    mfu knobs) come from profile_mode.json. A missing file or an unknown
    mode fails at startup, before any step runs (the enum validation itself
    lives in the entry resolver)."""
    art, env = _mfu_baseline_ws(tmp_path)
    (art / "profile_mode.json").unlink()
    proc = subprocess.run(
        ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5",
         "--seed", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 2
    assert "profile_mode.json missing" in proc.stderr

    (art / "profile_mode.json").write_text(
        json.dumps({"mode": "quantum"}), encoding="utf-8")
    proc2 = subprocess.run(
        ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5",
         "--seed", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    assert proc2.returncode == 2
    assert "placeholder|mfu" in proc2.stderr

    # the retired npu arguments are now a hard usage error
    proc3 = subprocess.run(
        ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5",
         "--seed", "0", "--npu-chip", "6613"],
        capture_output=True, text=True, timeout=60, env=env)
    assert proc3.returncode == 2
    assert "--npu-chip" in proc3.stderr


def test_check_business_logic_gate(tmp_path: Path):
    """The five-section + sentinel gate: the fixture document passes; each
    violation class (missing section, empty section, wrong sentinel) fails."""
    art = tmp_path / "art"
    (art / "baseline").mkdir(parents=True)
    doc = art / "baseline" / "business_logic.md"
    doc.write_text(_BL_MD, encoding="utf-8")
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    sh = _REPO / "workflows" / "prof-opt" / "agents" / "po_baseline" / "scripts" / "check_business_logic.sh"

    def run():
        return subprocess.run(["bash", str(sh)], capture_output=True, text=True,
                              timeout=30, env=env)

    assert run().returncode == 0

    # missing section
    doc.write_text(_BL_MD.replace("## 训练目标与指标方向\nacc higher\n", ""),
                   encoding="utf-8")
    proc = run()
    assert proc.returncode == 1
    assert "训练目标与指标方向" in proc.stderr

    # empty section (bare heading, no body)
    doc.write_text(_BL_MD.replace("per module", ""), encoding="utf-8")
    proc = run()
    assert proc.returncode == 1
    assert "逐模块职责与物理意义" in proc.stderr

    # wrong sentinel (document not authored by the subagent)
    doc.write_text("no sentinel\n" + _BL_MD, encoding="utf-8")
    proc = run()
    assert proc.returncode == 1
    assert "sentinel" in proc.stderr

    # absent document
    doc.unlink()
    proc = run()
    assert proc.returncode == 1
    assert "not found" in proc.stderr


# ── po_flatten reuse gate: fresh_start wipes the whole reusable workspace ─────

_REUSE_SH = _REPO / "workflows" / "prof-opt" / "agents" / "po_flatten" / "scripts" / "reuse_check.sh"


def test_reuse_check_fresh_start_wipes_all_but_run_lock(tmp_path: Path):
    """Regression (E2E round 3): a pinned wipe list left cross-paradigm
    leftovers in the workspace; fresh_start must clear EVERYTHING under the
    artifacts dir except .run_lock (this run's lock, refreshed by the gate)."""
    art = tmp_path / "art"
    art.mkdir()
    # a STALE foreign lock (dead pid + heartbeat far older than the 30-min
    # grace): the gate takes it over and refreshes it as ours
    lock = art / ".run_lock"
    lock.write_text(
        json.dumps({"run_id": "old-run", "pid": 999999, "ts": 0}), encoding="utf-8")
    stale = time.time() - 7200
    os.utime(lock, (stale, stale))
    # resumable state + HIDDEN top-level entries (a glob-style wipe would
    # silently keep them) + a legacy leftover NO pinned list would name
    for rel in ("variants/r1-01", "rounds/001", "baseline/.stamps/step6",
                "shadow/pkg", "scripts", "readiness", "verify"):
        art.joinpath(rel).mkdir(parents=True)
    for rel in ("contracts.json", "history.jsonl", "best.json",
                "project_manifest.md", "BASELINE.lock", ".user_pkg",
                ".round_advanced"):
        (art / rel).write_text("{}", encoding="utf-8")
    (art / "legacy_paradigm_leftover.bin").write_bytes(b"stale")

    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    env["ORCA_RUN_ID"] = "fresh-test-run"
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "", "1"],
        capture_output=True, text=True, timeout=60, env=env)

    assert proc.returncode == 1            # NO_REUSE -> rebuild from scratch
    assert "wiped the whole reusable workspace" in proc.stderr
    remaining = sorted(p.name for p in art.iterdir())
    assert remaining == [".run_lock"]      # everything else is gone
    lock = json.loads((art / ".run_lock").read_text(encoding="utf-8"))
    assert lock["run_id"] == "fresh-test-run"   # refreshed, not stale

    # chain follow-up: with fresh_start=0 the wiped workspace is a plain
    # first-run NO_REUSE (no BASELINE.lock) — the rebuild path resumes cleanly
    proc2 = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    assert proc2.returncode == 1
    assert "no BASELINE.lock (first run)" in proc2.stderr


def test_reuse_check_rejects_fresh_foreign_lock(tmp_path: Path):
    """The complementary invariant of the takeover branch: a foreign lock
    whose heartbeat is YOUNG (another run likely alive) must refuse the
    workspace (exit 3) — an unconditional takeover here would let two runs
    share one workspace."""
    art = tmp_path / "art"
    art.mkdir()
    (art / ".run_lock").write_text(
        json.dumps({"run_id": "other-live-run", "pid": 999999, "ts": 0}),
        encoding="utf-8")   # dead pid, but mtime is NOW -> heartbeat young

    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    env["ORCA_RUN_ID"] = "this-run"
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 3
    assert "owned by another live run" in proc.stderr
    assert json.loads((art / ".run_lock").read_text(encoding="utf-8"))["run_id"] \
        == "other-live-run"   # not taken over, not refreshed


def _write_baseline_lock(art: Path) -> None:
    """A BASELINE.lock whose py_files_sha256 anchors the CURRENT shadow tree
    (what Step 3 of po_flatten writes on a zero-promotion workspace)."""
    import hashlib
    shadow = art / "shadow"
    py = {str(p.relative_to(shadow)).replace("\\", "/"):
          hashlib.sha256(p.read_bytes()).hexdigest()
          for p in sorted(shadow.rglob("*.py"))}
    (art / "BASELINE.lock").write_text(
        json.dumps({"model_path": "model.py", "pretrained_ckpt": "",
                    "ckpt_sha256": "", "py_files_sha256": py}), encoding="utf-8")


def _reusable_ws(tmp_path: Path, *, profile_mode: dict | None = None) -> Path:
    """Minimal workspace whose BASELINE.lock fully matches the shadow tree and
    whose reuse products are complete — the state a healthy zero-promotion
    second run arrives at the gate with (including the recorded profiling
    mode; default = placeholder as resolved on a machine without NPU)."""
    art = tmp_path / "art"
    art.mkdir()
    (art / "shadow" / "pkg").mkdir(parents=True)
    (art / "shadow" / "pkg" / "model.py").write_text("# model v0\n", encoding="utf-8")
    (art / "project_manifest.md").write_text("# manifest\n", encoding="utf-8")
    (art / "readiness").mkdir()
    (art / "readiness" / "readiness.json").write_text(
        json.dumps({"constructible": True, "exportable": True,
                    "pretrained_loadable": True, "definition_located": True}),
        encoding="utf-8")
    (art / "profile_mode.json").write_text(json.dumps(
        profile_mode or {"mode": "placeholder", "chip": "",
                         "precision": None, "core_num": None,
                         "resolved_by": "fallback"}), encoding="utf-8")
    _write_baseline_lock(art)
    return art


def _reuse_env(art: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ORCA_PO_NPU")}
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    env["ORCA_RUN_ID"] = "reuse-test-run"
    env.update(extra)
    return env


def test_reuse_check_matching_lock_reaches_reuse(tmp_path: Path):
    """Regression (E2E round 3, D-B): the lock-verifier heredoc called
    .read_text() on a plain str (sys.argv[1]), so EVERY fresh_start=0 run on a
    workspace with an existing BASELINE.lock crashed exit 3 with a bogus
    'unreadable' verdict — REUSE was unreachable. A fully consistent lock must
    be read (and matched) all the way to the reuse verdict."""
    art = _reusable_ws(tmp_path)
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "", "0"],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 0, proc.stderr
    assert "REUSE" in proc.stdout
    assert "unreadable" not in proc.stderr


def test_reuse_check_mismatch_is_promotion_history_guidance(tmp_path: Path):
    """The readable-but-mismatched state is DESIGN BEHAVIOR on a workspace
    with a promotion history: after a round advanced, the shadow tree moved
    forward while BASELINE.lock still anchors the original baseline. The
    failure copy must say exactly that (cross-run reuse holds only for
    zero-promotion workspaces) and point at fresh_start=true — not a cryptic
    anchor error."""
    art = _reusable_ws(tmp_path)
    # the promoted round replaced the shadow model; the lock still anchors v0
    (art / "shadow" / "pkg" / "model.py").write_text(
        "# model v1 (promoted)\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "", "0"],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 3
    assert "does not match" in proc.stderr
    assert "promotion history" in proc.stderr
    assert "zero promotions" in proc.stderr
    assert "fresh_start=true" in proc.stderr
    assert "unreadable" not in proc.stderr   # states stay distinguishable


def test_reuse_check_corrupt_lock_is_a_real_error(tmp_path: Path):
    """The complementary state: a lock that EXISTS but cannot be parsed is a
    REAL error (corruption / unreadable file) — the copy must say so and must
    NOT dress it up as a reuse mismatch (a fresh_start hint would point away
    from the actual fault)."""
    art = _reusable_ws(tmp_path)
    (art / "BASELINE.lock").write_text("{not json", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "", "0"],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 3
    assert "unreadable/corrupt" in proc.stderr
    assert "real error" in proc.stderr
    assert "fresh_start" not in proc.stderr
    assert "promotion history" not in proc.stderr


@pytest.mark.parametrize("corrupt_body", [
    "[]",                                            # top level not an object
    '{"model_path": "model.py", "pretrained_ckpt": "", "ckpt_sha256": "",'
    ' "py_files_sha256": null}',                     # anchor map not a mapping
])
def test_reuse_check_structurally_corrupt_lock_is_not_a_mismatch(
        tmp_path: Path, corrupt_body: str):
    """Review F1: a PARSABLE but structurally corrupt lock must land in the
    unreadable/corrupt state too — without the type guard it either crashed
    the verifier heredoc ('lock verification crashed', exit 2) or, worse,
    degraded into a char-wise set() comparison and got narrated as promotion
    history (a type-corrupt lock misclassified as design behavior)."""
    art = _reusable_ws(tmp_path)
    (art / "BASELINE.lock").write_text(corrupt_body, encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "", "0"],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 3
    assert "unreadable/corrupt" in proc.stderr
    assert "not a valid anchor object" in proc.stderr
    assert "fresh_start" not in proc.stderr
    assert "promotion history" not in proc.stderr


def test_reuse_check_missing_anchor_map_is_mismatch_not_corrupt(tmp_path: Path):
    """A lock whose py_files_sha256 KEY is absent (vs present-but-wrong-type)
    is deliberately NOT the corrupt state: the comparisons degrade safely and
    the fresh_start rebuild hint is the right recovery — pin that boundary so
    a future guard tightening/loosening cannot drift silently."""
    art = _reusable_ws(tmp_path)
    (art / "BASELINE.lock").write_text(
        json.dumps({"model_path": "model.py", "pretrained_ckpt": "",
                    "ckpt_sha256": ""}), encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "", "0"],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 3
    assert "does not match" in proc.stderr
    assert "fresh_start=true" in proc.stderr
    assert "unreadable/corrupt" not in proc.stderr


def test_reuse_check_arity_rejects_fourth_positional(tmp_path: Path):
    """The npu trio args are RETIRED: a stale 4-arg invocation (the pre-v5
    call form) is a usage error (exit 2), never a silently-ignored extra."""
    art = _reusable_ws(tmp_path)
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "", "0", ""],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 2
    assert "unexpected extra argument" in proc.stderr


def test_reuse_check_mode_consistent_reaches_reuse_untouched(tmp_path: Path):
    """A consistent re-resolution (placeholder == placeholder) lets the reuse
    verdict through AND never touches the recorded profile_mode.json."""
    art = _reusable_ws(tmp_path)          # placeholder recorded, no NPU env
    mode_file = art / "profile_mode.json"
    before = mode_file.read_bytes()
    stamp = mode_file.stat().st_mtime_ns
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "", "0"],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 0, proc.stderr
    assert "REUSE" in proc.stdout
    assert mode_file.read_bytes() == before        # comparison never rewrites
    assert mode_file.stat().st_mtime_ns == stamp


def test_reuse_check_mode_drift_fails_loud(tmp_path: Path):
    """The recorded placeholder no longer matches the re-resolved mfu mode:
    cross-run cycles comparisons are invalid — exit 2 with fresh_start
    guidance (the check sits AFTER the lock match: first-run and
    fresh_start paths can never reach it)."""
    art = _reusable_ws(tmp_path)
    env = _reuse_env(art, ORCA_PO_NPU_CHIP="6613",
                     ORCA_PO_NPU_PRECISION="INT8", ORCA_PO_NPU_CORES="1")
    before = (art / "profile_mode.json").read_bytes()
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 2
    assert "profiling-mode mismatch" in proc.stderr
    assert "fresh_start=true" in proc.stderr
    assert (art / "profile_mode.json").read_bytes() == before  # not overwritten


def test_reuse_check_mode_file_missing_fails_loud(tmp_path: Path):
    """A pre-v5 (or half-built) reusable workspace has no profile_mode.json:
    reuse is refused with the same recovery (the check is contract behavior,
    not a regression)."""
    art = _reusable_ws(tmp_path)
    (art / "profile_mode.json").unlink()
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "", "0"],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 2
    assert "profile_mode.json missing" in proc.stderr
    assert "fresh_start=true" in proc.stderr


def test_reuse_check_resolved_by_flip_is_not_drift(tmp_path: Path):
    """Measurement-equivalent source flip: the mode was recorded via env, the
    re-resolution comes from npu-smi, but the four compared fields are
    identical — `resolved_by` is provenance, never drift; REUSE continues."""
    art = _reusable_ws(tmp_path, profile_mode={
        "mode": "mfu", "chip": "6613", "precision": "INT8", "core_num": 1,
        "resolved_by": "env"})
    npu_dir = tmp_path / "npu-stub"
    npu_dir.mkdir()
    (npu_dir / "npu-smi").write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s' '+------+------+\n"
        "| NPU  Name  | Health |\n"
        "+======+======+\n"
        "| 0     6613 | OK     |\n"
        "+------+------+\n'\n", encoding="utf-8")
    (npu_dir / "npu-smi").chmod(0o755)
    env = _reuse_env(art)   # NO ORCA_PO_NPU_CHIP: resolution goes via npu-smi
    env["PATH"] = f"{npu_dir}:{env.get('PATH', '')}"
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "REUSE" in proc.stdout
    # the recorded provenance is untouched
    assert json.loads((art / "profile_mode.json").read_text(encoding="utf-8")) \
        ["resolved_by"] == "env"


def test_reuse_check_no_lock_first_run_never_hits_mode_check(tmp_path: Path):
    """The consistency check sits after the lock match: a first run (no
    BASELINE.lock, no profile_mode.json) is a plain NO_REUSE — the mode file
    is written later by the fresh path."""
    art = tmp_path / "art"
    art.mkdir()
    (art / "shadow" / "pkg").mkdir(parents=True)
    (art / "shadow" / "pkg" / "model.py").write_text("# m\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "", "0"],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 1
    assert "no BASELINE.lock (first run)" in proc.stderr


# ── deploy_scripts: orphan retirement (defensive, upgrade-safe) ───────────────

_DEPLOY_SH = _SCRIPTS / "deploy_scripts.sh"


def test_deploy_scripts_retires_orphan_scripts(tmp_path: Path):
    """A script retired from the workflow source tree must not linger in the
    workspace scripts/ dir — a stale deployed copy would keep executing after
    an upgrade (fresh_start wipes it, workspace reuse does not)."""
    art = tmp_path / "art"
    (art / "scripts").mkdir(parents=True)
    (art / "orca_inject").mkdir(parents=True)
    (art / "scripts" / "make_variant_ckpt.py").write_text("# retired", encoding="utf-8")
    (art / "scripts" / "legacy_sweep.sh").write_text("# retired", encoding="utf-8")
    # perturb_ckpt.py is a REAL v4 retirement (D-V4-14): a reused v3.5
    # workspace must not keep executing it after the upgrade
    (art / "scripts" / "perturb_ckpt.py").write_text("# retired in v4", encoding="utf-8")
    (art / "scripts" / "notes.txt").write_text("not a script", encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k != "ORCA_PYTHON"}
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    proc = subprocess.run(["bash", str(_DEPLOY_SH)], capture_output=True,
                          text=True, timeout=60, env=env)

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["scripts_dir"] == str(art / "scripts")
    assert payload["orphans_removed"] == 3          # .py and .sh globs swept
    assert not (art / "scripts" / "make_variant_ckpt.py").exists()  # retired
    assert not (art / "scripts" / "legacy_sweep.sh").exists()       # retired
    assert not (art / "scripts" / "perturb_ckpt.py").exists()       # retired in v4
    assert (art / "scripts" / "notes.txt").is_file()                # non-script kept
    # the deployed script set equals the shipped set
    shipped = sorted(p.name for p in _SCRIPTS.glob("*.[ps][yh]"))
    deployed = sorted(p.name for p in (art / "scripts").glob("*.[ps][yh]"))
    assert deployed == shipped

    # steady state: a re-deploy onto a clean workspace retires nothing
    proc2 = subprocess.run(["bash", str(_DEPLOY_SH)], capture_output=True,
                           text=True, timeout=60, env=env)
    assert proc2.returncode == 0, proc2.stderr
    assert json.loads(proc2.stdout)["orphans_removed"] == 0


# ── v4 shared layer: gate_node syntax, metric_curve@k, stop_at_epoch ─────────

def test_gate_node_sh_parses_after_quote_fix():
    """D-V4-15: the --max-rounds argument had a transposed quote/paren
    (`"$MAXR)"` instead of `"$MAXR")"`) — the whole wrapper failed bash -n,
    so every gate decision fell to the hardcoded fallback emitter."""
    proc = subprocess.run(["bash", "-n", str(_SCRIPTS / "gate_node.sh")],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    src = (_SCRIPTS / "gate_node.sh").read_text(encoding="utf-8")
    assert '--max-rounds "$MAXR")"' in src   # paren outside the quotes


def test_metric_curve_compare_pins_depth_and_reports_anchor(tmp_path: Path):
    import metric_curve as mc

    def curve(path: Path, points):
        path.write_text("".join(
            json.dumps({"epoch": e, "metric": m}) + "\n" for e, m in points),
            encoding="utf-8")

    base = tmp_path / "base.jsonl"
    cand = tmp_path / "cand.jsonl"
    curve(base, [(1, 0.5), (2, 0.6), (3, 0.7)])
    curve(cand, [(1, 0.5), (2, 0.55), (3, 0.8)])

    # no --at-epoch: unchanged behavior (latest COMMON epoch) + the new
    # unconditional fields (at_epoch mirrors the depth actually compared,
    # baseline_path records the anchor; the v3.5 `epoch` key STAYS —
    # additive change, existing consumers keep working)
    out = mc.compare(mc.load_curve(base), mc.load_curve(cand),
                     direction="higher_better", budget=0.2,
                     baseline_path=str(base))
    assert out["epoch"] == 3 and out["at_epoch"] == 3
    assert out["epoch"] == out["at_epoch"]
    assert out["baseline_path"] == str(base)
    assert out["pass"] is True          # candidate BETTER at depth 3

    # pinned depth: both curves have MORE points than k=2 — the comparison
    # must happen AT 2, not silently slide to the deeper common epoch
    at2 = mc.compare(mc.load_curve(base), mc.load_curve(cand),
                     direction="higher_better", budget=0.01, at_epoch=2,
                     baseline_path=str(base))
    assert at2["at_epoch"] == 2 and at2["baseline_metric"] == 0.6
    assert at2["normalized_loss"] == 0.6 - 0.55
    assert at2["pass"] is False         # 0.05 > 0.01 budget at depth 2

    # either curve lacking the k-th point fails loud — never a fallback to a
    # shallower (unfair) depth
    shallow = tmp_path / "shallow.jsonl"
    curve(shallow, [(1, 0.5), (2, 0.55)])
    with pytest.raises(mc.MetricCurveError, match="candidate curve lacks epoch 3"):
        mc.compare(mc.load_curve(base), mc.load_curve(shallow),
                   direction="higher_better", budget=0.1, at_epoch=3)
    base_short = tmp_path / "base_short.jsonl"
    curve(base_short, [(1, 0.5)])
    with pytest.raises(mc.MetricCurveError, match="baseline curve lacks epoch 2"):
        mc.compare(mc.load_curve(base_short), mc.load_curve(cand),
                   direction="higher_better", budget=0.1, at_epoch=2)

    # CLI surface: --at-epoch flows through, output keys as the probe asserts
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "metric_curve.py"), "compare",
         "--baseline", str(base), "--candidate", str(cand),
         "--direction", "higher_better", "--budget", "0.01",
         "--at-epoch", "2"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["at_epoch"] == 2 and payload["epoch"] == 2
    assert payload["baseline_path"] == str(base)


@_RETIRED_V6
def test_history_probe_row_optional_eval_fields(tmp_path: Path):
    """D-V4-18: probe rows carry optional eval/monitor annotations — written
    only when passed, rejected when unknown, and NEVER part of the dedup
    config fingerprint (an eval annotation must not reopen a same-config
    probe_insufficient sig)."""
    hist = tmp_path / "history.jsonl"
    row = history_lib.append_probe(
        hist, "r1-01", proxy_acc=0.83, promote_gate="pass", outcome="promoted",
        gap=0.03, eval_skipped_no_epoch_ckpt=True, monitor_failed=False,
        eval_acc=0.9, eval_failed=False)
    assert row["eval_skipped_no_epoch_ckpt"] is True
    assert row["eval_acc"] == 0.9
    stored = history_lib.read_rows(hist)[0]
    assert set(stored) >= set(history_lib.PROBE_FIELDS)
    assert stored["monitor_failed"] is False
    assert stored["gap"] == 0.03

    # omitted optionals stay OUT of the row (old rows coexist harmlessly)
    hist2 = tmp_path / "h2.jsonl"
    history_lib.append_probe(hist2, "r1-01", proxy_acc=0.5,
                             promote_gate="fail", outcome="probe_insufficient")
    assert "eval_acc" not in history_lib.read_rows(hist2)[0]

    # unknown fields still fail loud (closed field set)
    with pytest.raises(TypeError):
        history_lib.append_probe(hist2, "r1-02", proxy_acc=0.5,
                                 promote_gate="fail", outcome="probe_insufficient",
                                 eval_bonus=1)

    # fingerprint unchanged: same probe config + eval annotations -> blocked
    hist3 = tmp_path / "h3.jsonl"
    _write_sig_history(hist3, "act:swap:m", ["probe_insufficient"],
                       probe_max_steps=None, probe_data_value=None)
    history_lib.append_probe(hist3, "r1-01", proxy_acc=0.4,
                             promote_gate="fail", outcome="probe_insufficient",
                             eval_failed=True)
    state = history_lib.dedup_state(hist3, "act:swap:m", 1, None, None)
    assert state["blocked"] is True
    reopened = history_lib.dedup_state(hist3, "act:swap:m", 2, None, None)
    assert reopened["blocked"] is False


# ── stop_at_epoch (D-V4-3): stop-at-k process-group kill ──────────────────────

_STOP_SH = _SCRIPTS / "stop_at_epoch.sh"


def _stop_ws(tmp_path: Path, *, pattern: str | None = None) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    rule = {"kind": "stdout_regex",
            "pattern": pattern or r"epoch (?P<epoch>\d+) loss=(?P<metric>[0-9.]+)"}
    (ws / "contracts.json").write_text(json.dumps(
        {"train": {"epoch_metric_extraction": rule}}), encoding="utf-8")
    return ws


def _write_worker(ws: Path, body: str) -> Path:
    worker = ws / "worker.py"
    worker.write_text(body, encoding="utf-8")
    return worker


# the wrapper form the probe node pins: group leader writes pid/rc and does
# NOT exec (pid/rc each have their own writer); start_new_session = setsid
def _launch_worker(ws: Path, worker: Path, log: Path):
    pid, rc = ws / "pid", ws / "rc"
    return subprocess.Popen(
        ["bash", "-c",
         f'echo $$ > "{pid}"; python3 "{worker}" "{log}"; echo $? > "{rc}"'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


def _kill_group(ws: Path):
    pid_file = ws / "pid"
    if pid_file.is_file():
        pid = pid_file.read_text(encoding="utf-8").strip()
        if pid.isdigit():
            subprocess.run(["kill", "-KILL", f"-{pid}"], capture_output=True)


def _run_stop(ws: Path, log: Path, stop_epoch: int, *extra: str):
    return subprocess.run(
        ["bash", str(_STOP_SH), "--log", str(log),
         "--contract", str(ws / "contracts.json"),
         "--stop-epoch", str(stop_epoch), "--pid-file", str(ws / "pid"),
         *extra],
        capture_output=True, text=True, timeout=60)


def test_stop_at_epoch_kills_group_and_reparses_actual_depth(tmp_path: Path):
    """The core kill protocol: TERM -> graceful handler writes MORE epochs ->
    the frozen-log re-parse reports the ACTUAL trained depth (3), never the
    stop epoch (1) — understating the comparison depth is the false-reject
    bug D-V4-3 exists to prevent."""
    ws = _stop_ws(tmp_path)
    log = ws / "train.log"
    worker = _write_worker(ws, f'''
import signal, sys, time
def w(line):
    with open({str(log)!r}, "a") as fh:
        fh.write(line)
w("epoch 1 loss=0.5\\n")
def on_term(signum, frame):
    w("epoch 2 loss=0.45\\n")
    w("epoch 3 loss=0.40\\n")
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
time.sleep(120)
''')
    _launch_worker(ws, worker, log)
    for _ in range(50):
        if log.is_file() and "epoch 1" in log.read_text(encoding="utf-8"):
            break
        time.sleep(0.1)

    proc = _run_stop(ws, log, 1, "--expect", "worker.py")
    assert proc.returncode == 0, proc.stderr
    status = json.loads(proc.stdout)
    assert status["status"] == "killed"
    assert status["stopped_at_epoch"] == 3    # actual parsed depth, NOT k=1
    assert status["rc"] is None               # killed branch: rc stays null
    on_disk = json.loads((ws / "stop_status.json").read_text(encoding="utf-8"))
    assert on_disk == status

    # idempotent: a second call replays the terminal record verbatim (never
    # kills again — the group is gone, a reused pid must not be signalled)
    proc2 = _run_stop(ws, log, 1)
    assert proc2.returncode == 0, proc2.stderr
    assert json.loads(proc2.stdout) == status


def test_stop_at_epoch_escalates_to_kill_after_grace(tmp_path: Path):
    """A TERM-immune worker survives the 10s grace -> KILL takes the group."""
    ws = _stop_ws(tmp_path)
    log = ws / "train.log"
    worker = _write_worker(ws, f'''
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open({str(log)!r}, "a") as fh:
    fh.write("epoch 1 loss=0.5\\n")
time.sleep(300)
''')
    _launch_worker(ws, worker, log)
    for _ in range(50):
        if log.is_file() and "epoch 1" in log.read_text(encoding="utf-8"):
            break
        time.sleep(0.1)

    started = time.monotonic()
    proc = _run_stop(ws, log, 1, "--expect", "worker.py")
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stderr
    status = json.loads(proc.stdout)
    assert status["status"] == "killed"
    assert status["stopped_at_epoch"] == 1
    assert elapsed >= 10          # the grace window really elapsed
    assert elapsed < 45           # ...but KILL ended it, no unbounded hang


def test_stop_at_epoch_natural_done_records_rc_and_monitor_flag(tmp_path: Path):
    """Worker finished on its own: natural_done + rc; epochs BEYOND k mark
    monitor_failed (the kill never landed) while an exact-k finish does not."""
    ws = _stop_ws(tmp_path)
    log = ws / "train.log"
    worker = _write_worker(ws, f'''
with open({str(log)!r}, "a") as fh:
    fh.write("epoch 1 loss=0.5\\nepoch 2 loss=0.45\\nepoch 3 loss=0.4\\n")
''')
    _launch_worker(ws, worker, log)
    for _ in range(100):
        if (ws / "rc").is_file():
            break
        time.sleep(0.1)

    proc = _run_stop(ws, log, 1)
    assert proc.returncode == 0, proc.stderr
    status = json.loads(proc.stdout)
    assert status["status"] == "natural_done"
    assert status["stopped_at_epoch"] == 3
    assert status["rc"] == 0
    assert status["monitor_failed"] is True   # ran to 3, kill depth was 1

    # exact-depth finish: 1 epoch trained, stop depth 1 -> no monitor flag
    ws2 = _stop_ws(tmp_path / "exact")
    log2 = ws2 / "train.log"
    worker2 = _write_worker(ws2, f'''
with open({str(log2)!r}, "a") as fh:
    fh.write("epoch 1 loss=0.5\\n")
''')
    _launch_worker(ws2, worker2, log2)
    for _ in range(100):
        if (ws2 / "rc").is_file():
            break
        time.sleep(0.1)
    proc2 = _run_stop(ws2, log2, 1)
    status2 = json.loads(proc2.stdout)
    assert status2["status"] == "natural_done"
    assert status2["monitor_failed"] is False


def test_stop_at_epoch_waits_below_depth_and_fails_loud_on_orphans(tmp_path: Path):
    # below the stop depth + group alive -> waiting (caller keeps polling);
    # a not-yet-created log is the same waiting state at epoch 0
    ws = _stop_ws(tmp_path)
    log = ws / "train.log"
    worker = _write_worker(ws, f'''
import time
time.sleep(3)
with open({str(log)!r}, "a") as fh:
    fh.write("epoch 1 loss=0.5\\n")
time.sleep(60)
''')
    _launch_worker(ws, worker, log)
    time.sleep(0.5)
    proc = _run_stop(ws, log, 1)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"status": "waiting", "max_epoch": 0}
    _kill_group(ws)

    # dead group without rc and without stop_status -> fail loud (crash
    # scene, no terminal state to fabricate)
    ws2 = _stop_ws(tmp_path / "crash")
    log2 = ws2 / "train.log"
    log2.write_text("epoch 1 loss=0.5\n", encoding="utf-8")
    dead = subprocess.Popen(["bash", "-c", "exit 0"], start_new_session=True)
    dead.wait()
    (ws2 / "pid").write_text(str(dead.pid), encoding="utf-8")
    proc2 = _run_stop(ws2, log2, 1)
    assert proc2.returncode == 2
    assert "without an rc file" in proc2.stderr


def test_stop_at_epoch_refuses_foreign_pid(tmp_path: Path):
    """/proc cmdline attribution: a pid file naming an unrelated live group
    must NEVER be signalled (pid reuse / wrong pid file)."""
    ws = _stop_ws(tmp_path)
    log = ws / "train.log"
    log.write_text("epoch 1 loss=0.5\n", encoding="utf-8")
    foreign = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        (ws / "pid").write_text(str(foreign.pid), encoding="utf-8")
        proc = _run_stop(ws, log, 1)
        assert proc.returncode == 2
        assert "refusing to kill" in proc.stderr
        assert foreign.poll() is None      # the foreign process survived
    finally:
        foreign.kill()
        foreign.wait()


def test_stop_at_epoch_rejects_pattern_and_shares_metric_curve_surface(tmp_path: Path):
    """E3-06: --pattern is not an argument (single source), and the epoch
    parse shares metric_curve extract's implementation — same depths under
    the same contract, same drift when the pattern changes, same error
    surface when the contract lacks the pattern."""
    # --pattern rejected with a pointed message
    proc = subprocess.run(
        ["bash", str(_STOP_SH), "--log", "x", "--contract", "c.json",
         "--stop-epoch", "1", "--pid-file", "p", "--pattern", "p"],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2
    assert "--pattern is not accepted" in proc.stderr

    log = tmp_path / "train.log"
    log.write_text("epoch 1 loss=0.5\nepoch 2 loss=0.45\nepoch 3 loss=0.4\n"
                   "acc 1 metric=0.9\n", encoding="utf-8")

    def extract_depth(ws: Path) -> int:
        out = ws / "extract.jsonl"
        proc = subprocess.run(
            [sys.executable, str(_SCRIPTS / "metric_curve.py"), "extract",
             "--contract", str(ws / "contracts.json"), "--log", str(log),
             "--out", str(out)],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        rows = [json.loads(line) for line in
                out.read_text(encoding="utf-8").splitlines() if line.strip()]
        return max(r["epoch"] for r in rows)

    def stop_depth(ws: Path) -> int:
        # finished worker (rc written) -> natural_done exposes the parsed depth
        (ws / "rc").write_text("0", encoding="utf-8")
        done = subprocess.Popen(["bash", "-c", "exit 0"], start_new_session=True)
        done.wait()
        (ws / "pid").write_text(str(done.pid), encoding="utf-8")
        proc = _run_stop(ws, log, 1)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)["stopped_at_epoch"]

    ws = _stop_ws(tmp_path / "a")
    assert extract_depth(ws) == 3 and stop_depth(ws) == 3

    # pattern change: both sides switch TOGETHER (the extra 'acc' line only
    # matches the second pattern — the shared parse must agree on 1 vs 3)
    ws2 = _stop_ws(tmp_path / "b", pattern=r"acc (?P<epoch>\d+) metric=(?P<metric>[0-9.]+)")
    assert extract_depth(ws2) == 1 and stop_depth(ws2) == 1

    # contract lacking the pattern: the SAME error surface, both exit 2
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "contracts.json").write_text(json.dumps({"train": {}}), encoding="utf-8")
    proc_stop = _run_stop(bad, log, 1)
    assert proc_stop.returncode == 2
    proc_ext = subprocess.run(
        [sys.executable, str(_SCRIPTS / "metric_curve.py"), "extract",
         "--contract", str(bad / "contracts.json"), "--log", str(log),
         "--out", str(tmp_path / "x.jsonl")],
        capture_output=True, text=True, timeout=60)
    assert proc_ext.returncode == 2
    surface = "lacks train.epoch_metric_extraction.pattern"
    assert surface in proc_stop.stderr and surface in proc_ext.stderr


# ── check_bottleneck (SPEC §5): closed schema + referential subset ───────────

def _bneck_ws(tmp_path: Path) -> Path:
    art = tmp_path / "art"
    (art / "base").mkdir(parents=True)
    (art / "base" / "bottleneck_report.json").write_text(json.dumps({
        "makespan_cycles": 310, "hot_patterns": [
            {"pattern_id": "P1", "op_type": "Erf", "count": 3,
             "total_cycles": 150, "share": 0.5},
            {"pattern_id": "P2", "op_type": "MatMul", "count": 1,
             "total_cycles": 100, "share": 0.34},
            {"pattern_id": "P3", "op_type": "Add", "count": 1,
             "total_cycles": 40, "share": 0.14},
        ]}), encoding="utf-8")
    return art


def _bneck_doc(art: Path, entries: list[dict], **extra) -> Path:
    doc = {"base_report": "base/bottleneck_report.json",
           "summary": "erf chain dominates the critical path",
           "top_bottlenecks": entries}
    doc.update(extra)
    path = art / "base" / "bottleneck_analysis.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _entry(pid: str, op: str, cycles: int, **over) -> dict:
    entry = {"name": pid, "op_type": op, "cycles": cycles,
             "analysis": "gelu-erf chain, swappable for relu"}
    entry.update(over)
    return entry


def test_check_bottleneck_accepts_order_preserving_subset(tmp_path: Path):
    """A SKIPPED selection is legal (subset, not prefix): P1+P3 in base rank
    order, numbers referenced verbatim."""
    from check_bottleneck import check
    art = _bneck_ws(tmp_path)
    path = _bneck_doc(art, [_entry("P1", "Erf", 150), _entry("P3", "Add", 40)])
    result = check(path, art)
    assert result["ok"] is True and result["entries"] == 2

    # CLI surface (the node-side validation call form)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_bottleneck.py"),
         "--artifacts", str(art), "--analysis", str(path)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True


def test_check_bottleneck_rejects_drift_and_fabrication(tmp_path: Path):
    from check_bottleneck import CheckError, check
    art = _bneck_ws(tmp_path)

    # rank-order violation: P3 before P1
    with pytest.raises(CheckError, match="rank order"):
        check(_bneck_doc(art, [_entry("P3", "Add", 40), _entry("P1", "Erf", 150)]), art)
    # fabricated pattern_id
    with pytest.raises(CheckError, match="not a pattern_id"):
        check(_bneck_doc(art, [_entry("P9", "Erf", 150)]), art)
    # referential drift: cycles re-typed by the analyst
    with pytest.raises(CheckError, match="total_cycles"):
        check(_bneck_doc(art, [_entry("P1", "Erf", 999)]), art)
    with pytest.raises(CheckError, match="op_type"):
        check(_bneck_doc(art, [_entry("P1", "Softmax", 150)]), art)
    # cycles column must follow the base rank order (out-of-rank selection
    # is rejected — with a desc-sorted base report this is also what keeps
    # the analysis's cycle column non-increasing)
    with pytest.raises(CheckError, match="rank order"):
        check(_bneck_doc(art, [_entry("P3", "Add", 40), _entry("P2", "MatMul", 100)]), art)
    # closed schema: unknown keys at both levels
    with pytest.raises(CheckError, match="unknown top-level keys"):
        check(_bneck_doc(art, [], fabricated="x"), art)
    with pytest.raises(CheckError, match="unknown keys"):
        check(_bneck_doc(art, [_entry("P1", "Erf", 150, confidence=0.9)]), art)
    # missing base report
    doc_path = _bneck_doc(art, [_entry("P1", "Erf", 150)])
    (art / "base" / "bottleneck_report.json").unlink()
    with pytest.raises(CheckError, match="base_report"):
        check(doc_path, art)


# ── push_curves (D-V4-2b): best-effort live-chart sidecar ─────────────────────

def _push_env(art: Path, sock: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    env["ORCA_NODE"] = "po_baseline"
    env["ORCA_SESSION_ID"] = "s-test"
    if sock is not None:
        env["ORCA_CHART_SOCK"] = str(sock)
    else:
        env.pop("ORCA_CHART_SOCK", None)
    return env


def _push(art: Path, sock: Path | None, *extra: str):
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / "push_curves.py"), *extra],
        capture_output=True, text=True, timeout=60, env=_push_env(art, sock))


def _chart_server(sock_path: Path, replies: int, silent: bool = False):
    """Tiny chart-daemon stub: records received payloads, acks each (or goes
    silent to pin the ack-timeout path). Returns (thread, messages)."""
    import socket
    import threading

    # AF_UNIX: the socket file survives close() — unlink before rebinding
    sock_path.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(4)
    srv.settimeout(30)
    messages: list[dict] = []

    def serve():
        for _ in range(replies):
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                if data:
                    messages.append(json.loads(data))
                if not silent:
                    conn.sendall(b'{"ok": true, "seq": 1}\n')
        srv.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread, messages


def _push_ws(tmp_path: Path) -> Path:
    art = tmp_path / "art"
    (art / "baseline").mkdir(parents=True)
    # variant curve at the PRODUCTION path the probe protocol writes
    # (variants/<vid>/metrics/metrics.jsonl — same as dashboard_snapshot)
    (art / "variants" / "r1-01" / "metrics").mkdir(parents=True)
    (art / "baseline" / "baseline_metrics.jsonl").write_text(
        '{"epoch": 1, "metric": 0.4}\n{"epoch": 2, "metric": 0.5}\n',
        encoding="utf-8")
    (art / "variants" / "r1-01" / "metrics" / "metrics.jsonl").write_text(
        '{"epoch": 1, "metric": 0.45}\n', encoding="utf-8")
    return art


def test_push_curves_missing_sock_is_silent_exit_zero(tmp_path: Path):
    art = _push_ws(tmp_path)
    proc = _push(art, None)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not (art / ".chart_push.log").exists()   # no push -> no audit line


def test_push_curves_pushes_one_chart_and_audits(tmp_path: Path):
    art = _push_ws(tmp_path)
    sock = tmp_path / "chart.sock"
    thread, messages = _chart_server(sock, replies=1)

    proc = _push(art, sock)
    assert proc.returncode == 0, proc.stderr
    thread.join(timeout=10)
    assert len(messages) == 1
    msg = messages[0]
    payload = msg["payload"]
    assert payload["chart_type"] == "line"
    assert payload["x"] == "epoch" and payload["y"] == "metric"
    assert payload["hue"] == "vid"
    vids = {row["vid"] for row in payload["data"]}
    assert vids == {"baseline", "r1-01"}

    audit_lines = (art / ".chart_push.log").read_text(
        encoding="utf-8").splitlines()
    assert len(audit_lines) == 1
    audit = json.loads(audit_lines[0])
    assert audit["baseline_epochs"] == 2
    assert {"vid": "baseline", "epochs": 2} in audit["curves"]
    assert {"vid": "r1-01", "epochs": 1} in audit["curves"]
    assert "ts" in audit

    # idempotent: a second push carries the IDENTICAL title+data (front-end
    # replacement semantics — the curve never duplicates), audit APPENDS
    thread2, messages2 = _chart_server(sock, replies=1)
    proc2 = _push(art, sock)
    assert proc2.returncode == 0, proc2.stderr
    thread2.join(timeout=10)
    assert len(messages2) == 1
    assert messages2[0]["payload"]["data"] == payload["data"]
    assert messages2[0]["payload"]["title"] == payload["title"]
    assert len((art / ".chart_push.log").read_text(
        encoding="utf-8").splitlines()) == 2


def test_push_curves_title_suffix_and_half_written_rows(tmp_path: Path):
    art = _push_ws(tmp_path)
    # half-written tail row (flush mid-write) must be skipped, not crash
    with open(art / "baseline" / "baseline_metrics.jsonl", "a",
              encoding="utf-8") as fh:
        fh.write('{"epoch": 3, "met')
    sock = tmp_path / "chart.sock"
    thread, messages = _chart_server(sock, replies=1)
    proc = _push(art, sock, "--title", "(final)")
    assert proc.returncode == 0, proc.stderr
    thread.join(timeout=10)
    payload = messages[0]["payload"]
    assert payload["title"].endswith("(final)")
    baseline_rows = [r for r in payload["data"] if r["vid"] == "baseline"]
    assert len(baseline_rows) == 2                    # truncated row skipped
    audit = json.loads((art / ".chart_push.log").read_text(
        encoding="utf-8").splitlines()[0])
    assert audit["baseline_epochs"] == 2


def test_push_curves_ack_timeout_never_hangs(tmp_path: Path):
    """A chart daemon that accepts but never acks must be abandoned within
    the 5s hard timeout — the sidecar exits 0, the worker never waits."""
    art = _push_ws(tmp_path)
    sock = tmp_path / "chart.sock"
    thread, _ = _chart_server(sock, replies=1, silent=True)
    started = time.monotonic()
    proc = _push(art, sock)
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stderr       # best-effort: never fatal
    assert elapsed < 30
    assert not (art / ".chart_push.log").exists()  # failed push -> no audit
    assert proc.stderr                             # ...but visible on stderr
    thread.join(timeout=10)


# ── run_latency_recheck (v5): mode-conditioned gate, thresholds retired ──────

_RECHECK_SH = _REPO / "workflows" / "prof-opt" / "agents" / "po_propose" / "scripts" / "run_latency_recheck.sh"

# CALIBRATED expectations (the reference script produced byte-identical
# values on this exact fixture; regenerate alongside any torch/onnx/profiler
# upgrade): base (GELU tiny) = 712 cycles, variant (ReLU tiny) = 568.
# Origin anchor: baseline 712, ratio 0.5 -> target 357; no best.json ->
# latency gate mode, incumbent = 712 (the anchor baseline).
_T8_OP_DELTA = {"Add": -1, "Div": -1, "Erf": -1, "Mul": -2, "Relu": 1}
_T8_PREDICTED = -144
_T8_PASS_VERDICT = {
    "vid": "r1-01", "round": 1, "structural_check": "pass",
    "makespan_cycles": 568, "base_makespan_cycles": 712,
    "incumbent_cycles": 712, "improvement_cycles": 144,
    "gate_mode": "latency", "pred_actual_ratio": 1.0,
    "latency_gate": "pass", "predicted_delta_cycles": -144,
    "outcome": "latency_pass",
}
_T8_MISMATCH_VERDICT = {
    "vid": "r1-02", "round": 1, "structural_check": "fail",
    "mismatch_layers": ["graph: ops Add,Div,Erf,Mul,Relu"],
    "makespan_cycles": None, "base_makespan_cycles": None,
    "incumbent_cycles": None, "improvement_cycles": None,
    "gate_mode": None, "pred_actual_ratio": None, "latency_gate": None,
    "predicted_delta_cycles": -144, "outcome": "structural_mismatch",
}


def _recheck_workspace(tmp_path: Path, *, mode: str = "placeholder",
                       best: dict | None = None,
                       anchor_baseline: int = 712) -> Path:
    """GELU->ReLU variant fixture: one variant whose declaration matches the
    real onnx graphs (latency-pass path) and one declaring an empty op_delta
    (graph-layer structural-mismatch path). The profiling mode comes from
    profile_mode.json; the gate anchors from the origin anchor (+ optional
    best.json)."""
    torch = pytest.importorskip("torch")
    pytest.importorskip("onnx")
    import placeholder_profiler  # noqa: E402

    art = tmp_path / "art"
    (art / "scripts").mkdir(parents=True)
    for src in ("diff_check.py", "history_lib.py", "emit_result.py",
                "round_state.py", "placeholder_profiler.py"):
        shutil.copy(_SCRIPTS / src, art / "scripts" / src)
    (art / "profile_mode.json").write_text(json.dumps(
        {"mode": "placeholder", "chip": "", "precision": None,
         "core_num": None, "resolved_by": "fallback"} if mode == "placeholder"
        else {"mode": "mfu", "chip": "6613", "precision": "INT8",
              "core_num": 1, "resolved_by": "env"}), encoding="utf-8")
    (art / "base" / "profile").mkdir(parents=True)
    (art / "base" / "origin_anchor.json").write_text(json.dumps({
        "baseline_makespan_cycles": anchor_baseline,
        "latency_reduction_min": 0.5, "accuracy_budget": 0.1,
        "target_cycles": int(anchor_baseline * 0.5) + 1,
        "frozen_at_round": 0}), encoding="utf-8")
    if best is not None:
        (art / "best.json").write_text(json.dumps(best), encoding="utf-8")

    class Tiny(torch.nn.Module):
        def __init__(self, act):
            super().__init__()
            self.fc1 = torch.nn.Linear(64, 64)
            self.act = act
            self.fc2 = torch.nn.Linear(64, 64)

        def forward(self, x):
            return self.fc2(self.act(self.fc1(x)))

    def export(model, path):
        model.eval()
        torch.onnx.export(model, torch.randn(1, 64), str(path),
                          input_names=["x"], output_names=["out"],
                          opset_version=17, do_constant_folding=True)

    torch.manual_seed(0)
    (art / "base").mkdir(exist_ok=True)
    export(Tiny(torch.nn.GELU()), art / "base" / "model.onnx")
    base_ms = placeholder_profiler.profile(
        art / "base" / "model.onnx", art / "base" / "profile")["makespan_cycles"]
    # calibration guard: if the pricing ever drifts, the hardcoded verdicts
    # above are void -> fail loud with the recalibration hint
    assert base_ms == anchor_baseline == 712, (
        f"calibration drift: base makespan {base_ms} != 712 — regenerate the "
        "T8 expectations (run the fixture once against the reference script)")

    (art / "shadow" / "pkg").mkdir(parents=True)
    (art / "shadow" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    hist = art / "history.jsonl"
    for seq, vid in enumerate(("r1-01", "r1-02"), 1):
        vdir = art / "variants" / vid
        (vdir / "onnx").mkdir(parents=True)
        torch.manual_seed(0)
        export(Tiny(torch.nn.ReLU()), vdir / "onnx" / "model.onnx")
        shutil.copytree(art / "shadow", vdir / "shadow")
        declared = _T8_OP_DELTA if vid == "r1-01" else {}
        (vdir / "declaration.json").write_text(json.dumps({
            "vid": vid, "edited_files": [], "op_delta": declared,
            "predicted_delta_cycles": _T8_PREDICTED}), encoding="utf-8")
        (vdir / "DONE").write_text("", encoding="utf-8")
        history_lib.append_implemented(
            hist, vid, round=1, seq=seq, parent_vid=None,
            change_sig=f"activation:gelu->relu:{vid}", probe_epochs=1,
            probe_max_steps=None, probe_data_value=None,
            target_modules=["act"], predicted_delta_cycles=_T8_PREDICTED,
            base_at_proposal={"vid": None, "makespan_cycles": 712})
    (art / "rounds" / "001").mkdir(parents=True)
    (art / "contracts.json").write_text(json.dumps(
        {"interpreter": {"sys_executable": sys.executable}}), encoding="utf-8")
    return art


def _run_recheck(art: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    return subprocess.run(
        ["bash", str(_RECHECK_SH),
         "--profiler", str(art / "scripts" / "placeholder_profiler.py")],
        capture_output=True, text=True, timeout=300, env=env)


@_RETIRED_V6
def test_run_latency_recheck_migration_regression(tmp_path: Path):
    """The batch verify semantics on the reference fixture: two-layer
    declaration check, re-profile, STRICT-improvement gate (568 < incumbent
    712 — the v5 judgement has no absolute/relative/ratio thresholds), typed
    history rows, and the calibrated verdict JSONs."""
    art = _recheck_workspace(tmp_path)
    proc = _run_recheck(art)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["status"] == "executed"
    assert out["verdicts_count"] == 2
    assert out["latency_pass_count"] == 1
    assert out["gate_mode"] == "latency"
    assert out["summary"] == "2 verdicts [latency_pass=1 structural_mismatch=1]"
    assert out["verdicts_path"] == str(art / "rounds" / "001" / "verdicts.jsonl")
    assert json.loads((art / "variants" / "r1-01" / "verdict.json")
                      .read_text(encoding="utf-8")) == _T8_PASS_VERDICT
    assert json.loads((art / "variants" / "r1-02" / "verdict.json")
                      .read_text(encoding="utf-8")) == _T8_MISMATCH_VERDICT

    latest = history_lib.read_latest(art / "history.jsonl")
    assert latest["r1-01"]["outcome"] == "latency_pass"
    assert latest["r1-01"]["makespan_cycles"] == 568
    assert latest["r1-01"]["pred_actual_ratio"] == 1.0   # informational only
    assert latest["r1-02"]["outcome"] == "structural_mismatch"
    # verdicts.jsonl is an append-only audit stream of both verdicts
    rows = [json.loads(line) for line in
            (art / "rounds" / "001" / "verdicts.jsonl").read_text(
                encoding="utf-8").splitlines() if line.strip()]
    assert [r["vid"] for r in rows] == ["r1-01", "r1-02"]

    # skip key = verdict.json presence: a re-run over settled verdicts is a no-op
    proc2 = _run_recheck(art)
    assert proc2.returncode == 0, proc2.stderr
    assert json.loads(proc2.stdout)["verdicts_count"] == 0

    # the repair-loop pin: deleting a rejected variant's verdict re-opens it
    # for a FRESH recheck (the node does exactly this before re-verifying a
    # repaired variant)
    (art / "variants" / "r1-02" / "verdict.json").unlink()
    proc3 = _run_recheck(art)
    assert proc3.returncode == 0, proc3.stderr
    out3 = json.loads(proc3.stdout)
    assert out3["verdicts_count"] == 1
    assert json.loads((art / "variants" / "r1-02" / "verdict.json")
                      .read_text(encoding="utf-8")) == _T8_MISMATCH_VERDICT

    # empty profiler input (the workflow default): the script's own
    # placeholder default applies — an omitted --profiler is NOT a hard
    # error and produces the SAME verdicts (regression: the empty-string
    # argument used to die at argparse)
    for vid in ("r1-01", "r1-02"):
        (art / "variants" / vid / "verdict.json").unlink()
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    proc4 = subprocess.run(["bash", str(_RECHECK_SH)],
                           capture_output=True, text=True, timeout=300, env=env)
    assert proc4.returncode == 0, proc4.stderr
    out4 = json.loads(proc4.stdout)
    assert out4["verdicts_count"] == 2 and out4["latency_pass_count"] == 1
    assert json.loads((art / "variants" / "r1-01" / "verdict.json")
                      .read_text(encoding="utf-8")) == _T8_PASS_VERDICT
    assert json.loads((art / "variants" / "r1-02" / "verdict.json")
                      .read_text(encoding="utf-8")) == _T8_MISMATCH_VERDICT


@_RETIRED_V6
def test_run_latency_recheck_small_strict_step_passes(tmp_path: Path):
    """v5 retired the 100-cycle / 1% / ratio thresholds: a variant only ONE
    cycle below the incumbent is a legitimate latency_pass (the pre-v5 gate
    rejected exactly this small-step improvement)."""
    art = _recheck_workspace(tmp_path, mode="mfu", best={
        "vid": "r0-99", "makespan_cycles": 711, "proxy_acc": None,
        "round": 0, "profile_dir": "x"})    # incumbent = best.json = 711
    import placeholder_profiler  # noqa: E402
    vdir = art / "variants" / "r1-01"
    placeholder_profiler.profile(vdir / "onnx" / "model.onnx", vdir / "profile")
    summary_path = vdir / "profile" / "profile_summary.json"
    doc = json.loads(summary_path.read_text(encoding="utf-8"))
    doc["makespan_cycles"] = 710            # exactly ONE cycle better
    summary_path.write_text(json.dumps(doc), encoding="utf-8")
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    proc = subprocess.run(["bash", str(_RECHECK_SH)], capture_output=True,
                          text=True, timeout=300, env=env)
    assert proc.returncode == 0, proc.stderr
    verdict = json.loads((vdir / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["makespan_cycles"] == 710
    assert verdict["incumbent_cycles"] == 711
    assert verdict["outcome"] == "latency_pass"   # strict improvement is enough


@_RETIRED_V6
def test_run_latency_recheck_recovery_mode_uses_target_line(tmp_path: Path):
    """Accuracy (recovery) gate mode: the frozen target line is the filter —
    a variant ABOVE the line is eliminated even though it beats the
    incumbent; the gate mode comes from round_state (best under the line)."""
    art = _recheck_workspace(tmp_path, best={
        "vid": "r0-99", "makespan_cycles": 300, "proxy_acc": 0.4,
        "round": 0, "profile_dir": "x"})   # 300 <= 357 -> accuracy mode
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    proc = subprocess.run(["bash", str(_RECHECK_SH)], capture_output=True,
                          text=True, timeout=300, env=env)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["gate_mode"] == "accuracy"
    verdict = json.loads((art / "variants" / "r1-01" / "verdict.json")
                         .read_text(encoding="utf-8"))
    # 568 > target 357 -> mechanically eliminated in the recovery phase
    assert verdict["gate_mode"] == "accuracy"
    assert verdict["latency_gate"] == "fail"
    assert verdict["outcome"] == "latency_fail"


@_RETIRED_V6
def test_run_latency_recheck_positive_prediction_is_informational(tmp_path: Path):
    """The pre-v5 `predicted_delta_cycles >= 0` hard guard is retired: a
    positive prediction no longer fails the run (the measured number is the
    only judgement); the ratio field degrades to None."""
    art = _recheck_workspace(tmp_path)
    decl = art / "variants" / "r1-02" / "declaration.json"
    doc = json.loads(decl.read_text(encoding="utf-8"))
    doc["op_delta"] = _T8_OP_DELTA        # make it structurally pass
    doc["predicted_delta_cycles"] = 10    # positive prediction
    decl.write_text(json.dumps(doc), encoding="utf-8")
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    proc = subprocess.run(["bash", str(_RECHECK_SH)], capture_output=True,
                          text=True, timeout=300, env=env)
    assert proc.returncode == 0, proc.stderr
    verdict = json.loads((art / "variants" / "r1-02" / "verdict.json")
                         .read_text(encoding="utf-8"))
    assert verdict["outcome"] == "latency_pass"
    assert verdict["pred_actual_ratio"] is None
    assert verdict["predicted_delta_cycles"] == 10


@_RETIRED_V6
def test_run_latency_recheck_reconciles_missing_history_rows(tmp_path: Path):
    """Crash window between the verdict write and the history append: the
    reconciliation pass re-appends the L0 row from the verdict file."""
    art = _recheck_workspace(tmp_path)
    proc = _run_recheck(art)
    assert proc.returncode == 0, proc.stderr

    # strip the L0 fields, simulating the crash-before-append state
    hist = art / "history.jsonl"
    rows = [r for r in history_lib.read_rows(hist)
            if "structural_check" not in r]
    hist.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    proc2 = _run_recheck(art)
    assert proc2.returncode == 0, proc2.stderr
    out2 = json.loads(proc2.stdout)
    assert "reconciled" in out2["summary"]
    latest = history_lib.read_latest(hist)
    assert latest["r1-01"]["outcome"] == "latency_pass"
    assert latest["r1-02"]["outcome"] == "structural_mismatch"


@_RETIRED_V6
def test_run_latency_recheck_mfu_mode_reads_four_piece(tmp_path: Path):
    """mfu mode (from profile_mode.json): the recheck reads the four-piece
    the node produced per variant (mfu-analyzer + mfu_adapter) BEFORE the
    call and never profiles inline — the verdict must carry the four-piece
    makespan verbatim (here 450, not the 568 an inline run would produce),
    so the gate math is attributable to the real evaluation."""
    art = _recheck_workspace(tmp_path, mode="mfu")
    import placeholder_profiler  # noqa: E402

    vdir = art / "variants" / "r1-01"
    placeholder_profiler.profile(vdir / "onnx" / "model.onnx", vdir / "profile")
    summary_path = vdir / "profile" / "profile_summary.json"
    doc = json.loads(summary_path.read_text(encoding="utf-8"))
    doc["makespan_cycles"] = 450     # make the four-piece value unmistakable
    summary_path.write_text(json.dumps(doc), encoding="utf-8")

    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    proc = subprocess.run(["bash", str(_RECHECK_SH)],
                          capture_output=True, text=True, timeout=300, env=env)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["status"] == "executed"
    assert out["verdicts_count"] == 2          # r1-02 still gets its structural verdict
    assert out["latency_pass_count"] == 1
    verdict = json.loads((vdir / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["makespan_cycles"] == 450   # the four-piece number, verbatim
    assert verdict["improvement_cycles"] == 712 - 450
    assert verdict["outcome"] == "latency_pass"
    # the skip key still applies on top of the mfu mode
    proc2 = subprocess.run(["bash", str(_RECHECK_SH)],
                           capture_output=True, text=True, timeout=300, env=env)
    assert json.loads(proc2.stdout)["verdicts_count"] == 0


def test_run_latency_recheck_mfu_fail_loud_matrix(tmp_path: Path):
    """Hard errors, never an inline fallback: in mfu mode a DONE variant
    without a four-piece, and --profiler (mutually exclusive with mfu mode)."""
    art = _recheck_workspace(tmp_path, mode="mfu")
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)

    # variant without the four-piece -> rc 2 naming the variant + the remedy
    proc = subprocess.run(["bash", str(_RECHECK_SH)],
                          capture_output=True, text=True, timeout=300, env=env)
    assert proc.returncode == 2
    assert "mfu mode" in proc.stderr
    assert "r1-01" in proc.stderr
    assert "inline profiling is disabled" in proc.stderr

    # mode conflict -> rc 2
    proc2 = subprocess.run(
        ["bash", str(_RECHECK_SH),
         "--profiler", str(art / "scripts" / "placeholder_profiler.py")],
        capture_output=True, text=True, timeout=300, env=env)
    assert proc2.returncode == 2
    assert "mutually exclusive" in proc2.stderr


def test_run_latency_recheck_mode_file_missing_rc2(tmp_path: Path):
    art = _recheck_workspace(tmp_path)
    (art / "profile_mode.json").unlink()
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    proc = subprocess.run(["bash", str(_RECHECK_SH)],
                          capture_output=True, text=True, timeout=300, env=env)
    assert proc.returncode == 2
    assert "profile_mode.json" in proc.stderr
# ── T12: admission clause single source (E3-07 dual pin) ──────────────────────

def test_admission_clause_single_source():
    """The admission clause's canonical home is po_contract/agent.md;
    check_contracts.sh embeds a constant substring and the yaml description
    carries the one-sentence version. All three must stay in textual sync —
    editing either side alone breaks this pin (never a silent drift)."""
    import re

    sh = (_REPO / "workflows" / "prof-opt" / "agents" / "po_contract" / "scripts"
          / "check_contracts.sh").read_text(encoding="utf-8")
    m = re.search(r'ADMISSION_CLAUSE = "([^"]+)"', sh)
    assert m, "check_contracts.sh lost its ADMISSION_CLAUSE constant"
    clause = m.group(1)

    agent_md = (_REPO / "workflows" / "prof-opt" / "agents" / "po_contract" / "agent.md"
                ).read_text(encoding="utf-8")
    assert clause in agent_md, (
        f"the admission clause {clause!r} drifted: po_contract/agent.md (the "
        "canonical source) no longer contains the gate's constant verbatim")

    yaml_text = (_REPO / "workflows" / "prof-opt" / "workflow.yaml").read_text(encoding="utf-8")
    assert clause in yaml_text, (
        "the workflow description lost its one-sentence admission clause")


# ── T14: eval@k degradation mechanics (D-V4-4 mechanical layer) ───────────────

@_RETIRED_V6
def test_eval_at_k_degradation_mechanics(tmp_path: Path):
    """An eval@k that cannot load degrades to curve-only judgment. The
    mechanical layer this test pins: the history row records the degradation
    WITHOUT a fabricated eval number, and the degraded input face (a
    curve-only pinned-depth compare) still yields a decision. The re-dispatch
    control flow itself (retry once, then degrade) lives in the probe agent
    protocol and is exercised by E2E."""
    import metric_curve as mc

    # degraded row: eval failed to load -> flagged, eval_acc omitted
    hist = tmp_path / "history.jsonl"
    history_lib.append_probe(hist, "r1-01", proxy_acc=0.83,
                             promote_gate="pass", outcome="promoted",
                             eval_failed=True)
    stored = history_lib.read_rows(hist)[0]
    assert stored["eval_failed"] is True
    assert "eval_acc" not in stored       # no fabricated number on degradation
    assert "eval_skipped_no_epoch_ckpt" not in stored  # ckpts EXIST here

    # the addressable-but-skipped counterpart (curve-only by design)
    hist2 = tmp_path / "h2.jsonl"
    history_lib.append_probe(hist2, "r1-02", proxy_acc=0.80,
                             promote_gate="pass", outcome="promoted",
                             eval_skipped_no_epoch_ckpt=True)
    stored2 = history_lib.read_rows(hist2)[0]
    assert stored2["eval_skipped_no_epoch_ckpt"] is True
    assert "eval_failed" not in stored2    # a design skip is not a failure

    # the degraded judgment input face: with NO eval at all, the pinned-depth
    # curve compare alone decides (higher_better, epoch 1, within budget)
    def curve(path: Path, points):
        path.write_text("".join(
            json.dumps({"epoch": e, "metric": m}) + "\n" for e, m in points),
            encoding="utf-8")

    base = tmp_path / "base.jsonl"
    cand = tmp_path / "cand.jsonl"
    curve(base, [(1, 0.85), (2, 0.9)])
    curve(cand, [(1, 0.83)])
    out = mc.compare(mc.load_curve(base), mc.load_curve(cand),
                     direction="higher_better", budget=0.05, at_epoch=1,
                     baseline_path=str(base))
    assert out["pass"] is True            # 0.85 - 0.83 <= 0.05
    assert out["at_epoch"] == 1
    out_fail = mc.compare(mc.load_curve(base), mc.load_curve(cand),
                          direction="higher_better", budget=0.01, at_epoch=1,
                          baseline_path=str(base))
    assert out_fail["pass"] is False      # same face, honest fail


# ── verdict_decide (v5): anchor-budget promote / final-budget gates ──────────
# v6 retired `promote` (probe k-depth gate) and moved final-budget to
# variants/<vid>/eval/final_acc.json — the v5 cases below are skipped in
# place (see _RETIRED_V6); v6 coverage lives in test_po_v6.py.

from verdict_decide import final_budget  # noqa: E402

_VERDICT_SH = _SCRIPTS / "verdict_decide.py"


def _probe_ws(tmp_path: Path, *, compare: dict, proxy: dict | None,
              k_acc: dict | None, direction: str = "higher_better",
              budget: float = 0.05) -> Path:
    art = tmp_path / "ws"
    (art / "variants" / "r1-01" / "metrics").mkdir(parents=True)
    (art / "variants" / "r1-01" / "metrics" / "epoch_compare.json").write_text(
        json.dumps(compare), encoding="utf-8")
    if proxy is not None:
        (art / "variants" / "r1-01" / "eval").mkdir(parents=True)
        (art / "variants" / "r1-01" / "eval" / "proxy.json").write_text(
            json.dumps(proxy), encoding="utf-8")
    if k_acc is not None:
        (art / "baseline").mkdir(parents=True)
        (art / "baseline" / "baseline_k_acc.json").write_text(
            json.dumps(k_acc), encoding="utf-8")
    (art / "base").mkdir(parents=True)
    (art / "base" / "origin_anchor.json").write_text(json.dumps({
        "baseline_makespan_cycles": 1000, "latency_reduction_min": 0.5,
        "accuracy_budget": budget, "target_cycles": 501,
        "frozen_at_round": 0}), encoding="utf-8")
    (art / "contracts.json").write_text(json.dumps(
        {"eval": {"metric_direction": direction}}), encoding="utf-8")
    return art


_PASS_COMPARE = {"at_epoch": 1, "baseline_metric": 0.85,
                 "candidate_metric": 0.84, "normalized_loss": 0.01,
                 "budget": 0.05, "metric_direction": "higher_better",
                 "pass": True}


@_RETIRED_V6
def test_verdict_promote_dual_gate_pass(tmp_path: Path):
    """Both gates green -> accuracy_pass with gap = the worst gate gap; the
    line is recomputed from the anchor RECORDED in epoch_compare.json."""
    art = _probe_ws(tmp_path, compare=dict(_PASS_COMPARE),
                    proxy={"vid": "r1-01", "metric_value": 0.84, "k": 1},
                    k_acc={"baseline_k_acc": 0.86, "k": 1})
    out = promote(art, "r1-01")
    assert out["curve_pass"] is True
    assert out["eval_acc"] == 0.84
    assert out["eval_pass"] is True          # 0.84 >= 0.86 - 0.05
    assert out["line"] == pytest.approx(0.80)  # 0.85 - 0.05
    assert out["accuracy_pass"] is True
    assert out["gap"] == pytest.approx(max(0.01, 0.86 - 0.84))  # worst gate


@_RETIRED_V6
def test_verdict_promote_eval_gate_blocks_with_eval_gap(tmp_path: Path):
    """Curve passes, eval misses: accuracy_pass=false and gap = the EVAL
    gap (the worst gate), not the curve gap."""
    art = _probe_ws(tmp_path,
                    compare=dict(_PASS_COMPARE, normalized_loss=0.01),
                    proxy={"vid": "r1-01", "metric_value": 0.70, "k": 1},
                    k_acc={"baseline_k_acc": 0.86, "k": 1})
    out = promote(art, "r1-01")
    assert out["curve_pass"] is True
    assert out["eval_pass"] is False           # 0.70 < 0.86-0.05
    assert out["accuracy_pass"] is False
    assert out["gap"] == pytest.approx(0.86 - 0.70)   # the eval gap dominates


@_RETIRED_V6
def test_verdict_promote_curve_only_gap_is_curve_gap(tmp_path: Path):
    """No proxy.json and no baseline_k_acc.json -> curve-only judgment: the
    gap IS the curve's normalized_loss, and pass <=> gap <= budget."""
    art = _probe_ws(tmp_path,
                    compare=dict(_PASS_COMPARE, normalized_loss=0.04),
                    proxy=None, k_acc=None)
    out = promote(art, "r1-01")
    assert out["eval_acc"] is None
    assert out["eval_pass"] is True
    assert out["accuracy_pass"] is True
    assert out["gap"] == pytest.approx(0.04)

    art2 = _probe_ws(tmp_path / "b",
                     compare=dict(_PASS_COMPARE, normalized_loss=0.06,
                                  **{"pass": False}),
                     proxy=None, k_acc=None)
    out2 = promote(art2, "r1-01")
    assert out2["accuracy_pass"] is False
    assert out2["gap"] == pytest.approx(0.06)   # 0.06 > 0.05 budget


@_RETIRED_V6
def test_verdict_promote_asymmetric_single_gate_branches(tmp_path: Path):
    """Either eval-side file alone absent -> still curve-only judgment (the
    gate needs BOTH numbers to apply); a present eval number is still
    echoed, never fabricated away."""
    art = _probe_ws(tmp_path, compare=dict(_PASS_COMPARE),
                    proxy={"vid": "r1-01", "metric_value": 0.84, "k": 1},
                    k_acc=None)
    out = promote(art, "r1-01")
    assert out["eval_acc"] == 0.84
    assert out["eval_pass"] is True
    assert out["accuracy_pass"] is True

    art2 = _probe_ws(tmp_path / "b", compare=dict(_PASS_COMPARE),
                     proxy=None, k_acc={"baseline_k_acc": 0.86, "k": 1})
    out2 = promote(art2, "r1-01")
    assert out2["eval_acc"] is None
    assert out2["eval_pass"] is True
    assert out2["accuracy_pass"] is True


@pytest.mark.parametrize("proxy_text,error_kw", [
    ('{"vid": "r1-01", "metric_value": "oops", "k": 1}', "metric_value"),
    ("{not json", "proxy.json"),
])
@_RETIRED_V6
def test_verdict_promote_fails_loud_on_present_but_malformed_eval(
        tmp_path: Path, proxy_text: str, error_kw: str):
    """A present-but-unreadable eval anchor FAILS — it must never silently
    downgrade the judgment to curve-only (the _optional_number contract)."""
    art = _probe_ws(tmp_path, compare=dict(_PASS_COMPARE),
                    proxy=None, k_acc=None)
    (art / "variants" / "r1-01" / "eval").mkdir(parents=True)
    (art / "variants" / "r1-01" / "eval" / "proxy.json").write_text(
        proxy_text, encoding="utf-8")
    with pytest.raises(ValueError, match=error_kw):
        promote(art, "r1-01")


@_RETIRED_V6
def test_verdict_promote_lower_better_line_direction(tmp_path: Path):
    compare = {"at_epoch": 2, "baseline_metric": 0.20,
               "candidate_metric": 0.23, "normalized_loss": 0.03,
               "budget": 0.05, "metric_direction": "lower_better",
               "pass": True}
    art = _probe_ws(tmp_path, compare=compare,
                    proxy={"vid": "r1-01", "metric_value": 0.23, "k": 2},
                    k_acc={"baseline_k_acc": 0.21, "k": 2},
                    direction="lower_better")
    out = promote(art, "r1-01")
    assert out["line"] == pytest.approx(0.25)   # b + slack for lower_better
    assert out["eval_pass"] is True             # 0.23 <= 0.21 + 0.05
    assert out["accuracy_pass"] is True
    assert out["gap"] == pytest.approx(max(0.03, 0.23 - 0.21))


@pytest.mark.parametrize("compare,error_kw", [
    ({"baseline_metric": 0.85, "normalized_loss": 0.01}, "pass"),
    ({"pass": "true", "baseline_metric": 0.85, "normalized_loss": 0.01}, "pass"),
    ({"pass": True, "normalized_loss": 0.01}, "baseline_metric"),
    ({"pass": True, "baseline_metric": 0.85}, "normalized_loss"),
])
@_RETIRED_V6
def test_verdict_promote_fails_loud_on_malformed_compare(tmp_path: Path, compare,
                                                         error_kw):
    art = _probe_ws(tmp_path, compare=compare, proxy=None, k_acc=None)
    with pytest.raises(ValueError, match=error_kw):
        promote(art, "r1-01")


@_RETIRED_V6
def test_verdict_promote_missing_anchor_rc2(tmp_path: Path):
    art = _probe_ws(tmp_path, compare=dict(_PASS_COMPARE), proxy=None,
                    k_acc=None)
    (art / "base" / "origin_anchor.json").unlink()
    with pytest.raises(FileNotFoundError, match="origin_anchor"):
        promote(art, "r1-01")


@_RETIRED_V6
def test_verdict_cli_rejects_budget_and_reads_anchor(tmp_path: Path):
    """The --budget flag is RETIRED on both subcommands (argparse rejects);
    the budget comes from the origin anchor only."""
    art = _probe_ws(tmp_path, compare=dict(_PASS_COMPARE), proxy=None,
                    k_acc=None)
    for sub in (["promote", "--vid", "r1-01"], ["final-budget"]):
        proc = subprocess.run(
            [sys.executable, str(_VERDICT_SH), *sub,
             "--artifacts", str(art), "--budget", "0.05"],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode != 0
        assert "--budget" in proc.stderr

    ok = subprocess.run(
        [sys.executable, str(_VERDICT_SH), "promote",
         "--artifacts", str(art), "--vid", "r1-01"],
        capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0, ok.stderr
    payload = json.loads(ok.stdout)
    assert payload["accuracy_pass"] is True and payload["gap"] == 0.01


@_RETIRED_V6
def test_verdict_final_budget_reads_anchor_both_directions(tmp_path: Path):
    art = tmp_path / "final-ws"
    (art / "final").mkdir(parents=True)
    (art / "base").mkdir(parents=True)
    (art / "base" / "origin_anchor.json").write_text(json.dumps({
        "baseline_makespan_cycles": 1000, "latency_reduction_min": 0.5,
        "accuracy_budget": 0.05, "target_cycles": 501,
        "frozen_at_round": 0}), encoding="utf-8")
    (art / "final" / "final_acc.json").write_text(json.dumps(
        {"vid": "r1-01", "final_acc": 0.90, "baseline_full_acc": 0.92,
         "metric_direction": "higher_better", "within_budget": None}),
        encoding="utf-8")
    assert final_budget(art) == {"within_budget": True}   # 0.9 >= 0.92-0.05

    (art / "final" / "final_acc.json").write_text(json.dumps(
        {"vid": "r1-01", "final_acc": 0.90, "baseline_full_acc": 0.92,
         "metric_direction": "higher_better", "within_budget": None},
    ), encoding="utf-8")
    (art / "base" / "origin_anchor.json").write_text(json.dumps({
        "baseline_makespan_cycles": 1000, "latency_reduction_min": 0.5,
        "accuracy_budget": 0.01, "target_cycles": 501,
        "frozen_at_round": 0}), encoding="utf-8")
    assert final_budget(art) == {"within_budget": False}  # 0.9 < 0.92-0.01

    (art / "final" / "final_acc.json").write_text(json.dumps(
        {"vid": "r1-01", "final_acc": 2.1, "baseline_full_acc": 2.0,
         "metric_direction": "lower_better", "within_budget": None}),
        encoding="utf-8")
    (art / "base" / "origin_anchor.json").write_text(json.dumps({
        "baseline_makespan_cycles": 1000, "latency_reduction_min": 0.5,
        "accuracy_budget": 0.05, "target_cycles": 501,
        "frozen_at_round": 0}), encoding="utf-8")
    assert final_budget(art) == {"within_budget": False}  # 2.1 > 2.0+0.05


@_RETIRED_V6
def test_verdict_final_budget_cli_fails_loud_on_bad_inputs(tmp_path: Path):
    """The final verdict's inputs are hand-assembled by the full-train agent
    — a hyphen slip in the direction or a non-numeric metric is exactly the
    transcription error class the script exists to catch (exit 2, never a
    guessed verdict)."""
    art = tmp_path / "fw"
    (art / "final").mkdir(parents=True)
    (art / "base").mkdir(parents=True)
    (art / "base" / "origin_anchor.json").write_text(json.dumps({
        "baseline_makespan_cycles": 1000, "latency_reduction_min": 0.5,
        "accuracy_budget": 0.05, "target_cycles": 501,
        "frozen_at_round": 0}), encoding="utf-8")
    final = art / "final" / "final_acc.json"
    base = {"vid": "r1-01", "final_acc": 0.90, "baseline_full_acc": 0.92,
            "metric_direction": "higher_better", "within_budget": None}

    def run_cli():
        return subprocess.run(
            [sys.executable, str(_VERDICT_SH), "final-budget",
             "--artifacts", str(art)],
            capture_output=True, text=True, timeout=60)

    final.write_text(json.dumps(dict(base, metric_direction="higher-better")),
                     encoding="utf-8")
    proc = run_cli()
    assert proc.returncode == 2
    assert "metric_direction" in proc.stderr

    final.write_text(json.dumps(dict(base, final_acc="n/a")), encoding="utf-8")
    proc = run_cli()
    assert proc.returncode == 2
    assert "final_acc" in proc.stderr

    final.write_text(json.dumps(dict(base, baseline_full_acc=None)),
                     encoding="utf-8")
    proc = run_cli()
    assert proc.returncode == 2
    assert "baseline_full_acc" in proc.stderr

    final.unlink()
    proc = run_cli()
    assert proc.returncode == 2
    assert "final_acc.json" in proc.stderr


# ── extract_user_pkg (cleanliness round): fail-loud path resolution ───────────

_EXTRACT_SH = (_REPO / "workflows" / "prof-opt" / "agents" / "po_flatten" / "scripts"
               / "extract_user_pkg.sh")

_ENTRY_BODY = "import os\nimport json\nfrom mymodel import layers\n"


def _run_extract(artifacts: Path, project_root: Path, model_path: str):
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(artifacts)
    return subprocess.run(["bash", str(_EXTRACT_SH), str(project_root),
                           model_path],
                          capture_output=True, text=True, timeout=60, env=env)


def test_extract_user_pkg_resolves_relative_model_path(tmp_path: Path):
    """model_path is resolved AGAINST the project root by the script (the
    caller never concatenates); stdlib names are filtered, user-owned
    (non-importable) names land in .user_pkg."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "model.py").write_text(_ENTRY_BODY, encoding="utf-8")
    art = tmp_path / "art"
    art.mkdir()
    proc = _run_extract(art, proj, "model.py")
    assert proc.returncode == 0, proc.stderr
    assert (art / ".user_pkg").read_text(encoding="utf-8") == "mymodel\n"


def test_extract_user_pkg_accepts_absolute_model_path(tmp_path: Path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    entry = elsewhere / "model.py"
    entry.write_text(_ENTRY_BODY, encoding="utf-8")
    art = tmp_path / "art"
    art.mkdir()
    proc = _run_extract(art, tmp_path / "proj-root-unused", str(entry))
    assert proc.returncode == 0, proc.stderr
    assert (art / ".user_pkg").read_text(encoding="utf-8") == "mymodel\n"


def test_extract_user_pkg_fails_loud_on_missing_entry(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    art = tmp_path / "art"
    art.mkdir()
    proc = _run_extract(art, proj, "gone.py")
    assert proc.returncode == 2
    assert "model entry not found" in proc.stderr


def test_extract_user_pkg_empty_marker_on_zero_imports(tmp_path: Path):
    """Zero import lines is the ONE legitimate empty-marker case: WARN on
    stderr (disclosed, not silent), marker still written, exit 0."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "model.py").write_text("# no imports here\n", encoding="utf-8")
    art = tmp_path / "art"
    art.mkdir()
    proc = _run_extract(art, proj, "model.py")
    assert proc.returncode == 0, proc.stderr
    assert "WARN: no import lines" in proc.stderr
    assert (art / ".user_pkg").read_text(encoding="utf-8") == ""


# ── po_propose check_prerequisites (cleanliness round) ────────────────────────

_PREREQ_SH = (_REPO / "workflows" / "prof-opt" / "agents" / "po_propose" / "scripts"
              / "check_prerequisites.sh")
_PREREQ_FILES = ("analyze.py", "predict_delta.py", "history_lib.py",
                 "experiment_ledger.py", "emit_result.py", "check_bottleneck.py",
                 "mfu_adapter.py", "mfu_benchmark.py", "round_state.py",
                 "resolve_profile_mode.sh", "rules_pool.py")


def _run_prereq(ws: Path):
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(ws)
    return subprocess.run(["bash", str(_PREREQ_SH)],
                          capture_output=True, text=True, timeout=60, env=env)


def test_check_prerequisites_passes_on_deployed_workspace(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "scripts").mkdir(parents=True)
    for name in _PREREQ_FILES:
        (ws / "scripts" / name).write_text("# deployed\n", encoding="utf-8")
    proc = _run_prereq(ws)
    assert proc.returncode == 0, proc.stderr
    assert "prerequisites: ok" in proc.stderr


def test_check_prerequisites_fails_loud_when_entry_stage_incomplete(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "scripts").mkdir(parents=True)
    for name in _PREREQ_FILES[1:]:
        (ws / "scripts" / name).write_text("# deployed\n", encoding="utf-8")
    proc = _run_prereq(ws)                    # analyze.py missing
    assert proc.returncode == 2
    assert "analyze.py not deployed" in proc.stderr


def test_check_prerequisites_fails_loud_without_artifacts_env():
    env = {k: v for k, v in os.environ.items() if k != "ORCA_ARTIFACTS_DIR"}
    proc = subprocess.run(["bash", str(_PREREQ_SH)], capture_output=True,
                          text=True, timeout=60, env=env)
    assert proc.returncode != 0
    assert "ORCA_ARTIFACTS_DIR not set" in proc.stderr

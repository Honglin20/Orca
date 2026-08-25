"""test_po_scripts.py — unit tests for the prof-opt shared deterministic scripts.

Covers: history_lib (builder field sets + R3 dedup branches + joint retry
budget), gate_decide (all four decisions + hard cap + stall reset),
advance_round (r1->r2 base/shadow double replacement + round-number
idempotency key), analyze (fixture-driven hot patterns / pipeline breakdown /
cost table + strict unknown-key failure), predict_delta (per-shape-class row
pricing incl. the small-site E2E regression + params normalization
idempotency), the placeholder profiler's delta-direction
guarantee (minimal GELU vs ReLU export), render_run (<<k>> token chain:
--set substitution incl. special-char values, builtin/header tokens, fail-loud
on unreplaced tokens and non-identifier --set keys), the check_contracts
gate (fairness-invariant token/budget enforcement incl. the knob/value
symmetric pair), run_baseline_chain's step6 stale-anchor proxy-budget
re-verification + schema-shaped stdout line (verbatim-forwardable), the
po_flatten reuse gate's fresh_start whole-workspace wipe, and deploy_scripts'
orphan-script retirement.
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
_SCRIPTS = _REPO / "workflows" / "agents" / "_po_scripts"
sys.path.insert(0, str(_SCRIPTS))

import history_lib  # noqa: E402
from analyze import ContractError, analyze  # noqa: E402
from gate_decide import decide  # noqa: E402
from advance_round import advance  # noqa: E402
from predict_delta import predict_delta  # noqa: E402


# ── history_lib ───────────────────────────────────────────────────────────────

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
        promote_gate="pass", outcome="promoted")

    rows = history_lib.read_rows(hist)
    assert len(rows) == 3
    impl, lat, probe = rows
    assert set(impl) >= set(history_lib.IMPL_FIELDS) | {"version", "ts"}
    assert set(lat) >= set(history_lib.LATENCY_FIELDS) | set(history_lib.IMPL_FIELDS)
    assert set(probe) >= set(history_lib.PROBE_FIELDS) | set(history_lib.LATENCY_FIELDS)
    assert [r["version"] for r in rows] == [1, 2, 3]

    latest = history_lib.read_latest(hist)
    assert latest["r1-01"]["outcome"] == "promoted"
    assert latest["r1-01"]["makespan_cycles"] == 900  # merged snapshot carries L0 fields


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
            history_lib.append_latency(hist, vid, structural_check="pass",
                                       makespan_cycles=100, latency_gate="pass",
                                       pred_actual_ratio=1.0, outcome="latency_pass")
            history_lib.append_probe(hist, vid, proxy_acc=0.9,
                                     promote_gate="pass", outcome="promoted")
        else:  # structural_mismatch / variant_broken / unsupported_op
            if outcome in ("structural_mismatch", "variant_broken"):
                history_lib.append_outcome(hist, vid, outcome)
            else:
                history_lib.append_latency(hist, vid, structural_check="fail",
                                           makespan_cycles=None, latency_gate=None,
                                           pred_actual_ratio=None, outcome=outcome)


@pytest.mark.parametrize("outcomes,blocked", [
    (["promoted"], True),                      # permanent: real validated winner
    (["unsupported_op"], True),                # permanent: structurally infeasible
    (["latency_pass"], False),                 # process state never blocks
    (["structural_mismatch"], False),          # joint budget allows one retry
    (["variant_broken"], False),               # the other class, same budget
    (["structural_mismatch", "variant_broken"], True),   # joint budget exhausted
    (["variant_broken", "variant_broken"], True),        # same class twice: exhausted
])
def test_history_dedup_branches(tmp_path: Path, outcomes, blocked):
    hist = tmp_path / "history.jsonl"
    _write_sig_history(hist, "activation:x->y", outcomes)
    state = history_lib.dedup_state(hist, "activation:x->y", 1, 500)
    assert state["blocked"] is blocked, state


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


# ── gate_decide ───────────────────────────────────────────────────────────────

def _gate_artifacts(tmp_path: Path, *, rounds: dict[int, bool],
                    history_rows: list[dict], best: dict | None) -> Path:
    art = tmp_path / "gate-artifacts"
    for rnd, exhausted in rounds.items():
        d = art / "rounds" / f"{rnd:03d}"
        d.mkdir(parents=True)
        (d / "proposals.json").write_text(
            json.dumps({"round": rnd, "exhausted": exhausted, "proposals": []}),
            encoding="utf-8")
    hist = art / "history.jsonl"
    with open(hist, "w", encoding="utf-8") as fh:
        for row in history_rows:
            fh.write(json.dumps(row) + "\n")
    if best is not None:
        art.mkdir(parents=True, exist_ok=True)
        (art / "best.json").write_text(json.dumps(best), encoding="utf-8")
    return art


_PROMOTED_R1 = [
    {"vid": "r1-01", "round": 1, "outcome": "promoted",
     "makespan_cycles": 100, "proxy_acc": 0.9},
]
_NO_PROMO_R1 = [
    {"vid": "r1-01", "round": 1, "outcome": "probe_insufficient"},
]


def test_gate_full_train_on_target_met(tmp_path: Path):
    art = _gate_artifacts(tmp_path, rounds={1: False}, history_rows=_PROMOTED_R1,
                          best={"vid": "r1-01", "makespan_cycles": 100, "proxy_acc": 0.9})
    out = decide(art, target_makespan=100, max_rounds=5, stall_rounds=2)
    assert out["decision"] == "full-train"
    assert out["best"]["vid"] == "r1-01"


def test_gate_loops_when_budget_remains(tmp_path: Path):
    art = _gate_artifacts(tmp_path, rounds={1: False}, history_rows=_NO_PROMO_R1,
                          best=None)
    out = decide(art, target_makespan=50, max_rounds=5, stall_rounds=2)
    assert out["decision"] == "loop"
    assert out["stall"] == 1
    assert out["best"] is None


def test_gate_best_effort_when_exhausted_with_best(tmp_path: Path):
    art = _gate_artifacts(tmp_path, rounds={1: True}, history_rows=_PROMOTED_R1,
                          best={"vid": "r1-01", "makespan_cycles": 100, "proxy_acc": 0.9})
    out = decide(art, target_makespan=50, max_rounds=5, stall_rounds=2)
    assert out["decision"] == "full-train-best-effort"


def test_gate_finish_failed_when_no_promoted_anywhere(tmp_path: Path):
    art = _gate_artifacts(tmp_path, rounds={1: True}, history_rows=_NO_PROMO_R1,
                          best=None)
    out = decide(art, target_makespan=50, max_rounds=5, stall_rounds=2)
    assert out["decision"] == "finish-failed"


def test_gate_hard_cap_with_best_is_best_effort(tmp_path: Path):
    art = _gate_artifacts(tmp_path, rounds={1: False, 2: False},
                          history_rows=_PROMOTED_R1 + [
                              {"vid": "r2-01", "round": 2, "outcome": "probe_insufficient"}],
                          best={"vid": "r1-01", "makespan_cycles": 100, "proxy_acc": 0.9})
    out = decide(art, target_makespan=50, max_rounds=2, stall_rounds=5)
    assert out["decision"] == "full-train-best-effort"  # cap + best present


def test_gate_target_met_wins_over_everything(tmp_path: Path):
    """full-train is judged FIRST: even at the round cap, a target-meeting
    best is a clean full-train, not best-effort."""
    art = _gate_artifacts(tmp_path, rounds={1: False, 2: False},
                          history_rows=_PROMOTED_R1,
                          best={"vid": "r1-01", "makespan_cycles": 100, "proxy_acc": 0.9})
    out = decide(art, target_makespan=100, max_rounds=2, stall_rounds=1)
    assert out["decision"] == "full-train"


def test_gate_hard_cap_never_loops_at_max_rounds(tmp_path: Path):
    art = _gate_artifacts(tmp_path, rounds={1: False, 2: False},
                          history_rows=_NO_PROMO_R1 + [
                              {"vid": "r2-01", "round": 2, "outcome": "probe_insufficient"}],
                          best=None)
    out = decide(art, target_makespan=50, max_rounds=2, stall_rounds=5)
    assert out["round"] == 2
    assert out["decision"] != "loop"
    assert out["decision"] == "finish-failed"


def test_gate_stall_resets_on_promoted_round(tmp_path: Path):
    rows = [
        {"vid": "r1-01", "round": 1, "outcome": "probe_insufficient"},
        {"vid": "r2-01", "round": 2, "outcome": "promoted",
         "makespan_cycles": 90, "proxy_acc": 0.8},
        {"vid": "r3-01", "round": 3, "outcome": "probe_insufficient"},
    ]
    art = _gate_artifacts(tmp_path, rounds={1: False, 2: False, 3: False},
                          history_rows=rows,
                          best={"vid": "r2-01", "makespan_cycles": 90, "proxy_acc": 0.8})
    out = decide(art, target_makespan=50, max_rounds=5, stall_rounds=2)
    assert out["decision"] == "loop"
    assert out["stall"] == 1  # r3 had no promotion, r2 reset the counter


def test_gate_fails_loud_without_proposals(tmp_path: Path):
    art = tmp_path / "empty-artifacts"
    (art / "rounds" / "001").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        decide(art, target_makespan=50, max_rounds=5, stall_rounds=2)


# ── advance_round ─────────────────────────────────────────────────────────────

def _variant_on_disk(art: Path, vid: str, round_no: int, makespan: int,
                     proxy_acc: float, promoted: bool = True,
                     shadow_tag: str | None = None):
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
        change_sig=f"sig:{vid}", probe_epochs=1, probe_max_steps=500,
        probe_data_value=None,
        target_modules=["m"], predicted_delta_cycles=-10,
        base_at_proposal={"vid": None, "makespan_cycles": 1000})
    if promoted:
        history_lib.append_latency(hist, vid, structural_check="pass",
                                   makespan_cycles=makespan, latency_gate="pass",
                                   pred_actual_ratio=1.0, outcome="latency_pass")
        history_lib.append_probe(hist, vid, proxy_acc=proxy_acc,
                                 promote_gate="pass", outcome="promoted")


def _advance_artifacts(tmp_path: Path) -> Path:
    art = tmp_path / "advance-artifacts"
    (art / "rounds" / "001").mkdir(parents=True)
    # initial base + global shadow from the round-1 starting point
    (art / "base").mkdir(parents=True)
    (art / "base" / "model.onnx").write_text("onnx-of-round0-base", encoding="utf-8")
    (art / "shadow" / "pkg").mkdir(parents=True)
    (art / "shadow" / "pkg" / "model.py").write_text("# shadow round0\n", encoding="utf-8")
    return art


def test_advance_round_r1_then_r2_replaces_base_and_shadow(tmp_path: Path):
    art = _advance_artifacts(tmp_path)
    _variant_on_disk(art, "r1-01", round_no=1, makespan=100, proxy_acc=0.8)

    out1 = advance(art)
    assert out1 == {"advanced": True, "round": 1, "vid": "r1-01",
                    "promoted_count": 1, "best_updated": True}
    assert (art / "base" / "model.onnx").read_text(encoding="utf-8") == "onnx-of-r1-01"
    assert (art / "shadow" / "pkg" / "model.py").read_text(encoding="utf-8") == "# shadow r1-01\n"
    # __pycache__ must not leak into the global shadow
    assert not (art / "shadow" / "pkg" / "__pycache__").exists()
    assert (art / "base" / "profile" / "profile_summary.json").is_file()
    best1 = json.loads((art / "best.json").read_text(encoding="utf-8"))
    assert best1["vid"] == "r1-01" and best1["makespan_cycles"] == 100
    assert json.loads((art / ".round_advanced").read_text(encoding="utf-8"))["round"] == 1

    # idempotency key: same marker round -> pure no-op even after new history
    out_again = advance(art)
    assert out_again["advanced"] is False

    # round 2 promotes something strictly better -> second replacement
    (art / "rounds" / "002").mkdir()
    _variant_on_disk(art, "r2-01", round_no=2, makespan=80, proxy_acc=0.75)
    out2 = advance(art)
    assert out2["advanced"] is True and out2["round"] == 2 and out2["vid"] == "r2-01"
    assert (art / "base" / "model.onnx").read_text(encoding="utf-8") == "onnx-of-r2-01"
    assert (art / "shadow" / "pkg" / "model.py").read_text(encoding="utf-8") == "# shadow r2-01\n"
    best2 = json.loads((art / "best.json").read_text(encoding="utf-8"))
    assert best2["vid"] == "r2-01" and best2["makespan_cycles"] == 80


def test_advance_round_stale_marker_replays(tmp_path: Path):
    """marker.round < max round (crash between rounds) -> replay the advance."""
    art = _advance_artifacts(tmp_path)
    _variant_on_disk(art, "r1-01", round_no=1, makespan=100, proxy_acc=0.8)
    advance(art)
    (art / "rounds" / "002").mkdir()
    _variant_on_disk(art, "r2-01", round_no=2, makespan=70, proxy_acc=0.7)
    # simulate a crash AFTER history write but BEFORE marker update
    (art / ".round_advanced").write_text(json.dumps({"round": 1, "vid": "r1-01"}),
                                         encoding="utf-8")
    out = advance(art)
    assert out["advanced"] is True and out["round"] == 2
    assert (art / "shadow" / "pkg" / "model.py").read_text(encoding="utf-8") == "# shadow r2-01\n"


def test_advance_round_replay_converges_after_mid_sequence_crash(tmp_path: Path):
    """The crash window the round-number key must survive: best.json already
    written for the NEW winner but base/shadow copy not done. Replay must
    re-derive and copy — not no-op on the equal best.json."""
    art = _advance_artifacts(tmp_path)
    _variant_on_disk(art, "r1-01", round_no=1, makespan=100, proxy_acc=0.8)
    advance(art)
    (art / "rounds" / "002").mkdir()
    _variant_on_disk(art, "r2-01", round_no=2, makespan=80, proxy_acc=0.75)

    # crash AFTER best.json write, BEFORE the copy and the marker:
    # best.json already names r2-01 while base/shadow still hold round 1
    (art / "best.json").write_text(json.dumps({
        "vid": "r2-01", "makespan_cycles": 80, "proxy_acc": 0.75, "round": 2,
        "profile_dir": "diag"}), encoding="utf-8")
    (art / ".round_advanced").write_text(json.dumps({"round": 1, "vid": "r1-01"}),
                                         encoding="utf-8")
    out = advance(art)
    assert out["advanced"] is True
    assert out["vid"] == "r2-01"
    assert (art / "base" / "model.onnx").read_text(encoding="utf-8") == "onnx-of-r2-01"
    assert (art / "shadow" / "pkg" / "model.py").read_text(encoding="utf-8") == "# shadow r2-01\n"
    # a second replay of the same round is a pure no-op again
    assert advance(art)["advanced"] is False


def test_advance_round_tie_break_prefers_higher_proxy_acc(tmp_path: Path):
    """Equal makespan -> the higher proxy accuracy wins the round (the v3.5
    renamed tie-break field)."""
    art = _advance_artifacts(tmp_path)
    _variant_on_disk(art, "r1-01", round_no=1, makespan=100, proxy_acc=0.7)
    _variant_on_disk(art, "r1-02", round_no=1, makespan=100, proxy_acc=0.9)
    out = advance(art)
    assert out["advanced"] is True and out["vid"] == "r1-02"
    assert json.loads((art / "best.json").read_text(encoding="utf-8"))["proxy_acc"] == 0.9


def test_advance_round_worse_promotion_keeps_base(tmp_path: Path):
    art = _advance_artifacts(tmp_path)
    _variant_on_disk(art, "r1-01", round_no=1, makespan=100, proxy_acc=0.8)
    advance(art)
    (art / "rounds" / "002").mkdir()
    _variant_on_disk(art, "r2-01", round_no=2, makespan=120, proxy_acc=0.9)  # worse makespan
    out = advance(art)
    assert out["advanced"] is True and out["vid"] == "r1-01" and out["best_updated"] is False
    assert (art / "base" / "model.onnx").read_text(encoding="utf-8") == "onnx-of-r1-01"
    assert (art / "shadow" / "pkg" / "model.py").read_text(encoding="utf-8") == "# shadow r1-01\n"


def test_advance_round_no_promotion_writes_marker(tmp_path: Path):
    art = _advance_artifacts(tmp_path)
    _variant_on_disk(art, "r1-01", round_no=1, makespan=100, proxy_acc=0.1,
                     promoted=False)
    # without a latency/probe row there is no promoted vid for round 1
    out = advance(art)
    assert out["advanced"] is True and out["vid"] is None and out["promoted_count"] == 0
    assert not (art / "best.json").exists()
    assert json.loads((art / ".round_advanced").read_text(encoding="utf-8"))["round"] == 1


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


# ── predict_delta.build_change_sig ────────────────────────────────────────────

def test_build_change_sig_is_canonical():
    from predict_delta import build_change_sig

    a = build_change_sig("activation", "Erf-4;Relu+2", ["blocks.1.mlp", "blocks.0.mlp"])
    b = build_change_sig("activation", "Erf-4;Relu+2", ["blocks.0.mlp", "blocks.1.mlp"])
    assert a == b == "activation:Erf-4;Relu+2:blocks.0.mlp,blocks.1.mlp"
    with pytest.raises(ValueError):
        build_change_sig("activation", "Erf-4", [])


# ── perturb_ckpt ──────────────────────────────────────────────────────────────

def test_perturb_ckpt_deterministic_and_container_aware(tmp_path: Path):
    torch = pytest.importorskip("torch")
    from perturb_ckpt import perturb

    model = torch.nn.Linear(8, 8)
    wrapped = {"model": model.state_dict(), "epoch": 3}
    src = tmp_path / "base.pth"
    torch.save(wrapped, str(src))

    a = perturb(src, tmp_path / "a.pth", "model", num_tensors=1, noise=0.5, seed=11)
    b = perturb(src, tmp_path / "b.pth", "model", num_tensors=1, noise=0.5, seed=11)
    sd_a = torch.load(str(tmp_path / "a.pth"), map_location="cpu", weights_only=False)
    sd_b = torch.load(str(tmp_path / "b.pth"), map_location="cpu", weights_only=False)
    assert sd_a["epoch"] == 3                          # sibling untouched
    assert torch.equal(sd_a["model"]["bias"], sd_b["model"]["bias"])   # deterministic seed
    assert not torch.equal(sd_a["model"]["bias"], wrapped["model"]["bias"])
    assert torch.equal(sd_a["model"]["weight"], wrapped["model"]["weight"])  # 1 of 2 keys
    assert a["perturbed_keys"] == ["bias"]             # sorted key order, deterministic


def test_perturb_ckpt_bare_container(tmp_path: Path):
    torch = pytest.importorskip("torch")
    from perturb_ckpt import perturb

    src = tmp_path / "bare.pth"
    torch.save(torch.nn.Linear(4, 4).state_dict(), str(src))
    out = perturb(src, tmp_path / "p.pth", None, num_tensors=1, noise=1.0, seed=0)
    assert out["perturbed_keys"] == ["bias"]
    got = torch.load(str(tmp_path / "p.pth"), map_location="cpu", weights_only=False)
    assert set(got) == {"bias", "weight"}


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

_CONTRACTS_SH = _REPO / "workflows" / "agents" / "po_contract" / "scripts" / "check_contracts.sh"


def _contracts_workspace(tmp_path: Path, *, probe_body: str | None = None,
                         full_body: str | None = None,
                         budget: dict | None = None) -> Path:
    """Minimal workspace satisfying the po_contract gate: contracts.json with
    a pinned proxy_budget, four templates, measured evidence, real entries."""
    import hashlib

    art = tmp_path / "art"
    (art / "templates").mkdir(parents=True)
    (art / "contract_work").mkdir(parents=True)

    for name in ("train.py", "eval.py", "exporter.py"):
        (art / name).write_text("# entry\n", encoding="utf-8")

    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    budget = budget or {"epochs": 1, "dataset_knob": "--limit",
                        "data_value": 2000, "max_steps": 500, "seed": 0}
    contracts = {
        "viable": True, "reason": "tier A, measured",
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
                  "ckpt_output_rule": "{out_dir}/model.pth",
                  "train_epochs_full": 10},
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
        "probe_cap_mechanism": "flag:--max-steps",
        "exemptions": [],
        "sitecustomize_merge": {"found": False, "path": "", "merged": False},
    }
    (art / "contracts.json").write_text(json.dumps(contracts), encoding="utf-8")

    probe = probe_body or (
        '"<<python>>" train.py --epochs <<epochs>> --out-dir <<out_dir>> '
        '--seed <<seed>> --limit <<data_value>> --max-steps <<max_steps>>\n')
    full = full_body or (
        '"<<python>>" train.py --epochs <<epochs>> --out-dir <<out_dir>> '
        '--seed <<seed>>\n')
    (art / "templates" / "run_probe_finetune.template.sh").write_text(
        probe, encoding="utf-8")
    (art / "templates" / "run_full_finetune.template.sh").write_text(
        full, encoding="utf-8")
    (art / "templates" / "run_eval.template.sh").write_text(
        '"<<python>>" eval.py --ckpt <<ckpt>> > <<log>> 2>&1\n', encoding="utf-8")
    (art / "templates" / "export_onnx.template.sh").write_text(
        '"<<python>>" exporter.py --out <<out>> --seed <<seed>>\n', encoding="utf-8")

    cw = art / "contract_work"
    (cw / "train_dryrun.json").write_text(
        json.dumps({"status": "runs_epochs_zero_rejected"}), encoding="utf-8")
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


def test_check_contracts_gate_enforces_token_budget_consistency(tmp_path: Path):
    # knob pinned but the probe template dropped the data token -> the
    # fairness invariant (same budget rendered) would silently break
    art = _contracts_workspace(
        tmp_path, probe_body='"<<python>>" train.py --epochs <<epochs>> '
        '--out-dir <<out_dir>> --seed <<seed>> --max-steps <<max_steps>>\n')
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "lacks <<data_value>>" in proc.stderr

    # max_steps pinned but the token vanished -> truncation would silently
    # disappear (render_run drops unused --set values)
    art2 = _contracts_workspace(
        tmp_path / "b", probe_body='"<<python>>" train.py --epochs <<epochs>> '
        '--out-dir <<out_dir>> --seed <<seed>> --limit <<data_value>>\n')
    proc2 = _run_contracts_gate(art2)
    assert proc2.returncode == 1
    assert "lacks" in proc2.stderr and "<<max_steps>>" in proc2.stderr

    # no knob recorded (epochs-only budget) yet the template still carries the
    # data token -> every render would fail on the unreplaced token
    art3 = _contracts_workspace(
        tmp_path / "c",
        budget={"epochs": 1, "dataset_knob": None, "data_value": None,
                "max_steps": None, "seed": 0})
    proc3 = _run_contracts_gate(art3)
    assert proc3.returncode == 1
    assert "carries <<data_value>>" in proc3.stderr

    # max_steps=null but the template still carries the step-cap token ->
    # the symmetric branch (renders would fail on the unreplaced token)
    art4 = _contracts_workspace(
        tmp_path / "d",
        budget={"epochs": 1, "dataset_knob": "--limit", "data_value": 2000,
                "max_steps": None, "seed": 0})
    proc4 = _run_contracts_gate(art4)
    assert proc4.returncode == 1
    assert "carries" in proc4.stderr and "<<max_steps>>" in proc4.stderr


def test_check_contracts_gate_forbids_ckpt_token_in_training_templates(tmp_path: Path):
    art = _contracts_workspace(
        tmp_path, probe_body='"<<python>>" train.py --epochs <<epochs>> '
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


# ── run_baseline_chain step6: stale-anchor proxy-budget re-verification ──────

_BASELINE_SH = _REPO / "workflows" / "agents" / "po_baseline" / "scripts" / "run_baseline_chain.sh"


def _baseline_workspace(tmp_path: Path, anchor_budget: dict) -> Path:
    """Workspace whose steps 1-5 products all exist, so the chain reaches the
    step6 product-exists branch with a promotion anchor (baseline_proxy_acc.json)
    already recorded under `anchor_budget`."""
    art = tmp_path / "art"
    for sub in ("baseline", "base/profile", "templates", "scripts",
                "readiness", "shadow/pkg"):
        art.joinpath(sub).mkdir(parents=True)
    (art / "shadow" / "pkg" / "__init__.py").write_text("", encoding="utf-8")

    budget = {"epochs": 1, "dataset_knob": "--limit", "data_value": 2000,
              "max_steps": 500, "seed": 0}
    (art / "contracts.json").write_text(json.dumps({
        "interpreter": {"sys_executable": sys.executable},
        "shadow": {"shadow_pkgs": ["pkg"]},
        "proxy_budget": budget,
    }), encoding="utf-8")
    (art / "readiness" / "readiness.json").write_text(
        json.dumps({"project_root": str(tmp_path)}), encoding="utf-8")
    for name in ("export_onnx.template.sh", "run_probe_finetune.template.sh",
                 "run_eval.template.sh"):
        (art / "templates" / name).write_text("echo template\n", encoding="utf-8")
    for src in ("render_run.sh", "analyze.py"):
        shutil.copy(_SCRIPTS / src, art / "scripts" / src)

    (art / "baseline" / "reference_check.json").write_text(
        json.dumps({"skipped": True}), encoding="utf-8")
    (art / "base" / "model.onnx").write_bytes(b"onnx-bytes")
    (art / "base" / "profile" / "profile_summary.json").write_text(
        json.dumps({"makespan_cycles": 500}), encoding="utf-8")
    (art / "base" / "bottleneck_report.json").write_text("{}", encoding="utf-8")
    (art / "baseline" / "baseline_ref.json").write_text(
        json.dumps({"baseline_ref_acc": 0.9, "source": "input"}), encoding="utf-8")
    (art / "baseline" / "baseline_proxy_acc.json").write_text(
        json.dumps({"proxy_acc": 0.5, "ckpt": "unused",
                    "proxy_budget": anchor_budget}), encoding="utf-8")
    return art


def _run_baseline_chain(art: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    return subprocess.run(
        ["bash", str(_BASELINE_SH), "--target-makespan", "100", "--seed", "0"],
        capture_output=True, text=True, timeout=120, env=env)


def test_baseline_chain_skip_reverifies_anchor_budget(tmp_path: Path):
    budget = {"epochs": 1, "dataset_knob": "--limit", "data_value": 2000,
              "max_steps": 500, "seed": 0}

    # anchor recorded under the current budget -> the skip stands, chain done
    art = _baseline_workspace(tmp_path / "fresh", anchor_budget=budget)
    proc = _run_baseline_chain(art)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["status"] == "executed"
    assert "step6 proxy train: product exists, skip" in proc.stderr

    # stale anchor (budget rebuilt since: epochs + max_steps drifted) ->
    # fail loud at step6 with per-field diff and rebuild guidance
    art2 = _baseline_workspace(
        tmp_path / "stale",
        anchor_budget={**budget, "epochs": 3, "max_steps": None})
    proc2 = _run_baseline_chain(art2)
    assert proc2.returncode == 1
    payload = json.loads(proc2.stdout)
    assert payload["status"] == "failed"
    assert payload["error"].startswith("baseline step 6:")
    assert "different proxy_budget" in payload["error"]  # self-contained error
    assert "different proxy_budget" in proc2.stderr
    assert "epochs" in proc2.stderr and "max_steps" in proc2.stderr
    assert "Delete baseline/baseline_proxy_acc.json" in proc2.stderr
    # never auto-deleted: rebuilding the anchor is a deliberate user action
    assert (art2 / "baseline" / "baseline_proxy_acc.json").is_file()

    # corrupt anchor (truncated mid-write) -> its own fail-loud attribution,
    # never misreported as a budget drift
    art3 = _baseline_workspace(tmp_path / "corrupt", anchor_budget=budget)
    (art3 / "baseline" / "baseline_proxy_acc.json").write_text(
        '{"proxy_acc": 0.5, "ck', encoding="utf-8")
    proc3 = _run_baseline_chain(art3)
    assert proc3.returncode == 1
    payload3 = json.loads(proc3.stdout)
    assert payload3["error"].startswith("baseline step 6:")
    assert "unreadable" in payload3["error"] and "unreadable" in proc3.stderr
    assert "different proxy_budget" not in proc3.stderr
    assert "Delete baseline/baseline_proxy_acc.json" in proc3.stderr


# the po_baseline node output schema — DERIVED from workflows/prof-opt.yaml,
# never hand-copied: the chain's stdout line is the agent's final reply
# VERBATIM, so its field set must be EXACTLY the schema's in BOTH directions
# (additionalProperties:false rejects extra keys; a schema edit must not
# silently strand the chain emitter either)
def _po_baseline_schema_fields() -> set[str]:
    import yaml
    wf = yaml.safe_load(
        (_REPO / "workflows" / "prof-opt.yaml").read_text(encoding="utf-8"))
    schema = next(n for n in wf["nodes"]
                  if n["name"] == "po_baseline")["output_schema"]
    props = set(schema["properties"])
    assert set(schema["required"]) == props, "schema itself drifted"
    return props


def test_baseline_chain_stdout_line_is_schema_shaped(tmp_path: Path):
    budget = {"epochs": 1, "dataset_knob": "--limit", "data_value": 2000,
              "max_steps": 500, "seed": 0}
    schema_fields = _po_baseline_schema_fields()

    # executed line: exactly the schema fields, products listed, no extras
    art = _baseline_workspace(tmp_path / "ok", anchor_budget=budget)
    proc = _run_baseline_chain(art)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert set(payload) == schema_fields
    assert payload["error"] == ""
    assert payload["base_onnx"] == "base/model.onnx"   # relative to $ORCA_ARTIFACTS_DIR
    assert payload["profile_dir"] == "base/profile"
    assert payload["makespan_cycles"] == 500
    assert payload["baseline_proxy_acc"] == 0.5        # schema type: number
    assert payload["baseline_ref_acc"] == 0.9
    assert "base/model.onnx" in payload["generated_artifacts"]
    assert "baseline/baseline_proxy_acc.json" in payload["generated_artifacts"]

    # explicit null marker (no user-provided ref acc) -> null, never 0
    (art / "baseline" / "baseline_ref.json").write_text(
        json.dumps({"baseline_ref_acc": None,
                    "source": "not-provided (auto-trained at the final stage when needed)"}),
        encoding="utf-8")
    proc_null = _run_baseline_chain(art)
    assert proc_null.returncode == 0, proc_null.stderr
    assert json.loads(proc_null.stdout)["baseline_ref_acc"] is None

    # failed line (step 3: profiling script not deployed): the failure must
    # still be forwardable verbatim — step number folded into error,
    # absent-product paths empty strings, no step key
    art2 = _baseline_workspace(tmp_path / "broken", anchor_budget=budget)
    (art2 / "base" / "profile" / "profile_summary.json").unlink()
    proc2 = _run_baseline_chain(art2)
    assert proc2.returncode == 1
    payload2 = json.loads(proc2.stdout)
    assert set(payload2) == schema_fields
    assert payload2["status"] == "failed"
    assert payload2["error"].startswith("baseline step ")
    assert payload2["profile_dir"] == ""          # product not produced
    assert payload2["makespan_cycles"] == 0
    assert "base/profile/" not in payload2["generated_artifacts"]


def test_baseline_chain_worker_logs_are_per_attempt(tmp_path: Path):
    """F1 (E2E round 3): a re-detached worker must never overwrite the previous
    attempt's log — the first-attempt failure scene stays diagnosable."""
    budget = {"epochs": 1, "dataset_knob": "--limit", "data_value": 2000,
              "max_steps": 500, "seed": 0}
    art = _baseline_workspace(tmp_path / "detach", anchor_budget=budget)
    (art / "baseline" / "baseline_proxy_acc.json").unlink()  # force step 6
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    proc = subprocess.run(
        ["bash", str(_BASELINE_SH), "--target-makespan", "100", "--seed", "0",
         "--poll-max-secs", "0"],
        capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode in (0, 1)
    # poll window 0: this invocation returns right after the detach — whether
    # it reports "running" or the fast-crashing fixture worker's "failed" is a
    # race; both prove the detach happened. The pinned contract is the
    # per-attempt log naming.
    payload = json.loads(proc.stdout)
    assert payload["status"] in ("running", "failed")
    stamps = art / "baseline" / ".stamps" / "step6"
    assert (stamps / "attempts").read_text(encoding="utf-8").strip() == "1"
    assert (stamps / "train_worker.attempt1.log").is_file()  # per-attempt name in use


# ── po_flatten reuse gate: fresh_start wipes the whole reusable workspace ─────

_REUSE_SH = _REPO / "workflows" / "agents" / "po_flatten" / "scripts" / "reuse_check.sh"


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
        ["bash", str(_REUSE_SH), "model.py", "", "1", ""],
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
        ["bash", str(_REUSE_SH), "model.py", "", "0", ""],
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
        ["bash", str(_REUSE_SH), "model.py", "", "0", ""],
        capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 3
    assert "owned by another live run" in proc.stderr
    assert json.loads((art / ".run_lock").read_text(encoding="utf-8"))["run_id"] \
        == "other-live-run"   # not taken over, not refreshed


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
    (art / "scripts" / "notes.txt").write_text("not a script", encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k != "ORCA_PYTHON"}
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    proc = subprocess.run(["bash", str(_DEPLOY_SH)], capture_output=True,
                          text=True, timeout=60, env=env)

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["scripts_dir"] == str(art / "scripts")
    assert payload["orphans_removed"] == 2          # both .py and .sh globs swept
    assert not (art / "scripts" / "make_variant_ckpt.py").exists()  # retired
    assert not (art / "scripts" / "legacy_sweep.sh").exists()       # retired
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

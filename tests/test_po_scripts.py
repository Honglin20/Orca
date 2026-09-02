"""test_po_scripts.py — unit tests for the prof-opt shared deterministic scripts.

Covers: history_lib (builder rejection + the dedup branches over raw rows —
permanent read-compat, joint retry budget, probe_insufficient permanently
consumed in v7), gate_decide (loop continuation + the missing-anchor
invariant), analyze (fixture-driven hot patterns / pipeline breakdown / cost
table + strict unknown-key failure + the frozen origin anchor), predict_delta
(v7: taskgraph-derived shape classes + critical-path weighting + the added-op
override), the mfu_adapter's four-piece mapping, render_run (<<k>> token
chain), check_contracts (v7: fairness-invariant token/budget enforcement,
profile block, admission ack, single training template), run_baseline_chain
(non-blocking baseline + finalizer guardian; mfu-only profiling with the
dispatch parameters from contracts.json; agent-chosen --device idx), the
po_flatten reuse gate (fresh_start whole-workspace wipe + v7 lock states),
and deploy_scripts' orphan retirement + version stamp.
Shared-layer coverage: metric_curve pinned-depth compare, push_curves
(top-10 / pareto / docs manifest), dashboard_snapshot, and the
extract_user_pkg / po_propose check_prerequisites helpers. The recheck
section pins the mfu-only gate's fail-loud matrix. The v6/v7 mechanics
(device ledger, watchdog, terminal rows, gate decision order, scenario
smokes) live in test_po_v6.py / test_po_v7.py; round_state / rules_pool /
gate_node stamp wiring live in test_po_v5.py.
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
import push_curves  # noqa: E402
from analyze import ContractError, analyze  # noqa: E402
from gate_decide import decide  # noqa: E402
from predict_delta import predict_delta  # noqa: E402


# ── history_lib ───────────────────────────────────────────────────────────────

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
                       probe_epochs: int = 1):
    """Raw JSONL rows, written directly: the read side judges any
    well-formed row, and dedup must also judge OLD workspace rows whose
    outcomes predate the append_terminal builder — so the fixture never
    goes through the write path."""
    for i, outcome in enumerate(outcomes, 1):
        vid = f"r1-{i:02d}"
        row = {"vid": vid, "round": 1, "seq": i, "parent_vid": None,
               "change_sig": sig, "probe_epochs": probe_epochs,
               "target_modules": ["m"], "predicted_delta_cycles": -10,
               "implemented": True,
               "base_at_proposal": {"vid": None, "makespan_cycles": 100},
               "version": 1, "ts": "2026-09-01T00:00:00+00:00"}
        if outcome == "latency_pass":
            row.update({"structural_check": "pass", "makespan_cycles": 100,
                        "latency_gate": "pass", "pred_actual_ratio": 1.0,
                        "outcome": "latency_pass"})
        elif outcome == "probe_insufficient":
            row.update({"outcome": "probe_insufficient", "stage": "train",
                        "max_retries_hit": True})
        else:  # advanced / promoted (read-compat) + the L0 eliminations
            row.update({"outcome": outcome})
        with open(hist, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")


@pytest.mark.parametrize("outcomes,blocked", [
    (["promoted"], True),                      # v4 read-compat: still permanent
    (["advanced"], True),                      # permanent: read-compat row
    (["unsupported_op"], True),                # permanent: structurally infeasible
    (["latency_pass"], False),                 # process state never blocks
    (["accuracy_fail"], False),                # composed re-proposals use NEW sigs
    (["structural_mismatch"], False),          # joint budget allows one retry
    (["variant_broken"], False),               # the other class, same budget
    (["structural_mismatch", "variant_broken"], True),   # joint budget exhausted
    (["variant_broken", "variant_broken"], True),        # same class twice: exhausted
    (["probe_insufficient"], True),            # v7: permanently consumed
])
def test_history_dedup_branches(tmp_path: Path, outcomes, blocked):
    hist = tmp_path / "history.jsonl"
    _write_sig_history(hist, "activation:x->y", outcomes)
    state = history_lib.dedup_state(hist, "activation:x->y", 1)
    assert state["blocked"] is blocked, state


def test_history_dedup_probe_insufficient_is_permanently_consumed(tmp_path: Path):
    """v7 §10: the proxy budget is fixed epoch-only — there is no knob whose
    change would reopen the sig, so a probe_insufficient signature is
    PERMANENTLY consumed; the reason names that (a genuine retry is a NEW
    composition with a new signature)."""
    hist = tmp_path / "history.jsonl"
    _write_sig_history(hist, "act:swap:m", ["probe_insufficient"], probe_epochs=1)
    same = history_lib.dedup_state(hist, "act:swap:m", 1)
    assert same["blocked"] is True
    assert "永久消费" in same["reason"]
    # even a different epoch count no longer reopens it (disclosure only)
    other = history_lib.dedup_state(hist, "act:swap:m", 2)
    assert other["blocked"] is True


def test_history_cli_surface(tmp_path: Path):
    """The po_propose node drives the CLI, not the function: pin the CLI
    surface — probe-epochs survives the round trip, the retired knob flags
    fail loud (v7 C6), and a probe_insufficient sig stays blocked."""
    hist = tmp_path / "history.jsonl"
    _write_sig_history(hist, "act:swap:m", ["probe_insufficient"], probe_epochs=1)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "history_lib.py"),
         "--history", str(hist), "--sig", "act:swap:m",
         "--probe-epochs", "1"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["blocked"] is True

    # the v6 knob flags are RETIRED: a stale invocation fails loud instead of
    # being silently ignored
    stale = subprocess.run(
        [sys.executable, str(_SCRIPTS / "history_lib.py"),
         "--history", str(hist), "--sig", "act:swap:m",
         "--probe-epochs", "1", "--probe-max-steps", "null"],
        capture_output=True, text=True, timeout=60)
    assert stale.returncode != 0
    assert "--probe-max-steps" in stale.stderr


# ── gate_decide: loop continuation + anchor invariant ─────────────────────────

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


# a vid whose round closed without reaching the line (no terminal pass)
_NO_PASS_R1 = [
    {"vid": "r1-01", "round": 1, "outcome": "latency_fail",
     "makespan_cycles": 800},
]


def test_gate_loops_across_consecutive_failing_rounds(tmp_path: Path):
    """v6 has no stall/plateau exit: many failing rounds still loop
    (the plateau's answer is proposal rerouting, not stopping)."""
    rows = [dict(_NO_PASS_R1[0], vid=f"r{r}-01", round=r)
            for r in range(1, 6)]
    art = _gate_artifacts(tmp_path, rounds=[1, 2, 3, 4, 5],
                          history_rows=rows, best=None)
    out = decide(art, max_rounds=100)
    assert out["decision"] == "loop"


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


# ── predict_delta (v7 §5.2: taskgraph shape derivation + critical-path
#    weighting; the --sites hand-binning mode is deleted) ──────────────────────

_REPORT = {
    "cost_table": [
        {"op_type": "Erf", "shape_class": "<1e2", "count": 4, "mean_cycles": 50,
         "min_cycles": 50, "max_cycles": 50},
        {"op_type": "Relu", "shape_class": "<1e2", "count": 2, "mean_cycles": 20,
         "min_cycles": 20, "max_cycles": 20},
        {"op_type": "Relu", "shape_class": "1e2-1e4", "count": 2, "mean_cycles": 80,
         "min_cycles": 80, "max_cycles": 80},
    ],
    "critical_path": [
        {"name": "erf1", "op_type": "Erf", "latency": 50},
        {"name": "erf2", "op_type": "Erf", "latency": 50},
    ],
    "profile_dir": ".",
}


def _tg(path: Path, ops: list[dict]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "taskgraph.json").write_text(json.dumps(
        {"schema_version": 1, "onnx": "fixture.onnx",
         "operators": [{"name": o["name"], "op_type": o["op_type"],
                        "task_id": f"t{i:04d}", "pipeline": "p000",
                        "latency": o["latency"], "depends_on": o.get("depends_on", []),
                        "output_memory": 8, "output_dimensions": o["dims"],
                        "onnx_nodes": [o["name"]]} for i, o in enumerate(ops)]}),
        encoding="utf-8")
    return path


def test_predict_delta_arithmetic_and_params(tmp_path: Path):
    # 4 removed Erf sites, all on the critical path -> weight 1.0 each
    _tg(tmp_path, [
        {"name": "erf1", "op_type": "Erf", "latency": 50, "dims": [1, 64]},
        {"name": "erf2", "op_type": "Erf", "latency": 50, "dims": [1, 64]},
        {"name": "erf3", "op_type": "Erf", "latency": 50, "dims": [1, 64]},
        {"name": "erf4", "op_type": "Erf", "latency": 50, "dims": [1, 64]},
    ])
    report = dict(_REPORT, profile_dir=str(tmp_path))
    out = predict_delta(report, {"Erf": -4}, {}, ["erf1", "erf2", "erf3", "erf4"])
    # erf1/erf2 are on the report's critical_path (1.0), erf3/erf4 off (0.25)
    assert out["predicted_delta_cycles"] == -(2 * 50 + 2 * 50 * 0.25)
    assert out["params"] == "Erf-4"
    assert out["basis"][0]["source"] == "cost_table:by-node"
    assert out["weights"] == {"on_critical_path": 1.0, "off_critical_path": 0.25,
                              "added_sites": 1.0}
    # added sites price at the explicit override (never guessed)
    out2 = predict_delta(report, {"Erf": -1, "Relu": 1}, {"Relu": 30}, ["erf1"])
    assert out2["predicted_delta_cycles"] == -50 + 30


def test_predict_delta_critical_path_weighting(tmp_path: Path):
    """v7 §5.2: on-path sites weigh 1.0, off-path 0.25 — and the split is
    DISCLOSED ({on_path_cycles, off_path_cycles_weighted}), never silent."""
    _tg(tmp_path, [
        {"name": "erf1", "op_type": "Erf", "latency": 50, "dims": [1, 64]},
        {"name": "erf2", "op_type": "Erf", "latency": 50, "dims": [1, 64]},
        {"name": "erf3", "op_type": "Erf", "latency": 50, "dims": [1, 64],
         "depends_on": []},
        {"name": "erf4", "op_type": "Erf", "latency": 50, "dims": [1, 64],
         "depends_on": []},
    ])
    report = dict(_REPORT, profile_dir=str(tmp_path))
    out = predict_delta(report, {"Erf": -4}, {},
                        ["erf1", "erf2", "erf3", "erf4"])  # erf1/erf2 on path
    # 2 sites on-path at 1.0 + 2 sites off-path at 0.25
    assert out["predicted_delta_cycles"] == -(2 * 50 + 2 * 50 * 0.25)
    assert out["on_path_cycles"] == 100.0
    assert out["off_path_cycles_weighted"] == 25.0
    sites = {s["node"]: s for s in out["basis"][0]["sites"]}
    assert sites["erf1"]["weight"] == 1.0 and sites["erf1"]["on_critical_path"]
    assert sites["erf3"]["weight"] == 0.25
    assert sites["erf3"]["on_critical_path"] is False


def test_predict_delta_shape_class_derived_from_taskgraph(tmp_path: Path):
    """v7: the shape class is DERIVED from the taskgraph node's
    output_dimensions (dims 1x64 = 64 elements -> <1e2 bucket here) — the
    LLM never hand-computes element counts."""
    _tg(tmp_path, [
        {"name": "big", "op_type": "Relu", "latency": 80, "dims": [1, 500]},
        {"name": "small", "op_type": "Relu", "latency": 20, "dims": [1, 50]},
    ])
    report = dict(_REPORT, profile_dir=str(tmp_path))
    out = predict_delta(report, {"Relu": -2}, {}, ["big", "small"])
    sites = {s["node"]: s for s in out["basis"][0]["sites"]}
    assert sites["big"]["shape_class"] == "1e2-1e4"   # 500 elements
    assert sites["small"]["shape_class"] == "<1e2"    # 50 elements
    # -1*80 (big, on-path weight 1.0; neither node is on the critical path
    # -> both off-path 0.25)
    assert out["predicted_delta_cycles"] == -int(round((80 + 20) * 0.25))


def test_predict_delta_nodes_validation_fails_loud(tmp_path: Path):
    # a node absent from taskgraph.json
    _tg(tmp_path, [{"name": "erf1", "op_type": "Erf", "latency": 50,
                    "dims": [1, 64]}])
    report = dict(_REPORT, profile_dir=str(tmp_path))
    with pytest.raises(ValueError, match="absent from taskgraph"):
        predict_delta(report, {"Erf": -1}, {}, ["ghost"])
    # wrong node count for the delta
    with pytest.raises(ValueError, match="operator\\(s\\) of op_type"):
        predict_delta(report, {"Erf": -2}, {}, ["erf1"])
    # an ADDED op with no override: never guessed
    with pytest.raises(ValueError, match="refusing to guess"):
        predict_delta(report, {"Relu": 1}, {})
    # duplicate node names
    with pytest.raises(ValueError, match="duplicate"):
        predict_delta(report, {"Erf": -2}, {}, ["erf1", "erf1"])


def test_predict_delta_missing_taskgraph_fails_loud(tmp_path: Path):
    report = dict(_REPORT, profile_dir=str(tmp_path / "nope"))
    with pytest.raises(ValueError, match="taskgraph.json not found"):
        predict_delta(report, {"Erf": -1}, {}, ["erf1"])

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


def _contracts_workspace(tmp_path: Path, *, train_body: str | None = None,
                         budget: dict | None = None,
                         full_train_budget: dict | None = None) -> Path:
    """Minimal workspace satisfying the po_contract v7 gate: contracts.json
    with the profile block, admission ack, early_stop thresholds, epoch-only
    budgets, ONE training template, measured evidence, real entries."""
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

    budget = budget or {"epochs": 1, "seed": 0}
    contracts = {
        "viable": True,
        "reason": "tier A, measured",
        "interpreter": {"sys_executable": sys.executable, "flags_check": "pass"},
        "shadow": {"shadow_root": str(art / "shadow"), "shadow_pkgs": ["pkg"]},
        "model_facts": {"module": "pkg.model", "factory": "build",
                        "args": [], "kwargs": {},
                        "dummy_inputs": [{"name": "x", "shape": [1, 4],
                                          "dtype": "float32"}]},
        "train": {"tier": "A", "entry": str(art / "train.py"),
                  "entry_sha256": sha(art / "train.py"),
                  "flags": {"epochs": "--epochs", "out_dir": "--out-dir",
                            "seed": "--seed"},
                  "ckpt_output_rule": "{out_dir}/epoch_*.pth",
                  "ckpt_per_epoch": True,
                  "epoch_metric_extraction": {
                      "kind": "stdout_regex",
                      "pattern": r"epoch (?P<epoch>\d+) metric=(?P<metric>[0-9.]+)"},
                  "train_epochs_full": 10},
        "full_train_budget": full_train_budget or {"epochs": 2, "seed": 0},
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
        "profile": {"chip": "6613", "precision": "INT8", "core_num": 1},
        "early_stop": {"warmup_frac": 0.1, "streak_frac": 0.3},
        "admission_clause_ack": True,
        "exemptions": [],
        "sitecustomize_merge": {"found": False, "path": "", "merged": False},
    }
    (art / "contracts.json").write_text(json.dumps(contracts), encoding="utf-8")

    body = train_body or (
        '"<<python>>" train.py --epochs <<epochs>> --out-dir <<out_dir>> '
        '--seed <<seed>> --device <<device>>\n')
    (art / "templates" / "run_full_finetune.template.sh").write_text(
        body, encoding="utf-8")
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
        json.dumps({"epochs": 1, "seed": 0,
                    "rationale": "epoch-only probe depth k=1"}), encoding="utf-8")
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
    """The quick-run evidence speaks ONLY the fixed classification set: a
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
    downstream consumer (metric_curve extract and the variant watchdogs)
    only after the
    full baseline has already run - the gate must re-run the extraction on
    the REAL quickrun log and fail here, at the contract stage."""
    art = _contracts_workspace(tmp_path)
    (art / "contract_work" / "quickrun_train.log").write_text(
        "epoch 0 metric=0.9\nepoch 1 metric=0.8\n", encoding="utf-8")
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "contiguous from 1" in proc.stderr


def test_check_contracts_gate_requires_device_token_in_training_template(tmp_path: Path):
    # a training template without <<device>> renders a card-agnostic training
    # — the allocation ledger's mutual exclusion silently breaks
    art = _contracts_workspace(
        tmp_path,
        train_body='"<<python>>" train.py --epochs <<epochs>> --out-dir <<out_dir>> '
                   '--seed <<seed>>\n')
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "<<device>>" in proc.stderr

    # the symmetric half: the EVAL/EXPORT templates must NOT grow the token
    # (only the training render binds a claimed card)
    art2 = _contracts_workspace(tmp_path / "b")
    tpl = art2 / "templates" / "run_eval.template.sh"
    tpl.write_text(tpl.read_text(encoding="utf-8").rstrip("\n")
                   + " --device <<device>>\n", encoding="utf-8")
    assert _run_contracts_gate(art2).returncode == 0

    # v7 C9: exactly ONE training template — the byte-identical twin is gone
    assert not (art / "templates" / "run_probe_finetune.template.sh").exists()


def test_check_contracts_gate_forbids_ckpt_token_in_training_template(tmp_path: Path):
    art = _contracts_workspace(
        tmp_path,
        train_body='"<<python>>" train.py --epochs <<epochs>> '
        '--out-dir <<out_dir>> --seed <<seed>> --device <<device>> '
        '--resume <<ckpt>>\n')
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "<<ckpt>>" in proc.stderr and "from scratch" in proc.stderr


def test_check_contracts_gate_validates_v7_fields(tmp_path: Path):
    def rewrite(art: Path, mutate):
        contracts = json.loads((art / "contracts.json").read_text(encoding="utf-8"))
        mutate(contracts)
        (art / "contracts.json").write_text(json.dumps(contracts), encoding="utf-8")

    # proxy_budget is pinned epoch-only: the knob/max-steps fields are GONE
    art = _contracts_workspace(
        tmp_path / "a",
        budget={"epochs": 1, "seed": 0, "dataset_knob": None, "max_steps": None})
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "dataset_knob/data_value/max_steps are deleted" in proc.stderr

    # proxy_budget.epochs is exactly 1 (min(1, full))
    art = _contracts_workspace(tmp_path / "b", budget={"epochs": 3, "seed": 0})
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "proxy_budget.epochs must be exactly 1" in proc.stderr

    # full_train_budget carries exactly {epochs, seed} (no data pair)
    art = _contracts_workspace(
        tmp_path / "c",
        full_train_budget={"epochs": 2, "seed": 0,
                           "data": {"dataset_knob": None, "data_value": None}})
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "data-knob pair is deleted" in proc.stderr

    # admission ack is the stable boolean (the clause text lives only in the
    # agent document — v7 C8)
    art = _contracts_workspace(tmp_path / "d")
    rewrite(art, lambda c: c.update(admission_clause_ack=False))
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "admission_clause_ack" in proc.stderr

    # profile block enums (the mfu dispatch parameters)
    art = _contracts_workspace(tmp_path / "e")
    rewrite(art, lambda c: c.update(profile={"chip": "9900", "precision": "INT8",
                                             "core_num": 1}))
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "profile.chip" in proc.stderr

    # early_stop fractions in (0, 1)
    art = _contracts_workspace(tmp_path / "f")
    rewrite(art, lambda c: c.update(early_stop={"warmup_frac": 0.1,
                                                "streak_frac": 3}))
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "early_stop.streak_frac" in proc.stderr

    # proxy_budget_selection.json carries the COMPLETE field set (C5)
    art = _contracts_workspace(tmp_path / "g")
    (art / "contract_work" / "proxy_budget_selection.json").write_text(
        json.dumps({"epochs": 1, "seed": 0}), encoding="utf-8")
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "proxy_budget_selection" in proc.stderr

    # missing ckpt_per_epoch -> ckpt addressability undecidable downstream
    art = _contracts_workspace(tmp_path / "h")
    rewrite(art, lambda c: c["train"].pop("ckpt_per_epoch"))
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "ckpt_per_epoch" in proc.stderr

    # wrong cap mechanism
    art = _contracts_workspace(tmp_path / "i")
    rewrite(art, lambda c: c.update(probe_cap_mechanism="epochs-only"))
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "stop-at-k" in proc.stderr

    # metric pattern that can truncate mid-number (boundary anchor check)
    art = _contracts_workspace(tmp_path / "j")
    rewrite(art, lambda c: c["train"].update(epoch_metric_extraction={
        "kind": "stdout_regex",
        "pattern": r"epoch (?P<epoch>\d+) metric=(?P<metric>[0-9.]{4})"}))
    proc = _run_contracts_gate(art)
    assert proc.returncode == 1
    assert "truncation" in proc.stderr

    # control: the unmutated v7 workspace passes (guard the fixture itself)
    assert _run_contracts_gate(_contracts_workspace(tmp_path / "ok")).returncode == 0


def test_check_contracts_reuse_rejects_pre_v7_contracts(tmp_path: Path):
    """A reusable workspace built before this workflow version lacks the v7
    fields — the reuse gate must fail loud (exit 3) with the fresh_start
    hint instead of failing cryptically downstream (v7 C4: version/config
    drift exits 3, sha drift exits 1 — different remedies)."""
    art = _contracts_workspace(tmp_path)
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)

    def reuse(*extra: str):
        return subprocess.run(["bash", str(_CONTRACTS_SH), "--reuse-check", *extra],
                              capture_output=True, text=True, timeout=60, env=env)

    # current-version contracts + matching shas -> REUSE
    assert reuse().returncode == 0

    # strip the v7 fields: a pre-v7 contracts.json -> exit 3 + fresh_start hint
    contracts = json.loads((art / "contracts.json").read_text(encoding="utf-8"))
    contracts.pop("full_train_budget")
    contracts.pop("profile")
    contracts["train"].pop("ckpt_per_epoch")
    (art / "contracts.json").write_text(json.dumps(contracts), encoding="utf-8")
    proc = reuse()
    assert proc.returncode == 3
    assert "predates the current workflow version" in proc.stderr
    assert "fresh_start" in proc.stderr


def test_check_contracts_reuse_profile_drift_needs_fresh_start(tmp_path: Path):
    """v7 §2: the profiling configuration is part of the measurement
    fingerprint — a reuse whose recorded profile block disagrees with the
    CURRENT workflow inputs exits 3 (cycles measured under a different
    configuration cannot be compared)."""
    art = _contracts_workspace(tmp_path)
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)

    def reuse(*flags: str):
        return subprocess.run(
            ["bash", str(_CONTRACTS_SH), "--reuse-check", *flags],
            capture_output=True, text=True, timeout=60, env=env)

    # matching inputs -> REUSE
    ok = reuse("--profile-chip", "6613", "--profile-precision", "INT8",
               "--profile-core-num", "1")
    assert ok.returncode == 0, ok.stderr

    # chip drift -> exit 3 with the drift named
    drift = reuse("--profile-chip", "1951", "--profile-precision", "INT8",
                  "--profile-core-num", "1")
    assert drift.returncode == 3
    assert "profile config drift" in drift.stderr
    assert "fresh_start" in drift.stderr


def test_check_contracts_reuse_rejects_viable_false(tmp_path: Path):
    """v7 C3: a workspace whose contract stage FAILED (viable=false) must
    never be reused as if it passed — exit 3, fresh_start guidance."""
    art = _contracts_workspace(tmp_path)
    contracts = json.loads((art / "contracts.json").read_text(encoding="utf-8"))
    contracts["viable"] = False
    (art / "contracts.json").write_text(json.dumps(contracts), encoding="utf-8")
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    proc = subprocess.run(["bash", str(_CONTRACTS_SH), "--reuse-check"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 3
    assert "viable is not true" in proc.stderr


def test_check_contracts_reuse_sha_drift_rebuilds(tmp_path: Path):
    """v7 C4's other exit: an entry sha drift (the recorded entry changed
    under the contracts) is exit 1 — rebuild the contracts, NOT fresh_start
    (the workspace itself is fine)."""
    art = _contracts_workspace(tmp_path)
    (art / "train.py").write_text("# entry, changed\n", encoding="utf-8")
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    proc = subprocess.run(["bash", str(_CONTRACTS_SH), "--reuse-check"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 1
    assert "sha256 drift" in proc.stderr
    assert "fresh_start" not in proc.stderr


# ── run_baseline_chain (v4): non-blocking baseline + finalizer guardian ──────

_BASELINE_SH = _REPO / "workflows" / "prof-opt" / "agents" / "po_baseline" / "scripts" / "run_baseline_chain.sh"

_BL_MD = ("[subagent:business-logic-analyst v1 BLA7K4]\n## 任务语义\nclassify\n"
          "## 输入输出\nx->y\n## 架构动机\nwhy\n"
          "## 逐模块职责与物理意义\nper module\n## 训练目标与指标方向\nacc higher\n")
_IX_MD = ("[subagent:information-analyst v2 IXA3N7]\n## 信息成分拆解\nwhat each "
          "step computes\n## 最小信息核心\nthe core\n## 冗余与可近似项\nredundancy\n"
          "## 创新结构方向\nat least one substantive direction\n")
_MFU_MD = ("[subagent:mfu-analyzer v2 MBA7K2]\n## MFU 时延瓶颈分析报告\n"
           "### 模型概况\nsmall\n### 瓶颈根因\nroot cause one\n"
           "### 算子级证据表（按显著性列行）\nevidence rows\n"
           "### 评测异常与披露\n无\n")


def _baseline_ws(tmp_path: Path, *, full_epochs: int = 2, probe_k: int = 1,
                 ckpt_per_epoch: bool = True, train_body: str | None = None,
                 with_docs: bool = True) -> Path:
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
                "push_curves.py", "analyze.py", "device_alloc.py",
                "pid_lib.py"):
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
    if with_docs:
        (art / "baseline" / "business_logic.md").write_text(_BL_MD, encoding="utf-8")
        (art / "base" / "information_analysis.md").write_text(_IX_MD, encoding="utf-8")
        (art / "base" / "profile" / "mfu_bottleneck_report.md").write_text(
            _MFU_MD, encoding="utf-8")

    rule = "{out_dir}/epoch_*.pth" if ckpt_per_epoch else "{out_dir}/model.pth"
    (art / "contracts.json").write_text(json.dumps({
        "interpreter": {"sys_executable": sys.executable},
        "shadow": {"shadow_pkgs": ["pkg"]},
        "full_train_budget": {"epochs": full_epochs, "seed": 0},
        "proxy_budget": {"epochs": probe_k, "seed": 0},
        "profile": {"chip": "6613", "precision": "INT8", "core_num": 1},
        "early_stop": {"warmup_frac": 0.1, "streak_frac": 0.3},
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
    # the training device backend the entry node resolved (synthetic card)
    (art / "train_device.json").write_text(
        json.dumps({"backend": "cuda", "device_count": 1,
                    "resolved_by": "test"}), encoding="utf-8")
    # the backend occupancy CLI the node's device_alloc probe reads (idle)
    stub_dir = art / "stubbin"
    stub_dir.mkdir()
    (stub_dir / "nvidia-smi").write_text(
        "#!/bin/sh\n"
        "printf 'GPU 0: stub (idle)\\nGPU 1: stub (idle)\\n'\n", encoding="utf-8")
    (stub_dir / "nvidia-smi").chmod(0o755)
    return art


def _chain_env(art: Path) -> dict:
    """Env for direct chain invocations: artifacts root + the fixture's
    backend-CLI stub dir in front of PATH (the node's probe passes the stub's
    stdout through verbatim; the chain's claim needs no PATH)."""
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    stub = art / "stubbin"
    if stub.is_dir():
        env["PATH"] = str(stub) + os.pathsep + env["PATH"]
    return env


def _run_baseline_chain(art: Path, timeout: int = 120,
                        device: str = "0") -> subprocess.CompletedProcess:
    cmd = ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5",
           "--seed", "0"]
    if device is not None:
        cmd += ["--device", device]
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, env=_chain_env(art))


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


def test_workflow_inputs_pin_v7_eleven_input_set():
    """The v7 input set: the profiling trio (chip/precision/core_num — the
    mfu dispatch parameters, explicitly given, never sniffed), max_rounds
    promoted to [ask] required (the workflow's one cost gate), the new
    idle_round_cap, and the retired six still gone."""
    import yaml
    wf = yaml.safe_load(
        (_REPO / "workflows" / "prof-opt" / "workflow.yaml").read_text(encoding="utf-8"))
    inputs = wf["inputs"]
    assert set(inputs) == {"project_root", "model_path", "latency_reduction_min",
                           "accuracy_budget", "profile_chip", "max_rounds", "seed",
                           "profile_precision", "profile_core_num",
                           "idle_round_cap", "fresh_start",
                           "full_train_epoch_cap"}
    pinned = {
        "project_root": ("string", True, None),
        "model_path": ("string", True, None),
        "latency_reduction_min": ("number", True, None),
        "accuracy_budget": ("number", True, None),
        "profile_chip": ("string", True, None),
        "max_rounds": ("integer", True, None),
        "seed": ("integer", False, 0),
        "profile_precision": ("string", False, "INT8"),
        "profile_core_num": ("integer", False, 1),
        "idle_round_cap": ("integer", False, 5),
        "fresh_start": ("boolean", False, False),
        "full_train_epoch_cap": ("string", False, ""),
    }
    for name, (typ, required, default) in pinned.items():
        assert inputs[name]["type"] == typ, name
        assert inputs[name]["required"] is required, name
        if default is not None:
            assert inputs[name]["default"] == default, name
    # the profiling enums are pinned at the input layer
    assert inputs["profile_chip"]["enum"] == ["6613", "1951"]
    assert inputs["profile_precision"]["enum"] == ["INT8", "INT16", "AMP"]
    assert inputs["profile_core_num"]["enum"] == [1, 2, 4]

    for retired in ("profile_script_path", "npu_chip", "npu_precision",
                    "npu_core_num", "write_back", "report_dir", "probe_epochs"):
        assert retired not in inputs, retired

    # the anchors the freeze consumes are referenced in the baseline body
    body = (_REPO / "workflows" / "prof-opt" / "agents" / "po_baseline" / "agent.md") \
        .read_text(encoding="utf-8")
    assert "{{ inputs.latency_reduction_min }}" in body
    assert "{{ inputs.accuracy_budget }}" in body


def test_workflow_po_report_schema_drops_pretrained_ref_acc():
    """v7 §9: the v5-era pretrained_ref_acc output field is deleted from the
    report schema (and outputs)."""
    import yaml
    wf = yaml.safe_load(
        (_REPO / "workflows" / "prof-opt" / "workflow.yaml").read_text(encoding="utf-8"))
    node = next(n for n in wf["nodes"] if n["name"] == "po_report")
    assert "pretrained_ref_acc" not in node["output_schema"]["properties"]
    assert "pretrained_ref_acc" not in wf["outputs"]


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
        "po_propose": {"status", "error", "repair_count",
                       "generated_artifacts"},
        "po_probe": {"status", "error", "device", "epoch1_ok",
                     "generated_artifacts"},
    }
    for node in wf["nodes"]:
        if node["name"] not in expected:
            continue
        schema = node["output_schema"]
        props = set(schema["properties"])
        assert set(schema["required"]) == props
        assert props == expected[node["name"]], node["name"]


def test_workflow_v6_shape_and_gate_routes_are_pinned():
    """The v6 closure is shape-pinned, not just convention: 7 nodes, the
    full-train node is gone, the gate routes exactly report/loop with a
    catch-all to po_report (a finish-failed decision that matches NEITHER
    explicit route lands there), and every node's routes stay total."""
    import yaml

    wf = yaml.safe_load(
        (_REPO / "workflows" / "prof-opt" / "workflow.yaml").read_text(
            encoding="utf-8"))
    nodes = {n["name"]: n for n in wf["nodes"]}
    assert list(nodes) == ["po_flatten", "po_contract", "po_baseline",
                           "po_propose", "po_probe", "po_gate", "po_report"]
    assert "po_full_train" not in nodes

    gate = nodes["po_gate"]["routes"]
    assert [(r.get("when"), r["to"]) for r in gate] == [
        ("po_gate.output.json.decision == 'report'", "po_report"),
        ("po_gate.output.json.decision == 'loop'", "po_propose"),
        (None, "po_report"),
    ]
    # the finish-failed disclosure matches no explicit when -> catch-all
    for decision in ("finish-failed", "full-train", "full-train-best-effort"):
        assert not any(decision in (r.get("when") or "") for r in gate)

    # every node's route set is total (last route has no when), the only
    # back edge is the gate's loop, and every target exists
    edges = []
    for name, node in nodes.items():
        routes = node["routes"]
        assert "when" not in routes[-1], f"{name}: catch-all must be last"
        for r in routes:
            edges.append((name, r["to"]))
    assert ("po_gate", "po_propose") in edges
    assert not any(a == "po_propose" and b == "po_propose" for a, b in edges)
    for a, b in edges:
        assert b in nodes or b == "$end", (a, b)


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


def test_baseline_chain_lock_excludes_other_claimants_while_training(tmp_path: Path):
    """Ledger intent (v7 §6.1): while the baseline training is alive, the
    claim is adopted by the long-lived finalizer (which owns the terminal
    release), and a LATER claimant's `claim --idx 0` gets ok:false naming
    the holder — the O_EXCL lock is the only allocation path."""
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

    # a later claimant for the SAME idx is refused with the holder named
    # (rc 0 — a legitimate park state, the caller re-probes and picks another)
    env = _chain_env(art)
    claim = subprocess.run(
        [sys.executable, str(art / "scripts" / "device_alloc.py"),
         "claim", "--artifacts", str(art), "--vid", "r9-99", "--idx", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    assert claim.returncode == 0, claim.stderr
    doc = json.loads(claim.stdout)
    assert doc["ok"] is False and "locked by vid=baseline" in doc["reason"]
    assert lock_path.is_file()   # not taken over under a live training


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
    assert any(a.endswith("base/information_analysis.md") for a in artifacts)
    assert any(a.endswith("base/profile/mfu_bottleneck_report.md")
               for a in artifacts)

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

    # accuracy anchors carry the value-level budget fingerprint (v7:
    # epoch-only — the data-knob pair is deleted)
    full_acc = json.loads((art / "baseline" / "baseline_full_acc.json").read_text(
        encoding="utf-8"))
    assert full_acc["baseline_full_acc"] == 0.9
    assert full_acc["full_train_budget"] == {"epochs": 2, "seed": 0}
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


def test_baseline_chain_running_until_all_three_docs_land(tmp_path: Path):
    """ALL THREE baseline analysis documents are a HARD precondition of
    executed (v7 §4.3): any missing one -> the agent-internal running line
    NAMING what is missing; once all are on disk (and the finalizer
    terminal), a re-invocation emits executed."""
    art = _baseline_ws(tmp_path, full_epochs=1, with_docs=False)
    proc = _run_baseline_chain(art)
    payload = json.loads(proc.stdout)
    assert payload["status"] == "running"
    for missing in ("baseline/business_logic.md",
                    "base/information_analysis.md",
                    "base/profile/mfu_bottleneck_report.md"):
        assert missing in payload["error"], payload["error"]

    # documents landing one at a time: still running until the LAST one
    (art / "baseline" / "business_logic.md").write_text(_BL_MD, encoding="utf-8")
    (art / "base" / "information_analysis.md").write_text(_IX_MD, encoding="utf-8")
    # the dummy training may already have ended by now — the chain then
    # reports the transient step-6 "finalizer finalizing" line first. Re-invoke
    # (bounded; the finalizer's poll cycle is 10 s) until the docs gate
    # speaks: the missing doc must BLOCK executed and the running line must
    # eventually NAME it.
    mid_err = ""
    for attempt in range(10):
        if attempt:
            time.sleep(3)
        mid = _run_baseline_chain(art)
        mid_doc = json.loads(mid.stdout)
        assert mid_doc["status"] == "running", mid_doc
        mid_err = mid_doc["error"]
        if "mfu_bottleneck_report.md" in mid_err:
            break
    assert "mfu_bottleneck_report.md" in mid_err, mid_err

    (art / "base" / "profile" / "mfu_bottleneck_report.md").write_text(
        _MFU_MD, encoding="utf-8")
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
    """Baseline workspace for the mfu handshake (v7: the ONE profiling path):
    a REAL exported onnx (the chain skips its export step), no profiling
    products yet, and the adapter deployed. The dispatch parameters come
    from contracts.json's profile block. Returns (art, env)."""
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
    shutil.copy(_SCRIPTS / "mfu_adapter.py", art / "scripts" / "mfu_adapter.py")
    model = Tiny().eval()
    torch.onnx.export(model, torch.randn(1, 32), str(art / "base" / "model.onnx"),
                      input_names=["x"], output_names=["out"],
                      opset_version=17, do_constant_folding=True)
    # strip the pre-made early-chain products: profile + analyze + the mfu
    # report doc must be re-derived through the mfu path (the tests re-write
    # the report themselves — the analyzer subagent's product)
    for rel in ("base/profile/profile_summary.json", "base/bottleneck_report.json",
                "base/profile/mfu_bottleneck_report.md"):
        p = art / rel
        if p.is_file():
            p.unlink()
    return art, _chain_env(art)


def test_baseline_chain_mfu_mode_awaits_analyzer_then_adapts(tmp_path: Path):
    """mfu handshake (the only profiling path, v7 §3.2): with no raw products
    the chain WAITS for the mfu-analyzer subagent (running line carrying the
    dispatch parameters from contracts.json); once the raw products land, the
    re-invoked chain adapts them and proceeds to executed with makespan ==
    the raw parallel_cycles."""
    art, env = _mfu_baseline_ws(tmp_path)
    base_cmd = ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5",
                "--seed", "0", "--device", "0"]

    first = subprocess.run(base_cmd, capture_output=True, text=True,
                           timeout=60, env=env)
    payload = json.loads(first.stdout)
    assert payload["status"] == "running"
    assert "awaiting mfu-analyzer" in payload["error"]
    # the running line carries the mfu dispatch parameters from contracts.json
    assert "chip=6613" in payload["error"]
    assert "precision=INT8" in payload["error"]
    assert "core_num=1" in payload["error"]
    # no fallback profiling happened while waiting
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
        "[subagent:mfu-analyzer v2 MBA7K2]\n\n## MFU 时延瓶颈分析报告\n",
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
    assert "profile: mfu (chip=6613" in \
        (art / "baseline_status.md").read_text(encoding="utf-8")
    # let the detached finalizer reach its terminal state before tmp cleanup
    assert _wait_for(art / "baseline" / "train_final.json", timeout_s=60)


def test_baseline_chain_mfu_mode_report_without_raw_is_fatal_no_fallback(tmp_path: Path):
    """The analyzer reported (its hard rule: even a failed evaluation writes
    the report) but left no usable raw products — the chain fails loud
    pointing at the report; there is NO fallback profiling path."""
    art, env = _mfu_baseline_ws(tmp_path)
    (art / "base" / "profile" / "mfu_bottleneck_report.md").write_text(
        "[subagent:mfu-analyzer v2 MBA7K2]\n\n## MFU 时延瓶颈分析报告\n"
        "评测失败：远程服务不可达\n", encoding="utf-8")

    proc = subprocess.run(
        ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5",
         "--seed", "0", "--device", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    payload = json.loads(proc.stdout)
    assert payload["status"] == "failed"
    assert "baseline step 2" in payload["error"]
    assert "no fallback profiling path" in payload["error"]
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
         "--seed", "0", "--device", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    payload = json.loads(chain.stdout)
    assert payload["status"] == "failed"
    assert "baseline step 2" in payload["error"]
    assert "mfu_adapter failed" in payload["error"]
    assert "output_memory" in payload["error"]
    assert not (art / "base" / "profile" / "profile_summary.json").exists()


def test_baseline_chain_device_argument_is_required_and_parks_when_locked(tmp_path: Path):
    """v7 §4.4: the chain claims the AGENT-CHOSEN card — --device is a
    required argument (missing -> usage error naming the probe-first flow),
    and an already-locked idx PARKS (stdout running with the re-probe
    guidance) instead of failing loud."""
    art, env = _mfu_baseline_ws(tmp_path)

    # missing --device -> rc 2 with the probe-first guidance
    proc = subprocess.run(
        ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5",
         "--seed", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 2
    assert "--device" in proc.stderr
    assert "probe" in proc.stderr

    # non-numeric idx -> usage error
    proc_bad = subprocess.run(
        ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5",
         "--seed", "0", "--device", "gpu0"],
        capture_output=True, text=True, timeout=60, env=env)
    assert proc_bad.returncode == 2

    # a foreign lock on the chosen idx -> the chain PARKS with the reason
    (art / "devices").mkdir(exist_ok=True)
    (art / "devices" / "0.lock").write_text(json.dumps(
        {"vid": "r9-99", "pid": 424242, "acquired_at": "now",
         "backend": "cuda"}), encoding="utf-8")
    # put the early chain products back so the run reaches step 4
    shutil.copy(_SCRIPTS / "mfu_adapter.py", art / "scripts" / "mfu_adapter.py")
    proc2 = subprocess.run(
        ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5",
         "--seed", "0", "--device", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    # (no raw mfu products in this fixture -> the run may park at step 2
    # instead; force the step-4 path by seeding the raw products)
    if json.loads(proc2.stdout)["status"] == "running" and \
            "awaiting mfu-analyzer" in proc2.stdout:
        bproc = subprocess.run(
            [sys.executable, str(_SCRIPTS / "mfu_benchmark.py"),
             str(art / "base" / "model.onnx"),
             "--chip", "6613", "--precision", "INT8", "--core-num", "1",
             "-o", str(art / "base" / "profile"), "--timeout", "60"],
            capture_output=True, text=True, timeout=120, env=env)
        assert bproc.returncode == 0, bproc.stderr
        proc2 = subprocess.run(
            ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5",
             "--seed", "0", "--device", "0"],
            capture_output=True, text=True, timeout=60, env=env)
    payload = json.loads(proc2.stdout)
    assert payload["status"] == "running"
    assert "already locked" in payload["error"]
    assert "re-run device_alloc probe" in payload["error"]
    # nothing was launched while parked
    assert not (art / "baseline" / "train.pid").exists()


def test_baseline_chain_profile_block_is_the_dispatch_source(tmp_path: Path):
    """v7: the mfu dispatch parameters live in contracts.json's profile
    block — a missing block fails at startup (never guessed, never sniffed)."""
    art, env = _mfu_baseline_ws(tmp_path)
    contracts = json.loads((art / "contracts.json").read_text(encoding="utf-8"))
    contracts.pop("profile")
    (art / "contracts.json").write_text(json.dumps(contracts), encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(_BASELINE_SH), "--latency-reduction-min", "0.5",
         "--seed", "0", "--device", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 2
    assert "profile" in proc.stderr


def test_check_baseline_docs_gate(tmp_path: Path):
    """v7 §4.3: the THREE-document gate (sentinel + sections per doc). The
    fixture documents pass; each violation class (doc missing, section
    missing, section empty, wrong sentinel) fails naming what failed."""
    art = tmp_path / "art"
    (art / "baseline").mkdir(parents=True)
    (art / "base" / "profile").mkdir(parents=True)
    (art / "baseline" / "business_logic.md").write_text(_BL_MD, encoding="utf-8")
    (art / "base" / "information_analysis.md").write_text(_IX_MD, encoding="utf-8")
    (art / "base" / "profile" / "mfu_bottleneck_report.md").write_text(
        _MFU_MD, encoding="utf-8")
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    sh = _REPO / "workflows" / "prof-opt" / "agents" / "po_baseline" / "scripts" / "check_baseline_docs.sh"

    def run():
        return subprocess.run(["bash", str(sh)], capture_output=True, text=True,
                              timeout=30, env=env)

    assert run().returncode == 0, run().stderr

    # one of the three missing (each is named)
    (art / "base" / "information_analysis.md").unlink()
    proc = run()
    assert proc.returncode == 1
    assert "information_analysis.md" in proc.stderr
    (art / "base" / "information_analysis.md").write_text(_IX_MD, encoding="utf-8")

    # a present but EMPTY file is the same rejection as a missing one
    (art / "base" / "information_analysis.md").write_text("", encoding="utf-8")
    proc = run()
    assert proc.returncode == 1
    assert "information_analysis.md" in proc.stderr
    (art / "base" / "information_analysis.md").write_text(_IX_MD, encoding="utf-8")

    # missing section
    doc = art / "baseline" / "business_logic.md"
    doc.write_text(_BL_MD.replace("## 训练目标与指标方向\nacc higher\n", ""),
                   encoding="utf-8")
    proc = run()
    assert proc.returncode == 1
    assert "训练目标与指标方向" in proc.stderr
    doc.write_text(_BL_MD, encoding="utf-8")

    # empty section (bare heading, no body)
    doc.write_text(_BL_MD.replace("per module", ""), encoding="utf-8")
    proc = run()
    assert proc.returncode == 1
    assert "逐模块职责与物理意义" in proc.stderr
    doc.write_text(_BL_MD, encoding="utf-8")

    # wrong sentinel on the mfu report (v2 sentinel — document not authored
    # by the subagent, or a stale v1 report from an old workspace)
    mfu = art / "base" / "profile" / "mfu_bottleneck_report.md"
    mfu.write_text(_MFU_MD.replace("v2 MBA7K2", "v1 MBA7K2"), encoding="utf-8")
    proc = run()
    assert proc.returncode == 1
    assert "sentinel" in proc.stderr
    mfu.write_text(_MFU_MD, encoding="utf-8")

    # the information doc's four sections are enforced too
    ix = art / "base" / "information_analysis.md"
    ix.write_text(_IX_MD.replace("## 创新结构方向\nat least one substantive direction\n", ""),
                  encoding="utf-8")
    proc = run()
    assert proc.returncode == 1
    assert "创新结构方向" in proc.stderr


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
        ["bash", str(_REUSE_SH), "model.py", "1"],
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
        ["bash", str(_REUSE_SH), "model.py", "0"],
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
        ["bash", str(_REUSE_SH), "model.py", "0"],
        capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 3
    assert "owned by another live run" in proc.stderr
    assert json.loads((art / ".run_lock").read_text(encoding="utf-8"))["run_id"] \
        == "other-live-run"   # not taken over, not refreshed


def _write_baseline_lock(art: Path) -> None:
    """A BASELINE.lock (v7 schema) whose py_files_sha256 anchors the CURRENT
    shadow tree (what Step 3 of po_flatten writes)."""
    import hashlib
    shadow = art / "shadow"
    py = {str(p.relative_to(shadow)).replace("\\", "/"):
          hashlib.sha256(p.read_bytes()).hexdigest()
          for p in sorted(shadow.rglob("*.py"))}
    (art / "BASELINE.lock").write_text(
        json.dumps({"version": 2, "model_path": "model.py",
                    "py_files_sha256": py}), encoding="utf-8")


def _reusable_ws(tmp_path: Path) -> Path:
    """Minimal workspace whose BASELINE.lock fully matches the shadow tree and
    whose reuse products are complete — the state a healthy zero-promotion
    second run arrives at the gate with (v7: no profile_mode.json anywhere —
    the profiling configuration lives in contracts.json, checked by the
    contract stage's own reuse gate)."""
    art = tmp_path / "art"
    art.mkdir()
    (art / "shadow" / "pkg").mkdir(parents=True)
    (art / "shadow" / "pkg" / "model.py").write_text("# model v0\n", encoding="utf-8")
    (art / "project_manifest.md").write_text("# manifest\n", encoding="utf-8")
    (art / "readiness").mkdir()
    (art / "readiness" / "readiness.json").write_text(
        json.dumps({"constructible": True, "exportable": True,
                    "definition_located": True}),
        encoding="utf-8")
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
        ["bash", str(_REUSE_SH), "model.py", "0"],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 0, proc.stderr
    assert "REUSE" in proc.stdout
    assert "unreadable" not in proc.stderr


def test_reuse_check_mismatch_is_anchor_change_guidance(tmp_path: Path):
    """The readable-but-mismatched state is DESIGN BEHAVIOR when the anchor
    truly changed: in v6 the base shadow never moves forward (there is no
    promotion history), so a shadow that drifted from BASELINE.lock means
    the model/ckpt anchor itself changed. The failure copy must say exactly
    that and point at fresh_start=true — not a cryptic anchor error."""
    art = _reusable_ws(tmp_path)
    # the shadow model drifted from what the lock anchors
    (art / "shadow" / "pkg" / "model.py").write_text(
        "# model v1 (changed)\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "0"],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 3
    assert "does not match" in proc.stderr
    assert "never moves forward" in proc.stderr
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
        ["bash", str(_REUSE_SH), "model.py", "0"],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 3
    assert "unreadable/corrupt" in proc.stderr
    assert "real error" in proc.stderr
    assert "fresh_start" not in proc.stderr
    assert "promotion history" not in proc.stderr


@pytest.mark.parametrize("corrupt_body", [
    "[]",                                            # top level not an object
    '{"version": 2, "model_path": "model.py", "py_files_sha256": null}',
    # anchor map not a mapping
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
        ["bash", str(_REUSE_SH), "model.py", "0"],
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
        json.dumps({"version": 2, "model_path": "model.py"}), encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "0"],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 3
    assert "does not match" in proc.stderr
    assert "fresh_start=true" in proc.stderr
    assert "unreadable/corrupt" not in proc.stderr


def test_reuse_check_arity_rejects_third_positional(tmp_path: Path):
    """The ckpt arg is RETIRED (v7 F2): a stale 3-arg invocation (the pre-v7
    call form) is a usage error (exit 2), never a silently-ignored extra."""
    art = _reusable_ws(tmp_path)
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "0", ""],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 2
    assert "unexpected extra argument" in proc.stderr


def test_reuse_check_old_schema_lock_needs_fresh_start(tmp_path: Path):
    """v7 §14: a lock that predates the v7 schema (no version field — the
    v6 ckpt-anchor form) fails the gate as a mismatch with the fresh_start
    hint; old workspaces are NEVER silently migrated."""
    art = _reusable_ws(tmp_path)
    import hashlib
    shadow = art / "shadow"
    py = {str(p.relative_to(shadow)).replace("\\", "/"):
          hashlib.sha256(p.read_bytes()).hexdigest()
          for p in sorted(shadow.rglob("*.py"))}
    (art / "BASELINE.lock").write_text(
        json.dumps({"model_path": "model.py", "pretrained_ckpt": "",
                    "ckpt_sha256": "", "py_files_sha256": py}),
        encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "0"],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 3
    assert "version" in proc.stderr
    assert "fresh_start=true" in proc.stderr
    assert "unreadable" not in proc.stderr


def test_reuse_check_no_profile_mode_file_exists(tmp_path: Path):
    """v7 §3.1 deletes the whole profile_mode.json mechanism: the gate
    neither reads nor writes it, and a healthy reusable workspace carries
    none (the profiling config is a workflow INPUT recorded in
    contracts.json)."""
    art = _reusable_ws(tmp_path)
    proc = subprocess.run(
        ["bash", str(_REUSE_SH), "model.py", "0"],
        capture_output=True, text=True, timeout=60, env=_reuse_env(art))
    assert proc.returncode == 0
    assert not (art / "profile_mode.json").exists()
    assert "profile_mode" not in proc.stderr


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


# ── v4 shared layer: gate_node syntax, metric_curve@k ────────────────────────

def test_gate_node_sh_parses_after_quote_fix():
    """D-V4-15: the --max-rounds argument had a transposed quote/paren
    (`"$MAXR)"` instead of `"$MAXR")"`) — the whole wrapper failed bash -n,
    so every gate decision fell to the hardcoded fallback emitter. v7 adds
    the --idle-round-cap knob with the same quoting discipline."""
    proc = subprocess.run(["bash", "-n", str(_SCRIPTS / "gate_node.sh")],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    src = (_SCRIPTS / "gate_node.sh").read_text(encoding="utf-8")
    # both knobs are quoted correctly and the command substitution closes
    # OUTSIDE the quotes (the D-V4-15 disease)
    assert '--idle-round-cap "$IDLE_CAP")"' in src


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


# ── push_curves v6 (§10): top-10 selection / pareto / docs manifest ───────────

def _po_variant(art: Path, vid: str, *, curve=None, train_status=None,
                shard=None, verdict=None, docs=()) -> None:
    """Seed one variant directory's v6 state (P3 shapes: train_status.json /
    ledger_entry.json shard / verdict.json / metric curve / analysis docs)."""
    vdir = art / "variants" / vid
    vdir.mkdir(parents=True, exist_ok=True)
    if curve is not None:
        (vdir / "metrics").mkdir(parents=True, exist_ok=True)
        (vdir / "metrics" / "metrics.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in curve), encoding="utf-8")
    if train_status is not None:
        (vdir / "train_status.json").write_text(
            json.dumps(train_status), encoding="utf-8")
    if shard is not None:
        (vdir / "ledger_entry.json").write_text(
            json.dumps(shard), encoding="utf-8")
    if verdict is not None:
        (vdir / "verdict.json").write_text(
            json.dumps(verdict), encoding="utf-8")
    for doc in docs:
        target = vdir / doc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"[sentinel] {vid} {doc}\n", encoding="utf-8")


def _po_ws(tmp_path: Path) -> Path:
    art = tmp_path / "art"
    (art / "baseline").mkdir(parents=True)
    (art / "baseline" / "baseline_metrics.jsonl").write_text(
        '{"epoch": 1, "metric": 0.4}\n', encoding="utf-8")
    return art


def test_push_curves_top10_selection_strategy(tmp_path: Path):
    """§10.1 three-branch selection: ① in-flight by most recent update first,
    ② terminal success next, ③ the rest by ascending gap (null last, vid as
    the deterministic tiebreak) — baseline always present, cap at 9 variants,
    the on-disk curve files beyond the cap are simply not pushed."""
    art = _po_ws(tmp_path)
    curve = [{"epoch": 1, "metric": 0.5}]
    # tier 1 — in-flight (curve + no terminal state), newer ts first
    _po_variant(art, "r1-old", curve=curve,
                train_status={"vid": "r1-old", "stage": "training",
                              "ts": "2026-08-31T10:00:00+00:00"},
                shard={"vid": "r1-old", "status": "training"})
    _po_variant(art, "r2-new", curve=curve,
                train_status={"vid": "r2-new", "stage": "training",
                              "ts": "2026-08-31T11:00:00+00:00"},
                shard={"vid": "r2-new", "status": "training"})
    # tier 2 — terminal success
    _po_variant(art, "r3-succ", curve=curve,
                train_status={"vid": "r3-succ", "stage": "done"},
                shard={"vid": "r3-succ", "status": "success", "gap": 0.01})
    # tier 3 — terminal non-success by ascending gap, then null-gap by vid
    for vid, gap in (("r4-fail-small", 0.10), ("r5-fail-mid", 0.20),
                     ("r6-fail-big", 0.30)):
        _po_variant(art, vid, curve=curve,
                    train_status={"vid": vid, "stage": "killed"},
                    shard={"vid": vid, "status": "accuracy_fail", "gap": gap})
    for vid in ("r7-fail-nul", "r8-fail-nul", "r10-fail-nul", "r11-fail-nul"):
        _po_variant(art, vid, curve=curve,
                    train_status={"vid": vid, "stage": "failed"},
                    shard={"vid": vid, "status": "probe_insufficient",
                           "gap": None})
    # latency_pass but no training yet -> NO curve -> never selected
    _po_variant(art, "r9-wait",
                shard={"vid": "r9-wait", "status": "latency_pass"},
                verdict={"vid": "r9-wait", "makespan_cycles": 900,
                         "outcome": "latency_pass"})
    # 10 variant curves, cap 9: the LAST tier-3 null-gap entry drops (r8)
    sock = tmp_path / "chart.sock"
    thread, messages = _chart_server(sock, replies=1)
    proc = _push(art, sock)
    assert proc.returncode == 0, proc.stderr
    thread.join(timeout=10)
    payload = messages[0]["payload"]
    assert payload["chart_type"] == "line"
    vids = {row["vid"] for row in payload["data"]}
    assert "baseline" in vids                                  # §10.1 always
    assert "r9-wait" not in vids                        # no curve -> no line
    assert "r8-fail-nul" not in vids                    # beyond the 9 cap
    assert vids == {"baseline", "r1-old", "r2-new", "r3-succ", "r4-fail-small",
                    "r5-fail-mid", "r6-fail-big", "r7-fail-nul", "r10-fail-nul",
                    "r11-fail-nul"}
    # audit reflects exactly the pushed set (full files stay on disk)
    audit = json.loads((art / ".chart_push.log").read_text(
        encoding="utf-8").splitlines()[0])
    assert {c["vid"] for c in audit["curves"]} == vids
    # the dropped curve file is untouched on disk (盘面全量保留)
    assert (art / "variants" / "r8-fail-nul" / "metrics" /
            "metrics.jsonl").is_file()


def test_push_curves_pareto_payload(tmp_path: Path):
    """§10.2 every variant one point: x = reduction vs the origin-anchor
    baseline makespan (negative = slower), y = final gap (metric fallback,
    null = the 达线未训 placeholder), status-colored, directions pinned."""
    art = _po_ws(tmp_path)
    (art / "base").mkdir()
    (art / "base" / "origin_anchor.json").write_text(json.dumps(
        {"baseline_makespan_cycles": 1000, "target_cycles": 500,
         "accuracy_budget": 0.1}), encoding="utf-8")
    _po_variant(art, "r1-01", curve=[{"epoch": 1, "metric": 0.38}],
                train_status={"vid": "r1-01", "stage": "done"},
                shard={"vid": "r1-01", "status": "success", "gap": 0.02,
                       "metric": 0.42},
                verdict={"vid": "r1-01", "makespan_cycles": 800,
                         "outcome": "latency_pass"})
    _po_variant(art, "r2-01", curve=[{"epoch": 1, "metric": 0.45}],
                train_status={"vid": "r2-01", "stage": "training"},
                shard={"vid": "r2-01", "status": "training", "gap": None,
                       "metric": 0.45},
                verdict={"vid": "r2-01", "makespan_cycles": 1200,
                         "outcome": "latency_pass"})
    # 达线未训: seeded shard only — y must stay null and be disclosed
    _po_variant(art, "r3-01",
                shard={"vid": "r3-01", "status": "latency_pass", "gap": None,
                       "metric": None},
                verdict={"vid": "r3-01", "makespan_cycles": 900,
                         "outcome": "latency_pass"})
    # no verdict yet (mid-measurement) -> no x -> not plottable, no point
    _po_variant(art, "r4-01", curve=[{"epoch": 1, "metric": 0.5}],
                train_status={"vid": "r4-01", "stage": "training"},
                shard={"vid": "r4-01", "status": "training"})
    sock = tmp_path / "chart.sock"
    thread, messages = _chart_server(sock, replies=2)   # line + pareto
    proc = _push(art, sock)
    assert proc.returncode == 0, proc.stderr
    thread.join(timeout=10)
    by_type = {m["payload"]["chart_type"]: m["payload"] for m in messages}
    pareto = by_type["pareto"]
    assert pareto["label"] == "prof-opt/pareto"
    assert pareto["pareto_x_direction"] == "max"
    assert pareto["pareto_y_direction"] == "min"
    assert pareto["color"] == "color"                    # per-row status color
    points = {row["vid"]: row for row in pareto["data"]}
    assert set(points) == {"r1-01", "r2-01", "r3-01"}    # 全量变体一个点
    assert points["r1-01"]["x"] == 20.0                  # 1 - 800/1000
    assert points["r1-01"]["y"] == 0.02
    assert points["r1-01"]["status"] == "success"
    assert points["r2-01"]["x"] == -20.0                 # slower than baseline
    assert points["r2-01"]["y"] == 0.45            # metric fallback (no gap)
    assert points["r2-01"]["status"] == "in-flight"
    assert points["r3-01"]["y"] is None                  # 达线未训占位
    assert points["r3-01"]["status"] == "latency_pass"
    assert "null" in pareto["caption"]                   # disclosed, not silent
    assert all(isinstance(row["color"], str) and row["color"].startswith("#")
               for row in pareto["data"])


def test_push_curves_docs_manifest_whitelist_and_columns(tmp_path: Path):
    """§10.4 the docs table: canonical columns vid/doc/status/path
    (+updated_at), paths ONLY from the constructed artifacts-relative
    whitelist, every listed file exists, eliminated variants stay listed
    (web §3.3), rounds + rules snapshot ride along (S-9)."""
    art = _po_ws(tmp_path)
    for rel in ("baseline/business_logic.md", "base/information_analysis.md",
                "base/profile/mfu_bottleneck_report.md",
                "rounds/001/analysis.md", "rounds/002/analysis.md",
                "rounds/notes/analysis.md",          # non-numeric round dir
                "base/accuracy_rules_snapshot.json"):
        target = art / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("doc\n", encoding="utf-8")
    _po_variant(art, "r1-01",
                train_status={"vid": "r1-01", "stage": "done"},
                shard={"vid": "r1-01", "status": "success"},
                docs=("assessment.md",
                      "profile/mfu_bottleneck_report.md"))
    _po_variant(art, "r2-01",     # eliminated — its docs STAY listed (web §3.3)
                verdict={"vid": "r2-01", "makespan_cycles": 900,
                         "outcome": "latency_fail"},
                docs=("assessment.md",))
    sock = tmp_path / "chart.sock"
    thread, messages = _chart_server(sock, replies=2)   # line + docs (no pareto anchor)
    proc = _push(art, sock, "--docs")
    assert proc.returncode == 0, proc.stderr
    thread.join(timeout=10)
    by_type = {m["payload"]["chart_type"]: m["payload"] for m in messages}
    # no baseline-makespan anchor on this workspace -> the pareto chart is
    # SKIPPED (only line + docs pushed), never fabricated
    assert "pareto" not in by_type
    docs = by_type["table"]
    assert docs["label"] == "prof-opt/docs"
    assert docs["columns"][:4] == ["vid", "doc", "status", "path"]
    rows = {row["path"]: row for row in docs["data"]}
    assert "variants/r1-01/assessment.md" in rows
    assert "variants/r2-01/assessment.md" in rows      # 淘汰变体保留
    assert "baseline/business_logic.md" in rows
    assert "base/information_analysis.md" in rows
    assert "base/profile/mfu_bottleneck_report.md" in rows
    assert "rounds/001/analysis.md" in rows and "rounds/002/analysis.md" in rows
    assert "base/accuracy_rules_snapshot.json" in rows     # S-9 rules source
    assert rows["variants/r2-01/assessment.md"]["status"] == "latency_fail"
    assert rows["variants/r1-01/assessment.md"]["status"] == "success"
    assert rows["base/accuracy_rules_snapshot.json"]["vid"] == "rules"
    # whitelist invariants: every path is artifacts-relative, traverses
    # nothing, and resolves to a file that exists inside the artifacts root
    for path, row in rows.items():
        assert not Path(path).is_absolute() and ".." not in Path(path).parts
        assert (art / path).is_file()
        assert set(row) == {"vid", "doc", "status", "path", "updated_at"}
    assert "rounds/notes/analysis.md" not in rows          # non-numeric round
    # idempotent replace (web §2.3): a second push carries the identical
    # label+title+data — the front end REPLACES, never duplicates
    thread2, messages2 = _chart_server(sock, replies=2)
    proc2 = _push(art, sock, "--docs")
    assert proc2.returncode == 0, proc2.stderr
    thread2.join(timeout=10)
    docs2 = {m["payload"]["chart_type"]: m["payload"] for m in messages2}["table"]
    assert docs2["title"] == docs["title"]
    assert docs2["data"] == docs["data"]


def test_push_curves_w3_joint_three_charts_idempotent(tmp_path: Path):
    """W3-T1 联调（web §6.3-1 后端侧）：一次 ``--docs`` 推送三图齐全——line
    （§10.1 top-10）/ pareto（§10.2 全量，y=null 占位**保持 null 不伪造 0**——前端
    W-P3 修正后按 null 剔除渲染，0 会让占位点画在 0 位）/ docs（§10.4 canonical
    列）；同工作区二次推送三图 label+title+data 逐字一致（幂等替换契约：前端按
    label+title 替换不复制——pareto 的幂等此前未覆盖）。"""
    art = _po_ws(tmp_path)
    (art / "base").mkdir()
    (art / "base" / "origin_anchor.json").write_text(json.dumps(
        {"baseline_makespan_cycles": 1000}), encoding="utf-8")
    _po_variant(art, "r1-01", curve=[{"epoch": 1, "metric": 0.38}],
                train_status={"vid": "r1-01", "stage": "done"},
                shard={"vid": "r1-01", "status": "success", "gap": 0.02},
                verdict={"vid": "r1-01", "makespan_cycles": 800,
                         "outcome": "latency_pass"},
                docs=("assessment.md",))
    # 达线未训: y stays null — the placeholder the front end must NOT plot at 0
    _po_variant(art, "r2-01",
                shard={"vid": "r2-01", "status": "latency_pass", "gap": None,
                       "metric": None},
                verdict={"vid": "r2-01", "makespan_cycles": 900,
                         "outcome": "latency_pass"},
                docs=("assessment.md",))
    sock = tmp_path / "chart.sock"
    pushed_charts = []
    for _ in range(2):
        thread, messages = _chart_server(sock, replies=3)  # line+pareto+docs
        proc = _push(art, sock, "--docs")
        assert proc.returncode == 0, proc.stderr
        thread.join(timeout=10)
        assert len(messages) == 3
        by_type = {m["payload"]["chart_type"]: m["payload"] for m in messages}
        assert set(by_type) == {"line", "pareto", "table"}
        # the three labels the front end keys its replace semantics on
        assert by_type["line"]["label"] == "prof-opt/curves"
        assert by_type["pareto"]["label"] == "prof-opt/pareto"
        assert by_type["table"]["label"] == "prof-opt/docs"
        # §10.2: the 达线未训 placeholder survives as a real null (never 0)
        points = {row["vid"]: row for row in by_type["pareto"]["data"]}
        assert points["r2-01"]["y"] is None
        assert points["r1-01"]["y"] == 0.02
        # §10.4 canonical columns carried verbatim
        assert by_type["table"]["columns"] == ["vid", "doc", "status", "path",
                                               "updated_at"]
        pushed_charts.append(by_type)
    # idempotent replace: the second push of an unchanged workspace is
    # byte-identical on label+title+data for ALL THREE charts
    for ctype in ("line", "pareto", "table"):
        assert pushed_charts[1][ctype]["label"] == pushed_charts[0][ctype]["label"]
        assert pushed_charts[1][ctype]["title"] == pushed_charts[0][ctype]["title"]
        assert pushed_charts[1][ctype]["data"] == pushed_charts[0][ctype]["data"]


def test_push_curves_recency_and_anchor_fallbacks(tmp_path: Path):
    """Unit pins for the deterministic fallbacks: _recency falls back to the
    curve file's mtime when the watchdog ts is absent or garbage, and
    _baseline_makespan walks the frozen-authority chain (origin anchor ->
    bottleneck report -> profile summary) and refuses to fabricate an anchor
    when none is on disk."""
    from datetime import datetime
    curve = tmp_path / "metrics.jsonl"
    curve.write_text('{"epoch": 1, "metric": 0.5}\n', encoding="utf-8")
    os.utime(curve, (1700000000, 1700000000))
    assert push_curves._recency({"ts": ""}, curve) == 1700000000.0
    assert push_curves._recency({"ts": "garbage"}, curve) == 1700000000.0
    assert push_curves._recency(
        {"ts": "2026-08-31T11:00:00+00:00"}, curve) == \
        datetime.fromisoformat("2026-08-31T11:00:00+00:00").timestamp()

    art = tmp_path / "art2"
    (art / "base" / "profile").mkdir(parents=True)
    assert push_curves._baseline_makespan(art) is None      # no fabrication
    (art / "base" / "profile" / "profile_summary.json").write_text(
        json.dumps({"makespan_cycles": 300}), encoding="utf-8")
    assert push_curves._baseline_makespan(art) == 300.0
    (art / "base" / "bottleneck_report.json").write_text(
        json.dumps({"makespan_cycles": 350}), encoding="utf-8")
    assert push_curves._baseline_makespan(art) == 350.0
    (art / "base" / "origin_anchor.json").write_text(
        json.dumps({"baseline_makespan_cycles": 712}), encoding="utf-8")
    assert push_curves._baseline_makespan(art) == 712.0     # frozen authority


def test_push_curves_variant_state_verdict_only(tmp_path: Path):
    """Without a ledger shard the verdict alone still drives the status —
    one outcome source for both the terminal and the 达线未训 branch."""
    vdir = tmp_path / "r1-01"
    vdir.mkdir()
    assert push_curves._variant_state(vdir) == {
        "vid": "r1-01", "terminal": False, "status": "in-flight",
        "gap": None, "metric": None, "ts": "", "makespan": None}
    (vdir / "verdict.json").write_text(json.dumps(
        {"vid": "r1-01", "makespan_cycles": 900,
         "outcome": "latency_fail"}), encoding="utf-8")
    state = push_curves._variant_state(vdir)
    assert state["terminal"] and state["status"] == "latency_fail"
    assert state["makespan"] == 900
    (vdir / "verdict.json").write_text(json.dumps(
        {"vid": "r1-01", "makespan_cycles": 900,
         "outcome": "latency_pass"}), encoding="utf-8")
    state = push_curves._variant_state(vdir)
    assert not state["terminal"] and state["status"] == "latency_pass"


# ── dashboard_snapshot v6 (§4.2/§7.5-②): aggregate-then-read + new fields ────

def test_dashboard_snapshot_aggregates_and_exposes_v6_fields(tmp_path: Path):
    """The snapshot FIRST re-runs the ledger aggregator (§7.5 trigger ② — the
    read path keeps the derived ledger convergent) and surfaces the §4.2
    fields: status / latest_epoch / latest_metric / gap / device /
    change_summary."""
    art = tmp_path / "art"
    (art / "baseline").mkdir(parents=True)
    (art / "variants" / "r1-01").mkdir(parents=True)
    (art / "baseline" / "ledger_entry.json").write_text(json.dumps(
        {"vid": "baseline", "status": "done", "epoch": 30, "metric": 0.9,
         "gap": None, "device": 0, "change_summary": None,
         "ts": "2026-08-31T00:00:00+00:00"}), encoding="utf-8")
    (art / "variants" / "r1-01" / "ledger_entry.json").write_text(json.dumps(
        {"vid": "r1-01", "status": "success", "epoch": 30, "metric": 0.88,
         "gap": 0.02, "device": 1, "change_summary": "gelu->relu blocks.0",
         "ts": "2026-08-31T01:00:00+00:00"}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "dashboard_snapshot.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    # §7.5 trigger ②: the derived ledger was rebuilt from the shards
    ledger = json.loads((art / "experiment_ledger.json").read_text(
        encoding="utf-8"))
    assert [r["vid"] for r in ledger["rows"]] == ["baseline", "r1-01"]
    data = json.loads((art / "dashboard.json").read_text(encoding="utf-8"))
    # v7 §12: the retired best.json read/field is gone (schema bumped)
    assert data["schema_version"] == 3
    assert "best" not in data
    rows = {r["vid"]: r for r in data["variants"]}
    v = rows["r1-01"]
    assert v["status"] == "success"
    assert v["latest_epoch"] == 30
    assert v["latest_metric"] == 0.88
    assert v["gap"] == 0.02
    assert v["device"] == 1
    assert v["change_summary"] == "gelu->relu blocks.0"
    html = (art / "dashboard.html").read_text(encoding="utf-8")
    assert "Latest epoch" in html and "Change summary" in html


def test_dashboard_snapshot_fails_loud_on_torn_shard(tmp_path: Path):
    """A torn shard is a real anomaly (single-writer files) — the snapshot
    fails loud (exit 2) instead of rendering a silently stale ledger."""
    art = tmp_path / "art"
    (art / "variants" / "r1-01").mkdir(parents=True)
    (art / "variants" / "r1-01" / "ledger_entry.json").write_text(
        '{"torn', encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "dashboard_snapshot.py"),
         "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "FAIL" in proc.stderr
    assert not (art / "dashboard.json").exists()


# ── run_latency_recheck (v7): mfu-only measurement, check_verdict predicate ──

_RECHECK_SH = _REPO / "workflows" / "prof-opt" / "agents" / "po_propose" / "scripts" / "run_latency_recheck.sh"


def _recheck_workspace(tmp_path: Path) -> tuple[Path, dict]:
    """GELU->ReLU variant fixture on the ONE mfu path: base + variant
    four-pieces produced through the real mfu_benchmark + adapter pair (the
    same chain the node drives). The anchor's target is set to the VARIANT's
    measured makespan so the boundary is exactly inclusive on the happy
    path. Returns (art, env)."""
    torch = pytest.importorskip("torch")
    pytest.importorskip("onnx")

    art = tmp_path / "art"
    (art / "scripts").mkdir(parents=True)
    for src in ("diff_check.py", "history_lib.py", "emit_result.py",
                "round_state.py", "check_verdict.py", "mfu_benchmark.py",
                "mfu_adapter.py"):
        shutil.copy(_SCRIPTS / src, art / "scripts" / src)
    (art / "base" / "profile").mkdir(parents=True)

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

    def measure(model, out_dir: Path) -> int:
        """The node's Step 3 chain: mfu_benchmark -> adapter; returns the
        canonical parallel makespan."""
        onnx_path = out_dir.parent / (out_dir.name + "_model.onnx")
        out_dir.mkdir(parents=True, exist_ok=True)
        export(model, onnx_path)
        proc = subprocess.run(
            [sys.executable, str(_SCRIPTS / "mfu_benchmark.py"), str(onnx_path),
             "--chip", "6613", "--precision", "INT8", "--core-num", "1",
             "-o", str(out_dir), "--timeout", "60"],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        subprocess.run([sys.executable, str(_SCRIPTS / "mfu_adapter.py"),
                        "--profile-dir", str(out_dir)],
                       capture_output=True, text=True, timeout=60, check=True)
        return json.loads(proc.stdout)["parallel_cycles"]

    torch.manual_seed(0)
    (art / "base").mkdir(exist_ok=True)
    base_ms = measure(Tiny(torch.nn.GELU()), art / "base" / "profile")
    variant_ms = measure(Tiny(torch.nn.ReLU()),
                         art / "variants" / "r1-01" / "profile")

    # anchor target = the variant's measured makespan -> the gate boundary is
    # exactly inclusive (variant == target HOLDS)
    (art / "base" / "origin_anchor.json").write_text(json.dumps({
        "baseline_makespan_cycles": base_ms,
        "latency_reduction_min": 0.5, "accuracy_budget": 0.1,
        "target_cycles": variant_ms, "frozen_at_round": 0}), encoding="utf-8")

    torch.manual_seed(0)
    export(Tiny(torch.nn.GELU()), art / "base" / "model.onnx")
    (art / "shadow" / "pkg").mkdir(parents=True)
    (art / "shadow" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    vdir = art / "variants" / "r1-01"
    (vdir / "onnx").mkdir(parents=True, exist_ok=True)
    shutil.copytree(art / "shadow", vdir / "shadow")
    torch.manual_seed(0)
    export(Tiny(torch.nn.ReLU()), vdir / "onnx" / "model.onnx")
    # the GELU->ReLU swap's real onnx op delta — the declaration must match
    # the graphs or the recheck's graph layer correctly flags a mismatch
    t8_op_delta = {"Add": -1, "Div": -1, "Erf": -1, "Mul": -2, "Relu": 1}
    (vdir / "declaration.json").write_text(json.dumps({
        "vid": "r1-01", "edited_files": [], "op_delta": t8_op_delta,
        "predicted_delta_cycles": -144}), encoding="utf-8")
    (vdir / "DONE").write_text("", encoding="utf-8")
    history_lib.append_implemented(
        art / "history.jsonl", "r1-01", round=1, seq=1, parent_vid=None,
        change_sig="activation:gelu->relu:r1-01", probe_epochs=1,
        target_modules=["act"], predicted_delta_cycles=-144,
        base_at_proposal={"vid": None, "makespan_cycles": base_ms})
    (art / "rounds" / "001").mkdir(parents=True)
    (art / "contracts.json").write_text(json.dumps(
        {"interpreter": {"sys_executable": sys.executable}}), encoding="utf-8")
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(art)
    return art, env


def test_run_latency_recheck_mfu_fail_loud_matrix(tmp_path: Path):
    """v7: there is exactly ONE measurement source — a DONE variant without
    the mfu four-piece is a hard error naming the variant and the remedy
    (dispatch mfu-analyzer + adapter); no inline profiling path exists."""
    art, env = _recheck_workspace(tmp_path)
    # wipe the variant's four-piece but keep its DONE marker
    shutil.rmtree(art / "variants" / "r1-01" / "profile")
    proc = subprocess.run(["bash", str(_RECHECK_SH)],
                          capture_output=True, text=True, timeout=300, env=env)
    assert proc.returncode == 2
    assert "r1-01" in proc.stderr
    assert "no inline profiling path" in proc.stderr
    assert not (art / "variants" / "r1-01" / "verdict.json").exists()


def test_run_latency_recheck_gate_passes_at_inclusive_boundary(tmp_path: Path):
    """The happy path with the boundary exactly AT the target: the gate is
    check_verdict.py's inclusive comparison (== target HOLDS) — the verdict
    lands latency_pass with the measured makespan recorded verbatim."""
    art, env = _recheck_workspace(tmp_path)
    proc = subprocess.run(["bash", str(_RECHECK_SH)],
                          capture_output=True, text=True, timeout=300, env=env)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "executed"
    assert payload["latency_pass_count"] == 1
    verdict = json.loads((art / "variants" / "r1-01" / "verdict.json")
                         .read_text(encoding="utf-8"))
    assert verdict["outcome"] == "latency_pass"
    assert verdict["latency_gate"] == "pass"
    # the gate number is the mfu four-piece's parallel makespan, verbatim
    summary = json.loads((art / "variants" / "r1-01" / "profile" /
                          "profile_summary.json").read_text(encoding="utf-8"))
    assert verdict["makespan_cycles"] == summary["makespan_cycles"]
    # the ONE predicate agrees (recheck and probe emit share it)
    check = subprocess.run(
        [sys.executable, str(art / "scripts" / "check_verdict.py"),
         "--vid", "r1-01", "--artifacts", str(art)],
        capture_output=True, text=True, timeout=60, env=env)
    assert check.returncode == 0, check.stderr
    assert json.loads(check.stdout)["ok"] is True

    # a target one BELOW the measurement flips the verdict to latency_fail
    anchor = json.loads((art / "base" / "origin_anchor.json").read_text(encoding="utf-8"))
    anchor["target_cycles"] = verdict["makespan_cycles"] - 1
    (art / "base" / "origin_anchor.json").write_text(json.dumps(anchor),
                                                     encoding="utf-8")
    (art / "variants" / "r1-01" / "verdict.json").unlink()
    proc2 = subprocess.run(["bash", str(_RECHECK_SH)],
                           capture_output=True, text=True, timeout=300, env=env)
    assert proc2.returncode == 0, proc2.stderr
    verdict2 = json.loads((art / "variants" / "r1-01" / "verdict.json")
                          .read_text(encoding="utf-8"))
    assert verdict2["outcome"] == "latency_fail"
    # the failed measurement consumed one repair attempt (the script's ledger)
    trace = json.loads((art / "variants" / "r1-01" / "repair_trace.json")
                       .read_text(encoding="utf-8"))
    assert trace["repair_count"] == 1


# ── admission clause single source (v7 C8: stable ack, text in ONE place) ─────

def test_admission_clause_single_source():
    """v7 C8: the clause TEXT lives in exactly two human places — the
    po_contract agent document (the canonical admission statement) and the
    workflow description (the user-facing one-sentence version). The gate
    no longer embeds a Chinese checksum constant; contracts.json records
    the stable boolean admission_clause_ack."""
    sh = (_REPO / "workflows" / "prof-opt" / "agents" / "po_contract" / "scripts"
          / "check_contracts.sh").read_text(encoding="utf-8")
    assert "admission_clause_ack" in sh
    assert "ADMISSION_CLAUSE" not in sh   # the checksum constant is deleted

    agent_md = (_REPO / "workflows" / "prof-opt" / "agents" / "po_contract" / "agent.md"
                ).read_text(encoding="utf-8")
    assert "训练须按给定轮数精确执行" in agent_md

    yaml_text = (_REPO / "workflows" / "prof-opt" / "workflow.yaml").read_text(encoding="utf-8")
    assert "训练须按给定轮数精确执行" in yaml_text


# ── extract_user_pkg (cleanliness round): fail-loud path resolution ───────────

_EXTRACT_SH = (_REPO / "workflows" / "prof-opt" / "agents" / "po_flatten" / "scripts"
               / "extract_user_pkg.sh")

_ENTRY_BODY = "import os\nimport json\nfrom mymodel import layers\n"


def _run_extract(artifacts: Path, project_root: Path, model_path: str):
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(artifacts)
    env["ORCA_PYTHON"] = sys.executable
    return subprocess.run(["bash", str(_EXTRACT_SH), str(project_root),
                           model_path],
                          capture_output=True, text=True, timeout=60, env=env)


def test_extract_user_pkg_resolves_relative_model_path(tmp_path: Path):
    """v7 F4: classification runs under $ORCA_PYTHON with the project root
    importable and decides by the imported module's FILE LOCATION — a real
    user package under the project root lands in .user_pkg; stdlib names do
    not; a name that cannot be resolved is listed as UNCERTAIN for the node
    agent to review (never silently dropped into either bucket)."""
    proj = tmp_path / "proj"
    (proj / "mymodel").mkdir(parents=True)
    (proj / "mymodel" / "__init__.py").write_text("", encoding="utf-8")
    (proj / "model.py").write_text(
        "import os\nimport json\nfrom mymodel import layers\n"
        "import nosuchpkg_anywhere\n", encoding="utf-8")
    art = tmp_path / "art"
    art.mkdir()
    proc = _run_extract(art, proj, "model.py")
    assert proc.returncode == 0, proc.stderr
    # mymodel resolves under the project root -> user-owned; os/json are not
    assert (art / ".user_pkg").read_text(encoding="utf-8") == "mymodel\n"
    # the unresolvable name is EXPLICITLY listed for agent review
    assert "UNCERTAIN: nosuchpkg_anywhere" in proc.stderr
    assert "review this name" in proc.stderr


def test_extract_user_pkg_accepts_absolute_model_path(tmp_path: Path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    entry = elsewhere / "model.py"
    (tmp_path / "proj" / "mymodel").mkdir(parents=True)
    (tmp_path / "proj" / "mymodel" / "__init__.py").write_text("", encoding="utf-8")
    entry.write_text("from mymodel import layers\n", encoding="utf-8")
    art = tmp_path / "art"
    art.mkdir()
    proc = _run_extract(art, tmp_path / "proj", str(entry))
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
                 "experiment_ledger.py", "emit_result.py",
                 "mfu_adapter.py", "mfu_benchmark.py", "round_state.py",
                 "rules_pool.py", "check_verdict.py")


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

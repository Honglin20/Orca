"""test_po_v8_frontier.py — frontier_snapshot.py contract tests (v8).

Covers the SPEC surface (docs/specs/prof-opt-v8-spec.md §1):
  - frontier: success-row extraction, domination (equal-is-dominated? no —
    one strict required), non-success rows excluded
  - in_flight: pending_launch (no train_status.json), on_track,
    at_risk (over_budget_streak > 0), terminating (terminal stage)
  - avoid: the three non-success terminal outcomes
  - fail loud: missing/unparseable anchor, unparseable history line,
    unparseable train_status.json of an in-flight vid
  - atomic write: base/frontier.json lands complete, stdout mirrors it
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workflows" / "prof-opt" / "agents" / "_po_scripts"
sys.path.insert(0, str(_SCRIPTS))

from frontier_snapshot import snapshot  # noqa: E402


_ANCHOR = {"baseline_makespan_cycles": 1000, "target_cycles": 500,
           "accuracy_budget": 0.05}


def _run_cli(args, env=None, timeout=120):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout, env=merged)


def _ws(tmp_path: Path) -> Path:
    art = tmp_path / "ws"
    (art / "base").mkdir(parents=True)
    (art / "base" / "origin_anchor.json").write_text(json.dumps(_ANCHOR),
                                                    encoding="utf-8")
    return art


def _row(art: Path, vid: str, outcome: str, **extra) -> None:
    row = {"vid": vid, "round": 1, "seq": 1, "change_sig": f"sig:{vid}",
           "probe_epochs": 1, "target_modules": ["m"], "absorbs": [],
           "implemented": True, "ts": "2026-09-10T00:00:00+00:00"}
    if outcome == "latency_improved":
        row.update({"structural_check": "pass", "makespan_cycles": 900,
                    "latency_gate": "pass", "pred_actual_ratio": 1.0})
    row["outcome"] = outcome
    row.update(extra)
    with open(art / "history.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _status(art: Path, vid: str, **fields) -> None:
    path = art / "variants" / vid / "train_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"vid": vid, "stage": "training", "epoch": 3, "metric": 0.5,
           "gap": 0.02, "over_budget_streak": 0, "stopped_at_epoch": None,
           "device": 0, "ts": "2026-09-10T00:00:00+00:00"}
    doc.update(fields)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_frontier_extracts_success_rows_and_dominates(tmp_path):
    """Domination needs ONE strict margin in either axis: a strictly faster
    AND not-worse point knocks out the slower one; equal on both axes keeps
    both (no strict margin); a worse-on-one-axis point is never dominated."""
    art = _ws(tmp_path)
    _row(art, "r1-01", "success", makespan_cycles=800, gap=0.02, final_acc=0.91)
    _row(art, "r1-02", "success", makespan_cycles=700, gap=0.02, final_acc=0.91)
    _row(art, "r1-03", "success", makespan_cycles=600, gap=0.03, final_acc=0.90)
    _row(art, "r1-04", "accuracy_fail", makespan_cycles=100, gap=0.5)
    snap = snapshot(art)
    vids = [e["vid"] for e in snap["frontier"]]
    assert vids == ["r1-02", "r1-03"]          # r1-01 dominated by r1-02
    by_vid = {e["vid"]: e for e in snap["frontier"]}
    assert by_vid["r1-02"]["makespan_cycles"] == 700
    assert by_vid["r1-02"]["change_sig"] == "sig:r1-02"
    assert by_vid["r1-03"]["gap"] == 0.03
    # accuracy_fail never enters the frontier (it is avoid material)
    assert "r1-04" not in vids


def test_frontier_equal_points_keep_both_no_strict_margin(tmp_path):
    art = _ws(tmp_path)
    _row(art, "r1-01", "success", makespan_cycles=800, gap=0.02, final_acc=0.9)
    _row(art, "r1-02", "success", makespan_cycles=800, gap=0.02, final_acc=0.9)
    snap = snapshot(art)
    assert [e["vid"] for e in snap["frontier"]] == ["r1-01", "r1-02"]


def test_in_flight_states_and_avoid_lists(tmp_path):
    art = _ws(tmp_path)
    _row(art, "r1-01", "latency_improved", makespan_cycles=900)          # no status file
    _row(art, "r1-02", "latency_improved", makespan_cycles=880)
    _status(art, "r1-02")                                                # on_track
    _row(art, "r1-03", "latency_improved", makespan_cycles=870)
    _status(art, "r1-03", over_budget_streak=2)                          # at_risk
    _row(art, "r1-04", "latency_improved", makespan_cycles=860)
    _status(art, "r1-04", stage="killed", stopped_at_epoch=3)            # terminating
    _row(art, "r1-05", "accuracy_fail", makespan_cycles=850, gap=0.4,
         over_budget_streak=3)
    _row(art, "r1-06", "probe_insufficient", stage="liveness",
         max_retries_hit=True)
    _row(art, "r1-07", "latency_fail", makespan_cycles=990,
         latency_gate="fail")
    snap = snapshot(art)
    risks = {r["vid"]: r["risk"] for r in snap["in_flight"]}
    assert risks == {"r1-01": "pending_launch", "r1-02": "on_track",
                     "r1-03": "at_risk", "r1-04": "terminating"}
    at_risk = next(r for r in snap["in_flight"] if r["vid"] == "r1-03")
    assert at_risk["over_budget_streak"] == 2 and at_risk["epoch"] == 3
    assert [a["vid"] for a in snap["avoid"]] == ["r1-05", "r1-06", "r1-07"]
    assert {a["outcome"] for a in snap["avoid"]} == {"accuracy_fail",
                                                     "probe_insufficient",
                                                     "latency_fail"}
    assert snap["avoid"][0]["change_sig"] == "sig:r1-05"


def test_terminal_vid_leaves_in_flight_even_when_latent_row_exists(tmp_path):
    """A vid with a latency_improved row AND a later terminal row is judged —
    it never appears in_flight (gate_decide's ANY-version predicate)."""
    art = _ws(tmp_path)
    _row(art, "r1-01", "latency_improved", makespan_cycles=900)
    _row(art, "r1-01", "success", makespan_cycles=900, gap=0.01,
         final_acc=0.93)
    snap = snapshot(art)
    assert snap["in_flight"] == []
    assert [e["vid"] for e in snap["frontier"]] == ["r1-01"]
    assert snap["avoid"] == []


def test_fail_loud_missing_anchor(tmp_path):
    art = tmp_path / "ws"
    (art / "base").mkdir(parents=True)
    with pytest.raises(ValueError, match="origin anchor missing"):
        snapshot(art)


def test_fail_loud_torn_anchor(tmp_path):
    art = _ws(tmp_path)
    (art / "base" / "origin_anchor.json").write_text("{torn", encoding="utf-8")
    with pytest.raises(ValueError, match="unparseable"):
        snapshot(art)


def test_fail_loud_torn_history_line(tmp_path):
    art = _ws(tmp_path)
    (art / "history.jsonl").write_text("{not json\n", encoding="utf-8")
    with pytest.raises(Exception, match="not valid JSON"):
        snapshot(art)


def test_fail_loud_torn_in_flight_status(tmp_path):
    """An UNPARSEABLE train_status.json of an in-flight vid is a torn
    workspace; a MISSING one is the legal pending_launch window."""
    art = _ws(tmp_path)
    _row(art, "r1-01", "latency_improved", makespan_cycles=900)
    (art / "variants" / "r1-01").mkdir(parents=True)
    (art / "variants" / "r1-01" / "train_status.json").write_text(
        "{torn", encoding="utf-8")
    with pytest.raises(ValueError, match="unparseable"):
        snapshot(art)


def test_cli_torn_history_exits_2_not_traceback(tmp_path):
    """CLI 契约：撕裂 history 行 → 干净的 FAIL 行 + exit 2（不是裸 traceback
    的 exit 1）。"""
    art = _ws(tmp_path)
    (art / "history.jsonl").write_text("{not json\n", encoding="utf-8")
    proc = _run_cli([sys.executable, str(_SCRIPTS / "frontier_snapshot.py"),
                     "--artifacts", str(art)])
    assert proc.returncode == 2
    assert "frontier_snapshot: FAIL" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_float_makespan_in_anchor_rejected(tmp_path):
    """锚的 cycles 字段必须严格整数：浮点静默截断会把谎报的数字写进下轮
    决策输入。"""
    art = _ws(tmp_path)
    (art / "base" / "origin_anchor.json").write_text(
        json.dumps({**_ANCHOR, "baseline_makespan_cycles": 1000.7}),
        encoding="utf-8")
    proc = _run_cli([sys.executable, str(_SCRIPTS / "frontier_snapshot.py"),
                     "--artifacts", str(art)])
    assert proc.returncode == 2
    assert "baseline_makespan_cycles" in proc.stderr


def test_cli_writes_atomic_snapshot_and_mirrors_stdout(tmp_path):
    art = _ws(tmp_path)
    _row(art, "r1-01", "success", makespan_cycles=800, gap=0.02, final_acc=0.9)
    proc = _run_cli([sys.executable, str(_SCRIPTS / "frontier_snapshot.py"),
                     "--artifacts", str(art)])
    assert proc.returncode == 0, proc.stderr
    on_disk = json.loads((art / "base" / "frontier.json")
                         .read_text(encoding="utf-8"))
    assert json.loads(proc.stdout) == on_disk
    assert on_disk["anchor"] == _ANCHOR
    assert on_disk["frontier"][0]["vid"] == "r1-01"
    assert not list((art / "base").glob("frontier.json.tmp.*"))  # no tmp litter

    # failure path: a torn anchor never leaves a stale-looking snapshot behind
    (art / "base" / "origin_anchor.json").write_text("{torn", encoding="utf-8")
    (art / "base" / "frontier.json").unlink()
    proc2 = _run_cli([sys.executable, str(_SCRIPTS / "frontier_snapshot.py"),
                      "--artifacts", str(art)])
    assert proc2.returncode == 2
    assert not (art / "base" / "frontier.json").exists()


def test_empty_workspace_yields_empty_views(tmp_path):
    art = _ws(tmp_path)
    snap = snapshot(art)
    assert snap == {"anchor": _ANCHOR, "frontier": [], "in_flight": [],
                    "avoid": []}

#!/usr/bin/env python3
"""frontier_snapshot.py — the mechanical Pareto/avoid/in-flight view (v8).

Derives ``base/frontier.json`` from TWO on-disk sources only — history.jsonl
terminal/process rows and each in-flight vid's ``train_status.json`` (the
watchdog rewrites it every epoch, so the accuracy comparison is always
current). It is the proposal/gate context's number layer: pure read, derived,
regenerable — never a second truth source (history.jsonl stays the audit base).

  {"anchor":    {"baseline_makespan_cycles", "target_cycles", "accuracy_budget"},
   "frontier":  [{vid, change_sig, target_modules, makespan_cycles, final_acc, gap}],
   "in_flight": [{vid, stage, epoch, metric, gap, over_budget_streak, risk}],
   "avoid":     [{vid, change_sig, outcome}]}

  frontier  non-dominated set over the latest ``success`` rows: A dominates B
            iff A.makespan <= B.makespan AND A.gap <= B.gap (one strict). A
            success row means the accuracy gate already passed, so accuracy
            enters only through the direction-normalized gap.
  in_flight vids with a latency_improved row and NO terminal row in ANY
            version (same predicate as the gate). risk is mechanical:
            terminating (watchdog reached a terminal stage) / at_risk
            (over_budget_streak > 0) / on_track / pending_launch (no
            train_status.json yet — the legal window between the proposal
            node's latency row and the probe launch).
  avoid     latest outcome in {accuracy_fail, probe_insufficient,
            latency_fail} — the mechanical rerouting list that replaces the
            retired direction.json failed_sigs.

Fail loud (exit 2) on: a missing/unparseable origin anchor, an unparseable
history line, or an UNPARSEABLE train_status.json of an in-flight vid. A
MISSING train_status.json is not an error (pending_launch). in_flight and
avoid never participate in the domination math.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from history_lib import TERMINAL_OUTCOMES, HistoryError, read_rows  # noqa: E402

AVOID_OUTCOMES = ("accuracy_fail", "probe_insufficient", "latency_fail")


def _anchor(art: Path) -> dict:
    path = art / "base" / "origin_anchor.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"origin anchor missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"origin anchor unparseable: {path} ({exc})") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"origin anchor is not a JSON object: {path}")
    out = {}
    for key in ("baseline_makespan_cycles", "target_cycles"):
        value = doc.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"origin anchor carries invalid {key!r}: {value!r} (must be "
                "an integer, never a silently truncated float)")
        out[key] = value
    budget = doc.get("accuracy_budget")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)):
        raise ValueError(
            f"origin anchor carries invalid 'accuracy_budget': {budget!r}")
    out["accuracy_budget"] = budget
    return out


def _dominated(a: dict, b: dict) -> bool:
    """True when frontier entry ``a`` dominates ``b`` (lower makespan and
    lower gap both win; at least one must be strict)."""
    le_ms = a["makespan_cycles"] <= b["makespan_cycles"]
    le_gap = a["gap"] <= b["gap"]
    strict = a["makespan_cycles"] < b["makespan_cycles"] or a["gap"] < b["gap"]
    return le_ms and le_gap and strict


def _risk(stage: object, streak: object) -> str:
    if stage in ("killed", "failed"):
        return "terminating"
    if isinstance(streak, int) and not isinstance(streak, bool) and streak > 0:
        return "at_risk"
    return "on_track"


def _in_flight_row(art: Path, vid: str) -> dict:
    status_path = art / "variants" / vid / "train_status.json"
    row: dict = {"vid": vid, "stage": None, "epoch": None, "metric": None,
                 "gap": None, "over_budget_streak": None}
    if not status_path.is_file():
        row["risk"] = "pending_launch"
        return row
    try:
        doc = json.loads(status_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"train_status.json of in-flight {vid} unparseable: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"train_status.json of in-flight {vid} is not a JSON object")
    for key in ("stage", "epoch", "metric", "gap", "over_budget_streak"):
        row[key] = doc.get(key)
    row["risk"] = _risk(row["stage"], row["over_budget_streak"])
    return row


def snapshot(art: Path) -> dict:
    anchor = _anchor(art)
    rows = read_rows(art / "history.jsonl")

    latest: dict[str, dict] = {}
    passed: set[str] = set()
    terminal: set[str] = set()
    for row in rows:
        vid = row.get("vid")
        if isinstance(vid, str) and vid:
            latest[vid] = row
            if row.get("outcome") == "latency_improved":
                passed.add(vid)
            if row.get("outcome") in TERMINAL_OUTCOMES:
                terminal.add(vid)

    success_rows = []
    for vid, row in latest.items():
        if row.get("outcome") != "success":
            continue
        makespan = row.get("makespan_cycles")
        gap = row.get("gap")
        if isinstance(makespan, bool) or not isinstance(makespan, int):
            raise ValueError(f"success row of {vid} has no integer makespan_cycles")
        if isinstance(gap, bool) or not isinstance(gap, (int, float)):
            raise ValueError(f"success row of {vid} has no numeric gap")
        success_rows.append({
            "vid": vid,
            "change_sig": row.get("change_sig"),
            "target_modules": row.get("target_modules", []),
            "makespan_cycles": makespan,
            "final_acc": row.get("final_acc"),
            "gap": float(gap),
        })
    frontier = [e for e in success_rows
                if not any(_dominated(o, e) for o in success_rows if o is not e)]
    frontier.sort(key=lambda e: e["vid"])

    in_flight = sorted(passed - terminal)
    flight_rows = [_in_flight_row(art, vid) for vid in in_flight]

    avoid = [{"vid": vid, "change_sig": row.get("change_sig"),
              "outcome": row.get("outcome")}
             for vid, row in sorted(latest.items())
             if row.get("outcome") in AVOID_OUTCOMES]

    return {"anchor": anchor, "frontier": frontier,
            "in_flight": flight_rows, "avoid": avoid}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", default=os.environ.get("ORCA_ARTIFACTS_DIR"))
    ns = ap.parse_args()
    if not ns.artifacts:
        print("frontier_snapshot: --artifacts or ORCA_ARTIFACTS_DIR is required",
              file=sys.stderr)
        return 2
    try:
        result = snapshot(Path(ns.artifacts))
        out_path = Path(ns.artifacts) / "base" / "frontier.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                  sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, out_path)
    except (OSError, ValueError, KeyError, HistoryError) as exc:
        print(f"frontier_snapshot: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

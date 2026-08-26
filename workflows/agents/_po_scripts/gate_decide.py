#!/usr/bin/env python3
"""gate_decide.py — pure-read loop/exit decision computed from the workspace.

Reads (never writes): history.jsonl (latest version per vid), best.json, and
rounds/<NNN>/proposals.json of the CURRENT round (exhausted flag comes from
disk, not from a node output — cross-node output references across a back edge
are unproven). Decision order is fixed (first match wins):

    full-train              best exists and best.makespan_cycles <= target
    loop                    round < max_rounds and not exhausted and
                            stall < stall_rounds
    full-train-best-effort  loop conditions exhausted but a promoted best exists
    finish-failed           no promoted best at all

stall: consecutive rounds (from history) with zero promoted vids; reset to 0
by any promoted round; initial value 0. Hard cap: round >= max_rounds can
NEVER yield `loop` — belt-and-suspenders against an unbounded cycle.
stdout: single-line JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from history_lib import read_latest  # noqa: E402


def _current_round(rounds_dir: Path) -> int:
    if not rounds_dir.is_dir():
        return 0
    numbers = []
    for child in rounds_dir.iterdir():
        if child.is_dir() and child.name.isdigit():
            numbers.append(int(child.name))
    return max(numbers) if numbers else 0


def _round_exhausted(rounds_dir: Path, round_no: int) -> bool:
    proposals = rounds_dir / f"{round_no:03d}" / "proposals.json"
    if not proposals.is_file():
        raise FileNotFoundError(
            f"gate_decide: {proposals} missing — the gate reads the exhausted "
            f"flag from disk; run the propose step first")
    data = json.loads(proposals.read_text(encoding="utf-8"))
    exhausted = data.get("exhausted")
    if not isinstance(exhausted, bool):
        raise ValueError(f"gate_decide: {proposals} has non-boolean 'exhausted'")
    return exhausted


def _stall_count(history_path: Path, round_no: int) -> int:
    """Consecutive most-recent rounds without a promoted vid (latest-version
    outcome). Rounds are walked in order so promotion resets the counter."""
    latest = read_latest(history_path)
    promoted_rounds = {row["round"] for row in latest.values()
                       if row.get("outcome") == "promoted" and "round" in row}
    stall = 0
    for r in range(1, round_no + 1):
        stall = 0 if r in promoted_rounds else stall + 1
    return stall


def _base_makespan(artifacts: Path) -> int:
    """Baseline makespan from the deterministic analyze.py report on disk.

    The latency target is RELATIVE (project-agnostic): the user gives a
    reduction ratio; the absolute threshold is derived from this baseline."""
    report = artifacts / "base" / "bottleneck_report.json"
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
        return int(data["makespan_cycles"])
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"gate_decide: {report} missing — the relative latency target "
            f"needs the baseline makespan; run the baseline chain first") from exc


def decide(artifacts: Path, latency_reduction_min: float, max_rounds: int,
           stall_rounds: int) -> dict:
    if not 0.0 < latency_reduction_min < 1.0:
        raise ValueError(
            f"gate_decide: --latency-reduction-min must be in (0, 1), got "
            f"{latency_reduction_min}")
    rounds_dir = artifacts / "rounds"
    round_no = _current_round(rounds_dir)
    exhausted = _round_exhausted(rounds_dir, round_no) if round_no else False
    stall = _stall_count(artifacts / "history.jsonl", round_no)

    base_makespan = _base_makespan(artifacts)
    target = int(base_makespan * (1.0 - latency_reduction_min)) + 1  # <= target ⇔ strictly below the line

    best = None
    best_path = artifacts / "best.json"
    if best_path.is_file():
        raw = json.loads(best_path.read_text(encoding="utf-8"))
        best = {"vid": raw["vid"], "makespan_cycles": raw["makespan_cycles"],
                "proxy_acc": raw.get("proxy_acc")}

    if best is not None and best["makespan_cycles"] <= target:
        decision = "full-train"
        reason = (f"best vid {best['vid']} makespan {best['makespan_cycles']} "
                  f"<= target {target} (baseline {base_makespan} x (1 - "
                  f"{latency_reduction_min}))")
    elif round_no >= max_rounds:
        # hard cap: never loop at/after max_rounds, whatever exhausted/stall say
        decision = "full-train-best-effort" if best is not None else "finish-failed"
        reason = f"round {round_no} >= max_rounds {max_rounds} (hard cap)"
    elif not exhausted and stall < stall_rounds:
        decision = "loop"
        reason = (f"target unmet, round {round_no}/{max_rounds}, "
                  f"exhausted={exhausted}, stall={stall}/{stall_rounds}")
    elif best is not None:
        decision = "full-train-best-effort"
        why = "proposals exhausted" if exhausted else f"stall {stall} >= {stall_rounds}"
        reason = f"{why}; promoting best vid {best['vid']} as-is"
    else:
        decision = "finish-failed"
        why = "proposals exhausted" if exhausted else f"stall {stall} >= {stall_rounds}"
        reason = f"{why} and no promoted variant ever passed probe"

    return {"decision": decision, "round": round_no, "stall": stall,
            "best": best, "reason": reason}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--latency-reduction-min", type=float, required=True,
                    help="required latency reduction ratio vs baseline, in (0, 1)")
    ap.add_argument("--max-rounds", type=int, default=5)
    ap.add_argument("--stall-rounds", type=int, default=2)
    ns = ap.parse_args()

    try:
        result = decide(Path(ns.artifacts), ns.latency_reduction_min,
                        ns.max_rounds, ns.stall_rounds)
    except (OSError, ValueError, KeyError) as exc:
        print(f"gate_decide: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

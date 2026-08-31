#!/usr/bin/env python3
"""gate_decide.py — pure-read loop/exit decision computed from the workspace.

Reads (never writes): history.jsonl (terminal rows), round_state current
(the single round source), and the frozen origin anchor
(base/origin_anchor.json — the target line never moves). The decision order
is fixed (first match wins, v6 §8.1):

    report   ANY vid has a `success` row in history — the winner judgment
             (gap-best success, ties by makespan) is po_report's job — OR
             round >= max_rounds (hard cap, never loops; with no success the
             in-flight trainings are awaited and harvested by po_report, not
             killed here)
    loop     everything else — the ONLY other exit; there is no wall-clock
             cap and no plateau early-exit (a plateau is answered by
             rerouting proposals, not by stopping)

Exiting never disturbs in-flight work: a success row only truncates FUTURE
proposal rounds; trainings already released by the probe keep running and
keep their judgment eligibility for po_report's terminal harvest (v6 §8.1).
best.json and the v5 mode/round-advance markers are no longer read
(retired in v6).

stdout: single-line JSON {"decision", "round", "target_cycles",
"success_vids", "in_flight", "reason"}; in_flight = vids with a
latency_pass row but no terminal row (success / accuracy_fail /
probe_insufficient / latency_fail) in ANY version. A missing origin anchor
is a hard error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from history_lib import TERMINAL_OUTCOMES, read_rows  # noqa: E402
from round_state import current_round  # noqa: E402


def _load_origin_anchor(artifacts: Path) -> dict:
    path = artifacts / "base" / "origin_anchor.json"
    try:
        anchor = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"gate_decide: origin anchor missing: {path} (the target line is "
            f"frozen by the baseline stage; run the baseline first)") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"gate_decide: origin anchor unparseable: {exc}") from exc
    if not isinstance(anchor.get("target_cycles"), int):
        raise ValueError(
            f"gate_decide: origin anchor lacks integer 'target_cycles': {anchor!r}")
    return anchor


def decide(artifacts: Path, max_rounds: int = 100) -> dict:
    round_no = current_round(artifacts)       # single source (round_state.py)
    anchor = _load_origin_anchor(artifacts)
    target = anchor["target_cycles"]

    rows = read_rows(artifacts / "history.jsonl")
    vids_with = {row.get("vid") for row in rows
                 if row.get("vid") and row.get("outcome") == "success"}
    success_vids = sorted(vids_with)
    passed_vids = {row.get("vid") for row in rows
                   if row.get("vid") and row.get("outcome") == "latency_pass"}
    terminal_vids = {row.get("vid") for row in rows
                     if row.get("vid") and row.get("outcome") in TERMINAL_OUTCOMES}
    in_flight = sorted(passed_vids - terminal_vids)

    if success_vids:
        decision = "report"
        reason = (f"success row(s) on disk for {', '.join(success_vids)} — "
                  f"exit to report (in-flight trainings keep running and are "
                  f"harvested by the terminal report, never killed here)")
    elif round_no >= max_rounds:
        # hard cap: never loop at/after max_rounds — without a success the
        # report awaits every in-flight training's terminal state
        decision = "report"
        reason = (f"round {round_no} >= max_rounds {max_rounds} (hard cap, "
                  f"never loops) — no success variant; the report awaits the "
                  f"in-flight terminal states")
    else:
        decision = "loop"
        reason = (f"no success row and round {round_no}/{max_rounds} — "
                  f"reroute proposals (failed_sigs); no other exit")

    return {"decision": decision, "round": round_no, "target_cycles": target,
            "success_vids": success_vids, "in_flight": in_flight,
            "reason": reason}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--max-rounds", type=int, default=100)
    ns = ap.parse_args()

    try:
        result = decide(Path(ns.artifacts), ns.max_rounds)
    except (OSError, ValueError, KeyError) as exc:
        print(f"gate_decide: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

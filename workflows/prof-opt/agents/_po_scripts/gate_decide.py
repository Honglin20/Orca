#!/usr/bin/env python3
"""gate_decide.py — pure-read loop/exit decision computed from the workspace.

Reads (never writes): history.jsonl (terminal rows), round_state current
(the single round source), every round's proposals.json (the idle probe),
the frozen origin anchor (base/origin_anchor.json — the target line never
moves), and the wrapper's `incumbent_promoted` fact. The decision order is
fixed (first match wins, v7 §8):

    report   ANY accuracy-success vid also meets the frozen origin target — OR
             round >= max_rounds (hard cap, never loops; with no success the
             in-flight trainings are awaited and harvested by po_report, not
             killed here) — OR the latest `idle_round_cap` rounds in a row
             are all zero-proposal rounds (decision reason `idle_exhausted`:
             the search space is genuinely spent and idling would burn
             nothing but time)
    loop     a new incumbent was promoted (old-base idle evidence resets), or
             everything else — the ONLY other exit; there is no wall-clock
             cap and no plateau early-exit (a plateau is answered by
             rerouting proposals, not by stopping)

Idle counting: walking from the latest round backwards, a round counts as
idle iff its proposals.json exists, parses, and holds an EMPTY `proposals`
list (a legal round outcome with a non-empty `exhausted_rationale`). The
streak stops at the first non-idle round — non-empty proposals, or a
missing/unparseable proposals.json (an incomplete round is never evidence
of an exhausted space).

Exiting never disturbs in-flight work. A success that improves the incumbent
but misses the origin target is promoted and the search continues.
best.json and the v5 mode/round-advance markers are not read (retired).

stdout: single-line JSON {"decision", "round", "target_cycles",
"success_vids", "in_flight", "idle_rounds", "reason"}; in_flight = vids
with a latency_improved row but no terminal row (success / accuracy_fail /
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


def _round_is_idle(artifacts: Path, round_no: int) -> bool | None:
    """True/False for a zero/non-zero proposal round; None when the round's
    proposals.json is missing or unparseable (incomplete — never idle)."""
    path = artifacts / "rounds" / f"{round_no:03d}" / "proposals.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("proposals"), list):
        return None
    return doc["proposals"] == []


def idle_streak(artifacts: Path, round_no: int) -> int:
    """Consecutive zero-proposal rounds counted backwards from `round_no`."""
    streak = 0
    for r in range(round_no, 0, -1):
        if _round_is_idle(artifacts, r) is True:
            streak += 1
        else:
            break
    return streak


def decide(artifacts: Path, max_rounds: int = 100, idle_round_cap: int = 5,
           incumbent_promoted: bool = False) -> dict:
    round_no = current_round(artifacts)       # single source (round_state.py)
    anchor = _load_origin_anchor(artifacts)
    target = anchor["target_cycles"]

    rows = read_rows(artifacts / "history.jsonl")
    success_rows = {row.get("vid"): row for row in rows
                    if row.get("vid") and row.get("outcome") == "success"}
    success_vids = sorted(success_rows)
    target_success_vids = sorted(
        vid for vid, row in success_rows.items()
        if isinstance(row.get("makespan_cycles"), int)
        and row["makespan_cycles"] <= target)
    passed_vids = {row.get("vid") for row in rows
                   if row.get("vid") and row.get("outcome")
                   == "latency_improved"}
    terminal_vids = {row.get("vid") for row in rows
                     if row.get("vid") and row.get("outcome") in TERMINAL_OUTCOMES}
    in_flight = sorted(passed_vids - terminal_vids)

    idle_rounds = idle_streak(artifacts, round_no)

    if target_success_vids:
        decision = "report"
        reason = (f"target-met success row(s) on disk for {', '.join(target_success_vids)} — "
                  f"exit to report (in-flight trainings keep running and are "
                  f"harvested by the terminal report, never killed here)")
    elif round_no >= max_rounds:
        # hard cap: never loop at/after max_rounds — without a final winner the
        # report awaits every in-flight training's terminal state
        decision = "report"
        reason = (f"round {round_no} >= max_rounds {max_rounds} (hard cap, "
                  f"never loops) — no target-meeting success variant; the report awaits the "
                  f"in-flight terminal states")
    elif incumbent_promoted:
        decision = "loop"
        reason = ("a new accuracy-safe incumbent was promoted at this gate — "
                  "prior zero-proposal rounds describe the old base, so reset "
                  "idle exhaustion and continue from the new incumbent")
    elif idle_round_cap > 0 and idle_rounds >= idle_round_cap:
        # the space is spent: the last N rounds each honestly reported zero
        # admissible proposals — idling further rounds burns nothing but time
        decision = "report"
        reason = (f"idle_exhausted: the latest {idle_rounds} round(s) in a row "
                  f"(>= idle_round_cap {idle_round_cap}) produced zero "
                  f"proposals each — exit to report; the exhausted_rationale "
                  f"records name what was tried")
    elif success_vids:
        decision = "loop"
        reason = (f"accuracy-safe success row(s) {', '.join(success_vids)} "
                  "improved the incumbent but have not reached the origin target — "
                  "continue proposing from the promoted incumbent")
    else:
        decision = "loop"
        reason = (f"no success row and round {round_no}/{max_rounds} "
                  f"(idle streak {idle_rounds}/{idle_round_cap}) — reroute "
                  f"proposals (failed_sigs); no other exit")

    return {"decision": decision, "round": round_no, "target_cycles": target,
            "success_vids": success_vids, "in_flight": in_flight,
            "idle_rounds": idle_rounds,
            "incumbent_promoted": incumbent_promoted, "reason": reason}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--max-rounds", type=int, default=100)
    ap.add_argument("--idle-round-cap", type=int, default=5,
                    help="consecutive zero-proposal rounds before idle exit "
                         "(<=0 disables the idle exit)")
    ap.add_argument("--incumbent-promoted", action="store_true",
                    help="ignore idle evidence produced against the prior base")
    ns = ap.parse_args()

    try:
        result = decide(Path(ns.artifacts), ns.max_rounds, ns.idle_round_cap,
                        ns.incumbent_promoted)
    except (OSError, ValueError, KeyError) as exc:
        print(f"gate_decide: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

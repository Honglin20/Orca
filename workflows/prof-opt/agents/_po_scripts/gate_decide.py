#!/usr/bin/env python3
"""gate_decide.py — pure-read loop/exit decision computed from the workspace.

Reads (never writes): history.jsonl (all version rows), best.json, and the
frozen origin anchor (base/origin_anchor.json — the target line never
moves). The decision order is fixed (first match wins):

    full-train              best exists AND best.makespan_cycles <= target
                            AND best.vid has an `accuracy_pass` row in ANY
                            version of its history (the probe's terminal
                            pass; the later `advanced` row does not erase it)
    full-train-best-effort  round >= max_rounds (hard cap, never loops) and
                            a best exists
    finish-failed           round >= max_rounds and no best
    loop                    everything else — the ONLY other exit; there is
                            no wall-clock cap and no plateau early-exit (a
                            plateau is answered by rerouting proposals, not
                            by stopping)

Invariant (checked before deciding): round_state mode == accuracy but
best.vid has no probe row at all -> exit 2 (mode=accuracy implies the probe
trained best.vid at least once; a violation means the workspace is torn).
A missing origin anchor is a hard error. stdout: single-line JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from history_lib import read_rows  # noqa: E402
from round_state import current_round, mode_state  # noqa: E402


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
    mode_info = mode_state(artifacts)         # single source (round_state.py)
    mode = mode_info["mode"]
    target = mode_info["target_cycles"]

    best: dict | None = None
    best_path = artifacts / "best.json"
    if best_path.is_file():
        try:
            raw = json.loads(best_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"gate_decide: best.json unparseable: {exc}") from exc
        best = {"vid": raw["vid"], "makespan_cycles": raw["makespan_cycles"],
                "proxy_acc": raw.get("proxy_acc")}

    rows = read_rows(artifacts / "history.jsonl")
    rows_of_best = [r for r in rows if best is not None
                    and r.get("vid") == best["vid"]]
    has_accuracy_pass = any(r.get("outcome") == "accuracy_pass"
                            for r in rows_of_best)
    has_probe_row = any("promote_gate" in r for r in rows_of_best)

    if mode == "accuracy" and not has_probe_row:
        raise ValueError(
            f"gate_decide: invariant broken — mode=accuracy but best vid "
            f"{best['vid']} has no probe row in history (the probe must have "
            f"trained it at least once); workspace is torn, see po_report")

    if (best is not None and best["makespan_cycles"] <= target
            and has_accuracy_pass):
        decision = "full-train"
        reason = (f"best vid {best['vid']} makespan {best['makespan_cycles']} "
                  f"<= target {target} (anchor frozen at baseline "
                  f"{anchor['baseline_makespan_cycles']}) and its accuracy "
                  f"gate passed (accuracy_pass row in history)")
    elif round_no >= max_rounds:
        # hard cap: never loop at/after max_rounds — the only exit besides
        # the accuracy double-pass
        decision = "full-train-best-effort" if best is not None else "finish-failed"
        reason = f"round {round_no} >= max_rounds {max_rounds} (hard cap)"
    else:
        decision = "loop"
        reason = (f"sequential gates unmet, round {round_no}/{max_rounds}, "
                  f"mode={mode} — reroute proposals (failed_sigs), no other exit")

    return {"decision": decision, "round": round_no, "mode": mode,
            "best": best, "target_cycles": target, "reason": reason}


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

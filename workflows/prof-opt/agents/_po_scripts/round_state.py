#!/usr/bin/env python3
"""round_state.py — the single source for round numbers, round paths, and the
latency/accuracy phase.

Every consumer (propose Step0, probe Step0, gate, advance) derives its round
number and working directory through THIS script — hand-derived `rounds/<RRR>`
reasoning in prompts is retired. All output is a single-line JSON on stdout;
every bad input fails loud (stderr message, exit 2).

  current   {"round": R, "round_dir": "rounds/RRR"|null}
            R = max purely-numeric directory name under rounds/ (0 when none;
            non-numeric directory names are ignored). round_dir is the %03d
            zero-padded form; null when R == 0.

  working   {"round": R_write, "round_dir": "rounds/RRR"}
            The round a re-entered propose node works in: .round_advanced
            exists with round == current -> current + 1; otherwise
            max(current, 1).

  mode      {"mode": "latency"|"accuracy", "target_cycles": T,
             "best_makespan": M|null}
            best.json exists and its makespan_cycles <= the frozen origin
            anchor's target_cycles -> accuracy (the latency line is met; the
            loop is in the accuracy phase). No best, or best above the line
            -> latency. A missing origin anchor is a hard error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _current_round(rounds_dir: Path) -> int:
    if not rounds_dir.is_dir():
        return 0
    numbers = [int(c.name) for c in rounds_dir.iterdir()
               if c.is_dir() and c.name.isdigit()]
    return max(numbers) if numbers else 0


def _round_dir(round_no: int) -> str | None:
    return None if round_no == 0 else f"rounds/{round_no:03d}"


def _read_json(path: Path, what: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"round_state: {what} missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"round_state: {what} unparseable: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"round_state: {what} is not a JSON object: {path}")
    return data


def _mode(artifacts: Path) -> dict:
    anchor = _read_json(artifacts / "base" / "origin_anchor.json",
                        "the frozen origin anchor (base/origin_anchor.json)")
    try:
        target = int(anchor["target_cycles"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"round_state: origin anchor has no integer target_cycles: "
            f"{anchor!r}") from exc

    best_makespan: int | None = None
    best_path = artifacts / "best.json"
    if best_path.is_file():
        best = _read_json(best_path, "best.json")
        try:
            best_makespan = int(best["makespan_cycles"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"round_state: best.json has no integer makespan_cycles: "
                f"{best!r}") from exc

    mode = ("accuracy" if best_makespan is not None and best_makespan <= target
            else "latency")
    return {"mode": mode, "target_cycles": target,
            "best_makespan": best_makespan}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("command", choices=["current", "working", "mode"])
    ns = ap.parse_args()
    artifacts = Path(ns.artifacts)
    try:
        if ns.command == "current":
            r = _current_round(artifacts / "rounds")
            result: dict = {"round": r, "round_dir": _round_dir(r)}
        elif ns.command == "working":
            r = _current_round(artifacts / "rounds")
            marker_path = artifacts / ".round_advanced"
            if marker_path.is_file():
                marker = _read_json(marker_path, ".round_advanced")
                if marker.get("round") == r:
                    r = r + 1
            working = max(r, 1)
            result = {"round": working, "round_dir": _round_dir(working)}
        else:
            result = _mode(artifacts)
    except (OSError, ValueError, KeyError) as exc:
        print(f"round_state: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

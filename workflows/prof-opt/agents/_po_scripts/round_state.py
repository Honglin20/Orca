#!/usr/bin/env python3
"""round_state.py — the single source for round numbers and round paths.

Every consumer (propose Step0, gate, recheck) derives its round number and
working directory through THIS script — hand-derived `rounds/<RRR>`
reasoning in prompts is retired. All output is a single-line JSON on stdout;
every bad input fails loud (stderr message, exit 2).

  current   {"round": R, "round_dir": "rounds/RRR"|null}
            R = max purely-numeric directory name under rounds/ (0 when none;
            non-numeric directory names are ignored). round_dir is the %03d
            zero-padded form; null when R == 0.

  working   {"round": R_write, "round_dir": "rounds/RRR"}
            The round a re-entered propose node works in:
            max(current + 1, 1) — one round is one variant (v6 §4.2), so the
            next proposal always goes to a fresh round directory. The v5
            `.round_advanced` marker linkage is retired (base never
            advances); a leftover marker file from an old workspace is
            ignored.
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


def current_round(artifacts: Path) -> int:
    """Public single source: the current round number (max purely-numeric
    directory under rounds/, 0 when none). Every consumer (propose, gate,
    recheck) derives its round through this — hand-rolled duplicates drift."""
    return _current_round(artifacts / "rounds")


def _round_dir(round_no: int) -> str | None:
    return None if round_no == 0 else f"rounds/{round_no:03d}"


def working_round(artifacts: Path) -> int:
    """Public single source for the round a re-entered propose node works in
    (v6 §4.2: working = max(current + 1, 1))."""
    return max(_current_round(artifacts / "rounds") + 1, 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("command", choices=["current", "working"])
    ns = ap.parse_args()
    artifacts = Path(ns.artifacts)
    try:
        if ns.command == "current":
            r = current_round(artifacts)
            result: dict = {"round": r, "round_dir": _round_dir(r)}
        else:
            working = working_round(artifacts)
            result = {"round": working, "round_dir": _round_dir(working)}
    except (OSError, ValueError, KeyError) as exc:
        print(f"round_state: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

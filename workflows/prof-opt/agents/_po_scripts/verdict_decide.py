#!/usr/bin/env python3
"""verdict_decide.py — direction-normalized accuracy-budget verdicts, scripted.

The terminal verdict (v6 §7.3): after a variant's full training finishes,
the watchdog's eval chain writes variants/<VID>/eval/final_acc.json (with
within_budget still null) and THIS call computes and backfills the verdict.
The numbers are read from the workspace file that RECORDED them — a verdict
line is never hand-derived from values copied into a protocol call. The
budget comes from the frozen origin anchor (base/origin_anchor.json
`accuracy_budget`) — the anchor is the single immutable source; a missing
anchor fails loud.

  final-budget  --artifacts <ws> --vid <VID>
      reads variants/<VID>/eval/final_acc.json (`final_acc`,
      `baseline_full_acc`, `metric_direction` — its `within_budget` may
      still be null: this call is what computes it) and the origin anchor.
      On success the computed `within_budget` is backfilled into the file
      (atomic replace, §7.3) unless it is already set to the same value
      (idempotent); a recorded non-null value that DISAGREES with the
      recomputation fails loud (a hand-edited or torn record is never
      silently overwritten). stdout: {"within_budget": <bool>}.

Budget semantics: slack = 1.0 x budget (the fixed relaxation factor);
higher_better passes at value >= anchor - slack, lower_better at
value <= anchor + slack. stdout is a single-line JSON; every bad input
fails loud (stderr message, exit 2). The v5 `promote` subcommand (the
probe's k-depth accuracy gate) is retired in v6 — early-stop and final
judgments are the watchdog's (§7.2/§7.3).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

DIRECTIONS = ("higher_better", "lower_better")


def _load_json(path: Path, what: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"verdict_decide: {what} missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"verdict_decide: {what} unparseable: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"verdict_decide: {what} is not a JSON object: {path}")
    return data


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"verdict_decide: {what} is not a number: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"verdict_decide: {what} is not finite: {value!r}")
    return number


def _direction(value: object, what: str) -> str:
    if value not in DIRECTIONS:
        raise ValueError(
            f"verdict_decide: {what} must be one of {DIRECTIONS}, "
            f"got {value!r}")
    return value  # type: ignore[return-value]


def _passes(value: float, anchor: float, direction: str, slack: float) -> bool:
    if direction == "higher_better":
        return value >= anchor - slack
    return value <= anchor + slack


def anchor_budget(artifacts: Path) -> float:
    """The frozen accuracy budget from base/origin_anchor.json."""
    anchor = _load_json(artifacts / "base" / "origin_anchor.json",
                        "the frozen origin anchor (base/origin_anchor.json)")
    budget = _number(anchor.get("accuracy_budget"),
                     "origin_anchor.json 'accuracy_budget'")
    if budget < 0:
        raise ValueError(
            f"verdict_decide: origin anchor accuracy_budget must be >= 0, "
            f"got {budget}")
    return budget


def final_budget(artifacts: Path, vid: str) -> dict:
    if not vid:
        raise ValueError("verdict_decide: --vid must be non-empty")
    budget = anchor_budget(artifacts)
    slack = budget
    path = artifacts / "variants" / vid / "eval" / "final_acc.json"
    final = _load_json(path, f"the final accuracy record for {vid}")
    final_acc = _number(final.get("final_acc"), "final_acc.json 'final_acc'")
    anchor = _number(final.get("baseline_full_acc"),
                     "final_acc.json 'baseline_full_acc'")
    direction = _direction(final.get("metric_direction"),
                           "final_acc.json 'metric_direction'")
    within = _passes(final_acc, anchor, direction, slack)

    recorded = final.get("within_budget")
    if recorded is not None and recorded is not within:
        raise ValueError(
            f"verdict_decide: final_acc.json within_budget is recorded as "
            f"{recorded!r} but the recomputation says {within!r} "
            f"(final_acc={final_acc}, baseline_full_acc={anchor}, "
            f"direction={direction}, budget={budget}) — the record was "
            f"hand-edited or torn; refusing to overwrite it silently")
    if recorded is None:
        # §7.3: the null placeholder is what this call backfills
        final["within_budget"] = within
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(final, ensure_ascii=False, indent=2,
                                  sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    return {"within_budget": within}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    ap_final = sub.add_parser(
        "final-budget", help="variant full-train final within-budget gate")
    ap_final.add_argument("--artifacts", required=True)
    ap_final.add_argument("--vid", required=True)
    ns = ap.parse_args()

    try:
        artifacts = Path(ns.artifacts)
        if ns.command == "final-budget":
            result: dict = final_budget(artifacts, ns.vid)
        else:  # pragma: no cover - argparse enforces the subcommand set
            raise ValueError(f"verdict_decide: unknown command {ns.command!r}")
    except (OSError, ValueError, KeyError) as exc:
        print(f"verdict_decide: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

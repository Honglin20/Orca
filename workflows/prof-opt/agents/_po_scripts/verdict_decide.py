#!/usr/bin/env python3
"""verdict_decide.py — direction-normalized accuracy-budget verdicts, scripted.

Two terminal verdicts live here (the probe stage's accuracy gate and the
full-train stage's final within-budget gate). Both read the numbers they
judge from the workspace files that RECORDED them — a verdict line is never
hand-derived from values copied into a protocol call. Both take the budget
from the frozen origin anchor (base/origin_anchor.json `accuracy_budget`) —
the anchor is the single immutable source; a missing anchor fails loud.

  promote       --artifacts <ws> --vid <VID>
      reads variants/<VID>/metrics/epoch_compare.json (the compare step's
      recorded `pass` + `baseline_metric` + `normalized_loss`), contracts.json
      (eval.metric_direction), and — only when present — the variant eval
      (variants/<VID>/eval/proxy.json `metric_value`) and the baseline
      anchor (baseline/baseline_k_acc.json `baseline_k_acc`). The eval gate
      applies only when BOTH eval numbers exist; either absent → curve-only
      judgment. gap = the WORST of the two gate gaps in budget units
      (higher_better gap = anchor − value, lower_better gap = value −
      anchor; pass <=> gap <= budget). stdout: {"curve_pass", "eval_acc",
      "eval_pass", "line", "accuracy_pass", "gap"}.

  final-budget  --artifacts <ws>
      reads final/final_acc.json (`final_acc`, `baseline_full_acc`,
      `metric_direction` — its `within_budget` may still be null: this call
      is what computes it). stdout: {"within_budget": <bool>}.

Budget semantics: slack = 1.0 x budget (the fixed relaxation factor);
higher_better passes at value >= anchor - slack, lower_better at
value <= anchor + slack. stdout is a single-line JSON; every bad input
fails loud (stderr message, exit 2).
"""
from __future__ import annotations

import argparse
import json
import math
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


def _gap(value: float, anchor: float, direction: str) -> float:
    """Direction-normalized distance from the line (bigger = further)."""
    return anchor - value if direction == "higher_better" else value - anchor


def _optional_number(path: Path, what: str, key: str) -> float | None:
    """The recorded number when the file exists; None when it does not.

    A file that exists but is malformed fails loud — a present-but-unreadable
    anchor must never silently downgrade the judgment to curve-only."""
    if not path.is_file():
        return None
    return _number(_load_json(path, what).get(key), f"{what} {key!r}")


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


def promote(artifacts: Path, vid: str) -> dict:
    budget = anchor_budget(artifacts)
    slack = budget  # fixed relaxation factor 1.0 x budget
    compare = _load_json(
        artifacts / "variants" / vid / "metrics" / "epoch_compare.json",
        "the pinned-depth comparison result")
    curve_ok = compare.get("pass")
    if not isinstance(curve_ok, bool):
        raise ValueError(
            "verdict_decide: epoch_compare.json 'pass' is not a boolean "
            f"(run the compare step first): {curve_ok!r}")
    baseline_metric = _number(compare.get("baseline_metric"),
                              "epoch_compare.json 'baseline_metric'")
    curve_gap = _number(compare.get("normalized_loss"),
                        "epoch_compare.json 'normalized_loss'")
    contracts = _load_json(artifacts / "contracts.json", "contracts.json")
    eval_block = contracts.get("eval")
    if not isinstance(eval_block, dict):
        raise ValueError("verdict_decide: contracts.json has no eval object")
    direction = _direction(eval_block.get("metric_direction"),
                           "contracts.json eval.metric_direction")

    eval_acc = _optional_number(
        artifacts / "variants" / vid / "eval" / "proxy.json",
        "the variant k-ckpt eval result", "metric_value")
    baseline_k_acc = _optional_number(
        artifacts / "baseline" / "baseline_k_acc.json",
        "the baseline k-ckpt anchor", "baseline_k_acc")

    line = (baseline_metric - slack if direction == "higher_better"
            else baseline_metric + slack)
    if eval_acc is None or baseline_k_acc is None:
        eval_ok = True
        gap = curve_gap  # curve-only judgment: the curve gap IS the gap
    else:
        eval_ok = _passes(eval_acc, baseline_k_acc, direction, slack)
        gap = max(curve_gap, _gap(eval_acc, baseline_k_acc, direction))
    return {"curve_pass": curve_ok, "eval_acc": eval_acc,
            "eval_pass": eval_ok, "line": line,
            "accuracy_pass": curve_ok and eval_ok, "gap": gap}


def final_budget(artifacts: Path) -> dict:
    budget = anchor_budget(artifacts)
    slack = budget
    final = _load_json(artifacts / "final" / "final_acc.json",
                       "the final accuracy record")
    final_acc = _number(final.get("final_acc"), "final_acc.json 'final_acc'")
    anchor = _number(final.get("baseline_full_acc"),
                     "final_acc.json 'baseline_full_acc'")
    direction = _direction(final.get("metric_direction"),
                           "final_acc.json 'metric_direction'")
    return {"within_budget": _passes(final_acc, anchor, direction, slack)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    ap_promote = sub.add_parser(
        "promote", help="probe accuracy gate (curve + optional k-ckpt eval)")
    ap_promote.add_argument("--artifacts", required=True)
    ap_promote.add_argument("--vid", required=True)

    ap_final = sub.add_parser(
        "final-budget", help="full-train final within-budget gate")
    ap_final.add_argument("--artifacts", required=True)
    ns = ap.parse_args()

    try:
        artifacts = Path(ns.artifacts)
        if ns.command == "promote":
            if not ns.vid:
                raise ValueError("verdict_decide: --vid must be non-empty")
            result: dict = promote(artifacts, ns.vid)
        else:
            result = final_budget(artifacts)
    except (OSError, ValueError, KeyError) as exc:
        print(f"verdict_decide: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

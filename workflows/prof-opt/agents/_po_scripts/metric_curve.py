#!/usr/bin/env python3
"""Epoch-aligned training-metric extraction and comparison.

The script is deliberately independent of the training framework.  A workflow
contract supplies a regular expression containing named groups ``epoch`` and
``metric``.  Comparison is at the latest *common* epoch by default, never at
two different training budgets; ``compare --at-epoch k`` pins the comparison
depth to epoch k instead (either curve lacking the k-th point fails loud —
comparing at two different depths is exactly the unfairness this tool exists
to prevent). The emitted ``at_epoch`` always records the depth actually
compared, and ``baseline_path`` records which curve anchored the comparison.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


class MetricCurveError(RuntimeError):
    """Raised when the log cannot prove the requested metric curve."""


def _extract(log_path: Path, pattern: str) -> list[dict[str, int | float]]:
    try:
        regex = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise MetricCurveError(f"invalid epoch metric regex: {exc}") from exc
    if "epoch" not in regex.groupindex or "metric" not in regex.groupindex:
        raise MetricCurveError(
            "epoch metric regex needs named groups 'epoch' and 'metric'")

    if not log_path.is_file():
        raise MetricCurveError(f"training log not found: {log_path}")
    text = log_path.read_text(encoding="utf-8", errors="replace")
    points: list[dict[str, int | float]] = []
    seen: set[int] = set()
    for match in regex.finditer(text):
        try:
            epoch = int(match.group("epoch"))
            metric = float(match.group("metric"))
        except (TypeError, ValueError) as exc:
            raw = match.group(0)
            raise MetricCurveError(
                f"epoch/metric in log line is not numeric: {raw!r}") from exc
        if epoch in seen:
            raise MetricCurveError(f"duplicate metric for epoch {epoch}")
        seen.add(epoch)
        points.append({"epoch": epoch, "metric": metric})

    points.sort(key=lambda item: int(item["epoch"]))
    if not points:
        raise MetricCurveError(f"no epoch metric matched in {log_path}")
    expected = list(range(1, len(points) + 1))
    actual = [int(item["epoch"]) for item in points]
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        raise MetricCurveError(
            f"epoch sequence is not contiguous from 1: got {actual}; "
            f"missing examples={missing[:10]}")
    return points


def load_curve(path: Path) -> list[dict[str, int | float]]:
    if not path.is_file():
        raise MetricCurveError(f"metric curve not found: {path}")
    try:
        rows = [json.loads(line) for line in path.read_text(
            encoding="utf-8").splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise MetricCurveError(f"invalid JSONL metric curve {path}: {exc}") from exc
    if not rows:
        raise MetricCurveError(f"metric curve is empty: {path}")
    return rows


def compare(baseline: list[dict[str, int | float]],
            candidate: list[dict[str, int | float]],
            *, direction: str, budget: float,
            at_epoch: int | None = None,
            baseline_path: str = "") -> dict[str, Any]:
    if direction not in {"higher_better", "lower_better"}:
        raise MetricCurveError(
            "direction must be higher_better or lower_better, got "
            f"{direction!r}")
    if budget < 0:
        raise MetricCurveError("accuracy budget must be >= 0")
    if at_epoch is not None and at_epoch < 1:
        raise MetricCurveError(f"--at-epoch must be >= 1, got {at_epoch}")
    base_epochs = {int(row["epoch"]): float(row["metric"]) for row in baseline}
    cand_epochs = {int(row["epoch"]): float(row["metric"]) for row in candidate}
    common = sorted(set(base_epochs) & set(cand_epochs))
    if not common:
        raise MetricCurveError("baseline and candidate share no epoch")
    if at_epoch is None:
        epoch = common[-1]
    else:
        # pinned depth: BOTH curves must carry the k-th point — a missing
        # point is a fail-loud contract breach, never a silent fallback to a
        # shallower (unfair) comparison depth
        for name, curve in (("baseline", base_epochs), ("candidate", cand_epochs)):
            if at_epoch not in curve:
                raise MetricCurveError(
                    f"{name} curve lacks epoch {at_epoch} "
                    f"(has up to {max(curve)}) — pinned-depth comparison "
                    f"cannot proceed at a different depth")
        epoch = at_epoch
    base = base_epochs[epoch]
    cand = cand_epochs[epoch]
    # Positive loss always means the candidate is worse after normalization.
    loss = base - cand if direction == "higher_better" else cand - base
    passed = loss <= budget
    return {
        "epoch": epoch,
        "at_epoch": epoch,          # depth actually compared (== epoch)
        "baseline_path": baseline_path,  # which curve anchored the comparison
        "baseline_metric": base,
        "candidate_metric": cand,
        "normalized_loss": loss,
        "budget": budget,
        "metric_direction": direction,
        "pass": passed,
    }


def _contract_pattern(contract_path: Path) -> str:
    if not contract_path.is_file():
        raise MetricCurveError(f"contract not found: {contract_path}")
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        rule = contract["train"]["epoch_metric_extraction"]
        pattern = rule["pattern"] if isinstance(rule, dict) else rule
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise MetricCurveError(
            "contracts.json lacks train.epoch_metric_extraction.pattern") from exc
    return str(pattern)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract", help="parse a log into metric curve JSONL")
    extract.add_argument("--log", required=True)
    extract.add_argument("--out", required=True)
    extract.add_argument("--pattern")
    extract.add_argument("--contract")
    extract.add_argument("--expected-epochs", type=int)

    cmp = sub.add_parser("compare", help="compare two curves at the same epoch")
    cmp.add_argument("--baseline", required=True)
    cmp.add_argument("--candidate", required=True)
    cmp.add_argument("--direction", required=True,
                     choices=["higher_better", "lower_better"])
    cmp.add_argument("--budget", required=True, type=float)
    cmp.add_argument("--at-epoch", type=int,
                     help="pin the comparison depth to epoch k (either curve "
                          "lacking the k-th point fails loud); default = "
                          "latest common epoch")

    ns = ap.parse_args()
    try:
        if ns.command == "extract":
            if bool(ns.pattern) == bool(ns.contract):
                raise MetricCurveError("use exactly one of --pattern or --contract")
            pattern = ns.pattern or _contract_pattern(Path(ns.contract))
            points = _extract(Path(ns.log), pattern)
            if ns.expected_epochs is not None:
                if len(points) != ns.expected_epochs:
                    raise MetricCurveError(
                        f"expected {ns.expected_epochs} epoch metrics, parsed "
                        f"{len(points)}")
            out = Path(ns.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("".join(
                json.dumps(row, sort_keys=True) + "\n" for row in points),
                encoding="utf-8")
            print(json.dumps({"path": str(out), "epochs": len(points)}))
        else:
            result = compare(load_curve(Path(ns.baseline)),
                             load_curve(Path(ns.candidate)),
                             direction=ns.direction, budget=ns.budget,
                             at_epoch=ns.at_epoch,
                             baseline_path=str(ns.baseline))
            print(json.dumps(result, sort_keys=True))
        return 0
    except MetricCurveError as exc:
        print(f"metric_curve: FAIL {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

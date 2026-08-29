#!/usr/bin/env python3
"""Pre-return gate for po_full_train.

Verifies final/final_acc.json completeness, the promised checkpoint and final
onnx existence, and the full_train_budget fingerprint. It never re-judges
training quality; it only checks that the terminal artifacts are present and
structurally complete.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path


def _load_json(path: Path, what: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise ValueError(f"{what} unparseable: {path} ({exc})") from exc


def _checkpoint_exists(contracts: dict, final_dir: Path) -> bool:
    rule = (contracts.get("train") or {}).get("ckpt_output_rule")
    if not isinstance(rule, str) or not rule:
        return False
    pattern = rule.replace("{out_dir}", str(final_dir))
    if "*" in pattern:
        return bool(glob.glob(pattern))
    return Path(pattern).is_file()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True)
    ns = ap.parse_args()
    art = Path(ns.artifacts)
    problems: list[str] = []

    contracts_path = art / "contracts.json"
    try:
        contracts = _load_json(contracts_path, "contracts.json")
    except ValueError as exc:
        print(f"check_full_train_emit: FAIL {exc}", file=sys.stderr)
        return 1
    if contracts is None or not isinstance(contracts, dict):
        print("check_full_train_emit: FAIL contracts.json missing or invalid",
              file=sys.stderr)
        return 1

    final_acc_path = art / "final" / "final_acc.json"
    try:
        final_acc = _load_json(final_acc_path, "final/final_acc.json")
    except ValueError as exc:
        problems.append(str(exc))
    else:
        if final_acc is None or not isinstance(final_acc, dict):
            problems.append("final/final_acc.json missing or invalid")
        else:
            for key in ("vid", "final_acc", "baseline_full_acc",
                        "baseline_full_acc_source", "full_train_budget",
                        "within_budget", "metric_direction"):
                if key not in final_acc:
                    problems.append(f"final/final_acc.json missing {key}")
            if not isinstance(final_acc.get("final_acc"), (int, float)):
                problems.append("final_acc must be a number")
            if not isinstance(final_acc.get("baseline_full_acc"), (int, float)):
                problems.append("baseline_full_acc must be a number")
            if final_acc.get("baseline_full_acc_source") != "baseline":
                problems.append("baseline_full_acc_source must be 'baseline'")
            if final_acc.get("full_train_budget") != contracts.get("full_train_budget"):
                problems.append("full_train_budget fingerprint mismatch")
            if not isinstance(final_acc.get("within_budget"), bool):
                problems.append("within_budget must be a boolean")
            if final_acc.get("metric_direction") not in ("higher_better", "lower_better"):
                problems.append("metric_direction must be higher_better|lower_better")

    final_dir = art / "final"
    if not (final_dir / "model.onnx").is_file():
        problems.append("final/model.onnx missing")
    if not (final_dir / "train_status.md").is_file() or (final_dir / "train_status.md").stat().st_size == 0:
        problems.append("final/train_status.md missing or empty")
    if not _checkpoint_exists(contracts, final_dir):
        problems.append("final checkpoint matching contracts train.ckpt_output_rule missing")
    metrics_path = final_dir / "final_metrics.jsonl"
    if not metrics_path.is_file() or metrics_path.stat().st_size == 0:
        problems.append("final/final_metrics.jsonl missing or empty")
    else:
        try:
            for line_no, line in enumerate(
                    metrics_path.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip() and not isinstance(json.loads(line), dict):
                    problems.append(
                        f"final/final_metrics.jsonl:{line_no} is not a JSON object")
        except json.JSONDecodeError as exc:
            problems.append(f"final/final_metrics.jsonl unparseable: {exc}")

    if problems:
        for p in problems:
            print(f"check_full_train_emit: FAIL {p}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Pre-assembly gate for the three po_contract sub-agent proposal files."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path: Path, what: str):
    if not path.is_file():
        raise ValueError(f"{what} missing: {path}")
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True)
    ns = ap.parse_args()
    art = Path(ns.artifacts)
    problems: list[str] = []

    checks = {
        "train_contract_proposal.json": {
            "tier": ("A", "B", "C"),
            "entry_sha256": None,
            "flags": None,
            "ckpt_output_rule": None,
            "ckpt_per_epoch": None,
            "epoch_metric_extraction": None,
            "train_epochs_full": None,
        },
        "eval_contract_proposal.json": {
            "tier": ("A", "B", "C"),
            "entry_sha256": None,
            "flags": None,
            "ckpt_container": None,
            "metric_extraction": None,
            "metric_direction": None,
        },
        "export_contract_proposal.json": {
            "entry_sha256": None,
            "generated": None,
            "argv_facts": None,
        },
    }
    for name, required in checks.items():
        try:
            doc = _load(art / "contract_work" / name, name)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        except json.JSONDecodeError as exc:
            problems.append(f"{name} unparseable: {exc}")
            continue
        if not isinstance(doc, dict):
            problems.append(f"{name} is not a JSON object")
            continue
        for key, allowed in required.items():
            if key not in doc:
                problems.append(f"{name} missing {key}")
            elif allowed is not None and doc[key] not in allowed:
                problems.append(f"{name} {key} must be one of {allowed}")

    if problems:
        for p in problems:
            print(f"check_contract_subagent_output: FAIL {p}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

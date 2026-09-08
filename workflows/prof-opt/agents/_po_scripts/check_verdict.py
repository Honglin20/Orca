#!/usr/bin/env python3
"""Check that a variant is strictly faster than the current incumbent."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid or missing {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def check_verdict(artifacts: Path, vid: str, makespan: int | None = None) -> dict:
    anchor = _load(artifacts / "base" / "origin_anchor.json")
    target = anchor.get("target_cycles")
    if not isinstance(target, int) or isinstance(target, bool):
        raise ValueError("origin anchor has no integer target_cycles")

    incumbent_path = artifacts / "base" / "incumbent.json"
    if incumbent_path.is_file():
        incumbent = _load(incumbent_path)
        incumbent_ms = incumbent.get("makespan_cycles")
        incumbent_vid = incumbent.get("vid")
    else:
        incumbent_ms = anchor.get("baseline_makespan_cycles")
        incumbent_vid = None
    if not isinstance(incumbent_ms, int) or isinstance(incumbent_ms, bool):
        raise ValueError("current incumbent has no integer makespan_cycles")

    if makespan is None:
        verdict = _load(artifacts / "variants" / vid / "verdict.json")
        variant_ms = verdict.get("makespan_cycles")
    else:
        variant_ms = makespan
    if not isinstance(variant_ms, int) or isinstance(variant_ms, bool):
        raise ValueError(f"{vid} has no integer makespan_cycles")
    if variant_ms >= incumbent_ms:
        raise ValueError(
            f"{vid} makespan {variant_ms} is not below incumbent {incumbent_ms}")
    return {
        "vid": vid,
        "makespan_cycles": variant_ms,
        "incumbent_vid": incumbent_vid,
        "incumbent_makespan_cycles": incumbent_ms,
        "improvement_cycles": incumbent_ms - variant_ms,
        "target_cycles": target,
        "target_met": variant_ms <= target,
        "ok": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vid", required=True)
    parser.add_argument("--artifacts", default=os.environ.get("ORCA_ARTIFACTS_DIR"))
    parser.add_argument("--makespan", type=int)
    args = parser.parse_args()
    if not args.artifacts:
        print("check_verdict: FAIL artifacts path is required", file=sys.stderr)
        return 2
    try:
        result = check_verdict(Path(args.artifacts), args.vid, args.makespan)
    except ValueError as exc:
        print(f"check_verdict: FAIL {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

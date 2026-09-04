#!/usr/bin/env python3
"""Promote the best accuracy-safe latency improvement to the current base."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"missing {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"unparseable {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    result = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no} is not valid JSON: {exc}") from exc
        if isinstance(row, dict):
            result.append(row)
    return result


def _makespan(row: dict) -> int | None:
    for key in ("makespan_cycles", "measured_makespan_cycles"):
        value = row.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _atomic_replace_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise ValueError(f"missing source tree {source}")
    temporary = destination.parent / f".{destination.name}.promote.{uuid.uuid4().hex}"
    shutil.copytree(source, temporary)
    if destination.exists():
        shutil.rmtree(destination)
    os.replace(temporary, destination)


def promote(artifacts: Path) -> dict:
    base = artifacts / "base"
    latest: dict[str, dict] = {}
    for row in _rows(artifacts / "history.jsonl"):
        vid = row.get("vid")
        if isinstance(vid, str) and vid:
            latest[vid] = row

    current_path = base / "incumbent.json"
    if current_path.is_file():
        current = _read_json(current_path)
        current_vid = current.get("vid")
        current_ms = current.get("makespan_cycles")
    else:
        anchor = _read_json(base / "origin_anchor.json")
        current_vid = None
        current_ms = anchor.get("baseline_makespan_cycles")
        current = {"vid": None, "makespan_cycles": current_ms,
                   "parent_vid": None, "promoted_round": 0,
                   "source": "baseline"}
    if not isinstance(current_ms, int) or isinstance(current_ms, bool) or current_ms < 0:
        raise ValueError(f"current incumbent has invalid makespan_cycles: {current_ms!r}")

    candidates = []
    for vid, row in latest.items():
        if row.get("outcome") != "success" or vid == current_vid:
            continue
        makespan = _makespan(row)
        if makespan is not None and makespan < current_ms:
            candidates.append((makespan, vid, row))
    if not candidates:
        return {"promoted": False, "vid": current_vid,
                "makespan_cycles": current_ms}

    makespan, vid, row = min(candidates, key=lambda item: (item[0], item[1]))
    variant = artifacts / "variants" / vid
    for required in (variant / "shadow", variant / "onnx" / "model.onnx",
                     variant / "profile"):
        if not required.exists():
            raise ValueError(f"cannot promote {vid}: missing {required}")

    _atomic_replace_tree(variant / "shadow", artifacts / "shadow")
    base.mkdir(parents=True, exist_ok=True)
    shutil.copy2(variant / "onnx" / "model.onnx", base / "model.onnx")
    _atomic_replace_tree(variant / "profile", base / "profile")
    payload = {
        "vid": vid,
        "parent_vid": row.get("parent_vid"),
        "makespan_cycles": makespan,
        "promoted_round": row.get("round"),
        "change_sig": row.get("change_sig"),
    }
    temporary = current_path.with_suffix(current_path.suffix + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(temporary, current_path)
    return {"promoted": True, **payload}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", default=os.environ.get("ORCA_ARTIFACTS_DIR"))
    args = parser.parse_args()
    if not args.artifacts:
        print("promote_incumbent: --artifacts or ORCA_ARTIFACTS_DIR is required",
              file=sys.stderr)
        return 2
    try:
        print(json.dumps(promote(Path(args.artifacts)), ensure_ascii=False))
    except (OSError, ValueError, KeyError) as exc:
        print(f"promote_incumbent: FAIL {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

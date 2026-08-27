#!/usr/bin/env python3
"""advance_round.py — deterministic, replayable end-of-round advance.

Recomputes the round outcome from history.jsonl (never from node outputs) and
applies the fixed-order atomic step sequence:

    1. this round's promoted set (latest-version outcome == promoted)
    2. overall winner = best over {incumbent best.json, this round's promoted}
       (makespan minimal, tie -> higher proxy_acc, tie -> stable vid order)
    3. copy winner onnx+profile -> base/, replace global shadow/ — EVERY time
       the sequence runs with a winner, not only when best.json changed: the
       copy is a pure overwrite of never-deleted variant artifacts, so a
       replay after a crash between steps converges instead of silently
       leaving best.json and base/ pointing at different rounds
    4. write best.json, then .round_advanced LAST (the commit point);
       both use tmp-file + os.replace so a torn write cannot strand a
       half-written JSON on disk

Idempotency key = round NUMBER (not marker existence): when
.round_advanced.round == max round under rounds/, the advance already
happened and the script returns a no-op; otherwise the sequence is replayed.
base/bottleneck_report.json is intentionally NOT copied: the next propose
step re-runs analyze on the new base profile.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from history_lib import read_latest  # noqa: E402

MARKER_NAME = ".round_advanced"


def _max_round(rounds_dir: Path) -> int:
    if not rounds_dir.is_dir():
        return 0
    numbers = [int(c.name) for c in rounds_dir.iterdir()
               if c.is_dir() and c.name.isdigit()]
    return max(numbers) if numbers else 0


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _copy_tree_replace(src: Path, dst: Path) -> None:
    """Replace dst with src, excluding __pycache__ dirs and *.pyc files."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    def _excluded(rel: Path) -> bool:
        return "__pycache__" in rel.parts or rel.suffix == ".pyc"

    for item in sorted(src.rglob("*")):
        rel = item.relative_to(src)
        if _excluded(rel):
            continue
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _promoted_this_round(artifacts: Path, round_no: int) -> list[dict]:
    latest = read_latest(artifacts / "history.jsonl")
    return [row for row in latest.values()
            if row.get("outcome") == "promoted"
            and row.get("round") == round_no
            and "makespan_cycles" in row]


def _rank_key(row: dict) -> tuple:
    """Lower is better: minimal makespan, then higher proxy_acc, then vid."""
    return (row["makespan_cycles"], -(row.get("proxy_acc") or 0.0), row["vid"])


def advance(artifacts: Path) -> dict:
    rounds_dir = artifacts / "rounds"
    round_no = _max_round(rounds_dir)
    if round_no == 0:
        raise FileNotFoundError("advance_round: no rounds/<NNN>/ directory exists yet")

    marker_path = artifacts / MARKER_NAME
    if marker_path.is_file():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("round") == round_no:
            return {"advanced": False, "round": round_no, "vid": marker.get("vid"),
                    "best_updated": marker.get("best_updated", False),
                    "reason": "marker round == current max round; already advanced"}

    promoted = sorted(_promoted_this_round(artifacts, round_no),
                      key=lambda r: r["vid"])
    best_path = artifacts / "best.json"
    current_best: dict | None = None
    if best_path.is_file():
        current_best = json.loads(best_path.read_text(encoding="utf-8"))

    # overall winner over incumbent + this round's promoted (may be None when
    # nothing was ever promoted)
    candidates = list(promoted)
    if current_best is not None:
        candidates.append(current_best)
    winner = min(candidates, key=_rank_key) if candidates else None
    winner_vid = winner["vid"] if winner is not None else None

    best_updated = False
    if winner is not None and (current_best is None
                               or current_best.get("vid") != winner_vid):
        _atomic_write_json(best_path, {
            "vid": winner_vid,
            "makespan_cycles": winner["makespan_cycles"],
            "proxy_acc": winner.get("proxy_acc"),
            "round": round_no,
            "profile_dir": str(artifacts / "variants" / winner_vid / "profile"),
        })
        best_updated = True

    # unconditional winner copy: a stale marker means base/ and shadow/ must be
    # re-derived; re-copying the incumbent is an idempotent no-op in content
    if winner_vid is not None:
        variant_dir = artifacts / "variants" / winner_vid
        onnx_src = variant_dir / "onnx" / "model.onnx"
        profile_src = variant_dir / "profile"
        shadow_src = variant_dir / "shadow"
        for required in (onnx_src, profile_src, shadow_src):
            if not required.exists():
                raise FileNotFoundError(
                    f"advance_round: winner {winner_vid} is missing {required}")
        base_dir = artifacts / "base"
        base_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(onnx_src, base_dir / "model.onnx")
        _copy_tree_replace(profile_src, base_dir / "profile")
        _copy_tree_replace(shadow_src, artifacts / "shadow")

    marker = {
        "round": round_no,
        "vid": winner_vid,
        "promoted_count": len(promoted),
        "best_updated": best_updated,
    }
    _atomic_write_json(marker_path, marker)
    return {"advanced": True, **marker}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ns = ap.parse_args()
    try:
        result = advance(Path(ns.artifacts))
    except (OSError, ValueError, KeyError) as exc:
        print(f"advance_round: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

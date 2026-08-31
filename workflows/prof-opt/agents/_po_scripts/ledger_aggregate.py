#!/usr/bin/env python3
"""ledger_aggregate.py — deterministic aggregator for the experiment ledger (v6 §7.5).

The shared experiment_ledger.json is a DERIVED artifact. Its truth source is
the per-variant shards variants/<vid>/ledger_entry.json — each written ONLY
by that variant's watchdog (atomic replace, single-writer: no cross-variant
read-modify-write, no lost rows) — plus the baseline row
(baseline/ledger_entry.json when present). This script collects the shard
set and atomically replaces the shared file.

Contract:
  - pure function: the same shard set always yields the same output bytes
    (no wall-clock, no environment, no history re-derivation) — lock-free
    and re-entrant; concurrent runs converge, a stale aggregate is fixed by
    the next trigger point, no shard data is ever lost;
  - the shared file can be fully rebuilt from the shards at any moment
    (deleting it and re-running yields the identical content);
  - row order is deterministic: baseline first, then variants by vid;
  - a shard that exists but is unparseable / not an object fails loud —
    shards are written atomically, so a torn read is a real anomaly, not a
    race to paper over.

Row shape = the shard verbatim (watchdog-owned fields: vid / status /
epoch / metric / gap / device / change_summary / ts).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCHEMA_VERSION = 2
BASELINE_SHARD = Path("baseline") / "ledger_entry.json"
VARIANTS_GLOB = "variants/*/ledger_entry.json"


def _load_shard(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"ledger_aggregate: shard unparseable: {path} ({exc})") from exc
    except OSError as exc:
        raise ValueError(f"ledger_aggregate: shard unreadable: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"ledger_aggregate: shard is not a JSON object: {path}")
    return data


def collect(artifacts: Path) -> dict:
    """Pure read side: the aggregate payload for the CURRENT shard set."""
    rows: list[dict] = []
    has_baseline = False
    baseline_path = artifacts / BASELINE_SHARD
    if baseline_path.is_file():
        rows.append(_load_shard(baseline_path))
        has_baseline = True
    for shard in sorted(artifacts.glob(VARIANTS_GLOB), key=lambda p: p.parent.name):
        rows.append(_load_shard(shard))
    return {"schema_version": SCHEMA_VERSION,
            "variant_count": len(rows) - (1 if has_baseline else 0),
            "rows": rows}


def aggregate(artifacts: Path) -> dict:
    """Collect the shard set and atomically replace the shared ledger file."""
    payload = collect(artifacts)
    target = artifacts / "experiment_ledger.json"
    tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                              sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ns = ap.parse_args()
    try:
        payload = aggregate(Path(ns.artifacts))
    except (OSError, ValueError) as exc:
        print(f"ledger_aggregate: FAIL {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    print(json.dumps({"ledger": str(Path(ns.artifacts) / "experiment_ledger.json"),
                      "variant_count": payload["variant_count"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

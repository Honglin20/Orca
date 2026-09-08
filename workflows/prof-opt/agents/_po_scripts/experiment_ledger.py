#!/usr/bin/env python3
"""Render the prof-opt experiment ledger view from the per-variant shards.

v6 (§7.5): experiment_ledger.json is a DERIVED artifact — the truth source
is variants/<vid>/ledger_entry.json (one shard per variant, each owned by
that variant's watchdog) plus the baseline shard. This entry point
(1) runs the deterministic aggregator (ledger_aggregate.py) so the shared
file reflects the current shard set, then (2) renders the human-readable
experiment_summary.md from the aggregated rows. The v5 history-derived
build (read-modify-write over history.jsonl + proposals.json) is retired —
it could not survive concurrent watchdog writers.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger_aggregate  # noqa: E402


def _markdown(ledger: dict, generated_at: str) -> str:
    lines = [
        "# Prof-Opt Experiment Ledger", "",
        f"Generated: `{generated_at}` · Variants: `{ledger['variant_count']}`"
        " (derived from per-variant shards; see §7.5)", "",
        "| VID | Status | Epoch | Metric | Gap | Device | Change summary |",
        "|---|---|---:|---:|---:|---|---|",
    ]
    for row in ledger["rows"]:
        def f(key: str) -> str:
            value = row.get(key)
            return "" if value is None else str(value)
        lines.append(
            f"| `{row.get('vid', '')}` | {f('status')} | {f('epoch')} | "
            f"{f('metric')} | {f('gap')} | {f('device')} |"
            f" {f('change_summary')} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ns = ap.parse_args()
    root = Path(ns.artifacts)
    try:
        ledger = ledger_aggregate.aggregate(root)
    except (OSError, ValueError) as exc:
        print(f"experiment_ledger: FAIL {exc}", file=sys.stderr)
        return 2
    # display-only timestamp: the aggregated JSON itself stays pure/deterministic
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary = root / "experiment_summary.md"
    tmp = summary.with_suffix(summary.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(_markdown(ledger, generated_at), encoding="utf-8")
    os.replace(tmp, summary)
    print(json.dumps({
        "ledger": str(root / "experiment_ledger.json"),
        "summary": str(root / "experiment_summary.md"),
        "variant_count": ledger["variant_count"],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())

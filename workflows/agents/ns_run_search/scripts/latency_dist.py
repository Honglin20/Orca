#!/usr/bin/env python3
"""latency_dist.py -- Search candidate latency distribution histogram.

Reads search_results.jsonl, extracts latency values from the NAS nested path,
bins them into a histogram, and pushes a ``bar`` chart.

Fail-soft on missing jsonl or no latency field.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    discover_metric_info,
    extract_numeric_values,
    push_chart,
    read_jsonl,
)

_NUM_BINS = 10


def main() -> int:
    ap = argparse.ArgumentParser(description="Push search latency distribution histogram.")
    ap.add_argument("--artifacts-dir", required=True)
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)

    records = read_jsonl(ad / "search_results.jsonl")
    if not records:
        push_chart(
            artifacts_dir_path=ad, script_name="latency_dist", label="nas-supernet/search",
            title="Search Latency Distribution", chart_type="bar", data=[],
            skip_reason="search_results.jsonl missing or empty",
        )
        return 0

    info = discover_metric_info(ad, records)
    latency_path = info.latency_path if info else ""
    if not latency_path:
        push_chart(
            artifacts_dir_path=ad, script_name="latency_dist", label="nas-supernet/search",
            title="Search Latency Distribution", chart_type="bar", data=[],
            skip_reason="cannot identify latency field in records",
        )
        return 0

    latencies = extract_numeric_values(records, latency_path)
    if not latencies:
        push_chart(
            artifacts_dir_path=ad, script_name="latency_dist", label="nas-supernet/search",
            title="Search Latency Distribution", chart_type="bar", data=[],
            skip_reason="no valid latency values in records",
        )
        return 0

    data = _histogram(latencies)
    push_chart(
        artifacts_dir_path=ad,
        script_name="latency_dist",
        label="nas-supernet/search",
        title="Search Latency Distribution",
        chart_type="bar",
        data=data,
        x="bin",
        y="count",
        x_label="Latency bin (ms)",
        y_label="Number of candidates",
        caption=f"Distribution of {len(latencies)} candidate latencies ({_NUM_BINS} bins). Shows search-space coverage.",
    )
    return 0


def _histogram(values: list[float]) -> list[dict[str, Any]]:
    lo = min(values)
    hi = max(values)
    if lo == hi:
        return [{"bin": f"{lo:.2f}", "count": len(values)}]

    width = (hi - lo) / _NUM_BINS
    counts = [0] * _NUM_BINS
    for v in values:
        idx = int((v - lo) / width)
        if idx >= _NUM_BINS:
            idx = _NUM_BINS - 1
        counts[idx] += 1

    data: list[dict[str, Any]] = []
    for i in range(_NUM_BINS):
        bin_lo = lo + i * width
        bin_hi = bin_lo + width
        data.append({"bin": f"{bin_lo:.1f}-{bin_hi:.1f}", "count": counts[i]})
    return data


if __name__ == "__main__":
    sys.exit(main())

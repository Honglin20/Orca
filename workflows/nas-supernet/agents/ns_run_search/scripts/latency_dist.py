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
    discover_latency_unit,
    discover_metric_info,
    extract_numeric_values,
    push_chart,
    read_jsonl,
)

_NUM_BINS = 10


def main() -> int:
    ap = argparse.ArgumentParser(description="Push search latency distribution histogram.")
    ap.add_argument("--artifacts-dir", required=True)
    ap.add_argument(
        "--latency-unit", default="",
        help="override latency unit (default: discover from search_record_schema.json)",
    )
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)
    unit = args.latency_unit.strip() or discover_latency_unit(ad)

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
    # NaN/overflow sentinels (float32 max) dropped — they would skew the bins.
    valid_latencies = [v for v in latencies if abs(v) < 1e6]

    # All-sentinel or all-0.0 latency → SYNTHESIZED placeholder bar
    # via normal push_chart (NOT skip_reason — empty data would skip rendering and the
    # diagnostic would never reach the front-end). The single bar carries a diagnostic
    # caption explaining the failure mode (sentinels vs all-zero timer-resolution issue).
    if not valid_latencies:
        _push_diagnostic(ad, unit, latencies, sentinel=True)
        return 0
    if all(v == 0.0 for v in valid_latencies):
        _push_diagnostic(ad, unit, valid_latencies, sentinel=False)
        return 0

    data = _histogram(valid_latencies)
    push_chart(
        artifacts_dir_path=ad,
        script_name="latency_dist",
        label="nas-supernet/search",
        title="Search Latency Distribution",
        chart_type="bar",
        data=data,
        x="bin",
        y="count",
        x_label=f"Latency bin ({unit})",
        y_label="Number of candidates",
        caption=f"Distribution of {len(valid_latencies)} candidate latencies ({_NUM_BINS} bins). Shows search-space coverage.",
    )
    return 0


def _push_diagnostic(ad, unit: str, raw_values: list[float], *, sentinel: bool) -> None:
    """Push a one-bar diagnostic chart when latency data is unusable.

    Two cases share the placeholder-bar shape ``{"bin": "(no valid data)", "count": 0}``:
      - ``sentinel=True``: every raw value was a NaN/overflow sentinel (likely the
        latency estimator failed for every candidate). Caption flags this.
      - ``sentinel=False``: every value is ``0.0`` (likely timer resolution too coarse
        for the workload on this device). Caption flags timer resolution.

    Goes through normal ``push_chart`` so the chart actually renders (skip_reason would
    suppress rendering — empty-data failure would be invisible).
    """
    if sentinel:
        diag = (
            "All latency values are NaN/overflow sentinels — measurement likely failed "
            "(estimator crashed / CUDA error / script returned non-finite). "
            "Check latency_estimator.py + run_search logs."
        )
    else:
        diag = (
            "All latency values are 0.0 — timer resolution too low for this workload on "
            "the current device (measure_module_latency returns 0.0 when elapsed time "
            "falls below perf_counter precision). Try larger batch_size / more repetitions "
            "/ a different device."
        )
    push_chart(
        artifacts_dir_path=ad,
        script_name="latency_dist",
        label="nas-supernet/search",
        title="Search Latency Distribution",
        chart_type="bar",
        data=[{"bin": "(no valid data)", "count": 0}],
        x="bin",
        y="count",
        x_label=f"Latency bin ({unit})",
        y_label="Number of candidates",
        caption=f"{len(raw_values)} candidates inspected. {diag}",
    )


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

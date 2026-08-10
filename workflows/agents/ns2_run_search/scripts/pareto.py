#!/usr/bin/env python3
"""pareto.py -- Pareto front scatter (latency vs metric) for nas-supernet.

Reads search_results.jsonl + selected-arch coords (from ns_select output via CLI args).
Pushes a ``pareto`` chart: all candidates as points, frontend computes + highlights
the non-dominated front. The selected architecture is annotated in the caption.

NAS data convention: stored objectives are smaller-is-better. If the metric is a
higher-better metric stored as negated (all values <= 0), values are un-negated for
display and pareto_y_direction is set to "max".

Fail-soft: missing/empty jsonl or undiscoverable fields -> records "skipped".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    discover_metric_info,
    flatten_record,
    init_marker,
    push_chart,
    read_jsonl,
    safe_float,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Push Pareto front scatter chart.")
    ap.add_argument("--artifacts-dir", required=True, help="$ORCA_ARTIFACTS_DIR")
    ap.add_argument("--selected-latency-ms", default="")
    ap.add_argument("--selected-acc", default="")
    args = ap.parse_args()

    ad = Path(args.artifacts_dir)
    init_marker(ad)

    records = read_jsonl(ad / "search_results.jsonl")
    if not records:
        push_chart(
            artifacts_dir_path=ad, script_name="pareto", label="nas-supernet/search",
            title="Pareto Front", chart_type="pareto", data=[],
            skip_reason="search_results.jsonl missing or empty",
        )
        return 0

    info = discover_metric_info(ad, records)
    if info is None or not info.latency_path or not info.field_path:
        push_chart(
            artifacts_dir_path=ad, script_name="pareto", label="nas-supernet/search",
            title="Pareto Front", chart_type="pareto", data=[],
            skip_reason=f"cannot identify metric/latency fields (info={info})",
        )
        return 0

    # Build scatter data from raw stored values, then convert for display.
    # NaN/overflow sentinels (float32 max from failed evaluations) are dropped —
    # they would otherwise pin a single point at 3.4e38 and wreck the chart.
    data: list[dict[str, float]] = []
    for rec in records:
        flat = flatten_record(rec)
        lat_raw = flat.get(info.latency_path)
        met_raw = flat.get(info.field_path)
        if lat_raw is None or met_raw is None:
            continue
        try:
            lat = float(lat_raw)
            met_stored = float(met_raw)
        except (ValueError, TypeError):
            continue
        # Keep only finite, in-range values. Positive-inclusion form (`abs < 1e6`)
        # matters: NaN fails `nan < x` (so it's dropped), whereas `nan >= 1e6` is
        # False and would let real NaN leak through.
        if not (abs(met_stored) < 1e6 and abs(lat) < 1e6):
            continue
        data.append({"latency": lat, "metric": info.for_display(met_stored)})

    if not data:
        push_chart(
            artifacts_dir_path=ad, script_name="pareto", label="nas-supernet/search",
            title=f"Pareto Front ({info.name})", chart_type="pareto", data=[],
            skip_reason="no valid (latency, metric) pairs in records",
        )
        return 0

    # Pareto directions on DISPLAY values:
    # latency always min; metric: "max" if display-direction is higher, else "min".
    pareto_y_dir = "max" if info.display_direction == "higher" else "min"

    # Caption with selected-arch annotation.
    metric_label = info.name.replace("_", " ").title()
    caption_parts = [f"{len(data)} candidates; Pareto front computed by front-end."]
    sel_lat = safe_float(args.selected_latency_ms)
    sel_acc = safe_float(args.selected_acc)
    if sel_lat is not None and sel_acc is not None:
        sel_display = info.for_display(sel_acc) if info.negate_for_display else sel_acc
        caption_parts.append(
            f"Selected arch: latency={sel_lat:.2f}ms, {info.name}={sel_display:.4f}."
        )
    dir_label = info.display_direction
    caption_parts.append(f"{info.name}: {dir_label}-is-better.")
    if info.negate_for_display:
        caption_parts.append("Values un-negated from NAS smaller-is-better storage.")

    push_chart(
        artifacts_dir_path=ad,
        script_name="pareto",
        label="nas-supernet/search",
        title=f"Pareto Front (latency vs {info.name})",
        chart_type="pareto",
        data=data,
        x="latency",
        y="metric",
        pareto_x_direction="min",
        pareto_y_direction=pareto_y_dir,
        x_label="Latency (ms, lower is better)",
        y_label=f"{metric_label} ({dir_label} is better)",
        caption=" ".join(caption_parts),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

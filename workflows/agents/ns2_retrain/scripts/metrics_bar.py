#!/usr/bin/env python3
"""metrics_bar.py -- Cross-phase metric comparison for nas-supernet-v2.

Aggregates the project metric from up to 4 phases into a readable chart:
  1. Supernet eval (best validation metric from training log, if parseable)
  2. Search best (best metric across all search candidates)
  3. Selected arch (from ns2_run_search output via CLI arg)
  4. Retrain final (from runs/retrain/test_metrics.json, if present)

All values are converted to display polarity (un-negated if NAS stores as negated).
Only phases with available data are included. Fail-soft on all-or-nothing missing.

Rendered as a bar chart when data spans multiple phases with visible differences,
or a table when values are near-saturated (accuracy ~0.99 across every phase).

Robustness: the entire data-collection + render is wrapped so that push_chart is
ALWAYS called — even on exception, a "skipped" entry is written to the marker (never
silent rc=0 no-op).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    best_val_metric_from_log,
    discover_metric_info,
    extract_numeric_values,
    final_metric_from_json,
    push_chart,
    read_jsonl,
    safe_float,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Push cross-phase metric comparison.")
    ap.add_argument("--artifacts-dir", required=True)
    ap.add_argument("--selected-acc", default="")
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)

    try:
        return _run(args, ad)
    except Exception as exc:  # noqa: BLE001 -- never silent: always write marker
        push_chart(
            artifacts_dir_path=ad, script_name="metrics_bar",
            label="nas-supernet-v2/metrics",
            title="Metric Across Phases", chart_type="bar", data=[],
            skip_reason=f"unexpected error: {exc}",
        )
        return 0


def _run(args: argparse.Namespace, ad: Path) -> int:
    records = read_jsonl(ad / "search_results.jsonl")
    info = discover_metric_info(ad, records)
    if info is None:
        push_chart(
            artifacts_dir_path=ad, script_name="metrics_bar",
            label="nas-supernet-v2/metrics",
            title="Metric Across Phases", chart_type="bar", data=[],
            skip_reason="cannot discover metric info (search_config.yaml objs or records missing)",
        )
        return 0

    metric_label = info.name.replace("_", " ").title()
    rows: list[dict[str, Any]] = []

    # Phase 1: supernet eval metric from training log (actual values, not NAS-negated).
    supernet_val = best_val_metric_from_log(ad, info.name, info.display_direction)
    if supernet_val is not None:
        rows.append({"phase": "Supernet Eval", "value": supernet_val})

    # Phase 2: best search candidate metric (NAS-negated -> un-negate for display).
    search_vals = extract_numeric_values(records, info.field_path) if records and info.field_path else []
    search_vals = [v for v in search_vals if abs(v) < 1e6]
    if search_vals:
        best_raw = min(search_vals)
        rows.append({"phase": "Search Best", "value": info.for_display(best_raw)})

    # Phase 3: selected arch metric (already natural direction from select output).
    selected_raw = safe_float(args.selected_acc)
    if selected_raw is not None:
        rows.append({"phase": "Selected Arch", "value": selected_raw})

    # Phase 4: retrain final test metric (actual values, not NAS-negated).
    retrain_raw = final_metric_from_json(ad, info.name)
    if retrain_raw is not None:
        rows.append({"phase": "Retrain Final", "value": retrain_raw})

    if not rows:
        push_chart(
            artifacts_dir_path=ad, script_name="metrics_bar",
            label="nas-supernet-v2/metrics",
            title=f"{metric_label} by Pipeline Phase", chart_type="bar", data=[],
            skip_reason=f"no metric data found in any phase (metric={info.name!r})",
        )
        return 0

    dir_label = info.display_direction
    caption = (
        f"Project metric '{info.name}' across NAS pipeline phases ({dir_label}-is-better). "
        f"Supernet Eval = best validation during training; Search Best = best of {len(records)} candidates; "
        f"Selected Arch = selected architecture; Retrain Final = final retrained value. "
        f"{len(rows)}/4 phases have data."
    )
    if info.negate_for_display:
        caption += " Values un-negated from NAS smaller-is-better storage."

    # Render as bar chart: each phase is a bar, x=phase name, y=metric value.
    bar_data = [{"phase": r["phase"], "value": r["value"]} for r in rows]

    push_chart(
        artifacts_dir_path=ad,
        script_name="metrics_bar",
        label="nas-supernet-v2/metrics",
        title=f"{metric_label} by Pipeline Phase",
        chart_type="bar",
        data=bar_data,
        x="phase",
        y="value",
        x_label="Pipeline Phase",
        y_label=metric_label,
        caption=caption,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

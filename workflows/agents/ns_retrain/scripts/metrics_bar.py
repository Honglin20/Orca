#!/usr/bin/env python3
"""metrics_bar.py -- Cross-phase metric comparison TABLE for nas-supernet.

Aggregates the project metric from up to 4 phases into a readable table:
  1. Supernet eval (best validation metric from training log, if parseable)
  2. Search best (best metric across all search candidates)
  3. Selected arch (from ns_select output via CLI arg)
  4. Retrain final (from runs/retrain/test_metrics.json, if present)

All values are converted to display polarity (un-negated if NAS stores as negated).
Only phases with available data are included. Fail-soft on all-or-nothing missing.

Rendered as a table (not a bar chart): the metric is often near-saturated (e.g.
accuracy ~0.99 across every phase), so identical-height bars carry no information;
a table makes the values directly readable.
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
    ap = argparse.ArgumentParser(description="Push cross-phase metric comparison table.")
    ap.add_argument("--artifacts-dir", required=True)
    ap.add_argument("--selected-acc", default="")
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)

    records = read_jsonl(ad / "search_results.jsonl")
    info = discover_metric_info(ad, records)
    if info is None:
        push_chart(
            artifacts_dir_path=ad, script_name="metrics_bar", label="nas-supernet/metrics",
            title="Metric Across Phases", chart_type="table", data=[],
            skip_reason="cannot discover metric info",
        )
        return 0

    metric_label = info.name.replace("_", " ").title()
    rows: list[dict[str, Any]] = []

    # Phase 1: supernet eval metric from training log.
    # Training logs use ACTUAL metric values (not NAS-negated) — display as-is.
    supernet_val = best_val_metric_from_log(ad, info.name, info.display_direction)
    if supernet_val is not None:
        rows.append({"phase": "Supernet Eval", "value": supernet_val})

    # Phase 2: best search candidate metric (NAS-negated → un-negate for display).
    # NaN/overflow sentinels (float32 max) from failed evaluations are filtered —
    # min() would otherwise pick 3.4e38 when the dataset is all-garbage.
    search_vals = extract_numeric_values(records, info.field_path) if records and info.field_path else []
    search_vals = [v for v in search_vals if abs(v) < 1e6]
    if search_vals:
        # NAS: all stored values smaller-is-better → best = min.
        best_raw = min(search_vals)
        rows.append({"phase": "Search Best", "value": info.for_display(best_raw)})

    # Phase 3: selected arch metric (from ns_select stdout — already natural
    # higher-is-better direction per select_architecture.py contract). Display as-is.
    selected_raw = safe_float(args.selected_acc)
    if selected_raw is not None:
        rows.append({"phase": "Selected Arch", "value": selected_raw})

    # Phase 4: retrain final test metric.
    # Retrain test_metrics.json uses ACTUAL values (not NAS-negated) — display as-is.
    retrain_raw = final_metric_from_json(ad, info.name)
    if retrain_raw is not None:
        rows.append({"phase": "Retrain Final", "value": retrain_raw})

    if not rows:
        push_chart(
            artifacts_dir_path=ad, script_name="metrics_bar", label="nas-supernet/metrics",
            title=f"{metric_label} by Pipeline Phase", chart_type="table", data=[],
            skip_reason=f"no metric data found in any phase (metric={info.name!r})",
        )
        return 0

    dir_label = info.display_direction
    caption = (
        f"同一项目指标 '{info.name}' 在 NAS 流水线各阶段的值（{dir_label}-is-better）。"
        f"Supernet Eval = supernet 训练最好验证值；Search Best = 搜索 640 候选中最优；"
        f"Selected Arch = 选定架构（select 产出）；Retrain Final = 重训后最终值。"
        f"共 {len(rows)}/4 阶段有数据。"
    )
    if info.negate_for_display:
        caption += " 值已从 NAS 小-优存储方向取负还原。"

    push_chart(
        artifacts_dir_path=ad,
        script_name="metrics_bar",
        label="nas-supernet/metrics",
        title=f"{metric_label} by Pipeline Phase",
        chart_type="table",
        data=rows,
        columns=["phase", "value"],
        caption=caption,
    )
    return 0


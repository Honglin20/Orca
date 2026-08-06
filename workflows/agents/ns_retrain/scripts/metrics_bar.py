#!/usr/bin/env python3
"""metrics_bar.py -- Cross-phase metric comparison bar chart for nas-supernet.

Aggregates the project metric from up to 4 phases:
  1. Supernet eval (best validation metric from training log, if parseable)
  2. Search best (best metric across all search candidates)
  3. Selected arch (from ns_select output via CLI arg)
  4. Retrain final (from runs/retrain/test_metrics.json, if present)

All values are converted to display polarity (un-negated if NAS stores as negated).
Only phases with available data are included. Fail-soft on all-or-nothing missing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    discover_metric_info,
    extract_numeric_values,
    find_latest_attempt_log,
    flatten_record,
    push_chart,
    read_jsonl,
    read_text,
    safe_float,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Push cross-phase metric comparison bar chart.")
    ap.add_argument("--artifacts-dir", required=True)
    ap.add_argument("--selected-acc", default="")
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)

    records = read_jsonl(ad / "search_results.jsonl")
    info = discover_metric_info(ad, records)
    if info is None:
        push_chart(
            artifacts_dir_path=ad, script_name="metrics_bar", label="nas-supernet/metrics",
            title="Metric Across Phases", chart_type="bar", data=[],
            skip_reason="cannot discover metric info",
        )
        return 0

    metric_label = info.name.replace("_", " ").title()
    bars: list[dict[str, Any]] = []

    # Phase 1: supernet eval metric from training log.
    # Training logs use ACTUAL metric values (not NAS-negated) — display as-is.
    supernet_val = _best_val_metric(ad, info.name, info.display_direction)
    if supernet_val is not None:
        bars.append({"phase": "Supernet Eval", info.name: supernet_val})

    # Phase 2: best search candidate metric (NAS-negated → un-negate for display).
    search_vals = extract_numeric_values(records, info.field_path) if records and info.field_path else []
    if search_vals:
        # NAS: all stored values smaller-is-better → best = min.
        best_raw = min(search_vals)
        bars.append({"phase": "Search Best", info.name: info.for_display(best_raw)})

    # Phase 3: selected arch metric (from ns_select, reads search_results → NAS-negated).
    selected_raw = safe_float(args.selected_acc)
    if selected_raw is not None:
        bars.append({"phase": "Selected Arch", info.name: info.for_display(selected_raw)})

    # Phase 4: retrain final test metric.
    # Retrain test_metrics.json uses ACTUAL values (not NAS-negated) — display as-is.
    retrain_raw = _retrain_final_metric(ad, info.name)
    if retrain_raw is not None:
        bars.append({"phase": "Retrain Final", info.name: retrain_raw})

    if not bars:
        push_chart(
            artifacts_dir_path=ad, script_name="metrics_bar", label="nas-supernet/metrics",
            title=f"{metric_label} Across Phases", chart_type="bar", data=[],
            skip_reason=f"no metric data found in any phase (metric={info.name!r})",
        )
        return 0

    dir_label = info.display_direction
    caption = f"Project metric '{info.name}' ({dir_label}-is-better) across NAS pipeline phases. {len(bars)} of 4 phases available."
    if info.negate_for_display:
        caption += " Values un-negated from NAS smaller-is-better storage."

    push_chart(
        artifacts_dir_path=ad,
        script_name="metrics_bar",
        label="nas-supernet/metrics",
        title=f"{metric_label} Across Phases",
        chart_type="bar",
        data=bars,
        x="phase",
        y=info.name,
        x_label="Pipeline Phase",
        y_label=f"{metric_label} ({dir_label} is better)",
        caption=caption,
    )
    return 0


def _best_val_metric(ad: Path, metric_name: str, display_direction: str) -> float | None:
    """Parse training log for the best validation metric matching ``metric_name``.

    Training logs use ACTUAL metric values (not NAS-negated). ``best`` is determined
    by ``display_direction``: higher → max, lower → min.
    """
    log_path = find_latest_attempt_log(ad, "train", "train")
    if log_path is None:
        return None

    text = read_text(log_path)
    if not text:
        return None

    best: float | None = None
    mn_lower = metric_name.lower()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        val: float | None = None

        # JSON line.
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                for key in (f"val_{metric_name}", f"test_{metric_name}", metric_name, "val_metric", "best_metric"):
                    if key in rec:
                        try:
                            val = float(rec[key])
                            break
                        except (ValueError, TypeError):
                            pass
        except json.JSONDecodeError:
            pass

        # Regex fallback on text.
        if val is None:
            for pattern in (
                rf"val_{re.escape(mn_lower)}\s*[:=]\s*([\d.eE+-]+)",
                rf"{re.escape(mn_lower)}\s*[:=]\s*([\d.eE+-]+)",
            ):
                m = re.search(pattern, line, re.IGNORECASE)
                if m:
                    try:
                        val = float(m.group(1))
                        break
                    except ValueError:
                        pass

        if val is not None:
            if best is None:
                best = val
            elif display_direction == "higher" and val > best:
                best = val
            elif display_direction == "lower" and val < best:
                best = val

    return best


def _retrain_final_metric(ad: Path, metric_name: str) -> float | None:
    """Read retrain final test metric from ``runs/retrain/test_metrics.json``.

    Returns raw stored value. Retrain scripts typically write un-negated values
    (e.g. 0.92 for accuracy), so the caller should NOT double-negate.
    """
    metrics_path = ad / "runs" / "retrain" / "test_metrics.json"
    if not metrics_path.is_file():
        return None
    try:
        data = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None

    for key in (metric_name, f"test_{metric_name}", f"val_{metric_name}", "test_metric", "best_metric", "metric"):
        if key in data:
            try:
                return float(data[key])
            except (ValueError, TypeError):
                pass

    # Fallback: first numeric value that is not loss.
    for key, val in data.items():
        if "loss" in key.lower():
            continue
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            try:
                return float(val)
            except (ValueError, TypeError):
                continue
    return None


if __name__ == "__main__":
    sys.exit(main())

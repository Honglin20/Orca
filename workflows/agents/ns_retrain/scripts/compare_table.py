#!/usr/bin/env python3
"""compare_table.py -- Full-supernet vs selected-subnet comparison table.

Builds a 2-column comparison table (Full Supernet vs Selected Subnet) across:
  - Parameters / FLOPs / Latency (ms) / Project metric

Data sources (best-effort, each row independent):
  - Full Supernet params/FLOPs: parsed from supernet_summary.md or inspect_supernet.py output.
  - Full Supernet metric: trained supernet's real best validation metric from the
    training log (fallback: best search candidate when the log is unavailable).
  - Full Supernet latency: max latency across candidates.
  - Selected Subnet latency/metric: from ns_select output (CLI args, already natural).

Metric values are un-negated for display if NAS stores them negated.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    best_val_metric_from_log,
    discover_metric_info,
    extract_numeric_values,
    push_chart,
    read_jsonl,
    read_text,
    run_inspect_supernet,
    safe_float,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Push supernet-vs-subnet comparison table.")
    ap.add_argument("--artifacts-dir", required=True)
    ap.add_argument("--selected-latency-ms", default="")
    ap.add_argument("--selected-acc", default="")
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)

    records = read_jsonl(ad / "search_results.jsonl")
    info = discover_metric_info(ad, records)

    metric_name = info.name if info else "metric"
    metric_label = metric_name.replace("_", " ").title()

    # Harvest full-supernet stats.
    inspect_output = run_inspect_supernet(ad)
    summary_text = read_text(ad / "supernet_summary.md")
    harvest = inspect_output + "\n" + summary_text

    full_params = _parse_numeric(harvest, ("parameters", "params", "num_params", "param_count"))
    full_flops = _parse_numeric(harvest, ("flops", "gflops", "macs", "gmacs"))

    # Full supernet latency proxy: max latency across candidates.
    full_latency = None
    if info and info.latency_path and records:
        lats = extract_numeric_values(records, info.latency_path)
        if lats:
            full_latency = max(lats)

    # Full supernet metric: the trained supernet's real best validation metric
    # (from the training log) — NOT a proxy over search candidates. Fallback when
    # the training log is unavailable: best search candidate (min negated acc),
    # which is a defensible lower bound on supernet quality. NaN/overflow
    # sentinels (float32 max) are filtered so they can never surface.
    full_metric = None
    full_metric_source = ""
    if info:
        full_metric = best_val_metric_from_log(ad, info.name, info.display_direction)
        if full_metric is not None:
            full_metric_source = "train-log best validation"
        elif info.field_path and records:
            vals = extract_numeric_values(records, info.field_path)
            valid = [v for v in vals if abs(v) < 1e6]
            if valid:
                # NAS: smaller-is-better -> best = min (most negative = highest acc).
                full_metric = info.for_display(min(valid))
                full_metric_source = "search best candidate (train log unavailable)"

    sel_latency = safe_float(args.selected_latency_ms)
    # Selected metric comes from ns_select stdout — already natural
    # higher-is-better direction (select_architecture.py contract). Do NOT negate.
    sel_metric_raw = safe_float(args.selected_acc)
    sel_metric = sel_metric_raw

    # Build comparison rows.
    rows: list[dict[str, str]] = []
    _add_row(rows, "Parameters", full_params, None)
    _add_row(rows, "FLOPs", full_flops, None)
    _add_row(rows, "Latency (ms)", full_latency, sel_latency)
    _add_row(rows, metric_label, full_metric, sel_metric)

    rows = [r for r in rows if r["Full Supernet"] != "-" or r["Selected Subnet"] != "-"]

    if not rows:
        push_chart(
            artifacts_dir_path=ad, script_name="compare_table", label="nas-supernet/comparison",
            title="Full Supernet vs Selected Subnet", chart_type="table", data=[],
            skip_reason="no comparable stats found (summary + inspect_supernet produced nothing)",
        )
        return 0

    caption = f"Full-open supernet vs selected subnet. Metric: {metric_name}."
    if full_metric_source:
        caption += f" Full Supernet metric = {full_metric_source}."
    if info and info.negate_for_display:
        caption += " Metric values un-negated from NAS storage."
    caption += " '-' = source did not report."

    push_chart(
        artifacts_dir_path=ad,
        script_name="compare_table",
        label="nas-supernet/comparison",
        title="Full Supernet vs Selected Subnet",
        chart_type="table",
        data=rows,
        columns=["metric", "Full Supernet", "Selected Subnet"],
        caption=caption,
    )
    return 0


def _add_row(rows: list[dict[str, str]], metric: str, full: float | None, sel: float | None) -> None:
    rows.append({
        "metric": metric,
        "Full Supernet": _fmt(full),
        "Selected Subnet": _fmt(sel),
    })


def _fmt(val: float | None) -> str:
    if val is None:
        return "-"
    if abs(val) >= 1e9:
        return f"{val / 1e9:.2f}G"
    if abs(val) >= 1e6:
        return f"{val / 1e6:.2f}M"
    if abs(val) >= 1e3:
        return f"{val / 1e3:.2f}K"
    return f"{val:.4f}".rstrip("0").rstrip(".") or "0"


def _parse_numeric(text: str, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        pat = rf"{re.escape(key)}\s*[:=]?\s*([\d.]+(?:[eE][+-]?\d+)?\s*[KMGTkmgt]?)"
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return _parse_si(m.group(1))
    return None


def _parse_si(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    multipliers = {"k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}
    suffix = raw[-1].lower()
    if suffix in multipliers:
        try:
            return float(raw[:-1]) * multipliers[suffix]
        except ValueError:
            return None
    try:
        return float(raw)
    except ValueError:
        return None


if __name__ == "__main__":
    sys.exit(main())

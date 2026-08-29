#!/usr/bin/env python3
"""compare_table.py -- All-original-path vs selected-subnet comparison table.

Builds a 2-column comparison table (All-original path (baseline anchor) vs Selected
Subnet) across:
  - Parameters / FLOPs / Latency (<unit>) / Project metric

The baseline anchor column is the **all-original path** — every layer slot on its
original (frozen, pretrained-inherited) branch, i.e. the pretrained model equivalent
inside the supernet — NOT a max-capacity configuration (the search space is
choice-only; dims are pinned to the original model's measured values).

Data sources (best-effort, each row independent):
  - Anchor params/FLOPs: parsed from supernet_summary.md or inspect_supernet.py output.
  - Anchor metric: trained supernet's real best validation metric from the
    training log (fallback: best search candidate when the log is unavailable).
  - Anchor latency: REAL measurement from ``.full_supernet_latency.json``
    (written by ``full_supernet_latency.py`` — the all-original path latency —
    using the same LatencyEstimator as the search). Fallback when that file is
    missing: ``max(valid candidate latencies)`` proxy with sentinel filter +
    caption marker.
  - Selected Subnet latency/metric: from psu_select output (CLI args, already natural).

Metric values are un-negated for display if NAS stores them negated.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    best_val_metric_from_log,
    discover_latency_unit,
    discover_metric_info,
    extract_numeric_values,
    push_chart,
    read_jsonl,
    read_text,
    run_inspect_supernet,
    safe_float,
)

# Baseline anchor column: the all-original path (every slot on its frozen original
# branch ≈ the pretrained model inside the supernet). Choice-only search has no
# "max-capacity" configuration to compare against.
ANCHOR_COL = "All-original path (baseline anchor)"


def main() -> int:
    ap = argparse.ArgumentParser(description="Push supernet-vs-subnet comparison table.")
    ap.add_argument("--artifacts-dir", required=True)
    ap.add_argument("--selected-latency", default="")
    ap.add_argument("--selected-acc", default="")
    ap.add_argument(
        "--latency-unit", default="",
        help="override latency unit (default: discover from search_record_schema.json)",
    )
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)
    unit = args.latency_unit.strip() or discover_latency_unit(ad)

    records = read_jsonl(ad / "search_results.jsonl")
    info = discover_metric_info(ad, records)

    metric_name = info.name if info else "metric"
    metric_label = metric_name.replace("_", " ").title()

    # Harvest baseline-anchor (all-original path) stats.
    inspect_output = run_inspect_supernet(ad)
    summary_text = read_text(ad / "supernet_summary.md")
    harvest = inspect_output + "\n" + summary_text

    full_params = _parse_numeric(harvest, ("parameters", "params", "num_params", "param_count"))
    full_flops = _parse_numeric(harvest, ("flops", "gflops", "macs", "gmacs"))

    # Anchor (all-original path) latency:
    #   1. Prefer ``.full_supernet_latency.json`` (real measurement via search's estimator).
    #   2. Fallback: ``max(valid candidate latencies)`` proxy — NaN/overflow sentinels
    #      (float32 max from failed evals) filtered so they can never surface as 3.4e38.
    full_latency, latency_source = _resolve_full_latency(ad, info, records)

    # Anchor metric: the trained supernet's real best validation metric
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

    sel_latency = safe_float(args.selected_latency)
    # Selected metric comes from psu_select stdout — already natural
    # higher-is-better direction (select_architecture.py contract). Do NOT negate.
    sel_metric_raw = safe_float(args.selected_acc)
    sel_metric = sel_metric_raw

    # Build comparison rows.
    rows: list[dict[str, str]] = []
    _add_row(rows, "Parameters", full_params, None)
    _add_row(rows, "FLOPs", full_flops, None)
    _add_row(rows, f"Latency ({unit})", full_latency, sel_latency)
    _add_row(rows, metric_label, full_metric, sel_metric)

    rows = [r for r in rows if r[ANCHOR_COL] != "-" or r["Selected Subnet"] != "-"]

    if not rows:
        push_chart(
            artifacts_dir_path=ad, script_name="compare_table", label="puzzle-supernet/comparison",
            title=f"{ANCHOR_COL} vs Selected Subnet", chart_type="table", data=[],
            skip_reason="no comparable stats found (summary + inspect_supernet produced nothing)",
        )
        return 0

    caption = f"All-original path (baseline anchor) vs selected subnet. Metric: {metric_name}."
    if latency_source:
        caption += f" Anchor latency = {latency_source}."
    if full_metric_source:
        caption += f" Anchor metric = {full_metric_source}."
    if info and info.negate_for_display:
        caption += " Metric values un-negated from NAS storage."
    caption += " '-' = source did not report."

    push_chart(
        artifacts_dir_path=ad,
        script_name="compare_table",
        label="puzzle-supernet/comparison",
        title=f"{ANCHOR_COL} vs Selected Subnet",
        chart_type="table",
        data=rows,
        columns=["metric", ANCHOR_COL, "Selected Subnet"],
        caption=caption,
    )
    return 0


def _resolve_full_latency(
    ad: Path, info, records: list[dict],
) -> tuple[float | None, str]:
    """Resolve the baseline-anchor (all-original path) latency.

    Returns ``(latency, source_description)``:
      - ``.full_supernet_latency.json`` exists with finite latency ≥ 0 (0.0 is a
        legitimate measurement) → ``(value, "real measurement via search LatencyEstimator
        (.full_supernet_latency.json, source=<src>)")``.
      - Missing/unreadable/non-finite → fallback to ``max(valid candidates)`` proxy with
        sentinel filter; source notes "proxy max(candidate latencies)".
      - No candidates / no valid → ``(None, "")``.
    """
    measured = _read_full_supernet_latency(ad)
    if measured is not None:
        val, src = measured
        return val, f"real measurement via search LatencyEstimator (.full_supernet_latency.json, source={src})"

    if info and info.latency_path and records:
        lats = extract_numeric_values(records, info.latency_path)
        # Filter NaN/overflow sentinels + non-finite; latency>=0 is LEGITIMATE
        # (don't drop all-zero measurements — those are a separate diagnostic).
        valid = [v for v in lats if math.isfinite(v) and v >= 0 and abs(v) < 1e6]
        if valid:
            return max(valid), "proxy max(candidate latencies) — .full_supernet_latency.json unavailable"
    return None, ""


def _read_full_supernet_latency(ad: Path) -> tuple[float, str] | None:
    """Read ``.full_supernet_latency.json``; return ``(latency, source)`` or None.

    File shape: ``{"latency": <num>, "unit": <str>, "source": <str>}``.
    Accepts source ∈ {estimator, proxy}. Latency must be finite and ≥0 (0.0 valid;
    caption annotation handles the 0.0 case at the caller side via source label).
    Returns None on missing file / parse error / non-finite value.
    """
    path = ad / ".full_supernet_latency.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        val = float(data.get("latency"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val < 0:
        return None
    src = str(data.get("source", "unknown"))
    return val, src


def _add_row(rows: list[dict[str, str]], metric: str, full: float | None, sel: float | None) -> None:
    rows.append({
        "metric": metric,
        ANCHOR_COL: _fmt(full),
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

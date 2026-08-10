#!/usr/bin/env python3
"""search_table.py -- Search results table for nas-supernet.

Reads search_results.jsonl and pushes a ``table`` chart: one row per candidate with
arch-config digest / latency / metric / Pareto flag. Columns are ordered for
readability (Pareto candidates first, then by metric).

Metric values are un-negated for display if the NAS convention stores them as negated
(all values <= 0). Fail-soft on missing/empty jsonl or undiscoverable fields.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    discover_metric_info,
    flatten_record,
    LATENCY_FIELDS,
    PARETO_FIELDS,
    push_chart,
    read_jsonl,
    find_field,
)

# Fields excluded from the "arch config" digest.
_NON_ARCH_KEYS: frozenset[str] = frozenset(
    PARETO_FIELDS + LATENCY_FIELDS + ("index", "id", "rank", "generation", "gen", "individual", "gene", "objs", "arch", "cached")
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Push search results table chart.")
    ap.add_argument("--artifacts-dir", required=True)
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)

    records = read_jsonl(ad / "search_results.jsonl")
    if not records:
        push_chart(
            artifacts_dir_path=ad, script_name="search_table", label="nas-supernet/search",
            title="Search Results — All Candidates", chart_type="table", data=[],
            skip_reason="search_results.jsonl missing or empty",
        )
        return 0

    info = discover_metric_info(ad, records)
    if info is None or not info.latency_path or not info.field_path:
        push_chart(
            artifacts_dir_path=ad, script_name="search_table", label="nas-supernet/search",
            title="Search Results — All Candidates", chart_type="table", data=[],
            skip_reason=f"cannot identify metric/latency fields (info={info})",
        )
        return 0

    pareto_field = find_field(records, PARETO_FIELDS)
    exclude_set = {info.latency_path, info.field_path, pareto_field, "objs", "gene", "cached"}

    rows: list[dict[str, Any]] = []
    for idx, rec in enumerate(records, start=1):
        flat = flatten_record(rec)
        lat_raw = flat.get(info.latency_path)
        lat = "-"
        try:
            lat_stored = float(lat_raw) if lat_raw is not None else None
        except (ValueError, TypeError):
            lat_stored = None
        # NaN/overflow sentinels (float32 max) shown as "-", same as the metric col.
        if lat_stored is not None and abs(lat_stored) < 1e6:
            lat = _to_str(lat_stored)
        met_raw = flat.get(info.field_path)
        met = "-"
        try:
            met_stored = float(met_raw) if met_raw is not None else None
        except (ValueError, TypeError):
            met_stored = None
        # NaN/overflow sentinels (float32 max from failed evals) shown as "-", not
        # a bogus 3.4e38.
        if met_stored is not None and abs(met_stored) < 1e6:
            met = _to_str(info.for_display(met_stored))
        is_pareto = _pareto_label(flat.get(pareto_field)) if pareto_field else ""
        arch_digest = _arch_digest(flat, exclude_set)
        rows.append({
            "#": idx,
            "arch": arch_digest,
            "latency_ms": lat,
            info.name: met,
            "pareto": is_pareto,
        })

    # Sort: Pareto candidates first, then by display metric (best first).
    best_first = info.display_direction == "higher"
    rows.sort(key=lambda r: _sort_key(r, info.name, best_first))
    for new_idx, row in enumerate(rows, start=1):
        row["#"] = new_idx

    columns = ["#", "arch", "latency_ms", info.name, "pareto"]
    caption = f"{len(rows)} candidates. Sorted: Pareto first, then best {info.name}."
    if info.negate_for_display:
        caption += f" {info.name} values un-negated from NAS storage."

    push_chart(
        artifacts_dir_path=ad,
        script_name="search_table",
        label="nas-supernet/search",
        title="Search Results — All Candidates",
        chart_type="table",
        data=rows,
        columns=columns,
        caption=caption,
    )
    return 0


def _sort_key(row: dict[str, Any], metric_name: str, best_first: bool) -> tuple[int, float]:
    pareto_rank = 0 if row.get("pareto") else 1
    try:
        met_val = float(row[metric_name])
    except (ValueError, TypeError, KeyError):
        met_val = float("-inf") if best_first else float("inf")
    metric_rank = -met_val if best_first else met_val
    return (pareto_rank, metric_rank)


def _pareto_label(val: Any) -> str:
    if isinstance(val, bool):
        return "yes" if val else ""
    if val is None:
        return ""
    return "yes" if str(val).lower() in ("true", "1", "yes") else ""


def _to_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, float):
        return f"{val:.4f}".rstrip("0").rstrip(".") or "0"
    return str(val)


def _arch_digest(flat: dict[str, Any], exclude: set[str]) -> str:
    parts: list[str] = []
    for key, val in flat.items():
        if key in _NON_ARCH_KEYS or key in exclude:
            continue
        if isinstance(val, (bool, list, dict)):
            continue
        parts.append(f"{key}={_to_str(val)}")
    return ", ".join(parts) if parts else "(see arch)"


if __name__ == "__main__":
    sys.exit(main())

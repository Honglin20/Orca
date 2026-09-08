#!/usr/bin/env python3
"""search_table.py -- Search Pareto-front table for puzzle-supernet.

Reads search_results.jsonl and pushes a ``table`` chart: one row per **Pareto-front
architecture** (deduped across generations) with arch columns / latency /
metric / Pareto flag. Columns are ordered for readability (by metric, best first).

Arch rendering: choice-only records (``arch = {"choices": [branch, ...]}`` — the PSU
transformer-layer codec form; ``{"choice": name}`` dict entries supported) get **one
column per slot** (``slot_1..slot_N``); other arch shapes fall back to the scalar-field
digest column. Dedup key = the full per-slot choice tuple / digest.

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
    discover_latency_unit,
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
    ap.add_argument(
        "--latency-unit", default="",
        help="override latency unit (default: discover from search_record_schema.json)",
    )
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)
    unit = args.latency_unit.strip() or discover_latency_unit(ad)
    latency_col = f"latency_{unit}"

    records = read_jsonl(ad / "search_results.jsonl")
    if not records:
        push_chart(
            artifacts_dir_path=ad, script_name="search_table", label="puzzle-supernet/search",
            title="Search Results — Pareto Front", chart_type="table", data=[],
            skip_reason="search_results.jsonl missing or empty",
        )
        return 0

    info = discover_metric_info(ad, records)
    if info is None or not info.latency_path or not info.field_path:
        push_chart(
            artifacts_dir_path=ad, script_name="search_table", label="puzzle-supernet/search",
            title="Search Results — Pareto Front", chart_type="table", data=[],
            skip_reason=f"cannot identify metric/latency fields (info={info})",
        )
        return 0

    pareto_field = find_field(records, PARETO_FIELDS)
    has_pareto_field = bool(pareto_field)
    exclude_set = {info.latency_path, info.field_path, pareto_field, "objs", "gene", "cached"}

    # Dedup by architecture: search logs record every generation's full
    # population, so the same gene/arch recurs across generations (parent kept +
    # mutated offspring). Keep one row per distinct arch — prefer the Pareto
    # entry, then the best metric value.
    best_first = info.display_direction == "higher"

    def _met_num(row: dict[str, Any]) -> float | None:
        try:
            return float(row[info.name])
        except (ValueError, TypeError, KeyError):
            return None

    seen: dict[str, dict[str, Any]] = {}
    n_slots = 0
    for rec in records:
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
        choices = _choices_from_arch(flat)
        if choices is not None:
            # choice-only arch: dedup key + one column per slot (slot_1..slot_N).
            n_slots = max(n_slots, len(choices))
            arch_key = "choices=" + ",".join(choices)
            row = {
                **{f"slot_{i + 1}": name for i, name in enumerate(choices)},
                latency_col: lat,
                info.name: met,
                "pareto": is_pareto,
            }
        else:
            arch_key = _arch_digest(flat, exclude_set)
            if not arch_key or arch_key == "(see arch)":
                continue  # no usable arch key -> skip (nothing to dedup or display)
            row = {
                "arch": arch_key,
                latency_col: lat,
                info.name: met,
                "pareto": is_pareto,
            }
        prev = seen.get(arch_key)
        if prev is None:
            seen[arch_key] = row
            continue
        # Keep the better representative: pareto over non-pareto, then best metric.
        prev_pareto = bool(prev.get("pareto"))
        cur_pareto = bool(is_pareto)
        if cur_pareto and not prev_pareto:
            seen[arch_key] = row
            continue
        if cur_pareto == prev_pareto:
            prev_met = _met_num(prev)
            cur_met = _met_num(row)
            if cur_met is not None and prev_met is not None:
                if (best_first and cur_met > prev_met) or (not best_first and cur_met < prev_met):
                    seen[arch_key] = row

    rows = list(seen.values())
    use_slots = n_slots > 0

    # Determine whether to show only the Pareto front or degrade to all deduped rows.
    # Degradation triggers when: no recognizable pareto field in the jsonl, OR the
    # field exists but no row has pareto=yes (all rows pareto=no / empty).
    pareto_rows = [r for r in rows if bool(r.get("pareto"))]
    degrade_to_all = not has_pareto_field or not pareto_rows

    if not degrade_to_all:
        rows = pareto_rows
        title = "Search Results — Pareto Front"
        caption = (
            f"{len(rows)} Pareto-front architectures "
            f"(deduped from {len(records)} records). Sorted by best {info.name}."
        )
    else:
        title = "Search Results — All Architectures (no Pareto labels)"
        caption = (
            f"{len(rows)} architectures "
            f"(deduped from {len(records)} records, sorted by best {info.name}). "
            f"未识别 Pareto 标注（jsonl 无 PARETO_FIELDS 字段 / 无前沿行），展示全部去重架构。"
        )

    if info.negate_for_display:
        caption += f" {info.name} values un-negated from NAS storage."

    # Sort by display metric (best first).
    rows.sort(key=lambda r: _sort_key(r, info.name, best_first))
    for new_idx, row in enumerate(rows, start=1):
        row["#"] = new_idx

    if use_slots:
        # choice-only arch records present: per-slot columns replace the arch digest.
        columns = ["#", *[f"slot_{i + 1}" for i in range(n_slots)], latency_col, info.name, "pareto"]
        caption += " Columns slot_i = per-slot branch choice (choice-only arch `choices` list)."
    else:
        columns = ["#", "arch", latency_col, info.name, "pareto"]

    push_chart(
        artifacts_dir_path=ad,
        script_name="search_table",
        label="puzzle-supernet/search",
        title=title,
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


def _choices_from_arch(flat: dict[str, Any]) -> list[str] | None:
    """Extract per-slot branch names from a flattened record's ``arch.choices``.

    Supports the choice-only arch forms produced by the PSU search codec:
    ``arch = {"choices": ["random_synthesizer", ...]}`` (per-slot branch-name list)
    and ``{"choices": [{"choice": name}, ...]}`` (dict entries). Returns None when
    the key is absent / empty / not one of these shapes (caller falls back to the
    scalar-field digest).
    """
    raw = flat.get("arch.choices")
    if not isinstance(raw, list) or not raw:
        return None
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            if not entry:
                return None
            names.append(entry)
        elif isinstance(entry, dict):
            choice = entry.get("choice")
            if not isinstance(choice, str) or not choice:
                return None
            names.append(choice)
        else:
            return None
    return names


def _arch_digest(flat: dict[str, Any], exclude: set[str]) -> str:
    """Human-readable architecture digest from a flattened record.

    Prefers the structured ``arch`` keys (flattened as ``arch.layer_configs`` /
    ``arch.stage_depths``) — rendered as ``stage1: a(k3)+b; stage2: c(k5)``.
    Falls back to flattening non-arch scalar fields when no ``arch`` is present.
    """
    layer_configs = flat.get("arch.layer_configs")
    if isinstance(layer_configs, dict):
        digest = _arch_layer_configs_to_str(layer_configs)
        if digest:
            return digest
    parts: list[str] = []
    for key, val in flat.items():
        if key in _NON_ARCH_KEYS or key in exclude:
            continue
        if isinstance(val, (bool, list, dict)):
            continue
        parts.append(f"{key}={_to_str(val)}")
    return ", ".join(parts) if parts else "(see arch)"


def _arch_layer_configs_to_str(layer_configs: dict[str, Any]) -> str:
    """Render the ``layer_configs`` dict as a short per-stage digest.

    Example: ``stage1: res_conv(k3)+mnist_cnn; stage2: mnist_cnn(k5)``.
    """
    stage_parts: list[str] = []
    for stage_name, layers in layer_configs.items():
        if not isinstance(layers, list):
            continue
        layer_descs: list[str] = []
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            choice = str(layer.get("choice", ""))
            cfg = layer.get("config")
            if isinstance(cfg, dict) and cfg:
                # Render every scalar config key (k/h/e/… are project-specific;
                # hardcoding a whitelist risks two distinct archs colliding when
                # their differing params fall outside it). Stable order = sorted.
                parts = []
                for key in sorted(cfg):
                    val = cfg[key]
                    if isinstance(val, bool):
                        continue
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        parts.append(f"{key}={val}")
                suffix = "(" + ",".join(parts) + ")" if parts else ""
            else:
                suffix = ""
            layer_descs.append(f"{choice}{suffix}")
        if layer_descs:
            stage_parts.append(f"{stage_name}: {'+'.join(layer_descs)}")
    return "; ".join(stage_parts)


if __name__ == "__main__":
    sys.exit(main())

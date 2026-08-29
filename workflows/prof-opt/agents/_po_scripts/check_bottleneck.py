#!/usr/bin/env python3
"""check_bottleneck.py — validate bottleneck_analysis.json (node-side check).

The bottleneck-analyst subagent writes ``base/bottleneck_analysis.json``: an
LLM narrative layered on top of the MECHANICAL analyze.py report. Everything
mechanical in it must be a faithful reference — this check enforces that, so
the structure-proposer downstream never reasons from fabricated numbers:

  * closed schema     — unknown top-level or entry keys fail loud;
  * referential       — entry ``name`` must be a ``pattern_id`` of the base
                        report (``base/bottleneck_report.json``), and its
                        ``op_type`` / ``cycles`` must equal that pattern's
                        ``op_type`` / ``total_cycles`` verbatim;
  * order-preserving SUBSET (not a prefix requirement) — the chosen entries
                        appear in the base report's rank order (a strictly
                        increasing index sequence), which with the base
                        report's own descending-total_cycles sort also makes
                        the analysis's cycle column non-increasing.

stdout: ``{"ok": true, ...}`` on success; every violation -> stderr + exit 2.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_TOP_KEYS = {"schema_version", "base_report", "summary", "top_bottlenecks"}
_ENTRY_KEYS = {"name", "op_type", "cycles", "analysis"}


class CheckError(RuntimeError):
    """Raised on any validation violation — callers fail loud, never patch."""


def _load_json(path: Path, what: str) -> Any:
    if not path.is_file():
        raise CheckError(f"{what} not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CheckError(f"{what} is not valid JSON ({path}): {exc}") from exc


def check(analysis_path: Path, artifacts: Path) -> dict[str, Any]:
    data = _load_json(analysis_path, "bottleneck_analysis.json")
    if not isinstance(data, dict):
        raise CheckError("bottleneck_analysis.json must be a JSON object")

    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise CheckError(f"bottleneck_analysis.json has unknown top-level keys "
                         f"{sorted(unknown)} (allowed: {sorted(_TOP_KEYS)})")
    for key in ("base_report", "summary", "top_bottlenecks"):
        if key not in data:
            raise CheckError(f"bottleneck_analysis.json missing '{key}'")
    if not isinstance(data["summary"], str) or not data["summary"].strip():
        raise CheckError("'summary' must be a non-empty string")
    if "schema_version" in data and data["schema_version"] != 1:
        raise CheckError(f"unsupported schema_version: {data['schema_version']!r}")

    # the mechanical report it derives from must exist and parse
    base_report_path = Path(data["base_report"])
    if not base_report_path.is_absolute():
        base_report_path = artifacts / base_report_path
    report = _load_json(base_report_path, "base_report")
    hot = report.get("hot_patterns") if isinstance(report, dict) else None
    if not isinstance(hot, list) or not hot:
        raise CheckError(f"base report has no hot_patterns list: {base_report_path}")

    entries = data["top_bottlenecks"]
    if not isinstance(entries, list) or not entries:
        raise CheckError("'top_bottlenecks' must be a non-empty list")

    by_pattern = {p.get("pattern_id"): (i, p) for i, p in enumerate(hot)}
    prev_idx: int | None = None
    prev_cycles: int | None = None
    seen: set[str] = set()
    for pos, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise CheckError(f"top_bottlenecks[{pos}] must be an object")
        unknown = set(entry) - _ENTRY_KEYS
        if unknown:
            raise CheckError(f"top_bottlenecks[{pos}] has unknown keys "
                             f"{sorted(unknown)} (allowed: {sorted(_ENTRY_KEYS)})")
        for key in _ENTRY_KEYS:
            if key not in entry:
                raise CheckError(f"top_bottlenecks[{pos}] missing '{key}'")
        name = entry["name"]
        if name in seen:
            raise CheckError(f"top_bottlenecks[{pos}] repeats pattern_id {name!r}")
        seen.add(name)
        if name not in by_pattern:
            raise CheckError(
                f"top_bottlenecks[{pos}].name {name!r} is not a pattern_id of "
                f"the base report (has {sorted(by_pattern)}) — fabricated "
                f"reference")
        idx, pattern = by_pattern[name]
        if entry["op_type"] != pattern.get("op_type"):
            raise CheckError(
                f"top_bottlenecks[{pos}].op_type {entry['op_type']!r} != base "
                f"report {pattern.get('op_type')!r} for {name}")
        if entry["cycles"] != pattern.get("total_cycles"):
            raise CheckError(
                f"top_bottlenecks[{pos}].cycles {entry['cycles']!r} != base "
                f"report total_cycles {pattern.get('total_cycles')!r} for {name}")
        if not isinstance(entry["analysis"], str) or not entry["analysis"].strip():
            raise CheckError(f"top_bottlenecks[{pos}].analysis must be a "
                             f"non-empty string")
        # order-preserving subset: base-report rank indices strictly increase
        if prev_idx is not None and idx <= prev_idx:
            raise CheckError(
                f"top_bottlenecks[{pos}] ({name}, base rank {idx}) breaks the "
                f"base report's rank order (previous base rank {prev_idx}) — "
                f"entries must follow the base report's ordering")
        # rank consistency: the base report sorts by total_cycles desc, so the
        # referenced cycles must be non-increasing along the analysis list
        if prev_cycles is not None and entry["cycles"] > prev_cycles:
            raise CheckError(
                f"top_bottlenecks[{pos}].cycles {entry['cycles']} > previous "
                f"{prev_cycles} — must be non-increasing (base-report rank "
                f"order)")
        prev_idx, prev_cycles = idx, entry["cycles"]

    return {"ok": True, "entries": len(entries), "base_report": data["base_report"],
            "patterns": sorted(seen)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--analysis", required=True,
                    help="bottleneck_analysis.json path (absolute, or relative "
                         "to --artifacts)")
    ap.add_argument("--artifacts", default=os.environ.get("ORCA_ARTIFACTS_DIR", "."),
                    help="workspace root the base_report path resolves against "
                         "(default: $ORCA_ARTIFACTS_DIR)")
    ns = ap.parse_args()
    art = Path(ns.artifacts)
    analysis = Path(ns.analysis)
    if not analysis.is_absolute():
        analysis = art / analysis
    try:
        result = check(analysis, art)
    except CheckError as exc:
        print(f"check_bottleneck: FAIL {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Create a portable Web dashboard snapshot for a prof-opt workspace (v6 §4.2).

Read path with a side effect (§7.5 trigger ②): every collect FIRST re-runs the
deterministic ledger aggregator, so the shared experiment_ledger.json the
snapshot renders always reflects the current per-variant shard set — the
single entry point keeps the derived ledger convergent without the watchdogs
coordinating.

Variant rows expose the v6 §4.2 fields — status / latest_epoch /
latest_metric / gap / device / change_summary — derived from the aggregated
shard rows (vid/epoch/metric/gap/device/change_summary verbatim, epoch/metric
surfaced under their dashboard names).
"""
from __future__ import annotations

import argparse
import json
import sys
from html import escape
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger_aggregate  # noqa: E402

SCHEMA_VERSION = 3  # v3: the retired best.json read/field is gone (§12)


def _json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _variant_view(row: dict[str, Any]) -> dict[str, Any]:
    """A shard row -> the dashboard row (§4.2 field names)."""
    return {
        "vid": row.get("vid"),
        "status": row.get("status"),
        "latest_epoch": row.get("epoch"),
        "latest_metric": row.get("metric"),
        "gap": row.get("gap"),
        "device": row.get("device"),
        "change_summary": row.get("change_summary"),
    }


def snapshot(artifacts: Path) -> dict[str, Any]:
    ledger = ledger_aggregate.aggregate(artifacts)  # §7.5 trigger ②: refresh first
    baseline = _json(artifacts / "base" / "origin_anchor.json", {})
    curves: dict[str, list[dict[str, Any]]] = {}
    curve_files = [(artifacts / "baseline" / "baseline_metrics.jsonl", "baseline")]
    curve_files += [(p, p.parent.parent.name) for p in (artifacts / "variants").glob("*/metrics/metrics.jsonl")]
    for path, label in curve_files:
        if path.is_file():
            try:
                curves[label] = [json.loads(line) for line in path.read_text(
                    encoding="utf-8").splitlines() if line.strip()]
            except json.JSONDecodeError:
                continue
    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_makespan_cycles": baseline.get("baseline_makespan_cycles"),
        "variants": [_variant_view(r) for r in ledger.get("rows", [])],
        "curves": curves,
    }


def _html(data: dict[str, Any]) -> str:
    rows = data.get("variants", [])
    def _cell(row: dict[str, Any], key: str) -> str:
        value = row.get(key, "")
        return escape("" if value is None else str(value))
    body = "".join(
        f"<tr><td>{_cell(r, 'vid')}</td><td>{_cell(r, 'status')}"
        f"</td><td>{_cell(r, 'latest_epoch')}</td><td>{_cell(r, 'latest_metric')}"
        f"</td><td>{_cell(r, 'gap')}</td><td>{_cell(r, 'device')}"
        f"</td><td>{_cell(r, 'change_summary')}</td></tr>"
        for r in rows)
    payload = escape(json.dumps(data, ensure_ascii=False), quote=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>prof-opt dashboard</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#17202a}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd1d1;padding:8px;text-align:left}}th{{background:#f2f4f4}}</style></head>
<body><h1>Prof-Opt Dashboard</h1><p>Baseline makespan: <b>{data.get('baseline_makespan_cycles')}</b> cycles</p>
<table><thead><tr><th>VID</th><th>Status</th><th>Latest epoch</th><th>Latest metric</th><th>Gap</th><th>Device</th><th>Change summary</th></tr></thead><tbody>{body}</tbody></table>
<script type="application/json" id="prof-opt-data">{payload}</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ns = ap.parse_args()
    root = Path(ns.artifacts)
    root.mkdir(parents=True, exist_ok=True)
    try:
        data = snapshot(root)
    except (OSError, ValueError) as exc:
        # a torn shard is a real anomaly (single-writer files) — fail loud
        print(f"dashboard_snapshot: FAIL {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    (root / "dashboard.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "dashboard.html").write_text(_html(data), encoding="utf-8")
    print(json.dumps({
        "dashboard": str(root / "dashboard.html"),
        "data": str(root / "dashboard.json"),
        "variant_count": len(data["variants"]),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())

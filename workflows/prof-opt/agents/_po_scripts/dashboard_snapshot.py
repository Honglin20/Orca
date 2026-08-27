#!/usr/bin/env python3
"""Create a portable Web dashboard snapshot for a prof-opt workspace."""
from __future__ import annotations

import argparse
import json
import sys
from html import escape
from pathlib import Path
from typing import Any


def _json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def snapshot(artifacts: Path) -> dict[str, Any]:
    ledger = _json(artifacts / "experiment_ledger.json", {"rows": []})
    best = _json(artifacts / "best.json", None)
    baseline = _json(artifacts / "base" / "bottleneck_report.json", {})
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
        "schema_version": 1,
        "baseline_makespan_cycles": baseline.get("makespan_cycles"),
        "best": best,
        "variants": ledger.get("rows", []),
        "curves": curves,
    }


def _html(data: dict[str, Any]) -> str:
    rows = data.get("variants", [])
    body = "".join(
        f"<tr><td>{escape(str(r.get('vid', '')))}</td><td>{escape(str(r.get('lever', '')))}"
        f"</td><td>{escape(str(r.get('outcome', '')))}</td><td>{r.get('measured_makespan_cycles', '')}"
        f"</td><td>{r.get('proxy_acc', '')}</td></tr>"
        for r in rows)
    payload = escape(json.dumps(data, ensure_ascii=False), quote=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>prof-opt dashboard</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#17202a}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd1d1;padding:8px;text-align:left}}th{{background:#f2f4f4}}</style></head>
<body><h1>Prof-Opt Dashboard</h1><p>Baseline makespan: <b>{data.get('baseline_makespan_cycles')}</b> cycles</p>
<table><thead><tr><th>VID</th><th>Lever</th><th>Outcome</th><th>Makespan</th><th>Proxy metric</th></tr></thead><tbody>{body}</tbody></table>
<script type="application/json" id="prof-opt-data">{payload}</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ns = ap.parse_args()
    root = Path(ns.artifacts)
    root.mkdir(parents=True, exist_ok=True)
    data = snapshot(root)
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

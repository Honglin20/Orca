#!/usr/bin/env python3
"""report.py -- Final summary JSON for ns_visualize.

Reads the chart-result marker file (``.ns_visualize_charts.jsonl``), discovers the
project metric metadata, and emits the agent's final single-line JSON.

This is the ONLY stdout the agent should echo. The marker file is populated by
the individual chart scripts (pareto.py / search_table.py / etc.).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import CHART_MARKER, discover_metric_info, read_jsonl  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Emit ns_visualize summary JSON.")
    ap.add_argument("--artifacts-dir", required=True)
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)

    chart_records = read_jsonl(ad / CHART_MARKER)
    pushed = sum(1 for r in chart_records if r.get("status") == "pushed")
    skipped = sum(1 for r in chart_records if r.get("status") == "skipped")

    info = discover_metric_info(ad)
    metric_name = info.name if info else "metric"
    metric_direction = info.display_direction if info else "higher"

    status = "executed" if pushed > 0 else "skipped"
    total = pushed + skipped
    summary = (
        f"Visualized {pushed}/{total} charts "
        f"(metric={metric_name}, {metric_direction}-is-better)."
        + ("" if skipped == 0 else f" {skipped} chart(s) skipped due to missing artifacts.")
    )

    payload: dict[str, Any] = {
        "status": status,
        "charts_pushed": pushed,
        "charts_skipped": skipped,
        "charts": chart_records,
        "metric_name": metric_name,
        "metric_direction": metric_direction,
        "summary": summary,
    }

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

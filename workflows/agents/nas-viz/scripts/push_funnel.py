#!/usr/bin/env python3
"""push_funnel.py —— C6 选择漏斗图（viz_finalize 调用）。

读 selection_summary.json 的 6 级计数，推一张 bar：input → pareto → unique → feasible →
feasible_pareto → selected。讲清「百万评估 → 最终 N 架构」的收敛证据。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from orca.chart import render_chart  # type: ignore
except Exception as e:  # pragma: no cover
    sys.stderr.write(f"[push_funnel] 无法 import orca.chart：{e}\n")
    sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--selection_summary", default="")
    args = ap.parse_args()
    output_dir = Path(args.output_dir)
    sel_path = (
        Path(args.selection_summary)
        if args.selection_summary
        else output_dir / "runs" / "retrain" / "selected" / "selection_summary.json"
    )
    if not sel_path.is_file():
        sys.stderr.write(f"[push_funnel] selection_summary.json 不存在：{sel_path}，跳过 C6\n")
        return 0
    try:
        s = json.loads(sel_path.read_text(encoding="utf-8"))
    except Exception as e:
        sys.stderr.write(f"[push_funnel] 解析失败：{e}\n")
        return 0

    stages = [
        ("input", s.get("num_input_records", 0)),
        ("pareto", s.get("num_input_pareto_records", 0)),
        ("unique", s.get("num_unique_pareto_architectures", 0)),
        ("feasible", s.get("num_feasible_architectures", 0)),
        ("feasible_pareto", s.get("num_feasible_pareto_architectures", 0)),
        ("selected", len(s.get("selected", []))),
    ]
    data = [{"stage": name, "count": cnt} for name, cnt in stages]
    render_chart(
        chart_type="bar",
        data=data,
        label="nas/selection",
        title="Selection Funnel",
        x="stage",
        y="count",
    )
    print(f"[push_funnel] C6 pushed: {' → '.join(f'{n}={c}' for n, c in stages)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

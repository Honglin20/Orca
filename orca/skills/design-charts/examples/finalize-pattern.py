#!/usr/bin/env python3
"""finalize-pattern.py —— 模式 C 样板：终态一次性推图。

在 workflow 终态节点（如 select/finalize）调用，读全部历史数据，
做离线聚合计算，推终态报告图。同 label 但 title 与 live 图不同。
真实用法见 workflows/agents/nas-select/scripts/push_funnel.py。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from orca.chart import render_chart  # type: ignore
except Exception:
    sys.stderr.write("[finalize_pattern] 不在 Orca run 上下文中（必须由 Orca agent spawn）\n")
    sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser(description="finalize chart pattern demo")
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--summary", default="",
                    help="summary JSON 路径（缺省推断 <output_dir>/summary.json）")
    args = ap.parse_args()
    output_dir = Path(args.output_dir)

    summary_path = Path(args.summary) if args.summary else output_dir / "summary.json"
    if not summary_path.is_file():
        sys.stderr.write(f"[finalize_pattern] {summary_path} 不存在，跳过终态图表\n")
        return 0

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception as e:
        sys.stderr.write(f"[finalize_pattern] {summary_path} 解析失败：{e}\n")
        return 0

    def _try(label: str, title: str, **kwargs: object) -> None:
        try:
            render_chart(  # type: ignore[call-arg]
                label=label,
                title=title,
                **kwargs,  # type: ignore[arg-type]
            )
        except Exception as e:
            sys.stderr.write(f"[finalize_pattern] chart 推送失败 {title!r}（不阻断）：{e}\n")

    # ── 终态图 1：汇总漏斗 ──
    stages = [
        ("input",   summary.get("num_input", 0)),
        ("pareto",  summary.get("num_pareto", 0)),
        ("unique",  summary.get("num_unique", 0)),
        ("selected",summary.get("num_selected", 0)),
    ]
    funnel_data = [{"stage": name, "count": cnt} for name, cnt in stages]
    if any(cnt > 0 for _, cnt in stages):
        _try(
            "demo/selection", "Selection Funnel (final)",
            chart_type="bar",
            data=funnel_data,
            x="stage",
            y="count",
            x_label="筛选阶段",
            y_label="数量",
            caption="终态聚合漏斗（finalize 模式样板）。",
        )

    # ── 终态图 2：单指标终态线 ──
    metrics = summary.get("metrics", [])
    if metrics:
        _try(
            "demo/metrics", "Final Metrics Summary",
            chart_type="line",
            data=metrics,
            x="step",
            y="value",
            x_label="步数",
            y_label="终态指标",
            caption="终态指标汇总（finalize 模式样板）。",
        )

    print(f"[finalize_pattern] 已推送终态图表", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

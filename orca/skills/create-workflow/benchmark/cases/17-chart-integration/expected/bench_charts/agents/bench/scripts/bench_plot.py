#!/usr/bin/env python3
"""bench_plot.py —— 跑 N 次基准评测（内联 mock 计算，零外部依赖），逐次指标写 jsonl 并推 web 图表。"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path


def run_bench(runs: int, seed: int) -> list[dict]:
    """Mock 评测：确定性伪随机模拟每次评测的 P95 latency 与 accuracy。"""
    rng = random.Random(seed)
    records = []
    for run in range(1, runs + 1):
        records.append({
            "run": run,
            "p95_latency_ms": round(60.0 - 0.8 * run + rng.uniform(-2.0, 2.0), 2),
            "accuracy": round(0.70 + 0.008 * run + rng.uniform(-0.003, 0.003), 4),
        })
    return records


def write_jsonl(records: list[dict], out_path: Path) -> None:
    with out_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def push_charts(records: list[dict]) -> None:
    """把逐次指标推到 web 图表；推送失败只告警，不阻断主流程。"""
    try:
        from orca.chart import render_chart
    except ImportError:
        sys.stderr.write("[bench_plot] 不在 Orca run 上下文，跳过图表推送\n")
        return
    try:
        render_chart(
            chart_type="line",
            data=records,
            label="bench/metrics",
            title="P95 Latency per run",
            x="run",
            y="p95_latency_ms",
            x_label="评测轮次",
            y_label="P95 latency (ms，越低越好)",
        )
    except Exception as e:
        sys.stderr.write(f"[bench_plot] latency 图推送失败（不阻断）：{e}\n")
    try:
        render_chart(
            chart_type="bar",
            data=records,
            label="bench/metrics",
            title="Accuracy per run",
            x="run",
            y="accuracy",
            x_label="评测轮次",
            y_label="accuracy（越高越好）",
        )
    except Exception as e:
        sys.stderr.write(f"[bench_plot] accuracy 图推送失败（不阻断）：{e}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="run N mock benchmark evals and push metric charts")
    ap.add_argument("--runs", type=int, required=True, help="评测次数")
    ap.add_argument("--seed", type=int, default=0, help="复现种子")
    ap.add_argument("--output_dir", default=".", help="jsonl 输出目录")
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    records = run_bench(args.runs, args.seed)
    jsonl_path = out_dir / "bench_metrics.jsonl"
    write_jsonl(records, jsonl_path)
    print(f"[bench_plot] {args.runs} 次评测完成 → {jsonl_path}")
    for rec in records:
        print(
            f"[bench_plot] run={rec['run']} p95_latency_ms={rec['p95_latency_ms']} "
            f"accuracy={rec['accuracy']}"
        )

    push_charts(records)
    return 0


if __name__ == "__main__":
    sys.exit(main())

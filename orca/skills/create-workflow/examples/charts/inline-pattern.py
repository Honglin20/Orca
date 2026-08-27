#!/usr/bin/env python3
"""inline-pattern.py —— 模式 A 样板：脚本内直接推图。

这是一个精简的教学样板，展示如何在 Python 脚本末尾添加 render_chart 调用。
真实用法见 workflows/agents/quant-qat/scripts/run_qat.py（QAT 训练 inline 推图）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from orca.chart import render_chart  # type: ignore
except Exception:
    sys.stderr.write("[inline_pattern] 不在 Orca run 上下文中，跳过 chart 推送\n")
    render_chart = None  # type: ignore


def do_work(output_dir: Path) -> list[dict]:
    """模拟核心计算——你的真实脚本里这是主逻辑。"""
    results = []
    for step in range(0, 101, 10):
        results.append({"step": step, "loss": 2.0 * (0.95 ** (step / 10)), "metric": 0.5 + 0.005 * step})
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"[inline_pattern] 计算完成，产出 {len(results)} 行 → {report_path}", flush=True)
    return results


def push_charts(results: list[dict]) -> None:
    """在脚本末尾调用：推图不阻断主流程。"""
    if render_chart is None:
        return

    def _try(label: str, title: str, **kwargs: object) -> None:
        try:
            render_chart(  # type: ignore[call-arg]
                data=results,
                label=label,
                title=title,
                **kwargs,  # type: ignore[arg-type]
            )
        except Exception as e:
            sys.stderr.write(f"[inline_pattern] chart 推送失败 {title!r}（不阻断）：{e}\n")

    _try("demo/inline", "Training Loss", chart_type="line", x="step", y="loss",
         x_label="步数", y_label="loss（越低越好）", caption="模拟训练 loss 曲线（inline 模式样板）。")
    _try("demo/inline", "Validation Metric", chart_type="line", x="step", y="metric",
         x_label="步数", y_label="metric", caption="模拟验证指标（inline 模式样板）。")


def main() -> int:
    ap = argparse.ArgumentParser(description="inline chart pattern demo")
    ap.add_argument("--output_dir", required=True)
    args = ap.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = do_work(output_dir)

    # chart 推送放在最后
    push_charts(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())

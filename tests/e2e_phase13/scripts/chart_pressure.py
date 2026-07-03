"""chart_pressure.py —— E2E-5 压测：1 个 script 推 10 张图，label/title 组合各不同。

3 个 run 并行 × 每 run 10 chart = 30 张图，验证 tape 隔离 + 无丢失 / 错位 / 串扰。

label 嵌 ORCA_RUN_ID 标识，title 取 0-9 序号。chart_type 5 种轮转（line/bar/area/scatter/table）。
"""

from __future__ import annotations

import os
import sys

from orca.chart import render_chart


def main() -> int:
    run_id = os.environ.get("ORCA_RUN_ID", "unknown")
    tag = run_id.split("-")[-1][:8] if "-" in run_id else run_id[:8]

    chart_types = ["line", "bar", "area", "scatter", "table"]
    seqs: list[int] = []
    for i in range(10):
        ct = chart_types[i % len(chart_types)]
        # 不同 chart_type 不同 data schema（小数据，重点是 10 张独立图）。
        if ct == "table":
            data = [{"col_a": i, "col_b": i * 2} for i in range(5)]
            kwargs = {"columns": ["col_a", "col_b"]}
        elif ct in ("line", "area"):
            data = [{"x": j, "y": j * i} for j in range(20)]
            kwargs = {"x": "x", "y": "y"}
        elif ct == "bar":
            data = [{"x": chr(ord("a") + j), "y": j * i} for j in range(5)]
            kwargs = {"x": "x", "y": "y"}
        else:  # scatter
            data = [{"x": j, "y": (j + i) % 7} for j in range(20)]
            kwargs = {"x": "x", "y": "y"}
        seq = render_chart(
            chart_type=ct,
            data=data,
            label=f"pressure-{tag}",
            title=f"chart-{i}",
            **kwargs,
        )
        seqs.append(seq)

    print(
        f"[chart_pressure] tag={tag} pushed={len(seqs)} seqs={seqs}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

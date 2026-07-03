"""chart_parallel.py —— E2E-2 multi-run parallel 用，每 run 一张 bar chart。

label/title 含 ORCA_RUN_ID（从 env 读），证明 env 不串：A run 的 tape 只含 A 标识。
"""

from __future__ import annotations

import os
import sys

from orca.chart import render_chart


def main() -> int:
    run_id = os.environ.get("ORCA_RUN_ID", "unknown")
    # label 嵌 run_id 前缀（gen_run_id 形如 <wf>-<8hex>，前 8 字符唯一）。
    tag = run_id.split("-")[-1][:8] if "-" in run_id else run_id[:8]
    data = [
        {"x": "a", "y": 10},
        {"x": "b", "y": 30},
        {"x": "c", "y": 20},
    ]
    seq = render_chart(
        chart_type="bar",
        data=data,
        label=f"parallel-{tag}",
        title=f"bar-{tag}",
        x="x",
        y="y",
    )
    print(f"[chart_parallel] run_id={run_id} tag={tag} seq={seq}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

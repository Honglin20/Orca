"""chart_demo.py —— phase-13 E2E 的 demo script（被 script node spawn）。

证明「script 子进程真调 orca.chart.render_chart 走完整 socket 链路」。

**不接收任何参数**：identity（run_id / node / session_id / sock_path）全部从 env 读
（phase-13 SPEC 铁律 #2）。env 由 ClaudeExecutor / orchestrator spawn 时注入。

退出码：
  0 = 成功推图（seq 已拿到）
  非 0 = render_chart raise（含 fail loud 信息，stderr 可见）
"""

from __future__ import annotations

import sys

from orca.chart import render_chart


def main() -> int:
    """推一张 line chart，5 个数据点（最小可见集合）。"""
    data = [
        {"step": 1, "loss": 0.95},
        {"step": 2, "loss": 0.82},
        {"step": 3, "loss": 0.71},
        {"step": 4, "loss": 0.63},
        {"step": 5, "loss": 0.55},
    ]
    seq = render_chart(
        chart_type="line",
        data=data,
        label="training",
        title="loss",
        x="step",
        y="loss",
    )
    # stdout 给 agent 看；orchestrator 不解析（script kind 的 output 是 stdout 全量）。
    print(f"[chart_demo] pushed chart, seq={seq}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

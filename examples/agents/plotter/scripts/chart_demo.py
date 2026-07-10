"""chart_demo.py —— render_chart example 的资源脚本（被 plotter agent 经 Bash spawn）。

演示 phase-13 + phase-14 链路：
  - phase-14：plotter 是文件夹化 agent，本脚本是其资源（$ORCA_AGENT_RESOURCES/scripts/）。
  - phase-13：identity（run_id/node/session_id/sock_path）从 env 读（铁律 #2），由
    ClaudeExecutor spawn 时注入，沿 subprocess 链继承到本 script。
  - 本 script 调 orca.chart.render_chart → 经 per-run Unix socket → tape 落 custom(chart)
    → 三壳（TUI/Web/MCP）渲染。

退出码：0 = 推图成功（seq 已拿到）；非 0 = render_chart raise（fail loud）。

不接收参数：identity 全部从 env 读。
"""

from __future__ import annotations

import sys

from orca.chart import render_chart


def main() -> int:
    """推一张 line chart，5 个数据点（训练 loss 下降）。"""
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
    print(f"[chart_demo] pushed chart, seq={seq}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

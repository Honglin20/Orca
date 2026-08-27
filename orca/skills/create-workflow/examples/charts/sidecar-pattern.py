#!/usr/bin/env python3
"""sidecar-pattern.py —— 模式 B 样板：独立轮询脚本。

被 agent 周期调用，读外部进程产的 jsonl 文件，推图（同 label+title → 替换=实时更新）。
真实用法见 workflows/nas-agent-pipeline/agents/nas-train-runner/scripts/tail_metrics.py。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from orca.chart import render_chart  # type: ignore
except Exception:
    sys.stderr.write("[sidecar_pattern] 不在 Orca run 上下文中（必须由 Orca agent spawn）\n")
    sys.exit(2)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """幂等读取 jsonl。空文件/不存在 → []。"""
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue  # 跳过半写尾巴行
    return rows


def push_everything(records: list[dict[str, Any]], label_prefix: str) -> None:
    """从 records 推全部 chart。同 label+title 幂等覆盖。"""
    if not records:
        return

    def _try(**kwargs: object) -> None:
        try:
            render_chart(**kwargs)  # type: ignore[call-arg]
        except Exception as e:
            sys.stderr.write(f"[sidecar_pattern] chart 推送失败 {kwargs.get('title')!r}（不阻断）：{e}\n")

    # 1. 时序线图
    _try(
        chart_type="line", data=records, label=label_prefix,
        title="Metric Over Time", x="step", y="value",
        x_label="步数", y_label="指标",
        caption="sidecar 轮询推送（实时覆盖）。",
    )

    # 2. 散点分布（仅当数据含 x_pos/y_pos 字段时触发；demo 数据不含此字段，因此不会推送）
    if all("x_pos" in r and "y_pos" in r for r in records):
        _try(
            chart_type="scatter", data=records, label=label_prefix,
            title="Scatter Distribution", x="x_pos", y="y_pos",
            x_label="X", y_label="Y",
        )

    # 3. 汇总表
    if len(records) <= 100:
        cols = list(records[0].keys())
        _try(
            chart_type="table", data=records, label=label_prefix,
            title="Raw Records", columns=cols,
            caption="全量记录（≤100 行，超过降采样）。",
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="sidecar chart polling pattern demo")
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--label_prefix", default="demo/sidecar",
                    help="chart label prefix（如 nas/training）")
    args = ap.parse_args()
    output_dir = Path(args.output_dir)

    records = _read_jsonl(output_dir / "metrics.jsonl")
    if not records:
        return 0  # 文件还没出现，静默跳过，下次轮询再读

    push_everything(records, args.label_prefix)
    print(f"[sidecar_pattern] 已推送 {len(records)} 行记录", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

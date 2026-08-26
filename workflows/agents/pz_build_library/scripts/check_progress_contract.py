#!/usr/bin/env python3
"""check_progress_contract.py —— 校验 progress.jsonl 是否符合 chart-feed 契约。

契约(与 train_supernet_script_generation.md §3(b) / retrain agent.md Step 3a 同源):

    每行 = {"step": <number>, "metrics": {"<name>": <number>, ...}}

warmup_poll.sh 在发散检查后、WARMUP_OK 判定前调用本脚本。文件缺/空/某行格式错
→ exit 1 + stderr 打印具体原因(行号 + 缺哪个键/值类型错)→ warmup 判
WARMUP_FAIL reason=progress-contract → HEAL-LOOP 自愈修生成脚本(train_supernet.py /
retrain.py 的 progress.jsonl 写入循环)。

与 progress_watcher.py 的根本区别:watcher 是 **fail-soft**(缺文件/格式错静默 exit 0,
绝不影响训练 rc);本脚本是 **fail-loud 契约闸门**——漏写 progress.jsonl 是生成代码 bug,
必须 fail loud 触发自愈,不许静默放过(否则训练 executed 但无实时图,见 plan Context)。

数值语义与 progress_watcher._is_number 同源:真实数值且非 bool。NaN/inf 也算 number
(契约只要求 number);NaN/inf 的发散检测由 warmup_poll.sh 发散段单独管,不在此重复。

退出码:0 = 契约过(每非空行合法);1 = 不过(stderr 说明)。

用法:
    python3 check_progress_contract.py --progress runs/train/progress.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _is_number(v: Any) -> bool:
    """True 仅对真实数值(排除 bool——isinstance(True, int) 为真但 bool 非 metric)。
    与 progress_watcher._is_number 同源语义。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate progress.jsonl against the chart-feed contract.")
    ap.add_argument("--progress", required=True, help="progress.jsonl 路径(相对 cwd 或绝对)")
    args = ap.parse_args()

    path = Path(args.progress)
    if not path.is_file():
        sys.stderr.write(f"[progress-contract] FAIL: progress.jsonl 不存在: {path}\n")
        return 1

    # splitlines 丢尾部换行产生的空串;非空行才计契约行。
    non_empty = [
        (i + 1, ln)
        for i, ln in enumerate(path.read_text(encoding="utf-8").splitlines())
        if ln.strip()
    ]
    if not non_empty:
        sys.stderr.write(f"[progress-contract] FAIL: progress.jsonl 无任何非空行: {path}\n")
        return 1

    for lineno, line in non_empty:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            sys.stderr.write(f"[progress-contract] FAIL: 第 {lineno} 行非合法 JSON: {exc}\n")
            return 1
        if not isinstance(row, dict):
            sys.stderr.write(f"[progress-contract] FAIL: 第 {lineno} 行非 JSON object: {path}\n")
            return 1
        step = row.get("step")
        if not _is_number(step):
            sys.stderr.write(
                f"[progress-contract] FAIL: 第 {lineno} 行缺 'step' 或非数值 (step={step!r})\n"
            )
            return 1
        metrics = row.get("metrics")
        if not isinstance(metrics, dict):
            sys.stderr.write(
                f"[progress-contract] FAIL: 第 {lineno} 行缺 'metrics' 或非 object (metrics={metrics!r})\n"
            )
            return 1
        if not metrics:
            sys.stderr.write(
                f"[progress-contract] FAIL: 第 {lineno} 行 'metrics' 为空 (无指标可推)\n"
            )
            return 1
        for name, val in metrics.items():
            if not _is_number(val):
                sys.stderr.write(
                    f"[progress-contract] FAIL: 第 {lineno} 行 metric '{name}' 非数值 (val={val!r})\n"
                )
                return 1

    sys.stdout.write(f"[progress-contract] OK: {len(non_empty)} 行契约合法 ({path})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""__main__.py —— python -m orca.run <yaml> [task] [-i k=v]... [--max-iter N] 最小入口。

phase 5 的最小 CLI（SPEC §5）：解析 yaml + task + 简单 -i/--max-iter，调 run_workflow。
完整 typer/click 命令绑定（``orca run`` 子命令）放 phase 6，本阶段只做能跑 demo 的最小入口。

用法示例：
    python -m orca.run examples/demo_linear.yaml
    python -m orca.run examples/demo_task.yaml "测试任务"
    python -m orca.run examples/demo_loop.yaml -i start=0 --max-iter 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _parse_input_value(raw: str):
    """``-i key=value`` 类型推断：true/false→bool，数字→int/float，JSON，str。"""
    # bool
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    # int
    try:
        return int(raw)
    except ValueError:
        pass
    # float
    try:
        return float(raw)
    except ValueError:
        pass
    # JSON（[...] / {...}）
    if raw.startswith(("[", "{")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return raw  # str 兜底


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m orca.run",
        description="最小 workflow 运行入口（phase 5；完整 CLI 在 phase 6）",
    )
    p.add_argument("yaml", help="workflow YAML 文件路径")
    p.add_argument("task", nargs="?", default=None, help="位置参数 task（注入 inputs.task）")
    p.add_argument(
        "-i", "--input", action="append", default=[], metavar="key=value",
        help="覆盖 inputs（可多次；类型推断：bool/int/float/JSON/str）",
    )
    p.add_argument("--max-iter", type=int, default=None, help="覆盖 max_iterations")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    # 惰性导入：CLI 启动时不拉起整个 orca 链（仅真正运行时才加载）
    from orca.compile.parser import load_workflow
    from orca.run import run_workflow

    wf = load_workflow(Path(args.yaml))

    inputs: dict = {}
    for item in args.input:
        if "=" not in item:
            print(f"错误：-i 参数需为 key=value 形式（得到 {item!r}）", file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        inputs[k] = _parse_input_value(v)

    if args.task is not None:
        inputs.setdefault("task", args.task)

    state = asyncio.run(run_workflow(wf, inputs, task=args.task, max_iter=args.max_iter))

    # 最小输出：run_id + status + outputs（完整渲染放 phase 6/7）
    print(f"run_id: {state.run_id}")
    print(f"status: {state.status}")
    if state.status == "completed":
        # outputs 在 workflow_completed 事件 data 里（重读 tape 最后一条）
        from orca.events.tape import Tape

        tape = Tape(Path("runs") / f"{state.run_id}.jsonl", run_id=state.run_id)
        last_completed = None
        for ev in tape.replay():
            if ev.type == "workflow_completed":
                last_completed = ev
        if last_completed is not None:
            print(f"outputs: {last_completed.data.get('outputs', {})}")
    return 0 if state.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())

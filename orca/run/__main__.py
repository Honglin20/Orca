"""__main__.py —— python -m orca.run <yaml> [task] [-i k=v]... [--max-iter N] 最小入口。

phase 5 的最小 CLI（SPEC §5）：解析 yaml + task + 简单 -i/--max-iter，调 run_workflow。
完整 typer/click 命令绑定（``orca run`` 子命令）放 phase 6，本阶段只做能跑 demo 的最小入口。

phase-11-process §3 / §1.3：
  - 退出码经 ``exit_for_terminal_status(state.status)`` 派生（5 档 0/1/2/3/130，SPEC §3.1）。
  - SIGINT / SIGTERM handler 只设 ``threading.Event``（async-signal-safe），由专门清理
    线程看到 Event 后调 ``registry.shutdown()``——**禁止**在 signal handler 里直接调
    （``threading.Lock`` 非 async-signal-safe，SPEC §1.3）。

用法示例：
    python -m orca.run examples/demo_linear.yaml
    python -m orca.run examples/demo_task.yaml "测试任务"
    python -m orca.run examples/demo_loop.yaml -i start=0 --max-iter 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import threading
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
    from orca.compile import ConfigurationError
    from orca.exec.registry import get_default_registry
    from orca.iface.cli.config import apply_kb_requirement
    from orca.iface.exit_codes import ExitCode, exit_for_terminal_status
    from orca.run import run_workflow

    try:
        wf = load_workflow(Path(args.yaml))
        apply_kb_requirement(wf)  # plan §1.4：requires knowledge_base 时预检 KB（fail loud）
    except ConfigurationError as e:
        print(str(e), file=sys.stderr)
        return int(ExitCode.CONFIG_ERROR)

    inputs: dict = {}
    for item in args.input:
        if "=" not in item:
            print(f"错误：-i 参数需为 key=value 形式（得到 {item!r}）", file=sys.stderr)
            return int(ExitCode.CONFIG_ERROR)
        k, v = item.split("=", 1)
        inputs[k] = _parse_input_value(v)

    if args.task is not None:
        inputs.setdefault("task", args.task)

    # phase-11-process §1.3：signal-safe shutdown。
    # SIGTERM handler 只设 ``threading.Event``（async-signal-safe subset），由 daemon
    # 清理线程看到 Event 后调 ``registry.shutdown()``——``registry`` 内含
    # ``threading.Lock``，在 signal handler 里直接调会死锁（SPEC §1.3 明示）。
    # daemon=True 让进程退出时不阻塞。
    #
    # **SIGINT 不在此时覆盖**：``asyncio.run`` 自带 SIGINT handler（cancel main task +
    # raise KeyboardInterrupt），覆盖它会让 Ctrl+C 无法中断 asyncio。SIGINT 走默认路径
    # → 下面 ``except KeyboardInterrupt`` 兜底返回 ``ExitCode.SIGINT`` (130)。SIGINT
    # 触发的 task cancel 会让 runner.py / script.py 的 finally 块调 registry.kill_one，
    # 兜底清子进程；本 handler 不掺和。
    registry = get_default_registry()
    shutdown_event = threading.Event()

    def _on_sigterm(signum, frame):  # noqa: ANN001 -- signal handler 签名固定
        # signal handler 里只做 async-signal-safe 操作：设 Event。
        # 不调 registry.shutdown / 不打 log / 不 allocate——清理线程负责后续。
        shutdown_event.set()

    cleanup_thread = threading.Thread(
        target=lambda: (shutdown_event.wait() and registry.shutdown()),
        name="orca-process-shutdown", daemon=True,
    )
    cleanup_thread.start()
    # 只覆盖 SIGTERM；SIGINT 留给 asyncio.run 默认 handler（cancel task + KBInterrupt）。
    prev_term = signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        state = asyncio.run(
            run_workflow(wf, inputs, task=args.task, max_iter=args.max_iter)
        )
    except KeyboardInterrupt:
        # SIGINT → asyncio 转 task cancel + KeyboardInterrupt。registry.shutdown 兜底
        # 在 finally 调（幂等）；清理线程因 SIGTERM 不会触发，但 finally 显式调能覆盖。
        return int(ExitCode.SIGINT)
    finally:
        # 复位 SIGTERM handler（避免后续 sys.exit 触发 atexit 时还在用 _on_sigterm）。
        signal.signal(signal.SIGTERM, prev_term)
        # 触发清理线程退出（若 SIGTERM 未触发）+ 显式调 shutdown 兜底（幂等）。
        # 覆盖 SIGINT 路径（asyncio CancelledError → runner.py finally kill_one 已清子进程，
        # 但 registry.shutdown 兜底任何遗漏 entry）。
        shutdown_event.set()
        registry.shutdown()

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
    # phase-11-process §3：退出码经权威派生函数（5 档契约，ADR §4.6）。
    return exit_for_terminal_status(state.status)


if __name__ == "__main__":
    sys.exit(main())

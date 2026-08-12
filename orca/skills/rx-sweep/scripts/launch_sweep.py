#!/usr/bin/env python3
"""launch_sweep.py —— rx-sweep 实验矩阵的 8 卡并行调度器（contracts §5）。

确定性 GPU 状态机：``gpu_busy[0..gpus-1]``，按实验顺序填空闲 GPU，复现可预测。
每实验 ``subprocess.Popen`` 启动，env 注入 ``CUDA_VISIBLE_DEVICES=<gpu>``，
stdout/stderr 重定向到 ``<project_root>/<exp_id>.log``。

每实验启动 / 完成各打一行：
    [SWEEP] exp_id=... gpu=.. status=STARTED
    [SWEEP] exp_id=... gpu=.. status=SUCCESS|FAIL_gate|FAIL_train|FAIL_eval|SKIP

末行：``SWEEP_DONE: <results.jsonl path>``。

状态裁决（contracts §4）：gate 没过（日志无 gate=PASS / runner 显式 gate=FAIL）
→ ``FAIL_gate``；gate 过但 runner 非零退出 → ``FAIL_train``；rc=0 时优先用
``[RESULT]`` 行的 status 字段（可能 SUCCESS / FAIL_eval），缺则 SUCCESS。

fail-soft：单实验崩只记 status=FAIL_gate / FAIL_train 不阻断其它；
launch_sweep 自身错误（矩阵读不上 / results 写不进）→ 非零退出。

results.jsonl 每实验一行（contracts §4 schema）。

纯 stdlib + subprocess（在用户工程跑，不依赖 orca）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ── variant → (pilot, lmmse) 映射（contracts §1）─────────────────────────────
def variant_flags(variant: str) -> tuple[bool, bool]:
    """据 variant 名推 (pilot, lmmse) 开关。model8 / pure_cnn 均无。"""
    if variant == "model8" or variant == "pure_cnn":
        return (False, False)
    pilot = "pilot" in variant
    lmmse = "lmmse" in variant
    return (pilot, lmmse)


# 默认结构超参（contracts §1，与 pure_cnn_model.py 默认一致）。
DEFAULT_NUM_BLOCKS = 4
DEFAULT_EMBED_DIM = 16

# 解析 [RESULT] 行中的 key=value token（值不含空白）。
_RESULT_KV_RE = re.compile(r"(\w+)=([^\s]+)")


def parse_result_line(line: str) -> dict[str, Any]:
    """解析 ``[RESULT] key=val key=val ...`` 行 → dict。

    数值字段（accuracy / latency_ms / train_loss_final / epochs）转 float；
    布尔字段（gate_passed）转 bool；其它保持字符串。
    """
    out: dict[str, Any] = {}
    for key, raw in _RESULT_KV_RE.findall(line):
        if raw in ("True", "False"):
            out[key] = raw == "True"
        elif key in {"accuracy", "latency_ms", "train_loss_final", "epochs"}:
            try:
                out[key] = float(raw)
            except ValueError:
                out[key] = raw  # 留原值，下游可见解析异常
        else:
            out[key] = raw
    return out


def scan_log(log_path: Path) -> tuple[dict[str, Any], bool]:
    """扫描实验日志，返回 (result_dict, gate_passed)。

    - [RX-GATE] 行含 ``gate=PASS`` → gate_passed=True；含 gate=FAIL → False；缺失 → False。
    - [RESULT] 行（通常末尾一行）→ 解析为 dict。
    """
    result: dict[str, Any] = {}
    gate_passed = False
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return (result, False)

    for line in text.splitlines():
        if "[RX-GATE]" in line:
            if "gate=PASS" in line:
                gate_passed = True
            elif "gate=FAIL" in line:
                gate_passed = False
        elif "[RESULT]" in line:
            # 多行 [RESULT] 时后行覆盖前行（最后一次为准）。
            result.update(parse_result_line(line))
    return (result, gate_passed)


def build_result_row(
    exp: dict[str, Any],
    gpu: int,
    parsed: dict[str, Any],
    gate_passed: bool,
    status: str,
    fail_reason: str,
    epochs: int,
) -> dict[str, Any]:
    """组装一行 results.jsonl（contracts §4 schema）。缺失数值字段用 None。"""
    pilot, lmmse = variant_flags(exp["variant"])
    return {
        "exp_id": exp["exp_id"],
        "variant": exp["variant"],
        "pilot": pilot,
        "lmmse": lmmse,
        "kd": bool(exp.get("kd", False)),
        "num_blocks": DEFAULT_NUM_BLOCKS,
        "embed_dim": DEFAULT_EMBED_DIM,
        "accuracy": parsed.get("accuracy"),
        "accuracy_kind": parsed.get("accuracy_kind"),
        "latency_ms": parsed.get("latency_ms"),
        "gate_passed": gate_passed,
        "train_loss_final": parsed.get("train_loss_final"),
        "epochs": epochs,
        "gpu": gpu,
        "status": status,
        "fail_reason": fail_reason,
    }


def next_idle_gpu(gpu_busy: list[bool]) -> int:
    """返回第一个空闲 GPU 下标；全忙 → -1。按 index 顺序填，确定性。"""
    for i, busy in enumerate(gpu_busy):
        if not busy:
            return i
    return -1


def launch_experiment(
    exp: dict[str, Any],
    gpu: int,
    project_root: Path,
    runner: str,
    teacher_ckpt: str | None,
    epochs: int,
    python: str,
) -> tuple[subprocess.Popen, Path]:
    """启动一个实验子进程。返回 (popen, log_path)。"""
    log_path = project_root / f"{exp['exp_id']}.log"
    cmd: list[str] = [python, runner, "--variant", exp["variant"], "--epochs", str(epochs)]
    if exp.get("kd"):
        cmd.append("--kd")
        if teacher_ckpt:
            cmd.extend(["--teacher-ckpt", teacher_ckpt])

    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = str(gpu)

    # stdout/stderr 合并写日志文件（跨平台，> log 2>&1 等价）。
    log_file = open(log_path, "w", encoding="utf-8")
    popen = subprocess.Popen(
        cmd,
        cwd=str(project_root),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
    )
    log_file.close()  # fd 已传给子进程；父端关文件对象避免泄漏
    return (popen, log_path)


def run_sweep(
    project_root: Path,
    matrix_path: Path,
    gpus: int,
    results_path: Path,
    runner: str,
    teacher_ckpt: str | None,
    epochs: int,
    python: str,
    poll: float,
) -> int:
    """主调度循环。返回 0=全部调度完成（个别实验失败已记入 results）。"""
    # 1. 读矩阵（fail loud）。
    try:
        experiments = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[launch_sweep] 错误：读矩阵失败 {matrix_path}: {e}", file=sys.stderr)
        return 1
    if not isinstance(experiments, list):
        print(
            f"[launch_sweep] 错误：矩阵顶层应为 list，实际 {type(experiments).__name__}",
            file=sys.stderr,
        )
        return 1

    # 2. 打开 results.jsonl（fail loud）。
    results_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        results_file = open(results_path, "w", encoding="utf-8")
    except OSError as e:
        print(
            f"[launch_sweep] 错误：无法写 results {results_path}: {e}",
            file=sys.stderr,
        )
        return 1

    # 3. 预处理队列：kd 实验无 teacher_ckpt → 早 SKIP，不进调度。
    #    按 exp 顺序遍历保确定性。
    queue: list[dict[str, Any]] = []
    with results_file:
        for exp in experiments:
            exp_id = exp.get("exp_id", "<no_exp_id>")
            if exp.get("kd") and not teacher_ckpt:
                row = build_result_row(
                    exp,
                    gpu=-1,
                    parsed={},
                    gate_passed=False,
                    status="SKIP",
                    fail_reason="kd 实验未提供 --teacher-ckpt",
                    epochs=epochs,
                )
                results_file.write(json.dumps(row, ensure_ascii=False) + "\n")
                results_file.flush()
                print(f"[SWEEP] exp_id={exp_id} gpu=-1 status=SKIP")
                continue
            queue.append(exp)

        # 4. GPU 状态机：gpu_busy[i]=True 表示第 i 卡在跑。
        gpu_busy = [False] * gpus
        # gpu_of[exp_id] = 分配的 GPU（完成后回收）。
        gpu_of: dict[str, int] = {}
        # in_flight[exp_id] = (popen, log_path)。
        in_flight: dict[str, tuple[subprocess.Popen, Path]] = {}

        # 5. 主循环：有空闲 GPU 且队列非空 → 启动；轮询完成的 → 收结果。
        while queue or in_flight:
            # 5a. 尽量填满空闲 GPU（按队列顺序，确定性）。
            while queue:
                gpu = next_idle_gpu(gpu_busy)
                if gpu < 0:
                    break
                exp = queue.pop(0)
                try:
                    popen, log_path = launch_experiment(
                        exp=exp,
                        gpu=gpu,
                        project_root=project_root,
                        runner=runner,
                        teacher_ckpt=teacher_ckpt,
                        epochs=epochs,
                        python=python,
                    )
                except OSError as e:
                    # 启动失败（runner 路径错 / python 找不到）→ FAIL_train，不阻断。
                    row = build_result_row(
                        exp,
                        gpu=gpu,
                        parsed={},
                        gate_passed=False,
                        status="FAIL_train",
                        fail_reason=f"启动失败：{e}",
                        epochs=epochs,
                    )
                    results_file.write(json.dumps(row, ensure_ascii=False) + "\n")
                    results_file.flush()
                    print(
                        f"[SWEEP] exp_id={exp['exp_id']} gpu={gpu} status=FAIL_train"
                    )
                    continue
                gpu_busy[gpu] = True
                gpu_of[exp["exp_id"]] = gpu
                in_flight[exp["exp_id"]] = (popen, log_path)
                print(f"[SWEEP] exp_id={exp['exp_id']} gpu={gpu} status=STARTED")

            # 5b. 没在跑的 → 结束。
            if not in_flight:
                break

            # 5c. 轮询所有在跑实验；完成的回收。rc_of 记每实验退出码（防
            #     误用上一轮循环的 rc：done_ids 处理时按 exp 各自查 rc_of）。
            time.sleep(poll)
            done_ids: list[str] = []
            rc_of: dict[str, int] = {}
            for exp_id, (popen, log_path) in in_flight.items():
                rc = popen.poll()
                if rc is None:
                    continue
                done_ids.append(exp_id)
                rc_of[exp_id] = rc

            for exp_id in done_ids:
                popen, log_path = in_flight.pop(exp_id)
                gpu = gpu_of.pop(exp_id)
                gpu_busy[gpu] = False
                exp = next(e for e in experiments if e.get("exp_id") == exp_id)
                rc = rc_of[exp_id]

                # 解析日志拿 [RESULT] + [RX-GATE]。
                parsed, gate_passed = scan_log(log_path)

                # 状态裁决（contracts §4）：先看 gate_passed —— runner 走 sys.exit(1)
                # 时 rc=1，单看 rc 会把 gate=FAIL 误标 FAIL_train（B3），真实原因丢失。
                if not gate_passed:
                    # gate 没过（runner 打了 gate=FAIL 后自己 exit 1，或根本没打 GATE 行）
                    status = "FAIL_gate"
                    fail_reason = f"runner gate=FAIL (rc={rc})"
                elif rc != 0:
                    status = "FAIL_train"
                    fail_reason = f"runner 退出码 {rc}"
                else:
                    # 退出 0：优先用 [RESULT] 的 status 字段（可能 SUCCESS / FAIL_eval），
                    # 无 [RESULT] 行 → SUCCESS。
                    parsed_status = parsed.get("status")
                    if isinstance(parsed_status, str) and parsed_status:
                        status = parsed_status
                    else:
                        status = "SUCCESS"
                    fail_reason = ""

                row = build_result_row(
                    exp,
                    gpu=gpu,
                    parsed=parsed,
                    gate_passed=gate_passed,
                    status=status,
                    fail_reason=fail_reason,
                    epochs=epochs,
                )
                results_file.write(json.dumps(row, ensure_ascii=False) + "\n")
                results_file.flush()
                print(f"[SWEEP] exp_id={exp_id} gpu={gpu} status={status}")

    # 6. 末行契约。
    print(f"SWEEP_DONE: {results_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="rx-sweep 实验矩阵 8 卡并行调度器（contracts §5）。"
    )
    parser.add_argument("--project-root", required=True, help="用户工程根目录。")
    parser.add_argument("--matrix", required=True, help="build_matrix.py 生成的 JSON。")
    parser.add_argument(
        "--gpus", type=int, default=8, help="并行 GPU 数（默认 8）。"
    )
    parser.add_argument(
        "--results", required=True, help="输出 results.jsonl 路径（每实验一行）。"
    )
    parser.add_argument(
        "--runner",
        default="rx_runner.py",
        help="实验 runner 脚本（相对 project-root 或绝对路径）。默认 rx_runner.py。",
    )
    parser.add_argument(
        "--teacher-ckpt",
        default=None,
        help="teacher checkpoint 路径（kd 实验必需，缺失则 kd 实验 SKIP）。",
    )
    parser.add_argument(
        "--epochs", type=int, default=3, help="训练轮数（默认 3）。"
    )
    parser.add_argument(
        "--python", default="python", help="Python 解释器（默认 python）。"
    )
    parser.add_argument(
        "--poll", type=float, default=5.0, help="轮询间隔秒（默认 5）。"
    )
    args = parser.parse_args(argv)

    if args.gpus < 1:
        print(f"[launch_sweep] 错误：--gpus 必须 ≥1，当前 {args.gpus}", file=sys.stderr)
        return 2

    project_root = Path(args.project_root)
    if not project_root.is_dir():
        print(
            f"[launch_sweep] 错误：project-root 不存在或非目录：{project_root}",
            file=sys.stderr,
        )
        return 2

    return run_sweep(
        project_root=project_root,
        matrix_path=Path(args.matrix),
        gpus=args.gpus,
        results_path=Path(args.results),
        runner=args.runner,
        teacher_ckpt=args.teacher_ckpt,
        epochs=args.epochs,
        python=args.python,
        poll=args.poll,
    )


if __name__ == "__main__":
    sys.exit(main())

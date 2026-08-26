#!/usr/bin/env python3
"""gate_check.py —— rx-sweep 检验门脚本

对单个 model 跑 1 step 验正确：
  1. 跑 `python <runner> --model <name> [--kd --teacher-ckpt <p>] --epochs 1`
     （设 CUDA_VISIBLE_DEVICES=<gpu>，默认 300s 超时）。
  2. 从 stdout 抓 [RX-GATE] key=val 行 + [RESULT]/loss 行。
  3. 验 gate=PASS 且 model 匹配请求且 io_in/io_out==[1,P,F,S,1]（P/F/S 从同一
     GATE 行解析，与 runner 的 RxConfig 单一真相源一致——不硬编码维度，根治 64/32 漂移），
     并确认前向+反向没崩（有 [RESULT] 或 loss 行）。
  4. stdout: [GATE-RESULT] model=... passed=true|false reason=...
     exit 0 (passed) / 1 (failed)。

被测工程崩（runner 崩 / gate FAIL）→ exit 1 + stdout 的 [GATE-RESULT]。
本脚本自身崩（参数错 / 内部异常，区别于被测工程崩）→ exit 2 + stderr。

契约：reference/contracts.md §3（GATE 格式）+ §5（脚本 CLI）。
纯 stdlib + subprocess，不依赖 orca / rx_models（维度从 GATE 行解析，无需 import 包）。
"""

import argparse
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path


# 子进程默认超时
_TIMEOUT_DEFAULT = 300

# [RX-GATE] key=val key=val ...
_GATE_LINE_RE = re.compile(r"\[RX-GATE\]\s*(.*)")
# [RESULT] ...
_RESULT_LINE_RE = re.compile(r"\[RESULT\]\s*(.*)")
# 任意 loss=... / step_loss=... token（确认前向+反向跑过）
_LOSS_TOKEN_RE = re.compile(r"(?:step_)?loss\s*=\s*([-\d.eE+]+)")
# Exception/Error 行（crash reason 提取）
_EXC_RE = re.compile(r"(\w*(?:Error|Exception)\w*(?::[^\n]*)?)")


# ---------------------------------------------------------------------------
# 解析辅助
# ---------------------------------------------------------------------------

def _parse_kv(payload: str) -> dict:
    """解析 'key=val key=val ...' 串。值原样字符串（io_in=[1,4,48,64,1] 整体作一个 token）。"""
    out = {}
    for tok in payload.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def _parse_shape(s: str) -> list | None:
    """解析 '[1,4,48,32,1]' → [1,4,48,32,1]。失败返回 None。"""
    s = s.strip().lstrip("[").rstrip("]")
    parts = [p.strip() for p in s.split(",") if p.strip()]
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            return None
    return out


def _parse_int(s: str) -> int | None:
    """解析 '32' → 32。失败返回 None。"""
    s = s.strip()
    try:
        return int(s)
    except ValueError:
        return None


def _extract_gate(stdout: str) -> dict | None:
    """从 stdout 抓最后一个 [RX-GATE] 行的 kv dict（多行时以最后一行为准）。"""
    found = None
    for line in stdout.splitlines():
        m = _GATE_LINE_RE.search(line)
        if m:
            found = _parse_kv(m.group(1))
    return found


def _has_result_or_loss(stdout: str) -> tuple[bool, str]:
    """确认前向+反向跑过：抓 [RESULT] 行 或 loss= token。返回 (hit, line)。"""
    for line in stdout.splitlines():
        if _RESULT_LINE_RE.search(line):
            return True, line.strip()
        if _LOSS_TOKEN_RE.search(line):
            return True, line.strip()
    return False, ""


def _extract_crash_reason(stdout: str, stderr: str, rc: int) -> str:
    """从 stderr/stdout 抓最后一个 Exception/Error 行，拼成人话 reason 片段。"""
    last = None
    for src in (stderr, stdout):
        for line in src.splitlines():
            m = _EXC_RE.search(line)
            if m:
                last = m.group(1).strip()
    if last:
        return f"rc={rc}, {last[:200]}"
    return f"rc={rc}"


# ---------------------------------------------------------------------------
# 子进程
# ---------------------------------------------------------------------------

def _run_runner(python, runner_path, model, kd, teacher_ckpt, gpu, timeout):
    """跑 runner 子进程。返回 (returncode, stdout, stderr, timed_out)。"""
    env = os.environ.copy()
    if gpu is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(gpu)

    cmd = [python, str(runner_path), "--model", model, "--epochs", "1"]
    if kd:
        cmd.append("--kd")
    if teacher_ckpt:
        cmd += ["--teacher-ckpt", str(teacher_ckpt)]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or "", False
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        err = e.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return -1, out, err, True


# ---------------------------------------------------------------------------
# 核心：跑一次检验
# ---------------------------------------------------------------------------

def check(project_root: Path, model: str, kd: bool, teacher_ckpt,
          gpu, runner: str, python: str, timeout: int):
    """跑一次检验。返回 (passed: bool, reason: str, detail: dict)。"""

    runner_path = (project_root / runner).resolve()
    if not runner_path.is_file():
        return False, f"runner not found: {runner_path}", {"error": "runner_missing"}

    rc, stdout, stderr, timed_out = _run_runner(
        python, runner_path, model, kd, teacher_ckpt, gpu, timeout,
    )

    detail = {
        "returncode": rc,
        "stdout_tail": (stdout or "")[-400:],
        "stderr_tail": (stderr or "")[-400:],
        "timed_out": timed_out,
    }

    if timed_out:
        return False, f"timeout ({timeout}s)", detail

    gate = _extract_gate(stdout)

    # (a) runner 在打印 GATE 前就崩
    if gate is None:
        crash = _extract_crash_reason(stdout, stderr, rc)
        return False, f"no [RX-GATE] line printed; {crash}", detail

    # (b) 逐项校验 GATE 字段
    gate_flag = gate.get("gate", "").upper()
    actual_model = gate.get("model", "")
    io_in = _parse_shape(gate.get("io_in", ""))
    io_out = _parse_shape(gate.get("io_out", ""))

    # 期望 io = [1, P, F, S, 1]：P/F/S 从同一 GATE 行解析（runner 的 RxConfig 单一
    # 真相源 → GATE 行报的维度即期望维度），不在 gate_check 硬编码——根治 64/32 漂移。
    p = _parse_int(gate.get("P", ""))
    f = _parse_int(gate.get("F", ""))
    s = _parse_int(gate.get("S", ""))
    if None not in (p, f, s):
        expected_io = [1, p, f, s, 1]
    else:
        expected_io = None

    reasons = []
    if gate_flag != "PASS":
        reasons.append(f"gate={gate_flag or '<missing>'}")
    if actual_model != model:
        reasons.append(
            f"model mismatch (got {actual_model!r}, want {model!r})"
        )
    if expected_io is None:
        reasons.append("GATE 行缺 P/F/S 维度字段（无法推期望 io）")
    else:
        if io_in is None or io_in != expected_io:
            reasons.append(f"io_in mismatch {io_in} (expect {expected_io})")
        if io_out is None or io_out != expected_io:
            reasons.append(f"io_out mismatch {io_out} (expect {expected_io})")

    # (c) runner 非零退出（前向/反向崩）—— 仅当 gate=PASS 才算"应跑通却崩"
    if gate_flag == "PASS" and rc != 0:
        crash = _extract_crash_reason(stdout, stderr, rc)
        reasons.append(f"runner exited non-zero ({crash})")

    # (d) 确认前向+反向跑过：抓 [RESULT] / loss 行
    if gate_flag == "PASS":
        ok_result, _ = _has_result_or_loss(stdout)
        if not ok_result:
            reasons.append(
                "no [RESULT] or loss line (forward/backward not confirmed)"
            )

    if reasons:
        return False, "; ".join(reasons), detail

    return True, "all checks passed", detail


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="rx-sweep 检验门")
    ap.add_argument("--project-root", required=True, help="工程根目录")
    ap.add_argument("--model", required=True,
                    help="待验 rx_models 方案名，如 pure_cnn / feat_complex / model8_trf")
    ap.add_argument("--kd", action="store_true", help="蒸馏模式")
    ap.add_argument("--teacher-ckpt", default=None, help="teacher ckpt 路径")
    ap.add_argument("--gpu", default=None, help="CUDA_VISIBLE_DEVICES")
    ap.add_argument("--runner", default="rx_runner.py",
                    help="runner 文件名（相对 project-root），默认 rx_runner.py")
    ap.add_argument("--python", default=sys.executable,
                    help="python 解释器，默认本脚本所用")
    ap.add_argument("--timeout", type=int, default=_TIMEOUT_DEFAULT,
                    help=f"子进程超时秒数（默认 {_TIMEOUT_DEFAULT}）")
    args = ap.parse_args()

    try:
        project_root = Path(args.project_root).resolve()
        if not project_root.is_dir():
            # gate_check 自身参数错（区别于被测 FAIL 的 exit 1）
            print(f"gate_check: --project-root 不是目录: {project_root}",
                  file=sys.stderr)
            return 2

        passed, reason, _detail = check(
            project_root=project_root,
            model=args.model,
            kd=args.kd,
            teacher_ckpt=args.teacher_ckpt,
            gpu=args.gpu,
            runner=args.runner,
            python=args.python,
            timeout=args.timeout,
        )
    except Exception as e:  # noqa: BLE001 —— gate_check 自身崩必须区别于被测 FAIL
        print(f"gate_check: internal error: {e!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    print(
        f"[GATE-RESULT] model={args.model} "
        f"passed={'true' if passed else 'false'} reason={reason}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

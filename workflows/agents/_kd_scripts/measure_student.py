"""measure_student.py —— measure_student 节点确定性后端（契约 §4）。

对齐 `workflows/agents/_kd_scripts/CONTRACTS.md` §4 `measure_student`。

步骤：
    1. 动态 import student + load ckpt（eval）；
    2. 导 ONNX → 实测 latency（load `--latency_provider module::func`）；
    3. 跑 `--eval_command` → 解析 student accuracy；
    4. 从 `--teacher_meta` 读 teacher accuracy，算 dB gap：
       - MSE/NMSE: db_gap = 10*log10(student_mse / teacher_mse)
       - SNR    : db_gap = teacher_snr − student_snr （SNR 高更好）
       - BER    : 用 MSE 比值 dB fallback，标 confidence=low
       - 解析失败: db_gap 用 |student-teacher| 占比，confidence=low
    5. met_accuracy = db_gap ≤ threshold（默认 0.5 dB，或 --accuracy_gap_db）
    6. met_latency = latency_ms ≤ target（teacher_meta.teacher_latency_ms 或 --target_latency_ms）

stdout::

    STUDENT_LATENCY_MS: <float>
    STUDENT_ACCURACY: <float>
    STUDENT_DB_GAP: <float>
    MET_ACCURACY: <bool>
    MET_LATENCY: <bool>
    STUDENT_ONNX: <path>

fail loud：import / ckpt / ONNX / latency 失败 → 非零退出 + stderr。
eval_command 非零退出 → fail loud；解析不出精度 → fallback + confidence=low（不致命）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any


# ── 复用 _struct_scripts/export_onnx.export_onnx ───────────────────────────────
def _export_onnx(model_path, build_fn, dummy_input, opset, out, device: str = "auto"):
    """复用 _struct_scripts/export_onnx.export_onnx（同 struct 测时延的导出路径）。

    device 透传给 export_onnx（默认 auto：cuda→npu→cpu 探测，与 latency_onnxrt 一致；
    P7 解开原硬编码 device="cpu"，让导出能上 GPU/NPU）。
    """
    here_struct = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_struct_scripts")
    )
    if here_struct not in sys.path:
        sys.path.insert(0, here_struct)
    from export_onnx import export_onnx  # type: ignore

    return export_onnx(
        model_path=model_path, build_fn=build_fn, dummy_input=dummy_input,
        opset=opset, out=out, device=device,
    )


def _load_measure(latency_provider: str):
    if "::" not in latency_provider:
        raise ValueError(f"latency_provider 需 'path::func'，得到 {latency_provider!r}")
    path, func = latency_provider.split("::", 1)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"latency_provider 文件不存在: {path}")
    spec = importlib.util.spec_from_file_location("cost_model", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    measure = getattr(mod, func, None)
    if not callable(measure):
        raise TypeError(f"{path}::{func} 不是 callable")
    return measure


_ACC_PATTERNS = [
    (re.compile(r"STUDENT_ACCURACY\s*[:=]\s*([0-9]*\.?[0-9]+)", re.I), "acc"),
    (re.compile(r"\baccuracy\s*[:=]\s*([0-9]*\.?[0-9]+)", re.I), "acc"),
    (re.compile(r"\bNMSE\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)", re.I), "nmse"),
    (re.compile(r"\bMSE\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)", re.I), "mse"),
    (re.compile(r"\bBER\s*[:=]\s*([0-9]*\.?[0-9]+)", re.I), "ber"),
    (re.compile(r"\bSNR[_-]?dB\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+)", re.I), "snr"),
    (re.compile(r"\bSNR\s*[:=]\s*([-+]?[0-9]*\.?[0-9]+)", re.I), "snr"),
]


def _parse_accuracy(stdout: str) -> tuple[float, str, str]:
    """返回 (value, kind, confidence)。"""
    for line in stdout.splitlines()[::-1]:
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                d = json.loads(s)
                for k, kind in (("accuracy", "acc"), ("acc", "acc"),
                                ("nmse", "nmse"), ("mse", "mse"),
                                ("ber", "ber"), ("snr", "snr"), ("snr_db", "snr")):
                    if k in d and isinstance(d[k], (int, float)):
                        return float(d[k]), kind, "high"
            except json.JSONDecodeError:
                pass
    for pat, kind in _ACC_PATTERNS:
        m = pat.search(stdout)
        if m:
            return float(m.group(1)), kind, "high"
    return 0.0, "unknown", "low"


def _run(cmd: str, cwd: str, env: dict | None = None) -> str:
    proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"eval_command 非零退出({proc.returncode}): {cmd}\nstderr:\n{proc.stderr[-1000:]}"
        )
    return proc.stdout


# ── dB gap 计算 ────────────────────────────────────────────────────────────────
def _compute_db_gap(
    teacher_acc: float, teacher_kind: str,
    student_acc: float, student_kind: str,
    accuracy_gap_db: float,
) -> tuple[float, str, bool]:
    """返回 (db_gap, confidence, met_accuracy)。

    kind 优先用 teacher_kind（teacher_meta 里记录）；student 没解析出来则用 teacher_kind 比值。
    """
    # 决定统一 kind：以 teacher 为准（teacher_meta 有记录）
    kind = teacher_kind if teacher_kind != "unknown" else student_kind

    confidence = "high" if (teacher_kind != "unknown" and student_kind != "unknown") else "low"

    try:
        if kind in ("mse", "nmse"):
            # 误差型，越小越好；teacher=0 时退化
            t = max(teacher_acc, 1e-12)
            s = max(student_acc, 1e-12)
            gap = 10.0 * math.log10(s / t)
        elif kind == "snr":
            # SNR 越高越好：db_gap = teacher − student（正=student 差）
            gap = teacher_acc - student_acc
        elif kind == "ber":
            # BER 越小越好；用 log 比值近似 dB
            t = max(teacher_acc, 1e-12)
            s = max(student_acc, 1e-12)
            gap = 10.0 * math.log10(s / t)
            confidence = "low"  # BER→dB 仅近似
        elif kind == "acc":
            # 分类 accuracy：越高越好；gap 用 (teacher - student) 占比放大
            # 近似 dB：10*log10((1-student)/(1-teacher))（error rate 比值）
            t_err = max(1.0 - teacher_acc, 1e-6)
            s_err = max(1.0 - student_acc, 1e-6)
            gap = 10.0 * math.log10(s_err / t_err)
        else:
            # 完全未知：占位
            gap = abs(student_acc - teacher_acc)
            confidence = "low"
    except (ValueError, ZeroDivisionError) as e:
        gap = float("inf")
        confidence = "low"
        print(f"[measure_student] dB gap 计算异常: {e}", file=sys.stderr)

    met = bool(gap <= accuracy_gap_db)
    return float(gap), confidence, met


# ── 内部 MSE 评测（自包含，无 eval_command 时用，便于测试）─────────────────────
def _eval_dataset_mse(model_path: str, build_fn: str, ckpt_path: str, dataset_path: str) -> tuple[float, str]:
    """load student + ckpt → 在 eval_dataset（.pt 含 {x, y}）上算 MSE。

    返回 (mse_value, "mse")。fail loud：模型/dataset 加载失败。
    """
    import torch

    if not os.path.isfile(dataset_path):
        raise FileNotFoundError(f"eval_dataset 不存在: {dataset_path}")
    data = torch.load(dataset_path, map_location="cpu")
    if not isinstance(data, dict) or "x" not in data or "y" not in data:
        raise ValueError(f"eval_dataset 需含 {'x','y'} 键，得到 keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
    x, y = data["x"], data["y"]

    # 动态 import student model（与 teacher_setup.py 同款 importlib）
    model_dir = os.path.dirname(os.path.abspath(model_path))
    module_name = Path(model_path).stem
    if model_dir not in sys.path:
        sys.path.insert(0, model_dir)
    spec = importlib.util.spec_from_file_location(module_name, model_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    factory = getattr(mod, build_fn, None)
    if not callable(factory):
        raise AttributeError(f"{module_name} 无 callable {build_fn}")
    student = factory()
    student.eval()

    if ckpt_path and os.path.isfile(ckpt_path):
        ck = torch.load(ckpt_path, map_location="cpu")
        sd = ck.get("state_dict", ck) if isinstance(ck, dict) else ck
        missing, unexpected = student.load_state_dict(sd, strict=False)
        if missing:
            print(f"[measure_student] WARN missing keys (top5): {list(missing)[:5]}", file=sys.stderr)
        if unexpected:
            print(f"[measure_student] WARN unexpected keys (top5): {list(unexpected)[:5]}", file=sys.stderr)

    with torch.no_grad():
        out = student(x)
        # 对齐 shape（student/teacher 输出 [B,4,48,64,1]；y 可能同形或 squeeze 过）
        if out.shape != y.shape:
            try:
                y = y.view_as(out)
            except Exception:
                out = out.reshape_as(y)
        mse = float(torch.mean((out - y) ** 2).item())
    return mse, "mse"


# ── 主流程 ────────────────────────────────────────────────────────────────────
def measure_student(args) -> dict:
    out_dir = os.path.abspath(args.output_dir)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    # 1. 导 ONNX（export_onnx 内部会 import + build；我们不持有 ckpt state，
    #    因为 export 只需结构。但 latency 由 ONNX 决定，不需要 ckpt 权重）。
    #    ckpt 仅在 student 需要量化权重时才 load；这里 ONNX 路径用 build_model 默认权重，
    #    若用户要量化后的 latency，让 engineer 在 export 前把 ckpt 烧进 model.py。
    student_onnx = os.path.join(out_dir, "student.onnx")
    _export_onnx(
        args.student_model_path, args.build_fn, args.dummy_input,
        args.opset, student_onnx, device=args.device,
    )

    # 2. 测 latency（device 透传给 latency_provider：auto/cuda/npu/cpu）
    measure = _load_measure(args.latency_provider)
    # latency_provider 是 `path::func`；measure 接受 device kwarg（latency_onnxrt.py 默认）。
    # 用 inspect 检测形参（裸 try/except TypeError 会误吞用户脚本内部的 TypeError）。
    import inspect
    if "device" in inspect.signature(measure).parameters:
        latency_ms = float(measure(student_onnx, device=args.device))
    else:
        latency_ms = float(measure(student_onnx))

    # 3. student accuracy
    #    优先用 --eval_dataset（.pt 含 {x, y}）内部算 MSE（自包含，便于测试/无 eval_command 时）；
    #    否则跑 --eval_command（用户 eval 脚本，自行加载 ckpt）。
    #    P7 M3：candidate_eval 短训阶段既不给 eval_command 也不给 eval_dataset → **跳过 db_gap 计算**
    #    （之前白算一个垃圾 0.0/unknown db_gap 写进 measure_report 误导 debug）。
    eval_provided = (
        (args.eval_dataset and args.eval_dataset.strip())
        or (args.eval_command and args.eval_command.strip())
    )
    if args.eval_dataset and args.eval_dataset.strip():
        student_acc, student_kind = _eval_dataset_mse(
            args.student_model_path, args.build_fn, args.student_ckpt,
            args.eval_dataset,
        )
    elif args.eval_command and args.eval_command.strip():
        import os as _os
        _env = dict(_os.environ)
        _env["STUDENT_CKPT"] = os.path.abspath(args.student_ckpt) if args.student_ckpt else ""
        _env["STUDENT_MODEL_PATH"] = os.path.abspath(args.student_model_path)
        _env["STUDENT_OUTPUT_DIR"] = out_dir
        raw = _run(args.eval_command, args.project_root, env=_env)
        student_acc, student_kind, _ = _parse_accuracy(raw)
    else:
        # 短训阶段（latency-first candidate_eval）：不跑 eval → accuracy 未知。
        # 不算 db_gap（避免占位 0.0/unknown 写盘误导）；stdout 仍打 MET_ACCURACY: false。
        print(
            "[measure_student] neither --eval_dataset nor --eval_command given; "
            "skipping accuracy + dB gap computation (latency-only mode for short-train phase).",
            file=sys.stderr,
        )
        student_acc, student_kind = 0.0, "unknown"

    # 4. 读 teacher_meta
    if not os.path.isfile(args.teacher_meta):
        raise FileNotFoundError(f"teacher_meta 不存在: {args.teacher_meta}")
    teacher_meta = json.loads(
        Path(args.teacher_meta).read_text(encoding="utf-8")
    )
    teacher_acc = float(teacher_meta.get("teacher_accuracy", 0.0))
    teacher_kind = str(teacher_meta.get("teacher_accuracy_kind", "unknown"))

    # 5. dB gap（仅当 eval 已跑；短训阶段 latency-only → 全 sentinel）
    accuracy_gap_db = float(args.accuracy_gap_db) if args.accuracy_gap_db is not None else 0.5
    if eval_provided:
        db_gap, gap_conf, met_acc = _compute_db_gap(
            teacher_acc, teacher_kind, student_acc, student_kind, accuracy_gap_db,
        )
    else:
        # latency-only 模式（candidate_eval 短训）：dB gap 未知，恒 sentinel。
        db_gap, gap_conf, met_acc = -1.0, "deferred", False

    # 6. latency target
    if args.target_latency_ms is not None:
        target_lat = float(args.target_latency_ms)
    elif "teacher_latency_ms" in teacher_meta:
        target_lat = float(teacher_meta["teacher_latency_ms"])
    else:
        target_lat = float("inf")
    met_lat = bool(latency_ms <= target_lat)

    # 7. 写 measure_report.json（非契约强制，但便于 agent 节点 debug）
    report = {
        "student_onnx": student_onnx,
        "student_model_path": os.path.abspath(args.student_model_path),
        "student_ckpt": os.path.abspath(args.student_ckpt) if args.student_ckpt else "",
        "latency_ms": latency_ms,
        "student_accuracy": student_acc,
        "student_accuracy_kind": student_kind,
        "teacher_accuracy": teacher_acc,
        "teacher_accuracy_kind": teacher_kind,
        "db_gap": db_gap,
        "db_gap_confidence": gap_conf,
        "db_gap_deferred": not eval_provided,  # P7：短训 latency-only 模式标 deferred
        "accuracy_gap_db_threshold": accuracy_gap_db,
        "target_latency_ms": target_lat,
        "met_accuracy": met_acc,
        "met_latency": met_lat,
    }
    report_path = os.path.join(out_dir, "measure_report.json")
    Path(report_path).write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {
        "student_onnx": student_onnx,
        "measure_report": report_path,
        "latency_ms": latency_ms,
        "student_accuracy": student_acc,
        "db_gap": db_gap,
        "db_gap_confidence": gap_conf,
        "met_accuracy": met_acc,
        "met_latency": met_lat,
    }


def _main() -> int:
    p = argparse.ArgumentParser(
        description="measure_student 确定性后端（契约 §4）"
    )
    p.add_argument("--student_model_path", required=True)
    p.add_argument("--student_ckpt", default="", help="可选（latency 由 ONNX 决定）")
    p.add_argument("--build_fn", required=True)
    p.add_argument("--dummy_input", required=True)
    p.add_argument("--eval_command", default="",
                   help="可选：用户 eval 脚本 shell 命令（自行加载 ckpt）；空则用 --eval_dataset")
    p.add_argument("--eval_dataset", default="",
                   help="可选：.pt 文件含 {'x':tensor,'y':tensor}，内部算 MSE（自包含，推荐用于测试/无 eval_command 时）")
    p.add_argument("--teacher_meta", required=True, help="teacher_meta.json 路径")
    p.add_argument("--output_dir", required=True)
    p.add_argument("--opset", type=int, default=17)
    p.add_argument("--latency_provider", required=True,
                   help="path::func，如 .../latency_onnxrt.py::measure")
    p.add_argument("--accuracy_gap_db", type=float, default=None,
                   help="判定 met_accuracy 的 dB 阈值（默认 0.5）")
    p.add_argument("--target_latency_ms", type=float, default=None,
                   help="latency 目标；缺省用 teacher_latency_ms")
    p.add_argument("--project_root", default=".", help="eval_command 的 cwd")
    p.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cuda", "npu", "cpu"],
        help="ONNX 导出 + latency 测量设备（P7；默认 auto，与 latency_onnxrt 一致）",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=0,
        help="复现种子（默认 0）",
    )
    args = p.parse_args()

    try:
        r = measure_student(args)
    except Exception as e:
        print(f"[measure_student] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    print(f"STUDENT_LATENCY_MS: {r['latency_ms']:.4f}")
    print(f"STUDENT_ACCURACY: {r['student_accuracy']}")
    print(f"STUDENT_DB_GAP: {r['db_gap']}")
    print(f"MET_ACCURACY: {str(r['met_accuracy']).lower()}")
    print(f"MET_LATENCY: {str(r['met_latency']).lower()}")
    print(f"STUDENT_ONNX: {r['student_onnx']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

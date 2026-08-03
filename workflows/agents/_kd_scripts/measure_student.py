"""measure_student.py —— KD-NAS student 精度/时延测量（确定性后端）。

本模块职责：
  - **精度测量**（distill 节点主用）：跑 ``--eval_command``（或 ``--eval_dataset``）→ 解析 student
    accuracy → 对比**用户提供的绝对精度基线** ``--accuracy_baseline``（方向由 kind 决定）。
    teacher 不再是精度参考（teacher 只当 KD 软标签源），故 ``--teacher_meta`` 改可选、
    teacher-relative dB-gap 路径降级为 legacy。
  - **时延测量**（可选）：导 ONNX + ``--latency_provider`` 实测。distill 节点**复用**
    selector 的 latency（不在真硬件上重测），故传 ``--skip_latency`` 跳过（latency_us=-1）。

方向语义（绝对基线对比）：
  - kind ∈ {mse, nmse, ber}（误差型，越低越好）→ ``met = student ≤ baseline``
  - kind ∈ {snr, acc}（越高越好）              → ``met = student ≥ baseline``
  - kind unknown                                → ``met = false`` + confidence=low + 大声 WARN
  - ``--accuracy_baseline_kind`` override：给则锁方向 + 校验自动检测一致（不符 WARN，用 override）。

stdout::

    STUDENT_LATENCY_US: <float>      # --skip_latency 时 -1
    STUDENT_ACCURACY:   <float>
    STUDENT_ACCURACY_KIND: <kind>
    MET_ACCURACY:       <bool>
    MET_LATENCY:        <bool>       # --skip_latency 时 false
    STUDENT_ONNX:       <path>       # --skip_latency 时空串
    ACCURACY_CONFIDENCE: high|low

fail loud：eval_command 非零退出 / latency_provider 加载失败 / export 失败 → 非零退出。
精度解析不出 → fallback + confidence=low（不致命，但 met_accuracy=false）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from kd_common import accuracy_direction  # noqa: E402


# ── 复用 _struct_scripts/export_onnx.export_onnx ───────────────────────────────
def _export_onnx(model_path, build_fn, dummy_input, opset, out, device: str = "auto",
                 build_kwargs: dict[str, Any] | None = None, seed: int = 0):
    """复用 _struct_scripts/export_onnx.export_onnx（build_kwargs 透传给 build_fn，KD 调参用）。"""
    here_struct = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_struct_scripts")
    )
    if here_struct not in sys.path:
        sys.path.insert(0, here_struct)
    from export_onnx import export_onnx  # type: ignore

    return export_onnx(
        model_path=model_path, build_fn=build_fn, dummy_input=dummy_input,
        opset=opset, out=out, device=device, seed=seed, build_kwargs=build_kwargs,
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

# kind → best 方向的单一真相源在 kd_common.accuracy_direction（HIGHER_BETTER_KINDS /
# LOWER_BETTER_KINDS，含 db）。本模块不再维护本地方向表（防三处漂移）。


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


def _compute_met_accuracy_absolute(
    student_acc: float, detected_kind: str, baseline: float, kind_override: str,
) -> tuple[bool, str, str]:
    """绝对精度基线对比。返回 (met_accuracy, used_kind, confidence)。

    kind_override 非空 → 锁方向；若与 detected_kind 不符 → WARN（用 override）。
    kind unknown → met=false, confidence=low（绝不静默 pass）。
    """
    used = (kind_override or "").strip().lower() or detected_kind
    confidence = "high"
    if kind_override and detected_kind != "unknown" and detected_kind != used:
        print(
            f"[measure_student] WARN: 自动检测 kind={detected_kind!r} 与 "
            f"--accuracy_baseline_kind={used!r} 不符；按 override {used!r} 判定。",
            file=sys.stderr,
        )
    direction = accuracy_direction(used)
    if direction == "max":
        met = bool(student_acc >= baseline)
    elif direction == "min":
        met = bool(student_acc <= baseline)
    else:
        # unknown：无法判方向 → 不达标 + 低置信 + 大声 WARN（绝不静默 pass）。
        met = False
        confidence = "low"
        print(
            f"[measure_student] WARN: accuracy kind 未知（detected={detected_kind!r}, "
            f"override={kind_override!r}）；无法判方向 → met_accuracy=false, confidence=low。",
            file=sys.stderr,
        )
    return met, used, confidence


# ── 内部 MSE 评测（自包含，无 eval_command 时用，便于测试）─────────────────────
def _eval_dataset_mse(model_path: str, build_fn: str, ckpt_path: str, dataset_path: str,
                      build_kwargs: dict[str, Any] | None = None) -> tuple[float, str]:
    """load student + ckpt → 在 eval_dataset（.pt 含 {x, y}）上算 MSE。返回 (mse_value, "mse")。"""
    import torch

    if not os.path.isfile(dataset_path):
        raise FileNotFoundError(f"eval_dataset 不存在: {dataset_path}")
    data = torch.load(dataset_path, map_location="cpu")
    if not isinstance(data, dict) or "x" not in data or "y" not in data:
        raise ValueError(f"eval_dataset 需含 {'x','y'} 键，得到 keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
    x, y = data["x"], data["y"]

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
    student = factory(**build_kwargs) if build_kwargs else factory()
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

    build_kwargs = None
    if args.build_cfg and args.build_cfg.strip():
        build_kwargs = json.loads(args.build_cfg)
        if not isinstance(build_kwargs, dict):
            raise ValueError("--build_cfg 必须是 JSON object")

    skip_latency = bool(args.skip_latency)

    # 1-2. 时延（可选；--skip_latency 跳过，distill 复用 selector latency）。
    if skip_latency:
        student_onnx = ""
        latency_us = -1.0
        met_lat = False
    else:
        if not (args.dummy_input and args.dummy_input.strip()):
            raise ValueError("非 --skip_latency 模式需要 --dummy_input（用户指定 I/O 维度）")
        student_onnx = os.path.join(out_dir, "student.onnx")
        _export_onnx(
            args.student_model_path, args.build_fn, args.dummy_input,
            args.opset, student_onnx, device=args.device,
            build_kwargs=build_kwargs, seed=args.seed,
        )
        measure = _load_measure(args.latency_provider)
        import inspect
        if "device" in inspect.signature(measure).parameters:
            latency_us = float(measure(student_onnx, device=args.device))
        else:
            latency_us = float(measure(student_onnx))
        if args.target_latency_us is not None:
            met_lat = bool(latency_us <= float(args.target_latency_us))
        else:
            met_lat = True  # 未给 target：不卡时延门（distill 不用此路径判门）

    # 3. student accuracy（eval_command 或 eval_dataset；都没有 → unknown）。
    eval_provided = (
        (args.eval_dataset and args.eval_dataset.strip())
        or (args.eval_command and args.eval_command.strip())
    )
    if args.eval_dataset and args.eval_dataset.strip():
        student_acc, student_kind = _eval_dataset_mse(
            args.student_model_path, args.build_fn, args.student_ckpt,
            args.eval_dataset, build_kwargs=build_kwargs,
        )
    elif args.eval_command and args.eval_command.strip():
        _env = dict(os.environ)
        _env["STUDENT_CKPT"] = os.path.abspath(args.student_ckpt) if args.student_ckpt else ""
        _env["STUDENT_MODEL_PATH"] = os.path.abspath(args.student_model_path)
        _env["STUDENT_OUTPUT_DIR"] = out_dir
        raw = _run(args.eval_command, args.project_root, env=_env)
        student_acc, student_kind, _ = _parse_accuracy(raw)
    else:
        student_acc, student_kind = 0.0, "unknown"

    # 4. 精度判定：绝对基线（唯一路径）。
    if args.accuracy_baseline is not None:
        met_acc, used_kind, acc_conf = _compute_met_accuracy_absolute(
            student_acc, student_kind, float(args.accuracy_baseline),
            args.accuracy_baseline_kind or "",
        )
    else:
        met_acc = False
        acc_conf = "low"
        used_kind = student_kind
        if not eval_provided:
            print("[measure_student] 无 eval + 无 accuracy_baseline → met_accuracy=false (low)",
                  file=sys.stderr)
        elif eval_provided:
            print("[measure_student] WARN: 有 eval 但未给 --accuracy_baseline → met_accuracy=false "
                  "(low)；新设计须显式给绝对基线。", file=sys.stderr)

    # 5. 写 measure_report.json（debug 用）。
    report = {
        "student_onnx": student_onnx,
        "student_model_path": os.path.abspath(args.student_model_path),
        "student_ckpt": os.path.abspath(args.student_ckpt) if args.student_ckpt else "",
        "build_cfg": build_kwargs,
        "latency_us": latency_us,
        "latency_skipped": skip_latency,
        "student_accuracy": student_acc,
        "student_accuracy_kind": student_kind,
        "accuracy_kind_used": used_kind,
        "accuracy_baseline": args.accuracy_baseline,
        "accuracy_confidence": acc_conf,
        "target_latency_us": args.target_latency_us,
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
        "latency_us": latency_us,
        "student_accuracy": student_acc,
        "student_accuracy_kind": used_kind,
        "accuracy_confidence": acc_conf,
        "met_accuracy": met_acc,
        "met_latency": met_lat,
    }


def _compute_db_gap(*args, **kwargs):  # noqa: D401
    """不再支持：teacher-relative dB gap 路径已移除，用 --accuracy_baseline 绝对基线对比。"""
    raise NotImplementedError(
        "teacher-relative dB gap 已移除；用 --accuracy_baseline 绝对基线对比"
    )


def _main() -> int:
    p = argparse.ArgumentParser(description="KD-NAS student 精度/时延测量（确定性后端）")
    p.add_argument("--student_model_path", required=True)
    p.add_argument("--student_ckpt", default="", help="可选；eval_command 自行加载")
    p.add_argument("--build_fn", required=True)
    p.add_argument("--build_cfg", default="", help="JSON：传给 build_fn 的 kwargs（调参后 cfg）")
    p.add_argument("--dummy_input", default="",
                   help="JSON I/O 维度（用户指定）；--skip_latency 时可省")
    p.add_argument("--eval_command", default="", help="用户 eval 脚本 shell 命令")
    p.add_argument("--eval_dataset", default="",
                   help=".pt 含 {'x','y'}，内部算 MSE（测试/无 eval_command 时）")
    p.add_argument("--accuracy_baseline", type=float, default=None,
                   help="用户提供的绝对精度基线（新设计主路径）")
    p.add_argument("--accuracy_baseline_kind", default="",
                   help="锁方向 + 校验：nmse/mse/ber(越低越好) | snr/acc(越高越好)")
    p.add_argument("--output_dir", required=True)
    p.add_argument("--opset", type=int, default=17)
    p.add_argument("--latency_provider", default="",
                   help="path::func；--skip_latency 时可省")
    p.add_argument("--target_latency_us", type=float, default=None, help="latency 目标")
    p.add_argument("--project_root", default=".", help="eval_command 的 cwd")
    p.add_argument("--skip_latency", action="store_true",
                   help="跳过 ONNX 导出 + latency 测量（distill 复用 selector latency 时用）")
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "npu", "cpu"])
    p.add_argument("--seed", type=int, default=0, help="复现种子（ONNX 导出用）")
    args = p.parse_args()

    try:
        r = measure_student(args)
    except Exception as e:
        print(f"[measure_student] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    print(f"STUDENT_LATENCY_US: {r['latency_us']:.4f}")
    print(f"STUDENT_ACCURACY: {r['student_accuracy']}")
    print(f"STUDENT_ACCURACY_KIND: {r['student_accuracy_kind']}")
    print(f"MET_ACCURACY: {str(r['met_accuracy']).lower()}")
    print(f"MET_LATENCY: {str(r['met_latency']).lower()}")
    print(f"STUDENT_ONNX: {r['student_onnx']}")
    print(f"ACCURACY_CONFIDENCE: {r['accuracy_confidence']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

"""measure_baseline.py —— baseline_measure 节点的确定性后端（草稿 §4/§5/§10）。

把原 family_detect 里"导出 ONNX + 测时延 + 取 baseline accuracy + seed champions"这 4 件
**确定性**活抽出来，做成脚本（Rule 5：deterministic 逻辑用代码）。baseline_measure 节点
只负责调本脚本 + 解析输出。

pre-trained 模式（用户有 baseline 预训练权重时）：
  - 给了 --test_command → 只跑 test_command（load ckpt 测，**不训练**）。
  - 给了 --baseline_accuracy → 直接用（连测试都省）。
  - 否则 → 跑 --train_command（训练）。

stdout 结构化（key=value，末行 JSON），供节点解析。fail loud：任何异常 → 非零退出 + stderr。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path


# ── 复用同目录的 export_onnx + 动态加载 latency_provider ──────────────────────
def _export_onnx(model_path, build_fn, dummy_input, opset, out, device: str = "auto", seed: int = 0):
    """复用 export_onnx.export_onnx（同目录 import）。

    P7：解开原硬编码 device="cpu"，透传 device + seed。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from export_onnx import export_onnx  # type: ignore

    return export_onnx(
        model_path=model_path,
        build_fn=build_fn,
        dummy_input=dummy_input,
        opset=opset,
        out=out,
        device=device,
        seed=seed,
    )


def _load_measure(latency_provider: str):
    """latency_provider 格式 '/abs/path/file.py::measure' → callable。fail loud。"""
    if "::" not in latency_provider:
        raise ValueError(f"latency_provider 需 'path::func' 形态，得到 {latency_provider!r}")
    path, func = latency_provider.split("::", 1)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"latency_provider 文件不存在: {path}")
    import importlib.util

    spec = importlib.util.spec_from_file_location("cost_model", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    measure = getattr(mod, func, None)
    if not callable(measure):
        raise TypeError(f"{path}::{func} 不是 callable")
    return measure


# ── 从命令 stdout 解析 accuracy（鲁棒多格式）──────────────────────────────────
_ACC_PATTERNS = [
    re.compile(r"FINAL_ACCURACY\s*[:=]\s*([0-9]*\.?[0-9]+)", re.I),
    re.compile(r"baseline[_-]?acc(?:uracy)?\s*[:=]\s*([0-9]*\.?[0-9]+)", re.I),
    re.compile(r"\baccuracy\s*[:=]\s*([0-9]*\.?[0-9]+)", re.I),
]


def _parse_accuracy(stdout: str) -> float:
    """从训练/测试命令 stdout 解析 accuracy。先 JSON 行，再正则。fail loud。"""
    for line in stdout.splitlines()[::-1]:  # 末行优先
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                d = json.loads(s)
                for k in ("accuracy", "acc", "val_acc", "val_accuracy"):
                    if k in d and isinstance(d[k], (int, float)):
                        return float(d[k])
            except json.JSONDecodeError:
                pass
    for pat in _ACC_PATTERNS:
        m = pat.search(stdout)
        if m:
            return float(m.group(1))
    raise ValueError(
        f"无法从命令 stdout 解析 accuracy。stdout 末尾:\n{stdout[-500:]!r}"
    )


def _run(cmd: str, cwd: str) -> str:
    """原样 shell 执行用户的 train/test 命令，返回 stdout。fail loud（非零退出 raise）。"""
    proc = subprocess.run(
        cmd, shell=True, cwd=cwd, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"命令非零退出({proc.returncode}): {cmd}\nstderr:\n{proc.stderr[-1000:]}"
        )
    return proc.stdout


# ── 主流程 ────────────────────────────────────────────────────────────────────
def measure_baseline(args) -> dict:
    out_dir = os.path.abspath(args.output_dir)
    snapshots = os.path.join(out_dir, "snapshots")
    Path(snapshots).mkdir(parents=True, exist_ok=True)
    champions_path = os.path.join(out_dir, "champions.jsonl")
    ledger_path = os.path.join(out_dir, "ledger.jsonl")

    # 1. 导出 baseline ONNX（P7：seed 透传给 export_onnx，dummy 输入复现）
    onnx_path = _export_onnx(
        args.model_path, args.build_fn, args.dummy_input, args.opset,
        out=os.path.join(snapshots, "baseline.onnx"),
        device=getattr(args, "device", "auto"),
        seed=getattr(args, "seed", 0),
    )

    # 2. 实测时延（cost model，LLM 永不预测）
    measure = _load_measure(args.latency_provider)
    # device 透传给 latency_provider；旧式 measure 无 device kwarg → 用 inspect 检测后 fallback。
    import inspect
    device = getattr(args, "device", "auto")
    if "device" in inspect.signature(measure).parameters:
        latency_ms = float(measure(onnx_path, device=device))
    else:
        latency_ms = float(measure(onnx_path))

    # 3. baseline accuracy（pre-trained 只测 / 给定 / 训练）
    if args.test_command and args.test_command.strip():
        mode = "test"
        acc = _parse_accuracy(_run(args.test_command, args.project_root))
    elif args.baseline_accuracy and args.baseline_accuracy.strip():
        mode = "given"
        acc = float(args.baseline_accuracy)
    else:
        mode = "train"
        acc = _parse_accuracy(_run(args.train_command, args.project_root))

    # 4. accuracy_target（默认 baseline − 0.005）
    if args.accuracy_target and args.accuracy_target.strip():
        target = float(args.accuracy_target)
    else:
        target = acc - 0.005

    # 5. seed champions.jsonl（baseline = round 0 champion）+ 空 ledger.jsonl
    model_abs = os.path.abspath(args.model_path)
    champion = {
        "round": 0,
        "id": "baseline",
        "latency_ms": latency_ms,
        "accuracy": acc,
        "delta_vs_baseline_ms": 0.0,
        "snapshot": model_abs,
    }
    Path(champions_path).write_text(json.dumps(champion) + "\n", encoding="utf-8")
    if not os.path.exists(ledger_path):
        Path(ledger_path).write_text("", encoding="utf-8")

    return {
        "status": "ok",
        "onnx_path": onnx_path,
        "baseline_latency_ms": latency_ms,
        "baseline_accuracy": acc,
        "accuracy_target": target,
        "accuracy_mode": mode,
        "champions_path": champions_path,
        "ledger_path": ledger_path,
        "model_snapshot": model_abs,
    }


def _main() -> int:
    p = argparse.ArgumentParser(description="baseline_measure 确定性后端（fail loud）")
    p.add_argument("--output_dir", required=True)
    p.add_argument("--model_path", required=True)
    p.add_argument("--build_fn", required=True)
    p.add_argument("--dummy_input", required=True, help="JSON 字符串或文件路径")
    p.add_argument("--opset", type=int, default=17)
    p.add_argument("--latency_provider", required=True, help="path::func")
    p.add_argument("--train_command", required=True)
    p.add_argument("--test_command", default="", help="pre-trained 模式：只测不训")
    p.add_argument("--pretrained_ckpt", default="", help="可选：预训练权重路径（仅元信息）")
    p.add_argument("--baseline_accuracy", default="", help="给定则跳过 train/test")
    p.add_argument("--accuracy_target", default="")
    p.add_argument("--project_root", default=".", help="train/test 命令的 cwd")
    p.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cuda", "npu", "cpu"],
        help="ONNX 导出 + latency 测量设备（P7；默认 auto，cuda→npu→cpu 探测）",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=0,
        help="复现种子（默认 0）",
    )
    args = p.parse_args()

    try:
        r = measure_baseline(args)
    except Exception as e:
        print(f"[measure_baseline] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2
    # 结构化 stdout（key=value + 末行 JSON）
    for k, v in r.items():
        print(f"{k.upper()}: {v}")
    print(f"RESULT_JSON: {json.dumps(r)}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

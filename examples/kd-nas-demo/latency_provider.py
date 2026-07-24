"""latency_provider.py —— kd-nas-demo 的 ONNX latency 测量（onnxruntime 真实计时）。

签名对齐 ``tune_latency._measure_cfg`` / ``measure_student._load_measure`` 的期望：
``measure(onnx_path, ...) -> float (ms)``（``device`` 形参可选，调用方按需注入）。

用法（kd-nas inputs.latency_provider 的值）::

    <abs>/examples/kd-nas-demo/latency_provider.py::measure

**绝不伪造**：onnxruntime 实跑 ``runs`` 次取中位数（ms）；文件缺失/加载失败 → raise（fail loud）。
默认 ``runs=5 / warmup=2``（demo 偏快；真硬件实测可调大）。
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time


def _select_providers(device: str) -> list[str]:
    """device → onnxruntime providers 顺位（无可用加速器 / cpu → CPUExecutionProvider）。"""
    import onnxruntime as ort

    available = set(ort.get_available_providers())
    if device == "cuda" and "CUDAExecutionProvider" in available:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    if device == "npu" and "CANNExecutionProvider" in available:
        return ["CANNExecutionProvider", "CPUExecutionProvider"]
    # auto / cpu / 加速器不可用 → CPU（demo 默认路径，CPU-only 环境稳健）
    return ["CPUExecutionProvider"]


def measure(
    onnx_path: str,
    runs: int = 5,
    warmup: int = 2,
    device: str = "auto",
    seed: int = 0,
) -> float:
    """实跑 ONNX 取中位数单次推理时延（ms）。

    Args:
        onnx_path: ONNX 文件路径。
        runs: 正式计时跑次数（中位数降噪）。
        warmup: 预热次数（不计入）。
        device: auto / cuda / npu / cpu → onnxruntime providers 顺位。
        seed: dummy 输入随机种子（可复现）。

    Returns:
        中位数单次推理时延（ms，浮点）。

    Raises:
        FileNotFoundError: onnx_path 不存在。
        Exception: onnxruntime 加载/推理异常原样抛（fail loud）。
    """
    if not os.path.isfile(onnx_path):
        raise FileNotFoundError(f"ONNX 文件不存在: {onnx_path}")

    import numpy as np
    import onnxruntime as ort

    providers = _select_providers(device)
    sess = ort.InferenceSession(onnx_path, providers=providers)
    actual = sess.get_providers()
    print(
        f"[demo latency_provider] device_arg={device!r} requested={providers} actual={actual}",
        file=sys.stderr,
    )

    rng = np.random.default_rng(seed)
    inp: dict[str, object] = {}
    for i in sess.get_inputs():
        # 动态维度（字符串/None）→ 取 1；静态 int → 原值。size=list（非解包）兼容 numpy 2.x。
        shape = [d if isinstance(d, int) else 1 for d in i.shape]
        inp[i.name] = rng.standard_normal(size=shape).astype(np.float32)

    for _ in range(max(warmup, 0)):
        sess.run(None, inp)

    ts: list[float] = []
    for _ in range(max(runs, 1)):
        t = time.perf_counter()
        sess.run(None, inp)
        ts.append(time.perf_counter() - t)
    return statistics.median(ts) * 1000.0


def _main() -> int:
    p = argparse.ArgumentParser(description="kd-nas-demo ONNX latency provider（onnxruntime 实测）")
    p.add_argument("--onnx", required=True, help="ONNX 文件路径")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--warmup", type=int, default=2)
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "npu", "cpu"])
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    try:
        ms = measure(args.onnx, runs=args.runs, warmup=args.warmup, device=args.device, seed=args.seed)
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(f"LATENCY_MS: {ms:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

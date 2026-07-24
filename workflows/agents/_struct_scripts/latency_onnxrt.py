"""latency_onnxrt.py —— 默认 cost model（§5 逐字实现）。

契约钉死（草稿 §5 / 不变量1）：workflow 永远通过一个 ``measure(onnx_path)->float`` 函数取
时延，**LLM 永不预测时延**。本模块是默认实现：onnxruntime 实跑取中位数 ms。

被加载方式（见 struct-evaluator / setup / finalize）::

    path, func = "latency_onnxrt.py::measure".split("::")
    spec = importlib.util.spec_from_file_location("cost_model", path)
    mod  = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    measure = getattr(mod, func)
    latency_ms = measure(onnx_path)

也可直接 CLI 跑（自检 / 一次性测量）::

    python latency_onnxrt.py --onnx path/to/model.onnx
    python latency_onnxrt.py --onnx path/to/model.onnx --runs 50 --warmup 10 --device npu

device（P7 新增）：
    - ``measure`` 接受可选 ``device`` 参数（默认 "auto"），由 ``_device.ort_providers`` 映射到
      onnxruntime provider 顺位。NPU=Ascend 走 CANNExecutionProvider。
    - 通过 ``::measure`` 动态加载的调用方（evaluator/setup/finalize）若不传 device 则用 auto；
      agent 节点用 ``--device`` CLI 显式跑或读 ``inputs.device`` 注入。
    - 跑完 stderr 打印实际 providers（透明性：让用户看到真在 cuda/npu/cpu 上测）。

fail loud：ONNX 文件缺失 / onnxruntime 加载失败 → 非零退出 + stderr 完整异常（§5 契约：
"不是 callable 或加载失败 → fail loud，整轮停"）。
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from typing import Any


def measure(
    onnx_path: str,
    runs: int = 20,
    warmup: int = 5,
    device: str = "auto",
    seed: int = 0,
) -> float:
    """实跑 ONNX 取中位数时延（ms）。§5 逐字实现 + 边界 fail loud。

    Args:
        onnx_path: ONNX 模型文件路径（绝对或相对 cwd）。
        runs: 正式计时跑次数（中位数降噪）。
        warmup: 预热次数（不计入计时；消除首次开销）。
        device: "auto"（cuda→npu→cpu 探测，默认）/ "cuda" / "npu" / "cpu"。映射到
            onnxruntime providers 顺位（``_device.ort_providers``）。
        seed: dummy 输入随机种子（可复现）。

    Returns:
        中位数单次推理时延（ms，浮点）。

    Raises:
        FileNotFoundError: onnx_path 不存在（fail loud，§5）。
        Exception: onnxruntime 加载 / 推理异常原样抛（fail loud，整轮停）。
    """
    # 延迟 import：只在实际测量时加载 onnxruntime / numpy（避免 CLI --help 拖重）。
    if not os.path.isfile(onnx_path):
        raise FileNotFoundError(f"ONNX 文件不存在: {onnx_path}")

    import numpy as np
    import onnxruntime as ort

    # 本脚本同目录的 _device.py 提供 ort_providers（inline 自 NAS，不引跨包依赖）。
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from _device import ort_providers  # type: ignore

    providers = ort_providers(device)
    rng = np.random.default_rng(seed)

    sess = ort.InferenceSession(onnx_path, providers=providers)
    # 透明性：实际跑在哪些 provider 上（agent 节点 / CLI 用户可见）。
    actual = sess.get_providers()
    print(
        f"[latency_onnxrt] device_arg={device!r} requested_providers={providers} "
        f"actual_providers={actual}",
        file=sys.stderr,
    )

    # 构造 dummy 输入：动态维度（字符串/None）→ 取 1；静态 int → 原值。逐输入独立 dtype。
    inp: dict[str, Any] = {}
    for i in sess.get_inputs():
        shape = []
        for d in i.shape:
            shape.append(d if isinstance(d, int) else 1)
        inp[i.name] = rng.standard_normal(*shape).astype(np.float32)

    for _ in range(warmup):
        sess.run(None, inp)

    ts: list[float] = []
    for _ in range(runs):
        t = time.perf_counter()
        sess.run(None, inp)
        ts.append(time.perf_counter() - t)
    return statistics.median(ts) * 1000.0


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="默认 cost model：onnxruntime 实跑取中位数时延(ms)。§5 契约实现。"
    )
    parser.add_argument("--onnx", required=True, help="ONNX 模型文件路径")
    parser.add_argument(
        "--runs", type=int, default=20, help="正式计时跑次数（默认 20）"
    )
    parser.add_argument(
        "--warmup", type=int, default=5, help="预热次数（默认 5，不计入计时）"
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cuda", "npu", "cpu"],
        help='Device: "auto" 探测 cuda→npu→cpu；"cuda"/"npu" 走对应 ORT provider（NPU=CANN）',
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="dummy 输入随机种子（默认 0，保复现）",
    )
    args = parser.parse_args()

    try:
        latency_ms = measure(
            args.onnx,
            runs=args.runs,
            warmup=args.warmup,
            device=args.device,
            seed=args.seed,
        )
    except Exception as e:
        import traceback

        print(f"[latency_onnxrt] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2  # 非零：fail loud（§5 整轮停）。
    # 结构化 stdout（key=value，下游 agent bash 解析）。
    print(f"LATENCY_MS: {latency_ms:.4f}")
    print(f"ONNX: {args.onnx}")
    print(f"RUNS: {args.runs}")
    print(f"WARMUP: {args.warmup}")
    print(f"DEVICE: {args.device}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

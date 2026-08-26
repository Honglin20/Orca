"""measure_latency.py —— model-flatten 契约默认 cfg 的 latency 测量（自包含，fail loud）。

把「`<base>_flat.py` 的 `__main__` 跑一次 = 正确性 + latency」中的 latency 部分抽成 helper，
供 flatten agent / flat 文件 `__main__` 复用。与 `validate_contract.py` 同目录
（`model-flatten/scripts/`），**不 import `_kd_scripts` / `_struct_scripts`**（flatten 保 standalone，
与 `validate_contract.py` 同款自包含原则；ONNX 导出 inline 实现，不依赖 `export_onnx.py`）。

职责（单一）：
  1. import 契约 .py → 取 build_model / DUMMY_INPUT / KNOBS（只读不校验——校验由 validate_contract 负责）
  2. `build_model(**KNOBS.defaults)` → 导 ONNX（inline `torch.onnx.export`，inline device 解析）
  3. 测 latency：
     - `latency_provider` 非空 → 加载 `path::func` → `measure(onnx[, device=...])` repeats 次取 median+std
     - `latency_provider` 空 → fallback ONNXRT-CPU + stderr WARN（非用户真硬件；KD-NAS workflow
       必填 latency_provider 时不应走到这里）
  4. 返回 `{latency_us_median, latency_us_std, source, confidence, onnx_path}`

绝不伪造（CONTRACTS §6）：测不出 → raise（caller fail loud），不编造数值。

CLI::

    python3 measure_latency.py --contract <flat.py> \\
        [--latency_provider path::func] [--device auto] [--seed 0] \\
        [--opset 17] [--repeats 3] [--onnx_out <path>]

stdout::

    LATENCY_US: <median>
    LATENCY_STD: <std>
    LATENCY_SOURCE: provider|cpu-fallback
    LATENCY_CONFIDENCE: high|low
    ONNX: <abs path>
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import os
import statistics
import sys
import time
import traceback
from pathlib import Path
from typing import Any


def _emit(key: str, value: Any) -> None:
    """stdout ``KEY: value`` 行（flatten agent bash awk 解析）。value 非 str → JSON 串。"""
    if isinstance(value, str):
        print(f"{key}: {value}")
    else:
        print(f"{key}: {json.dumps(value, ensure_ascii=False)}")


def _load_module(path: str) -> Any:
    """import .py 为 module（不入 sys.modules 持久化——测量只跑一次）。

    与 ``validate_contract._load_module`` 同款：契约文件的 sibling import（如 ``_model8_blocks``）
    需要其目录在 sys.path。
    """
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"contract 文件不存在：{p}")
    spec = importlib.util.spec_from_file_location(f"_measure_contract_{p.stem}", str(p))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {p} 创建 module spec")
    mod = importlib.util.module_from_spec(spec)
    parent = str(p.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec.loader.exec_module(mod)
    return mod


def _resolve_device(device_arg: str) -> Any:
    """torch.device 解析（auto → cuda→npu→cpu，对齐 validate_contract._resolve_device）。"""
    import torch

    if device_arg and device_arg != "auto":
        return torch.device(device_arg)
    if torch.cuda.is_available():
        return torch.device("cuda")
    try:
        import torch_npu  # noqa: F401
        if hasattr(torch, "npu") and torch.npu.is_available():
            return torch.device("npu")
    except ImportError:
        pass
    return torch.device("cpu")


def _ort_providers(device_arg: str) -> list[str]:
    """onnxruntime InferenceSession provider 顺位（CPU fallback 路径用）。

    Inline 自包含（不 import ``_kd_scripts/_device.py``，保 flatten standalone）；
    逻辑与 ``_device.ort_providers`` 一致。
    """
    import onnxruntime as ort

    avail = set(ort.get_available_providers())
    if device_arg == "auto":
        if "CUDAExecutionProvider" in avail:
            wanted = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        elif "CANNExecutionProvider" in avail:
            wanted = ["CANNExecutionProvider", "CPUExecutionProvider"]
        else:
            wanted = ["CPUExecutionProvider"]
    elif device_arg == "cuda":
        wanted = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    elif device_arg == "npu":
        wanted = ["CANNExecutionProvider", "CPUExecutionProvider"]
    else:
        wanted = ["CPUExecutionProvider"]
    picked = [p for p in wanted if p in avail]
    return picked or ["CPUExecutionProvider"]


def _load_measure(provider: str):
    """latency_provider（``path::func``）→ callable。

    与 ``tune_latency._load_measure`` / ``teacher_setup._load_measure`` 同款契约。
    """
    if "::" not in provider:
        raise ValueError(f"latency_provider 须为 'path::func' 形态，得到 {provider!r}")
    path, func = provider.split("::", 1)
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"latency_provider 文件不存在: {path}")
    d = os.path.dirname(path)
    if d not in sys.path:
        sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location(
        "_latprov_" + os.path.basename(path).replace(".", "_"), path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 latency_provider {path} 创建 module spec")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, func):
        raise AttributeError(f"{path} 无函数 {func!r}")
    return getattr(mod, func)


def _materialize_dummy(dummy_input: dict, device) -> Any:
    """``DUMMY_INPUT = {"shape":[...],"dtype":"float32"}`` → torch.randn tensor。"""
    import torch

    shape = list(dummy_input["shape"])
    dtype_name = dummy_input.get("dtype", "float32")
    if not hasattr(torch, dtype_name):
        raise ValueError(f"DUMMY_INPUT.dtype={dtype_name!r} 不是合法 torch dtype 名")
    dtype = getattr(torch, dtype_name)
    return torch.randn(*shape, dtype=dtype, device=device)


def _export_onnx_inline(
    mod: Any,
    build_fn_name: str,
    defaults: dict[str, Any],
    dummy_input: dict,
    device_arg: str,
    seed: int,
    opset: int,
    out_path: str,
) -> str:
    """inline ONNX 导出（不依赖 ``_struct_scripts/export_onnx.py``）。

    flatten 保 standalone（validate_contract.py 同款自包含原则）。导出确定性与 ``export_onnx``
    一致：``torch.manual_seed(seed)`` → 权重确定。
    """
    import torch

    torch.manual_seed(seed)
    build_fn = getattr(mod, build_fn_name, None)
    if not callable(build_fn):
        raise AttributeError(f"契约缺 callable {build_fn_name}")
    model = build_fn(**defaults)
    if not isinstance(model, torch.nn.Module):
        raise TypeError(f"{build_fn_name}() 返回 {type(model).__name__}，期望 nn.Module")
    device = _resolve_device(device_arg)
    model = model.to(device).eval()
    dummy = _materialize_dummy(dummy_input, device)
    out_path = os.path.abspath(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(
        model,
        dummy,
        out_path,
        input_names=["input"],
        opset_version=opset,
        dynamo=False,
    )
    return out_path


def _measure_with_provider(
    measure, onnx_path: str, device_arg: str, repeats: int
) -> tuple[float, float]:
    """调 ``measure(onnx[, device])`` ``repeats`` 次 → (median, pstdev)。

    与 ``tune_latency._measure_cfg`` 同款：provider 自带 runs/warmup，本函数外层再取
    ``repeats`` 次.median（抗硬件噪声）。
    """
    vals: list[float] = []
    accepts_device = "device" in inspect.signature(measure).parameters
    for _ in range(max(1, repeats)):
        if accepts_device:
            vals.append(float(measure(onnx_path, device=device_arg)))
        else:
            vals.append(float(measure(onnx_path)))
    med = float(statistics.median(vals))
    std = float(statistics.pstdev(vals)) if len(vals) > 1 else 0.0
    return med, std


def _measure_cpu_fallback(
    onnx_path: str, device_arg: str, repeats: int
) -> tuple[float, float]:
    """ONNXRT 直接测（latency_provider 未给时的 fallback）。

    返回 (median_us, pstdev_us)。stderr 打 WARN：非用户真硬件（KD-NAS workflow 必填
    latency_provider 时不应走到这里；flatten agent 自身容错为通用性）。
    """
    import numpy as np
    import onnxruntime as ort

    providers = _ort_providers(device_arg)
    sess = ort.InferenceSession(onnx_path, providers=providers)
    actual = sess.get_providers()
    print(
        f"[measure_latency] WARN: latency_provider 未给，fallback ONNXRT providers={actual}"
        "（非用户真硬件；KD-NAS workflow 必填 latency_provider，此路径仅 flatten 通用容错）",
        file=sys.stderr,
    )
    rng = np.random.default_rng(0)
    inp: dict[str, Any] = {}
    for i in sess.get_inputs():
        shape = [d if isinstance(d, int) else 1 for d in i.shape]
        inp[i.name] = rng.standard_normal(size=shape).astype(np.float32)
    # warmup（消除首次开销；与 latency_onnxrt 同款）
    for _ in range(2):
        sess.run(None, inp)
    ts: list[float] = []
    for _ in range(max(1, repeats)):
        t = time.perf_counter()
        sess.run(None, inp)
        ts.append(time.perf_counter() - t)
    med = float(statistics.median(ts) * 1e6)
    std = float(statistics.pstdev(ts) * 1e6) if len(ts) > 1 else 0.0
    return med, std


def measure_contract_latency(
    *,
    contract_path: str,
    latency_provider: str = "",
    device: str = "auto",
    seed: int = 0,
    opset: int = 17,
    repeats: int = 3,
    onnx_out: str = "",
) -> dict[str, Any]:
    """测契约默认 cfg 的 latency。

    Args:
        contract_path: 契约 .py 绝对路径（须含 build_model / DUMMY_INPUT / KNOBS）。
        latency_provider: ``path::func``；空 → ONNXRT-CPU fallback（confidence=low）。
        device: auto/cuda/npu/cpu（ONNX 导出 + provider 测量 device）。
        seed: 复现种子（ONNX 导出权重 init）。
        opset: ONNX opset 版本。
        repeats: 外层测量重复次数 → median+std。
        onnx_out: ONNX 输出路径；空 → contract 同目录 ``<stem>_baseline.onnx``。

    Returns:
        ``{latency_us_median, latency_us_std, source, confidence, onnx_path}``。

    Raises:
        任何 import / export / measure 异常（caller fail loud，不伪造数值）。
    """
    contract_path = os.path.abspath(contract_path)
    mod = _load_module(contract_path)

    # 只读契约字段（校验由 validate_contract 负责；此处只取测 latency 所需）。
    build_fn_name = getattr(mod, "BUILD_FN", "build_model")
    di = getattr(mod, "DUMMY_INPUT", None)
    if not isinstance(di, dict) or not isinstance(di.get("shape"), list) or not di["shape"]:
        raise ValueError("契约缺 DUMMY_INPUT.shape（无法导 ONNX 测 latency）")
    knobs = getattr(mod, "KNOBS", {})
    defaults = (
        {k: v["default"] for k, v in knobs.items()}
        if isinstance(knobs, dict) and knobs
        else {}
    )

    # ONNX 输出路径：显式 > contract 同名 _baseline.onnx
    if onnx_out:
        out_path = onnx_out
    else:
        stem = Path(contract_path).stem
        out_path = str(Path(contract_path).parent / f"{stem}_baseline.onnx")

    onnx_path = _export_onnx_inline(
        mod, build_fn_name, defaults, di, device, seed, opset, out_path
    )

    provider = (latency_provider or "").strip()
    if provider:
        measure = _load_measure(provider)
        med, std = _measure_with_provider(measure, onnx_path, device, repeats)
        source = "provider"
        confidence = "high"
    else:
        med, std = _measure_cpu_fallback(onnx_path, device, repeats)
        source = "cpu-fallback"
        confidence = "low"

    return {
        "latency_us_median": med,
        "latency_us_std": std,
        "source": source,
        "confidence": confidence,
        "onnx_path": onnx_path,
    }


def _main() -> int:
    p = argparse.ArgumentParser(
        description="model-flatten 契约默认 cfg latency 测量（fail loud；自包含）"
    )
    p.add_argument("--contract", required=True, help="展平产出的契约 .py 绝对路径")
    p.add_argument(
        "--latency_provider",
        default="",
        help="path::func（空 → ONNXRT-CPU fallback + WARN）",
    )
    p.add_argument("--device", default="auto", help="auto/cuda/npu/cpu")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--opset", type=int, default=17)
    p.add_argument(
        "--repeats", type=int, default=3, help="测量重复次数 → median+std"
    )
    p.add_argument(
        "--onnx_out",
        default="",
        help="ONNX 输出路径（空 → contract 同目录 <stem>_baseline.onnx）",
    )
    args = p.parse_args()

    try:
        res = measure_contract_latency(
            contract_path=args.contract,
            latency_provider=args.latency_provider,
            device=args.device,
            seed=args.seed,
            opset=args.opset,
            repeats=args.repeats,
            onnx_out=args.onnx_out,
        )
    except Exception as e:
        print(f"[measure_latency] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    _emit("LATENCY_US", f"{res['latency_us_median']:.6f}")
    _emit("LATENCY_STD", f"{res['latency_us_std']:.6f}")
    _emit("LATENCY_SOURCE", res["source"])
    _emit("LATENCY_CONFIDENCE", res["confidence"])
    _emit("ONNX", res["onnx_path"])
    return 0


if __name__ == "__main__":
    sys.exit(_main())

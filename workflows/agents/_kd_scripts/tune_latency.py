"""tune_latency.py —— KD-NAS 最小缩量 latency 调参（确定性，有界）。

哲学：latency 是结构属性（默认权重导 ONNX 即可测，不需训练）。变体默认 cfg 的 latency 超
``target_latency_ms`` 时，按 KNOBS 的 leverage 高→低、每次缩一档，**刚跨过 target 即停**
（最小缩量，保精度余量），不在真硬件上过度缩到很小。预算耗尽仍超 → FAIL_latency。

确定性 / 复现：
  - 每次 export 前 ``torch.manual_seed(seed)``（``export_onnx`` 内部已做）→ 每 cfg 权重确定。
  - ``cudnn.benchmark=False`` + ``use_deterministic_algorithms(True)``（best-effort）。
  - 每 cfg 测 ``--measure_repeats`` 次取 median + std（抗硬件噪声）。
  - 结果缓存 ``<artifacts_dir>/tune_cache.json`` key=(variant_id,cfg_hash,target)→{median,std}，
    distill recoverable 重试时读缓存、不在真硬件上重测。

CLI::
    python3 tune_latency.py --variant_path <.py> --dummy_input '<json>' --knobs '<json>' \
        --target_latency_ms <f> --latency_provider <path::func> --artifacts_dir <dir> \
        [--build_fn build_model] [--max_measurements 40] [--measure_repeats 3] \
        [--device auto] [--seed 0] [--opset 17]

stdout::
    TUNE_STATUS: ACCEPTED|FAIL_latency
    ACCEPTED_CFG: <json>        # ACCEPTED 时
    BEST_EFFORT_CFG: <json>     # FAIL_latency 时（最低 latency 的 cfg）
    LATENCY_MS_MEDIAN: <f>
    LATENCY_MS_STD: <f>
    MEASUREMENTS: <int>

fail loud：export/measure 异常 → exit 2（非零）。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import os
import statistics
import sys
import traceback
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_STRUCT = os.path.join(os.path.dirname(_HERE), "_struct_scripts")
if _STRUCT not in sys.path:
    sys.path.insert(0, _STRUCT)

from kd_common import RANK, provider_id  # noqa: E402


def _load_measure(provider: str):
    """加载 latency provider（``path::func``）。fail loud。"""
    if "::" not in provider:
        raise ValueError(f"latency_provider 须为 'path::func' 形态，得到 {provider!r}")
    path, func = provider.split("::", 1)
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"latency_provider 文件不存在: {path}")
    d = os.path.dirname(path)
    if d not in sys.path:
        sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location("_latprov_" + hashlib.md5(path.encode()).hexdigest()[:8], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, func):
        raise AttributeError(f"{path} 无函数 {func!r}")
    return getattr(mod, func)


def _cfg_hash(cfg: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]


def _atomic_write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_cache(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        obj = json.load(open(path, encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}  # 坏缓存不阻断（重新测）


def _measure_cfg(
    measure, onnx: str, device: str, repeats: int
) -> tuple[float, float]:
    """调 measure(onnx[, device]) ``repeats`` 次 → (median, pstdev)。"""
    vals: list[float] = []
    accepts_device = "device" in inspect.signature(measure).parameters
    for _ in range(max(1, repeats)):
        if accepts_device:
            vals.append(float(measure(onnx, device=device)))
        else:
            vals.append(float(measure(onnx)))
    med = float(statistics.median(vals))
    std = float(statistics.pstdev(vals)) if len(vals) > 1 else 0.0
    return med, std


def tune_latency(
    *,
    variant_path: str,
    build_fn: str,
    dummy_input: str,
    knobs: dict[str, dict[str, Any]],
    target_latency_ms: float,
    latency_provider: str,
    artifacts_dir: str,
    max_measurements: int,
    measure_repeats: int,
    device: str,
    seed: int,
    opset: int,
) -> dict[str, Any]:
    """跑最小缩量调参，返回结果 dict（status / cfg / latency_median / latency_std / measurements）。"""
    import torch  # 延迟 import（脚本顶层不强制 torch）
    # 确定性（best-effort；某些算子无 deterministic 实现则忽略）。
    try:
        torch.backends.cudnn.benchmark = False
    except Exception:
        pass
    try:
        torch.use_deterministic_algorithms(True, warn_only=True)
    except Exception:
        pass

    from export_onnx import export_onnx  # noqa: E402（_struct_scripts 已在 sys.path）

    measure = _load_measure(latency_provider)
    variant_id = os.path.splitext(os.path.basename(variant_path))[0]
    cur_provider_id = provider_id(latency_provider)  # cache key 含 provider 维度（换 provider → miss）
    cache_path = os.path.join(artifacts_dir, "tune_cache.json")
    cache = _load_cache(cache_path)
    tune_dir = os.path.join(artifacts_dir, "tune")
    os.makedirs(tune_dir, exist_ok=True)
    onnx_path = os.path.join(tune_dir, f"{variant_id}.onnx")

    measurements = {"n": 0}  # 闭包可变计数

    def measure_cfg(cfg: dict[str, Any]) -> tuple[float, float]:
        key = f"{variant_id}|{_cfg_hash(cfg)}|{target_latency_ms}|{cur_provider_id}"
        cached = cache.get(key)
        if isinstance(cached, dict) and "median" in cached:
            return float(cached["median"]), float(cached.get("std", 0.0))
        # 导 ONNX（默认权重 + 本 cfg；export_onnx 内部 torch.manual_seed(seed) → 权重确定）。
        export_onnx(
            model_path=variant_path,
            build_fn=build_fn,
            dummy_input=dummy_input,
            opset=opset,
            out=onnx_path,
            device=device,
            seed=seed,
            build_kwargs=cfg,
        )
        med, std = _measure_cfg(measure, onnx_path, device, measure_repeats)
        cache[key] = {"median": med, "std": std}
        _atomic_write_json(cache_path, cache)  # 每测必写（crash-safe）
        measurements["n"] += 1
        return med, std

    # 起点：默认 cfg。
    cfg = {k: kn["default"] for k, kn in knobs.items()}
    best_cfg = dict(cfg)
    med, std = measure_cfg(cfg)
    best_med = med

    def accepted(c: dict[str, Any], m: float, s: float) -> dict[str, Any]:
        return {
            "status": "ACCEPTED",
            "accepted_cfg": c,
            "latency_ms_median": m,
            "latency_ms_std": s,
            "measurements": measurements["n"],
        }

    if med <= target_latency_ms:
        return accepted(cfg, med, std)

    # 不可调变体（无 KNOBS）→ 起点 latency 即超阈 → FAIL_latency。
    if not knobs:
        return {
            "status": "FAIL_latency",
            "best_effort_cfg": best_cfg,
            "latency_ms_median": best_med,
            "latency_ms_std": std,
            "measurements": measurements["n"],
        }

    # 最小缩量：按 leverage 高→低，每 knob 每次缩一档，刚跨 target 即停。
    order = sorted(knobs, key=lambda k: RANK[knobs[k]["leverage"]])
    for k in order:
        step = knobs[k]["step"]
        mn = knobs[k]["min"]
        while cfg[k] + step >= mn:
            cfg[k] = cfg[k] + step
            med, std = measure_cfg(cfg)
            if med < best_med:
                best_med, best_cfg = med, dict(cfg)
            if med <= target_latency_ms:
                return accepted(cfg, med, std)
            if measurements["n"] >= max_measurements:
                return {
                    "status": "FAIL_latency",
                    "best_effort_cfg": best_cfg,
                    "latency_ms_median": best_med,
                    "latency_ms_std": std,
                    "measurements": measurements["n"],
                    "reason": "max_measurements reached",
                }
    # 所有 knob 缩到地板仍未达标。
    return {
        "status": "FAIL_latency",
        "best_effort_cfg": best_cfg,
        "latency_ms_median": best_med,
        "latency_ms_std": std,
        "measurements": measurements["n"],
        "reason": "all knobs at floor",
    }


def _main() -> int:
    p = argparse.ArgumentParser(description="KD-NAS 最小缩量 latency 调参（确定性，有界）")
    p.add_argument("--variant_path", required=True)
    p.add_argument("--build_fn", default="build_model")
    p.add_argument("--dummy_input", required=True, help="JSON 字符串（变体 DUMMY_INPUT）")
    p.add_argument("--knobs", required=True, help="JSON 字符串（变体 KNOBS；不可调传 {}）")
    p.add_argument("--target_latency_ms", required=True, type=float)
    p.add_argument("--latency_provider", required=True, help="path::func（用户真硬件 latency 脚本）")
    p.add_argument("--artifacts_dir", required=True, help="稳定 artifact 根（tune_cache.json + 临时 ONNX）")
    p.add_argument("--max_measurements", type=int, default=40, help="最大 cfg 测量数（硬 cap）")
    p.add_argument("--measure_repeats", type=int, default=3, help="每 cfg 重复测量次数 → median+std")
    p.add_argument("--device", default="auto")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--opset", type=int, default=17)
    args = p.parse_args()

    try:
        knobs = json.loads(args.knobs) if args.knobs.strip() else {}
        if not isinstance(knobs, dict):
            raise ValueError("--knobs 必须是 JSON object")
        dummy = args.dummy_input
        # dummy_input 允许传 JSON 对象串或 dict 串；export_onnx 要 JSON 串。
        if not isinstance(dummy, str):
            dummy = json.dumps(dummy)
        res = tune_latency(
            variant_path=args.variant_path,
            build_fn=args.build_fn,
            dummy_input=dummy,
            knobs=knobs,
            target_latency_ms=args.target_latency_ms,
            latency_provider=args.latency_provider,
            artifacts_dir=args.artifacts_dir,
            max_measurements=args.max_measurements,
            measure_repeats=args.measure_repeats,
            device=args.device,
            seed=args.seed,
            opset=args.opset,
        )
    except Exception as e:
        print(f"[tune_latency] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    status = res["status"]
    print(f"TUNE_STATUS: {status}")
    if status == "ACCEPTED":
        print(f"ACCEPTED_CFG: {json.dumps(res['accepted_cfg'], sort_keys=True)}")
    else:
        print(f"BEST_EFFORT_CFG: {json.dumps(res['best_effort_cfg'], sort_keys=True)}")
    print(f"LATENCY_MS_MEDIAN: {res['latency_ms_median']:.6f}")
    print(f"LATENCY_MS_STD: {res['latency_ms_std']:.6f}")
    print(f"MEASUREMENTS: {res['measurements']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

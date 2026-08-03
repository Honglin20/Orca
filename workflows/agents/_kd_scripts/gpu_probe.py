"""gpu_probe.py —— KD-NAS setup 阶段的 GPU 探测 + 并发判定（确定性，fail-soft）。

为什么需要：setup 是「并发数唯一权威」（见 CONTRACTS §6）。并发数不能写死（写死 2 在 OOM 边缘的卡上
会崩，在 8 卡机器上又浪费）。本脚本在 setup 内一次性探测：
  - 解析 device（cuda→npu→cpu auto）
  - per-variant 训练显存占用（建 representative 变体 + 加载 teacher_cache + Adam + 一次 fwd/bwd，
    读 ``max_memory_allocated`` 峰值——含 model/grad/Adam(m,v)/activation/teacher_cache）
  - 各卡 free VRAM
  - 并发公式 ``max(1, floor(total_free * safety / per_variant))``，cap 到 ``min(variants_count,
    max_concurrency)``，多卡 round-robin ``device_plan``

fail-soft（不阻塞 workflow）：
  - 无 CUDA/NPU（或 device=cpu）→ ``CONCURRENCY: 1`` + ``DEVICE_PLAN: [""]`` + WARN，exit 0
  - 探测过程异常（建模型 / 加载 teacher_cache 失败）→ 同上 fail-soft + WARN，exit 0
  - **仅输入契约不符**（representative_variant 缺 build_model / teacher_cache 文件不存在）→ exit 2
    （fail loud，是配置错误不是硬件缺失）

CLI::

    python3 gpu_probe.py --teacher_cache <teacher_cache.pt> \
        --representative_variant <baseline_model.py> \
        --variants_count <N> --device auto --safety 0.8 --max_concurrency 8 [--seed 0]

stdout::

    RESOLVED_DEVICE: cuda:0
    N_GPUS: 2
    FREE_VRAM_BYTES: 21873864704          # 全部卡 free 之和
    PER_VARIANT_VRAM_BYTES: 3221225472    # max_memory_allocated 峰值
    CONCURRENCY: 3
    DEVICE_PLAN: ["cuda:0","cuda:1","cuda:0"]
    GPU_REPORT: 2x GPU, 21.9GB free, ~3.2GB/variant -> concurrency=3 (safety 0.8)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from _device import is_npu_available, resolve_device  # noqa: E402


def _emit_fail_soft(reason: str, *, device_arg: str) -> int:
    """无 CUDA/NPU 或探测异常：concurrency=1 + WARN，exit 0（不阻塞 workflow）。"""
    resolved = "cpu"
    try:
        resolved = str(resolve_device(device_arg))
    except Exception:
        pass
    print("RESOLVED_DEVICE: " + resolved)
    print("N_GPUS: 0")
    print("FREE_VRAM_BYTES: 0")
    print("PER_VARIANT_VRAM_BYTES: 0")
    print("CONCURRENCY: 1")
    print('DEVICE_PLAN: [""]')
    print(f"GPU_REPORT: WARN {reason} -> serial fallback (concurrency=1)")
    return 0


def _load_variant_module(path: str) -> Any:
    """按路径 import representative variant .py（与 historical pick_variant._load_variant 同语义（pick_variant 删于 2026-08-04 cleanup §3））。"""
    p = os.path.abspath(path)
    if not os.path.isfile(p):
        raise FileNotFoundError(f"representative_variant 不存在: {p}")
    model_dir = os.path.dirname(p)
    if model_dir not in sys.path:
        sys.path.insert(0, model_dir)
    name = Path(p).stem
    spec = importlib.util.spec_from_file_location(name, p)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {p} 构造 import spec")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _build_representative(mod: Any, path: str):
    """build_model(**KNOBS.default) 或 build_model()（无 KNOBS）。"""
    factory = getattr(mod, "build_model", None)
    if not callable(factory):
        raise AttributeError(f"{path} 无 callable build_model（契约必备）")
    knobs = getattr(mod, "KNOBS", None)
    if isinstance(knobs, dict) and knobs:
        cfg = {k: kn["default"] for k, kn in knobs.items()
               if isinstance(kn, dict) and "default" in kn}
        return factory(**cfg)
    return factory()


def _dummy_input(mod: Any, path: str):
    """取 DUMMY_INPUT.shape（contract §1）；缺失则 fail loud。"""
    di = getattr(mod, "DUMMY_INPUT", None)
    if not isinstance(di, dict) or not isinstance(di.get("shape"), list) or not di["shape"]:
        raise ValueError(
            f"{path} DUMMY_INPUT 缺 shape（list）—— gpu_probe 需真实 I/O 维度造 dummy batch"
        )
    return di


def _probe_per_variant_vram(
    *, teacher_cache: str, rep_path: str, device_arg: str, seed: int,
) -> tuple[int, str, int, list[int]]:
    """探测 per-variant 训练显存（bytes）。

    Builds a dummy input batch (``torch.randn`` from the variant's ``DUMMY_INPUT.shape``)
    to run one fwd/bwd/step and read ``max_memory_allocated`` peak —— this is a dummy
    input for VRAM probing (smoke-style capacity probe), not a production data path.
    返回 (per_variant_bytes, resolved_device_str, n_gpus, free_per_card_bytes_list)。
    仅 CUDA / NPU 路径调得通；其它由 caller 走 fail-soft。
    """
    import torch

    torch.manual_seed(seed)

    mod = _load_variant_module(rep_path)
    model = _build_representative(mod, rep_path)
    dummy = _dummy_input(mod, rep_path)
    shape = list(dummy["shape"])
    # batch 维度保持 DUMMY_INPUT 给的（用户真实 batch=shape[0]）；不放大。
    x = torch.randn(*shape)

    if device_arg == "auto":
        if torch.cuda.is_available():
            backend = "cuda"
        elif is_npu_available():
            backend = "npu"
        else:
            raise RuntimeError("no CUDA/NPU available")
    elif device_arg in ("cuda", "npu"):
        backend = device_arg
    else:
        raise RuntimeError(f"device={device_arg!r} 无 VRAM 概念")

    # 多卡：device 0 做 per-variant 占用探测（单卡占用与卡无关，只取决于模型）。
    dev = torch.device(f"{backend}:0")
    model = model.to(dev)
    x = x.to(dev)

    # teacher_cache 加载到同一 device（每 worker 各自加载一份，必须计入占用）
    if not os.path.isfile(teacher_cache):
        raise FileNotFoundError(f"teacher_cache 不存在: {teacher_cache}")
    # weights_only 兼容：torch>=2.6 默认 True（拒载任意 pickle 对象如 TeacherCache），
    # 老版无此 kwarg。先试 weights_only=False（信任自家 production 文件），TypeError 回退。
    # teacher_cache.pt 是 teacher_setup.py 自家产物（trusted-internal trust boundary），
    # setup step 5 已校验可加载；此处若 load 失败 = 输入契约不符（损坏），fail loud 归入 ValueError
    # 让 _main 走 exit 2（与 missing-file 同政策）。
    try:
        try:
            cache = torch.load(teacher_cache, map_location=dev, weights_only=False)
        except TypeError:
            cache = torch.load(teacher_cache, map_location=dev)
    except Exception as e:
        raise ValueError(
            f"teacher_cache 损坏或格式不可加载：{type(e).__name__}: {e}（setup step 5 应保证可加载）"
        ) from e
    # cache 可能是 TeacherCache 对象 / state dict / tuple；若是 TeacherCache 则 .to(dev)
    if hasattr(cache, "to"):
        try:
            cache = cache.to(dev)
        except Exception:
            pass

    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)

    # reset peak + 跑一次 fwd/bwd/step（激活 + grad + Adam(m,v) 全计入）
    if backend == "cuda":
        torch.cuda.reset_peak_memory_stats(dev)
    else:  # npu
        try:
            torch.npu.reset_peak_memory_stats(dev)
        except Exception:
            pass

    model.train()
    y = model(x)
    # 简易损失：对齐 y 自身（探测目的不是收敛，是撑开显存）
    loss = (y.float() - y.detach().float()).pow(2).mean()
    loss.backward()
    optimizer.step()

    if backend == "cuda":
        per_variant = int(torch.cuda.max_memory_allocated(dev))
    else:
        try:
            per_variant = int(torch.npu.max_memory_allocated(dev))
        except Exception:
            per_variant = 0

    # n_gpus + 各卡 free
    if backend == "cuda":
        n_gpus = torch.cuda.device_count()
        free_per_card = [int(torch.cuda.mem_get_info(i)[0]) for i in range(max(n_gpus, 1))]
    else:  # npu
        try:
            n_gpus = torch.npu.device_count()
        except Exception:
            n_gpus = 1
        free_per_card = []
        for i in range(max(n_gpus, 1)):
            try:
                free_per_card.append(int(torch.npu.mem_get_info(i)[0]))
            except Exception:
                free_per_card.append(0)

    return per_variant, f"{backend}:0", max(n_gpus, 1), free_per_card


def compute_concurrency(
    *, total_free_bytes: int, per_variant_bytes: int, safety: float,
    variants_count: int, max_concurrency: int,
) -> int:
    """并发公式（确定性，纯函数，便于单测）::

        concurrency = max(1, floor(total_free * safety / per_variant))
        concurrency = min(concurrency, variants_count, max_concurrency)
    """
    if per_variant_bytes <= 0:
        return 1
    raw = int((total_free_bytes * safety) // per_variant_bytes)
    return max(1, min(raw, variants_count, max_concurrency))


def build_device_plan(concurrency: int, n_gpus: int, backend: str) -> list[str]:
    """round-robin device_plan：concurrency 个 worker 在 n_gpus 卡上交替绑卡（纯函数，便于单测）。"""
    if n_gpus <= 0:
        return [""] * concurrency
    return [f"{backend}:{i % n_gpus}" for i in range(concurrency)]


def _format_bytes(n: int) -> str:
    """人类可读 bytes（GB/MB）。诊断用。"""
    if n <= 0:
        return "0B"
    gb = n / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f}GB"
    mb = n / (1024 ** 2)
    return f"{mb:.0f}MB"


def _main() -> int:
    p = argparse.ArgumentParser(description="KD-NAS GPU 探测 + 并发判定（确定性，fail-soft）")
    p.add_argument("--teacher_cache", required=True, help="teacher_cache.pt（per-variant 占用必含）")
    p.add_argument("--representative_variant", required=True,
                   help="representative 变体 .py（通常 = baseline_model_path）")
    p.add_argument("--variants_count", required=True, type=int, help="KB 变体总数（cap 并发上限）")
    p.add_argument("--device", default="auto", help="auto / cuda / npu / cpu")
    p.add_argument("--safety", type=float, default=0.8, help="free VRAM 安全系数（默认 0.8）")
    p.add_argument("--max_concurrency", type=int, default=8, help="并发硬 cap（默认 8）")
    p.add_argument("--seed", type=int, default=0, help="复现种子（fwd/bwd 用）")
    args = p.parse_args()

    # 显式 cpu → 无 VRAM 概念，直接 fail-soft（不算错误）
    if args.device in ("cpu",):
        return _emit_fail_soft(f"device={args.device} (no VRAM concept)", device_arg=args.device)

    # 1) 先看硬件存不存在（torch import 失败 / 无 CUDA/NPU → fail-soft）
    try:
        import torch  # noqa: F401
    except Exception as e:  # torch 未装
        return _emit_fail_soft(f"torch import 失败：{type(e).__name__}: {e}", device_arg=args.device)

    has_cuda = False
    has_npu = False
    try:
        has_cuda = bool(torch.cuda.is_available())
    except Exception:
        pass
    try:
        has_npu = is_npu_available()
    except Exception:
        pass

    if args.device == "auto" and not has_cuda and not has_npu:
        return _emit_fail_soft("无 CUDA/NPU", device_arg=args.device)
    if args.device == "cuda" and not has_cuda:
        return _emit_fail_soft("device=cuda 但 CUDA 不可用", device_arg=args.device)
    if args.device == "npu" and not has_npu:
        return _emit_fail_soft("device=npu 但 NPU 不可用", device_arg=args.device)

    # 2) per-variant 占用探测 + free VRAM。探测异常 → fail-soft（不阻塞）。
    try:
        per_variant, resolved, n_gpus, free_per_card = _probe_per_variant_vram(
            teacher_cache=args.teacher_cache, rep_path=args.representative_variant,
            device_arg=args.device, seed=args.seed,
        )
    except FileNotFoundError as e:
        # 输入契约不符（teacher_cache 文件不存在 / variant 文件不存在）→ fail loud
        print(f"[gpu_probe] FAIL (input contract): {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    except (AttributeError, ValueError) as e:
        # variant 缺 build_model / DUMMY_INPUT.shape → fail loud（契约错误）
        print(f"[gpu_probe] FAIL (variant contract): {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        # 探测本身崩（OOM / teacher_cache 加载失败 / cudnn 错……）→ fail-soft
        print(f"[gpu_probe] WARN: 探测异常 -> fail-soft：{type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return _emit_fail_soft(f"probe error ({type(e).__name__})", device_arg=args.device)

    total_free = sum(free_per_card)
    if per_variant <= 0:
        # max_memory_allocated 测不到（某些 NPU 后端 / API 缺失）→ fail-soft 但**不静默估算**：
        # 用 ``per_variant = total_free // 4`` 估算驱动并发会破坏 fail-loud（昇腾部署相关），
        # 故走 cpu 同款 fail-soft：PER_VARIANT_VRAM_BYTES=0 + CONCURRENCY=1 + GPU_REPORT 标
        # ``[probe failed]``，不估算驱动并发。
        print(
            "[gpu_probe] WARN: per-variant VRAM 探测失败（max_memory_allocated 返 0 / NPU 后端"
            "不支持）→ fail-soft：PER_VARIANT_VRAM_BYTES=0 + CONCURRENCY=1，不估算驱动并发。",
            file=sys.stderr,
        )
        return _emit_fail_soft(
            "per-variant VRAM probe failed (max_memory_allocated unavailable on this backend)",
            device_arg=args.device,
        )

    concurrency = compute_concurrency(
        total_free_bytes=total_free, per_variant_bytes=per_variant,
        safety=args.safety, variants_count=max(args.variants_count, 1),
        max_concurrency=max(args.max_concurrency, 1),
    )
    backend = resolved.split(":")[0]
    device_plan = build_device_plan(concurrency, n_gpus, backend)

    print(f"RESOLVED_DEVICE: {resolved}")
    print(f"N_GPUS: {n_gpus}")
    print(f"FREE_VRAM_BYTES: {total_free}")
    print(f"PER_VARIANT_VRAM_BYTES: {per_variant}")
    print(f"CONCURRENCY: {concurrency}")
    print(f"DEVICE_PLAN: {json.dumps(device_plan)}")
    print(
        f"GPU_REPORT: {n_gpus}x {backend.upper()}, {_format_bytes(total_free)} free, "
        f"~{_format_bytes(per_variant)}/variant -> concurrency={concurrency} "
        f"(safety {args.safety})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())

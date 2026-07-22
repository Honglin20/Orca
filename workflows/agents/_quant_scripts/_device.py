"""_device.py —— quant workflow 共享的 device / seed 解析逻辑。

单一真相源（plan §P5 硬约束）。四个 quant 脚本（ptq-sweep / sensitivity / qat /
bit-curve）经 ``sys.path`` 注入本目录后 ``from _device import ...``，**避免 4 份复制**。

实现照搬 ``nas-agent/nas_agent/train/distributed.py`` 的 ``resolve_device`` +
``is_npu_available``（不引跨包依赖；nas-agent 不在 orca pyproject 依赖里）。

两套 device 语义显式区分：
- ``torch.device``：本模块 ``resolve_device`` 返回；用于 ``model.to(device)``、
  ``tensor.to(device)``、``torch.cuda.set_device``。
- onnxruntime provider：本模块**不**处理；struct/kd 的 latency_onnxrt.py 自管
  （Ascend/CANN/CUDAExecutionProvider）。
"""

from __future__ import annotations

import importlib.util
import random
import sys
from functools import lru_cache
from typing import Any

# torch 在脚本顶层 try-except import；本模块的兜底仅用于「torch 未装时 import 本模块
# 不炸」——脚本顶层已经 fail loud（exit 2），走不到这里。
try:
    import torch  # noqa: F401
    _TORCH_OK = True
except Exception:  # pragma: no cover - 脚本顶层先拦截
    _TORCH_OK = False


@lru_cache
def is_npu_available() -> bool:
    """``torch_npu`` 是否安装且 NPU 可用。

    与 ``nas_agent.train.distributed.is_npu_available`` 同实现：先 ``find_spec``
    （不触发 NPU runtime 初始化），再 ``torch.npu.is_available()``。
    """
    if importlib.util.find_spec("torch_npu") is None:
        return False
    if not _TORCH_OK:
        return False
    return hasattr(torch, "npu") and torch.npu.is_available()


def resolve_device(device_arg: str = "auto", local_rank: int = 0) -> "torch.device":
    """按 device_arg + 硬件可用性解析出 ``torch.device``。

    Args:
        device_arg: ``"auto"`` / ``"cuda"`` / ``"cuda:0"`` / ``"npu"`` / ``"cpu"``。
            空 / ``"auto"`` → 自动探测（cuda 优先，npu 次之，cpu 兜底）。
        local_rank: DDP local rank；仅当 device_arg 给裸 accelerator 名（无 index）时
            绑定 ``cuda:{local_rank}`` / ``npu:{local_rank}``。quant 单卡场景默认 0。

    Returns:
        ``torch.device``。调用方负责 ``model.to(device)`` + batch ``.to(device)``。

    Raises:
        ValueError: device_arg 显式给了非法值（如 ``"tpu"``）—— fail loud（不静默退 cpu）。
    """
    if not _TORCH_OK:
        raise RuntimeError("torch 未安装；脚本顶层应先 fail loud exit 2")

    arg = (device_arg or "auto").strip().lower()
    if arg and arg != "auto":
        # 显式给了——交给 torch.device 解析；非法值（如 "tpu"）会 raise，由 caller 处理
        parsed = torch.device(arg)
        # 仅给裸 accelerator 类型（"cuda" / "npu"）时按 local_rank 绑 index
        if parsed.type in ("cuda", "npu") and parsed.index is None:
            return torch.device(parsed.type, local_rank)
        return parsed

    # auto 探测：cuda 优先（含 mps？不，quant 量化的 GPU kernel 只走 cuda）→ npu → cpu
    if torch.cuda.is_available():
        return torch.device(f"cuda:{local_rank}")
    if is_npu_available():
        return torch.device(f"npu:{local_rank}")
    return torch.device("cpu")


def set_seed(seed: int) -> None:
    """固定 ``random`` / ``numpy``（若装） / ``torch``（含 cuda/npu）随机源。

    复现性底座：所有 quant workflow 的 ``--seed``（默认 0）经此函数贯穿。
    SDK 内部随机（SmoothQuant / GPTQ / AutoRound 等）默认用 torch 全局 RNG，
    故 ``torch.manual_seed`` 已覆盖。NumPy 路径（如候选 shuffle）额外固定。
    """
    random.seed(seed)
    try:
        import numpy as np  # noqa: F401
        np.random.seed(seed)
    except ImportError:
        pass
    if not _TORCH_OK:
        return
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    if is_npu_available():
        # 缩窄异常（code-reviewer 🟡）：只吞 AttributeError（老 torch_npu 可能无
        # manual_seed_all API）；其余（NPU runtime 未初始化 / OOM）应 stderr WARN 不静默。
        try:
            torch.npu.manual_seed_all(seed)
        except AttributeError as e:
            sys.stderr.write(
                f"[_device] WARN: torch.npu.manual_seed_all 不可用 ({e})；"
                f"NPU seed 未固定，复现性可能受影响。\n"
            )


def move_batch_to_device(batch: Any, device: "torch.device") -> Any:
    """把 forward_fn 收到的 batch 整体搬到 device（递归处理 Tensor / dict / tuple / list）。

    脚本用它**包装** adapter 的 ``forward_fn``——adapter 不需要懂 device，由脚本作为
    cross-cutting concern 统一处理（DRY：4 个脚本共用同一 wrapper 语义）。

    - ``torch.Tensor`` → ``tensor.to(device)``（已在目标 device 时 no-op，幂等）。
    - ``dict`` → 逐 value 递归。
    - ``tuple`` / ``list`` → 同序递归（保留容器类型）。
    - 其它（int / str / None）→ 原样返回。
    """
    if not _TORCH_OK:
        return batch
    if isinstance(batch, torch.Tensor):
        return batch.to(device)
    if isinstance(batch, dict):
        return {k: move_batch_to_device(v, device) for k, v in batch.items()}
    if isinstance(batch, tuple):
        return tuple(move_batch_to_device(v, device) for v in batch)
    if isinstance(batch, list):
        return [move_batch_to_device(v, device) for v in batch]
    return batch


def wrap_forward_with_device(raw_forward_fn: Any, device: "torch.device") -> Any:
    """返回一个把 batch 先搬到 device 再喂给 raw_forward_fn 的包装函数。

    adapter 的 ``forward_fn(module, batch) -> Tensor`` 不感知 device；脚本加载完
    ``fp_model.to(device)`` 后用本函数包一层，eval / 训练 loop / SDK 内部调用都自动
    走设备搬移。raw_forward_fn 为 None 时直接回 None（脚本上层会 fail loud）。
    """
    if raw_forward_fn is None:
        return None

    def _wrapped(module, batch):
        return raw_forward_fn(module, move_batch_to_device(batch, device))

    return _wrapped


def add_device_seed_args(parser: Any) -> None:
    """把 ``--device`` / ``--seed`` / ``--env_file`` 三参数加到 argparse parser。

    单一真相源（code-reviewer 🟡 DRY）：P5 引入的 device/seed/env_file argparse +
    resolve + set_seed 逻辑原本在 4 个 quant 脚本里各写一份（~15 行 × 4 = 60 行重复）。
    抽本 helper 后，脚本调用形如：

        ap = argparse.ArgumentParser()
        ap.add_argument("--adapter", required=True)
        # ... 其它 workflow-specific 参数
        add_device_seed_args(ap)
        args = ap.parse_args()
        _load_env_file(args.env_file)
        device, seed = resolve_device_and_seed(args.device, args.seed)

    env_file 由 ``_load_env_file``（各脚本既有，留给 P9 与其它 helpers 统一抽取）消费。
    """
    parser.add_argument(
        "--device",
        default="auto",
        help="目标硬件：auto / cuda / cuda:N / npu / cpu。auto→resolve_device 探测（cuda 优先，npu 次之）",
    )
    parser.add_argument(
        "--seed",
        default="0",
        help="随机种子（贯穿 torch / numpy / random；复现性底座）",
    )


def resolve_device_and_seed(
    device_arg: str, seed_arg: str, *, log_prefix: str = ""
) -> tuple["torch.device", int]:
    """解析 device + seed，set_seed 后返回 (device, seed)。失败 stderr + exit 2。

    抽出此 helper 是为了消除 4 个 quant 脚本里 device/seed 解析的重复 try/except 块。
    ``log_prefix`` 如 ``"[run_ptq_sweep] "`` 用于 stderr 消息前缀。
    """
    import sys

    try:
        seed = int(seed_arg or "0")
    except ValueError:
        sys.stderr.write(f"{log_prefix}--seed 非整数 '{seed_arg}'\n")
        sys.exit(2)
    try:
        device = resolve_device(device_arg)
    except Exception as e:
        sys.stderr.write(
            f"{log_prefix}--device='{device_arg}' 解析失败: {type(e).__name__}: {e}\n"
        )
        sys.exit(2)
    set_seed(seed)
    sys.stderr.write(f"{log_prefix}device={device} seed={seed}\n")
    return device, seed

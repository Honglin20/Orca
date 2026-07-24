"""_device.py —— device 解析（inline 自 NAS，不引跨包依赖）。

来源：nas-agent/nas_agent/train/distributed.py::resolve_device（is_npu_available 同源）。
逐字 inline 副本（P7 决策：struct/kd 抄 NAS 的 resolve_device，不引跨包依赖；本文件是 struct 侧
副本，kd 侧在 _kd_scripts/_device.py 同款）。

两套 device 语义，明确区分：
    - torch.device：训练 / ONNX 导出实例化用（resolve_device() 返回）。
    - onnxruntime providers：ONNX 推理时延测量用（ort_providers() 返回 provider 顺位列表）。

NPU = Ascend + CANN（torch_npu + onnxruntime CANNExecutionProvider）。
"""

from __future__ import annotations

import importlib
from functools import lru_cache
from typing import Any


@lru_cache
def is_npu_available() -> bool:
    """torch_npu 是否可用（Ascend NPU via CANN）。"""
    if importlib.util.find_spec("torch_npu") is None:
        return False
    import torch

    return hasattr(torch, "npu") and torch.npu.is_available()


def resolve_device(device_arg: str = "auto", local_rank: int = 0) -> Any:
    """torch.device 解析（NAS 同款）。

    Args:
        device_arg: "auto"（默认，cuda→npu→cpu 自动探测）/ "cuda" / "npu" / "cpu" /
                    或带 index 如 "cuda:1"。任何 torch.device 接受的串均可。
        local_rank: 多卡时绑定的 device index（bare "cuda"/"npu" 自动绑到 local_rank）。

    Returns:
        torch.device（cuda/npu/cpu，含 index）。
    """
    import torch

    if device_arg and device_arg != "auto":
        parsed = torch.device(device_arg)
        if parsed.index is None and parsed.type in ("cuda", "npu"):
            return torch.device(parsed.type, local_rank)
        return parsed
    if torch.cuda.is_available():
        return torch.device(f"cuda:{local_rank}")
    if is_npu_available():
        return torch.device(f"npu:{local_rank}")
    return torch.device("cpu")


def ort_providers(device_arg: str = "auto") -> list[str]:
    """onnxruntime InferenceSession 的 provider 顺位列表（按 device_arg 选）。

    与 ``resolve_device`` 独立（ORT 推理 provider，非 torch.device）。NPU=Ascend 走
    CANNExecutionProvider；CUDA 走 CUDAExecutionProvider；其余 CPUExecutionProvider。

    只返回本机 ``onnxruntime.get_available_providers()`` 实际可用的 provider，过滤后为空
    则退回 ``["CPUExecutionProvider"]``（fail-safe；caller 仍能跑）。
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
    else:  # "cpu" 或其他显式串
        wanted = ["CPUExecutionProvider"]

    picked = [p for p in wanted if p in avail]
    return picked or ["CPUExecutionProvider"]


def describe_device(device_arg: str = "auto") -> dict[str, Any]:
    """跑完一次探测，返回 {torch_device, ort_providers} 供脚本 stderr 打印（透明性）。"""
    torch_dev = resolve_device(device_arg)
    providers = ort_providers(device_arg)
    return {
        "device_arg": device_arg,
        "torch_device": str(torch_dev),
        "ort_providers": providers,
    }

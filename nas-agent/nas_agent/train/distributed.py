"""Distributed training utilities and environment setup."""

import os
import importlib
from functools import lru_cache
from contextlib import nullcontext
from typing import Any, Optional, Tuple, Union

import torch
import torch.distributed as dist
from torch import nn
from torch.nn.parallel import DistributedDataParallel

def tree_to_device(obj: Any, device: torch.device) -> Any:
    """Recursively move tensors in a nested structure to `device`.

    Handles `torch.Tensor`, `tuple`, `list`, and `dict` containers.
    Non-tensor leaves are returned unchanged.
    """
    if isinstance(obj, torch.Tensor):
        return obj.to(device)
    if isinstance(obj, tuple):
        return tuple(tree_to_device(item, device) for item in obj)
    if isinstance(obj, list):
        return [tree_to_device(item, device) for item in obj]
    if isinstance(obj, dict):
        return {k: tree_to_device(v, device) for k, v in obj.items()}
    return obj


def tree_detach_cpu(obj: Any) -> Any:
    """Recursively detach tensors in a nested structure and move to CPU.

    Handles `torch.Tensor`, `tuple`, `list`, and `dict` containers.
    Non-tensor leaves are returned unchanged.
    """
    if isinstance(obj, torch.Tensor):
        return obj.detach().cpu()
    if isinstance(obj, tuple):
        return tuple(tree_detach_cpu(item) for item in obj)
    if isinstance(obj, list):
        return [tree_detach_cpu(item) for item in obj]
    if isinstance(obj, dict):
        return {k: tree_detach_cpu(v) for k, v in obj.items()}
    return obj


def is_distributed() -> bool:
    """Checks if the distributed environment is available and initialized.

    Returns:
        bool: True if distributed is available and initialized, False otherwise.
    """
    return dist.is_available() and dist.is_initialized()


def get_rank() -> int:
    """Gets the rank of the current process in the distributed group.

    Returns:
        int: The rank of the current process, or 0 if not distributed.
    """
    return dist.get_rank() if is_distributed() else 0


def get_world_size() -> int:
    """Gets the total number of processes in the distributed group.

    Returns:
        int: The world size, or 1 if not distributed.
    """
    return dist.get_world_size() if is_distributed() else 1


def get_device_count() -> int:
    """Gets the total number of available accelerator devices (CUDA or NPU).

    Returns:
        int: The device count.
    """
    return torch.accelerator.device_count()


def get_local_rank() -> int:
    """Gets the local rank of the current process from environment variables.

    Returns:
        int: The local rank (default 0 if not set).
    """
    return int(os.environ.get("LOCAL_RANK", "0"))


def is_main_process() -> bool:
    """Checks if the current process is the main process (rank 0).

    Returns:
        bool: True if the current process is rank 0, False otherwise.
    """
    return get_rank() == 0


def setup_distributed(device_arg: str = "auto") -> torch.device:
    """Sets up the distributed environment and resolves the target device.

    Args:
        device_arg (str, optional): The device identifier string. Defaults to "auto".

    Returns:
        torch.device: The resolved local device for the current process.
    """
    local_rank = get_local_rank()
    device = resolve_device(device_arg=device_arg, local_rank=local_rank)
    set_device(device)
    if "RANK" in os.environ:
        dist.init_process_group(backend=distributed_backend(device))
    return device


def cleanup_distributed() -> None:
    """Cleans up the distributed process group if initialized."""
    if is_distributed():
        dist.destroy_process_group()


def barrier() -> None:
    """Synchronizes all processes in the distributed group."""
    if is_distributed():
        dist.barrier()


def unwrap_model(model: nn.Module) -> nn.Module:
    """Unwraps a model from DistributedDataParallel if wrapped.

    Args:
        model (nn.Module): The potentially wrapped PyTorch model.

    Returns:
        nn.Module: The unwrapped core model.
    """
    return model.module if isinstance(model, DistributedDataParallel) else model


def set_sample_config_ddp(model: nn.Module, arch_config: Any) -> None:
    """Sets the sampled architecture configuration on a (potentially wrapped) model.

    Args:
        model (nn.Module): The model to configure.
        arch_config (Any): The architecture configuration to apply.
    """
    unwrap_model(model).set_sample_config(arch_config)



@lru_cache
def is_npu_available() -> bool:
    """Checks if `torch_npu` is installed and an NPU is available.

    Returns:
        bool: True if NPU is available, False otherwise.
    """
    if importlib.util.find_spec("torch_npu") is None:
        return False
    return hasattr(torch, "npu") and torch.npu.is_available()



def _get_visibility_env_var() -> str | None:
    """Return the env-var name that controls accelerator device visibility.

    Uses compile-time / install-time checks that do NOT trigger runtime
    initialisation, so it is safe to call before any device operation.
    """
    # Check NPU first: torch_npu is an optional package.
    if importlib.util.find_spec('torch_npu') is not None:
        return 'ASCEND_RT_VISIBLE_DEVICES'
    # torch.version.cuda is a compile-time constant (e.g. '12.1') that
    # does not trigger CUDA runtime init.
    if torch.version.cuda is not None:
        return 'CUDA_VISIBLE_DEVICES'
    return None


def isolate_device(device_index: int) -> None:
    """Restrict this process to a single accelerator device.

    Sets the platform-appropriate visibility environment variable
    (`CUDA_VISIBLE_DEVICES` or `ASCEND_RT_VISIBLE_DEVICES`) so that
    the process sees only one device — which then appears as device 0.

    Must be called before any accelerator runtime initialisation
    (i.e. before `set_device`, `get_device_count`, or any tensor
    operation on the accelerator).

    If the visibility env-var is already set (e.g. by a launcher),
    the function indexes into the existing device list rather than
    using raw physical indices.

    Args:
        device_index: Logical device index to isolate. After this call
            the device will appear as device 0 to the process.
    """
    env_var = _get_visibility_env_var()
    if env_var is None:
        return

    current = os.environ.get(env_var)
    if current is not None:
        devices = current.split(',')
        os.environ[env_var] = devices[device_index]
    else:
        os.environ[env_var] = str(device_index)


def resolve_device(device_arg: str = "auto", local_rank: int = 0) -> torch.device:
    """Resolves the correct device based on arguments and availability.

    Args:
        device_arg (str, optional): Requested device string. Defaults to "auto".
        local_rank (int, optional): The local rank for binding the device index. Defaults to 0.

    Returns:
        torch.device: The resolved device.
    """
    if device_arg and device_arg != "auto":
        parsed = torch.device(device_arg)
        # When only an accelerator type is given without an explicit index
        # (e.g. "cuda", "npu"), bind it to local_rank so each torchrun
        # process targets its own device.
        if parsed.index is None and parsed.type in ("cuda", "npu"):
            return torch.device(parsed.type, local_rank)
        return parsed
    if torch.cuda.is_available():
        return torch.device(f"cuda:{local_rank}")
    if is_npu_available():
        return torch.device(f"npu:{local_rank}")
    return torch.device("cpu")


def distributed_backend(device: Optional[torch.device] = None) -> str:
    """Determines the appropriate distributed backend for the given device.

    Args:
        device (Optional[torch.device], optional): The target device. Defaults to None.

    Returns:
        str: The name of the distributed backend ("nccl", "hccl", or "gloo").
    """
    if device is not None:
        if device.type == "cuda":
            return "nccl"
        if device.type == "npu":
            return "hccl"
        return "gloo"
    if torch.cuda.is_available():
        return "nccl"
    if is_npu_available():
        return "hccl"
    return "gloo"


def set_device(device_or_index: Union[torch.device, int]) -> None:
    """Sets the current active device for the process.

    Args:
        device_or_index (Union[torch.device, int]): The device or device index to set.
        
    Raises:
        RuntimeError: If attempting to set an NPU device but NPU is unavailable.
    """
    if isinstance(device_or_index, int):
        if torch.cuda.is_available():
            torch.cuda.set_device(device_or_index)
        elif is_npu_available():
            torch.npu.set_device(device_or_index)
        return

    if device_or_index.type == "cuda":
        torch.cuda.set_device(device_or_index)
    elif device_or_index.type == "npu":
        if not is_npu_available():
            raise RuntimeError("torch.npu is not available")
        torch.npu.set_device(device_or_index)


def torch_manual_seed(seed: int) -> None:
    """Sets the random seed for PyTorch CPU, CUDA, and NPU random number generators.

    Args:
        seed (int): The seed value to use.
    """
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
    if is_npu_available():
        torch.npu.manual_seed(seed)


def synchronize(device: torch.device) -> None:
    """Synchronizes operations on the given device.

    Args:
        device (torch.device): The device to synchronize.
        
    Raises:
        RuntimeError: If synchronizing an NPU device but NPU is unavailable.
    """
    if device.type == "cuda":
        torch.cuda.synchronize(device)
    elif device.type == "npu":
        if not is_npu_available():
            raise RuntimeError("torch.npu is not available")
        torch.npu.synchronize()


def empty_cache(device: torch.device) -> None:
    """Empties the memory cache for the given device type.

    Args:
        device (torch.device): The device for which to empty the cache.
    """
    if device.type == "cuda":
        torch.cuda.empty_cache()
    elif device.type == "npu" and is_npu_available():
        torch.npu.empty_cache()


def make_events(device: torch.device) -> Tuple[Any, Any]:
    """Creates a pair of timing events for the specified device.

    Args:
        device (torch.device): The device to create events for.

    Returns:
        Tuple[Any, Any]: A tuple containing two event objects (start, end).
        
    Raises:
        RuntimeError: If the device does not support timing events or is unavailable.
    """
    if device.type == "cuda":
        return torch.cuda.Event(enable_timing=True), torch.cuda.Event(
            enable_timing=True
        )
    if device.type == "npu":
        if not is_npu_available():
            raise RuntimeError("torch.npu is not available")
        return torch.npu.Event(enable_timing=True), torch.npu.Event(enable_timing=True)
    raise RuntimeError(f"{device.type} does not support device-side event timing")


def get_device_name(device: torch.device) -> str:
    """Gets the name of the specified device.

    Args:
        device (torch.device): The device to query.

    Returns:
        str: The name of the device (e.g., "CPU", GPU/NPU model name).
    """
    if device.type == "cuda":
        return torch.cuda.get_device_name(device)
    if device.type == "npu" and is_npu_available():
        index = 0 if device.index is None else device.index
        return torch.npu.get_device_name(index)
    return "CPU"


def autocast(
    device: torch.device,
    enabled: bool,
    dtype: Optional[torch.dtype] = None,
) -> Any:
    """Creates a context manager for automatic mixed precision (AMP).

    Args:
        device (torch.device): The device type.
        enabled (bool): Whether autocasting should be enabled.
        dtype (Optional[torch.dtype], optional): The target data type for autocasting. Defaults to None.

    Returns:
        Any: An autocast context manager, or a nullcontext if disabled or on CPU.
    """
    if not enabled or device.type == "cpu":
        return nullcontext()
    if device.type == "npu" and is_npu_available():
        amp = importlib.import_module("torch_npu.npu.amp")
        # Since GradScaler is disabled for NPU, we must use bfloat16 
        # which shares the float32 dynamic range and avoids underflow.
        npu_dtype = torch.bfloat16
        try:
            return amp.autocast(dtype=npu_dtype)
        except TypeError:
            # Fallback if older torch_npu doesn't accept dtype
            return amp.autocast()
    return torch.amp.autocast(device_type=device.type, dtype=dtype, enabled=True)


def grad_scaler(device: torch.device, enabled: bool) -> Any:
    """Creates a gradient scaler for mixed precision training.

    Args:
        device (torch.device): The device the scaler is for.
        enabled (bool): Whether the gradient scaler should be enabled.

    Returns:
        Any: A PyTorch GradScaler instance.
    """
    enabled = enabled and (
        device.type == "cuda" or (device.type == "npu" and is_npu_available())
    )
    if device.type == "npu":
        # NPU GradScaler is unusable in supernet. We rely on bfloat16 
        # autocast instead, which natively avoids gradient underflow and 
        # does not require a GradScaler.
        return torch.amp.GradScaler("cpu", enabled=False)

    scaler_device = device.type if device.type == "cuda" else "cpu"
    return torch.amp.GradScaler(scaler_device, enabled=enabled)

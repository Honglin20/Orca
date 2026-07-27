"""Checkpointing utilities for model training."""

import os
from typing import Any, Dict, Optional

import torch
from torch import nn

_STANDARD_CHECKPOINT_KEYS = frozenset({
    "args", "best_metric", "epoch", "global_step",
    "model", "optimizer", "scheduler", "scaler",
})



def load_checkpoint(
    path: str,
    model: nn.Module,
    device: torch.device,
    *,
    optimizer: Optional[torch.optim.Optimizer] = None,
    scheduler: Optional[Any] = None,
    scaler: Optional[Any] = None,
    strict: bool = True,
) -> Dict[str, Any]:
    """Loads a model checkpoint from a file.

    Args:
        path (str): The file path to the checkpoint.
        model (nn.Module): The PyTorch model to load weights into.
        device (torch.device): The device to map the loaded tensors to.
        optimizer: The optimizer to load state for.
        scheduler: The LR scheduler to load state for.
        scaler: The gradient scaler to load state for.
        strict: Whether to strictly enforce that the keys in state_dict match.

    Returns:
        Dict[str, Any]: A dictionary containing:
            - `best_metric`: The best metric value, or None if not saved.
            - `extra`: Non-standard checkpoint entries saved via
              `save_checkpoint`'s `extra` parameter.
            - `global_step`: The global step counter.
            - `missing_keys`: Keys in state_dict missing from the model.
            - `start_epoch`: The epoch to resume from.
            - `unexpected_keys`: Keys in the model not found in state_dict.
    """
    checkpoint = torch.load(path, map_location=device)
    state_dict = (
        checkpoint["model"]
        if isinstance(checkpoint, dict) and "model" in checkpoint
        else checkpoint
    )
    missing_keys, unexpected_keys = model.load_state_dict(state_dict, strict=strict)

    start_epoch: int = 0
    global_step: int = 0
    best_metric: float | None = None
    extra: Dict[str, Any] = {}

    if isinstance(checkpoint, dict):
        if optimizer is not None and "optimizer" in checkpoint:
            optimizer.load_state_dict(checkpoint["optimizer"])
        if scheduler is not None and "scheduler" in checkpoint:
            scheduler.load_state_dict(checkpoint["scheduler"])
        if scaler is not None and checkpoint.get("scaler") is not None:
            scaler.load_state_dict(checkpoint["scaler"])
        start_epoch = checkpoint.get("epoch", -1) + 1
        global_step = checkpoint.get("global_step", 0)
        best_metric = checkpoint.get("best_metric", None)
        extra = {
            k: v for k, v in checkpoint.items()
            if k not in _STANDARD_CHECKPOINT_KEYS
        }

    return {
        "best_metric": best_metric,
        "extra": extra,
        "global_step": global_step,
        "missing_keys": missing_keys,
        "start_epoch": start_epoch,
        "unexpected_keys": unexpected_keys,
    }


def save_checkpoint(
    path: str,
    model: nn.Module,
    *,
    optimizer: Optional[torch.optim.Optimizer] = None,
    scheduler: Optional[Any] = None,
    scaler: Optional[Any] = None,
    epoch: Optional[int] = None,
    global_step: Optional[int] = None,
    best_metric: Optional[float] = None,
    args: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Saves a model checkpoint to a file.

    Args:
        path (str): The file path where the checkpoint will be saved.
        model (nn.Module): The PyTorch model to save.
        optimizer: The optimizer to save state for.
        scheduler: The LR scheduler to save state for.
        scaler: The gradient scaler to save state for.
        epoch: The current training epoch. Omitted from checkpoint when None.
        global_step: The current global training step. Omitted from checkpoint when None.
        best_metric: The best evaluation metric achieved so far. Omitted from checkpoint when None.
        args: Additional arguments or configuration to save.
        extra: Extra arbitrary data to save in the checkpoint.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    checkpoint: Dict[str, Any] = {"model": model.state_dict()}
    if args is not None:
        checkpoint["args"] = args
    if epoch is not None:
        checkpoint["epoch"] = epoch
    if global_step is not None:
        checkpoint["global_step"] = global_step
    if best_metric is not None:
        checkpoint["best_metric"] = best_metric
    if optimizer is not None:
        checkpoint["optimizer"] = optimizer.state_dict()
    if scheduler is not None:
        checkpoint["scheduler"] = scheduler.state_dict()
    if scaler is not None:
        checkpoint["scaler"] = scaler.state_dict() if scaler.is_enabled() else None
    if extra:
        checkpoint.update(extra)
    torch.save(checkpoint, path)


def save_checkpoint_ddp(
    path: str,
    model: nn.Module,
    **kwargs,
) -> None:
    """DDP-aware checkpoint save: unwraps DDP, gates on rank 0, barriers.

    Automatically unwraps `DistributedDataParallel` to produce clean
    state-dict keys (without ``module.`` prefix).  In distributed mode,
    only the main process (rank 0) writes the file; other ranks wait at
    a barrier until the write completes.

    In non-distributed mode this is equivalent to calling `save_checkpoint`
    directly.

    Args:
        path: File path where the checkpoint will be saved.
        model: The PyTorch model to save (may be DDP-wrapped).
        **kwargs: Forwarded to `save_checkpoint` (optimizer, scaler,
            epoch, global_step, best_metric, args, extra).
    """
    from nas_agent.train.distributed import barrier, is_distributed, is_main_process
    from torch.nn.parallel import DistributedDataParallel

    raw_model = model.module if isinstance(model, DistributedDataParallel) else model

    if is_main_process():
        save_checkpoint(path, raw_model, **kwargs)

    if is_distributed():
        barrier()

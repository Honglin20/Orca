"""Lightweight direct PyTorch latency helpers."""

import gc
import time
from typing import Any

import torch
import torch.nn as nn

from nas_agent.blocks.choice_layer import ChoiceLayer
from nas_agent.train import empty_cache, make_events, synchronize, tree_to_device


def trace_choice_layer_inputs(
    model: nn.Module,
    dummy_input: Any,
    dummy_kwargs: dict[str, Any] | None = None,
) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    """Run one forward pass and capture each `ChoiceLayer` input.

    Registers a forward-pre-hook on every `ChoiceLayer` in `model`, runs
    a single no-grad forward with `dummy_input`, and returns the captured
    inputs so the caller knows the real tensor shapes flowing into each
    `ChoiceLayer`.

    Args:
        model: The supernet (must already have `set_sample_config` applied
            and be on the target device).
        dummy_input: The model-level input (tensor or tuple of tensors),
            already on the same device as `model`.
        dummy_kwargs: The model-level keyword arguments.

    Returns:
        Ordered list of `(module_name, input_args, input_kwargs)` tuples, one per
        `ChoiceLayer` execution in forward order.
    """
    if dummy_kwargs is None:
        dummy_kwargs = {}

    traces: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    handles: list[torch.utils.hooks.RemovableHandle] = []

    def _make_hook(name: str):
        def hook(
            _module: nn.Module,
            input_args: tuple[Any, ...],
            input_kwargs: dict[str, Any],
        ) -> None:
            traces.append((name, input_args, input_kwargs))

        return hook

    for name, module in model.named_modules():
        if isinstance(module, ChoiceLayer):
            handles.append(
                module.register_forward_pre_hook(_make_hook(name), with_kwargs=True)
            )

    try:
        model.eval()
        with torch.no_grad():
            if isinstance(dummy_input, torch.Tensor):
                model(dummy_input, **dummy_kwargs)
            elif isinstance(dummy_input, (tuple, list)):
                model(*dummy_input, **dummy_kwargs)
            else:
                model(dummy_input, **dummy_kwargs)
    finally:
        for handle in handles:
            handle.remove()

    return traces


@torch.inference_mode()
def measure_module_latency(
    module: nn.Module,
    input_args: Any,
    device: torch.device,
    input_kwargs: dict[str, Any] | None = None,
    repetitions: int = 20,
    warmup: int = 20,
    verbose: bool = False,
) -> float:
    """Measure the inference latency of a module.

    Args:
        module: The module to measure.
        input_args: The input arguments to the module (tensor or tuple).
        device: The device to perform measurement on.
        input_kwargs: The input keyword arguments to the module.
        repetitions: Number of measurement runs.
        warmup: Number of warmup runs prior to measurement.
        verbose: Whether to print detailed latency statistics.

    Returns:
        The median single-inference latency in milliseconds.
    """
    if input_kwargs is None:
        input_kwargs = {}

    module.eval().to(device)
    input_args = tree_to_device(input_args, device)
    input_kwargs = tree_to_device(input_kwargs, device)
    if not isinstance(input_args, (tuple, list)):
        input_args = (input_args,)

    empty_cache(device)

    events = []
    if device.type in {"cuda", "npu"}:
        # Pre-allocate events to keep allocation out of the timed loop.
        # Record back-to-back and sync once at the end to avoid host stalls.
        events = [make_events(device) for _ in range(repetitions)]

    gc.collect()
    gc.disable()
    try:
        if device.type in {"cuda", "npu"}:
            for _ in range(warmup):
                module(*input_args, **input_kwargs)
            synchronize(device)
            for starter, ender in events:
                starter.record()
                module(*input_args, **input_kwargs)
                ender.record()
            synchronize(device)
            times = [starter.elapsed_time(ender) for starter, ender in events]
        else:
            for _ in range(warmup):
                module(*input_args, **input_kwargs)
            times = []
            for _ in range(repetitions):
                t0 = time.perf_counter()
                module(*input_args, **input_kwargs)
                times.append((time.perf_counter() - t0) * 1000.0)
    finally:
        gc.enable()

    times.sort()
    n = len(times)
    median = times[n // 2]
    p90 = times[min(int(n * 0.90), n - 1)]
    p99 = times[min(int(n * 0.99), n - 1)]
    mean = sum(times) / n
    std = (sum((t - mean) ** 2 for t in times) / n) ** 0.5
    
    if verbose:
        print(
            f"  [latency] median={median:.3f}  p90={p90:.3f}  p99={p99:.3f}  "
            f"mean={mean:.3f}  std={std:.3f} (ms)"
        )
    return median

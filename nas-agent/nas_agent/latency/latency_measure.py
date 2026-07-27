"""ONNX-based latency measurement for CPU, GPU, and NPU backends."""

from pathlib import Path
from typing import Any

import torch
import torch.nn as nn

from .latency_npu import ACL_MEM_MALLOC_HUGE_FIRST, export_and_measure_om_latency
from .latency_ort import export_and_measure_ort_latency
from .latency_utils import LatencyStats

# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------

_DEVICE_TYPE_TO_PROVIDERS: dict[str, list[str]] = {
    "cpu": ["CPUExecutionProvider"],
    "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
}


def export_and_measure_latency(
    module: nn.Module,
    model_args: torch.Tensor | tuple[Any, ...],
    device: str | torch.device,
    *,
    model_kwargs: dict[str, Any] | None = None,
    work_dir: str | Path = "runs/latency",
    model_name: str = "model",
    opset_version: int | None = None,
    warmup: int = 10,
    repetitions: int = 100,
    # --- NPU-specific (ignored for cpu/cuda) ---
    export_on_cpu: bool = True,
    soc_version: str = "Ascend910B1",
    input_format: str = "NCHW",
    input_shape_str: str | None = None,
    mem_malloc_policy: int = ACL_MEM_MALLOC_HUGE_FIRST,
    extra_atc_args: list[str] | None = None,
    # --- ORT-specific (ignored for npu) ---
    use_io_binding: bool | None = None,
) -> LatencyStats:
    """Unified latency measurement dispatching to ORT (cpu/cuda) or OM (npu).

    Args:
        module: PyTorch model to benchmark.
        model_args: Positional forward inputs — `Tensor` or `tuple`
            of tensors, passed directly to `torch.onnx.export` as
            `args`.
        device: Target device for latency measurement, e.g.
            `"cpu"`, `"cuda:0"`, `"npu:1"`, or a `torch.device`
            instance.  The device type selects the backend (ORT for
            cpu/cuda, ATC/pyACL for npu) and the index selects the
            device ordinal.
        model_kwargs: Keyword forward inputs.  `None` if the model's
            `forward` takes only positional arguments.  Requires
            PyTorch ≥ 2.1.
        work_dir: Directory for intermediate artefacts.
        model_name: Base name used for artefact files.
        opset_version: ONNX opset version.  `None` defers to the chosen
            backend (13 for NPU/ATC; torch's bundled default for ORT).
        warmup: Warm-up passes before timing.
        repetitions: Timed passes.
        export_on_cpu: (NPU) Whether to force PyTorch tracing on CPU.
        soc_version: (NPU) Ascend SoC version passed to ATC.
        input_format: (NPU) ATC `--input_format` value.
        input_shape_str: (NPU) ATC `--input_shape`; inferred if omitted.
        mem_malloc_policy: (NPU) ACL device-memory policy.
        extra_atc_args: (NPU) Additional raw ATC CLI arguments.
        use_io_binding: (ORT) Override IOBinding auto-selection.

    Returns:
        `LatencyStats` for the chosen backend.

    Raises:
        ValueError: If the device type is not `"cpu"`, `"cuda"`,
            or `"npu"`.
    """
    device = torch.device(device)
    device_type = device.type

    if device_type in _DEVICE_TYPE_TO_PROVIDERS:
        return export_and_measure_ort_latency(
            module, model_args,
            model_kwargs=model_kwargs,
            work_dir=work_dir,
            model_name=model_name,
            providers=_DEVICE_TYPE_TO_PROVIDERS[device_type],
            opset_version=opset_version,
            warmup=warmup,
            repetitions=repetitions,
            use_io_binding=use_io_binding,
            device=device,
        )
    if device_type == "npu":
        return export_and_measure_om_latency(
            module, model_args,
            model_kwargs=model_kwargs,
            work_dir=work_dir,
            model_name=model_name,
            input_shape_str=input_shape_str,
            soc_version=soc_version,
            input_format=input_format,
            device=device,
            opset_version=opset_version,
            warmup=warmup,
            repetitions=repetitions,
            mem_malloc_policy=mem_malloc_policy,
            extra_atc_args=extra_atc_args,
            export_on_cpu=export_on_cpu,
        )
    raise ValueError(
        f"Unsupported device type: {device_type!r}. "
        f"Use 'cpu', 'cuda', or 'npu'."
    )

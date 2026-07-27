"""Shared utilities: latency statistics container and ONNX export helper."""

import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn as nn


# ---------------------------------------------------------------------------
# Latency statistics
# ---------------------------------------------------------------------------

@dataclass
class LatencyStats:
    """Per-iteration latency samples plus summary statistics.

    Construct with the raw per-iteration timings (milliseconds).  Summary
    statistics are computed lazily as properties so the dataclass stays
    cheap to copy / pickle.
    """

    raw_ms: list[float] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.raw_ms)

    @property
    def mean_ms(self) -> float:
        return statistics.fmean(self.raw_ms) if self.raw_ms else float("nan")

    @property
    def median_ms(self) -> float:
        return statistics.median(self.raw_ms) if self.raw_ms else float("nan")

    @property
    def std_ms(self) -> float:
        return statistics.stdev(self.raw_ms) if len(self.raw_ms) > 1 else 0.0

    @property
    def min_ms(self) -> float:
        return min(self.raw_ms) if self.raw_ms else float("nan")

    @property
    def max_ms(self) -> float:
        return max(self.raw_ms) if self.raw_ms else float("nan")

    @property
    def p50_ms(self) -> float:
        return float(np.percentile(self.raw_ms, 50)) if self.raw_ms else float("nan")

    @property
    def p90_ms(self) -> float:
        return float(np.percentile(self.raw_ms, 90)) if self.raw_ms else float("nan")

    @property
    def p99_ms(self) -> float:
        return float(np.percentile(self.raw_ms, 99)) if self.raw_ms else float("nan")

    def __repr__(self) -> str:
        if not self.raw_ms:
            return "LatencyStats(n=0)"
        return (
            f"LatencyStats(n={self.n}, "
            f"mean={self.mean_ms:.3f}ms, "
            f"std={self.std_ms:.3f}ms)"
        )

    def __str__(self) -> str:
        if not self.raw_ms:
            return "LatencyStats(empty)"
        return (
            f"LatencyStats(n={self.n}, "
            f"mean={self.mean_ms:.3f}ms, "
            f"median={self.median_ms:.3f}ms, "
            f"p90={self.p90_ms:.3f}ms, "
            f"p99={self.p99_ms:.3f}ms, "
            f"std={self.std_ms:.3f}ms, "
            f"min={self.min_ms:.3f}ms, "
            f"max={self.max_ms:.3f}ms)"
        )


# ---------------------------------------------------------------------------
# Device helpers
# ---------------------------------------------------------------------------

def get_device_index(device: str | torch.device) -> int:
    """Extract the integer device index from a device specification.

    Returns 0 for devices without an explicit index (e.g. `"cpu"`,
    `"npu"`).
    """
    return torch.device(device).index or 0


# ---------------------------------------------------------------------------
# Common ONNX export
# ---------------------------------------------------------------------------

def torch_onnx_export(
    module: nn.Module,
    model_args: torch.Tensor | tuple[Any, ...],
    onnx_path: Path,
    *,
    opset_version: int,
    dynamo: bool = True,
    model_kwargs: dict[str, Any] | None = None,
    input_names: list[str] | None = None,
    output_names: list[str] | None = None,
) -> Path:
    """Core ONNX export logic shared by CPU/GPU and NPU backends.

    The module must already be in eval mode and on the desired device
    before calling this function; the same goes for the tensors in
    `model_args` / `model_kwargs`.

    Args:
        module: The PyTorch model (must be in eval mode).
        model_args: Positional forward inputs — passed directly to
            `torch.onnx.export` as `args`.
        onnx_path: Destination `.onnx` file path.
        opset_version: ONNX opset version (must be resolved beforehand).
        dynamo: If `True`, use the Dynamo-based exporter which supports
            more operators (e.g. `aten::fft_rfft2` via ONNX `DFT` op
            at opset ≥ 20).  If `False` (default), use the legacy
            TorchScript tracer.
        model_kwargs: Keyword forward inputs.  `None` if the model's
            `forward` takes only positional arguments.
        input_names: ONNX input node names.  `None` → `["input"]`.
        output_names: ONNX output node names.  `None` → `["output"]`.

    Returns:
        Resolved path to the written `.onnx` file.
    """
    onnx_path.parent.mkdir(parents=True, exist_ok=True)

    if input_names is None:
        input_names = ["input"]
    if output_names is None:
        output_names = ["output"]

    # ── Build export kwargs ─────────────────────────────────────────────
    export_kwargs: dict[str, Any] = dict(
        opset_version=opset_version,
        input_names=input_names,
        output_names=output_names,
        do_constant_folding=True,
        dynamo=dynamo,
    )

    if model_kwargs:
        export_kwargs["kwargs"] = model_kwargs

    with torch.no_grad():
        torch.onnx.export(module, model_args, str(onnx_path), **export_kwargs)

    return onnx_path

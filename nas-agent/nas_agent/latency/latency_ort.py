"""ONNX Runtime (CPU/GPU) measurement backend."""

import time
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
import torch
import torch.nn as nn

from .latency_utils import LatencyStats, get_device_index, torch_onnx_export

# ---------------------------------------------------------------------------

# ONNX Runtime type-string → numpy dtype.  Bfloat16 has no numpy
# equivalent and is left to raise explicitly rather than silently miscast.
_ORT_TYPE_TO_NUMPY: dict[str, np.dtype] = {
    "tensor(float)":   np.dtype(np.float32),
    "tensor(float16)": np.dtype(np.float16),
    "tensor(double)":  np.dtype(np.float64),
    "tensor(int8)":    np.dtype(np.int8),
    "tensor(int16)":   np.dtype(np.int16),
    "tensor(int32)":   np.dtype(np.int32),
    "tensor(int64)":   np.dtype(np.int64),
    "tensor(uint8)":   np.dtype(np.uint8),
    "tensor(uint16)":  np.dtype(np.uint16),
    "tensor(uint32)":  np.dtype(np.uint32),
    "tensor(uint64)":  np.dtype(np.uint64),
    "tensor(bool)":    np.dtype(np.bool_),
}

# Providers whose tensors live on a GPU and benefit from IOBinding.
_GPU_PROVIDER_TO_DEVICE: dict[str, str] = {
    "CUDAExecutionProvider":     "cuda",
    "ROCMExecutionProvider":     "cuda",   # ORT uses "cuda" for ROCm too
    "TensorrtExecutionProvider": "cuda",
}


def _ort_dtype(type_str: str) -> np.dtype:
    if type_str not in _ORT_TYPE_TO_NUMPY:
        raise ValueError(
            f"Unsupported ONNX Runtime input dtype {type_str!r}; "
            f"supported: {sorted(_ORT_TYPE_TO_NUMPY)}"
        )
    return _ORT_TYPE_TO_NUMPY[type_str]


def measure_ort_latency(
    onnx_path: str | Path,
    input_shapes: Sequence[Sequence[int]],
    *,
    providers: list[str] | None = None,
    warmup: int = 10,
    repetitions: int = 100,
    use_io_binding: bool | None = None,
    device: str | torch.device = "cpu",
) -> LatencyStats:
    """Measure inference latency of an ONNX model via ONNX Runtime.

    Differences from a naive `session.run` loop:

    * **Per-iteration timings** via `perf_counter_ns`; returns a full
      `LatencyStats` distribution, not just a mean.
    * **Graph optimisations** explicitly enabled
      (`GraphOptimizationLevel.ORT_ENABLE_ALL`).
    * **Input dtypes** read from the session metadata so the feed dict
      always matches what the graph expects.
    * **IOBinding** with device-resident tensors for GPU providers —
      `OrtValue`s are allocated on device once before the timed loop,
      eliminating H2D / D2H copies from the timed window.
      `synchronize_outputs()` is called inside the timed region for
      GPU EPs to guarantee the GPU work has actually completed before
      the timer stops.

    Args:
        onnx_path: Path to the `.onnx` model file.
        input_shapes: List of shapes, one per model input.  Any axes that
            the ONNX file marks as dynamic must be filled in here.
        providers: ONNX Runtime execution providers, e.g.
            `["CUDAExecutionProvider", "CPUExecutionProvider"]`.
            Defaults to `["CPUExecutionProvider"]`.
        warmup: Number of warm-up passes before timing starts.
        repetitions: Number of timed passes.
        use_io_binding: Whether to bind inputs/outputs to device memory.
            `None` (default) auto-enables for GPU EPs and disables for
            CPU.  Override to force one or the other.
        device: Target device, e.g. `"cpu"` or `"cuda:1"`.  The
            device index is used for GPU IOBinding.

    Returns:
        `LatencyStats` with per-iteration end-to-end timings.

    Raises:
        ImportError: If `onnxruntime` is not installed.
        RuntimeError: If the primary execution provider is not available.
    """
    try:
        import onnxruntime as ort
    except ImportError as e:
        raise ImportError(
            "onnxruntime is not installed.  Install with:\n"
            "  CPU:  pip install onnxruntime\n"
            "  GPU:  pip install onnxruntime-gpu"
        ) from e

    if providers is None:
        providers = ["CPUExecutionProvider"]

    # Validate that the primary provider is actually available so we
    # don't silently fall back to CPU when GPU was requested.
    available = set(ort.get_available_providers())
    if providers[0] not in available:
        raise RuntimeError(
            f"Primary ONNX Runtime provider {providers[0]!r} is not "
            f"available.  Installed providers: {sorted(available)}"
        )

    # Enable all graph optimisations for representative latency.
    sess_options = ort.SessionOptions()
    sess_options.graph_optimization_level = (
        ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    )

    session = ort.InferenceSession(
        str(onnx_path), sess_options=sess_options, providers=providers,
    )

    sess_inputs = session.get_inputs()
    if len(input_shapes) != len(sess_inputs):
        raise ValueError(
            f"input_shapes has {len(input_shapes)} entries but the ONNX "
            f"graph declares {len(sess_inputs)} inputs: "
            f"{[i.name for i in sess_inputs]}"
        )

    primary = providers[0]
    is_gpu = primary in _GPU_PROVIDER_TO_DEVICE
    if use_io_binding is None:
        use_io_binding = is_gpu

    run_one: Callable[[], None]

    if use_io_binding:
        device_type = _GPU_PROVIDER_TO_DEVICE.get(primary, "cpu")
        dev_idx = get_device_index(device) if is_gpu else 0

        io_binding = session.io_binding()

        # Bind inputs as device-resident OrtValues so the H2D copy
        # happens exactly once, before the timed loop starts.
        for inp, shape in zip(sess_inputs, input_shapes):
            arr = np.zeros(tuple(shape), dtype=_ort_dtype(inp.type))
            ortvalue = ort.OrtValue.ortvalue_from_numpy(
                arr, device_type, dev_idx,
            )
            io_binding.bind_ortvalue_input(inp.name, ortvalue)

        # Outputs are bound by name + device; ORT allocates of the
        # correct shape on first run and reuses the allocation after.
        for out in session.get_outputs():
            io_binding.bind_output(out.name, device_type, dev_idx)

        if is_gpu:
            def run_one() -> None:
                session.run_with_iobinding(io_binding)
                # Ensure all GPU work has completed before stopping
                # the timer.  No-op when the EP is already synchronous,
                # cheap when it isn't.
                io_binding.synchronize_outputs()
        else:
            def run_one() -> None:
                session.run_with_iobinding(io_binding)
    else:
        feed: dict[str, np.ndarray] = {}
        for inp, shape in zip(sess_inputs, input_shapes):
            feed[inp.name] = np.zeros(
                tuple(shape), dtype=_ort_dtype(inp.type),
            )

        def run_one() -> None:
            session.run(None, feed)

    # ── Warmup ──────────────────────────────────────────────────────────
    for _ in range(warmup):
        run_one()

    # ── Timed loop ──────────────────────────────────────────────────────
    latencies_ms: list[float] = []
    for _ in range(repetitions):
        t0 = time.perf_counter_ns()
        run_one()
        t1 = time.perf_counter_ns()
        latencies_ms.append((t1 - t0) / 1e6)

    return LatencyStats(raw_ms=latencies_ms)


# ---------------------------------------------------------------------------
# Opset resolution
# ---------------------------------------------------------------------------

def _resolve_opset(opset_version: int | None) -> int:
    """Pick a sensible ONNX opset for the CPU/GPU target backend.

    Prefers the opset bundled into the installed torch
    (`torch.onnx._constants.ONNX_DEFAULT_OPSET`), which is the version
    `torch.onnx` is built and regression-tested against. Falls back to 17
    if that constant is unavailable.
    """
    if opset_version is not None:
        return int(opset_version)
    try:
        from torch.onnx._constants import ONNX_DEFAULT_OPSET  # type: ignore[attr-defined]
        return int(ONNX_DEFAULT_OPSET)
    except Exception:
        return 17


# ---------------------------------------------------------------------------
# Step 1 — PyTorch  →  ONNX
# ---------------------------------------------------------------------------

def export_to_onnx(
    module: nn.Module,
    model_args: torch.Tensor | tuple[Any, ...],
    onnx_path: str | Path,
    *,
    model_kwargs: dict[str, Any] | None = None,
    opset_version: int | None = None,
    input_names: list[str] | None = None,
    output_names: list[str] | None = None,
) -> Path:
    """Export `module` to ONNX for CPU/GPU backends.

    Forces export on CPU for maximum stability and consistency with NPU exports.

    Args:
        module: The PyTorch model to export (set to eval mode in place).
        model_args: Positional forward inputs — passed directly to
            `torch.onnx.export` as `args`.
        onnx_path: Destination `.onnx` file path.
        model_kwargs: Keyword forward inputs.  `None` if the model's
            `forward` takes only positional arguments.
        opset_version: ONNX opset version.  `None` defers to torch's
            bundled default.
        input_names: ONNX input node names.  `None` → `["input"]`.
        output_names: ONNX output node names.  `None` → `["output"]`.

    Returns:
        Resolved path to the written `.onnx` file.
    """
    from nas_agent.train import tree_detach_cpu

    module = module.eval().cpu()
    model_args = tree_detach_cpu(model_args)
    if model_kwargs:
        model_kwargs = tree_detach_cpu(model_kwargs)

    return torch_onnx_export(
        module, model_args, Path(onnx_path),
        opset_version=_resolve_opset(opset_version),
        dynamo=True,
        model_kwargs=model_kwargs,
        input_names=input_names,
        output_names=output_names,
    )


# ---------------------------------------------------------------------------
# Convenience: full pipeline
# ---------------------------------------------------------------------------

def export_and_measure_ort_latency(
    module: nn.Module,
    model_args: torch.Tensor | tuple[Any, ...],
    *,
    model_kwargs: dict[str, Any] | None = None,
    work_dir: str | Path = "runs/latency",
    model_name: str = "model",
    providers: list[str] | None = None,
    opset_version: int | None = None,
    warmup: int = 10,
    repetitions: int = 100,
    use_io_binding: bool | None = None,
    device: str | torch.device = "cpu",
) -> LatencyStats:
    """Full pipeline: PyTorch → ONNX → ORT latency.

    Args:
        module: PyTorch model to benchmark.
        model_args: Positional forward inputs (`Tensor` or `tuple`).
        model_kwargs: Keyword forward inputs (`None` if positional-only).
        work_dir: Directory for the intermediate `.onnx` artefact.
        model_name: Base name for the artefact file.
        providers: ONNX Runtime execution providers.  Defaults to
            `["CPUExecutionProvider"]`.
        opset_version: ONNX opset version.  `None` → torch's bundled
            default.
        warmup: Warm-up passes before timing.
        repetitions: Timed passes.
        use_io_binding: Whether to bind tensors to device memory.  `None`
            auto-enables for GPU EPs and disables for CPU.
        device: Target device, e.g. `"cpu"` or `"cuda:1"`.  The
            device index is used for GPU IOBinding.

    Returns:
        `LatencyStats` for `session.run` /
        `session.run_with_iobinding`.
    """
    work_dir = Path(work_dir)
    onnx_path = work_dir / f"{model_name}.onnx"

    # --- export ---
    export_to_onnx(
        module, model_args, onnx_path,
        model_kwargs=model_kwargs,
        opset_version=opset_version,
    )

    # --- collect input shapes from the exported ONNX graph ---
    # Read shapes from the ONNX file itself rather than guessing from
    # Python-side inputs.  This is always correct because the graph may
    # have folded / reshaped inputs during export.
    try:
        import onnx
        onnx_model = onnx.load(str(onnx_path))
        shapes: list[list[int]] = []
        for inp in onnx_model.graph.input:
            dims = [
                d.dim_value
                for d in inp.type.tensor_type.shape.dim
            ]
            shapes.append(dims)
    except ImportError:
        # Fallback: extract shapes from model_args directly.
        if isinstance(model_args, torch.Tensor):
            shapes = [list(model_args.shape)]
        else:
            shapes = [
                list(t.shape) for t in model_args
                if isinstance(t, torch.Tensor)
            ]

    # --- measure ---
    return measure_ort_latency(
        onnx_path, shapes,
        providers=providers,
        warmup=warmup,
        repetitions=repetitions,
        use_io_binding=use_io_binding,
        device=device,
    )

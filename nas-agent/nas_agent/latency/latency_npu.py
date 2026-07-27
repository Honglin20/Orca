"""NPU (Ascend OM / pyACL) measurement backend."""

import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn as nn

from .latency_utils import LatencyStats, get_device_index, torch_onnx_export

# ---------------------------------------------------------------------------
# Step 1 — PyTorch  →  ONNX (NPU specific)
# ---------------------------------------------------------------------------

def export_to_onnx_npu(
    module: torch.nn.Module,
    model_args: torch.Tensor | tuple[Any, ...],
    onnx_path: str | Path,
    *,
    model_kwargs: dict[str, Any] | None = None,
    opset_version: int | None = None,
    input_names: list[str] | None = None,
    output_names: list[str] | None = None,
    export_on_cpu: bool = True,
) -> Path:
    """Export `module` to ONNX for the NPU (ATC) backend.

    Forces `dynamo=False` because ATC shape-inference requires legacy
    graph structures.  Supports `export_on_cpu=False` so that
    `torch_npu` custom operators are available during tracing.

    Args:
        module: The PyTorch model to export (set to eval mode in place).
        model_args: Positional forward inputs — passed directly to
            `torch.onnx.export` as `args`.
        onnx_path: Destination `.onnx` file path.
        model_kwargs: Keyword forward inputs.  `None` if the model's
            `forward` takes only positional arguments.
        opset_version: ONNX opset version.  `None` defaults to 13
            (the ATC-recommended opset).
        input_names: ONNX input node names.  `None` → `["input"]`.
        output_names: ONNX output node names.  `None` → `["output"]`.
        export_on_cpu: If `True` (default), move module and inputs to
            CPU before tracing.  Set to `False` when tracing must run
            on NPU (e.g. models containing `torch_npu` custom ops).

    Returns:
        Resolved path to the written `.onnx` file.
    """
    from nas_agent.train import tree_detach_cpu, tree_to_device

    onnx_path = Path(onnx_path)
    opset_version = int(opset_version) if opset_version is not None else 13
    module = module.eval()

    if export_on_cpu:
        module = module.cpu()
        model_args = tree_detach_cpu(model_args)
        if model_kwargs:
            model_kwargs = tree_detach_cpu(model_kwargs)
    else:
        device = next(module.parameters(), torch.tensor(0)).device
        model_args = tree_to_device(model_args, device)
        if model_kwargs:
            model_kwargs = tree_to_device(model_kwargs, device)
        try:
            import torch_npu.onnx  # noqa: F401
        except ImportError:
            pass

    return torch_onnx_export(
        module, model_args, onnx_path,
        opset_version=opset_version,
        dynamo=False,  # ATC requires legacy TorchScript graph structure
        model_kwargs=model_kwargs,
        input_names=input_names,
        output_names=output_names,
    )


# ---------------------------------------------------------------------------
# Step 2 — ONNX  →  OM  (via ATC)
# ---------------------------------------------------------------------------

def build_input_shape_str(
    model_args: torch.Tensor | tuple[Any, ...],
    *,
    input_names: list[str] | None = None,
) -> str:
    """Build an ATC `--input_shape` string from model positional args.

    Useful when the ONNX model has dynamic dimensions and ATC requires
    an explicit `--input_shape`.  For fixed-shape models, ATC infers
    shapes from the graph and this function is unnecessary.

    Args:
        model_args: The same positional inputs passed to
            `torch.onnx.export`.  Only `torch.Tensor` values are
            considered; non-tensor args are silently skipped.
        input_names: Names for each tensor input.  `None` defaults to
            `["input"]` for a single tensor, or `["input_0",
            "input_1", ...]` for multiple.

    Returns:
        ATC-formatted string, e.g. `"input:1,3,224,224"` or
        `"input_0:1,3,224,224;input_1:1,10"`.

    Raises:
        ValueError: If no `torch.Tensor` is found in `model_args`.

    Example::

        x = torch.randn(1, 3, 224, 224)
        build_input_shape_str(x)
        # => "input:1,3,224,224"

        build_input_shape_str((x, torch.randn(1, 10)))
        # => "input_0:1,3,224,224;input_1:1,10"
    """
    if isinstance(model_args, torch.Tensor):
        tensors = [model_args]
    else:
        tensors = [t for t in model_args if isinstance(t, torch.Tensor)]

    if not tensors:
        raise ValueError(
            "No torch.Tensor found in model_args.  Cannot build "
            "--input_shape string."
        )

    if input_names is None:
        if len(tensors) == 1:
            input_names = ["input"]
        else:
            input_names = [f"input_{i}" for i in range(len(tensors))]

    if len(input_names) != len(tensors):
        raise ValueError(
            f"input_names has {len(input_names)} entries but found "
            f"{len(tensors)} tensor inputs."
        )

    return ";".join(
        f"{name}:{','.join(str(d) for d in t.shape)}"
        for name, t in zip(input_names, tensors)
    )


def convert_onnx_to_om(
    onnx_path: str | Path,
    om_output_path: str | Path,
    *,
    input_shape_str: str | None = None,
    soc_version: str = "Ascend910B1",
    input_format: str = "NCHW",
    extra_atc_args: list[str] | None = None,
) -> Path:
    """Convert an ONNX model to an Ascend OM model using the ATC tool.

    The ATC binary must be on `PATH` (installed with CANN).

    Args:
        onnx_path: Source `.onnx` file.
        om_output_path: Output path.  The `.om` extension is appended by
            ATC; pass a path *without* the suffix (e.g. `"runs/model"`).
        input_shape_str: ATC `--input_shape` value, e.g.
            `"input:1,3,224,224"` or
            `"input_0:1,3,224,224;input_1:1,10"`.
            Optional for fixed-shape ONNX models — ATC infers shapes
            from the graph.  Required when the ONNX model contains
            dynamic dimensions.  See `build_input_shape_str` for a
            helper that generates this from `model_args`.
        soc_version: Target SoC, e.g. `"Ascend910B1"` or `"Ascend310P3"`.
        input_format: Input tensor layout, typically `"NCHW"` or `"ND"`.
        extra_atc_args: Additional raw ATC arguments appended verbatim.

    Returns:
        Path to the generated `.om` file (`<om_output_path>.om`).

    Raises:
        FileNotFoundError: If `onnx_path` does not exist.
        RuntimeError: If ATC returns a non-zero exit code.  The exception
            message includes the full ATC command, stdout, and stderr so
            the failure is debuggable even when the parent process
            captures or hides subprocess output.
    """
    onnx_path = Path(onnx_path)
    if not onnx_path.exists():
        raise FileNotFoundError(
            f"ONNX model not found: {onnx_path}. "
            f"Export the model first before calling convert_onnx_to_om."
        )

    om_output_path = Path(om_output_path)
    om_output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "atc",
        "--model", str(onnx_path),
        "--framework", "5",          # 5 = ONNX
        "--output", str(om_output_path),
        "--input_format", input_format,
        "--soc_version", soc_version,
        "--export_compile_stat", "0",  # suppress fusion_result.json
    ]
    if input_shape_str is not None:
        cmd.extend(["--input_shape", input_shape_str])
    if extra_atc_args:
        cmd.extend(extra_atc_args)

    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ATC failed (exit {result.returncode}) for command:\n"
            f"  {' '.join(cmd)}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
    return Path(str(om_output_path) + ".om")


# ---------------------------------------------------------------------------
# Step 3a — OM latency measurement via pyACL
# ---------------------------------------------------------------------------

# ACL memory-malloc policies (see acl/acl_rt.h):
ACL_MEM_MALLOC_HUGE_FIRST: int = 0   # try huge pages first, fallback (default)
ACL_MEM_MALLOC_HUGE_ONLY: int = 1
ACL_MEM_MALLOC_NORMAL_ONLY: int = 2

ACL_MEMCPY_HOST_TO_DEVICE: int = 1

# Event flag that explicitly enables timing-capable events.  Kept for
# completeness — measurement uses `perf_counter_ns` so this is currently
# unused, but the constant is exported for callers that want events
# elsewhere.
ACL_EVENT_TIME_LINE: int = 0x00000008

# Returned by `acl.init` / `acl.rt.set_device` when the resource has
# already been acquired in this process (e.g. by torch_npu).
_ACL_REPEAT_INITIALIZE: int = 100002


def _check_acl(ret: Any, op: str, allow: tuple[int, ...] = (0,)) -> Any:
    """Validate a pyACL return value and (if any) hand back the payload.

    pyACL functions either return a plain status integer, or a
    `(value, code)` / `(value0, value1, code)` tuple where `code` is
    always the last element.  This helper raises on any code outside
    `allow` and returns the unwrapped payload (or `None` if the
    function returned only a status code).
    """
    if isinstance(ret, tuple):
        *values, code = ret
    else:
        values, code = (), ret
    if code not in allow:
        raise RuntimeError(f"ACL call '{op}' failed with error code {code}")
    if not isinstance(ret, tuple):
        return None
    return values[0] if len(values) == 1 else tuple(values)


class _AclState:
    """Process-wide ACL initialisation guard.

    Encapsulates the mutable state that was previously tracked via module-
    level `global` variables.  `acl.init` and `acl.rt.set_device`
    are process-global and refuse to be called twice — they return
    `ACL_ERROR_REPEAT_INITIALIZE` (100002) on the second invocation.
    We treat that code as success so `ensure_initialized` can be invoked
    from any number of measurement passes and works whether or not
    `torch_npu` has already initialised ACL.

    We deliberately do *not* call `acl.rt.reset_device` or
    `acl.finalize` anywhere: both are process-global teardowns that
    would destroy state owned by `torch_npu` or other ACL clients.
    Process exit handles the cleanup.
    """

    _initialized: bool = False
    _devices: set[int] = set()

    @classmethod
    def ensure_initialized(cls, device_id: int) -> None:
        """Idempotent ACL init + set_device that coexists with torch_npu."""
        import acl  # type: ignore[import]

        if not cls._initialized:
            _check_acl(
                acl.init(), "acl.init",
                allow=(0, _ACL_REPEAT_INITIALIZE),
            )
            cls._initialized = True

        if device_id not in cls._devices:
            _check_acl(
                acl.rt.set_device(device_id),
                f"acl.rt.set_device({device_id})",
                allow=(0, _ACL_REPEAT_INITIALIZE),
            )
            cls._devices.add(device_id)


def measure_om_latency(
    om_path: str | Path,
    *,
    device: str | torch.device = "npu:0",
    warmup: int = 10,
    repetitions: int = 100,
    mem_malloc_policy: int = ACL_MEM_MALLOC_HUGE_FIRST,
) -> LatencyStats:
    """Measure inference latency of an Ascend OM model.

    Input shapes, dtypes and byte sizes are read from the OM model
    description itself (`aclmdlGetInputSizeByIndex`); the function fills
    host-side zero buffers of the exact expected byte size, copies them
    to the device once, then times only the
    `aclmdlExecuteAsync + synchronize_stream` window for each iteration.

    `acl.init` and `acl.rt.set_device` are issued at most once per
    process via `_AclState.ensure_initialized`, so this function is
    safe to call repeatedly and safe to call from a process that also
    uses `torch_npu`.  Per-call resources (context, stream, model
    handle, device buffers) are created and released around each
    measurement via `try`/`finally`.

    Args:
        om_path: Path to the compiled `.om` model file.
        device: Target NPU device, e.g. `"npu:0"` or `"npu:1"`.
        warmup: Number of warm-up inference passes before timing starts.
        repetitions: Number of timed passes.
        mem_malloc_policy: ACL device-memory policy:
            `ACL_MEM_MALLOC_HUGE_FIRST` (0, default) — try huge pages
            first, fall back to normal pages.  Best for large inputs.
            `ACL_MEM_MALLOC_HUGE_ONLY` (1) — huge pages only.
            `ACL_MEM_MALLOC_NORMAL_ONLY` (2) — normal pages only.
            Use on memory-constrained edge devices.

    Returns:
        `LatencyStats` with per-iteration wall-clock timings around
        `aclmdlExecuteAsync` + stream synchronisation.

    Raises:
        ImportError: If `acl` (pyACL) is not installed.
        RuntimeError: If any ACL call returns a non-zero, non-tolerated code.
    """
    try:
        import acl  # type: ignore[import]
    except ImportError as e:
        raise ImportError(
            "pyACL is not installed.  Install CANN and its Python bindings "
            "before calling measure_om_latency."
        ) from e

    om_path = str(om_path)
    device_id = get_device_index(device)
    _AclState.ensure_initialized(device_id)

    # Resources to clean up; assigned as we go so the finally block
    # always sees a coherent view even on partial setup failure.
    context = None
    stream = None
    model_id: int | None = None
    model_desc = None
    input_dataset = None
    output_dataset = None
    device_input_ptrs: list[int] = []
    device_output_ptrs: list[int] = []
    input_buffers: list[Any] = []
    output_buffers: list[Any] = []

    try:
        context = _check_acl(acl.rt.create_context(device_id),
                             "acl.rt.create_context")

        # ── Load model and read its input/output spec ───────────────────
        model_id = _check_acl(acl.mdl.load_from_file(om_path),
                              "acl.mdl.load_from_file")
        model_desc = acl.mdl.create_desc()
        _check_acl(acl.mdl.get_desc(model_desc, model_id),
                   "acl.mdl.get_desc")

        # ── Allocate input buffers sized from the model itself ─────────
        # We do NOT trust user-supplied shapes/dtypes: ATC may have
        # changed them via --input_fp16_nodes / --insert_op_conf etc.
        input_dataset = acl.mdl.create_dataset()
        num_inputs = acl.mdl.get_num_inputs(model_desc)
        for i in range(num_inputs):
            nbytes = acl.mdl.get_input_size_by_index(model_desc, i)
            # Raw zero-bytes host buffer; the kernel doesn't care what
            # the data looks like for latency measurement.
            host_array = np.zeros(nbytes, dtype=np.uint8)

            dev_ptr = _check_acl(acl.rt.malloc(nbytes, mem_malloc_policy),
                                 f"acl.rt.malloc (input {i})")
            device_input_ptrs.append(dev_ptr)

            _check_acl(
                acl.rt.memcpy(dev_ptr, nbytes, host_array.ctypes.data,
                              nbytes, ACL_MEMCPY_HOST_TO_DEVICE),
                f"acl.rt.memcpy H2D (input {i})",
            )

            buf = acl.create_data_buffer(dev_ptr, nbytes)
            input_buffers.append(buf)
            _check_acl(acl.mdl.add_dataset_buffer(input_dataset, buf),
                       f"acl.mdl.add_dataset_buffer (input {i})")

        # ── Allocate output buffers ────────────────────────────────────
        output_dataset = acl.mdl.create_dataset()
        num_outputs = acl.mdl.get_num_outputs(model_desc)
        for i in range(num_outputs):
            out_size = acl.mdl.get_output_size_by_index(model_desc, i)
            dev_ptr = _check_acl(acl.rt.malloc(out_size, mem_malloc_policy),
                                 f"acl.rt.malloc (output {i})")
            device_output_ptrs.append(dev_ptr)

            buf = acl.create_data_buffer(dev_ptr, out_size)
            output_buffers.append(buf)
            _check_acl(acl.mdl.add_dataset_buffer(output_dataset, buf),
                       f"acl.mdl.add_dataset_buffer (output {i})")

        # ── Stream ──────────────────────────────────────────────────────
        stream = _check_acl(acl.rt.create_stream(), "acl.rt.create_stream")

        # ── Warmup ──────────────────────────────────────────────────────
        for _ in range(warmup):
            _check_acl(
                acl.mdl.execute_async(model_id, input_dataset,
                                      output_dataset, stream),
                "acl.mdl.execute_async (warmup)",
            )
            _check_acl(acl.rt.synchronize_stream(stream),
                       "acl.rt.synchronize_stream (warmup)")

        # ── Timed loop (per-iteration wall clock) ──────────────────────
        # We use perf_counter_ns around execute_async + synchronize_stream
        # instead of aclrtEventElapsedTime: it's within microseconds of
        # the pure-NPU number for any non-trivial kernel and lets us
        # report a full latency distribution rather than just totals.
        latencies_ms: list[float] = []
        for _ in range(repetitions):
            t0 = time.perf_counter_ns()
            _check_acl(
                acl.mdl.execute_async(model_id, input_dataset,
                                      output_dataset, stream),
                "acl.mdl.execute_async",
            )
            _check_acl(acl.rt.synchronize_stream(stream),
                       "acl.rt.synchronize_stream")
            t1 = time.perf_counter_ns()
            latencies_ms.append((t1 - t0) / 1e6)

        return LatencyStats(raw_ms=latencies_ms)

    finally:
        # Resource cleanup; tolerate partial setup.
        if stream is not None:
            acl.rt.destroy_stream(stream)
        for buf in input_buffers:
            acl.destroy_data_buffer(buf)
        if input_dataset is not None:
            acl.mdl.destroy_dataset(input_dataset)
        for ptr in device_input_ptrs:
            acl.rt.free(ptr)
        for buf in output_buffers:
            acl.destroy_data_buffer(buf)
        if output_dataset is not None:
            acl.mdl.destroy_dataset(output_dataset)
        for ptr in device_output_ptrs:
            acl.rt.free(ptr)
        if model_desc is not None:
            acl.mdl.destroy_desc(model_desc)
        if model_id is not None:
            acl.mdl.unload(model_id)
        if context is not None:
            acl.rt.destroy_context(context)


def export_and_measure_om_latency(
    module: nn.Module,
    model_args: torch.Tensor | tuple[Any, ...],
    *,
    model_kwargs: dict[str, Any] | None = None,
    work_dir: str | Path = "runs/latency",
    model_name: str = "model",
    input_shape_str: str | None = None,
    soc_version: str = "Ascend910B1",
    input_format: str = "NCHW",
    device: str | torch.device = "npu:0",
    opset_version: int | None = None,
    warmup: int = 10,
    repetitions: int = 100,
    mem_malloc_policy: int = ACL_MEM_MALLOC_HUGE_FIRST,
    extra_atc_args: list[str] | None = None,
    export_on_cpu: bool = True,
) -> LatencyStats:
    """Full pipeline: PyTorch → ONNX → OM → NPU latency.

    Args:
        module: Trained PyTorch model to benchmark.
        model_args: Positional forward inputs (`Tensor` or `tuple`).
            batch_size=1 recommended for deployment latency.
        model_kwargs: Keyword forward inputs (`None` if positional-only).
        work_dir: Directory for intermediate artefacts (`.onnx`, `.om`).
        model_name: Base name used for artefact files.
        input_shape_str: ATC `--input_shape` string, e.g.
            `"input:1,3,224,224"`.  `None` (default) lets ATC
            infer shapes from the ONNX graph, which works for
            fixed-shape models.  Required only when the ONNX model
            contains dynamic dimensions.
        soc_version: Ascend SoC version passed to ATC.
        input_format: ATC `--input_format` value.
        device: Target NPU device, e.g. `"npu:0"` or `"npu:1"`.
        opset_version: ONNX opset version.  `None` → 13 (ATC default).
        warmup: Warm-up passes before timing.
        repetitions: Timed passes.
        mem_malloc_policy: ACL device-memory policy (default HUGE_FIRST).
        extra_atc_args: Additional raw ATC CLI arguments.
        export_on_cpu: Whether to force PyTorch tracing on CPU.

    Returns:
        `LatencyStats` for the `aclmdlExecuteAsync + sync` window.
    """
    work_dir = Path(work_dir)
    onnx_path = work_dir / f"{model_name}.onnx"
    om_base = work_dir / model_name

    # --- export ---
    export_to_onnx_npu(
        module, model_args, onnx_path,
        model_kwargs=model_kwargs,
        opset_version=opset_version,
        export_on_cpu=export_on_cpu,
    )

    # --- ATC conversion ---
    # For fixed-shape ONNX models (the common case), ATC reads shapes
    # directly from the graph — no --input_shape needed.  Users can
    # still pass input_shape_str for dynamic-dimension models.
    om_path = convert_onnx_to_om(
        onnx_path, om_base,
        input_shape_str=input_shape_str,
        soc_version=soc_version,
        input_format=input_format,
        extra_atc_args=extra_atc_args,
    )

    # --- measure ---
    return measure_om_latency(
        om_path,
        device=device,
        warmup=warmup,
        repetitions=repetitions,
        mem_malloc_policy=mem_malloc_policy,
    )

# Latency Estimator Generation Workflow

Use this workflow after the train script has been generated to create `latency_estimator.py`. The estimator measures whole-architecture latency on-the-fly using PyTorch directly. It supports CPU, CUDA, and NPU devices via PyTorch's native device abstraction. All generated artifacts are written under `<output_dir>`.

## Read And Inspect

Read the generated example:

- `references/supernet_workflow_examples/latency_estimator.py`

Inspect the following to adapt the example to the concrete `<output_dir>/supernet.py`:

- `SearchSpace`, `ArchConfig`, `SuperNet`, and `set_sample_config()` / `get_active_subnet()` APIs.
- The supernet's forward signature and validated dummy input shapes from the generated training script and the original project under `<user_project_root>`.

## Handling Non-Searchable Model Logic

Non-Searchable Model Logic (iterative/fixed-point loops, `self.training` branches, gradient boundaries, runtime weight manipulation, solution/state initialization) destabilizes latency measurement when it is **dynamic**: its control flow or iteration count depends on the random dummy input, a threshold, or `self.training`. Inspect the exported subnet class (returned by `get_active_subnet()`) for this before writing `get_latency()`. If present, pin it to a fixed, deterministic path for the duration of measurement only; never alter the exported subnet class's real semantics in `supernet.py`. Examples:

- A convergence loop (e.g. DEQ / OAMP fixed-point refinement) with a data-dependent iteration count: measure only a single loop iteration instead of running the full loop, so the measured cost is one architecture-dependent round, decoupled from data-dependent convergence behavior.
- An input-dependent conditional branch (e.g. a confidence-based early exit, or a threshold check on an intermediate activation) that selects between different code paths: force it to always take the same branch (typically the full/deepest path), so the measured cost does not depend on which values the random dummy input happens to produce.

Wrap the frozen path in a small callable (e.g. a nested function inside `get_latency()` that closes over the subnet) and pass it to `measure_module_latency` instead of `subnet` (it accepts either an `nn.Module` or a plain callable). `measure_module_latency` only moves a plain `nn.Module` to `device` and calls `.eval()` automatically, so the callable must already close over a subnet that is on `device` and in eval mode. Skip this step entirely when the supernet has no such dynamic logic, as in the base example.

## Generate `latency_estimator.py`

Adapt `references/supernet_workflow_examples/latency_estimator.py`; it already shows the constructor, whole-architecture measurement, and the `get_latency()` interface. Keep the generated script concrete to `supernet.py`.

- Import the generated supernet as a plain sibling import: `from supernet import ArchConfig, SearchSpace, SuperNet`.
- Import `measure_module_latency` from `nas_agent.latency.pytorch_latency_utils`.
- Import `empty_cache` from `nas_agent.train`.
- **Constructor** `LatencyEstimator(search_space, latency_cfg, device)`:
  - Store `latency_cfg`, `device` (`torch.device`), and create `SuperNet` once on CPU.
- **`get_latency(arch_config)`**:
  - Call `self.supernet.set_sample_config(arch_config)`, extract the active subnet via `self.supernet.get_active_subnet()`.
  - Apply the freeze step from "Handling Non-Searchable Model Logic" above when applicable.
  - Construct a dummy input matching the subnet's forward signature. Use `latency_cfg.batch_size` for the batch dimension and **hardcode** the remaining dimensions (channels, spatial size, sequence length, etc.) to match the concrete supernet. For single-tensor input models, use `torch.randn(batch_size, ...)`. For models with multi-arg or kwargs forward signatures, construct the corresponding inputs.
  - Call `measure_module_latency(subnet, dummy_input, device=self.device, ...)` passing `warmup` and `repetitions` from `self.latency_cfg`. The function returns the median latency in milliseconds directly. Pass the frozen callable instead of `subnet` when a freeze step was applied above.
  - After measurement, free device memory: `del subnet; empty_cache(self.device)`.
- **Model Naming**: Set `model_name=f"subnet_{arch_hash}"` where `arch_hash` is a 16-character hex hash of `repr(arch_config)` (e.g., using `hashlib.sha1`). This is reserved for future ONNX-based latency measurements that require unique filenames.

### CLI Smoke Test (Testing and Usage Demonstration Only)

The `if __name__ == "__main__":` entry point in the generated script is strictly for testing and demonstrating usage. It should accept CLI arguments: `--device` (e.g. `cpu`, `cuda:0`, `npu:0`), `--warmup`, `--repetitions`, `--batch_size`, and `--num_samples` (default 5), sample representative architecture configurations, call `get_latency()` on each, output the latency, and assert that the measured latencies are non-negative.



### Latency unit (no conversion — declaration only)

The workflow input `latency_unit` (default `ms`) declares the unit of every latency value this estimator returns. The estimator MUST return the raw measured number with **no unit conversion** (no ×1000 / ÷1000); the unit is metadata recorded in `search_record_schema.json` (`latency_unit`), and downstream charts / selection / comparison label values in that unit.

- Default path (built-in `measure_module_latency`): always returns `ms`. This matches `latency_unit=ms` (the default). Declaring `latency_unit ∈ {us, s}` on the default path would mislabel `ms` values, so the workflow bootstrap rejects that combination — a non-`ms` unit requires the user-script path below.
- User-script path: the user's script defines the unit (microseconds, seconds, …). The user declares the matching `latency_unit`; the estimator stores the script's raw return value unchanged.

## User-provided latency script (when `latency_script_path` is given)

When the workflow input `{{ inputs.latency_script_path }}` is provided, the user's script is the
**single source of truth** for latency. `latency_estimator.py` wraps it; do **not** fall back to the
built-in PyTorch `measure_module_latency`, FLOPs/MACs/params, or any proxy. The latency this path
produces is the `latency` objective in `search_config.yaml objs` and the latency source in
`select_architecture.py` — single source of truth across the whole search pipeline (see the user-measure
fidelity rule in `psu_search_pipeline/agent.md`).

### Wrapper contract

`get_latency(arch_config)` must:

1. `self.supernet.set_sample_config(arch_config)` + `get_active_subnet()` to extract the active subnet
   (same as the default path), and apply the freeze step from "Handling Non-Searchable Model Logic" when
   applicable.
2. Construct a dummy input matching the subnet's forward signature (use `latency_cfg.batch_size` for the
   batch dim; hardcode the rest per the concrete supernet + manifest input shape) — this is
   `latency_estimator.py`'s responsibility, not the user script's.
3. Export the subnet to a **single-file ONNX**: `torch.onnx.export(...)`, then
   `onnx.save_model(path, model, save_as_external_data=False)` to forbid the `.data` sidecar (keep params
   <2GB; `torch.onnx.export` has no `external_data` arg — use the onnx-package call to disable it). Adapt
   IO tensor names / shapes / dtypes to what the user script expects **inside `latency_estimator.py`** —
   never modify the user script.
4. Invoke the user script with the onnx path as a CLI arg (`subprocess.run([script, onnx_path], ...)`).
5. Parse the **last stdout line** (or the script's return value) as the raw latency value (do not convert; the unit is declared by the workflow's `latency_unit` — see "Latency unit" above).
6. If the script exits non-zero → `raise` / explicit error (fail loud, never swallow). After measurement,
   `del subnet; empty_cache(self.device)`.

### User script contract (record in `latency_estimator.py` docstring/comments)

- Input: onnx file path (CLI arg).
- Output: raw latency value as the last stdout line or return value (unit declared via the workflow's `latency_unit`; see "Latency unit" above).
- Exit code 0 = success; non-zero = failure.

### Validation (user-script path)

- `python -m py_compile latency_estimator.py` (inline).
- Extend the persistent `tests/test_latency_estimator_smoke.py`: when `latency_script_path` is provided,
  the smoke test wraps the user script path (skip the runtime call if the script is unavailable in this
  runtime and say so — never fake a result). Non-negative latency + exit 0 on the wrapped path.

## Validation

If a check fails, fix the generated file and rerun the failed check before proceeding.

- `python -m py_compile latency_estimator.py` (inline).
- Persistent smoke test: write `<output_dir>/tests/test_latency_estimator_smoke.py` (plain script per the skill's Persistent Tests convention, starting with the sibling-import `sys.path` bootstrap) and run `python tests/test_latency_estimator_smoke.py` from `<output_dir>`. The script must:
  - Dynamically import `LatencyEstimator` using the `latency_estimator` import path, construct `SearchSpace()`, and verify the constructor accepts `(search_space, latency_cfg, device)` without raising a `TypeError` on signature inspection.
  - Build a minimal latency cfg inline (small positive `warmup`/`repetitions`, `batch_size` 1), resolve one available device via `resolve_device`, sample 2–3 `ArchConfig`s, call `get_latency` on each, and assert every returned latency is non-negative.
  - If a freeze step was added for dynamic non-searchable logic, also call `get_latency` twice with the **same** `arch_config` and assert the two latencies are within a loose relative tolerance (e.g. within 50%) of each other, to confirm the freeze actually stabilized measurement.
- Optional inline check: the CLI entry also works directly, e.g. `python latency_estimator.py --device cpu --warmup 2 --repetitions 5 --num_samples 2` (substitute `cuda:0` / `npu:0` per available hardware).

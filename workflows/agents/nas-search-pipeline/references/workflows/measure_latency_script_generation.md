# Latency Estimator Generation Workflow

Use this workflow after the train script has been generated to create `latency_estimator.py`. The estimator measures whole-architecture latency on-the-fly using PyTorch directly. It supports CPU, CUDA, and NPU devices via PyTorch's native device abstraction. All generated artifacts are written under `<output_dir>`.

## Read And Inspect

Read the generated example:

- `references/supernet_workflow_examples/latency_estimator.py`

Inspect the following to adapt the example to the concrete `<output_dir>/supernet.py`:

- `SearchSpace`, `ArchConfig`, `SuperNet`, and `set_sample_config()` / `get_active_subnet()` APIs.
- The supernet's forward signature and validated dummy input shapes from the generated training script and the original project under `<user_project_root>`.

## Generate `latency_estimator.py`

Adapt `references/supernet_workflow_examples/latency_estimator.py`; it already shows the constructor, whole-architecture measurement, and the `get_latency()` interface. Keep the generated script concrete to `supernet.py`.

- Import the generated supernet as a plain sibling import: `from supernet import ArchConfig, SearchSpace, SuperNet`.
- Import `measure_module_latency` from `nas_agent.latency.pytorch_latency_utils`.
- Import `empty_cache` from `nas_agent.train`.
- **Constructor** `LatencyEstimator(search_space, latency_cfg, device)`:
  - Store `latency_cfg`, `device` (`torch.device`), and create `SuperNet` once on CPU.
- **`get_latency(arch_config)`**:
  - Call `self.supernet.set_sample_config(arch_config)`, extract the active subnet via `self.supernet.get_active_subnet()`.
  - Construct a dummy input matching the subnet's forward signature. Use `latency_cfg.batch_size` for the batch dimension and **hardcode** the remaining dimensions (channels, spatial size, sequence length, etc.) to match the concrete supernet. For single-tensor input models, use `torch.randn(batch_size, ...)`. For models with multi-arg or kwargs forward signatures, construct the corresponding inputs.
  - Call `measure_module_latency(subnet, dummy_input, device=self.device, ...)` passing `warmup` and `repetitions` from `self.latency_cfg`. The function returns the average latency in milliseconds directly.
  - After measurement, free device memory: `del subnet; empty_cache(self.device)`.
- **Model Naming**: Set `model_name=f"subnet_{arch_hash}"` where `arch_hash` is a 16-character hex hash of `repr(arch_config)` (e.g., using `hashlib.sha1`). This is reserved for future ONNX-based latency measurements that require unique filenames.

### CLI Smoke Test (Testing and Usage Demonstration Only)

The `if __name__ == "__main__":` entry point in the generated script is strictly for testing and demonstrating usage. It should accept CLI arguments: `--device` (e.g. `cpu`, `cuda:0`, `npu:0`), `--warmup`, `--repetitions`, `--batch_size`, and `--num_samples` (default 5), sample representative architecture configurations, call `get_latency()` on each, output the latency, and assert that the measured latencies are non-negative.



## Validation

If a check fails, fix the generated file and rerun the failed check before proceeding.

- `python -m py_compile latency_estimator.py`
- Config integration check: dynamically import `LatencyEstimator` using the `latency_estimator` import path, construct `SearchSpace()`, and verify the constructor accepts `(search_space, latency_cfg, device)` without raising a `TypeError` on signature inspection.
- Smoke test — choose the command matching the hardware available in the current runtime environment:
  - CPU: `python latency_estimator.py --device cpu --warmup 2 --repetitions 5 --num_samples 2`
  - GPU: `python latency_estimator.py --device cuda:0 --warmup 2 --repetitions 5 --num_samples 3`
  - NPU: `python latency_estimator.py --device npu:0 --warmup 2 --repetitions 5 --num_samples 3`
- Verify it prints a non-negative latency for each sampled architecture and exits 0.


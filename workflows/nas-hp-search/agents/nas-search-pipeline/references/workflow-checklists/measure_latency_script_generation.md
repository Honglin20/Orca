# Checklist: Latency Estimator Generation

Companion to: `workflows/measure_latency_script_generation.md`

## How To Use

Each item below is a verifiable requirement extracted from the companion workflow. Verify items in order. For items marked `auto-fixable: yes`, fix the artifact directly. For items marked `auto-fixable: no`, report the issue for the caller.

## Items

### [CRITICAL] 1. Sibling Import From Supernet
**auto-fixable**: yes
**Section**: Generate `latency_estimator.py`
**Check**: `latency_estimator.py` imports `ArchConfig`, `SearchSpace`, and `SuperNet` from `supernet` as a plain sibling import.
**Verify**: grep for `from supernet import` in `latency_estimator.py`. Must find `ArchConfig`, `SearchSpace`, `SuperNet`.
**Anti-pattern**: `import supernet` without `from`, or `sys.path` manipulation.
**Fix**: Replace the import line with `from supernet import ArchConfig, SearchSpace, SuperNet`.

### [CRITICAL] 2. Uses `measure_module_latency` From `nas_agent.latency.pytorch_latency_utils`
**auto-fixable**: yes
**Section**: Generate `latency_estimator.py`
**Check**: The script imports `measure_module_latency` from `nas_agent.latency.pytorch_latency_utils` and `empty_cache` from `nas_agent.train`.
**Verify**: grep for `from nas_agent.latency.pytorch_latency_utils import measure_module_latency` and `from nas_agent.train import empty_cache`.
**Anti-pattern**: Implementing custom latency measurement instead of using the framework utility.
**Fix**: Add `from nas_agent.latency.pytorch_latency_utils import measure_module_latency` and `from nas_agent.train import empty_cache`.

### [CRITICAL] 3. Constructor Signature
**auto-fixable**: yes
**Section**: Generate `latency_estimator.py`
**Check**: `LatencyEstimator.__init__` accepts `(self, search_space, latency_cfg, device)`. Constructor stores `latency_cfg`, `device` (as `torch.device`), and creates `SuperNet` once on CPU.
**Verify**: Inspect `__init__` signature and body. Confirm `SuperNet` is created on CPU (not on `device`).
**Anti-pattern**: Creating `SuperNet` on GPU/NPU in constructor; missing `latency_cfg` or `device` parameters.
**Fix**: Adjust constructor signature and ensure `SuperNet(search_space=search_space)` without `.to(device)`.

### [MINOR] 4. Model Naming With Arch Hash
**auto-fixable**: yes
**Section**: Generate `latency_estimator.py`
**Check**: Uses `model_name=f"subnet_{arch_hash}"` where `arch_hash` is a 16-character hex hash of `repr(arch_config)`. This is reserved for future ONNX-based latency measurements and is not consumed by the PyTorch measurement path.
**Verify**: grep for `arch_hash` or `model_name` in `get_latency`. Confirm hash length is 16 hex chars.
**Anti-pattern**: Using full `repr(arch_config)` as filename (too long, may contain special chars).
**Fix**: Add `import hashlib`, `arch_hash = hashlib.sha1(repr(arch_config).encode()).hexdigest()[:16]`, and `model_name = f"subnet_{arch_hash}"`.

### [CRITICAL] 5. `get_latency()` Flow
**auto-fixable**: no
**Section**: Generate `latency_estimator.py`
**Check**: `get_latency(arch_config)` follows this flow: (1) `self.supernet.set_sample_config(arch_config)`, (2) `self.supernet.get_active_subnet()`, (3) construct dummy input matching the subnet's forward signature using `latency_cfg.batch_size`, (4) call `measure_module_latency(subnet, dummy_input, device=self.device, ...)` passing `warmup` and `repetitions` from `self.latency_cfg`, (5) `del subnet; empty_cache(self.device)` to free device memory.
**Verify**: Read `get_latency` and confirm all five steps are present in order.

### [CRITICAL] 6. Dummy Input Matches Supernet Forward Signature
**auto-fixable**: no
**Section**: Generate `latency_estimator.py`
**Check**: The dummy input constructed in `get_latency()` matches the supernet's actual forward signature (shape, dtype, number of arguments). For single-tensor models, uses `torch.randn(batch_size, ...)`. For multi-arg/kwargs models, constructs corresponding inputs.
**Verify**: Cross-reference the dummy input construction in `latency_estimator.py` with the `SuperNet.forward()` signature in `supernet.py`.
**Anti-pattern**: Hardcoded input shapes that don't match the supernet; missing batch_size from `latency_cfg`.

### [MAJOR] 7. CLI Smoke Test Block
**auto-fixable**: no
**Section**: CLI Smoke Test
**Check**: `if __name__ == "__main__":` block accepts CLI args: `--device`, `--warmup`, `--repetitions`, `--batch_size`, `--num_samples` (default 5). It samples representative configs, calls `get_latency()`, outputs latency, and asserts non-negative.
**Verify**: Read the `__main__` block and confirm all required CLI arguments are present.
**Anti-pattern**: Including removed args like `--soc_version` or `--work_dir`; missing assertion on non-negative latency.

### [MAJOR] 8. API Consistency With Supernet (Cross-Reference)
**auto-fixable**: no
**Section**: (Cross-reference check — typically requested by caller)
**Check**: The following are consistent between `latency_estimator.py` and `supernet.py`:
- `SearchSpace` / `ArchConfig` / `SuperNet` field names match
- `set_sample_config` / `get_active_subnet` call signatures match
- Dummy input shapes match the supernet's forward signature
**Verify**: Read both files and compare the API surface. Check constructor args, method signatures, and attribute names.

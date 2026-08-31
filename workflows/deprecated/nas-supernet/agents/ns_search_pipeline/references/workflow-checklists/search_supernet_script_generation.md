# Checklist: Search Supernet Script Generation

Companion to: `workflows/search_supernet_script_generation.md`

## How To Use

Each item below is a verifiable requirement extracted from the companion workflow. Verify items in order. For items marked `auto-fixable: yes`, fix the artifact directly. For items marked `auto-fixable: no`, report the issue for the caller.

This checklist covers artifact/interface compliance only. Project-semantics fidelity (helper correctness, training semantics, metric/reward fidelity, non-standard paradigms, RL environment fidelity) is audited by the `project-fidelity-verifier` subagent and is intentionally absent here.

**Definitions:**
- `<user_project_root>`: The path to the user's original PyTorch project repository containing the original training loop, data pipeline, and original model definitions.
- `<output_dir>`: The directory where the search artifacts (e.g., `arch_codec.py`, `evaluator.py`) are being generated.

This checklist covers four generated files: `arch_codec.py`, `evaluator.py`, `search_config.yaml`, and `run_search_supernet.sh`.

---

## arch_codec.py

### [CRITICAL] 1. Sibling Import From Supernet
**auto-fixable**: yes
**Section**: §1 Gene And ArchConfig Codec
**Check**: `arch_codec.py` imports `SearchSpace` and `ArchConfig` from `supernet` as a plain sibling import: `from supernet import SearchSpace, ArchConfig`.
**Verify**: grep for `from supernet import` in `arch_codec.py`.
**Fix**: Replace with `from supernet import ArchConfig, SearchSpace`.

### [CRITICAL] 2. Gene Layout Matches SearchSpace
**auto-fixable**: no
**Section**: §1 Encoding Layout
**Check**: The gene layout exactly mirrors the `SearchSpace` schema:
- For staged models: depth genes first (one per stage), then layer-level genes in stage/layer order
- For isotropic models: global depth gene first, then layer-level genes in layer order
- Gene length equals the maximum active subnet size (not any one sampled candidate)
- Every gene stores a **candidate index**, not the actual value
- Fixed architecture metadata (fixed widths, fixed stems, etc.) does NOT appear as gene entries unless `ArchConfig` explicitly searches them
**Verify**: Read `ArchCodec.__init__` and `get_gene_space()`. Cross-reference with `SearchSpace` fields in `supernet.py`. Count gene segments and verify they match.
**Anti-pattern**: Gene length varies per candidate; actual values stored instead of indices; fixed fields encoded as genes.

### [CRITICAL] 3. Depth Padding — Inactive Layers Ignored
**auto-fixable**: no
**Section**: §1 Encoding Layout
**Check**: For depth-search supernets, gene slots for every layer up to max depth are reserved. Inactive layer slots (padding) do not affect the decoded `ArchConfig`. Unused branch parameter slots also do not affect `ArchConfig`.
**Verify**: Read `gene_to_arch()` logic. When decoded depth is less than max, trailing layer genes should be skipped.
**Anti-pattern**: Inactive layer genes influencing the decoded architecture; `ArchConfig.layer_configs` containing entries for inactive layers.

### [CRITICAL] 4. Public API Methods
**auto-fixable**: no
**Section**: §1 Public API
**Check**: `ArchCodec` has these instance methods:
- `get_gene_space()` → dict with `gene_len`, `lower_bounds`, `upper_bounds`, `metadata`
- `gene_to_arch(gene)` → `ArchConfig` (rounds floats to ints internally)
**Verify**: Read class definition and confirm both methods exist with correct signatures.
**Anti-pattern**: Static methods where instance methods are expected; missing `metadata` in gene space.

### [CRITICAL] 5. `gene_to_arch` Constructs Exact ArchConfig
**auto-fixable**: no
**Section**: §1 Public API
**Check**: `gene_to_arch` constructs the exact generated `ArchConfig` schema without silently renaming choices, guessing missing fields, or remapping block names.
**Verify**: Read `gene_to_arch` and compare the constructed `ArchConfig` with the `ArchConfig` definition in `supernet.py`. Field names, nesting structure, and value types must match exactly.
**Anti-pattern**: Renaming `ArchConfig` fields; constructing a dict instead of `ArchConfig` dataclass; hardcoding block names different from the layer configs field keys.

---

## evaluator.py

### [CRITICAL] 6. Single Evaluation Paradigm
**auto-fixable**: no
**Section**: §2 Evaluation Paradigms
**Check**: `evaluator.py` generates only the code path for the effective evaluation paradigm (one of `validate`, `finetune`, `train_from_scratch`). No runtime mode-switching logic.
**Verify**: Read `evaluate()` method. Confirm only one paradigm's flow is implemented.
**Anti-pattern**: `if self.paradigm == "validate"` runtime branching.

### [MAJOR] 7. OmegaConf `evaluator_cfg` Usage
**auto-fixable**: no
**Section**: §2 Public API
**Check**: `evaluator_cfg` is treated as an OmegaConf node: attributes via dot notation (`self.cfg.batch_size`), `.get()` for optional properties (`self.cfg.get("supernet_ckpt_path")`).
**Verify**: Read `__init__` and confirm cfg access patterns.
**Anti-pattern**: Using `evaluator_cfg["batch_size"]` dict-style access on OmegaConf node.

### [MAJOR] 8. Self-Contained — No User Project Imports
**auto-fixable**: no
**Section**: §2 Model Construction, §Data Pipeline
**Check**: `evaluator.py` and all generated helper files (e.g., `data_utils.py`, `losses.py`) do not import modules from `<user_project_root>`. All needed logic must be ported into the generated files.
**Verify**: grep for imports referencing `<user_project_root>` in all generated `.py` files under `<output_dir>`.
**Anti-pattern**: Relying on `<user_project_root>` being in `sys.path` or importing dataset classes directly from it.

### [MAJOR] 9. Data Pipeline Wiring
**auto-fixable**: no
**Section**: §Data Pipeline, evaluator_training_loop_guide.md §Data Pipeline
**Check**: When `train_supernet.py` exists and its helpers already contain the adapted data pipeline, they are reused as sibling imports rather than duplicated. Data loaders are built in `__init__` using `evaluator_cfg` fields for data paths and shared across `evaluate()` calls. DataLoaders use standard single-device loading (`shuffle=True` for training loaders, no `DistributedSampler`). No hardcoded data paths. Whether the ported pipeline preserves the original project's preprocessing semantics is audited by the `project-fidelity-verifier`, not this checklist.
**Verify**: Read `__init__` for loader construction and confirm data paths come from `self.cfg.*`, not hardcoded literals. Check for absence of `DistributedSampler`. Check that existing `train_supernet.py` data helpers are imported, not re-implemented.
**Anti-pattern**: Using `DistributedSampler`; hardcoding data paths; duplicating data pipeline code that already exists in `train_supernet.py` helpers.

### [CRITICAL] 10. Metric Return Format: Smaller Is Better
**auto-fixable**: no
**Section**: §2 Data And Metric Semantics, Public API
**Check**: All metric values returned by `evaluate()` are smaller-is-better. Larger-is-better metrics (accuracy, F1, mAP, etc.) are negated. Return values are Python built-in scalars (`.item()` on tensors).
**Verify**: Read the return statement of `evaluate()`. Check that accuracy-like metrics are negated. Check for `.item()` calls on tensor values.
**Anti-pattern**: Returning raw accuracy without negation; returning PyTorch tensors instead of Python floats.

### [CRITICAL] 11. Metric Keys Match `search_config.yaml` `objs`
**auto-fixable**: no
**Section**: §2 Public API, §3 Runtime Config
**Check**: The keys in the dict returned by `evaluate()` exactly match the quality objective entries in `search_config.yaml` `objs` (excluding `latency`).
**Verify**: Read `evaluate()` return dict keys. Read `objs` list in `search_config.yaml`. Compare (excluding `latency` from objs).
**Anti-pattern**: `evaluate()` returns `{"acc": ...}` but `objs` lists `"accuracy"`.

### [CRITICAL] 12. Evaluator Forward-Pass Matches Supernet
**auto-fixable**: no
**Section**: §2 Model Construction (Cross-reference check)
**Check**: The model forward-pass call in `evaluator.py` matches the `SuperNet.forward()` signature in `supernet.py`:
- Input tensor construction (shape, dtype, number of args)
- Batch unpacking from dataloader matches model expectations
- Forward call in `evaluator.py` matches `SuperNet.forward()` in `supernet.py` (and `train_supernet.py` when available)
**Verify**: Cross-reference `evaluator.py` forward call with `SuperNet.forward()` in `supernet.py` and model construction in `train_supernet.py` (when available).

### [CRITICAL] 13. Paradigm-Specific Evaluator Flow
**auto-fixable**: no
**Section**: §2 Evaluation Paradigms, evaluator_training_loop_guide.md §Evaluation Flow
**Check**: `evaluate()` follows the exact flow for the effective paradigm:
- **validate**: configures `self.supernet` with the candidate `ArchConfig` and runs validation directly. No subnet extraction, no training.
- **finetune**: computes a stable `arch_id`, prints start/done banners, extracts the active subnet with inherited weights, trains it for the configured short budget, tracks best validation metric, saves per-candidate checkpoints (see item 14), then deletes the subnet and clears cache.
- **train_from_scratch**: same per-candidate flow as finetune, but re-initializes the extracted subnet weights before optimizer construction and records `paradigm=train_from_scratch` in `arch_info.json`.
**Verify**: Read `evaluate()` and any helper methods. Confirm the code path matches the effective paradigm.
**Anti-pattern**: `validate` extracting or training a subnet; `finetune` returning metrics without per-architecture cleanup; `train_from_scratch` using inherited supernet weights without reset.

### [MAJOR] 14. Per-Candidate Checkpoint Saves
**auto-fixable**: no
**Section**: evaluator_training_loop_guide.md §Evaluation Flow
**Check**: For `finetune` and `train_from_scratch`, `evaluator_cfg` must include `save_dir` with a default path. The evaluator saves per-candidate checkpoints under `{save_dir}/{arch_id}/`:
- `last.pth`: saved after training completes
- `best.pth`: saved when the best validation metric is achieved during training
- `arch_info.json`: metadata with `arch_id`, `gene` (integer tuple), `arch_config`, `paradigm`, `epochs`, `best_metric`, and `metrics`
**Verify**: Read the checkpoint saving logic in `evaluate()`. Confirm it creates a per-architecture subdirectory using `arch_id`, saves all three files, and that `best.pth` is based on validation metric tracking.
**Anti-pattern**: Missing `save_dir` in `evaluator_cfg`; saving all candidates' checkpoints into one flat directory without `arch_id`; missing `best.pth` or `arch_info.json`; not creating the subdirectory.

### [CRITICAL] 15. Supernet Checkpoint Loading (When Applicable)
**auto-fixable**: no
**Section**: §2 Evaluation Paradigms
**Check**: For `validate` and `finetune` paradigms, the evaluator loads the supernet checkpoint from `self.cfg.supernet_ckpt_path` trained by `train_supernet.py`. For `train_from_scratch`, no checkpoint is loaded and weights are re-initialized via `subnet.apply(reset_module)`.
**Verify**: Read `__init__` for checkpoint loading. For `train_from_scratch`, check for `reset_parameters` logic.

*Items 16–20 apply only to `finetune` and `train_from_scratch` paradigms. Skip for `validate`. The **Section** fields in these items refer to `evaluator_training_loop_guide.md` unless stated otherwise.*

### [CRITICAL] 16. No DDP In Evaluator
**auto-fixable**: yes
**Section**: §Key Constraints
**Check**: `evaluator.py` does NOT use DDP utilities: no `DistributedDataParallel`, no `DistributedSampler`, no `set_sample_config_ddp`, no `save_checkpoint_ddp`, no `sync_random_seed`, no rank guards (`is_main_process()`), no `setup_distributed()`.
**Verify**: grep for `DistributedDataParallel`, `DistributedSampler`, `set_sample_config_ddp`, `save_checkpoint_ddp`, `sync_random_seed`, `is_main_process`, `setup_distributed` in `evaluator.py`.
**Anti-pattern**: Importing or calling DDP-specific utilities (the items in the Verify list above) in the evaluator. Note: device-compatibility wrappers re-exported through `nas_agent.train`, such as `autocast`, `empty_cache`, `load_checkpoint`, `grad_scaler`, are acceptable and not DDP-specific.
**Fix**: Remove DDP imports and calls. Use direct `subnet.train()`, `subnet.eval()`, plain `DataLoader` with `shuffle=True`, and `torch.save()` for checkpoints.

### [CRITICAL] 17. AMP Uses `autocast` And `grad_scaler` From `nas_agent.train`
**auto-fixable**: yes
**Section**: §Optimizer, Scheduler, And AMP
**Check**: AMP uses `autocast` and `grad_scaler` from `nas_agent.train` (device-compatibility wrappers). The scaler is created via `grad_scaler(self.device, enabled=use_amp)`, which handles NPU incompatibility internally. Autocast enable flag is independent from `scaler.is_enabled()`.
**Verify**: Read the AMP setup code in `evaluate()`. Check that `grad_scaler` is imported from `nas_agent.train` and called with `(self.device, enabled=use_amp)`. Check that `autocast` is imported from `nas_agent.train`.
**Anti-pattern**: Constructing `torch.amp.GradScaler` directly instead of using `grad_scaler()`; manually disabling the scaler on NPU instead of letting the helper handle it.
**Fix**: Replace direct `torch.amp.GradScaler(...)` with `from nas_agent.train import grad_scaler; scaler = grad_scaler(self.device, enabled=use_amp)`.

### [CRITICAL] 18. NPU `foreach` Compatibility
**auto-fixable**: yes
**Section**: Skill SKILL.md NPU Compatibility
**Check**: For `finetune` and `train_from_scratch` paradigms, `is_npu = device.type == "npu"` is set and both optimizer constructor and `clip_grad_norm_` (if used) pass `foreach=False if is_npu else None`.
**Verify**: grep for `is_npu` and `foreach` in `evaluator.py`.
**Fix**: Add `is_npu = device.type == "npu"` and `foreach=False if is_npu else None` parameters.

### [MAJOR] 19. Per-Candidate Resource Lifecycle
**auto-fixable**: no
**Section**: §Key Constraints, §Evaluation Flow
**Check**: Per-candidate resources (subnet, optimizer, scheduler, scaler) are created inside `evaluate()` and destroyed before returning. Shared resources (supernet, data loaders, criteria) are initialized in `__init__` and reused across calls.
**Verify**: Read `evaluate()` and `__init__`. Confirm optimizer, scheduler, and scaler are NOT stored as `self.*` attributes. Confirm `del subnet, optimizer, scheduler, scaler` and `empty_cache()` are called before return.
**Anti-pattern**: Storing optimizer/scheduler as `self.optimizer`/`self.scheduler` and reusing across candidates; missing cleanup of scaler.

### [CRITICAL] 20. Memory Cleanup
**auto-fixable**: no
**Section**: §2 finetune / train_from_scratch
**Check**: For `finetune` and `train_from_scratch` paradigms, after evaluation: `del subnet` and `empty_cache(self.device)` are called to free memory.
**Verify**: grep for `del subnet` and `empty_cache` in `evaluator.py`.
**Anti-pattern**: Missing cleanup; subnet accumulating across candidates.

---

## search_config.yaml

### [CRITICAL] 21. Required Config Keys Present
**auto-fixable**: no
**Section**: §3 Runtime Config
**Check**: All required keys exist: `search_space`, `arch_codec`, `evaluator`, `latency_estimator`, `latency_cfg`, `objs`, `search_log_path`, `concurrency`, `population_size`, `num_generations`, `evaluator_cfg`.
**Verify**: Read `search_config.yaml` and check for each required key.
**Anti-pattern**: Missing keys; renamed keys (e.g. `objectives` instead of `objs`). Do not invent missing values (budgets, population sizes, etc.); report under Unresolved for the caller.

### [CRITICAL] 22. Import Paths Resolve
**auto-fixable**: no
**Section**: §3 Runtime Config (Cross-reference check)
**Check**: The import paths in `search_config.yaml` resolve to actual class names (the exact names below are examples, but the resolved classes must exist):
- `search_space` → e.g., `supernet.SearchSpace`
- `arch_codec` → e.g., `arch_codec.ArchCodec`
- `evaluator` → e.g., `evaluator.CandidateEvaluator`
- `latency_estimator` → e.g., `latency_estimator.LatencyEstimator`
**Verify**: Read each import path. Confirm the module and class name exist in the corresponding `.py` file.
**Anti-pattern**: Configuring an import path for a class name that was not actually generated.

### [CRITICAL] 23. `latency_cfg` Fields Match `latency_estimator.py`
**auto-fixable**: no
**Section**: §3 Runtime Config (Cross-reference check)
**Check**: The fields in `latency_cfg` match the `cfg.latency_cfg` attribute accesses in `latency_estimator.py`. Must include: `warmup`, `repetitions`, `batch_size`.
**Verify**: Read `latency_estimator.py` for all `self.latency_cfg.*` or `latency_cfg.*` accesses. Compare with keys in `search_config.yaml` `latency_cfg`.
**Anti-pattern**: Config has `num_warmup` but code accesses `latency_cfg.warmup`.

### [CRITICAL] 24. `objs` Ends With `latency`
**auto-fixable**: yes
**Section**: §3 Runtime Config
**Check**: `objs` lists quality objectives first and `latency` last.
**Verify**: Read `objs` in `search_config.yaml`.
**Anti-pattern**: `latency` not last; `latency` missing entirely.
**Fix**: Move `latency` to the end of the `objs` list, or add it if missing.

### [MAJOR] 25. `evaluator_cfg` Paradigm-Appropriate Fields
**auto-fixable**: no
**Section**: §3 evaluator_cfg details
**Check**: `evaluator_cfg` contains fields appropriate for the effective paradigm:
- **validate**: `data_dir`, data-related fields, `supernet_ckpt_path`, batch_size, num_workers, amp
- **finetune**: validate fields + `lr`, `weight_decay`, `epochs`; must have `save_dir`
- **train_from_scratch**: finetune fields except NO `supernet_ckpt_path`; must have `data_dir` and `save_dir`
**Verify**: Read `evaluator_cfg` and confirm fields match the paradigm.

### [MAJOR] 26. Budget-Hyperparameter Coherence
**auto-fixable**: no
**Section**: Validation (Budget-hyperparameter coherence)
**Check**: For `finetune` and `train_from_scratch`, budget-dependent hyperparameters in `evaluator_cfg` (scheduler, warmup, milestones) are coherent with the configured training budget. For finetune, budget is typically 5-20% of original. For train_from_scratch, budget is strictly less than original.
**Verify**: Compare `evaluator_cfg.epochs` with original project budget. Check scheduler params are proportionally adjusted.

---

## run_search_supernet.sh

### [CRITICAL] 27. Launcher Calls `nas-search` With Only `--config`
**auto-fixable**: yes
**Section**: §4 Search Launcher
**Check**: `run_search_supernet.sh` calls `nas-search --config "./search_config.yaml"` and passes **only** `--config`. All search parameters live in `search_config.yaml`; the launcher must not pass any other CLI arguments or parameter overrides.
**Verify**: grep for `nas-search` in `run_search_supernet.sh`; confirm `--config` is the sole argument and no other flags/overrides are present.
**Anti-pattern**: Custom search orchestrator; running `python search.py` instead of `nas-search`; passing extra flags or overrides (e.g. `--population_size`, `--device`, `--objs`) alongside `--config`.
**Fix**: Replace with `nas-search --config "./search_config.yaml"` and move any extra parameters into `search_config.yaml`.

### [CRITICAL] 28. Launcher Is Executable And Valid
**auto-fixable**: yes
**Section**: §4 Search Launcher
**Check**: `run_search_supernet.sh` has executable permission and passes `bash -n` syntax check.
**Verify**: Check file permissions and run `bash -n run_search_supernet.sh` (syntax-only).
**Fix**: Run `chmod +x run_search_supernet.sh` if needed, and fix any syntax errors found by `bash -n`.

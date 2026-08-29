# Checklist: Search Supernet Script Generation

Companion to: `workflows/search_supernet_script_generation.md`

## How To Use

Each item below is a verifiable requirement extracted from the companion workflow. Verify items in order. For items marked `auto-fixable: yes`, fix the artifact directly. For items marked `auto-fixable: no`, report the issue for the caller.

This checklist covers artifact/interface compliance only. Project-semantics fidelity (helper correctness, validation semantics, metric/reward fidelity, non-standard paradigms, RL environment fidelity) is audited by the `project-fidelity-verifier` subagent and is intentionally absent here.

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

### [CRITICAL] 2. Choice-Index Gene Layout Matches SearchSpace
**auto-fixable**: no
**Section**: §1 Encoding Layout
**Check**: The gene layout is choice-only and exactly mirrors the `SearchSpace` schema:
- One branch-choice gene per transformer layer slot, in the original layer order
- `gene_len` equals the slot count (`SearchSpace.depth`) — there is no depth segment, no padding slot, and no branch-local parameter segment
- Bounds are `[0, len(branch_choices) - 1]` for every gene
- Every gene stores a **candidate index** into `branch_choices`, not the branch name; decoding applies `branch_choices[idx]`
- Pinned dimensions (depth, widths, head counts, sequence lengths) and fixed architecture metadata (embeddings, stems, heads) do NOT appear as gene entries
**Verify**: Read `ArchCodec.__init__` and `get_gene_space()`. Cross-reference `SearchSpace.branch_choices` and `SearchSpace.depth` in `supernet.py`. Confirm `gene_len == depth` and bounds match the branch count.
**Anti-pattern**: A depth gene; padding slots for inactive layers; branch names stored instead of indices; per-branch parameter genes for pinned dimensions.

### [CRITICAL] 3. Public API Methods
**auto-fixable**: no
**Section**: §1 Public API
**Check**: `ArchCodec` has these instance methods:
- `get_gene_space()` → dict with `gene_len`, `lower_bounds`, `upper_bounds`, `metadata`
- `gene_to_arch(gene)` → `ArchConfig` (rounds floats to ints internally)
**Verify**: Read class definition and confirm both methods exist with correct signatures.
**Anti-pattern**: Static methods where instance methods are expected; missing `metadata` in gene space.

### [CRITICAL] 4. `gene_to_arch` Constructs Exact ArchConfig
**auto-fixable**: no
**Section**: §1 Public API
**Check**: `gene_to_arch` constructs the exact generated `ArchConfig` schema without silently renaming choices, guessing missing fields, or remapping branch names.
**Verify**: Read `gene_to_arch` and compare the constructed `ArchConfig` with the `ArchConfig` definition in `supernet.py`. Field names and value types must match exactly (for a choice-only schema: `choices` with one branch name per slot).
**Anti-pattern**: Renaming `ArchConfig` fields; constructing a dict instead of an `ArchConfig` dataclass; emitting per-layer config dicts with searched dimension values.

---

## evaluator.py

### [CRITICAL] 5. Single Evaluation Paradigm: `validate`
**auto-fixable**: no
**Section**: §2 Evaluation Paradigm
**Check**: `evaluator.py` generates only the `validate` (zero-training) code path. No runtime mode-switching logic.
**Verify**: Read `evaluate()` method. Confirm only the validate flow is implemented.
**Anti-pattern**: `if self.paradigm == "validate"` runtime branching; any finetune / train-from-scratch code path.

### [MAJOR] 6. OmegaConf `evaluator_cfg` Usage
**auto-fixable**: no
**Section**: §2 Public API
**Check**: `evaluator_cfg` is treated as an OmegaConf node: attributes via dot notation (`self.cfg.batch_size`), `.get()` for optional properties (`self.cfg.get("supernet_ckpt_path")`).
**Verify**: Read `__init__` and confirm cfg access patterns.
**Anti-pattern**: Using `evaluator_cfg["batch_size"]` dict-style access on OmegaConf node.

### [MAJOR] 7. Self-Contained — No User Project Imports
**auto-fixable**: no
**Section**: §2 Model Construction
**Check**: `evaluator.py` and all generated helper files (e.g., `data_utils.py`, `losses.py`) do not import modules from `<user_project_root>`. All needed logic must be ported into the generated files.
**Verify**: grep for imports referencing `<user_project_root>` in all generated `.py` files under `<output_dir>`.
**Anti-pattern**: Relying on `<user_project_root>` being in `sys.path` or importing dataset classes directly from it.

### [MAJOR] 8. Data Pipeline Wiring
**auto-fixable**: no
**Section**: §2 Data And Metric Semantics
**Check**: When `train_supernet.py` helpers already contain the adapted data pipeline, they are reused as sibling imports rather than duplicated. Data loaders are built in `__init__` using `evaluator_cfg` fields for data paths and shared across `evaluate()` calls. DataLoaders use standard single-device loading (no `DistributedSampler`). No hardcoded data paths. Whether the ported pipeline preserves the original project's preprocessing semantics is audited by the `project-fidelity-verifier`, not this checklist.
**Verify**: Read `__init__` for loader construction and confirm data paths come from `self.cfg.*`, not hardcoded literals. Check for absence of `DistributedSampler`. Check that existing `train_supernet.py` data helpers are imported, not re-implemented.
**Anti-pattern**: Using `DistributedSampler`; hardcoding data paths; duplicating data pipeline code that already exists in `train_supernet.py` helpers.

### [CRITICAL] 9. Metric Return Format: Smaller Is Better
**auto-fixable**: no
**Section**: §2 Data And Metric Semantics
**Check**: All metric values returned by `evaluate()` are smaller-is-better. Larger-is-better metrics (accuracy, F1, mAP, etc.) are negated. Return values are Python built-in scalars (`.item()` on tensors).
**Verify**: Read the return statement of `evaluate()`. Check that accuracy-like metrics are negated. Check for `.item()` calls on tensor values.
**Anti-pattern**: Returning raw accuracy without negation; returning PyTorch tensors instead of Python floats.

### [CRITICAL] 10. Metric Keys Match `search_config.yaml` `objs`
**auto-fixable**: no
**Section**: §2 Public API, §3 Runtime Config
**Check**: The keys in the dict returned by `evaluate()` exactly match the quality objective entries in `search_config.yaml` `objs` (excluding `latency`).
**Verify**: Read `evaluate()` return dict keys. Read `objs` list in `search_config.yaml`. Compare (excluding `latency` from objs).
**Anti-pattern**: `evaluate()` returns `{"acc": ...}` but `objs` lists `"accuracy"`.

### [CRITICAL] 11. Evaluator Forward-Pass Matches Supernet
**auto-fixable**: no
**Section**: §2 Model Construction (Cross-reference check)
**Check**: The model forward-pass call in `evaluator.py` matches the `SuperNet.forward()` signature in `supernet.py`:
- Input tensor construction (shape, dtype, number of args)
- Batch unpacking from dataloader matches model expectations
- Forward call in `evaluator.py` matches `SuperNet.forward()` in `supernet.py` and the model construction in `train_supernet.py`
**Verify**: Cross-reference `evaluator.py` forward call with `SuperNet.forward()` in `supernet.py` and model construction in `train_supernet.py`.

### [CRITICAL] 12. Zero-Training Validate Flow + Reverse Guards
**auto-fixable**: no
**Section**: §2 Evaluation Paradigm
**Check**: `evaluate()` follows the exact validate flow: configures `self.supernet` with the candidate `ArchConfig` via `set_sample_config()` and runs validation directly on the supernet. No subnet extraction, no training. **Reverse guards** — the evaluator must NOT contain:
- optimizer construction (`torch.optim.*`), LR schedulers, or gradient scalers
- a training loop (backward pass, parameter updates)
- checkpoint / model writes (no `torch.save`, no per-candidate ckpt dirs)
- a teacher model or any KD loss term in the search objective
- DDP utilities (`DistributedDataParallel`, `DistributedSampler`, `set_sample_config_ddp`, `save_checkpoint_ddp`, `sync_random_seed`, `is_main_process`, `setup_distributed`)
**Verify**: Read `evaluate()`. grep for `torch.optim`, `backward(`, `torch.save`, `GradScaler`, `scheduler`, `teacher`, `kd_`, and the DDP utility names in `evaluator.py`.
**Anti-pattern**: `validate` extracting or training a subnet; any optimizer/scheduler/scaler object; a KD loss entering the returned metrics.

### [CRITICAL] 13. Supernet Checkpoint Strict Loading
**auto-fixable**: no
**Section**: §2 Model Construction
**Check**: The evaluator loads the supernet checkpoint from `self.cfg.supernet_ckpt_path` (produced by the upstream KD training run, default `./runs/train/supernet_best.pth`) with **strict key matching** — any key mismatch must fail loud, never a silent partial load.
**Verify**: Read `__init__` checkpoint loading. Confirm `strict=True` semantics (a partial/lenient load that ignores missing or unexpected keys fails this item).
**Anti-pattern**: `strict=False` or error-swallowing load paths; treating a key mismatch as a warning.

---

## search_config.yaml

### [CRITICAL] 14. Required Config Keys Present
**auto-fixable**: no
**Section**: §3 Runtime Config
**Check**: All required keys exist: `search_space`, `arch_codec`, `evaluator`, `latency_estimator`, `latency_cfg`, `objs`, `search_log_path`, `concurrency`, `population_size`, `num_generations`, `evaluator_cfg`.
**Verify**: Read `search_config.yaml` and check for each required key.
**Anti-pattern**: Missing keys; renamed keys (e.g. `objectives` instead of `objs`). Do not invent missing values (budgets, population sizes, etc.); report under Unresolved for the caller.

### [CRITICAL] 15. Import Paths Resolve
**auto-fixable**: no
**Section**: §3 Runtime Config (Cross-reference check)
**Check**: The import paths in `search_config.yaml` resolve to actual class names (the exact names below are examples, but the resolved classes must exist):
- `search_space` → e.g., `supernet.SearchSpace`
- `arch_codec` → e.g., `arch_codec.ArchCodec`
- `evaluator` → e.g., `evaluator.CandidateEvaluator`
- `latency_estimator` → e.g., `latency_estimator.LatencyEstimator`
**Verify**: Read each import path. Confirm the module and class name exist in the corresponding `.py` file.
**Anti-pattern**: Configuring an import path for a class name that was not actually generated.

### [CRITICAL] 16. `latency_cfg` Fields Match `latency_estimator.py`
**auto-fixable**: no
**Section**: §3 Runtime Config (Cross-reference check)
**Check**: The fields in `latency_cfg` match the `cfg.latency_cfg` attribute accesses in `latency_estimator.py`. Must include: `warmup`, `repetitions`, `batch_size`.
**Verify**: Read `latency_estimator.py` for all `self.latency_cfg.*` or `latency_cfg.*` accesses. Compare with keys in `search_config.yaml` `latency_cfg`.
**Anti-pattern**: Config has `num_warmup` but code accesses `latency_cfg.warmup`.

### [CRITICAL] 17. `objs` Ends With `latency`
**auto-fixable**: yes
**Section**: §3 Runtime Config
**Check**: `objs` lists quality objectives first and `latency` last.
**Verify**: Read `objs` in `search_config.yaml`.
**Anti-pattern**: `latency` not last; `latency` missing entirely.
**Fix**: Move `latency` to the end of the `objs` list, or add it if missing.

### [MAJOR] 18. `evaluator_cfg` Validate-Only Fields
**auto-fixable**: no
**Section**: §3 evaluator_cfg details
**Check**: `evaluator_cfg` contains exactly the fields the zero-training validate paradigm needs: `data_dir`, data-related fields, `supernet_ckpt_path`, batch_size, num_workers, amp, and optional validation-budget controls (`max_val_samples` / `max_val_batches`). **Reverse guard**: no optimizer / scheduler / training-budget fields (`lr`, `weight_decay`, `epochs`, `save_dir`, ...) — their presence is a paradigm leak.
**Verify**: Read `evaluator_cfg` and confirm the field set; grep for the forbidden field names.
**Anti-pattern**: `lr`/`epochs`/`save_dir` configured for the evaluator; missing `supernet_ckpt_path`.

### [MAJOR] 19. `population_size` Within The Real Search Space
**auto-fixable**: no
**Section**: §3 Runtime Config
**Check**: `population_size = min(default 32, len(branch_choices) ** num_slots)` — it must not exceed the number of distinct choice paths. NSGA-II draws its initial population randomly; on a space smaller than the population (e.g. a single slot with 6 branches → 6 paths) initialization fails.
**Verify**: Read `population_size` in `search_config.yaml`; cross-reference `len(branch_choices)` and the slot count (`SearchSpace.depth`) in `supernet.py`.
**Anti-pattern**: `population_size: 32` on a 6-path search space.

---

## run_search_supernet.sh

### [CRITICAL] 20. Launcher Invokes The Runner As A Module With Only `--config`
**auto-fixable**: yes
**Section**: §4 Search Launcher
**Check**: `run_search_supernet.sh` calls the fixed runner via module invocation — `python3 -m nas_agent.cli.search --config "./search_config.yaml"` — and passes **only** `--config`. Module invocation makes no `PATH` assumption (the `nas-search` console script may not be installed on `PATH` on the remote box; the module path resolves as soon as `nas_agent` is importable). All search parameters live in `search_config.yaml`; the launcher must not pass any other CLI arguments or parameter overrides.
**Verify**: grep for `nas_agent.cli.search` in `run_search_supernet.sh`; confirm `--config` is the sole argument and no other flags/overrides are present.
**Anti-pattern**: Bare `nas-search --config ...` (PATH assumption); custom search orchestrator; running `python search.py` instead of the fixed runner; launching via `torchrun`; passing extra flags or overrides (e.g. `--population_size`, `--device`, `--objs`) alongside `--config`.
**Fix**: Replace with `python3 -m nas_agent.cli.search --config "./search_config.yaml"` and move any extra parameters into `search_config.yaml`.

### [CRITICAL] 21. Launcher Is Executable And Valid
**auto-fixable**: yes
**Section**: §4 Search Launcher
**Check**: `run_search_supernet.sh` has executable permission and passes `bash -n` syntax check.
**Verify**: Check file permissions and run `bash -n run_search_supernet.sh` (syntax-only).
**Fix**: Run `chmod +x run_search_supernet.sh` if needed, and fix any syntax errors found by `bash -n`.

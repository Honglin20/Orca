# Checklist: Train Supernet Script Generation

Companion to: `workflows/train_supernet_script_generation.md`

## How To Use

Each item below is a verifiable requirement from the companion workflow. Verify items in order. For items marked `auto-fixable: yes`, fix the artifact directly. For items marked `auto-fixable: no`, report the issue for the caller.

**DDP-specific items** (DistributedDataParallel, DistributedSampler, all_reduce, sync_random_seed broadcast) apply **only when `is_distributed()`** (torchrun multi-GPU). On single-device (default, plain `python3`), these are not required—the script correctly skips DDP wrap, uses plain sampler, and `AverageMeter.avg` is a no-op. Items referencing these should be evaluated as "if DDP is active, does the script do X correctly?"

**Fixed training paradigm**: the training loop is the workflow's fixed KD recipe (frozen pretrained teacher + per-slot hidden cosine + final-logits KL, variant-branch parameters only, one sampled choice path per step). KD is never an optional feature here: do not flag its presence as a deviation, and do not evaluate it against the user's original training loss.

**Definitions:**
- `<user_project_root>`: The path to the user's original PyTorch project repository containing the original data pipeline, evaluation function, and original model definitions.
- `<output_dir>`: The directory where the training artifacts (e.g., `train_supernet.py`, `run_train_supernet.sh`) are being generated.

## Items

### [MAJOR] 1. Stable Base CLI Contract
**auto-fixable**: no
**Section**: §1 CLI And Runtime Args
**Check**: `train_supernet.py` exposes the stable base CLI required by the workflow: `--output_dir`, `--eval_interval`, `--device` with choices `["auto", "cuda", "npu", "cpu"]`, `--amp`, `--lr`, `--max_grad_norm`, `--max_train_steps`, `--progress-every` (progress.jsonl chart-feed granularity, default `50`), `--pretrained_ckpt`, `--seed`, `--kd_hidden_weight`, and `--kd_logits_weight`. Project-derived training arguments are exposed as CLI overrides rather than hardcoded remote-only literals.
**Verify**: Inspect the argparse block in `train_supernet.py` statically. Confirm required flags exist with compatible defaults and choices. Do NOT run the script.
**Anti-pattern**: Missing `--device` or `--pretrained_ckpt`; hardcoded dataset/config path without CLI override; `--amp` implemented as a string argument instead of a boolean flag.

### [MAJOR] 2. Distributed And Checkpoint Imports
**auto-fixable**: yes
**Section**: §2 Distributed Setup, §7 Checkpoint
**Check**: Uses `setup_distributed`, `get_local_rank`, `get_rank`, `is_main_process`, `torch_manual_seed`, `unwrap_model`, `set_sample_config_ddp`, `autocast`, `grad_scaler` from `nas_agent.train.distributed`. Uses `save_checkpoint_ddp` from `nas_agent.train`.
**Verify**: grep for `from nas_agent.train.distributed import` and `from nas_agent.train import` and confirm the needed symbols are imported.
**Fix**: Add missing imports.

### [CRITICAL] 3. Model Construction — Supernet Import
**auto-fixable**: yes
**Section**: §5 Model Construction
**Check**: `SearchSpace` and `SuperNet` are imported from `supernet.py` as a plain sibling import: `from supernet import SearchSpace, SuperNet`.
**Verify**: grep for `from supernet import` in `train_supernet.py`.
**Fix**: Replace with `from supernet import SearchSpace, SuperNet`.

### [CRITICAL] 4. DDP `find_unused_parameters=True`
**auto-fixable**: yes
**Section**: §2 Distributed Setup
**Check**: `DistributedDataParallel` is constructed with `find_unused_parameters=True`.
**Verify**: grep for `DistributedDataParallel` and check kwargs.
**Anti-pattern**: Missing `find_unused_parameters` or set to `False`.
**Fix**: Add `find_unused_parameters=True` to DDP constructor. (Required because each sampled choice path activates only one branch per slot.)

### [CRITICAL] 5. DDP Unwrap Rule
**auto-fixable**: no
**Section**: §2 Distributed Setup
**Check**: Standard `nn.Module` methods (`forward()` / `model(x)`, `parameters()`, `train()`, `eval()`, `state_dict()`, `zero_grad()`) are called directly on the DDP-wrapped model, with no unwrapping needed. Only custom supernet attributes (`search_space`, `get_active_subnet()`) access the inner module via `unwrap_model()`. `set_sample_config` is called via `set_sample_config_ddp()`.
**Verify**: Read `train_supernet.py` training and validation loops. Confirm no manual unwrapping is used for standard `nn.Module` methods. Confirm custom attributes use `unwrap_model()`. Confirm `set_sample_config_ddp` is used instead of manual unwrap for configuring the supernet.
**Anti-pattern**: `unwrap_model(model)(inputs)` for forward pass; `model.search_space` on DDP wrapper.

### [MAJOR] 6. Rank-Gated I/O
**auto-fixable**: no
**Section**: §3 Progress Driver
**Check**: All single-writer side effects are guarded by `if is_main_process()` so that only rank 0 performs them. This includes:
- **Logging**: `print()`, `tqdm` output (`disable=not is_main_process()`), and any metric reporting
- **File writes**: any file output during training
  - Exception: `save_checkpoint_ddp()` does **not** need this guard (see item 23)
- **Directory creation**: `os.makedirs()` for output directories, log directories, etc.
**Verify**: grep for `print(`, `open(`, `os.makedirs`, and similar I/O calls. Confirm each is inside an `if is_main_process()` block or a helper that gates on rank 0. Confirm `save_checkpoint_ddp` is NOT inside such a guard.
**Anti-pattern**: Unguarded `print()` producing N duplicate lines; multiple ranks creating the same directory; wrapping `save_checkpoint_ddp()` inside `if is_main_process()` (deadlock, see item 23).

### [MINOR] 7. Device Placement Consistency
**auto-fixable**: no
**Section**: Validation (Device placement consistency)
**Check**: All tensors participating in the same operation reside on the same device. No cross-device operations. GPU/NPU tensors are moved to CPU before NumPy/Python scalar conversion, and tensors are not mixed with NumPy arrays in mathematical operations. Model tensor attributes are properly registered.
**Verify**: Read the data processing and metric calculation logic in `train_supernet.py` and its generated helper files. Check for operations mixing `np.ndarray` with `torch.Tensor`, and ensure any tensor converted via `.numpy()` is first moved to CPU. Also verify that any tensor assigned as an attribute of an `nn.Module` uses `nn.Parameter` or `register_buffer`.
**Anti-pattern**: `tensor.numpy()` without `.cpu()` first; mathematical operations combining `np.array` and `torch.Tensor`; assigning `self.my_tensor = torch.tensor(...)` inside an `nn.Module` without `register_buffer`.

### [MAJOR] 8. Data Pipeline — DDP Sampler (when `is_distributed()`)
**auto-fixable**: no
**Section**: §4 Data Pipeline
**Check**: When `is_distributed()` (torchrun multi-GPU), for map-style datasets `DistributedSampler` is used and `sampler.set_epoch(epoch)` is called at the beginning of each epoch. On single-device (default), plain sampler is used—no `DistributedSampler` needed.
**Verify**: If `is_distributed()` is used, grep for `DistributedSampler` and `set_epoch`. On single-device, verify plain sampler.

### [MAJOR] 9. No Hardcoded Paths In Data-Loading Code
**auto-fixable**: no
**Section**: §4 Data Pipeline
**Check**: All generated data-loading code (dataset classes, auxiliary loaders, etc.) accepts data paths as parameters. No function or class hardcodes or derives a path from a package location.
**Verify**: In generated helper files, grep for file-loading calls (`loadmat`, `np.load`, `open(`, `torch.load`, etc.) and confirm the file path traces back to a function/class parameter, not an internally constructed path.
**Anti-pattern**: A dataset class or loader function internally resolves a path from the installed package or repo layout instead of accepting it as a parameter.

### [MAJOR] 10. Real-Time Training Progress
**auto-fixable**: no
**Section**: §3 Progress Driver
**Check**: The training loop provides real-time progress feedback via periodic batch-level logging (rank 0 only). The primary approach is `tqdm` (`disable=not is_main_process()`): wrapping the batch iterator for epoch-based training, or a single bar tracking `global_step` for step-based training, with running metrics in `postfix`. If the user's original project environment is not suitable for `tqdm`, a periodic `print` statement (e.g. `if global_step % args.log_interval == 0:`) is used instead.
**Verify**: Check the training loop for a batch-level progress indicator (either `tqdm` or periodic `print`). Confirm it is disabled/gated on non-main ranks.
**Anti-pattern**: Logging only at epoch boundaries; `tqdm` or `print` enabled on all ranks producing duplicate logs.

### [MAJOR] 11. Progress Unit Consistency
**auto-fixable**: no
**Section**: §3 Progress Driver
**Check**: Training progress unit (epoch or `global_step`) is chosen from the user's project and used consistently for: training budget, `--eval_interval`, scheduler stepping, checkpoint save interval, logging interval, and final validation.
**Verify**: Identify the progress unit and confirm it's consistent across all uses.
**Anti-pattern**: Mixing epoch-based and step-based counting; forcing streaming data into artificial epochs.

### [CRITICAL] 12. Optimizer Receives Only Trainable Variant Parameters
**auto-fixable**: yes
**Section**: §6 Optimizer, Scheduler, AMP, And Gradient Clipping
**Check**: The optimizer is constructed over the trainable-parameter filter (e.g. `[p for p in model.parameters() if p.requires_grad]` or the equivalent collected variant-branch parameter list), **never** bare `model.parameters()`. Bare `model.parameters()` would push frozen original-branch and non-slot parameters into the optimizer and defeat the freeze grouping. The optimizer class is recipe-owned (AdamW default, `--lr` exposed); it is not required to match the user's original project.
**Verify**: Read the optimizer construction. Confirm the parameter list is the trainable filter. grep for `model.parameters()` appearing directly inside an optimizer constructor call — any hit fails this item.
**Anti-pattern**: `optim.AdamW(model.parameters(), lr=...)`; constructing the optimizer before applying the freeze grouping.
**Fix**: Apply the freeze grouping first, then pass the filtered trainable parameter list to the optimizer.

### [MAJOR] 13. Batch Size And Learning Rate Under DDP
**auto-fixable**: no
**Section**: §6 Batch Size & Learning Rate
**Check**: `--batch_size` is per-device; the effective batch size under DDP is `batch_size * world_size`. `args.lr` is passed directly to the optimizer using the user's original LR as the default starting point. If the user's original code includes DDP-aware LR scaling, that rule is reused. LR and batch-size values are exposed as CLI or launcher overrides.
**Verify**: Confirm `--batch_size` controls per-device samples. Inspect optimizer construction: LR handling should either faithfully port the user's original DDP scaling logic, or use `args.lr` directly if the original code has no scaling.
**Anti-pattern**: Introducing new LR scaling logic that the user's original code does not have; hardcoded LR or batch size without CLI override.

### [MAJOR] 14. Scheduler Coherence
**auto-fixable**: no
**Section**: §6 Optimizer, Scheduler, AMP, And Gradient Clipping
**Check**: If a scheduler is used, its step granularity fits the chosen progress unit (§3) and its total steps/epochs match the training budget. The scheduler is recipe-owned; it is not required to match the user's original project.
**Verify**: Read the scheduler configuration and confirm totals match the training budget and the progress unit.

### [CRITICAL] 15. AMP Autocast And GradScaler Decoupled
**auto-fixable**: yes
**Section**: §2 Distributed Setup
**Check**: Uses `autocast()` and `grad_scaler()` from `nas_agent.train.distributed`. The autocast enable flag is independent from `scaler.is_enabled()`, using `autocast(device, enabled=args.amp)` directly. GradScaler may be disabled on some devices but autocast should still follow the user's AMP setting.
**Verify**: grep for `autocast` and `grad_scaler` imports and usage. Confirm autocast enabled flag uses `args.amp` directly.
**Anti-pattern**: Coupling autocast enable to scaler state; using `torch.cuda.amp` directly instead of `nas_agent.train.distributed`.
**Fix**: Import `autocast` and `grad_scaler` from `nas_agent.train.distributed`. Replace direct `torch.cuda.amp` / `torch.amp` usage with `autocast(device, enabled=args.amp)` and `grad_scaler(device, enabled=args.amp)`.

### [CRITICAL] 16. NPU `foreach` Compatibility
**auto-fixable**: yes
**Section**: §6 NPU Compatibility
**Check**: `is_npu = device.type == "npu"` is set once after `setup_distributed()`. Both optimizer constructor and `clip_grad_norm_` pass `foreach=False if is_npu else None` or similar behavior.
**Verify**:
- grep for `is_npu` (should exist)
- grep for `foreach` (should appear in both optimizer and clipping contexts)
**Anti-pattern**: Missing `foreach` parameter; hardcoding `foreach=False` unconditionally.
**Fix**: Add `is_npu = device.type == "npu"` after device setup. Add `foreach=False if is_npu else None` to optimizer and `clip_grad_norm_` calls.

### [CRITICAL] 17. Gradient Clipping After KD Loss Backward
**auto-fixable**: no
**Section**: §6 Gradient Clipping, §8 Training Example
**Check**: Gradient clipping via `clip_grad_norm_` happens after the per-path KD loss has called `backward()` and BEFORE `optimizer.step()`. When AMP scaling is enabled, `scaler.unscale_(optimizer)` is called before clipping.
**Verify**: Read the training loop. Confirm clipping is between the `backward()` call and `scaler.step(optimizer)`.
**Anti-pattern**: Clipping before the loss `backward()` (too early); missing `scaler.unscale_` before clipping.

### [CRITICAL] 18. Choice Sampling: Sync Random Seed (guarded)
**auto-fixable**: no
**Section**: §8 Training Example
**Check**: Uses `sync_random_seed(device)` with a **guarded** implementation: `if not is_distributed(): return random.SystemRandom().randrange(...)` (single-device local random). When `is_distributed()` (multi-GPU), broadcasts rank0 seed so all ranks sample the identical choice path each step.
**Verify**: grep for `sync_random_seed`. Confirm it has `is_distributed()` guard (early return on single-device). Confirm it's called before `sample_choice_path`.
**Anti-pattern**: Unconditional `dist.broadcast` (crashes on single-device when process group not initialized); each rank sampling independently without synchronization.

### [CRITICAL] 19. Choice Sampling: Path Construction
**auto-fixable**: no
**Section**: §8 Choice Sampling
**Check**: `sample_choice_path(search_space, rng)` samples **one branch per slot from the slot's actual branch list** (canonical order: `original`, `vanilla`, `random_synthesizer`, `relu_attention`, `fnet`, `softs_star`) and returns a choice-only `ArchConfig`:
- The per-slot **choice** is the only sampled dimension; depth and layer dimensions are pinned to the original model's values and never sampled.
- The branch list is read from the search space, not a hardcoded copy of branch names.
- All randomness uses the provided `rng`, not global RNG.
**Verify**: Read the sampling helper. Confirm per-slot choice sampling from `search_space` branch data, choice-only config construction, and no depth/layer-dimension sampling calls.
**Anti-pattern**: Using `random.choice()` instead of `rng.choice()`; hardcoding branch names instead of reading the search space; sampling any layer-dimension field; constructing configs with non-choice fields.

### [CRITICAL] 20. Evaluation: K-Path Mean + All-Original Sanity
**auto-fixable**: no
**Section**: §9 Evaluation
**Check**: Evaluation follows the fixed protocol:
- **K-path mean (best-ckpt basis)**: K=8 choice paths sampled once with a **fixed seed derived from `args.seed`** — the same path set at every eval point — evaluated with the user's validation metric; the mean is the best-checkpoint basis.
- **All-original sanity**: the all-original path is evaluated separately at every eval point as a freeze-violation detector; it must stay ≈ constant across training, does not participate in best-ckpt selection, and drift must be surfaced (fail loud), not optimized.
**Verify**: Read the eval helpers. Confirm the K-path set is derived deterministically from the fixed seed (identical across eval points), the all-original path selects `original` for every slot, and the best-ckpt comparison uses the K-path mean.
**Anti-pattern**: Re-sampling different paths at each eval point (destroys cross-epoch comparability); using the all-original value as the best-ckpt basis; silently ignoring all-original drift.

### [CRITICAL] 21. DDP Metric Aggregation: AverageMeter
**auto-fixable**: no
**Section**: §3 Progress Driver, §9 DDP Metric Aggregation
**Check**:
- Uses `AverageMeter` from `nas_agent.train` for metric aggregation in **both** the training loop and the validation function.
- Exception (global reference-set metrics): when the validation metric cannot be computed per-batch (KNN / retrieval / embedding-matching metrics computed over a full reference set), the validation function uses the all-gather pattern instead: each rank collects its shard's embeddings, `all_gather` them, the main rank computes the metric over the full gathered set, and the result is broadcast to all ranks. This is the data & evaluation paradigm iron rule; do NOT force-fit such metrics into per-batch `AverageMeter` aggregation.
- `.avg` and `.count` trigger `all_reduce`, a collective operation that all ranks must call together. Every `.avg` / `.count` call must be outside any `if is_main_process():` guard.
- `.avg` returns a Python `float` (not a tensor). Any post-processing must use `math` / plain Python operations, not `torch.*` ops.
**Verify**:
- grep for `AverageMeter` import and usage in both the training loop and the validation function. When the validation metric is global reference-set (KNN / retrieval), grep for the all-gather pattern (`all_gather` / `all_gather_object`) in the validation function instead, and confirm the metric is computed on the main rank from full gathered data with the result broadcast.
- Confirm training metrics displayed in `tqdm` postfix or periodic `print` come from `AverageMeter.avg`, not raw per-rank values.
- Verify that every `.avg` or `.count` access is NOT inside an `if is_main_process():` block.
- Verify that `.avg` results are not passed to `torch.*` ops (`.avg` returns `float`).
**Anti-pattern**:
- Calling `.avg` inside `if is_main_process():` (causes multi-GPU deadlock).
- Passing `.avg` to `torch.*` ops like `torch.log10` (use `math.log10` instead).
- Per-rank training loss in `tqdm` without aggregation.
- Per-rank validation metrics without `all_reduce`.
- Computing `total_loss / num_batches` per rank then `all_reduce`-averaging (biased when ranks have different sample counts).
- Force-fitting a global reference-set metric (KNN / retrieval) into per-batch `AverageMeter` aggregation — that computes a different quantity than the user's metric (semantic deviation per the data & evaluation paradigm iron rule).

### [CRITICAL] 22. Fixed KD Recipe (loss composition + declared hidden-KD basis)
**auto-fixable**: no
**Section**: §8 KD Training Loop
**Check**: KD is the fixed training paradigm — always enabled, no enable/disable decision, no delayed-start scheduling. When auditing:
- **Loss composition**: `kd_loss = kd_hidden_weight * hidden_term + kd_logits_weight * logits_term` where hidden_term = mean over slots of `cosine_kd_loss` on per-slot layer outputs and logits_term = `logits_kd_loss` on final outputs. Uses the `nas_agent.train.distillation` helpers.
- **Pure KD**: the objective contains **no task/supervised loss term**; the user's original training loss is not ported into the objective.
- **Declared hidden-KD basis**: per-slot hidden cosine is a **declared, first-class component of the fixed recipe** — the teacher is the same-topology parent model (every slot's original branch IS the parent's layer), so teacher/student layer outputs align slot-by-slot by construction and no adapters are involved. Do NOT flag it as forbidden feature-level KD and do NOT replace it with a final-output-only loss.
- **Runtime shape guard** on the logits KD term (`student_outputs.shape == teacher_outputs.shape`).
- KD CLI args match the recipe: `--kd_hidden_weight`, `--kd_logits_weight`; `--kd_temperature` only when the KL helper accepts a temperature.
**Verify**: Read the loss construction. Confirm composition, helper usage, shape guard, absence of any task-loss term, and absence of KD enable/disable branching or delayed-start scheduling.
**Anti-pattern**: Adding the user's supervised loss into the objective; replacing hidden cosine with MSE "for stability"; gating KD behind a CLI switch; temperature passed to `cosine_kd_loss` (cosine KD has no temperature parameter); adding unused KD CLI args to the launcher.

### [CRITICAL] 23. Checkpoint Uses `save_checkpoint_ddp` Without Extra Rank Guard
**auto-fixable**: yes
**Section**: Checkpoint
**Check**: All checkpoint writes use `save_checkpoint_ddp` (not raw `save_checkpoint`). The function must be called by **all ranks** (not inside `if is_main_process()`) because it contains an internal barrier (see workflow §7). `epoch`, `global_step`, and `best_metric` are passed as keyword arguments.
**Verify**: Read the checkpoint save logic in `train_supernet.py`. Confirm `save_checkpoint_ddp` is NOT indented under `if is_main_process():`. Confirm `epoch=`, `global_step=`, and `best_metric=` kwargs are present.
**Anti-pattern**: Wrapping `save_checkpoint_ddp(...)` inside `if is_main_process():` (multi-GPU deadlock); using raw `save_checkpoint` with manual rank gate / unwrap / barrier.
**Fix**: Move `save_checkpoint_ddp(...)` outside any `if is_main_process():` guard. Add any missing keyword arguments.

### [MAJOR] 24. Latest Checkpoint Saved After Evaluation
**auto-fixable**: no
**Section**: §7 Checkpoint
**Check**: When evaluation is scheduled for the current epoch/step, `supernet_latest.pth` is saved **after** evaluation and `best_metric` update, not before. This ensures that resumed training uses an up-to-date `best_metric`.
**Verify**: Read the training loop. Confirm `save_checkpoint_ddp(..., best_metric=best_metric, ...)` for `supernet_latest.pth` appears after the evaluation block and after `best_metric` is updated.
**Anti-pattern**: Saving `supernet_latest.pth` before evaluation; `best_metric` in latest checkpoint is always one eval cycle behind.

### [CRITICAL] 25. Best Checkpoint Uses K-Path Mean Metric
**auto-fixable**: no
**Section**: §9 Evaluation
**Check**: `supernet_best.pth` is saved based on the **K-path mean** validation metric (§9 fixed protocol), using the user's metric direction. The best-metric comparison must use the globally aggregated metric (from `AverageMeter.avg`) so that all ranks reach the same save-or-skip decision for `save_checkpoint_ddp` (which contains an internal barrier).
**Verify**: Read the checkpoint save logic. Confirm the comparison uses the K-path mean (not the all-original value, not a single path). Confirm the metric comes from `AverageMeter.avg` (not a per-rank value), so all ranks agree on whether to save.
**Anti-pattern**: Using the all-original sanity value or a single sampled path for the best checkpoint decision; comparing against a per-rank metric that may differ across ranks, causing only some ranks to enter `save_checkpoint_ddp` (deadlock).

### [MAJOR] 26. Intermediate Snapshot Saves
**auto-fixable**: no
**Section**: §7 Checkpoint
**Check**: Besides `supernet_latest.pth` and `supernet_best.pth`, the training loop saves periodic intermediate snapshots (e.g. `supernet_epoch_<epoch:04d>.pth` or `supernet_step_<global_step:08d>.pth`) at a configurable interval controlled by a CLI parameter (e.g. `--save_interval`).
**Verify**: Read the training loop and argparse block. Confirm an interval-based snapshot save exists and is controlled by a CLI argument with a reasonable default.
**Anti-pattern**: Only saving `latest` and `best` without any intermediate snapshots; hardcoding the save interval without CLI control.

### [CRITICAL] 27. Self-Contained Generated Training Artifacts
**auto-fixable**: no
**Section**: Source Evidence
**Check**: `train_supernet.py` and generated helper files do not import modules from `<user_project_root>`. Any required project-specific dataset, preprocessing, collate, evaluation, metric, wrapper, or checkpoint logic is copied and adapted into files under `<output_dir>`.
**Verify**: Inspect imports in `train_supernet.py` and helper files. Check for imports that reference the original project package, absolute project paths, `sys.path` insertion, or `PYTHONPATH` assumptions.
**Anti-pattern**: `sys.path.append(<user_project_root>)`; `from user_project.datasets import ...`; helper files that only work when launched from the original project root.

### [CRITICAL] 28. Budget: Single-Path Distillation Baseline
**auto-fixable**: no
**Section**: §6 Optimizer, Scheduler, AMP, And Gradient Clipping
**Check**: The training budget (total epochs or total optimizer steps) starts from the user's original training budget (same order of magnitude) and is tuned for convergence — each optimizer step costs one student path forward plus one teacher `no_grad` forward. There is **no fixed budget multiplier**; any deviation from the user's original budget must be justified in the summary.
**Verify**: Compare the training budget in `run_train_supernet.sh` (or `train_supernet.py` defaults) with the original project's budget. Confirm the chosen value is justified.
**Anti-pattern**: Scaling the budget by an arbitrary factor without justification; copying the user's budget verbatim while silently assuming a different per-step cost.

### [CRITICAL] 29. Budget-Dependent Hyperparameters Coherent
**auto-fixable**: no
**Section**: §6, Validation (Budget-hyperparameter coherence)
**Check**: All budget-dependent hyperparameters are coherent with the chosen training budget:
- LR scheduler total steps/epochs
- Decay milestones
- Any other budget-coupled parameters
**Verify**: Read scheduler configuration in `train_supernet.py`. Confirm total steps/milestones match the training budget, not values from a different horizon.
**Anti-pattern**: Budget extended but scheduler milestones left at the original values.

### [MAJOR] 30. Launcher Editable Variables Complete
**auto-fixable**: no
**Section**: Run Launcher
**Check**: The launcher exposes key training parameters as editable shell variables at the top: `DATA_DIR`, `OUTPUT_DIR`, training budget, `BATCH_SIZE`, `LR`, `NUM_WORKERS`, `EVAL_INTERVAL`, `SEED`, `MAX_GRAD_NORM`, `MAX_TRAIN_STEPS` (default `0`), `PRETRAINED_CKPT`, `KD_HIDDEN_WEIGHT`, `KD_LOGITS_WEIGHT`, `AMP`. `NUM_WORKERS` defaults to `0` (DataLoader Launch Hygiene, item 36). `AMP` defaults to `false` (single-device default).
**Verify**: Read the editable variables section of `run_train_supernet.sh`.
**Anti-pattern**: Hardcoding values in the launch command instead of using variables; missing key variables (especially `PRETRAINED_CKPT`); including `NNODES`/`NPROC_PER_NODE`/`MASTER_PORT` (default is single-process, no torchrun).

### [MAJOR] 31. Launcher Uses Plain `python3` (Single-Device Default)
**auto-fixable**: yes
**Section**: §2 Distributed Setup, Run Launcher
**Check**: Launcher uses plain `python3 train_supernet.py` as the default (single-device, no torchrun). DDP wrap in the script is conditional on `is_distributed()`. Multi-GPU is available by switching the launcher to `torchrun --nproc_per_node=N`.
**Verify**: grep for `python3 train_supernet.py` in `run_train_supernet.sh`. Verify no **active** (non-comment) `torchrun` invocation exists in the default launcher — full-line comments mentioning the multi-GPU switch are allowed.
**Anti-pattern**: Using `torchrun` in the default launcher; using `python -m torch.distributed.launch` (deprecated).
**Fix**: Replace torchrun with plain `python3 train_supernet.py`.

### [MAJOR] 32. Boolean Flag Handling
**auto-fixable**: yes
**Section**: Run Launcher
**Check**: Boolean flags like `--amp` are handled correctly in the launcher: only passed when the shell variable is true, omitted otherwise. Uses `store_true` pattern.
**Verify**: Check that `AMP_FLAG` (or equivalent) is conditionally set and appended.
**Anti-pattern**: Always passing `--amp true` or `--amp false` instead of presence/absence pattern.
**Fix**: Use conditional flag pattern: `AMP_FLAG=""; [ "$AMP" = true ] && AMP_FLAG="--amp"`.

### [CRITICAL] 33. Launcher CLI Flags Match Argparse
**auto-fixable**: yes
**Section**: Run Launcher, Validation (Launcher-script CLI consistency)
**Check**: Every `--flag` in the `python3` invocation inside `run_train_supernet.sh` corresponds to an argument that `train_supernet.py` actually accepts. No extra flags, no missing flags.
**Verify**: Extract all `--flag_name` from `run_train_supernet.sh` python3 block. Extract all `add_argument('--flag_name')` from `train_supernet.py`. Compare the two lists.
**Anti-pattern**: Launcher passes `--learning_rate` but script defines `--lr`; launcher passes a flag the script doesn't define.
**Fix**: Rename the mismatched flags in `run_train_supernet.sh` to match `train_supernet.py` argparse definitions.

### [CRITICAL] 34. Launcher Shell Syntax Valid
**auto-fixable**: yes
**Section**: Run Launcher
**Check**: `run_train_supernet.sh` passes `bash -n` syntax check.
**Verify**: Run `bash -n run_train_supernet.sh` (syntax-only, no execution) to check shell syntax.
**Fix**: Fix shell syntax errors found by `bash -n`.

### [CRITICAL] 35. Launcher Is Executable
**auto-fixable**: yes
**Section**: Run Launcher
**Check**: `run_train_supernet.sh` has executable permission.
**Verify**: Check file permissions.
**Fix**: `chmod +x run_train_supernet.sh`.

### [CRITICAL] 36. DataLoader Launch Hygiene
**auto-fixable**: yes
**Section**: §4 Data Pipeline (DataLoader Launch Hygiene)
**Check**: All generated DataLoaders use `num_workers=0` and `pin_memory=False`. The launcher's `NUM_WORKERS` variable defaults to `0` and is not raised without justification. No DataLoader enables pin memory.
**Verify**: grep for `DataLoader(` in `train_supernet.py` and generated helper files; confirm `num_workers=0` (or default) and `pin_memory=False` on every constructor. Confirm `NUM_WORKERS=0` in `run_train_supernet.sh`.
**Anti-pattern**: `num_workers>0` on CUDA training boxes (fork worker crashes after CUDA init); `pin_memory=True` (CUDA tensors cannot be pinned).
**Fix**: Set `num_workers=0`, `pin_memory=False` on every DataLoader; reset launcher default to `0`.

### [N/A] 37. Rendezvous Port Uniqueness (multi-GPU only)
**auto-fixable**: yes
**Section**: Run Launcher
**Check**: The default launcher uses plain `python3` (no torchrun, no MASTER_PORT needed). When switching to `torchrun --nproc_per_node=N` for multi-GPU, the user should set `--master_port` to a unique port. This item only applies to the torchrun path.
**Verify**: Default launcher should NOT contain an active `MASTER_PORT` or `torchrun` line (full-line comments excluded). If torchrun is used, verify `--master_port` is set.
**Anti-pattern**: Including `MASTER_PORT` / `torchrun` in the default single-device launcher.

### [CRITICAL] 38. Progress JSONL Write Loop (chart feed)
**auto-fixable**: no
**Section**: §3 Progress Driver (machine-parseable progress, feed b)
**Check**: `train_supernet.py` writes the progress JSONL chart feed consumed by the live chart watcher (`progress_watcher.py`) at **step granularity**: a line is appended **every `--progress-every` optimizer steps (default 50) AND at every progress-unit boundary** (epoch/step end, including the final partial unit). Each line (rank 0) is `{"step": <global_step>, "metrics": {"<name>": <float>, ...}}` written via `json.dumps(row) + "\n"` + `flush()`, guarded by `if is_main_process()`. `metrics` contains the scalars accumulated since the last written line under real names (KD recipe terms `kd_hidden_loss`/`kd_logits_loss`/`kd_loss` as window means + instantaneous `lr`; validation metrics under the user's real names on eval lines, including `val_<metric>_kpath_mean` and `val_<metric>_all_original` per §9).
**Verify**: grep `train_supernet.py` for `progress.jsonl` AND `json.dumps`. Confirm a `--progress-every`-style arg (or an equivalent step-modulo write condition) exists and the write is inside the batch loop (not only at eval/unit boundaries) and inside an `is_main_process()` gate. If `progress.jsonl` is absent, written only inside the eval block, or written only once per progress unit (per-epoch-only feed = too sparse for a live curve), this item fails.
**Anti-pattern**: No `progress.jsonl` write at all (training executes but the live chart has no data); writing the JSONL only at eval or epoch boundaries instead of every N steps (5 epochs → 5 points is not a convergence curve); emitting metrics under fabricated names instead of the recipe's / user's real metric names; writing outside an `is_main_process()` guard (multi-rank races).

---

## PSU Fixed-Paradigm Items

Items 39 through 42 verify the fixed KD paradigm's project-independent contracts. All are statically greppable in `train_supernet.py`.

### [CRITICAL] 39. Weight Inheritance And Freeze Grouping
**auto-fixable**: no
**Section**: §5 Model Construction And Weight Inheritance
**Check**:
- Original branches inherit the parent weights from the pretrained checkpoint (loading goes through the `load_pretrained.py` asset; no hand-rolled second loading path).
- Variant branches keep their random initialization.
- Freeze grouping: original branches and non-slot modules are frozen via `requires_grad_(False)`; after construction exactly the variant-branch parameters have `requires_grad=True`.
**Verify**: grep for `load_pretrained` import and `requires_grad_(False)`. Read the grouping code and confirm the invariant (trainable set = variant-branch parameters only).
**Anti-pattern**: Re-initializing original branches; freezing variant branches; hand-rolled `torch.load` + `load_state_dict` bypassing `load_pretrained.py`; freezing nothing and training all parameters.

### [CRITICAL] 40. Teacher: Independent Frozen Instance
**auto-fixable**: no
**Section**: §5 Teacher, §8 Teacher
**Check**: The teacher is built via `build_pretrained_model()` from `load_pretrained.py` — an independent instance of the pretrained original model, **not extracted from the supernet** and never modified. `teacher.eval()` is called, all teacher parameters have `requires_grad_(False)`, and every teacher forward runs inside `torch.no_grad()`. Teacher parameters never enter the optimizer.
**Verify**: grep for `build_pretrained_model`, `teacher.eval()`, `no_grad`, and `requires_grad_(False)` in the teacher construction block. Confirm no `get_active_subnet()`-based teacher extraction.
**Anti-pattern**: Teacher = the supernet's all-original path forward; teacher parameters in the optimizer; teacher in `train()` mode; teacher forward outside `no_grad` (backprop builds graph through frozen weights — wasted memory).

### [CRITICAL] 41. Full-Module Checkpoint Save Contract
**auto-fixable**: no
**Section**: §7 Checkpoint
**Check**: The supernet checkpoint stores the full module state_dict — frozen original branches and non-slot modules **included**. Filtering the state_dict by `requires_grad` when saving is forbidden: the downstream search evaluator loads this checkpoint with strict key matching, and a filtered checkpoint fails loud or silently under-represents the supernet.
**Verify**: grep for `save_checkpoint_ddp` (serializes `model.state_dict()` whole). Inspect any manual `state_dict` manipulation — a line combining `state_dict` with a `requires_grad` filter fails this item.
**Anti-pattern**: `model.state_dict()` filtered through `requires_grad` before saving; saving only `optimizer.param_groups` parameters; custom per-branch checkpoints that omit frozen modules.

### [CRITICAL] 42. Startup Deterministic Assertions
**auto-fixable**: no
**Section**: §5 Startup Deterministic Assertions
**Check**: Before the training loop, the script runs (fail loud — raise, never warn-and-continue):
1. **Original-branch inheritance spot-check**: a deterministic sample of original-branch parameters compared against the teacher's parameters via `torch.allclose`; mismatch raises with the list of mismatched entries.
2. **Teacher forward smoke**: one `torch.no_grad()` teacher forward on a dummy batch.
**Verify**: grep for `allclose` and the assertion/raise in the startup block; confirm both assertions precede the training loop and raise on failure.
**Anti-pattern**: Inheritance verification deferred to "first eval"; teacher smoke omitted; mismatch only logged (training continues with silently misaligned weights).

# Checklist: Retrain Script Generation

Companion to: `workflows/retrain_script_generation.md`

## How To Use

Each item below is a verifiable requirement from the companion workflow. Verify items in order. For items marked `auto-fixable: yes`, fix the artifact directly. For items marked `auto-fixable: no`, report the issue for the caller.

**DDP-specific items** (DistributedDataParallel, DistributedSampler, all_reduce, `sync_random_seed` broadcast) apply **only when `is_distributed()`** (torchrun multi-GPU). On single-device (default, plain `python3`), these are not required — the script correctly skips DDP wrap, uses a plain sampler, and aggregation is a no-op. Items referencing these are evaluated as "if DDP is active, does the script do X correctly?"

**Definitions:**
- `<user_project_root>`: The path to the user's original PyTorch project repository.
- `<output_dir>`: The directory where the retrain artifacts (`retrain.py`, `finetune.py`, `run_retrain.sh`) are generated.

## Items

### [CRITICAL] 1. Stable Base CLI Contract
**auto-fixable**: no
**Section**: §1 CLI And Runtime Args
**Check**: `retrain.py` exposes the stable base CLI: `--output_dir` (default `runs/retrain`), `--eval_interval`, `--device` with choices `["auto", "cuda", "npu", "cpu"]`, `--amp`, `--lr`, `--max_grad_norm`, `--seed`, `--resume`. `--supernet_ckpt` is present iff the strategy is `finetune-from-supernet`. Project-derived training arguments are CLI overrides, not hardcoded remote-only literals.
**Verify**: Inspect the argparse block statically. Confirm required flags exist with compatible defaults and choices. Do NOT run the script.
**Anti-pattern**: Missing `--device`; hardcoded dataset path without CLI override; `--supernet_ckpt` present on a `train-from-scratch` script.

### [CRITICAL] 2. Strategy Coherence
**auto-fixable**: no
**Section**: Strategy Decision, §5 Subnet Extraction
**Check**: The generated file set matches the decided strategy. `finetune-from-supernet` → `retrain.py` + `finetune.py` + `run_retrain.sh` with `SUPERNET_CKPT` + `--supernet_ckpt`. `train-from-scratch` → `retrain.py` + `run_retrain.sh` only, no `finetune.py`, no `SUPERNET_CKPT` / `--supernet_ckpt`.
**Verify**: List generated files; grep `run_retrain.sh` for `SUPERNET_CKPT` / `--supernet_ckpt`; grep for `finetune.py`. Confirm presence/absence matches `retrain_strategy`.
**Anti-pattern**: `train-from-scratch` script that still references `--supernet_ckpt`; `finetune-from-supernet` script missing `finetune.py`.

### [CRITICAL] 3. Subnet Extraction Via Exposed API
**auto-fixable**: yes
**Section**: §5 Subnet Extraction
**Check**: `retrain.py`/`finetune.py` import `SearchSpace`, `ArchConfig`, `SuperNet` from `supernet.py` as a plain sibling import and call only manifest-exposed APIs (`set_sample_config` / `get_active_subnet`). The selected architecture is constructed from the rendered `SELECTED_ARCH` dict (Jinja `{{ ns3_run_search.output.selected_arch }}`), not a hardcoded/fabricated config. The supernet is deleted + `empty_cache` after extraction.
**Verify**: grep `from supernet import` in `retrain.py`/`finetune.py`. Confirm `ArchConfig(**SELECTED_ARCH)` / `set_sample_config` / `get_active_subnet` are used. Confirm no hardcoded subnet topology.
**Anti-pattern**: Hardcoding layer widths/depths instead of using the selected arch dict; reaching into supernet internals; leaving the supernet in memory during training.
**Fix**: Replace with the exposed-API extraction pattern from §5.

### [CRITICAL] 4. Strategy-Specific Weight Initialization
**auto-fixable**: no
**Section**: §5 Subnet Extraction
**Check**: `finetune-from-supernet` → `build_selected_subnet` is called WITH `supernet_ckpt=args.supernet_ckpt` (subnet inherits trained weights). `train-from-scratch` → `build_selected_subnet` is called with `supernet_ckpt=None` AND the extracted subnet weights are re-initialized using the same init logic as `evaluator.py`'s `train_from_scratch` path + the original project.
**Verify**: Read the extraction call site. For `train-from-scratch`, confirm an explicit re-initialization step after extraction (grep for the init helper / `reset_parameters` / the evaluator's reset logic).
**Anti-pattern**: `train-from-scratch` that forgets to drop `supernet_ckpt` and accidentally inherits weights; `train-from-scratch` that extracts but never re-initializes; `finetune-from-supernet` that loads the wrong ckpt path.

### [MAJOR] 5. Distributed And Checkpoint Imports
**auto-fixable**: yes
**Section**: §2 Distributed Setup, §7 Checkpoint
**Check**: Uses `setup_distributed`, `get_local_rank`, `get_rank`, `is_main_process`, `torch_manual_seed`, `unwrap_model`, `autocast`, `grad_scaler` from `nas_agent.train.distributed`. Uses `save_checkpoint_ddp` from `nas_agent.train`.
**Verify**: grep for `from nas_agent.train.distributed import` and `from nas_agent.train import` and confirm the needed symbols are imported.
**Fix**: Add missing imports.

### [CRITICAL] 6. DDP `find_unused_parameters=True`
**auto-fixable**: yes
**Section**: §2 Distributed Setup
**Check**: `DistributedDataParallel` is constructed with `find_unused_parameters=True` (when `is_distributed()`).
**Verify**: grep for `DistributedDataParallel` and check kwargs.
**Fix**: Add `find_unused_parameters=True`.

### [MAJOR] 7. DDP Unwrap Rule
**auto-fixable**: no
**Section**: §2 Distributed Setup
**Check**: Standard `nn.Module` methods (`forward()`, `parameters()`, `train()`, `eval()`, `state_dict()`, `zero_grad()`) are called directly on the DDP-wrapped model. Only custom supernet attributes access the inner module via `unwrap_model()`.
**Verify**: Read the training and validation loops. Confirm no manual unwrapping for standard methods.
**Anti-pattern**: `unwrap_model(model)(inputs)` for a forward pass.

### [MAJOR] 8. Rank-Gated I/O
**auto-fixable**: no
**Section**: §3 Progress Driver
**Check**: All single-writer side effects are guarded by `if is_main_process()`: `print()`, `tqdm` output, file writes (exception: `save_checkpoint_ddp`), directory creation.
**Verify**: grep for `print(`, `open(`, `os.makedirs` and confirm each is inside an `is_main_process()` gate (or a helper that gates). Confirm `save_checkpoint_ddp` is NOT inside such a guard.
**Anti-pattern**: Unguarded `print()` producing N duplicate lines; wrapping `save_checkpoint_ddp()` inside `if is_main_process()` (deadlock).

### [MINOR] 9. Device Placement Consistency
**auto-fixable**: no
**Section**: Validation (Device placement consistency)
**Check**: All tensors in the same operation reside on the same device. GPU/NPU tensors moved to CPU before NumPy/Python scalar conversion. Model tensor attributes registered via `nn.Parameter` / `register_buffer`.
**Verify**: Read data processing and metric logic. Check for `np.ndarray` mixed with `torch.Tensor`; verify `.numpy()` is preceded by `.cpu()`.

### [MAJOR] 10. Data Pipeline — DDP Sampler (when `is_distributed()`)
**auto-fixable**: no
**Section**: §4 Data Pipeline
**Check**: When `is_distributed()`, map-style datasets use `DistributedSampler` and `sampler.set_epoch(epoch)` is called each epoch. On single-device, a plain sampler is used.
**Verify**: If `is_distributed()`, grep for `DistributedSampler` and `set_epoch`.

### [MAJOR] 11. No Hardcoded Paths In Data-Loading Code
**auto-fixable**: no
**Section**: §4 Data Pipeline
**Check**: All generated data-loading code accepts data paths as parameters. No function or class hardcodes or derives a path from a package location.
**Verify**: grep for file-loading calls (`np.load`, `open(`, `torch.load`, etc.) and confirm paths trace back to a parameter.

### [MAJOR] 12. Real-Time Training Progress
**auto-fixable**: no
**Section**: §3 Progress Driver
**Check**: The training loop provides batch-level progress feedback (rank 0 only) via `tqdm` (`disable=not is_main_process()`) or periodic `print`.
**Verify**: Check the loop for a batch-level indicator. Confirm it is disabled/gated on non-main ranks.
**Anti-pattern**: Logging only at epoch boundaries.

### [MAJOR] 13. Progress Unit Consistency
**auto-fixable**: no
**Section**: §3 Progress Driver
**Check**: The progress unit (epoch or `global_step`) is used consistently for: training budget, `--eval_interval`, scheduler stepping, checkpoint save interval, logging interval, final validation.
**Verify**: Identify the progress unit and confirm consistency across uses.
**Anti-pattern**: Mixing epoch-based and step-based counting.

### [MAJOR] 14. Optimizer And Scheduler Type From User Project
**auto-fixable**: no
**Section**: §6 Optimizer, Scheduler, AMP, And Gradient Clipping
**Check**: The optimizer class and scheduler class match the user's original training code. Workflow template example types are not copied verbatim.
**Verify**: Compare optimizer/scheduler construction against the user's original code under `<user_project_root>`.
**Anti-pattern**: Using `AdamW` when the user uses `Adam`; copying workflow template defaults without checking.

### [MAJOR] 15. Batch Size And Learning Rate Under DDP
**auto-fixable**: no
**Section**: §6 Batch Size & Learning Rate
**Check**: `--batch_size` is per-device; effective batch under DDP is `batch_size * world_size`. `args.lr` passed directly; any DDP LR scaling reuses the user's original rule.
**Verify**: Confirm per-device semantics and LR handling.

### [MAJOR] 16. Scheduler Step Granularity Preserved
**auto-fixable**: no
**Section**: §6 LR Scheduler
**Check**: `scheduler.step()` is called at the same granularity as the original project (per-epoch vs per-batch).
**Verify**: Compare scheduler step placement with the user's original code.

### [CRITICAL] 17. AMP Autocast And GradScaler Decoupled
**auto-fixable**: yes
**Section**: §2 Distributed Setup
**Check**: Uses `autocast()` and `grad_scaler()` from `nas_agent.train.distributed`. The autocast enable flag is independent from `scaler.is_enabled()`: `autocast(device, enabled=args.amp)`.
**Verify**: grep for `autocast` and `grad_scaler`. Confirm the enable flag uses `args.amp` directly.
**Fix**: Import from `nas_agent.train.distributed`; replace `torch.cuda.amp` / `torch.amp` with `autocast(device, enabled=args.amp)`.

### [CRITICAL] 18. NPU `foreach` Compatibility
**auto-fixable**: yes
**Section**: §6 NPU Compatibility
**Check**: `is_npu = device.type == "npu"` set once after `setup_distributed()`. Both optimizer constructor and `clip_grad_norm_` pass `foreach=False if is_npu else None`.
**Verify**: grep for `is_npu` and `foreach`.
**Fix**: Add `is_npu` and `foreach=False if is_npu else None` to optimizer + clipping.

### [CRITICAL] 19. Gradient Clipping After Backward, Before Step
**auto-fixable**: no
**Section**: §8 Training Loop, §6 Gradient Clipping
**Check**: `clip_grad_norm_` happens after `loss.backward()` (and `scaler.unscale_(optimizer)` when AMP is enabled) and BEFORE `scaler.step(optimizer)`.
**Verify**: Read the training loop. Confirm clip placement between backward and step.
**Anti-pattern**: Clipping before backward; missing `scaler.unscale_` before clipping under AMP.

### [CRITICAL] 20. Final-ckpt Contract Path
**auto-fixable**: no
**Section**: §7 Checkpoint
**Check**: The final best checkpoint is written to `<output_dir>/retrain_best.pth` where `<output_dir>` resolves to `$ORCA_ARTIFACTS_DIR/runs/retrain` (i.e. the contractual `$ORCA_ARTIFACTS_DIR/runs/retrain/retrain_best.pth`). This path is consumed by `ns3_retrain`'s `status.sh` / `emit_result.py`.
**Verify**: Read checkpoint save logic. Confirm `retrain_best.pth` lands under `runs/retrain/`.
**Anti-pattern**: Writing the best ckpt to a differently named file or a different directory; breaking completion detection downstream.

### [CRITICAL] 21. Checkpoint Uses `save_checkpoint_ddp` Without Extra Rank Guard
**auto-fixable**: yes
**Section**: §7 Checkpoint
**Check**: All checkpoint writes use `save_checkpoint_ddp` (not raw `save_checkpoint`), called by all ranks (not inside `if is_main_process()`). `epoch`, `global_step`, `best_metric` passed as keyword arguments.
**Verify**: Confirm `save_checkpoint_ddp` is NOT indented under `if is_main_process():`. Confirm kwargs present.
**Fix**: Move outside the rank guard; add missing kwargs.

### [MAJOR] 22. Latest Checkpoint Saved After Evaluation
**auto-fixable**: no
**Section**: §7 Checkpoint
**Check**: When evaluation is scheduled for the current progress unit, `retrain_latest.pth` is saved AFTER evaluation and `best_metric` update.
**Verify**: Confirm `retrain_latest.pth` save appears after the eval block.
**Anti-pattern**: Saving `retrain_latest.pth` before evaluation.

### [CRITICAL] 23. Best Checkpoint Uses Validation Metric
**auto-fixable**: no
**Section**: §9 Evaluation
**Check**: `retrain_best.pth` is saved based on the validation metric using the user's metric direction (maximize accuracy/F1/mAP; minimize loss/error/WER). The comparison uses the globally aggregated metric (`AverageMeter.avg`) so all ranks reach the same save/skip decision.
**Verify**: Read the best-ckpt logic. Confirm metric + direction match the manifest; confirm `.avg` (not a per-rank value) drives the decision.

### [MAJOR] 24. Intermediate Snapshot Saves
**auto-fixable**: no
**Section**: §7 Checkpoint
**Check**: Besides latest/best, the loop saves periodic snapshots (`retrain_epoch_<n>.pth` / `retrain_step_<n>.pth`) at a CLI-controlled interval (`--save_interval`).
**Verify**: Confirm an interval-based snapshot save exists with a CLI argument and reasonable default.

### [CRITICAL] 25. Self-Contained Generated Artifacts
**auto-fixable**: no
**Section**: Source Evidence
**Check**: `retrain.py`, `finetune.py`, and helper files do not import modules from `<user_project_root>`. Required project logic is copied/adapted into `<output_dir>`.
**Verify**: Inspect imports. Check for project-package imports, absolute project paths, `sys.path` insertion, `PYTHONPATH` assumptions.
**Anti-pattern**: `sys.path.append(<user_project_root>)`; `from user_project.datasets import ...`.

### [CRITICAL] 26. Progress JSONL Write Loop (chart feed)
**auto-fixable**: no
**Section**: §3 Progress Driver (machine-parseable progress, feed b)
**Check**: `retrain.py` (or `finetune.py`) writes the progress JSONL chart feed consumed by `ns3_retrain`'s live chart watcher. Each progress unit (rank 0) appends exactly one line `{"step": <number>, "metrics": {"<name>": <float>, ...}}` to `$ORCA_ARTIFACTS_DIR/runs/retrain/progress.jsonl` via `json.dumps(row) + "\n"` + `flush()`, guarded by `if is_main_process()`. `metrics` contains every scalar metric the unit produces under its real name (`loss` is NOT assumed).
**Verify**: grep for `progress.jsonl` AND `json.dumps`. Confirm the write is inside the per-unit loop and an `is_main_process()` gate. This mirrors the static gate in `check_retrain_script.sh` §4; the runtime content check is `ns3_retrain`'s `check_progress_contract.py`.
**Anti-pattern**: No `progress.jsonl` write (retrain executes but live chart empty); writing only at eval boundaries; a fabricated `loss` name instead of the user's real metrics.

### [CRITICAL] 27. Budget Rule — Read Original Project, No Guess
**auto-fixable**: no
**Section**: §6 Budget rule
**Check**: The training budget (epochs/steps), optimizer, and scheduler are read from the original project's training code, not guessed and not copied from `evaluator.py`'s reduced search-time budget. Strategy-specific: `finetune-from-supernet` uses a moderate budget; `train-from-scratch` uses a full budget. Evaluation uses the full validation set (no subsampling).
**Verify**: Compare the budget in `run_retrain.sh` / `retrain.py` defaults with the original project. Confirm it is not the search-time `evaluator_cfg.epochs`.
**Anti-pattern**: Reusing the reduced search budget; guessing an epoch count; subsampling the validation set.

---

## Launcher And Budget

Items 28 through 34 verify `run_retrain.sh` and budget/launcher coherence.

### [MAJOR] 28. Launcher Editable Variables Complete
**auto-fixable**: no
**Section**: Run Launcher
**Check**: `run_retrain.sh` exposes editable shell variables at the top: `DATA_DIR`, `OUTPUT_DIR`, training budget (`EPOCHS` or `MAX_STEPS`), `BATCH_SIZE`, `LR`, `NUM_WORKERS`, `EVAL_INTERVAL`, `SEED`, `MAX_GRAD_NORM`, `AMP`. `NUM_WORKERS` defaults to `0`. `AMP` defaults to `false`. For `finetune-from-supernet`, also `SUPERNET_CKPT`.
**Verify**: Read the editable variables section.
**Anti-pattern**: Hardcoding values in the launch command; missing key variables; including `NNODES`/`NPROC_PER_NODE`/`MASTER_PORT` (default is single-process, no torchrun).

### [MAJOR] 29. Launcher Uses Plain `python3` (Single-Device Default)
**auto-fixable**: yes
**Section**: §2 Distributed Setup, Run Launcher
**Check**: Launcher uses plain `python3 retrain.py` as the default. DDP wrap is conditional on `is_distributed()`. Multi-GPU via switching to `torchrun --nproc_per_node=N`.
**Verify**: grep for `python3 retrain.py`. Verify `torchrun` is NOT present in the default launcher.
**Fix**: Replace torchrun with plain `python3 retrain.py`.

### [MAJOR] 30. Boolean Flag Handling
**auto-fixable**: yes
**Section**: Run Launcher
**Check**: Boolean flags like `--amp` are handled via presence/absence (`store_true`): `AMP_FLAG=""; [ "$AMP" = true ] && AMP_FLAG="--amp"`.
**Fix**: Use the conditional flag pattern.

### [CRITICAL] 31. Launcher CLI Flags Match Argparse
**auto-fixable**: yes
**Section**: Run Launcher
**Check**: Every `--flag` in the `python3` invocation corresponds to an argument `retrain.py` accepts. No extra/missing flags.
**Verify**: Extract `--flag_name` from `run_retrain.sh`; extract `add_argument('--flag_name')` from `retrain.py`; compare.
**Fix**: Rename mismatched flags in `run_retrain.sh` to match `retrain.py`.

### [CRITICAL] 32. Launcher Shell Syntax Valid
**auto-fixable**: yes
**Section**: Run Launcher
**Check**: `run_retrain.sh` passes `bash -n`.
**Fix**: Fix shell syntax errors.

### [CRITICAL] 33. Launcher Is Executable
**auto-fixable**: yes
**Section**: Run Launcher
**Check**: `run_retrain.sh` has executable permission.
**Fix**: `chmod +x run_retrain.sh`.

### [CRITICAL] 34. DataLoader Launch Hygiene
**auto-fixable**: yes
**Section**: §4 Data Pipeline (DataLoader Launch Hygiene)
**Check**: All generated DataLoaders use `num_workers=0` and `pin_memory=False`. The launcher's `NUM_WORKERS` defaults to `0`. No DataLoader enables pin memory.
**Verify**: grep `DataLoader(`; confirm `num_workers=0` and `pin_memory=False` on every constructor. Confirm `NUM_WORKERS=0` in `run_retrain.sh`.
**Fix**: Set `num_workers=0`, `pin_memory=False`; reset launcher default to `0`.

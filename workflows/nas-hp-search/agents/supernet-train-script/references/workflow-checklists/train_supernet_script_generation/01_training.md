# Checklist: Train Supernet Script — Training Logic

Companion to: `workflows/train_supernet_script_generation.md`

## How To Use

Each item below is a verifiable requirement extracted from the companion workflow's training logic sections (§1–§9). Verify items in order. For items marked `auto-fixable: yes`, fix the artifact directly. For items marked `auto-fixable: no`, report the issue for the caller.

**Definitions:**
- `<user_project_root>`: The path to the user's original PyTorch project repository containing the original training loop, data pipeline, and original model definitions.
- `<output_dir>`: The directory where the training artifacts (e.g., `train_supernet.py`, `run_train_supernet.sh`) are being generated.

## Items

### [MAJOR] 1. Stable Base CLI Contract
**auto-fixable**: yes
**Section**: §1 CLI And Runtime Args
**Check**: `train_supernet.py` exposes the stable base CLI required by the workflow: `--output_dir`, `--eval_interval`, `--device` with choices `["auto", "cuda", "npu", "cpu"]`, `--amp`, `--max_grad_norm`, `--sandwich_n_random`, `--seed`, `--kd_weight`, `--kd_warmup_start`, and `--kd_warmup_length`. Project-derived training arguments are exposed as CLI overrides rather than hardcoded remote-only literals.
**Verify**: Inspect the argparse block in `train_supernet.py` statically. Confirm required flags exist with compatible defaults and choices. Do NOT run the script.
**Anti-pattern**: Missing `--device`; hardcoded dataset/config path without CLI override; `--amp` implemented as a string argument instead of a boolean flag.
**Fix**: Add missing argparse entries and wire them into the runtime instead of hardcoded values.

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
**Fix**: Add `find_unused_parameters=True` to DDP constructor.

### [CRITICAL] 5. DDP Unwrap Rule
**auto-fixable**: yes
**Section**: §2 Distributed Setup
**Check**: Standard `nn.Module` methods (`forward()` / `model(x)`, `parameters()`, `train()`, `eval()`, `state_dict()`, `zero_grad()`) are called directly on the DDP-wrapped model — no unwrapping needed. Only custom supernet attributes (`search_space`, `get_active_subnet()`, `elastic_num_params`) access the inner module via `unwrap_model()`. `set_sample_config` is called via `set_sample_config_ddp()`.
**Verify**: Read `train_supernet.py` training and validation loops. Confirm no manual unwrapping is used for standard `nn.Module` methods. Confirm custom attributes use `unwrap_model()`. Confirm `set_sample_config_ddp` is used instead of manual unwrap for configuring the supernet.
**Anti-pattern**: `unwrap_model(model)(inputs)` for forward pass; `model.search_space` on DDP wrapper.
**Fix**: Replace `unwrap_model(model).forward(x)` with `model(x)`. Replace `model.search_space` with `unwrap_model(model).search_space`. Replace manual `unwrap_model(model).set_sample_config(cfg)` with `set_sample_config_ddp(model, cfg)`.

### [MAJOR] 6. Rank-Gated I/O
**auto-fixable**: yes
**Section**: §3 Progress Driver
**Check**: All single-writer side effects are guarded by `if is_main_process()` so that only rank 0 performs them. This includes:
- **Logging**: `print()`, `tqdm` output (`disable=not is_main_process()`), and any metric reporting
- **File writes**: any file output during training
  - Exception: `save_checkpoint_ddp()` does **not** need this guard (see item 23)
- **Directory creation**: `os.makedirs()` for output directories, log directories, etc.
**Verify**: grep for `print(`, `open(`, `os.makedirs`, and similar I/O calls. Confirm each is inside an `if is_main_process()` block or a helper that gates on rank 0. Confirm `save_checkpoint_ddp` is NOT inside such a guard.
**Anti-pattern**: Unguarded `print()` producing N duplicate lines; multiple ranks creating the same directory; wrapping `save_checkpoint_ddp()` inside `if is_main_process()` (deadlock, see item 23).
**Fix**: Wrap with `if is_main_process():`. For `save_checkpoint_ddp`, do the opposite — ensure it is called by all ranks.

### [MINOR] 7. Device Placement Consistency
**auto-fixable**: no
**Section**: Validation (Device placement consistency)
**Check**: All tensors participating in the same operation reside on the same device. No cross-device operations. GPU/NPU tensors are moved to CPU before NumPy/Python scalar conversion, and tensors are not mixed with NumPy arrays in mathematical operations. Model tensor attributes are properly registered.
**Verify**: Read the data processing and metric calculation logic in `train_supernet.py` and its generated helper files. Check for operations mixing `np.ndarray` with `torch.Tensor`, and ensure any tensor converted via `.numpy()` is first moved to CPU. Also verify that any tensor assigned as an attribute of an `nn.Module` uses `nn.Parameter` or `register_buffer`.
**Anti-pattern**: `tensor.numpy()` without `.cpu()` first; mathematical operations combining `np.array` and `torch.Tensor`; assigning `self.my_tensor = torch.tensor(...)` inside an `nn.Module` without `register_buffer`.

### [MAJOR] 8. Data Pipeline — DDP Sampler
**auto-fixable**: no
**Section**: §4 Data Pipeline
**Check**: For map-style datasets, `DistributedSampler` is used and `sampler.set_epoch(epoch)` is called at the beginning of each epoch. For iterable/streaming datasets, data is sharded across ranks.
**Verify**: grep for `DistributedSampler` and `set_epoch`.

### [MAJOR] 9. No Hardcoded Paths In Data-Loading Code
**auto-fixable**: no
**Section**: §4 Data Pipeline
**Check**: All generated data-loading code (dataset classes, auxiliary loaders, etc.) accepts data paths as parameters. No function or class hardcodes or derives a path from a package location.
**Verify**: In generated helper files, grep for file-loading calls (`loadmat`, `np.load`, `open(`, `torch.load`, etc.) and confirm the file path traces back to a function/class parameter, not an internally constructed path.
**Anti-pattern**: A dataset class or loader function internally resolves a path from the installed package or repo layout instead of accepting it as a parameter.

### [MAJOR] 10. Real-Time Training Progress
**auto-fixable**: yes
**Section**: §3 Progress Driver
**Check**: The training loop provides real-time progress feedback via periodic batch-level logging (rank 0 only). The primary approach is `tqdm` (`disable=not is_main_process()`): wrapping the batch iterator for epoch-based training, or a single bar tracking `global_step` for step-based training, with running metrics in `postfix`. If the user's original project environment is not suitable for `tqdm`, a periodic `print` statement (e.g. `if global_step % args.log_interval == 0:`) is used instead.
**Verify**: Check the training loop for a batch-level progress indicator (either `tqdm` or periodic `print`). Confirm it is disabled/gated on non-main ranks.
**Anti-pattern**: Logging only at epoch boundaries; `tqdm` or `print` enabled on all ranks producing duplicate logs.
**Fix**: Add `tqdm` (with `set_postfix`) or a periodic `print`, gated by `is_main_process()`.

### [MAJOR] 11. Progress Unit Consistency
**auto-fixable**: no
**Section**: §3 Progress Driver
**Check**: Training progress unit (epoch or `global_step`) is chosen from the user's project and used consistently for: training budget, `--eval_interval`, scheduler stepping, checkpoint save interval, logging interval, and final validation.
**Verify**: Identify the progress unit and confirm it's consistent across all uses.
**Anti-pattern**: Mixing epoch-based and step-based counting; forcing streaming data into artificial epochs.

### [MAJOR] 12. Optimizer And Scheduler Type From User Project
**auto-fixable**: yes
**Section**: §6 Optimizer, Scheduler, AMP, And Gradient Clipping
**Check**: The optimizer class (e.g. `Adam` vs `AdamW` vs `SGD`) and scheduler class (e.g. `StepLR` vs `CosineAnnealingLR`) match the user's original training code. The workflow code templates use example optimizer/scheduler types that must not be copied verbatim.
**Verify**: Compare the optimizer and scheduler construction in `train_supernet.py` against the user's original training code under `<user_project_root>`. Confirm the class names match.
**Anti-pattern**: Using `AdamW` when the user's project uses `Adam`; using `CosineAnnealingLR` when the user's project uses `StepLR`; copying workflow template defaults without checking the user's code.
**Fix**: Replace the optimizer/scheduler with the user's original choice and port its hyperparameters.

### [MAJOR] 13. Batch Size And Learning Rate Under DDP
**auto-fixable**: yes
**Section**: §6 Batch Size & Learning Rate
**Check**: `--batch_size` is per-device; the effective batch size under DDP is `batch_size * world_size`. `args.lr` is passed directly to the optimizer using the user's original LR as default. If the user's original code includes DDP-aware LR scaling, that rule is reused. LR and batch-size values are exposed as CLI or launcher overrides.
**Verify**: Confirm `--batch_size` controls per-device samples. Inspect optimizer construction: LR handling should either faithfully port the user's original DDP scaling logic, or use `args.lr` directly if the original code has no scaling.
**Anti-pattern**: Introducing new LR scaling logic that the user's original code does not have; hardcoded LR or batch size without CLI override.
**Fix**: Check the user's original training code. If it includes DDP-aware LR scaling, keep that logic. If it does not, remove any LR scaling that the generated script introduced and pass `args.lr` directly to the optimizer.

### [MAJOR] 14. Scheduler Step Granularity Preserved
**auto-fixable**: no
**Section**: §6 LR Scheduler
**Check**: `scheduler.step()` is called at the same granularity as the original project (per-epoch vs per-batch). If the original steps once per epoch, the generated script must not move it into the batch loop.
**Verify**: Compare scheduler step placement with the user's original training code.

### [CRITICAL] 15. AMP Autocast And GradScaler Decoupled
**auto-fixable**: no
**Section**: §2 Distributed Setup
**Check**: Uses `autocast()` and `grad_scaler()` from `nas_agent.train.distributed`. The autocast enable flag is independent from `scaler.is_enabled()` — uses `autocast(device, enabled=args.amp)` directly. GradScaler may be disabled on some devices but autocast should still follow the user's AMP setting.
**Verify**: grep for `autocast` and `grad_scaler` imports and usage. Confirm autocast enabled flag uses `args.amp` directly.
**Anti-pattern**: Coupling autocast enable to scaler state; using `torch.cuda.amp` directly instead of `nas_agent.train.distributed`.

### [CRITICAL] 16. NPU `foreach` Compatibility
**auto-fixable**: yes
**Section**: §6 NPU Compatibility
**Check**: `is_npu = device.type == "npu"` is set once after `setup_distributed()`. Both optimizer constructor and `clip_grad_norm_` pass `foreach=False if is_npu else None` or similar behavior.
**Verify**:
- grep for `is_npu` — should exist
- grep for `foreach` — should appear in both optimizer and clipping contexts
**Anti-pattern**: Missing `foreach` parameter; hardcoding `foreach=False` unconditionally.
**Fix**: Add `is_npu = device.type == "npu"` after device setup. Add `foreach=False if is_npu else None` to optimizer and `clip_grad_norm_` calls.

### [CRITICAL] 17. Gradient Clipping After Sandwich Backward
**auto-fixable**: no
**Section**: §6 Gradient Clipping, §8 Training Example
**Check**: Gradient clipping via `clip_grad_norm_` happens after ALL sandwich losses (max + min + random) have called `backward()` and BEFORE `optimizer.step()`. When AMP scaling is enabled, `scaler.unscale_(optimizer)` is called before clipping.
**Verify**: Read the training loop. Confirm clipping is between the last `backward()` and `scaler.step(optimizer)`.
**Anti-pattern**: Clipping after each individual subnet's backward (too early); missing `scaler.unscale_` before clipping.

### [CRITICAL] 18. Sandwich Sampling: DDP Sync
**auto-fixable**: no
**Section**: §8 Sandwich Training Loop
**Check**: Uses `sync_random_seed(device)` to broadcast a seed from rank 0, ensuring all ranks produce identical block choices and architecture configs from the same `rng` state each iteration.
**Verify**: grep for `sync_random_seed`. Confirm it's called before `sample_sandwich_arch_configs` in the training loop.
**Anti-pattern**: Each rank sampling independently without synchronization.

### [CRITICAL] 19. Sandwich Sampling: Config Construction
**auto-fixable**: no
**Section**: §8 Choice Sampling
**Check**: `sample_sandwich_arch_configs(search_space, n_random, rng)` returns `(max_config, min_config, random_configs)` where:
- Block choices are sampled per layer along max depth
- **max config**: all depths max, all elastic params max
- **min config**: all depths min, all elastic params min
- **random config**: random depth, random elastic params
- All random choices use the provided `rng`, not global RNG
**Verify**: Read the sampling helper and verify per-spec construction.
**Anti-pattern**: Using `random.choice()` instead of `rng.choice()`; depth set incorrectly for min/max.

### [CRITICAL] 20. Evaluation: Fixed Block Choice
**auto-fixable**: no
**Section**: §9 Evaluation
**Check**: Evaluation configs use a **fixed** block choice (user-model-expanded elastic block = first `choice` in generated candidate order), NOT random. `sample_fixed_eval_arch_configs` is a separate function from `sample_sandwich_arch_configs`.
- **max eval config**: all depths max, fixed block choice, all elastic params max
- **min eval config**: all depths min, fixed block choice, all elastic params min
**Verify**: Read the eval sampling helper. Confirm block choice is hardcoded (first choice), not `rng.choice()`.
**Anti-pattern**: Reusing `sample_sandwich_arch_configs` for eval; random block choice in eval configs.

### [CRITICAL] 21. DDP Metric Aggregation: AverageMeter
**auto-fixable**: yes
**Section**: §3 Progress Driver, §9 DDP Metric Aggregation
**Check**:
- Uses `AverageMeter` from `nas_agent.train` for metric aggregation in **both** the training loop and the validation function.
- `.avg` and `.count` trigger `all_reduce` — a collective operation that all ranks must call together. Every `.avg` / `.count` call must be outside any `if is_main_process():` guard.
- `.avg` returns a Python `float` (not a tensor). Any post-processing must use `math` / plain Python operations, not `torch.*` ops.
**Verify**:
- grep for `AverageMeter` import and usage in both the training loop and the validation function.
- Confirm training metrics displayed in `tqdm` postfix or periodic `print` come from `AverageMeter.avg`, not raw per-rank values.
- Verify that every `.avg` or `.count` access is NOT inside an `if is_main_process():` block.
- Verify that `.avg` results are not passed to `torch.*` ops (`.avg` returns `float`).
**Anti-pattern**:
- Calling `.avg` inside `if is_main_process():` (causes multi-GPU deadlock).
- Passing `.avg` to `torch.*` ops like `torch.log10` (use `math.log10` instead).
- Per-rank training loss in `tqdm` without aggregation.
- Per-rank validation metrics without `all_reduce`.
- Computing `total_loss / num_batches` per rank then `all_reduce`-averaging (biased when ranks have different sample counts).
**Fix**: Move `.avg` / `.count` calls outside the `if is_main_process():` guard. Compute the metric on all ranks first, then gate only the `print()` or `tqdm` display on `is_main_process()`.

### [CRITICAL] 22. KD Only When Appropriate
**auto-fixable**: no
**Section**: §8 Distillation
**Check**: KD is NOT enabled when any of these conditions hold: (a) no clear teacher/student tensors, (b) KD would need engineering beyond standard final-output loss, (c) original loss is multi-component weighted combination, (d) sandwich KD conflicts with training objective. When KD is enabled:
- Runtime shape guard (`min_outputs.shape == teacher_outputs.shape`) is present.
- `KDWeightScheduler` from `nas_agent.train.distillation` is used to schedule the KD weight, constructed with `args.kd_weight`, `args.kd_warmup_start`, and `args.kd_warmup_length`. The sandwich loop uses the scheduler's output (not the static `args.kd_weight`) as the loss coefficient.
**Verify**: If KD is enabled, check which KD loss function is used and verify it matches the output type (logits → `logits_kd_loss`, multi-label → `soft_bce_kd_loss`, continuous → `mse_kd_loss`, embedding → `cosine_kd_loss`). Verify shape guard exists. Verify `KDWeightScheduler` is constructed and its `get_weight()` result is used in the sandwich loop. Also verify that KD-related CLI args match the chosen loss: `--kd_temperature` should only exist when the loss accepts temperature (`logits_kd_loss`, `soft_bce_kd_loss`), not for `mse_kd_loss` or `cosine_kd_loss`.
**Anti-pattern**: Using `mse_kd_loss` but exposing `--kd_temperature` and passing `temperature=args.kd_temperature` (MSE KD has no temperature parameter); adding unused KD CLI args to the launcher; using static `args.kd_weight` directly instead of `KDWeightScheduler`.

### [CRITICAL] 23. Checkpoint Uses `save_checkpoint_ddp` Without Extra Rank Guard
**auto-fixable**: yes
**Section**: Checkpoint
**Check**: All checkpoint writes use `save_checkpoint_ddp` (not raw `save_checkpoint`). The function must be called by **all ranks** (not inside `if is_main_process()`) because it contains an internal barrier (see workflow §7). `epoch`, `global_step`, and `best_metric` are passed as keyword arguments.
**Verify**: Read the checkpoint save logic in `train_supernet.py`. Confirm `save_checkpoint_ddp` is NOT indented under `if is_main_process():`. Confirm `epoch=`, `global_step=`, and `best_metric=` kwargs are present.
**Anti-pattern**: Wrapping `save_checkpoint_ddp(...)` inside `if is_main_process():` (multi-GPU deadlock); using raw `save_checkpoint` with manual rank gate / unwrap / barrier.
**Fix**: Move `save_checkpoint_ddp(...)` outside any `if is_main_process():` guard. Add any missing keyword arguments.

### [MAJOR] 24. Latest Checkpoint Saved After Evaluation
**auto-fixable**: yes
**Section**: §7 Checkpoint
**Check**: When evaluation is scheduled for the current epoch/step, `supernet_latest.pth` is saved **after** evaluation and `best_metric` update, not before. This ensures that resumed training uses an up-to-date `best_metric`.
**Verify**: Read the training loop. Confirm `save_checkpoint_ddp(..., best_metric=best_metric, ...)` for `supernet_latest.pth` appears after the evaluation block and after `best_metric` is updated.
**Anti-pattern**: Saving `supernet_latest.pth` before evaluation; `best_metric` in latest checkpoint is always one eval cycle behind.
**Fix**: Move the `supernet_latest.pth` save to after the evaluation and `best_metric` update block.

### [CRITICAL] 25. Best Checkpoint Uses Max Config Metric
**auto-fixable**: no
**Section**: §9 Evaluation
**Check**: `supernet_best.pth` is saved based on the **max config's** validation metric, not min config's. The best-metric comparison must use the globally aggregated metric (from `AverageMeter.avg`) so that all ranks reach the same save-or-skip decision for `save_checkpoint_ddp` (which contains an internal barrier).
**Verify**: Read the checkpoint save logic. Confirm it checks metric from `max_config` evaluation. Confirm the metric used in the comparison comes from `AverageMeter.avg` (not a per-rank value), so all ranks agree on whether to save.
**Anti-pattern**: Using min config or average of max+min for best checkpoint decision; comparing against a per-rank metric that may differ across ranks, causing only some ranks to enter `save_checkpoint_ddp` (deadlock).

### [CRITICAL] 26. Self-Contained Generated Training Artifacts
**auto-fixable**: no
**Section**: Source Evidence
**Check**: `train_supernet.py` and generated helper files do not import modules from `<user_project_root>`. Any required project-specific dataset, preprocessing, collate, loss, metric, wrapper, or checkpoint logic is copied and adapted into files under `<output_dir>`.
**Verify**: Inspect imports in `train_supernet.py` and helper files. Check for imports that reference the original project package, absolute project paths, `sys.path` insertion, or `PYTHONPATH` assumptions.
**Anti-pattern**: `sys.path.append(<user_project_root>)`; `from user_project.datasets import ...`; helper files that only work when launched from the original project root.

### [MAJOR] 27. Structured Training Metrics JSONL (Orca visualization sidecar)
**auto-fixable**: yes
**Section**: §3 Progress Driver (Orca visualization)
**Check**: `train_supernet.py` writes a structured metrics file at `<output_dir>/runs/train/train_metrics.jsonl` so the Orca sidecar can tail it and refresh training curves live. Requirements:
- **Rank-0 only**: every write is guarded by `if is_main_process():` (same pattern as item 6 — ordinary single-writer file I/O).
- **One JSON object per line** (JSONL), appended, with **flush after each write** (sidecar reads concurrently; unflushed partial lines are skipped).
- **Schema** (exact field names): `epoch` (int), `global_step` (int), `phase` ("train" | "val"), `loss` (float), `acc` (float or null), `lr` (float), `best_metric` (float or null).
- **Frequency**: a `phase="train"` row every `args.log_interval` steps (`acc` may be null on train rows); one `phase="val"` row per evaluation cycle carrying `acc` and current `best_metric`.
- The writer must be **Orca-agnostic**: plain `open(..., "a")` + `json.dumps` + `flush`. No `import orca`. The artifact stays runnable standalone (just writes an extra file).
**Verify**: grep for `train_metrics.jsonl` under an `is_main_process()` guard; confirm `json.dumps` + append + `flush`; confirm the 7 schema fields.
**Anti-pattern**: only `tqdm`/`print` (no structured file — sidecar cannot chart); writing without `flush`; writing from all ranks (DDP duplicate rows); missing required fields; `import orca` inside the generated script.
**Fix**: Add an `is_main_process()`-guarded JSONL append writer for `runs/train/train_metrics.jsonl` with the exact schema; flush after each write.

### [MAJOR] 28. Chart-Inline Live Push (Orca visualization, training curves)
**auto-fixable**: yes
**Section**: §3 Progress Driver (Orca visualization, chart-inline)
**Check**: `train_supernet.py` inlines a `_push_chart()` helper so training curves stream **live** without needing a tail sidecar or a separate viz node. Requirements:
- **Orca-agnostic import guard**: `try: from orca.chart import render_chart\nexcept Exception: render_chart = None`. When `render_chart is None` (running standalone, outside Orca) the helper is a **no-op** — the artifact stays runnable with no Orca dependency. No top-level `import orca`.
- **Rank-0 only**: every push guarded by `if is_main_process() and render_chart is not None:` (same single-writer discipline as item 6/27).
- **Accumulate + full-series push**: the helper keeps in-process accumulators (`list`s) of all points seen so far; each call **appends** the new point then pushes the **FULL accumulated series** with a fixed `label`+`title`. (Because `render_chart` with the same `label`+`title` **replaces** the chart, pushing a single point would erase history. Re-reading the jsonl each call is also acceptable but accumulating in memory is preferred — same process throughout training.)
- **Exact chart contracts** (label/title MUST match `tail_metrics.py` C3a/C3b so inline and tail are refresh-idempotent). The `x_label`/`y_label`/`caption` kws MUST also be passed — dedup key is `label+title`, so the **last writer wins**: if the inline pusher omits them it replaces `tail_metrics`' labeled chart with an unlabeled one (labels flicker off whenever training pushes between tail polls). Keep them byte-aligned with `tail_metrics.py` C3a/C3b:
  - **C3a — Training Loss**: `render_chart(chart_type="line", data=<full loss series>, label="nas/training", title="Training Loss", x="global_step", y="loss", hue="phase", x_label="全局训练步（global_step）", y_label="loss（越低越好）", caption="每 log_interval 步采样的训练 loss；hue=phase 区分 train/val。")`. Each row `{"global_step": int, "loss": float, "phase": "train"|"val"}`. Pushed every `args.log_interval` steps (train rows) and every eval (val loss rows, `phase="val"`).
  - **C3b — Validation Metric**: `render_chart(chart_type="line", data=<full val series>, label="nas/training", title="Validation Metric", x="global_step", y="metric", x_label="全局训练步（global_step）", y_label="metric（验证集指标）", caption="验证集指标；每 eval 周期一个点。")`. Each row `{"global_step": int, "metric": float}`. Pushed every evaluation cycle (val subset only).
- **Frequency**: C3a train-row push every `log_interval`; C3a val-row + C3b push at each evaluation. Push failures must NOT crash training (wrap push in `try/except` → stderr loud, continue — chart is best-effort sidecar).
**Verify**: grep for `_push_chart` (or equivalent helper) + `from orca.chart import render_chart` under a `try/except`; confirm the helper pushes the full accumulated series (not a single point); confirm the two fixed `label="nas/training"` / `title="Training Loss"` / `title="Validation Metric"` contracts; confirm push is `is_main_process()`-guarded and wrapped so failure can't abort training; confirm no top-level `import orca` (guarded import only).
**Anti-pattern**: pushing a single latest point each call (erases history — same label+title replaces); hardcoding `import orca` so the script dies outside Orca; pushing from all ranks; label/title that differ from `tail_metrics.py` (produces duplicate charts instead of refreshing); letting a `render_chart` exception kill the training loop.
**Fix**: Add a module-level `render_chart = None`-guarded `_push_chart()` helper with two in-memory accumulators (loss rows keyed by `phase`, val-metric rows); call it (rank-0 guarded, `try/except`-wrapped) at each `log_interval` and each eval, pushing the full series with the exact C3a/C3b label/title above.

**Reference sketch** (gold example —— 仿此实现，label/title 与 tail_metrics.py C3a/C3b 完全一致以保证 refresh-idempotent)：
```python
try:
    from orca.chart import render_chart  # type: ignore
except Exception:
    render_chart = None  # Orca-agnostic：脱离 Orca 时 no-op

# 进程内累加器（同进程贯穿训练；每次推**全序列**——同 label+title 是替换语义，推单点会擦历史）
_loss_rows: list[dict] = []
_val_rows: list[dict] = []

def _push_chart(global_step, *, loss=None, phase="train", val_metric=None, is_main):
    """best-effort：render_chart=None 或推图异常都不得 crash 训练（sidecar）。"""
    if not is_main or render_chart is None:
        return
    try:
        if loss is not None:
            _loss_rows.append({"global_step": int(global_step), "loss": float(loss), "phase": phase})
            render_chart(chart_type="line", data=list(_loss_rows),
                         label="nas/training", title="Training Loss",
                         x="global_step", y="loss", hue="phase",
                         x_label="全局训练步（global_step）", y_label="loss（越低越好）",
                         caption="每 log_interval 步采样的训练 loss；hue=phase 区分 train/val。")
        if val_metric is not None:  # eval 周期
            _loss_rows.append({"global_step": int(global_step), "loss": float(loss) if loss is not None else None, "phase": "val"})  # C3a 的 val loss 点
            render_chart(chart_type="line", data=list(_loss_rows),
                         label="nas/training", title="Training Loss",
                         x="global_step", y="loss", hue="phase",
                         x_label="全局训练步（global_step）", y_label="loss（越低越好）",
                         caption="每 log_interval 步采样的训练 loss；hue=phase 区分 train/val。")
            _val_rows.append({"global_step": int(global_step), "metric": float(val_metric)})
            render_chart(chart_type="line", data=list(_val_rows),
                         label="nas/training", title="Validation Metric",
                         x="global_step", y="metric",
                         x_label="全局训练步（global_step）", y_label="metric（验证集指标）",
                         caption="验证集指标；每 eval 周期一个点。")
    except Exception as e:  # sidecar：stderr loud 但不抛
        print(f"[chart] push failed (ignored): {type(e).__name__}: {e}")
```
调用点：训练循环每 `args.log_interval` 步 `_push_chart(global_step, loss=train_loss, phase="train", is_main=is_main_process())`；每个 eval 周期 `_push_step(global_step, loss=val_loss, val_metric=val_acc, is_main=is_main_process())`。


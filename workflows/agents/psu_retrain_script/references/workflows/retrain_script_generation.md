# Retrain Script Generation Workflow

Use this workflow to generate `<output_dir>/retrain.py`, `<output_dir>/finetune.py`, and `<output_dir>/run_retrain.sh`, plus any small helper files needed to obtain the **final weights of the one selected subnet**. Do not start training in this node — the downstream `psu_retrain` node executes the generated launcher.

The goal is to produce a project-specific retraining entry point for the single architecture chosen upstream by `psu_run_search`: reuse the user's real dataset loading, evaluation, optimizer/scheduler, and checkpoint conventions; materialize the selected subnet, strict-load its branch weights from the supernet checkpoint, and finetune it with the same frozen pretrained-teacher KD used during supernet training.

## Strategy (the spine of this workflow)

The retrain strategy is a **fixed constant: `finetune-from-supernet`** — it is NOT a decision, NOT bound to the search-time `evaluation_paradigm`, and has **no from-scratch fallback** (a from-scratch retrain would discard the pretrained weight inheritance and the KD distillation premise). Both generated files are therefore always produced:

- `retrain.py` — main entry: the frozen-teacher KD finetune loop for the one materialized subnet.
- `finetune.py` — the subnet materialization + weight-inheritance + teacher/KD seam: `get_active_subnet` extraction, strict branch-weight loading from the supernet ckpt per the selected per-slot choices, freeze continuation, teacher construction via `load_pretrained.py`, and the KD loss helpers mirrored from `train_supernet.py`.
- `run_retrain.sh` — the launcher.

Verify the strategy's two prerequisites deterministically; a missing prerequisite → fail loud (emit `error`, generate nothing):

1. The supernet checkpoint file. Its path is the `supernet_ckpt_path` configured in `$ORCA_ARTIFACTS_DIR/search_config.yaml` (contractual default `runs/train/supernet_best.pth`). The file must exist and be non-empty.
2. The teacher checkpoint `{{ inputs.pretrained_ckpt }}` (the pretrained original model). The file must exist and be non-empty.

Record the evidence in `strategy_reason` (one line, e.g. `finetune-from-supernet: supernet ckpt runs/train/supernet_best.pth present, teacher ckpt <path> present`).

## Source Evidence

Build the script from the upstream artifacts, the user's own training code, and the generated supernet. The generated script must follow the user's dataset, preprocessing, batch format, model-call signature, loss, metrics, optimizer, scheduler, logging, checkpoint, and runtime conventions.

Read these to fill the script (cwd-independent under `$ORCA_ARTIFACTS_DIR`):

- `supernet_summary.md` — KD recipe facts recorded by `psu_train_script` (loss composition, weights, teacher source, freeze groups), evaluation paradigm, generated-artifact list.
- `project_manifest.md` — the original project's training/evaluation semantics, data/environment details, and the navigation index of key source files. Use it as the map before opening files under the Original Project Root.
- `supernet.py` — the `SearchSpace`, `ArchConfig`, `SuperNet` API surface (`build_supernet` / `set_sample_config` / `get_active_subnet`, etc.). Call the supernet ONLY through the APIs the manifest exposes; never hardcode its internals.
- `load_pretrained.py` — the pretrained-checkpoint loader generated at flatten (DataParallel prefix strip / ckpt wrapper unwrap / strict key check against the prepared model). The frozen teacher AND any original-branch weight checks load through this same script — never re-implement loading.
- `train_supernet.py` — the KD loss implementation (hidden cosine + logits KL + weights), teacher construction, freeze grouping, data pipeline, AMP, checkpoint policy to mirror exactly.
- `evaluator.py` — the subnet-extraction and weight-loading behavior actually used during search; mirror its extraction route for the retrain script.
- `{{ inputs.project_root }}` — the original project training code: training budget, optimizer, scheduler, metric formulas.

Use `project_manifest.md` to navigate the user's training code: read the specific files under the Original Project Root it points to instead of bulk-reading the project, and when a needed detail is missing from the manifest or looks inaccurate, explore the project (targeted) and update the manifest in place, since source code is always authoritative.

Generated artifacts must be self-contained for project-specific code. Do not import modules from the **user's project**. Copy and adapt any required project logic into `retrain.py` / `finetune.py` or helper files under `<output_dir>`. Apart from the Python standard library, installed third-party packages, and `nas_agent`, generated artifacts should import only files under `<output_dir>`.

### Ported Helper Files

When the calling skill delegated porting to `project-porter` subagents, the ported helper files already exist under `<output_dir>`: import them as siblings and write call sites against the porter's API report instead of re-porting. Interface mismatches are resolved in this workflow as they surface: adapt your call sites or edit the helper files directly, never add wrapper layers. When touching ported logic (formulas, control flow, constants), preserve the original project's semantics.

## Generation Rules

Write the generated retrain script with these sections and contracts.

### 1. CLI And Runtime Args

Use a stable base CLI for the retrain runtime:

- `--output_dir`: Default `"runs/retrain"`; the final ckpt is always written to `<output_dir>/retrain_best.pth` (contract path consumed by `psu_retrain`'s status/emit scripts — must not drift).
- `--eval_interval`: Evaluation frequency in the generated script's chosen progress unit.
- `--device`: Default `"auto"`, allow choices `["auto", "cuda", "npu", "cpu"]`.
- `--amp`: Enable AMP.
- `--lr`: Learning rate; default from the user's original training config.
- `--max_grad_norm`
- `--max_train_steps`: Global optimizer-step budget cap (default `0` = unlimited); when `> 0`, short-circuit the batch loop at `global_step >= max_train_steps`, still run the end-of-progress-unit eval + checkpoint for the partial unit, then break the outer loop. Mandatory; expose as launcher `MAX_TRAIN_STEPS` (default `0`, cap for CPU/smoke runs). Orthogonal to `--epochs`/`--max_steps`.
- `--progress-every`: progress.jsonl chart-feed granularity in optimizer steps (default `50`; `1` = every step). The feed appends a line every N steps **and mandatorily at every progress-unit boundary** (see §3(b)) — a per-epoch-only feed yields a curve too sparse to watch live.
- `--seed`
- `--resume`: Resume from `<output_dir>/retrain_latest.pth` if present.
- `--supernet_ckpt`: path to the trained supernet checkpoint the subnet's branch weights are loaded from (always present — the strategy is constant finetune-from-supernet).
- `--teacher_ckpt`: path to the pretrained original-model checkpoint (`{{ inputs.pretrained_ckpt }}`); the frozen KD teacher is built via `load_pretrained.py`. The generated script must assert `Path(args.teacher_ckpt).resolve() == Path(load_pretrained.PRETRAINED_CKPT).resolve()` and fail loud on mismatch — the loader's module constant is the single weight source, the CLI flag is a consistency precheck, not a second load path.

Expose project-derived training and runtime arguments from the user's project, such as dataset path (`--data_dir`), training budget (`--epochs` or `--max_steps`), `--batch_size`, `--num_workers`, optimizer and scheduler hyperparameters, augmentation flags, validation controls, and task options. Preserve the user's defaults where they exist, but allow CLI overrides for remote runs.

### 2. Distributed Setup

The **default launcher is single-process `python`** (no torchrun, no DDP wrap). When launched with `torchrun --nproc_per_node=N` (multi-GPU), `RANK` env is present → `is_distributed()=True` → DDP wrap activates automatically.

Do not infer the target GPU/NPU runtime from the current machine. The generated script is intended for a remote training server, so device and backend selection must remain runtime-configurable through the launcher, environment, and `nas_agent.train`. Restrict visible devices through `CUDA_VISIBLE_DEVICES` (GPU) or `ASCEND_RT_VISIBLE_DEVICES` (NPU) in the launcher / environment; never hardcode device indices.

**Single-device path (default):** plain `python retrain.py` (no `RANK` env) → `setup_distributed()` does **not** `init_process_group` → `is_distributed()=False` → `get_world_size()=1` / `is_main_process()=True` / `barrier()` no-op.

**Multi-GPU path (when `torchrun` used):** `RANK` env present → `setup_distributed()` calls `init_process_group` → `is_distributed()=True` → DDP wrap, `DistributedSampler`, `all_reduce` all active.

Use `nas_agent.train.distributed` to configure distributed setup, device resolution, DDP-safe helpers, and AMP.

**DDP wrap is conditional (when `is_distributed()`):** Only wrap in `DistributedDataParallel` when `is_distributed()` returns True. On single-device, skip DDP entirely.

**AMP usage rule:** Use `autocast()` and `grad_scaler()` from `nas_agent.train.distributed`. Keep the autocast enable flag independent from `scaler.is_enabled()`: `autocast(device, enabled=args.amp)`. `autocast(device, enabled=False)` is a nullcontext—orthogonal to single-device. Ascend NPU may use bf16 autocast without loss scaling, so `grad_scaler()` can be disabled on NPU; the autocast enable flag stays driven by `args.amp` regardless.

Use this template unless the user's project has stricter distributed conventions that must be preserved:

```python
from nas_agent.train.distributed import (
    autocast,
    get_local_rank,
    get_rank,
    grad_scaler,
    is_distributed,
    is_main_process,
    setup_distributed,
    torch_manual_seed,
    unwrap_model,
)
from torch.nn.parallel import DistributedDataParallel


device = setup_distributed(args.device)
torch_manual_seed(args.seed + get_rank())
scaler = grad_scaler(device, enabled=args.amp)

model = model.to(device)

# optimizer and scheduler go here, before DDP wrapping
# Note: use the actual optimizer and scheduler from the user's original project
optimizer = optim.AdamW(model.parameters(), lr=args.lr)
scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)

# DDP wrap is conditional: only when torchrun sets RANK env → is_distributed()=True.
if is_distributed():
    device_ids = None if device.type == "cpu" else [get_local_rank()]
    model = DistributedDataParallel(
        model,
        device_ids=device_ids,
        find_unused_parameters=True,
    )
```

Note: a materialized fixed-subnet has no unused parameters by construction; `find_unused_parameters=True` is kept only to mirror the supernet-training wrap convention.

### 3. Progress Driver

First choose the generated script's training progress unit from the user's project. Use epochs only when the original training code and data pipeline are naturally epoch-based. Otherwise, use optimizer update count, stored as `global_step`.

Use that same progress unit consistently for the training horizon, `--eval_interval`, scheduler stepping, checkpoint save interval, logging interval, and final validation. Do not force streaming or iterable inputs into artificial epochs.

Do **not** port the user's complex logging frameworks (WandB, TensorBoard, custom file loggers). Use the simple stdout progress tracking below.

Provide real-time training progress via periodic batch-level logging (rank 0 only). Without batch-level logs, a long-running epoch produces no output and looks indistinguishable from a hang.

- **Primary approach (`tqdm`)**: `disable=not is_main_process()`. For epoch-based training, wrap the batch iterator per epoch. For step-based training, use a single bar tracking `global_step`.
- **Fallback approach (`print`)**: a periodic `print` statement (e.g. `if global_step % args.log_interval == 0:`).

**CRITICAL — machine-parseable progress (contract, mandatory).** On top of the human-readable progress, the generated script **must** emit **two** machine feeds per completed progress unit (rank 0 only). Both are required. **The user's original code is the sole authority for which metrics exist and what they are named; `loss` is merely a common example, never an assumption.**

**(a) Telemetry line (stdout)** — consumed by `psu_retrain` ETA / health / warmup (line-oriented, structurally regex-parsed). Print exactly one line per progress unit:
- epoch-based: `epoch <cur>/<total> <primary_metric> <value>` — e.g. `epoch 3/10 loss 0.4521`
- step-based: `step <cur>/<total> <primary_metric> <value>` — e.g. `step 1200/6000 psnr 28.4`

`<primary_metric>` is the **training scalar this script actually produces under its real name** — under the KD finetune recipe that is the KD loss scalar (e.g. `kd_loss` / `total_loss`; use the name mirrored from `train_supernet.py`). Do **not** hardcode the literal word `loss` when the scalar is named otherwise. Do not print other lines containing the bare words `epoch`/`step` followed by digits in a different meaning (e.g. a save message like "saved retrain_epoch_0005.pth" — use "checkpoint epoch 5 saved" instead) so the downstream regex cannot misparse. Expose the horizon as `--epochs N` (epoch-based) or `--max_steps N` (step-based).

**(b) Progress JSONL (chart feed)** — consumed by `psu_retrain`'s live chart watcher. Append one JSON line **every N optimizer steps** (`--progress-every`, default `50`) **and mandatorily at every progress-unit boundary** (epoch/step end, including the final partial unit) to `$ORCA_ARTIFACTS_DIR/runs/retrain/progress.jsonl`:

```
{"step": <global_step>, "metrics": {"<name>": <float>, ...}}
```

- Write granularity is **step-level, not per progress unit**: a per-epoch-only feed (e.g. 1 epoch → 1 point) is too sparse for a live convergence curve. Every N steps, at the end of each progress unit, and once at training end, append one line (`1` via `--progress-every` = every step for short runs).
- `step` = the global optimizer-step count at write time (int) — monotonically increasing across the whole run, including the boundary lines.
- `metrics` = **every scalar metric accumulated since the last written line** — the KD loss scalars as running means over the window plus the instantaneous `lr`; on lines written at evaluation points, also every evaluation metric the ported evaluation path tracks, each under its real name. **No name is assumed and none is special**: emit exactly the scalars the script computes.
- Open in append mode, write `json.dumps(row) + "\n"`, `flush()` after each write, guard with `if is_main_process()`. The launcher truncates this file at the start of each attempt, so each attempt starts fresh.

**(c) Terminal metric lines (deterministic, contract)** — consumed structurally by `psu_retrain`'s terminal status refresh (`update_status_md.sh`) and `psu_report`'s `final_metrics` extraction. On rank 0, print (stdout, flush):

- at every evaluation point: `[eval] unit <N> <user_val_metric> <value>` — `<N>` = the progress-unit index; additional `<name> <value>` pairs after the first metric are allowed;
- once at training end: `done best <user_val_metric> <value>` — the best validation metric under its real name (a leading bracketed tag such as `[retrain] done best ... updates 469` is tolerated).

These two line shapes are parsed with structural regexes downstream; without them the final report cannot quote a real number. Do not print other lines matching `done best` in a different meaning.

**CRITICAL: Guard all single-writer side effects with `if is_main_process()`** so only rank 0 performs them: `print()`, `tqdm` output, any file write (exception: `save_checkpoint_ddp` handles rank-gating internally), and `os.makedirs()`. Separate metric computation (`.avg` triggers `all_reduce`, a collective all ranks must call) from logging (gated on `is_main_process()`).

### 4. Data Pipeline

Port the user's real data semantics: dataset builders, transforms/tokenizers, collate function, batch structure, model-call inputs, labels, masks, and metadata. When `is_distributed()` (torchrun multi-GPU), adapt sampler, dataloader, seeding, and metric reduction for distributed training (`DistributedSampler` + `sampler.set_epoch(epoch)` for map-style datasets). On single-device (default), use a plain sampler.

Prefer decoupling dataset and data-loading logic into a separate helper file (e.g. `dataset.py` or `data_utils.py`) under `<output_dir>`. Inline data logic in the training script only when it is trivially short.

Keep supervised loss, auxiliary losses, validation metrics, and best-metric direction aligned with the user's original code.

Expose dataset paths as CLI arguments (e.g. `--data_dir`), not hardcoded literals. All generated data-loading functions must accept data paths as parameters; do not hardcode or derive paths from package locations.

#### DataLoader Launch Hygiene (mandatory)

On CUDA/NPU training boxes, a DataLoader with `num_workers>0` forks child processes; if the parent has already initialized CUDA, the forked workers crash (`CUDA initialization error`). `pin_memory=True` additionally errors on CUDA tensors.

- **`num_workers=0` by default**: all generated DataLoaders default to zero worker processes. The launcher exposes `NUM_WORKERS` with default `0`; do not change this default.
- **`pin_memory=False`**: all generated DataLoaders pass `pin_memory=False`.
- Both values are part of the launch contract, not style choices.

### 5. Subnet Extraction, Weight Inheritance, And Freeze Continuation

Extract one fixed subnet from the supernet for the selected architecture, strict-load its branch weights from the
supernet checkpoint, and continue the supernet training's freeze grouping. The selected architecture is the
Jinja-rendered `{{ psu_run_search.output.selected_arch }}` (a per-slot choice dict — one branch name per transformer
layer slot). Construct the generated `ArchConfig` from it, instantiate the supernet, load the trained checkpoint,
configure it, extract the standalone subnet, then release the supernet.

```python
import torch
from supernet import SearchSpace, ArchConfig, SuperNet
from nas_agent.train import empty_cache, load_checkpoint, resolve_device


# The selected architecture dict is rendered in at generation time (Jinja).
# Do NOT hardcode a path to it; it is a literal value supplied upstream.
SELECTED_ARCH = {{ psu_run_search.output.selected_arch }}


def build_selected_subnet(device, supernet_ckpt):
    """Construct supernet, load ckpt, configure the selected arch, extract standalone subnet, cleanup."""
    search_space = SearchSpace()
    supernet = SuperNet(search_space).to(device)
    # strict=True: the materialized subnet's state_dict keys are the canonical keys of the
    # selected branches under the original model topology (supernet.py contract). A silent
    # partial load would drop branch weights and corrupt the finetune initialization —
    # key mismatches must fail loud with the unmatched-key list.
    load_checkpoint(supernet_ckpt, supernet, device, strict=True)
    arch_config = ArchConfig(**SELECTED_ARCH)
    supernet.set_sample_config(arch_config)
    subnet = supernet.get_active_subnet()
    del supernet
    empty_cache(device)
    return subnet
```

Put this inheritance logic in `finetune.py` and import it from `retrain.py`, so the weight-injection seam is
isolated. `finetune.py` is always generated (the strategy is constant) and additionally owns:

- **Freeze continuation**: after extraction, re-apply the supernet training's freeze grouping — only the selected
  path's variant-branch parameters are trainable; original branches and non-slot modules stay
  `requires_grad_(False)`. The optimizer receives only the trainable (filtered) parameter set — never bare
  `model.parameters()`.
- **Frozen teacher construction**: an independent original-model instance built via `load_pretrained.py` (its
  module-level `PRETRAINED_CKPT` constant is the weight source; `--teacher_ckpt` is asserted equal to it)
  (`requires_grad_(False)` + `.eval()`), never extracted from the supernet.
- **KD loss seam**: the hidden-cosine + logits-KL helpers mirrored verbatim from `train_supernet.py`.

Mirror `evaluator.py`'s concrete extraction route for the retrain script. Do not hardcode `supernet.py` internals —
call only the manifest-exposed APIs. If a needed API is not exposed, fail loud (do not work around `supernet.py`).

### 6. Optimizer, Scheduler, AMP, And Gradient Clipping

Reuse the user's optimizer and scheduler config. Do not introduce a generic learning-rate schedule unless the user's project has no scheduler and the user explicitly accepts a fallback.

**Budget rule (mandatory, do not guess):** read the original project's training code to determine the training budget, optimizer, and scheduler. Search-time budgets in `evaluator.py` are reduced for throughput and must NOT be used as the retrain baseline. Retrain uses **full evaluation** (the entire validation set, no subsampling). Use a **moderate finetune budget** — enough to adapt the inherited branch weights to convergence under the KD recipe, referencing the original project's training configuration and scaling down sensibly.

When the budget differs from the original project's, adjust budget-dependent hyperparameters (LR scheduler total steps/epochs, warmup steps, decay milestones) accordingly.

#### LR Scheduler

Preserve the original project's scheduler step granularity (per-epoch vs per-batch).

#### Batch Size & Learning Rate

`--batch_size` is per-device. Under DDP the effective batch size is `batch_size * world_size`. Use the user's original LR as the `--lr` default and pass `args.lr` directly to the optimizer. Reuse any DDP-aware LR scaling rule the user's original code already has.

#### Gradient Clipping

Add `--max_grad_norm` with default `1.0`. When `--max_grad_norm > 0`, call `torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)` after `loss.backward()` and before the optimizer step. If AMP scaling is enabled, call `scaler.unscale_(optimizer)` before clipping.

#### NPU Compatibility: Disable `foreach` Optimizations

Huawei Ascend NPU does not support PyTorch's `foreach`-based multi-tensor optimization. When the resolved device type is `"npu"`, pass `foreach=False` to both optimizer constructors and gradient clipping utilities. Determine `is_npu` once after `setup_distributed()` and reuse it:

```python
is_npu = device.type == "npu"
optimizer = optim.AdamW(model.parameters(), ..., foreach=False if is_npu else None)
torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm, foreach=False if is_npu else None)
```

### 7. Checkpoint

Save `retrain_latest.pth` whenever the generated script is scheduled to save a checkpoint. Use progress-specific snapshot names, such as `retrain_epoch_<epoch:04d>.pth` (epoch-based) or `retrain_step_<global_step:08d>.pth` (step-based). Save `retrain_best.pth` when the validation metric improves. Expose `--save_interval` to control intermediate snapshots with a reasonable default.

When evaluation is scheduled for the same progress unit, save `retrain_latest.pth` **after** evaluation and `best_metric` update, so `best_metric` in the checkpoint reflects the most recent result.

**Final-ckpt contract (must not drift):** the final best checkpoint is always written to `$ORCA_ARTIFACTS_DIR/runs/retrain/retrain_best.pth`. This path is the contractual default consumed by `psu_retrain`'s `status.sh` / `emit_result.py`; a different path breaks completion detection.

Use `save_checkpoint_ddp` from `nas_agent.train` for all checkpoint writes. It automatically unwraps `DistributedDataParallel`, gates the write on rank 0, and barriers other ranks. Do **not** wrap `save_checkpoint_ddp()` inside `if is_main_process()` — its internal `barrier()` would deadlock multi-GPU.

```python
from nas_agent.train import save_checkpoint_ddp

save_checkpoint_ddp(
    os.path.join(args.output_dir, "retrain_latest.pth"),
    model,
    optimizer=optimizer,
    scheduler=scheduler,
    scaler=scaler,
    epoch=epoch,
    global_step=global_step,
    best_metric=best_metric,
    args=vars(args),
)
```

### 8. Training Loop (frozen-teacher KD finetune)

Retrain finetunes **one fixed subnet** under the same frozen-teacher KD recipe the supernet training used: the
teacher (an independent frozen pretrained original-model instance from `finetune.py`) runs a `no_grad` forward each
step; the student is the materialized subnet; the loss = hidden cosine + logits KL, composed by the helpers mirrored
from `train_supernet.py` (never re-invented here). The subnet's trainable set is the freeze-continuation filtered
parameter set from §5. This KD replacement of the user's supervised loss is a **declared designed deviation** —
record it in the fidelity intended-behavior statement.

```python
import torch
from nas_agent.train import autocast, is_distributed
from finetune import kd_loss

model.train()
teacher.eval()  # frozen: requires_grad_(False), never in the optimizer
for epoch in range(start_epoch, args.epochs):
    if is_distributed() and isinstance(train_sampler, DistributedSampler):
        train_sampler.set_epoch(epoch)
    for inputs, targets in train_loader:
        inputs = inputs.to(device, non_blocking=True)
        targets = targets.to(device, non_blocking=True)
        optimizer.zero_grad(set_to_none=True)
        with autocast(device, enabled=args.amp):
            with torch.no_grad():
                teacher_outputs = teacher(inputs)
            outputs = model(inputs)
            loss = kd_loss(outputs, teacher_outputs)  # hidden cosine + logits KL (mirrors train_supernet.py)
        scaler.scale(loss).backward()
        if args.max_grad_norm > 0:
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(
                trainable_params, args.max_grad_norm,   # freeze-continuation filtered set
                foreach=False if is_npu else None,
            )
        scaler.step(optimizer)
        scaler.update()
        if scheduler is not None:
            scheduler.step()
        global_step += 1
        # emit telemetry line + progress.jsonl (§3(a) per unit; §3(b) every --progress-every
        # steps + unit end; §3(c) [eval]/done-best terminal metric lines)
```

Adapt task-specific batch unpacking, model call, and scheduler timing to the user's project (teacher/student forward
signatures follow the same ported conventions). There is no task loss term — the KD recipe is pure distillation.

### 9. Evaluation (single subnet)

Run evaluation every `--eval_interval` in the generated script's progress unit, and once at the final training boundary. Unlike supernet training, there is only **one** subnet to evaluate — no max/min config switching. Evaluate the fixed subnet with the user's validation metric, ported wholesale from the original project's evaluation function.

```python
def run_project_validation(model, val_loader, device):
    # Port the user's original evaluation function verbatim (signature, metric computation).
    ...
    return primary_metric_value
```

#### DDP Metric Aggregation (when `is_distributed()`)

When the validation set is sharded across ranks, use `AverageMeter` from `nas_agent.train` to accumulate running totals per rank. `.avg` triggers `all_reduce` across ranks and returns a Python `float`. On single-device, `AverageMeter.avg` is a no-op.

**CRITICAL: `.avg` and `.count` are collective operations.** Compute `.avg` on **all ranks** first, then gate only the logging/printing/checkpoint decision on `is_main_process()`. Never place an `.avg` or `.count` call inside an `if is_main_process():` block — it causes a multi-GPU deadlock.

**Validation metrics flow to the live chart.** After computing aggregated validation metrics on rank 0, append them to the §3(b) progress JSONL under their real names so they appear on the live chart alongside training metrics. Omit validation entries on progress units where no evaluation runs. Also print the §3(c) `[eval] unit <N> <metric> <value>` line at every eval point — it is the deterministic source for the final report's `final_metrics`.

Select `retrain_best.pth` based on the validation metric, using the user's own metric direction (maximize accuracy/F1/mAP; minimize loss/error/WER). When the project computes multiple validation metrics, choose the most representative one for best-checkpoint selection.

## Run Launcher

Generate `run_retrain.sh` as the launcher for subnet retraining. The **default launcher is single-process `python3`** (no torchrun, no DDP). For multi-GPU, the user switches to `torchrun --nproc_per_node=N`.

Launcher skeleton (the strategy is constant finetune-from-supernet — both ckpt variables are always present):

```bash
#!/usr/bin/env bash
set -euo pipefail

# ── Editable variables ──────────────────────────────────────────────
DATA_DIR="/path/to/dataset"
OUTPUT_DIR="runs/retrain"
SUPERNET_CKPT="runs/train/supernet_best.pth"
TEACHER_CKPT="{{ inputs.pretrained_ckpt }}"
EPOCHS=100
BATCH_SIZE=64
LR=1e-3
NUM_WORKERS=0          # DataLoader Launch Hygiene; do not change default
EVAL_INTERVAL=1
SEED=42
MAX_GRAD_NORM=1.0
PROGRESS_EVERY=50       # progress.jsonl chart-feed granularity (optimizer steps; unit end always writes)
AMP=false              # AMP disabled by default (single-device)
# Multi-GPU: replace the python3 line below with:
#   torchrun --nproc_per_node=N retrain.py ... (same args)

# ── Launch retraining ───────────────────────────────────────────────
AMP_FLAG=""
[ "$AMP" = true ] && AMP_FLAG="--amp"

python3 retrain.py \
    --data_dir "$DATA_DIR" \
    --output_dir "$OUTPUT_DIR" \
    --supernet_ckpt "$SUPERNET_CKPT" \
    --teacher_ckpt "$TEACHER_CKPT" \
    --epochs "$EPOCHS" \
    --batch_size "$BATCH_SIZE" \
    --lr "$LR" \
    --num_workers "$NUM_WORKERS" \
    --eval_interval "$EVAL_INTERVAL" \
    --seed "$SEED" \
    --max_grad_norm "$MAX_GRAD_NORM" \
    --progress-every "$PROGRESS_EVERY" \
    $AMP_FLAG
```

After writing, mark executable: `chmod +x run_retrain.sh`.

Before finalizing, cross-check every `--arg_name` in the `python3` invocation against the generated `retrain.py` argparse definitions. Run `python retrain.py --help` to confirm every shell variable passed as a CLI flag is accepted.

## Validation

The generated artifacts are for remote-server execution by the downstream `psu_retrain` node. Local validation is layered: static checks first, then a functional smoke test.

Allowed:

- `bash -n run_retrain.sh`
- `python -m py_compile retrain.py` + `python -m py_compile finetune.py` (both are always generated — the strategy is constant)
- **Hard gate (deterministic, must pass):** `bash "$ORCA_AGENT_RESOURCES/scripts/check_retrain_script.sh"` — validates py_compile, conditional DDP, guarded `sync_random_seed`, launcher hygiene (delegated to `check_launcher.sh`), the progress.jsonl write contract, and the teacher/KD static gates. On failure → fix and re-run.
- **Diagnostic check (does not modify files):** `ruff check --no-fix --config <nas_agent_root>/nas_agent/internal_ruff_check.toml retrain.py`. Fix any reported errors (undefined names, missing imports) and re-run.
- **Launcher-script CLI consistency:** run `python retrain.py --help` and verify every `--flag` in `run_retrain.sh` is accepted.
- **Budget-hyperparameter coherence:** verify budget-dependent hyperparameters are coherent with the moderate finetune budget.
- **KD/file-set coherence:** verify the generated file set matches the constant strategy — `retrain.py` + `finetune.py` + `run_retrain.sh` with `SUPERNET_CKPT` / `--supernet_ckpt` and `TEACHER_CKPT` / `--teacher_ckpt`; the KD loss helpers in `finetune.py` mirror `train_supernet.py`; the teacher is constructed via `load_pretrained.py` and frozen (`requires_grad_(False)` + `.eval()` + `no_grad` forward); the optimizer receives only the freeze-continuation trainable set.
- **Device placement consistency:** review each generated `.py` file for device placement consistency. All tensors in the same operation must reside on the same device; GPU/NPU tensors are moved to CPU before NumPy/Python scalar conversion.
- **Functional smoke test (always):** write it as the persistent script `<output_dir>/tests/test_retrain_smoke.py` (plain script starting with the sibling-import `sys.path` bootstrap) and run it from `<output_dir>`. Exercise subnet extraction + strict branch-weight loading (`build_selected_subnet`), a frozen-teacher forward, the KD loss computation + backward + optimizer step (on the freeze-continuation filtered parameter set), validation metric computation, and checkpoint writing — on a single device, without torchrun. If any test fails, fix the code and re-run. The persistent test stays synthetic-only so it runs anywhere.

Forbidden:

- Do not run `run_retrain.sh` at full scale or with the production budget. Full execution is the downstream `psu_retrain` node's job.
- Do not download datasets.

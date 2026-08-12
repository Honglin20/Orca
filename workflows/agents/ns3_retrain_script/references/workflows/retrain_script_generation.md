# Retrain Script Generation Workflow

Use this workflow to generate `<output_dir>/retrain.py`, `<output_dir>/run_retrain.sh`, and (for the finetune-from-supernet strategy) `<output_dir>/finetune.py`, plus any small helper files needed to obtain the **final weights of the one selected subnet**. Do not start training in this node — the downstream `ns3_retrain` node executes the generated launcher.

The goal is to produce a project-specific retraining entry point for the single architecture chosen upstream by `ns3_run_search`: reuse the user's real dataset loading, training, loss, evaluation, and checkpoint conventions, then train that one fixed subnet (either inheriting supernet weights or from scratch) to convergence.

## Strategy Decision (the spine of this workflow)

Retrain strategy is a **deterministic binary decision driven by supernet availability** — it is NOT bound to the search-time `evaluation_paradigm` and does not require LLM judgment. Decide it first; it determines which files to generate.

Read the supernet-availability signal from two deterministic sources and AND them:

1. `$ORCA_ARTIFACTS_DIR/supernet_summary.md` → the **Supernet Training Viability** section: `viable: Yes` / `viable: No`.
2. The supernet checkpoint file on disk. Its path is the `supernet_ckpt_path` configured in `$ORCA_ARTIFACTS_DIR/search_config.yaml` (contractual default `runs/train/supernet_best.pth`). The file must exist and be non-empty.

Branch:

- **`finetune-from-supernet`** — when viability is `Yes` **AND** the supernet ckpt file exists. Generate `retrain.py` (main entry, the training loop) + `finetune.py` (subnet-weight inheritance logic) + `run_retrain.sh`. The subnet is extracted from the supernet **with inherited weights** as the initialization, then incrementally fine-tuned.
- **`train-from-scratch`** — the fallback, when viability is `No` **OR** the supernet ckpt is absent. Generate `retrain.py` (main entry) + `run_retrain.sh` only. The subnet is extracted **without** loading any supernet ckpt and re-initialized from scratch, then trained with a full budget.

Record the chosen strategy in `strategy_reason` (one line of project evidence, e.g. `supernet viable=Yes, ckpt runs/train/supernet_best.pth present` or `supernet viable=No per summary`).

## Source Evidence

Build the script from the upstream artifacts, the user's own training code, and the generated supernet. The generated script must follow the user's dataset, preprocessing, batch format, model-call signature, loss, metrics, optimizer, scheduler, logging, checkpoint, and runtime conventions.

Read these to fill the script (cwd-independent under `$ORCA_ARTIFACTS_DIR`):

- `AGENTS.md` — the scaffold written by `ns3_search_pipeline`. Its **Final Weight Acquisition** section holds the two-branch strategy, subnet-extraction reference, script requirements, launcher skeleton, and validation steps. It is the primary guide; this workflow refines it into exact contracts.
- `supernet_summary.md` — supernet training viability (the strategy signal), evaluation paradigm, KD decision, generated-artifact list.
- `project_manifest.md` — the original project's training/evaluation semantics, data/environment details, and the navigation index of key source files. Use it as the map before opening files under the Original Project Root.
- `supernet.py` — the `SearchSpace`, `ArchConfig`, `SuperNet` API surface (`build_supernet` / `set_sample_config` / `get_active_subnet`, etc.). Call the supernet ONLY through the APIs the manifest/scaffold expose; never hardcode its internals.
- `train_supernet.py` (present only when viability is `Yes`) — training conventions, data pipeline, AMP, checkpoint policy to mirror.
- `evaluator.py` — the subnet-extraction and weight-initialization behavior actually used during search; mirror its extraction route for the retrain script.
- `{{ inputs.project_root }}` — the original project training code: training budget, optimizer, scheduler, initialization, loss/metric formulas.

Use `project_manifest.md` to navigate the user's training code: read the specific files under the Original Project Root it points to instead of bulk-reading the project, and when a needed detail is missing from the manifest or looks inaccurate, explore the project (targeted) and update the manifest in place, since source code is always authoritative.

Generated artifacts must be self-contained for project-specific code. Do not import modules from the **user's project**. Copy and adapt any required project logic into `retrain.py` / `finetune.py` or helper files under `<output_dir>`. Apart from the Python standard library, installed third-party packages, and `nas_agent`, generated artifacts should import only files under `<output_dir>`.

### Ported Helper Files

When the calling skill delegated porting to `project-porter` subagents, the ported helper files already exist under `<output_dir>`: import them as siblings and write call sites against the porter's API report instead of re-porting. Interface mismatches are resolved in this workflow as they surface: adapt your call sites or edit the helper files directly, never add wrapper layers. When touching ported logic (formulas, control flow, constants), preserve the original project's semantics.

## Generation Rules

Write the generated retrain script with these sections and contracts.

### 1. CLI And Runtime Args

Use a stable base CLI for the retrain runtime:

- `--output_dir`: Default `"runs/retrain"`; the final ckpt is always written to `<output_dir>/retrain_best.pth` (contract path consumed by `ns3_retrain`'s status/emit scripts — must not drift).
- `--eval_interval`: Evaluation frequency in the generated script's chosen progress unit.
- `--device`: Default `"auto"`, allow choices `["auto", "cuda", "npu", "cpu"]`.
- `--amp`: Enable AMP.
- `--lr`: Learning rate; default from the user's original training config.
- `--max_grad_norm`
- `--seed`
- `--resume`: Resume from `<output_dir>/retrain_latest.pth` if present.
- `--supernet_ckpt` (finetune-from-supernet only): path to the trained supernet checkpoint used to initialize the subnet.

Expose project-derived training and runtime arguments from the user's project, such as dataset path (`--data_dir`), training budget (`--epochs` or `--max_steps`), `--batch_size`, `--num_workers`, optimizer and scheduler hyperparameters, augmentation flags, validation controls, and task options. Preserve the user's defaults where they exist, but allow CLI overrides for remote runs.

### 2. Distributed Setup

The **default launcher is single-process `python`** (no torchrun, no DDP wrap). When launched with `torchrun --nproc_per_node=N` (multi-GPU), `RANK` env is present → `is_distributed()=True` → DDP wrap activates automatically.

Do not infer the target GPU/NPU runtime from the current machine. The generated script is intended for a remote training server, so device and backend selection must remain runtime-configurable through the launcher, environment, and `nas_agent.train`.

**Single-device path (default):** plain `python retrain.py` (no `RANK` env) → `setup_distributed()` does **not** `init_process_group` → `is_distributed()=False` → `get_world_size()=1` / `is_main_process()=True` / `barrier()` no-op.

**Multi-GPU path (when `torchrun` used):** `RANK` env present → `setup_distributed()` calls `init_process_group` → `is_distributed()=True` → DDP wrap, `DistributedSampler`, `all_reduce` all active.

Use `nas_agent.train.distributed` to configure distributed setup, device resolution, DDP-safe helpers, and AMP.

**DDP wrap is conditional (when `is_distributed()`):** Only wrap in `DistributedDataParallel` when `is_distributed()` returns True. On single-device, skip DDP entirely.

**AMP usage rule:** Use `autocast()` and `grad_scaler()` from `nas_agent.train.distributed`. Keep the autocast enable flag independent from `scaler.is_enabled()`: `autocast(device, enabled=args.amp)`. `autocast(device, enabled=False)` is a nullcontext—orthogonal to single-device.

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

Note: a fixed subnet has no unused-parameter hazard from sandwich sampling, but `find_unused_parameters=True` is still safe and keeps the wrap uniform with the supernet-training convention.

### 3. Progress Driver

First choose the generated script's training progress unit from the user's project. Use epochs only when the original training code and data pipeline are naturally epoch-based. Otherwise, use optimizer update count, stored as `global_step`.

Use that same progress unit consistently for the training horizon, `--eval_interval`, scheduler stepping, checkpoint save interval, logging interval, and final validation. Do not force streaming or iterable inputs into artificial epochs.

Do **not** port the user's complex logging frameworks (WandB, TensorBoard, custom file loggers). Use the simple stdout progress tracking below.

Provide real-time training progress via periodic batch-level logging (rank 0 only). Without batch-level logs, a long-running epoch produces no output and looks indistinguishable from a hang.

- **Primary approach (`tqdm`)**: `disable=not is_main_process()`. For epoch-based training, wrap the batch iterator per epoch. For step-based training, use a single bar tracking `global_step`.
- **Fallback approach (`print`)**: a periodic `print` statement (e.g. `if global_step % args.log_interval == 0:`).

**CRITICAL — machine-parseable progress (contract, mandatory).** On top of the human-readable progress, the generated script **must** emit **two** machine feeds per completed progress unit (rank 0 only). Both are required. **The user's original code is the sole authority for which metrics exist and what they are named; `loss` is merely a common example, never an assumption.**

**(a) Telemetry line (stdout)** — consumed by `ns3_retrain` ETA / health / warmup (line-oriented, structurally regex-parsed). Print exactly one line per progress unit:
- epoch-based: `epoch <cur>/<total> <primary_metric> <value>` — e.g. `epoch 3/10 loss 0.4521`
- step-based: `step <cur>/<total> <primary_metric> <value>` — e.g. `step 1200/6000 psnr 28.4`

`<primary_metric>` is the **user's primary training scalar under its real name** (`loss`, `reward`, `gain`, `psnr`, …). Do **not** hardcode the literal word `loss` when the user's metric is named otherwise. Do not print other lines containing the bare words `epoch`/`step` followed by digits in a different meaning (e.g. a save message like "saved retrain_epoch_0005.pth" — use "checkpoint epoch 5 saved" instead) so the downstream regex cannot misparse. Expose the horizon as `--epochs N` (epoch-based) or `--max_steps N` (step-based).

**(b) Progress JSONL (chart feed)** — consumed by `ns3_retrain`'s live chart watcher. Append one JSON line per progress unit to `$ORCA_ARTIFACTS_DIR/runs/retrain/progress.jsonl`:

```
{"step": <cur>, "metrics": {"<name>": <float>, ...}}
```

- `step` = the same `<cur>` as the telemetry line (int).
- `metrics` = **every scalar metric this progress unit produces** — the full set the user's original training AND evaluation code tracks, each under its real name. **Loss is not assumed and not special**: if the user's code logs `reward` and `gain` but no `loss`, emit `reward` and `gain` and nothing else.
- Open in append mode, write `json.dumps(row) + "\n"`, `flush()` after each write, guard with `if is_main_process()`. The launcher truncates this file at the start of each attempt, so each attempt starts fresh.

**CRITICAL: Guard all single-writer side effects with `if is_main_process()`** so only rank 0 performs them: `print()`, `tqdm` output, any file write (exception: `save_checkpoint_ddp` handles rank-gating internally), and `os.makedirs()`. Separate metric computation (`.avg` triggers `all_reduce`, a collective all ranks must call) from logging (gated on `is_main_process()`).

### 4. Data Pipeline

Port the user's real data semantics: dataset builders, transforms/tokenizers, collate function, batch structure, model-call inputs, labels, masks, and metadata. When `is_distributed()` (torchrun multi-GPU), adapt sampler, dataloader, seeding, and metric reduction for distributed training (`DistributedSampler` + `sampler.set_epoch(epoch)` for map-style datasets). On single-device (default), use a plain sampler.

Prefer decoupling dataset and data-loading logic into a separate helper file (e.g. `dataset.py` or `data_utils.py`) under `<output_dir>`. Inline data logic in the training script only when it is trivially short.

Keep supervised loss, auxiliary losses, validation metrics, and best-metric direction aligned with the user's original code.

Expose dataset paths as CLI arguments (e.g. `--data_dir`), not hardcoded literals. All generated data-loading functions must accept data paths as parameters; do not hardcode or derive paths from package locations.

#### DataLoader Launch Hygiene (mandatory)

On CUDA/NPU training boxes, a DataLoader with `num_workers>0` forks child processes; if the parent has already initialized CUDA, the forked workers crash (`CUDA initialization error`). `pin_memory=True` additionally errors on CUDA tensors. Both are real-world incidents.

- **`num_workers=0` by default**: all generated DataLoaders default to zero worker processes. The launcher exposes `NUM_WORKERS` with default `0`; do not change this default.
- **`pin_memory=False`**: all generated DataLoaders pass `pin_memory=False`.
- Both values are part of the launch contract, not style choices.

### 5. Subnet Extraction

Both strategies require extracting one fixed subnet from the supernet for the selected architecture. The selected architecture is the Jinja-rendered `{{ ns3_run_search.output.selected_arch }}` (a dict). Construct the generated `ArchConfig` from it, instantiate the supernet, optionally load the trained checkpoint, configure it, extract the standalone subnet, then release the supernet.

```python
import json
import torch
from supernet import SearchSpace, ArchConfig, SuperNet
from nas_agent.train import empty_cache, load_checkpoint, resolve_device


# The selected architecture dict is rendered in at generation time (Jinja).
# Do NOT hardcode a path to it; it is a literal value supplied upstream.
SELECTED_ARCH = {{ ns3_run_search.output.selected_arch }}


def build_selected_subnet(device, supernet_ckpt=None):
    """Construct supernet, configure the selected arch, extract standalone subnet, cleanup."""
    search_space = SearchSpace()
    supernet = SuperNet(search_space).to(device)
    if supernet_ckpt is not None:
        load_checkpoint(supernet_ckpt, supernet, device, strict=False)
    arch_config = ArchConfig(**SELECTED_ARCH)
    supernet.set_sample_config(arch_config)
    subnet = supernet.get_active_subnet()
    del supernet
    empty_cache(device)
    return subnet
```

Strategy-specific extraction:

- **`finetune-from-supernet`**: call `build_selected_subnet(device, supernet_ckpt=args.supernet_ckpt)` — the subnet inherits trained supernet weights as initialization. Put this inheritance logic in `finetune.py` and import it from `retrain.py`, so the weight-injection seam is isolated.
- **`train-from-scratch`**: call `build_selected_subnet(device, supernet_ckpt=None)` — do NOT load the supernet checkpoint. After extraction, **re-initialize** the subnet weights using the same initialization logic as the search evaluator's `train_from_scratch` path (see `evaluator.py`) and any project-specific initialization from the original project.

Mirror `evaluator.py`'s concrete extraction and initialization logic; adapt it for the retrain script. Do not hardcode `supernet.py` internals — call only the manifest/scaffold-exposed APIs. If a needed API is not exposed, fail loud (do not work around `supernet.py`).

### 6. Optimizer, Scheduler, AMP, And Gradient Clipping

Reuse the user's optimizer and scheduler config. Do not introduce a generic learning-rate schedule unless the user's project has no scheduler and the user explicitly accepts a fallback.

**Budget rule (mandatory, do not guess):** read the original project's training code to determine the training budget, optimizer, and scheduler. Search-time budgets in `evaluator.py` are reduced for throughput and must NOT be used as the retrain baseline. Retrain uses **full evaluation** (the entire validation set, no subsampling). Strategy-specific guidance:

- **`finetune-from-supernet`**: use a moderate budget — enough to adapt the inherited supernet weights to the specific subnet topology, not a full from-scratch schedule. Reference the original project's training configuration and scale down sensibly.
- **`train-from-scratch`**: use a full training budget, significantly larger than `evaluator_cfg.epochs` from search, referencing the original project's training configuration.

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

**Final-ckpt contract (must not drift):** the final best checkpoint is always written to `$ORCA_ARTIFACTS_DIR/runs/retrain/retrain_best.pth`. This path is the contractual default consumed by `ns3_retrain`'s `status.sh` / `emit_result.py`; a different path breaks completion detection.

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

### 8. Training Loop

Retrain trains **one fixed subnet** (no sandwich sampling, no KD). The loop is a standard supervised training loop: forward → loss → backward → gradient clip → optimizer step → scheduler step. The only strategy-specific aspect is the subnet's weight initialization, handled in §5 Subnet Extraction before the loop begins.

```python
import torch
from nas_agent.train import autocast, is_distributed

model.train()
for epoch in range(start_epoch, args.epochs):
    if is_distributed() and isinstance(train_sampler, DistributedSampler):
        train_sampler.set_epoch(epoch)
    for inputs, targets in train_loader:
        inputs = inputs.to(device, non_blocking=True)
        targets = targets.to(device, non_blocking=True)
        optimizer.zero_grad(set_to_none=True)
        with autocast(device, enabled=args.amp):
            outputs = model(inputs)
            loss = criterion(outputs, targets)
        scaler.scale(loss).backward()
        if args.max_grad_norm > 0:
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(
                model.parameters(), args.max_grad_norm,
                foreach=False if is_npu else None,
            )
        scaler.step(optimizer)
        scaler.update()
        if scheduler is not None:
            scheduler.step()
        global_step += 1
        # emit telemetry line + progress.jsonl (§3 contract) on this progress unit
```

Adapt task-specific batch unpacking, model call, loss, and scheduler timing to the user's project.

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

**Validation metrics flow to the live chart.** After computing aggregated validation metrics on rank 0, append them to the §3(b) progress JSONL under their real names so they appear on the live chart alongside training metrics. Omit validation entries on progress units where no evaluation runs.

Select `retrain_best.pth` based on the validation metric, using the user's own metric direction (maximize accuracy/F1/mAP; minimize loss/error/WER). When the project computes multiple validation metrics, choose the most representative one for best-checkpoint selection.

## Run Launcher

Generate `run_retrain.sh` as the launcher for subnet retraining. The **default launcher is single-process `python3`** (no torchrun, no DDP). For multi-GPU, the user switches to `torchrun --nproc_per_node=N`.

Launcher skeleton (finetune-from-supernet shown; for train-from-scratch drop `SUPERNET_CKPT` and the `--supernet_ckpt` line):

```bash
#!/usr/bin/env bash
set -euo pipefail

# ── Editable variables ──────────────────────────────────────────────
DATA_DIR="/path/to/dataset"
OUTPUT_DIR="runs/retrain"
SUPERNET_CKPT="runs/train/supernet_best.pth"   # finetune-from-supernet only; remove for train-from-scratch
EPOCHS=100
BATCH_SIZE=64
LR=1e-3
NUM_WORKERS=0          # DataLoader Launch Hygiene; do not change default
EVAL_INTERVAL=1
SEED=42
MAX_GRAD_NORM=1.0
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
    --epochs "$EPOCHS" \
    --batch_size "$BATCH_SIZE" \
    --lr "$LR" \
    --num_workers "$NUM_WORKERS" \
    --eval_interval "$EVAL_INTERVAL" \
    --seed "$SEED" \
    --max_grad_norm "$MAX_GRAD_NORM" \
    $AMP_FLAG
```

After writing, mark executable: `chmod +x run_retrain.sh`.

Before finalizing, cross-check every `--arg_name` in the `python3` invocation against the generated `retrain.py` argparse definitions. Run `python retrain.py --help` to confirm every shell variable passed as a CLI flag is accepted.

## Validation

The generated artifacts are for remote-server execution by the downstream `ns3_retrain` node. Local validation is layered: static checks first, then a functional smoke test.

Allowed:

- `bash -n run_retrain.sh`
- `python -m py_compile retrain.py` (+ `finetune.py` if generated)
- **Hard gate (deterministic, must pass):** `bash "$ORCA_AGENT_RESOURCES/scripts/check_retrain_script.sh"` — validates py_compile, conditional DDP, guarded `sync_random_seed`, launcher hygiene (delegated to `check_launcher.sh`), and the progress.jsonl write contract. On failure → fix and re-run.
- **Diagnostic check (does not modify files):** `ruff check --no-fix --config <nas_agent_root>/nas_agent/internal_ruff_check.toml retrain.py`. Fix any reported errors (undefined names, missing imports) and re-run.
- **Launcher-script CLI consistency:** run `python retrain.py --help` and verify every `--flag` in `run_retrain.sh` is accepted.
- **Budget-hyperparameter coherence:** verify budget-dependent hyperparameters are coherent with the chosen strategy's budget (moderate for finetune-from-supernet, full for train-from-scratch).
- **Strategy coherence:** verify the generated files match the decided strategy — `finetune.py` + `--supernet_ckpt` present iff `finetune-from-supernet`; neither present iff `train-from-scratch`.
- **Device placement consistency:** review each generated `.py` file for device placement consistency. All tensors in the same operation must reside on the same device; GPU/NPU tensors are moved to CPU before NumPy/Python scalar conversion.
- **Functional smoke test (always):** write it as the persistent script `<output_dir>/tests/test_retrain_smoke.py` (plain script starting with the sibling-import `sys.path` bootstrap) and run it from `<output_dir>`. Exercise subnet extraction (`build_selected_subnet`), a forward pass with dummy inputs, loss + backward + optimizer step, validation metric computation, and checkpoint writing — on a single device, without torchrun. If any test fails, fix the code and re-run. The persistent test stays synthetic-only so it runs anywhere.

Forbidden:

- Do not run `run_retrain.sh` at full scale or with the production budget. Full execution is the downstream `ns3_retrain` node's job.
- Do not download datasets.

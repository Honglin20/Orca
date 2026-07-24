# Train Supernet Script Generation Workflow

Use this workflow for `<output_dir>/supernet.py` to generate `<output_dir>/train_supernet.py`, `<output_dir>/run_train_supernet.sh`, and any small helper files needed by the generated training workflow. Do not start training locally.

The goal is to produce a project-specific supernet training entry point: reuse the user's real dataset loading, training, loss, evaluation, and checkpoint conventions, then train the supernet with sandwich sampling.

## Source Evidence

Build the script from the Step 1 project context, the user's own training code, and the Step 6 `<output_dir>/supernet.py` and `<output_dir>/inspect_supernet.py`. The generated script must follow the user's dataset, preprocessing, batch format, model-call signature, loss, metrics, optimizer, scheduler, logging, checkpoint, and runtime conventions.

Read the files under `<user_project_root>` to reference the user's training behavior. Refer to the generated `supernet.py` and `inspect_supernet.py` for supernet construction and sampling constraints. The inspector defines, for each elastic parameter, which end of its value range is "max" and which is "min"; all max/min config construction in the training script must follow the same convention.

Generated artifacts must be self-contained for project-specific code. Do not import modules from the **user's project** (`<user_project_root>`). Copy and adapt any required project logic into `train_supernet.py` or helper files under `<output_dir>`. Apart from the Python standard library, installed third-party packages, and `nas_agent`, generated artifacts should import only files under `<output_dir>`.

## Generation Rules

Write the generated training script with these sections and contracts.

### 1. CLI And Runtime Args

Use a stable base CLI for NAS training runtime:

- `--output_dir`: Default `"runs/train"`; all checkpoints (`supernet_latest.pth`, `supernet_best.pth`, progress snapshots) and training logs are written here
- `--eval_interval`: Evaluation frequency measured in the generated script's chosen training progress unit
- `--device`: Default `"auto"`, allow choices `["auto", "cuda", "npu", "cpu"]`
- `--amp`: Enable AMP (Automatic Mixed Precision)
- `--lr`: Learning rate; default from the user's original training config
- `--max_grad_norm`
- `--sandwich_n_random`: Number of random subnets sampled per sandwich step; default `2`
- `--seed`

KD arguments (See Distillation for details):
- `--kd_weight`: KD loss coefficient; default `1.0`
- `--kd_warmup_start`: Training progress value at which KD begins, measured in the same unit as `--eval_interval`; default `0`
- `--kd_warmup_length`: Duration over which the KD weight ramps from `0` to `--kd_weight`, in the same unit; default `0` (no ramp)

Expose project-derived training and runtime arguments from the user's project, such as dataset/config paths, training budget, batch size, worker count, optimizer and scheduler hyperparameters, augmentation flags, validation controls, and task options. Preserve the user's defaults where they exist, but allow CLI overrides for remote runs.

### 2. Distributed Setup

Unless the user specifies another target, generate for a single-node 8-device training platform. The remote platform may be NVIDIA GPU or Huawei Ascend NPU.

Do not infer the target GPU/NPU runtime from the current machine. The generated script is intended for a remote training server, so device and backend selection must remain runtime-configurable through the launcher, environment, and `nas_agent.train`.

The generated script always runs under `torchrun` (even single-GPU uses `--nproc_per_node=1`), so `setup_distributed()` is guaranteed to initialize the process group and DDP is always active.

Use `nas_agent.train.distributed` to configure distributed setup, device resolution, DDP-safe helpers, and AMP.

**DDP unwrap rule:** `DistributedDataParallel` delegates all standard `nn.Module` methods — `forward()` (i.e. `model(x)`), `parameters()`, `train()`, `eval()`, `state_dict()`, `zero_grad()`, etc. — so calling them on the DDP-wrapped model works directly without unwrapping. Only custom attributes and methods defined by the supernet (e.g. `search_space`, `get_active_subnet()`, `elastic_num_params`) require `unwrap_model()` to access the inner module. In practice, the most frequent NAS-specific call in the training loop is `set_sample_config`, which `set_sample_config_ddp()` already handles; manual `unwrap_model()` is only needed for occasional access outside the training loop such as reading `search_space` or extracting a subnet.

**AMP usage rule:** Use `autocast()` and `grad_scaler()` from `nas_agent.train.distributed` for AMP; they prefer PyTorch native `torch.amp` APIs for CUDA and NPU. Keep the autocast enable flag independent from `scaler.is_enabled()`. Use the user's AMP setting directly, e.g. `autocast(device, enabled=args.amp)`. `GradScaler` may be disabled on some devices (especially NPU where bf16 autocast is used without scaling), but autocast should still follow the user's AMP setting.

Use this template unless the user's project has stricter distributed or model-access conventions that must be preserved:

```python
from nas_agent.train.distributed import (
    autocast,
    get_local_rank,
    get_rank,
    grad_scaler,
    is_main_process,
    set_sample_config_ddp,
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

device_ids = None if device.type == "cpu" else [get_local_rank()]
model = DistributedDataParallel(
    model,
    device_ids=device_ids,
    find_unused_parameters=True,
)

...

# Standard nn.Module methods (forward, parameters, train, ...) work on the
# DDP-wrapped model directly.  Only supernet-specific members need unwrap.
output = model(data)                               # forward → no unwrap needed
set_sample_config_ddp(model, arch_config)          # handles unwrap internally
subnet = unwrap_model(model).get_active_subnet()   # custom method → unwrap
search_space = unwrap_model(model).search_space     # custom attribute → unwrap
```

Note: Keep `find_unused_parameters=True` for DDP-wrapped supernets because each sampled forward activates only part of the search space, so unsampled parameters may not contribute to the current loss.

### 3. Progress Driver

First choose the generated script's training progress unit from the user's project. Use epochs only when the original training code and data pipeline are naturally epoch-based. Otherwise, use optimizer update count, stored as `global_step`.

Use that same progress unit consistently for the training horizon, `--eval_interval`, scheduler stepping, checkpoint save interval, logging interval, and final validation. Do not force streaming or iterable inputs into artificial epochs just to match common image-classification scripts.

Do **not** port the user's complex logging frameworks (e.g. WandB, TensorBoard, custom file loggers). Instead, use the simple standard output (stdout) progress tracking described below. You may refer to basic logging parameters from the original project (e.g., `args.log_interval`) if applicable.


Provide real-time training progress via periodic batch-level logging (rank 0 only) inside the training loop. Without batch-level logs, a long-running epoch produces no output and makes it impossible to tell whether training is progressing or hung (especially when the dataset is large, the model forward is expensive, or sandwich sampling multiplies the per-batch cost).
- **Primary approach (`tqdm`)**: Use `tqdm` progress bars via `disable=not is_main_process()`. For epoch-based training, wrap the batch iterator so each epoch displays a per-batch progress bar. For step-based training without an epoch concept, use a single `tqdm` bar tracking `global_step` up to the total training horizon. Include running metrics (e.g. loss, learning rate) in the bar's `postfix` or `description`.
- **Fallback approach (`print`)**: If the user's original project environment is not suitable for `tqdm`, use a periodic `print` statement (e.g. `if global_step % args.log_interval == 0:`) instead.

**CRITICAL: Guard all single-writer side effects with `if is_main_process()` so only rank 0 performs them.** Failure to do so causes race conditions or duplicate outputs across ranks. Operations that require this guard include:

- **Logging**: `print()` statements, `tqdm` output, and any metric reporting
- **File writes**: any file output during training
  - Exception: `save_checkpoint_ddp()` does **not** need this guard. It already handles rank-gating and barriers internally (see Checkpoint section).
- **Directory creation**: `os.makedirs()` for output directories, log directories, etc.

**CRITICAL: Separate metric computation from logging.** `AverageMeter.avg` (returns `float`, not tensor) triggers `dist.all_reduce`, a collective operation that all ranks must call together. Compute `.avg` on all ranks first, then gate only the `print()` / `tqdm` / file-write on `is_main_process()`. See §9 Evaluation, DDP Metric Aggregation for the full rule, correct pattern, and anti-patterns.

When training data is sharded across ranks, metrics logged during training (loss, accuracy, etc.) must also be aggregated across ranks via `AverageMeter` so that `tqdm` or `print` displays globally correct values.

### 4. Data Pipeline

Port the user's real data semantics: dataset builders, transforms/tokenizers, collate function, batch structure, model-call inputs, labels, masks, and metadata. Adapt sampler, dataloader, seeding, and metric reduction for distributed training when the user's original code is single-device or incomplete. For map-style datasets, ensure `DistributedSampler` is used and `sampler.set_epoch(epoch)` is called at the beginning of each epoch under DDP. For iterable or streaming datasets, ensure data is sharded correctly across workers and ranks instead of using a sampler.

Prefer decoupling dataset and data-loading logic (dataset class, transforms, collate function, metric helpers, data-loading utilities) into a separate helper file (e.g., `dataset.py` or `data_utils.py`) under `<output_dir>`, keeping the training entry point focused on the training loop. Inline data logic in the training script only when it is trivially short.

Keep supervised loss, auxiliary losses, validation metrics, and best-metric direction aligned with the user's original code unless distributed execution requires a mechanical adjustment.

Expose dataset paths as CLI arguments (e.g. `--data_dir`), not hardcoded literals. When the user's data path under `<user_project_root>` can be identified, use its absolute path as the default. All generated data-loading functions must accept data paths as parameters; do not hardcode or derive paths from package locations inside function bodies.

### 5. Model Construction

Import `SearchSpace`, `ArchConfig`, and `SuperNet` from the generated `supernet.py` as a plain sibling import, eg: `from supernet import ArchConfig, SearchSpace, SuperNet`.

Instantiate `SearchSpace()` and `SuperNet(...)` using the generated constructor signature. Pass model-construction arguments (e.g. `num_classes`, `action_dim`) via the constructor, using values from the user's original project.

```python
search_space = SearchSpace()
model = SuperNet(search_space, num_classes=args.num_classes, ...)
```

### 6. Optimizer, Scheduler, AMP, And Gradient Clipping

Reuse the user's optimizer and scheduler config. Do not introduce a generic NAS learning-rate schedule unless the user's project has no scheduler and the user explicitly accepts a fallback.

Use the user's original single-model training hyperparameters as the baseline reference and scale the training budget to approximately **3×** (e.g. total epochs, optimizer steps, etc.). Sandwich training optimizes shared weights across multiple sub-architectures per step and needs a longer horizon to converge properly. When scaling the budget, also adjust budget-dependent hyperparameters (lr scheduler, warmup steps, decay milestones, etc.) accordingly; the specific adjustments depend on the project's training recipe and should be reasoned from context.

#### LR Scheduler

Preserve the original project's scheduler step granularity (e.g. if the original calls `scheduler.step()` once per epoch, keep it per epoch, not per batch).

Additionally, follow these rules:

#### Batch Size & Learning Rate

`--batch_size` is per-device. Under DDP the effective batch size is `batch_size * world_size`.

Use the user's original LR as the `--lr` default and pass `args.lr` directly to the optimizer. If the user's original training code already runs under DDP and includes an explicit LR scaling rule (e.g. linear scaling with world size), reuse that rule.

#### Gradient Clipping

Add `--max_grad_norm` as a stable numeric CLI argument with default `1.0`. When `--max_grad_norm > 0`, call `torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)` after all sandwich losses have called `backward()` and before the optimizer step. If AMP scaling is enabled, call `scaler.unscale_(optimizer)` before clipping.

#### NPU Compatibility: Disable `foreach` Optimizations

Huawei Ascend NPU does not support PyTorch's `foreach`-based multi-tensor optimization. In practice, the `foreach` parameter appears in two places in the training loop: optimizer constructors (`torch.optim.*`) and gradient clipping utilities (`clip_grad_norm_`, `clip_grad_value_`). When the resolved device type is `"npu"`, pass `foreach=False` to both. Determine `is_npu` once after `setup_distributed()` resolves the device and reuse it:

```python
is_npu = device.type == "npu"
```

For example:

```python
# Optimizer constructor
optimizer = optim.AdamW(model.parameters(), ..., foreach=False if is_npu else None)

# Gradient clipping utility
torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm, foreach=False if is_npu else None)
```

### 7. Checkpoint

Save `supernet_latest.pth` whenever the generated script is scheduled to save a checkpoint. Use progress-specific snapshot names, such as `supernet_epoch_<epoch:04d>.pth` for epoch-based training or `supernet_step_<global_step:08d>.pth` for step-based training. Save `supernet_best.pth` when the **max config's** validation metric improves (see Evaluation for rationale).

Note: When evaluation is also scheduled for the same epoch/step, save `supernet_latest.pth` **after** evaluation and `best_metric` update, so that `best_metric` in the checkpoint reflects the most recent result.

Use `save_checkpoint_ddp` from `nas_agent.train` for all checkpoint writes. It automatically unwraps `DistributedDataParallel` (clean state-dict keys), gates the write on rank 0, and barriers other ranks. `save_checkpoint_ddp` forwards its keyword arguments to `save_checkpoint`; `epoch`, `global_step`, `best_metric`, and `args` are optional but should be included whenever the training context tracks them.

Do **not** wrap `save_checkpoint_ddp()` inside `if is_main_process()`. The function contains an internal `barrier()` that all ranks must reach; wrapping it in a rank guard causes a multi-GPU deadlock.

```python
from nas_agent.train import save_checkpoint_ddp

# ── Anti-pattern (causes multi-GPU deadlock) ──
# if is_main_process():
#     save_checkpoint_ddp(...)

# ── Save (inside training loop / after evaluation) ──
save_checkpoint_ddp(
    os.path.join(args.output_dir, "supernet_latest.pth"),
    model,
    optimizer=optimizer,
    scheduler=scheduler,
    scaler=scaler,
    epoch=epoch,
    global_step=global_step,
    best_metric=best_metric,
    args=vars(args),
    # extra={"sampler_state": ...},  # optional project-specific state
)
```

### 8. Sandwich Training Loop

Sandwich training samples multiple subnets per batch — the largest (max), the smallest (min), and `--sandwich_n_random` random configurations — accumulates gradients from all of them, then performs a single optimizer step.

#### Distillation

Before implementing sandwich sampling, decide whether KD can be added between sampled subnets. All sampled subnets still use supervised loss; KD is only an additional loss when aligned teacher/student tensors are clear. If no suitable KD target and loss are clear, keep the Sandwich Training Loop but do not enable KD.

Do not enable KD when:

- The project code does not clearly identify comparable teacher and student tensors.
- Applying KD on the chosen tensors would require engineering beyond a standard model-agnostic final-output loss, such as new adapters, intermediate feature matching, layer-to-layer mapping, attention-map matching, task-specific decoding, assignment, sampling, beam search, NMS, or metric-only post-processing.
- The original supervised loss is a weighted combination of multiple components (e.g. classification + box regression + objectness in detection) and the generated script cannot determine a safe KD weight scale from project context.
- The sandwich KD loss would semantically conflict with the user's original training objective. Note: an existing external-teacher KD recipe in the project does not by itself conflict, because sandwich KD uses the max subnet as teacher, which is a different mechanism.

For multi-output or dict-output models, KD does not need to cover every output. Apply KD only to the subset of outputs that have clear teacher-student alignment and use a standard model-agnostic loss; leave the remaining outputs with supervised loss only.

Use `nas_agent.train.distillation` helpers for KD when they fit. Use only model-agnostic distillation on naturally aligned final outputs such as logits, probability distributions, final hidden states, regression outputs, or other task-level tensors. Do not generate architecture-specific adapters, intermediate feature matching, layer-to-layer alignment, attention-map matching, or other feature KD that depends on the internal teacher/student architecture.

Recommended choices — select based on the nature of the **final output tensor** being distilled:

- **Mutually exclusive discrete-distribution outputs** — single-label classification, token classification, per-position vocabulary prediction in language modeling or sequence-to-sequence tasks: use `logits_kd_loss()` on final logits with the same valid-label mask used by the supervised loss. The teacher and student must share the same label or vocabulary space so that logit dimensions are directly comparable. This loss uses softmax, which assumes labels are mutually exclusive.
  - Prefer enabling `logit_standardization` to compensate for logit scale mismatch between the max subnet (teacher) and smaller subnets (students), which is common in sandwich training due to capacity differences.
- **Independent multi-label outputs** — multi-label classification, multi-attribute prediction, or any task where the supervised loss is `binary_cross_entropy_with_logits`: use `soft_bce_kd_loss()` on raw logits. Each label is treated as an independent Bernoulli distribution via sigmoid, unlike `logits_kd_loss` which assumes mutually exclusive classes via softmax. Temperature softens the teacher's per-label probabilities, exposing inter-label confidence structure to the student.
- **Continuous outputs where absolute magnitude is meaningful** — pixel-level regression, bounding-box coordinates, scalar/vector prediction, dense depth/flow maps: use `mse_kd_loss()` when teacher and student outputs have the same shape and numeric scale. MSE penalizes element-wise magnitude differences, so each element must be directly comparable in both value and scale between teacher and student.
- **Continuous outputs where direction matters more than magnitude** — the model's final task-level output is an embedding or representation vector (e.g. sentence encoder output, retrieval embedding, contrastive feature): use `cosine_kd_loss()`. Cosine similarity is scale-invariant — it measures only directional agreement in the feature space, ignoring absolute magnitude. Prefer this over MSE when:
  - The final output is an embedding whose L2 norm may vary between teacher and student (common with elastic-width subnets producing representations of different magnitudes).
  - Orientation in the feature space encodes the task-relevant semantics (e.g. retrieval similarity, contrastive alignment).
  - A shared projection head maps backbones of different widths to the same embedding dimension, but the resulting norms diverge, making element-wise MSE an unreliable similarity signal.
- **None of the above**: add model-agnostic KD only when the aligned final output and a suitable differentiable comparison method are clear. Do not force KD when the output format does not fit any of the above categories.

The code template includes a runtime shape guard (`min_outputs.shape == teacher_outputs.shape`) as a safety net. The generation-time decision above determines whether KD code is emitted at all; the runtime guard handles residual edge cases such as dynamic shapes or optional outputs.

If the chosen KD loss (e.g. `logits_kd_loss` and `soft_bce_kd_loss`) accepts a temperature parameter and a more suitable value can be inferred from the task context (e.g. the class/vocabulary count is very large, or the teacher distribution is known to be very peaky or very flat), set that value as the default and expose `--kd_temperature` as a CLI argument for override.

#### KD Weight Warmup

Sandwich KD is inplace: the max-subnet teacher is trained alongside the student subnets with shared weights, so early in training the teacher's outputs are essentially random. Applying KD with an untrained teacher injects noisy gradients that can destabilize convergence. A delayed start and/or linear warmup lets the shared weights first converge under supervised loss before KD signals are introduced.

Use `KDWeightScheduler` from `nas_agent.train.distillation` to schedule the effective KD weight. Its `start` and `warmup_length` use the same training progress unit as the generated script (same unit as `--eval_interval`):

- `--kd_warmup_start`: when KD begins (e.g. epoch or step).
- `--kd_warmup_length`: duration to linearly ramp from `0` to `--kd_weight`, starting at `--kd_warmup_start`. The ramp spans `[start, start + warmup_length]`. `0` means jump to full weight immediately.

```python
from nas_agent.train.distillation import KDWeightScheduler

kd_scheduler = KDWeightScheduler(
    target_weight=args.kd_weight,
    start=args.kd_warmup_start,
    warmup_length=args.kd_warmup_length,
)

# Inside the training loop — pass native progress directly:
# Epoch-based:  kd_loss_weight = kd_scheduler.get_weight(epoch)
# Step-based:   kd_loss_weight = kd_scheduler.get_weight(global_step)
kd_loss_weight = kd_scheduler.get_weight(global_step)
```

#### Choice Sampling

Write small local sampling helpers in `train_supernet.py`; do not import `inspect_supernet.py` directly. Follow the inspector's max/min value-selection convention.

Block choices are sampled per layer along the max depth (since max activates all layers); min and random configs with smaller depths simply leave the trailing layers' choices unused. Different layers may choose different blocks independently. Within the shared block choice at each active layer:

- **max config**: every stage depth at its maximum, every elastic block parameter at its maximum.
- **min config**: every stage depth at its minimum, every elastic block parameter at its minimum.
- **random config**: each stage depth is randomly sampled from the valid candidates, each elastic block parameter is sampled uniformly from its valid range.

Implement `sample_sandwich_arch_configs(search_space, n_random, rng)` as a local helper that returns `(max_config, min_config, random_configs)`.

Simplified example (adapt to the generated `SearchSpace`):

```python
def sample_sandwich_arch_configs(search_space, n_random, rng):
    max_depths = tuple(max(d) for d in search_space.stage_depth_candidates)
    min_depths = tuple(min(d) for d in search_space.stage_depth_candidates)

    # Sample block choices per layer along max depth (all layers active)
    # Use stage-specific stage_layer_configs to get block choices per stage.
    stage_choices = {}
    for stage_name, depth, layer_configs in zip(
        search_space.stage_names, max_depths, search_space.stage_layer_configs
    ):
        choices = list(layer_configs.keys())
        stage_choices[stage_name] = [rng.choice(choices) for _ in range(depth)]

    # Build max config: max depth, all params at max
    max_config = ...  # ArchConfig(stage_depths=max_depths, ...), each param = max(v)

    # Build min config: min depth, all params at min (trailing layers unused)
    min_config = ...  # ArchConfig(stage_depths=min_depths, ...), each param = min(v)

    # Build N random configs: random depth, random params
    random_configs = []
    for _ in range(n_random):
        rand_depths = tuple(
            rng.choice(d) for d in search_space.stage_depth_candidates
        )
        rand_layer_configs = {}
        for stage_name, depth, layer_configs in zip(
            search_space.stage_names, rand_depths, search_space.stage_layer_configs
        ):
            rand_layer_configs[stage_name] = tuple(
                {
                    "choice": stage_choices[stage_name][i],
                    "config": {k: rng.choice(v) for k, v in layer_configs[stage_choices[stage_name][i]].items()},
                }
                for i in range(depth)
            )
        random_configs.append(ArchConfig(stage_depths=rand_depths, layer_configs=rand_layer_configs))

    return max_config, min_config, random_configs
```

#### Training Example

For each training batch, sample one set of shared block choices, build the max, min, and `--sandwich_n_random` random configs from those choices, accumulate gradients from all subnets, then call the optimizer once.

All random choices inside `sample_sandwich_arch_configs` must use the provided local `rng`, not global Python or PyTorch RNG state.

**DDP arch-sampling constraint:** In distributed training every rank must activate the exact same sub-network on every forward pass; otherwise DDP's gradient all-reduce will mix gradients from different architectures and corrupt training. Use `sync_random_seed` (broadcast a seed from rank 0) so that all ranks produce identical block choices and configs from the same `rng` state each iteration.

The example below is a distributed and AMP-aware Sandwich Training template for the inner training loop. Adapt task-specific batch unpacking, model call, losses, KD, and scheduler timing to the user's project.

```python
import random

import torch
import torch.distributed as dist

from nas_agent.train import (
    autocast,
    is_main_process,
    logits_kd_loss,
    set_sample_config_ddp,
    unwrap_model,
)
from nas_agent.train.distillation import KDWeightScheduler


def sync_random_seed(device):
    seed_source = random.SystemRandom()
    seed_tensor = torch.empty((), dtype=torch.long, device=device)
    if is_main_process():
        seed_tensor.fill_(seed_source.randrange(0, 2**31))
    dist.broadcast(seed_tensor, src=0)
    return int(seed_tensor.item())

# ── KD weight scheduler (construct once before training loop) ──
kd_scheduler = KDWeightScheduler(
    target_weight=args.kd_weight,
    start=args.kd_warmup_start,
    warmup_length=args.kd_warmup_length,
)

model.train()
for batch in train_loader:
    inputs, targets = batch
    inputs = inputs.to(device, non_blocking=True)
    targets = targets.to(device, non_blocking=True)
    optimizer.zero_grad(set_to_none=True)

    # Compute current KD weight using native progress unit.
    # Epoch-based:  kd_loss_weight = kd_scheduler.get_weight(epoch)
    # Step-based:   kd_loss_weight = kd_scheduler.get_weight(global_step)
    kd_loss_weight = kd_scheduler.get_weight(global_step)  # adapt to project progress unit

    arch_seed = sync_random_seed(device)
    arch_rng = random.Random(arch_seed)
    max_config, min_config, random_configs = sample_sandwich_arch_configs(
        search_space,
        n_random=args.sandwich_n_random,
        rng=arch_rng,
    )

    # ── max subnet (teacher) ──
    set_sample_config_ddp(model, max_config)
    with autocast(device, enabled=args.amp):
        max_outputs = model(inputs)
        max_loss = criterion(max_outputs, targets)
    scaler.scale(max_loss).backward()
    teacher_outputs = max_outputs.detach()

    # ── min subnet ──
    set_sample_config_ddp(model, min_config)
    with autocast(device, enabled=args.amp):
        min_outputs = model(inputs)
        min_loss = criterion(min_outputs, targets)
        if kd_loss_weight > 0 and min_outputs.shape == teacher_outputs.shape:
            min_loss = min_loss + kd_loss_weight * logits_kd_loss(
                min_outputs,
                teacher_outputs,
                temperature=args.kd_temperature,
            )
    scaler.scale(min_loss).backward()

    # ── random subnets ──
    for rand_config in random_configs:
        set_sample_config_ddp(model, rand_config)
        with autocast(device, enabled=args.amp):
            rand_outputs = model(inputs)
            rand_loss = criterion(rand_outputs, targets)
            if kd_loss_weight > 0 and rand_outputs.shape == teacher_outputs.shape:
                rand_loss = rand_loss + kd_loss_weight * logits_kd_loss(
                    rand_outputs,
                    teacher_outputs,
                    temperature=args.kd_temperature,
                )
        scaler.scale(rand_loss).backward()

    if args.max_grad_norm > 0:
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(
            model.parameters(),
            args.max_grad_norm,
            foreach=False if is_npu else None,
        )
    scaler.step(optimizer)
    scaler.update()
    if scheduler is not None:
        scheduler.step()
    global_step += 1
```

### 9. Evaluation

Run evaluation every `--eval_interval` in the generated script's training progress unit (`epoch` for epoch-based training, otherwise optimizer update count stored as `global_step`), and once at the final training boundary. Evaluate two fixed subnet configs with the user's validation metric.

For evaluation sampling, every layer selects the user-model-expanded elastic block (the first `choice` in generated candidate order). This is a fixed structural decision, not a random sample. The stage depth and elastic block parameter construction follows the same max/min convention as the sandwich training configs in Choice Sampling, except that the block choice is fixed rather than randomly sampled:

- **max config**: every stage depth is set to its maximum; all elastic block parameters (width, kernel size, etc.) are set to their maximum values.
- **min config**: every stage depth is set to its minimum; all elastic block parameters are set to their minimum values.

```python
max_config, min_config = sample_fixed_eval_arch_configs(search_space)
for subnet_name, subnet_config in (("max", max_config), ("min", min_config)):
    set_sample_config_ddp(model, subnet_config)
    metrics[subnet_name] = run_project_validation(model, val_loader, device)
```

#### DDP Metric Aggregation

When the validation set is sharded across ranks (e.g. via `DistributedSampler`), each rank only sees its own shard, so per-rank metrics are incomplete. Use `AverageMeter` from `nas_agent.train` to accumulate running totals per rank. Reading `.avg` or `.count` triggers `all_reduce` across ranks. `.avg` returns a Python `float` (not a tensor), so any post-processing must use `math` / plain Python operations (e.g. `math.log10`), not `torch.*` ops.

**CRITICAL: `.avg` and `.count` are collective operations** — `all_reduce` requires every rank to participate. Always compute `.avg` on **all ranks** first, then gate only the logging / printing / checkpoint decision on `is_main_process()`. Never place an `.avg` or `.count` call inside an `if is_main_process():` block; doing so causes a multi-GPU deadlock because the non-rank-0 processes never enter the `all_reduce` and hang waiting.

```python
from nas_agent.train import AverageMeter

loss_meter = AverageMeter(device)
acc_meter = AverageMeter(device)

for inputs, targets in val_loader:
    loss = criterion(model(inputs), targets)
    loss_meter.update(loss.item(), n=inputs.shape[0])
    acc_meter.update(acc.item(), n=inputs.shape[0])

# ── Correct: compute on ALL ranks, then gate only the print ──
avg_loss = loss_meter.avg   # all_reduce across ranks
avg_acc = acc_meter.avg     # all_reduce across ranks
if is_main_process():
    print(f"loss={avg_loss:.3e}  acc={avg_acc:.4f}")
```

The following pattern causes a multi-GPU deadlock and must be avoided:

```python
# ── ANTI-PATTERN (causes multi-GPU deadlock) ──
if is_main_process():
    avg_loss = loss_meter.avg   # ← only rank 0 calls all_reduce → HANG
    print(f"loss={avg_loss:.3e}")
```

Select `supernet_best.pth` based on the **max config's** validation metric. Max config activates all shared parameters, so its metric is the most direct and stable measure of shared weight quality; it also serves as the sandwich KD teacher, so checkpointing when the teacher is strongest maximizes KD effectiveness for smaller subnets. Use the user's own metric direction (e.g. maximize accuracy, F1, mAP; minimize loss, error, WER, perplexity).

The best-metric comparison must use the globally aggregated metric (from `AverageMeter.avg`) so that all ranks reach the same save-or-skip decision. `save_checkpoint_ddp` contains an internal barrier; if some ranks enter it while others skip, the training deadlocks.

## Run Launcher

Generate `run_train_supernet.sh` as the remote launcher for distributed supernet training. The launcher defaults to a single-node 8-device `torchrun` configuration and exposes key training parameters as editable shell variables.

Launcher skeleton:

```bash
#!/usr/bin/env bash
set -euo pipefail

# ── Editable variables ──────────────────────────────────────────────
DATA_DIR="/path/to/dataset"
OUTPUT_DIR="runs/train"
EPOCHS=100
BATCH_SIZE=64
LR=1e-3
NUM_WORKERS=4
EVAL_INTERVAL=1
SEED=42
MAX_GRAD_NORM=1.0
SANDWICH_N_RANDOM=2
AMP=true
# distributed launch options
NNODES=1
NPROC_PER_NODE=8

# ── Launch training ─────────────────────────────────────────────────
AMP_FLAG=""
[ "$AMP" = true ] && AMP_FLAG="--amp"

torchrun \
    --nnodes="$NNODES" \
    --nproc_per_node="$NPROC_PER_NODE" \
    train_supernet.py \
    --data_dir "$DATA_DIR" \
    --output_dir "$OUTPUT_DIR" \
    --epochs "$EPOCHS" \
    --batch_size "$BATCH_SIZE" \
    --lr "$LR" \
    --num_workers "$NUM_WORKERS" \
    --eval_interval "$EVAL_INTERVAL" \
    --seed "$SEED" \
    --max_grad_norm "$MAX_GRAD_NORM" \
    --sandwich_n_random "$SANDWICH_N_RANDOM" \
    $AMP_FLAG
```

After writing, mark executable: `chmod +x run_train_supernet.sh`.

Before finalizing the launcher, cross-check every `--arg_name` in the `torchrun` invocation against the generated `train_supernet.py` argparse definitions. Every shell variable passed as a CLI flag must correspond to an argument the script actually accepts. Run `python train_supernet.py --help` (or inspect the argparse block) to confirm.

## Validation

The generated training artifacts are for remote-server execution. Local validation is layered: static checks and functional smoke tests always run first, followed by an optional end-to-end single-GPU smoke test when the user's dataset is locally available.

Allowed:

- `bash -n run_train_supernet.sh`
- `python -m py_compile train_supernet.py`
- **Diagnostic check** (does not modify files): `ruff check --no-fix --config <nas_agent_root>/nas_agent/internal_ruff_check.toml train_supernet.py`. If diagnostic errors are reported (e.g. undefined names, missing imports), fix the code and re-run.
- a lightweight import-path sanity check that does not execute `train_supernet.py`, build datasets, download data, start `torchrun`, or run training
- **Launcher-script CLI consistency:** run `python train_supernet.py --help` and verify every `--flag` in `run_train_supernet.sh` is accepted. Fix any mismatched argument names.
- **Budget-hyperparameter coherence:** verify that budget-dependent hyperparameters (lr scheduler total steps/epochs, warmup steps, decay milestones, etc.) are coherent with the scaled training budget. If the training budget was adjusted (e.g. 3× the original), confirm the scheduler and related settings were adjusted accordingly.
- **Scheduler step granularity:** verify that `scheduler.step()` is called at the same granularity as the original project (e.g. per-epoch vs per-batch). If the original scheduler steps once per epoch, the generated script must not move it into the batch loop.
- **Device placement consistency:** after writing each PyTorch `.py` file, review it for device placement consistency before proceeding. Verify that all tensors participating in the same operation reside on the same device, and that GPU/NPU tensors are explicitly moved to CPU before conversion to NumPy or Python scalars. Common violations include: cross-tensor computation before `.to(device)`, auxiliary tensors not matching the model's device, and on-device tensors passed directly to NumPy functions or Python builtins.
- **Functional smoke tests (always):** import and exercise the generated helpers from `train_supernet.py` and its companion files against the `supernet.py` `SearchSpace` / `SuperNet` on a single device, without `torchrun`. These catch component-level bugs before attempting end-to-end execution. If any test fails, fix the corresponding code and re-run.
  - Model construction: instantiate the `SuperNet` and run a forward pass with dummy inputs.
  - Sampling helpers: verify that sampled configs are accepted by `set_sample_config` and produce valid forward outputs.
  - KD integration (if enabled): verify the KD loss call does not error on model-shaped dummy tensors.
  - Evaluation function: call with a small dummy dataloader of synthetic batches.
  - Standalone model correctness: set a config via `set_sample_config()` and extract the standalone model via `get_active_subnet()`. Assert `torch.allclose(supernet_out, subnet_out)` in both `.eval()` and `.train()` modes, and verify `output.sum().backward()` succeeds on the standalone model.
  - Data pipeline (if the user's dataset is locally available): exercise dataset construction, transforms, and collate function with real data.
- **End-to-end single-GPU smoke test (if the user's dataset is locally available and functional tests pass):** launch the training script on a single device with a minimal budget to verify the full pipeline runs without errors. This catches integration issues (DDP + data + training loop + eval + checkpoint save) that component tests miss.
  - **Launch:** Use `torchrun --nproc_per_node=1` and override the training budget to a small value (e.g. 2 epochs or ~20 steps). Pass the data path from `<user_project_root>` via CLI arguments. Run the command in the foreground.
    - **Anti-pattern:** Do not reduce batch size to shorten an epoch-based smoke test — fewer samples per batch means more iterations per epoch, making it *slower*.
  - **Observe:** Watch the periodic progress output (e.g., `tqdm` bar or batch logs) to confirm the training loop iterates successfully and gradients are being computed without error.
  - **Kill (CRITICAL):** Do NOT wait for the training to finish naturally. Waiting for even a single epoch can take too long. Once you have verified that the training outputs normally and progresses a few steps without crashing, actively interrupt and kill the process.
  - **Fix:** If any errors occur, fix the code and re-run the smoke test until it passes.
- **Format cleanup** (run once after all other checks and smoke tests pass): `ruff check --fix --quiet --config <nas_agent_root>/nas_agent/internal_ruff.toml train_supernet.py` followed by `ruff format --quiet --config <nas_agent_root>/nas_agent/internal_ruff.toml train_supernet.py`. Treat as silent final formatting only.

Forbidden:

- Do not run `run_train_supernet.sh` at full scale or with the production budget.
- Do not download datasets.

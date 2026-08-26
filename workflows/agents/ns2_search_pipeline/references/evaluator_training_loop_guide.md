# Evaluator Single-Subnet Training Loop

This document supplements `workflows/search_supernet_script_generation.md` §2 Evaluator, providing the detailed evaluation flow and training loop implementation for the `finetune` and `train_from_scratch` paradigms. For the `validate` paradigm, see `supernet_workflow_examples/evaluator.py`.

Key constraints:

- **Single device, no DDP**: do not use distributed utilities (`DistributedDataParallel`, `DistributedSampler`, `torchrun`, rank guards, etc.)
- **Per-candidate lifecycle**: subnet, optimizer, scheduler, and scaler are created inside `evaluate()` and destroyed before returning (`del subnet, optimizer, scheduler, scaler; empty_cache(self.device)`)
- **Shared resources**: supernet, data loaders, and criteria are initialized in `__init__` and reused across `evaluate()` calls

Paradigm differences:

- **finetune**: supernet checkpoint loaded in `__init__` via `self.cfg.supernet_ckpt_path`; subnet inherits pretrained weights; short training budget (5–20% of original)
- **train_from_scratch**: no supernet checkpoint; subnet weights re-initialized after extraction; training budget is reduced but larger than finetune

## Evaluation Flow

### finetune

Configure the supernet, extract the active subnet, short-train it, then validate. When the search targets a different dataset than the one used for supernet pretraining, both `train_loader` and `val_loader` must point to the target dataset so finetuning adapts the inherited weights to the target domain. The flow is:

1. Compute `arch_id = hash_arch(arch_config)` (`from nas_agent.search.arch_utils import hash_arch`), a 16-character hex hash that uniquely identifies the candidate in logs and saved files
2. Print start banner: `print(f"[Eval Start] arch={arch_id} | paradigm=finetune | epochs={self.cfg.epochs}", flush=True)`
3. `self.supernet.set_sample_config(arch_config)`
4. `subnet = self.supernet.get_active_subnet().to(self.device)`
5. Build optimizer, scheduler, AMP scaler for `subnet`
6. Train `subnet` for `self.cfg.epochs` epochs on `self.train_loader`; track best validation metric. After each epoch (or at a regular step interval for step-based training), print a one-line progress summary to track training progress:
   - Epoch-based: `print(f"[{arch_id}] epoch {epoch+1}/{self.cfg.epochs} | loss={avg_loss:.4f} | metric={val_metric:.4f}", flush=True)`
   - Step-based: `print(f"[{arch_id}] step {step}/{total_steps} | loss={avg_loss:.4f} | metric={val_metric:.4f}", flush=True)`
7. Save checkpoints under `{save_dir}/{arch_id}/`:
   - `last.pth`: subnet state dict after the final epoch
   - `best.pth`: subnet state dict at the best validation metric
   - `arch_info.json`: metadata with fields `arch_id`, `gene` (integer tuple), `arch_config` (serialized ArchConfig), `paradigm`, `epochs`, `best_metric`, and `metrics` (final evaluation results)
8. Validate `subnet` on `self.val_loader`
9. Print finish banner: `print(f"[Eval Done ] arch={arch_id} | metrics={metrics}", flush=True)`
10. `del subnet, optimizer, scheduler, scaler; empty_cache(self.device)`
11. Return metrics. Compute and track metrics in their natural direction throughout the training loop and checkpoints. Negate larger-is-better metrics only at the final return to satisfy the search framework's smaller-is-better convention.

### train_from_scratch

Follow the complete **finetune** flow above (steps 1-11), with two differences:

- After the above step 4, re-initialize subnet weights:

  ```python
  @torch.no_grad()
  def reset_module(m: nn.Module):
      reset_parameters = getattr(m, "reset_parameters", None)
      if callable(reset_parameters):
          reset_parameters()

  subnet.apply(reset_module)
  ```

  Apply project-specific initialization from `<user_project_root>` if present.

- Use `paradigm=train_from_scratch` in the start banner and `arch_info.json`.

### Example

The following shows a standard supervised `evaluate()` for the `finetune` or `train_from_scratch` paradigm.

```python
import json
from pathlib import Path

from nas_agent.search.arch_utils import hash_arch
from nas_agent.train import autocast, empty_cache, grad_scaler
from nas_agent.train.metrics import AverageMeter

def evaluate(self, arch_config: ArchConfig) -> dict[str, float]:
    arch_id = hash_arch(arch_config)
    print(f"[Eval Start] arch={arch_id} | paradigm=finetune | epochs={self.cfg.epochs}", flush=True)

    # 1. Extract subnet
    self.supernet.set_sample_config(arch_config)
    subnet = self.supernet.get_active_subnet().to(self.device)

    # 2. [train_from_scratch only] Re-initialize weights
    # subnet.apply(reset_module)

    # 3. Per-candidate resources
    is_npu = self.device.type == "npu"
    optimizer = optim.AdamW(
        subnet.parameters(),
        lr=self.cfg.lr,
        weight_decay=self.cfg.weight_decay,
        foreach=False if is_npu else None,
    )
    scheduler = ...  # project-specific, budget adjusted
    use_amp = self.cfg.get("amp", False)
    scaler = grad_scaler(self.device, enabled=use_amp)

    # 4. Training loop (standard supervised; for RL/GAN/self-supervised, replace with the project's actual training logic, see §Non-Standard Training Paradigms)
    best_metric = None
    save_dir = Path(self.cfg.save_dir) / arch_id
    save_dir.mkdir(parents=True, exist_ok=True)

    for epoch in range(self.cfg.epochs):
        subnet.train()
        loss_meter = AverageMeter(self.device)
        for batch in self.train_loader:
            inputs, targets = batch  # adapt to project's batch structure
            inputs = inputs.to(self.device, non_blocking=True)
            targets = targets.to(self.device, non_blocking=True)

            optimizer.zero_grad(set_to_none=True)
            with autocast(self.device, enabled=use_amp):
                outputs = subnet(inputs)
                loss = self.criterion(outputs, targets)

            scaler.scale(loss).backward()
            if self.cfg.get("max_grad_norm", 0) > 0:
                if scaler.is_enabled():
                    scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(
                    subnet.parameters(),
                    self.cfg.max_grad_norm,
                    foreach=False if is_npu else None,
                )
            scaler.step(optimizer)
            scaler.update()
            loss_meter.update(loss.item(), n=inputs.size(0))

        if scheduler is not None:
            scheduler.step()

        # Per-epoch validation and best-metric tracking.
        # Compute metrics in their natural direction (e.g. accuracy 0–1, loss as-is).
        # When train_supernet.py exists, align metric key and comparison direction with it.
        # Smaller-is-better conversion happens only at the final return of evaluate().
        avg_loss = loss_meter.avg
        metrics = eval_epoch(subnet, ...)  # project-specific validation; returns e.g. {"acc": 0.85}
        val_metric = metrics["acc"]

        if best_metric is None or val_metric > best_metric:  # natural direction
            best_metric = val_metric
            torch.save(subnet.state_dict(), save_dir / "best.pth")

        print(f"[{arch_id}] epoch {epoch + 1}/{self.cfg.epochs} | loss={avg_loss:.4f} | acc={val_metric:.4f}", flush=True)

    # 5. Save last checkpoint and arch metadata
    torch.save(subnet.state_dict(), save_dir / "last.pth")
    (save_dir / "arch_info.json").write_text(json.dumps({
        "arch_id": arch_id,
        "gene": list(self.codec.arch_to_gene(arch_config)),  # integer tuple for re-decoding
        "arch_config": arch_config.to_dict(),
        "paradigm": "finetune",  # or "train_from_scratch"
        "epochs": self.cfg.epochs,
        "best_metric": best_metric,
        "metrics": metrics,
    }, indent=2))

    print(f"[Eval Done ] arch={arch_id} | metrics={metrics}", flush=True)

    # 7. Cleanup
    del subnet, optimizer, scheduler, scaler
    empty_cache(self.device)

    # Negate larger-is-better metrics for the search framework (smaller-is-better convention).
    # Leave naturally smaller-is-better metrics (e.g. loss) as-is.
    return {"acc": -metrics["acc"]}
```

## Training Loop Implementation

The authoritative source for training semantics is `<user_project_root>`. When `train_supernet.py` exists, it serves as a pre-adapted reference that has already ported the data pipeline, loss, metrics, optimizer, and scheduler from the original project; prefer reusing its code and helper files. When `train_supernet.py` does not exist or does not cover the needed training semantics, explore `<user_project_root>` directly and extract the training logic. Port these into one or more self-contained helper files under `<output_dir>` (e.g. `data_utils.py`, `env_wrapper.py`, `losses.py`).

### Metric Fidelity

The metric or reward is the architecture-ranking signal. Trace the call chain from the training entry point to find the function that computes it, and port that function's logic faithfully.

To reduce evaluation cost, cut iteration counts (fewer episodes, epochs, or steps) via `evaluator_cfg`. Do not substitute the per-step computation itself with a cheaper function.

### Optimizer, Scheduler, And AMP

All three are created per-candidate inside `evaluate()` and destroyed before returning (see the Example above for the complete pattern).

**Optimizer**: port the configuration from `<user_project_root>` (or `train_supernet.py` when available). Apply NPU `foreach` compatibility (`foreach=False if is_npu else None`).

**Scheduler**: port from `<user_project_root>`. Adjust budget-dependent parameters (warmup, milestones) proportionally to the evaluator's reduced epoch count. Preserve original step granularity (per-epoch vs per-batch).

**AMP**: use `autocast` and `grad_scaler` from `nas_agent.train` (device-compatibility wrappers). `grad_scaler(self.device, enabled=use_amp)` handles NPU incompatibility internally (disables the scaler on NPU while keeping autocast enabled). The autocast enable flag is independent from `scaler.is_enabled()`.

### Data Pipeline

Data pipeline code should be decoupled into separate helper files under `<output_dir>` (e.g. `data_utils.py`), keeping `evaluator.py` focused on the evaluation logic.

- **When `train_supernet.py` exists**: its helper files may already contain the adapted dataset classes, transforms, and collate functions. Import and reuse them as sibling modules.
- **When `train_supernet.py` does not exist**: port the data pipeline from `<user_project_root>` into new helper files under `<output_dir>`. Preserve batch structure, input format, label format, preprocessing, tokenizers, and augmentation.

Data loaders are built in `__init__` and shared across `evaluate()` calls. Use standard single-device data loading (`shuffle=True`, no `DistributedSampler`). Expose data paths via `evaluator_cfg` fields, not hardcoded literals.

### Non-Standard Training Paradigms

When the original project uses a non-standard training paradigm, the evaluator must reproduce it faithfully. Rules:

1. **Port faithfully.** Mirror the original control flow, loss computation, reward/metric computation, and gradient flow. Do not collapse into a generic supervised loop when the original is structurally different. This includes the **objective function**; see §Metric Fidelity above.
2. **Self-contained.** Port all auxiliary components into helper files under `<output_dir>`. The evaluator must not import from `<user_project_root>` at runtime.
3. **Search scope.** The supernet and subnet replace only the network(s) that will be deployed at inference time. Auxiliary networks that are separate `nn.Module` instances used only during training (e.g., GAN discriminator, separate critic network in RL) are not part of the search and should retain their original architecture from `<user_project_root>`.

Paradigm notes:

- **RL**: port environment interaction and rollout collection; budget is episodes or env-steps, not epochs.
  - **Environment fidelity.** The ported environment step must reproduce the original's full data flow: state/observation construction (features, dimensions, normalization, history), the per-step environment function (simulation, physics processing, game step), the reward formula (same terms, constants, signs), and action space handling (discrete/continuous, masking, clipping).
  - **Actor/critic structure.** Inspect `<user_project_root>` to determine how the project structures its actor (policy) and critic (value) networks, then apply the matching pattern:
    - **Separate actor and critic**: the supernet covers the complete policy network (backbone + policy head). The critic is ported from `<user_project_root>` as a **fixed original architecture**, independent of the supernet and the candidate subnet's shape. This holds even if the original project uses the same architecture design for both actor and critic but instantiates them independently; a full-capacity critic provides stable training signal for ranking candidate actors and is not deployed at inference.
    - **Shared backbone with different heads** (common in PPO/A2C): the project uses a single backbone feeding both a policy head and a value head. The supernet already includes the shared backbone plus both heads as fixed non-searchable modules. `get_active_subnet()` returns a complete model with both heads so training can compute policy and value losses jointly. The value head is training-only and is discarded at deployment.
- **GANs**: the supernet covers the complete generator. Instantiate the discriminator from the original fixed architecture in `<user_project_root>` and train it alongside each candidate subnet. Preserve the original alternating update schedule (G/D step ratio) and loss formulation.
- **Self-supervised learning (architecturally coupled)**: methods like MoCo, BYOL, DINO, and SimSiam rely on a momentum encoder or stop-gradient twin that must match the online encoder's architecture. The supernet covers the complete online encoder (backbone + projection head). Reconstruct the momentum/target branch from the original architecture during each candidate's from-scratch training and update it per the original EMA or copy schedule.
- **Iterative / fixed-point solvers**: the supernet already includes the full iterative loop, convergence checks, domain-specific linear operators, and buffers around the searchable neural layers. The evaluator training loop uses the extracted subnet directly without needing to reconstruct any outer computation.
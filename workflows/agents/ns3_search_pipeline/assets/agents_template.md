# AGENTS.md — NAS Pipeline Workflow Guide

> This document guides AI coding assistants (Claude Code, Cursor, etc.) through executing the NAS pipeline and generating post-search retrain/finetune scripts.

## Project Context

### Generated Artifacts

All generated source files and launchers are flat files in this directory. Runtime outputs (training checkpoints, search logs) are written under the paths configured by the launchers and `search_config.yaml`.

```text
.
├── supernet.py                         # Elastic supernet with SearchSpace, ArchConfig, SuperNet
├── inspect_supernet.py                 # Supernet architecture inspector
├── train_supernet.py                   # Supernet training script
├── latency_estimator.py                # Online latency estimator (PyTorch-based, CPU/CUDA/NPU)
├── arch_codec.py                       # Gene to ArchConfig codec for NAS search
├── evaluator.py                        # Candidate subnet evaluator for search workers
├── search_config.yaml                  # NAS search runtime configuration
├── run_train_supernet.sh               # Launcher: supernet training
├── run_search_supernet.sh              # Launcher: NAS search
├── tests/                              # Persistent smoke tests (plain scripts, run as python tests/<file>.py)
├── project_manifest.md                 # Persistent memory of the original project (structure, training, data)
└── supernet_summary.md                 # NAS decisions: training viability, evaluation paradigm, generated artifacts
```

List any ported project-logic helper files (e.g. environment, data, loss helpers) in the tree above as well.

### Original Project Root

`/absolute/path/to/user_project`

**Whenever any section in this document says "reference the original project" (training budget, data pipeline, optimizer, scheduler, initialization, etc.), you MUST read the relevant source files under this path.** Do not guess or infer project conventions from the generated scripts alone; open and inspect the actual project code.

If this path does not exist or is inaccessible, **stop and ask the user** for the correct or updated path before proceeding. Verify the new path exists, then update this section in `AGENTS.md`.

### `nas_agent` Library

The generated scripts depend on `nas_agent` (installed in editable mode via `pip install -e .`). To locate its source:

```bash
python -c "import nas_agent; print(nas_agent.__path__[0])"
```

Browse the source when you need to understand the API of helpers like `nas_agent.train.resolve_device`, `load_checkpoint`, `save_checkpoint`, `autocast`, `grad_scaler`, etc.

### Evaluation Paradigm

The search evaluation paradigm used by this project is: **{{EVALUATION_PARADIGM}}**

### Search Objectives

The search objectives (all smaller-is-better) are: `acc`, `latency`

Objective Semantics:

| Objective | Original Metric | Smaller-Is-Better Reason |
|-----------|-----------------|--------------------------|
| `acc` | validation accuracy | sign-flipped by evaluator |
| `latency` | inference latency (ms) | naturally smaller-is-better |

### Key API Surface

```python
from supernet import ArchConfig, SearchSpace, SuperNet

search_space = SearchSpace()
supernet = SuperNet(search_space, ...)

# Configure for a selected architecture.
arch_config = ArchConfig(...)
supernet.set_sample_config(arch_config)

# Extract a standalone fixed subnet.
subnet = supernet.get_active_subnet()
```

### Notes

- **Refer to `supernet_summary.md`** for NAS decisions: supernet training viability, evaluation paradigm rationale, and the list of generated artifacts.
- **Refer to `project_manifest.md`** for the original project: its structure, training/evaluation semantics, data/environment details, and a navigation list of key source files. Use it as the map before opening files under the Original Project Root.

---

## Running the NAS Pipeline

The generated scripts are ready to run but have not been executed yet. Run them in the following order on the target hardware.

### 1. Supernet Training

Run this step only when the active evaluator route uses a trained supernet checkpoint (`validate` or `finetune`). The generated `train_supernet.py` and `supernet_summary.md` record whether supernet training is viable for this project. Edit variables in `run_train_supernet.sh` (data path, epochs, batch size, device count, etc.) before running.

```bash
bash run_train_supernet.sh
```

This produces the trained supernet checkpoint (see Runtime Paths). Check `train_supernet.py` and `supernet_summary.md` for training conventions and expected outputs.

If the active evaluator route is `train_from_scratch`, skip this step.

### 2. Architecture Search

Run multi-objective evolutionary search over the generated search space. Latency is measured on-the-fly using PyTorch during search (no separate profiling step required). Edit `search_config.yaml` (`latency_cfg.batch_size`) to match the target hardware before running. For `validate` and `finetune`, a trained supernet checkpoint is also required; for `train_from_scratch`, no supernet checkpoint is loaded by the evaluator.

```bash
bash run_search_supernet.sh
```

Search runtime settings (population size, generations, concurrency, evaluator config) are in `search_config.yaml`. The search writes:
- `search.log`: human-readable per-generation log
- `search.jsonl`: machine-readable per-individual log (used for architecture selection)

The `.jsonl` path is derived from `search_log_path` in `search_config.yaml`

---

## Architecture Selection

After search completes, select architectures from the Pareto front. Two approaches:

1. **Automated**: run `nas-select-architecture` to automatically pick high-tradeoff candidates (see [CLI Usage](#cli-usage) below).
2. **Interactive**: write scripts to parse `search.jsonl`, analyze and visualize the Pareto front, present the results to the user, and select architectures based on their feedback. Useful tips:
   - Filter records with the maximum `generation` value and `pareto` set to true to extract the final-generation Pareto front.
   - Plot objective scatter plots (e.g. accuracy vs latency) from the `objs` dict to help the user visualize tradeoffs.
   - All objectives are stored as smaller-is-better; metrics like accuracy are negated (e.g. `acc = -0.95` means 95% accuracy).

### JSONL Record Format

Each line in the search JSONL is a JSON object with exactly these keys:

| Key | Type | Description |
|-----|------|-------------|
| `generation` | int | Search generation index |
| `gene` | list[int] | Fixed-length integer gene vector |
| `objs` | dict[str, float] | Objective values (smaller-is-better), key order matches `search_config.yaml` `objs` |
| `cached` | bool | Whether the individual was served from the evaluation cache |
| `pareto` | bool | Whether the individual is on the Pareto front for its generation |
| `arch` | dict | Architecture config dict produced by `serialize_arch(arch_config)` from `nas_agent.search.arch_utils` |

### CLI Usage

```bash
nas-select-architecture \
    --config "search_config.yaml" \
    --input "runs/search/search.jsonl" \
    --arch_output_dir "runs/retrain/selected" \
    -n 1
```

Run `nas-select-architecture --help` for full argument details. Key points:

- `--constraints` filters candidates using a Python expression over JSONL `objs` names. Because the search minimizes all objectives, metrics like accuracy are stored negated; write `acc < -0.9` to require accuracy above 90%.

The CLI writes all outputs to `--arch_output_dir`:

1. **`selection_summary.json`**: Summary of the selection run: input/config paths, record counts, constraint expression, objective names, and the ranked list of selected candidates with objectives, selection reasons, and full `arch` payloads.
2. **`arch_{arch_id}.json`**: One per selected candidate, where `arch_id` is the 16-character hex hash from `hash_arch(arch_config)`. See [Subnet Extraction](#subnet-extraction) for how to load and use these files.

You can select multiple candidates (increase `-n`) and retrain each one independently. Each selected candidate gets its own `arch_*.json` file in the output directory.

---

## Final Weight Acquisition

After selecting one or more subnet architectures, generate a retrain script and launcher to obtain the final model weights. The approach is a deterministic binary decision driven by **supernet availability** — not by the search-time evaluation paradigm.

Search-time evaluation paradigm for this project (informational only, does NOT drive the retrain strategy): **{{EVALUATION_PARADIGM}}**

**Strategy decision:** read the **Supernet Training Viability** section of `supernet_summary.md` (`viable: Yes` / `viable: No`) and check whether the supernet checkpoint file exists (path = `supernet_ckpt_path` in `search_config.yaml`, default `runs/train/supernet_best.pth`).

- **Branch (a) `finetune-from-supernet`** — when viability is `Yes` **AND** the supernet ckpt exists. Generate a finetune script that inherits supernet weights and incrementally fine-tunes the extracted subnet.
- **Branch (b) `train-from-scratch`** — the fallback, when viability is `No` **OR** the supernet ckpt is absent. Generate a train-from-scratch script that re-initializes the subnet and trains with a full budget.

**Reference code:**

- `evaluator.py`: subnet extraction, weight initialization, evaluation logic used during search.
- `train_supernet.py` and its helper files: data pipeline, AMP, checkpoint policy. Only available when supernet training is viable.
- Original project under **Original Project Root**: training budget, optimizer, scheduler, initialization.

> **Budget rule:** You MUST read the original project's training code to determine the training budget, optimizer, and scheduler configuration. Do not guess these values. Search-time budgets in `evaluator.py` may be reduced for throughput and should not be used as a baseline for retrain. Retrain should use **full evaluation** (entire validation set, no subsampling).

### Subnet Extraction

All paradigms require extracting a fixed subnet from the supernet. The key steps are:

1. Read the selected architecture JSON (`arch_{arch_id}.json` from `--arch_output_dir`) and construct the generated `ArchConfig`
2. Instantiate the supernet; optionally load a trained checkpoint for finetune paradigms
3. Call `set_sample_config(arch_config)` then `get_active_subnet()` to get the standalone subnet
4. Delete the supernet and call `empty_cache` to release memory before training

```python
import json
from pathlib import Path
import torch
from supernet import SearchSpace, ArchConfig, SuperNet
from nas_agent.train import empty_cache, load_checkpoint, resolve_device


def load_arch_config(arch_file: str) -> ArchConfig:
    """Load a selected architecture from the JSON exported by nas-select-architecture."""
    return ArchConfig(**json.loads(Path(arch_file).read_text(encoding="utf-8")))


def extract_subnet(
    arch_config: ArchConfig,
    device: torch.device,
    supernet_ckpt: str | None = None,
) -> torch.nn.Module:
    """Build supernet, optionally load checkpoint, extract standalone subnet, and cleanup."""
    search_space = SearchSpace()
    supernet = SuperNet(search_space).to(device)
    if supernet_ckpt is not None:
        load_checkpoint(supernet_ckpt, supernet, device, strict=False)
    supernet.set_sample_config(arch_config)
    subnet = supernet.get_active_subnet()
    del supernet
    empty_cache(device)
    # For train_from_scratch: re-initialize subnet weights here using
    # the same init logic as evaluator.py and the original project.
    return subnet
```

For branch (b) `train-from-scratch`, skip the checkpoint loading and **re-initialize weights** after extraction, using the same initialization as the original project and the search evaluator's `train_from_scratch` path (see `evaluator.py` for the reset logic used during search).

Refer to `evaluator.py` for the concrete subnet extraction and initialization logic used during search, and adapt it for the retrain script.

### Branch (a): `finetune-from-supernet` (supernet available)

**Context:** Supernet training was viable and a trained supernet checkpoint exists. During search, subnets were evaluated by inheriting supernet weights (either validating directly or short-finetuning) to rank architectures.

**Goal:** Generate a **finetune script** that inherits supernet weights and incrementally fine-tunes the extracted subnet to obtain the final model.

**Key points:**
- Load the supernet checkpoint and extract the subnet with inherited weights
- Finetuning adapts the inherited weights to the specific subnet topology
- Use the data pipeline, loss, optimizer, scheduler, and validation metric from `train_supernet.py` and the original project
- Training budget: reference the original project's training configuration; use a moderate number of epochs (enough to adapt the shared weights, not a full from-scratch budget)
- Use full evaluation (entire validation set, no subsampling)
- **Alternative starting point**: besides inheriting weights from the supernet, the finetune script can also load a per-candidate checkpoint saved during search (configured via `evaluator_cfg.save_dir`). Checkpoint directory layout: `{save_dir}/{arch_id}/best.pth`, where `arch_id` is the first 8 hex digits of the MD5 hash of the serialized architecture
- Adjust the LR scheduler configuration to match the training horizon

### Branch (b): `train-from-scratch` (supernet not available — fallback)

**Context:** Supernet training was not viable, or no trained supernet checkpoint is available. During search, each candidate was trained from scratch with a small budget to rank architectures.

**Goal:** Generate a **train-from-scratch script** that re-initializes the subnet weights and trains with a **full training budget**.

**Key points:**
- Extract the subnet **without** loading a supernet checkpoint
- Re-initialize weights after extraction, using the same initialization logic as the search evaluator's `train_from_scratch` path (see `evaluator.py`) and any project-specific initialization from the original project
- Training budget: reference the original project's training configuration; use a full training budget (significantly larger than `evaluator_cfg.epochs` from search)
- If `supernet_summary.md` indicated supernet training was not viable, `train_supernet.py` was not generated; adapt the training loop primarily from the **original project**
- Otherwise, use `train_supernet.py` as the primary training reference alongside the original project

### Script Requirements

The generated retrain/finetune script should:

1. **Be self-contained**: importable sibling modules only (from this directory), no dependency on the original project being importable at runtime
2. **Default to single-device execution**: no DDP/torchrun unless explicitly requested
3. **Use `nas_agent.train` helpers**: `resolve_device`, `autocast`, `grad_scaler`, `empty_cache` for device-agnostic execution
4. **NPU compatibility**: see the NPU Compatibility section at the end of this document
5. **Expose CLI arguments**: `--supernet_ckpt` (branch (a) only), `--data_dir`, `--output_dir`, `--device`, `--epochs`, `--batch_size`, `--num_workers`, `--amp`, `--resume`, `--max_grad_norm`, `--seed`. The selected architecture is injected as a Jinja-rendered literal (`SELECTED_ARCH = {{ ns3_run_search.output.selected_arch }}`), not a CLI argument.
6. **Checkpoint saving**: save `retrain_best.pth` on validation improvement and `retrain_latest.pth` regularly; the final best ckpt is always at `<output_dir>/retrain_best.pth` (default `runs/retrain/retrain_best.pth`, the cross-node contract path consumed by the downstream executor)
7. **Include a shell launcher** (`run_retrain.sh`)

### Launcher Skeleton

```bash
#!/usr/bin/env bash
set -euo pipefail

# ── Editable variables ──────────────────────────────────────────────
SUPERNET_CKPT="runs/train/supernet_best.pth"       # branch (a) finetune-from-supernet only; remove for branch (b) train-from-scratch
DATA_DIR="/path/to/dataset"
OUTPUT_DIR="runs/retrain"
DEVICE="auto"
EPOCHS=100                  # read from the original project training budget (do not guess)
BATCH_SIZE=64
NUM_WORKERS=0               # DataLoader Launch Hygiene; do not change default
AMP=false                   # single-device default

# ── Retrain the selected subnet ─────────────────────────────────────
AMP_FLAG=""
[ "$AMP" = true ] && AMP_FLAG="--amp"

python3 retrain.py \
    --supernet_ckpt "$SUPERNET_CKPT" \
    --data_dir "$DATA_DIR" \
    --output_dir "$OUTPUT_DIR" \
    --device "$DEVICE" \
    --epochs "$EPOCHS" \
    --batch_size "$BATCH_SIZE" \
    --num_workers "$NUM_WORKERS" \
    $AMP_FLAG
```

### Retrain Script Validation

This stage happens after search has completed and the user has selected one or more architectures, so validation should exercise the generated retrain/finetune route against the selected architecture instead of stopping at static checks.

- `python -m py_compile retrain.py` (+ `finetune.py` if generated)
- `bash -n run_retrain.sh`
- Construct the generated `ArchConfig` from the rendered selected architecture, instantiate `SearchSpace` and `SuperNet`, and verify subnet extraction follows the same route as `evaluator.py`
- Verify the script imports only sibling generated modules, standard library modules, installed third-party packages, and `nas_agent`; it must not require the original project to be importable at runtime
- **Device placement consistency:** review the generated `.py` file for device placement consistency before running any smoke test. Verify that all tensors participating in the same operation reside on the same device. Common violations include: constructor `__init__` performing cross-tensor computation before `.to(device)` is called, auxiliary tensors created without matching the model's device, and input/target tensors not moved to the model's device before use.
- Run a single-device smoke test using the selected architecture and the real retrain/finetune code path. Prefer a tiny real-data run when the dataset is available, such as one epoch with one to two batches or an equivalent `--max_*_batches` debug setting. If the real dataset is unavailable, use synthetic inputs matching the generated validation shapes to exercise subnet extraction, forward, loss, optimizer step, validation metric computation, and checkpoint writing.
- If the user explicitly asks for an actual short retrain/finetune run, execute the launcher with a reduced budget and verify it writes the expected `retrain_latest.pth` and, when validation is configured, `retrain_best.pth`. Do not run a full production retraining schedule unless the user explicitly requests it.

---

## NPU Compatibility

The execution platform may be Huawei Ascend NPU instead of NVIDIA GPU. Apply the following rules to every generated `.py` file that touches device selection, AMP, optimizers, or gradient clipping.

### Device Selection

- CLI `--device` must include `"npu"` in the allowed choices (e.g. `["auto", "cuda", "npu", "cpu"]`).
- Do not infer the target GPU/NPU runtime from the current machine. Device and backend selection must remain runtime-configurable through the launcher and environment variables.
- Restrict visible devices through `CUDA_VISIBLE_DEVICES` (GPU) or `ASCEND_RT_VISIBLE_DEVICES` (NPU). Do not hardcode device indices.

### AMP (Automatic Mixed Precision)

- Use `autocast()` and `grad_scaler()` from `nas_agent.train`. They prefer PyTorch native `torch.amp` APIs for both CUDA and NPU.
- `GradScaler` may be disabled on NPU, where bf16 autocast is used without loss scaling. Keep the autocast enable flag independent from `scaler.is_enabled()`; use the user's AMP setting directly (e.g. `autocast(device, enabled=args.amp)`).

### Disable `foreach` Optimizations

Huawei Ascend NPU does not support PyTorch's `foreach`-based multi-tensor optimization. When the resolved device type is `"npu"`, pass `foreach=False` to both optimizer constructors and gradient clipping utilities. Determine `is_npu` once after device resolution and reuse it:

```python
is_npu = device.type == "npu"

# Optimizer constructor
optimizer = optim.AdamW(model.parameters(), ..., foreach=False if is_npu else None)

# Gradient clipping utility
torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm, foreach=False if is_npu else None)
```

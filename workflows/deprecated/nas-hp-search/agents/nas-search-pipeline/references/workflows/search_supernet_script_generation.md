# Search Supernet Script Generation Workflow

Use this workflow to generate the project-specific artifacts required for parallel NAS search. The supernet training viability and evaluation paradigm have already been determined by the calling skill and are resolved in Source Evidence below.

Generate exactly these project-specific files under `<output_dir>`:

- `search_config.yaml`: side-effect-free runtime config that tells the fixed search framework where to import the generated search space, codec, evaluator, and latency estimator.
- `arch_codec.py`: `ArchCodec` class encapsulating fixed-length gene layout, `gene_to_arch()`, and stable architecture serialization.
- `evaluator.py`: project-specific worker-side evaluator used by the fixed worker process to compute quality metrics.
- `run_search_supernet.sh`: remote launcher that calls the fixed search runner.

Do not generate `search.py`, `problem.py`, `dynamic_import.py`, `select_architecture.py`, or a separate `worker.py` as project artifacts. Search orchestration and worker-process dispatch are fixed framework behavior under `nas_agent/search/`.

The fixed goal is to produce a decoupled parallel NAS search workflow: represent each candidate as a fixed-length gene, decode it into the generated `ArchConfig`, evaluate candidate quality through validation/training semantics captured from the original user project `<user_project_root>`, measure latency on-the-fly via the generated `latency_estimator.py`, and optimize the smaller-is-better objectives `(*metrics, latency)` where `metrics` are the project quality objectives.

## Dynamic Execution Principle

Search execution is owned by the fixed framework under `nas_agent/search/`. This workflow only generates the project-specific files listed above. In the dynamic call path, the important generated artifacts are the YAML config `search_config.yaml` and the Python modules `arch_codec.py`, `evaluator.py`, plus the generated `latency_estimator.py`. The shell script is a launch helper and is not part of the dynamic import boundary.

The dynamic call flow is:

1. `run_search_supernet.sh` calls the fixed runner, for example `nas-search --config search_config.yaml`.
2. `nas-search` loads `search_config.yaml` with OmegaConf, imports `cfg.search_space`, imports `cfg.arch_codec`, constructs `search_space = SearchSpace()`, constructs `codec = ArchCodec(search_space)`, obtains gene bounds from `codec.get_gene_space()`, constructs NSGA-II, creates the fixed `NASProblem`, drives generations, and writes search logs.
3. `NASProblem` decodes genes via `arch_config=codec.gene_to_arch()` and caches results keyed by `nas_agent.search.arch_utils.serialize_arch(arch_config)`; only unseen architectures are dispatched to fixed worker processes. Fitness results are collected and returned as `(*metrics, latency)` tensors to the optimizer.
4. Each fixed worker process imports the generated modules named by `search_config.yaml`, constructs a worker-local `SearchSpace` and `ArchCodec`, constructs `LatencyEstimator(search_space, cfg.latency_cfg, device=device)`, constructs `CandidateEvaluator(device=..., evaluator_cfg=cfg.evaluator_cfg)`, decodes each gene through `codec.gene_to_arch()`, calls `evaluator.evaluate(arch_config)` for quality metrics, queries `latency_estimator.get_latency(arch_config)`, and merges those values into one result dict per gene.

The queue payload between worker processes and `NASProblem` is `(idx, result_dict)`. The dictionary contains evaluator metric keys plus the `latency` key. Do not add parameter-count payload fields by default. `NASProblem` reads objective values by key and returns them to NSGA-II in the exact order listed by `cfg.objs`.

## Source Evidence

Before generating any artifact, resolve the effective evaluation paradigm and read project sources using the priorities below. All subsequent sections in this document reference these decisions and priorities implicitly; they are not restated per-section.

### Paradigm And Viability Resolution

Read `<output_dir>/supernet_summary.md` to obtain:

- **Supernet training viability** (`Yes`/`No`): determines whether `train_supernet.py` exists under `<output_dir>`.
- **Recorded evaluation paradigm** (`validate`, `finetune`, or `train_from_scratch`).
- **Task and training context**: task type, non-obvious model-call/output specifics, and code-reference pointers captured by the upstream skill. Treat `<user_project_root>` as authoritative for full training semantics.

The **effective paradigm** is the user-specified override if one was provided by the calling skill; otherwise it is the paradigm recorded in `supernet_summary.md`. Use the effective paradigm throughout this workflow.

### Sources

Read these sources before generating any artifact:

- **`supernet_summary.md`**: supernet training viability, recorded evaluation paradigm, KD decision, and task/training context captured from the original project.
- **`<user_project_root>`** (original project): authoritative source for training semantics including data pipeline, preprocessing, transforms, loss, metrics, optimizer, scheduler, and training budget baseline.
- **`supernet.py`**: `SearchSpace`, `ArchConfig`, `SuperNet` definitions, depth controls, branch modules, and `set_sample_config()` / `get_active_subnet()` APIs.
- **`inspect_supernet.py`** (optional): structured printout of the `SearchSpace` for quick reference.
- **`train_supernet.py` and its helper files** (only available when supernet training is viable): model construction, checkpoints, data pipeline, training loop, and evaluation utilities adapted for the supernet. Reference its code when generating the evaluator and config.
- **`latency_estimator.py`**: `LatencyEstimator` class and the config attributes it expects from `search_config.yaml`.

When supernet training is not viable, `train_supernet.py` does not exist; skip any references to it and derive from `supernet.py` and `<user_project_root>` directly.

## 1. Gene And ArchConfig Codec

The EvoX evolutionary algorithm operates on fixed-length integer gene vectors; the generated supernet accepts project-specific `ArchConfig` objects. `arch_codec.py` bridges these two representations through one-way decoding (gene -> `ArchConfig`). It does not need to encode an `ArchConfig` back into a gene.

Use `references/supernet_workflow_examples/arch_codec.py` as an implementation example when generating this file, after reading the generated supernet schema.
- **Note**: that reference example follows a staged/hierarchical schema (per-stage depth candidates, `stage_names`, per-stage layer iteration). If the generated supernet uses an isotropic schema (a single flat sequence of layers with one global depth and uniform layer structure), adapt the gene layout accordingly — do not copy the staged iteration or per-stage depth segments verbatim.

Generate `arch_codec.py` containing an `ArchCodec` class that encapsulates the gene layout and all codec operations. The constructor `ArchCodec(search_space)` precomputes bounds and segment sizes once; per-gene decode calls use instance state without re-deriving the layout. Use one fixed gene layout consistently in bounds, decoding, worker dispatch, search logs, and selected-candidate export.

Since `arch_codec.py` is a generated sibling of the generated supernet module, it should directly import `SearchSpace` and `ArchConfig` from the supernet module (e.g. `from supernet import SearchSpace, ArchConfig`). Use the concrete types in type hints and construction calls.

### Public API

All public methods are instance methods on `ArchCodec`. Callers construct `codec = arch_codec.ArchCodec(search_space)` once and reuse it.

- `codec.get_gene_space()`: returns a dictionary containing `gene_len`, `lower_bounds`, `upper_bounds`, and a nested `metadata` dictionary. Called by `search.py` at startup to obtain EvoX bounds.
- `codec.gene_to_arch(gene)`: converts one raw optimizer gene into the exact generated `ArchConfig`. Internally rounds continuous floats to integers. Must construct the exact generated `ArchConfig` schema without silently renaming choices, guessing missing fields, or remapping block names.

### Encoding Layout

The gene is a fixed-length integer vector whose length is determined by the maximum active subnet allowed by the generated `SearchSpace`, not by any one sampled candidate. Every gene stores the candidate index, not the actual candidate value; decoding applies `candidates[gene_index]` to obtain the value written into `ArchConfig`.

The codec must infer the layout from the generated `SearchSpace` and `ArchConfig` schema:

- staged or hierarchical schemas: encode depth first, then any explicitly searchable stage-level discrete fields, then layer-level branch choices and branch-local parameters in deterministic stage/layer order;
- isotropic schemas: encode global depth first, then layer-level branch choices and branch-local parameters in deterministic layer order;
- fixed architecture metadata such as fixed widths, fixed embedding dimensions, fixed stems, fixed merge/downsample modules, and fixed heads must not become gene entries unless the generated `ArchConfig` explicitly records them as searched values.

For depth-search supernets, reserve gene slots for every layer position up to the maximum depth represented by the generated `SearchSpace`. Smaller architectures still carry the full-length gene; inactive layer slots are padding by convention and must not affect the decoded `ArchConfig`. Branch parameter slots unused by the selected branch must also not affect the decoded `ArchConfig`.

Example

max-depth candidate (all layers active):

```python
# SearchSpace:
#   2 stages, stage_names = ("stage1", "stage2")
#   stage_depth_candidates = ((1, 2), (1, 2, 3))
#   block choices per stage: ["cswin", "swin_window"]  (same in both stages)
#   searchable param keys (union of key names across blocks):
#     ffn_dim, num_heads                                (sorted alphabetically)
#   per-stage candidate ranges:
#     stage1: ffn_dim from (128, 256),            num_heads from (2, 4)
#     stage2: ffn_dim from (256, 512, 768, 1024), num_heads from (4, 8, 12)
#
# max_active_layers = max(1,2) + max(1,2,3) = 2 + 3 = 5
# genes_per_layer  = 1 (branch choice) + 2 (ffn_dim, num_heads) = 3
# gene_len = 2 (depth) + 5 x 3 (layers) = 17

# Candidate: stage1 depth=2, stage2 depth=3  ->  5 active layers
gene = (
    # depth segment (2 genes)
    1,                 # stage1 depth: candidates[1] = 2
    2,                 # stage2 depth: candidates[2] = 3
    # stage1 layer segment (2 layers x 3 genes)
    1, 1, 1,           # stage1.layer0: branch=swin_window, ffn_dim=256,  num_heads=4
    0, 0, 0,           # stage1.layer1: branch=cswin,       ffn_dim=128,  num_heads=2
    # stage2 layer segment (3 layers x 3 genes)
    1, 3, 2,           # stage2.layer0: branch=swin_window, ffn_dim=1024, num_heads=12
    0, 1, 0,           # stage2.layer1: branch=cswin,       ffn_dim=512,  num_heads=4
    1, 2, 1,           # stage2.layer2: branch=swin_window, ffn_dim=768,  num_heads=8
)

# codec.gene_to_arch(gene) decodes the gene into the corresponding ArchConfig:
arch_config = ArchConfig(
    stage_depths=(2, 3),
    layer_configs={
        "stage1": (
            {"choice": "swin_window", "config": {"num_heads": 4, "ffn_dim": 256}},
            {"choice": "cswin",       "config": {"num_heads": 2, "ffn_dim": 128}},
        ),
        "stage2": (
            {"choice": "swin_window", "config": {"num_heads": 12, "ffn_dim": 1024}},
            {"choice": "cswin",       "config": {"num_heads": 4,  "ffn_dim": 512}},
            {"choice": "swin_window", "config": {"num_heads": 8,  "ffn_dim": 768}},
        ),
    },
)
```

reduced-depth candidate (inactive layers zeroed):

```python
# Candidate: stage1 depth=1, stage2 depth=2  ->  3 active, 2 inactive
gene = (
    0,                 # stage1 depth: candidates[0] = 1
    1,                 # stage2 depth: candidates[1] = 2
    # stage1
    0, 0, 1,           # stage1.layer0: branch=cswin, ffn_dim=128, num_heads=4    (active)
    0, 0, 0,           # stage1.layer1: padding -- inactive
    # stage2
    1, 3, 2,           # stage2.layer0: branch=swin_window, ffn_dim=1024, num_heads=12 (active)
    0, 1, 0,           # stage2.layer1: branch=cswin, ffn_dim=512, num_heads=4    (active)
    0, 0, 0,           # stage2.layer2: padding -- inactive
)
# gene_len is still 17; padding slots do not affect arch_config.
# Each stage in layer_configs contains only its active layers.
```

## 2. Evaluator

The generated `evaluator.py` is responsible for evaluating the fitness of generated candidate subnets using the real project's data pipelines, loss functions, and metrics.

Design assumptions:

- The `CandidateEvaluator` runs exclusively on one worker-selected device.
- It maintains one complete supernet instance on that device at all times.

Use `references/supernet_workflow_examples/evaluator.py` as an implementation example when generating this file.

Generate `evaluator.py` following Source Evidence. Mirror the data pipeline, preprocessing/tokenizer/transforms, batch structure, model-call signature, loss, checkpoint loading, and metric behavior from those sources. Generate only the code path for the effective evaluation paradigm. Do not generate runtime mode-switching logic.

### Model Construction

Import `SearchSpace` and `SuperNet` from the generated `supernet.py`.

When `train_supernet.py` exists, reference its model construction for constructor arguments and checkpoint-key compatibility. When `train_supernet.py` does not exist (supernet training not viable), derive constructor arguments from `<user_project_root>`.

Keep generated evaluation code self-contained for remote execution. It may import generated helper files under `<output_dir>`, but it must not depend on the original project under `<user_project_root>` being importable on the remote search server unless that dependency is already part of the generated artifact contract.

### Public API

- `CandidateEvaluator(device=..., evaluator_cfg=...)`
  - Constructor. The fixed worker passes `device` and the raw OmegaConf `evaluator_cfg` node from `search_config.yaml`.
  - Eagerly initialize the supernet, data loaders, and criteria here.
  - Treat `evaluator_cfg` as an OmegaConf node; access attributes via dot notation (e.g. `self.cfg.batch_size`, `self.cfg.lr`) and use `.get()` for optional properties like `supernet_ckpt_path` or `amp`.
- `evaluate(arch_config: ArchConfig) -> dict[str, float]`
  - Configure the supernet with the target `arch_config`, run the chosen evaluation paradigm, and return a dict of smaller-is-better metric values.
  - **CRITICAL Return Format**: Keys must be pure strings matching `search_config.yaml` `objs` (excluding `latency`); values must be Python built-in scalars (extract PyTorch tensors via `.item()` to prevent OOM/crashes).

### Evaluation Paradigms

Generate only the chosen evaluation paradigm's code path:

- **validate**: Default. Supernet has been trained with sandwich sampling and weight-sharing quality is expected to be reliable. Configure the supernet with the candidate `ArchConfig` and run the validation loop directly; no subnet extraction or additional training.
- **finetune**: Supernet has been trained but direct validation is unreliable due to a large weight-sharing gap or a domain shift between training and evaluation (e.g. highly heterogeneous block types, large capacity variance across subnets, pretrained backbone replacement, or cross-dataset evaluation where the supernet was pretrained on a source dataset and the search must rank subnets on a different target dataset). Extract the subnet from the trained supernet, short-train it on the target data, then validate.
- **train_from_scratch**: No trained supernet checkpoint is used. Extract the subnet, re-initialize weights, train from scratch, then validate. Derive training semantics following Source Evidence; whether `train_supernet.py` is available depends on supernet training viability.

**validate**:
Configure the supernet (`self.supernet.set_sample_config(arch_config)`) and run the validation loop directly on the supernet. No training, no subnet extraction.

**finetune** and **train_from_scratch**:
Both paradigms extract a subnet via `get_active_subnet()`, train it independently on a single device, validate, clean up memory, and return metrics.
- `finetune` inherits pretrained weights from the trained supernet
- `train_from_scratch` re-initializes the extracted subnet before training.
See `references/evaluator_training_loop_guide.md` for the detailed evaluation flow, training loop implementation, and example code.

### Data And Metric Semantics

- **Metric selection**: Identify the quality metrics from the project sources (see Source Evidence). Inspect validation or test functions for the metrics they compute and log (e.g. accuracy, top-k accuracy, F1, mAP, BLEU). If no source defines explicit metrics beyond the training loss, fall back to returning the validation loss as the sole quality objective. The chosen metric names become the `evaluate()` return dict keys and the quality objective entries in `search_config.yaml` `objs` (excluding the special `latency` entry).
- Eagerly create validation dataloaders, and optional training dataloaders for finetune/train_from_scratch directly inside `__init__`, using parameters from `evaluator_cfg`.
- Preserve preprocessing, tokenizer/transforms, collation, batch structure, target format, and loss semantics following Source Evidence.
- All metric values returned by `evaluate` must be smaller-is-better. For any larger-is-better metric (accuracy, top-k accuracy, F1, mAP, reward, BLEU, task score, etc.), negate it. For loss, perplexity, error rate, WER, or other lower-is-better metrics, return the value directly.

#### Metric Fidelity

The metrics are the architecture-ranking signals. Trace the call chain from the project's training entry point, find the function that computes each metric, and port that function's logic faithfully — do not substitute it with a simpler approximation. To reduce per-candidate evaluation cost, cut iteration counts (fewer episodes, epochs, or steps) via `evaluator_cfg`; do not replace the per-step computation itself with a cheaper function.

## 3. Runtime Config

Generate `search_config.yaml` after `evaluator.py` is finalized. It must contain plain YAML values only, with no dataset construction, no model construction, and no side effects.

Use this section as the complete config contract. The config key names are part of the fixed framework interface and must be used exactly as written. Do not rename them.

Required generated config keys:

- `search_space`: importable path to the generated `SearchSpace` class, e.g. `"supernet.SearchSpace"`;
- `arch_codec`: importable path to `ArchCodec`;
- `evaluator`: importable path to `CandidateEvaluator`;
- `latency_estimator`: importable path to `LatencyEstimator`;
- `latency_cfg`: latency measurement parameters consumed by the generated `LatencyEstimator`.
  - Must include: `warmup`, `repetitions`, and `batch_size`.
  - **Note**: The evaluation `device` (e.g., `"npu:0"`) is injected dynamically by the search worker and is no longer configured here.
- `objs`: objective names in the exact order passed to NSGA-II; list quality objectives first and `latency` last;
- `latency_constraint`: optional latency upper bound consumed directly by the fixed search problem. Set to `null` or omit it to disable latency rejection; set a number to skip quality evaluation for over-constraint candidates and assign worst fitness directly;
- `search_log_path`;
- `concurrency`, `population_size`, `num_generations`;
- `evaluator_cfg`: project-specific evaluator settings.

Use `references/supernet_workflow_examples/search_config.yaml` as the structural reference when generating this file. Replace placeholder paths and values with the generated project values.

The `evaluator_cfg` block holds all project-specific evaluator settings. Adapt settings following Source Evidence. Put the supernet checkpoint path at `evaluator_cfg.supernet_ckpt_path` when applicable; the fixed worker passes only `evaluator_cfg` to `CandidateEvaluator`.

Populate `evaluator_cfg` with the fields appropriate for the effective evaluation paradigm:

- **validate**: `data_dir` and data-related fields, `supernet_ckpt_path` when a trained supernet checkpoint is expected, batch size, worker count, AMP flag, and any project-specific validation controls.
  - **Validation budget**: By default, run the full validation set per candidate. If the agent determines that per-candidate validation is prohibitively expensive (e.g. very large validation set, multi-step generative inference such as diffusion sampling, multi-scale / test-time augmentation, costly post-processing such as NMS or CRF), reduce the validation budget by subsampling the validation set or capping evaluation iterations. Expose the reduction as `evaluator_cfg` fields (e.g. `max_val_samples` or `max_val_batches`) and document the rationale in `search_config.yaml` comments.
- **finetune**: validate fields plus optimizer/scheduler fields such as `lr`, `weight_decay`, and `epochs`.
  - **Training budget**: Use the original single-model training budget from `<user_project_root>` (see Source Evidence) as the reference. Since subnets inherit pretrained weights from the supernet, the finetune budget should be a small fraction of the original, typically **5%–20%** of the full training horizon (minimum 1 epoch). Choose the exact fraction by considering task complexity, dataset scale, and the per-candidate cost multiplied by total search population.
  - **Scheduler and related hyperparameters**: The finetune horizon is drastically shortened, so the original scheduler strategy may no longer be appropriate (e.g. multi-step milestones become meaningless at 5 epochs). Decide whether to compress the original schedule's values or switch to a simpler strategy (e.g. cosine annealing, constant LR) that fits the short window. Also adjust warmup steps, decay milestones, and other budget-dependent hyperparameters. Reason about the best choice from the project's training recipe and the chosen epoch count.
  - **Dataset**: Set `data_dir` and related fields to point to the evaluation data. When the search targets a different dataset than supernet pretraining, point to the target dataset instead.
  - **Checkpoints**: Set `save_dir` (string, default `"./runs/search/finetune_ckpts"`) to specify the root directory for per-architecture checkpoint subdirectories (see the checkpoint layout in the finetune flow above).
- **train_from_scratch**: same fields as finetune, except omit `supernet_ckpt_path`.
  - **Training budget**: Use the original single-model training budget from `<user_project_root>` (see Source Evidence) as the reference. Estimate the convergence speed from the project's training configuration (dataset size, model scale, optimizer, scheduler) and set a reduced `epochs` that is sufficient to differentiate architecture quality without running the full training schedule. The search-phase budget must be strictly less than the original single-model budget.
  - **Scheduler and related hyperparameters**: Scale down the original project's budget-dependent hyperparameters (lr scheduler, warmup steps, decay milestones, etc.) proportionally to the reduced training horizon. Reason about the specific adjustments from the project's training recipe.
  - **Dataset**: Set `data_dir` and related fields to point to the training and validation data used in `<user_project_root>`. Preserve the original data split, preprocessing, and augmentation conventions.
  - **Checkpoints**: Set `save_dir` (string, default `"./runs/search/scratch_ckpts"`) to specify the root directory for per-architecture checkpoint subdirectories.

Keep GPU/NPU device selection runtime-configurable. The fixed framework detects visible devices; restrict devices through the runtime's visible-device environment variables such as `CUDA_VISIBLE_DEVICES` or the remote NPU equivalent. Do not add a `gpus` config key unless the fixed framework implements it.

## 4. Search Launcher

The generated `run_search_supernet.sh` is the entry point for this step, in the same spirit that the train workflow generates a launcher for `train_supernet.py`. The search launcher should call the existing framework search runner with the generated `search_config.yaml`.

Required launcher behavior:

- the working directory is `<output_dir>`; sibling modules are importable as plain imports;
- do not run training or latency profiling;
- assume the remote server has already produced the trained supernet checkpoint;
- all search parameters are defined in `search_config.yaml`; the launcher passes only `--config "./search_config.yaml"` to `nas-search` and must not pass any other CLI arguments or parameter overrides.

Launcher skeleton:

```bash
#!/usr/bin/env bash
set -euo pipefail

nas-search --config "./search_config.yaml"
```

After writing, mark executable: `chmod +x run_search_supernet.sh`.

The launcher must not implement Pareto, population, resume, or payload formatting logic. Those behaviors and settings are owned by the existing search runner and `search_config.yaml`. The launcher only points the runner at the generated config and ensures the generated modules and latency artifacts are importable.

## Validation

The generated search artifacts are for remote-server execution. Local validation must not run full NAS search, spawn search workers, or evaluate a population of candidates. However, single-device smoke tests are required to surface runtime errors that static checks like `py_compile` cannot detect.

If a check fails, fix the generated files and rerun the failed check until all checks pass.

Allowed:

- `bash -n run_search_supernet.sh`
- Verify the `search_config.yaml` integration: load the config, dynamically import `SearchSpace`, `ArchCodec`, `CandidateEvaluator`, and `LatencyEstimator` using the import paths in the config, construct `SearchSpace()` and `ArchCodec(search_space)`, and verify gene bounds are valid — reproducing the initialization steps of `nas-search` up to but not including `NASProblem` construction or worker spawning
- Smoke-test `evaluator.py` on a single device: construct `CandidateEvaluator` with a minimal synthetic config (synthetic data matching the project's expected input shapes, small batch size, `num_workers=0`), sample one random `ArchConfig`, call `evaluate()`, and verify the returned metric keys and values are valid
- **Budget-hyperparameter coherence:** for `finetune` and `train_from_scratch` paradigms, verify that budget-dependent hyperparameters in `evaluator_cfg` (lr scheduler total steps/epochs, warmup steps, decay milestones, etc.) are coherent with the configured training budget. If the budget was reduced from the original project baseline, confirm the scheduler and related settings were adjusted accordingly. For all paradigms, if the validation budget was reduced (e.g. `max_val_samples` or `max_val_batches` set in `evaluator_cfg`), verify that `evaluator.py` actually consumes these fields to cap evaluation.

Forbidden:

- Do not run full NAS search (`nas-search`), spawn worker processes, or evaluate a population of candidates locally
- Do not use real datasets for the evaluator smoke test; use only synthetic data

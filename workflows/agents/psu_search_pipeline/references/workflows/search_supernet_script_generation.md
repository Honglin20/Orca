# Search Supernet Script Generation Workflow

Use this workflow to generate the project-specific artifacts required for parallel NAS search. The supernet is trained upstream by knowledge distillation; the search-time evaluation paradigm is fixed to zero-training `validate` (see Source Evidence).

Generate exactly these project-specific files under `<output_dir>`:

- `search_config.yaml`: side-effect-free runtime config that tells the fixed search framework where to import the generated search space, codec, evaluator, and latency estimator.
- `arch_codec.py`: `ArchCodec` class encapsulating fixed-length gene layout, `gene_to_arch()`, and stable architecture serialization.
- `evaluator.py`: project-specific worker-side evaluator used by the fixed worker process to compute quality metrics.
- `run_search_supernet.sh`: remote launcher that calls the fixed search runner.

Do not generate `search.py`, `problem.py`, `dynamic_import.py`, `select_architecture.py`, or a separate `worker.py` as project artifacts. Search orchestration and worker-process dispatch are fixed framework behavior under `nas_agent/search/`.

The fixed goal is to produce a decoupled parallel NAS search workflow: represent each candidate as a fixed-length gene, decode it into the generated `ArchConfig`, evaluate candidate quality through zero-training validation semantics captured from the original user project `<user_project_root>`, measure latency on-the-fly via the generated `latency_estimator.py`, and optimize the smaller-is-better objectives `(*metrics, latency)` where `metrics` are the project quality objectives.

## Dynamic Execution Principle

Search execution is owned by the fixed framework under `nas_agent/search/`. This workflow only generates the project-specific files listed above. In the dynamic call path, the important generated artifacts are the YAML config `search_config.yaml` and the Python modules `arch_codec.py`, `evaluator.py`, plus the generated `latency_estimator.py`. The shell script is a launch helper and is not part of the dynamic import boundary.

The dynamic call flow is:

1. `run_search_supernet.sh` calls the fixed runner via module invocation: `python3 -m nas_agent.cli.search --config search_config.yaml` (module invocation — no PATH assumption; the `nas-search` console script may not be on `PATH` on the remote box).
2. The runner (`nas_agent.cli.search`) loads `search_config.yaml` with OmegaConf, imports `cfg.search_space`, imports `cfg.arch_codec`, constructs `search_space = SearchSpace()`, constructs `codec = ArchCodec(search_space)`, obtains gene bounds from `codec.get_gene_space()`, constructs NSGA-II, creates the fixed `NASProblem`, drives generations, and writes search logs.
3. `NASProblem` decodes genes via `arch_config=codec.gene_to_arch()` and caches results keyed by `nas_agent.search.arch_utils.serialize_arch(arch_config)`; only unseen architectures are dispatched to fixed worker processes. Fitness results are collected and returned as `(*metrics, latency)` tensors to the optimizer.
4. Each fixed worker process imports the generated modules named by `search_config.yaml`, constructs a worker-local `SearchSpace` and `ArchCodec`, constructs `LatencyEstimator(search_space, cfg.latency_cfg, device=device)`, constructs `CandidateEvaluator(device=..., evaluator_cfg=cfg.evaluator_cfg)`, decodes each gene through `codec.gene_to_arch()`, calls `evaluator.evaluate(arch_config)` for quality metrics, queries `latency_estimator.get_latency(arch_config)`, and merges those values into one result dict per gene.

The queue payload between worker processes and `NASProblem` is `(idx, result_dict)`. The dictionary contains evaluator metric keys plus the `latency` key. Do not add parameter-count payload fields by default. `NASProblem` reads objective values by key and returns them to NSGA-II in the exact order listed by `cfg.objs`.

## Source Evidence

Before generating any artifact, read project sources using the priorities below. All subsequent sections in this document reference these decisions and priorities implicitly; they are not restated per-section.

### Paradigm Resolution

The evaluation paradigm is a fixed constant: **`validate` (zero-training)**. There is no paradigm override. Each candidate is a per-slot choice path applied with `set_sample_config()`; the evaluator runs direct inference on the supernet's inherited weights — it never trains, fine-tunes, or re-initializes a candidate.

Read `<output_dir>/supernet_summary.md` to obtain the KD training facts and NAS decisions made upstream. The recorded evaluation paradigm is `validate`; treat any other recorded value as an upstream contract error and fail loud instead of adapting the evaluator.

Read `<output_dir>/project_manifest.md` for the original project's task context, training semantics, and code-reference pointers. The manifest is the navigation layer, not the ground truth: when a detail you need is missing from it or looks inaccurate, explore `<user_project_root>` (targeted, guided by **Relevant Source Files**) and update the manifest in place. `<user_project_root>` source code is always authoritative.

### Sources

Read these sources before generating any artifact:

- **`supernet_summary.md`**: KD training facts, the recorded evaluation paradigm (`validate`), and NAS decisions made upstream. KD distillation is this pipeline's fixed training paradigm — the teacher is the frozen pretrained original model — decided at the upstream train node; the search never re-reads or re-decides it. The KD loss and the teacher must not enter the evaluator: the search-time metric is the user's original validation metric, nothing else.
- **`project_manifest.md`**: the persistent map of the original project (model, training/evaluation, data/environment, key source files). Use it to navigate `<user_project_root>` instead of bulk-reading.
- **`<user_project_root>`** (original project): authoritative source for the validation data pipeline, preprocessing, transforms, loss, and metrics. Consult it through targeted, manifest-guided reads; when it corrects or extends the manifest, write the fix back into `project_manifest.md`.
- **`supernet.py`**: `SearchSpace`, `ArchConfig`, `SuperNet` definitions, the branch modules behind each layer slot, and `set_sample_config()` / `get_active_subnet()` APIs.
- **`inspect_supernet.py`** (optional): structured printout of the `SearchSpace` for quick reference.
- **`train_supernet.py` and its helper files** (the KD training script generated upstream): model construction, checkpoint conventions, data pipeline, and evaluation utilities adapted for the supernet. Reference its code when generating the evaluator and config — especially the data pipeline and validation entry, which the evaluator reuses.
- **`latency_estimator.py`**: `LatencyEstimator` class and the config attributes it expects from `search_config.yaml`.
- **Ported helper files** (when the calling skill delegated porting to `project-porter` subagents): already under `<output_dir>`; import them as siblings and write call sites against the porter's API report instead of re-porting. Interface mismatches are resolved in this workflow as they surface: adapt your call sites or edit the helper files directly, never add wrapper layers. When touching ported logic (formulas, control flow, constants), preserve the original project's semantics.

## 1. Gene And ArchConfig Codec

The EvoX evolutionary algorithm operates on fixed-length integer gene vectors; the generated supernet accepts project-specific `ArchConfig` objects. `arch_codec.py` bridges these two representations through one-way decoding (gene -> `ArchConfig`). It does not need to encode an `ArchConfig` back into a gene.

Use `references/supernet_workflow_examples/arch_codec.py` as an implementation example when generating this file, after reading the generated supernet schema. It implements the choice-only layout directly; adapt names and values to the generated `SearchSpace`.

Generate `arch_codec.py` containing an `ArchCodec` class that encapsulates the gene layout and all codec operations. The constructor `ArchCodec(search_space)` precomputes bounds and segment sizes once; per-gene decode calls use instance state without re-deriving the layout. Use one fixed gene layout consistently in bounds, decoding, worker dispatch, search logs, and selected-candidate export.

Since `arch_codec.py` is a generated sibling of the generated supernet module, it should directly import `SearchSpace` and `ArchConfig` from the supernet module (e.g. `from supernet import SearchSpace, ArchConfig`). Use the concrete types in type hints and construction calls.

### Public API

All public methods are instance methods on `ArchCodec`. Callers construct `codec = arch_codec.ArchCodec(search_space)` once and reuse it.

- `codec.get_gene_space()`: returns a dictionary containing `gene_len`, `lower_bounds`, `upper_bounds`, and a nested `metadata` dictionary. Called by `search.py` at startup to obtain EvoX bounds.
- `codec.gene_to_arch(gene)`: converts one raw optimizer gene into the exact generated `ArchConfig`. Internally rounds continuous floats to integers. Must construct the exact generated `ArchConfig` schema without silently renaming choices, guessing missing fields, or remapping block names.

### Encoding Layout

The gene is a fixed-length integer vector with **one gene per transformer layer slot**; its length equals the slot count (`SearchSpace.depth`). Every gene stores a **candidate index** into `SearchSpace.branch_choices`, not the branch name; decoding applies `branch_choices[gene_index]` to obtain the branch written into `ArchConfig.choices`.

The codec must infer the layout from the generated `SearchSpace` and `ArchConfig` schema:

- one branch-choice gene per layer slot, in the original layer order;
- no depth segment: depth is pinned to the original layer count — no layer is ever dropped, added, or skipped, so there are no padding slots and no inactive genes;
- no branch-local parameter segments: every branch has a fixed shape derived from the pinned slot facts (widths, head counts, sequence lengths are never searched);
- fixed architecture metadata (embeddings, stems, merge/downsample modules, heads) never becomes gene entries.

Example

```python
# SearchSpace (choice-only):
#   branch_choices = ("original", "vanilla", "random_synthesizer",
#                     "relu_attention", "fnet", "softs_star")
#   depth = 4          # slot count, pinned to the original layer count
#
# gene_len = 4 (one branch-index gene per slot)
# bounds    = [0, 5] for every gene

# Candidate: slot0=original, slot1=relu_attention,
#            slot2=softs_star, slot3=vanilla
gene = (
    0,   # slot0: branch_choices[0] = "original"
    3,   # slot1: branch_choices[3] = "relu_attention"
    5,   # slot2: branch_choices[5] = "softs_star"
    1,   # slot3: branch_choices[1] = "vanilla"
)

# codec.gene_to_arch(gene) decodes the gene into the corresponding ArchConfig:
arch_config = ArchConfig(
    choices=("original", "relu_attention", "softs_star", "vanilla"),
)
```

all-original candidate (the equivalence anchor and baseline reference):

```python
gene = (0, 0, 0, 0)
# gene_to_arch(gene) ->
# ArchConfig(choices=("original", "original", "original", "original"))
```

## 2. Evaluator

The generated `evaluator.py` is responsible for evaluating the fitness of generated candidate subnets using the real project's data pipelines, loss functions, and metrics.

Design assumptions:

- The `CandidateEvaluator` runs exclusively on one worker-selected device.
- It maintains one complete supernet instance on that device at all times.

Use `references/supernet_workflow_examples/evaluator.py` as an implementation example when generating this file.

Generate `evaluator.py` following Source Evidence. Mirror the data pipeline, preprocessing/tokenizer/transforms, batch structure, model-call signature, loss (when the validation loss serves as the quality objective), checkpoint loading, and metric behavior from those sources. Generate only the `validate` code path. Do not generate runtime mode-switching logic.

### Model Construction

Import `SearchSpace` and `SuperNet` from the generated `supernet.py`.

Reference `train_supernet.py`'s model construction for constructor arguments and checkpoint-key compatibility — the evaluator must load the KD training run's checkpoint with strict key matching.

Keep generated evaluation code self-contained for remote execution. It may import generated helper files under `<output_dir>`, but it must not depend on the original project under `<user_project_root>` being importable on the remote search server unless that dependency is already part of the generated artifact contract.

### Public API

- `CandidateEvaluator(device=..., evaluator_cfg=...)`
  - Constructor. The fixed worker passes `device` and the raw OmegaConf `evaluator_cfg` node from `search_config.yaml`.
  - Eagerly initialize the supernet, data loaders, and criteria here.
  - Treat `evaluator_cfg` as an OmegaConf node; access attributes via dot notation (e.g. `self.cfg.batch_size`, `self.cfg.num_workers`) and use `.get()` for optional properties like `supernet_ckpt_path` or `amp`.
- `evaluate(arch_config: ArchConfig) -> dict[str, float]`
  - Configure the supernet with the target `arch_config`, run the chosen evaluation paradigm, and return a dict of smaller-is-better metric values.
  - **CRITICAL Return Format**: Keys must be pure strings matching `search_config.yaml` `objs` (excluding `latency`); values must be Python built-in scalars (extract PyTorch tensors via `.item()` to prevent OOM/crashes).

### Evaluation Paradigm

The paradigm is fixed and single-valued: **`validate` (zero-training)**.

Configure the supernet (`self.supernet.set_sample_config(arch_config)`) and run the validation loop directly on the supernet. No subnet extraction, no training, no re-initialization.

**Reverse guard (fail loud)**: the generated `evaluator.py` must contain none of the following —

- optimizer construction (`torch.optim.*`), LR schedulers, or gradient scalers;
- a training loop: no backward pass, no `loss.backward()`, no parameter updates;
- checkpoint writes — the evaluator never saves a model; per-candidate checkpoints do not exist in this paradigm;
- a teacher model or any KD loss term. The search-time metric is the user's original validation metric; KD and the teacher belong to the upstream training node only.

### Data And Metric Semantics

- **Metric selection**: Identify the quality metrics from the project sources (see Source Evidence). Inspect validation or test functions for the metrics they compute and log (e.g. accuracy, top-k accuracy, F1, mAP, BLEU). If no source defines explicit metrics beyond the training loss, fall back to returning the validation loss as the sole quality objective. The chosen metric names become the `evaluate()` return dict keys and the quality objective entries in `search_config.yaml` `objs` (excluding the special `latency` entry).
- Eagerly create the validation dataloader(s) directly inside `__init__`, using parameters from `evaluator_cfg`.
- Preserve preprocessing, tokenizer/transforms, collation, batch structure, target format, and loss semantics following Source Evidence.
- All metric values returned by `evaluate` must be smaller-is-better. For any larger-is-better metric (accuracy, top-k accuracy, F1, mAP, reward, BLEU, task score, etc.), negate it. For loss, perplexity, error rate, WER, or other lower-is-better metrics, return the value directly. Compute and track metrics in their natural direction throughout the evaluation; apply negation only at the final return of `evaluate()`.
- Use `AverageMeter` from `nas_agent.train.metrics` to accumulate loss and metric values across batches.

#### Metric Fidelity

The metrics are the architecture-ranking signals. Trace the call chain from the project's training entry point, find the function that computes each metric, and port that function's logic faithfully; do not substitute it with a simpler approximation. To reduce per-candidate evaluation cost, cut iteration counts (fewer episodes, epochs, or steps) via `evaluator_cfg`; do not replace the per-step computation itself with a cheaper function.

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
  - **Note**: The evaluation `device` (e.g., `"npu:0"`) is injected dynamically by the search worker and is not configured here.
- `objs`: objective names in the exact order passed to NSGA-II; list quality objectives first and `latency` last;
- `latency_constraint`: optional latency upper bound consumed directly by the fixed search problem. Set to `null` or omit it to disable latency rejection; set a number to skip quality evaluation for over-constraint candidates and assign worst fitness directly;
- `search_log_path`;
- `concurrency`, `population_size`, `num_generations`;
- `evaluator_cfg`: project-specific evaluator settings.

Use `references/supernet_workflow_examples/search_config.yaml` as the structural reference when generating this file. Replace placeholder paths and values with the generated project values.

The `evaluator_cfg` block holds all project-specific evaluator settings. Adapt settings following Source Evidence. Put the supernet checkpoint path at `evaluator_cfg.supernet_ckpt_path` when applicable; the fixed worker passes only `evaluator_cfg` to `CandidateEvaluator`.

Populate `evaluator_cfg` with the fields the `validate` paradigm needs:

- `data_dir` and data-related fields, `supernet_ckpt_path` (the KD training run's checkpoint, typically `./runs/train/supernet_best.pth`), batch size, worker count, AMP flag, and any project-specific validation controls.
- **Validation budget**: By default, run the full validation set per candidate. If the agent determines that per-candidate validation is prohibitively expensive (e.g. very large validation set, multi-step generative inference such as diffusion sampling, multi-scale / test-time augmentation, costly post-processing such as NMS or CRF), reduce the validation budget by subsampling the validation set or capping evaluation iterations. Expose the reduction as `evaluator_cfg` fields (e.g. `max_val_samples` or `max_val_batches`) and document the rationale in `search_config.yaml` comments.
- **Forbidden fields**: optimizer / scheduler / training-budget fields (`lr`, `weight_decay`, `epochs`, `save_dir`, ...) do not belong in `evaluator_cfg` — the evaluator never trains. Their presence indicates a paradigm leak.

Keep GPU/NPU device selection runtime-configurable. The fixed framework detects visible devices; restrict devices through the runtime's visible-device environment variables such as `CUDA_VISIBLE_DEVICES` or the remote NPU equivalent. Do not add a `gpus` config key unless the fixed framework implements it.

## 4. Search Launcher

The generated `run_search_supernet.sh` is the entry point for this step. The search launcher should call the existing framework search runner with the generated `search_config.yaml`.

Required launcher behavior:

- the working directory is `<output_dir>`; sibling modules are importable as plain imports;
- do not run training or latency profiling;
- assume the remote server has already produced the trained supernet checkpoint;
- all search parameters are defined in `search_config.yaml`; the launcher passes only `--config "./search_config.yaml"` to the runner and must not pass any other CLI arguments or parameter overrides.

Launcher skeleton:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Module invocation — no PATH assumption (the nas-search console script may not be
# installed on PATH on the remote box; the module path always resolves once
# nas_agent is importable).
python3 -m nas_agent.cli.search --config "./search_config.yaml"
```

After writing, mark executable: `chmod +x run_search_supernet.sh`.

The launcher must not implement Pareto, population, resume, or payload formatting logic. Those behaviors and settings are owned by the existing search runner and `search_config.yaml`. The launcher only points the runner at the generated config and ensures the generated modules and latency artifacts are importable.

## Validation

The generated search artifacts are for remote-server execution. Local validation must not run full NAS search, spawn search workers, or evaluate a population of candidates. However, single-device smoke tests are required to surface runtime errors that static checks like `py_compile` cannot detect.

If a check fails, fix the generated files and rerun the failed check until all checks pass.

Allowed:

- `bash -n run_search_supernet.sh` (inline)
- Persistent smoke test: write `<output_dir>/tests/test_search_evaluator_smoke.py` (plain script per the skill's Persistent Tests convention, starting with the sibling-import `sys.path` bootstrap) and run `python tests/test_search_evaluator_smoke.py` from `<output_dir>`. The script must cover:
  - **Config integration**: load `search_config.yaml`, dynamically import `SearchSpace`, `ArchCodec`, `CandidateEvaluator`, and `LatencyEstimator` using the import paths in the config, construct `SearchSpace()` and `ArchCodec(search_space)`, and verify gene bounds are valid, reproducing the initialization steps of `nas_agent.cli.search` up to but not including `NASProblem` construction or worker spawning
  - **Evaluator smoke**: construct `CandidateEvaluator` on a single device with a minimal synthetic config (synthetic data matching the project's expected input shapes, small batch size, `num_workers=0`), sample one random `ArchConfig`, call `evaluate()`, and verify the returned metric keys and values are valid
- **Validation-budget coherence (inline review):** if the validation budget was reduced (e.g. `max_val_samples` or `max_val_batches` set in `evaluator_cfg`), verify that `evaluator.py` actually consumes these fields to cap evaluation.

Forbidden:

- Do not run full NAS search (`python3 -m nas_agent.cli.search`), spawn worker processes, or evaluate a population of candidates locally
- Do not use real datasets for the evaluator smoke test; use only synthetic data

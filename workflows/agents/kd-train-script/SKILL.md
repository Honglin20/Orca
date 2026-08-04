---
name: kd-train-script
description: Generate the four KD-NAS training leaves (loss/data/eval/optim) plus run_config.yaml and run.sh from the user's train.py. The leaves are consumed by the fixed KDTrainer engine at _kd_scripts/train_pipeline.py.
---

# KD-NAS Train Leaves Generator

Use this skill to generate the four project-specific training **leaves** that
the fixed `KDTrainer` engine (`workflows/agents/_kd_scripts/train_pipeline.py`)
loads at runtime. The leaves plus a `run_config.yaml` and a human-use `run.sh`
are the only artifacts produced — there is no monolithic generated script.

## Leaf contract (authoritative — `workflows/agents/_kd_scripts/CONTRACTS.md` §6)

Four files land under `<output_dir>/user/`:

| file | callable | signature | returns |
|---|---|---|---|
| `loss.py` | `compute_loss` | `(s_out, y)` | scalar `Tensor` |
| `data.py` | `build_dataloader` | `(batch_size)` | re-iterable yielding `(x, y)` |
| `eval.py` | `eval_metric` | `(student, device)` | `(value, kind)`; `kind ∈ {nmse, mse, ber, db, snr, acc}` |
| `optim.py` | `build_optimizer` | `(params, lr)` | `Optimizer | None` |
| `optim.py` | `build_scheduler` | `(optimizer, epochs)` | `LRScheduler | None` |

Plus three files directly under `<output_dir>`:

- `run_config.yaml` — default training knobs consumed by the engine entry.
- `run.sh` — human-only launcher (the workflow never invokes it).

### Self-containment (hard requirement, enforced by the engine loader)

A leaf must not import sibling files or the user's project. Allowed top-level
imports are limited to the whitelist
`{torch, math, numpy, typing, itertools, functools, collections, dataclasses, random}`.
Relative imports (`from . import …`) are forbidden. Constants / helpers used by
a leaf must live in the same file.

The engine loader (`kd/_leaves.py`) AST-validates each leaf **before** exec'ing
the body: function name + required positional args must match the contract
exactly (defaults are additive — you may add optional kwargs but cannot drop
or rename a required param).

### Kind direction

`eval_metric` returns `(value, kind)`. The kind fixes the metric direction:

- **higher is better (max)**: `snr`, `acc`
- **lower is better (min)**: `mse`, `nmse`, `ber`, `db`

The kind's direction group must match `inputs.accuracy_baseline_kind`'s
direction group, else the verify stage fails loud (D2 hard check).

## Required Inputs

- `<output_dir>` — the per-run artifacts dir (`$ORCA_ARTIFACTS_DIR`). All
  generated artifacts are written under `<output_dir>/`. The four leaves go
  into `<output_dir>/user/`.
- `<user_project_root>` — root of the user's PyTorch project (contains
  `train.py`).
- `<user_train_script>` — absolute path to the user's `train.py`.
- `<teacher_model_path>` — absolute path to a KD-NAS variant `.py` exposing
  `build_model` + `DUMMY_INPUT` (+ optional `feature_hook_names()`). Used only
  for I/O shape reference + smoke.
- `<kd_scripts_dir>` — absolute path to `workflows/agents/_kd_scripts/`. The
  fixed engine entry lives at `<kd_scripts_dir>/train_pipeline.py`.
- `<baseline_accuracy>` / `<baseline_accuracy_kind>` — from workflow inputs,
  written into `run_config.yaml`.

## Working Directory and Path Conventions

- Run `cd <output_dir>` once before executing commands. The working directory
  persists across subsequent commands.
- Use `pathlib.Path` for all path construction.
- The engine resolves `<output_dir>` via the `--artifacts_dir` CLI flag (not
  `cwd`); the leaves must be at `<output_dir>/user/` regardless of `cwd`.

## Lazy Loading

Do not read all reference files upfront. Only read the materials a specific
step requires when you begin that step.

## Workflow

### Step 1: Load Context

1. **Read the user's `train.py`**. Identify (by semantics, not by name):
   - The task loss — the `(output, target) -> scalar` function.
   - Data loading — `build_dataloader()` or the loader construction inside
     the training loop. Note whether it is re-iterable.
   - Optimizer / scheduler when present (they must be ported).
2. **Discover and read the user's eval script** under `<user_project_root>`
   (glob `test_*.py` / `eval*.py` / `evaluate*.py` / `test.py`, or an eval/metric
   fn inside `train.py`). Extract its metric formula + eval data loading. No
   eval script found → fail loud.
3. **Probe the teacher / student model contracts** (do not execute them):
   - Confirm `build_model` exists and is callable.
   - Read `DUMMY_INPUT` shape (the user's real I/O shape — never hardcode a
     fallback).
   - Read `feature_hook_names()` when present (decides whether KD uses ofd/fitnets).
4. **Read the four leaf skeletons** under
   `<skill_dir>/references/templates/leaves/{loss,data,eval,optim}.py.skel`.
   Each skeleton raises `NotImplementedError` with the contract signature.
5. **Read the KD library surface** (read-only) under `<kd_scripts_dir>/kd/`:
   `compose.py` (`build_kd_loss`), `wrapper.py` (`KDStudentWrapper`,
   `TeacherCache`), `ema.py` (`MeanTeacherEMA`). These are referenced by the
   engine, not by the leaves.

### Step 2: AST-detect unsupported training regimes (D8)

Before porting, scan the user's `train.py` for tokens that the KD-NAS engine
does **not** support:

- GAN: `Discriminator`, `adversarial`, `gan`, `generator_loss`,
  `discriminator_loss`.
- RL: `policy_gradient`, `reinforce`, `actor_critic`, `ppo`, `reward_model`.
- DDP: `torch.nn.parallel.DistributedDataParallel`, `torchrun`,
  `torch.distributed`, `local_rank`, `world_size`.

If any token is found, fail loud with a stderr message that names the token
and the file:line. The user may pass `--force-template` to override (declares
the hit a false positive; the engine will then likely produce a silently-wrong
model — the user owns that risk). Without the override, emit no JSON and exit
non-zero.

### Step 3: Generate the four leaves + run_config.yaml + run.sh

Read `<skill_dir>/references/workflows/train_pipeline_script_generation.md`
for the verbatim-port rules. Generation steps:

1. **Instantiate the four skeletons** to `<output_dir>/user/{loss,data,eval,optim}.py`.
2. **Port the user's loss** into `loss.py::compute_loss` verbatim — same ops,
   same reduction, same shape assumptions. Copy its module-level dependency
   closure (constants / helpers) into the same file. No `from <user_pkg>`.
3. **Port the user's dataloader** into `data.py::build_dataloader`. Ensure the
   returned object is re-iterable; one-shot generators must be wrapped in a
   re-iterable adapter.
4. **Port the user's eval metric** into `eval.py::eval_metric`. Same formula,
   same normalization, same data source. Return `(value, kind)` with kind in
   the allowed set.
5. **Port the user's optimizer / scheduler** into `optim.py`. Return `None`
   when the user defines none — the engine falls back to `Adam` and no
   scheduler. Never invent hyperparameters the user didn't supply.
6. **Write `run_config.yaml`** at `<output_dir>/run_config.yaml`. The engine
   reads `epochs / lr / batch_size / eval_every / early_stop_patience /
   accuracy_baseline / accuracy_baseline_kind / build_cfg` (and, for distill
   mode, `kd_config` — written by the distill agent each round, not here).
   Mode is not written to yaml (driven solely by `--mode`).
7. **Write `run.sh`** at `<output_dir>/run.sh` — a human-only launcher that
   calls `python3 <kd_scripts_dir>/train_pipeline.py --config run_config.yaml
   --artifacts_dir <output_dir> --mode ${MODE:-teacher}`. The workflow never
   invokes this file.

### Step 4: Validate

Run the four validation layers described in the workflow's Validation section,
in order: (1) per-leaf static + AST self-containment + AST signature, (2) engine
smoke against the fixed engine entry with a synthetic teacher + ckpt, (3)
`fidelity_check.py` per-leaf numeric equivalence + AST + kind direction hard
check, (4) `workflow-verifier` subagent over the four leaves in parallel.

### Step 5: Extract teacher defaults

Extract the user's default `lr` / `epochs` from `<user_project_root>/train.py`.
Extraction failure → fail loud.

## Verifier Subagent Prompt Template

When invoking the `workflow-verifier` subagent in Step 4, use this framework:

```
You are verifying the four KD-NAS training leaves produced by kd-train-script.

Workflow doc (read-only contract):
  <skill_dir>/references/workflows/train_pipeline_script_generation.md

Checklists (you consume these, read-only):
  <skill_dir>/references/workflow-checklists/train_pipeline_script_generation/01_training.md
  <skill_dir>/references/workflow-checklists/train_pipeline_script_generation/02_cli.md

Artifacts (you may modify these):
  <output_dir>/user/loss.py
  <output_dir>/user/data.py
  <output_dir>/user/eval.py
  <output_dir>/user/optim.py
  <output_dir>/run_config.yaml
  <output_dir>/run.sh

Cross-references (read-only):
  User's original train.py: <user_project_root>/train.py
  User's original eval script: <discovered eval script path>
  KD-NAS contracts: workflows/agents/_kd_scripts/CONTRACTS.md §6
  Leaf skeletons: <skill_dir>/references/templates/leaves/*.py.skel

Verify (in priority order):
0. Each leaf's required callable exists with the contract signature
   (function name + required positional args). Defaults are additive.
1. Each leaf is self-contained: only whitelisted top-level imports, no sibling
   / relative imports. Run `python -c "import ast; ..."` or equivalent.
2. `loss.py::compute_loss` body == user's loss body (same ops / reduction).
3. `data.py::build_dataloader` returns a re-iterable loader yielding the
   DUMMY_INPUT batch shape.
4. `eval.py::eval_metric` body == user's eval body (same formula). kind ∈
   {nmse,mse,ber,db,snr,acc}; kind direction matches
   `inputs.accuracy_baseline_kind` direction.
5. `optim.py` ported the user's optimizer / scheduler (same class + kwargs)
   or returns None when the user has none.
6. `run_config.yaml` parses and carries the user-default lr/epochs plus the
   inputs.accuracy_baseline(_kind).
7. `fidelity_check.py` printed FIDELITY: PASS for the four leaves.

For each item, report PASS / FIXED / UNRESOLVED. End with:
  VERDICT: all-pass | unresolved
```

## Output

After Step 4 completes (verifier `all-pass`), emit a structured summary on
stdout:

```
OUTPUT_DIR: <output_dir>
LEAVES_DIR: <output_dir>/user
RUN_CONFIG_PATH: <output_dir>/run_config.yaml
RUN_SH_PATH: <output_dir>/run.sh
TRAIN_PIPELINE_PATH: <kd_scripts_dir>/train_pipeline.py
LEAVES: loss.py,data.py,eval.py,optim.py
TEACHER_DEFAULT_LR: <float>
TEACHER_DEFAULT_EPOCHS: <int>
FIDELITY_CHECK: PASS
VERIFIER_VERDICT: all-pass
```

## Guidelines

- Keep generated Python variable names, function names, classes, string
  literals, comments, and docstrings in English.
- Leaves must be runnable standalone (no top-level `import orca`).
- Never invent eval metric formulas the user's eval script doesn't compute.
- Fail loud on every contract violation; never silently substitute.

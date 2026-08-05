---
name: kd-train-script
description: Generate the four KD-NAS training leaves (loss/data/eval/optim) plus run_config.yaml and run.sh from the user's train.py. The leaves are consumed by the fixed KDTrainer engine at _kd_scripts/train_pipeline.py.
---

# KD-NAS Train Leaves Generator

Use this skill to generate the four project-specific training **leaves** that
the fixed `KDTrainer` engine (`workflows/agents/_kd_scripts/train_pipeline.py`)
loads at runtime. The leaves plus a `run_config.yaml` and a human-use `run.sh`
are the only artifacts produced — there is no monolithic generated script.

## Guiding Principle: Faithful Mover, Not Designer

You are a **faithful mover** of the user's training/eval logic into the four
leaves, **not a designer**. Preserve every behavior: formulas, constants,
signs, control flow, randomness semantics. **Do not simplify, approximate, or
substitute look-alike utilities.** Replacing the user's real dataloader with
`torch.rand(...)` because "the leaf must be self-contained" is a forbidden
fabrication — it decouples pixels from labels and silently produces a model
that cannot learn.

The four leaves exist so the fixed engine can drive training while the user's
real loss / data / eval / optim live in self-contained files. Your job is to
**port** that logic verbatim (inline its dependency closure — constants,
helpers, transforms), never to **re-design** it. When you cannot port
faithfully (the user's dataloader depends on a user-project module you cannot
inline, or on data that is genuinely unavailable), report it as **Unresolved**
and emit an ask-user sentinel — never fabricate a look-alike.

## Leaf contract (authoritative — `workflows/agents/_kd_scripts/CONTRACTS.md`)

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

A leaf must not import sibling files or the user's project. **"Self-contained"
forbids user-project modules — NOT the standard scientific stack.** Allowed
top-level imports cover the pip scientific stack + Python stdlib:
`{torch, torchvision, torchaudio, numpy, scipy, sklearn, PIL, math, os, sys,
json, pathlib, typing, itertools, functools, collections, dataclasses, random,
io, abc, copy, re, warnings, time}`. `from torchvision.datasets import
<RealDataset>` is legitimate and expected — port the user's real
torchvision/PIL/numpy dataloader verbatim. Relative imports (`from . import …`)
and any non-whitelisted absolute import (e.g. `from user_pkg import …`) are
forbidden. Constants / helpers used by a leaf must live in the same file.

The engine loader (`kd/_leaves.py`) AST-validates each leaf **before** exec'ing
the body: function name + required positional args must match the contract
exactly (defaults are additive — you may add optional kwargs but cannot drop
or rename a required param).

### Anti-fabrication (hard requirement, enforced by `fidelity_check.py`)

`data.py` and `eval.py` must load the user's **real** dataset.  Using
`torch.rand` / `torch.randn` / `torch.randint` / `torch.randperm` or
`numpy.random.*` as the **source of pixels or labels** is fabrication — it
decouples inputs from targets and silently produces a model that cannot learn.

- **Forbidden in `data.py` / `eval.py`**: any call to the random-tensor
  factories above as the data/label source.  `fidelity_check.py` rejects such
  leaves with `LEAF_FABRICATION_OK: false`.
- **Allowed**: randomness for parameter init (`torch.nn.init.*`), shuffling
  (`torch.randperm` for batch order inside a DataLoader that yields real
  samples), and augmentation applied on top of ground-truth data loaded from
  disk.
- **Unportable user data**: if the user's dataloader depends on a
  **user-project module** or on data that is genuinely unavailable (not a pip
  package, not on disk), **fail loud** and emit an ask-user sentinel
  describing the missing dependency / data path.  Never substitute random
  tensors for the real loader — fabrication is forbidden.

### Kind direction

`eval_metric` returns `(value, kind)`. The kind fixes the metric direction:

- **higher is better (max)**: `snr`, `acc`
- **lower is better (min)**: `mse`, `nmse`, `ber`, `db`

The kind's direction group must match `inputs.accuracy_baseline_kind`'s
direction group, else the verify stage fails loud.

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

### Step 2: AST-detect unsupported training regimes

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
3. **Port the user's dataloader** into `data.py::build_dataloader`. Port the
   user's real loader verbatim — including its `torchvision` / `PIL` / `numpy`
   imports and the real dataset path/transform.  Ensure the returned object is
   re-iterable; one-shot generators must be wrapped in a re-iterable adapter.
   **Never substitute `torch.rand` / `torch.randint` / `numpy.random.*` for the
   user's dataset** — that is fabrication (see Anti-fabrication above).  If the
   user's loader depends on a user-project module or genuinely unavailable
   data, fail loud + emit an ask-user sentinel.
4. **Port the user's eval metric** into `eval.py::eval_metric`. Same formula,
   same normalization, **same real data source** (port the user's eval loader,
   do not synthesise eval inputs). Return `(value, kind)` with kind in the
   allowed set.
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

Run the validation layers in this exact order:

1. **L1** — per-leaf `py_compile` + AST self-containment + AST signature.
2. **L2** — engine smoke against the fixed engine entry with a synthetic
   teacher + ckpt.
3. **L3** — `fidelity_check.py` per-leaf numeric equivalence + AST + kind
   direction hard check. L3 FAIL → fail loud and stop; do **not** enter
   L4-semantic (avoid double-reporting the same deviation with the
   deterministic layer).
4. **L4-semantic** — bounded convergence loop spawning the
   `project-fidelity-verifier-kd` subagent. See
   `## L4-semantic — project-fidelity-verifier spawn` below for the spawn
   templates (first-run + resume) and the deterministic control flow.
5. **L4-mechanical** — one-shot `workflow-verifier` spawn over the four
   leaves in parallel. Pass the Accepted IDs from L4-semantic into that
   spawn prompt so the mechanical layer does not re-audit IDs that the
   semantic layer already accepted.

### Step 5: Extract teacher defaults

Extract the user's default `lr` / `epochs` from `<user_project_root>/train.py`.
Extraction failure → fail loud.

## L4-semantic — project-fidelity-verifier spawn

The semantic fidelity audit runs as a bounded convergence loop. You drive
the loop with deterministic control flow (not LLM self-direction): spawn,
parse, fix, re-run, repeat — at most `MAX_TURNS = 3` turns.

**Read the subagent contract first**: open
`{{ subagents_root }}/project-fidelity-verifier-kd.md` and read it. That file
is the authoritative contract for the subagent — its scope, audit procedure,
deviation judgment, STATUS contract, and red lines. Embed its body into the
spawn context; do not paraphrase it.

**State you carry across turns**:
- `id_stash` — the set of all IDs the verifier has ever reported in this
  audit instance (Static Fidelity / Accepted / Unresolved combined). The
  resume ID-range defense checks every resume-reported ID against this set.
- `reaffirm_count` — `id -> consecutive open count`. When an ID hits 2,
  stop the loop and fail loud with an ask-user sentinel.
- `fixed_ids` — the IDs whose latest STATUS is `closed`; these go into the
  next turn's `Fixed:` line.

**Deterministic loop** (pseudo-code; you execute it step by step):

```
turn = 0
fixed_ids = []
reaffirm_count = {}
loop:
    turn += 1
    if turn == 1:
        spawn fidelity-verifier with the first-run template
    else:
        spawn fidelity-verifier with the resume template (Fixed: <fixed_ids>)
    if spawn crashed (rc != 0, sentinel missing, output has no all-pass
                       line and no Static Fidelity section):
        fail loud, do not retry, stderr the raw output,
        emit ask-user sentinel (protocol-layer crash, not transient)
    verify every ID in the resume report is a subset of id_stash
        (hallucination defense); on violation, fail loud
    parse STATUS lines -> closed_ids, open_ids, accepted_ids
    id_stash.update(every ID in this turn's report)
        (so turn-1 findings are stash members before turn-2's subset check)
    if report == all-pass: break (proceed to L4-mechanical)
    for id in open_ids:
        reaffirm_count[id] += 1 else reaffirm_count[id] = 1
    if any reaffirm_count[id] >= 2:
        fail loud + ask-user sentinel
        ("ID <id> repeatedly reaffirmed; agent cannot resolve it")
    if turn >= MAX_TURNS (=3):
        fail loud, stderr the unclosed IDs + last findings,
        exit non-zero, do not emit JSON
    if any Unresolved items are present:
        do not guess fixes; fail loud + ask-user sentinel
    apply fixes to leaves only (never touch engine / KD library)
    re-run L1 py_compile + L3 fidelity_check
    if L1 or L3 FAIL:
        fail loud + ask-user sentinel
        ("fix broke L1/L3 — roll back or hand off")
        exit non-zero
    fixed_ids = closed_ids (from this turn's STATUS lines)
        # overwrite, not accumulate: once an ID is closed the verifier
        # does not re-audit it next turn unless the caller edits its code
        # again, in which case the verifier would re-flag it with a new ID
```

**Parse the resume report mechanically, never by prose inference**: for
every re-checked ID the verifier's report block opens with a `STATUS:
closed | open | accepted` line. Read those lines directly; do not infer
status from surrounding prose.

**You may only fix what the verifier flagged as caller-actionable semantic
findings (open IDs)**. Unresolved items mean the verifier lacks the basis
to judge — never fabricate a fix for them; surface them via the ask-user
sentinel instead.

### First-run spawn template

```
You are auditing the four KD-NAS training leaves for fidelity to the
user's original training / evaluation logic.

Read your contract (this is your authoritative instruction):
  {{ subagents_root }}/project-fidelity-verifier-kd.md

Leaves (read-only for you; the caller fixes them between turns):
  <leaves_dir>/loss.py
  <leaves_dir>/data.py
  <leaves_dir>/eval.py
  <leaves_dir>/optim.py

User original code (read-only):
  user train.py: <user_train_script>
  user eval script: <discovered eval script path>
  user project root: <user_project_root>

Source -> generated mapping:
  loss.py::compute_loss       <-> user's task loss function
  data.py::build_dataloader   <-> user's training dataloader
  eval.py::eval_metric        <-> user's evaluation metric function
  optim.py::build_optimizer   <-> user's optimizer constructor
  optim.py::build_scheduler   <-> user's scheduler constructor

Intended behavior (fixed):
  - Each leaf must port the user's loss / dataloader / eval metric /
    optimizer / scheduler verbatim — formulas, constants, signs, control
    flow, randomness semantics.
  - Designed-in non-deviations: the distillation loss combination lives
    in the engine (kd.compose.build_kd_loss), not in any leaf; no leaf
    references kd.*. eval_metric returns (value, kind); the kind direction
    is enforced by an earlier deterministic check, so do not re-test the
    kind direction — audit only the metric formula body, the eval data
    source, and transform content.

This is the first audit pass: perform the full static comparison and any
differential probes you can. Return the standard report (Coverage /
Static Fidelity / Runtime Fidelity / Accepted Deviations / Unresolved /
all-pass) per your contract.
```

### Resume spawn template

```
You are resuming the KD-NAS training-leaves fidelity audit. Same contract:
  {{ subagents_root }}/project-fidelity-verifier-kd.md

Fixed: <fixed_ids>     # caller changed code for these IDs

Leaves (read-only):
  <leaves_dir>/loss.py
  <leaves_dir>/data.py
  <leaves_dir>/eval.py
  <leaves_dir>/optim.py

For each re-checked ID, open its report block with the STATUS line
(`STATUS: closed | open | accepted`) per your contract. Return the
standard report for the re-checked items only — do not repeat the full
audit.
```

## Verifier Subagent Prompt Template

When invoking the `workflow-verifier` subagent in Step 4 (L4-mechanical), use
this framework. This is the mechanical layer that runs once after the
L4-semantic loop has reached `all-pass`; it covers file/import/config keys
and may auto-fix mechanical issues. If L4-semantic produced an Accepted
Deviations list, pass those IDs in explicitly so the mechanical layer does
not re-audit them (the mechanical layer does not recognize the Accepted
concept and would otherwise re-report them as `unresolved`).

```
You are verifying the four KD-NAS training leaves produced by kd-train-script.

Accepted IDs (do not re-audit; the semantic layer already accepted these):
  <accepted_ids>     # e.g. "[2], [5]" — empty if L4-semantic had none

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
  KD-NAS contracts: workflows/agents/_kd_scripts/CONTRACTS.md
  Leaf skeletons: <skill_dir>/references/templates/leaves/*.py.skel

Verify (in priority order):
0. Each leaf's required callable exists with the contract signature
   (function name + required positional args). Defaults are additive.
1. Each leaf is self-contained: only whitelisted top-level imports (standard
   scientific stack + stdlib), no sibling / relative imports, no user-project
   modules. Run `python -c "import ast; ..."` or equivalent.
2. `loss.py::compute_loss` body == user's loss body (same ops / reduction).
3. `data.py::build_dataloader` ports the user's **real** loader (torchvision /
   PIL / numpy) and returns a re-iterable loader yielding the DUMMY_INPUT
   batch shape. **No `torch.rand` / `torch.randn` / `torch.randint` /
   `torch.randperm` / `numpy.random.*` as data or label source** — that is
   fabrication. If the user's data is genuinely unavailable, the leaf should
   fail loud at runtime (not synthesise).
4. `eval.py::eval_metric` body == user's eval body (same formula) and uses the
   user's real eval data source (no random fabrication). kind ∈
   {nmse,mse,ber,db,snr,acc}; kind direction matches
   `inputs.accuracy_baseline_kind` direction.
5. `optim.py` ported the user's optimizer / scheduler (same class + kwargs)
   or returns None when the user has none.
6. `run_config.yaml` parses and carries the user-default lr/epochs plus the
   inputs.accuracy_baseline(_kind).
7. `fidelity_check.py` printed FIDELITY: PASS for the four leaves
   (incl. `LEAF_FABRICATION_OK: true`).

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

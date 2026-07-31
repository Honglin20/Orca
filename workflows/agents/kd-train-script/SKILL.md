---
name: kd-train-script
description: Generate the unified KD-NAS training script (train_pipeline.py) supporting teacher + distill modes from the user's train.py. Invoke after the user's train.py and teacher/student model contracts are available.
---

# KD-NAS Train Pipeline Script Generator

Use this skill to generate the project-specific KD-NAS training entry point
`train_pipeline.py`. The script supports two modes behind one CLI
(`--mode teacher` / `--mode distill`), is self-contained (user logic copied
in, never imported), and loads models by path via `importlib.util`.

This is the KD-NAS analogue of the NAS `supernet-train-script` skill, with
sandwich sampling / DDP / torchrun removed and KD provided by the existing
`kd.compose` / `kd.wrapper` / `kd.ema` library instead of
`nas_agent.train.distillation`.

Skill resource paths:

- `<skill_dir>`: the directory containing this `SKILL.md`. All `references/`
  paths are relative to `<skill_dir>`.
- Do **not** read files under `<skill_dir>/references/workflow-checklists/`.
  Those are exclusively consumed by the `workflow-verifier` subagent.

## Lazy Loading

Do **not** read all reference files upfront. Only read the materials a specific
step requires when you begin that step.

## Required Inputs

- `<output_dir>`: the directory where `train_pipeline.py` will be generated.
  Created if absent.
- `<user_project_root>`: the root directory of the user-provided PyTorch
  project containing `train.py` (the user's task loss + dataloader builder).
- `<teacher_model_path>`: absolute path to the teacher model `.py` (exposes
  `build_model` + `DUMMY_INPUT` + `feature_hook_names()` per
  `workflows/agents/_kd_scripts/CONTRACTS.md` §1). For KD-NAS this is
  typically `workflows/agents/_kd_scripts/teacher_model.py`.
- `<student_model_path>` (distill mode only): absolute path to a KD-NAS
  student variant `.py` under `knowledge_base/families/receiver/`.
- `<kd_scripts_dir>`: absolute path to `workflows/agents/_kd_scripts/`. The
  generated script needs this on `sys.path` (via `ORCA_KD_SCRIPTS_DIR`) to
  import the `kd/` library.

## Working Directory and Path Conventions

- `<output_dir>` **(working directory)**: All generated artifacts are written
  under `<output_dir>`. Run `cd <output_dir>` once before executing commands;
  the working directory persists across subsequent commands.
- **Path handling**: use `pathlib.Path` for all path construction (no
  `os.path.join` with string concatenation). Example:
  ```python
  from pathlib import Path
  out_ckpt = Path(args.out_ckpt)
  out_ckpt.parent.mkdir(parents=True, exist_ok=True)
  ```
- All paths must be CLI-overridable. No hardcoded dataset / model / ckpt
  literals.

## Workflow

### Step 1: Load Context

1. **Read the user's `train.py` under `<user_project_root>`**. Focus on:
   - `compute_loss(s_out, y)` body — the task loss formula, reduction, shape
     assumptions.
   - `build_dataloader()` signature + body — dataset, transforms, collate,
     batch structure. Note whether it's a re-iterable class or a one-shot
     generator.
   - Optimizer / scheduler if present.
   - Any domain-specific training patterns worth porting (e.g. gradient
     accumulation, custom regularizers).
2. **Probe the teacher and student model contracts** (do not execute them):
   - Confirm `build_model` exists and is callable.
   - Read `DUMMY_INPUT` shape (the user's real I/O shape — never hardcode a
     fallback per CONTRACTS §6 BLK-4).
   - Read `feature_hook_names()` (required for feature-based KD terms).
3. **Read the KD library surface** to know what's available (read-only):
   - `workflows/agents/_kd_scripts/kd/compose.py` — `build_kd_loss` factory +
     `KDComposite` (calls user_loss internally + adds KD terms).
   - `workflows/agents/_kd_scripts/kd/wrapper.py` — `KDStudentWrapper` +
     `TeacherCache.load`.
   - `workflows/agents/_kd_scripts/kd/ema.py` — `MeanTeacherEMA` (optional).
4. **Read the reference template** at
   `<skill_dir>/references/templates/train_pipeline.py`. This is your starting
   point — a complete, smoke-testable gold example. You will **copy this file
   to `<output_dir>/train_pipeline.py` and specialise it**, not write from
   scratch.

### Step 2: Generate `train_pipeline.py`

Read `<skill_dir>/references/workflows/train_pipeline_script_generation.md`.
Follow it to specialise the reference template into the project-specific
`train_pipeline.py`.

Generation steps:

1. **Copy the reference template** verbatim to `<output_dir>/train_pipeline.py`.
2. **Port the user's task loss**. Set
   `USER_TRAIN_MODULE = "/abs/path/to/train.py"` (or a module name reachable
   on `sys.path`) and `USER_LOSS_FN = "compute_loss"` so the existing
   path-loader in `_load_user_train` resolves it. **Single strategy
   (path/module injection)** — keeps the template simple and avoids contract
   drift between an "inlined" sentinel and a separate dispatch branch. The
   user's dataloader / optimizer / scheduler logic must still be **copied
   into** `train_pipeline.py` or a sibling helper — only the loss-function
   *reference* is resolved by path injection (one-shot init load).
3. **Port the user's dataloader** into a sibling helper file (e.g.
   `<output_dir>/data_utils.py`) when non-trivial, or inline when short.
   Ensure the loader is re-iterable.
4. **Port the user's optimizer / scheduler** when present. Otherwise leave
   the Adam + no-scheduler fallback, annotated with `# TODO(kd-train-script):`.
5. **Pick KD terms** based on the user's task semantics (see workflow §7):
   - Default: task loss only (`{"kd_losses": [], "weights": {}}`).
   - Output MSE for regression tasks where teacher/student output shapes match
     exactly.
   - OFD / FitNets / RKD only when `feature_hook_names()` aligns between
     teacher and student; raise loudly on mismatch, never silently drop.
6. **Update the default `--kd_config`** in argparse to the chosen KD recipe.
7. **Verify CLI consistency** by running
   `python <output_dir>/train_pipeline.py --help` and confirming every flag
   in the workflow §1 stable base CLI is listed.

### Step 3: Validate

Run the three validation layers described in the workflow's Validation
section, in order:

1. **Layer 1 — Static checks**: `py_compile`, `--help`, CLI consistency.
2. **Layer 2 — Functional smoke tests**: run both teacher and distill modes
   with a tiny budget on CPU (1 epoch, batch_size 2). Verify stdout keys +
   ckpt schemas.
3. **Layer 3 — Verifier subagent**: invoke `workflow-verifier` with the
   workflow doc + checklists + artifacts + user's original `train.py` +
   CONTRACTS.md as cross-references.

Handle the verifier response:

- `all-pass` with no **Fixed** section → done.
- `all-pass` with a **Fixed** section → re-run Layer 2 smoke tests.
- `unresolved` → apply each suggested fix, re-run Layer 1 + Layer 2.

## Verifier Subagent Prompt Template

When invoking the `workflow-verifier` subagent in Step 3, use this prompt
framework (adapt paths to the actual run):

```
You are verifying a generated KD-NAS training script.

Workflow doc (read-only contract):
  <skill_dir>/references/workflows/train_pipeline_script_generation.md

Checklists (you consume these, read-only):
  <skill_dir>/references/workflow-checklists/train_pipeline_script_generation/01_training.md
  <skill_dir>/references/workflow-checklists/train_pipeline_script_generation/02_cli.md

Artifacts (you may modify these):
  <output_dir>/train_pipeline.py
  <output_dir>/data_utils.py   (if present)

Cross-references (read-only):
  User's original train.py: <user_project_root>/train.py
  KD-NAS contracts: workflows/agents/_kd_scripts/CONTRACTS.md
  Reference template: <skill_dir>/references/templates/train_pipeline.py

Verify:
1. The generated train_pipeline.py faithfully ports the user's compute_loss
   + build_dataloader logic (no behaviour drift, no silent substitutions).
2. CLI matches the stable base CLI in workflow §1 (every flag present,
   --mode required with choices teacher/distill).
3. Checkpoint schemas match workflow §5 (teacher) and §6 (distill).
4. No distributed-launch / architecture-sampling / nas_agent.train.distillation
   residue (checklist 01 item 2 + item 9).
5. Distill optimizer includes kd_loss.kd_parameters() (checklist 01 item 8).
6. KD library used: kd.compose.build_kd_loss + kd.wrapper.* + kd.ema.* (not
   nas_agent.train.distillation).

For each item, report: PASS / FIXED (with what you changed) / UNRESOLVED
(with a suggested fix). End with a single line: `VERDICT: all-pass` or
`VERDICT: unresolved`.
```

## Output

After Step 3 completes (verifier `all-pass`), emit a structured summary on
stdout for the orchestrating agent:

```
OUTPUT_DIR: <output_dir>
GENERATED_SCRIPT: train_pipeline.py
HELPER_FILES: <list, e.g. data_utils.py or none>
MODES_SUPPORTED: teacher,distill
KD_TERMS_ENABLED: <list, e.g. mse or empty>
TEACHER_MODE_SMOKED: Yes
DISTILL_MODE_SMOKED: Yes|Skipped (no teacher_cache available)
VERIFIER_VERDICT: all-pass
```

## Guidelines

- Preserve all generated artifacts unless the user explicitly asks for cleanup.
- Keep generated Python variable names, function names, classes, string
  literals, comments, and docstrings in English.
- The generated script must be runnable standalone (outside Orca) — no
  top-level `import orca` (guarded lazy import only, for live chart push).
- Prefer conservative KD defaults (task-loss only) when task semantics are
  ambiguous; raise loudly on hook mismatch rather than silently dropping a
  feature-KD term.

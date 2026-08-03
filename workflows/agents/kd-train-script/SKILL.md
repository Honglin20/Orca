---
name: kd-train-script
description: Generate the unified KD-NAS training script (train_pipeline.py) supporting teacher + distill + eval modes from the user's train.py. Invoke after the user's train.py and teacher/student model contracts are available.
---

# KD-NAS Train Pipeline Script Generator

Use this skill to generate the project-specific KD-NAS training entry point
`train_pipeline.py`. The script supports three modes behind one CLI
(`--mode teacher` / `--mode distill` / `--mode eval`; eval is read-only — it
loads a student ckpt and runs the user's eval metric discovered from the user
repo), is self-contained (user logic ported in verbatim, never imported), and
loads models by path via `importlib.util`.

**Generation strategy: specialise a skeleton, do not fill placeholders.** The
reference template is a **skeleton** — a non-runnable intermediate whose five
`user_*` slots raise `NotImplementedError`. You port the user's own code into
those slots verbatim (function body + its module-level dependency closure);
an unfilled slot fails loud at runtime. There is no placeholder fallback and
no `--user_*` runtime-injection flag.

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
   - The task loss function — **identified by semantics, not by name**: the
     `(output, target) -> scalar loss` function (often `compute_loss`, but do
     not require that name) — its formula, reduction, shape assumptions.
   - Data loading — `build_dataloader()` signature + body, or the
     dataset/loader construction inside the training loop. Note whether it's
     a re-iterable class or a one-shot generator.
   - Optimizer / scheduler **if present** (when present they must be ported).
   - Any domain-specific training patterns worth porting (e.g. gradient
     accumulation, custom regularizers).
1b. **Discover and read the user's eval script** under `<user_project_root>`
   (glob `test_*.py` / `eval*.py` / `evaluate*.py` / `test.py`, or an
   eval/metric fn inside `train.py`). Focus on its metric computation
   (NMSE/MSE/BER/SNR/acc) + eval data loading. This is ported into
   `user_eval_metric` (workflow §3.1). **No eval script found → fail loud.**
2. **Probe the teacher and student model contracts** (do not execute them):
   - Confirm `build_model` exists and is callable.
   - Read `DUMMY_INPUT` shape (the user's real I/O shape — never hardcode a
     fallback per CONTRACTS §6).
   - Read `feature_hook_names()` (required for feature-based KD terms).
3. **Read the KD library surface** to know what's available (read-only):
   - `workflows/agents/_kd_scripts/kd/compose.py` — `build_kd_loss` factory +
     `KDComposite` (calls user_loss internally + adds KD terms).
   - `workflows/agents/_kd_scripts/kd/wrapper.py` — `KDStudentWrapper` +
     `TeacherCache.load`.
   - `workflows/agents/_kd_scripts/kd/ema.py` — `MeanTeacherEMA` (optional).
4. **Read the reference skeleton template** at
   `<skill_dir>/references/templates/train_pipeline.py`. This is your starting
   point — a **skeleton, not a runnable gold example**: the five `user_*`
   slots raise `NotImplementedError` until you port the user's code. You
   **instantiate this skeleton** (copy it to `<output_dir>/train_pipeline.py`)
   and **specialise the slots** — never leave one unfilled, never write from
   scratch.

### Step 2: Generate `train_pipeline.py`

Read `<skill_dir>/references/workflows/train_pipeline_script_generation.md`.
Follow it to specialise the skeleton into the project-specific
`train_pipeline.py`.

Generation steps:

1. **Instantiate the skeleton**: copy the reference template verbatim to
   `<output_dir>/train_pipeline.py`.
2. **Port the user's task loss** into `user_compute_loss` **verbatim**: same
   function body, same ops, same reduction, same shape assumptions. The
   ported body plus its module-level dependency closure (constants / helper
   classes it references) are copied in — the result must be self-contained
   (no `from <user_pkg> import ...` residue; if porting still depends on user
   project symbols, fail loud — never load the user's module at runtime).
3. **Port the user's dataloader** into `user_build_dataloader` (sibling
   helper file `<output_dir>/data_utils.py` when non-trivial, or inline when
   short). Ensure the loader is re-iterable; wrap one-shot generators in a
   re-iterable adapter (or re-invoke the builder every epoch).
4. **Port the user's optimizer / scheduler** when present: `build_user_optimizer`
   returns the user's constructor verbatim (same class, same hyperparameters);
   `build_user_scheduler` same (step cadence must match the user's). When the
   user defines none, return `None` (the skeleton then uses the annotated
   `Adam` fallback — never invent hyperparameters).
5. **Port the user's eval metric** into `user_eval_metric` (workflow §3.1):
   metric formula + eval data loading, self-contained, returning
   `(value, kind)` with kind ∈ {nmse, mse, ber, snr, acc}. No dummy
   degradation — if the eval script is missing, fail loud.
   - **归一化由 agent 完成，不要求用户脚本打印特定 key**：实际项目的 eval 可能打
     `MSE:0.02` / `loss:...` / 表格 / 自定义指标名，甚至只打原始数值。agent 须把
     **指标计算公式**（不是 stdout 文本）移植进 `user_eval_metric`，由它返回 `(value, kind)`；
     train_pipeline `--mode eval` 恒打标准化的 `STUDENT_ACCURACY: {value}` +
     `STUDENT_ACCURACY_KIND: {kind}`（与用户原指标名/打印格式无关），下游
     （student/teacher 评估、全模型总表）一律读此标准 key。
   - kind 映射覆盖两个方向：误差型 `nmse/mse/ber`（越低越好）/ 得分型 `snr/acc`（越高越好）；
     自定义指标按方向归入最近的一类（如 PSNR→snr，任意 higher-better→acc）。
6. **Pick KD terms** based on the user's task semantics (see workflow §7):
   - **distill 模式必须含非空 KD 项**——`build_kd_loss` 对空 `kd_losses`（且未开 ema）
     fail loud 拒绝（纯 task loss 不是蒸馏）。默认 `{"kd_losses": ["mse"], "weights": {"mse": 1.0}}`，
     epoch 0 即生效（默认不加 warmup block）。
   - Output MSE (`mse`) for regression tasks where teacher/student output shapes match
     exactly — the safe default.
   - OFD / FitNets / RKD only when `feature_hook_names()` aligns between
     teacher and student; raise loudly on mismatch, never silently drop.
7. **Update the default `--kd_config`** in argparse to the chosen KD recipe
   （默认 `mse`；纯 task loss 属于 `--mode teacher`，不是 distill）.
8. **Verify CLI consistency** by running
   `python <output_dir>/train_pipeline.py --help` and confirming every flag
   in the workflow §1 stable base CLI is listed (**no `--user_*` flags**).

### Step 3: Validate

Run the four validation layers described in the workflow's Validation
section, in order:

1. **Layer 1 — Static + no-residue checks**: `py_compile`, `--help`, CLI
   consistency, and an **AST scan for zero placeholder residue**
   (`{{` / `_placeholder_*` / `USER_TRAIN_MODULE` / `_load_user_train` /
   `_load_user_eval` / `--user_*` flags must not appear).
2. **Layer 2 — Functional smoke tests** (tiny budget on CPU, **no override
   flags**): teacher mode must run (an unspecialised slot crashes with
   `NotImplementedError` = fail-loud gate); distill mode with a test
   teacher_cache built via `kd.wrapper.TeacherCache.build` on the (untrained)
   teacher state dict, or explicitly `Skipped` if unavailable — never a
   placeholder fallback; eval mode runs the real `user_eval_metric` on the
   teacher smoke ckpt. Verify stdout keys + ckpt schemas.
3. **Layer 3 — fidelity_check.py**: run
   `python <skill_dir>/scripts/fidelity_check.py --train_pipeline
   <output_dir>/train_pipeline.py --user_train <user_project_root>/train.py
   --user_eval <discovered eval script> --dummy_input <baseline DUMMY_INPUT>
   --model_path <teacher_model_path>` — must print `FIDELITY: PASS` (loss /
   loader / eval / optimizer / model-I/O numeric equivalence with the user's
   original code).
4. **Layer 4 — Verifier subagent (mandatory, never skip)**: invoke the
   `workflow-verifier` subagent with the workflow doc + checklists + artifacts
   + user's original `train.py` + CONTRACTS.md as cross-references. This is not
   optional — you **must** actually spawn and run the subagent (not narrate a
   pass). Feed it checklist items C21-C24 (zero residue / loss verbatim / eval
   verbatim / fidelity evidence), 7 (optimizer), 20 (shape reads DUMMY_INPUT),
   20b (teacher/student I/O == baseline) as priority checks.

Handle the verifier response:

- `all-pass` with no **Fixed** section → done.
- `all-pass` with a **Fixed** section → re-run Layer 2 smoke tests.
- `unresolved` → apply each suggested fix, re-run Layer 1 + Layer 2 + Layer 3.
  **Never emit the output JSON while any item is unresolved** (a skipped
  verifier is a failed generation).

### Step 4: Extract Teacher Defaults

Extract the user's default `lr` / `epochs` from `<user_project_root>/train.py`
(the `teacher_default_lr` / `teacher_default_epochs` contract values consumed
by `train_teacher`). Use the grep logic in the orchestrating agent's
`agent.md` Step 4; extraction failure → fail loud (a user teacher trained with
a non-user default lr may not converge).

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
  User's original eval script: <discovered eval script path>
  KD-NAS contracts: workflows/agents/_kd_scripts/CONTRACTS.md
  Reference skeleton template: <skill_dir>/references/templates/train_pipeline.py

Verify:
0. **All five fixed user interface slots are specialised** (regex
   `^def\s+(user_compute_loss|user_build_dataloader|user_eval_metric|build_user_optimizer|build_user_scheduler)\s*\(`
   over `train_pipeline.py` / `data_utils.py`) and **zero placeholder residue**:
   no `{{`, no `_placeholder_*`, no `USER_TRAIN_MODULE`, no `_load_user_train`
   / `_load_user_eval`, no `--user_*` CLI flags (checklist C21).
1. The generated train_pipeline.py faithfully ports the user's loss (C22:
   same ops, same reduction, same shape assumptions — silent substitution is
   FAIL), dataloader and eval metric (C23: same formula, same normalization,
   same data source) — no behaviour drift, no silent substitutions.
2. CLI matches the stable base CLI in workflow §1 (every flag present,
   --mode required with choices teacher/distill/eval; no `--user_*` flags).
3. Checkpoint schemas match workflow §5 (teacher) and §6 (distill).
4. No distributed-launch / architecture-sampling / nas_agent.train.distillation
   residue (checklist 01 item 2 + item 9).
5. Distill optimizer includes kd_loss.kd_parameters() (checklist 01 item 8).
6. KD library used: kd.compose.build_kd_loss + kd.wrapper.* + kd.ema.* (not
   nas_agent.train.distillation).
7. **Optimizer/scheduler faithfully ported** from the user's `train.py`
   (checklist item 7): grep the user's optimizer class + kwargs; any drift
   (e.g. user `AdamW` → generated `Adam`) is FAIL. The `Adam` fallback is
   allowed only if the user defines no optimizer.
8. **No hardcoded shape** (checklist item 20): every shape literal in
   `train_pipeline.py` / `data_utils.py` matches `DUMMY_INPUT["shape"]`; a
   hand-typed number (e.g. 32 where DUMMY_INPUT says 64) is FAIL.
9. **Teacher/student I/O == baseline DUMMY_INPUT** (checklist item 20b): both
   models forward `DUMMY_INPUT` and produce the baseline output shape; a smoke
   proxy that changes I/O shape is FAIL, not a workaround.
10. **Fidelity evidence** (checklist C24): fidelity_check.py printed
   `FIDELITY: PASS` with `FIDELITY_LEVEL: numeric` (or an AST degradation with
   a declared reason) for this artifact against the user's original code.

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
MODES_SUPPORTED: teacher,distill,eval
KD_TERMS_ENABLED: <list, e.g. mse or empty>
TEACHER_MODE_SMOKED: Yes
DISTILL_MODE_SMOKED: Yes|Skipped (no teacher_cache available)
FIDELITY_CHECK: PASS
VERIFIER_VERDICT: all-pass
```

## Guidelines

- Preserve all generated artifacts unless the user explicitly asks for cleanup.
- Keep generated Python variable names, function names, classes, string
  literals, comments, and docstrings in English.
- The generated script must be runnable standalone (outside Orca) — no
  top-level `import orca` (guarded lazy import only, for live chart push).
- KD is mandatory in distill mode (default `mse`); empty `kd_losses` is
  rejected fail loud. Prefer `mse` when task semantics are ambiguous; raise
  loudly on hook mismatch rather than silently dropping a feature-KD term.

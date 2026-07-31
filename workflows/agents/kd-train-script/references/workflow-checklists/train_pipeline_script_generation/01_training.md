# Checklist: Train Pipeline Script — Training Logic (KD-NAS)

Companion to: `workflows/train_pipeline_script_generation.md`

## How To Use

Each item below verifies a verifiable requirement from the companion workflow's
training-logic sections (§1–§9). Verify items in order. For items marked
`auto-fixable: yes`, fix the artifact directly. For `auto-fixable: no`, report
the issue for the caller.

**Definitions:**
- `<user_project_root>`: user's original PyTorch project root containing
  `train.py` (loss + dataloader builder).
- `<output_dir>`: directory where `train_pipeline.py` is generated.
- `<skill_dir>`: the `kd-train-script` agent resource directory (containing
  this checklist's ancestor `SKILL.md`).

## Items

### [CRITICAL] 1. Two Modes In One Script
**auto-fixable**: yes
**Section**: §1 CLI And Runtime Args, SKILL.md Workflow
**Check**: `train_pipeline.py` exposes `--mode {teacher,distill}` (required,
choices exactly `teacher` and `distill`). Both modes share the training
infrastructure (optimizer / scheduler / dataloader / task_loss); mode-specific
code diverges only at loss + ckpt schema.
**Verify**: grep `add_argument("--mode"` and confirm `choices=["teacher",
"distill"]`. Confirm the main entry dispatches to `run_teacher_mode` /
`run_distill_mode`.
**Anti-pattern**: Two separate scripts; missing `--mode` flag; mode duplicated
as two near-identical scripts instead of one with a mode switch.
**Fix**: Add `--mode` argparse entry and a dispatcher.

### [CRITICAL] 2. No Distributed / Architecture-Sampling Residue
**auto-fixable**: yes
**Section**: generation rules header (KD-NAS adaptation #1)
**Check**: The generated script contains none of these **code-level** tokens
(imports, names, attribute accesses, function calls — docstrings/comments
excluded from the check, only actual code is scanned):
`torch.distributed`, `DistributedDataParallel`, `setup_distributed`,
`set_sample_config`, `sandwich`, `sample_sandwich_arch_configs`,
`is_main_process`, `get_rank`, `nas_agent.train.distillation`,
`logits_kd_loss`, `soft_bce_kd_loss`, `cosine_kd_loss`.
The string `torchrun` must also not appear in any **shell invocation** or
`subprocess` call.
**Verify**: Parse the script with `ast` and walk all `Import` / `ImportFrom`
/ `Name` / `Attribute` nodes. Confirm none of the forbidden identifiers
appear. Docstring mentions (e.g. "no DDP / torchrun" in the module docstring)
are allowed; code usage is not.
**Anti-pattern**: Copying NAS `supernet-train-script` templates verbatim and
leaving distributed / sampling scaffolding in.
**Fix**: Delete the offending lines; KD-NAS is single-device.

### [CRITICAL] 3. Model Loaded By Path (importlib)
**auto-fixable**: yes
**Section**: §2 Model Construction
**Check**: Both teacher and student are loaded via
`importlib.util.spec_from_file_location` (by absolute path), not via package
import. The model file's directory is inserted into `sys.path` so shared
blocks (e.g. `from _model8_blocks import ...`) resolve. The build function is
called as `getattr(mod, build_fn)(**cfg)`.
**Verify**: grep for `spec_from_file_location` and confirm a single
`_load_model_by_path` helper used by both modes.
**Anti-pattern**: `from teacher_model import build_model` (couples to a
hard-coded package location); `sys.path.append(<repo_root>)` (couples to repo
layout).
**Fix**: Replace with `_load_model_by_path(args.model_path, args.build_fn, cfg)`.

### [CRITICAL] 4. Self-Contained — No User Project Imports
**auto-fixable**: no
**Section**: generation rules Source Evidence
**Check**: `train_pipeline.py` and any sibling helper file do not import
modules from `<user_project_root>`. Required user logic (loss, dataset,
dataloader, optimizer, scheduler) is copied into the generated artifacts.
**Verify**: Inspect all `import` / `from ... import` statements in
`train_pipeline.py` and helper files. Check for absolute project paths,
`sys.path.insert(0, <user_project_root>)` (only the optional `--project_root`
injection is permitted, for resolving the user's loss/loader builder module
by name), or `PYTHONPATH` assumptions.
**Anti-pattern**: `from user_project.datasets import ...`; helper files that
only work when launched from the user's project root.
**Fix**: Copy the relevant code into `train_pipeline.py` or a sibling helper.

### [MAJOR] 5. User Task Loss Ported Fidelity
**auto-fixable**: no
**Section**: §3 User Task Loss + Dataloader
**Check**: The user's `compute_loss(s_out, y)` is faithfully ported: same
formula, same reduction, same shape assumptions. The placeholder fallback is
only kept when the placeholder strings are unexpanded.
**Verify**: Compare the loss function body in `train_pipeline.py` against the
user's `train.py` `compute_loss`. Confirm same ops (e.g. `F.mse_loss`,
`F.l1_loss`, etc.), same reduction mode, same order of operations.
**Anti-pattern**: Silently swapping `mse_loss` for `l1_loss`; adding a
normalization factor the user didn't have; inverting argument order.
**Fix**: Replace the ported body with the user's verbatim.

### [MAJOR] 6. Dataloader Re-Iterable
**auto-fixable**: yes
**Section**: §3 User Task Loss + Dataloader
**Check**: The dataloader is re-iterable: each epoch's `iter(dl)` yields a
fresh stream. If the user's `build_dataloader` returns a one-shot generator,
it is wrapped in a re-iterable adapter or re-invoked per epoch.
**Verify**: Read the dataloader construction + training loop. Confirm either
(a) `build_dataloader()` returns a class with `__iter__` that re-yields, or
(b) the loop calls `build_dataloader()` inside the epoch loop.
**Anti-pattern**: A generator function whose `iter()` exhausts after epoch 0;
training stops at epoch 1 with "dataloader empty".
**Fix**: Wrap the generator in a class with `__iter__` that re-invokes the
factory.

### [MAJOR] 7. Optimizer / Scheduler — Port Or Explicit Fallback
**auto-fixable**: yes
**Section**: §4 Optimizer, Scheduler
**Check**: When the user's `train.py` defines an optimizer / scheduler, the
generated script uses the same class + hyperparameters. When absent, uses
`torch.optim.Adam(model.parameters(), lr=args.lr)` with no scheduler and a
`# TODO(kd-train-script):` comment marking the fallback.
**Verify**: Compare the optimizer constructor against the user's `train.py`.
If the user uses `AdamW` / `SGD`, the generated script must match. If the user
has a `CosineAnnealingLR` / `StepLR`, it must be ported with the same
milestones. The fallback must not introduce a scheduler the user didn't have.
**Anti-pattern**: Inventing a `CosineAnnealingLR` for the fallback; using
`AdamW` when the user's `train.py` uses `Adam`.
**Fix**: Replace with the user's optimizer/scheduler, or add the
`# TODO(kd-train-script):` fallback note.

### [CRITICAL] 8. Distill Optimizer Includes KD Adapter Parameters
**auto-fixable**: yes
**Section**: §4 Optimizer, Scheduler (distill mode)
**Check**: In distill mode, the optimizer parameter group includes both
`wrapper.parameters()` and `kd_loss.kd_parameters()`. The KD adapter
parameters must be registered before the optimizer is constructed, which
requires materialising one batch and calling `kd_loss.prepare(s_feats0,
t_feats0)` first.
**Verify**: Read the distill mode setup. Confirm:
1. One batch materialised (`x0, y0 = next(iter(dl))`).
2. `wrapper.eval()` + `with torch.no_grad(): teacher(x0); wrapper(x0)`.
3. `kd_loss.prepare(s_feats0, t_feats0)`.
4. `optimizer = torch.optim.Adam(list(wrapper.parameters()) +
   list(kd_loss.kd_parameters()), lr=args.lr)`.
**Anti-pattern**: Constructing the optimizer before `prepare` (OFD/FitNets
adapters have no parameters yet → never trained); omitting
`kd_loss.kd_parameters()` from the optimizer.
**Fix**: Re-order: materialise → prepare → construct optimizer.

### [CRITICAL] 9. KD Library — kd.compose / kd.wrapper / kd.ema
**auto-fixable**: yes
**Section**: §7 KD Loss Composition
**Check**: Distill mode uses `kd.compose.build_kd_loss`, `kd.wrapper.KDStudentWrapper`,
`kd.wrapper.TeacherCache.load`, and (optionally) `kd.ema.MeanTeacherEMA`. Does
**not** use `nas_agent.train.distillation` or any NAS distillation helper.
**Verify**: grep for `from kd.compose import build_kd_loss`,
`from kd.wrapper import KDStudentWrapper, TeacherCache`. Confirm zero
references to `nas_agent.train.distillation` or `logits_kd_loss` /
`soft_bce_kd_loss` / `cosine_kd_loss` (NAS helpers).
**Anti-pattern**: `from nas_agent.train.distillation import KDWeightScheduler`.
**Fix**: Replace with the KD-NAS library equivalents.

### [MAJOR] 10. KD Imports Lazy (Teacher Mode Independent)
**auto-fixable**: yes
**Section**: SKILL.md Workflow, generation rules
**Check**: The KD library is imported lazily inside `run_distill_mode` (or
guarded so teacher-mode runs do not require `kd/` on `sys.path`). Teacher-mode
smoke tests must run successfully without `ORCA_KD_SCRIPTS_DIR` pointing at
`_kd_scripts/`.
**Verify**: grep for `from kd.wrapper import` / `from kd.compose import` /
`from kd.ema import`; confirm they are inside a function body (not at module
top-level) or guarded by `try/except ImportError`.
**Anti-pattern**: Top-level `from kd.wrapper import KDStudentWrapper` —
teacher-mode runs crash if `_kd_scripts/` isn't on sys.path.
**Fix**: Move the imports into `run_distill_mode`'s body.

### [MAJOR] 11. Teacher Mode — Pure Task Loss Only
**auto-fixable**: yes
**Section**: §5 Teacher Mode Loop
**Check**: Teacher mode computes `loss = user_loss(out, y)` with no KD terms.
No reference to `teacher_cache`, `KDStudentWrapper`, `kd_loss`, `TeacherCache`
in the teacher code path.
**Verify**: Read `run_teacher_mode`. Confirm the loss line is just
`user_loss(teacher(x), y)` (no KD composite).
**Anti-pattern**: Conditioning KD on a teacher flag in teacher mode (KD-NAS
teacher training is supervised only — KD happens in distill mode against a
cached teacher).
**Fix**: Remove KD logic from the teacher path.

### [CRITICAL] 12. Checkpoint Schemas Match Contract
**auto-fixable**: yes
**Section**: §5 Teacher Mode Loop, §6 Distill Mode Loop
**Check**:
- Teacher ckpt dict has keys: `state_dict`, `build_cfg`, `variant_id`,
  `epochs`, `final_loss`, `mode` (mode == `"teacher"`).
- Student ckpt dict has keys: `student_state_dict`, `variant_id`,
  `student_cfg`, `kd_config`, `epochs`, `proxy_mse`, `mode` (mode ==
  `"distill"`).
**Verify**: Read the two `torch.save({...})` calls. Compare key sets.
**Anti-pattern**: Missing `mode` key (downstream cannot dispatch on mode);
using `state_dict` for the student (breaks `KDStudentWrapper` consumers that
expect `student_state_dict`).
**Fix**: Adjust the dict keys to match the contract.

### [CRITICAL] 13. Stdout Keys Present
**auto-fixable**: yes
**Section**: §5 Teacher Mode Loop, §6 Distill Mode Loop
**Check**:
- Teacher mode prints: `TEACHER_CKPT: <path>` + `TASK_LOSS_FINAL: <float>`.
- Distill mode prints: `STUDENT_CKPT: <path>` + `KD_LOSS_FINAL: <float>` +
  `KD_PROXY_MSE: <float>`.
**Verify**: grep for `print(f"TEACHER_CKPT:` / `print(f"STUDENT_CKPT:` /
`print(f"TASK_LOSS_FINAL:` / `print(f"KD_LOSS_FINAL:` / `print(f"KD_PROXY_MSE:`.
**Anti-pattern**: Missing keys (downstream `train_pool` / `teacher_setup`
cannot parse output); extra keys polluting stdout.
**Fix**: Add the missing print statements.

### [CRITICAL] 13b. NaN-Loss Fail-Loud Guard Before Ckpt Save
**auto-fixable**: yes
**Section**: §5 Teacher Mode Loop, §6 Distill Mode Loop (fail-loud guard)
**Check**: Before `torch.save(...)`, both modes assert
`math.isfinite(last_avg)` and raise `SystemExit` with a stderr message when
the dataloader yielded zero batches across all epochs (last_avg remains
`float("nan")`). Also, `_compute_proxy_mse` raises `SystemExit` when the
dataloader yields no batch (rather than silently returning `0.0`).
**Verify**: Read the ckpt-save block in both `run_teacher_mode` and
`run_distill_mode`. Confirm `if not math.isfinite(last_avg): raise
SystemExit(...)`. Read `_compute_proxy_mse` and confirm `if seen == 0: raise
SystemExit(...)`.
**Anti-pattern**: Silently writing a NaN teacher ckpt that propagates NaN
through teacher_setup → distill → proxy_mse with returncode=0; returning a
fake `0.0` proxy_mse that masks a broken dataloader (CLAUDE.md Rule 12).
**Fix**: Add the `math.isfinite` assertion + the `seen == 0` raise.

### [MAJOR] 14. Proxy MSE Computed In Distill Mode
**auto-fixable**: yes
**Section**: §6 Distill Mode Loop
**Check**: Distill mode computes `proxy_mse = _compute_proxy_mse(wrapper,
teacher, dl, device)` after training, over a few batches (default 3), under
`torch.no_grad()` and `wrapper.eval()`. The value is stored in the ckpt and
emitted on stdout.
**Verify**: Read `_compute_proxy_mse` and its call site.
**Anti-pattern**: Skipping proxy MSE (downstream loses the soft-align signal);
computing it without `eval()` / `no_grad()` (leaks memory).
**Fix**: Add the helper + call site.

### [MAJOR] 15. Live Chart Push Best-Effort
**auto-fixable**: yes
**Section**: §9 Live Chart Push
**Check**: `_make_live_push(variant_id, mode)` lazy-imports
`orca.chart.render_chart` (try/except ImportError → no-op). Push failure is
wrapped in try/except → stderr warning, never aborts training. Same `label`+
`title` re-push = refresh semantics.
**Verify**: grep for `_make_live_push` and confirm the try/except guards.
Confirm no top-level `import orca`.
**Anti-pattern**: Top-level `import orca.chart` (script dies outside Orca);
letting `render_chart` exceptions propagate into the training loop.
**Fix**: Move import into the helper; wrap push call in try/except.

### [MINOR] 16. Path Handling Consistency
**auto-fixable**: yes
**Section**: §8 Path Handling
**Check**: All paths constructed via `pathlib.Path` (no `os.path.join` with
string concatenation). Output directory created lazily via
`out_path.parent.mkdir(parents=True, exist_ok=True)`.
**Verify**: grep for `os.path.join` — should be absent (or only inside the
importlib loader for `os.path.abspath` / `os.path.isfile` checks).
**Anti-pattern**: Hardcoded `"/tmp/..."` literals; pre-creating directories
the script never writes to.
**Fix**: Switch to `pathlib.Path` / literals to CLI args.

### [MAJOR] 17. Placeholder Fallback Keeps Script Runnable
**auto-fixable**: no
**Section**: §3 User Task Loss + Dataloader (placeholder fallback)
**Check**: When `USER_TRAIN_MODULE.startswith("{{")`, the script falls back to
`_placeholder_user_loss` (MSE) + `_PlaceholderDataLoader` (re-iterable random
loader). This keeps the template smoke-testable before specialisation.
**Verify**: Read `_load_user_train`. Confirm the placeholder branch returns
both fallbacks and doesn't raise.
**Anti-pattern**: Raising on unexpanded placeholders (breaks smoke testing
before the agent specialises); fallback only returning loss, not dataloader.
**Fix**: Add the fallback branch.

### [MAJOR] 18. EMA Decay Default 0.999
**auto-fixable**: yes
**Section**: §7 KD Loss Composition
**Check**: When `kd_config["ema"]` is true and `ema_decay` is not provided,
the default is `0.999` (mean-teacher convention). The EMA shadow is
`.to(device)` aligned with the student.
**Verify**: grep for `MeanTeacherEMA` and confirm `decay=float(kd_config.get("ema_decay", 0.999))`.
**Anti-pattern**: Hardcoding `decay=0.99` (not the mean-teacher default); not
moving EMA to device.
**Fix**: Use 0.999 default and `.to(device)`.

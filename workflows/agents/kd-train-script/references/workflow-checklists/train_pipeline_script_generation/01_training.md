# Checklist: Train Leaves — Training Logic (KD-NAS)

Companion to: `workflows/train_pipeline_script_generation.md`

## How To Use

Each item verifies a verifiable requirement for the four generated leaves
(`loss.py` / `data.py` / `eval.py` / `optim.py` under `<output_dir>/user/`).
For items marked `auto-fixable: yes`, fix the leaf directly. For
`auto-fixable: no`, report the issue for the caller.

**Definitions:**
- `<user_project_root>`: user's original PyTorch project root containing
  `train.py` (loss + dataloader builder).
- `<output_dir>`: per-run artifacts dir (`$ORCA_ARTIFACTS_DIR`); the leaves
  live under `<output_dir>/user/`.
- `<skill_dir>`: the `kd-train-script` agent resource directory (containing
  this checklist's ancestor `SKILL.md`).

## Items

### [CRITICAL] 1. Four Leaves Present With Contract Callables
**auto-fixable**: yes
**Section**: workflow §1 Leaf Contract
**Check**: All four leaves exist under `<output_dir>/user/`:
- `loss.py` defines `compute_loss(s_out, y)`.
- `data.py` defines `build_dataloader(batch_size)`.
- `eval.py` defines `eval_metric(student, device)`.
- `optim.py` defines `build_optimizer(params, lr)` and
  `build_scheduler(optimizer, epochs)`.

Required positional args match the contract exactly (defaults additive —
optional kwargs allowed; a required name dropped or renamed is FAIL).
**Verify**: `ast.parse` each leaf, walk for `FunctionDef`, confirm name +
required positional arg names (positional args without defaults).
**Anti-pattern**: Renaming `s_out` to `output`; dropping `device` from
`eval_metric`; merging optimizer + scheduler into one fn.
**Fix**: Restore the contract signature from the skeleton.

### [CRITICAL] 2. No Distributed / Sampling / NAS Residue In Leaves
**auto-fixable**: yes
**Section**: workflow §1 (forbidden tokens)
**Check**: No leaf references `torch.distributed`,
`DistributedDataParallel`, `set_sample_config`, `sandwich`,
`nas_agent.train.distillation`, `logits_kd_loss`, `soft_bce_kd_loss`,
`cosine_kd_loss`. Docstrings/comments may mention them for context; code
must not.
**Verify**: `ast.walk` each leaf; check `Import` / `ImportFrom` / `Name` /
`Attribute` nodes against the deny-list.
**Anti-pattern**: Copying NAS `supernet-train-script` scaffolding into a leaf.
**Fix**: Delete the offending lines; KD-NAS leaves contribute only user logic.

### [CRITICAL] 3. Self-Contained Leaves (no sibling / user-project imports)
**auto-fixable**: no
**Section**: workflow §2 Self-Containment Rules, CONTRACTS §6
**Check**: Each leaf's top-level `import` / `from import` targets only the
whitelist `{torch, math, numpy, typing, itertools, functools, collections,
dataclasses, random}`. Relative imports (`from .`) are forbidden. No leaf
imports another leaf or any user-project module.
**Verify**: `ast.walk` each leaf; reject `ImportFrom` with `level > 0` or
`module.split('.')[0]` not in the whitelist; reject `Import` whose
`alias.name.split('.')[0]` is not in the whitelist.
**Anti-pattern**: `from data_utils import ...`; `from <user_pkg> import ...`;
`from . import helpers`.
**Fix**: Copy the helper into the same leaf file.

### [CRITICAL] 4. AST Signature Equality
**auto-fixable**: yes
**Section**: workflow §1 (AST signature), CONTRACTS §6
**Check**: Each contract callable's required positional arg set matches the
contract (function name equality + positional-without-default names equal).
Defaults are additive — extra optional kwargs are allowed.
**Verify**: Mirror `kd/_leaves.py::_check_signature`: parse, locate the
`FunctionDef`, compute `args.args[: len(args.args) - len(args.defaults)]`,
compare against the contract list.
**Anti-pattern**: Renaming `params` → `parameters`; adding a required
positional `optim_state`; dropping `device` from `eval_metric`.
**Fix**: Restore the contract names from the skeleton.

### [MAJOR] 5. User Task Loss Ported Fidelity
**auto-fixable**: no
**Section**: workflow §3
**Check**: `loss.py::compute_loss` ports the user's loss fn (semantically
identified: `(output, target) -> scalar`) verbatim — same ops, same reduction,
same shape assumptions. No placeholder fallback.
**Verify**: AST-compare the user loss fn body against `compute_loss` (with
parameter-name normalization). Layer 3 `fidelity_check.py` must print
`LOSS_ALLCLOSE: true` (or `LOSS_AST_MATCH: true` on degraded path).
**Anti-pattern**: Swapping `mse_loss` for `l1_loss`; adding a normalisation
factor the user didn't have; swapping argument order.
**Fix**: Replace with the user's verbatim body + dependency closure.

### [MAJOR] 6. Dataloader Re-Iterable
**auto-fixable**: yes
**Section**: workflow §3
**Check**: `data.py::build_dataloader(batch_size)` returns a re-iterable
object — each `iter()` re-yields the batch stream.
**Verify**: Call `build_dataloader(batch_size=2)`, run `iter()` twice,
confirm both yield at least one batch with identical `x.shape` / `y.shape`.
Layer 2 engine smoke catches non-re-iterable loaders (NaN-loss fail-loud
guard).
**Anti-pattern**: A generator function whose `iter()` exhausts after epoch 0.
**Fix**: Wrap the generator in a class with `__iter__` that re-invokes the
factory.

### [CRITICAL] 7. Optimizer / Scheduler Ported Or None
**auto-fixable**: yes
**Section**: workflow §5
**Check**: When the user's `train.py` defines an optimizer / scheduler,
`optim.py` uses the **same class + same hyperparameters** (`AdamW` not `Adam`,
same `lr`/`weight_decay`, same scheduler milestones). When the user has none,
`build_optimizer` / `build_scheduler` return `None` — the engine falls back
to `Adam` + no scheduler.
**Verify**: grep the user's `train.py` for `torch.optim.<Class>`; compare
against the leaf body verbatim. Any drift = FAIL.
**Anti-pattern**: Using `Adam` when the user has `AdamW`; inventing a
scheduler the user didn't have.
**Fix**: Replace with the user's optimizer/scheduler verbatim; return None
only if the user defines none.

### [MAJOR] 8. No KD Logic In Leaves
**auto-fixable**: yes
**Section**: workflow §8 (KD recipe selection)
**Check**: The leaves do not carry the KD recipe — no `kd.compose`, no
`KDStudentWrapper`, no `TeacherCache`, no `MeanTeacherEMA` references. The
engine composes KD from `kd_config` (decided by distill agent) and
`leaves.compute_loss`.
**Verify**: grep the leaves for `kd.compose` / `kd.wrapper` / `kd.ema`;
zero hits.
**Anti-pattern**: Inlining KD logic into `compute_loss`.
**Fix**: Remove KD references; the engine owns KD composition.

### [MAJOR] 9. EMA / Scheduler Not Invented
**auto-fixable**: yes
**Section**: workflow §5
**Check**: `build_scheduler` returns `None` when the user's `train.py` has
no scheduler. The engine's `if sch is not None: sch.step()` guard handles
`None`. Never invent a scheduler the user didn't define.
**Verify**: Read `build_scheduler`; confirm None-return path when user has
no scheduler.
**Anti-pattern**: Hardcoding `CosineAnnealingLR` because it "might help".
**Fix**: Return None.

### [CRITICAL] 10. Eval Metric Ported Fidelity
**auto-fixable**: no
**Section**: workflow §4
**Check**: `eval.py::eval_metric` ports the user's eval metric verbatim —
same formula, same normalization, same data source. Returns `(value, kind)`
with `kind ∈ {nmse, mse, ber, db, snr, acc}`.
**Verify**: Compare the leaf body against the user's eval script. Layer 3
`fidelity_check.py` must print `EVAL_ALLCLOSE: true`.
**Anti-pattern**: Inventing a metric the user's eval script doesn't compute;
swapping NMSE for MSE; auto-guessing direction from the value's sign.
**Fix**: Replace with the user's verbatim eval body + dependency closure.

### [CRITICAL] 11. Kind Direction Matches accuracy_baseline_kind
**auto-fixable**: no
**Section**: workflow §1 (kind direction), CONTRACTS §6
**Check**: The kind returned by `eval_metric` belongs to the same direction
group as `inputs.accuracy_baseline_kind`:
- **max group**: `{snr, acc}`
- **min group**: `{mse, nmse, ber, db}`

A cross-group mismatch is FAIL.
**Verify**: Read `eval_metric`'s return literal / computed kind; compare
group with `--accuracy_baseline_kind`. Layer 3 fidelity_check enforces this.
**Anti-pattern**: User says `kind=snr` (higher-better) but the leaf returns
`mse` (lower-better) — the engine's `_metric_improved` would invert the
comparison and silently train towards the wrong objective.
**Fix**: Change the leaf kind to the correct family for the user's metric;
surface a mismatch to the user if `accuracy_baseline_kind` is itself wrong.

### [CRITICAL] 12. I/O Shape Reads DUMMY_INPUT (no hardcoded shape)
**auto-fixable**: yes
**Section**: workflow §3 / §4
**Check**: Every shape literal in the leaves — dataloader batch shape,
eval-metric random data — matches the model contract's `DUMMY_INPUT`. A
hand-typed number where `DUMMY_INPUT` says otherwise is FAIL.
**Verify**: grep the leaves for shape literals; cross-check against
`DUMMY_INPUT["shape"]`. Layer 2 smoke forwards a `DUMMY_INPUT` batch through
teacher + student without a shape error.
**Anti-pattern**: Hardcoding `(..., 32, ...)` while `DUMMY_INPUT` is
`(..., 64, ...)`.
**Fix**: Source every shape from `DUMMY_INPUT`.

### [MAJOR] 13. run_config.yaml Parses + Carries Defaults
**auto-fixable**: yes
**Section**: workflow §6
**Check**: `<output_dir>/run_config.yaml` parses cleanly and carries the
user-default `lr` / `epochs` plus `accuracy_baseline` / `accuracy_baseline_kind`.
`build_cfg: {}` (teacher default). Mode is not written (driven by `--mode`).
**Verify**: `python -c "import yaml; yaml.safe_load(open(...))"`.
**Anti-pattern**: Omitting `epochs`; hardcoding `lr: 1e-3` instead of the
user default; writing `mode: teacher` (breaks `--mode` CLI precedence).
**Fix**: Re-extract user defaults; remove the `mode` key.

### [MAJOR] 14. run.sh Points At Fixed Engine Entry
**auto-fixable**: yes
**Section**: workflow §7
**Check**: `<output_dir>/run.sh` invokes
`python3 <kd_scripts_dir>/train_pipeline.py --config <output_dir>/run_config.yaml
--artifacts_dir <output_dir> --mode ${MODE:-teacher}`. It is not invoked by
any workflow node.
**Verify**: Read the script; confirm the engine path + flags.
**Anti-pattern**: A run.sh that calls a non-existent monolithic
`train_pipeline.py` under `<output_dir>`.
**Fix**: Point it at the fixed engine entry.

### [CRITICAL] 15. Zero Placeholder Residue
**auto-fixable**: yes
**Section**: workflow Layer 1
**Check**: No leaf body retains the skeleton's `raise NotImplementedError(...)`.
Every contract callable has a real ported body. No `{{` / `_placeholder_*` /
`NotImplementedError` string survives into the artifact.
**Verify**: grep each leaf for `NotImplementedError`; zero hits.
**Anti-pattern**: Leaving `eval.py` unported because the user's eval script
was ambiguous.
**Fix**: Port the user's eval logic; if no eval script exists, fail loud
upstream (do not leave a placeholder).

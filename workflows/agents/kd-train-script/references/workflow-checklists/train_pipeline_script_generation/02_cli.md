# Checklist: Train Leaves — Static + CLI Surface (KD-NAS)

Companion to: `workflows/train_pipeline_script_generation.md`

## How To Use

Each item below verifies static-correctness of the four generated leaves and
the run_config.yaml / run.sh companions. The leaves do not own a CLI — the
fixed engine entry owns the CLI surface (`_kd_scripts/train_pipeline.py`).
This checklist therefore focuses on leaf-shape correctness + companion-file
shape correctness.

## Items

### [CRITICAL] 1. Each Leaf py_compiles
**auto-fixable**: yes
**Section**: workflow Layer 1
**Check**: `python -m py_compile <output_dir>/user/<leaf>.py` exits 0 for
each of `loss.py` / `data.py` / `eval.py` / `optim.py`.
**Verify**: Run the command; observe exit status.
**Anti-pattern**: Syntax errors; unbalanced braces.
**Fix**: Fix the syntax error.

### [CRITICAL] 2. Engine --help Succeeds And Lists The Engine CLI
**auto-fixable**: no
**Section**: workflow Layer 1
**Check**: `python <kd_scripts_dir>/train_pipeline.py --help` exits 0 and
lists the engine's stable CLI: `--mode`, `--artifacts_dir`, `--out_ckpt`,
`--config`, `--experiment`, `--resume`, `--early_stop_patience`, `--epochs`,
`--lr`, `--batch_size`, `--eval_every`, `--device`, `--seed`, `--variant_id`,
`--build_fn`, `--build_cfg`, `--kd_config`, `--model_path`,
`--student_model_path`, `--teacher_cache`, `--student_ckpt`,
`--accuracy_baseline`, `--accuracy_baseline_kind`, `--project_root`,
`--env_anchor`.
**Verify**: Run the command; cross-check the printed flags.
**Anti-pattern**: Engine argparse broken (engine code, not leaf code).
**Fix**: This is engine code (out of scope for the leaf generator); surface
the issue — do not modify `_kd_scripts/train_pipeline.py` from this skill.

### [CRITICAL] 3. AST Self-Containment (deny-list)
**auto-fixable**: no
**Section**: Self-Containment Rules
**Check**: Each leaf passes `kd/_leaves.py`'s AST self-containment check:
no relative imports; no top-level imports outside the whitelist
`{torch, torchvision, torchaudio, numpy, scipy, sklearn, PIL, math, os, sys,
json, pathlib, typing, itertools, functools, collections, dataclasses,
random, io, abc, copy, re, warnings, time}`. The standard scientific stack
(torch / torchvision / numpy / scipy / scikit-learn / Pillow) IS allowed —
port the user's real torchvision loader.
**Verify**: Mirror `_leaves._check_self_contained` against each leaf.
**Anti-pattern**: `from <user_pkg> import ...`; `from . import helpers`;
`import pandas` (not in the whitelist); avoiding `torchvision` under the
false belief that the leaf "must be self-contained".
**Fix**: Copy the needed helper into the leaf; drop the user-project
import; or keep the standard-package import (torchvision/PIL/numpy are fine).

### [CRITICAL] 4. AST Signature Equality
**auto-fixable**: yes
**Section**: Leaf Contract (AST signature)
**Check**: Each contract callable has the exact required positional args
(defaults additive). Mirror `_leaves._check_signature`:
- `loss.py::compute_loss` → `["s_out", "y"]`
- `data.py::build_dataloader` → `["batch_size"]`
- `eval.py::eval_metric` → `["student", "device"]`
- `optim.py::build_optimizer` → `["params", "lr"]`
- `optim.py::build_scheduler` → `["optimizer", "epochs"]`

**Verify**: `ast.parse`, locate `FunctionDef`, compute required positional
args, compare.
**Anti-pattern**: Renaming `s_out` to `output`; adding `optim_state` as a
required positional to `build_optimizer`.
**Fix**: Restore the contract names.

### [MAJOR] 5. No Hardcoded Paths
**auto-fixable**: no
**Section**: workflow §6
**Check**: No hardcoded dataset / model / ckpt paths in any leaf. The leaves
do not own path resolution — the engine does.
**Verify**: grep the leaves for absolute path literals (`/home`, `/tmp`,
`C:\\`, `data/`); zero hits.
**Anti-pattern**: `DATA_DIR = "/path/to/dataset"` inside `data.py`.
**Fix**: Use the workflow's path conventions; the engine reads the
`--artifacts_dir` and the model contract's `DUMMY_INPUT`.

### [MAJOR] 6. run_config.yaml Schema
**auto-fixable**: yes
**Section**: workflow §6
**Check**: `run_config.yaml` parses and contains at minimum: `epochs`,
`lr`, `batch_size`, `accuracy_baseline`, `accuracy_baseline_kind`, `build_cfg`.
Optional: `eval_every`, `early_stop_patience`. Mode is NOT written (driven by
`--mode`).
**Verify**: `python -c "import yaml; d=yaml.safe_load(open(...));
assert 'epochs' in d and 'lr' in d and 'mode' not in d"`.
**Anti-pattern**: Forgetting `epochs`; writing `mode: teacher` (breaks
`--mode` CLI precedence — distill would silently use the yaml's mode).
**Fix**: Remove `mode`; ensure required keys present.

### [MAJOR] 7. run.sh Engine Path + Flags
**auto-fixable**: yes
**Section**: workflow §7
**Check**: `run.sh` calls the fixed engine entry with `--config`, `--artifacts_dir`,
`--mode ${MODE:-teacher}`, and optional `--resume` via env. It does not call
a monolithic per-project `train_pipeline.py`.
**Verify**: Read the script.
**Anti-pattern**: `python3 train_pipeline.py` from the cwd (no path); missing
`--artifacts_dir` (leaves won't be found).
**Fix**: Point at the fixed engine; pass both flags.

### [MAJOR] 8. No `--user_*` Flags Anywhere
**auto-fixable**: yes
**Section**: workflow §6
**Check**: The flag names `--user_train_import` / `--user_loss_fn` /
`--user_eval_import` / `--user_eval_fn` and the helper names
`_load_user_train` / `_load_user_eval` appear nowhere in the leaves,
run_config.yaml, or run.sh.
**Verify**: grep the artifacts; zero hits.
**Anti-pattern**: Introducing any `--user_*` flag or runtime user-logic loader.
**Fix**: Remove; user logic lives only inside the leaves.

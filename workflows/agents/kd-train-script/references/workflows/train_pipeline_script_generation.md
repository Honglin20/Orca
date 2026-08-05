# Train Leaves Generation Workflow (KD-NAS)

Use this workflow to generate the four project-specific training **leaves** that
the fixed `KDTrainer` engine (`workflows/agents/_kd_scripts/train_pipeline.py`)
loads at runtime. The leaves plus a `run_config.yaml` and a human-use `run.sh`
are the only artifacts produced.

**Generation strategy: specialise the four leaf skeletons, never write leaves
from scratch.** Each skeleton at
`<skill_dir>/references/templates/leaves/{loss,data,eval,optim}.py.skel` raises
`NotImplementedError` with the contract signature in its docstring. You
instantiate a skeleton (copy to `<output_dir>/user/<leaf>.py`) and replace the
`raise NotImplementedError(...)` body with the user's ported code. The body
plus its module-level dependency closure (constants / helpers) must live in
the same file — no sibling imports, no user-project imports.

Key characteristics of the generated leaves:

1. **No sandwich sampling, no DDP, no torchrun.** KD-NAS is single-device;
   the fixed engine drives the training loop, the leaves only contribute the
   user's loss / dataloader / eval-metric / optimizer / scheduler.
2. **Self-contained** — the engine loader (`kd/_leaves.py`) does not inject
   `sys.path`; each leaf is loaded via `importlib.util.spec_from_file_location`
   as its own module. "Self-contained" forbids **user-project modules**
   (relative imports + non-whitelisted absolute imports such as
   `from user_pkg import …`), NOT the standard scientific stack. Top-level
   imports are limited to the whitelist
   `{torch, torchvision, torchaudio, numpy, scipy, sklearn, PIL, math, os,
   sys, json, pathlib, typing, itertools, functools, collections,
   dataclasses, random, io, abc, copy, re, warnings, time}`. The pip
   scientific stack (torch / torchvision / numpy / scipy / scikit-learn /
   Pillow) is always available — port the user's real torchvision / PIL /
   numpy loader verbatim. Relative imports and sibling imports are forbidden.
3. **No data fabrication** — `data.py` and `eval.py` must load the user's
   **real** dataset. `torch.rand` / `torch.randn` / `torch.randint` /
   `torch.randperm` / `numpy.random.*` as the source of pixels or labels is
   fabrication (it decouples inputs from targets and silently produces a
   model that cannot learn). `fidelity_check.py` rejects such leaves. If the
   user's data is genuinely unavailable (user-project module, missing files),
   fail loud + emit an ask-user sentinel; never silently substitute random
   data.
4. **Four leaves + run_config.yaml + run.sh** — no monolithic script. The
   fixed engine entry consumes the leaves via the `--artifacts_dir` flag.
5. **AST signature contract** — function name + required positional args must
   match the contract exactly (defaults are additive; you may add optional
   kwargs, never drop or rename a required param).

## Source Evidence

Build the leaves from:

* **User train.py** under `<user_project_root>` — task loss, dataloader,
  optimizer/scheduler when present, batch format, model-call signature.
* **User eval script** under `<user_project_root>` (discovered: `test_*.py` /
  `eval*.py` / `evaluate*.py` / `test.py`, or an eval/metric fn inside
  `train.py`) — the accuracy metric + eval data loading, ported into
  `eval.py::eval_metric`. If none is found, fail loud.
* **Teacher model** at `<teacher_model_path>` — exposes `build_model(**cfg)`,
  `DUMMY_INPUT`, `feature_hook_names()` (optional).
* **Student variant** at `<student_model_path>` (user student model `.py`)
  — same contract.
* The **leaf skeletons** at
  `<skill_dir>/references/templates/leaves/{loss,data,eval,optim}.py.skel` —
  non-runnable starting points that fix the contract surface.

## Generation Rules

### 1. Leaf Contract (authoritative)

Four leaves land under `<output_dir>/user/`. Each must define the required
callable with the exact signature (defaults additive):

| file | callable | required positional args | returns |
|---|---|---|---|
| `loss.py` | `compute_loss` | `s_out, y` | scalar `Tensor` |
| `data.py` | `build_dataloader` | `batch_size` | re-iterable yielding `(x, y)` |
| `eval.py` | `eval_metric` | `student, device` | `(value, kind)` |
| `optim.py` | `build_optimizer` | `params, lr` | `Optimizer | None` |
| `optim.py` | `build_scheduler` | `optimizer, epochs` | `LRScheduler | None` |

`kind` returned by `eval_metric` must be one of `{nmse, mse, ber, db, snr, acc}`.
The kind fixes the metric direction:

- **higher is better (max)**: `snr`, `acc`
- **lower is better (min)**: `mse`, `nmse`, `ber`, `db`

The kind's direction group must match `inputs.accuracy_baseline_kind`'s
direction group, else `fidelity_check.py` fails loud.

### 2. Self-Containment Rules

A leaf must not import sibling files or the user's project. The engine loader
(`kd/_leaves.py`) AST-validates each leaf before exec and rejects:

- Any `from . import …` / `from .. import …` (relative imports).
- Any top-level `import X` / `from X import Y` where `X.split('.')[0]` is
  outside the whitelist `{torch, torchvision, torchaudio, numpy, scipy,
  sklearn, PIL, math, os, sys, json, pathlib, typing, itertools, functools,
  collections, dataclasses, random, io, abc, copy, re, warnings, time}`.

"Self-contained" forbids **user-project modules**, NOT the standard scientific
stack — `from torchvision.datasets import <RealDataset>` is legitimate and
expected. Port the user's real torchvision / PIL / numpy loader verbatim.

Constants / helper classes / helper functions used by a leaf must live in the
same file. There is no `data_utils.py` sibling — put loader helpers in
`data.py`.

### 2b. No Data Fabrication (hard rule, enforced by `fidelity_check.py`)

`data.py` and `eval.py` exist to load the user's **real** dataset.  Using the
following calls as the **source of pixels or labels** is fabrication and is
rejected by `fidelity_check.py` (`LEAF_FABRICATION_OK: false`):

- `torch.rand(...)`, `torch.randn(...)`, `torch.randint(...)`,
  `torch.randperm(...)`
- `numpy.random.<any>(...)` / `np.random.<any>(...)`

Randomness for parameter init (`torch.nn.init.*`), batch shuffling
(`torch.randperm` used as a DataLoader sampler over **real** samples), and
augmentation applied on top of ground-truth data loaded from disk is allowed.

If the user's dataloader depends on a **user-project module** or on data that
is genuinely unavailable (not a pip package, not on disk), **fail loud** and
emit an ask-user sentinel describing the missing dependency / data path.
Never silently substitute random tensors for the real loader — fabricated
data decouples inputs from targets and silently produces a model that cannot
learn.

### 3. User Task Loss + Dataloader (port verbatim)

`loss.py::compute_loss` ports the user's loss fn (identified by semantics:
the `(output, target) -> scalar` function) verbatim — same ops, same
reduction, same shape assumptions. The function body plus its module-level
dependency closure (constants / helpers it references) is copied in. A ported
function that still depends on user-project symbols is a fail-loud condition.

`data.py::build_dataloader` ports the user's data-loading logic verbatim —
including its `torchvision` / `PIL` / `numpy` imports and the real dataset
path/transform. The returned object must be **re-iterable**: each epoch's
`iter(dl)` yields a fresh batch stream. If the user's loader is a one-shot
generator, wrap it in a re-iterable adapter (a class with `__iter__` that
re-invokes the factory). **Never substitute `torch.rand` / `torch.randint` /
`numpy.random.*` for the user's dataset** — see §2b.

### 4. User Eval Metric (port verbatim)

`eval.py::eval_metric(student, device)` ports the user's eval metric verbatim:
same formula, same normalization, **same real data source** (port the user's
eval loader — torchvision/PIL/numpy, never `torch.rand`/`torch.randint` — see
§2b). The eval data loading is copied into the leaf, not imported live.
Returns `(value, kind)` with kind in the allowed set.

Discovery (the agent's judgment): glob `<user_project_root>` for `test_*.py` /
`eval*.py` / `evaluate*.py` / `test.py`, and read `train.py` for an eval/metric
fn. Read the hit, extract its metric computation + eval data loading, and port
into `eval_metric`. **No eval script found → fail loud** (no dummy-metric
degradation — eval always measures the user's real metric).

Custom-named metrics map to the closest direction family (PSNR→`snr`,
arbitrary higher-better→`acc`). The kind's direction must match
`inputs.accuracy_baseline_kind`.

### 5. Optimizer, Scheduler (port-or-return-None)

`optim.py::build_optimizer(params, lr)` ports the user's optimizer constructor
verbatim (same class, same hyperparameters — `AdamW` stays `AdamW`). When the
user's `train.py` defines no optimizer, `build_optimizer` returns `None`; the
engine falls back to `torch.optim.Adam(params, lr=lr)`. Never invent
hyperparameters.

`optim.py::build_scheduler(optimizer, epochs)` ports the user's scheduler
verbatim. Returns `None` when the user defines none. The ported scheduler's
step cadence must match the user's — the engine calls `scheduler.step()` once
per epoch; if the user stepped per batch, port it as a per-epoch equivalent
or document the deviation in a comment.

### 6. run_config.yaml

`<output_dir>/run_config.yaml` carries the teacher-mode default knobs the
engine reads via `--config`. Fields:

```yaml
epochs: <user_default>        # extracted from the user's train.py
lr: <user_default>            # extracted from the user's train.py
batch_size: 4
eval_every: 1
early_stop_patience: 0
accuracy_baseline: <from inputs.accuracy_baseline>
accuracy_baseline_kind: <from inputs.accuracy_baseline_kind>
build_cfg: {}                 # teacher default
# mode is NOT written here — driven solely by the --mode flag.
```

distill mode patches `kd_config` into this yaml each round (distill's
unique kd_config source of truth). gen_train_script does not pre-write
`kd_config` — it's a distill-agent responsibility.

Priority: `CLI --flag` > `run_config.yaml` > engine default.

### 7. run.sh (human-only launcher)

`<output_dir>/run.sh` lets a human re-run training without the workflow. It is
**never** invoked by any workflow node.

```bash
#!/usr/bin/env bash
set -euo pipefail
export ORCA_KD_SCRIPTS_DIR="<kd_scripts_dir abs>"
KD_ENTRY="$ORCA_KD_SCRIPTS_DIR/train_pipeline.py"
ARTIFACTS_DIR="<output_dir abs>"
MODE="${MODE:-teacher}"; EXPERIMENT="${EXPERIMENT:-$MODE}"; RESUME="${RESUME:-}"
python3 "$KD_ENTRY" --config "$ARTIFACTS_DIR/run_config.yaml" \
  --artifacts_dir "$ARTIFACTS_DIR" --mode "$MODE" --experiment "$EXPERIMENT" \
  ${RESUME:+--resume "$RESUME"}
```

### 8. KD Recipe Selection

The leaves do not carry the KD recipe — `kd_config` is decided by the distill
agent (AST on `feature_hook_names`) and written to `run_config.yaml` each
round. gen_train_script only ensures the user's loss + dataloader are
self-contained and faithful; the engine composes the KD loss from
`kd.compose.build_kd_loss(leaves.compute_loss, kd_config)`.

## Validation

Generated leaves are validated in **four layers**, run in order.

### Layer 1: Static + AST self-containment + AST signature

For each leaf under `<output_dir>/user/`:

- `python -m py_compile <leaf>` must succeed.
- AST scan: no sibling / relative imports; top-level imports limited to the
  whitelist (standard scientific stack + stdlib).
- AST signature: function name + required positional args match the contract
  exactly (defaults additive).
- Anti-fabrication scan (data.py / eval.py only): no `torch.rand` /
  `torch.randn` / `torch.randint` / `torch.randperm` / `numpy.random.*` calls.
  Mirrored by `fidelity_check.py`'s `LEAF_FABRICATION_OK` check (Layer 3).

### Layer 2: Engine smoke

Run the fixed engine entry against the generated leaves, tiny budget on CPU.
The engine loads the leaves via `--artifacts_dir <output_dir>`; a missing
leaf or a signature mismatch fails loud at load time.

**Teacher mode smoke** (synthetic teacher model + ckpt path):

```bash
ORCA_KD_SCRIPTS_DIR=<kd_scripts_dir> \
python <kd_scripts_dir>/train_pipeline.py \
    --mode teacher --artifacts_dir <output_dir> \
    --model_path <teacher_model_path> --build_cfg '{}' \
    --epochs 1 --batch_size 2 --device cpu \
    --out_ckpt <output_dir>/smoke_teacher.pth --experiment smoke
```

Assert stdout contains `TEACHER_CKPT:` + `TASK_LOSS_FINAL:`; the ckpt file
exists with a dict carrying `state_dict`, `build_cfg`, `variant_id`, `epochs`,
`final_loss`, `mode == "teacher"`.

**Eval mode smoke** (read-only, on the teacher smoke ckpt):

```bash
ORCA_KD_SCRIPTS_DIR=<kd_scripts_dir> \
python <kd_scripts_dir>/train_pipeline.py \
    --mode eval --artifacts_dir <output_dir> \
    --student_model_path <teacher_model_path> --build_cfg '{}' \
    --student_ckpt <output_dir>/smoke_teacher.pth \
    --accuracy_baseline <inputs.accuracy_baseline> \
    --accuracy_baseline_kind <inputs.accuracy_baseline_kind> \
    --device cpu --experiment smoke
```

Assert stdout contains `STUDENT_ACCURACY:` + `STUDENT_ACCURACY_KIND:` +
`MET_ACCURACY:` + `ACCURACY_CONFIDENCE:`; no checkpoint file is written.

### Layer 3: fidelity_check.py (per-leaf numeric equivalence, mandatory)

Run `python <skill_dir>/scripts/fidelity_check.py`:

```bash
python <skill_dir>/scripts/fidelity_check.py \
    --leaves_dir <output_dir>/user \
    --user_train <user_project_root>/train.py \
    --user_eval <discovered user eval script> \
    --dummy_input '{"shape": [...], "dtype": "float32"}' \
    --model_path <teacher_model_path> \
    --build_fn build_model --build_cfg '{}' \
    --accuracy_baseline_kind <inputs.accuracy_baseline_kind> \
    --project_root <user_project_root>
```

Must print `FIDELITY: PASS`. The script checks per-leaf loss / dataloader /
eval-metric / optimizer numeric equivalence with the user's original code
(`torch.allclose(rtol=1e-5)`), AST self-containment, AST signature equality,
anti-fabrication (no random-tensor data source in data/eval leaves), and
kind-direction hard consistency with `--accuracy_baseline_kind`.
`FIDELITY: FAIL` (or exit 2) → fix the leaves and re-run Layers 1–2.

### Layer 4: Cross-reference verifier subagent

Invoke the `workflow-verifier` subagent per the SKILL.md prompt template. It
reviews the four leaves in parallel against the user's original `train.py` /
eval script.

Handle the verifier response:

- `all-pass` with no **Fixed** section → done.
- `all-pass` with a **Fixed** section → re-run Layer 2 smoke tests.
- `unresolved` → apply each suggested fix, re-run Layers 1–3.

## Forbidden

- Do not generate a monolithic `train_pipeline.py`. The engine is fixed code;
  only the four leaves + `run_config.yaml` + `run.sh` are products.
- Do not enable DDP / torchrun / `set_sample_config`.
- Do not `import nas_agent.train.distillation` — the engine uses `kd.compose` /
  `kd.wrapper` / `kd.ema`.
- Do not hardcode dataset / model / ckpt paths in the leaves — they are
  project-fixed at workflow-run scope, not leaf scope.
- Do not skip Layer 2 smoke tests.
- No sibling imports, no relative imports, no top-level imports outside the
  whitelist (standard scientific stack + stdlib).
- No data fabrication: `data.py` / `eval.py` must not use `torch.rand` /
  `torch.randn` / `torch.randint` / `torch.randperm` / `numpy.random.*` as
  data/label source. Fail loud + ask-user sentinel if the user's data is
  genuinely unavailable.
- No placeholder fallbacks: an unspecialised leaf body must keep its
  `NotImplementedError` (fail loud), never a dummy substitution.

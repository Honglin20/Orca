# Train Pipeline Script Generation Workflow (KD-NAS)

Use this workflow to generate `<output_dir>/train_pipeline.py` — the unified
KD-NAS training entry point supporting **teacher**, **distill**, and **eval**
modes behind one CLI. The script is self-contained: user project code is
**ported in verbatim**, never imported at runtime.

**Generation strategy: specialise a skeleton, not fill placeholders.** The
reference template
(`<skill_dir>/references/templates/train_pipeline.py`) is a **skeleton** — a
non-runnable intermediate whose five fixed user-interface slots raise
`NotImplementedError`. You copy it to `<output_dir>/train_pipeline.py` and
port the user's own code into those slots. The generated artifact is
project-specific code: zero placeholder strings, zero dummy fallbacks, zero
runtime loading of the user's project.

Key characteristics of the generated script (must not regress):

1. **No sandwich sampling, no DDP, no torchrun.** KD-NAS is single-device +
   `--device` CLI; serial kd-nas workflow (gen_student → distill → decide loop, one
   student per round) handles orchestration outside this script. (Historical:
   2026-08-04 cleanup §2 deleted `train_pool` ThreadPoolExecutor parallel sweep.)
2. **Models loaded by path** via `importlib.util.spec_from_file_location` —
   teacher and student share the same contract (`build_model` + `DUMMY_INPUT` +
   `KNOBS`, see `workflows/agents/_kd_scripts/CONTRACTS.md` §1).
3. **Three modes** in one script (teacher / distill / eval). Teacher + distill
   share the training infrastructure (optimizer / scheduler / dataloader /
   task_loss / loop skeleton); mode-specific code paths diverge at loss +
   ckpt schema. **Eval is read-only**: it loads a student ckpt, runs the
   ported `user_eval_metric`, and emits the accuracy protocol consumed by
   the serial `distill` node's ledger append (replacing the historical
   `measure_student --eval_command` path; see 2026-08-04 cleanup).
4. **KD loss** uses the existing KD-NAS library
   (`kd.compose.build_kd_loss` + `kd.wrapper.KDStudentWrapper` +
   `kd.wrapper.TeacherCache.load` + `kd.ema.MeanTeacherEMA`).
5. **Narrower user-train.py contract.** KD-NAS users provide a task loss
   (`compute_loss(s_out, y)` or semantically equivalent) + data loading
   (`build_dataloader()` or equivalent in the training loop); optimizer /
   scheduler are optional and ported when present (see
   `examples/kd-nas-demo/train.py`). Absent optimizer → explicit `Adam`
   fallback with an annotated note; absent dataloader-equivalent →
   **fail loud**.
6. **Eval metric auto-discovered.** The user's repo already contains an eval
   script (e.g. `examples/kd-nas-demo/test_student.py`) — the agent discovers
   and reads it (see §3.1 "User Eval Metric"), ports its metric computation
   into `user_eval_metric`, and emits the `STUDENT_ACCURACY` protocol. No
   `test_command` workflow input is required. No eval script → fail loud (no
   dummy degradation).

## Source Evidence

Build the script from:

* **User train.py** under `<user_project_root>` — task loss, dataloader,
  optimizer/scheduler if present, batch format, model-call signature.
* **User eval script** under `<user_project_root>` (discovered: `test_*.py` /
  `eval*.py` / `evaluate*.py` / `test.py`, or an eval/metric fn inside
  `train.py`) — the accuracy metric (NMSE/MSE/BER/SNR/acc) + eval data
  loading, ported into `user_eval_metric`. If none is found, fail loud (the
  user contract asserts the repo contains one).
* **Teacher model** at `<teacher_model_path>` (e.g. a teacher-gen output
  wrapper `.py`, or any KD variant `.py` exposing the same contract) — exposes
  `build_model(**cfg)`, `DUMMY_INPUT`, `feature_hook_names()`.
* **Student variant** at `<student_model_path>` (KB receiver `.py`) — same
  contract.
* The reference **skeleton template** at
  `<skill_dir>/references/templates/train_pipeline.py` — a non-runnable
  intermediate with five fixed `user_*` slots. **Start from this file**:
  copy it to `<output_dir>/train_pipeline.py` and **specialise the slots**
  by porting the user's code verbatim (see §3 / §3.1 / §4).

Generated artifacts must be self-contained. Apart from the Python standard
library, installed third-party packages, and the KD-NAS ``kd/`` library
(provided by the workflow), generated artifacts must not import from
`<user_project_root>`. Copy any required user logic (dataset, loss, optimizer,
scheduler, dataloader, eval metric) into `train_pipeline.py` or a sibling
helper file under `<output_dir>`; a ported function body that still depends
on user-project symbols (e.g. `from <user_pkg> import ...`) is a **fail loud**
condition — never load the user's module at runtime to cover it.

## Generation Rules

### 1. CLI And Runtime Args

The reference skeleton already exposes the full CLI contract. Preserve it
verbatim; only add project-derived arguments (dataset/config paths, training
budget, optimizer/scheduler hyperparameters) when the user's project requires
them.

Stable base CLI (must remain in every generated `train_pipeline.py`):

- `--mode {teacher,distill,eval}` (required) — selects mode.
- `--out_ckpt PATH` (required) — checkpoint output path.
- `--epochs INT` (default 3).
- `--lr FLOAT` (default 1e-3).
- `--batch_size INT` (default 4).
- `--device STR` (default `auto`; resolves to `cuda` if available else `cpu`).
- `--seed INT` (default 0).
- `--variant_id STR` (default `"model"`; used in chart label/title + ckpt metadata).
- `--build_fn STR` (default `build_model`).
- `--build_cfg JSON` (default `{}`; passed to `build_model(**cfg)` —
  teacher's `build_cfg` and student's `student_cfg` share this flag).
- `--model_path PATH` (required in teacher mode) — teacher `.py` path.
- `--student_model_path PATH` (required in distill & eval mode) — student `.py` path.
- `--teacher_cache PATH` (required in distill mode) — `teacher_cache.pt` from
  `teacher_setup.py`.
- `--kd_config JSON` (default `{"kd_losses": ["mse"], "weights": {"mse": 1.0}}`; distill mode — non-empty kd_losses mandatory).
- `--student_ckpt PATH` (required in eval mode) — student checkpoint to load.
- `--accuracy_baseline FLOAT` (eval mode) — absolute accuracy baseline (user-provided).
- `--accuracy_baseline_kind STR` (eval mode) — nmse/mse/ber/db (lower better) |
  snr/acc (higher better); locks direction via `kd_common.accuracy_direction`.
- `--project_root PATH` — prepended to `sys.path`; **semantics narrowed to
  data-file / path resolution** (user data files referenced by relative
  paths), no longer a runtime user-module injection mechanism.
- `--env_anchor PATH` — ORCA env bootstrap anchor (per-run artifacts dir).

**Removed flags (must NOT reappear):** the four placeholder-override flags
`--user_train_import`, `--user_loss_fn`, `--user_eval_import`,
`--user_eval_fn` are **deleted** from the stable base CLI. All user logic is
ported into the five fixed slots at generation time; no runtime
module/function injection exists. A generated script exposing any of them is
a regression (checklist 02 [CRITICAL]).

**No DDP / torchrun / world-size / local-rank flags.** Single device only.

### 2. Model Construction (by path)

Use `_load_model_by_path(model_path, build_fn, cfg)` from the reference
skeleton verbatim. It:

1. Resolves the model file's absolute path and inserts its directory into
   `sys.path` (so KD-NAS variant shared blocks like `from _model8_blocks
   import ...` resolve).
2. Imports the module via `importlib.util.spec_from_file_location` and caches
   it in `sys.modules` (so downstream imports hit cache).
3. Calls `getattr(mod, build_fn)(**cfg)`.

**Teacher mode**: `teacher = _load_model_by_path(args.model_path, args.build_fn,
cfg).to(device).train()`.

**Distill mode**: build student, then wrap with `KDStudentWrapper(student,
hook_names)` where `hook_names = list(student.feature_hook_names())` (empty
list if the student doesn't expose the optional hook method — KD-NAS contract
requires it for feature-KD terms but not for plain MSE KD). Load the teacher
cache via `TeacherCache.load(args.teacher_cache)`.

### 3. User Task Loss + Dataloader (self-contained specialisation)

**Single strategy: port the user's code into the fixed slots verbatim.** The
user's `compute_loss` (identified **by semantics**, not by name: the
`(output, target) -> scalar loss` function) is copied into
`user_compute_loss` — same function body, same ops, same reduction, same
shape assumptions. The user's data loading (`build_dataloader()` or the
equivalent loader construction found in the training loop) is copied into
`user_build_dataloader`. No runtime loading of the user's train module
exists — the old path/module-injection mechanism (`_load_user_train` +
`USER_TRAIN_MODULE`/`USER_LOSS_FN` constants + `--user_train_import` /
`--user_loss_fn` flags) is **removed**.

**Dependency-closure rule (port boundary):** a ported function body includes
its **module-level dependency closure** — constants, helper classes and
helper functions it references (e.g. demo's `_SHAPE` / `_RandomDataLoader`
in `examples/kd-nas-demo/train.py`) — copied alongside the body. A ported
function that still depends on user-project symbols (an unresolved
`from <user_pkg> import ...`) is a **fail loud** condition, never a
runtime-import workaround.

The dataloader must be **re-iterable**: each epoch's `iter(dl)` yields a fresh
batch stream. The reference shape is a class with `__iter__`/`__len__`. If
the user's loader is a one-shot generator, wrap it in a re-iterable adapter
(or call the builder at the start of every epoch).

### 3.1 User Eval Metric (eval mode, self-contained)

**Single strategy: port the user's eval metric into `user_eval_metric`
verbatim.** `user_eval_metric(student, device)` returns `(value, kind)` with
kind ∈ {nmse, mse, ber, snr, acc} and owns its own eval data loading (ported
from the user's eval script).

Discovery (the agent's judgment, not a workflow input): glob
`<user_project_root>` for `test_*.py` / `eval*.py` / `evaluate*.py` /
`test.py`, and read `train.py` for an eval/metric fn. Read the hit, extract
its metric computation + eval data loading, and port it into
`user_eval_metric`. When the user's eval logic is inline in `main()`, extract
the metric computation + data loading and inline it in `user_eval_metric`.
**No eval script found → fail loud** (no dummy-metric degradation — eval
always measures the user's real metric).

The eval data-loading code is ported (copied), not imported live — same
self-containment rule as the dataloader.

### 4. Optimizer, Scheduler (user-port-or-explicit-None)

Port the user's optimizer / scheduler into `build_user_optimizer(params, lr)`
/ `build_user_scheduler(optimizer, epochs)` **verbatim** (same class, same
hyperparameters — `AdamW` stays `AdamW`). When the user's `train.py` defines
no optimizer, `build_user_optimizer` returns `None` and the skeleton's
explicit fallback applies:

```python
optimizer = build_user_optimizer(teacher.parameters(), args.lr)
if optimizer is None:
    # kd-train-script: user train.py defines no optimizer — explicit fallback.
    optimizer = torch.optim.Adam(teacher.parameters(), lr=args.lr)
```

Annotate the fallback explicitly; it must not invent hyperparameters not
present in the user's project. `build_user_scheduler` returns `None` when the
user defines no scheduler (never invent one); the ported scheduler's **step
cadence must match the user's** (per-epoch vs per-batch — align with the
loop's `scheduler.step()`).

**Distill mode**: the optimizer parameter group must include both the student
parameters (`wrapper.parameters()`) **and** the KD adapter parameters
(`kd_loss.kd_parameters()`) so OFD/FitNets adapters train. This requires
materialising one batch, calling `wrapper(x0)` + `teacher(x0)` once under
`torch.no_grad`, and `kd_loss.prepare(s_feats0, t_feats0)` to pre-build the
adapters **before** constructing the optimizer. The reference skeleton shows
the exact ordering.

### 5. Teacher Mode Loop

```python
teacher.train()
for epoch in range(args.epochs):
    for x, y in iter(dl):
        x, y = x.to(device), y.to(device)
        out = teacher(x)
        loss = user_compute_loss(out, y)   # pure task loss — no KD in teacher mode
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        optimizer.step()
```

Save checkpoint with schema:
```python
{
    "state_dict": teacher.state_dict(),
    "build_cfg": cfg,
    "variant_id": args.variant_id,
    "epochs": args.epochs,
    "final_loss": last_avg,
    "mode": "teacher",
}
```

**Fail-loud guard (mandatory)**: before `torch.save`, assert
`math.isfinite(last_avg)`. If the dataloader yielded zero batches across all
epochs (e.g. user loader returned an empty loader or a one-shot generator that
already exhausted), `last_avg` stays at its `float("nan")` initialiser —
raise `SystemExit` with a clear stderr message rather than silently writing a
NaN ckpt. A NaN teacher ckpt would propagate NaN through `teacher_setup` →
distill → `proxy_mse` with `returncode=0`, violating CLAUDE.md Rule 12.

Stdout keys (downstream `teacher_setup` parses these):
```
TEACHER_CKPT: <path>
TASK_LOSS_FINAL: <float>
```

### 6. Distill Mode Loop

```python
wrapper.train()
for epoch in range(args.epochs):
    for x, y in iter(dl):
        x, y = x.to(device), y.to(device)
        s_out, s_feats = wrapper(x)
        with torch.no_grad():
            t_out, t_feats = teacher(x)
        ema_out = ema(x) if ema is not None else None
        # kd_loss calls user_compute_loss(s_out, y) internally + KD terms.
        loss = kd_loss(s_out, y, s_feats, t_out, t_feats, ema_out, epoch)
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        optimizer.step()
        if ema is not None:
            ema.update(wrapper.student)
```

Save checkpoint with schema:
```python
{
    "student_state_dict": wrapper.student.state_dict(),
    "variant_id": args.variant_id,
    "student_cfg": student_cfg,
    "kd_config": kd_config,
    "epochs": args.epochs,
    "proxy_mse": proxy_mse,
    "mode": "distill",
}
```

**Fail-loud guard (mandatory)**: same as teacher mode — assert
`math.isfinite(last_avg)` before `torch.save`. Additionally,
`_compute_proxy_mse` raises `SystemExit` if the dataloader yields no batch
(returning a fake `0.0` would mask a broken pipeline — proxy_mse is a
downstream-consumed signal, CLAUDE.md Rule 12).

Stdout keys (downstream `distill` node parses these via ledger append):
```
STUDENT_CKPT: <path>
KD_LOSS_FINAL: <float>
KD_PROXY_MSE: <float>
```

### 6.5 Eval Mode (read-only metric)

```python
student = _load_model_by_path(args.student_model_path, args.build_fn, cfg).to(device)
ck = torch.load(args.student_ckpt, map_location=device)
# tolerate distill / teacher / bare-state_dict ckpt formats
sd = ck["student_state_dict"] if isinstance(ck.get("student_state_dict"), dict) \
    else (ck["state_dict"] if isinstance(ck.get("state_dict"), dict) else ck)
student.load_state_dict(sd, strict=False)
student.eval()
with torch.no_grad():
    value, kind = user_eval_metric(student, device)   # ported from the user eval script
```

**No checkpoint is written** — eval is read-only. **Fail-loud guard
(mandatory)**: assert `math.isfinite(value)` before emitting; a non-finite
metric must raise (never a silent fake value — CLAUDE.md Rule 12).

Direction + met judgment use `kd_common.accuracy_direction(kind)` (lazy
import, mirrors distill mode's lazy `kd.wrapper` import) against
`--accuracy_baseline` / `--accuracy_baseline_kind`; unknown kind →
`met_accuracy=false`, `confidence=low` + stderr WARN (never auto-guess
direction).

Stdout keys (downstream `distill` node parses these via ledger append — same protocol the
old `measure_student` emitted):
```
STUDENT_ACCURACY: <float>
STUDENT_ACCURACY_KIND: <nmse|mse|ber|snr|acc>
MET_ACCURACY: <bool>
ACCURACY_CONFIDENCE: high|low
```

### 7. KD Loss Composition

Use `kd.compose.build_kd_loss(user_compute_loss, kd_config)` to assemble the
composite. The composite owns OFD / FitNets adapters internally; their
parameters land in the optimizer via `kd_loss.kd_parameters()` (see §4).

**Decision rules** (select KD terms from the user's task semantics, do not
enable all by default):

- Output MSE (`"mse"` term) is the safe default for KD-NAS regression tasks
  where teacher/student share the exact output shape `[B, 4, 48, 64, 1]`.
- OFD / FitNets / RKD require `feature_hook_names()` on both teacher and
  student (KD-NAS contract §1). If the student variant doesn't expose hooks
  or the hook count mismatches the teacher, raise loudly — do not silently
  drop the feature-KD term.
- EMA (mean teacher) is enabled only when `kd_config["ema"]` is true; decay
  defaults to 0.999 (mean-teacher convention).

The default `--kd_config` is `{"kd_losses": ["mse"], "weights": {"mse": 1.0}}`
— distill mode **must** carry a non-empty KD term; `build_kd_loss` rejects
empty `kd_losses` (with ema off) fail loud (pure task loss is not distillation;
that belongs to `--mode teacher`). The agent picks KD terms based on the user's
task, never invents exotic KD recipes.

### 8. Path Handling

Use `pathlib.Path` for all path construction (no `os.path.join`). All paths
must be CLI-overridable (no hardcoded literals). The reference skeleton
already does this — preserve the pattern.

The output directory is created lazily via
`out_path.parent.mkdir(parents=True, exist_ok=True)` before `torch.save` —
do not pre-create directories outside the ckpt path.

### 9. Live Chart Push (best-effort sidecar)

The reference skeleton includes `_make_live_push(variant_id, mode)` which
lazy-imports `orca.chart.render_chart` and degrades to no-op outside Orca.
Preserve this helper verbatim — it is the live-training-curve sidecar.
Push failure must never abort training (`try/except` → stderr warning).

## Validation

Generated artifacts are validated in **four layers**, run in order. Fix any
failure before invoking the verifier subagent.

### Layer 1: Static + no-residue checks

- `python -m py_compile <output_dir>/train_pipeline.py` — must succeed.
- `python <output_dir>/train_pipeline.py --help` — must succeed and list all
  CLI flags. Verify the actual argparse block matches the documented stable
  base CLI.
- **CLI consistency**: every `--flag` the workflow expects (listed in §1) is
  accepted; no orphaned flags (**zero `--user_*` flags**).
- **AST scan — zero placeholder residue**: parse with `ast` and confirm no
  `{{` literal, no `_placeholder_*` identifier, no `USER_TRAIN_MODULE` /
  `USER_EVAL_MODULE` constant, and no `_load_user_train` / `_load_user_eval`
  function definition (the removed runtime-injection mechanism) anywhere in
  the generated artifacts.

### Layer 2: Functional smoke tests (always, no override flags)

Run all three modes with a tiny budget on CPU. **No `--user_*` override
flags are passed** — the script must carry its ported logic itself; an
unspecialised slot crashes with `NotImplementedError` (fail-loud gate).

**Teacher mode smoke**:

```bash
ORCA_KD_SCRIPTS_DIR=<kd_scripts_dir> \
python <output_dir>/train_pipeline.py \
    --mode teacher \
    --model_path <teacher_model_path> \
    --build_cfg '{}' \
    --epochs 1 \
    --batch_size 2 \
    --device cpu \
    --out_ckpt <output_dir>/smoke_teacher.pth
```

Assert: stdout contains `TEACHER_CKPT:` + `TASK_LOSS_FINAL:`; the ckpt file
exists and `torch.load(...)` returns a dict with keys `state_dict`, `build_cfg`,
`variant_id`, `epochs`, `final_loss`, `mode` (mode == `"teacher"`).

**Distill mode smoke** requires a teacher_cache.pt. At gen_train_script time
the DAG has not trained a teacher yet, so build a **test cache** via
`kd.wrapper.TeacherCache.build` with the (untrained) teacher state dict
(in-repo precedent: `tests/workflows/test_kd_train_script.py`), or mark this
smoke explicitly `Skipped` if the cache cannot be constructed — never a
placeholder fallback. Tiny budget:

```bash
ORCA_KD_SCRIPTS_DIR=<kd_scripts_dir> \
python <output_dir>/train_pipeline.py \
    --mode distill \
    --student_model_path <student_variant_path> \
    --teacher_cache <test teacher_cache.pt> \
    --build_cfg '{"num_blocks": 3, "embed_dim": 16}' \
    --kd_config '{"kd_losses": ["mse"], "weights": {"mse": 1.0}}' \
    --epochs 1 \
    --batch_size 2 \
    --device cpu \
    --out_ckpt <output_dir>/smoke_student.pth \
    --variant_id smoke
```

Assert: stdout contains `STUDENT_CKPT:` + `KD_LOSS_FINAL:` + `KD_PROXY_MSE:`;
the ckpt file exists and `torch.load(...)` returns a dict with keys
`student_state_dict`, `variant_id`, `student_cfg`, `kd_config`, `epochs`,
`proxy_mse`, `mode` (mode == `"distill"`).

**Eval mode smoke** runs the real ported `user_eval_metric` on the teacher
smoke ckpt (read-only; teacher ckpt keys are tolerated):

```bash
ORCA_KD_SCRIPTS_DIR=<kd_scripts_dir> \
python <output_dir>/train_pipeline.py \
    --mode eval \
    --student_model_path <teacher_model_path> \
    --student_ckpt <output_dir>/smoke_teacher.pth \
    --build_cfg '{}' \
    --accuracy_baseline 1.5 \
    --accuracy_baseline_kind nmse \
    --device cpu
```

Assert: stdout contains `STUDENT_ACCURACY:` + `STUDENT_ACCURACY_KIND:` +
`MET_ACCURACY:` + `ACCURACY_CONFIDENCE:`; **no checkpoint file is written**
by eval mode (read-only).

### Layer 3: fidelity_check.py (numeric equivalence, mandatory)

Run `python <skill_dir>/scripts/fidelity_check.py` against the generated
artifact and the user's original code:

```bash
python <skill_dir>/scripts/fidelity_check.py \
    --train_pipeline <output_dir>/train_pipeline.py \
    --user_train <user_project_root>/train.py \
    --user_eval <discovered user eval script> \
    --dummy_input '{"shape": [1,4,48,64,1], "dtype": "float32"}' \
    --model_path <teacher_model_path> \
    --build_fn build_model --build_cfg '{}' \
    --project_root <user_project_root>
```

Must print `FIDELITY: PASS`. The script checks loss / dataloader / eval
metric numeric equivalence with the user's original code (fixed seed, same
tensors, `torch.allclose(rtol=1e-5)`), optimizer class-name equivalence, and
model I/O shape against `DUMMY_INPUT`. `FIDELITY: FAIL` (or exit 2) → fix
the artifact and re-run Layers 1-2.

### Layer 4: Cross-reference verifier subagent

Invoke the `workflow-verifier` subagent with:

- **Workflow doc** (read-only contract): `<skill_dir>/references/workflows/train_pipeline_script_generation.md`
- **Checklists** (verifier consumes these, read-only):
  - `<skill_dir>/references/workflow-checklists/train_pipeline_script_generation/01_training.md`
  - `<skill_dir>/references/workflow-checklists/train_pipeline_script_generation/02_cli.md`
- **Artifacts** (verifier may modify): `<output_dir>/train_pipeline.py` and any
  generated helper files.
- **Cross-references** (read-only): the user's original `train.py` and eval
  script under `<user_project_root>`; the KD-NAS contract
  `workflows/agents/_kd_scripts/CONTRACTS.md`.

The verifier checks: (a) the five fixed slots are all specialised with the
user's logic ported verbatim (zero placeholder residue — C21/C22/C23), (b)
CLI contract matches §1 (no `--user_*` flags), (c) checkpoint schemas match
§5/§6, (d) no DDP/sandwich/torchrun residue, (e) the kd library is used (not
`nas_agent.train.distillation`), (f) eval mode ports the user's eval metric
faithfully, emits the `STUDENT_ACCURACY` protocol, writes no ckpt, and uses
`kd_common.accuracy_direction` for direction (no symbol auto-guess), (g)
fidelity_check.py printed `FIDELITY: PASS` (C24).

Handle the verifier response:

- `all-pass` with no **Fixed** section → done.
- `all-pass` with a **Fixed** section → re-run Layer 2 smoke tests.
- `unresolved` → apply each suggested fix, re-run Layer 1 + Layer 2 + Layer 3.

## Forbidden

- Do not run the generated `train_pipeline.py` at full budget locally (smoke
  tests only — 1 epoch, 2-4 batches).
- Do not enable DDP / torchrun / torch.distributed.launch / launcher.sh.
- Do not add sandwich sampling, subnet switching, or `set_sample_config`.
- Do not import `nas_agent.train.distillation` — use `kd.compose` /
  `kd.wrapper` / `kd.ema`.
- Do not hardcode dataset paths, model paths, or ckpt paths — all CLI-driven.
- Do not skip Layer 2 smoke tests; they catch integration bugs the static
  checks miss.
- **Zero placeholder residue**: no `{{` literals, no `_placeholder_*` names,
  no `USER_TRAIN_MODULE` / `USER_EVAL_MODULE` constants, no
  `_load_user_train` / `_load_user_eval` runtime-loading code in the
  generated artifacts — unfilled slots must stay `NotImplementedError`
  (fail loud), never dummy fallbacks.
- **No `--user_*` flags**: the four removed flags
  (`--user_train_import` / `--user_loss_fn` / `--user_eval_import` /
  `--user_eval_fn`) must not reappear in the generated CLI.
- **No runtime loading of the user's project**: all user logic is ported in
  at generation time; a ported body that still imports user-project symbols
  is a fail-loud condition, not a runtime workaround.

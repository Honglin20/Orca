# Train Pipeline Script Generation Workflow (KD-NAS)

Use this workflow to generate `<output_dir>/train_pipeline.py` — the unified
KD-NAS training entry point supporting both **teacher** and **distill** modes
behind one CLI. The script is self-contained: user project code is **copied
in**, never imported.

This is the KD-NAS analogue of the NAS supernet-train-script workflow, with
these key differences (KD-NAS adaptations, must not regress):

1. **No sandwich sampling, no DDP, no torchrun.** KD-NAS is single-device +
   `--device` CLI; concurrency is handled at the workflow level via
   `train_pool` ThreadPoolExecutor round-robin binding, not inside this script.
2. **Models loaded by path** via `importlib.util.spec_from_file_location` —
   teacher and student share the same contract (`build_model` + `DUMMY_INPUT` +
   `KNOBS`, see `workflows/agents/_kd_scripts/CONTRACTS.md` §1).
3. **Two modes** in one script (teacher / distill), sharing the training
   infrastructure (optimizer / scheduler / dataloader / task_loss / loop
   skeleton); mode-specific code paths diverge only at loss + ckpt schema.
4. **KD loss** uses the existing KD-NAS library
   (`kd.compose.build_kd_loss` + `kd.wrapper.KDStudentWrapper` +
   `kd.wrapper.TeacherCache.load` + `kd.ema.MeanTeacherEMA`); **not** the NAS
   `nas_agent.train.distillation` helpers.
5. **Narrower user-train.py contract.** KD-NAS users only provide
   `compute_loss(s_out, y)` + `build_dataloader()` (see
   `examples/kd-nas-demo/train.py`); optimizer / scheduler default to
   `Adam` + none when absent, with an explicit fallback note.

## Source Evidence

Build the script from:

* **User train.py** under `<user_project_root>` — task loss, dataloader,
  optimizer/scheduler if present, batch format, model-call signature.
* **Teacher model** at `<teacher_model_path>` (e.g.
  `workflows/agents/_kd_scripts/teacher_model.py`) — exposes `build_model(**cfg)`,
  `DUMMY_INPUT`, `feature_hook_names()`.
* **Student variant** at `<student_model_path>` (KB receiver `.py`) — same
  contract.
* The reference template at
  `<skill_dir>/references/templates/train_pipeline.py` — a complete,
  smoke-testable gold example. **Start from this file**: copy it to
  `<output_dir>/train_pipeline.py` and specialise the placeholders.

Generated artifacts must be self-contained. Apart from the Python standard
library, installed third-party packages, and the KD-NAS ``kd/`` library
(provided by the workflow), generated artifacts must not import from
`<user_project_root>`. Copy any required user logic (dataset, loss, optimizer,
scheduler, dataloader) into `train_pipeline.py` or a sibling helper file under
`<output_dir>`.

## Generation Rules

### 1. CLI And Runtime Args

The reference template already exposes the full CLI contract. Preserve it
verbatim; only add project-derived arguments (dataset/config paths, training
budget, optimizer/scheduler hyperparameters) when the user's project requires
them.

Stable base CLI (must remain in every generated `train_pipeline.py`):

- `--mode {teacher,distill}` (required) — selects training mode.
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
- `--student_model_path PATH` (required in distill mode) — student `.py` path.
- `--teacher_cache PATH` (required in distill mode) — `teacher_cache.pt` from
  `teacher_setup.py`.
- `--kd_config JSON` (default `{"kd_losses": [], "weights": {}}`; distill mode).
- `--user_train_import STR` — overrides `USER_TRAIN_MODULE` placeholder.
- `--user_loss_fn STR` — overrides `USER_LOSS_FN` placeholder.
- `--project_root PATH` — prepended to `sys.path` for user-side
  `from <pkg> import <mod>` imports.
- `--env_anchor PATH` — BLK-5 ORCA env bootstrap anchor (per-run artifacts dir).

**No DDP / torchrun / world-size / local-rank flags.** Single device only.

### 2. Model Construction (by path)

Use `_load_model_by_path(model_path, build_fn, cfg)` from the reference
template verbatim. It:

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

### 3. User Task Loss + Dataloader (self-contained)

**Single strategy: path/module injection.** Set
`USER_TRAIN_MODULE = "/abs/path/to/train.py"` (or a module name reachable on
`sys.path`) and `USER_LOSS_FN = "compute_loss"`. The user's module is loaded
via `importlib.util.spec_from_file_location` (path form) or
`importlib.import_module` (module form). Add the user's project root to
`sys.path` via `--project_root` so their internal imports resolve.

The earlier "inline copy" strategy (sentinel `USER_TRAIN_MODULE =
"__inlined__"` + dispatch branch in `_load_user_train`) was **removed** in
favour of single-strategy simplicity (KISS): the inline approach required
the agent to modify both the placeholder *and* add a dispatch branch, which
is a contract-drift risk; path injection is a one-shot init load and covers
all cases. The reference template's `_load_user_train` raises loudly on
unknown module names — no silent sentinel dispatch.

The user's optimizer/scheduler/dataloader must still be **copied into
`train_pipeline.py` or a sibling helper file** under `<output_dir>`. Only
the loss-function *reference* is resolved by path injection — the loader
code is ported, not imported live at training time.

The dataloader must be **re-iterable**: each epoch's `iter(dl)` yields a fresh
batch stream. The placeholder fallback `_PlaceholderDataLoader` (a class with
`__iter__`/`__len__`) is the reference shape. If the user's `build_dataloader`
returns a one-shot generator, wrap it in a re-iterable adapter (or call
`build_dataloader()` at the start of every epoch).

### 4. Optimizer, Scheduler (user-port-or-fallback)

Reuse the user's optimizer and scheduler when they exist in `train.py`. When
absent (common for KD-NAS demo users), use the fallback:

```python
optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
# NOTE: no scheduler in fallback — KD-NAS teacher/student distillation is
# short-horizon; port the user's scheduler if present, do not invent one.
```

Annotate the fallback explicitly with a `# TODO(kd-train-script):` comment
marking where the agent would substitute the user's optimizer/scheduler. The
fallback must not invent hyperparameters not present in the user's project.

**Distill mode**: the optimizer parameter group must include both the student
parameters (`wrapper.parameters()`) **and** the KD adapter parameters
(`kd_loss.kd_parameters()`) so OFD/FitNets adapters train. This requires
materialising one batch, calling `wrapper(x0)` + `teacher(x0)` once under
`torch.no_grad`, and `kd_loss.prepare(s_feats0, t_feats0)` to pre-build the
adapters **before** constructing the optimizer. The reference template shows
the exact ordering.

### 5. Teacher Mode Loop

```python
teacher.train()
for epoch in range(args.epochs):
    for x, y in iter(dl):
        x, y = x.to(device), y.to(device)
        out = teacher(x)
        loss = user_loss(out, y)        # pure task loss — no KD in teacher mode
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
epochs (e.g. user `build_dataloader` returned an empty loader or a one-shot
generator that already exhausted), `last_avg` stays at its `float("nan")`
initialiser — raise `SystemExit` with a clear stderr message rather than
silently writing a NaN ckpt. A NaN teacher ckpt would propagate NaN through
`teacher_setup` → distill → `proxy_mse` with `returncode=0`, violating
CLAUDE.md Rule 12.

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
        # kd_loss calls user_loss(s_out, y) internally and adds KD terms.
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

Stdout keys (downstream `train_pool` worker parses these):
```
STUDENT_CKPT: <path>
KD_LOSS_FINAL: <float>
KD_PROXY_MSE: <float>
```

### 7. KD Loss Composition

Use `kd.compose.build_kd_loss(user_loss, kd_config)` to assemble the composite.
The composite owns OFD / FitNets adapters internally; their parameters land in
the optimizer via `kd_loss.kd_parameters()` (see §4).

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

The default `--kd_config` is `{"kd_losses": [], "weights": {}}` (task-loss
only) — the agent picks KD terms based on the user's task, never invents
exotic KD recipes.

### 8. Path Handling

Use `pathlib.Path` for all path construction (no `os.path.join`). All paths
must be CLI-overridable (no hardcoded literals). The reference template
already does this — preserve the pattern.

The output directory is created lazily via
`out_path.parent.mkdir(parents=True, exist_ok=True)` before `torch.save` —
do not pre-create directories outside the ckpt path.

### 9. Live Chart Push (best-effort sidecar)

The reference template includes `_make_live_push(variant_id, mode)` which
lazy-imports `orca.chart.render_chart` and degrades to no-op outside Orca.
Preserve this helper verbatim — it is the live-training-curve sidecar.
Push failure must never abort training (`try/except` → stderr warning).

## Validation

Generated artifacts are validated in three layers, run in order. Fix any
failure before invoking the verifier subagent.

### Layer 1: Static checks

- `python -m py_compile <output_dir>/train_pipeline.py` — must succeed.
- `python <output_dir>/train_pipeline.py --help` — must succeed and list all
  CLI flags. Verify the actual argparse block matches the documented stable
  base CLI.
- **CLI consistency**: every `--flag` the workflow expects (listed in §1) is
  accepted; no orphaned flags.

### Layer 2: Functional smoke tests (always)

Run both modes with a tiny budget on CPU. Both must complete without error
and produce the documented stdout keys.

**Teacher mode smoke** (uses placeholder fallback if `USER_TRAIN_MODULE`
unexpanded; otherwise points at the user's `train.py`):

```bash
ORCA_KD_SCRIPTS_DIR=<kd_scripts_dir> \
python <output_dir>/train_pipeline.py \
    --mode teacher \
    --model_path <teacher_model_path> \
    --build_cfg '{}' \
    --epochs 1 \
    --batch_size 2 \
    --device cpu \
    --out_ckpt <output_dir>/smoke_teacher.pth \
    --user_train_import <abs path to user train.py> \
    --user_loss_fn compute_loss
```

Assert: stdout contains `TEACHER_CKPT:` + `TASK_LOSS_FINAL:`; the ckpt file
exists and `torch.load(...)` returns a dict with keys `state_dict`, `build_cfg`,
`variant_id`, `epochs`, `final_loss`, `mode` (mode == `"teacher"`).

**Distill mode smoke** requires a teacher_cache.pt — produce one via
`teacher_setup.py` (or reuse an existing one). Tiny budget:

```bash
ORCA_KD_SCRIPTS_DIR=<kd_scripts_dir> \
python <output_dir>/train_pipeline.py \
    --mode distill \
    --student_model_path <student_variant_path> \
    --teacher_cache <teacher_cache.pt> \
    --build_cfg '{"num_blocks": 3, "embed_dim": 16}' \
    --kd_config '{"kd_losses": ["mse"], "weights": {"mse": 1.0}}' \
    --epochs 1 \
    --batch_size 2 \
    --device cpu \
    --out_ckpt <output_dir>/smoke_student.pth \
    --variant_id smoke \
    --user_train_import <abs path to user train.py> \
    --user_loss_fn compute_loss
```

Assert: stdout contains `STUDENT_CKPT:` + `KD_LOSS_FINAL:` + `KD_PROXY_MSE:`;
the ckpt file exists and `torch.load(...)` returns a dict with keys
`student_state_dict`, `variant_id`, `student_cfg`, `kd_config`, `epochs`,
`proxy_mse`, `mode` (mode == `"distill"`).

### Layer 3: Cross-reference verifier subagent

Invoke the `workflow-verifier` subagent with:

- **Workflow doc** (read-only contract): `<skill_dir>/references/workflows/train_pipeline_script_generation.md`
- **Checklists** (verifier consumes these, read-only):
  - `<skill_dir>/references/workflow-checklists/train_pipeline_script_generation/01_training.md`
  - `<skill_dir>/references/workflow-checklists/train_pipeline_script_generation/02_cli.md`
- **Artifacts** (verifier may modify): `<output_dir>/train_pipeline.py` and any
  generated helper files.
- **Cross-references** (read-only): the user's original `train.py` under
  `<user_project_root>`; the KD-NAS contract `workflows/agents/_kd_scripts/CONTRACTS.md`.

The verifier checks: (a) the generated script faithfully ports the user's
loss/dataloader/optimizer logic (no behaviour drift), (b) CLI contract
matches §1, (c) checkpoint schemas match §5/§6, (d) no DDP/sandwich/torchrun
residue, (e) the kd library is used (not `nas_agent.train.distillation`).

Handle the verifier response:

- `all-pass` with no **Fixed** section → done.
- `all-pass` with a **Fixed** section → re-run Layer 2 smoke tests.
- `unresolved` → apply each suggested fix, re-run Layer 1 + Layer 2.

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

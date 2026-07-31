# Checklist: Train Pipeline Script — CLI And Static Checks (KD-NAS)

Companion to: `workflows/train_pipeline_script_generation.md`

## How To Use

Each item below verifies CLI contract compliance and static-correctness of the
generated `train_pipeline.py`. Run after the Layer 1 static checks in the
workflow's Validation section. For items marked `auto-fixable: yes`, fix the
artifact directly. For `auto-fixable: no`, report the issue for the caller.

## Items

### [CRITICAL] 1. Stable Base CLI Present
**auto-fixable**: yes
**Section**: workflow §1 CLI And Runtime Args
**Check**: `train_pipeline.py` exposes the full stable base CLI:
`--mode`, `--out_ckpt`, `--epochs`, `--lr`, `--batch_size`, `--device`,
`--seed`, `--variant_id`, `--build_fn`, `--build_cfg`, `--model_path`,
`--student_model_path`, `--teacher_cache`, `--kd_config`,
`--user_train_import`, `--user_loss_fn`, `--project_root`, `--env_anchor`.
**Verify**: Run `python <output_dir>/train_pipeline.py --help`. Confirm every
flag above is listed with a compatible default. Confirm `--mode` is required
with `choices=["teacher", "distill"]`; `--out_ckpt` is required.
**Anti-pattern**: Missing `--mode` or `--out_ckpt` (required); `--device`
missing the `auto` default; `--mode` accepting values beyond teacher/distill.
**Fix**: Add the missing argparse entries.

### [CRITICAL] 2. --mode Required With Exact Choices
**auto-fixable**: yes
**Section**: workflow §1
**Check**: `--mode` is added with `required=True` and `choices=["teacher",
"distill"]`. No other choices are accepted.
**Verify**: grep `add_argument("--mode"`; inspect `required=` and `choices=`.
**Anti-pattern**: `--mode` optional with a default (downstream can't tell
which mode produced the ckpt); `choices` including legacy values like
`"train"` or `"finetune"`.
**Fix**: Set `required=True, choices=["teacher", "distill"]`.

### [CRITICAL] 3. Mode-Required Flags Enforced At Runtime
**auto-fixable**: yes
**Section**: workflow §1 (mode-specific required flags)
**Check**: When `--mode teacher`, `--model_path` must be provided; when
`--mode distill`, `--student_model_path` + `--teacher_cache` must be provided.
Enforced via `if not args.<flag>: raise SystemExit(...)` in the respective
mode function (argparse can't express conditional requirements cleanly).
**Verify**: Read `run_teacher_mode` and `run_distill_mode` openings. Confirm
the explicit presence checks.
**Anti-pattern**: Letting argparse handle it (it can't conditionally require);
silently defaulting to placeholder values (downstream crashes on None).
**Fix**: Add explicit `raise SystemExit` guards.

### [MAJOR] 4. Build Cfg JSON Parse
**auto-fixable**: yes
**Section**: workflow §1, §2
**Check**: `--build_cfg` is parsed as JSON via `json.loads(args.build_cfg)`.
Default is the string `"{}"` (parses to empty dict). Teacher and student share
this flag (teacher's `build_cfg` and student's `student_cfg`).
**Verify**: grep `json.loads(args.build_cfg)`. Confirm both modes parse it.
**Anti-pattern**: `eval(args.build_cfg)` (unsafe); treating build_cfg as a
string (breaks `build_model(**cfg)`); different flags for teacher vs student.
**Fix**: Use `json.loads` and share the flag.

### [MAJOR] 5. KD Config JSON Parse (Distill Mode Only)
**auto-fixable**: yes
**Section**: workflow §1, §7
**Check**: `--kd_config` is parsed as JSON only in distill mode (`json.loads(
args.kd_config)`). Default is `'{"kd_losses": [], "weights": {}}'` (task
loss only — no KD terms).
**Verify**: Read `run_distill_mode`. Confirm `json.loads(args.kd_config)`.
Confirm teacher mode does not parse it (would crash if user passes
`--mode teacher` without `--kd_config`).
**Anti-pattern**: Parsing kd_config at module load time (breaks teacher mode);
default enabling exotic KD terms.
**Fix**: Parse inside `run_distill_mode` only.

### [MAJOR] 6. Project Root sys.path Injection
**auto-fixable**: yes
**Section**: workflow §3 User Task Loss + Dataloader
**Check**: When `--project_root` is provided, it is inserted into `sys.path`
**before** `_load_user_train()` is called (so user-side `from <pkg> import
<mod>` resolves).
**Verify**: Read `main()`. Confirm the ordering: parse args → (optional)
env bootstrap → inject `args.project_root` → override placeholders → call
`_load_user_train()`.
**Anti-pattern**: Calling `_load_user_train()` before sys.path injection
(ImportError on user's internal imports).
**Fix**: Re-order in `main()`.

### [MAJOR] 7. Env Bootstrap Non-Fatal
**auto-fixable**: yes
**Section**: workflow (BLK-5 env anchor)
**Check**: `--env_anchor` triggers `_maybe_bootstrap_env(env_anchor)` which
wraps the import + call in try/except → stderr warning. Failure must not
abort training.
**Verify**: Read `_maybe_bootstrap_env`. Confirm try/except and stderr print.
**Anti-pattern**: Letting `load_run_env_from_artifacts` exceptions propagate
(kills training if env anchor is malformed).
**Fix**: Wrap in try/except.

### [CRITICAL] 8. py_compile Succeeds
**auto-fixable**: yes
**Section**: workflow Validation Layer 1
**Check**: `python -m py_compile <output_dir>/train_pipeline.py` exits 0.
**Verify**: Run the command; observe exit status.
**Anti-pattern**: Syntax errors; unbalanced braces.
**Fix**: Fix the syntax error.

### [CRITICAL] 9. --help Succeeds And Lists All Flags
**auto-fixable**: yes
**Section**: workflow Validation Layer 1
**Check**: `python <output_dir>/train_pipeline.py --help` exits 0 and prints
every documented flag in §1.
**Verify**: Run the command; cross-check the printed flags against §1.
**Anti-pattern**: argparse errors on `--help` (missing required args before
help is shown); silent typos in flag names.
**Fix**: Fix argparse definitions.

### [CRITICAL] 10. Mode Dispatch Correct
**auto-fixable**: yes
**Section**: workflow SKILL.md Workflow
**Check**: `main()` dispatches `args.mode == "teacher"` → `run_teacher_mode`,
else → `run_distill_mode`. The dispatch happens after placeholder override +
`_load_user_train()` so both modes get the resolved `(user_loss,
build_dataloader)`.
**Verify**: Read `main()`. Confirm the dispatch ordering.
**Anti-pattern**: Dispatching before resolving user_loss/build_dataloader
(each mode would have to re-resolve); switching on a string typo.
**Fix**: Re-order dispatch after `_load_user_train()`.

### [MAJOR] 11. Device Resolution
**auto-fixable**: yes
**Section**: workflow §1
**Check**: `--device` accepts `"auto"`, `"cuda"`, `"cpu"` (and any valid
`torch.device` string). `_resolve_device("auto")` returns cuda if available
else cpu. Used identically by both modes.
**Verify**: Read `_resolve_device`. Confirm both modes call it.
**Anti-pattern**: Hardcoding `cuda:0`; branching on `torch.cuda.is_available()`
inline in each mode (DRY violation).
**Fix**: Use the shared `_resolve_device` helper.

### [MINOR] 12. Out Ckpt Path Parent Created
**auto-fixable**: yes
**Section**: workflow §8 Path Handling
**Check**: `out_path.parent.mkdir(parents=True, exist_ok=True)` is called
before `torch.save(...)` in both modes.
**Verify**: grep for `mkdir(parents=True, exist_ok=True)`.
**Anti-pattern**: Assuming the output dir exists (crashes on first run);
pre-creating dirs the script doesn't write to.
**Fix**: Add the mkdir before torch.save.

### [MAJOR] 13. No Hardcoded Paths
**auto-fixable**: no
**Section**: workflow §8
**Check**: No hardcoded dataset paths, model paths, or ckpt paths in the
generated script. All paths come from CLI args or env vars.
**Verify**: Read the script; grep for absolute path literals like `"/home"`,
`"/tmp"`, `"C:\\"`, `"data/"`.
**Anti-pattern**: `DATA_DIR = "/path/to/dataset"`; `MODEL_PATH = "model.py"`.
**Fix**: Replace literals with CLI args.

### [MAJOR] 14. Dataloader Builder Signature Tolerant
**auto-fixable**: yes
**Section**: workflow §3
**Check**: `_build_dataloader(build_dataloader, batch_size)` tries
`build_dataloader()` first and falls back to `build_dataloader(batch_size=)`
on TypeError. The user contract permits either signature.
**Verify**: Read `_build_dataloader`.
**Anti-pattern**: Only supporting one signature (breaks the other); broad
`except Exception` (masks real errors in the builder body).
**Fix**: Use the narrow `except TypeError` pattern.

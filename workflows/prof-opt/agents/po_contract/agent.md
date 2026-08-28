---
description: Discover and empirically verify the three entry contracts (training / evaluation / export) that connect optimization variants to the user's original pipeline, adapt entries when a contract switch is missing, pin both training budgets (full-epoch fingerprint + probe stop depth k) and the checkpoint addressability, reject early-stopping projects at the gate, and render the parameterized run templates every downstream node executes.
tools: [bash, read, write, edit, glob, grep, task]
---
# po_contract

You are the **contract** folder-agent of the prof-opt pipeline. The flatten node built
a shadow copy of the model code; your job is to connect that shadow to the user's
ORIGINAL training / evaluation / export entries WITHOUT touching a single user file:

- discover how each entry is invoked and which contract switches exist
  (epochs / out-dir for training; checkpoint path for evaluation; output
  path + determinism for export);
- decide the adaptation tier per entry and, when switches are missing, port the
  user's entry into an adapted entry under the workspace (verbatim paradigm, only
  the missing switches parameterized);
- pin **two budgets** in `contracts.json` — `full_train_budget` (the full
  effective epoch count + seed: the value-level fingerprint the baseline
  full training, every variant render, and the winner full training must
  share) and `proxy_budget` (the variant stop depth k) — the single sources
  downstream nodes render verbatim (fairness invariant);
- pin the **checkpoint addressability** (`train.ckpt_output_rule` +
  `train.ckpt_per_epoch`) from measured quick-run behavior;
- **best-effort early-stopping rejection**: a project whose training stops
  before the rendered epoch count makes every epoch-aligned comparison
  unfair — detect what you can (argparse/config scan + quick-run
  observation) and reject with an honest attribution; what you cannot
  detect is caught strictly at the baseline final check;
- MEASURE everything you claim — a contract that was not exercised end-to-end is
  not a contract;
- leave behind `contracts.json` + four run templates, so downstream nodes only pick
  parameters and never hand-copy plumbing.

**Admission clause (this document is its single source)**: the workflow
description and the `reason` field of `contracts.json` both carry it —
训练须按给定轮数精确执行，自带 early-stopping 的项目不在本 workflow 范围。
Copy this sentence verbatim into the top-level `reason` you assemble; the
validation gate checks for it as a constant substring.

Everything runs through the deployed shared scripts at
`$ORCA_ARTIFACTS_DIR/scripts/` (assert_shadow / render_run /
gen_export_onnx / emit_result) — do NOT reference workflow source paths.

## Resource Anchors (cwd-independent)

- `$ORCA_AGENT_RESOURCES` (injected by the engine) = this agent's resources
  directory (`scripts/check_contracts.sh`).
- `$ORCA_ARTIFACTS_DIR` (injected by the engine) = the workspace root.
  **`cd` into it before running any command.**
- Upstream facts on disk (read, never re-derive): `readiness/readiness.json`
  (`python` = the working interpreter, `model_facts` = module/factory/args/kwargs/
  container_key/dummy_inputs), `project_manifest.md` (entry points, metric
  direction, data environment), `shadow/` + `shadow_pkgs`.
- `{{ inputs.project_root }}` (read-only), `{{ inputs.full_train_epoch_cap }}`
  (empty = uncapped), `{{ inputs.seed }}`. The probe stop depth k has no
  input: it is derived mechanically from the full training budget.

## Path Handling Iron Rules

All path construction in generated code must use `pathlib.Path` (preferred) or
`os.path.*`. Forbidden: string concatenation, f-strings, `+` for paths.

## Subagent Call Protocol (point-to-file)

This node calls ONE subagent, and only when a training or evaluation tier-B adapted
entry was produced: `paradigm-verifier`. Its body lives at
`{{ subagents_root }}/<name>.md` (inlined as an absolute path at render time).

To invoke `<name>` (first round):
`Task(subagent_type=<host built-in generic type>, prompt="First fully Read {{ subagents_root }}/<name>.md, strictly follow its Procedure for this round's task. This round's inputs: <specific inputs>. Return in the format the md specifies. The **first line of the report** must verbatim echo the sentinel field from the frontmatter of the md you Read (format at the top of the md; don't guess, don't infer from this prompt — it must come from the file you Read).")`

## Lazy Loading

Read only what a step needs: the entry files themselves, their imported local
dependencies, and the evidence files you produce. Do not re-read the shadow tree or
profiler docs.

## Workflow

Run the steps in order; keep a numbered todolist (0-9) in your reply. Re-execution
is at-least-once — every measurement writes an evidence file under `contract_work/`,
and re-entry reuses existing evidence instead of re-running measurements (each
evidence file records the inputs it was produced with; mismatch → re-measure).

### Step 0: Reuse Gate

```bash
export ORCA_PYTHON="$(python3 -c '
import json; from pathlib import Path
print(json.loads(Path("readiness/readiness.json").read_text(encoding="utf-8"))["python"])')"
bash "$ORCA_AGENT_RESOURCES/scripts/check_contracts.sh" --reuse-check
```

- Exit 0 (REUSE) → read the fields back out of `contracts.json` and go straight to
  Output (fill every output_schema field from disk, `error=""`).
- Exit 1 naming missing version fields ("predates the current workflow
  version") → the workspace was built by an older contract stage: fail loud
  with `viable=false` and `reason` quoting the fresh_start guidance (the
  contracts are never partially patched onto an old workspace).
- Exit 1 (sha drift) → continue with Step 1 (rebuild). Exit 2 → fail loud
  (`viable=false` + `error` naming the environment problem). Missing
  `readiness/readiness.json` means flatten did not complete → fail loud
  (`viable=false`, reason names it).

### Step 1: Snapshot The Project (pre-measurement)

Record the in-place state of the user project so every side effect of your dry-runs
can be disclosed later. Run this helper twice in this node — now as
`contract_work/snapshot_pre.json`, and again in Step 8 as
`contract_work/snapshot_post.json`:

```bash
python3 "$ORCA_AGENT_RESOURCES/scripts/snapshot_tree.py" \
  --root "{{ inputs.project_root }}" --out "$PWD/contract_work/snapshot_pre.json"
```

### Step 2: Train Contract (discovery + tier + quick-run)

1. **Locate** the training entry from the manifest / user request; open it and its
   local imports yourself. Extract the argparse (or equivalent) mapping for:
   epochs / out-dir, plus seed when supported.
   Also identify the **per-epoch metric log format**. The training contract is
   complete only when you can write a regular expression with named groups
   `epoch` and `metric` that extracts one metric for every completed epoch from
   the training log. Record it as `train.epoch_metric_extraction.pattern` and
   prove it against the epoch lines the quick-run (item 6) produces. The
   pattern MUST anchor the metric group with an end-of-line or non-digit
   boundary — a pattern that can stop mid-number (0.1234 truncated to 0.12)
   silently corrupts every curve; the gate functionally tests this on a
   canonical sample. If the project emits no per-epoch metric, adapt the
   logging cadence in a Tier-B entry without changing training behavior; if
   that is impossible, set `viable=false` with reason
   `per_epoch_metric_unavailable` — later epoch-aligned comparison must not
   silently compare different budgets.
2. **Checkpoint output rule + addressability.** Determine where the entry
   writes its checkpoint. A rule that embeds a fresh timestamp/random
   directory per run is NOT predictable → for tier A that is a contract
   failure (no predictable output path); port it away in a tier-B adapted
   entry (fixed filename under out-dir). The rule is recorded as
   `train.ckpt_output_rule` (a literal pattern with an `{out_dir}`
   placeholder; a trailing `*` glob means newest match). Addressability
   (`train.ckpt_per_epoch`, boolean) is decided by the quick-run's OBSERVED
   behavior in item 6: one checkpoint file per epoch (glob rule matching
   exactly N files for N epochs, in epoch write order) → `true`; a single
   rolling/overwritten file or an undecidable mix → `false`. When `true`,
   the k-th checkpoint is addressable as the k-th glob match in write
   order — downstream k-epoch evals depend on this, so it is measured,
   never assumed.
3. **Pin `train_epochs_full`** from the argparse default / constants — a mechanical
   fact, never a guess.
4. **Best-effort early-stopping scan** (admission clause enforcement at the
   gate): grep the entry + its config files for early-stopping mechanisms —
   an early-stopping / patience / min-delta / val-triggered break flag or
   config, a `break` out of the epoch loop on a validation condition, a
   scheduler-with-early-stop wrapper. THEN observe the quick-run (item 6):
   if the training ends before the rendered epoch count with a
   stop-condition message, that is the decisive evidence. Detected by
   EITHER scan or observation → `viable=false` with reason
   `early_stopping_detected: <mechanism + evidence>` — do not try to
   parameterize the stopping away; the workflow's admission clause is a
   scope boundary, not a repair target. Not detected → record
   `"early_stopping_check": "pass"` in the quick-run evidence (an honest
   best-effort, not a guarantee — the baseline final check catches what the
   scan missed).
5. **Interpreter flag scan.** Grep the entry (and its shebang / any `python`
   subprocess it spawns) for `-S` / `-E` interpreter flags. Found → `viable=false`
   with reason "interpreter flag -S/-E disables the shadow injection" (fail loud;
   there is no workaround in this pipeline). Otherwise
   `interpreter.flags_check = "pass"`.
6. **Tier decision:**
   - **Tier A** — epochs / out-dir all parameterized → template the user
     entry directly.
   - **Tier B** — some switches missing or the output rule is not predictable →
     write `adapted/train_proxy_entry.py`: a VERBATIM port of the user's
     training paradigm (loss / optimizer / scheduler / dataset & loader / metric /
     logging cadence) where the ONLY changes are (a) new CLI switches for the
     missing contract parameters (epochs / out-dir / seed), (b) path
     parameterization (out-dir, checkpoint path) replacing hardcoded
     values, (c) intra-workspace import adjustments
     needed to run from the workspace — the same default list
     paradigm-verifier judges against. Do not simplify, substitute, reorder, or drop anything
     behavioral. User files stay untouched — the adapted entry lives only in
     the workspace and imports the user's modules exactly as the original
     entry does.
   - **Tier C** — the training logic is so entangled that parameterizing it would
     change behavior → `viable=false`, reason `training_prerequisites_missing`,
     name the coupling in `reason`.
7. **Quick-run (mandatory measurement, ONE run, TWO uses).** Render the
   train template (Step 5 defines the file; you may write it now) with
   `epochs=2` (≥ 2 required — one run must prove BOTH the epoch-line format
   AND the checkpoint behavior), `out_dir=contract_work/quickrun_train/`,
   `seed={{ inputs.seed }}`, execute it, and classify into
   `contract_work/train_quickrun.json` as an object whose `"status"` key is
   exactly one of:
   - `"runs_minimal_budget"` — the 2-epoch run completed (EXPECTED good
     case: proves the entry executes under the injection header AND yields
     ≥ 2 epoch metric lines);
   - anything else → the entry cannot run: fix the adapted entry (tier B) or
     declare tier C. That includes a quick-run the project cannot afford at
     ≥ 2 epochs — `viable=false` with the cost named in the reason (fail
     loud, never downgrade: the 2-epoch run is the ONLY proof of the
     epoch-line format and the checkpoint behavior, and no weaker probe
     exists in this pipeline).
   Record in the same evidence file (the second use of the run):
   - `epoch_lines_matched`: the number of epoch metric lines the pattern
     extracted (must equal the rendered epoch count for a clean run —
     best-effort assert actual == rendered; a mismatch with no
     early-stopping message is investigated before proceeding);
   - `ckpt_files`: the checkpoint files that appeared under the out-dir
     (names + count) — the measured basis of `ckpt_per_epoch`;
   - `early_stopping_check`: `"pass"` or the detection from item 4;
   - `out_dir_effective` and `ckpt_output_example` (the concrete path the
     rule predicts for that out-dir — this is the rule downstream nodes
     rely on).

### Step 3: Eval Contract (discovery + tier + dual-checkpoint probe)

1. **Locate** the evaluation entry; extract the checkpoint path switch, the
   metric extraction rule (stdout regex, or output file + JSON key), and the
   metric direction (confirm the manifest's `higher-better`/`lower-better`
   against the code — accuracy-like → higher_better, loss-like →
   lower_better).
2. **Tier decision** exactly like Step 2 (tier B → `adapted/eval_entry.py`, same
   verbatim-port rule; tier C only when the metric cannot be extracted or the ckpt
   cannot be threaded).
3. **Dual-checkpoint probe (mandatory measurement).** The evaluation must actually
   LOAD the checkpoint you pass — prove it with two different checkpoints. Their
   source (mechanical, no training needed): **two random initializations of the
   model at different seeds** — two random inits differ enough that the eval
   metric moves when the entry truly loads the ckpt.

   1. Read the eval entry's checkpoint-loading code and record the container form
      it expects: `eval.ckpt_container` = `bare` (it loads a bare state_dict) or
      `wrapper:<key>` (it unwraps `ckpt["<key>"]` first).
   2. Write `contract_work/make_dual_ckpt.py` (English, pathlib, fail loud): read
      `model_facts` from `readiness/readiness.json`; for each seed in
      `0, 1`: `torch.manual_seed(seed)`; construct the model
      (`getattr(importlib.import_module(module), factory)(*args, **kwargs)`);
      save its `state_dict()` to
      `contract_work/random_init_s{seed}.pth` in the recorded container form
      (bare file = the state_dict itself; `wrapper:<key>` = a dict with ONLY that
      key carrying the state_dict — mirror exactly what the eval entry reads).
   3. Run it in the run-template form (the renderer injects the header + the
      shadow assertion — never hand-export PYTHONPATH). The shadow package
      list comes from `contracts.json` when already assembled, else from
      `readiness/readiness.json` (same list the flatten stage pinned):

      ```bash
      SHADOW_PKGS="$(python3 "$ORCA_AGENT_RESOURCES/scripts/shadow_pkgs_csv.py" \
        --artifacts "$ORCA_ARTIFACTS_DIR")"
      printf '%s\n' '<<python>> <<artifacts>>/contract_work/make_dual_ckpt.py' \
        > "$PWD/contract_work/make_dual_ckpt.template.sh"
      bash "$ORCA_ARTIFACTS_DIR/scripts/render_run.sh" \
        --template "$PWD/contract_work/make_dual_ckpt.template.sh" \
        --out "$PWD/contract_work/make_dual_ckpt.rendered.sh" \
        --set shadow_dir="$ORCA_ARTIFACTS_DIR/shadow" \
        --set shadow_pkgs="$SHADOW_PKGS" \
        --set project_root="{{ inputs.project_root }}" \
        --set python="$ORCA_PYTHON"
      bash "$PWD/contract_work/make_dual_ckpt.rendered.sh"
      ```
   4. Render the eval template twice (`contract_work/random_init_s0.pth` →
      `contract_work/eval_seed0.log`, `contract_work/random_init_s1.pth` →
      `contract_work/eval_seed1.log`), apply the recorded metric
      extraction to both logs, and write `contract_work/eval_dual_ckpt.json`:
      `{"metric_seed0": <number>, "metric_seed1": <number>, "moved": <bool>,
      "ckpt_container": "<bare|wrapper:key>", "metric_extraction": {...},
      "metric_direction": "..."}`. `moved=false` (identical
      metrics) proves the entry ignores the passed checkpoint → `viable=false`,
      reason "evaluation does not load the checkpoint parameter".

### Step 4: Export Contract (generate + measure)

1. **Existing user export script?** If the project has one and it (a) accepts an
   output path argument, (b) exports static shapes at opset 17 (verify by reading
   it, then by the measurement below), pin it: `export.entry = <abs path>`,
   `generated = false`, `argv_facts` = the pinned argv.
   Otherwise generate the canonical exporter:
   - write `contracts.json` containing AT LEAST `model_facts` (copied verbatim from
     `readiness/readiness.json` — module/factory/args/kwargs/dummy_inputs);
   - run `"$ORCA_PYTHON" "$ORCA_ARTIFACTS_DIR/scripts/gen_export_onnx.py"
     --contracts "$PWD/contracts.json" --out-dir "$ORCA_ARTIFACTS_DIR"`;
   - its stdout carries `{"script", "sha256"}` — those become
     `export.entry = <script>` / `export.entry_sha256` / `generated = true`.
     Determinism is pinned: static shapes, opset 17, fixed-seed dummy inputs,
     constant folding — never regenerate ad hoc variants of this script.
2. **Measure:** render the export template with `out=contract_work/export_probe.onnx`
   and `seed={{ inputs.seed }}`, execute, then `onnx.load` +
   `onnx.shape_inference` and verify all dims are static positive ints →
   `contract_work/export_check.json` `{"loaded": true, "opset": 17,
   "static_shapes": true, "entry": "<export entry>", "sha256": "..."}`. Load
   failure or dynamic dims → the export contract fails (`viable=false`).

### Step 5: Run Templates (write + validate by using them)

Write four templates under `templates/` — bodies contain ONLY the entry command
with `<<token>>` placeholders (never `{% raw %}{{token}}{% endraw %}` — this
prompt is Jinja2-rendered and such a token would be parsed as a prompt
variable); the renderer prepends the injection header and the shadow assertion,
so never hand-copy `PYTHONPATH`/env plumbing into a template:

- `templates/run_full_finetune.template.sh` — the ONE training pipeline:
  `<<python>> <train entry> <epochs flag> <<epochs>> <out flag> <<out_dir>> <seed flag> <<seed>>`
  (+ `<<vid>>` when the entry accepts an identifier). Variants are NOT
  rendered at a smaller epoch count — every training (baseline, every
  variant, the final winner) renders the SAME full effective epochs from
  THIS template; the variant probe depth is an EXTERNAL stop at epoch k,
  applied by the probe stage, never a template value. Training always uses
  the complete dataset and starts from the entry's own fixed-seed random
  initialization — there is NO checkpoint token and no data/truncation
  token.
- `templates/run_probe_finetune.template.sh` — kept as a second file for
  downstream naming compatibility; it MUST be byte-identical to
  `run_full_finetune.template.sh` (the validation gate asserts identity —
  one training pipeline, two names).
- `templates/run_eval.template.sh` — `<<python>> <eval entry> <ckpt flag> <<ckpt>> ... > <<log>> 2>&1`
  (metric extraction happens on `<<log>>` afterwards).
- `templates/export_onnx.template.sh` — `<<python>> <export entry> --out <<out>> --seed <<seed>>`
  (or the pinned user-script argv with the same tokens).

Tier-B entries point at `adapted/*.py`; tier-A at the user's original entry paths.
Validation of the templates IS the Step 2-4 measurements: every quick-run above must
have been produced by rendering these exact template files (not ad-hoc commands).

### Step 6: Injection Environment Disclosure

1. **User-owned `sitecustomize`.** The injection works by shipping our own
   `sitecustomize.py` FIRST on `PYTHONPATH` — which silently disables a
   `sitecustomize.py` the user relies on. Discover all of: `{{ inputs.project_root }}/sitecustomize.py`,
   the interpreter's user-site directory (`"$ORCA_PYTHON" -c "import site; print(site.getusersitepackages())"`
   → check for `sitecustomize.py` there), and site-packages. When found, MERGE by
   appending this chain block to `$ORCA_ARTIFACTS_DIR/orca_inject/sitecustomize.py`
   (workspace copy only — never the canonical source):

   ```python
   # Chained user sitecustomize discovered at contract time (merge disclosure):
   # our injection must not silently disable user environment behavior.
   import os as _u_os, runpy as _u_runpy
   _USER_SITECUSTOMIZE = r"<absolute path of the user's sitecustomize.py>"
   if _u_os.path.isfile(_USER_SITECUSTOMIZE):
       _u_runpy.run_path(_USER_SITECUSTOMIZE, run_name="sitecustomize_user")
   ```

   Record `sitecustomize_merge = {"found": true, "path": ..., "merged": true}` in
   contracts.json; not found → `{"found": false, "path": "", "merged": false}`.
   If the user's sitecustomize cannot execute under `runpy` (errors), that is a
   hard environment conflict → `viable=false` with the error quoted.
2. Re-run the eval dry-run once after any merge so the measured evidence reflects
   the merged injection.

### Step 7: Budget Selection (the two fairness fingerprints)

Mechanical, recorded in contracts.json (never re-derived downstream) —
downstream nodes render these values VERBATIM.

**`full_train_budget`** (the full-training value-level fingerprint — the
baseline full training, every variant render, and the winner full training
must carry IDENTICAL values):

- `epochs` = `min(int("{{ inputs.full_train_epoch_cap }}"),
  train_epochs_full)` when `{{ inputs.full_train_epoch_cap }}` is non-empty;
  else `train_epochs_full` (the project's full count, uncapped).
- `seed` = `{{ inputs.seed }}`.
- `data` = `{"dataset_knob": null, "data_value": null}` — always the null
  pair: full-data training is a pinned VALUE, not an omission (the gate
  enforces the pair; a recorded knob here would silently change the
  budget's meaning).

**`proxy_budget`** (the variant stop depth):

- `epochs` = k: `min(1, full_train_budget.epochs)` — mechanically derived
  from the full budget, with no user override. Variants render at
  `full_train_budget.epochs` and are stopped at epoch k externally — k only
  caps the comparison depth.
- `dataset_knob` / `data_value` / `max_steps`: always `null`.
- `seed` = `{{ inputs.seed }}` (same value as the full budget).

`probe_cap_mechanism` is always `"stop-at-k"`.

Write the selection rationale to `contract_work/proxy_budget_selection.json`:
`{"probe_k": <k>, "full_epochs": <effective full epochs>,
"rationale": "stop-at-k fairness invariant: full-epoch render + external
stop at k; full_train_budget is the value-level fingerprint"}`.

### Step 8: Post-Snapshot + Exemptions

Re-run the Step 1 helper with `--out contract_work/snapshot_post.json`, then:

```bash
python3 "$ORCA_AGENT_RESOURCES/scripts/snapshot_diff.py" \
  --pre contract_work/snapshot_pre.json --post contract_work/snapshot_post.json \
  --out contract_work/exemptions.json
```

`exemptions` lists every file your measurements touched inside the user project —
disclose them as-is (they are inputs to the final report; hiding one is a
validation failure).

### Step 9: paradigm-verifier (tier B only)

For EVERY tier-B adapted entry, call `paradigm-verifier` with inputs: the user
source scope (original entry path + the local files its paradigm lives in), the
adapted entry path, the allowed-adaptations list from Step 2, and the report
path — ONE FILE PER ENTRY, `$ORCA_ARTIFACTS_DIR/verify/paradigm_verifier_report_train.md`
for the train entry, `..._eval.md` for the eval entry (a shared name would let
the second call overwrite the first entry's audit artifact; both must stay on
disk). The verifier writes the report there — first line = its sentinel. Then
mechanically prove the review happened (a report only in the Task return value
does not count):

```bash
REPORT="$ORCA_ARTIFACTS_DIR/verify/paradigm_verifier_report_<train|eval>.md"
[ -s "$REPORT" ] && [ "$(head -n 1 "$REPORT")" = "[subagent:paradigm-verifier v1 PV8RK2]" ] || {
  echo "FATAL: paradigm-verifier report missing or sentinel mismatch at $REPORT — treat as NOT reviewed" >&2
  exit 1; }
```

A missing file or sentinel mismatch → the entry is treated as **not
reviewed**: re-invoke the verifier once; still bad → `viable=false` with
`error` naming the report path (fail loud). Check and read EACH entry's
report before moving to the next entry. Report body handling:

- `pass` → done;
- `fail` → fix the adapted entry per the findings (one round), re-run the relevant
  dry-run measurement, and call the verifier again. Second `fail` → tier C:
  `viable=false`, reason = "adapted entry failed the paradigm fidelity review
  twice: <top findings>". Never weaken the checklist to pass.

### Validation (gate)

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/check_contracts.sh" \
  || { echo "FAIL" >&2; exit 1; }
```

Fix-loop ≤ 3 iterations; exceeded → `viable=false` + `error` naming the stuck
check.

### Final contracts.json assembly

One JSON object at `$ORCA_ARTIFACTS_DIR/contracts.json` — assemble it from the
evidence files (deterministic snippet or a small python script you write under
`contract_work/`), with exactly this shape:

```json
{
  "viable": true,
  "reason": "<tier decision + measurement summary; MUST contain the admission clause sentence verbatim — 训练须按给定轮数精确执行，自带 early-stopping 的项目不在本 workflow 范围>",
  "interpreter": {"sys_executable": "<abs>", "flags_check": "pass"},
  "shadow": {"shadow_root": "<abs>", "shadow_pkgs": ["..."]},
  "model_facts": {"module": "...", "factory": "...", "args": [], "kwargs": {},
                  "container_key": null, "dummy_inputs": [{"name": "...", "shape": [], "dtype": "float32"}]},
  "train": {"tier": "A", "entry": "<abs entry or adapted entry>", "entry_sha256": "...",
            "flags": {"epochs": "--epochs",
                      "out_dir": "--out-dir", "seed": "--seed"},
            "ckpt_output_rule": "<literal pattern under out-dir, with an {out_dir} placeholder; a trailing * glob means newest match; per-epoch addressable forms are per-epoch glob patterns>",
            "ckpt_per_epoch": true,
            "epoch_metric_extraction": {"kind": "stdout_regex", "pattern": "<named groups epoch and metric; the metric group anchored by an end-of-line/non-digit boundary>"},
            "train_epochs_full": <int>},
  "eval": {"tier": "A", "entry": "<abs>", "entry_sha256": "...",
           "flags": {"ckpt": "--ckpt"},
           "ckpt_container": "bare",
           "metric_extraction": {"kind": "stdout_regex|json", "pattern": "...", "json_pointer": "..."},
           "metric_direction": "higher_better"},
  "export": {"entry": "<abs>", "entry_sha256": "...", "generated": true, "argv_facts": "..."},
  "full_train_budget": {"epochs": <int>, "seed": 0,
                        "data": {"dataset_knob": null, "data_value": null}},
  "proxy_budget": {"epochs": 1, "dataset_knob": null, "data_value": null,
                   "max_steps": null, "seed": 0},
  "probe_cap_mechanism": "stop-at-k",
  "exemptions": [],
  "sitecustomize_merge": {"found": false, "path": "", "merged": false}
}
```

The `reason` admission clause is copied VERBATIM from the Admission clause
paragraph near the top of this document (this document is its single
source; the gate checks the constant substring). (`model_facts` verbatim from
`readiness/readiness.json`; sha256 fields recomputed at assembly time via
hashlib — the gate re-checks them.)

## Guidelines

- User files are read-only. All writes stay inside `$ORCA_ARTIFACTS_DIR`.
- Every claim in contracts.json must trace to a `contract_work/` evidence file.
- Generated Python (adapted entries, helpers): English identifiers/comments,
  pathlib, fail loud on bad inputs.
- All logs to stderr; script stdout stays machine-readable single-line JSON.

## Output (output_schema mandates JSON)

Your ENTIRE final reply = exactly one line of valid JSON (no prose, no fences) —
run the emitter and reply with its stdout verbatim:

```bash
"$ORCA_PYTHON" "$ORCA_ARTIFACTS_DIR/scripts/emit_result.py" \
  --field viable=true \
  --field reason="<tier + measurement summary>" \
  --field contracts_path="$ORCA_ARTIFACTS_DIR/contracts.json" \
  --field run_probe_script="$ORCA_ARTIFACTS_DIR/templates/run_probe_finetune.template.sh" \
  --field run_full_script="$ORCA_ARTIFACTS_DIR/templates/run_full_finetune.template.sh" \
  --field run_eval_script="$ORCA_ARTIFACTS_DIR/templates/run_eval.template.sh" \
  --field export_script="$ORCA_ARTIFACTS_DIR/templates/export_onnx.template.sh" \
  --field metric_direction=higher_better \
  --field train_epochs_full=<int> \
  --field proxy_budget='{"epochs": <k>, "dataset_knob": null, "data_value": null, "max_steps": null, "seed": <int>}' \
  --field probe_cap_mechanism="stop-at-k" \
  --field exemptions='[...]' \
  --field error="" \
  --field generated_artifacts='["contracts.json", "templates/", "adapted/", "contract_work/", "verify/paradigm_verifier_report_train.md", ...]'
```

On `viable=false` (tier C / dual-ckpt failure / flag conflict): same emitter with
`viable=false`, `reason` carrying the root cause, all `*_script` fields `""`,
`train_epochs_full=0`,
`proxy_budget='{"epochs": 0, "dataset_knob": null, "data_value": null, "max_steps": null, "seed": 0}'`,
`probe_cap_mechanism=""`, and `error` restating the root cause. `metric_direction`
still reports what was measured (or `higher_better` when nothing ran).

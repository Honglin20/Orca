# Variant Implementation Protocol

Per-proposal procedure for building a model variant. Every path is relative
to the workspace root (`$ORCA_ARTIFACTS_DIR`) unless stated absolute. `VID`
is the proposal's vid, `RRR` the zero-padded round number.

Angle-bracket placeholders (`<seed>`, `<project-root>`) are runtime values
from your node prompt's input anchors — substitute the actual values when you
run the commands.

## Directory layout produced per variant

```
variants/<VID>/
├── shadow/               # edited copy of the base shadow (this variant only)
├── declaration.json      # what was changed, machine-checkable
├── DONE                  # marker: export + file-layer pre-check passed
├── onnx/model.onnx       # exported from THIS variant's shadow
└── onnx/export.log
```

The DONE marker exists ONLY when steps 5-6 below all passed. A variant
without DONE is either in progress or terminally skipped; the latency node
ignores it either way. Training artifacts are NOT produced here — every
variant trains later from a fixed-seed random initialization.

## declaration.json schema (pinned)

```json
{
  "vid": "r1-01",
  "round": 1,
  "seq": 1,
  "change_sig": "<from the proposal, verbatim>",
  "lever": "activation",
  "change_spec": "<from the proposal, verbatim>",
  "target_modules": ["blocks.0.mlp.act"],
  "op_delta": {"Erf": -4, "Relu": 4},
  "edited_files": ["pkg/model.py"],
  "predicted_delta_cycles": -3792,
  "prediction_basis": "<from the proposal, verbatim>"
}
```

Copy proposal fields verbatim — the declaration is the machine-checked mirror
of the proposal, not a re-derivation.

## Step-by-step

### 1. Skip checks (idempotent re-entry)

- `variants/<VID>/DONE` exists → already complete — UNLESS the
  `declaration_sha256` recorded inside the DONE marker disagrees with the
  sha256 of the current `declaration.json` (that means the declaration was
  edited after the marker: fail loud, do not silently reuse). On agreement,
  skip to the next proposal (count as implemented; write nothing).
- `history.jsonl` contains a row with this vid (check by grepping the vid)
  but no DONE exists → the variant was terminally skipped in an earlier
  attempt. Read that vid's latest row: if it carries a terminal outcome
  (`structural_mismatch` / `variant_broken`), count it under `skipped` with
  that outcome and write nothing; if it carries `implemented=false` with no
  outcome yet (a crash between the two appends), append the missing outcome
  row per the classification below and count it under `skipped`.
- Otherwise proceed.

### 2. Fresh variant shadow

```bash
rm -rf "$ORCA_ARTIFACTS_DIR/variants/$VID"
python3 -c "import shutil; shutil.copytree(
    '$ORCA_ARTIFACTS_DIR/shadow', '$ORCA_ARTIFACTS_DIR/variants/$VID/shadow',
    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))"
```

### 3. Edit the model source

- Edit ONLY files listed in `edited_files`, ONLY inside
  `variants/<VID>/shadow/`. The base shadow and the user project are never
  touched.
- Surgical edits: replace exactly the structures `change_spec` describes.
  Preserve the public interface (constructor signature, forward input/output
  shapes) — this workflow changes internals, not the calling contract.
- Parameters may change freely (train-from-scratch: nothing inherits
  weights) — but keep the change exactly as declared (`edited_files` +
  `op_delta`), nothing more.
- New/changed identifiers, comments, and docstrings in edited code: English.
  Any helper path handling: `pathlib`.
- After editing, re-read the changed region and confirm each declared site
  was applied.

### 4. Write declaration.json (schema above)

### 5. Export the variant onnx

The export template is
`$ORCA_ARTIFACTS_DIR/templates/export_onnx.template.sh` (tokens: `out` /
`seed`). Read it once to confirm its tokens, then render with the shared
renderer — the renderer assembles the injection header, the runtime shadow
assertion and the pinned interpreter, you only choose parameter values:

```bash
mkdir -p "$ORCA_ARTIFACTS_DIR/variants/$VID/onnx"
bash "$ORCA_ARTIFACTS_DIR/scripts/render_run.sh" \
  --template "$ORCA_ARTIFACTS_DIR/templates/export_onnx.template.sh" \
  --out "$ORCA_ARTIFACTS_DIR/variants/$VID/onnx/export.rendered.sh" \
  --set "out=$ORCA_ARTIFACTS_DIR/variants/$VID/onnx/model.onnx" \
  --set "seed=<seed>" \
  --set "shadow_dir=$ORCA_ARTIFACTS_DIR/variants/$VID/shadow" \
  --set "shadow_pkgs=$(python3 -c "import json; print(','.join(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['shadow']['shadow_pkgs']))")" \
  --set "project_root=<project-root>" \
  --set "python=$(python3 -c "import json; print(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['interpreter']['sys_executable'])")"
```

`shadow_pkgs` comes from `contracts.json` (`shadow.shadow_pkgs` — the list
the prepare stage pinned; the variant copy preserves the same tree). Then
run it:

```bash
bash "$ORCA_ARTIFACTS_DIR/variants/$VID/onnx/export.rendered.sh" \
  > "$ORCA_ARTIFACTS_DIR/variants/$VID/onnx/export.log" 2>&1 || true
```

Non-zero exit or `onnx/model.onnx` absent → **variant_broken path** (below).

### 6. File-layer pre-check (declaration vs reality)

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/diff_check.py" --layer file \
  --base-shadow "$ORCA_ARTIFACTS_DIR/shadow" \
  --variant-shadow "$ORCA_ARTIFACTS_DIR/variants/$VID/shadow" \
  --edited-files '<JSON list copied from declaration.edited_files>'
```

Exit 0 = declared edits match the real diff. Exit 1 = mismatch (a legitimate
verdict: the edited file set does not equal the declared set — read the
JSON on stdout to see `not_declared` / `declared_but_absent`). Exit 1 →
**structural_mismatch path**. Exit ≥ 2 = hard error → fail loud.

### 7. DONE marker + history row

```bash
python3 -c "import hashlib, json, datetime; from pathlib import Path; \
d = Path('$ORCA_ARTIFACTS_DIR/variants/$VID'); \
decl = (d / 'declaration.json').read_text(encoding='utf-8'); \
(d / 'DONE').write_text(json.dumps({'vid': '$VID', \
'declaration_sha256': hashlib.sha256(decl.encode()).hexdigest(), \
'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}), encoding='utf-8')"
```

Then append the implemented history row (the ONLY first-version row a vid
ever gets; `parent_vid` = the vid recorded in `best.json` or null, and
`base_at_proposal` = `{"vid": <same>, "makespan_cycles": <base
bottleneck-report makespan>}`):

```bash
python3 -c "import sys; sys.path.insert(0, '$ORCA_ARTIFACTS_DIR/scripts'); \
from history_lib import append_implemented; \
append_implemented('$ORCA_ARTIFACTS_DIR/history.jsonl', '$VID', \
round=<R>, seq=<seq>, parent_vid=<best vid or None>, \
change_sig='''<sig>''', probe_epochs=<proxy_budget.epochs from contracts.json>, \
probe_max_steps=<proxy_budget.max_steps from contracts.json, or None>, \
probe_data_value=<proxy_budget.data_value from contracts.json, or None>, \
target_modules=<list>, predicted_delta_cycles=<int>, \
base_at_proposal={'vid': <best vid or None>, 'makespan_cycles': <int>}, \
implemented=True)"
```

## Terminal-skip paths (no DONE marker)

Both paths append history rows and record a `skipped` entry; neither blocks
the round. The variant directory is left as-is for diagnosis.

**structural_mismatch path** (file-layer pre-check failed): the edited-file
set disagrees with the declaration. Append the implemented row first with
`implemented=False` (same arguments as step 7), then:

```bash
python3 -c "import sys; sys.path.insert(0, '$ORCA_ARTIFACTS_DIR/scripts'); \
from history_lib import append_outcome; \
append_outcome('$ORCA_ARTIFACTS_DIR/history.jsonl', '$VID', 'structural_mismatch')"
```

**variant_broken path** (export failed / the edit could
not be made to match the declaration after honest attempts): same two-step
append, with `'variant_broken'` as the outcome. If the edit itself is
unachievable (the change does not fit the real source structure), use this
path with the reason recorded in the `skipped` entry.

Failure classification is fixed: `structural_mismatch` is reserved for the
file-layer verdict; every other failure here is `variant_broken`. The
two-layer judgement authority stays with the latency node — this protocol
only pre-checks the file layer.

## Reconciliation (re-entry after a crash between marker and history)

For every `variants/<VID>/DONE` whose vid has no row in `history.jsonl`:
read `declaration.json`, reconstruct `round`/`seq` from the vid
(`r{round}-{seq}`), and append the implemented row exactly as step 7
(`parent_vid`/`base_at_proposal` from `best.json` + the current base
bottleneck report — within a round the base cannot have moved, so these are
the values the lost row would have carried).

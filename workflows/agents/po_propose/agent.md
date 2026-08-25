---
description: Generate structure-level optimization proposals for the current base model from the refreshed bottleneck report and the optimization playbook, mechanically deduplicated against run history and admitted only with a strictly negative predicted cycle delta.
tools: [bash, read, write, edit, glob, grep, task]
---
# po_propose

You are the **proposal generation** node of the optimization loop. The loop
re-enters you every round, so everything is derived from disk and every write
is safe to re-derive. Each entry you:

1. refresh the bottleneck report of the CURRENT base model;
2. derive candidate structure changes from the playbook against the report's
   hot patterns, deduplicated mechanically against run history;
3. admit candidates through the mechanical admission checks and write this
   round's `rounds/<NNN>/proposals.json`.

Zero proposals in a round is a legitimate outcome (`exhausted=true`), not a
failure.

## Resource Anchors (cwd-independent)

- `$ORCA_ARTIFACTS_DIR` (injected by `orca spawn`) = this run's workspace.
  **`cd "$ORCA_ARTIFACTS_DIR"` before running any command.**
- `$ORCA_AGENT_RESOURCES` (injected by `orca spawn`) = this agent's resources
  directory; the playbook lives at `$ORCA_AGENT_RESOURCES/references/playbook.md`.
- Per-round proposal quota is a fixed constant: keep at most **4** proposals
  per round (training-budget control, not a user input).
- Shared deterministic scripts are deployed at `$ORCA_ARTIFACTS_DIR/scripts/`
  by the entry node. Use only that path. Guard before anything else:
  ```bash
  cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: workspace unreachable" >&2; exit 2; }
  for f in analyze.py predict_delta.py history_lib.py experiment_ledger.py emit_result.py; do
    [ -f "$ORCA_ARTIFACTS_DIR/scripts/$f" ] || {
      echo "FATAL: scripts/$f not deployed — entry stage incomplete" >&2; exit 2; }
  done
  ```
  Missing deployed scripts = the entry stage failed = fail loud (exit 2), do
  not look for the scripts anywhere else.

## Path Handling Rules

All path construction in any helper code you write must use `pathlib.Path`
(or `os.path.*`). Forbidden: string concatenation, f-strings, and `+` for
paths.

## Subagent Call Protocol

This node dispatches **no subagents**. All work is done directly.

## Lazy Loading

Do not pre-read reference files. Read
`$ORCA_AGENT_RESOURCES/references/playbook.md` only when Step 3 begins; read
shadow source files only for the modules a candidate actually targets.

## Workflow

Run the steps in order. Keep a numbered markdown checklist (0-5) of progress
in intermediate replies (your FINAL reply is JSON only).

### Step 0: Reuse-check (idempotent re-entry)

Determine the target round number `R` from disk, never from memory:

- `cur` = the maximum numeric directory name under `rounds/` (0 when `rounds/`
  does not exist).
- If `.round_advanced` exists and its `"round"` equals `cur` → `R = cur + 1`
  (the previous round fully advanced; start a fresh round).
- Otherwise → `R = max(cur, 1)` (resume the current round).

**Reuse guard**: if `rounds/<RRR>/proposals.json` exists AND parses as JSON
(`RRR` = `R` zero-padded to 3 digits) → this round's proposals are already on
disk from an earlier attempt. Do NOT regenerate, do NOT skip the round
number, do NOT rewrite the file. Run Step 1 (analyze is idempotent), run
Step 2 to rebuild the experiment ledger, skip Steps 3-4, and emit the Output
directly with the counts read from the existing file.

If the file exists but does not parse, treat the round as fresh (log to
stderr) and regenerate.

### Step 1: Refresh the bottleneck report

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/analyze.py" \
  --profile-dir "$ORCA_ARTIFACTS_DIR/base/profile"
```

Fail loud (exit 2) if it exits non-zero — without the report there is nothing
to reason from. The report lands at `base/bottleneck_report.json` (fixed
output location). Read it; the fields you consume are `makespan_cycles`,
`hot_patterns` (pattern ids, op types, counts, cycle shares, onnx node
names), `cost_table`, `critical_path`, `pipeline_breakdown`.

### Step 2: Read run-history dedup inputs

- `probe_epochs` = `contracts.json` `proxy_budget.epochs` (the effective
  proxy epoch count the contract stage pinned; read it from disk).
- `probe_max_steps` = `contracts.json` `proxy_budget.max_steps` VERBATIM —
  an integer, or null when the project's training entry has no step-truncation
  mechanism (null is a pinned budget value, not "unset"; never substitute the
  raw workflow input here — the dedup fingerprint must match what the
  variants actually trained with).
- `probe_data_value` = `contracts.json` `proxy_budget.data_value` verbatim
  (null when no data knob exists).
- The history file is `history.jsonl` at the workspace root (may be absent on
  round 1 → every dedup query returns not-blocked).

Rebuild and read the compact experiment memory:

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/experiment_ledger.py" \
  --artifacts "$ORCA_ARTIFACTS_DIR"
```

`experiment_ledger.json` is the required summary of every prior proposal:
what changed, predicted versus measured latency, prediction error, accuracy
result, verdict, and the deterministic `next_hint`. A new proposal must either
use a new signature or explicitly incorporate the relevant `next_hint`; never
repeat a failed change unchanged.

### Step 3: Derive candidates (playbook x evidence x source)

Read the playbook now. For each playbook entry whose trigger evidence matches
a hot pattern with a meaningful critical-path share:

1. Open the shadow source (`shadow/`) around the modules behind the listed
   onnx node names; count the sites the entry would touch; list the target
   module paths (`target_modules`) and the file(s) to edit (`edited_files`,
   paths relative to the shadow root).
2. Derive the op delta: per-site removal + insertion pattern (verified
   against `base/model.onnx` — count the actual ops around those node names)
   x site count.
3. Predict (never by hand). Derive every affected site's shape class from
   the actual profile — the site's op row in `base/profile/taskgraph.json`
   (element count = product of `output_dimensions`, bucketed by the
   cost_table's shape-class labels) — and pass the per-site classes so the
   predictor prices the actual shape-class rows (a change touching small
   sites must never be priced at the big sites of the same op type):
   ```bash
   python3 "$ORCA_ARTIFACTS_DIR/scripts/predict_delta.py" \
     --report "$ORCA_ARTIFACTS_DIR/base/bottleneck_report.json" \
     --op-delta '{"Erf":-4,"Tanh":-4,"Mul":-8,"Add":-4,"Relu":4}' \
     --sites '{"Erf":["<1e2","<1e2","<1e2","<1e2"],"Tanh":["<1e2","<1e2","<1e2","<1e2"],"Mul":["<1e2","<1e2","<1e2","<1e2","<1e2","<1e2","<1e2","<1e2"],"Add":["<1e2","<1e2","<1e2","<1e2"],"Relu":["<1e2","<1e2","<1e2","<1e2"]}' \
     [--added-cost 'Relu=<cycles from the closest same-class cost_table row>']
   ```
   `--sites` lists one shape class per affected op instance for EVERY op in
   the delta (its length per op must equal the op's |delta| — an op left out
   of `--sites` falls back to worst-case whole-op pricing, which over-prices
   removals and wastes an admission slot). Omit `--sites` only when a site's
   shape
   is genuinely unobtainable — the predictor then prices that op at the sum
   of all its shape-class rows (a worst-case bound). stdout is
   `{"predicted_delta_cycles": N, "params": "...", "basis": [...]}`.
   Non-zero exit → the candidate is dropped (log the reason to stderr). If
   you used an `--added-cost` override, the derivation (which cost_table row,
   why same class) goes into `prediction_basis`.
4. Build the canonical signature (never hand-assemble):
   ```bash
   python3 -c "import sys; sys.path.insert(0, '$ORCA_ARTIFACTS_DIR/scripts'); \
from predict_delta import build_change_sig; \
print(build_change_sig('activation', '<params from the predictor stdout>', \
['blocks.0.mlp.act', 'blocks.1.mlp.act']))"
   ```
   Lever ids: `activation` / `normalization` / `compute_relocation`.
5. Dedup against history (mechanical):
   ```bash
   python3 "$ORCA_ARTIFACTS_DIR/scripts/history_lib.py" \
     --history "$ORCA_ARTIFACTS_DIR/history.jsonl" \
     --sig '<signature>' --probe-epochs <effective> \
     --probe-max-steps <proxy_budget.max_steps, or null> \
     --probe-data-value <proxy_budget.data_value, or null>
   ```
   `"blocked": true` → count this candidate into `filtered_count` (one per
   blocked signature) and drop it. Keep the reason for the stderr log only.
6. Admission checks (all must hold, playbook checklist):
   - `predicted_delta_cycles < 0` strictly;
   - every `edited_files` entry exists under `shadow/`;
   - op delta ⊕ change description consistency: per-site pattern x site count
     equals the declared op delta, and the description says exactly that.
   - `expected_accuracy_impact`, `accuracy_confidence`, `accuracy_evidence`,
     and `sota_ref` are present and use the playbook's closed vocabulary.

### Step 4: Rank and select on the latency/accuracy Pareto front

Apply the playbook's Pareto contract. Discard candidates dominated by another
candidate with no worse expected accuracy risk and a larger predicted
reduction. Rank non-dominated candidates by ledger evidence and prediction
quality, then larger predicted reduction and lower accuracy risk. A candidate
whose ledger row has `latency_fail` or `probe_insufficient` is eligible only
when its new spec incorporates the recorded `next_hint`.
Keep at most **4** proposals.

Assign vids in ranked order: `r{R}-{seq:02d}` (seq starts at 1).

### Step 5: Write proposals.json

Write `rounds/<RRR>/proposals.json` (create the directory) with EXACTLY this
top-level shape:

```json
{"round": R, "exhausted": false, "proposals": [...], "filtered_count": N}
```

- `exhausted` = (number of admissible candidates == 0) — a mechanical count
  of what survived Steps 3-4, never a verbal judgement.
- `filtered_count` = number of candidates blocked by the history dedup rules
  in Step 3.5.
- Each proposal item:
```json
{"vid": "r1-01", "lever": "activation", "change_sig": "<canonical>",
 "target_modules": ["blocks.0.mlp.act"], "target_pattern_id": "P2",
 "pattern_evidence": "<one line: which hot pattern rows, what share>",
 "change_spec": "<precise edit description, per-site pattern x site count>",
 "op_delta": {"Erf": -4, "Relu": 4},
 "predicted_delta_cycles": -3792,
 "prediction_basis": "<predictor basis summary + any override derivation>",
 "accuracy_risk": "medium",
 "expected_accuracy_impact": "small_negative",
 "accuracy_confidence": "medium",
 "accuracy_evidence": "<prior ledger rows and/or playbook evidence>",
 "sota_ref": "<playbook reference names>",
 "edited_files": ["pkg/model.py"]}
```

## Validation

Re-read `rounds/<RRR>/proposals.json` and verify mechanically:

- parses as JSON; `round` equals `R`;
- every proposal: `predicted_delta_cycles < 0`; every `edited_files` entry
  exists under `shadow/`; `op_delta` values are non-zero integers;
  `expected_accuracy_impact`, `accuracy_confidence`, `accuracy_evidence`, and
  `sota_ref` are non-empty.

Fix and re-validate on failure (fix-loop ≤ 3); still failing → fail loud
(exit 2, `error` states what is stuck).

## Output

The entire final reply = the single line of JSON printed by:

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/emit_result.py" \
  --field "proposals_count=<len(proposals)>" \
  --field "exhausted=<true|false>" \
  --field "proposals_path=$ORCA_ARTIFACTS_DIR/rounds/<RRR>/proposals.json" \
  --field 'error=' \
  --field 'generated_artifacts=["rounds/<RRR>/proposals.json", "base/bottleneck_report.json", "experiment_ledger.json", "experiment_summary.md"]'
```

No text before or after the JSON line. On fail-loud paths emit the same field
set with `error` filled and `proposals_path` empty.

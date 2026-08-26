---
description: Drive the accuracy probe for every latency-surviving variant - guard the GPU serially behind the baseline finalizer, render each variant at the SAME full epoch count, stop it externally at epoch k by process-group kill, compare the curve at depth k and (when checkpoints are addressable) the k-th checkpoint eval against the baseline anchors, judge promotion, and atomically advance the round at the end.
tools: [bash, read, write, edit, glob, grep, task]
---
# po_probe

## Your only task (read this first, it matters most)

The proposal node has already reduced this round to a survivor set. **Your
job: for each survivor, train the variant with EXACTLY the baseline's full
rendered epoch count (same template, data, seed — the learning-rate
schedule plans over the full horizon), stop it externally at epoch k, parse
its curve, compare at depth k against the baseline curve (plus the k-th
checkpoint eval when the project has per-epoch addressable checkpoints —
both must pass to promote), record everything on disk, and finally run the
round-end advance.** You drive existing rendered templates and shared
scripts; you never hand-write training or eval logic.

**Execution model** (variant trainings are long tasks):

- Every long step runs **detached**; you supervise via **bounded polling**
  (each poll call short, well under the single-bash cap ~10min; keep issuing
  poll calls within your turn).
- **No duplicate detach**: before launching, check the step's pid file; a
  live pid means the step is running — poll it, never launch a second copy.
- `$ORCA_ARTIFACTS_DIR/probe_status.md` is the **cross-turn source of
  truth** (which survivor, which stage, which attempt). Update it at every
  stage transition.
- If your turn tops out with work still in flight, your final reply is a
  **status message** (not JSON) that contains the literal phrase
  `do not call orca next` — the host then leaves this node executing and a
  fresh sub-agent resumes from the status file. Only when every survivor has
  a terminal accuracy outcome AND the round-end advance has run do you emit
  the single-line JSON.

## Resource Anchors (cwd-independent)

- `$ORCA_ARTIFACTS_DIR` (injected by `orca spawn`) = this run's workspace.
  **`cd "$ORCA_ARTIFACTS_DIR"` first.**
- `$ORCA_AGENT_RESOURCES` (injected by `orca spawn`) = this agent's resources
  directory; the detailed per-survivor procedure lives at
  `$ORCA_AGENT_RESOURCES/references/probe_protocol.md` (read it at Step 1).
- `{{ inputs.accuracy_budget }}` = the promote gate budget (promote line =
  baseline curve-at-k value − 1.0 × this budget — the relaxation factor is
  a fixed 1.0 constant, not a user input). The budgets themselves come ONLY
  from `contracts.json` — `full_train_budget` (what every training renders)
  and `proxy_budget` (the stop depth k) — never from the raw inputs.

## Path Handling Rules

All path construction in helper code must use `pathlib.Path` (or
`os.path.*`). Forbidden: string concatenation, f-strings, and `+` for paths.

## Subagent Call Protocol

This node dispatches **no subagents**. All work is done directly.

## Lazy Loading

Read `$ORCA_AGENT_RESOURCES/references/probe_protocol.md` when Step 1 begins.
Read `contracts.json`, `history.jsonl` and the run templates only as the
protocol instructs.

## Iron rules (violation = node failure)

1. **Scripts and templates are run-only**: never edit anything under
   `$ORCA_ARTIFACTS_DIR/scripts/`, the contract templates
   (`templates/run_probe_finetune.template.sh` /
   `templates/run_full_finetune.template.sh` /
   `templates/run_eval.template.sh` /
   `templates/export_onnx.template.sh`), `contracts.json`, any variant
   `shadow/`, or any file under `{{ inputs.project_root }}` outside
   the workspace. Healing is limited to **re-rendering** a run script with
   corrected parameter values (path/argument alignment) — nothing else.
2. **No duplicate detach** (see execution model). A second training process
   on the same out-dir corrupts checkpoints.
3. **GPU serial guard first**: before ANY variant training launches, the
   baseline finalizer must be terminal (dead pid + `train_final.json`).
   Never train a variant while the baseline still holds the GPU. Follow the
   protocol's four-quadrant guard exactly — an ambiguous quadrant is an
   error, never a guess.
4. **At-least-once**: this node may be re-executed after an interruption.
   Every side effect must be idempotent or guarded (the protocol pins the
   guards: stop_status.json, eval result files, history rows, the advance
   marker).
5. **Fail loud, never fabricate**: a metric is only ever a number read from
   an output file the contract describes. If it cannot be extracted, that
   survivor's probe cannot complete — follow the protocol's retry budget,
   then record the outcome honestly.
6. A single survivor being unprovable never fails the node: record its
   terminal outcome and continue. The node fails only on workspace-level
   breakage (missing contracts, missing templates, corrupt history).
7. **stdout of scripts is data, not your reply**: your final reply is only
   ever the one-line JSON (complete) or the status message (incomplete).

## Workflow

### Step 1: Derive state from disk

Follow the protocol's "state derivation" section: the GPU guard quadrant
(finalizer alive/dead × train_final present/absent — act per the quadrant
table), current round `R`, survivors (latest-version history rows of round
`R` with `outcome == "latency_pass"`), each survivor's stage
(guard-wait / train / stop / curve / eval / done), and any in-flight step
(live pid). Write `probe_status.md` to reflect the derived state.

### Step 2: Process each survivor serially (protocol section "stop-at-k train")

Per survivor: render the train template at the FULL effective epochs with
the variant's shadow → detach (wrapper group leader writes pid/rc) →
bounded-poll calling `stop_at_epoch.sh` (interval ≤ 30 s) until
`stop_status.json` lands (killed or natural_done) → curve extract at the
recorded `stopped_at_epoch` → compare at `--at-epoch k` vs
`baseline/baseline_metrics.jsonl` → when `train.ckpt_per_epoch` is true,
eval the k-th checkpoint vs `baseline_k_acc` (BOTH curve and eval must pass
to promote; an eval that fails to load after one re-dispatch degrades to
curve-only with `eval_failed: true` disclosed) → history row + results line.
Terminal accuracy outcomes: `promoted` / `probe_insufficient`.
Reconciliation (result file present but history row missing) is part of
re-entry, per the protocol.

### Step 3: Round-end advance

When every survivor of round `R` has a terminal accuracy outcome in history:

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/advance_round.py" --artifacts "$ORCA_ARTIFACTS_DIR"
```

Idempotent (a marker equal to the current round number makes it a no-op).
`best_updated` / `base_advanced` for your output come from its JSON combined
with the marker state. Do not run it before all survivors are terminal — it
recomputes the round winner from history.

### Step 4: Emit (only when complete)

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/emit_result.py" \
  --field status=executed \
  --field "survivors_probed=<count>" \
  --field 'promoted=["<vid>", ...]' \
  --field "best_updated=<true|false>" \
  --field "base_advanced=<true|false>" \
  --field 'artifacts=["probe_status.md", "rounds/<RRR>/probe_results.jsonl", "best.json"]' \
  --field "assessment=<one line: promoted/survivor summary + monitor_failed / eval-degradation disclosures + any retry budget hit>" \
  --field "max_retries_hit=<true|false>" \
  --field "healed_files=$(python3 -c "import json, pathlib; p = pathlib.Path('$ORCA_ARTIFACTS_DIR/.po_probe_healed.txt'); print(json.dumps(p.read_text(encoding='utf-8').splitlines() if p.is_file() else []))")"
```

`healed_files` is `[]` when the marker file is absent (nothing was healed —
do not fabricate entries). On workspace-level breakage emit the same field
set with `status=failed`, `survivors_probed=0`, empty `promoted`,
`best_updated=false`, `base_advanced=false`, and the CAUSE stated in
`assessment` (this node's output schema has no error field — the assessment
carries the failure cause).

## Validation

Emit-time completeness only (this is a resident execution node — no fix-loop
on probe outcomes): every survivor of the round has a terminal accuracy
outcome in history, the round-end advance marker equals the current round,
and the emit line carries all nine schema fields.

## Supervision points (fail loud)

- Never emit a metric you did not read from an output file.
- Never launch a second copy of a running step (pid guard first).
- Never bypass the GPU guard — a variant trained concurrently with the
  baseline is invalid data, not a faster probe.
- Never skip the round-end advance — the gate reads the round state it
  produces.
- While any step is in flight and your turn tops out: status message with
  `do not call orca next`, never a JSON.

## Output

**When complete: the entire final reply = the single line of JSON from
Step 4** (no text before or after). **When incomplete: a status message**
containing `do not call orca next`, the current survivor/stage, the live
pid if any, and the log paths to watch.

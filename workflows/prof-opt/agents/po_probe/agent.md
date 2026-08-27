---
description: Conditional accuracy gate - verify the deployed scripts, derive the phase from the single round source (latency phase passes through with zero training; accuracy phase trains the survivors at the baseline's full epoch count, stops each at epoch k by process-group kill, judges both the curve and the k-th checkpoint eval against the frozen anchors, extracts accuracy rules through the accuracy-analyst subagent, and advances the round when an accuracy-pass winner emerges).
tools: [bash, read, write, edit, glob, grep, task]
---
# po_probe

## Your only task (read this first, it matters most)

The proposal node has already reduced this round. **Your job depends on the
phase, resolved at YOUR entry from the single round source — the mode may
have flipped since the proposal node ran:

- **latency phase** → **pass through**: no variant is trained, no GPU guard
  is awaited (the round's advance already happened inside the proposal
  node). Emit the passthrough state honestly (`survivors_probed=0`) and
  leave.
- **accuracy phase** → run the coarse accuracy gate: for the mechanical
  training set (below), train each variant with EXACTLY the baseline's full
  rendered epoch count (same template, data, seed — the learning-rate
  schedule plans over the full horizon), stop it externally at epoch k,
  parse its curve, judge at depth k against the baseline curve (plus the
  k-th checkpoint eval when the project has per-epoch addressable
  checkpoints — the worst of the two gate gaps must be within the FROZEN
  accuracy budget from the origin anchor), record everything on disk, have
  the accuracy-analyst extract/update rules from the measured outcomes,
  and finally run the recovery advance (only an accuracy-pass winner under
  the frozen line moves the base).**

You drive existing rendered templates and shared scripts; you never
hand-write training or eval logic.

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
  fresh sub-agent resumes from the status file. Only when every survivor of
  the training set has a terminal accuracy outcome AND the round advance
  (accuracy phase) has run do you emit the single-line JSON.

## Resource Anchors (cwd-independent)

- `$ORCA_ARTIFACTS_DIR` (injected by `orca spawn`) = this run's workspace.
  **`cd "$ORCA_ARTIFACTS_DIR"` first.**
- `$ORCA_AGENT_RESOURCES` (injected by `orca spawn`) = this agent's resources
  directory; the detailed per-survivor procedure lives at
  `$ORCA_AGENT_RESOURCES/references/probe_protocol.md` (read it at Step 1).
- The accuracy budget comes ONLY from the frozen origin anchor
  (`base/origin_anchor.json` `accuracy_budget`) — never from a raw input,
  never recomputed. The budgets that render trainings come ONLY from
  `contracts.json` — `full_train_budget` (what every training renders) and
  `proxy_budget` (the stop depth k).

## Path Handling Rules

All path construction in helper code must use `pathlib.Path` (or
`os.path.*`). Forbidden: string concatenation, f-strings, and `+` for paths.

## Subagent Call Protocol (point-to-file)

This node dispatches ONE subagent: `accuracy-analyst` (accuracy phase, once
per judged round). Its body lives at `{{ subagents_root }}/<name>.md`
(inlined as an absolute path at render time).

`Task(subagent_type=<host built-in generic type>, prompt="First fully Read {{ subagents_root }}/accuracy-analyst.md, strictly follow its Method for this task. This task's inputs: <specific inputs per the md's Inputs section>. Return in the format the md specifies. The **first line of the report** must verbatim echo the sentinel field from the frontmatter of the md you Read (format at the top of the md; don't guess, don't infer from this prompt — it must come from the file you Read).")`

**Failure matrix**: the returned first line is not the sentinel, or
`rules_pool.py check` fails on the updated rule file → re-dispatch ONCE
with the failure quoted. Second failure → drop the offending rule rows,
disclose in `assessment`, and CONTINUE the round (rules are an incremental
asset — they never block the round).

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
3. **GPU serial guard first** (accuracy phase only): before ANY variant
   training launches, the baseline finalizer must be terminal (dead pid +
   `train_final.json`). Never train a variant while the baseline still
   holds the GPU. Follow the protocol's four-quadrant guard exactly — an
   ambiguous quadrant is an error, never a guess.
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
   breakage (missing contracts, missing templates, corrupt history, a
   broken invariant).
7. **stdout of scripts is data, not your reply**: your final reply is only
   ever the one-line JSON (complete) or the status message (incomplete).

## Workflow

### Step 0: Script stamp + phase dispatch

Verify the deployed script set (BEFORE the phase decision — a passthrough
round verifies the stamp too):

```bash
bash "$ORCA_ARTIFACTS_DIR/scripts/deploy_scripts.sh" --verify
```

Then resolve the phase AT THIS MOMENT (the mode may have flipped when the
proposal node's advance put the best under the line — that flip makes THIS
entry the accuracy first-entry):

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/round_state.py" \
  --artifacts "$ORCA_ARTIFACTS_DIR" mode
```

- `latency` → **passthrough**: skip to Step 4 with `mode=latency`,
  `survivors_probed=0`, `assessment` noting "latency phase passthrough —
  no accuracy training this round". For `advanced_vid` / `best_updated` /
  `base_advanced`, re-derive from the round's own disk state: read
  `.round_advanced` and `rounds/<RRR>/direction.json` — when the marker
  records `(current round, latency)` take its `vid` / `improved` (the
  advance happened in the proposal node; report it as this round's state);
  when the marker is stale or absent (e.g. the proposal node crashed after
  its advance, deferring the round) report the zero-advance form
  (`advanced_vid=""`, both flags false) — never read an older round's
  values.
- `accuracy` → derive the mechanical training set and continue at Step 1.

**Training set (mechanical, no timing inference):** read `best.json`'s vid
and look up ALL its history rows:

- best.vid has NO probe row at all → **first entry**: train ONLY
  `best.vid`.
- best.vid already has probe rows → **recovery round**: train this round's
  survivor set — latest history rows of the CURRENT round (`round_state
  current`) with `outcome == "latency_pass"`, EXCLUDING vids whose latest
  row is already a terminal probe row (`accuracy_pass` / `accuracy_fail` /
  `probe_insufficient` all count as already trained).

### Step 1: Derive state from disk

Follow the protocol's "state derivation" section: the GPU guard quadrant
(finalizer alive/dead × train_final present/absent — act per the quadrant
table), the training set from Step 0, each survivor's stage
(guard-wait / train / stop / curve / eval / done), and any in-flight step
(live pid). Write `probe_status.md` to reflect the derived state.

### Step 2: Process each survivor serially (protocol section "stop-at-k train")

Per survivor: render the train template at the FULL effective epochs with
the variant's shadow → detach (wrapper group leader writes pid/rc) →
bounded-poll calling `stop_at_epoch.sh` (interval ≤ 30 s) until
`stop_status.json` lands (killed or natural_done) → curve extract at the
recorded `stopped_at_epoch` → compare at `--at-epoch k` vs
`baseline/baseline_metrics.jsonl` → when `train.ckpt_per_epoch` is true,
eval the k-th checkpoint vs `baseline_k_acc` → the scripted
`verdict_decide.py promote` (reads the FROZEN budget from the origin
anchor; outputs `accuracy_pass` and the worst-gate `gap`) → history row
(`accuracy_pass` / `accuracy_fail` / `probe_insufficient`, carrying `gap`)
+ results line. Reconciliation (result file present but history row
missing) is part of re-entry, per the protocol.

### Step 3: Rule extraction + recovery advance

**Rule extraction** — once every vid of the training set has a terminal
accuracy outcome: dispatch `accuracy-analyst` with this round's probe rows
(vid / gap / accuracy_pass + each vid's lineage `change_sig` from history)
and the current `$ORCA_ARTIFACTS_DIR/accuracy_rules.json`. On return run
the mechanical validation:

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/rules_pool.py" check \
  --artifacts "$ORCA_ARTIFACTS_DIR"
```

(failure matrix above: one re-dispatch, then drop the bad rows and
disclose).

**Recovery advance** — then run:

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/advance_round.py" \
  --artifacts "$ORCA_ARTIFACTS_DIR"
```

Idempotent ((round, mode) keyed). In the accuracy phase only an
`accuracy_pass` winner at or under the frozen line advances — a round
without one keeps the base fixed (the rerouting signal lands in the
round's `direction.json`). `best_updated` / `base_advanced` /
`advanced_vid` for your output come from its JSON combined with the marker
state.

### Step 4: Emit (only when complete)

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/emit_result.py" \
  --field status=executed \
  --field "survivors_probed=<count>" \
  --field "mode=<latency|accuracy from Step 0>" \
  --field 'accuracy_pass_vids=["<vid>", ...]' \
  --field "advanced_vid=<vid or empty string>" \
  --field "best_updated=<true|false>" \
  --field "base_advanced=<true|false>" \
  --field 'artifacts=["probe_status.md", "rounds/<RRR>/probe_results.jsonl", "best.json", "rounds/<RRR>/direction.json"]' \
  --field "assessment=<one line: mode + accuracy-pass/survivor summary + monitor_failed / eval-degradation / rule-extraction disclosures + any retry budget hit>" \
  --field "max_retries_hit=<true|false>" \
  --field "healed_files=$(python3 -c "import json, pathlib; p = pathlib.Path('$ORCA_ARTIFACTS_DIR/.po_probe_healed.txt'); print(json.dumps(p.read_text(encoding='utf-8').splitlines() if p.is_file() else []))")"
```

`healed_files` is `[]` when the marker file is absent (nothing was healed —
do not fabricate entries). On workspace-level breakage emit the same field
set with `status=failed`, `survivors_probed=0`, empty `accuracy_pass_vids`,
`advanced_vid=""`, `best_updated=false`, `base_advanced=false`, and the
CAUSE stated in `assessment` (this node's output schema has no error field
— the assessment carries the failure cause).

## Validation

Emit-time completeness only (this is a resident execution node — no fix-loop
on probe outcomes): every vid of the derived training set has a terminal
accuracy outcome in history (accuracy phase), the advance marker records
the current round for the phase in play (accuracy phase), and the emit line
carries all eleven schema fields.

## Supervision points (fail loud)

- Never emit a metric you did not read from an output file.
- Never launch a second copy of a running step (pid guard first).
- Never bypass the GPU guard — a variant trained concurrently with the
  baseline is invalid data, not a faster probe.
- Never skip the recovery advance in the accuracy phase — the gate reads
  the round state it produces.
- While any step is in flight and your turn tops out: status message with
  `do not call orca next`, never a JSON.

## Output

**When complete: the entire final reply = the single line of JSON from
Step 4** (no text before or after). **When incomplete: a status message**
containing `do not call orca next`, the current survivor/stage, the live
pid if any, and the log paths to watch.

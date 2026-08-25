# Probe Protocol

Per-survivor accuracy probe procedure (stop-at-k). Paths are relative to
the workspace root (`$ORCA_ARTIFACTS_DIR`) unless absolute. `RRR` =
current round zero-padded to 3 digits.

Angle-bracket placeholders (`<project-root>`, `<accuracy-budget>`) are
runtime values from your node prompt's input anchors — substitute the
actual values when you run the commands.

Gate parameters used here (direction-normalized by `metric_direction` from
`contracts.json`):

- stop depth `k` = `contracts.json` `proxy_budget.epochs` — read from disk.
- full effective epochs `E` = `contracts.json` `full_train_budget.epochs`.
- promote line (curve gate): `baseline_curve_at_k − 1.0 ×
  <accuracy-budget>` where `baseline_curve_at_k` = the baseline curve's
  metric at epoch k (from `baseline/baseline_metrics.jsonl`).
- eval gate (only when `train.ckpt_per_epoch` is true): variant k-th ckpt
  eval must be within the same budget of `baseline/baseline_k_acc.json`'s
  `baseline_k_acc`.

For `higher_better` metrics "worse than the line" means value < line; for
`lower_better` it means value > line. All comparisons are computed by a
python one-liner reading the JSON values — never by mental arithmetic.

**Fairness invariant (iron rule)**: every training (baseline, variant,
winner) renders the SAME template with the SAME full effective epochs,
data, and seed — the learning-rate schedule plans over the full horizon in
every run. A variant differs from the baseline ONLY in structure and in
being stopped at epoch k from the outside. Never render a smaller epoch
count, never tune a data/step cap here.

## GPU serial guard (run BEFORE any variant training; re-check on entry)

The baseline full training runs detached behind its finalizer. The guard
target is `baseline/finalizer.pid` (the finalizer's lifetime ⊇ the
training's — it survives the training's completion to finish the anchors).
Four quadrants, act per table:

| finalizer.pid state | `baseline/train_final.json` | action |
|---|---|---|
| alive | — | **bounded-wait**: poll again on your next turn (status message + `do not call orca next`); each individual wait call ≤ 480 s. **Stall is an error**: while the TRAINING is alive, `baseline/train.attempt<N>.log` mtime AND the curve point count BOTH frozen for ≥ 30 min → `error` route (status=failed, cause "baseline training stalled"). During the finalizer-only tail (training done), `baseline/finalizer.log` frozen for ≥ 30 min → same. |
| dead | `status: done` | **pass** — the baseline is finalized; proceed. |
| dead | `status: failed` | **error route**: emit `status=failed`, assessment names `train_final.stage`. |
| dead | missing | **fail loud**: the finalizer exited without a terminal state (assessment names `baseline/finalizer.log`); never proceed on an unknown baseline. |

## State derivation (every entry, including re-entry)

1. Run the GPU guard. Still waiting → status message; terminal-done →
   continue.
2. `R` = maximum numeric directory under `rounds/`.
3. Survivors = vids whose LATEST history row has `round == R` and
   `outcome == "latency_pass"`.
4. Per survivor, the stage:
   - history row already terminal (`outcome` in `promoted` /
     `probe_insufficient`) → done, skip;
   - `variants/<vid>/eval/proxy.json` exists but the history row is still
     `latency_pass` → reconcile first (see below), then re-derive;
   - `variants/<vid>/stop_status.json` exists but the history row is still
     `latency_pass` → the training already terminated; resume at the curve
     extraction (never re-detach);
   - a pid file under `variants/<vid>/train/` with a live group → the step
     is in flight: poll it with `stop_at_epoch.sh` (never re-launch);
   - otherwise → start at the train step.
5. Write `probe_status.md`: round, survivor list, per-survivor stage,
   in-flight pids, attempt counts, timestamp. Truncate the heal ledger
   `.po_probe_healed.txt` on node entry (it reports THIS entry's heals only).

## Reconciliation (crash between result file and history row)

For a survivor whose result file exists while its history row is still the
process-state `latency_pass`: re-derive the outcome from the result file
exactly as the stage below would have, append the history row and the
results line, then continue with the next survivor. Result files are the
payload; history is the ledger — after reconciliation they must agree.

## Stop-at-k train (per survivor)

1. The training template is
   `$ORCA_ARTIFACTS_DIR/templates/run_full_finetune.template.sh` (required
   tokens: `epochs` / `out_dir` / `seed`; some templates also declare
   `vid`). Read it once and note exactly which tokens it declares.
2. Render at the FULL effective epochs with the VARIANT's shadow (read the
   budget values from `contracts.json` — `full_train_budget.epochs` /
   `.seed`):
   ```bash
   mkdir -p "$ORCA_ARTIFACTS_DIR/variants/<VID>/train"
   bash "$ORCA_ARTIFACTS_DIR/scripts/render_run.sh" \
     --template "$ORCA_ARTIFACTS_DIR/templates/run_full_finetune.template.sh" \
     --out "$ORCA_ARTIFACTS_DIR/variants/<VID>/train/train.rendered.sh" \
     --set "epochs=<full_train_budget.epochs>" \
     --set "out_dir=$ORCA_ARTIFACTS_DIR/variants/<VID>/train" \
     --set "seed=<full_train_budget.seed>" \
     --set "vid=<VID>" \
     --set "shadow_dir=$ORCA_ARTIFACTS_DIR/variants/<VID>/shadow" \
     --set "shadow_pkgs=$(python3 -c "import json; print(','.join(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['shadow']['shadow_pkgs']))")" \
     --set "project_root=<project-root>" \
     --set "python=$(python3 -c "import json; print(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['interpreter']['sys_executable'])")"
   ```
3. **Detach** (wrapper group leader writes its OWN pid and does NOT exec —
   pid/rc each have their own writer, so a killed group leaves rc absent):
   ```bash
   cd "$ORCA_ARTIFACTS_DIR/variants/<VID>/train" && \
   setsid bash -c 'echo $$ > train.pid; bash train.rendered.sh > train.log 2>&1; echo $? > rc' \
     </dev/null >>wrapper.log 2>&1 &
   ```
4. **Bounded-poll with the external stop** (interval ≤ 30 s between calls;
   each call is ONE idempotent check — re-issue until it reports a terminal
   state; never sleep past the single-bash cap in one call):
   ```bash
   bash "$ORCA_ARTIFACTS_DIR/scripts/stop_at_epoch.sh" \
     --log "$ORCA_ARTIFACTS_DIR/variants/<VID>/train/train.log" \
     --contract "$ORCA_ARTIFACTS_DIR/contracts.json" \
     --stop-epoch <k> \
     --pid-file "$ORCA_ARTIFACTS_DIR/variants/<VID>/train/train.pid"
   ```
   - `{"status": "waiting"}` → the group is alive and below k: poll again
     on the next call (between polls, push the live curves — below). If
     your turn tops out → status message with `do not call orca next`.
   - `{"status": "killed", "stopped_at_epoch": N}` → terminal: the group
     was TERM→(grace)→KILLed; `N` is the ACTUAL parsed depth (≥ k, not
     always k — lines written before the group died count).
   - `{"status": "natural_done", "stopped_at_epoch": N, "rc": R,
     "monitor_failed": B}` → terminal: the worker finished on its own;
     `monitor_failed=true` (N > k) means the kill missed — disclose it in
     the assessment and the history row.
   - Terminal-state priority: `stop_status.json` wins over the rc file.
   - **Retry budget**: `rc != 0` (failed training) or the script's hard
     errors (attribution refusal, crash scene) → read the log tail, fix
     ONLY by re-rendering with corrected parameter values (the heal
     whitelist), wipe the PARTIAL CHECKPOINT ARTIFACTS the train contract's
     output rule predicts under the out-dir (never the control files
     `train.pid` / `rc` / `train.log` / `train.rendered.sh` — a from-scratch
     relaunch re-creates the checkpoints, the control files must survive),
     relaunch (attempt counter in `probe_status.md`). After 2 failed
     retries → terminal `probe_insufficient` (`proxy_acc=null`,
     `promote_gate="fail"`), `max_retries_hit=true`, next survivor. Record
     heals under `.po_probe_healed.txt`.
   - **While waiting**: push the live curves each poll cycle (best-effort
     sidecar, never fatal):
     ```bash
     python3 "$ORCA_ARTIFACTS_DIR/scripts/push_curves.py" \
       --artifacts "$ORCA_ARTIFACTS_DIR" || true
     ```
5. **Curve extraction** at the RECORDED depth (never assume k):
   ```bash
   python3 "$ORCA_ARTIFACTS_DIR/scripts/metric_curve.py" extract \
     --contract "$ORCA_ARTIFACTS_DIR/contracts.json" \
     --log "$ORCA_ARTIFACTS_DIR/variants/<VID>/train/train.log" \
     --out "$ORCA_ARTIFACTS_DIR/variants/<VID>/metrics/metrics.jsonl" \
     --expected-epochs "<stopped_at_epoch from stop_status.json>"
   ```
   A missing, duplicate, or non-contiguous epoch is a hard failure — do not
   substitute the final eval value for the curve.
6. **Pinned-depth comparison** (always at k, against the baseline FULL
   curve — the anchor is the file itself, recorded in the output):
   ```bash
   python3 "$ORCA_ARTIFACTS_DIR/scripts/metric_curve.py" compare \
     --baseline "$ORCA_ARTIFACTS_DIR/baseline/baseline_metrics.jsonl" \
     --candidate "$ORCA_ARTIFACTS_DIR/variants/<VID>/metrics/metrics.jsonl" \
     --direction "<contracts.json eval.metric_direction>" \
     --budget "<accuracy-budget>" \
     --at-epoch <k>
   ```
   Either curve lacking epoch k fails loud (never silently compare at a
   different depth). Persist this JSON as
   `variants/<VID>/metrics/epoch_compare.json` — its `at_epoch` must equal
   k and its `baseline_path` names the anchor.
7. **k-th checkpoint eval** (only when `contracts.json`
   `train.ckpt_per_epoch` is true): resolve the k-th checkpoint from the
   variant's train out-dir (the k-th match of `train.ckpt_output_rule` in
   write order), render + run the eval template with `ckpt=<that path>` and
   `log=.../variants/<VID>/eval/probe.log` (same render form as the node's
   other eval renders), extract the metric, write
   `variants/<VID>/eval/proxy.json`:
   `{"vid": "<VID>", "ckpt": "<path>", "metric_value": <number>, "k": <k>}`.
   **Eval-load failure matrix**: the eval fails to load/run → re-run it
   ONCE (re-render, new log); still failing → **degrade to curve-only
   judgment** with `eval_failed: true`, `eval_acc: null`, and the
   degradation disclosed in the history row and the assessment — the probe
   is not lost, only less strict.
   When `ckpt_per_epoch` is false → curve-only by design: set
   `eval_skipped_no_epoch_ckpt: true` in the history row (disclosed, not an
   error).
8. **Promote check** (scripted; BOTH gates when the eval ran, curve alone
   otherwise):
   ```bash
   python3 -c "import json; \
   c = json.load(open('$ORCA_ARTIFACTS_DIR/variants/<VID>/metrics/epoch_compare.json')); \
   b = <baseline curve metric at k>; d = '<metric_direction>'; slack = 1.0 * <accuracy_budget>; \
   line = b - slack if d == 'higher_better' else b + slack; \
   curve_ok = c['pass'] is True; \
   ev = json.load(open('$ORCA_ARTIFACTS_DIR/variants/<VID>/eval/proxy.json'))['metric_value'] \
        if <proxy.json exists> else None; \
   ba = <baseline_k_acc from baseline/baseline_k_acc.json> if <ckpt_per_epoch and baseline_k_acc exists> else None; \
   eval_ok = True if ev is None or ba is None else (ev >= ba - slack if d == 'higher_better' else ev <= ba + slack); \
   promoted = curve_ok and eval_ok; \
   print(json.dumps({'curve_pass': curve_ok, 'eval_acc': ev, 'eval_pass': eval_ok, 'line': line, 'promoted': promoted}))"
   ```
   outcome = `promoted` if true else `probe_insufficient`.

## History row + results line (after each terminal outcome)

```bash
python3 -c "import sys; sys.path.insert(0, '$ORCA_ARTIFACTS_DIR/scripts'); \
from history_lib import append_probe; \
append_probe('$ORCA_ARTIFACTS_DIR/history.jsonl', '<VID>', \
proxy_acc=<the CURVE metric at k — always the curve value, never the eval value>, \
promote_gate='<pass|fail>', outcome='<promoted|probe_insufficient>', \
eval_acc=<eval metric or None>, \
eval_failed=<true only when the k-ckpt eval could not run>, \
eval_skipped_no_epoch_ckpt=<true only when ckpt_per_epoch is false>, \
monitor_failed=<true only when stop_status says so>)"
```

(Omit the optional fields that do not apply — `None` defaults keep them out
of the row.) Then the results line:

```bash
python3 -c "import json, pathlib; \
p = pathlib.Path('$ORCA_ARTIFACTS_DIR/rounds/<RRR>/probe_results.jsonl'); \
row = {'vid': '<VID>', 'proxy_acc': <curve-at-k or None>, 'eval_acc': <or None>, \
'promote_gate': '<pass|fail>', 'outcome': '<outcome>', \
'stop_status': '<killed|natural_done>', 'stopped_at_epoch': <N>, \
'monitor_failed': <bool>}; \
p.parent.mkdir(parents=True, exist_ok=True); \
with p.open('a', encoding='utf-8') as f: f.write(json.dumps(row) + chr(10))"
```

Append the results line ONLY when creating the outcome fresh (not during
reconciliation of a line that already exists — check the file for the vid
first).

## Round-end advance

When every survivor of round `R` has a terminal outcome in history:

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/advance_round.py" --artifacts "$ORCA_ARTIFACTS_DIR"
```

- `base_advanced` = true iff `.round_advanced` now records round `R`
  (a no-op return with `advanced:false` and a matching marker round still
  means the advance is in effect).
- `best_updated` = the `best_updated` flag from the advance JSON.
- A non-zero exit is a workspace-level failure: emit `status=failed` with
  the cause stated in `assessment` and all count fields 0 (this node's
  output schema has no error field — the assessment carries the cause).

## Assessment marker

Keep a one-line human summary in `.po_probe_assessment.txt` (promotions,
retry hits, monitor_failed and eval-degradation disclosures). It feeds the
`assessment` output field.

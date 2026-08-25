# Probe Protocol

Per-survivor accuracy probe procedure. Paths are relative to the workspace
root (`$ORCA_ARTIFACTS_DIR`) unless absolute. `RRR` = current round
zero-padded to 3 digits.

Angle-bracket placeholders (`<project-root>`, `<accuracy_budget>`) are runtime
values from your node prompt's input anchors — substitute the actual values
when you run the commands.

Gate parameter used here (direction-normalized by `metric_direction` from
`contracts.json`):

- promote line: `baseline_proxy_acc − 1.0 × accuracy_budget` (the relaxation
  factor is a fixed 1.0 constant — baseline and variants train from scratch at
  the same budget, so the anchor already absorbs the systematic proxy loss)

For `higher_better` metrics "worse than the line" means value < line; for
`lower_better` it means value > line. All comparisons are computed by a
python one-liner reading the JSON values — never by mental arithmetic.

**Fairness invariant (iron rule)**: the proxy is epoch-only. The survivor and
baseline use the same full data, sampler, entry, and seed; only the shared
`proxy_budget.epochs` controls budget. Never invent, tune, or "improve" a
data-subset or step cap here; a variant trained at a different data/steps
budget than the baseline makes the comparison meaningless.

## State derivation (every entry, including re-entry)

1. `R` = maximum numeric directory under `rounds/`.
2. Survivors = vids whose LATEST history row has `round == R` and
   `outcome == "latency_pass"`.
3. Per survivor, the stage:
   - history row already terminal (`outcome` in `promoted` /
     `probe_insufficient`) → done, skip;
   - `variants/<vid>/eval/proxy.json` exists but the history row is still
     `latency_pass` → reconcile first (see below), then re-derive;
   - a training pid file under `variants/<vid>/proxy_train/` with a live pid
     → the step is in flight: poll it (never re-launch);
   - otherwise → start at the proxy-training step.
4. Write `probe_status.md`: round, survivor list, per-survivor stage,
   in-flight pids, attempt counts, timestamp. Truncate the heal ledger
   `.po_probe_healed.txt` on node entry (it reports THIS entry's heals only).

## Reconciliation (crash between result file and history row)

For a survivor whose result file exists while its history row is still the
process-state `latency_pass`: re-derive the outcome from the result file
exactly as the stage below would have, append the history row and the
results line, then continue with the next survivor. Result files are
the payload; history is the ledger — after reconciliation they must agree.

## Proxy train + eval (per survivor)

1. The proxy template is
   `$ORCA_ARTIFACTS_DIR/templates/run_probe_finetune.template.sh` (required
   tokens: `epochs` / `out_dir` / `seed`; some templates also declare `vid`,
   a data-subset token, or a step-cap token). Read it once and note exactly
   which tokens it declares.
2. Read `contracts.json` `proxy_budget` — {epochs, dataset_knob, data_value,
   max_steps, seed} — the SAME values the baseline trained with. Render with
   those values (supply the data-subset and step-cap tokens ONLY when the
   template declares them, which the contract stage guaranteed matches its
   own knob discovery):
   ```bash
   mkdir -p "$ORCA_ARTIFACTS_DIR/variants/<VID>/proxy_train"
   bash "$ORCA_ARTIFACTS_DIR/scripts/render_run.sh" \
     --template "$ORCA_ARTIFACTS_DIR/templates/run_probe_finetune.template.sh" \
     --out "$ORCA_ARTIFACTS_DIR/variants/<VID>/proxy_train/run_proxy.rendered.sh" \
     --set "epochs=<proxy_budget.epochs>" \
     --set "out_dir=$ORCA_ARTIFACTS_DIR/variants/<VID>/proxy_train" \
     --set "seed=<proxy_budget.seed>" \
     [--set "data_value=<proxy_budget.data_value>"] \
     [--set "max_steps=<proxy_budget.max_steps>"] \
     [--set "vid=<VID>"] \
     --set "shadow_dir=$ORCA_ARTIFACTS_DIR/variants/<VID>/shadow" \
     --set "shadow_pkgs=$(python3 -c "import json; print(','.join(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['shadow']['shadow_pkgs']))")" \
     --set "project_root=<project-root>" \
     --set "python=$(python3 -c "import json; print(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['interpreter']['sys_executable'])")"
   ```
   (The renderer fails loud on any unreplaced token; extra `--set` values
   that match no token are simply unused. Training starts from the entry's
   own seeded random initialization — no checkpoint is passed.)
3. **Detach** (single short call; the pid/rc pair lives next to the run):
   ```bash
   cd "$ORCA_ARTIFACTS_DIR/variants/<VID>/proxy_train" && \
   setsid nohup bash -c 'bash run_proxy.rendered.sh; echo $? > .proxy_rc' \
     > proxy.stdout.log 2>&1 < /dev/null & echo $! > .proxy_pid
   ```
4. **Bounded polling** (re-issue this short call until it reports a terminal
   state; never sleep past the single-bash cap in one call):
   ```bash
   cd "$ORCA_ARTIFACTS_DIR/variants/<VID>/proxy_train" && \
   if [ -f .proxy_rc ]; then echo "DONE rc=$(cat .proxy_rc)"; \
   elif kill -0 "$(cat .proxy_pid)" 2>/dev/null; then echo "RUNNING pid=$(cat .proxy_pid)"; \
   else echo "DEAD no-rc pid=$(cat .proxy_pid)"; fi
   ```
   - `DONE rc=0` → resolve the trained checkpoint (the output-path rule
     recorded in the train contract) and continue at step 6.
   - `DEAD no-rc` or `DONE rc!=0` → retry budget (below).
   - `RUNNING` → poll again; if your turn tops out, update `probe_status.md`
     and end with the status message.
5. **Retry budget**: on failure, read the tail of `proxy.stdout.log`, fix
   ONLY by re-rendering with corrected parameter values (path/argument
   alignment — the heal whitelist), wipe the partial out-dir contents that
   the train contract does not resume over, and relaunch (attempt counter in
   `probe_status.md`). After 2 failed retries for the same survivor →
   terminal outcome `probe_insufficient` (history row with
   `proxy_acc=null`, `promote_gate="fail"`), set `max_retries_hit=true`,
   continue with the next survivor. Record every healed re-render under
   `.po_probe_healed.txt` (one relative path per line).
6. **Epoch curve extraction (mandatory)**: after training succeeds, parse every
   epoch from `proxy.stdout.log`:
   ```bash
   python3 "$ORCA_ARTIFACTS_DIR/scripts/metric_curve.py" extract \
     --contract "$ORCA_ARTIFACTS_DIR/contracts.json" \
     --log "$ORCA_ARTIFACTS_DIR/variants/<VID>/proxy_train/proxy.stdout.log" \
     --out "$ORCA_ARTIFACTS_DIR/variants/<VID>/metrics/metrics.jsonl" \
     --expected-epochs "<proxy_budget.epochs>"
   ```
   A missing, duplicate, or non-contiguous epoch is a hard failure — do not
   substitute the final eval value for the curve.
7. **Epoch-aligned comparison**: compare against the baseline at the latest
   common epoch using the same direction normalization:
   ```bash
   python3 "$ORCA_ARTIFACTS_DIR/scripts/metric_curve.py" compare \
     --baseline "$ORCA_ARTIFACTS_DIR/baseline/baseline_metrics.jsonl" \
     --candidate "$ORCA_ARTIFACTS_DIR/variants/<VID>/metrics/metrics.jsonl" \
     --direction "<contracts.json eval.metric_direction>" \
     --budget "{{ inputs.accuracy_budget }}"
   ```
   Persist this JSON as `variants/<VID>/metrics/epoch_compare.json`.
8. **Proxy eval**: render + run the eval template with
   `ckpt=<trained checkpoint path>` and
   `log=.../variants/<VID>/eval/proxy.log`:
   ```bash
   mkdir -p "$ORCA_ARTIFACTS_DIR/variants/<VID>/eval"
   bash "$ORCA_ARTIFACTS_DIR/scripts/render_run.sh" \
     --template "$ORCA_ARTIFACTS_DIR/templates/run_eval.template.sh" \
     --out "$ORCA_ARTIFACTS_DIR/variants/<VID>/eval/probe.rendered.sh" \
     --set "ckpt=<trained checkpoint>" \
     --set "log=$ORCA_ARTIFACTS_DIR/variants/<VID>/eval/proxy.log" \
     --set "shadow_dir=$ORCA_ARTIFACTS_DIR/variants/<VID>/shadow" \
     --set "shadow_pkgs=$(python3 -c "import json; print(','.join(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['shadow']['shadow_pkgs']))")" \
     --set "project_root=<project-root>" \
     --set "python=$(python3 -c "import json; print(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['interpreter']['sys_executable'])")"
   bash "$ORCA_ARTIFACTS_DIR/variants/<VID>/eval/probe.rendered.sh" \
     > "$ORCA_ARTIFACTS_DIR/variants/<VID>/eval/probe.stdout.log" 2>&1
   ```
   The trained checkpoint path is resolved per `contracts.json`
   (`train.ckpt_output_rule`, applied to the proxy_train out-dir). If the
   full-budget eval runs long, detach + poll it exactly like the training
   step. Extract the metric per `contracts.json`
   (`eval.metric_extraction`); write `variants/<VID>/eval/proxy.json`:
   `{"vid": "<VID>", "ckpt": "<path>", "metric_value": <number>}`.
9. **Promote check** (scripted): require `epoch_compare.json.pass == true`; if
   the curve fails, the survivor is `probe_insufficient` even when the final
   eval passes. If the curve passes, use the final eval metric with anchor
   `baseline/baseline_proxy_acc.json` for the persisted `proxy_acc`:
   ```bash
   python3 -c "import json; \
p = json.load(open('$ORCA_ARTIFACTS_DIR/variants/<VID>/eval/proxy.json'))['metric_value']; \
b = <baseline proxy value from baseline/baseline_proxy_acc.json>; \
d = '<metric_direction>'; slack = 1.0 * <accuracy_budget>; \
line = b - slack if d == 'higher_better' else b + slack; \
promoted = p >= line if d == 'higher_better' else p <= line; \
print(json.dumps({'proxy_acc': p, 'line': line, 'promoted': promoted}))"
   ```
   outcome = `promoted` if true else `probe_insufficient`.

## History row + results line (after each terminal outcome)

```bash
python3 -c "import sys; sys.path.insert(0, '$ORCA_ARTIFACTS_DIR/scripts'); \
from history_lib import append_probe; \
append_probe('$ORCA_ARTIFACTS_DIR/history.jsonl', '<VID>', \
proxy_acc=<number or None>, \
promote_gate='<pass|fail>', outcome='<promoted|probe_insufficient>')"
python3 -c "import json, pathlib; \
p = pathlib.Path('$ORCA_ARTIFACTS_DIR/rounds/<RRR>/probe_results.jsonl'); \
row = {'vid': '<VID>', 'proxy_acc': <number or None>, \
'promote_gate': '<pass|fail>', 'outcome': '<outcome>'}; \
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
retry hits). It feeds the `assessment` output field.

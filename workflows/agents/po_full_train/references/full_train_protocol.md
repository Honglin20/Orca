# Full-Train Protocol

Resident full from-scratch training of the winning variant. Paths are
relative to the workspace root (`$ORCA_ARTIFACTS_DIR`) unless absolute.
`WINNER` = the vid recorded in `best.json`. Every training in this protocol
starts from the entry's own fixed-seed random initialization — NO checkpoint
is ever loaded (train-from-scratch paradigm; the baseline and the winner
use the SAME `full_train_budget` from `contracts.json`).

## State derivation (every entry)

1. `best.json` must exist and carry a vid — a missing best on this route is
   an immediate `status=failed` (cause in the assessment: no winner to
   train).
2. Read `contracts.json`: `full_train_budget` (epochs + seed — the
   effective values, never recomputed here), the pinned interpreter, the
   checkpoint output-path rule, and the eval metric extraction.
3. Baseline anchor state (read-only; the finalizer owns its production):
   - `baseline/train_final.json` exists with `status: done` AND
     `baseline/baseline_full_acc.json` exists AND its recorded
     `full_train_budget` equals `contracts.json`'s field-for-field →
     anchor resolved: `baseline_full_acc` = its value,
     `baseline_full_acc_source = "baseline"`.
   - `train_final.json` missing or `failed` → `status=failed`, cause names
     the missing/failed baseline terminal state (a verdict without an
     honestly-produced anchor is not a verdict).
   - fingerprint mismatch → `status=failed`, cause names the stale anchor
     (the workspace's baseline was trained under a different budget —
     never silently compared).
4. Stage:
   - `final/final_acc.json` exists with a NON-NULL `within_budget` → done:
     re-emit from disk (Step 5 of the node; nothing to re-run); exists with
     `within_budget: null` (a crash between the write and the verdict
     backfill) → re-run the scripted judgement (step 4) and backfill it;
   - `final/.train_rc` exists with 0 AND the pid is gone AND the promised
     checkpoint exists → training done: go to the symmetric final check,
     then the final evaluation;
   - `final/.train_pid` has a live group → training in flight: poll (never
     re-launch);
   - otherwise → not started (or a dead attempt without rc: count it as an
     attempt and relaunch).
5. Write `final/train_status.md`: stage, attempt count, pid, epochs, anchor
   state, ts.

## Launch (the only place that detaches the winner training)

The training template is
`$ORCA_ARTIFACTS_DIR/templates/run_full_finetune.template.sh` (required
tokens: `epochs` / `out_dir` / `seed`; some templates also declare `vid`).
Read it once and note exactly which tokens it declares, then:

```bash
mkdir -p "$ORCA_ARTIFACTS_DIR/final"
bash "$ORCA_ARTIFACTS_DIR/scripts/render_run.sh" \
  --template "$ORCA_ARTIFACTS_DIR/templates/run_full_finetune.template.sh" \
  --out "$ORCA_ARTIFACTS_DIR/final/train.rendered.sh" \
  --set "epochs=<full_train_budget.epochs>" \
  --set "out_dir=$ORCA_ARTIFACTS_DIR/final" \
  --set "seed=<full_train_budget.seed>" \
  --set "vid=$WINNER" \
  --set "shadow_dir=$ORCA_ARTIFACTS_DIR/shadow" \
  --set "shadow_pkgs=$(python3 -c "import json; print(','.join(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['shadow']['shadow_pkgs']))")" \
  --set "project_root=<project-root>" \
  --set "python=$(python3 -c "import json; print(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['interpreter']['sys_executable'])")"
```

(The render values are the SAME `full_train_budget` the baseline chain and
every variant rendered with — the fairness invariant is the fingerprint,
checked at anchor resolution.) Then detach (wrapper group leader writes its
own pid, does NOT exec):

```bash
cd "$ORCA_ARTIFACTS_DIR/final" && \
setsid bash -c 'echo $$ > .train_pid; bash train.rendered.sh > train.stdout.log 2>&1; echo $? > .train_rc' \
  </dev/null >>wrapper.log 2>&1 &
```

The winner IS the current global shadow (the round-end advance replaced it),
so the injection points at the global shadow root.

## Bounded polling (re-issue this short call; never sleep past the bash cap)

```bash
cd "$ORCA_ARTIFACTS_DIR/final" && \
if [ -f .train_rc ]; then echo "DONE rc=$(cat .train_rc)"; \
elif kill -0 "$(cat .train_pid)" 2>/dev/null; then echo "RUNNING pid=$(cat .train_pid)"; \
else echo "DEAD no-rc pid=$(cat .train_pid)"; fi
```

- `RUNNING` → update the status file occasionally and poll again; if the
  turn tops out → status message with `do not call orca next`.
- `DONE rc=0` → verify the promised checkpoint exists (per the train
  contract's output rule) and continue to the symmetric final check.
- `DONE rc!=0` / `DEAD no-rc` → retry path below.

## Retry path (heal whitelist + budget)

1. Read the tail of `final/train.stdout.log` (~100 lines) and locate the
   root cause.
2. Fixable by re-rendering with corrected parameter values (path/argument
   alignment) → re-render, record the healed script path under
   `.po_full_train_healed.txt`, wipe the partial out-dir contents the train
   contract does not resume over, relaunch (attempt++ in the status file).
3. Root cause needs a forbidden edit (anything outside the rendered script)
   → stop: `status=failed` with the cause in the assessment.
4. Retry budget: 2 failed retries → `status=failed`,
   `max_retries_hit=true`.

## Symmetric final check (after rc=0, before the evaluation)

The baseline finalizer enforces "actual == rendered" on the baseline; the
winner gets the SAME check (symmetry — an early-stopped winner trained at a
smaller effective budget would beat the anchor unfairly or lose to it
unfairly, both silently):

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/metric_curve.py" extract \
  --contract "$ORCA_ARTIFACTS_DIR/contracts.json" \
  --log "$ORCA_ARTIFACTS_DIR/final/train.stdout.log" \
  --out "$ORCA_ARTIFACTS_DIR/final/final_metrics.jsonl" \
  --expected-epochs "<full_train_budget.epochs>"
```

Non-zero (count mismatch / unparsable) → `status=failed`, assessment
attributes the failure to the symmetric final check and quotes the
admission clause (trainings must execute the rendered epoch count exactly;
early-stopping projects are out of scope — see `contracts.json` `reason`).

## Final evaluation

1. Resolve the final checkpoint path per `contracts.json`
   (`train.ckpt_output_rule`, applied to the `final/` out-dir).
2. Render + run the eval template
   (`templates/run_eval.template.sh`, tokens `ckpt` / `log`) against it:
   ```bash
   bash "$ORCA_ARTIFACTS_DIR/scripts/render_run.sh" \
     --template "$ORCA_ARTIFACTS_DIR/templates/run_eval.template.sh" \
     --out "$ORCA_ARTIFACTS_DIR/final/final_eval.rendered.sh" \
     --set "ckpt=<resolved final checkpoint>" \
     --set "log=$ORCA_ARTIFACTS_DIR/final/final_eval.log" \
     --set "shadow_dir=$ORCA_ARTIFACTS_DIR/shadow" \
     --set "shadow_pkgs=$(python3 -c "import json; print(','.join(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['shadow']['shadow_pkgs']))")" \
     --set "project_root=<project-root>" \
     --set "python=$(python3 -c "import json; print(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['interpreter']['sys_executable'])")"
   bash "$ORCA_ARTIFACTS_DIR/final/final_eval.rendered.sh" \
     > "$ORCA_ARTIFACTS_DIR/final/final_eval.stdout.log" 2>&1
   ```
   A full-budget eval can be long: detach + poll it like the training step
   (pid/rc pair under `final/`) instead of blocking one call.
3. Extract the metric per `contracts.json` (`eval.metric_extraction`,
   applied to the eval log). Write `final/final_acc.json`:
   `{"vid": "<WINNER>", "final_acc": <number>,
   "baseline_full_acc": <number>, "baseline_full_acc_source": "baseline",
   "full_train_budget": <verbatim from contracts.json>,
   "within_budget": <bool>, "metric_direction": "<direction>"}`.
4. Budget judgement (scripted; anchor = the resolved `baseline_full_acc`).
   Write `final/final_acc.json` FIRST with `"within_budget": null`, then:
   ```bash
   python3 "$ORCA_ARTIFACTS_DIR/scripts/verdict_decide.py" final-budget \
     --artifacts "$ORCA_ARTIFACTS_DIR" \
     --budget "<accuracy-budget>"
   ```
   It reads `final_acc` / `baseline_full_acc` / `metric_direction` back from
   the file and prints `{"within_budget": <bool>}` — overwrite the null with
   that value. The direction-normalized comparison is scripted, never
   hand-derived.
5. Copy the winner structure (referenced, never re-measured):
   `variants/$WINNER/onnx/model.onnx` → `final/model.onnx`
   (`base/model.onnx` is the same structure after the round-end advance;
   either source is valid — copy from the variant for a stable provenance).

## Idempotency notes

- `final/final_acc.json` present on entry with a NON-NULL `within_budget` →
  do not re-run anything; emit from disk. Present with `within_budget:
  null` (a crash between the write and the verdict backfill) → re-run the
  scripted judgement (step 4) and backfill it — idempotent.
- A live pid is never re-launched; a dead attempt without rc counts as one
  attempt and is relaunched after the log-tail check.
- All file writes under `final/` are overwrite-safe (status/checkpoint/
  result files keyed by fixed names).

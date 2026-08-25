# Full-Train Protocol

Resident full from-scratch training of the winning variant. Paths are
relative to the workspace root (`$ORCA_ARTIFACTS_DIR`) unless absolute.
`WINNER` = the vid recorded in `best.json`. Every training in this protocol
starts from the entry's own fixed-seed random initialization — NO checkpoint
is ever loaded (train-from-scratch paradigm; the winner and the auto-trained
baseline use the SAME seed).

## State derivation (every entry)

1. `best.json` must exist and carry a vid — a missing best on this route is
   an immediate `status=failed` (cause in the assessment: no winner to train).
2. Read `contracts.json`: full epoch count (`train_epochs_full`), the pinned
   interpreter, the checkpoint output-path rule, and the eval metric
   extraction.
3. Actual epochs = `min(cap, train_epochs_full)` where cap =
   the `<full-train-epoch-cap>` workflow input; an empty cap means no cap (actual =
   `train_epochs_full`). Never exceed the project's full epoch count.
4. Baseline anchor state: `baseline/baseline_ref.json` carries a number →
   anchor resolved (`ref-input`, nothing to train). Null/missing AND
   `baseline/baseline_full_acc.json` exists → anchor cached (`auto-trained`).
   Null/missing and no cache → the baseline auto-train (below) must run.
5. Stage:
   - `final/final_acc.json` exists → done: re-emit from disk (Step 5 of the
     node; nothing to re-run);
   - `final/.train_rc` exists with 0 AND the pid is gone AND the promised
     checkpoint exists → training done: go to the anchor resolution, then
     the final evaluation;
   - `final/.train_pid` has a live pid → training in flight: poll (never
     re-launch);
   - otherwise → not started (or a dead attempt without rc: count it as an
     attempt and relaunch).
6. Write `final/train_status.md`: stage, attempt count, pid, epochs, anchor
   state, ts.

## Launch (the only place that detaches the winner training)

The full-training template is
`$ORCA_ARTIFACTS_DIR/templates/run_full_finetune.template.sh` (required
tokens: `epochs` / `out_dir` / `seed`; some templates also declare `vid`).
Read it once and note exactly which tokens it declares, then:

```bash
mkdir -p "$ORCA_ARTIFACTS_DIR/final"
bash "$ORCA_ARTIFACTS_DIR/scripts/render_run.sh" \
  --template "$ORCA_ARTIFACTS_DIR/templates/run_full_finetune.template.sh" \
  --out "$ORCA_ARTIFACTS_DIR/final/run_full.rendered.sh" \
  --set "epochs=<actual epochs>" \
  --set "out_dir=$ORCA_ARTIFACTS_DIR/final" \
  --set "seed=<seed>" \
  [--set "vid=$WINNER"] \
  --set "shadow_dir=$ORCA_ARTIFACTS_DIR/shadow" \
  --set "shadow_pkgs=$(python3 -c "import json; print(','.join(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['shadow']['shadow_pkgs']))")" \
  --set "project_root=<project-root>" \
  --set "python=$(python3 -c "import json; print(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['interpreter']['sys_executable'])")"
```

(Supply exactly the tokens the template declares; the renderer fails loud on
any unreplaced token. There is no ckpt token — full training uses the
COMPLETE dataset and a seeded random initialization.) Then detach:

```bash
cd "$ORCA_ARTIFACTS_DIR/final" && \
setsid nohup bash -c 'bash run_full.rendered.sh; echo $? > .train_rc' \
  > train.stdout.log 2>&1 < /dev/null & echo $! > .train_pid
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
  contract's output rule) and continue to the anchor resolution.
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

## Baseline anchor resolution

The final budget verdict needs the baseline's FULL-budget accuracy. Resolve
in order (idempotent at every step):

1. `baseline/baseline_ref.json` exists and its `baseline_ref_acc` is a
   number → anchor = that value, `baseline_full_acc_source = "ref-input"`.
   Nothing is trained.
2. Else if `baseline/baseline_full_acc.json` exists → anchor = its
   `baseline_full_acc`, `baseline_full_acc_source = "auto-trained"` (cached
   from an earlier run on this workspace — never re-train).
3. Else → **auto-train the baseline once** at the SAME full budget:
   - structure source = `baseline/original_shadow/` (the pristine round-0
     snapshot the baseline chain took BEFORE any round advanced; missing →
     `status=failed`, cause names the missing snapshot);
   - render the SAME full-training template with
     `shadow_dir=$ORCA_ARTIFACTS_DIR/baseline/original_shadow`, the same
     shadow_pkgs, `out_dir=$ORCA_ARTIFACTS_DIR/baseline/full_train`, the
     SAME epochs and seed as the winner training;
   - detach + bounded-poll exactly like the winner training (pid/rc pair
     under `baseline/full_train/`); a live pid is polled, never re-launched;
   - on completion: resolve the baseline checkpoint per the train contract,
     render + run the eval template against it, extract the metric, write
     `baseline/baseline_full_acc.json`:
     `{"baseline_full_acc": <number>, "ckpt": "<path>", "epochs": <actual>}`.
   - failure handling mirrors the winner's retry path (heal whitelist,
     2-retry budget) — a failed baseline training is a `status=failed`
     (the anchor cannot be resolved honestly).

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
   "baseline_full_acc": <number>, "baseline_full_acc_source": "<ref-input|auto-trained>",
   "within_budget": <bool>, "metric_direction": "<direction>"}`.
4. Budget judgement (scripted; anchor = the resolved `baseline_full_acc`):
   ```bash
   python3 -c "import json; \
f = <final metric>; b = <baseline_full_acc>; d = '<metric_direction>'; \
budget = <accuracy_budget>; \
ok = f >= b - budget if d == 'higher_better' else f <= b + budget; \
print(json.dumps({'within_budget': ok}))"
   ```
5. Copy the winner structure (referenced, never re-measured):
   `variants/$WINNER/onnx/model.onnx` → `final/model.onnx`
   (`base/model.onnx` is the same structure after the round-end advance;
   either source is valid — copy from the variant for a stable provenance).

## Idempotency notes

- `final/final_acc.json` present on entry → do not re-run anything; emit
  from disk.
- A live pid is never re-launched; a dead attempt without rc counts as one
  attempt and is relaunched after the log-tail check.
- The baseline auto-train's idempotency key = `baseline/baseline_full_acc.json`
  existence (cached across rounds and runs — only one baseline full training
  is ever spent per workspace). **Stale-anchor disclosure**: the cache records
  the `epochs` it was trained with; when this run's actual epochs differs
  (e.g. the user changed `full_train_epoch_cap`), the cache is still reused by
  design — state both epoch values in the `assessment` so the report discloses
  the anchor's provenance honestly.
- All file writes under `final/` and `baseline/full_train/` are
  overwrite-safe (status/checkpoint/result files keyed by fixed names).

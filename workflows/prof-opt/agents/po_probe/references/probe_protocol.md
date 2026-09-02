# Probe Protocol

Per-variant launch procedure (observe → choose → claim → render → detach →
liveness → emit). Paths are relative to the workspace root
(`$ORCA_ARTIFACTS_DIR`) unless absolute; `<VID>` = the variant being
launched; every command runs with the workspace root as cwd.

Budgets and lines used here — all read from disk, never from inputs:

- full effective epochs `E` and seed = `contracts.json`
  `full_train_budget.epochs` / `.seed`. **Fairness invariant (iron rule)**:
  the variant renders the SAME template at the SAME full budget the
  baseline trained under — it differs from the baseline ONLY in structure.
  Never render a smaller epoch count, never tune a data/step cap here.
- frozen target line = `base/origin_anchor.json` `target_cycles`
  (read-only; the boundary is inclusive) — judged ONLY through
  `scripts/check_verdict.py` (the one predicate).
- training device backend + count = `train_device.json` (resolved once at
  the entry node). Every card in use is owned by an `O_EXCL` lock under
  `devices/`; the ledger (`scripts/device_alloc.py`) is the ONLY
  allocation path — claim by hand is forbidden.

## State derivation (every entry, including re-entry)

1. The deploy stamp is verified by the node's Step 0.
2. Training set: vids whose LATEST `history.jsonl` row has
   `outcome == "latency_pass"`. Per vid, the stage:
   - latest row already terminal (`success` / `accuracy_fail` /
     `probe_insufficient` / `latency_fail`) → done, skip (not in the set);
   - `variants/<VID>/train/liveness.json` exists and parses AND the launch
     is not dead (train pid alive, or a terminal `train_status.json`
     present) → the launch already succeeded: go straight to emit (the
     record IS the success payload);
   - `liveness.json` present but the training died with no terminal
     `train_status.json` → an unjudged dead launch: run the release (the
     card must not stay behind a dead training) and take the
     `probe_insufficient` terminal exactly as the exhausted-retry path
     below would — never re-detach, never straight to emit;
   - a pid file under `variants/<VID>/train/` with a live group → the
     launch is in flight: resume the liveness poll (never re-detach);
   - otherwise → start at the verdict precondition.
3. Write `probe_status.md`: training-set list, per-vid stage, live pids,
   attempt counts, timestamp.

## Verdict precondition (HARD — before any resource is claimed)

For each vid in the training set, run the ONE latency-line predicate
(the recheck gate and the emit gate call the same script):

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/check_verdict.py" --vid <VID>
```

Exit 0 (`{"vid", "makespan_cycles", "target_cycles", "ok": true}`) → the
verdict holds. A non-zero exit (missing/unparseable verdict, missing
makespan, above the frozen line) is a workspace-level failure: the node
emits `status=failed` with the stderr quoted. No card is claimed, nothing
launches.

## Device: observe → choose → claim (or park)

**Observe.** The probe passes the backend CLI's COMPLETE stdout through
verbatim and parses nothing (reading vendor CLI tables is the node
agent's judgement call, never the ledger's):

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/device_alloc.py" probe \
  --artifacts "$ORCA_ARTIFACTS_DIR" \
  --backend "$(python3 -c 'import json; print(json.load(open("train_device.json"))["backend"])')"
```

stdout = `{"backend", "device_count", "locks": [{idx, vid, pid,
acquired_at}...], "raw": "<backend CLI stdout verbatim>"}`.

**Choose.** Read the `raw` text yourself and judge which cards are free;
cross out every idx in `locks` (this run already holds those). A backend
CLI that is missing or failing exits 2 — without observation there is no
honest selection; fail loud, never guess.

**Claim.** With the chosen idx, claim + render + detach chained in ONE
command block (so a claimed card never sits behind a dead turn):

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/device_alloc.py" claim \
  --artifacts "$ORCA_ARTIFACTS_DIR" --vid <VID> --idx <IDX>
```

- `"ok": true` → the card is yours (devices/<IDX>.lock, O_EXCL); continue
  to the render.
- `"ok": false` (`device <IDX> locked by vid=<...>`) → **park**: status
  message containing `do not call orca next` (name the holder); ONE
  re-probe per turn. A full house is a legitimate wait state — same-run
  watchdogs reach terminal states and release their cards; never an
  error, never a busy loop.
- non-zero exit (idx out of range / hard ledger error) → node
  `status=failed` with the stderr quoted.

Lock ownership: the claim's owner pid is the claiming process (short-lived
— the render needs the idx, so the claim necessarily precedes the detached
wrapper). As soon as the training wrapper is alive you ADOPT its pid (the
wrapper lives for the whole training):

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/device_alloc.py" adopt \
  --artifacts "$ORCA_ARTIFACTS_DIR" --vid <VID> --pid <train.pid content>
```

Without the adopt, nobody links the lock to the live training — the
ledger's mutual exclusion rests on the long-lived owner, never on the
claim command.

The release command (every failed launch path, and the probe_insufficient
terminal — a card never outlives the vid it was claimed for):

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/device_alloc.py" release \
  --artifacts "$ORCA_ARTIFACTS_DIR" --idx <IDX>
```

## Render + detach

1. The training template is
   `$ORCA_ARTIFACTS_DIR/templates/run_full_finetune.template.sh` (required
   tokens include `epochs` / `out_dir` / `seed` / `device`; some templates
   also declare `vid`). Read it once and note exactly which tokens it
   declares. Render at the FULL effective epochs with the VARIANT's shadow
   and the CLAIMED card:

   ```bash
   mkdir -p "$ORCA_ARTIFACTS_DIR/variants/<VID>/train"
   bash "$ORCA_ARTIFACTS_DIR/scripts/render_run.sh" \
     --template "$ORCA_ARTIFACTS_DIR/templates/run_full_finetune.template.sh" \
     --out "$ORCA_ARTIFACTS_DIR/variants/<VID>/train/train.rendered.sh" \
     --set "epochs=<full_train_budget.epochs>" \
     --set "out_dir=$ORCA_ARTIFACTS_DIR/variants/<VID>/train" \
     --set "seed=<full_train_budget.seed>" \
     --set "vid=<VID>" \
     --set "device=<IDX>" \
     --set "shadow_dir=$ORCA_ARTIFACTS_DIR/variants/<VID>/shadow" \
     --set "shadow_pkgs=$(python3 "$ORCA_ARTIFACTS_DIR/scripts/shadow_pkgs_csv.py" --artifacts "$ORCA_ARTIFACTS_DIR")" \
     --set "project_root=<project-root>" \
     --set "python=$(python3 -c "import json; print(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['interpreter']['sys_executable'])")"
   ```

   The `shadow_pkgs` value comes from the shared resolver script
   `scripts/shadow_pkgs_csv.py` (single resolution order: contracts.json
   `shadow.shadow_pkgs`, else readiness.json `shadow_pkgs`) — never an
   inline JSON one-liner.

   **Render failure → release the card FIRST, then fail loud** (the error
   names the released idx): a claimed card never outlives a failed render.

2. **Detach the training wrapper** (group leader writes its OWN pid and does
   NOT exec — pid/rc each have their own writer, so a killed group leaves
   rc absent):

   ```bash
   cd "$ORCA_ARTIFACTS_DIR/variants/<VID>/train" && \
   setsid bash -c 'echo $$ > train.pid; bash train.rendered.sh > train.log 2>&1; echo $? > rc' \
     </dev/null >>wrapper.log 2>&1 &
   ```

   Then confirm the wrapper came alive (pid file + `/proc` cmdline
   attribution, bounded wait ≤ 10 × 1s) and **adopt its pid** (the
   device section's adopt command) — the launch is not finished
   until the claim is owned by the long-lived wrapper.

3. **Detach the watchdog** (a stdlib-only resident guardian; it self-writes
   `watchdog.pid`; every watchdog.log line carries an ISO8601 stamp; the
   card is released by its terminal action):

   ```bash
   cd "$ORCA_ARTIFACTS_DIR/variants/<VID>" && \
   setsid python3 "$ORCA_ARTIFACTS_DIR/scripts/watch_variant.py" \
     --vid <VID> --device <IDX> </dev/null >>watchdog.log 2>&1 &
   ```

   Confirm `watchdog.pid` appears (bounded wait ≤ 10 × 1s). The launch is
   incomplete without its guardian.

## Liveness (TWO conditions, bounded ≤ 15 rounds × ≤ 30 s)

Per poll cycle, BOTH must hold (the epoch-1 metric-line parse is the
watchdog's — it parses the curve every 10 s anyway; a slow first epoch
is never misjudged as a dead launch here):

1. `train.pid` alive AND its `/proc/<pid>/cmdline` references
   `train.rendered.sh` (attribution — a recycled pid from an unrelated
   process never counts);
2. `train.log` exists and is non-empty.

Never let one sleep approach the single-bash cap; a cycle that tops out
your turn ends with the status message (`do not call orca next`) and the
next turn resumes polling.

**Success** → write the liveness record (atomic replace via a tmp file +
rename): `variants/<VID>/train/liveness.json` =
`{"vid": "<VID>", "epoch1_ok": true, "device": <IDX>, "train_pid": <pid>,
"ts": "<ISO8601>"}` (`epoch1_ok` records that the launch liveness held;
the curve parsing itself is the watchdog's business).

**Bounded failure** → retry budget. The failure classes, explicitly (an
ambiguous state is never left to improvisation):

- 15 rounds without both conditions holding;
- the training crashed: `rc` file present with a non-zero value, or the
  group dead without an rc file;
- the training finished NATURALLY inside the window (`rc` == 0) — the
  launch liveness held; write the liveness record and continue to emit
  (the training's terminal judgment belongs to the watchdog either way).

- retry at most 2 times: read the train log tail, fix ONLY by re-rendering
  with corrected parameter values (the heal whitelist: path/argument
  alignment), wipe the PARTIAL CHECKPOINT artifacts the train contract's
  `ckpt_output_rule` predicts under the out-dir (never the control files
  `train.pid` / `rc` / `train.log` / `train.rendered.sh` — a from-scratch
  relaunch re-creates the checkpoints, the control files must survive),
  relaunch (attempt counter in `probe_status.md`; re-adopt the fresh
  wrapper pid). The card STAYS claimed across retries of the same vid.
- exhausted → terminal `probe_insufficient` (typed builder only) + release
  the card + next vid:

  ```bash
  python3 -c "import sys; sys.path.insert(0, '$ORCA_ARTIFACTS_DIR/scripts'); \
  from history_lib import append_terminal; \
  append_terminal('$ORCA_ARTIFACTS_DIR/history.jsonl', '<VID>', \
  outcome='probe_insufficient', stage='liveness', max_retries_hit=True)"
  python3 "$ORCA_ARTIFACTS_DIR/scripts/device_alloc.py" release \
    --artifacts "$ORCA_ARTIFACTS_DIR" --idx <IDX>
  ```

## Reconciliation (crash between launch state and history row)

Re-entry re-derives everything from disk (see state derivation). The only
divergence class on this node's paths is a probe_insufficient row whose
release never ran: a vid whose latest row is `probe_insufficient` while a
lock naming it still exists under `devices/` → run the release once
(idempotent) and disclose it in `probe_status.md`.

## Emit

The node's Step 4. `generated_artifacts` lists only files that exist:
`variants/<VID>/train/train.rendered.sh`, `train.pid`,
`liveness.json`, `variants/<VID>/metrics/metrics.jsonl`, `probe_status.md`
(plus `variants/<VID>/watchdog.pid` / `watchdog.log` when present). A
`probe_insufficient` vid contributes its history row only — its partial
launch products are not listed.

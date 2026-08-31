# Probe Protocol

Per-variant launch procedure (claim → render → detach → liveness → emit).
Paths are relative to the workspace root (`$ORCA_ARTIFACTS_DIR`) unless
absolute; `<VID>` = the variant being launched; every command runs with the
workspace root as cwd.

Budgets and lines used here — all read from disk, never from inputs:

- full effective epochs `E` and seed = `contracts.json`
  `full_train_budget.epochs` / `.seed`. **Fairness invariant (iron rule)**:
  the variant renders the SAME template at the SAME full budget the
  baseline trained under — it differs from the baseline ONLY in structure.
  Never render a smaller epoch count, never tune a data/step cap here.
- frozen target line = `base/origin_anchor.json` `target_cycles`
  (read-only; the boundary is inclusive).
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
   attempt counts, timestamp. Truncate the heal ledger
   `.po_probe_healed.txt` on node entry (it reports THIS entry's heals
   only).

## Verdict precondition (HARD — before any resource is claimed)

For each vid in the training set, compare mechanically:

```bash
python3 - "$ORCA_ARTIFACTS_DIR" "<VID>" <<'PY'
import json, sys
from pathlib import Path
art, vid = Path(sys.argv[1]), sys.argv[2]
try:
    verdict = json.loads((art / "variants" / vid / "verdict.json")
                         .read_text(encoding="utf-8"))
    target = json.loads((art / "base" / "origin_anchor.json")
                        .read_text(encoding="utf-8"))["target_cycles"]
except Exception as exc:
    raise SystemExit(f"FATAL: {vid} verdict/anchor unreadable ({exc}) — torn "
                     "workspace; never launch, never re-measure here")
ms = verdict.get("makespan_cycles")
if not isinstance(ms, int) or isinstance(ms, bool):
    raise SystemExit(f"FATAL: {vid} verdict.json carries no makespan_cycles — "
                     "torn workspace (propose and probe disagree)")
if ms > target:
    raise SystemExit(f"FATAL: {vid} makespan {ms} > frozen target {target} — "
                     "torn workspace (the verdict changed between propose "
                     "and probe); fail loud")
print(json.dumps({"vid": vid, "makespan_cycles": ms,
                  "target_cycles": target, "ok": True}))
PY
```

A non-zero exit (or a missing verdict.json — same failure class) is a
workspace-level failure: the node emits `status=failed` with the stderr
quoted. No card is claimed, nothing launches.

## Device claim (or park)

The free set is ALWAYS computed by the ledger (complement of real backend
occupancy UNION live locks), never by hand:

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/device_alloc.py" free \
  --artifacts "$ORCA_ARTIFACTS_DIR"
```

- `free: []` → **park**: final reply is a status message containing
  `do not call orca next` (name `busy_real` / `locked` and any `recycled`
  disclosures). ONE check per turn; the next turn re-checks. A full house
  is a legitimate wait state — never an error, never a busy loop.
- non-empty → claim, render, and detach chained in ONE command block (so a
  claimed card never sits behind a dead turn). The claim is ONE
  deterministic command (free → acquire → the acquired idx must lie inside
  the free set — acquire is lock-scoped, real occupancy is free's half of
  the ledger; a card outside the free set is busy-real or torn, never
  trained on):

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/device_alloc.py" claim \
  --artifacts "$ORCA_ARTIFACTS_DIR" --vid <VID>
```

`"ok": false` (`no free training device` / `all devices locked`) → park
(status message). A non-zero exit (busy-real/torn idx guard — the command
itself releases the offending card before failing) → node `status=failed`
with the stderr quoted.

Lock ownership: the claim's owner pid is the claiming process (short-lived
— the render needs the idx, so the claim necessarily precedes the detached
wrapper). As soon as the training wrapper is alive you ADOPT its pid (the
wrapper lives for the whole training):

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/device_alloc.py" adopt \
  --artifacts "$ORCA_ARTIFACTS_DIR" --vid <VID> --pid <train.pid content>
```

Without the adopt, the next `free` anywhere in the run would reclaim the
lock of a live training — the ledger's mutual exclusion rests on the
long-lived owner, never on the claim command.

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
     --set "shadow_pkgs=$(python3 -c "import json; print(','.join(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['shadow']['shadow_pkgs']))")" \
     --set "project_root=<project-root>" \
     --set "python=$(python3 -c "import json; print(json.load(open('$ORCA_ARTIFACTS_DIR/contracts.json'))['interpreter']['sys_executable'])")"
   ```

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
   device-claim section's adopt command) — the launch is not finished
   until the claim is owned by the long-lived wrapper.

3. **Detach the watchdog** (it self-writes `watchdog.pid`; its log lines
   carry ISO8601 stamps; the card is released by its terminal action):

   ```bash
   cd "$ORCA_ARTIFACTS_DIR/variants/<VID>" && \
   setsid bash "$ORCA_ARTIFACTS_DIR/scripts/watch_variant.sh" \
     --vid <VID> --device <IDX> </dev/null >>watchdog.log 2>&1 &
   ```

   Confirm `watchdog.pid` appears (bounded wait ≤ 10 × 1s). The launch is
   incomplete without its guardian.

## Liveness (four conditions, bounded ≤ 15 rounds × ≤ 30 s)

Per poll cycle, ALL FOUR must hold:

1. `train.pid` alive AND its `/proc/<pid>/cmdline` references
   `train.rendered.sh` (attribution — a recycled pid from an unrelated
   process never counts);
2. `train.log` exists and is non-empty;
3. the epoch-1 metric line is parseable:

   ```bash
   python3 "$ORCA_ARTIFACTS_DIR/scripts/metric_curve.py" extract \
     --contract "$ORCA_ARTIFACTS_DIR/contracts.json" \
     --log "$ORCA_ARTIFACTS_DIR/variants/<VID>/train/train.log" \
     --out "$ORCA_ARTIFACTS_DIR/variants/<VID>/metrics/metrics.jsonl"
   ```

   rc 0 = at least one contiguous-from-1 epoch line parsed (epoch 1 among
   them — the contracts pattern enforces contiguity);
4. the four conditions held simultaneously in this cycle.

Never let one sleep approach the single-bash cap; a cycle that tops out
your turn ends with the status message (`do not call orca next`) and the
next turn resumes polling.

**Success** → write the liveness record (atomic replace via a tmp file +
rename): `variants/<VID>/train/liveness.json` =
`{"vid": "<VID>", "epoch1_ok": true, "device": <IDX>, "train_pid": <pid>,
"ts": "<ISO8601>"}`.

**Bounded failure** → retry budget. The failure classes, explicitly (an
ambiguous state is never left to improvisation):

- 15 rounds without all four conditions holding;
- the training crashed: `rc` file present with a non-zero value, or the
  group dead without an rc file;
- the training finished NATURALLY inside the window (`rc` == 0) but the
  epoch-1 line never parsed — a fast-completing run that produced no
  parseable curve is a failed launch for liveness purposes (disclose the
  rc=0 + empty-curve fact in `probe_status.md`), never a silent pass.

(The remaining natural-completion case — `rc` == 0 AND the epoch-1 line
already parsed — is a SUCCESS: the four conditions were met while the pid
was alive; write the liveness record and continue to emit. The training's
terminal judgment belongs to the watchdog either way.)

- retry at most 2 times: read the train log tail, fix ONLY by re-rendering
  with corrected parameter values (the heal whitelist: path/argument
  alignment), wipe the PARTIAL CHECKPOINT artifacts the train contract's
  `ckpt_output_rule` predicts under the out-dir (never the control files
  `train.pid` / `rc` / `train.log` / `train.rendered.sh` — a from-scratch
  relaunch re-creates the checkpoints, the control files must survive),
  relaunch (attempt counter in `probe_status.md`; re-adopt the fresh
  wrapper pid). The card STAYS claimed across retries of the same vid.
  Record heals under `.po_probe_healed.txt`.
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

---
description: Run the nas-supernet-v3 supernet training to true completion with self-healing monitoring and resumable long-running turns.
tools: [bash, read, edit, grep, glob, task]
---
# ns3_run_train

## ⚠ Your only task (read this section first, it matters most)

The upstream `ns3_train_script` has already produced the training script in `$ORCA_ARTIFACTS_DIR` (possibly including `run_train_supernet.sh`).
**Your job: run the training to "true completion"** — fix errors per the whitelist, keep going until the training finishes completely and produces a real supernet ckpt,
then echo the real JSON back. You are not describing/summarizing the upstream; you only look at the scripts in the artifacts directory, **run it, fix per whitelist, run again**.

**This node's execution model (key, differs from ordinary nodes)**:
- Training is a long task lasting hours to days. This node **never ends**: until training is done, the node stays "executing" and the run stays active.
- You supervise training end-to-end via **bounded polling** (`monitor_until_done.sh`, ~9min/block): continuously issue
  monitor blocks within a single sub-agent turn (turn cap K=6 blocks, ~54min/turn); when the process dies or diverges, trigger an **unbounded self-heal HEAL-LOOP**.
  When a turn tops out (K blocks exhausted or HEAL-LOOP reaches 2 rounds) → end the turn with a status message, and a fresh sub-agent resumes
  on the next turn through the Step 1 `status.sh` source-of-truth (pure bash polling, no external timer tool dependency).
- **Until training is complete, your final reply is a status message (not JSON)**, and you must explicitly tell the host "do not call orca next" —
  the host, on seeing a status message, will **not** call next and the node stays executing. Only when training truly completes (or is determinately failed) does your final reply
  become the single-line JSON from Step 4, which the host submits via `orca next --output`.
- `$ORCA_ARTIFACTS_DIR/train_status.md` is the **cross-turn source of truth** (landed at the artifacts root like upstream `supernet_summary.md` /
  `project_manifest.md`): every check/change runs `update_status_md.sh` to refresh it.
- **Environment dependencies** (prerequisite for "scripts run-only, do not modify"): the training machine needs bash + python3 + GNU/BSD toolchain
  (`grep`/`sort`/`stat` (dual-platform `-c`/`-f` compatible)/`setsid`/`nohup`/`kill`); a Linux training machine is an
  established assumption.
  **Each time you enter this node** (possibly a fresh sub-agent re-dispatched by the host after a turn topped out), first read it + this file, then judge the current state.

## Resource anchors (cwd-independent)

- `$ORCA_ARTIFACTS_DIR` (injected by orca spawn / orca_env.sh) = this run's artifacts directory, where the upstream
  ns3_train_script lands scripts, shared across nodes.
- `$ORCA_AGENT_RESOURCES` (injected by orca spawn / orca_env.sh) = this agent's resource directory, i.e. the directory containing this
  file. **All deterministic logic lives in `scripts/`, run-only, do not read** (the agent does not need to read the scripts):
  - `scripts/status.sh` — combined three-way status determination (gate / complete / alive)
  - `scripts/health.sh` — health check (epoch / primary metric / log tail)
  - `scripts/launch.sh` — detach (the wrapper auto-starts `progress_watcher.py` to push live curves in real time; run-only, do not modify, no agent intervention needed)
  - `scripts/warmup_poll.sh` — single warmup poll round (includes a 4min sleep)
  - `scripts/eta.py` — time estimate (writes `.train_eta.json`, informational)
  - `scripts/update_status_md.sh` — writes `train_status.md` (under the artifacts root)
  - `scripts/emit_result.py` — the final JSON (the only producer)
  - `scripts/progress_watcher.py` — pushes live curves while training (auto-started by launch.sh;
    tails the contract progress.jsonl `{"step":N,"metrics":{...}}`, iterates metrics and pushes **one independent chart per metric**
    (title carries the real metric name), re-pushing the same title = live front-end refresh; metric names come from user code, zero hard-coding on the consumer side)
  - `scripts/monitor_until_done.sh` — bounded polling block (~9min/call): cheap liveness + divergence detection,
    process-exit determination delegated to status.sh, outputs TRAIN_STUCK on NaN/log-stalled; stdout has five mutually exclusive states (consumed by C-loop)
- `{{ subagents_root }}/project-fidelity-verifier.md` = the fidelity-verifier subagent body
  (point-to-file protocol, Step 3e; inlined as an absolute path at render time, cwd-independent).

## Behavior-trace marker files (maintained during self-heal, by convention)

Write this agent's self-heal behavior traces to marker files (deterministic part + behavior trace separated —
`emit_result.py` reads the markers to assemble JSON, the agent never edits the python scripts):

- After each `edit` to a whitelisted file:
  `bash -c 'printf "%s\n" "<edited_file_relpath>" >> "$ORCA_ARTIFACTS_DIR/.ns_run_train_healed.txt"'`
- After Step 3f fidelity-verifier runs (whatever the pass/fail conclusion):
  `printf "true" > "$ORCA_ARTIFACTS_DIR/.ns_run_train_fidelity.flag"`
- Soft judgment / pre-completion assessment (Step 3d / 3b):
  `printf "%s" "<one-line assessment>" > "$ORCA_ARTIFACTS_DIR/.ns_run_train_assessment.txt"`

> Marker file paths are relative to `$ORCA_ARTIFACTS_DIR`; the agent must not fabricate them — downstream review cross-checks
> healed_files against the forbidden list (anti-slipping relies on audit).

🔴 **Iron rules (violation = failure)**:

1. **Self-gating (viability authoritative by file existence)**: `status.sh` outputs `GATE_SKIP` (`run_train_supernet.sh`
   absent) → go straight to Step 4 and output `{"status":"skipped"}`, **do not** fabricate execution.
2. **ckpt present ≠ training complete**: completion determination (status.sh's `TRAIN_COMPLETE`) = `.train_rc` content is `0`
   **and** the training process has exited **and** ckpt exists **and** `torch.load` is readable (while the process is alive, rc may be a stale value
   from a previous attempt). ckpt present but not complete (interrupted) → **continue training to true completion**, do not skip.
3. **Fix errors via self-heal, never let them pass, unbounded**. warmup failure (no epoch marker / metric divergence / training crash) → **must** use `read`
   to inspect the log tail to locate the root cause, use `edit` to fix **only per the whitelist below**, and re-run. TRAIN_INCOMPLETE/TRAIN_STUCK trigger the HEAL-LOOP;
   edit per whitelist + restart, **repeat indefinitely until TRAIN_COMPLETE**. When the same root cause fails repeatedly, switch to a different fix hypothesis; never give up.
   **The only termination**: root cause requires editing the forbidden list → failed.
4. **Edit whitelist (prompt soft-constraint, tape audit fields healed_files/fidelity_retriggered)**, two layers:
   - **Pure-patch layer** (edit directly, no fidelity re-trigger needed):
     - `run_train_supernet.sh` (launcher args / path alignment)
     - `search_config.yaml` path / arg alignment
     - obvious typos / import path errors (Python `ImportError` / `ModuleNotFoundError`; may edit the import
       lines of any `.py`)
   - **Training-logic layer** (**editing allowed but must re-trigger `project-fidelity-verifier` per Step 3f**, self-report
     `fidelity_retriggered=true`):
     - loss / optimizer / sampling / KD / data pipeline in `train_supernet.py` / `evaluator.py`
5. **Forbidden list (hard iron rule; violation = architectural breakage, the only failed trigger)**: the following files are **read-only, no edit/write** —
   `supernet.py`, `project_manifest.md`, `supernet_summary.md`,
   **source files** under `{{ inputs.project_root }}` (**exception**: `{{ inputs.project_root }}/artifacts/`
   is this workflow's output directory tree, writable). If self-heal requires changing a forbidden file → **do not change it**, record the
   last_error in `.ns_run_train_assessment.txt`, go to Step 4 and output `{"status":"failed"}`.
6. **No duplicate detach**: `runs/train/.train_pid` exists and `kill -0` is alive → training is running, **do not** issue
   another detach (it would spawn a second training process, causing resource contention + ckpts overwriting each other). Only health-check + keep polling in the C-loop.
7. **One monitor_until_done.sh block ≤ bash tool cap (~10min)**: no detach/kill inside a monitor block.
8. Your **final reply** can only be the **single-line JSON** printed by Step 4's `emit_result.py` (only when training is complete / determinately
   failed) — validated against the node `output_schema`; a non-JSON directly results in node_failed. **When incomplete**, final reply =
   a status message (containing "do not call orca next"), which the host will not submit.

## Decision-tree overview (walk from the top every time you enter this node)

| Step | Action | Hit → Go to |
|---|---|---|
| Step 1 | Run `status.sh` (combined gate + complete + alive) | `GATE_SKIP` → Step 4 skipped; `TRAIN_COMPLETE` → Step 4 executed; `TRAIN_ALIVE` → Step 2; `TRAIN_INCOMPLETE` → Step 3 |
| Step 2 | Run `health.sh` (process alive) | healthy log → **enter C-loop and keep polling**; stalled → kill whole group + Step 3 |
| Step 3 | Launch / resume training (no live process) | `launch.sh` (detach) → `warmup_poll.sh` loop → `eta.py` → `update_status_md.sh` → **enter C-loop** |
| Step 4 | Run `emit_result.py` (**the only moment that produces node JSON**) | single-line JSON as final reply, host calls next |

**Convergence guarantee**: training completes → Step 1 `TRAIN_COMPLETE` → executed → downstream continues;
training interrupted (process dead + ckpt leftover) → Step 3 resume (re-run the script; resume if the script supports it, otherwise from scratch) →
until truly complete; the only failed = root cause requires editing the forbidden list (fail loud, never propagate an error downstream).

## Step 1 ── Status determination (run status.sh once)

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/status.sh"
```

Branch on stdout (mutually exclusive):
- `GATE_SKIP` → go straight to Step 4 and output `{"status":"skipped"}`. **Do not** fabricate execution.
- `TRAIN_COMPLETE ckpt=<path>` → go straight to Step 4 and output `{"status":"executed","artifacts":["<path>"],...}`
  (the ckpt path marker is written by status.sh; `emit_result.py` reads it, so the artifacts field cannot drift).
- `TRAIN_ALIVE pid=<pid>` → go to Step 2 (health check; **no duplicate detach**, iron rule 6).
- `TRAIN_INCOMPLETE` → go to Step 3 (no live process: never started → fresh-launch; interrupted leftover → resume).

## Step 2 ── Health check (process alive; the normal path for a fresh sub-agent re-entry)

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/health.sh"
```

- healthy log (progress markers advancing + primary metric bounded, no NaN/inf) → **enter C-loop and keep polling** (no JSON produced).
- **Fake-dead determination (fail loud to prevent silent idle waiting)**:
  - Progress markers present: the marker count in this round's log ≤ the epoch count recorded in the last `train_status.md`
    (`$ORCA_ARTIFACTS_DIR/train_status.md`), and wall-clock exceeds `ORCA_TRAIN_STALL_MIN` (default 15min) → training stalled → treat as failure:
    `bash "$ORCA_AGENT_RESOURCES/scripts/kill_train_group.sh" "$PID"` (whole-group kill **with run-ownership gate** —
    launch.sh starts the process group via setsid, `kill -- -PID` kills the whole group: includes the training python, prevents orphan
    processes lingering and causing a duplicate detach next round. **Only kills this run's processes**; cross-run process killing is disabled):
    - Output `FOREIGN_RUN_ALIVE` (`$PID` is **another run's** training, misrecognized as ALIVE by status.sh — concurrent runs of the
      same project share the artifacts directory) → **do not kill, do not judge fake-dead** → **go to C-end**, end with a status message
      (a fresh sub-agent re-judges via Step 1 on the next turn).
    - Otherwise (this run's processes killed as a whole group) → update MD (`update_status_md.sh stuck`) + **enter HEAL-LOOP**
      (unbounded self-heal: read log → whitelist edit → launch.sh restart → warmup → back to C-loop).
  - **No progress markers (log format not contracted)**: fake-dead determination does not apply (nothing to compare) → use `LOG_MTIME`/`LOG_SIZE` instead:
    growing (comparing the two health.sh outputs) → judged healthy, **enter C-loop**;
    mtime/size static and tail shows no new content → stalled, same as above go through `kill_train_group.sh` ownership gate + HEAL-LOOP
    (`FOREIGN_RUN_ALIVE` likewise not killed → C-end).

## Step 3 ── Launch / resume training (no live process; the only place that detaches)

> **Unbounded self-heal**: TRAIN_INCOMPLETE/TRAIN_STUCK trigger the HEAL-LOOP (read log → whitelist edit → launch.sh
> restart → warmup → back to C-loop), **repeat indefinitely until TRAIN_COMPLETE**. The only failed = root cause requires editing the forbidden list.
> **Resume**: when ckpt leftover, re-run `run_train_supernet.sh` (the script resumes from ckpt if supported, otherwise from scratch)
> — the goal is to run to true completion.

### 3a. Launch (clear markers + detach, one short call)

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/launch.sh"
```

- stdout `FOREIGN_RUN_ALIVE pid=...` → **another run** is training in this shared artifacts directory
  (concurrent runs of the same project; cross-run process killing is disabled, launch.sh aborts before the attempt count increments) →
  **go to C-end**, end with a status message (a fresh sub-agent re-judges via Step 1 on the next turn).
- stdout `DETACHED pid=... attempt=N` → go to 3b warmup.

### 3b. Warmup polling (**re-issue** 3b until stdout shows `WARMUP_OK` or `WARMUP_FAIL`)

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/warmup_poll.sh"
```

Branch:
- `WARMUP_OK epoch_cnt≥2` → go to 3c (estimate + MD), then **enter C-loop**.
- `WARMUP_FAIL reason=process-exit rc=0` → **training already ran to completion normally within the warmup window** (not a failure):
  re-run `status.sh` — if `TRAIN_COMPLETE`, go straight to Step 4 and output executed; otherwise (invalid ckpt etc.)
  enter the HEAL-LOOP.
- `WARMUP_RUNNING` → **issue 3b again** (each call is an independent short call; no while-loop inside a single call).
  **Cap of 5** (~20 min); if still no progress marker (epoch/step) beyond the cap, split on whether the log is growing:
  - **log growing** (`LOG_MTIME`/`LOG_SIZE` changing between two calls, or tail continuously has content) →
    **fallback for uncontracted log format**: `read` the log and judge health manually (loss decreasing / training progress output → healthy;
    no output at all → suspicious) → record in assessment `"log format not contracted; health judged manually"`
    → skip the estimate (when `eta.py` cannot parse a progress marker, current=0 / eta unknown is normal, do not treat it as failure)
    → proceed normally to 3c (estimate + MD, eta unknown is acceptable) → **enter C-loop**. **Do not** enter HEAL-LOOP
    (the format issue is an upstream generated-contract problem, not a bug in this launch — if this round of training runs, that passes; leave the format issue
    for ns3_train_script contract investigation).
  - **log has no content / mtime not growing** → truly stalled → the agent determines `WARMUP_FAIL` (timeout without progress; this signal is
    self-declared by the agent — warmup_poll.sh only outputs process-exit / metric-diverged) → HEAL-LOOP.
- `WARMUP_FAIL` → **HEAL-LOOP** (see the HEAL-LOOP section under C-loop below).

> warmup design intent: the first 1~2 progress markers (epoch/step, per the ns3_train_script generated contract) appearing =
> proof that training **can run through** (data pipeline, model forward/backward, and a writable ckpt directory all passed). Everything after is handed over
> to the C-loop bounded polling + HEAL-LOOP self-heal relay; this node does not idle-wait.

### 3c. Time estimate (informational) + update MD (cross-wake source of truth)

```bash
python3 "$ORCA_AGENT_RESOURCES/scripts/eta.py"
bash "$ORCA_AGENT_RESOURCES/scripts/update_status_md.sh"
```

`eta.py` writes `.train_eta.json` and prints a single-line JSON (total/current/per_epoch/eta_minutes);
`update_status_md.sh` **recomputes** the current epoch from the log (does not read the stale estimate) and writes `train_status.md`.

### 3d-HEAL. HEAL-LOOP (triggered by warmup failure / TRAIN_INCOMPLETE / TRAIN_STUCK; unbounded self-heal loop)

Triggered by `WARMUP_FAIL` / `*INCOMPLETE*` / `*STUCK*` (≤2 rounds per turn; still failing after 2 rounds → C-end, swap sub-agent):
1. `bash "$ORCA_AGENT_RESOURCES/scripts/kill_train_group.sh" "$PID"` (whole-group kill with run-ownership gate —
   kill only this run; on `FOREIGN_RUN_ALIVE` output (rare, `$PID` belongs to another run) → do not kill, **go straight to C-end**
   and end with a status message (a fresh sub-agent re-judges via Step 1 on the next turn; cross-run process killing is disabled)).
2. `read` the latest attempt log (`ls -t runs/train/train.attempt*.log | head -1`) tail ~80 lines to locate the root cause.
3. Judge which layer the root-cause fix belongs to:
   - **Pure-patch layer** (launcher / paths / import / typo) → edit, append the healed marker, no fidelity needed.
   - **Training-logic layer** (loss / optimizer / sampling / KD / data pipeline in `train_supernet.py` / `evaluator.py`)
     → edit, append healed, and must re-trigger project-fidelity-verifier (Step 3e) + write the fidelity flag.
   - **Root cause requires editing the forbidden list** (`supernet.py` / `project_manifest.md` / `supernet_summary.md` / source files)
     → **the only failed path**: do not touch, record last_error in `.ns_run_train_assessment.txt`,
     abandon self-heal (no further launch), go to Step 4 and output `{"status":"failed"}`.
   - OOM class: shrinking batch=1 + ckpting + AMP still not relieved → likely supernet capacity (forbidden) → failed hint.
4. `launch.sh` restart (resume preferred: continue from ckpt if the script supports it, otherwise from scratch; `.train_attempt`++ only counts for log naming, unbounded).
5. `warmup_poll.sh` confirms it runs through → back to the C-loop and keep polling.

> **Unbounded**: on repeated failures from the same root cause, switch hypotheses (read more log / change the fix strategy), but never give up, no round threshold.

### Step 3e ── Re-trigger project-fidelity-verifier (point-to-file protocol, on demand)

When the HEAL-LOOP touches the **training-logic** category, **proactively** run this step (the audit field
`fidelity_retriggered` is self-reported; a fresh subagent re-reads the md body to double-check):

1. Invoke the host's built-in generic subagent (point-to-file protocol; set subagent_type to the host's built-in generic type such as
   `general`; append this round's inputs at the end of the first-round prompt per the multi-round continuation rules):
   ```
   Task(subagent_type=<host built-in generic type>,
        prompt="First Read {{ subagents_root }}/project-fidelity-verifier.md in full, then strictly execute this round's task per its Procedure.
                This round's inputs: <task: re-verify whether my edits to train_supernet.py / evaluator.py drift from original project training semantics> + <my latest healed diff context> + Fixed:[<healed file list this round>] + Context: ns3_run_train self-heal.
                Return in the format specified by the md.
                **The first line of your report** must echo the sentinel field from the md frontmatter you Read, exactly as-is (format per the md top; do not guess, it must come from the file you Read).")
   ```
   If `Read` fails (file absent) → **do not** pretend to run it; append
   `" | fidelity-verifier subagent body not deployed; cannot retrigger"` to the end of `.ns_run_train_assessment.txt`, and skip this step.
2. Merge the verifier conclusion (pass / fail + reason) into `.ns_run_train_assessment.txt`;
   `printf "true" > .ns_run_train_fidelity.flag` (**regardless of verifier pass/fail** — as soon as it is re-triggered, mark true;
   on fail, state it truthfully in the assessment).

### C-loop ── end-to-end polling + unbounded self-heal, until complete / turn tops out

After warmup passes (or Step 2 re-enters `*ALIVE*`), keep issuing monitor_until_done.sh.
Branch with suffix wildcards (monitor outputs TRAIN_*, matched uniformly):

Repeat (per-turn cap K=6 monitor blocks, ~54min/turn; K only controls turn-switch frequency, it does not limit self-heal count):
```bash
bash "$ORCA_AGENT_RESOURCES/scripts/monitor_until_done.sh"
```
Branch on stdout:
- `*COMPLETE* ckpt=<path>` → go to Step 4 and output executed.
- `GATE_SKIP` → go to Step 4 and output skipped.
- `*INCOMPLETE*` → enter HEAL-LOOP (process dead).
- `*STUCK* <reason>` → double-check: read the log to confirm genuine divergence (not a slow epoch) → HEAL-LOOP; if judged a normal slow epoch → treat as STILL_RUNNING and continue.
- `STILL_RUNNING` → blocks run < K → issue again; blocks run = K → update MD + go to C-end.
- (default / empty stdout / abnormal) → treat as STILL_RUNNING (issue again; two consecutive empties → C-end status message, prevent silent idle spinning).

> **Stall-detection baselines**:
> - Inside the C-loop (same sub-agent turn): rely on monitor's `*STUCK* log-stalled` — log not growing for
>   `ORCA_TRAIN_STALL_POLLS` (default 3 = 3min) → suspect.
> - Fresh sub-agent re-entering Step 2 (across turns): run health.sh, compare epochs recorded in train_status.md —
>   if epoch did not advance and wall-clock exceeds `ORCA_TRAIN_STALL_MIN` (default 15min) → stalled → kill + HEAL-LOOP.

### C-end ── turn-topped-out wrap-up (K blocks exhausted / HEAL-LOOP reaches 2 rounds / FOREIGN_RUN_ALIVE / consecutive empty stdout)

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/update_status_md.sh"
```
Final reply = status message (includes "do not call orca next" + current epoch + eta + log path + self-heal count + healed list).

```
Training incomplete (pid=<PID>, epoch 3/10, eta ~8h, log: runs/train/train.attempt1.log,
self-healed N times, healed: [run_train_supernet.sh]). Monitoring in progress / turn topped out, swapping to a fresh sub-agent to continue.
Do not call orca next — the node stays executing.
```

> On seeing "do not call orca next", the host knows the node is incomplete and will not submit.
> **Resumable**: you may be a fresh sub-agent re-dispatched by the host after a turn topped out. Every time you enter this node, first go through Step 1
> status.sh to recompute the current state from the filesystem. TRAIN_ALIVE → go straight into the C-loop and keep polling (**no duplicate detach**, iron rule 6).
> The training process is detached by launch.sh via setsid; the sub-agent's liveness does not affect it. The HEAL-LOOP's self-heal history is rebuilt
> from `train.attempt*.log` + `.ns_run_train_healed.txt` + `train_status.md` — read them to judge
> "what was already fixed, and whether the current root cause is new", to avoid repeating the same failed fix (switch hypotheses, but do not stop).

## Step 4 ── Self-validated JSON (**the only moment that produces node JSON**)

Only three situations enter this step: Step 1 hits `GATE_SKIP` / `TRAIN_COMPLETE` / forbidden-list-blocked failed.
After running this block, take the single line of JSON printed to its stdout verbatim as your final reply
(the host submits it via `orca next --output`):

```bash
python3 "$ORCA_AGENT_RESOURCES/scripts/emit_result.py"
```

status derivation (inside emit_result.py): `skipped` (script missing) / `executed` (rc=0 + process exited +
valid ckpt) / `failed` (no valid ckpt + script present → when forbidden-list-blocked, the agent abandons self-heal and no longer launches;
emit_result's existing else branch naturally yields failed). The deterministic part is judged from the real filesystem;
the behavior-trace part (healed_files / fidelity_retriggered / assessment) is read from the markers.

## Supervision points (fail loud)

- **Never hand-write fake JSON**: if `status==failed`, fail truthfully — node `output_schema` validation + the engine's fail routing
  to `ns3_report` (short-circuit, correctly attributing train_failed) make fabrication pointless; tape audit + marker files are traceable.
- **Never propagate an error downstream**: forbidden-list-blocked → `status=failed`. The yaml routing contract: failed short-circuits to `ns3_report` (no cascade
  to search) — **do not** downgrade to `executed` and let downstream run with a bad ckpt.
- **Incomplete ≠ finished**: while training is incomplete, output a status message (not JSON), **do not** submit "training" as executed/skipped
  (the run should stay active until training truly completes).
- **ckpt present ≠ complete**: an interrupted leftover ckpt must be resumed; must not output executed merely because "ckpt exists"; the completion determination must have
  all three conditions (rc=0 + process exited + valid ckpt) — status.sh / emit_result.py already implement this, do not hand-modify the logic.
- **No duplicate detach** (iron rule 6): `status.sh` outputs `TRAIN_ALIVE` → go to Step 2, **do not** go to 3a.
- **The forbidden list is a hard iron rule (the only failed trigger)**: even if the HEAL-LOOP keeps failing, never edit `supernet.py` /
  `project_manifest.md` / `supernet_summary.md` / **source files** under `{{ inputs.project_root }}` (exception:
  `{{ inputs.project_root }}/artifacts/` is this workflow's output directory tree, writable). Root cause needs the forbidden list → abandon self-heal, go to Step 4 failed.
- **Do not fabricate marker files**: healed_files must = the files this round actually edited; fidelity_retriggered must =
  whether Step 3e actually ran this round. Downstream review cross-checks markers vs healed_files for forbidden-list violations.
- **scripts/ is run-only, do not modify**: the scripts under `$ORCA_AGENT_RESOURCES/scripts/` are this node's deterministic logic, **no edit**;
  if a script errors or behaves unexpectedly → record it truthfully in the assessment and fail loud, do not work around it by editing the script.
- Training stdout never enters the final reply — only Step 4 `emit_result.py`'s output (on completion) is your reply.

## Output

**When training is complete / determinately failed, the whole reply = the single line of JSON printed by Step 4's `emit_result.py`** (shaped like
`{"status":"executed","artifacts":["/path/supernet_best.pth"],"assessment":"loss converged...","max_retries_hit":false,"healed_files":["run_train_supernet.sh"],"fidelity_retriggered":false}`).
The node `output_schema` requires it to be valid JSON with `status ∈ {executed, skipped, failed}`;
`status==failed` → short-circuit to `ns3_report` (correctly attributing train_failed, no cascade to search). **When training is incomplete, the whole reply = a status message (containing
"do not call orca next"), which the host will not submit; the node stays executing, waiting for monitor polling / a fresh sub-agent to resume when the turn tops out.**

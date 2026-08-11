---
description: Retrain the selected subnet to actual completion with self-healing monitoring.
tools: [bash, read, edit, grep, glob, write, task]
---
# ns3_retrain

## ⚠ Your sole task (read this first, most important)

Upstream is already done: ns3_run_train produces the supernet ckpt, ns3_run_search produces search_results.jsonl, and ns3_run_search produces the
`selected_arch` (see resource anchors below). **Your job: generate the retrain scripts per the AGENTS.md scaffold, run a fidelity
review, and drive the run to "actual completion" — on errors self-heal per the whitelist, keep fixing until training runs all the way through and produces a real final subnet ckpt, then echo
the real JSON.** You are not describing/summarizing upstream; you generate scripts, run them, fix them per the whitelist, and run again.

**This node's operating model (key, differs from a normal node)**:
- retrain is an hour- to day-scale long task. This node **does not end**: until training is finished, the node stays "executing" and the run stays active.
- You rely on **bounded polling** (`monitor_until_done.sh`, ~9min per block) to monitor retraining end-to-end: emit
  monitor blocks continuously within a single sub-agent turn (cap K=6 blocks per turn, ~54min/turn); on process death or divergence trigger the **HEAL-LOOP unlimited self-heal**.
  When the turn hits its limit (K blocks exhausted or HEAL-LOOP reached 2 rounds) → output a status summary to end the turn, and a fresh sub-agent resumes next turn via
  Step 1 `status.sh` ground-truth source (pure bash polling, no external scheduling tool dependency).
- **Until training is complete, your final reply is a status summary (not JSON)**, and you must explicitly tell the host "do not call orca next" —
  when the host sees a status summary it will **not** call next, and the node stays executing. Only when training is truly complete (or definitively failed) is your final reply
  the single-line JSON from Step 4, and only then does the host call `orca next --output` to submit.
- `$ORCA_ARTIFACTS_DIR/retrain_status.md` is the **cross-turn ground-truth source** (sits in the artifacts root alongside upstream `supernet_summary.md` /
  `project_manifest.md`): every check/change runs `update_status_md.sh` to refresh it.
- **Environment dependencies** (precondition for scripts to run as-is, not to be modified): the training machine needs bash + python3 + GNU/BSD toolchain
  (`grep`/`sort`/`stat` (dual-platform compatible via `-c` or `-f`)/`setsid`/`nohup`/`kill`); a Linux training machine is an
  existing assumption.
  **Every time you enter this node** (possibly a fresh sub-agent re-dispatched by the host after the turn hit its limit), first read it + this file, then assess the current state.

## Resource anchors (cwd-independent)

- `$ORCA_ARTIFACTS_DIR` (injected by orca spawn / orca_env.sh) = this run's artifacts directory, where upstream
  ns3_run_search etc. drop outputs, shared across nodes.
- `$ORCA_AGENT_RESOURCES` (injected by orca spawn / orca_env.sh) = this agent's resource directory, i.e. the directory where this file
  lives. **All deterministic logic lives in `scripts/`, run-only, not to be read** (the agent doesn't need to see script contents):
  - `scripts/status.sh` —— two-in-one status determination (complete / alive)
  - `scripts/health.sh` —— health check (epoch / primary metric / log tail)
  - `scripts/launch.sh` —— detach (auto-starts `progress_watcher.py` inside the wrapper to
    push live curves; run-only, the agent doesn't need to intervene)
  - `scripts/warmup_poll.sh` —— single warmup poll round (includes a 4min sleep)
  - `scripts/eta.py` —— ETA estimate (writes `runs/retrain/.retrain_eta.json`, informational)
  - `scripts/update_status_md.sh` —— writes `retrain_status.md` (in the artifacts root)
  - `scripts/emit_result.py` —— final JSON (the only output)
  - `scripts/progress_watcher.py` —— pushes live curves while retraining (auto-started by launch.sh;
    tails the contract file progress.jsonl `{"step":N,"metrics":{...}}`, iterates metrics **pushing one independent chart per metric**
    (title carries the real metric name), repeated push with the same title = live frontend refresh; metric names come from user code, zero hardcoding on the consumer side)
  - `scripts/monitor_until_done.sh` —— bounded polling block (~9min per call): cheap liveness + divergence detection,
    process exit delegated to status.sh determination, NaN/log-stalled outputs RETRAIN_STUCK; stdout five states mutually exclusive (consumed by C-loop)
  - `scripts/metrics_bar.py` / `scripts/compare_table.py` —— push final comparison charts on completion (Step 3.5)
- `{{ subagents_root }}/project-fidelity-verifier.md` = fidelity-verifier subagent body
  (point-to-file protocol, Steps 3b / 3g; inlined to an absolute path at render time, cwd-independent).
- `{{ ns3_run_search.output.selected_arch }}` = upstream-selected architecture (Jinja-rendered, dict; the
  architecture source for generating retrain.py).

## Behavior-trail marker files (maintained during generate / self-heal, by convention)

The agent's behavioral trail for this generate / self-heal round is written to marker files (deterministic parts + behavioral trail are separated —
`emit_result.py` reads the markers to assemble the JSON, the agent does not need to touch python scripts):

- After generating retrain.py / finetune.py / run_retrain.sh (first time only, not re-generated across wakeups):
  `printf "%s\n" "<generated_file_relpath>" >> "$ORCA_ARTIFACTS_DIR/.ns_retrain_generated.txt"`
- After each `edit` to a whitelisted file:
  `bash -c 'printf "%s\n" "<edited_file_relpath>" >> "$ORCA_ARTIFACTS_DIR/.ns_retrain_healed.txt"'`
- After running Step 3b / 3g fidelity-verifier (regardless of pass/fail):
  `printf "true" > "$ORCA_ARTIFACTS_DIR/.ns_retrain_fidelity.flag"`
  (**launch.sh does not clear this flag** — semantics = "fidelity has been run against the current scripts"; 3b first-launch writes it before launch,
  overwrite is stale-free, remains accurate across attempts / across runs)
- Soft judgment / pre-completion assessment (Steps 3a / 3b / 3d):
  `printf "%s" "<one-line assessment>" > "$ORCA_ARTIFACTS_DIR/.ns_retrain_assessment.txt"`

> Marker file paths are relative to `$ORCA_ARTIFACTS_DIR`; the agent must not forge them — downstream review cross-checks healed_files
> against the forbidden-touch list (anti-sloppiness enforced by audit).

🔴 **Iron rules (violation = failure)**:

1. **Read upstream contracts before generating** (only read, forbidden-touch list, iron rule 5): `AGENTS.md` (the retrain scaffold produced by ns3_search_pipeline) + `supernet_summary.md` + `project_manifest.md` + `selected_arch`
   (resource anchors). **Any of them missing → go straight to Step 4 and output `{"status":"failed"}`** (nothing to generate from without the scaffold,
   fail loud, iron rule 5), **do not** fabricate selected_arch or the scaffold.
2. **ckpt exists ≠ training complete**: completion determination (status.sh's `RETRAIN_COMPLETE`) = `.retrain_rc` content is `0`
   **and** the training process has exited **and** the ckpt exists **and** `torch.load` can read it (when the process is alive the rc may be a stale value from a previous
   attempt). ckpt present but not complete (interrupted) → **resume training to real completion**, do not skip.
3. **Self-heal on errors, don't let anything pass, no limit**. warmup failure (no epoch marker / metric divergence / training crash) → **must** use `read`
   on the log tail to locate the root cause, use `edit` **only per the whitelist below** to fix, and rerun. RETRAIN_INCOMPLETE/RETRAIN_STUCK triggers HEAL-LOOP:
   whitelisted edit + restart, **repeat indefinitely until RETRAIN_COMPLETE**. If the same root cause fails repeatedly, switch repair hypotheses; never give up.
   **The only termination**: root cause requires changing the forbidden-touch list → failed.
4. **Edit whitelist (soft prompt constraint, tape audit fields healed_files/fidelity_retriggered)**, two layers:
   - **Pure-patch layer** (direct edit, no fidelity re-trigger needed):
     - `run_retrain.sh` (launcher params / path alignment)
     - obvious typo / wrong import path (Python `ImportError` / `ModuleNotFoundError`, may edit any `.py`
       import line — **except the forbidden-touch list**, iron rule 5)
   - **Training-logic layer** (**edit allowed but must re-trigger `project-fidelity-verifier` per Step 3g**, self-report
     `fidelity_retriggered=true`):
     - loss / optimizer / sampling / KD / data pipeline in `retrain.py` / `finetune.py`
5. **Forbidden-touch list (hard iron rule, violation = architecture breakage, the only failed trigger)**: the following files are **read-only, edit/write prohibited** —
   `supernet.py`, `project_manifest.md`, `supernet_summary.md`, `AGENTS.md`,
   **source files** under `{{ inputs.project_root }}` (**exception**: `{{ inputs.project_root }}/artifacts/`
   is this workflow's artifact directory tree, writable), plus upstream-node-produced `select_architecture.py` /
   `search_config.yaml` / `run_train_supernet.sh` / `run_search_supernet.sh`. If self-heal
   requires changing these → **don't**, record last_error in `.ns_retrain_assessment.txt`, go to Step 4 and output `{"status":"failed"}`.
6. **No duplicate detach**: if `runs/retrain/.retrain_pid` exists and `kill -0` says alive → training is running, **prohibited** to issue another
   detach (it would start a second training process, resource contention + ckpts overwriting each other). Only health check + keep polling in C-loop.
7. **monitor_until_done.sh single block ≤ bash tool limit (~10min)**: no detach/kill inside a monitor block.
8. Your **final reply** may only be the **single-line JSON** printed by `emit_result.py` in Step 4 (only when training is complete/definitively
   failed) — the node's `output_schema` validates it, non-JSON → node_failed directly. **When not complete**, final reply =
   a status summary (containing "do not call orca next"), which the host will not submit.

9. **User metrics are authoritative (when generating retrain.py)**: loss / optimizer /
   scheduler / data flow / metric names / metric directions / metric transforms in retrain.py / finetune.py are taken **verbatim** from
   the **Training And Evaluation** section of `project_manifest.md` + the AGENTS.md scaffold (see Step 3a generation contract + self-check). NAS
   changes are limited to what supernet-ization requires (subnet extraction / budget compression); prohibited to replace the optimizer class, change loss formulas/constants, change
   metric names/directions/transforms, or introduce proxy measures like FLOPs. Drift → belongs to the training-logic layer, self-heal per 3f + re-trigger
   fidelity per 3g.

## Decision-tree overview (walk from the top every time you enter this node)

| Step | Action | Hit → go to |
|---|---|---|
| Step 1 | Run `status.sh` (complete + alive, two-in-one) | `RETRAIN_COMPLETE` → Step 3.5 push final chart → Step 4 executed; `RETRAIN_ALIVE` → Step 2; `RETRAIN_INCOMPLETE` → Step 3 |
| Step 2 | Run `health.sh` (process alive) | log healthy → **enter C-loop, keep polling**; hung → group kill + Step 3 |
| Step 3 | Generate / launch / resume (no live process) | 3a generate (when scripts missing) → 3b fidelity (first launch) → `launch.sh` (detach) → `warmup_poll.sh` loop → `eta.py` → `update_status_md.sh` → **enter C-loop** |
| Step 4 | Run `emit_result.py` (**the only moment that produces node JSON**) | single-line JSON as final reply, host calls next |

**Convergence guarantee**: training complete → Step 1 `RETRAIN_COMPLETE` → executed → downstream continues;
training interrupted (process dead + ckpt leftover) → Step 3 resume (rerun run_retrain.sh; resume from ckpt if the script supports it, otherwise from
scratch) → until truly complete; the only failed = root cause requires changing the forbidden-touch list (fail loud, never propagate errors downstream).

## Step 1 ── Status determination (run status.sh once)

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/status.sh"
```

Branch on stdout (mutually exclusive):
- `RETRAIN_COMPLETE ckpt=<path>` → go to Step 3.5 to push the final comparison chart, then Step 4 to output
  `{"status":"executed","artifacts":["<path>"],...}`
  (the ckpt path marker is written by status.sh, `emit_result.py` reads it, so the artifacts field cannot drift).
- `RETRAIN_ALIVE pid=<pid>` → go to Step 2 (health check; **no duplicate detach**, iron rule 6).
- `RETRAIN_INCOMPLETE` → go to Step 3 (no live process: never run / scripts missing → generate + fresh-launch;
  interrupted leftover → resume).

## Step 2 ── Health check (process alive; the normal re-entry path for a fresh sub-agent)

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/health.sh"
```

- log healthy (progress markers advancing + primary metric finite, no NaN/inf) → **enter C-loop, keep polling** (no JSON output).
- **Fake-death determination (fail loud, prevents silently waiting for nothing)**:
  - Progress markers present: this round's marker count in the log ≤ the epoch count recorded in the last `retrain_status.md` (`$ORCA_ARTIFACTS_DIR/retrain_status.md`),
    and wall-clock exceeds `ORCA_TRAIN_STALL_MIN` (default 15min) → training hung → treat as failure:
    `bash "$ORCA_AGENT_RESOURCES/scripts/kill_train_group.sh" "$PID"` (group kill with a **run-ownership gate** —
    launch.sh starts the process group with setsid, `kill -- -PID` kills the whole group: including the training python, preventing orphan processes
    from causing a duplicate detach next round. **Kills only this run's processes**; cross-run process killing is disabled):
    - Outputs `FOREIGN_RUN_ALIVE` (`$PID` is **another run's** training, status.sh mistakenly judged ALIVE — concurrent runs of the same
      project share the artifacts directory) → **don't kill, don't judge fake-dead** → **enter C-end** and end with a status summary
      (a fresh sub-agent re-judges via Step 1 next turn).
    - Otherwise (this run's process group killed) → update MD (`update_status_md.sh stuck`) + **enter HEAL-LOOP**
      (unlimited self-heal: read log → whitelist edit → launch.sh restart → warmup → back to C-loop).
  - **No progress markers (log format not contracted)**: fake-death determination doesn't apply (nothing to compare) → fall back to `LOG_MTIME`/`LOG_SIZE`:
    growing (compare two health.sh outputs) → judge healthy, **enter C-loop**;
    mtime/size not advancing and tail has no new content → hung, same path through `kill_train_group.sh` ownership gate + HEAL-LOOP
    (`FOREIGN_RUN_ALIVE` likewise not killed → C-end).

## Step 3 ── Generate / launch / resume (no live process; the only place that detaches)

> **Unlimited self-heal**: RETRAIN_INCOMPLETE/RETRAIN_STUCK triggers HEAL-LOOP (read log → whitelist edit → launch.sh
> restart → warmup → back to C-loop), **repeat indefinitely until RETRAIN_COMPLETE**. The only failed = root cause requires changing the forbidden-touch list.
> **Resume**: when a ckpt is leftover, rerun `run_retrain.sh` (resumes from ckpt if the script supports it, otherwise from scratch)
> ——the goal is to run to real completion.
> **Generate only once**: if both `run_retrain.sh` **and** `retrain.py` already exist (generated in a previous attempt / a previous wakeup) →
> skip 3a/3b and go straight to 3c (`finetune.py` is generated conditionally per the scaffold, not gated; the two-file gate prevents a single leftover file
> from an interrupted "write three files" sequence from wrongly skipping generation).

### 3a. Generate retrain scripts (only when `run_retrain.sh` doesn't exist; first-time / first launch across runs)

First, per iron rule 1, check the upstream contracts: if any of `AGENTS.md` / `supernet_summary.md` / `project_manifest.md` is missing →
go straight to Step 4 and output `{"status":"failed"}`, and state in the assessment which file is missing.

Per the AGENTS.md scaffold's instructions (retrain strategy: from-scratch / finetune-from-supernet / KD etc.),
use `write` to generate **into the `$ORCA_ARTIFACTS_DIR/` root** (launch.sh / eta.py / emit_result.py all resolve against the
artifacts root; wrong directory will waste attempt counters):

- `retrain.py`: main training entry (architecture = `{{ ns3_run_search.output.selected_arch }}`; data pipeline / loss /
  optimizer / metric names / metric directions / metric transforms taken **verbatim** from the Training
  And Evaluation section of `project_manifest.md` + the AGENTS.md scaffold, no substitution — see iron rule 9).
- `finetune.py` (if the scaffold specifies finetune-from-supernet): extract the selected subnet weights from the supernet ckpt
  as init + fine-tune.
- `run_retrain.sh`: launcher (**plain python3 by default, no torchrun**; for multi-GPU the user switches to
  `torchrun --nproc_per_node=N`, auto-detected by the script's `is_distributed()`),
  `cd $ORCA_ARTIFACTS_DIR` + call `python3 retrain.py --artifacts-dir "$ORCA_ARTIFACTS_DIR" ...`.

**Generation contract (precondition for scripts to parse, must be satisfied verbatim)**:
- **Single-device default (mandatory when generating retrain.py + run_retrain.sh)**:
  - The `run_retrain.sh` launcher uses **plain `python3`** (no torchrun / NPROC_PER_NODE / MASTER_PORT).
    `AMP=false` off by default. `NUM_WORKERS=0` by default. For multi-GPU the user changes to `torchrun --nproc_per_node=N`.
  - The DDP wrap in `retrain.py` becomes conditional: only wrap DistributedDataParallel `if is_distributed():`.
    Single device (plain python3, no RANK env) → `is_distributed()=False` → skip the wrap.
  - `sync_random_seed` uses the **guarded version**: `if not is_distributed(): return random.SystemRandom().randrange(...)`
    first. Unconditional `dist.broadcast` is prohibited (crashes on single device with `Default process group not initialized`).
  - `autocast(device, enabled=args.amp)`: AMP=false → nullcontext, orthogonal to single device.
- **Machine progress (dual feed, per progress unit, rank 0; user metrics are the only authority, `loss` is not assumed)**:
  - **(a) telemetry line (stdout, consumed by eta/health/warmup)**: `epoch <cur>/<total> <primary_metric> <v>`
    (epoch-based) or `step <cur>/<total> <primary_metric> <v>` (step-based). `<primary_metric>`
    = the **real name** of the user's primary training scalar (`loss`/`reward`/`gain`/…, taken verbatim from user code, hardcoding `loss` is prohibited). Downstream
    parses it structurally (`epoch|step` token + fraction + name token + trailing value), so the name can be any space-free token. Bare
    `epoch`/`step` words with ambiguous lines are banned (tqdm doesn't count).
  - **(b) progress JSONL (chart feed, consumed by the live chart watcher)**: per progress unit append one line to
    `$ORCA_ARTIFACTS_DIR/runs/retrain/progress.jsonl`:
    `{"step": <cur>, "metrics": {"<name>": <float>, ...}}`. `metrics` = **all** scalar metrics produced by that unit
    (every one present in the user's training + eval code, real names; `loss` is not special-cased — if the user has no loss, don't write loss). Append mode +
    `json.dumps(row)+"\n"` + `flush()` + `is_main_process()` guard. The launcher clears this file per attempt.
- **Total progress**: exposed via `--epochs N` (or `--max_steps N`); using `--epochs "$EPOCHS"` in run_retrain.sh with
  `EPOCHS=N` variable form is also fine (eta.py parses both).
- **Final ckpt always written to `$ORCA_ARTIFACTS_DIR/runs/retrain/retrain_best.pth`** (the contracted path for status.sh /
  emit_result.py; must not drift).
- **DataLoader hygiene (hard rule on CUDA training machines, real incidents)**: every
  `DataLoader` in retrain.py / finetune.py uses `num_workers=0` + `pin_memory=False` — `num_workers>0` fork workers
  crash after the parent process has already initialized CUDA (`CUDA initialization error`); `pin_memory=True` reports
  `cannot be pinned` for CUDA tensors. Do not change this default.
- **Do not** hardcode supernet.py's internal implementation in retrain.py / finetune.py — call it only through the manifest-exposed
  APIs (`build_supernet` / `extract_subnet` etc.). If the manifest doesn't expose the needed API → fail loud (iron rule 1),
  don't work around supernet.py.
- **User-metric self-check (mandatory after generation)**: grep `retrain.py` / `finetune.py` for the optimizer construction + loss call
  tokens — the optimizer class name + loss function name must match what the Training And Evaluation section of `project_manifest.md`
  records; undeclared substitution is prohibited. Drift → belongs to the training-logic layer, self-heal per 3f (after edit append
  `.ns_retrain_healed.txt`) + re-trigger fidelity per 3g. Fidelity of metric names / directions / transforms is a semantic-layer matter, covered by the 3g
  `project-fidelity-verifier`'s Evaluation-measure fidelity dimension.

After generation, append the file names to `.ns_retrain_generated.txt`.

### Step 3b ── fidelity-verifier review (mandatory after first generation, point-to-file protocol)

Run a fidelity review on the **first-generation** retrain.py / finetune.py (first trigger also writes
`.ns_retrain_fidelity.flag=true`):

1. Invoke the host's built-in generic subagent (point-to-file protocol, subagent_type set to a host built-in generic type such as
   `general`; append this round's inputs to the end of the first-round prompt per the multi-round continuation rule):
   ```
   Task(subagent_type=<host built-in generic type>,
        prompt="First fully Read {{ subagents_root }}/project-fidelity-verifier.md, and execute this round's task strictly per its Procedure.
                This round's inputs: <task: verify whether my generated retrain.py / finetune.py faithfully reflect original project training semantics (loss / optimizer / sampling / KD / data pipeline), given AGENTS.md scaffold + supernet_summary.md + project_manifest.md> + <my generated scripts full content> + Context: ns3_retrain Step 3b first-time review。
                Return in the format specified by the md.
                **first line of report** must echo verbatim the sentinel field in the md frontmatter you Read (format at the top of the md; don't guess, it must come from the file you Read).")
   ```
   `Read` fails (file doesn't exist) → **don't** pretend you ran it; append
   `" | fidelity-verifier subagent body not deployed; cannot review"` to `.ns_retrain_assessment.txt`, skip this step (doesn't block execution,
   but leaves a trace in the tape).
2. Write the verifier's conclusion (pass / fail + reason) into `.ns_retrain_assessment.txt`;
   `printf "true" > .ns_retrain_fidelity.flag` (**regardless of pass/fail** — marked true once run, on fail regenerate the scripts per the
   verifier's suggestions, then run this step again).

If the verifier fails and the suggested change belongs to the iron-rule-5 forbidden-touch list → don't touch forbidden files, record last_error, go to Step 4 fail loud.

**Solidified-script gate (after 3b, before 3c)**:
```bash
bash "$ORCA_AGENT_RESOURCES/scripts/check_retrain.sh"
  || { echo "FAIL" >&2; exit 1; }
```
Validates py_compile + launcher hygiene (no torchrun + AMP=false + NUM_WORKERS=0) + conditional DDP. On failure → fix.

### 3c. Launch (clear markers + detach, one short call)

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/launch.sh"
```

- stdout `FOREIGN_RUN_ALIVE pid=...` → **another run** is training in this shared artifacts directory
  (concurrent runs of the same project; cross-run process killing is disabled, launch.sh already aborted before the attempt counter) →
  **enter C-end** and end with a status summary (a fresh sub-agent re-judges via Step 1 next turn).
- stdout `DETACHED pid=... attempt=N` → go to 3d warmup.

### 3d. warmup polling (**re-send** 3d until stdout shows `WARMUP_OK` or `WARMUP_FAIL`)

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/warmup_poll.sh"
```

Branch:
- `WARMUP_OK epoch_cnt≥2` → go to 3e (ETA + MD), then **enter C-loop**.
- `WARMUP_FAIL reason=process-exit rc=0` → **training already completed normally within the warmup window** (not a failure):
  rerun `status.sh` — if `RETRAIN_COMPLETE` go to Step 3.5 to push the final chart then Step 4 and output executed;
  otherwise (invalid ckpt etc.) enter HEAL-LOOP.
- `WARMUP_RUNNING` → **send 3d again** (each call is an independent short call; while-loop inside the same call is prohibited).
  **Cap 5 times** (~20 min); still no progress marker (epoch/step) past the cap → branch on whether the log is growing:
  - **log growing** (`LOG_MTIME`/`LOG_SIZE` changing between two calls, or tail continuously has content) →
    **uncontracted-log-format fallback**: `read` the log and judge health manually (loss decreasing / training progress output → healthy;
    no output at all → suspicious) → record in assessment `"log format not contracted; health judged manually"`
    → skip ETA (when eta.py can't parse a progress marker current=0 / eta unknown, that's normal, don't treat as failure)
    → proceed as normal to 3e (ETA + MD, eta unknown acceptable) → **enter C-loop**. **Don't** enter HEAL-LOOP
    (the format issue is a retrain.py generation-contract problem, not a bug in this launch — if training runs this round it passes; leave format issues to
    retrain.py contract troubleshooting).
  - **log empty / mtime not advancing** → truly hung → the agent judges `WARMUP_FAIL` (timeout without progress, this signal is
    self-assessed by the agent — warmup_poll.sh only outputs process-exit / metric-diverged) → HEAL-LOOP.
- `WARMUP_FAIL` → **HEAL-LOOP** (see the HEAL-LOOP section under C-loop below).

> warmup design intent: the first 1~2 progress markers (epoch/step, see the 3a generation contract) appearing = proof that training **can run**
> (data pipeline, model forward/backward, ckpt directory writable all passed). Training after that is handed over to
> C-loop bounded polling + HEAL-LOOP self-heal relay; this node doesn't idle-wait.

### 3e. ETA (informational) + update MD (cross-wakeup ground-truth source)

```bash
python3 "$ORCA_AGENT_RESOURCES/scripts/eta.py"
bash "$ORCA_AGENT_RESOURCES/scripts/update_status_md.sh"
```

`eta.py` writes `runs/retrain/.retrain_eta.json` and prints a single-line JSON (total/current/per_epoch/eta_minutes);
`update_status_md.sh` **recomputes** the current epoch from the log (doesn't read stale ETA values) and writes `retrain_status.md`.

### 3f-HEAL. HEAL-LOOP (triggered by warmup failure / RETRAIN_INCOMPLETE / RETRAIN_STUCK; unlimited self-heal loop)

Triggered by `WARMUP_FAIL` / `*INCOMPLETE*` / `*STUCK*` (≤2 rounds per turn; still failing after 2 rounds → C-end and swap sub-agent):
1. `bash "$ORCA_AGENT_RESOURCES/scripts/kill_train_group.sh" "$PID"` (group kill with run-ownership gate —
   kills only this run; `FOREIGN_RUN_ALIVE` output (rare, $PID belongs to another run) → don't kill, **go straight to C-end**
   and end with a status summary (a fresh sub-agent re-judges via Step 1 next turn; cross-run process killing is disabled)).
2. `read` the latest attempt log (`ls -t runs/retrain/retrain.attempt*.log | head -1`) tail ~80 lines to locate the root cause.
3. Decide which layer the root-cause fix belongs to:
   - **Pure-patch layer** (launcher / path / import / typo) → edit, append healed marker, no fidelity needed.
   - **Training-logic layer** (loss / optimizer / sampling / KD / data pipeline in `retrain.py` / `finetune.py`)
     → edit, append healed, and must re-trigger project-fidelity-verifier (Step 3g) + write the fidelity flag.
   - **Root cause requires changing the forbidden-touch list** (`supernet.py` / `project_manifest.md` / `supernet_summary.md` /
     `AGENTS.md` / source files / `select_architecture.py` / `search_config.yaml` /
     `run_train_supernet.sh` / `run_search_supernet.sh`)
     → **the only failed path**: forbidden, record last_error in `.ns_retrain_assessment.txt`,
     give up self-heal (no more launch), go to Step 4 and output `{"status":"failed"}`.
   - OOM class: shrinking batch=1 + ckpting + AMP still not relieved → likely supernet capacity (forbidden-touch) → failed hint.
   - A whitelisted file is wholly corrupted and must be rebuilt → **`write` to regenerate self-produced files is allowed** (retrain.py / finetune.py /
     run_retrain.sh, not on the forbidden-touch list), validate per the 3a generation contract after rebuild + append to `.ns_retrain_generated.txt`;
     if the change belongs to the training-logic layer → follow the same "training-logic layer" rule into 3g to re-trigger fidelity.
4. `launch.sh` restart (resume preferred: from ckpt if the script supports it, otherwise from scratch; `.retrain_attempt`++ only counts for log naming, no limit).
5. `warmup_poll.sh` confirms it runs → back to C-loop and keep polling.

> **Unlimited**: if the same root cause fails repeatedly, switch hypotheses (read more log / change repair strategy), but never give up and no round-count threshold.

### Step 3g ── Re-trigger project-fidelity-verifier (point-to-file protocol, on demand)

Run this step **proactively** when HEAL-LOOP touches the **training-logic** category (audit field
`fidelity_retriggered` self-reported; the fresh subagent re-reads the md body to double-check):

1. Invoke the host's built-in generic subagent (point-to-file protocol, subagent_type set to a host built-in generic type such as
   `general`; append this round's inputs to the end of the first-round prompt per the multi-round continuation rule):
   ```
   Task(subagent_type=<host built-in generic type>,
        prompt="First fully Read {{ subagents_root }}/project-fidelity-verifier.md, and execute this round's task strictly per its Procedure.
                This round's inputs: <task: re-verify whether my edits to retrain.py / finetune.py drift from original project training semantics> + <my latest healed diff context> + Fixed:[<healed file list this round>] + Context: ns3_retrain self-heal。
                Return in the format specified by the md.
                **first line of report** must echo verbatim the sentinel field in the md frontmatter you Read (format at the top of the md; don't guess, it must come from the file you Read).")
   ```
   `Read` fails (file doesn't exist) → **don't** pretend you ran it; append
   `" | fidelity-verifier subagent body not deployed; cannot retrigger"` to the end of `.ns_retrain_assessment.txt`, skip this step.
2. Merge the verifier's conclusion (pass / fail + reason) into `.ns_retrain_assessment.txt`;
   `printf "true" > .ns_retrain_fidelity.flag` (**regardless of verifier pass/fail** — once re-triggered mark true,
   on fail state it honestly in the assessment).

### C-loop ── full-course polling + unlimited self-heal, until complete / turn limit

After warmup passes (or Step 2 re-enters `*ALIVE*`), send monitor_until_done.sh continuously.
Branch with trailing wildcards (monitor outputs RETRAIN_*, matched uniformly):

Repeat (cap K=6 monitor blocks per turn, ~54min/turn; K only controls turn-switch frequency, doesn't limit self-heal rounds):
```bash
bash "$ORCA_AGENT_RESOURCES/scripts/monitor_until_done.sh"
```
Branch on stdout:
- `*COMPLETE* ckpt=<path>` → first run Step 3.5 final charts (metrics_bar+compare_table) → then Step 4 executed ⚠ don't skip Step 3.5.
- `GATE_SKIP` → go to Step 4 and output skipped.
- `*INCOMPLETE*` → enter HEAL-LOOP (process dead).
- `*STUCK* <reason>` → double-check: read the log to confirm real divergence (not a slow epoch) → HEAL-LOOP; if judged a normal slow epoch → treat as STILL_RUNNING and continue.
- `STILL_RUNNING` → blocks run < K → send again; blocks run = K → update MD + enter C-end.
- (default / empty stdout / exception) → treat as STILL_RUNNING (send again; two consecutive empties → C-end status summary, prevents silent spinning).

> **Hung-determination baselines**:
> - Inside C-loop (same sub-agent turn): relies on monitor's `*STUCK* log-stalled` — log not growing for
>   `ORCA_TRAIN_STALL_POLLS` (default 3 = 3min) consecutive → suspect.
> - Fresh sub-agent re-entering Step 2 (cross-turn): run health.sh, compare against the epoch recorded in retrain_status.md —
>   if epoch hasn't advanced and wall-clock exceeds `ORCA_TRAIN_STALL_MIN` (default 15min) → hung → kill + HEAL-LOOP.

### C-end ── turn-limit wrap-up (K blocks exhausted / HEAL-LOOP reached 2 rounds / FOREIGN_RUN_ALIVE / two consecutive empty stdouts)

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/update_status_md.sh"
```
Final reply = a status summary (containing "do not call orca next" + current epoch + eta + log path + self-heal count + healed list).

```
Retraining not complete (pid=<PID>, epoch 3/10, eta ~8h, log: runs/retrain/retrain.attempt1.log,
self-healed N times, healed: [retrain.py]). Monitor polling / turn limit reached, swapping sub-agent to continue.
Do not call orca next — the node stays executing.
```

> When the host sees "do not call orca next" it knows the node is incomplete and won't submit.
> **Resumable**: you may be a fresh sub-agent re-dispatched by the host after the turn limit. Every time you enter this node, first walk Step 1
> status.sh to recompute the current state from the filesystem. RETRAIN_ALIVE → go straight into C-loop and keep polling (**no duplicate detach**, iron rule 6).
> The training process was detached by launch.sh via setsid; a sub-agent's liveness doesn't affect it. HEAL-LOOP's self-heal history is rebuilt from
> `retrain.attempt*.log` + `.ns_retrain_healed.txt` + `retrain_status.md` — read them to judge
> "what was already fixed, whether the current root cause is new", to avoid repeating the same failed fix (switch hypotheses, but don't stop).

### 3.5 ── Push final comparison charts (when training is truly complete; `|| true` doesn't block)

After `RETRAIN_COMPLETE`, before Step 4, run 2 chart scripts to push cross-phase metric comparison + openness before/after comparison
to the frontend. The scripts are fail-soft: missing artifact → skip + stderr, no crash; stdout/stderr fully discarded —
the final reply must contain only `emit_result.py`'s output. (source the env per the host's prompt instructions first; chart push
depends on ORCA_CHART_SOCK. The Jinja-rendered values = the selected architecture coordinates from upstream ns3_run_search, copy the numeric strings verbatim.)

```bash
cd "$ORCA_ARTIFACTS_DIR" || exit 1
python3 "$ORCA_AGENT_RESOURCES/scripts/metrics_bar.py" --artifacts-dir "$ORCA_ARTIFACTS_DIR" --selected-acc "{{ ns3_run_search.output.selected_acc }}" > /dev/null || true
python3 "$ORCA_AGENT_RESOURCES/scripts/compare_table.py" --artifacts-dir "$ORCA_ARTIFACTS_DIR" --selected-latency "{{ ns3_run_search.output.selected_latency }}" --selected-acc "{{ ns3_run_search.output.selected_acc }}" --latency-unit "{{ ns3_run_search.output.latency_unit }}" > /dev/null || true
# subnet_profile.py: materializes the selected subnet, writes subnet_structure.md (read by ns3_report) + pushes a table chart.
# fail-soft: if materialize fails → no md + stderr + exit 0; the reporter treats a missing md as an empty string.
python3 "$ORCA_AGENT_RESOURCES/scripts/subnet_profile.py" --artifacts-dir "$ORCA_ARTIFACTS_DIR" --latency-unit "{{ ns3_run_search.output.latency_unit }}" > /dev/null || true
```

## Step 4 ── Self-validating JSON (**the only moment that produces the node JSON**)

Only three situations enter this step: Step 1 hit `RETRAIN_COMPLETE` / upstream contract missing (iron rule 1) / forbidden-touch-blocked failed.
After running this block, use that one line of JSON from its stdout verbatim as your final reply
(the host calls `orca next --output` to submit):

```bash
python3 "$ORCA_AGENT_RESOURCES/scripts/emit_result.py"
```

Status derivation (inside emit_result.py): `failed` (AGENTS.md missing — upstream error) / `executed` (rc=0 +
process exited + ckpt valid) / `failed` (no valid ckpt + scripts present — when forbidden-touch-blocked the agent gives up self-heal and doesn't launch,
emit_result's existing else branch naturally yields failed). The deterministic parts are judged from the real filesystem;
the behavioral-trail parts (healed_files / fidelity_retriggered / assessment) are read from the markers.

## Supervision points (fail loud)

- **Never hand-fabricate fake JSON**: if `status==failed` fail honestly — the node's output_schema validates + downstream fallback exists, faking is pointless,
  traceable via tape audit + marker files.
- **Never propagate errors downstream**: forbidden-touch-blocked → `status=failed`. The yaml routing contract: failed goes to catch-all
  `ns3_report` (explicit routing; the engine doesn't auto-judge failure on an AgentNode's output.status) — **don't**
  downgrade to `executed` and let downstream run with a broken ckpt.
- **Not complete ≠ done**: when training is incomplete output a status summary (not JSON), **don't** write "training in progress" as executed
  and submit (the run should stay active until training is truly complete).
- **ckpt present ≠ complete**: an interrupted leftover ckpt must be resumed, don't output executed just because "ckpt exists"; completion requires all
  three conditions together (rc=0 + process exited + ckpt valid) — status.sh / emit_result.py already implement this, don't hand-modify the logic.
- **No duplicate detach** (iron rule 6): `status.sh` outputs `RETRAIN_ALIVE` → go to Step 2, **don't** go to 3c.
- **Forbidden-touch list is a hard iron rule (the only failed trigger)**: even if HEAL-LOOP keeps failing, don't edit `supernet.py` /
  `project_manifest.md` / `supernet_summary.md` / `AGENTS.md` / **source files** under `{{ inputs.project_root }}`
  (exception: `{{ inputs.project_root }}/artifacts/` is this workflow's artifact directory tree, writable) / upstream-node-produced
  `select_architecture.py` / `search_config.yaml` / `run_train_supernet.sh` / `run_search_supernet.sh`.
  Root cause requires touching forbidden files → give up self-heal, go to Step 4 failed.
- **Fidelity review doesn't block but must run**: Step 3b is mandatory (after first generation), Step 3g is on demand (when HEAL-LOOP changes training
  logic). When the verifier body isn't deployed, state it honestly, **don't** pretend you ran it.
- **Marker files are not to be forged**: healed_files must = the files actually edited this round; fidelity_retriggered must =
  actually ran Step 3b or 3g this round. Downstream review cross-checks whether marker vs healed_files touches the forbidden-touch list.
- **scripts/ is run-only**: the scripts under `$ORCA_AGENT_RESOURCES/scripts/` are this node's deterministic logic, **edit prohibited**;
  if a script errors / behaves differently than expected → record it honestly in the assessment and fail loud, don't modify the script to bypass.
- retrain stdout doesn't enter the final reply — only Step 4 `emit_result.py`'s output (when complete) is your reply.

## Output

**When training is complete / definitively failed, the whole reply = the one line of JSON printed by Step 4 `emit_result.py`** (e.g.
`{"status":"executed","artifacts":["/path/retrain_best.pth"],"assessment":"final test acc 0.93, latency 4.2ms vs full 8.1ms","max_retries_hit":false,"healed_files":["retrain.py"],"fidelity_retriggered":true}`).
The node's `output_schema` requires it to be valid JSON with `status ∈ {executed, failed}`;
`status==failed` → explicitly routed to `ns3_report`. **When training is incomplete, the whole reply = a status summary
(containing "do not call orca next"); the host won't submit, the node stays executing, waiting for monitor polling / turn limit to swap sub-agent and continue.**

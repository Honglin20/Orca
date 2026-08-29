---
description: Run the supernet search, push result charts, select the best architecture, and emit the result JSON.
tools: [bash, read, edit, grep, glob, task]
---
# ns3_run_search

## ⚠ Your Only Task (read this first, most important)

Upstream `ns3_search_pipeline` has generated the search script `run_search_supernet.sh`
(+ `search_config.yaml` + evaluator / arch_codec, etc.) in `$ORCA_ARTIFACTS_DIR`. **Your job:
run it until it truly succeeds — self-heal on errors per the whitelist, keep fixing until
`search_results.jsonl` holds ≥1 real record, then echo the real JSON.** You are not
describing/summarizing upstream; you only look at the scripts in the artifacts directory,
**run them, fix them per the whitelist, and run them again**.

🔴 **Iron Rules (violating any = failure)**:

1. **Self-heal on errors, never let go, no limit**. If `run_search_supernet.sh`'s `wait`
   exit code ≠ 0, or `$ORCA_ARTIFACTS_DIR/search_results.jsonl` is missing / has 0 lines →
   **must** use `read` to read the log tail to locate the root cause, use `edit` to fix **only
   per the whitelist below**, and rerun. **Repeat indefinitely until `search_results.jsonl` has
   ≥1 line (rc=0)**. N is only used for naming attempt logs (`search.attemptN.log`), not as a
   blocker. If the same root cause fails repeatedly, try different fix hypotheses and never give up.
2. **Edit whitelist (prompt soft constraint; tape audit fields healed_files/fidelity_retriggered)**, two layers:
   - **Pure-patch layer** (edit directly, no need to retrigger fidelity):
     - `run_search_supernet.sh` (launcher arguments / path alignment)
     - `search_config.yaml` path / argument alignment (including `supernet_ckpt_path` /
       `search_results` output path aligned to `$ORCA_ARTIFACTS_DIR/search_results.jsonl`,
       depended on by downstream ns3_retrain)
     - Obvious typo / import path errors (Python `ImportError` / `ModuleNotFoundError`; you may
       edit the import lines of any `.py`)
   - **Search/evaluation logic layer** (**edit allowed but must retrigger `project-fidelity-verifier` per Step 2.5**, self-report
     `fidelity_retriggered=true`):
     - `evaluator.py` / `arch_codec.py` / `search_supernet.py`'s sampling / subnet extraction /
       metric computation / data pipeline
3. **Forbidden-touch list (hard rule; violating = architecture breakage)**: the following files are
   **read-only, no edit/write** — `supernet.py`, `project_manifest.md`, `supernet_summary.md`,
   source files under `{{ inputs.project_root }}` (**exception**: `{{ inputs.project_root }}/artifacts/`
   is this workflow's artifact tree and may be written). If self-healing requires changing a
   forbidden-touch file → **don't change it**; record last_error to
   `.ns_run_search_assessment.txt` and go to Step 3 to emit `{"status":"failed"}`.
4. **Missing upstream ckpt is not your fault, but fail loud**: if ns3_run_train output `status=skipped` or
   a missing ckpt prevents the search from running, **do not** fake a search success — fail honestly
   so the user can see training did not run.
5. **Soft judgment (report, not a gate)**: after success, read `search_results.jsonl` (candidate subnets / latency / metric /
   Pareto markers), self-judge and write `assessment` (e.g. "640 candidates, Pareto front size 12,
   max-acc 0.91 @ latency 4.2ms"). This is a soft judgment, **not** the success gate — the gate is RC=0 + jsonl ≥1 line.
6. Your **final reply** must be exactly the single-line JSON printed by the Step 3 python (**the whole
   reply must be valid JSON, no text before or after**) — validated against the node `output_schema`;
   non-JSON directly causes node_failed.

## Resource Anchors (cwd-independent)

- `$ORCA_ARTIFACTS_DIR` (injected by orca spawn) = this run's artifacts directory, where upstream
  ns3_search_pipeline drops its scripts; shared across nodes; the authoritative `search_results.jsonl`
  artifact lands here.
- `{{ subagents_root }}/project-fidelity-verifier.md` = fidelity-verifier subagent body
  (point-to-file protocol, Step 2.5; inlined as an absolute path at render time, cwd-independent).

## Behavior-Trace Marker Files (maintained during self-heal; convention)

The agent writes its self-heal behavior traces to three marker files (deterministic parts + behavior
traces are separated — the Step 3 python reads the markers to build the JSON; the agent does not need
to modify the python script):

- After each `edit` to a whitelisted file:
  `bash -c 'printf "%s\n" "<edited_file_relpath>" >> "$ORCA_ARTIFACTS_DIR/.ns_run_search_healed.txt"'`
- After running Step 2.5 fidelity-verifier (regardless of pass/fail):
  `printf "true" > "$ORCA_ARTIFACTS_DIR/.ns_run_search_fidelity.flag"`
- After the soft judgment (Step 2.6):
  `printf "%s" "<one-line assessment>" > "$ORCA_ARTIFACTS_DIR/.ns_run_search_assessment.txt"`

> Marker file paths are relative to `$ORCA_ARTIFACTS_DIR`; the agent must not forge them — downstream
> review cross-checks healed_files against the forbidden-touch list (audit prevents sneaking through).

## Step R ── Resume Guard (cross-turn continuation detection; run before Step 0)

> You may be a fresh sub-agent re-dispatched by the host after the turn budget was exhausted. The
> search process is detached via `setsid` into its own process group; whether the sub-agent lives or
> dies does not affect it.

🔴 **Each branch computes N independently; never a one-size-fits-all max+1**:
when the search is running, N = the attempt number currently running (Step 2b uses it immediately to
`tail` the log); when the search is dead/not started, N = max(existing number)+1 (Step 2a re-detach
must not overwrite existing logs). When dead attempt logs linger, the max number ≠ the currently
running number — a blanket max+1 would `tail` a non-existent log → falsely judge dead-hang → issue
`kill -- -<pgid>` and kill a search that is actually running.

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/resume_guard.sh"
```

- stdout `RESUME_SEARCH attempt=...` → **skip Step 0 / Step 1**, go straight to Step 2b short polling
  (the search process is running; re-detach is forbidden; **Step 2b uses this N immediately** to
  `tail -8 search.attempt${N}.stdout.log` + read `.search_pid`/`.search_rc`/`search_results.jsonl`
  + healed markers to rebuild state).
- stdout `RESUME_HEAL new_attempt=...` → normal Step 0 (reuse-check) → Step 1, and **Step 2a uses this N
  to re-detach** (writing a new `search.attempt${N}.stdout.log`, not overwriting existing attempt1..attempt${N-1}).
- **N only names the log** — it is decoupled from `.ns_run_search_healed.txt` /
  `.ns_run_search_fidelity.flag` (the healed marker is a self-heal behavior trace; N is an attempt
  counter; they are independent). Step R does not clear the healed marker (preserve on resume);
  Step 0/1 will clear it when reached.

## Step 0 ── Reuse-Check (soft skip)

> project-scoped artifacts are reused across runs: this node's authoritative artifact =
> `$ORCA_ARTIFACTS_DIR/search_results.jsonl` (≥1 line + valid JSON). This step **first checks whether the
> artifact exists; if it does, verify it meets the bar and skip the redo** — avoid burning compute on a duplicate search.

**Deterministic check + verification (no blind skip)**: run before the Step 1 pre-checks:

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/reuse_check.sh" "{{ inputs.latency_unit }}"
```

- Meets the bar (`search_results.jsonl` ≥1 valid-JSON line) → skip the Step 1 / Step 2 search redo, but
  **still run Step 2.7 to push 3 charts + Step 2.8 select** (reuse must also produce the
  `.selected_arch.json` marker for ns3_report to read the final state), then Step 3 emits
  `{"status":"executed","artifacts":["$RESULTS"],...}` (the Step 3 python reads it off disk, naturally
  producing executed). `assessment` prefixed with `reused existing search_results.jsonl: <path>`
  (reuse observability, mechanically checkable: the artifact's mtime predates this run's start).
- Absent / does not meet the bar → run Step 1 pre-checks + Step 2 self-heal as normal.
- **The status enum is unchanged**: reuse goes through `executed` (same status as the success path; the
  ns3_run_search route guard reads executed without misjudging).

## Step 1 ── Pre-checks (deterministic, run once)

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/precheck.sh"
```

If the block above prints `cannot proceed` → go straight to Step 3 (the python will judge `status=failed`).

## Step 2 ── Search (detach + poll across many short calls; unlimited self-heal)

The search is a genuinely long task.

🔴 **Long-task execution iron rule**: a single bash tool call has a timeout cap (about 10 min).
**Forbidden** to put detach + polling loop into one bash call — a long search would time out the whole
call and kill it, terminating the search. The correct posture is **multiple short tool calls**: first one
call detaches (returns in seconds), then **repeat** short polling calls until the process finishes.

For **each attempt** N=1,2,3,... (no limit — N only names the attempt log, never blocks):

### 2a. detach (one short call, returns in seconds; **no wait/sleep in this call**)

```bash
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }
mkdir -p runs/search
rm -f runs/search/.search_pid runs/search/.search_rc
# setsid: the search runs in its own session/process group (PGID == session-leader PID, recorded by
# the leader itself into .search_pid). On a dead-hang HEAL, `kill -- -<pgid>` kills the whole group
# (wrapper + script + python + GPU workers), fixing the orphan where the old `nohup ... &` + `kill $!`
# only killed the wrapper and left the reparented python search holding the GPU. Group isolation
# verified: does not cross runs/projects and does not touch the chart daemon (fresh unique PGID).
setsid bash -c 'echo "$$" > runs/search/.search_pid; bash run_search_supernet.sh > "runs/search/search.attempt'"$N"'.stdout.log" 2>&1; echo $? > runs/search/.search_rc' </dev/null >/dev/null 2>&1 &
# race-free wait for the leader to record its PGID (if setsid had to fork, $! may be a transient
# parent; trust only .search_pid).
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -f runs/search/.search_pid ] && break; sleep 0.2; done
echo "DETACHED pgid=$(cat runs/search/.search_pid 2>/dev/null || echo '?') attempt=$N"
```

### 2b. Short polling (**repeat** this call until stdout shows `DONE`; ≤5 min each, never hits the tool timeout)

```bash
cd "$ORCA_ARTIFACTS_DIR" || exit 1
PID="$(cat runs/search/.search_pid 2>/dev/null)"
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
  echo "DONE rc=$(cat runs/search/.search_rc 2>/dev/null || echo unknown)"
  tail -30 "runs/search/search.attempt${N}.stdout.log" 2>/dev/null
else
  sleep 240   # 4 min; do not increase (would hit the bash tool timeout)
  echo "RUNNING"
  tail -8 "runs/search/search.attempt${N}.stdout.log" 2>/dev/null
fi
```

- stdout `RUNNING` → **send 2b again** (no while loop inside one call; each 2b is an independent short call).
- stdout `DONE rc=...` → proceed to 2c.
- **No polling cap**: the search may run for a long time (hours to days). Repeat 2b until `DONE` —
  **no attempt cap**. Warmup only detects early dead-hangs; once past warmup (the first few polls already
  showed generation/candidate-eval markers + bounded objective), trust the process is running and keep
  polling to the end.
- **Warmup health check**: the first 2–3 `RUNNING` `tail` outputs should show generation / candidate-eval
  markers (objective not NaN/inf). No markers / diverging objective → search is dead-hung or silently
  crashed → `kill` + self-heal, **don't idle-wait**.
- **Mid-search divergence detection**: after warmup, if the log shows no new generation markers for a long
  time / the tail shows NaN/inf/objective divergence → judge dead-hang → `kill` + self-heal (same
  TRAIN_STUCK idea as train, but search uses the agent's in-poll judgment, no monitor script).
- **Kill the whole group on dead-hang** (🔴 never just `kill "$PID"` → the python search gets orphaned
  and keeps holding the GPU): run
  `kill -- -"$(cat runs/search/.search_pid 2>/dev/null)" 2>/dev/null || true` (negative PGID = kill the
  whole process group: wrapper + script + python + GPU workers die together), then return to 2c self-heal.
  Group isolation verified: does not cross runs/projects and does not touch the chart daemon. The
  `kill -0 "$PID"` liveness check is unchanged (leader PID == PGID; leader alive ⇔ search running).

> Cross-shell RC: the detach sub-shell writes `echo $? > .search_rc` at the end; polling calls are different
> bash sub-shells (`wait` is ineffective across shells) → read RC from `.search_rc`.

> **Turn budget + resumability** (unlimited retries may cross turns): within one turn, detach + poll via
> many short calls + self-heal loops; if the turn's tool-call budget is nearly exhausted (e.g. ≥2 self-heal
> cycles already) → **end the turn with a status note** (non-JSON, including "please do not call orca next"
> + current attempt + search pid/rc + log path); a fresh sub-agent resumes next turn via Step R (reads
> `.search_pid`/`.search_rc`/`search_results.jsonl` + `search.attempt*.log` + healed markers to recompute
> the situation — search running → keep polling; dead/failed → HEAL resumes, already-edited files are
> rebuilt from markers to avoid repeating the same fix).

### 2c. Success judgment

`DONE rc=0` **and** `$ORCA_ARTIFACTS_DIR/search_results.jsonl` has ≥1 line → success → go to Step 2.6
(soft judgment) → Step 3.
- If the search script wrote results elsewhere (e.g. `runs/search/search.jsonl`) and `search_results.jsonl`
  is missing, that is a `search_config.yaml` output-path mismatch → during self-heal, align the
  `search_config.yaml` output path to `search_results.jsonl` (absolute path or relative to
  `$ORCA_ARTIFACTS_DIR`) and rerun.

`DONE rc≠0` / `search_results.jsonl` missing or 0 lines → **self-heal**:
- `read` the tail of `runs/search/search.attempt${N}.stdout.log` + `runs/search/search.log` (if any).
- Common root-cause judgment:
  - Missing supernet ckpt → look back at the ns3_run_train output. If ns3_run_train `status=skipped` /
    `failed`, the ckpt is doomed to be missing — record last_error, **do not** fake by changing the ckpt
    path; go to Step 3 and emit `{"status":"failed"}` (missing upstream cannot be fixed at this node).
  - Framework reports something "device / concurrency"-related → check `CUDA_VISIBLE_DEVICES`; if needed,
    add an export at the top of `run_search_supernet.sh` (pure-patch layer).
- Judge which layer the root cause belongs to (iron rule 2 whitelist, two layers):
  - **Pure-patch layer** (launcher / path / import error / typo / `search_config.yaml` output-path
    alignment) → use `edit` to change the file, append the changed file's relative path to
    `.ns_run_search_healed.txt` (Step 0 marker protocol). No need to retrigger fidelity.
  - **Search/evaluation logic layer** (`evaluator.py` / `arch_codec.py` / `search_supernet.py`'s sampling /
    subnet extraction / metric computation / data pipeline) → use `edit` to change, append to
    `.ns_run_search_healed.txt`, **and must** run Step 2.5 to retrigger fidelity-verifier and write
    `.ns_run_search_fidelity.flag`.
  - Otherwise (root cause touches the **forbidden-touch list**, iron rule 3) → **edit forbidden**; record
    last_error to `.ns_run_search_assessment.txt` and go to Step 3 to emit `{"status":"failed"}`.
- `N++` and back to 2a (**no limit** — if the same root cause fails repeatedly, try different fix
  hypotheses and never give up).

### Step 2.5 ── Retrigger project-fidelity-verifier (point-to-file protocol, on demand)

Run this step **proactively** when Step 2's self-heal touches the **search/evaluation logic** category
(audit field `fidelity_retriggered` self-reports; fresh subagents re-read the md body to cross-check):

1. Invoke the host's built-in generic subagent (point-to-file protocol; set subagent_type to the host's
   built-in generic type, e.g. `general`; append this round's inputs at the end of the first prompt per
   the multi-round continuation rules):
   ```
   Task(subagent_type=<host built-in generic type>,
        prompt="First fully Read {{ subagents_root }}/project-fidelity-verifier.md and strictly execute this round's task per its Procedure.
                This round's inputs: <task: re-verify whether my edits to evaluator.py / arch_codec.py / search_supernet.py drift from original project search semantics> + <my latest healed diff context> + Fixed:[<healed file list this round>] + Context: ns3_run_search self-heal.
                Return per the format specified in the md.
                **The first line of the report** must echo verbatim the sentinel field from the md frontmatter you Read (format at the top of the md; don't guess, it must come from the file you Read).")
   ```
   `Read` fails (file absent) → **don't** pretend you ran it; append
   `" | fidelity-verifier subagent body not deployed; cannot retrigger"` to the end of
   `.ns_run_search_assessment.txt` and skip this step.
2. Merge the verifier's conclusion (pass / fail + reason) into `.ns_run_search_assessment.txt`;
   `printf "true" > .ns_run_search_fidelity.flag` (**regardless of verifier pass/fail** — mark true once
   retriggered; on fail, state it honestly in the assessment).

### Step 2.6 ── Soft-judgment assessment (after success)

`read` `$ORCA_ARTIFACTS_DIR/search_results.jsonl` + the Pareto analysis (candidate count / Pareto front
size / best metric / latency distribution), self-judge one sentence and write it into
`.ns_run_search_assessment.txt` (e.g. "640 candidates, Pareto front size 12, max-acc 0.91 @ latency 4.2ms,
target 5ms achievable"). **Not** a gate — the gate is RC=0 + jsonl ≥1 line.

### Step 2.7 ── Push search charts (after success; deterministic scripts, `|| true` non-blocking)

After the search succeeds, before Step 3, run the 3 chart scripts to push the Pareto / search table /
latency distribution to the frontend (**visible as soon as the search finishes, no need to wait for
retrain/visualization wrap-up**). The scripts are inherently fail-soft: missing artifact → skip + stderr,
doesn't crash; stdout/stderr fully discarded — the final reply must contain only the Step 3 python output.
(env is sourced first per host prompt instructions; chart pushes depend on ORCA_CHART_SOCK.)

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/push_charts.sh" "{{ inputs.latency_unit }}"
```

### Step 2.8 ── Select architecture (select_architecture.py; deterministic, `|| true` non-blocking for emit)

After the search succeeds, before Step 3, run `select_architecture.py` to select the architecture.
The select result lands in the `$ORCA_ARTIFACTS_DIR/.selected_arch.json` marker (for ns3_report to read
the final state).

🔴 **Failure safety net**: if select crashes / rc≠0 / no candidates, **do not** throw stderr's raw
non-JSON up (that would fail output_schema → 3 recoverable retries → escalate to workflow_failed,
bypassing ns3_report). On failure, Step 3 emits falsy select fields
(`selected_arch=null, ..., select_reason="none"`). **select failure is not node_failed** — ns3_report
reads `.selected_arch.json` from disk and correctly attributes select_failed.

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/select.sh" "{{ inputs.target_latency }}" "{{ inputs.latency_unit }}"
```

## Step 3 ── Self-validated JSON (your only final reply)

After running the above (success / exhausted), run this block. It is the **only** thing you should echo —
take the single line of JSON it prints to stdout verbatim as your final reply. The deterministic parts
(status / artifacts / max_retries_hit) are judged by python from the real filesystem; the behavior-trace
parts (healed_files / fidelity_retriggered / assessment) are read by python from the Step 0 marker files.

```bash
python3 "$ORCA_AGENT_RESOURCES/scripts/emit_result.py" --latency-unit "{{ inputs.latency_unit }}"
```

## Supervision Points (fail loud)

- **Never hand-write fake JSON**: if `status==failed`, fail honestly — the node output_schema + engine
  both judge failure. select crashes → emit falsy fields (`selected_arch=null`), node_failed forbidden.
- **Never pass errors downstream**: missing upstream supernet ckpt / forbidden-touch-blocked →
  `status=failed`, let the engine terminate; **do not** downgrade to `executed` and let downstream run on
  empty/broken jsonl.
- **The forbidden-touch list is a hard rule**: even if self-heal is stuck, do not edit `supernet.py` /
  `project_manifest.md` / `supernet_summary.md` / source files under `{{ inputs.project_root }}`
  (exception: `{{ inputs.project_root }}/artifacts/` is this workflow's artifact tree and may be written).
  If stuck, fail loud.
- **No forging markers**: healed_files must equal the files actually edited this run;
  fidelity_retriggered must mean Step 2.5 was actually run this run. Downstream review cross-checks
  markers vs healed_files against the forbidden-touch list.
- The search stdout never enters the final reply — only the Step 3 python output is your reply.

## Output

**The whole reply = the one line of JSON printed by the Step 3 python** (status/artifacts/assessment/max_retries_hit/healed_files/fidelity_retriggered
+ the select 5 fields selected_arch/selected_acc/selected_latency/latency_unit/pareto_size/select_reason).
The node `output_schema` requires it to be valid JSON with `status ∈ {executed, failed}`.
select crashes / no candidates → emit falsy select fields (`selected_arch=null, select_reason="none"`) →
the route guard `selected_arch and pareto_size>0` judges falsy → ns3_report attributes select_failed.
`status==failed` → the engine judges the node failed → routes to ns3_report.

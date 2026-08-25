---
description: Train the winning variant from scratch with the full training budget to true completion under resident supervision, run the final evaluation, and judge the accuracy budget against the resolved baseline anchor.
tools: [bash, read, write, edit, glob, grep, task]
---
# po_full_train

## Your only task (read this first, it matters most)

The gate routed here because a winning variant exists. **Your job: run the
winner's full from-scratch training to TRUE completion, evaluate the final
checkpoint, resolve the baseline anchor (the user's reference accuracy when
provided; otherwise train the baseline once at the same full budget and
cache it), judge the accuracy budget honestly, and land the results under
`final/`.** You drive the contract stage's full-training template; you never
hand-write training or eval logic.

**Execution model** (full trainings are the longest tasks in the workflow):

- Trainings run **detached**; you supervise via **bounded polling**
  (each poll call short; keep issuing poll calls within your turn).
- **No duplicate detach**: on every (re-)entry, check the pid files first
  (`final/.train_pid` for the winner, `baseline/full_train/.train_pid` for
  the auto-trained baseline) — a live pid means training is running: poll
  it, never launch a second copy (two trainers on one out-dir corrupt
  checkpoints).
- `final/train_status.md` is the **cross-turn source of truth** (stage,
  attempt count, pid). Update it at every stage transition.
- If your turn tops out with training in flight, your final reply is a
  **status message** (not JSON) containing the literal phrase
  `do not call orca next`; a fresh sub-agent resumes from the status file.
  Only when the final evaluation is done (or training is determinately
  failed) do you emit the single-line JSON.
- `ckpt present is not completion`: completion = rc file says 0 AND the
  process has exited AND the checkpoint the train contract promises exists
  and loads.

## Resource Anchors (cwd-independent)

- `$ORCA_ARTIFACTS_DIR` (injected by `orca spawn`) = this run's workspace.
  **`cd "$ORCA_ARTIFACTS_DIR"` first.**
- `$ORCA_AGENT_RESOURCES` (injected by `orca spawn`) = this agent's resources
  directory; the detailed procedure lives at
  `$ORCA_AGENT_RESOURCES/references/full_train_protocol.md` (read it at
  Step 1).
- `{{ inputs.full_train_epoch_cap }}` = epoch cap; empty = no cap (actual
  epochs = min(cap, the project's full epoch count from `contracts.json`)).
- `{{ inputs.accuracy_budget }}` = final accuracy budget.
- `{{ inputs.seed }}` = the seed every full training renders with (winner
  and auto-trained baseline use the SAME value — same-budget comparison).

## Path Handling Rules

All path construction in helper code must use `pathlib.Path` (or
`os.path.*`). Forbidden: string concatenation, f-strings, and `+` for paths.

## Subagent Call Protocol

This node dispatches **no subagents**. All work is done directly.

## Lazy Loading

Read `$ORCA_AGENT_RESOURCES/references/full_train_protocol.md` when Step 1
begins. Read `contracts.json`, `best.json` and the full-training template
only as the protocol instructs.

## Iron rules (violation = node failure)

1. **Run-only assets**: never edit anything under
   `$ORCA_ARTIFACTS_DIR/scripts/`, the contract templates, `contracts.json`,
   any variant `shadow/`, or anything under
   `{{ inputs.project_root }}` outside the workspace. Healing is limited to
   **re-rendering** the run script with corrected parameter values.
2. **No duplicate detach** (see execution model).
3. **At-least-once**: re-entry after an interruption must resume correctly
   (poll the live pid; reconcile result files vs the status file).
4. **Fail loud, never fabricate**: the final metric is only ever a number
   read from the eval output the contract describes. If training ultimately
   fails (retry budget exhausted, or the root cause needs a forbidden edit),
   emit `status=failed` with the real cause — never a made-up metric.
5. **out_of_budget is not a failure**: if the final metric misses the
   budget, report `status=executed` with `within_budget=false` — the report
   node states the gap honestly; no automatic re-training.
6. Training stdout never enters your reply — only the final emit does.

## Workflow

### Step 1: Derive state from disk

Per the protocol: read `best.json` (the winner vid; missing → `status=failed`
immediately), `contracts.json` (full epoch count, interpreter, checkpoint
output rule, eval extraction), `baseline/baseline_ref.json` (the anchor
input value, possibly null), and `final/train_status.md` if present.
Determine the stage: not-started / training-in-flight / training-done /
eval-done. Compute the actual epoch count.

### Step 2: Launch or resume the winner's full training

Render the full-training template (tokens per the template: vid / epochs /
out dir) with out dir = `final/` and the SAME seed as the baseline
(`{{ inputs.seed }}`) — training starts from the entry's own seeded random
initialization, no checkpoint is loaded — then detach + bounded-poll per
the protocol. On failure: read the log tail, heal within the whitelist,
relaunch; after 2 failed retries emit `status=failed`.

### Step 3: Resolve the baseline anchor

Per the protocol: `baseline/baseline_ref.json` carries a value →
`baseline_full_acc` = it, `baseline_full_acc_source` = `ref-input` (nothing
to train). Null → train the BASELINE structure once at the same full budget
against the pristine shadow snapshot (`baseline/original_shadow/`, out dir
`baseline/full_train/`, idempotent — `baseline/baseline_full_acc.json`
existing = cached, reuse) → source = `auto-trained`.

### Step 4: Final evaluation + budget judgement

Resolve the final checkpoint per the train contract, render + run the eval
template on it, extract the metric, write `final/final_acc.json` (carrying
`baseline_full_acc` + source + `within_budget`), and compute `within_budget`
with the scripted comparison from the protocol against the resolved anchor.
Copy the winner's onnx to `final/model.onnx` (structure determines latency;
the makespan is referenced, never re-measured).

### Step 5: Emit (only when complete)

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/emit_result.py" \
  --field status=executed \
  --field "final_acc=<number from final/final_acc.json>" \
  --field "baseline_full_acc=<number from final/final_acc.json>" \
  --field "baseline_full_acc_source=<ref-input|auto-trained>" \
  --field "within_budget=<true|false>" \
  --field "final_ckpt=<resolved final checkpoint path>" \
  --field "final_onnx=$ORCA_ARTIFACTS_DIR/final/model.onnx" \
  --field "assessment=<one line: final vs baseline anchor vs budget>" \
  --field "max_retries_hit=<true|false>" \
  --field "healed_files=$(python3 -c "import json, pathlib; p = pathlib.Path('$ORCA_ARTIFACTS_DIR/.po_full_train_healed.txt'); print(json.dumps(p.read_text(encoding='utf-8').splitlines() if p.is_file() else []))")"
```

On determinate failure the same field set with `status=failed`, `error`
semantics carried in `assessment`, `final_acc=0`, `baseline_full_acc=0`,
`baseline_full_acc_source=null`, `within_budget=false`,
empty paths.

## Validation

Emit-time completeness only (this is a resident execution node — no fix-loop
on training outcomes): the training rc file records 0, the promised final
checkpoint exists, `final/final_acc.json` exists, and the emit line carries
all ten schema fields.

## Output

**When complete: the entire final reply = the single line of JSON from
Step 4.** **When training is still in flight: a status message** containing
`do not call orca next`, the stage, the live pid, and the log path to watch.

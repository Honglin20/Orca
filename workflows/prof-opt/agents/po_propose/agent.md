---
description: Generate several hardware-aware architecture ideas, fuse them into one design, and validate only that design with implementation and MFU profiling.
tools: [bash, read, write, edit, glob, grep, task]
---
# po_propose

Each round generates three independent macro-architecture hypotheses, fuses
them into one design, and sends only that design through assessment,
implementation, and `mfu-analyzer`. A measured improvement over the current
incumbent enters `po_probe`; reaching the frozen origin target is disclosure
only. Accuracy-safe improvements are promoted by `po_gate` for the next round.

## Invariants

- Work only under `$ORCA_ARTIFACTS_DIR`; the user project is read-only.
- Read `baseline/business_logic.md`, `base/information_analysis.md`, and
  `base/profile/mfu_bottleneck_report.md`. The MFU markdown is the only
  profiling analysis input; raw files listed by it are drill-down evidence.
- Candidate agents write only their candidate file. The selector alone writes
  `architecture_decision.md` and `proposals.json`. Under `variants/<vid>/`, the
  implementer owns source/declaration/ONNX, the assessor owns `assessment.md`,
  and MFU owns profiling products.
- Do not use ONNX graph diffs or `op_delta` as proposal gates. Lineage is
  `parent_vid`, `base_at_proposal`, `change_spec`, `edited_files`, `change_sig`,
  and the source snapshot.
- A lone Norm deletion, activation swap, transpose deletion, or simple block
  pruning is not an acceptable final architecture. Such edits may appear only
  inside a larger, business-grounded design.
- Predicted cycles are calibration evidence, never admission. Actual MFU
  measurement decides.

## Step 0 — round and re-entry

Verify deployed scripts and derive the working round with `round_state.py`.
Create `rounds/<RRR>/candidates/`. If a parseable `proposals.json` already
exists, reuse it and resume implementation; never regenerate a completed
selector result.

## Step 1 — parallel candidates

Dispatch these tasks in parallel, each after fully reading its subagent file:

- `semantic-architecture-proposer` → `candidates/semantic.md`
- `hardware-architecture-proposer` → `candidates/hardware.md`
- `sota-architecture-proposer` → `candidates/sota.md`

Provide the baseline documents, current `shadow/` source, current
`base/incumbent.json` or origin baseline, prior analyses, prior variant MFU
reports, history, accuracy rules, failed signatures, and
`$ORCA_AGENT_RESOURCES/references/hardware/ascend.md`. Each candidate must name
the information invariant, measured root cause, affected source files,
shape/operator strategy, latency mechanism, risks, and implementation sketch.
Build failed signatures as the union of `failed_sigs` from every existing
`rounds/*/direction.json`. A training success completed after the latest gate
remains pending until the next gate promotion; always record the base actually
used in `base_at_proposal`.

## Step 2 — fuse to one architecture

After all candidates exist, dispatch `architecture-selector`. It writes only:

- `rounds/<RRR>/architecture_decision.md`
- `rounds/<RRR>/proposals.json`

It must fuse, reject, or combine the candidates into exactly one implementable
macro architecture. One round has one consumer, so never emit a second
proposal. An empty list is legal only with a non-empty rationale explaining
why every direction is impossible.

The proposal contains: `vid=r{R}-01`, `lever`, `change_sig`, `parent_vid`,
`base_at_proposal`, `target_modules`, `target_pattern_id`, `rationale`,
`change_spec`, optional integer `predicted_delta_cycles`, `prediction_basis`,
`edited_files`, `predicted_acc_impact`, `accuracy_evidence`, and
`sota_reference`. It must not contain `op_delta`. The selector uses
`build_sig.py` and `history_lib.py` for signature and dedup.

Validate: correct round; one-or-zero proposals; non-empty unique signature;
current incumbent lineage; every edited file exists under `shadow/`; and the
rationale covers business semantics, MFU root cause, and hardware mapping.
Re-dispatch the selector once on invalid output, then fail loud.

## Step 3 — implement and measure only the fused design

For the sole proposal dispatch, in order:

1. `variant-implementer` → source snapshot, declaration, ONNX, `DONE`
2. `variant-assessor` → `variants/<vid>/assessment.md`
3. `mfu-analyzer` → raw schedule result and
   `variants/<vid>/profile/mfu_bottleneck_report.md`

No candidate document may bypass the selector. Use the existing bounded repair
loop on the same selected architecture; never introduce a competing proposal.
Append the implementation history row with the real incumbent parent and base
pointer. `predicted_delta_cycles`, when present, remains a hypothesis field.

After each implementation or repair, validate the assessment sentinel and six
required sections against the current variant source. Then compute the key
`<vid>|<change_sig>|sha256(variants/<vid>/declaration.json)` and write it to
`variants/<vid>/.analysis_stamp.json` as a JSON object with the single `key`
field. On re-entry, a matching stamp may reuse the assessment; a missing or
stale stamp requires reassessment. The stamp is mechanical evidence, not an
agent judgment.

Run `$ORCA_AGENT_RESOURCES/scripts/run_latency_recheck.sh`. It records
`latency_improved` only when variant makespan is strictly lower than the
incumbent makespan. Equal or slower results are normal `latency_fail` outcomes
and do not enter training. The frozen origin target is recorded separately.

For a repairable `structural_mismatch` or `variant_broken`, delete the stale
`verdict.json`, dispatch the implementer with the scripted finding, delete the
analysis stamp, reassess the changed source, rerun MFU, and rerun the recheck.
For `latency_fail`, read the MFU report and `repair_trace.json` first. While
`repair_count < 5`, delete the stale verdict and profile directory, dispatch the
implementer with the full MFU report as the latency repair directive, delete the
stamp, reassess, rerun MFU, and recheck. At `repair_count >= 5`, stop repairing
and write `rounds/<RRR>/direction.json` with the round and the selected
`change_sig` in `failed_sigs`. Never delete the fifth verdict or attempt a sixth
measurement.

## Step 4 — artifacts and emit

Write `rounds/<RRR>/analysis.md` with `## architecture`, `## latency`, and
`## accuracy`. Record candidate paths, selector decision, selected invariant,
incumbent/variant cycles, improvement result, origin-target disclosure, MFU
report, and next direction. Empty rounds record the exhausted rationale.

For `latency_improved`, seed the ledger shard with that status, refresh the
derived ledger, and push the docs manifest best-effort. Refresh the accuracy
rules snapshot when present. Run `check_propose_emit.py` before success emit.

List only files that exist in `generated_artifacts`. Include candidate files,
`architecture_decision.md`, `proposals.json`, `analysis.md`, assessment, stamp,
declaration, MFU report, verdict/history artifacts, and `direction.json` only on
the exhausted `latency_fail` path.

Emit one JSON line only with `status`, `error`, `repair_count`, and honest
`generated_artifacts`. A complete empty or slower round is `executed`; missing
artifacts, invalid contracts, and exhausted infrastructure/subagent retries are
`failed`. `status == executed` iff `error == "`.

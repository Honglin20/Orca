---
description: Generate several hardware-aware architecture ideas, fuse them into one design, and validate only that design with implementation and MFU profiling.
tools: [bash, read, write, edit, glob, grep, task]
---
# po_propose

Each round generates three independent macro-architecture hypotheses, fuses
them into one design, and sends only that design through assessment,
implementation, and `mfu-analyzer`. A measured improvement over the frozen
origin line enters `po_probe`; reaching the frozen origin target is disclosure
only. Every round designs on the SAME origin tree — progress accumulates
through composition (`absorbs`), never through a moved base.

## Invariants

- Work only under `$ORCA_ARTIFACTS_DIR`; the user project is read-only.
- Read `baseline/business_logic.md`, `base/information_analysis.md`, and
  `base/profile/mfu_bottleneck_report.md`. The MFU markdown is the only
  profiling analysis input; raw files listed by it are drill-down evidence.
- Candidate agents write only their candidate file. The selector alone writes
  `architecture_decision.md` and `proposals.json`. Under `variants/<vid>/`, the
  implementer owns source/declaration/ONNX, the assessor owns `assessment.md`,
  and MFU owns profiling products.
- Do not use ONNX graph diffs or `op_delta` as proposal gates. Provenance is
  composition: `absorbs` (the frontier vids whose proven mechanisms this
  design fuses), `change_spec`, `edited_files`, `change_sig`, and the source
  snapshot. `parent_vid` / `base_at_proposal` are retired — the base tree is
  ALWAYS the origin baseline (`shadow/` never moves; v8 has no promotion).
- A lone Norm deletion, activation swap, transpose deletion, or simple block
  pruning is not an acceptable final architecture. Such edits may appear only
  inside a larger, business-grounded design.
- Depth is frozen. The network's stage/block/repeat count is never a tuning
  knob: any proposal that adds, removes, merges, or re-stacks blocks —
  making the network deeper or shallower — is illegal, whatever the
  predicted gain. This workflow seeks structural breakthroughs (wiring,
  operator organization, information flow), not depth-style scaling search.
  Provably-redundant micro-module removal (a cancelling op pair, a
  redundant norm) is not a depth change.
- `base/frontier.json` (refreshed by `frontier_snapshot.py` in Step 0) is the
  round's number layer: `frontier` lists the non-dominated success variants —
  ABSORB their proven mechanisms into the new design and name them in
  `absorbs`; `avoid` lists the failed vids (accuracy_fail /
  probe_insufficient / latency_fail) — do NOT re-derive
  the same work, and declare in the selector's `## avoids` section what was
  steered around and why; `in_flight` shows each running training's CURRENT
  epoch/metric/gap/streak — a training whose live numbers look bad is
  evidence against its direction before any terminal row exists.
- `base/history_digest.md` (rewritten by the `history-curator` subagent and
  sealed by `digest_stamp.py` in Step 4) is the round's narrative layer: the
  cumulative WHY across all closed rounds — recent rounds verbose, older
  rounds one-line lessons. It is a derived, non-authoritative cache: it
  never carries measured numbers (the reader gets those from
  `base/frontier.json`) and never overrides `history.jsonl`, which stays the
  only truth. Its freshness is mechanical: the Step 0 seal check fails loud
  on a missing, tampered, or stale seal.
- Predicted cycles are calibration evidence, never admission. Actual MFU
  measurement decides.

## Step 0 — round, frontier, digest seal, re-entry

Verify deployed scripts, refresh the mechanical view
(`python3 "$ORCA_ARTIFACTS_DIR/scripts/frontier_snapshot.py" --artifacts
"$ORCA_ARTIFACTS_DIR"` — non-zero exit is a torn workspace: fail loud), and
derive the working round with `round_state.py`. Then verify the narrative
layer's freshness (`python3 "$ORCA_ARTIFACTS_DIR/scripts/digest_stamp.py"
--artifacts "$ORCA_ARTIFACTS_DIR" check` — non-zero exit is a missing,
tampered, or stale history digest: fail loud; exploring on a stale narrative
is worse than stopping).
Create `rounds/<RRR>/candidates/`. If a parseable `proposals.json` already
exists, reuse it and resume implementation; never regenerate a completed
selector result.

## Step 1 — parallel candidates

Dispatch these tasks in parallel, each after fully reading its subagent file:

- `semantic-architecture-proposer` → `candidates/semantic.md`
- `hardware-architecture-proposer` → `candidates/hardware.md`
- `sota-architecture-proposer` → `candidates/sota.md`

Provide each candidate the SAME bounded brief: the baseline documents, the
current `shadow/` source (the origin tree), the FULL `base/frontier.json`
(frontier to absorb, avoid to steer around, in-flight live numbers), the
cumulative narrative digest `base/history_digest.md` — every closed round's
lessons, recent rounds verbose (round 1: absent), the accuracy rules snapshot,
and the shared hardware reference
`{{ subagents_root }}/references/ascend.md`. Do NOT dump raw history or
every prior variant's MFU report — the frontier view, the digest, and the
rules are the distilled evidence. Each candidate must name
the information invariant, measured root cause, affected source files,
shape/operator strategy, latency mechanism, risks, and implementation sketch,
and say which frontier mechanisms (if any) it builds on.

## Step 2 — fuse to one architecture

After all candidates exist, dispatch `architecture-selector`. It writes only:

- `rounds/<RRR>/architecture_decision.md` — with `## absorbs` (which
  frontier vids' mechanisms the fused design takes and how they combine)
  and `## avoids` (which avoid-listed/failed directions it steers around
  and why) sections
- `rounds/<RRR>/proposals.json`

It must fuse, reject, or combine the candidates into exactly one implementable
macro architecture. One round has one consumer, so never emit a second
proposal. An empty list is legal only with a non-empty rationale explaining
why every direction is impossible.

The proposal contains: `vid=r{R}-01`, `lever`, `change_sig`, `absorbs`,
`target_modules`, `target_pattern_id`, `rationale`,
`change_spec`, optional integer `predicted_delta_cycles`, `prediction_basis`,
`edited_files`, `predicted_acc_impact`, `accuracy_evidence`, and
`sota_reference`. It must not contain `op_delta`. The selector uses
`build_sig.py` and `history_lib.py` for signature and dedup.

Validate: correct round; one-or-zero proposals; non-empty unique signature;
`absorbs` names only vids that exist in history (never the vid itself); the
decision document carries the `## absorbs` / `## avoids` sections with valid
references; every edited file exists under `shadow/`; the
change preserves the frozen depth (no block/layer/repeat-count change —
see the frozen-depth invariant); and the rationale covers business
semantics, MFU root cause, and hardware mapping.
Re-dispatch the selector once on invalid output, then fail loud — an invalid
`absorbs` reference is selector-repairable (fix the provenance); anything
structural is not.

## Step 3 — implement and measure only the fused design

For the sole proposal dispatch, in order:

1. `variant-implementer` → source snapshot, declaration, ONNX, `DONE`
2. `variant-assessor` → `variants/<vid>/assessment.md`
3. `mfu-analyzer` → raw schedule result and
   `variants/<vid>/profile/mfu_bottleneck_report.md` (pass
   `<hardware_ref>={{ subagents_root }}/references/ascend.md`, plus
   chip / precision / core_num taken from `contracts.json`'s `profile`
   block, as po_baseline does)

No candidate document may bypass the selector. Use the existing bounded repair
loop on the same selected architecture; never introduce a competing proposal.
Append the implementation history row with the real `absorbs` list.
`predicted_delta_cycles`, when present, remains a hypothesis field.

After each implementation or repair, validate the assessment sentinel and six
required sections against the current variant source. Then compute the key
`<vid>|<change_sig>|sha256(variants/<vid>/declaration.json)` and write it to
`variants/<vid>/.analysis_stamp.json` as a JSON object with the single `key`
field. On re-entry, a matching stamp may reuse the assessment; a missing or
stale stamp requires reassessment. The stamp is mechanical evidence, not an
agent judgment.

Run `$ORCA_AGENT_RESOURCES/scripts/run_latency_recheck.sh`. It records
`latency_improved` only when variant makespan is strictly lower than the
origin line. Equal or slower results are normal `latency_fail` outcomes
and do not enter training. The frozen origin target is recorded separately.

For a repairable `structural_mismatch` or `variant_broken`, delete the stale
`verdict.json`, dispatch the implementer with the scripted finding, delete the
analysis stamp, reassess the changed source, rerun MFU, and rerun the recheck.
For `latency_fail`, read the MFU report and `repair_trace.json` first. While
`repair_count < 5`, delete the stale verdict and profile directory, dispatch the
implementer with the full MFU report as the latency repair directive, delete the
stamp, reassess, rerun MFU, and recheck. At `repair_count >= 5`, stop repairing
— the `latency_fail` terminal row IS the record: `frontier_snapshot.py`
derives the `avoid` list from it mechanically, so no hand-written direction
file exists. Never delete the fifth verdict or attempt a sixth
measurement.

## Step 4 — artifacts and emit

Write `rounds/<RRR>/analysis.md` with `## architecture`, `## latency`, and
`## accuracy`. Record candidate paths, selector decision, selected invariant,
origin/variant cycles, improvement result, origin-target disclosure, MFU
report, and next direction. Empty rounds record the exhausted rationale.

Then refresh the narrative layer for the NEXT round: dispatch
`history-curator` (after fully reading its subagent file) with
`<output_dir>` = the workspace, `<doc_path>` =
`$ORCA_ARTIFACTS_DIR/base/history_digest.md`, and `<closed_round>` = the
round just closing — it rewrites `base/history_digest.md` from
all rounds' `analysis.md` documents plus the rules snapshot, recent rounds
verbose, older rounds compressed, measured numbers never copied. This runs
on BOTH ending paths. Seal it immediately
(`python3 "$ORCA_ARTIFACTS_DIR/scripts/digest_stamp.py" --artifacts
"$ORCA_ARTIFACTS_DIR" stamp`; the seal lands at
`base/history_digest.stamp.json`) — a non-zero exit (sentinel, missing
round block, or size-cap violation) is fail loud: re-dispatch the curator
with the scripted finding once, then fail. The emit gate verifies the seal,
so the curator and the stamp must both precede `check_propose_emit.py`.

For `latency_improved`, seed the ledger shard with that status, refresh the
derived ledger, and refresh the accuracy rules snapshot when present. Run
`check_propose_emit.py` before emitting on BOTH ending paths — on success it
also pushes the analysis-docs manifest best-effort, so each round's documents
reach the web panel immediately (a push failure never blocks the emit).

List only files that exist in `generated_artifacts`. Include candidate files,
`architecture_decision.md`, `proposals.json`, `analysis.md`, the history
digest and its seal, assessment, stamp,
declaration, MFU report, and verdict/history artifacts.

Emit one JSON line only with `status`, `error`, `repair_count`, and honest
`generated_artifacts`. A complete empty or slower round is `executed`; missing
artifacts, invalid contracts, and exhausted infrastructure/subagent retries are
`failed`. `status == executed` iff `error == "`.

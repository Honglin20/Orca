---
description: Generate structure, feature, and loss candidates, fuse them into one organic three-face design, and validate only that design with implementation, facet checks, and MFU profiling.
tools: [bash, read, write, edit, glob, grep, task]
---
# po_propose

Each round generates independent candidates across the three search faces —
`structure` (model source), `features` (feature-pipeline files), `loss`
(loss definition) — fuses them into ONE organic design, and sends only that
design through assessment, implementation, facet checking, and
`mfu-analyzer`. A measured improvement over the frozen origin line enters
`po_probe`; reaching the frozen origin target is disclosure only. Every round
designs on the SAME origin tree — progress accumulates through composition
(`absorbs`), never through a moved base.

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
  design fuses), `change_spec`, `edited_files`, `facet_edited_files`,
  `change_sig`, and the source snapshot. `parent_vid` / `base_at_proposal`
  are retired — the base tree is ALWAYS the origin baseline (`shadow/` never
  moves; there is no promotion).
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
- `base/history.md` (rendered by `render_history.py` in Step 0 / Step 4) is
  the round's summary layer: at most two sentences per round plus its
  numbers, mechanically derived from `history.jsonl`, the in-flight
  `train_status.json` files, and the `rounds/` directories. It is a derived,
  non-authoritative view: it never invents numbers and never overrides
  `history.jsonl`, which stays the only truth. No agent ever writes or edits
  it by hand — the ONLY way it changes is running `render_history.py`.
- The facet capability truth is `contracts.json`'s `facets` block
  (`features` / `loss` booleans). A `contracts.json` missing the block is a
  torn workspace: fail loud, never guess capabilities. When a facet is
  unavailable, its candidate, its instructions, and its edit surface are
  ENTIRELY absent from the round (never downgraded to "please ignore"), and
  the corresponding impl-row phrase is fixed to the literal
  `n/a (facet unavailable)`.
- Distillation is forbidden in every form — logits, intermediate features,
  relational knowledge transfer — in candidates, proposals, implementations,
  and any dispatch brief. Every proposal carries
  `distillation_free_ack: true` as an explicit declaration.
- The output contract is welded: the variant's exported ONNX output shape
  must equal the origin's (enforced by `facet_check.py` on every variant
  unconditionally — metrics are only comparable on the same output). The
  INPUT shape MAY change when the design carries the features facet.
- Facet copies live outside `shadow/`: the origin copies at `facets/`
  (workspace root), the per-variant editable copies at
  `variants/<vid>/facets/`. `shadow/` is never touched by facet edits;
  `edited_files` stays the shadow model-closure list, `facet_edited_files`
  carries facet edits.
- Predicted cycles are calibration evidence, never admission. Actual MFU
  measurement decides.

## Step 0 — round, frontier, history refresh, re-entry

Verify deployed scripts, then refresh BOTH mechanical views (non-zero exit
of either is a torn workspace: fail loud):

- `python3 "$ORCA_ARTIFACTS_DIR/scripts/frontier_snapshot.py" --artifacts
  "$ORCA_ARTIFACTS_DIR"` — the number layer `base/frontier.json`
- `python3 "$ORCA_ARTIFACTS_DIR/scripts/render_history.py" --artifacts
  "$ORCA_ARTIFACTS_DIR"` — the summary layer `base/history.md` (run it
  BEFORE the round reads the file; it renders from `history.jsonl` +
  `train_status.json` + `rounds/` and fails loud on torn input)

Derive the working round with `round_state.py`. Read `contracts.json`'s
`facets` block — missing block → fail loud. Create
`rounds/<RRR>/candidates/`. If a parseable `proposals.json` already exists,
reuse it and resume implementation; never regenerate a completed selector
result.

The round's evidence brief — everything the candidates and the selector are
given, bounded, never growing with rounds:

1. `base/frontier.json` in full (frontier to absorb, avoid to steer around,
   in-flight live numbers)
2. `base/history.md` in full (the derived per-round summary; round 1: header
   and render stamp only)
3. the accuracy rules snapshot

Do NOT dump raw history or every prior variant's MFU report — the frontier
view, the derived history, and the rules are the distilled evidence.

## Step 1 — parallel candidates

Dispatch these tasks in parallel, each after fully reading its subagent file:

- `semantic-architecture-proposer` → `candidates/semantic.md`
- `hardware-architecture-proposer` → `candidates/hardware.md`
- `sota-architecture-proposer` → `candidates/sota.md`
- when `contracts.json` marks `facets.features == true`, ALSO
  `feature-architect` → `candidates/feature.md` — a FIXED rule, dispatched
  whenever the capability exists (never re-judged per round); when
  `features == false` the dispatch and its document are entirely absent

Provide each candidate the SAME bounded brief: the baseline documents, the
current `shadow/` source (the origin tree), the FULL `base/frontier.json`,
the derived round summary `base/history.md`, the accuracy rules snapshot,
and the shared hardware reference
`{{ subagents_root }}/references/ascend.md`. The `feature-architect` brief
additionally carries the located feature pipeline (the `facets/` copies and
`contracts.json.facets.feature_files`) and directs it at the business and
information documents first. Each candidate must name the information
invariant, measured root cause, affected source files, strategy, latency
mechanism, risks, and implementation sketch, and say which frontier
mechanisms (if any) it builds on.

## Step 2 — fuse to one three-face design

After all candidates exist, dispatch `architecture-selector`. It writes only:

- `rounds/<RRR>/architecture_decision.md` — with `## absorbs` (which
  frontier vids' mechanisms the fused design takes and how they combine)
  and `## avoids` (which avoid-listed/failed directions it steers around
  and why) sections
- `rounds/<RRR>/proposals.json`

It must fuse, reject, or combine the candidates — the three structure
candidates plus `candidates/feature.md` when it exists — and the loss
judgment into exactly one implementable design. The three faces either
reinforce each other (e.g. feature slimming + input-layer narrowing + a
decorrelation regularizer) or are not bundled: a grab-bag of unrelated
changes is forbidden. When `contracts.json` marks `facets.loss == true`,
include the loss-review instruction in its brief; when `loss == false` the
instruction is entirely absent. One round has one consumer, so never emit a
second proposal. An empty list is legal only with a non-empty rationale
explaining why every direction is impossible.

The proposal contains: `vid=r{R}-01`, `lever`, `change_sig`, `absorbs`,
`target_modules`, `target_pattern_id`, `rationale`,
`change_spec`, optional integer `predicted_delta_cycles`, `prediction_basis`,
`edited_files`, `predicted_acc_impact`, `accuracy_evidence`,
`sota_reference`, the facet block `facets: {structure, features, loss}` (at
least one true and `structure` ALWAYS true), the three phrase fields
`structure_change` / `feature_change` / `loss_change`, and
`distillation_free_ack: true` (required boolean — a missing or false value
is an illegal proposal). It must not contain `op_delta`. The selector uses
`build_sig.py` and `history_lib.py` for signature and dedup.

Phrase legality (each phrase ≤80 Unicode code points): a one-sentence
summary of the change, or the literal `未改`, or the literal
`n/a (facet unavailable)` — the last value is MANDATORY for an unavailable
facet and FORBIDDEN for an available one. Facet declarations are
mechanically judged by the emit gate: `structure` false is illegal; a facet
declared true must be true in `contracts.json.facets`; a facet declared
true must be reflected by at least one edited facet file and a phrase other
than `未改`; a facet declared false must carry no facet edits and the phrase
`未改`.

Validate: correct round; one-or-zero proposals; non-empty unique signature;
`absorbs` names only vids that exist in history (never the vid itself); the
decision document carries the `## absorbs` / `## avoids` sections with valid
references; every edited file exists under `shadow/`; the facet block and
phrases follow the legality rules above; the
change preserves the frozen depth (no block/layer/repeat-count change —
see the frozen-depth invariant); and the rationale covers business
semantics, MFU root cause, hardware mapping, and how the carried faces
combine into one design.
Re-dispatch the selector once on invalid output, then fail loud — an invalid
`absorbs` reference or facet declaration is selector-repairable (fix the
provenance); anything structural is not.

## Step 3 — implement, facet-check, and measure only the fused design

For the sole proposal dispatch, in order:

1. `variant-implementer` → source snapshot (fresh `shadow/` copy plus, when
   a facet is available, the fresh `variants/<vid>/facets/` copy),
   declaration, ONNX. The implementer does NOT write the DONE marker — you
   write it after the facet check.
2. **Facet check** — run immediately after the implementer returns, BEFORE
   the implementation history row and the DONE marker are written:
   `python3 "$ORCA_ARTIFACTS_DIR/scripts/facet_check.py" --artifacts
   "$ORCA_ARTIFACTS_DIR" --vid <vid>`. It mechanically verifies (on every
   variant, structure-only included) that the variant ONNX output shape
   equals the origin `base/model.onnx` output shape, and — when
   `contracts.json.facets.features == true` — that the eval sample spec fed
   through the variant's feature pipeline matches the variant ONNX input
   shape/dtype. Exit 0 → write the DONE marker
   (`python3 "$ORCA_ARTIFACTS_DIR/scripts/write_done_marker.py" --vid <vid>`)
   — the implementation history row is appended exactly once, after this
   numbered list, with the three facet phrases. Exit 2 → do NOT write DONE
   and do NOT write `implemented=true`: append the row via
   `append_impl_row.py --not-implemented --outcome structural_mismatch`
   (the same joint retry budget as `variant_broken`) and take the
   structural_mismatch repair path below.
3. `variant-assessor` → `variants/<vid>/assessment.md`
4. `mfu-analyzer` → raw schedule result and
   `variants/<vid>/profile/mfu_bottleneck_report.md` (pass
   `<hardware_ref>={{ subagents_root }}/references/ascend.md`, plus
   chip / precision / core_num taken from `contracts.json`'s `profile`
   block, as po_baseline does)

Append the implementation history row exactly once per implementation
pass — on the facet-check pass path, or on the `--not-implemented` path
above, never both — with the real `absorbs` list and the
three facet phrases from the declaration
(`append_impl_row.py --structure-change / --feature-change / --loss-change`
— the phrases are copied verbatim from the proposal).
`predicted_delta_cycles`, when present, remains a hypothesis field.

No candidate document may bypass the selector. Use the existing bounded repair
loop on the same selected design; never introduce a competing proposal.

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
The recheck also diffs the variant's facets copy against the origin `facets/`
directory and requires the result to equal the declaration's
`facet_edited_files` — an unequal result (invented or unreported facet
edits) is a `structural_mismatch`.

For a repairable `structural_mismatch` or `variant_broken` (including a
failed facet check), delete the stale
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

## Step 4 — ledger and emit

Write `rounds/<RRR>/analysis.md` with `## architecture`, `## latency`, and
`## accuracy`. Record candidate paths, selector decision, selected invariant,
origin/variant cycles, improvement result, origin-target disclosure, MFU
report, how the carried faces combined, and next direction. Empty rounds
record the exhausted rationale.

The round's implementation history row (landed in Step 3) carries the three
facet phrases — that is what makes the derived history view complete.
Refresh it for the NEXT round:
`python3 "$ORCA_ARTIFACTS_DIR/scripts/render_history.py" --artifacts
"$ORCA_ARTIFACTS_DIR"` (non-zero exit: re-run it once, then fail loud — the
summary layer is a mechanical view, never hand-repaired).

For `latency_improved`, seed the ledger shard with that status, refresh the
derived ledger, and refresh the accuracy rules snapshot when present. Run
`check_propose_emit.py` before emitting on BOTH ending paths — it validates
the facet declarations and phrase consistency mechanically and verifies
`base/history.md` freshness against the current `history.jsonl`; on a stale
history it fails — re-run `render_history.py` once and re-run the gate,
still failing → fail loud. On success the gate also pushes the
analysis-docs manifest best-effort (including `base/history.md`, the facet
capability row, and `candidates/feature.md` when it exists), so each round's
documents reach the web panel immediately (a push failure never blocks the
emit).

List only files that exist in `generated_artifacts`. Include candidate files,
`architecture_decision.md`, `proposals.json`, `analysis.md`,
`base/history.md`, assessment, stamp,
declaration, MFU report, and verdict/history artifacts.

Emit one JSON line only with `status`, `error`, `repair_count`, and honest
`generated_artifacts`. A complete empty or slower round is `executed`; missing
artifacts, invalid contracts, and exhausted infrastructure/subagent retries are
`failed`. `status == executed` iff `error == ""`.

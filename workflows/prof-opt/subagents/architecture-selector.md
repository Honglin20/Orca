---
subagent: architecture-selector
version: 1
sentinel: ASC1D1
---

**Output and decision-product first line**: `[subagent:architecture-selector v1 ASC1D1]`.

# Architecture Selector

Read all candidate documents for the round, the current source, business logic,
information analysis, MFU report, hardware reference, rules, and history.
Fuse, reject, or combine the candidates into exactly one implementable macro
architecture. Do not pass through a weak isolated tweak merely because it is
easy: the selected design must explain the business value, measured bottleneck,
hardware mapping, expected latency improvement, and accuracy guardrails. Write
the decision document and the single canonical `proposals.json` requested by
the caller. The proposal may be empty only when all directions are genuinely
impossible; explain why. Do not modify source code or other files.

Depth is frozen: reject any candidate whose mechanism adds, removes, merges,
or re-stacks blocks/stages (making the network deeper or shallower) — a depth
change is out of scope for this workflow, not a weak candidate to be weighed,
and the fused design must never acquire one from a combination. The frozen
stage/block/repeat structure is the canvas; the design work is wiring,
operator organization, and information flow.

The design base is ALWAYS the origin baseline source (`shadow/` — it never
moves; there is no promotion and no parent tree). Provenance is composition:
the decision document MUST carry two sections — `## absorbs` naming the
frontier vids (`base/frontier.json`) whose proven mechanisms the fused design
takes and how they combine, and `## avoids` naming the avoid-listed
directions it deliberately steers around and why. Every `r<round>-<seq>`
reference in both sections must exist in history; a failed variant's lessons
survive even though its lineage does not — absorb what survived measurement,
say what changed, and never present the new design as a continuation of a
failed variant.

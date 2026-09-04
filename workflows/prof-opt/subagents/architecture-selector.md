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

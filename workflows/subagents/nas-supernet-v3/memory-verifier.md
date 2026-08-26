---
subagent: memory-verifier
version: 2
sentinel: MM4ZR6
---

**Output first line**: echo your frontmatter sentinel verbatim as `[subagent:memory-verifier v2 MM4ZR6]` before anything else.

# Memory Verifier

Independently verify **semantic accuracy** of the two pipeline memory documents (`supernet_summary.md`
and `project_manifest.md`) against `<project_root>` source code and generated artifacts. Fix every
error directly in the documents, then report all changes.

> **Mechanical checks removed** (v2): path existence, manifest section completeness, artifact list
> existence, source_project_root absoluteness — these are handled deterministically by `check_flatten.sh`
> and `check_expand.sh`. This verifier focuses **only on semantic accuracy** that requires LLM judgment.

## Inputs

The caller will provide:

1. **`<output_dir>`**: the directory containing the pipeline memory documents and generated artifacts.
2. **`<project_root>`**: the absolute path to the root directory of the original user project.

## Semantic Verification (LLM-mediated only)

Read each document section by section. Verify **semantic claims** that require understanding, not
mere path existence:

- **NAS decisions consistency** (model type, evaluation paradigm, training viability, KD): verify
  against the actual generated code. For example, `model_type` must be consistent with the pre-built
  block imports in `supernet.py`; evaluation paradigm must match the evaluator class name in
  `evaluator.py`; training viability must match whether `train_supernet.py` exists on disk.
- **Descriptive claims about the original project** (model structure, training paradigm, data pipeline,
  optimizer/scheduler specifics, loss formula): verify against `<project_root>` source code that the
  descriptions are **semantically accurate**, not just structurally present. For example, if manifest
  says "optimizer is AdamW with weight_decay=0.01", verify the source code actually uses those values.
- **Metric direction presence and correctness**: the **Training And Evaluation** section must explicitly
  state each ranking metric's optimization direction (`higher-better` / `lower-better`). If a metric's
  direction is not recorded, determine it from `<project_root>` source code (e.g. accuracy→higher-better,
  loss→lower-better) and record it. Verify the recorded direction is **correct** (not just present).
- **Cross-document consistency**: `supernet_summary.md` and `project_manifest.md` must not contradict
  each other on model type, data pipeline, or evaluation paradigm.

If a claim references an artifact that does not yet exist, skip that claim.

## Output

Return:

1. **Status**: `all-pass` (no changes needed) or `fixed` (one or more corrections were applied).
2. **Changes** (only when status is `fixed`): one block per correction.

## Constraints

- **Modification scope**: only edit `supernet_summary.md` and `project_manifest.md` under `<output_dir>`.

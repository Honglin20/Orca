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

> Path existence, manifest section completeness, artifact list existence, and source_project_root
> absoluteness are handled deterministically by `check_flatten.sh` / `check_expand.sh` — out of scope
> here. This verifier focuses **only on semantic accuracy** that requires LLM judgment.

## Inputs

The caller will provide:

1. **`<output_dir>`**: the directory containing the pipeline memory documents and generated artifacts.
2. **`<project_root>`**: the absolute path to the root directory of the original user project.

## Semantic Verification (LLM-mediated only)

Read each document section by section. Verify **semantic claims** that require understanding, not
mere path existence:

- **NAS decisions consistency** (model type, choice-only search space, weight inheritance, KD training
  paradigm, retrain strategy): verify against the actual generated code. For example:
  - `model_type` must be consistent with the slot structure in `supernet.py` (transformer layer slots);
  - the search space must be **choice-only**: `branch_choices` is the sole searchable dimension, every
    other dimension appears as a pinned scalar equal to the original model's measured value;
  - **weight inheritance** must be recorded and real: the summary's pretrained-weight source must point
    at the workflow's pretrained checkpoint (loaded via `load_pretrained.py`), and the supernet's
    `original` branches + non-slot fixed modules must load from it (frozen), with variant branches
    trainable — a summary describing random/from-scratch initialization contradicts the pipeline;
  - **KD training paradigm** must be recorded consistently wherever training is mentioned: teacher =
    independent frozen instance of the pretrained original model, loss = hidden-state cosine +
    logits KL, only variant-branch parameters trainable, one sampled choice path per step;
  - **retrain strategy** must be `finetune-from-supernet` (never from-scratch);
  - evaluation paradigm of the search stage is validate-only.
- **Descriptive claims about the original project** (model structure, training paradigm, data pipeline,
  optimizer/scheduler specifics, loss formula): verify against `<project_root>` source code that the
  descriptions are **semantically accurate**, not just structurally present. For example, if manifest
  says "optimizer is AdamW with weight_decay=0.01", verify the source code actually uses those values.
- **Pretrained-checkpoint facts** (Model section): the recorded checkpoint path must match the workflow's
  `pretrained_ckpt` input, and the recorded state_dict layout / loading entry must match what
  `load_pretrained.py` actually does.
- **Metric direction presence and correctness**: the **Training And Evaluation** section must explicitly
  state each ranking metric's optimization direction (`higher-better` / `lower-better`). If a metric's
  direction is not recorded, determine it from `<project_root>` source code (e.g. accuracy→higher-better,
  loss→lower-better) and record it. Verify the recorded direction is **correct** (not just present).
- **Cross-document consistency**: `supernet_summary.md` and `project_manifest.md` must not contradict
  each other on model type, the pretrained-weight source / teacher construction, the data pipeline, or
  the evaluation paradigm.

If a claim references an artifact that does not yet exist, skip that claim.

## Output

Return:

1. **Status**: `all-pass` (no changes needed) or `fixed` (one or more corrections were applied).
2. **Changes** (only when status is `fixed`): one block per correction.

## Constraints

- **Modification scope**: only edit `supernet_summary.md` and `project_manifest.md` under `<output_dir>`.

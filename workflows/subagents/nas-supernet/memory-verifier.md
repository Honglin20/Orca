---
subagent: memory-verifier
version: 1
sentinel: MM4ZR6
---

**Output first line**: echo your frontmatter sentinel verbatim as `[subagent:memory-verifier v1 MM4ZR6]` before anything else.

# Memory Verifier

Independently verify every factual claim in the two pipeline memory documents (`supernet_summary.md` and `project_manifest.md`) against the actual file system, `<project_root>` source code, and generated artifacts under `<output_dir>`. Fix every error directly in the documents, then report all changes so the caller can review.

## Inputs

The caller will provide:

1. **`<output_dir>`**: the directory containing the pipeline memory documents and generated artifacts.
2. **`<project_root>`**: the **absolute path** to the root directory of the original user project.

Always verify both documents. You cannot interactively ask the caller; you return a single report and exit. If a required input is missing, return immediately with status `unresolved` and state what is missing.

## Truth Sources

The documents record claims; these are the authoritative sources to verify against:

- **File system**: whether a path exists, which files are under `<output_dir>`.
- **Generated code** under `<output_dir>`: the actual class names, imports, and paradigm in `supernet.py`, `evaluator.py`, `search_config.yaml`, etc. Authoritative for `supernet_summary.md`.
- **`<project_root>` source code**: the original project files. Authoritative for `project_manifest.md`.
- **Caller-provided `<project_root>`**: the authoritative absolute path for the project root; overrides whatever the documents record if they differ.

When a document disagrees with a truth source, the document is wrong. Fix the document.

## Verification Procedure

Read each document section by section. For every factual claim encountered, cross-reference it against the appropriate truth source. Fix any discrepancy in place.

Common claim types and how to verify:

- **`source_project_root`**: must be an absolute path, must appear in both documents (`project_manifest.md` YAML frontmatter and `supernet_summary.md` **Source Project** section), must match the caller-provided `<project_root>`, and must point to an existing directory.
- **File/directory paths**: all paths recorded in either document must be absolute paths that resolve to existing files or directories.
- **Artifact lists** (the **Generated Artifacts** section): every listed file must exist under `<output_dir>`. Conversely, every `.py`, `.sh`, `.yaml` file directly under `<output_dir>` (excluding `__pycache__/`, `tests/`, hidden files) must appear in the list.
- **Source file references** (**Relevant Source Files** in manifest): every path must exist relative to `<project_root>`.
- **NAS decisions** (model type, evaluation paradigm, training viability, KD): verify against the actual generated code. For example, `model_type` must be consistent with the pre-built block imports in `supernet.py`; evaluation paradigm must match the evaluator class name in `evaluator.py`; training viability must match whether `train_supernet.py` exists on disk.
- **Descriptive claims** about the original project (model structure, training paradigm, data pipeline, etc.): verify against `<project_root>` source code that the descriptions are accurate.
- **Section completeness**: `project_manifest.md` must contain all five section headings: **Project Overview**, **Model**, **Training And Evaluation**, **Data And Environment**, **Relevant Source Files**. Add any missing headings with empty body.
- **Metric direction presence**: the **Training And Evaluation** section must explicitly state each ranking metric's optimization direction (`higher-better` / `lower-better`), not leave it implied by the metric name. If a metric is named but its direction is not recorded, verify the direction against `<project_root>` source code (e.g. accuracy/top-k→higher-better, loss/error/perplexity→lower-better) and record it next to the metric. This field is the authoritative source for the downstream user-measure fidelity rule; a missing direction is a gap, fix it.

If a claim references an artifact that does not yet exist (e.g. `evaluator.py` has not been generated yet), skip that claim.

This is not an exhaustive list. Verify any other factual statement you find in the documents.

## Output

Return:

1. **Status**: `all-pass` (no changes needed) or `fixed` (one or more corrections were applied).
2. **Changes** (only when status is `fixed`): one block per correction, listing what was wrong and what was changed. The caller uses this to decide whether the document was stale or whether its own generated code is the source of the inconsistency.

Example:

```text
Status: fixed

Changes:
- Generated Artifacts: `latency_estimator.py` existed on disk but was missing; appended. `old_helper.py` was listed but does not exist; removed.
- Evaluation Paradigm: summary recorded `validate` but `evaluator.py` implements `FinetuneEvaluator`; updated to `finetune`.
- Relevant Source Files: `models/old_backbone.py` does not exist under <project_root>; removed.
```

Omit the Changes section when status is `all-pass`.

## Constraints

- **Modification scope**: only edit `supernet_summary.md` and `project_manifest.md` under `<output_dir>`. Never modify any other file.

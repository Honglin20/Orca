---
name: pytorch-model-optimizer
description: Pre-optimize a project PyTorch model into validated standalone artifacts, and generate and refine a NAS supernet from it.
---

# PyTorch Model Optimizer

Use this skill to take a PyTorch model from a local project, flatten the model and its local runtime dependencies into a validated standalone artifact, apply user-approved model-level optimizations as a pre-processing step, and use the resulting validated model as the source for NAS `supernet.py` generation and `SearchSpace` refinement.

Skill resource paths:

- `<skill_dir>`: The directory containing this `SKILL.md` file. All `references/` and `assets/` paths in this skill are relative to `<skill_dir>`.
- Do NOT read files under `<skill_dir>/references/workflow-checklists/`. Those are exclusively consumed by the `workflow-verifier` subagent.

## Lazy Loading

Do **not** read all reference files, workflow documents, or asset files upfront. Only read the materials that a specific step requires **when you begin that step**. This keeps the context focused and avoids loading irrelevant content that may never be needed.

## Working Directory and Path Conventions

- `<output_dir>`: If the user provides a save folder for NAS artifacts, use it. Otherwise default to `llm_artifacts/` under the current working directory. All generated artifacts are written under `<output_dir>`. **Run `cd <output_dir>` once before executing any commands in this skill**; the working directory persists across subsequent commands, so all relative paths will resolve correctly. Sibling modules (e.g. `supernet.py`) are importable as plain imports without `sys.path` or `PYTHONPATH` manipulation. File paths in the steps below are relative to `<output_dir>` unless otherwise noted.
- `<user_project_root>`: The root directory of the user-provided PyTorch project. Run the workflow based on the contents under this path.
- `<nas_agent_root>`: The root directory of the `nas-agent` project (the directory containing `nas_agent/` and `pyproject.toml`). The working directory is `<output_dir>`, not the project root, so resolve `<nas_agent_root>` once by probing the installed package:
  ```bash
  python -c "from pathlib import Path; import nas_agent; print(Path(nas_agent.__file__).resolve().parent.parent)"
  ```
  Treat the printed absolute path as the resolved value of `<nas_agent_root>` throughout this skill (e.g. `ruff check --fix --config <nas_agent_root>/nas_agent/internal_ruff.toml`).
- **Path handling**: Use `pathlib.Path` objects for path construction instead of string concatenation or `os.path.join`. Example:
  ```python
  from pathlib import Path

  output_dir = Path(args.output_dir)
  output_dir.mkdir(parents=True, exist_ok=True)
  checkpoint_path = output_dir / "supernet_latest.pth"
  ```

## Workflow

Follow these 7 steps in order. Before starting, use the todolist tool to create a checklist of all 7 steps.

### Step 1: Prepare a Validated Flat Model

1. **Collect task context:** Read the user request and only the project files under `<user_project_root>` needed to understand the workload and target model entry point: dataset assumptions, training or inference flow, loss and metrics, preprocessing, deployment constraints, bottlenecks, and optimization priorities. Mark unconfirmed details as `Unknown` or conservative inferences.
2. **Flatten local dependencies:** Starting from the target model entry point found from context, keep standard-library and third-party imports as imports. Inline only local project code required for the model to run, recursively resolving nested local imports and ordering definitions to avoid local import errors or `NameError`.
3. **Add a runnable test block:** Append `if __name__ == "__main__":`, instantiate the model with its actual constructor arguments from `<user_project_root>` (e.g., real `num_classes`, `in_channels`), create dummy input tensor(s) whose shapes match the user project's real input specification (e.g., actual resolution, sequence length, channel count — not arbitrary small sizes), run a forward pass, and print readable output shape information. Use `from nas_agent.train.distributed import resolve_device` to obtain the runtime device (it auto-detects CUDA, NPU, or CPU); do not hardcode device strings.
4. **Ensure device portability:** Before saving, review every `nn.Module` class in the flattened file to ensure the model can run on any device (CPU, CUDA, NPU) via `.to(device)`. Tensors stored as plain Python attributes (not via `register_buffer` or `nn.Parameter`) will not follow `.to(device)`, causing device mismatch errors at runtime. Convert them to `register_buffer` or `nn.Parameter` as appropriate. Also ensure tensors created dynamically in `forward()` are placed on the correct device.
5. **Infer `<base_name>` and save:** After task context and the flattened model are understood, infer `<base_name>` from the semantic model type, architecture, and project context when possible; otherwise use the primary model class name converted to snake case. Write `<base_name>_flat.py` and create `<output_dir>` if needed.
6. **Review and validate:** Before running, re-read the flattened file and verify correctness: check that definitions are ordered correctly, constructor arguments and default values are consistent, `forward()` computation is logically correct, and no unintended errors were introduced during inlining or device-portability fixes. Then run `python <base_name>_flat.py`. Step 1 is complete only after validation succeeds.

### Step 2: Supernet Readiness and Optional Rules

1. **Load supernet readiness rules:**
   - List the filenames in `<skill_dir>/assets/optimize_rules/supernet_readiness/`.
   - Analyze the flat model from Step 1 to identify the model's macro-architecture category (e.g., multi-stage CNN, hierarchical 2D vision Transformer).
   - If a readiness rule file matches the model category, read it. These rules are mandatory and will be applied in Step 3 regardless of the user's consent choice for optional rules.
     - **Note**: Do not blindly apply all rules in the file. Select only the rules that are applicable based on the user model.
   - If no readiness file matches the model category, no mandatory structural modifications are needed; proceed to load optional rulesets below.
2. **Load optional rulesets:**
   - Load `<skill_dir>/assets/optimize_rules/general_rules.md` as the base optional ruleset.
   - Identify the best-matching task subdirectory under `<skill_dir>/assets/optimize_rules/` (e.g., `cv/`, `nlp/`, `telecom/`) based on the workload, model architecture, and data modality gathered in Step 1.
   - List the filenames in the matched subdirectory and selectively read only the rule files whose names are relevant to the current model and task — do not load every file.
   - Load any user-provided rule Markdown files.
   - If a custom rule has the same name as a built-in rule, prefer the custom definition and keep its source.
   - If supernet readiness rules already cover a topic (e.g., normalization replacement), do not redundantly recommend the general version.
3. **Curate optional recommendations:**
   - Analyze `<output_dir>/<base_name>_flat.py` together with the project context gathered in Step 1.
   - Recommend only optional rules that fit the model structure and workload. Use each rule's pros and cons as applicability criteria, not display-only metadata.
   - Before adding each rule to the curated list, verify that there is concrete evidence in the flat model or task context that the rule applies. If applicability is uncertain, do not recommend the rule.
4. **Present rules and get consent:**

   If supernet readiness rules were identified, list them as mandatory (will be applied automatically). Then present the A/B consent:

   > How would you like to proceed?
   > - A) Review the AI-curated recommended optimization rules.
   > - B) Skip optional optimization and proceed with only the mandatory readiness rules.

   If no optional rules are recommended, inform the user and proceed to Step 3 with readiness rules only. If neither readiness rules nor optional rules apply, skip Step 3 entirely, keep the flat file as the NAS input candidate and continue to Step 4.

   Use an interactive A/B choice if available; otherwise use plain text. Stop and wait for the user's reply.

   If the user selects A, present this warning followed by the curated rule list:

   > Risk Warning: Model Optimization
   > Both "Safe" and "Tradeoff" optimizations carry risks and may alter architecture, break compatibility, or degrade performance. You assume all risks and are solely responsible for validating the optimized model.

   Then present one plain-text enumerated list:
   - Start every item with exactly `<index>. [mandatory] <Rule Name>`, `<index>. [safe] <Rule Name>`, or `<index>. [tradeoff] <Rule Name>`. List mandatory readiness rules first.
   - Mark user-provided rules as `<Rule Name> [custom: <file_name>]`.
   - For each item, include the rule description, pros, cons, why it fits this model and task, and any expected changes to the public interface, default `__init__` arguments, or `forward` tensor shapes.
   - For tradeoff rules, also state the main compatibility or architecture risk.
   - Tell the user that the displayed list is already screened and the default is to apply all listed rules.
   - Ask for `all`, exact indices or rule names to exclude, or exact indices or rule names to keep.

   Stop and wait for the user's choice. If the user sends an empty reply, `skip`, or equivalent, all displayed rules are approved.

### Step 3: Apply Approved Rules

Run this step if any rules (readiness or optional) need to be applied. If no rules apply, skip to Step 4.

1. **Rewrite the flat model** using all approved rules (readiness + optional). Use each rule's instruction section as the implementation guide. Preserve the public interface, default `__init__` arguments, and `forward` tensor shapes by default. Change those contracts only when an approved rule requires it and the change was explicitly surfaced during Step 2 review.
2. **Save, review, and validate:** Write `<base_name>_llm-optimized.py`, preserve `<base_name>_flat.py`. Before running, re-read the optimized file and verify each applied rule was applied correctly without unintended side effects on surrounding logic. Then validate with `python <base_name>_llm-optimized.py`. The `__main__` test block must use `resolve_device` for device selection to support multiple types of devices (same as Step 1).

### Step 4: Classify Model for NAS

1. **Choose `<prepared_model>`:** Use `<base_name>_llm-optimized.py` if Step 3 produced and validated it; otherwise use `<base_name>_flat.py`.
2. **Load model type definitions:** Read `<skill_dir>/references/model_type.json` only when this step begins. Treat it as the source of truth for supported architecture labels and their definitions.
3. **Analyze the macro-architecture:** Inspect `<prepared_model>` directly and compare its structure against the JSON-defined labels.
   - Inspect both `__init__` and `forward()`.
   - Focus on parameterized `nn.Module` components and follow the main tensor flow through them. Non-parameterized control flow — such as iteration loops, convergence checks, or non-learnable linear operators — is not part of the model architecture.
   - After excluding non-parameterized components, classify based solely on the remaining parameterized body. If all learnable computation falls under a single label, assign that label even when the parameterized body occupies only a fraction of the overall forward pass.
   - Classify by the macro-level architecture of the parameterized body, such as stage transitions, spatial downsampling, token or sequence length behavior, and the main sequence of repeated blocks.
4. **Macro-level layer classification:** Classify by how the parameterized layers are stacked and the primary feature-mixing mechanism they use. Auxiliary operations inside a layer that belong to a different architecture family do not affect the classification.
   - For example, `nn.Conv2d` for QKV projections inside a transformer block is auxiliary and does not make the model a CNN.
   - Initial downsampling stems, patch embeddings, final upsampling heads, and final projection heads are boundary components and do not affect the classification.
   - Reject only when the macro-level layer stacking is a hybrid of two or more architecture families that does not fit any single supported model type.
5. **Output the classification as a Markdown list:** Use exactly these fields and keep the reason short.
   - `Model Type`: one label from `<skill_dir>/references/model_type.json`, or `No supported match`.
   - `Confidence`: `high`, `medium`, or `low`.
   - `Reason`: one concise sentence citing the macro-level structure or the reason no supported label fits.
6. **Stop unsupported NAS branches:** If `Model Type` is not one of the labels loaded from `<skill_dir>/references/model_type.json`, keep the validated model artifact and any report that was created, explain that this macro-architecture is unsupported or unclear for the current supernet workflow, and **stop here** — do not proceed to Step 5 or any subsequent steps.

### Step 5: Generate Supernet

Read `<skill_dir>/references/workflows/supernet_generation.md` before starting this step. Follow it to generate `<output_dir>/supernet.py` from `<prepared_model>` and `model_type`.

Use context accumulated from earlier steps — such as the task scenario (e.g., image classification, dense prediction, language modeling), input data characteristics (resolution, sequence length), and any user-stated preferences — to guide pre-built block selection within the workflow.

After the workflow completes (including validation and smoke tests), enter the evaluator verification loop:

1. **Invoke the `supernet-evaluator` subagent** with:
   - Path to `<prepared_model>` (the flattened or optimized model from Steps 1–3)
   - Path to `<output_dir>/supernet.py`
   - The `model_type` classification from Step 4

2. **If the evaluator returns issues:**
   - Read the feedback carefully. Each issue includes severity (`[BLOCKER]` / `[MAJOR]` / `[MINOR]`), symptom, reason, and fix guidance.
   - Apply targeted fixes to `supernet.py` based on the feedback. Prioritize `[BLOCKER]` > `[MAJOR]` > `[MINOR]`.
   - Re-run the workflow validation.
   - Re-invoke the `supernet-evaluator` subagent.

3. **Repeat** steps 1–2 until the evaluator returns PASS (`LGTM`). If PASS, proceed to Step 6.

### Step 6: Inspect and Refine `SearchSpace`

Read `<skill_dir>/references/workflows/search_space_refinement.md` before starting this step. Follow it to inspect the generated supernet, show each representative candidate block's parameters and latency on current host device, tune existing `SearchSpace` fields, validate each accepted round, and run the user feedback loop.

This phase primarily changes existing `SearchSpace` field values such as fixed dimensions, ranges, candidates, stage settings, layer configs, and branch choices; see the referenced workflow for the full refinement rules. After refinement:

1. **Invoke the `workflow-verifier` subagent** with:
   - **Workflow**: `<skill_dir>/references/workflows/search_space_refinement.md`
   - **Artifacts** (verifier may modify): `supernet.py`, `inspect_supernet.py`
2. **Handle the verifier response:**
   - If `all-pass` and the response has no **Fixed** section: proceed to Step 7.
   - If `all-pass` and the response includes a **Fixed** section: re-run the workflow validation (+ `python inspect_supernet.py`) before proceeding to Step 7.
   - If `unresolved`: read each unresolved item, apply the suggested fix in the artifacts, then re-run the workflow validation (+ `python inspect_supernet.py`) before proceeding to Step 7.

### Step 7: Write Initial Summary And Present Next Steps

1. **Write `supernet_summary.md`:** Generate `<output_dir>/supernet_summary.md` with the following sections:

   - **Source Project**:
      - `<user_project_root>` used as the original PyTorch project root.
      - Local source files from the original project that were inlined into `<base_name>_flat.py`.
      - Dummy input shapes used for validation.
      - Validation status for `<base_name>_flat.py`.
   - **Model Optimization** (include only if Step 3 was executed):
      - Task context that materially influenced optimization decisions.
      - Supernet readiness rules applied (mandatory), and optional rules recommended, approved, and applied, including built-in vs custom sources.
      - Validation status for `<base_name>_llm-optimized.py`.
   - **Model Type And Pre-built Blocks**:
      - `model_type` label from Step 4 (e.g. `cnn`, `isotropic_transformer`, `hierarchical_transformer`).
      - List of pre-built block names selected from the nas-agent block pool for the current supernet.
      - `jq` command for querying available pre-built blocks of this type: `jq '.{model_type}' <nas_agent_root>/nas_agent/blocks/metadata.json`.
   - **Task And Training Context**:
      - Task type, plus only the **non-obvious specifics** a downstream skill could otherwise miss: multi-output structure, non-standard model-call conventions, and required auxiliary inputs. Do not mirror full training semantics (loss math, data-pipeline internals, tensor shapes/dtype) here — `<user_project_root>` and `supernet.py` are authoritative and are re-read downstream; record only what is hard to discover from the code.
      - Key training code references from `<user_project_root>`: file paths for the training loop, data pipeline, optimizer/scheduler configuration, loss function, and metrics — enough for the downstream skill to know WHERE to look, not a full copy of the training code.
   - **Generated Artifacts**: list all files generated under `<output_dir>` during Steps 1–6.

2. **Present next steps to the user (conversational only, do not write into `supernet_summary.md`):** Briefly recap what was generated (supernet, search space) and inform the user that the next step is to generate supernet training scripts in a new session using the `supernet-train-script` skill.

## Validation

- A step that creates or updates a model artifact is complete only when its required validation succeeds.
- For standalone model artifacts such as `<base_name>_flat.py`, `<base_name>_llm-optimized.py`, and generated `supernet.py`, success means the command exits successfully and the artifact runs without import, shape, dtype, device, or runtime errors.
- If validation fails, fix the artifact and rerun the same validation before proceeding.

## Guidelines

- Preserve all generated artifacts unless the user explicitly asks for cleanup.
- Keep standalone model files free of `ModuleNotFoundError` for local project code.
- Prefer conservative recommendations when task, data, or deployment context is uncertain.
- Keep generated Python variable names, function names, classes, string literals, comments, and docstrings in English.

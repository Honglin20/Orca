# Supernet Generation Workflow

This workflow generates `<output_dir>/supernet.py` — a single executable file containing the NAS supernet (`SearchSpace`, `ArchConfig`, `SuperNet`, and all `Elastic*` modules) derived from the user's model.

**Inputs:**

- **`<prepared_model>`**: the flattened or optimized model file (e.g., `<base_name>_flat.py` or `<base_name>_llm-optimized.py`) that serves as the reference architecture for supernet construction.
- **`model_type`**: the classified architecture label (e.g., `cnn`, `isotropic_transformer`, `hierarchical_transformer`) that determines which model-type-specific spec and pre-built blocks to use.

## Procedure

### 1. Read Specifications and Load Block Metadata

Read `references/supernet_specs/general_specs.md` — it contains all supernet constraints, `Elastic*` API contracts, block selection rules (including metadata category keys and selection dimensions), and output requirements. It also directs you to the model-type-specific `{model_type}/spec.md` and `{model_type}/search_space.py`.

Read the primitive block source files to understand their API before using them:

- `nas_agent/blocks/primitive_blocks.py` — Stores all standard elastic primitives (linear, normalization, convolution, embedding, and projection modules) across both CNN and Transformer domains. This file is large (~950 lines); only read the classes you need.

- `nas_agent/blocks/choice_layer.py` — `ChoiceLayer`

Load block metadata for the current model type:

```bash
jq '.{model_type}' <nas_agent_root>/nas_agent/blocks/metadata.json
```

Each entry has a `name`, `description`, and `search_space_fields`. Use the `description` to evaluate candidate suitability (combined with task context from the conversation), and `search_space_fields` to understand what dimensions each block exposes. The block's source code lives at `nas_agent/blocks/{name}.py` and is imported as `from nas_agent.blocks.{name} import ...`.

Refer to the "Elastic* Layer-Level Blocks" section in `general_specs.md` for the full block selection procedure and constraints.

### 2. Analyze User Model and Generate `supernet.py`

Analyze `<prepared_model>` and determine the supernet boundary per the "Full-Model Scope & Component Boundary" rules in `general_specs.md`. Use this boundary to guide code generation.

Then follow `general_specs.md` to:

1. Build a user-model-derived `Elastic*` block and its `is_valid_*_block` validator.
2. Shortlist pre-built block candidates from the loaded metadata: evaluate each candidate's `description` against the task context and apply the selection constraints in `general_specs.md`.
3. For each shortlisted candidate, read its source file to review the exported elastic class, validator, and constructor signatures. Drop any candidate that turns out to be incompatible after source review.
4. Generate `<output_dir>/supernet.py` as a complete, executable single file per the Output Content Requirements in `general_specs.md`.

### 3. Validate

Run validation commands from inside `<output_dir>`:

**Diagnostic check** (does not modify files — catches undefined names and missing imports):

```bash
ruff check --no-fix --config <nas_agent_root>/nas_agent/internal_ruff_check.toml supernet.py
```

If diagnostic errors are reported, fix the code and re-run the diagnostic check until it passes.

**Runtime validation**:

```bash
python supernet.py
```

If `python supernet.py` fails, inspect the error, fix the code, and re-validate from the diagnostic check.

After `python supernet.py` passes, run the following additional smoke tests to catch issues beyond the basic `__main__` demo:

1. **Multi-sample consistency**: sample 3–5 different `ArchConfig`s, run `set_sample_config` + `forward` + `get_active_subnet` + subnet `forward` on each, and verify supernet vs subnet output consistency for every sample.
2. **`elastic_num_params` sanity**: check that `elastic_num_params` returns different values for configs with different width/depth settings.
3. **Buffer registration / device portability**: registered buffers and parameters live in `model._buffers` / `model._parameters`, not in `vars(model)`, so any `torch.Tensor` found as a plain attribute is a device-portability bug. Iterate over every submodule (`model.named_modules()`) and assert no tensor remains in `vars(mod)`; any that does must be moved to `register_buffer` / `nn.Parameter` (or stored as a Python scalar).

These smoke tests can be run inline (e.g., via a temporary script or by appending to the `__main__` block) — they do not need to persist in the final `supernet.py`.

If any smoke test fails, inspect the failure, fix the code, re-run from the diagnostic check, and re-run the smoke tests.

**Format cleanup** (run once after all validation and smoke tests pass):

```bash
ruff check --fix --quiet --config <nas_agent_root>/nas_agent/internal_ruff.toml supernet.py
ruff format --quiet --config <nas_agent_root>/nas_agent/internal_ruff.toml supernet.py
```

Treat the format cleanup as silent final formatting only. Do not surface Ruff's format-only output, and do not use formatting-only output as a reason for additional manual edits.

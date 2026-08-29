# Search Space Refinement Workflow

Use this workflow after `<output_dir>/supernet.py` has been generated.

## Create `inspect_supernet.py`

Create `<output_dir>/inspect_supernet.py` beside `<output_dir>/supernet.py`. The first working inspector should be reused across refinement rounds because normal refinement edits are limited to `SearchSpace` ranges, candidates, and related controls. Update the inspector only if the initial version cannot summarize the generated objects; do not modify `supernet.py` structure to fit the inspector.

Use the reference example that matches the recorded `Model Type` from Step 4:

- `cnn`: `references/inspect_supernet_examples/cnn.py`
- `isotropic_transformer`: `references/inspect_supernet_examples/isotropic_transformer.py`
- `hierarchical_transformer`: `references/inspect_supernet_examples/hierarchical_transformer.py`

The inspector must:

- Import the generated `supernet.py` with a plain sibling import, eg: `from supernet import SearchSpace, SuperNet`.
- Instantiate `SearchSpace` and `SuperNet`.
- Summarize searchable fields, candidate `Elastic*` blocks, `is_valid_*_block()` constraints, capacity levers, and structural choices.
- Print the ordinary search-space levers first: depth candidates, stage names, fixed global dimensions, width candidates when present, and the layer configs field (`layer_configs` for isotropic models, `stage_layer_configs` for staged models). For staged models, print each stage's block-choice dict from the positionally aligned tuple entry.
- Exit with an error if it cannot import, instantiate, or summarize the generated supernet.

### Candidate Size Summary

The inspector must print representative candidate model-size information:

- Use each `Elastic*Block` object's `elastic_num_params` attribute directly when reporting parameter counts.
- Automatically inspect the generated code logic to identify the minimum-shape and maximum-shape representative block for each candidate block family. Do not assume that setting every architecture value to its maximum creates the largest block; follow the actual construction and validation logic.
- If the model has no stages, inspect the first searchable layer because layers are expected to share the same candidate structure. For an isotropic Transformer this is usually `layer0 = supernet.layers[0]`.
- If the model has stages, inspect the first searchable layer of each stage. The stage container is iterable: use `blocks = list(stage)` and `representative = blocks[0]`.
- For each inspected first layer, enumerate each candidate branch family and its raw config grid from the layer configs field. For staged models, use the positionally aligned `stage_layer_configs[stage_idx]` for the corresponding stage.
- For each candidate branch family, print the parameter range from the measured minimum-shape and maximum-shape candidate blocks, including the candidate name, sampled config, stage width or global width, candidate-block params, and first-layer params when available.

### Latency Summary

The inspector must also print latency on current host device for the same representative candidate blocks:

#### Phase 1: Discover ChoiceLayer Input Shapes (one-shot trace)

Before creating `inspect_supernet.py`, run a disposable inline script to discover the input shapes flowing into each `ChoiceLayer`. Use `nas_agent.latency.trace_choice_layer_inputs`:

1. Build a max-depth `ArchConfig` manually: select the maximum depth and use any single valid layer config (e.g. the first choice with any valid config values) for every active layer. The exact choice and config values do not matter; the trace only captures input shapes, not branch computation.
2. Copy the dummy input shape from `supernet.py` `__main__` (which already uses the user project's data dimensions).

Example trace script — execute inline from `<output_dir>`:

```python
import torch
from supernet import ArchConfig, SearchSpace, SuperNet
from nas_agent.latency import trace_choice_layer_inputs
from nas_agent.train.distributed import resolve_device

device = resolve_device("auto")
search_space = SearchSpace()
supernet = SuperNet(search_space)

# Build a max-depth ArchConfig using any single valid layer config.
# The ArchConfig construction is model-type-specific:
#   Isotropic:  ArchConfig(depth=max_depth, layers_config=tuple(...))
#   Staged:     ArchConfig(stage_depths=max_stage_depths, layer_configs={stage_name: tuple(...), ...})
# See inspect_supernet_examples/ for concrete construction patterns.
max_arch = ...  # TODO: construct per model type
supernet.set_sample_config(max_arch)
supernet.to(device)

dummy_input = ...  # TODO: copy shape from supernet.py __main__
traces = trace_choice_layer_inputs(supernet, dummy_input)
for name, args, kwargs in traces:
    shapes = [tuple(a.shape) for a in args if hasattr(a, "shape")]
    kw_shapes = {k: tuple(v.shape) for k, v in kwargs.items() if hasattr(v, "shape")}
    extra = f", kwargs={kw_shapes}" if kw_shapes else ""
    print(f"{name}: {shapes}{extra}")
```

Read the output. For staged models (CNN, hierarchical transformer), group entries by the `layers.{N}` prefix and take the first shape per stage. For isotropic models, all entries share the same shape; just use the first one.

#### Phase 2: Hardcode Shapes in `inspect_supernet.py`

Hardcode the discovered shapes as fixed dummy tensors:

- Staged models: `stage_choice_inputs = [torch.randn(...).to(device), ...]` indexed by stage.
- Isotropic models: `choice_input = torch.randn(...).to(device)`.
- If refinement changes width/dim candidates that affect intermediate shapes, re-run Phase 1 and update.

Measure latency with `nas_agent.latency.measure_module_latency`. Move the active subnet to `device` before measuring. Print latency alongside params for each candidate config. See the reference examples for the full pattern.


## Refinement Execution

First run one or a few autonomous refinement rounds before asking for user feedback:

- Run `python inspect_supernet.py` (from inside `<output_dir>`).
- Use the inspector summary, including per-block parameter and latency output, original user requirements, source model evidence, and the **value preference guidelines** from the matching `references/supernet_specs/{model_type}/spec.md` to decide whether the generated `SearchSpace` needs refinement (e.g. adjusting candidate ranges, enforcing value alignment preferences, reducing obviously inefficient block/config ranges, or correcting structural issues).
- Stop autonomous refinement once there is no clear improvement to make.

Then enter the user feedback loop:

- After each validated round, rerun `python inspect_supernet.py` (from inside `<output_dir>`).
- Show the complete raw stdout from `inspect_supernet.py` to the user exactly as emitted, preferably in a long text block. Do not summarize, paraphrase, truncate, omit sections, or replace it with a manually shortened stage list.
- Ask for feedback on the search space, and wait for the user's response.
- If the user gives concrete feedback, refine again, validate, rerun the inspector, show the updated complete raw stdout, and **ask for feedback again**.
- **CRITICAL:** Do not automatically proceed to the next pipeline step after applying user feedback. You must remain in this refinement loop until the user explicitly approves the current state.
- If the user explicitly confirms the current search space is acceptable (e.g., says "looks good", "skip", or gives an empty reply), end the refinement workflow and leave the current validated `supernet.py` in place, then proceed to the next step.

## Refinement Rules

Apply these rules during both autonomous and user-feedback rounds:

- Revise only existing `SearchSpace` field values, such as fixed dimensions, candidate tuples, depth ranges, width ranges, stage settings, layer configs, branch choices, and similar search-space controls.
- Do not change `SearchSpace` methods such as `sample()` or `validate()`.
- Do not add or remove `SearchSpace` fields, imports (unless replacing blocks), `SuperNet`, or unrelated module structure.
- Keep all generated configurations valid under existing `is_valid_*_block()` rules. If a user request would produce invalid configurations, ignore the invalid part and still produce a valid `SearchSpace`.
- During autonomous refinement, prioritize the original user requirements, source model evidence, and NAS background Markdown.
- During user-feedback rounds, treat the latest user feedback as primary.
- Refine conservatively when feedback is broad or underspecified.

### Pre-built Block Replacement

Pre-built block replacement is a heavier operation than normal refinement. It is allowed **only** during user-feedback rounds and **only** when necessary to address the user's feedback.

Introducing a new block branch is effectively a localized re-run of supernet generation (Step 5), so the full Step 5 generation specifications apply to the replacement block — `references/supernet_specs/general_specs.md` (Elastic* API, per-block `super_*` / `candidate_kernel_sizes` derivation, max-capacity construction, `ChoiceLayer` branch interchangeability, validator import, etc.) and the model-type-specific `{model_type}/spec.md`. If those specs are not already in your context, read them before editing.

- The user-model-derived `Elastic*` block must always remain. Only pre-built blocks from the nas-agent block pool (`nas_agent/blocks/`) are eligible for replacement.
- A candidate block may be swapped for another block from the same `model_type` in `nas_agent/blocks/metadata.json`. Do not mix blocks from different model types.
- When replacing a block, you must update all of the following accordingly:
  - The import statement
  - `ChoiceLayer` branches
  - the layer configs field (`stage_layer_configs` or `layer_configs`)
  - `is_valid_*_block()` validators
  - Related `SuperNet` or stage construction logic
  - `inspect_supernet.py`
- After applying the replacement and passing the Validation checks below, re-invoke the `supernet-evaluator` subagent — same inputs and PASS/feedback loop as Step 5 (`<prepared_model>`, `supernet.py`, `model_type`) — to confirm the new block complies with the supernet spec, then resume the user-feedback loop. Value-only refinement rounds (candidate/range edits that introduce no new block) do not require the evaluator.

## Validation

Validate every accepted round. If any command fails, inspect the failure, repair the `SearchSpace` without expanding scope, and rerun the same checks.

**Diagnostic check** (does not modify files):

- `ruff check --no-fix --config <nas_agent_root>/nas_agent/internal_ruff_check.toml supernet.py`

If diagnostic errors are reported, fix the code and re-run the diagnostic check.

**Runtime validation**:

- `python supernet.py`

**Format cleanup** (run once after all checks pass):

- `ruff check --fix --quiet --config <nas_agent_root>/nas_agent/internal_ruff.toml supernet.py`
- `ruff format --quiet --config <nas_agent_root>/nas_agent/internal_ruff.toml supernet.py`

Treat the format cleanup as silent final formatting only. Do not surface Ruff's format-only output to the user, and do not use formatting-only output as a reason for additional manual edits. If the execution interface allows it, do not inspect successful format cleanup output.

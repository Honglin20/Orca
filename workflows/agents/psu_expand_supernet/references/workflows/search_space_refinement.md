# Search Space Refinement Workflow

Use this workflow after `<output_dir>/supernet.py` has been generated.

## Create `inspect_supernet.py`

Create `<output_dir>/inspect_supernet.py` beside `<output_dir>/supernet.py`. The first working inspector should be reused across refinement rounds because normal refinement edits are limited to the branch set and related controls. Update the inspector only if the initial version cannot summarize the generated objects; do not modify `supernet.py` structure to fit the inspector.

Use the reference example for the `transformer_layer` family:

- `references/inspect_supernet_examples/transformer_layer.py`

The inspector must:

- Import the generated `supernet.py` with a plain sibling import, eg: `from supernet import SearchSpace, SuperNet`.
- Instantiate `SearchSpace` and build the supernet via `build_supernet()`.
- Summarize the branch set, the pinned dimensions (depth, widths, head layout, sequence length), and the per-branch parameter counts.
- Print the pinned facts first: the fixed layer count, every pinned scalar on `SearchSpace` (with the branch set last as the only searchable dimension).
- Exit with an error if it cannot import, instantiate, or summarize the generated supernet.

### Candidate Size Summary

The inspector must print representative candidate model-size information:

- Use each branch module's `elastic_num_params` attribute directly when reporting parameter counts.
- Inspect the first searchable slot because all slots share the same candidate structure (`layer0 = supernet.layers[0]`).
- Enumerate each branch in the slot's `branches` and report its parameter count. Branches are fixed-shape — one measurement per branch, no per-branch config grid.
- Report first-layer params; optionally report the all-original path total (`set_sample_config(search_space.all_original())` then `elastic_num_params`) as the inherited-baseline anchor.

### Latency Summary

The inspector must also print latency on current host device for the same representative branch modules:

#### Phase 1: Discover ChoiceLayer Input Shapes (one-shot trace)

Before creating `inspect_supernet.py`, run a disposable inline script to discover the input shapes flowing into each `ChoiceLayer`. Use `nas_agent.latency.trace_choice_layer_inputs`:

1. Build an all-active `ArchConfig` via `search_space.all_original()` (every slot active and on `original` — the deterministic default; any valid config traces the same input shapes because the residual stream width is branch-invariant).
2. Copy the dummy input shape from `supernet.py` `__main__` (which already uses the user project's data dimensions).

Example trace script — execute inline from `<output_dir>`:

```python
import torch
from supernet import SearchSpace, build_supernet
from nas_agent.latency import trace_choice_layer_inputs
from nas_agent.train.distributed import resolve_device

device = resolve_device("auto")
search_space = SearchSpace()
supernet = build_supernet()

supernet.set_sample_config(search_space.all_original())
supernet.to(device)

dummy_input = ...  # TODO: copy shape from supernet.py __main__
traces = trace_choice_layer_inputs(supernet, dummy_input)
for name, args, kwargs in traces:
    shapes = [tuple(a.shape) for a in args if hasattr(a, "shape")]
    kw_shapes = {k: tuple(v.shape) for k, v in kwargs.items() if hasattr(v, "shape")}
    extra = f", kwargs={kw_shapes}" if kw_shapes else ""
    print(f"{name}: {shapes}{extra}")
```

Read the output. All slots share the same input shape; just use the first one.

#### Phase 2: Hardcode Shapes in `inspect_supernet.py`

Hardcode the discovered shape as a fixed dummy tensor:

- `choice_input = torch.randn(...).to(device)`
- If refinement changes anything that affects intermediate shapes, re-run Phase 1 and update.

Measure latency with `nas_agent.latency.measure_module_latency`. Move the measured module to `device` before measuring. Print latency alongside params for each branch. See the reference example for the full pattern.


## Refinement Execution

First run one or a few autonomous refinement rounds before asking for user feedback:

- Run `python inspect_supernet.py` (from inside `<output_dir>`).
- Use the inspector summary, including per-branch parameter and latency output, original user requirements, and source model evidence to decide whether the branch set needs refinement (e.g., dropping a variant branch whose parameter/latency profile is clearly unacceptable for the deployment target, or restoring one that was dropped). Pinned dimensions are never refined — they equal the original model's measured values by contract.
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

- Revise only the branch set and related controls: remove branches from (or restore branches to) `SearchSpace.branch_choices` and the matching `ChoiceLayer` branch dicts. The `original` branch must always remain.
- Do not change `SearchSpace` methods such as `sample()`, `validate()`, or `all_original()`.
- Do not change pinned dimensions — they are fixed to the original model's measured values and cross-checked against `.baseline.json` by the deterministic gate.
- Do not add new `SearchSpace` fields, imports (unless replacing a variant branch), `SuperNet`, or unrelated module structure.
- Keep every remaining branch constructible from the recorded slot facts. If a user request would need a fact that was never measured, ignore that part and report it.
- During autonomous refinement, prioritize the original user requirements and source model evidence.
- During user-feedback rounds, treat the latest user feedback as primary.
- Refine conservatively when feedback is broad or underspecified.

### Variant Branch Replacement

Variant branch replacement is a heavier operation than normal refinement. It is allowed **only** during user-feedback rounds and **only** when necessary to address the user's feedback.

Introducing a new variant branch is effectively a localized re-run of supernet generation, so the full generation specifications apply to the replacement — `references/supernet_specs/general_specs.md` (branch adapter contract, ChoiceLayer interchangeability, weight inheritance, freeze groups) and `references/supernet_specs/transformer_layer/spec.md`. If those specs are not already in your context, read them before editing.

- The `original` branch must always remain. Only variant branches from the variant snapshot (`assets/layer_variants/`) are eligible for replacement — a variant may be swapped for another factory from the same snapshot, or dropped entirely from the branch set.
- When replacing a variant branch, you must update all of the following accordingly:
  - The embedded variant implementation in `supernet.py`
  - `ChoiceLayer` branches and `SearchSpace.branch_choices`
  - Related `SuperNet` construction logic
  - `inspect_supernet.py`
- After applying the replacement and passing the Validation checks below, re-run the equivalence gate (`check_equivalence.py`) and re-invoke the `supernet-evaluator` subagent — same inputs and PASS/feedback loop as generation (`<prepared_model>`, `supernet.py`, `model_type`) — to confirm the new branch complies with the supernet spec, then resume the user-feedback loop. Branch-set-only refinement rounds (removal/restoration that introduce no new variant implementation) do not require the evaluator, but still re-run the equivalence gate if any branch module changed.

## Validation

Validate every accepted round. If any command fails, inspect the failure, repair the search space without expanding scope, and rerun the same checks.

**Diagnostic check** (does not modify files):

- `ruff check --no-fix --config <nas_agent_root>/nas_agent/internal_ruff_check.toml supernet.py`

If diagnostic errors are reported, fix the code and re-run the diagnostic check.

**Runtime validation**:

- `python supernet.py`
- `python inspect_supernet.py` when this round also changed the inspector or when the calling skill asks for an inspector re-run

**Format cleanup** (run once after all checks pass):

- `ruff check --fix --quiet --config <nas_agent_root>/nas_agent/internal_ruff.toml supernet.py`
- `ruff format --quiet --config <nas_agent_root>/nas_agent/internal_ruff.toml supernet.py`

Treat the format cleanup as silent final formatting only. Do not surface Ruff's format-only output, and do not use formatting-only output as a reason for additional manual edits. If the execution interface allows it, do not inspect successful format cleanup output.

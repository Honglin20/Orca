# Checklist: Search Space Refinement

Companion to: `workflows/search_space_refinement.md`

## How To Use

Each item below is a verifiable requirement extracted from the companion workflow. Verify items in order. For items marked `auto-fixable: yes`, fix the artifact directly. For items marked `auto-fixable: no`, report the issue for the caller.

## Items

### [CRITICAL] 1. Refinement Scope — Branch Set Only
**auto-fixable**: no
**Section**: Refinement Rules
**Check**: The `SearchSpace` must not contain workflow-forbidden structural edits. Methods like `sample()`, `validate()`, or `all_original()` must remain standard and unhacked. No arbitrary `SearchSpace` fields should be added/removed (unless explicitly required by a variant branch replacement). Pinned dimensions must not have been "refined" — they stay equal to the original model's measured values.
**Verify**: Since you run in an independent context and likely lack a pre-refinement baseline, inspect the current `SearchSpace` class purely on its final state. Look for suspicious method edits, added fields unrelated to the branch set, pinned scalars that drifted, or structural changes that alter how the class operates.
**Anti-pattern**: Editing pinned dimensions to "improve" the space; adding new fields to `SearchSpace` or modifying `sample()` / `validate()` logic.

### [CRITICAL] 2. Branch Set Integrity
**auto-fixable**: no
**Section**: Refinement Rules
**Check**: `branch_choices` contains `original` (first), has no duplicates, and matches the `branches` dict of every `ChoiceLayer` in `supernet.layers`. No branch appears in some slots but not others (the branch set is uniform across slots).
**Verify**: Read `SearchSpace.branch_choices` and each slot's `ChoiceLayer.branches` keys; compare as sets.

### [CRITICAL] 3. Inspector Exists And Imports Supernet
**auto-fixable**: no
**Section**: Create `inspect_supernet.py`
**Check**: `inspect_supernet.py` exists, imports the generated `supernet.py` with a plain sibling import (`from supernet import SearchSpace, ...`), and constructs `SearchSpace()` and builds the supernet (directly or via `build_supernet()`) in the inspector code.
**Verify**: Read `inspect_supernet.py` to confirm the sibling import and instantiation. Do not rigidly rely on exact string greps like `SuperNet(` as it may falsely reject valid multi-line or aliased instantiations.
**Anti-pattern**: importing supernet via `sys.path` manipulation or absolute package paths.

### [CRITICAL] 4. Inspector Summarizes Branch Set And Pinned Dimensions
**auto-fixable**: no
**Section**: Create `inspect_supernet.py`
**Check**: Inspector prints the branch set (`branch_choices`), the fixed layer count, the pinned scalar dimensions (residual width, head layout, FFN width, sequence length), and the per-slot branch structure. There are no candidate grids to print — dimensions are pinned scalars.
**Verify**: Read `inspect_supernet.py` and confirm it accesses `branch_choices` and the pinned `SearchSpace` attributes.

### [CRITICAL] 5. Candidate Size Summary
**auto-fixable**: no
**Section**: Candidate Size Summary
**Check**: Inspector uses each branch module's `elastic_num_params` attribute to report parameter counts. It inspects the first searchable slot (`supernet.layers[0]`), enumerates every branch in its `branches`, and reports one measurement per branch (branches are fixed-shape — no per-branch config grid, no min/max representative selection).
**Verify**: grep for `elastic_num_params` in `inspect_supernet.py`. Confirm the inspector enumerates `branch_choices` / `branches` and prints per-branch parameter counts (first-layer params, and the all-original path total when reported).
**Anti-pattern**: Computing parameter counts manually via `sum(p.numel() ...)` instead of using `elastic_num_params`; constructing min/max "extreme configs" per branch (there is no per-branch config); reporting only total model params without per-branch breakdown.

### [CRITICAL] 6. Latency Inputs Are Hardcoded In Inspector
**auto-fixable**: no
**Section**: Latency Summary / Phase 2
**Check**: `inspect_supernet.py` contains a fixed dummy tensor for the ChoiceLayer input (e.g., `choice_input`). All slots share the same input shape. The normal inspector must not call `trace_choice_layer_inputs`.
**Verify**: Read `inspect_supernet.py` and confirm the hardcoded input tensor variable and `torch.randn` exist. Do not rigidly fail if the variable name differs from `choice_input` as long as the semantic purpose is met. grep for `trace_choice_layer_inputs` should find 0 matches. If caller provided trace output as context, compare the hardcoded tensor shapes against that output; otherwise verify only the artifact structure.
**Anti-pattern**: Guessing an input shape without the trace; using the original model input tensor as the ChoiceLayer input tensor; tracing ChoiceLayer inputs every time `inspect_supernet.py` runs.

### [CRITICAL] 7. Latency Uses Official Helper On Branch Modules
**auto-fixable**: no
**Section**: Latency Summary / Phase 2
**Check**: Inspector measures latency with `nas_agent.latency.measure_module_latency` on the same branch modules used for parameter reporting. The measured module is moved to the selected `device` before measurement.
**Verify**: grep for `measure_module_latency` and `.to(device)` in `inspect_supernet.py`. Read the measurement helper and confirm it receives the branch module and the hardcoded ChoiceLayer input tensor, not the full supernet or original full-model dummy input.
**Anti-pattern**: Measuring latency with `time.time()`, CUDA events, or a hand-written benchmark; measuring the whole `SuperNet`; reporting latency for a different set of branches than the parameter summary.

### [CRITICAL] 8. Latency Printed Beside Branch Params
**auto-fixable**: yes
**Section**: Latency Summary / Refinement Execution
**Check**: Inspector output includes latency for each branch in the same per-branch output as parameter counts, using a clear field such as `latency_ms`.
**Verify**: Read `inspect_supernet.py` to confirm latency is emitted in the branch parameter summary string/print, not just as a separate aggregate at the end.
**Anti-pattern**: Printing only a single total latency; omitting latency for some branches; printing latency in a manual summary that is disconnected from the branch params.
**Fix**: Only when latency is already measured into a local variable (e.g., `latency_ms`) and the branch loop print merely omits it: append that value to the existing print/f-string (e.g., `, latency: {latency_ms:.2f}ms`). If measurement logic itself is missing, report under Unresolved — do not invent a measurement path.

### [CRITICAL] 9. Variant Branch Replacement Artifact Consistency
**auto-fixable**: no
**Section**: Variant Branch Replacement
**Check**: If any variant branch was replaced, verify artifact consistency: (a) the `original` branch still remains, (b) the replacement is a variant from the snapshot (`assets/layer_variants/`), (c) the embedded implementation, `ChoiceLayer` branches, `SearchSpace.branch_choices`, related `SuperNet` construction logic, and `inspect_supernet.py` were all updated, (d) the equivalence gate (`check_equivalence.py`) was re-run and the `supernet-evaluator` was re-invoked for the replacement.
**Verify**: If branch implementations changed, check all listed update points. Do not try to verify whether replacement happened during autonomous or user-feedback rounds unless caller provided run logs or conversation context.

### [MINOR] 10. Model Traversal Matches Flat Layer List
**auto-fixable**: no
**Section**: Create `inspect_supernet.py`
**Check**: The inspector traverses the supernet structure correctly: `supernet.layers` is a flat `nn.ModuleList` of `ChoiceLayer` slots (no stage containers).
**Verify**: Infer the structure directly from `supernet.py`, then confirm `inspect_supernet.py` uses the flat traversal pattern.

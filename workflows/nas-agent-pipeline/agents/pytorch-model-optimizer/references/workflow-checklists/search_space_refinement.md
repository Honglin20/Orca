# Checklist: Search Space Refinement

Companion to: `workflows/search_space_refinement.md`

## How To Use

Each item below is a verifiable requirement extracted from the companion workflow. Verify items in order. For items marked `auto-fixable: yes`, fix the artifact directly. For items marked `auto-fixable: no`, report the issue for the caller.

## Items

### [CRITICAL] 1. Refinement Scope — Fields Only
**auto-fixable**: no
**Section**: Refinement Rules
**Check**: The `SearchSpace` must not contain obvious workflow-forbidden structural edits. Methods like `sample()` or `validate()` must remain standard and unhacked. No arbitrary `SearchSpace` fields should be added/removed (unless explicitly required by a pre-built block replacement).
**Verify**: Since you run in an independent context and likely lack a pre-refinement baseline, inspect the current `SearchSpace` class purely on its final state. Look for suspicious method edits, added fields unrelated to ranges/candidates, or structural changes that alter how the class operates.
**Anti-pattern**: Adding new fields to `SearchSpace` or modifying `sample()` / `validate()` logic.

### [CRITICAL] 2. Configurations Valid Under Validators
**auto-fixable**: no
**Section**: Refinement Rules
**Check**: All generated configurations are valid under existing `is_valid_*_block()` rules. No configuration values were introduced that would fail validation.
**Verify**: Check that candidate tuples in `SearchSpace` only contain values accepted by the corresponding validators.

### [CRITICAL] 3. Inspector Exists And Imports Supernet
**auto-fixable**: no
**Section**: Create `inspect_supernet.py`
**Check**: `inspect_supernet.py` exists, imports the generated `supernet.py` with a plain sibling import (`from supernet import SearchSpace, SuperNet`), and constructs `SearchSpace()` and `SuperNet(...)` in the inspector code.
**Verify**: Read `inspect_supernet.py` to confirm `SearchSpace` and `SuperNet` are imported from the sibling module and instantiated. Do not rigidly rely on exact string greps like `SuperNet(` as it may falsely reject valid multi-line or aliased instantiations.
**Anti-pattern**: importing supernet via `sys.path` manipulation or absolute package paths.

### [CRITICAL] 4. Inspector Summarizes Searchable Fields
**auto-fixable**: yes
**Section**: Create `inspect_supernet.py`
**Check**: Inspector prints searchable fields, candidate `Elastic*` blocks, `is_valid_*_block()` constraints, capacity levers, and structural choices. Must print depth candidates, stage names and widths (when present), fixed global dimensions (when present), and `SearchSpace.layer_configs` (isotropic) or `SearchSpace.stage_layer_configs` (staged).
**Verify**: Read `inspect_supernet.py` and confirm it accesses these SearchSpace attributes.
**Fix**: If any required search-space attribute is omitted from the print summaries, directly inject a `print()` statement to output it.

### [CRITICAL] 5. Candidate Size Summary
**auto-fixable**: no
**Section**: Candidate Size Summary
**Check**: Inspector uses each `Elastic*Block` object's `elastic_num_params` attribute to report parameter counts. It identifies minimum-shape and maximum-shape representative blocks for each candidate family by following the generated construction and validation logic, not by assuming that setting every architecture value to max/min gives the largest/smallest valid block. For staged models, it inspects a representative searchable layer per stage; for non-staged models, it inspects the first searchable layer.
**Verify**: grep for `elastic_num_params` in `inspect_supernet.py`, then read the min/max representative selection logic. Confirm the inspector enumerates candidate branch families from `SearchSpace.layer_configs` or `SearchSpace.stage_layer_configs` (for staged models, iterate the tuple entries), constructs or selects representative valid configs, and reports both candidate-block params and first-layer params when available.
**Anti-pattern**: Computing parameter counts manually via `sum(p.numel() ...)` instead of using `elastic_num_params`; blindly using all-max values as the largest block without checking validator constraints; reporting only total model params without per-candidate family ranges.

### [CRITICAL] 6. Latency Inputs Are Hardcoded In Inspector
**auto-fixable**: no
**Section**: Latency Summary / Phase 2
**Check**: `inspect_supernet.py` contains fixed dummy tensors for ChoiceLayer inputs (e.g., using variables like `stage_choice_inputs` or `choice_input`). Staged models use a list or dict of tensors indexed by stage; isotropic models use a single tensor. The normal inspector must not call `trace_choice_layer_inputs`.
**Verify**: Read `inspect_supernet.py` and confirm the hardcoded input tensor variables and `torch.randn` exist. Do not rigidly fail if the variable names differ from `stage_choice_inputs` as long as the semantic purpose is met. grep for `trace_choice_layer_inputs` should find 0 matches. If caller provided trace output as context, compare the hardcoded tensor shapes against that output; otherwise verify only the artifact structure.
**Anti-pattern**: Guessing a single input shape for all stages; using the original model input tensor as the ChoiceLayer input tensor; tracing ChoiceLayer inputs every time `inspect_supernet.py` runs.

### [CRITICAL] 7. Latency Uses Official Helper On Candidate Blocks
**auto-fixable**: no
**Section**: Latency Summary / Phase 2
**Check**: Inspector measures latency with `nas_agent.latency.measure_module_latency` on the same representative candidate blocks/configs used for parameter reporting. The active candidate subnet is moved to the selected `device` before measurement.
**Verify**: grep for `measure_module_latency` and `.to(device)` in `inspect_supernet.py`. Read the measurement helper and confirm it receives the candidate subnet/module and the hardcoded ChoiceLayer input tensor, not the full supernet or original full-model dummy input.
**Anti-pattern**: Measuring latency with `time.time()`, CUDA events, or a hand-written benchmark; measuring the whole `SuperNet`; reporting latency for a different set of configs than the parameter summary.

### [CRITICAL] 8. Latency Printed Beside Candidate Params
**auto-fixable**: yes
**Section**: Latency Summary / Refinement Execution
**Check**: Inspector output includes latency for each representative candidate config in the same per-candidate output as parameter counts, using a clear field such as `latency_ms`.
**Verify**: Read `inspect_supernet.py` to confirm latency is emitted in the candidate block parameter summary string/print, not just as a separate aggregate at the end.
**Anti-pattern**: Printing only a single total latency; omitting latency for min/max representative configs; printing latency in a manual summary that is disconnected from the candidate params.
**Fix**: Modify the print statement or f-string inside the candidate evaluation loop to append the measured latency (e.g., `, latency: {latency_ms:.2f}ms`) next to the parameter counts.

### [MAJOR] 9. Pre-built Block Replacement Artifact Consistency
**auto-fixable**: no
**Section**: Pre-built Block Replacement
**Check**: If any pre-built block was replaced, verify artifact consistency: (a) the user-model-derived `Elastic*` block still remains, (b) replacement is from the same `model_type` in `nas_agent/blocks/metadata.json`, (c) import, `ChoiceLayer` branches, the layer configs field, validators, and `inspect_supernet.py` were all updated.
**Verify**: If block imports changed, check all listed update points. Do not try to verify whether replacement happened during autonomous or user-feedback rounds unless caller provided run logs or conversation context.

### [MINOR] 10. Model Type Reference Matches
**auto-fixable**: no
**Section**: Create `inspect_supernet.py`
**Check**: The inspector traverses the supernet structure correctly according to its architecture (e.g., staged loops for CNNs/Hierarchical, or flat layer lists for Isotropic).
**Verify**: Since you do not have the caller's Step 4 context, infer the model architecture directly from `supernet.py` (look for `stages` vs flat `layers`). Then confirm `inspect_supernet.py` uses the appropriate traversal pattern.

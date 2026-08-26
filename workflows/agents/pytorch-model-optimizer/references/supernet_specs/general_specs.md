# Universal Supernet Generation Specification

This document defines the core constraints and API contracts for generating a NAS supernet from a user PyTorch model.

Also read the following model-type-specific references before generating code:

* `{model_type}/spec.md`: model-type-specific supernet structure, stage/block conventions, and search space rules
* `{model_type}/search_space.py`: canonical search space example for field names and validation style

---

## Core Supernet Constraints

A valid Supernet must:

* Preserve **architecture connectivity** (same residuals, norms, convolution/attention/MLP order as the input model to supernet generation)
* Provide variation only through **elastic widths & depth** (channels, bottleneck dim, FFN dim, heads, layers)
* Support **slice + branch** at the **layer level**:

  * Each major layer chooses **one Block** (a branch) from a finite set.
  * **Blocks are parameter-isolated** from each other (no weight sharing across different Blocks).
  * Within the chosen Block, the active subnetwork is realized by **weight slicing** (no reinit; no graph changes).

---

## Search Space Definition

Define two `@dataclass` structures:

* `SearchSpace`: candidates + sampling logic
* `ArchConfig`: a single sampled architecture (record searchable variables only)

Besides necessary fields, they **must** include the following methods:

`SearchSpace`:

* `sample(self) -> ArchConfig`: sample an architecture config from the search space.
* `validate(self) -> bool`: validate that all combinations in the entire search space are valid. Return False if **any** combination is invalid.

`ArchConfig`:

* `validate(self) -> bool`: return True if itself is valid.

Tips: These two `validate()` methods can utilize `is_valid_*_block()` to validate the architecture config.

`ArchConfig`'s and `SearchSpace`'s layer config container shapes vary by model type:

* Isotropic models:
  * `ArchConfig.layers_config`: a flat tuple of per-layer config dicts indexed by layer position (sampled result).
  * `SearchSpace.layer_configs`: a dict mapping each block-choice name to its config candidate tuples (search definition).
* Staged models (CNN, hierarchical transformer):
  * `ArchConfig.layer_configs`: a dict mapping each stage name to a tuple of per-layer config dicts (sampled result).
  * `SearchSpace.stage_layer_configs`: a **tuple** aligned with `stage_names`; each entry is one stage's block-choice dict with per-block config candidate tuples (search definition), so dimension-related fields can scale independently with each stage's width/embedding dimension.

See each `{model_type}/spec.md` for the definitive field names, container shapes, and additional fields.

## General Search Space Construction Rules

### Full-Model Scope & Component Boundary

Build the search space around the **complete user model**, not just the backbone.

The `SuperNet` must include every component of the original model that participates in its `forward()` computation. Searchable layers reside in `self.layers` as `ChoiceLayer` instances, each wrapping multiple candidate `Elastic*` blocks; all other components (task heads, output projections, fixed operators, registered buffers, normalization layers) are included as fixed modules (standard `nn` layers, not `Elastic*` primitives) outside `self.layers`. The `SuperNet.forward()` must reproduce the full forward semantics of the original model, and `get_active_subnet()` must return a complete standalone model.

**Included in the supernet (as fixed, non-searchable components outside `self.layers`):**

* **Task heads**: classification heads, regression heads, detection heads, value heads (RL actor-critic), projection heads (self-supervised learning), and any other component that produces the model's final output. Even training-only heads (e.g., value head in RL) should be included so that `get_active_subnet()` returns a model usable for both training and inference without external wrappers.
* **Input stems and output projections**: patch embeddings, initial downsampling, channel-reduction convolutions, feature-space reshaping, spatial-to-channel projections.
* **Iterative / fixed-point forward logic**: when the model runs searchable layers inside a convergence loop (e.g., DEQ, unrolled ADMM, OAMP iterative refinement), include the iterative loop, convergence checks, and domain-specific linear operators in `SuperNet.forward()`. The searchable layers within the loop are `Elastic*` modules in `self.layers`; the loop structure and fixed operators are standard Python / `nn.Module` logic around them.
* **Non-neural transforms**: FFT/IFFT, wavelet transforms, activation-level transforms between neural layers. Preserve them at their original positions as fixed operations.
* **Pre-computed external operators**: measurement matrices, codebooks, basis functions accepted at construction time, and any tensor derived from them. Register them via `register_buffer` in the supernet.
* **Multi-input forward signature**: preserve all auxiliary inputs (conditioning signals, attention masks, external data) that the original model's `forward()` accepts.

**Not included in the supernet:** auxiliary networks that are separate `nn.Module` instances used only during training and never part of the same model graph (e.g., GAN discriminator, external teacher model, separate critic network in RL when actor and critic are independent modules).

### Topology Preservation

The search space must preserve the **topology and stage structure of the input model**:

* same macro connectivity
* same residual / shortcut pattern
* same normalization / activation ordering
* same stage transitions
* same spatial resolution transitions if they exist in the user model

### Searchable Dimensions

Searchable dimensions should be limited to the user model's natural elastic axes, such as:

* width / channels / embed dim / bottleneck dim / FFN dim
* heads or groups where applicable
* per-stage or global depth
* layer-level branch choice

Use **discrete value sets** for all searchable parameters. Use tuple instead of list.

## General Guidelines for Search Space Values (Weight Slicing)

* Dimension Alignment:
  * For sliced width variables, prefer multiples of 8 or 32 when applicable.
  * Non-slicing params (e.g. num_heads, kernel_size, choice labels) are exempt unless the block itself requires constraints.
* Complexity Cap:
  * Limit the total Cartesian product of all parameter combinations **within a single block** to under 1,000.
  * Search space boundaries should not deviate excessively from the user-provided baseline model.
* Validity by construction:
  * The default search space range for each block should consider its corresponding `is_valid_*_block(layer_config: dict[str, Any]) -> bool`.
  * Carefully design the range to ensure **ALL** combinations are already valid without relying on `validate()`.
  * Parameters within a block should match each other and match the scale of the user model.

---

## `Elastic*` Modules

Build hierarchically:

* Analyze the original model structure first
* Start build from primitive blocks
* Build a set of hierarchical `Elastic*` modules that implement **in-block weight slicing**
* Introduce layer-level branching only at the major-layer granularity of the original model
* Build top-level `class SuperNet(nn.Module)`

### `Elastic*` API

**Hard requirement:**

* Class name must have prefix `Elastic`.
* All `Elastic*` methods that take configuration MUST be **kw-only with explicitly named parameters**.
* Never accept a single config dict / kwargs blob and unpack it inside.
* In `Elastic*.__init__`, searchable maximum-capacity parameters MUST use the `super_*` prefix and appear first. Fixed structural/non-search parameters that do not have a `super_*` counterpart should appear after all `super_*` parameters.

Concretely, write signatures like:

* `__init__(self, *, super_in_dim: int, super_out_dim: int, super_num_heads: int, super_ffn_dim: int, dropout: float = 0.0, ...)`
* `set_sample_config(self, *, sample_in_dim: int, sample_out_dim: int, sample_num_heads: int, sample_ffn_dim: int)`

Required API:

* `__init__(self, *, <explicit super_* params>, ...)`
  * Construct the maximum-capacity elastic module using the provided `super_*` parameters.
  * Place all searchable `super_*` parameters before fixed non-search parameters in the signature.
  * Preserve the original module structure as much as possible.

* `set_sample_config(self, *, <explicit sample_* params>) -> None`
  * Name convention: use `sample_*` prefix for weight slicing parameters, and they should match the `super_*` names in `__init__`.
  * Example: `super_num_heads` -> `sample_num_heads`
  * Do not add `**kwargs`. Each block only receives parameters from its own config entry, never from sibling branches.

* `forward(self, *x, **kwargs)`
  * Keep the same input layout as the original module.
  * Keep the same forward logic, except using sliced weights.

* `get_active_subnet(self) -> nn.Module`
  * Export a standalone fixed-shape subnet matching the active sample config.
  * Recursively call `get_active_subnet()` for `Elastic*` children. Deep-copy (`copy.deepcopy`) non-elastic fixed children that carry parameters (e.g., `nn.LayerNorm`, `nn.BatchNorm2d`).
  * Prefer native equivalents (e.g. `nn.MultiheadAttention`) when possible; otherwise provide a fixed-shape mirror module (drop the `Elastic` prefix).

* Property `elastic_num_params`
  * Return the parameter count for the active subnet (recursively sum children where applicable).

### Primitive Blocks

Always prioritize and utilize the pre-built primitive elastic blocks defined in `nas_agent.blocks.primitive_blocks` to construct your supernet submodules. Avoid rewriting these layer-level operations unless absolutely necessary.

* Available primitive elastic blocks under `nas_agent.blocks.primitive_blocks`:
  `ElasticLinear`, `ElasticLayerNorm`, `ElasticConv2d`, `ElasticBatchNorm2d`, `ElasticLayerNorm2d`, `ElasticGroupNorm2d`, `ElasticConv1d`, `ElasticEmbedding`, `ElasticQKVProjector`, `ElasticMHSAQKVProjector`

#### `ElasticConv2d` kernel-size candidates

When a layer-level block exposes a searchable `kernel_size` and uses `ElasticConv2d` internally:

* The block constructor must accept `candidate_kernel_sizes` and pass it (together with `kernel_size=max(candidate_kernel_sizes)`) to every internal `ElasticConv2d` driven by `sample_kernel_size`. An `ElasticConv2d` can only slice kernel sizes it was constructed with.
* In `SuperNet.__init__`, each block derives `candidate_kernel_sizes` from its own layer-config entry, following the same per-block derivation rule as other `super_*` values: do not hardcode kernel tuples, and do not reuse one block's kernel candidates for another block.
* Convolutions with a fixed `kernel_size` do not need `candidate_kernel_sizes`.

## Elastic* Layer-Level Blocks

**User Model First**:

* You MUST first build a layer-level `Elastic*` block based on the user model. Its running logic should stay the same as the user model.
* Create a concrete `is_valid_*_block` function to validate this layer-level block config search space. For example:

```python
def is_valid_vit_block(layer_config: dict[str, Any]) -> bool:
    # No hard constraints are required for layer_config["num_heads"] and layer_config["ffn_dim"].
    # So always return True.
    return True
```

**Select pre-built blocks**:

The top-level keys in `nas_agent/blocks/metadata.json` match the model type labels from `references/model_type.json` directly (e.g., `cnn`, `isotropic_transformer`, `hierarchical_transformer`). Filter by the classified model type to get the candidate block list.

Each metadata entry contains:
* `name`: file name and import identifier; source lives at `nas_agent/blocks/{name}.py`, imported as `from nas_agent.blocks.{name} import ...`
* `description`: what the block does, its complexity profile, strengths, and limitations
* `search_space_fields`: the searchable dimensions this block exposes

Shortlist candidates from the filtered block list. Use each candidate's `description` to evaluate suitability, combined with the following dimensions from the task context from original project:

* **Workload Modality**: task scenario (e.g., CV dense prediction, NLP sequence modeling) and its natural bias toward block types
* **Input Data Profile**: input scale (batch size, sequence length, resolution), composition, and variability that affect block compatibility
* **Hard Compatibility Constraints**: masking patterns (causal vs bidirectional), spatial geometry assumptions (2D grid vs 1D sequence), divisibility requirements (window size, partition size), dynamic shape behavior
* **Efficiency or Capacity Priority**: whether the task prioritizes latency/memory efficiency or maximum model capacity
* **User Preferences**: any explicit preferences the user has stated regarding block families, search directions, or architecture biases

For each shortlisted candidate, read its source file to confirm compatibility: check the exported elastic class, `is_valid_*_block` validator, constructor signatures, and any assumptions about input layout, required auxiliary inputs, or constraints not apparent from the metadata. Drop any candidate that proves incompatible with the user model's I/O contract.

**Constraint**:

* Choose **at most 3 blocks** for layer-level branching, including:

  * 1 Elastic* block derived from the user model
  * at most 2 pre-built blocks

* If no compatible pre-built blocks remain after source review (e.g., the user model's I/O layout has no matching pre-built blocks), use only the user-derived block. Each layer position must still use a `ChoiceLayer` wrapper (with a single branch) to maintain a uniform API for `SuperNet.set_sample_config`, `get_active_subnet`, and downstream tools.

## Layer-Level Branching

Implement the major-layer branching module using `ChoiceLayer`. Read `nas_agent/blocks/choice_layer.py` to understand its API.

Use `from nas_agent.blocks.choice_layer import ChoiceLayer` to import it.

**Branching rules:**

* The `branches` dict maps `choice` names from each active layer config entry to concrete block modules.
* Block modules must not share parameters with each other.
* Each block module supports **slicing** internally based on its `Elastic*` submodules.
* One Elastic block should be based on the user model. Other blocks can be selected from the pre-built blocks shown in the metadata.

---

## `SuperNet` API

Note:

* The class name must be exactly `SuperNet`.

* It does not follow the kw-only parameter rule like `Elastic*`.

* `__init__(self, search_space: SearchSpace, **kwargs)`

  * The constructor arguments must follow the original model's complete constructor signature.
  * Include all model-construction arguments from the original model (e.g., `num_classes`, `action_dim`, iteration counts, buffer shapes), and provide kw-only default values when appropriate based on the user model.
  * **Per-block `super_*` derivation:** Each `Elastic*` block is constructed at maximum capacity so that smaller sub-networks can be sliced from it. For each block, choose the `super_*` values from that block's candidate range in the layer configs of `SearchSpace` that yield the maximum-capacity block. Each block derives its own `super_*` independently; never aggregate across different block types. For staged models, derive per stage and block type since each stage may define different candidate ranges. Derive values dynamically rather than writing literal constants.
    * When block constructors require `candidate_kernel_sizes` (see §`ElasticConv2d` kernel-size candidates), derive them following the same per-block (and per-stage) rule.
  * **Device-portable buffers:** never store a `torch.Tensor` as a plain attribute (`self.x = <tensor>`); register it with `register_buffer` or make it an `nn.Parameter`. Plain tensor attributes are not moved by `.to(device)`. If a value is conceptually a scalar number, store a Python `float` instead.
  * **Must** store searchable major layers / blocks in this attribute: `self.layers = nn.ModuleList()`. Do not use other names.
  * **`Elastic*` blocks belong only inside `self.layers`**. Each searchable layer position uses a `ChoiceLayer` that wraps multiple candidate `Elastic*` blocks as branches (`self.layers` may contain `ChoiceLayer` instances directly, or stage containers that hold `ChoiceLayer` instances; model-type-specific specs may mandate one form over the other). All other components (input/output stems, fixed projections, task heads, iterative-loop logic, domain-specific operators, and other non-searchable structural components) must use standard `nn` modules (`nn.Conv2d`, `nn.LayerNorm`, `nn.Linear`, etc.), not `Elastic*` primitives. These fixed components are not part of the search space and do not require weight slicing.
  * Build enough layers / blocks / stages to cover the maximum active architecture allowed by `search_space`.
  * **Default to the maximum architecture:** at the end of `__init__`, set the active config so the supernet is runnable by default. Set **depth to its built maximum (every stage/global depth) so all layer positions stay active**. Use a valid default `choice` per position.
  * Keep non-layer components' names and structure close to the original model wherever possible.

* `set_sample_config(self, arch_config: ArchConfig) -> None`

  * Validate `arch_config`.
  * Respect all active-depth / active-stage decisions in `arch_config`.
  * Follow the model-type-specific `ArchConfig` layer config schema (defined in §Search Space Definition) when iterating active layer entries.
  * For each active major layer / block in `self.layers`:
    * The layer entry's `"config"` stores raw keys like `num_heads` / `ffn_dim` / `out_channels`, not `sample_*` keys.
    * Before calling the branch, remap every raw key to its `sample_*` form, then call the choice module's `set_sample_config(...)`.
    * Example (isotropic transformer):

      ```python
      layer_cfg = arch_config.layers_config[i]
      choice = layer_cfg["choice"]
      raw_config = layer_cfg["config"]
      sample_config = {f"sample_{k}": v for k, v in raw_config.items()}
      self.layers[i].set_sample_config(choice_name=choice, **sample_config)
      ```
    * Passing raw keyword names like `num_heads=` / `ffn_dim=` / `out_channels=` directly into `set_sample_config(...)` is incorrect.
    * The "no config dict / kwargs blob" rule applies to `Elastic*` method signatures and their internal implementation, not to this caller-side keyword remapping step in `SuperNet.set_sample_config`.

* `forward(self, <original-model inputs>)`

  * Run forward through the active subnetwork.
  * Use the same forward signature as the original model (same inputs, same output structure).
  * Execute only active layers / stages / blocks according to `arch_config` within `self.layers`; all non-searchable components (task heads, iterative loops, fixed operators, etc.) are not affected by `arch_config` and always participate in the forward pass.
  * Preserve `.detach()` boundaries, `torch.no_grad()` scoping, `self.training` conditional branches, iterative loops, convergence logic, and domain-specific operators exactly as in the original model.

* `get_active_subnet(self) -> nn.Module`

  * Recursively export a standalone subnet matching the current active config.
  * Must reuse weights via branch selection + slicing (no reinit; no graph changes).
  * All non-searchable fixed modules (stems, output projections, task heads, normalization layers, domain-specific operators, etc.) that carry parameters should be deep-copied (`copy.deepcopy`) into the exported subnet so it is fully independent from the supernet.
  * **Fully independent**: must not keep a reference to the `SuperNet`, any `ChoiceLayer`, or any `Elastic*` module (no `self._super = supernet` delegation). Contains only resolved active-branch subnets (from each child's own `get_active_subnet()`) and deep-copied fixed modules.
  * **Forward fidelity**: the subnet `forward()` must reproduce `SuperNet.forward()` for the active config — preserve `self.training` branches, gradient boundaries (`.detach()` / `torch.no_grad()`), and any iterative/convergence structure. Do not export only the inference path; the subnet must stay trainable.

* Property `elastic_num_params`

  * Active-subnet parameter count, computed recursively from child modules' `elastic_num_params`.
  * For all non-searchable fixed modules (stems, output projections, task heads, etc.), count parameters with standard `sum(p.numel() for p in module.parameters())`.
  * Tips: For `nn.Parameter`, use `p.numel()` to count the number of parameters.

---

## Import Rules

Except modules explicitly mentioned above (`ChoiceLayer` when applicable, primitive blocks, `ElasticLinear`, `ElasticLayerNorm`, and pre-built blocks from `nas_agent.blocks`), do not import modules speculatively. If you need any other module, create it yourself.

## Self-Check Before Finalizing

Verify all of the following are true:

* Every call from an active layer entry's `"config"` dict into an elastic block converts raw keys to `sample_*` keys first.
* No active layer passes raw kwargs like `num_heads=` / `ffn_dim=` / `out_channels=` directly to `set_sample_config(...)`. Use `sample_*` keys instead.
* No `Elastic*` block's `set_sample_config` signature contains `**kwargs`. Each parameter must be an explicit `sample_*` keyword argument that the block actually uses.
* Each block branch's `super_*` constructor parameters are chosen from that block's candidate range in the layer configs of `SearchSpace` to yield the maximum-capacity block. No `super_*` value is computed by aggregating across entries of different block types.
* If any branch config exposes `kernel_size`, each block receives `candidate_kernel_sizes` from its own layer-config entry — not hardcoded, not reused across blocks — and passes it to every user-derived `ElasticConv2d` controlled by `sample_kernel_size`.
* No `torch.Tensor` is stored as a plain attribute on `SuperNet` or any `Elastic*` module; all are `register_buffer` or `nn.Parameter`.
* The user-derived `is_valid_*_block` validates inter-parameter constraints of its block (e.g. `embed_dim % num_heads == 0`). `return True` is valid only when the block's searchable parameters have no inter-parameter constraints.
* The search space's layer config candidates are chosen so that every Cartesian-product combination is already valid for each block.
* `Elastic*` primitives and blocks are used only inside `self.layers` (within `ChoiceLayer` wrappers). All other components (stems, projections, task heads, iterative-loop logic, fixed operators) use standard `nn` modules (`nn.Conv2d`, `nn.LayerNorm`, `nn.Linear`, etc.).
* The generated supernet preserves the original model's complete macro structure, dataflow, and forward semantics.
* Gradient-flow patterns (`torch.no_grad()` scoping, `.detach()` boundaries, `self.training` conditional branches), iterative loops, and convergence logic are preserved exactly as in the original model.

## Output Content Requirements

The generated `supernet.py` must be a complete, executable single file that includes:

1. All dependencies correctly imported.
2. `SearchSpace` + `ArchConfig` dataclasses; all combinations defined in `SearchSpace` must be valid (manually verify using the implementation code of `is_valid_*_block`, avoid using `validate()`).
3. A `SuperNet` supporting `set_sample_config`, `forward`, `get_active_subnet`, and `elastic_num_params`.
4. All required `Elastic*` modules implementing the `Elastic*` API.
5. `is_valid_*_block` functions for every layer-level Elastic* block: define the user-derived validator locally, and import pre-built validators from their source files in `nas_agent.blocks`.
6. `if __name__ == "__main__":` demo that:

   * builds `SearchSpace()` + `SuperNet(search_space, ...)` — the constructor keyword arguments (e.g., `num_classes`, `in_channels`, `seq_len`) **must** use the actual values from the user project (`<user_project_root>`), not arbitrary test placeholders
   * creates dummy input tensor(s) whose shape matches the real input specification of the user project (e.g., actual image resolution, actual sequence length, actual number of input channels); do **not** use made-up sizes like `(1, 3, 32, 32)` when the project uses `(1, 3, 224, 224)`
   * calls `search_space.validate()` to validate the entire search space is valid
   * samples an `ArchConfig` and runs a forward pass on the dummy input in eval mode (to avoid stochasticity like dropout)
   * exports `get_active_subnet()` and runs a forward pass through the exported subnet in eval mode
   * compares the output of supernet and subnet and raises Exception if the difference is not within a small threshold

Use `from nas_agent.train.distributed import resolve_device` to obtain the runtime device (auto-detects CUDA, NPU, or CPU); do not hardcode device strings.

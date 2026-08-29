# Universal Supernet Generation Specification

This document defines the core constraints and API contracts for generating a choice-only NAS supernet from a user PyTorch model that has a pretrained checkpoint.

Also read the model-family reference before generating code:

* `transformer_layer/spec.md`: the transformer-layer-slot family rules (slot facts, branch set, pinned dimensions, weight inheritance, materialization key contract)
* `transformer_layer/search_space.py`: canonical search space example for field names and validation style

---

## Core Supernet Constraints

A valid Supernet must:

* Preserve **architecture connectivity** (same residuals, norms, attention/FFN order as the input model).
* Keep every architectural **dimension pinned to the original model's measured values** (depth, residual width, head layout, FFN width). No dimension is expanded, sliced, or searched.
* Provide variation only through **branch choice at the layer level**:
  * Each searchable layer position (slot) chooses **one branch** (a complete layer implementation) from a finite set.
  * Branches are **parameter-isolated** from each other (no weight sharing across branches).
  * Each branch is a **fixed-dimension `nn.Module`** — there is no weight slicing and no per-branch sampling config. Switching a slot's architecture = switching which branch runs.
* **Inherit the pretrained weights**: the `original` branch of each slot and every non-slot fixed module load their weights from the pretrained original model's `state_dict`; variant branches initialize randomly. With every slot on `original`, the supernet's forward must reproduce the pretrained original model's outputs tensor-by-tensor (the equivalence gate enforces this).

---

## Search Space Definition

Define two `@dataclass` structures:

* `SearchSpace`: the branch set + sampling logic
* `ArchConfig`: a single sampled architecture — the per-layer branch choices only

`SearchSpace` must include the following methods:

* `sample(self) -> ArchConfig`: sample an architecture — one branch choice per slot. The choice is the **only** sampled variable.
* `validate(self) -> bool`: validate the whole space (branch set integrity, pinned-dimension self-consistency). Return `False` if anything is invalid.
* `all_original(self) -> ArchConfig`: the default config — every slot chooses `original`.

`ArchConfig` must include:

* `validate(self) -> bool`: return `True` if the config is structurally valid (every choice is a known branch).
* `choices: tuple[str, ...]`: one branch name per slot, in slot order, length equal to the fixed layer count.

### SearchSpace Hard Constraints

Three deterministic consumers `exec` `supernet.py` and reflect over a zero-argument `SearchSpace()` instance (search-record schema generation, the expand gate, the supernet-latency sidecar). The constraints below are hard requirements, not style advice:

1. **Choice-only public containers**: the only public (`dir()`-visible, non-`_`-prefixed) `SearchSpace` attribute whose value is a non-empty `list`/`tuple` is the choice container `branch_choices`. Any other container-valued attribute must be `_`-prefixed private.
2. **Pinned dims are scalars** (or `_`-prefixed private) — never single-value tuples. The schema reflection reports every public non-empty flat list/tuple as a searchable dimension; a single-value tuple becomes a false dimension.
3. **Zero-argument construction, module-level zero side effects**: `SearchSpace()` must construct with no arguments and must not need the checkpoint. The checkpoint enters only through `SuperNet.__init__` / `build_supernet(pretrained_state=...)`. Executing the module must not run the demo block or perform any I/O.

## General Construction Rules

### Full-Model Scope & Component Boundary

Build the supernet around the **complete user model**, not just the backbone.

The `SuperNet` must include every component of the original model that participates in its `forward()` computation. Searchable layers reside in `self.layers` as `ChoiceLayer` instances, each wrapping the branch set; all other components (task heads, input stems and output projections, stage transitions, fixed operators, registered buffers, normalization layers outside the slots) are included as **fixed modules** (standard `nn` layers) outside `self.layers`. The `SuperNet.forward()` must reproduce the full forward semantics of the original model, and `get_active_subnet()` must return a complete standalone model.

**Included in the supernet (as fixed, non-searchable components outside `self.layers`):**

* **Task heads**: classification heads, regression heads, detection heads, value heads (RL actor-critic), projection heads (self-supervised learning), and any other component that produces the model's final output. Even training-only heads (e.g., value head in RL) should be included so that `get_active_subnet()` returns a model usable for both training and inference without external wrappers.
* **Input stems and output projections**: patch embeddings, initial downsampling, channel-reduction convolutions, feature-space reshaping, spatial-to-channel projections.
* **Iterative / fixed-point forward logic**: when the model runs searchable layers inside a convergence loop (e.g., DEQ, unrolled ADMM, OAMP iterative refinement), include the iterative loop, convergence checks, and domain-specific linear operators in `SuperNet.forward()`. The searchable layers within the loop are slot modules in `self.layers`; the loop structure and fixed operators are standard Python / `nn.Module` logic around them. See §Non-Searchable Model Logic for how this logic must be preserved between `SuperNet` and the exported subnet.
* **Non-neural transforms**: FFT/IFFT, wavelet transforms, activation-level transforms between neural layers. Preserve them at their original positions as fixed operations.
* **Pre-computed external operators**: measurement matrices, codebooks, basis functions accepted at construction time, and any tensor derived from them. Register them via `register_buffer` in the supernet.
* **Multi-input forward signature**: preserve all auxiliary inputs (conditioning signals, attention masks, external data) that the original model's `forward()` accepts.

**Not included in the supernet:** auxiliary networks that are separate `nn.Module` instances used only during training and never part of the same model graph (e.g., GAN discriminator, a separate critic network in RL when actor and critic are independent modules). The distillation teacher is likewise not a supernet component: it is an independent frozen instance of the pretrained original model, constructed downstream by the training pipeline from the same checkpoint (`load_pretrained.py`) — never extracted from the supernet.

### Non-Searchable Model Logic

Non-searchable model logic is everything in `<prepared_model>` that is unrelated to the branch choice: it defines *how the model operates* rather than *what architecture it uses*. Typical categories: iterative / fixed-point loops and their convergence checks, `self.training` conditional branches, gradient boundaries (`detach()` / `torch.no_grad()` scopes), runtime weight manipulation (rescaling safeguards, weight tying, normalization hooks), solution or state initialization, and helper methods these depend on.

Both `SuperNet` and the exported subnet class (the fixed model class whose instance `get_active_subnet()` returns) must keep all of this logic and behave the same as `<prepared_model>`. Build to satisfy the following requirements, comparing against `<prepared_model>` as you write each method:

- **Method completeness**: keep everything `SuperNet`'s API methods and its non-searchable model logic actually use, plus anything downstream code genuinely depends on (for example, a training or evaluation script), checked by tracing calls from those methods, especially `forward()`, and from `__init__`, under any mode or flag combination. Actively check for methods and branches that serve none of these purposes, and drop them for code cleanliness instead of keeping them for parity with the original model. This covers a method nothing in the class ever calls, such as checkpoint save/load, logging, visualization, and other helpers unrelated to the model's computation. It also covers a branch that only prints a warning, does nothing, or raises an error instead of doing real computation. Drop the argument that picks a branch like that too.
- **Semantic equivalence**: each preserved method must produce the same behavioral outcome as the original, covering formula, constants, signs, control-flow branches, and gradient-boundary placement (`detach()` / `torch.no_grad()` scopes). Mechanical adaptations that change how the method navigates the module tree without altering the computation are fine; never simplify, drop terms, or reorder operations in a way that alters the numerical outcome.
- **Attribute preservation**: `__init__` attributes that govern forward-time behavior (boolean switches, convergence thresholds, scaling factors, iteration limits) must be present in both `SuperNet` and the exported subnet class, with matching default values.
- **Structural reference consistency**: methods that introspect or modify model internals by attribute path or index (e.g., runtime weight rescaling, weight tying, spectral normalization hooks) must update their references to match the supernet's reorganized module tree. For example, if `<prepared_model>` rescales weights via `self.blocks[i][0].weight.data *= factor`, the same rescaling method in `SuperNet` must reach the equivalent parameter through `self.layers` / `ChoiceLayer` / the branch modules, and in the exported subnet class through its own fixed module tree.
- **`SuperNet` / subnet forward consistency**: `SuperNet.forward()` and the exported subnet class's `forward()` must share the same non-searchable control flow and both must match `<prepared_model>`. The NAS-specific differences between the two (choice resolution vs. fixed module iteration) are expected and not subject to this requirement.
- **Formula constraint preservation**: non-searchable logic can contain a constant or formula whose correctness depends on the specific branch it was written for, not on every possible branch. When writing `SuperNet`'s non-searchable logic, check whether each such formula still holds for every candidate branch at that layer position, and fix it where it does not. For example, a safeguard that rescales weights to keep a component's Lipschitz constant below 1 so a fixed-point iteration elsewhere in the model converges.

### Topology Preservation

The supernet must preserve the **topology and stage structure of the input model**:

* same macro connectivity
* same residual / shortcut pattern
* same normalization / activation ordering outside the slot interiors
* same stage transitions
* same spatial resolution transitions if they exist in the user model
* same layer count — depth is pinned; no layer position is added or dropped

### Pinned Dimensions

Every architectural dimension is fixed to the original model's measured value, recorded in `.baseline.json` by the caller and mirrored as scalar fields on `SearchSpace`:

* `depth` = the original layer count
* per-family pinned facts (e.g., for the transformer layer family: `global_dim`, `head_dim`, `num_heads`, `ffn_dim`, `max_seq_len`, `activation`) = the original model's measured values

Never introduce candidate sets, ranges, or single-value tuples for these dimensions. The deterministic gate fails the node on any public container attribute other than `branch_choices` and on any pinned scalar that disagrees with `.baseline.json`.

---

## Branch Modules

Build hierarchically:

* Analyze the original model structure first.
* Wrap each branch (the `original` layer copy and each variant implementation) as one module exposing the branch adapter contract below.
* Introduce layer-level branching at the major-layer granularity of the original model.
* Build top-level `class SuperNet(nn.Module)`.

### Branch Adapter Contract

Each branch is a fixed-shape module with **no sampling API**. The ChoiceLayer-facing contract:

* `forward(...)` — delegates to the inner layer module; same external I/O as the original layer at that position (input/output widths, sequence behavior, mask argument handling).
* `get_active_subnet() -> nn.Module` — returns a **deep copy** of the inner module, fully standalone (no reference back to the supernet, the wrapper, or sibling branches). Independently copy any buffer or parameter registered directly on the module (`register_buffer`, `register_parameter`, or a bare `nn.Parameter` attribute).
* property `elastic_num_params` — the branch's own parameter count, `sum(p.numel() for p in self.parameters())`.

There is no `set_sample_config` on branches — there are no per-branch dimensions to sample. Switching a slot's architecture is only the choice of which branch runs.

### Branch Selection

The branch set is defined by the model-family spec (`transformer_layer/spec.md`) and its variant implementations live in the variant snapshot (`assets/layer_variants/`). The rules that apply universally:

* The **`original` branch is mandatory** in every slot: a deep copy of the original model's layer at that position, unchanged in module types, construction parameters, and forward computation. It carries the inherited parent weights and anchors the equivalence gate.
* Variant branches embed the implementation from the variant snapshot file — copy the needed factory and support classes into `supernet.py` (the generated file is standalone; never import across directories). Every construction parameter comes from the slot facts recorded on `SearchSpace`; never substitute a default or a guessed value.
* All branches at the same slot must share the same external I/O contract so any branch can occupy the slot.
* Branch modules must not share parameters with each other.

## Layer-Level Branching

Implement the slot module using `ChoiceLayer`. Read `nas_agent/blocks/choice_layer.py` to understand its API.

Use `from nas_agent.blocks.choice_layer import ChoiceLayer` to import it.

**Branching rules:**

* The `branches` dict maps branch names to the branch adapter modules; the names come from `SearchSpace.branch_choices` (`original` first).
* One `ChoiceLayer` per slot, collected in `self.layers = nn.ModuleList()` in the original layer order.
* A slot never has fewer than two branches (`original` plus at least one variant).

---

## `SuperNet` API

Note:

* The class name must be exactly `SuperNet`.

* `__init__(self, search_space: SearchSpace, pretrained_state: dict[str, torch.Tensor] | None = None, ...)`

  * The constructor signature must follow the original model's: include its model-construction arguments (e.g., `num_classes`, `action_dim`, iteration counts, buffer shapes), and provide kw-only default values when appropriate based on the user model.
  * **Weight inheritance**: when `pretrained_state` is given, non-slot fixed modules and every slot's `original` branch load their weights from it; variant branches initialize randomly. The key mapping must be complete — every key in `pretrained_state` consumed, every inherited parameter filled; raise a fail-loud error listing the unmatched keys. Never `strict=False` silent-partial-load.
  * **Freeze groups**: `original` branch parameters and non-slot fixed modules get `requires_grad_(False)`; variant branch parameters stay `requires_grad=True`. Downstream training tunes variant branches only — the inherited anchor must stay weight-exact.
  * **Device-portable buffers:** never store a `torch.Tensor` as a plain attribute (`self.x = <tensor>`); register it with `register_buffer` or make it an `nn.Parameter`. Plain tensor attributes are not moved by `.to(device)`. If a value is conceptually a scalar number, store a Python `float` instead.
  * **Must** store searchable slots in this attribute: `self.layers = nn.ModuleList()`. Do not use other names.
  * **Default config = all original**: at the end of `__init__`, set the active config to `search_space.all_original()` so the freshly built supernet is runnable and equivalence-checkable as-is.
  * Keep non-layer components' names and structure close to the original model wherever possible, and actively remove constructor arguments and `self.*` attributes the supernet no longer needs (see §Remove Constructor Arguments and Attributes the Supernet No Longer Needs).

* `set_sample_config(self, arch_config: ArchConfig) -> None`

  * Validate `arch_config`.
  * For each slot `i`, activate the chosen branch. Branches have no sampling API, so activation is the choice assignment itself, e.g. `self.layers[i].choice_name = arch_config.choices[i]`. An unknown branch name must fail loud (raise), never fall back to a default branch.

* `forward(self, <original-model inputs>)`

  * Run forward through the active branch of each slot.
  * Use the same forward signature as the original model (same inputs, same output structure), forwarding mask and auxiliary arguments exactly as the original model does.
  * All non-searchable components (task heads, iterative loops, fixed operators, etc.) are not affected by the config and always participate in the forward pass.
  * Preserve domain-specific operators exactly as in the original model, and follow §Non-Searchable Model Logic for all other non-searchable control flow.

* `get_active_subnet(self) -> nn.Module`

  * Export a standalone fixed model matching the current active config.
  * **Materialization key contract**: the exported model's module tree mirrors the original model's topology — fixed components at their original paths, each slot position holding the active branch's exported module (from that branch's `get_active_subnet()`), with **no `ChoiceLayer` wrapper and no branch-name level** in between. For the all-original config the exported subnet's `state_dict()` keys equal the pretrained original model's keys exactly.
  * Must reuse weights via branch selection (no reinit; no graph changes).
  * All non-searchable fixed modules that carry parameters, and every fixed buffer or parameter registered directly on `SuperNet` (`register_buffer`, `register_parameter`, or a bare `nn.Parameter` attribute, e.g., positional embeddings, `cls_token`, precomputed operators), must be deep-copied (`copy.deepcopy`) into the exported subnet so it is fully independent from the supernet.
  * **Fully independent**: must not keep a reference to the `SuperNet`, any `ChoiceLayer`, or any branch module (no `self._super = supernet` delegation). Contains only exported active-branch modules and deep-copied fixed modules, buffers, and parameters.
  * **Forward fidelity**: the subnet `forward()` must reproduce `SuperNet.forward()` for the active config, per §Non-Searchable Model Logic (e.g.: `self.training` branches, gradient boundaries, iterative/convergence logic). Do not export only the inference path; the subnet must stay trainable.

* Property `elastic_num_params`

  * Active-path parameter count: the sum over slots of the active branch's `elastic_num_params`, plus standard `sum(p.numel() for p in module.parameters())` for every non-searchable fixed module.
  * For a bare `nn.Parameter`, use `p.numel()` to count.

### Remove Constructor Arguments and Attributes the Supernet No Longer Needs

`SuperNet.__init__` is ported from the original constructor, but the supernet restructures the model, so some original constructor arguments and `self.*` attributes are no longer needed. Common cases:

* A constructor argument, or a `self.*` attribute, that used to fix an architectural property now recorded as a pinned fact on `SearchSpace` (e.g., a width/embedding-dim argument whose value `SearchSpace.global_dim` already defines); if `__init__` still needs that width to build something (e.g. the stem), read it directly from `search_space` instead of also keeping it as a separate constructor argument whose default could quietly end up different from `search_space`'s value.
* Regularization hyperparameters such as dropout and drop-path rates: the supernet does not need and should not apply these; remove them from the constructor signature instead of storing or forwarding them.

While writing `__init__`, actively judge each ported constructor argument and `self.*` attribute: does the supernet still need it? Remove those it does not; keeping parity with the original model is not a reason to keep them. If an unneeded one must stay (e.g., an external contract), add a one-line comment saying why.

---

## Import Rules

Except modules explicitly mentioned above (`ChoiceLayer` and `resolve_device`), do not import modules speculatively — including from the user project (forbidden) and from the variant snapshot directory (embed the code instead). If you need any other module, create it yourself in the file.

## Self-Check Before Finalizing

Verify all of the following are true:

* The only public list/tuple attribute on `SearchSpace` is `branch_choices`; every pinned dimension is a scalar (no single-value tuples).
* `SearchSpace()` constructs with zero arguments; executing the module has no side effects; no checkpoint is needed to construct.
* `SearchSpace.branch_choices` contains `original` (first), has no duplicates, and matches the `branches` of every `ChoiceLayer` in `self.layers`.
* Every pinned scalar on `SearchSpace` equals the corresponding measured value in `.baseline.json`.
* No branch module has a `set_sample_config` — activation of a slot is the choice assignment in `SuperNet.set_sample_config`, which raises on unknown branch names.
* With `pretrained_state` given, the weight mapping is complete and fail-loud on unmatched keys; with it absent, construction still works (random init) and freeze groups are still set.
* `original` branch parameters and non-slot fixed modules have `requires_grad=False`; variant branch parameters have `requires_grad=True`.
* `get_active_subnet()` at the all-original config produces a subnet whose `state_dict()` keys equal the original model's keys.
* No `torch.Tensor` is stored as a plain attribute on `SuperNet` or any branch module; all are `register_buffer` or `nn.Parameter`.
* Every fixed buffer or parameter reachable by `get_active_subnet()` is deep-copied into the exported subnet rather than referenced by alias.
* The generated supernet preserves the original model's complete macro structure, dataflow, and forward semantics, with the layer count unchanged.
* `SuperNet.__init__` keeps no constructor argument or attribute the supernet no longer needs; any intentionally kept exception carries a comment saying why.
* Every module with a direct counterpart in the original model, whether a branch in `self.layers` (the `original` branch) or a fixed component (stems, projections, task heads), matches the original's layer construction parameters (bias, eps, affine, normalized shape, etc.), not just its class name or shape.
* All requirements in §Non-Searchable Model Logic are satisfied: gradient-flow patterns (`torch.no_grad()` scoping, `.detach()` boundaries, `self.training` conditional branches), iterative loops, and convergence logic are preserved exactly as in `<prepared_model>`; every reachable method and forward-governing attribute exists in both `SuperNet` and the exported subnet class with matching semantics and defaults; structural references are updated to the reorganized module tree; and `SuperNet.forward()` / the exported subnet's `forward()` share the same non-searchable control flow.

## Output Content Requirements

The generated `supernet.py` must be a complete, executable single file that includes:

1. All dependencies correctly imported (stdlib / third-party only; the variant implementations embedded, not imported from the snapshot directory).
2. `SearchSpace` + `ArchConfig` dataclasses following §Search Space Definition and the family spec's canonical example.
3. A `SuperNet` supporting `set_sample_config`, `forward`, `get_active_subnet`, `elastic_num_params`, weight inheritance, and freeze groups.
4. All branch adapter modules following the Branch Adapter Contract (the `original` copy + the embedded variant implementations).
5. A module-level `build_supernet(pretrained_state: dict | None = None) -> SuperNet` helper that constructs `SearchSpace()` with the project-pinned values internally — the deterministic entry used by the equivalence gate and the latency sidecar.
6. `if __name__ == "__main__":` demo that:
   * builds `SearchSpace()` + `build_supernet()` — the construction values **must** use the actual values from the user project, not arbitrary test placeholders
   * creates dummy input tensor(s) whose shape matches the real input specification of the user project; do **not** use made-up sizes
   * calls `search_space.validate()` to validate the entire search space
   * samples an `ArchConfig`, runs a forward pass on the dummy input in eval mode, exports `get_active_subnet()`, runs a forward pass through the exported subnet, and compares the two outputs, raising an exception if the difference is not within a small threshold
   * when a sibling `load_pretrained.py` exists, additionally builds the pretrained original model via `build_pretrained_model()`, sets the supernet to `all_original()`, and compares the two models' outputs tensor-by-tensor on the probe inputs from `build_probe_inputs()` (including the mask case when the original forward accepts one), raising on mismatch

Use `from nas_agent.train.distributed import resolve_device` to obtain the runtime device (auto-detects CUDA, NPU, or CPU); do not hardcode device strings.

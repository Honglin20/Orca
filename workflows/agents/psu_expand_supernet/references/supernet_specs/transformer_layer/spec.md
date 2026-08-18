# Transformer Layer Slot Supernet Specification

This supplement extends `general_specs.md` for the only supported model family: a transformer layer stack where every repeated layer is a **transformer layer slot**. The supernet does not expand architectural dimensions — depth, widths, and head counts stay pinned to the original model's measured values. The only searchable dimension is, per slot, which **branch** (layer implementation) runs at that position.

## Supported Model

* The repeated layers mix tokens through an attention mechanism over a sequence (softmax attention or a variant), each layer with its own FFN, two normalizations, and two residual additions.
* Slot facts must be extractable from `<prepared_model>` and **uniform across the layer stack**:
  * `global_dim`: layer input/output width (the residual stream width)
  * `num_heads`, `head_dim`: the original attention's head layout (attention internals may be narrower than the residual stream — record both values as measured, do not derive one from the other)
  * `ffn_dim`: the original layer's FFN intermediate width
  * `max_seq_len`: the real input sequence length of this workload
  * `activation`: the original FFN activation name
* **Missing or non-uniform slot facts → unsupported, fail loud** (name the missing or non-uniform fact in the classification output). This check runs in Step 1, before any supernet code is generated: a variant branch built from a wrong or default-guessed fact (e.g. a fallback `max_seq_len`) silently under- or over-parameterizes the branch.
* Stage-structured transformers (stacks with token merging, pooling, or spatial downsampling between macro-stages) are supported **only as fixed topology**: stems, stage transitions, and heads are non-slot fixed modules kept exactly as in the original model (frozen); only the transformer layers themselves are slots.

## ChoiceLayer Per Slot

* `self.layers = nn.ModuleList()`, one `ChoiceLayer` per transformer layer slot, in the original layer order. The layer count is fixed at the original stack depth — no layer is ever dropped, added, or skipped.
* The branch set is uniform across all slots, in this canonical enumeration order:

  ```
  ("original", "vanilla", "random_synthesizer", "relu_attention", "fnet", "softs_star")
  ```

* **`original` branch (mandatory, first)**: a deep copy of the original model's layer at that position, unchanged — same module types, same construction parameters, same forward computation. It is the weight-inheritance carrier and the equivalence anchor.
* **Variant branches**: implementations live in `$ORCA_AGENT_RESOURCES/assets/layer_variants/transformer_layer_variants.py`. Read that file and embed the needed factory and support classes into `supernet.py` — the generated file is standalone and never imports across directories. Every variant construction parameter comes from the slot facts above; never substitute a default or a guessed value. `random_synthesizer` requires `max_seq_len` and raises without it — that failure is correct behavior; fix the slot fact instead of adding a fallback.
* **Branch I/O contract**: every branch takes `[B, L, global_dim]` and returns `[B, L, global_dim]` with `L` unchanged. Layer-internal attention may operate at `num_heads * head_dim`; the residual stream width is branch-invariant so any branch can occupy any slot.
* **Mask handling**: branches accept the parent layer's mask argument (`attn_mask` / `src_mask` / `attention_mask` / `mask` / `key_padding_mask` — first non-None wins). Mask-aware branches consume it; mask-blind branches accept and ignore it. `SuperNet.forward()` passes mask arguments through exactly as the original model does.
* **Parameter isolation**: branches never share parameters with each other.

## Branch Adapter Contract

Each branch is wrapped as one module exposing the ChoiceLayer-facing API. Branches have a fixed shape — there is no per-branch sampling config:

* `forward(...)` — delegates to the inner module.
* `get_active_subnet() -> nn.Module` — returns a **deep copy** of the inner module, fully standalone (no reference back to the supernet or the wrapper). For the `original` branch this is the copied original layer itself.
* property `elastic_num_params` — the branch's own parameter count, `sum(p.numel() for p in self.parameters())`.

## Pinned Dimensions (reverse gate)

* `depth`, `global_dim`, `head_dim`, `num_heads`, `ffn_dim`, `max_seq_len` are **fixed to the original model's measured values**. They are not search dimensions.
* Any pinned dimension appearing with more than one candidate value is a contract violation — the deterministic gate (`check_choice_contract.py` behind `check_expand.sh` check 5) fails the node on it. A single-value tuple is equally invalid: the schema reflection used downstream misreports it as a searchable list.

## SearchSpace Hard Constraints

Three deterministic consumers `exec` `supernet.py` and reflect over a zero-argument `SearchSpace()` instance (search-record schema generation, the expand gate, the supernet-latency sidecar). Violating any constraint below breaks them:

1. **Choice-only public containers**: the only public (`dir()`-visible, non-`_`-prefixed) `SearchSpace` attribute whose value is a non-empty `list`/`tuple` is the choice container `branch_choices`. Any other container-valued attribute must be `_`-prefixed private.
2. **Pinned dims are scalars** (or `_`-prefixed private) — never single-value tuples, which the reflection walk would report as a searchable dimension.
3. **Zero-argument construction, module-level zero side effects**: `SearchSpace()` must construct with no arguments and must not need the checkpoint; the checkpoint enters only through `SuperNet.__init__` / `build_supernet(pretrained_state=...)`. Executing the module must not run the demo block or perform any I/O.

`SearchSpace` must also expose:

* `sample() -> ArchConfig` — one branch choice per slot (the only sampled variable).
* `validate() -> bool` — whole-space validity: `original` present in `branch_choices`, no duplicate branch names, pinned dims self-consistent (e.g. divisibility the original attention requires).
* `all_original() -> ArchConfig` — the default config: every slot chooses `original`. This is the equivalence gate's running premise.

`ArchConfig` records the per-layer choices only: `choices: tuple[str, ...]` with `len(choices) == depth`.

## Weight Inheritance And Freeze Groups

`SuperNet.__init__(self, search_space: SearchSpace, pretrained_state: dict[str, torch.Tensor] | None = None, ...)`:

* When `pretrained_state` is given (the pretrained original model's `state_dict`, produced through the flatten node's `load_pretrained.py`):
  * Non-slot fixed modules (embeddings, positional encodings, stems, stage transitions, heads) load their weights from it.
  * Each slot's `original` branch loads the original layer's weights for that position.
  * Variant branches initialize randomly (fresh construction).
  * The mapping must be complete: every key in `pretrained_state` is consumed and every inherited parameter is filled. Unconsumed keys or unfilled parameters raise a fail-loud error listing the unmatched keys — never a silent partial load.
* Freeze groups (set in `__init__`, whether `pretrained_state` is given or not):
  * `original` branch parameters and non-slot fixed modules: `requires_grad_(False)`.
  * Variant branch parameters: `requires_grad=True`.
* At the end of `__init__`, set the active config to `search_space.all_original()` so the freshly built supernet is runnable and equivalence-checkable as-is.

## get_active_subnet Materialization Key Contract

`get_active_subnet()` returns a standalone fixed model whose module tree **mirrors the original model's topology**: fixed components sit at their original paths, and each slot position holds the active branch's exported module — no `ChoiceLayer` wrapper and no branch-name level in between. Consequences:

* For the all-original config, the exported subnet's `state_dict()` keys equal the pretrained original model's keys exactly. `check_equivalence.py` asserts this (key-set equality plus per-tensor value equality).
* For any other config, the slot position holds the chosen variant's exported module under the original layer path. Downstream strict `load_state_dict` and retrain weight extraction depend on this canonical placement.

## `__main__` Self-Check

The `__main__` demo block must:

* build `SearchSpace()` and `build_supernet()` with the user project's real construction values, and a dummy input matching the project's real input spec;
* call `search_space.validate()`;
* sample an `ArchConfig`, then run the supernet and its `get_active_subnet()` export on the same input in eval mode and compare outputs;
* when a sibling `load_pretrained.py` exists (it does once the flatten node has run), additionally build the pretrained original model and compare the **all-original path** against it tensor-by-tensor — the generation-time form of the equivalence gate;
* obtain the device via `resolve_device` from `nas_agent.train.distributed`.

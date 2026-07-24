# Isotropic Transformer Supernet Specification

This supplement extends `general_specs.md` with isotropic-transformer-specific rules for supernet generation.

## Isotropic Transformer Model

* The user model is an **equal-width, equal-resolution Transformer stack** with no stage-wise hierarchy.
* Keep one shared external residual width `global_dim` across all searchable layers.
* Keep `head_dim` fixed across the whole supernet.
* `depth` selects an active prefix of the uniform layer stack.
* `self.layers` should be an `nn.ModuleList` of `ChoiceLayer` containers, one for each Transformer layer position in the isotropic stack.
* Keep fixed components (e.g., patch embedding, positional embedding, `cls_token`, final norm) outside `self.layers`. These fixed components must use standard `nn` modules (`nn.Linear`, `nn.LayerNorm`, `nn.Parameter`, etc.), not `Elastic*` primitives, since they are not part of the search space.
* Do not introduce stage-wise or resolution-changing search variables.
* Do not search fixed component structure (patch embedding, positional embedding, final norm), task-head structure, or task-head dimensions.

## Transformer Block Requirements (Slice + Branch)

A "Block" is a Transformer-layer candidate implementation (a branch).

**Requirements:**

### Block I/O: all operations at `global_dim`

Each block takes input `[B, N, global_dim]` and outputs `[B, N, global_dim]`.

* **LayerNorm** operates at `global_dim`.
* **Attention** Q/K/V projections go from `global_dim` to internal `attn_dim = num_heads * head_dim`. The output projection goes from `attn_dim` back to `global_dim`.
* **FFN** projects `global_dim -> ffn_dim -> global_dim`.
* **Residual** connections add at `global_dim`.
* **Flow:** `output = x + attn(norm1(x))` and `output = x + ffn(norm2(x))`.

### Elastic Multi-Head Attention

* Q/K/V projections map from `global_dim` to `attn_dim = num_heads * head_dim`.
* Output projection maps from `attn_dim` back to `global_dim`.
* `num_heads` is searchable; `head_dim` is fixed. Only `attn_dim` changes with `num_heads`.
* Q/K/V projections:
  * If separate projections: use 3 `ElasticLinear`.
  * If fused QKV in original model: split into 3 logical `ElasticLinear` branches in the supernet and slice each branch correctly.

### Elastic FFN

* FFN projects `global_dim -> ffn_dim -> global_dim`.
* Use `ElasticLinear` for FFN layers so that smaller widths are sliced from a shared maximum-capacity weight tensor.

### Elastic Depth

* Initialize with maximum depth: `max_depth = max(search_space.depth_candidates)`.
* If active `depth = d`, forward only the first `d` layers.

## Export Rule

* `get_active_subnet()` must export the active depth prefix of the layer stack.
* The exported subnet must preserve the same fixed components (patch embedding, positional embedding, `cls_token`, final norm, task head) and the same searchable layer structure as the active supernet path. Fixed modules that carry parameters should be deep-copied (`copy.deepcopy`) into the exported subnet.

---

## Search Space Specification

The user model is an **isotropic Transformer**: all backbone layers share one residual width and there is no stage-wise downsampling or resolution change.

Define the search space as a **uniform layer stack** with:
- fixed `global_dim`
- fixed `head_dim`
- searchable global depth via `depth_candidates`: use **positive integers**; include the original model's layer count as the central value.
- per-layer block candidates in `layer_configs`

For this model family, keep the searchable per-layer variables centered on:
- `choice`: mandatory for searchable non-merging blocks
- `num_heads`: use **multiples of a base value** (e.g. base=2 -> 2, 4, 6, 8; base=3 -> 3, 6, 9, 12). Infer the base from the user model's head-count pattern. Powers of 2 are one common case but not a hard requirement.
- `ffn_dim`: prefer **multiples of 64 or 128** for hardware-friendly alignment.

`ArchConfig` records:
- `depth`: active depth
- `layers_config`: a tuple of per-layer configs, where the tuple length equals `depth`

All layer candidates must stay compatible with the same external residual stream:
- every active layer preserves `global_dim` as its I/O dimension
- attention uses internal `attn_dim = num_heads * head_dim` for Q/K/V projections, independent of `global_dim`

Use the `search_space.py` in this directory as the canonical reference for field names, raw config keys, and validation style.

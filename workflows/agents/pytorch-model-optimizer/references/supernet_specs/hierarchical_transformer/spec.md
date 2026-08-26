# Hierarchical Transformer Supernet Specification

This supplement extends `general_specs.md` with hierarchical-transformer-specific rules for supernet generation.

## Hierarchical Transformer Model

* The user model is a **hierarchical Transformer model** whose searchable backbone is organized by multiple searchable stages.
* Preserve the original macro skeleton:
  * input stem structure
  * stage order
  * stage-entry downsampling locations where they exist
  * residual / shortcut pattern
  * attention / FFN / normalization ordering
  * stage transitions
* Keep `head_dim` fixed across the whole supernet.
* Do not introduce CNN-specific spatial convolution structures into the searchable attention blocks of the Transformer supernet. Fixed components such as `stem` and `stage_downsample` modules are standard `nn` modules and are not part of the search space.
* Do not search stem structure, stage-downsample structure, task-head structure, or task-head dimensions.

## Stage Structure

* A searchable stage contains only the searchable non-downsampling attention blocks that belong to that stage.
* Fixed non-searchable modules (`stem`, `stage_downsample`) are outside the stage definition and outside stage depth.
* A repeated stack container of homogeneous attention blocks with no internal downsampling can itself be treated as one searchable stage.
* Infer stage partition from the prepared model using spatial-downsampling boundaries that reduce spatial resolution.
* Do not move, delete, or bypass the original downsample module when it is required for spatial-resolution reduction or dimension alignment.

## Supernet Layout

* Build the supernet with the **maximum active depth of each searchable stage**.
* Keep fixed non-searchable modules (`stem`, `stage_downsample`) outside `self.layers`. These must use standard `nn` modules (`nn.Linear`, `nn.LayerNorm`, `nn.Conv2d`, etc.), not `Elastic*` primitives, since they are not part of the search space.
* Store searchable stages in `self.layers = nn.ModuleList()` so that:
  * `self.layers[stage_idx]` corresponds to exactly one searchable stage
  * each stage container internally holds the maximum number of searchable non-downsampling attention blocks for that stage
* Keep explicit stage bookkeeping so the model knows:
  * `SearchSpace.stage_names` should explicitly name each searchable stage
  * which `stage_downsample` module precedes each stage (and `stem` for the first stage)
  * how many active searchable blocks to execute in each stage for the current sample
* The `forward(x)` execution order follows the prepared model's stage structure:
  * `stem` -> `stage_1_blocks` -> `stage_downsample_1` -> `stage_2_blocks` -> `stage_downsample_2` -> `stage_3_blocks` -> ... -> `task_head`
  * `stem` and `stage_downsample` modules are taken directly from the prepared model. They are never skipped, never searched, and never placed inside the searchable block stack.
  * `task_head` is the fixed non-searchable output module (e.g. classifier, detector head) that produces the model's final output.
* All inter-block and inter-stage tensors use **feature-last** `(B, *spatial, C)` layout (e.g., `(B, H, W, C)` for 2D vision), where `C` is the feature/embedding dimension on the last axis. `LayerNorm` and `Linear` operate directly on `C` without permutation. Convolution-based operations (inside `stage_downsample` and certain block internals) temporarily permute to place `C` before spatial dims as needed.

## Per-Stage Embedding Dimension

* Use a fixed `stage_emb_dims` tuple (e.g. `(96, 192, 384, 768)`) in the `SearchSpace` to specify the embedding dimension for each stage. Infer these values from the prepared model's existing stage dimensions.
* Each stage's blocks use its corresponding `stage_emb_dim` as their feature dimension (last dim of `(B, *spatial, C)`).
* All branch candidates at the same searchable layer position within a stage share the same `stage_emb_dim`.
* The SuperNet builds each stage directly using the fixed per-stage dimension from `stage_emb_dims`.

## Block Requirements (Slice + Branch)

A "Block" is a Transformer-layer candidate implementation (a branch).

* The user-model-derived block must preserve the original local logic as much as possible:
  * attention ordering
  * FFN ordering
  * normalization placement
  * pre-norm residual pattern: `output = x + f(norm(x))`
* For each searchable non-downsampling layer position inside a stage, build a real `ChoiceLayer` branch pool:
  * one branch should be the user-model-derived block
  * the remaining branches should come from compatible pre-built Transformer blocks imported from `nas_agent.blocks` metadata
* For the same searchable layer position, all branch candidates must share the same external contract:
  * same input & output feature dimension (`stage_emb_dim`)
  * same spatial resolution (no downsampling inside the branch)
  * same `(B, *spatial, C)` layout
* `choice` should always be a searchable dimension for each searchable layer position.
* Treat `num_heads` and `ffn_dim` as the default first-line local search variables for attention blocks.
* Do not modify any module that downsamples spatial resolution as part of the branch pool; keep those modules fixed outside the stage.

**Requirements:**

### Block I/O: `(B, *spatial, stage_emb_dim)`

Each searchable non-downsampling block takes input `[B, *spatial, stage_emb_dim]` and outputs `[B, *spatial, stage_emb_dim]`.

* **Normalization** uses `LayerNorm` operating on the last dimension `stage_emb_dim`. Use `ElasticLayerNorm` for elastic norm layers in supernet blocks — it operates directly on the last dimension without permutation.
* **Attention** Q/K/V projections go from `stage_emb_dim` to internal `attn_dim = num_heads * head_dim`. Blocks reshape `(B, *spatial, C)` to a flat sequence `[B, L, C]` where `L = prod(*spatial)` (zero-cost reshape, no permute) for attention computation and reshape back. The output projection goes from `attn_dim` back to `stage_emb_dim`.
* **FFN** projects `stage_emb_dim -> ffn_dim -> stage_emb_dim`. `Linear` layers operate directly on the last dimension `C` without permutation.
* **Residual** connections add at `stage_emb_dim`. Residual shortcuts are **block-internal** (e.g., `output = x + attn(norm(x))`); there is **no cross-stage residual** because `stage_downsample` changes both spatial resolution and feature dimension between stages.
* **Flow:** `output = x + attn(norm(x))`, etc. All at `(B, *spatial, C)`.
* Internal `attn_dim = num_heads * head_dim` is independent of `stage_emb_dim`.
* Width slicing inside a block must still preserve the block's fixed external `(B, *spatial, stage_emb_dim)` interface at that layer position.
* Blocks that require convolution operations internally (e.g., depthwise conv for local spatial mixing) temporarily permute `C` before spatial dims as needed.

### Elastic Multi-Head Attention

* Q/K/V projections map from `stage_emb_dim` to `attn_dim = num_heads * head_dim`.
* Output projection maps from `attn_dim` back to `stage_emb_dim`.
* `num_heads` is searchable; `head_dim` is fixed. Only `attn_dim` changes with `num_heads`.
* Blocks reshape `(B, *spatial, C)` to `[B, L, C]` where `L = prod(*spatial)` for linear projections and attention computation (zero-cost reshape on contiguous data).
* Q/K/V projections:
  * If separate projections: use 3 `ElasticLinear`.
  * If fused QKV in original model: split into 3 logical `ElasticLinear` branches in the supernet and slice each branch correctly.

### Elastic FFN

* FFN projects `stage_emb_dim -> ffn_dim -> stage_emb_dim`.
* Use `ElasticLinear` for FFN layers so that smaller widths are sliced from a shared maximum-capacity weight tensor.
* `ElasticLinear` operates on the last dimension of `(B, *spatial, C)` directly — no permutation needed.

### Elastic Depth

* `ArchConfig.stage_depths` should contain one active depth value per searchable stage.
* Each stage depth selects an active **prefix** of the searchable non-downsampling attention blocks in that stage.
* Fixed non-searchable modules (`stem`, `stage_downsample`) are not counted in stage depth.
* For a repeated stack container such as `blocks` or `layers`, treat that stack as one searchable stage and let `d` control its active prefix length.

## Export Rule

* `get_active_subnet()` must export the same stage-wise active prefix selected by `ArchConfig.stage_depths`.
* The exported subnet must preserve the same fixed non-searchable modules (`stem`, `stage_downsample`, task head) and the same searchable stage structure as the active supernet path. Fixed modules that carry parameters should be deep-copied (`copy.deepcopy`) into the exported subnet.

---

## Search Space Specification

Define the search space as a **fixed stage skeleton** with:
- fixed `stage_names`
- fixed `stage_emb_dims`: a tuple of per-stage embedding dimensions (e.g. `(96, 192, 384, 768)`).
- fixed `head_dim`
- searchable per-stage `depth` via `stage_depth_candidates` (active prefix of attention blocks)
- per-stage, per-layer block candidates in `stage_layer_configs`

`stage_layer_configs` is a **tuple** aligned with `stage_names`: each entry is one stage's block-choice dict, so dimension-related fields (e.g. `num_heads`, `ffn_dim`) can scale independently with the stage's fixed embedding dimension. Block choices should be the same across stages; only the candidate value ranges differ per stage where appropriate. This avoids creating an overly broad shared range that must cover all stages' different embedding dimension scales.

**Scaling rule**: dimension-related candidate values should scale proportionally to each stage's `stage_emb_dim`. Use the user model's original per-stage internal dimensions as the central reference, and expand nearby when sensible. Specifically, `num_heads` candidates should grow with `stage_emb_dim` (keeping `head_dim` fixed), and `ffn_dim` candidates should grow proportionally.

For this model family, keep the searchable per-layer variables centered on:
- `choice`: mandatory for searchable non-downsampling blocks
- `num_heads`: use **multiples of a base value** (e.g. base=2 -> 2, 4, 6, 8; base=3 -> 3, 6, 9, 12). Infer the base from the user model's head-count pattern. Powers of 2 are one common case but not a hard requirement.
- `ffn_dim`: prefer **multiples of 64 or 128** for hardware-friendly alignment.
- per-stage `depth`: use **positive integers**; include the original model's block count as the central value.

Some pre-built blocks are shared across model types; when selecting `choice` candidates, ensure each block's spatial assumptions (e.g., 2D window partition, stripe layout) match the user model's spatial layout.

`ArchConfig` records:
- `stage_depths`: one active depth per stage
- `layer_configs`: a dictionary mapping each stage name to a tuple of active layer configs, where the tuple length equals the corresponding stage depth

All layer candidates must stay compatible with the residual stream at each stage:
- every active block preserves `[B, *spatial, stage_emb_dim]` I/O within its stage
- attention uses internal `attn_dim = num_heads * head_dim` for Q/K/V projections, independent of `stage_emb_dim`

Do **not** search: stage count, stage order, stem or stage-downsample positions, stage_emb_dims, arbitrary rewiring, or arbitrary per-branch interface changes.

Use the `search_space.py` in this directory as the canonical reference for field names, raw config keys, and validation style.

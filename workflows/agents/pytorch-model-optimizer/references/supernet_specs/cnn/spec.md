# CNN Supernet Specification

This supplement extends `general_specs.md` with CNN-specific rules for supernet generation.

## CNN Model

* The user model is a **CNN model** whose searchable backbone is organized by multiple searchable stages.
* Preserve the original macro skeleton:
  * stem structure
  * stage order
  * hard representation-changing locations (pooling-only downsampling, pixel shuffle / unshuffle, flattening, explicit resizing, reshape-driven layout changes)
  * residual / shortcut pattern
  * convolution / normalization / activation ordering
* Do not search stem structure, task-head structure, or task-head dimensions.

## Stage Structure

* A searchable stage contains all searchable conv-like blocks that belong to that stage, **including** any stride>1 downsampling block at the first layer position.
* Unlike hierarchical Transformers where token-merge modules are extracted outside the stage, CNN downsampling (stride>1) is performed by the **first block inside the stage** and is part of the stage definition.
* Fixed hard representation-changing modules that are **not** conv-like blocks stay outside the stage definition and outside searchable branch pools.
  * Examples: pooling-only downsampling, pixel shuffle / unshuffle, patch embedding / merging, flattening, tokenization, explicit resizing, reshape-driven layout changes.
* Infer stage partition from the original backbone using channel-width boundaries or explicit downsampling boundaries.
* Determine whether a module is a fixed representation-changing transition by reading the original model's pooling, reshape, or any other non-conv-like spatial-change logic.
* Do not move, delete, or bypass the original fixed representation-changing module when it is required for spatial or channel alignment.

## Supernet Layout

* Build the supernet with the **maximum active depth of each searchable stage**.
* Keep fixed stem and hard representation-changing modules outside `self.layers`. These fixed components must use standard `nn` modules (`nn.Conv2d`, `nn.LayerNorm`, etc.), not `Elastic*` primitives, since they are not part of the search space.
* Store searchable stages in `self.layers = nn.ModuleList()` so that:
  * `self.layers[stage_idx]` corresponds to exactly one searchable stage
  * each stage container internally holds the maximum number of searchable layer positions for that stage
  * This applies even for single-stage CNNs: `self.layers[0]` must be a stage container (e.g. `nn.ModuleList` of `ChoiceLayer`), not a bare `ChoiceLayer`. Example: `self.layers = nn.ModuleList([nn.ModuleList([ChoiceLayer(...), ...])])`.
* Keep explicit stage bookkeeping so the model knows:
  * `SearchSpace.stage_names` should explicitly name each searchable stage
  * which fixed modules (stem, pooling, etc.) belong before each stage
  * how many active searchable blocks to execute in each stage for the current sample
* The `forward(x)` execution order should be:
  * `stem -> stage_1_blocks -> stage_2_blocks -> ... -> stage_N_blocks -> task_head`
  * `stem` is the fixed initial module that converts raw input into the first stage's feature maps at `stage_widths[0]`.
  * `task_head` is the fixed non-searchable output module (e.g. classifier, detector head) that produces the model's final output.
  * Each stage's first block may perform stride>1 downsampling internally. This downsampling block is **inside** the stage, not extracted outside.
  * Fixed hard representation-changing modules (if any) are executed at their original positions between stages.

## Per-Stage Width

* Use a fixed `stage_widths` tuple in the `SearchSpace` to specify the channel count for each searchable stage.
* The stem outputs feature maps at `stage_widths[0]` (or the appropriate first-stage width).
* Each stage's blocks operate directly at the stage's fixed width — no `input_embed` / `output_embed` 1×1 convolution adapters.
* The SuperNet builds each stage using the fixed per-stage width from `stage_widths`.
* All branch candidates at the same searchable layer position within a stage share the same `stage_width`.

## Block Requirements (Slice + Branch)

A "Block" is a conv-like candidate implementation (a branch).

* The user-model-derived block should preserve the original block's high-level computation structure where feasible:
  * convolution / normalization / activation ordering
  * internal expansion or bottleneck pattern
  * Minor structural simplifications are acceptable when wrapping the original block into an elastic searchable form, as long as the block's external I/O contract at `stage_width` is preserved.
  * All blocks use clean identity residual `return x + body(x)` and handle stride via `self.downsample` (see Block I/O below).
* For each searchable layer position inside a stage, build a real `ChoiceLayer` branch pool:
  * one branch should be the user-model-derived block
  * the remaining branches should come from compatible pre-built CNN blocks imported from `nas_agent.blocks` metadata
* For the same searchable layer position, all branch candidates must share the same external contract:
  * same input & output channel dimensions (see Block I/O below for first-block vs. subsequent-block rules)
  * same spatial output behavior: all branch bodies use stride=1; downsampling is handled by each block's `self.downsample`
  * same residual behavior: clean identity residual `return x + body(x)`
* `choice` should always be a searchable dimension for each searchable layer position.

**Requirements:**

### Block I/O

Every block has the same structure: `self.downsample` followed by a body at `out_channels` with `stride=1`, then a clean identity residual `return x + body(x)`.

* Each block is constructed with `(in_channels, out_channels, stride)`. Internally, `self.downsample = make_cnn_stage_downsample(in_channels, out_channels, stride)` is the first operation in `forward()`:
  * At stage boundaries (stride>1 or in_channels != out_channels): produces a `CNNStageDownsample2d` that handles spatial reduction and channel projection.
  * Within stages (stride=1 and in_channels == out_channels): produces `nn.Identity()`.
* After `self.downsample`, the block body operates entirely at `out_channels` with `stride=1`. All convolutions in the body use the same channel width and do not perform spatial downsampling.
* When building the user-model-derived elastic block, use the loaded pre-built CNN block source files as few-shot structural references for this Block I/O pattern.

Channel rules:

* **Conv / BN / activation** external I/O keeps both input and output channel dimensions at the position's expected channels (see above).
* **Internal hidden channels** (e.g. expanded channels in MBConv, bottleneck channels in ResBottleneck) are independent of the external channel dimensions and are searchable via absolute channel counts like `expand_channels`.
  * Do not use float ratio parameters (e.g. `expand_ratio`) for internal channel search. Use absolute integer channel values.
* Width slicing inside a block must preserve the block's fixed external interface.

Residual rules:

* All blocks (including the first downsampling block) use a clean **identity residual**: `return x + body(x)`. The `self.downsample` module handles spatial and channel alignment before the body, so no projection shortcut is needed.
* Residual connections add at `stage_width`.

### Elastic Convolution

* Conv layers use `ElasticConv2d` so that smaller kernel sizes are sliced from a shared maximum-kernel weight tensor.
* For `kernel_size` search on conv-like blocks:
  * preserve the original layer-position output-size behavior
  * for `stride=1` positions, keep shape unchanged when the original position is shape-preserving
  * for stride-downsampling positions, preserve the original downsampling behavior
* For non-standard shape-changing positions where output size depends on the combined `kernel_size` / `stride` / `padding` / `dilation`, preserve the original target output shape rather than only copying `stride`.
  * recompute candidate padding per kernel size
  * exclude candidate kernels that cannot match the original output shape with valid padding

### Elastic Depth

* `ArchConfig.stage_depths` should contain one active depth value per searchable stage.
* Each stage depth selects an active **prefix** of the searchable blocks in that stage.
* The fixed stem and hard representation-changing modules are not counted in stage depth.
* If a searchable block changes feature-map resolution and later tensors depend on that change, the minimum active depth must include that block.
* In particular, a mandatory `stride > 1` searchable layer position (typically the first block) must not be skipped by active-depth sampling.

## Export Rule

* `get_active_subnet()` must export the same stage-wise active prefix selected by `ArchConfig.stage_depths`.
* The exported subnet must preserve the same fixed stem, hard representation-changing modules, task head, and the same searchable stage structure as the active supernet path. Fixed modules that carry parameters should be deep-copied (`copy.deepcopy`) into the exported subnet.

---

## Search Space Specification

Define the search space as a **fixed backbone-stage skeleton** with:
- fixed `stage_names`
- fixed `stage_widths`: a tuple of per-stage channel widths (e.g. `(32, 64, 128, 256)`). Default values should be inferred from the user model's channel progression pattern. Prefer **multiples of 32** for optimal GPU throughput.
- searchable per-stage `depth` via `stage_depth_candidates`
- per-stage, per-layer block candidates in `stage_layer_configs`

`stage_layer_configs` is a **tuple** aligned with `stage_names`: each entry is one stage's block-choice dict, so dimension-related fields (e.g. `expand_channels`) can scale independently with the stage's fixed width. Block choices should be the same across stages; only the candidate value ranges differ per stage where appropriate. This avoids creating an overly broad shared range that must cover all stages' different width scales.

**Scaling rule**: dimension-related candidate values (e.g. `expand_channels`, `mid_channels`) should scale proportionally to each stage's `stage_width`. Use the user model's original per-stage internal widths as the central reference, and expand nearby when sensible.

For this model family, keep the searchable per-layer variables centered on:
- `choice`: mandatory for every searchable layer position
- `kernel_size`: use **odd values only** (e.g. 3, 5, 7). Even kernels break symmetric same-padding and are almost never used in practice.
- internal channel dimensions as absolute values when possible (e.g. `expand_channels` for MBConv, `mid_channels` for ResBottleneck): prefer **multiples of 32** for hardware-friendly alignment.
- per-stage `depth`: use **positive integers**; include the original model's repeat count as the central value.

`ArchConfig` records:
- `stage_depths`: one active depth per stage
- `layer_configs`: a dictionary mapping each stage name to a tuple of active layer configs, where the tuple length equals the corresponding stage depth

All layer candidates must stay compatible with the stage's channel stream:
- every active block assumes `stage_width` as its primary I/O dimension within its stage
- internal hidden channels are independent of the external `stage_width` and should be searched via absolute values

Derive default candidate ranges from the input model when no extra task-specific instruction is given:
- if a shape-changing searchable block is required by later tensor shapes, the minimum active depth must still include it

Do **not** search: stage count, stage order, hard downsampling / representation-changing positions, stage_widths, or arbitrary rewiring.

Use the `search_space.py` in this directory as the canonical reference for field names, raw config keys, and validation style.

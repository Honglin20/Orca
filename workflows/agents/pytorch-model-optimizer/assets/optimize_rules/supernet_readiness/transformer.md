# Transformer Supernet Readiness


## All Transformers

### Rule: Transformer BatchNorm Replacement

- name: Transformer BatchNorm Replacement
- type: mandatory
- description: Replace any `BatchNorm` layers with `LayerNorm`. BatchNorm running statistics are invalid in weight-sharing supernets where architecture dimensions vary per sample. `LayerNorm` is the standard Transformer normalization and operates on the last dimension independently of batch statistics.

#### Instruction

**When to apply**
- The Transformer model contains `nn.BatchNorm1d`, `nn.BatchNorm2d`, or `nn.BatchNorm3d` modules (uncommon in pure Transformers but possible in hybrid architectures, custom embeddings, or non-standard implementations).

**Do not apply when**
- The model already uses only `LayerNorm`, `RMSNorm`, or `GroupNorm` — no BatchNorm to replace.

**Implementation**
1. Replace every BatchNorm module with `nn.LayerNorm` over the appropriate normalized shape.
2. Keep affine parameters enabled unless the original BatchNorm was non-affine.
3. Do not carry over BatchNorm running-stat buffers (`running_mean`, `running_var`, `num_batches_tracked`).

**Validation**
- Verify that **no** BatchNorm modules remain anywhere in the model.
- Confirm the output shape of each rewritten module is unchanged.

---

### Rule: Pre-Norm Residual Standardization

- name: Pre-Norm Residual Standardization
- type: mandatory
- description: Convert all Transformer blocks from post-norm residual pattern to pre-norm residual pattern. All Transformer pre-built blocks (both isotropic and hierarchical) use pre-norm ordering `x = x + f(norm(x))`. Mixing post-norm blocks with pre-norm pre-built blocks in the same `ChoiceLayer` causes normalization position mismatch, breaking the interchangeability contract.

#### Instruction

**When to apply**
- The model's Transformer blocks use post-norm residual pattern: `x = norm(x + f(x))` or equivalent where normalization is applied **after** the residual addition.

**Do not apply when**
- The model already uses pre-norm residual pattern: `x = x + f(norm(x))`.

**Implementation**
1. Identify post-norm patterns in each block's `forward()`:
   ```python
   # Post-norm (BEFORE):
   x = self.norm1(x + self.attn(x))
   x = self.norm2(x + self.mlp(x))
   ```

2. Convert to pre-norm:
   ```python
   # Pre-norm (AFTER):
   x = x + self.attn(self.norm1(x))
   x = x + self.mlp(self.norm2(x))
   ```

3. Handle block-level final norm: if the original block applies a final normalization at the end of its `forward()` (e.g., `return self.final_norm(x)`) and this norm exists in every block instance, it is part of the post-norm pattern and should be removed. However, if a single final normalization exists only at the backbone level (after the last stage, before the classifier head), preserve it — it is not a block-internal norm.

4. The residual connection must be clean identity: `x = x + branch(norm(x))`. Do not add any normalization or activation after the residual addition.

**Validation**
- Confirm that every block's `forward()` follows the `x = x + f(norm(x))` pattern for each residual branch (attention and FFN).
- Confirm that no normalization or activation is applied after the residual addition inside any block.
- Verify that the backbone-level final norm (if any) is preserved.

---

## Hierarchical 2D Vision Transformer Only

The following rules apply only when the model is a hierarchical multi-stage 2D vision Transformer. They are not needed for isotropic (flat, single-scale) Transformer models.

> **Scope**: This section targets **2D vision** (images). All implementations — `TransformerStageDownsample2d`, window partitioning, NAS prebuilt blocks — are 2D-specific. For other spatial domains (3D volumetric, video), a separate rule set would be needed.

### Rule: Spatial Layout Conversion

- name: Spatial Layout Conversion
- type: mandatory
- description: Convert block I/O from flattened sequence layout `(B, L, C)` to explicit 2D spatial layout BHWC `(B, H, W, C)`. The channel dimension `C` remains on the last axis, preserving the BLC semantic — BHWC is simply BLC with the sequence length `L` unfolded into spatial dimensions `H × W`. This enables zero-cost reshape between the two views.

#### Instruction

**When to apply**
- The model's transformer blocks use `(B, L, C)` (BLC) I/O where `L = H * W` is a flattened spatial sequence.
- The model is being organized into a hierarchical multi-stage backbone where blocks must be aware of spatial dimensions.

**Implementation**
1. Change each block's `forward()` signature from `(B, L, C)` to `(B, H, W, C)` (BHWC). The channel dimension `C` stays on the last axis — this is the same semantic as BLC, just with spatial structure exposed.
2. Inside the block, if internal logic needs the flattened sequence view `(B, L, C)` (e.g., for Q/K/V projections, attention computation), use a zero-cost reshape: `x_seq = x.reshape(B, H * W, C)`. Reshape back with `x = x_seq.reshape(B, H, W, C)`. No permutation or memory copy is involved.
3. `LayerNorm` and `Linear` operate directly on the last dimension `C` — they work identically on both `(B, H, W, C)` and `(B, L, C)` shapes without any code change.
4. Blocks that need Conv2d operations internally (e.g., depthwise convolution for local spatial mixing) should permute locally: `x_bchw = x.permute(0, 3, 1, 2)` → `Conv2d` → `x = x_bchw.permute(0, 2, 3, 1)`. This permute is block-internal and does not affect the BHWC block I/O contract.

**Validation**
- Verify that all block `forward()` methods accept and return `(B, H, W, C)` tensors.
- Confirm that `LayerNorm` and `Linear` layers require no changes (they operate on the last dimension regardless of shape rank).
- Check that internal BLC ↔ BHWC conversions use `reshape` (not `permute` or `contiguous`), verifying zero-cost semantics.

---

### Rule: Convolutional Stage Downsampling

- name: Convolutional Stage Downsampling
- type: mandatory
- description: Uses a unified `TransformerStageDownsample2d` module (`LayerNorm → Conv2d`) to downsample spatial resolution by 2× and project channels between stages, replacing Swin-style PatchMerging (2×2 concat + Linear). Accepts and outputs BHWC tensors; Conv2d is applied on a temporarily permuted BCHW view.

#### Instruction

**When to apply**
- The model architecture requires a hierarchical feature pyramid instead of a single-scale columnar structure.
- You need to reduce the spatial resolution of the feature map to increase the receptive field of deeper layers and increase capacity (channels).
- You want a uniform downsample module that works consistently across all stage boundaries.

**Implementation**
1. Group transformer blocks into distinct sequential stages based on the network's overall configured depth.
2. Insert a `TransformerStageDownsample2d` between consecutive stages to build the feature pyramid. Use the same module type for every stage boundary. Import as `from nas_agent.blocks.common import TransformerStageDownsample2d`. I/O: `(B, H, W, C_in) → (B, ceil(H/stride), ceil(W/stride), C_out)`. Constructor keyword arguments: `in_channels`, `out_channels`, `stride` (default 2), `kernel_size` (2 or 3, default 2), `conv_bias` (default True), `norm_eps` (default 1e-6), `pad_odd_input` (default True). Two kernel-size variants:
   - **Latency-first default** (`kernel_size=2`): Minimal overlap, fastest.
   - **Accuracy / dense-prediction option** (`kernel_size=3`): Overlapping receptive field, smoother spatial downsampling.
3. The initial patch embedding stem uses the same convolutional pattern: `nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)` followed by permute to BHWC and optional `LayerNorm`. No special PatchEmbed design is needed — it is simply a standard Conv2d stem.

**Validation**
- Ensure that the output spatial dimensions are `(H // 2, W // 2)` (or `ceil(H / 2)` when odd-input padding is active for `k=2`).
- Verify that the channel dimension transitions correctly from `C_in` to `C_out` at each stage boundary.
- **[CRITICAL WARNING]**: Track spatial resolution dynamically through the model. Do not compute spatial sizes mathematically (like `H // 2**stage`) without accounting for odd-input padding. Instead, track actual spatial dimensions by reading the output of each stage (e.g., `H, W = x.shape[1], x.shape[2]`).


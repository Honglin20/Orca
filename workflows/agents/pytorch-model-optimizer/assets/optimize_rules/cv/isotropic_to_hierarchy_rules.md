# Isotropic-to-Hierarchy Conversion Rules

These rules convert an isotropic (flat, single-scale) vision Transformer into a hierarchical multi-stage backbone suitable for NAS supernet conversion. This is a **destructive** structural transformation that fundamentally reorganizes the model and requires full retraining.

> **[CRITICAL OVERRIDE]**: You are explicitly authorized to bypass the "preserve public interface" constraint in your skill workflow guidelines ONLY for the depth and dimension topology parameters to support multiple stages (e.g., replacing `depth=12` with `depths=[2, 5, 5, 2]`, `dim=768` with `embed_dim=[96, 192, 384, 768]`).

## Rule: Isotropic-to-Hierarchical Conversion

- name: Isotropic-to-Hierarchical Conversion
- type: tradeoff
- description: Restructures a flat/isotropic vision Transformer (uniform depth, single-scale, global attention) into a hierarchical multi-stage backbone with progressive spatial downsampling and channel expansion. The original Transformer block is replaced with a default windowed self-attention block with RPB; efficient local attention is used for all stages.
- pros: Dramatically reduces compute by processing smaller spatial resolutions in deeper stages; builds multi-scale feature pyramids for dense prediction tasks; enables NAS search over stage depths and block choices.
- cons: Fundamentally changes model architecture; requires retraining.

### Instruction

**When to apply**
- **Only when explicitly requested by the user.** This conversion is a destructive architectural change. Do NOT apply it automatically based on model analysis alone — it requires explicit user confirmation.
- The user has specifically requested converting an isotropic/flat vision Transformer (e.g., ViT, DeiT) to a hierarchical multi-stage backbone.

**Do not apply when**
- The user has not explicitly requested hierarchical conversion.
- The model is already a hierarchical backbone.

**Implementation**

#### 1. Stage Partitioning

Divide the uniform block stack into multiple stages with different spatial resolutions and channel dimensions. Infer the stage configuration from the model's **input spatial size**, **total depth**, and **embedding dimension**.

- **Number of stages**: Determined by the input spatial size after patch embedding. Each stage boundary halves spatial resolution via 2× downsampling, so the number of stages is constrained by `log2(H_patch / H_min)` where `H_min` is the smallest practical spatial size (typically 7). For example, with `patch_size=4` and input 224×224: patch resolution 56×56 → stages at 56→28→14→7, giving **4 stages**.
- **Depth distribution**: Distribute the total depth `D` across stages. The general principle is:
  - Early stages (high resolution): fewer layers — each layer is expensive due to large spatial size.
  - One deep stage (middle-to-late): the bulk of compute — moderate resolution, best compute/accuracy tradeoff.
  - Final stage (lowest resolution): fewer layers — already compact.
  - A common pattern is `[d_thin, d_thin, d_deep, d_thin]` where `d_deep` receives most of the depth budget.
- **Channel progression**: Double channels at each stage transition: `embed_dim = [C, 2C, 4C, 8C]`. The reason: when spatial resolution halves (H/2, W/2), the number of spatial tokens drops to 1/4. Doubling channels compensates, keeping roughly constant compute per layer across stages — the same principle as ResNet (64→128→256→512). The base `C` is chosen so that total FLOPs/parameters are comparable to the original model.
- **Head count**: Keep `head_dim` fixed across all stages. `num_heads[i] = embed_dim[i] // head_dim`.

You MUST modify the model's `__init__` signature to natively support multi-stage configurations, replacing single arguments like `depth=12` and `dim=768` with list-based signature defaults (e.g., `depths=[2, 5, 5, 2]`, `embed_dim=[96, 192, 384, 768]`, and `num_heads=...`). Do not alter unrelated interface signatures (e.g., `forward` inputs or `num_classes`).

#### 2. Default Hierarchical Block

Replace the original isotropic Transformer block with a standard **windowed self-attention block with relative position bias (RPB)** as the default building block for the hierarchical backbone. This is the most generic form of local attention for hierarchical vision Transformers — no cross-window mechanism (shifted window, dilated window, etc.) is included in the default; those are block-level variants provided by NAS prebuilt blocks during Step 5.

**Block structure** (pre-norm, BHWC I/O):
- `LayerNorm → Window Attention (with RPB) → DropPath → Residual`
- `LayerNorm → FFN → DropPath → Residual`
- I/O: `(B, H, W, C) → (B, H, W, C)` — spatial resolution and channel dimension unchanged.

**Block design**:
- Partition the spatial feature map into non-overlapping windows of fixed size (e.g., `window_size=7`). Pad H/W to the nearest multiple of `window_size` before partitioning; crop after merging.
- Compute standard multi-head self-attention within each window. Cost is $O(H \cdot W \cdot W_s^2 \cdot D)$, linear in spatial size.
- Include **relative position bias** (RPB) for position awareness within each window — a learnable table of shape `((2*Ws-1)*(2*Ws-1), num_heads)` indexed by pairwise relative positions (registered as a buffer), added to attention scores before softmax. RPB is **mandatory**: without it, window attention is completely position-blind within each window.
- FFN uses standard `Linear → GELU → Linear` with a configurable expansion ratio (default 4×).
- Include `DropPath` for stochastic depth regularization (see `vision_transformer_rules.md`).

**Reference implementation**: see `window_attention_block.py` in this directory for the complete, validated implementation including `WindowAttention`, `WindowAttentionBlock`, and all utility functions (`window_partition`, `window_reverse`, `pad_to_window_size`).

In the NAS supernet (Step 5), this default block becomes **one candidate** in each layer's `ChoiceLayer`. Other prebuilt blocks (shifted-window, NAT, MaxViT, PoolFormer, etc.) provide alternative candidates with different attention mechanisms.

#### 3. Apply Hierarchy Backbone Rules

The following structural and model-level changes are needed when converting from an isotropic to a hierarchical backbone:

- **Spatial Layout Conversion**: Convert block I/O from `(B, L, C)` to `(B, H, W, C)`.
- **Convolutional Stage Downsampling**: Insert `TransformerStageDownsample2d` between stages.
- **Remove Position Embeddings**: Remove global absolute position embeddings (`self.pos_embed`). The hierarchical backbone uses block-internal position encoding (e.g., RPB in window attention).
- **Global Average Pooling Head**: Replace `cls_token`-based classification with GAP over spatial feature maps.

See `../supernet_readiness/transformer.md` for structural rules (Spatial Layout Conversion, Convolutional Stage Downsampling) and `vision_transformer_rules.md` for model-level adaptations (Remove Position Embeddings, Global Average Pooling Head).

**Validation**
- Verify the converted model has the expected number of stages, each with the correct depth, channel dimension, and spatial resolution.
- Run a forward pass with a standard input (e.g., 224×224) and confirm:
  - Each stage outputs `(B, H_stage, W_stage, stage_emb_dim)`.
  - Spatial resolution halves at each stage transition.
  - The final classifier outputs `[batch_size, num_classes]`.
- Compare total parameter count and FLOPs to the original model to confirm they are in a reasonable range.

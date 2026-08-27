# General Optimization Rules

## Rule: Conv-Norm Bias Removal

- name: Conv-Norm Bias Removal
- type: safe
- description: If a convolution layer is immediately followed by a mean-subtracting normalization layer (BatchNorm, GroupNorm, LayerNorm, or InstanceNorm), remove the convolution bias. The mean subtraction step absorbs the per-channel additive bias, making it a redundant parameter.
- pros: Removes redundant parameters and a small amount of compute while preserving the same effective computation.
- cons: Usually none, but do not apply when the bias value is consumed before normalization.

### Instruction

**When to apply**
- Apply when a `Conv1d`, `Conv2d`, or `Conv3d` layer is immediately followed by a normalization layer that performs mean subtraction: `BatchNorm*`, `GroupNorm`, `LayerNorm`, or `InstanceNorm*`.
- Confirm the convolution bias is not consumed by any branch or side path before normalization.

**Do not apply when**
- The normalization is `RMSNorm` — it does not subtract the mean, so the conv bias is not redundant.
- The bias value is read or reused before normalization.
- Removing the bias would change a side effect relied on elsewhere.

**Implementation**
- Set `bias=False` on the convolution module.
- Keep tensor shapes, call order, and external interfaces unchanged.

---

# General Transformer Rules

## Rule: RMSNorm Substitution

- name: RMSNorm Substitution
- type: tradeoff
- description: Replace standard `nn.LayerNorm` with `nn.RMSNorm`. RMSNorm drops the mean-centering step, keeping only variance normalization, which reduces compute per norm layer.
- pros: Reduces normalization overhead and often fits modern decoder-style Transformer implementations well.
- cons: Changes the normalization behavior (removes mean-centering) and may require retraining to recover accuracy.

### Instruction

**When to apply**
- Decoder-style Transformers (e.g., LLM decoders) and sequence Transformers in NLP, telecom, or signal-processing domains using a pre-norm residual pattern with sufficient depth that normalization overhead accumulates.
- Models that already use RMSNorm in some layers — unify for consistency.

**Do not apply when**
- The model is a vision Transformer (e.g., ViT, Swin, DeiT, PVT, or other CV Transformer architectures). LayerNorm is the validated default for vision Transformers and the compute savings from RMSNorm are minimal in typical CV workloads.

**Implementation**
- Replace `nn.LayerNorm` modules with `nn.RMSNorm`.
- Preserve the normalized dimension and keep epsilon handling sensible for the target dtype and backend.
- Update both module construction and runtime calls without changing the surrounding block interface.

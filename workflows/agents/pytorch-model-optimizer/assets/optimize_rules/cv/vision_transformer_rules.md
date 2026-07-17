# Vision Transformer Rules

Optional optimization rules for vision Transformer architectures. These rules address individual training, regularization, or model-level adaptation decisions and can be selected independently of backbone structure.

## Rule: Stochastic Depth (DropPath)

- name: Stochastic Depth
- type: safe
- description: Introduces random dropping of paths within residual blocks (Stochastic Depth) during training to prevent over-fitting.
- pros: Regularizes very deep models effectively and promotes feature independence, allowing much deeper models to train successfully.
- cons: Adds slight complexity to the residual path logic, only active during training.

### Instruction

**When to apply**
- Deep architectures are prone to overfitting and gradient flow issues over many deep residual iterations.
- Necessary when implementing highly-layered dynamic multi-stage blocks where layer numbers grow (e.g., deeper network components config `[2, 2, 30, 2]`).

**Implementation**
1. Create a `drop_path` functional mapping and a corresponding `DropPath` `nn.Module` to encapsulate the stochastic depth drop logic.
2. Implement a linear decay rule (`torch.linspace(0, drop_path_rate, sum(depths))`) so that earlier blocks have a lower drop probability, and progressively deep blocks have a higher drop probability up to `drop_path_rate` (e.g., 0.1).
3. Incorporate the `DropPath` module identically in the main paths of residual blocks (typically after `attn` block and `mlp` block before addition).
4. `x = shortcut + self.drop_path(x)` followed by `x = x + self.drop_path(self.mlp(self.norm2(x)))`.

```python
def drop_path(x, drop_prob: float = 0., training: bool = False):
    if drop_prob == 0. or not training:
        return x
    keep_prob = 1 - drop_prob
    shape = (x.shape[0],) + (1,) * (x.ndim - 1)
    random_tensor = keep_prob + torch.rand(shape, dtype=x.dtype, device=x.device)
    random_tensor.floor_()
    return x.div(keep_prob) * random_tensor

class DropPath(nn.Module):
    def __init__(self, drop_prob=None):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob

    def forward(self, x):
        return drop_path(x, self.drop_prob, self.training)
```

**Validation**
- Ensure `DropPath` effectively acts as `nn.Identity()` or preserves inputs unchanged via probability scaling when model is in evaluation mode (`training=False`).
- Verify that the array scaling allocates correct probability per dynamically created block layer.

---

## Rule: Remove Position Embeddings

- name: Remove Position Embeddings
- type: tradeoff
- description: Remove global absolute position embeddings (APE) from the backbone. Modern hierarchical ViT and hybrid backbones do not add global APE at the backbone level — position information is encoded inside individual blocks or attention modules (e.g., relative position bias, conditional position encoding, or implicitly through local attention structure).
- pros: Removes a parameter that is tied to a fixed spatial resolution, enabling resolution-independent inference and compatibility with hierarchical stage transitions.
- cons: Changes how position information is provided; requires that block-internal position encoding (RPB, conditional PE, etc.) is available to compensate.

### Instruction

**When to apply**
- The backbone has global absolute position embeddings added to tokens before the transformer blocks (e.g., `x = x + self.pos_embed`).
- The model is being converted to a hierarchical backbone where spatial dimensions change between stages, making fixed-resolution global APE invalid.

**Do not apply when**
- The model already uses only block-internal position encoding (e.g., relative position bias, conditional PE) and has no global absolute position embeddings.

**Implementation**
1. Locate and remove the `pos_embed` parameter — typically `nn.Parameter(torch.zeros(1, num_patches, dim))`, `nn.Parameter(torch.zeros(1, num_patches + 1, dim))`, or `nn.Parameter(torch.zeros(1, H, W, dim))`.
2. Remove the addition step in the forward pass: `x = x + self.pos_embed`.
3. Remove any associated dropout applied after position embedding addition (e.g., `self.pos_drop(x)`), unless the same dropout is still needed elsewhere.
4. Do not add any replacement position encoding at the model level. Position information is handled internally by each block's attention mechanism (e.g., relative position bias for window attention, neighborhood structure for NAT, conditional position encoding for other blocks).

**Validation**
- Verify that no global position embedding parameter remains in the model's `state_dict`.
- Confirm that the model's forward pass no longer adds position embeddings before the transformer blocks.

---

## Rule: Global Average Pooling Head

- name: Global Average Pooling Head
- type: tradeoff
- description: Replace `cls_token`-based classification with Global Average Pooling (GAP) over spatial feature maps. Hierarchical backbones produce spatial outputs `(B, H, W, C)`, not a single cls_token. GAP aggregates spatial information into a channel vector for downstream task heads.
- pros: Resolution-independent; works naturally with spatial feature maps from hierarchical backbones; removes a learnable parameter.
- cons: Changes the classification mechanism; may affect accuracy for models specifically designed around cls_token aggregation.

### Instruction

**When to apply**
- The backbone uses a `cls_token` (prepended learnable token) for classification output.
- The backbone produces spatial feature maps `(B, H, W, C)` and the cls_token is not needed for the hierarchical design.

**Implementation**
1. Remove the `cls_token` parameter (`nn.Parameter(torch.zeros(1, 1, dim))`) and its concatenation with patch tokens in the forward pass.
2. Remove any cls_token-specific indexing used to extract the classification output (e.g., `x = x[:, 0]`).
3. If the model has `num_patches + 1` related computations (due to the extra cls_token position), update them to `num_patches`.
4. Replace with Global Average Pooling over spatial dimensions:

```python
# For BHWC backbone output:
x = self.norm(x)             # (B, H, W, C) — final LayerNorm
x = x.mean(dim=[1, 2])       # (B, C) — global average pool over spatial dims
x = self.head(x)             # (B, num_classes) — classifier
```

5. Update the classifier head from `nn.Linear(embed_dim, num_classes)` if it previously expected a cls_token output at a different dimension.

**Validation**
- Verify that the `cls_token` parameter and its concatenation/indexing are fully removed.
- Confirm the output shape remains `[batch_size, num_classes]` for classification tasks.
- Test with multiple input resolutions to confirm resolution independence.

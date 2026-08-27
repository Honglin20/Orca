# Wireless Link Scheduling Transformer Rules

Reusable optimization rules for transformer-based wireless link scheduling and per-token decision models.

## Rule: Sigmoid-Stable Cosine Projection

- name: Sigmoid-Stable Cosine Projection
- type: tradeoff
- description: Replaces the final `nn.Linear` → `sigmoid` projection with a `NormedLinear` layer (cosine linear) that L2-normalizes both input features and weight vectors, producing cosine-similarity scores in [-1, 1] scaled by a learnable scale factor. This decouples logit magnitude from feature and weight norms, so the logit scale is governed solely by the scale parameter rather than growing uncontrollably with training.
- pros: Decouples logit scale from feature/weight magnitudes, giving stable sigmoid gradients throughout training. The learnable scale adapts the logit dynamic range to the task automatically.
- cons: Changes the projection semantics from unconstrained affine to bounded cosine similarity. Requires `import torch.nn.functional as F`.

### Instruction

**When to apply**
- The final projection layer whose output is passed directly through `sigmoid` (or equivalent element-wise activation) to produce per-element binary decisions or independent probabilities.
- The model produces **independent per-element predictions** (e.g., per-token scheduling decisions, per-link resource allocation, per-pixel binary masks), NOT mutually-exclusive class logits consumed by softmax/cross-entropy.
- There is evidence (or strong expectation) that unconstrained logit magnitudes from `nn.Linear` cause sigmoid saturation and gradient vanishing during training.

**Do not apply when**
- The **input feature dimension is small** (e.g., `in_features` < 64). L2 normalization discards magnitude, which is a proportionally larger part of the representation in low dimensions, and cosine similarity between random vectors has high variance (std ≈ `1/√d`), making the projection unreliable.
- The output feeds into `softmax` or `cross_entropy` for multi-class classification — cosine classifiers exist for that setting (e.g., ArcFace) but use different margins and temperature semantics not covered by this rule.
- The output is consumed by a loss function that expects unconstrained logits (e.g., `BCEWithLogitsLoss` relies on the logit scale for its built-in numerical stability).
- The projection is an intermediate layer, not the final output projection.

**Implementation**
1. Implement the `NormedLinear` module:
```python
class NormedLinear(nn.Module):
    def __init__(self, in_features, out_features, scale=16.0):
        """Cosine-similarity linear layer with learnable scale.

        Effective scale = `scale` + `bias`, where `bias` is a learnable
        per-output offset initialized to zero.  Higher scale produces sharper
        sigmoid outputs (closer to 0/1).

        Args:
            in_features: Size of each input sample.
            out_features: Size of each output sample.
            scale: Base scale factor for the cosine similarity.  Initial
                logit std is approximately `scale / sqrt(in_features)`.  The
                default 16.0 gives logit std ≈ 1.0 at `in_features` = 256.
                The learnable `bias` adapts the effective scale during training.
        """
        super().__init__()
        self.weight = nn.Parameter(torch.empty(out_features, in_features))
        nn.init.kaiming_uniform_(self.weight, a=5**0.5)
        self.scale = scale
        self.bias = nn.Parameter(torch.zeros(out_features))

    def forward(self, x):
        cos_sim = F.normalize(x, dim=-1) @ F.normalize(self.weight.T, dim=0)
        return (self.scale + self.bias) * cos_sim
```
2. Replace the final `nn.Linear` with `NormedLinear(emb_dim, out_dim)`.
3. If the original model uses `BCEWithLogitsLoss`, switch to `BCELoss` (or apply `sigmoid` before the loss) since `NormedLinear` already controls the logit range.

**Validation**
- Run a forward pass and check that `output.abs().max()` is on the order of `scale` (not orders of magnitude larger), confirming that logit scale is governed by the scale parameter rather than unbounded feature magnitudes.
- Confirm that `sigmoid(output)` produces a healthy spread (not all near 0 or 1) at initialization — this indicates the scale is appropriate for the feature dimension.

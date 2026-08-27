# Telecom Transformer Rules

Reusable optimization rules broadly applicable to transformer architectures across telecom and signal processing workloads.

## Rule: Replace GELU with ReLU in FeedForward

- name: Replace GELU with ReLU in FeedForward
- type: tradeoff
- description: Swaps the `nn.GELU` activation function for `nn.ReLU` in the transformer's FeedForward network.
- pros: ReLU is faster and requires fewer FLOPs than GELU.
- cons: May reduce the expressivity and smooth optimization landscape typically provided by GELU.

### Instruction

**When to apply**
- Apply to the `FeedForward` or MLP block of transformer encoders.

**Implementation**
- Locate the `nn.Sequential` or sequential definition of the feedforward block.
- Replace instances of `nn.GELU()` with `nn.ReLU()` or `F.relu()`.

**Validation**
- Ensure there are no runtime errors during the forward pass of the feedforward layer.

---

## Rule: Simplify Embedding Projection

- name: Simplify Embedding Projection
- type: tradeoff
- description: Removes pre- and post-normalization layers from the initial input embedding projection, leaving a single linear projection.
- pros: Reduces redundant computations at the model entry point.
- cons: Might affect the initial scale and variance of embeddings entering the first attention block.

### Instruction

**When to apply**
- Apply when the input embedding projection is defined as a sequence of `LayerNorm -> Linear -> LayerNorm` or similar layered normalizations wrapping a single linear layer.

**Implementation**
- Replace `nn.Sequential(nn.LayerNorm(input_dim), nn.Linear(input_dim, emb_dim), nn.LayerNorm(emb_dim))` with a single `nn.Linear(input_dim, emb_dim)`.
- If positional encoding is used, apply it directly to the output of the linear projection.

**Validation**
- Ensure the tensor shape entering the first encoder layer matches expectations.

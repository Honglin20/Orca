# Isotropic Transformer Supernet Readiness

Rules in this file apply when the model is classified as an isotropic Transformer: token count constant across all repeated layers, no stage-wise merging, pooling, or downsampling.

## Rule: Native Layout to BLC Adapter

- name: Native Layout to BLC Adapter
- type: mandatory
- description: Every Isotropic Transformer block must expose `[B, L, C]` I/O, so its layer position can later host other prebuilt BLC layout blocks in the supernet. The model's computation stays the same, only the I/O shape at each layer's boundary changes.

### Instruction

**When to apply**
- The Isotropic Transformer block's native I/O is not `[B, L, C]`: it may have more axes (e.g. a native 4D layout `[B, N1, N2, N3]`), or the same axes in a different order.

**Axis roles**
- **`L`**: the block relates positions along this axis to each other, so one position's output depends on other positions.
- **`C`**: weights transform or contract this axis as part of a single token's representation, without relating its positions to each other as tokens.
- **`B`**: every position along this axis is processed identically and independently with shared weights, so it could move into the batch dimension with zero behavior change.

**Implementation**
1. Read the block's own attention or token-mixing computation: this fully determines `L` (every axis it relates positions along) and already fixes `C` for any axis that same computation folds in as head or content structure.
2. For every other native axis, determine whether anything in the block treats its positions differently through a learned parameter. If so, the axis is `C`. Otherwise, it is `B`. Never assign `L` here: the block itself never relates positions along such an axis, so treating it as `L` would introduce token relations the original model never had.
3. Edit the block class's `forward()` in place. Do not add a separate wrapper module around it, since that extra layer of indirection later complicates how the supernet-generation step locates and reasons about this block. At entry, accept `[B, L, C]` (`L` and `C` are each the product of their assigned axis sizes, and `C` becomes `global_dim`), reconstruct the exact native layout by unflattening `L` and `C` and folding any `B`-assigned axis into the batch dimension, then run the rest of the original computation unmodified. At exit, convert the result back to `[B, L, C]` before returning. Also adjust the surrounding model code so it works with `[B, L, C]`.

**Validation**
- The block's own `forward()` now accepts and returns `[B, L, C]`, with the same `L` and `C` definition at every layer position.
- With the block's `forward()` converted in place and the surrounding model code adjusted, the model reproduces the original model's outputs on a test input.
- `L` matches the number of positions the block's own attention/token-mixing relates, and `C` matches the per-token content width its weights operate over, following the axis mapping from Implementation step 1. A split that merely produces a valid round trip, without matching that mapping, is not enough.

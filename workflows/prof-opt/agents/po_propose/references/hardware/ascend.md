# Ascend Hardware Reference (design prior)

This is a pre-design reference, not a latency gate. The post-implementation
`mfu-analyzer` measurement is authoritative.

## Prefer

- Dense matrix-heavy paths with dimensions that tile into regular, aligned
  blocks; keep reduction and feature dimensions stable where possible.
- Reusable batched matmul, convolution, and attention projections rather than
  many tiny heterogeneous operators.
- Regular batch, sequence, and channel dimensions with small tail tiles. Where
  semantics permit, use padding or structured projection for irregular tails.
- Operator sequences that expose fusion and keep compatible layouts, reducing
  repeated format conversion and DMA traffic.
- Bounded-depth residual or gated blocks with predictable memory reuse instead
  of long serial chains of small standalone operations.

## Be cautious

- Very small matmuls, irregular dimensions, dynamic control flow, repeated
  transpose/layout changes, and frequent host-device transfers.
- Replacing meaningful normalization, attention, or gating with a cheaper
  activation without an accuracy hypothesis and training plan.
- Parallel branches whose outputs immediately require expensive concatenation,
  reduction, or synchronization.

## Design checklist

1. Name the measured MFU root cause.
2. Explain changed shapes/operator groups and their hardware mapping.
3. State the retained business-information invariant.
4. Treat latency direction as a hypothesis until MFU measurement.
5. Record exact source files and a reversible implementation plan.

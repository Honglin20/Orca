# Multi-Axis Attention Rules

Optimization strategies for telecom transformer architectures that process data along multiple axes (e.g., tokens/time-steps on the primary axis, and users/groups/channels/frequencies on a secondary axis) using interleaved attention loops.

## Rule: Deferred Secondary-Axis Attention

- name: Deferred Secondary-Axis Attention
- type: tradeoff
- description: When a transformer interleaves primary-axis attention (e.g., token-level) and secondary-axis attention (e.g., group/user-level) inside the same loop, defer all secondary-axis attention to a single pass after the primary-axis loop completes.
- pros: Eliminates repeated tensor reshaping and permutation between axes inside the loop, improving runtime efficiency while retaining some secondary-axis modeling capacity.
- cons: Reduces the depth of secondary-axis interaction to a single layer, which may hurt performance on tasks where deep cross-axis dependencies are important.

### Instruction

**When to apply**
- The model alternates between primary-axis and secondary-axis attention in a shared depth loop, with expensive reshape/permute operations at each transition.
- Cross-axis relationships are simple enough to be captured by a single attention pass rather than deep interleaving.

**Implementation**
1. Reduce the secondary-axis attention to a single layer.
2. Move the secondary-axis attention out of the primary-axis depth loop.
3. Run all primary-axis attention layers in a single loop without inter-axis reshaping.
4. After the loop, reshape the tensor once to secondary-axis layout, apply the single secondary-axis attention layer, and reshape back.

```python
# Before: interleaved primary and secondary attention
for i in range(num_layers):
    x = primary_attn[i](x, mask=mask)
    x = x.view(...).permute(...).contiguous().view(...)
    x = secondary_attn[i](x)
    x = x.view(...)

# After: all primary first, then one secondary pass
for i in range(num_layers):
    x = primary_attn[i](x, mask=mask)

x = x.view(batch, secondary_dim, primary_dim, embed_dim).permute(0, 2, 1, 3).contiguous()
x = x.view(-1, secondary_dim, embed_dim)
x = secondary_attn[0](x)
x = x.view(batch, primary_dim, secondary_dim, embed_dim)
```

**Validation**
- Verify that the output tensor shape matches the original model output shape.
- The secondary-axis attention modules beyond index 0 are removed.

---

## Rule: Replace Secondary-Axis Attention with Linear Projection

- name: Replace Secondary-Axis Attention with Linear Projection
- type: tradeoff
- description: Completely removes secondary-axis attention and replaces it with a static linear projection that expands the primary-axis output back to the secondary dimension.
- pros: Eliminates all O(secondary_dim²) attention cost and all inter-axis reshape/permute overhead, providing the largest latency reduction.
- cons: Replaces dynamic cross-axis interaction with a static linear mapping, potentially reducing expressiveness for complex inter-axis relationships.

### Instruction

**When to apply**
- The model interleaves primary-axis and secondary-axis attention in a shared depth loop.
- Primary-axis interaction dominates task performance, and cross-axis dependencies can be approximated by a learned linear mapping.

**Implementation**
1. Remove all secondary-axis attention modules.
2. Remove all reshape, permute, and contiguous operations that prepare tensors for secondary-axis attention.
3. Run the primary-axis attention on tokens directly with native masks (do not replicate masks across the secondary dimension).
4. Add a linear projection to recover the secondary dimension: `nn.Linear(d_model, d_model * secondary_dim)`.
5. Reshape the output back to `[batch, primary_dim, secondary_dim, d_model]` for downstream heads.

```python
# Before: interleaved primary and secondary attention
for i in range(num_layers):
    x = primary_attn[i](x, mask=mask_replicated)
    x = x.view(...).permute(...)
    x = secondary_attn[i](x)
    x = x.view(...)

# After: primary-only followed by linear expansion
for i in range(num_layers):
    x = primary_attn[i](x, mask=mask)

x = linear_expand(x)  # [batch, primary_dim, d_model] -> [batch, primary_dim, d_model * secondary_dim]
x = x.view(batch, primary_dim, secondary_dim, d_model)
```

**Validation**
- Verify that downstream heads receive the expected `[batch, primary_dim, secondary_dim, d_model]` tensor.
- All secondary-axis attention weights are dropped; a new linear expansion layer is introduced and requires finetuning.

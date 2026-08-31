# Common Transformer Supernet Readiness

Rules in this file are mandatory for every Transformer, regardless of isotropic or hierarchical subtype. Subtype-specific mandatory rules live in their own files, matched separately based on the model's classification.

## Rule: Transformer BatchNorm Replacement

- name: Transformer BatchNorm Replacement
- type: mandatory
- description: Replace any `BatchNorm` layers with `LayerNorm`. BatchNorm running statistics are invalid in weight-sharing supernets where architecture dimensions vary per sample. `LayerNorm` is the standard Transformer normalization and operates on the last dimension independently of batch statistics.

### Instruction

**When to apply**
- The Transformer model contains `nn.BatchNorm1d`, `nn.BatchNorm2d`, or `nn.BatchNorm3d` modules (uncommon in pure Transformers but possible in hybrid architectures, custom embeddings, or non-standard implementations).

**Do not apply when**
- The model already uses only `LayerNorm`, `RMSNorm`, or `GroupNorm`, with no BatchNorm to replace.

**Implementation**
1. Replace every BatchNorm module with `nn.LayerNorm` over the appropriate normalized shape.
2. Keep affine parameters enabled unless the original BatchNorm was non-affine.
3. Do not carry over BatchNorm running-stat buffers (`running_mean`, `running_var`, `num_batches_tracked`).

**Validation**
- Verify that **no** BatchNorm modules remain anywhere in the model.
- Confirm the output shape of each rewritten module is unchanged.

---

## Rule: Pre-Norm Residual Standardization

- name: Pre-Norm Residual Standardization
- type: mandatory
- description: Convert all Transformer blocks from post-norm residual pattern to pre-norm residual pattern. All Transformer pre-built blocks (both isotropic and hierarchical) use pre-norm ordering `x = x + f(norm(x))`. Mixing post-norm blocks with pre-norm pre-built blocks in the same `ChoiceLayer` causes normalization position mismatch, breaking the interchangeability contract.

### Instruction

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

3. Handle block-level final norm: if the original block applies a final normalization at the end of its `forward()` (e.g., `return self.final_norm(x)`) and this norm exists in every block instance, it is part of the post-norm pattern and should be removed. However, if a single final normalization exists only at the backbone level (after the last stage, before the classifier head), preserve it, since it is not a block-internal norm.

4. The residual connection must be clean identity: `x = x + branch(norm(x))`. Do not add any normalization or activation after the residual addition.

**Validation**
- Confirm that every block's `forward()` follows the `x = x + f(norm(x))` pattern for each residual branch (attention and FFN).
- Confirm that no normalization or activation is applied after the residual addition inside any block.
- Verify that the backbone-level final norm (if any) is preserved.

---

## Rule: Dropout / Stochastic Depth Removal

- name: Dropout / Stochastic Depth Removal
- type: mandatory
- description: Remove all dropout and stochastic-depth (`DropPath`) regularization from the model, including the constructor arguments that configure them. Neither belongs in a weight-sharing supernet: stochastic depth approximates variable-depth training by randomly skipping residual branches, but the supernet already searches depth as a first-class dimension, so every training step already runs a genuinely variable-depth architecture; dropout regularizes a fixed-capacity model by randomly zeroing activations, but a weight-sliced supernet already exercises a different subset of the same shared weights every step through elastic width, so stacking a second, uncoordinated source of randomness on top only adds noise to training without extra regularization benefit.

### Instruction

**When to apply**
- The model contains any `nn.Dropout`, `nn.Dropout2d`, custom `DropPath`/`drop_path` module or function, or dropout-probability constructor arguments (`dropout=`, `attn_drop=`, `proj_drop=`, `drop_path_rate=`, etc.), anywhere in `__init__` or `forward()`.

**Do not apply when**
- The model has no dropout or stochastic-depth logic anywhere in `__init__`/`forward()`.

**Implementation**
1. Delete every `nn.Dropout*` module instantiation and its call site in `forward()` (e.g. `x = self.pos_drop(x)`, `attn = self.attn_drop(attn)`); the tensor simply flows through unchanged where the call used to be.
2. Delete every `DropPath`/`drop_path` module or function (including its local class/function definition, if defined inside the model file) and its call sites in residual branches, e.g. `x = shortcut + self.drop_path(x)` becomes `x = shortcut + x`.
3. Remove the constructor arguments that configure the above (`dropout`, `drop_rate`, `attn_drop_rate`, `proj_drop_rate`, `drop_path_rate`, etc.) and any derived per-layer rate schedule.
4. Keep every other part of the block (attention, FFN, normalization, residual structure) unchanged; this rule removes stochastic regularization only, not architecture.

**Validation**
- Confirm no `nn.Dropout*` or `DropPath` module remains anywhere in the model.
- Confirm the constructor no longer accepts dropout- or drop-path-related keyword arguments.
- Run the model's forward pass once in `train()` mode and once in `eval()` mode on the same input; outputs must match exactly, confirming no stochastic op remains.

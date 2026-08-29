# Common Transformer Supernet Readiness

Rules in this file are mandatory for every Transformer, regardless of isotropic or hierarchical subtype. Subtype-specific mandatory rules live in their own files, matched separately based on the model's classification.

**Eligibility constraint**: every rule below must keep the model's outputs tensor-equivalent and the pretrained `state_dict` loadable as-is. Structure-changing rewrites (normalization-type replacement, pre-norm/post-norm conversion, downsample module replacement) are **not** readiness rules here: downstream, the original layers inherit the checkpoint weights verbatim and must reproduce the pretrained model's outputs tensor-by-tensor, which any computation-graph rewrite breaks by construction. If the flat model already computes what it should, the correct readiness action is no structural change at all.

## Rule: Dropout / Stochastic Depth Removal

- name: Dropout / Stochastic Depth Removal
- type: mandatory
- description: Remove all dropout and stochastic-depth (`DropPath`) regularization from the model, including the constructor arguments that configure them. The downstream pipeline trains variant branches by distillation against a frozen pretrained teacher and keeps the original layers weight-frozen; stochastic operators inject run-to-run randomness that breaks the tensor-exact equivalence check between the all-original supernet path and the pretrained model, and they contribute nothing once the inherited weights are frozen. The rule removes no parameters, so weight inheritance is unaffected.

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
- Reload the pretrained checkpoint: dropout modules carry no parameters or buffers, so the strict `load_state_dict` key set must be unchanged by this rule.

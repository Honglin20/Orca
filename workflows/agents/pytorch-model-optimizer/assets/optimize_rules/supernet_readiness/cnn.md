# CNN Supernet Readiness


## Rule: CNN Normalization Standardization

- name: CNN Normalization Standardization
- type: mandatory
- description: Replace **all** BatchNorm layers in the CNN model with `GroupNorm`. BatchNorm's running statistics become invalid in a weight-sharing supernet where width, depth, kernel, or branch structure varies per sample. GroupNorm normalizes over fixed-size channel groups, making it independent of batch statistics and compatible with elastic channel widths.

### Instruction

**Implementation**
1. Replace every `nn.BatchNorm1d`, `nn.BatchNorm2d`, and `nn.BatchNorm3d` in the model with `GroupNorm`.
2. Configure via `channels_per_group` (default 16) so each group normalizes over a fixed number of channels regardless of elastic width. When channel count is smaller than `channels_per_group`, GroupNorm automatically degrades to LayerNorm (single group).
3. Apply to all CNN conv feature paths without exception: stem conv, all backbone stages (standard/depthwise/dilated/pointwise conv), shortcuts, fusion layers, SE modules, and output head conv layers.
4. Keep affine parameters enabled unless the original BatchNorm was non-affine.
5. Do not carry over BatchNorm running-stat buffers (`running_mean`, `running_var`, `num_batches_tracked`).
6. Prefer channel counts that are multiples of 32 for hardware-friendly alignment and to ensure clean divisibility with `channels_per_group`.

**When to use LayerNorm instead of GroupNorm**
- Positions where spatial dimensions are already pooled to 1×1 (e.g. inside Squeeze-and-Excitation modules after `AdaptiveAvgPool2d(1)`, global average pooling branches). At 1×1 spatial size, the per-group sample is very small, so `LayerNorm` (single group over all channels) gives more stable statistics. GroupNorm also works here and will auto-degrade to `LayerNorm` for small channel counts.
- Non-conv paths: Transformer self-attention, MLP blocks, after reshape/flatten to `[N, L, D]` token sequences, and patch embedding layers in hybrid CNN-Transformer architectures.

Reference implementations of `GroupNorm2d` and `LayerNorm2d` are defined in `nas_agent.blocks.primitive_blocks`.

**Validation**
- Verify that **no** BatchNorm modules remain anywhere in the model.
- Confirm the output shape of each rewritten module is unchanged.

---

## Rule: CNN Stage Downsample Standardization

- name: CNN Stage Downsample Standardization
- type: mandatory
- description: Decouple stage-transition downsampling from CNN block bodies into a dedicated `CNNStageDownsample2d` module (`GroupNorm2d → Conv2d`, no activation, no residual). This standardizes the stage-transition contract: each block receives its input already at the target resolution and channel width, runs its body at `stride=1`, and uses an identity residual shortcut. This uniform contract is required for supernet conversion where different candidate blocks must be interchangeable at each layer position.

### Instruction

**When to apply**
- The CNN backbone has multi-stage structure with spatial downsampling between stages.
- Stage-transition downsampling uses a model-specific recipe (e.g., strided conv inside the block body, projection shortcut with post-add normalization) that does not match the `CNNStageDownsample2d` pattern.

**Do not apply when**
- Downsampling is in the input stem, classifier head, decoder, or auxiliary branches rather than backbone stage transitions.
- The model has no multi-stage structure.

**Implementation**
1. Decouple stage-transition downsampling: for each backbone block, add `self.downsample = make_cnn_stage_downsample(in_channels, out_channels, stride)` as the first operation in `forward()`. At stage boundaries (stride=2), this produces a `CNNStageDownsample2d` that handles spatial reduction and channel projection in one step; within stages (stride=1, same channels), it produces `nn.Identity()`. When stride=1 but `in_channels != out_channels` (channel expansion without spatial reduction), it also produces a `CNNStageDownsample2d` that handles the channel projection.
2. `CNNStageDownsample2d` implements ConvNeXt-style inter-stage downsampling (`GroupNorm → Conv2d`), handling spatial reduction and channel projection in one step. After `x = self.downsample(x)`, the tensor is already at `(out_channels, H/stride, W/stride)`. The block body then only needs to operate at `out_channels` with `stride=1` and use an identity residual shortcut: `return x + body(x)`. Therefore, remove the following from the original block:
   - Strided convolutions in the main path (change to stride=1).
   - Shortcut downsample projection (e.g., ResNet's `1×1 Conv(stride=2) + BN` for shape matching) — replace with identity since `CNNStageDownsample2d` already handles spatial and channel alignment.
3. Import as `from nas_agent.blocks.common import CNNStageDownsample2d, make_cnn_stage_downsample`. Constructor keyword arguments for `CNNStageDownsample2d`: `in_channels`, `out_channels`, `stride` (default 2), `kernel_size` (2 or 3, default 2), `conv_bias` (default False), `pad_odd_input` (default True). The convenience factory `make_cnn_stage_downsample(in_channels, out_channels, stride)` returns `nn.Identity()` when no transition is needed.
   - **Latency-first (default)**: `kernel_size=2, stride=2` — non-overlapping, fastest.
   - **Accuracy/dense-prediction**: `kernel_size=3, stride=2, padding=1` — overlapping receptive field, smoother downsampling.

**Example** — ResNet BasicBlock transformation:

```python
# Before: stride-dependent conditional logic in block
class BasicBlock(nn.Module):
    def __init__(self, in_ch, out_ch, stride=1):
        # conditional shortcut: projection when stride or channels change
        if stride != 1 or in_ch != out_ch:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_ch, out_ch, 1, stride=stride, bias=False),
                GroupNorm2d(out_ch),
            )
        else:
            self.shortcut = nn.Identity()
        self.conv1 = nn.Conv2d(in_ch, out_ch, 3, stride=stride, padding=1, bias=False)
        self.norm1 = GroupNorm2d(out_ch)
        self.conv2 = nn.Conv2d(out_ch, out_ch, 3, padding=1, bias=False)
        self.norm2 = GroupNorm2d(out_ch)

    def forward(self, x):
        identity = self.shortcut(x)
        out = F.relu(self.norm1(self.conv1(x)))
        out = self.norm2(self.conv2(out))
        return F.relu(out + identity)

# After: uniform block — stride handling absorbed by make_cnn_stage_downsample
class BasicBlock(nn.Module):
    def __init__(self, in_ch, out_ch, stride=1):
        self.downsample = make_cnn_stage_downsample(    # ConvNeXt-style downsample
            in_channels=in_ch, out_channels=out_ch, stride=stride,
        )  # CNNStageDownsample2d when stride=2; Identity when stride=1 & in_ch==out_ch
        # body: always at out_ch, stride=1 — no conditional logic
        self.norm1 = GroupNorm2d(out_ch)
        self.conv1 = nn.Conv2d(out_ch, out_ch, 3, padding=1, bias=False)
        self.norm2 = GroupNorm2d(out_ch)
        self.conv2 = nn.Conv2d(out_ch, out_ch, 3, padding=1, bias=False)

    def forward(self, x):
        x = self.downsample(x)
        out = F.relu(self.norm1(self.conv1(x)))
        out = self.norm2(self.conv2(out))
        return x + out                                  # identity shortcut
```

**Validation**
- Verify that each stage transition produces the expected spatial resolution and channel width.
- Confirm that no model-specific projection shortcuts or strided convolutions remain at stage boundaries.

---

## Rule: CNN Clean Residual Path

- name: CNN Clean Residual Path
- type: mandatory
- description: Ensure each CNN block uses a clean identity residual shortcut `return x + body(x)` with no post-addition normalization or activation. This guarantees that all candidate blocks in a `ChoiceLayer` produce outputs with identical residual path semantics, regardless of the internal body structure (pre-activation, post-activation, inverted bottleneck, etc.).

### Instruction

**When to apply**
- Any CNN block that uses a residual connection.
- Common violations: `F.relu(out + identity)`, `self.norm(out + identity)`, `F.relu(self.norm(out + identity))`.

**Do not apply when**
- The block has no residual connection (e.g., plain VGG-style sequential convolutions). In that case, add identity shortcut `return x + body(x)` if the spatial and channel dimensions match; otherwise the downsample rule (above) handles the alignment.

**Implementation**
1. Remove activation functions applied after the residual addition. For example, change `return F.relu(out + identity)` to `return x + out`.
2. Remove normalization layers applied after the residual addition. For example, change `return self.norm(out + identity)` to `return x + out`.
3. The block body may use any internal normalization ordering (pre-activation `norm → act → conv` or post-convolution `conv → norm → act`) — this rule does not constrain the body's internal structure, only the residual path.
4. After applying the Stage Downsample Standardization rule, the shortcut path is always identity (`x` after `self.downsample(x)`), so the residual addition is simply `x + body_output`.

**Validation**
- Confirm that every block's `forward()` ends with `return x + out` (or equivalent) with no additional operations between the addition and the return.
- Verify that the residual addition does not pass through any `nn.Module` or functional call.

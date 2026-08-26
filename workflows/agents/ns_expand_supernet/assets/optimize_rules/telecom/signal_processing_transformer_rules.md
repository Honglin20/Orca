# Signal Processing Transformer Rules

Reusable optimization rules for transformer architectures processing multi-channel signal data over discrete sequence and frequency dimensions.

## Rule: Sequence Downsampling and Upsampling

- name: Sequence Downsampling and Upsampling
- type: tradeoff
- description: Wraps the transformer backbone with a downsampling layer (e.g., MaxPool1d) on a sequence dimension and a corresponding upsampling layer (e.g., ConvTranspose1d), while potentially reducing the number of transformer blocks.
- pros: Significantly reduces the sequence length for the attention mechanism, substantially decreasing computational complexity and memory usage.
- cons: Can lead to loss of fine-grained resolution; requires careful shape management across the downsampled dimension.

### Instruction

**When to apply**
- When a target sequence dimension is large, leading to expensive self-attention computation, and local dependencies can be summarized.
- Do not apply if exact element-wise resolution must be preserved inside the attention mechanism without information loss.

**Implementation**
1. Add a downsampling layer before the transformer backbone:
   ```python
   self.ds = nn.MaxPool1d(kernel_size=2, stride=2, padding=0)
   ```
2. Add a corresponding upsampling layer after the transformer backbone:
   ```python
   self.us = nn.ConvTranspose1d(in_channels=embed_dim, out_channels=embed_dim, kernel_size=2, stride=2, padding=0, bias=True)
   ```
3. Adjust the downstream transformer blocks' initialization to expect the halved dimension size. Optionally reduce the total number of blocks.
4. In the `forward` pass, reshape the input tensor to isolate the target dimension, apply downsampling, pass through the transformer backbone, then apply upsampling to restore the original dimension size.

**Validation**
- Verify that the final output tensor shape perfectly matches the input tensor shape, specifically checking that the downsampled sequence dimension is fully restored.
- Confirm checkpoints that rely on the original sequence length are not loaded directly without shape adjustments.

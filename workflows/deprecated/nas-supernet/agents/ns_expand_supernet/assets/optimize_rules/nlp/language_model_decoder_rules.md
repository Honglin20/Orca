# Language Model Decoder Rules

Language model decoder transformer optimizations including modern positional encodings and structural simplifications to improve parameter efficiency and long-context behavior.

## Rule: Replace Absolute Positional Encoding with RoPE

- name: Replace Absolute Positional Encoding with RoPE
- type: tradeoff
- description: Switch from absolute positional embeddings to Rotary Positional Encoding (RoPE) applied to query and key tensors at each layer.
- pros: Better extrapolation to longer sequences, relative distance awareness, no learned position embeddings.
- cons: Slightly higher FLOPs per attention computation, breaks exact numerical equivalence with baseline.

### Instruction

**When to apply**
- The model uses `nn.Embedding` for positions and adds them to token embeddings before the transformer layers.
- The attention mechanism projects to queries and keys.

**Implementation**
1. Remove `AbsolutePositionalEncoding` (or equivalent `nn.Embedding` for positions) and the addition step `x = x + pos` before the transformer layers.
2. Introduce a `RotaryPositionalEncoding` module that precomputes and caches sine and cosine values based on inverse frequencies.
3. Apply RoPE to the Query and Key tensors *after* they are projected from the input, but *before* computing attention scores.

```python
# Helper to apply RoPE
class RotaryPositionalEncoding(nn.Module):
    def __init__(self, d_model, max_len, base=10000.0):
        super().__init__()
        inv_freq = 1.0 / (base ** (torch.arange(0, d_model, 2).float() / d_model))
        self.register_buffer("inv_freq", inv_freq)
        self._build_cache(max_len)
        
    def _build_cache(self, max_len):
        t = torch.arange(max_len, device=self.inv_freq.device).type_as(self.inv_freq)
        freqs = torch.einsum("i,j->ij", t, self.inv_freq)
        emb = torch.cat((freqs, freqs), dim=-1)
        self.register_buffer("cos_cached", emb.cos()[None, None, :, :])
        self.register_buffer("sin_cached", emb.sin()[None, None, :, :])

    def forward(self, q, k, seq_len):
        cos = self.cos_cached[:, :, :seq_len, :]
        sin = self.sin_cached[:, :, :seq_len, :]
        d_k = q.size(-1)
        cos, sin = cos[..., :d_k], sin[..., :d_k]
        
        def _rotate_half(x):
            x1, x2 = x[..., :x.shape[-1]//2], x[..., x.shape[-1]//2:]
            return torch.cat((-x2, x1), dim=-1)
            
        return (q * cos) + (_rotate_half(q) * sin), (k * cos) + (_rotate_half(k) * sin)
```

4. Modify the self-attention `forward` pass to apply RoPE. Pass the sequence length down.
```python
# Before calculating scores:
Q, K = self.rotary_pe(Q, K, seq_len)
```

**Validation**
- Ensure Q and K shapes are `[batch, heads, seq_len, head_dim]` before applying RoPE.
- Verify that `head_dim` is an even number.

---

## Rule: Remove Linear Biases

- name: Remove Linear Biases
- type: tradeoff
- description: Remove bias terms from all `nn.Linear` projections (Q, K, V, O, FFN, and Output projection).
- pros: Reduces parameter count slightly, often empirically matches performance of biased models in large LLMs.
- cons: Requires retraining.

### Instruction

**When to apply**
- The transformer uses standard `nn.Linear` layers with default `bias=True`.

**Implementation**
1. Locate all instances of `nn.Linear` in the Attention module (`w_q`, `w_k`, `w_v`, `w_o`).
2. Locate all instances of `nn.Linear` in the FeedForward module.
3. Locate the final `output_proj` (or similar language modeling head).
4. Explicitly add `bias=False` to their instantiation.

```python
# Change:
self.w_q = nn.Linear(d_model, d_model)
# To:
self.w_q = nn.Linear(d_model, d_model, bias=False)
```

**Validation**
- `count_parameters()` should decrease.
- The old `state_dict` will no longer load directly because the `*.bias` keys will be missing from the new model.

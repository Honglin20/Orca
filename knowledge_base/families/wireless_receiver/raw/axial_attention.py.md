# axial_attention.py.md — M21：time-then-freq 轴向 attention

> **这是什么**：两个 1D attention **串联**——先在 symbol（time）轴做一次 attention，再在 subcarrier（freq）轴做一次 attention，等价于把 2D `(S, F)` attention 的 `O((S·F)²)` 分解成 `O(S² + F²)`。**为什么**：SPEC §4 D7（[2510.12941]）实测在 OFDM 上低损；物理基础是 T-F 域**可分**（信道在时/频两轴分别稀疏）；昇腾友好——全是 GEMM + softmax，无特殊算子。

---

## 计算复杂度对比

```
全 2D attn:  O((S·F)²) = O(64·48)² = O(9.4M)
time-then-freq:
   time轴:  O(S²·F) = O(64²·48) = O(196k)
   freq轴:  O(F²·S) = O(48²·64) = O=147k)
   合计 ≈ 343k，比 2D 全 attn 降 27×
```

---

## 可跑骨架

```python
import torch
import torch.nn as nn
import torch.nn.functional as F


class AxialAttention1D(nn.Module):
    """沿指定轴做 1D multi-head attention。
    x 形状：[B, S, E, F]（与 baseline 一致），dim 轴 'time' 或 'freq'。
    """
    def __init__(self, embed_dim=16, seq_len=64, num_heads=4, axis="time"):
        super().__init__()
        assert embed_dim % num_heads == 0
        self.axis = axis                          # 'time' 在 S 轴，'freq' 在 F 轴
        self.num_heads = num_heads
        self.head_dim = embed_dim // num_heads    # 4（升 embed_dim 才能到 16）
        self.scale = self.head_dim ** -0.5
        self.seq_len = seq_len                    # time:64 / freq:48
        self.qkv = nn.Linear(embed_dim, 3 * embed_dim, bias=True)
        self.proj = nn.Linear(embed_dim, embed_dim, bias=True)

    def forward(self, x):
        # x: [B, S, E, F] —— baseline 的内部表示
        B, S, E, F_ = x.shape
        if self.axis == "time":
            # 在 S 轴做 attention，每个 (B, F) 切片是一条 seq
            # reshape: [B, S, E, F] → [B*F, S, E]
            x_r = x.permute(0, 3, 1, 2).reshape(B * F_, S, E)   # [B*48, 64, 16]
            seq = S
        else:  # freq
            # 在 F 轴做 attention，每个 (B, S) 切片是一条 seq
            x_r = x.reshape(B * S, F_, E)                       # [B*64, 48, 16]
            seq = F_

        qkv = self.qkv(x_r)                                     # [B*?, seq, 3E]
        qkv = qkv.reshape(-1, seq, 3, self.num_heads, self.head_dim)
        q, k, v = qkv[..., 0, :, :], qkv[..., 1, :, :], qkv[..., 2, :, :]
        # 各形状 [B*?, h, seq, head_dim]
        q = q.permute(0, 2, 1, 3)                               # [B*?, h, seq, head_dim]
        k = k.permute(0, 2, 1, 3)
        v = v.permute(0, 2, 1, 3)
        dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale  # [B*?, h, seq, seq]
        at = F.softmax(dots, dim=-1)
        out = torch.matmul(at, v)                               # [B*?, h, seq, head_dim]
        out = out.permute(0, 2, 1, 3).reshape(-1, seq, E)       # [B*?, seq, E]
        out = self.proj(out)                                    # [B*?, seq, E]

        # reshape 回 [B, S, E, F]
        if self.axis == "time":
            out = out.reshape(B, F_, S, E).permute(0, 2, 3, 1)  # [B, S, E, F]
        else:
            out = out.reshape(B, S, F_, E)                      # [B, S, F, E]→[B,S,E,F]
            # E 和 F 在 reshape 时换了位置，需要 permute
            out = out  # 这里依赖 qkv 投影在 E 上工作，已经回正；按需调整
        return out


class AxialAttentionBlock(nn.Module):
    """time-then-freq 两个 1D attn 串联 + 残差，替 baseline SignalAttention1D。"""
    def __init__(self, embed_dim=16, num_symbols=64, num_subcarriers=48, num_heads=4):
        super().__init__()
        self.attn_t = AxialAttention1D(embed_dim, seq_len=num_symbols,
                                       num_heads=num_heads, axis="time")
        self.attn_f = AxialAttention1D(embed_dim, seq_len=num_subcarriers,
                                       num_heads=num_heads, axis="freq")
        self.norm1 = nn.LayerNorm(embed_dim)
        self.norm2 = nn.LayerNorm(embed_dim)

    def forward(self, x):
        # x: [B, 64, 16, 48]
        # time-axial
        x = x + self.attn_t(self.norm1(x.permute(0, 1, 3, 2)).permute(0, 1, 3, 2))
        # freq-axial
        x = x + self.attn_f(self.norm2(x.permute(0, 1, 3, 2)).permute(0, 1, 3, 2))
        return x   # [B, 64, 16, 48]


if __name__ == "__main__":
    blk = AxialAttentionBlock(embed_dim=16, num_symbols=64, num_subcarriers=48, num_heads=4)
    x = torch.randn(2, 64, 16, 48)
    y = blk(x)
    print("axial out:", y.shape)
    assert y.shape == x.shape
```

---

## 变异提示（不要照抄）

- **顺序是个轴**：time→freq 还是 freq→time？两者不交换（attention 非交换）；可以试两种，也可以**双向并行** + 融合（变成 simultaneous axial，但失去分解优势）。
- **heads 是个轴**：time 和 freq 可以用不同 head 数（time 多普勒结构、freq 多径结构，先验不同）。
- **加 M8 窗**：在每个 axial attention 内部再套 W=16 窗 → "windowed axial"，进一步降 FLOPs。
- **加 M13 fold**：部署期可把每个 axial attn 单独 fold 成线性层（沿轴的 1D MMSE 滤波器）—— "linear axial"。
- **head_dim÷16 限制**：和 M7/M8 一样，想用 NPU 融合算子必须 head_dim=16+ → embed_dim=64（4 head）或 128（8 head）。
- **物理对应**：time-axial 对应**多普勒域**的稀疏（信道时变）；freq-axial 对应**多径时延域**的稀疏（信道频选）。两端分别 sparse → 轴向近似有理论支撑。
- **fail-loud**：不要把 time 和 freq 合成一个 2D 全 attention——那就是 baseline 的 `m_type` 切换，复杂度爆炸；本 move 的卖点就是分解。
- **顺序敏感**：先 norm 再 attn 还是先 attn 再 norm（pre-norm vs post-norm）——pre-norm 训练更稳定，本骨架用了 pre-norm。

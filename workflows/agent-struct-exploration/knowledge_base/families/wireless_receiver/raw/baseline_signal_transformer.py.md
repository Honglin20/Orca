# baseline_signal_transformer.py.md — D0 锚点

> **这是什么**：当前线上模型 `SignalProcessingTransformer` 的逐块标注 + 可变异点清单。它是所有 RAW 示例的**坐标原点**——其他 move 都标"相对 baseline 改了哪一块"。**不要照抄本文件来"优化"，它是被优化的对象**。

---

## 接口契约（所有 RAW 必须对齐）

- 输入：`[B, num_ports=4, num_subcarriers=48, num_symbols=64, 1]`
- 输出：同形
- 归一化：`alpha = sqrt(mean(inp², dim=[1,2,3], keepdim=True) * 2)`，前除后乘（`x/α` → 网络 → `x*α`）
- 内部主轴：`permute(0,3,1,2)` 后 reshape 到 `[B*num_symbols, num_ports, num_subcarriers]`，**Conv1d 把 num_ports 当 channel、num_subcarriers 当 length**

---

## 逐块标注代码

```python
import torch
import torch.nn as nn


# ============================================================
# 模块 A：per-channel 64×64 symbol attention（怪异写法！）
# ============================================================
# 怪在哪里：
#   正常 MHA 是 embed_dim 切成 num_heads 份；这里反过来——
#   每个 embed_dim 通道**独立**做一次 64×64 attention，
#   相当于 embed_dim=16 个并行 attention 块，没有任何混合。
#   q/k/v 形状 [B, 16, 64, 48]，dots 形状 [B, 16, 64, 64]，
#   16 是"假头"，不是 head split。
# 这正是 SPEC §1 的 17% 占比来源，也是 TransData 税重的位置。
class SignalAttention1D(nn.Module):
    def __init__(self, embed_dim, num_symbols, num_subcarriers, b_flg=True, m_type="t1"):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols          # 64
        self.num_subcarriers = num_subcarriers  # 48
        self.m_type = m_type
        # t1: 在 symbol 轴做 attention，scale 用 num_subcarriers^-0.5
        # 其它: 在 subcarrier 轴做 attention，scale 用 embed_dim^-0.5
        self.s = num_subcarriers ** -0.5 if m_type == "t1" else embed_dim ** -0.5
        # ⚠️ LayerNorm 不可 fold（Vector 归约，昇腾不支持 conv 融合）—— 见 M1
        self.ln = nn.LayerNorm([embed_dim, num_symbols, num_subcarriers], elementwise_affine=False)
        self.sm = nn.Softmax(dim=-1)
        # ★ 变异点 1：QKV 投影——3× embed_dim 输出，kernel=3（会触发 im2col+TransData）
        #   → M4 改 kernel=1（pointwise，纯 GEMM，无 im2col）
        #   → M5 把 stem 的 e_lyr 与 p_lyr 融合成单投影
        self.p_lyr = nn.Conv1d(embed_dim, 3 * embed_dim, kernel_size=3, padding=1, bias=b_flg)

    def forward(self, x):
        # x: [B, num_syms=64, embed_dim=16, num_subs=48]
        batch, num_syms, embed_dim, num_subs = x.shape

        # LN 前后 permute：[B,16,64,48] → LN → [B,64,16,48]
        x = x.permute(0, 2, 1, 3)                                   # [B, 16, 64, 48]
        x = self.ln(x)                                              # [B, 16, 64, 48]
        x = x.permute(0, 2, 1, 3)                                   # [B, 64, 16, 48]

        x_f = torch.reshape(x, [batch * num_syms, embed_dim, num_subs])  # [B*64, 16, 48]
        qkv = self.p_lyr(x_f)                                       # [B*64, 48, 48]  ⚠️ TransData!
        qkv = torch.reshape(qkv, [batch, num_syms, 3 * embed_dim, num_subs])  # [B, 64, 48, 48]

        q = qkv[:, :, 0:embed_dim, :]              # [B, 64, 16, 48]
        k = qkv[:, :, embed_dim:2*embed_dim, :]    # [B, 64, 16, 48]
        v = qkv[:, :, 2*embed_dim:, :]             # [B, 64, 16, 48]

        if self.m_type == "t1":
            # 每个通道独立 attention：[B,16,64,64] dots
            q = q.permute(0, 2, 1, 3)              # [B, 16, 64, 48]
            k = k.permute(0, 2, 1, 3)              # [B, 16, 64, 48]
            v = v.permute(0, 2, 1, 3)              # [B, 16, 64, 48]
            # ★ 变异点 2：手搓 matmul+softmax → M7 改 npu_fusion_attention
            # ★ 变异点 3：N=64 太短，linear-attn 是陷阱 → M8/M21 改窗/轴向
            dots = torch.matmul(q, k.transpose(-1, -2)) * self.s  # [B, 16, 64, 64]
            at = self.sm(dots)                                    # [B, 16, 64, 64]
            out = torch.matmul(at, v).permute(0, 2, 1, 3)         # [B, 64, 16, 48]
        else:
            q = q.permute(0, 3, 1, 2)              # [B, 48, 16, 64]  ← subcarrier 轴 attn
            k = k.permute(0, 3, 1, 2)
            v = v.permute(0, 3, 1, 2)
            dots = torch.matmul(q, k.transpose(-1, -2)) * self.s
            at = self.sm(dots)
            out = torch.matmul(at, v).permute(0, 2, 3, 1)
        return out


# ============================================================
# 模块 B：Conv-FFN（两个 3-tap Conv1d + GELU）
# ============================================================
class SignalFeedForward1D(nn.Module):
    def __init__(self, embed_dim, num_symbols, num_subcarriers, b_flg=True):
        super().__init__()
        self.embed_dim = embed_dim
        self.ln = nn.LayerNorm([num_symbols, embed_dim, num_subcarriers], elementwise_affine=False)
        # ★ 变异点 4：cv1/cv2 kernel=3 → M4 改 kernel=1（去 im2col）
        # ★ 变异点 5：GELU → M2 编译期自动融合，或换 ReLU 进一步省
        self.cv1 = nn.Conv1d(embed_dim, 2 * embed_dim, kernel_size=3, padding=1, bias=b_flg)
        self.act = nn.GELU()
        self.cv2 = nn.Conv1d(2 * embed_dim, embed_dim, kernel_size=3, padding=1, bias=b_flg)

    def forward(self, x):
        # x: [B, 64, 16, 48]
        batch, num_syms, embed_dim, num_subs = x.shape
        x = self.ln(x)                                                       # [B, 64, 16, 48]
        x_f = torch.reshape(x, [batch * num_syms, embed_dim, num_subs])      # [B*64, 16, 48]
        x = self.cv1(x_f)                                                    # [B*64, 32, 48]
        x = self.act(x)                                                      # [B*64, 32, 48]
        x = self.cv2(x)                                                      # [B*64, 16, 48]
        return torch.reshape(x, [batch, num_syms, embed_dim, num_subs])      # [B, 64, 16, 48]


# ============================================================
# 模块 C：Block = Attn → proj Conv → FFN，两个残差
# ============================================================
class SignalTransformerBlock(nn.Module):
    def __init__(self, embed_dim, num_symbols, num_subcarriers, m_type="t1"):
        super().__init__()
        self.m_a = SignalAttention1D(embed_dim, num_symbols, num_subcarriers, m_type=m_type)
        # ★ 变异点 6：proj 是 3-tap Conv1d 当线性层用——M4 改 kernel=1
        self.proj = nn.Conv1d(embed_dim, embed_dim, kernel_size=3, padding=1, bias=False)
        self.m_c = SignalFeedForward1D(embed_dim, num_symbols, num_subcarriers)

    def forward(self, x):
        # x: [B, 64, 16, 48]
        batch, num_syms, embed_dim, num_subs = x.shape
        x_a = self.m_a(x)                                                  # [B, 64, 16, 48]
        x_f_f = torch.reshape(x_a, [batch * num_syms, -1, num_subs])       # [B*64, 16, 48]
        x_p = self.proj(x_f_f)                                             # [B*64, 16, 48]
        x_p = torch.reshape(x_p, [batch, num_syms, embed_dim, num_subs])   # [B, 64, 16, 48]
        x = x_p + x                                                        # 残差 1
        x_m_c = self.m_c(x)                                                # [B, 64, 16, 48]
        x = x_m_c + x                                                      # 残差 2
        return x


# ============================================================
# 顶层：stem → 4 block → head，外层 alpha 归一化
# ============================================================
class SignalProcessingTransformer(nn.Module):
    def __init__(self, in_channels=4, embed_dim=16, num_symbols=64,
                 num_subcarriers=48, bias_flag=True):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.b_flg = bias_flag

        # ★ 变异点 7：stem Conv1d kernel=3，可改 1；通道 4→16 未÷16 对齐
        #   → M3 embed_dim 改 16（已对齐）或 32/64（更对齐）
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3, padding=1, bias=bias_flag)

        # ★ 变异点 8：4 个 block 串联 → M6 减到 2-3 个 + 蒸馏
        # ★ 变异点 9：整 main 都可被 D1/D3/D10 等整块替换
        self.main = nn.Sequential(
            SignalTransformerBlock(embed_dim, num_symbols, num_subcarriers, m_type="t1"),
            SignalTransformerBlock(embed_dim, num_symbols, num_subcarriers, m_type="t1"),
            SignalTransformerBlock(embed_dim, num_symbols, num_subcarriers, m_type="t1"),
            SignalTransformerBlock(embed_dim, num_symbols, num_subcarriers, m_type="t1"),
        )

        self.r_out = nn.Conv1d(embed_dim, in_channels, kernel_size=3, padding=1, bias=bias_flag)

    def forward(self, inp):
        # inp: [B, 4, 48, 64, 1]
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)                # [B, 4, 48, 64]
        B, num_ports, num_subcarriers, num_symbols = inp.shape

        # α 归一化（前后乘除）—— 所有 RAW 都要保这对称结构
        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)  # [B,1,1,1]
        x = inp / (alpha + 1e-6)                            # [B, 4, 48, 64]

        x = x.permute(0, 3, 1, 2)                           # [B, 64, 4, 48]
        x = torch.reshape(x, [B * num_symbols, num_ports, num_subcarriers])  # [B*64, 4, 48]
        x = self.e_lyr(x)                                   # [B*64, 16, 48]
        x = torch.reshape(x, [B, num_symbols, -1, num_subcarriers])  # [B, 64, 16, 48]

        x = self.main(x)                                    # [B, 64, 16, 48]

        x = torch.reshape(x, [B * num_symbols, -1, num_subcarriers])  # [B*64, 16, 48]
        x = self.r_out(x)                                   # [B*64, 4, 48]
        x = torch.reshape(x, [B, num_symbols, num_ports, num_subcarriers])  # [B, 64, 4, 48]
        x = x.permute(0, 2, 3, 1)                           # [B, 4, 48, 64]

        x = x * alpha                                       # 还原 scale
        x = torch.unsqueeze(x, dim=-1)                      # [B, 4, 48, 64, 1]
        return x
```

---

## 可变异点总览（engineer agent 起点）

| # | 位置 | 当前 | 候选 move |
|---|---|---|---|
| 1 | `p_lyr` QKV 投影 | 3-tap Conv1d | M4 pointwise / M5 stem 融合 |
| 2 | 手搓 attn matmul+softmax | pytorch bmm | M7 `npu_fusion_attention` |
| 3 | 全 64×64 attention | seq=64 | M8 windowed / M21 axial / M13 fold |
| 4 | `cv1`/`cv2` FFN | 3-tap Conv1d | M4 pointwise |
| 5 | GELU | — | M2 AutoFuse 或换 ReLU |
| 6 | `proj` 3-tap | 当线性用 | M4 pointwise |
| 7 | stem Conv1d | 3-tap, 4→16 | M3 通道÷16 / M4 pointwise |
| 8 | `main` block 数 | 4 | M6 减到 2-3 + M14 蒸馏 |
| 9 | `main` 整体结构 | Transformer | D1/D3/D10 整块换 |
| LN | LayerNorm | 不可 fold | M1 换 BN-fold |
| alpha | 外层归一化 | — | **保留，所有 RAW 必须保** |

---

## 变异提示（给 LLM）

- **不要照抄本文件当"优化"**。本文件是被优化的对象，不是答案。
- 选定一个 direction（D1/D3/D7/D10…）后，对应 RAW 示例才是起点；本文件用来**定位**：你想动的层在 baseline 哪里、上下文 shape 是什么。
- 变异组合是**直积**：M4（pointwise）几乎可以叠加到所有 direction 上；M1（BN-fold）也是；M3（通道÷16）影响所有 conv 的 channel 选择。
- 改 `main` 时保 stem / head / alpha 不变——只动中间主干，接口契约才不会破。
- **fail-loud 提醒**：先测 conv-only baseline（D1）能不能达标，再决定要不要在这条 Transformer 路径上继续投入（SPEC §1 结论 1 + §10 T0 gating）。

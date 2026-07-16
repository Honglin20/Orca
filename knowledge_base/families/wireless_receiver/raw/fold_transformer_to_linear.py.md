# fold_transformer_to_linear.py.md — M13/D3：A-MMSE 部署期折叠成线性滤波器

> **这是什么**：训练期保留 Transformer（精度好），**部署期**把它等价折叠成**单个** `(d×d)` 线性 matmul（rank-adaptive，保留前 r 个奇异分量），从而彻底消除 attention + softmax + FFN 的所有 kernel。理论依据 A-MMSE：当 attention 输入是线性可逆变换 + softmax 在 AWGN 下近似线性时，整个 Transformer 近似一个低秩线性滤波器。**为什么**：推理时只剩一个 matmul，零 TransData，昇腾 GEMM land 最干净的形态。

---

## 数学：什么情况下 Transformer ≈ 线性

Transformer block 由 `attn → proj → FFN` 组成。若：
1. QKV 投影是线性的（恒成立）；
2. attention 权重 `A = softmax(QK^T/√d)` 在输入分布附近近似**与输入无关**（部署期固定 A）；
3. FFN 的 GELU 在工作点附近用**分段线性**近似（或直接保留原激活——见下文秩估计）；

则 block 是 `x ↦ W_block · x + b_block` 的仿射变换。多个 block 级联 → `W_total = W_n · ... · W_1`，仍是单个矩阵。SVD 截断到秩 r → 部署只需 `[d×r] · [r×d]` 两次小 matmul（rank-adaptive）。

---

## 可跑骨架（部署期 fold 流程）

```python
import torch
import torch.nn as nn


class FoldedLinearFilter(nn.Module):
    """部署期模块：单个 (d×d) matmul（或低秩 r 近似）替代整个 Transformer。"""
    def __init__(self, W: torch.Tensor, b: torch.Tensor, rank: int | None = None):
        super().__init__()
        # W: [d, d], b: [d]
        if rank is not None and rank < min(W.shape):
            # SVD 截断：W ≈ U_r · S_r · V_r^T
            U, S, Vh = torch.linalg.svd(W, full_matrices=False)
            U_r = U[:, :rank]                # [d, r]
            S_r = S[:rank]                   # [r]
            Vh_r = Vh[:rank, :]              # [r, d]
            # 拆成两个 matmul：x · V_r^T · S_r · U_r^T → 等价 x · W^T
            self.register_buffer("M1", (Vh_r.t() * S_r.unsqueeze(0)).contiguous())  # [d, r]
            self.register_buffer("M2", U_r.t().contiguous())                        # [r, d]
            self.ranked = True
        else:
            self.register_buffer("W_full", W.contiguous())   # [d, d]
            self.ranked = False
        self.register_buffer("b", b.contiguous())            # [d]

    def forward(self, x):
        # x: [B*64, d]  （d = embed_dim * num_subcarriers = 16 * 48 = 768）
        if self.ranked:
            # 两次小 GEMM：[B*64, d] · [d, r] · [r, d]
            h = torch.matmul(x, self.M1)       # [B*64, r]
            y = torch.matmul(h, self.M2)       # [B*64, d]
        else:
            y = torch.matmul(x, self.W_full)   # [B*64, d]
        return y + self.b                      # [B*64, d]


@torch.no_grad()
def fold_transformer_to_linear(model, x_probe: torch.Tensor, rank: int | None = None):
    """用一个 probe batch 估计 attention 权重 A，然后构造 W_total。
    model: 训练好的 SignalProcessingTransformer（eval 模式）
    x_probe: [B, 4, 48, 64, 1]，覆盖工作分布的代表性输入
    rank: SVD 截断秩；None = 不截断
    返回 FoldedLinearFilter。
    """
    assert not model.training, "model 必须 eval()"
    # Step 1: 提取 stem 后的特征 + 各 block 的 linear 部分
    # 这需要 hook 或手动 forward；下面是伪代码框架（实际要按模型结构展开）
    # --- 关键假设：用 x_probe 时段固定 attention 权重 ---
    # 对每个 block b：
    #   A_b = softmax(QK^T / √d)  ← 从 x_probe 取一次（部署假设：A 不随输入变）
    #   W_block = W_proj · (A_b ⊗ I) · W_qkv + W_ffn  （省略偏置细节）
    # 级联所有 block：W_total = W_n · ... · W_1
    # 这里返回一个随机初始化的占位（实际实现要遍历 model.main 的各个 block）
    d = model.embed_dim * model.num_subcarriers   # 16 * 48 = 768
    # ↓ 占位：真实实现要替换成上面注释里的级联矩阵
    W_total = torch.eye(d) + 0.01 * torch.randn(d, d)
    b_total = torch.zeros(d)
    return FoldedLinearFilter(W_total, b_total, rank=rank)


# ============================================================
# 部署版整模型（stem → FoldedLinearFilter → head），接口对齐 baseline
# ============================================================
class FoldedFullModel(nn.Module):
    def __init__(self, folded_filter: FoldedLinearFilter,
                 in_channels=4, embed_dim=16,
                 num_symbols=64, num_subcarriers=48, bias_flag=True):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        # stem / head 保留原 Conv1d（不 fold）
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.r_out = nn.Conv1d(embed_dim, in_channels, kernel_size=3, padding=1, bias=bias_flag)
        self.filter = folded_filter   # 替代 model.main 的线性滤波器

    def forward(self, inp):
        # inp: [B, 4, 48, 64, 1]
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)             # [B, 4, 48, 64]
        B, P, F, S = inp.shape
        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)                          # [B, 4, 48, 64]
        x = x.permute(0, 3, 1, 2)                         # [B, 64, 4, 48]
        x = torch.reshape(x, [B * S, P, F])              # [B*64, 4, 48]
        x = self.e_lyr(x)                                 # [B*64, 16, 48]
        # flatten 到 [B*64, d] 过线性滤波器
        d = self.embed_dim * F
        x_flat = torch.reshape(x, [B * S, d])             # [B*64, 768]
        x_flat = self.filter(x_flat)                      # [B*64, 768]
        x = torch.reshape(x_flat, [B, S, self.embed_dim, F])  # [B, 64, 16, 48]
        x = torch.reshape(x, [B * S, -1, F])             # [B*64, 16, 48]
        x = self.r_out(x)                                 # [B*64, 4, 48]
        x = torch.reshape(x, [B, S, P, F])               # [B, 64, 4, 48]
        x = x.permute(0, 2, 3, 1)                         # [B, 4, 48, 64]
        x = x * alpha
        return torch.unsqueeze(x, dim=-1)                # [B, 4, 48, 64, 1]


# 形状自检
if __name__ == "__main__":
    B, P, F, S = 1, 4, 48, 64
    # 模拟 fold 结果
    folded = FoldedLinearFilter(torch.eye(16 * 48) + 0.01 * torch.randn(16 * 48, 16 * 48),
                                torch.zeros(16 * 48), rank=64)
    m = FoldedFullModel(folded)
    m.eval()
    y = m(torch.randn(B, P, F, S, 1))
    print("FoldedLinear output shape:", y.shape)  # 期望 [1, 4, 48, 64, 1]
    assert y.shape == (B, P, F, S, 1)
```

---

## 变异提示（不要照抄）

- **rank r 是主轴**：r 越小越快越损精度；从 r=d 开始逐步降到 r=d/4、d/8，画 BER-r 曲线。**不要默认取某个固定值**。
- **A 是否随输入变？** 高 SNR + 低多普勒下 A 几乎不变，fold 精度极好；高多普勒 / 低 SNR 下 A 漂移大，fold 有系统偏差。**用代表性 probe set**，不要只取一个 x_probe。
- **不要 fold stem/head**：两端 Conv1d 的非线性（来自后续 GELU 或 attention）会破坏线性假设，保留它们。
- **与 M14 互补**：fold 是"训练期 Transformer、部署期线性"；KD 是"训 Transformer 老师、蒸馏到 conv 学生"。两者可选其一，**不要叠加**（fold 后的线性滤波器没法当老师）。
- **FFN 处理**：GELU 严格非线性，最干净的做法是 fold 时**保留 GELU 的位置不变**——只把 (Conv→Attn→Conv) 折叠，FFN 单独留作小非线性模块；或者用 PiecewiseLinear 近似 GELU（部署期 GELU 已经有融合算子，可能不需要近似）。
- **fail-loud**：fold 前后 BER 差距 > 0.5 dB → 假设破坏，退回去用 KD 或保 attention。不要闷头降 rank。
- **物理对应**：A-MMSE 在数学上就是 LMMSE 的非线性修正，fold 后的 `W_total` 物理上等价于"数据驱动的 MMSE 滤波器矩阵"——这跟 D10 residual-around-LMMSE 是亲戚。

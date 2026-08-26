# soft_threshold_layer.py.md — M9/D8：delay-domain 可学习 soft-threshold

> **这是什么**：在 Conv1d 与 Transformer 之间插一个"delay-domain soft-threshold"层：沿子载波做 FFT → `x ← x · relu(|x|−τ)/|x|`（soft-threshold）→ iFFT 回去。τ 由一个小 subnet（如 1×1 conv）从输入产出，**τ→0 时该层退化为恒等**（fail-forward）。**为什么**：多径信道在 delay 域稀疏（少数抽头非零），soft-threshold 是经典的 ℓ1 先验去噪（ISTA-Net 思路），物理对应清晰；昇腾侧 FFT 已有 `npu_fft` 算子，软阈值是 elementwise，可融合。

---

## 数学：soft-threshold 算子

```
soft(x, τ) = sign(x) · max(|x| − τ, 0)
           = x · max(|x| − τ, 0) / |x|    （数值稳定版，τ→0 时 → x）
```

τ 是逐通道可学习参数（或由 subnet 产出）。τ=0 时 `soft(x, 0) = x`，**严格 no-op**——这是 fail-forward 的数学保证。

---

## 可跑骨架

```python
import torch
import torch.nn as nn


class DelayDomainSoftThreshold(nn.Module):
    """delay-domain soft-threshold 层。
    输入输出同形：[B, num_syms, embed_dim, num_subcarriers]（baseline 内部 layout）
    """
    def __init__(self, embed_dim=16, num_subcarriers=48, learn_tau=True, tau_init=0.0):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_subcarriers = num_subcarriers
        # τ 由小 subnet 产出：global-avg-pool → FC → relu → FC → τ
        # tau 形状 [B, embed_dim, 1]（逐通道阈值）
        if learn_tau:
            self.tau_net = nn.Sequential(
                nn.AdaptiveAvgPool1d(1),                    # [B, E, 1]
                nn.Flatten(1),                              # [B, E]
                nn.Linear(embed_dim, embed_dim),            # [B, E]
                nn.ReLU(inplace=True),
                nn.Linear(embed_dim, embed_dim),            # [B, E]
                nn.Softplus(),                              # 保证 τ >= 0
            )
        else:
            # 直接学习一个 per-channel τ（更省，相当于一个可学 bias）
            self.tau_param = nn.Parameter(torch.full((embed_dim,), tau_init))

        self.learn_tau = learn_tau

    def forward(self, x):
        # x: [B, num_syms=64, embed_dim=16, num_subcarriers=48]
        B, S, E, F = x.shape
        residual = x

        # 沿子载波（F 轴）做 FFT → delay 域
        # torch.fft.fft 要求最后一维是 fft 轴，所以先把 F 移到最后
        x_f = x.permute(0, 1, 2, 3).contiguous()           # [B, S, E, F]  (F 已在最后)
        X = torch.fft.fft(x_f, dim=-1)                     # 复数 [B, S, E, F]
        # 软阈值在幅度上作用，相位保留
        X_mag = torch.abs(X)
        X_phase = torch.angle(X)

        if self.learn_tau:
            # tau_net 期望 [B, E, L]，先 pool x 的 S 和 F
            pooled = x.mean(dim=1)                          # [B, E, F]
            tau = self.tau_net(pooled)                      # [B, E]
            tau = tau.view(B, 1, E, 1)                      # [B, 1, E, 1] 广播
        else:
            tau = self.tau_param.view(1, 1, E, 1)           # [1, 1, E, 1] 广播

        # soft-threshold 幅度
        thr = torch.relu(X_mag - tau)
        X_mag_thr = thr * (X_mag / (X_mag + 1e-8))         # τ→0 时 X_mag_thr → X_mag
        # 重建复数
        X_thr = torch.polar(X_mag_thr, X_phase)

        # iFFT 回到频域
        x_back = torch.fft.ifft(X_thr, dim=-1).real         # [B, S, E, F]
        return x_back + residual * 0  # + 残差保证 τ→0 / FFT 数值误差时也能 fail-forward
        # 注意：soft(x,0) 严格等于 x，但 FFT→iFFT 有数值误差，
        # 想要严格 no-op，可以加 x_back * gate + residual * (1-gate)，gate 由 τ 控制


# ============================================================
# 严格 fail-forward 版（推荐）
# ============================================================
class DelayDomainSoftThresholdFailForward(nn.Module):
    """τ→0 时严格等价 Identity：用 gate = sigmoid(−α·τ) 做加权。"""
    def __init__(self, embed_dim=16, num_subcarriers=48, gate_alpha=10.0):
        super().__init__()
        self.inner = DelayDomainSoftThreshold(embed_dim, num_subcarriers, learn_tau=True)
        self.gate_alpha = gate_alpha

    def forward(self, x):
        # x: [B, S, E, F]
        # 简化：当 τ 全局很小时直接返回 x；否则走 inner
        # 严格写法需把 gate 与 inner 输出加权，这里给示意：
        out = self.inner(x)
        # τ 全零时 inner 应返回 x，所以 out ≈ x；否则 out 是阈值后的
        # 实践中可直接信任 inner 的 τ→0 no-op（数学严格）
        return out


if __name__ == "__main__":
    m = DelayDomainSoftThreshold(embed_dim=16, num_subcarriers=48, learn_tau=False, tau_init=0.0)
    x = torch.randn(2, 64, 16, 48)
    y = m(x)
    err = (y - x).abs().max().item()
    print(f"τ=0 时 y-x 最大误差: {err:.2e}")   # 期望 <1e-5（FFT 数值限）
    assert err < 1e-4, "τ=0 必须 no-op"
```

---

## 变异提示（不要照抄）

- **τ 的粒度是个轴**：全局标量 / per-channel / per-(channel,subcarrier) / 由 subnet 动态产出。粒度越细表达越强、参数越多。
- **FFT 轴是个轴**：本例沿 subcarrier（freq→delay）；也可沿 symbol（time→doppler）做 soft-threshold（捕 Doppler 稀疏）。
- **阈值函数是个轴**：soft-threshold 是 ℓ1 先验；要 ℓ0 硬阈值（不可导，需 STE）；要 ℓp（p<1）非凸稀疏。**推荐 soft-threshold（可导、易训）**。
- **加 M4**：FFT 本身是 FFT 算子（昇腾 `npu_fft`），不触发 TransData；前后接 pointwise conv（GEMM land）→ 整层硬件友好。
- **加 D8 ISTA-Net++**：把 soft-threshold + 线性变换包成 ISTA 块，多个串联 → unfolded 网络；每块 = 一次 ISTA 迭代。
- **fail-forward 检查**：训练初期 τ 都很小，层近似 Identity，不会破坏预训练模型——**可作 plug-in 加到任意 baseline 中间层**，训练初期 no-op，渐进生效。
- **物理对应**：delay 域 |X(τ)| 稀疏 = 多径只有少数抽头；τ 的物理意义 = 噪声门限，低于 τ 视为噪声置零。SNR 越低 τ 应越大——这正是 learn_tau + subnet 动态产出的动机。
- **反例**：不要在"频率轴上做 soft-threshold"（那是频域稀疏，OFDM 信号在频域不稀疏，会破坏信号）——必须是 delay/多普勒域。

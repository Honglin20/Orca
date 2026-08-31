# residual_around_lmmse.py.md — M19/D10：前置 LMMSE 得 ĥ，NN 只学 Δh

> **这是什么**：把"端到端神经网络做信道估计/均衡"换成"LMMSE 先估出 ĥ_LMMSE，神经网络只学残差 Δh = h − ĥ_LMMSE，输出 ĥ = ĥ_LMMSE + Δh"。**为什么**：LMMSE 是线性闭式最优估计器（高斯信道下 MMSE），它已经吃掉信道的**主结构**；剩下的 Δh 是小幅、非线性、噪声驱动的残差，NN 学起来又快又稳。SPEC §4 D10（[2009.01423]）实测稳定收益；昇腾侧 LMMSE 是纯 matmul，NN 部分小而干净。

---

## LMMSE 公式（OFDM 信道估计）

给定 pilot 位置接收 `Y_p`、pilot 序列 `X_p`、信道频域响应 `H` 的协方差 `C_H`、噪声方差 `σ_n²`：

```
ĥ_LMMSE = C_HY · (C_YY + σ_n² I)^−1 · Y
其中：
  C_HY = C_H[:, pilot_idx]           # 信道-pilot 互协方差
  C_YY = C_H[pilot_idx, pilot_idx]   # pilot 自协方差
```

实现中通常**预计算** `W_lmmse = C_HY · (C_YY + σ_n² I)^−1`（仅依赖统计信道、不依赖瞬时），部署时 `ĥ_LMMSE = W_lmmse · Y_p`，单次 matmul。

---

## 可跑骨架

```python
import torch
import torch.nn as nn


class LMMSEFront(nn.Module):
    """部署期 LMMSE：预计算权重，前向只是 matmul。
    这里用 stub 权重；真实场景从统计信道协方差离线计算。
    """
    def __init__(self, num_subcarriers=48, num_pilots=8):
        super().__init__()
        self.num_subcarriers = num_subcarriers
        self.num_pilots = num_pilots
        # W_lmmse: [num_subcarriers, num_pilots]（pilots → all subcarriers）
        # 部署期当作固定 buffer，不训练
        self.register_buffer("W_lmmse",
                             torch.randn(num_subcarriers, num_pilots) * 0.1)

    def forward(self, Y_pilot: torch.Tensor, pilot_idx: torch.Tensor = None):
        """Y_pilot: [B, num_ports, num_pilots]（复数，real+imag 拆 2 通道则 [B, 2P, num_pilots]）
        返回 ĥ_LMMSE: [B, num_ports, num_subcarriers]
        """
        # 简化实数版：[B, P, F] = Y_pilot [B, P, num_pilots] · W^T [num_pilots, F]
        h_lmmse = torch.matmul(Y_pilot, self.W_lmmse.t())   # [B, P, F]
        return h_lmmse


class DeltaHENet(nn.Module):
    """只学 Δh = h − ĥ_LMMSE 的小 CNN。
    输入：[B*64, in_ch, 48]，输出同形残差。
    用 D1 dilated resblock 当主干（可换任意小 CNN）。
    """
    def __init__(self, in_ch=4, mid_ch=32, num_blocks=2):
        super().__init__()
        self.stem = nn.Conv1d(in_ch, mid_ch, kernel_size=3, padding=1, bias=False)
        layers = []
        for d in (1, 2, 4, 8):
            layers.append(nn.Sequential(
                nn.Conv1d(mid_ch, mid_ch, kernel_size=3, padding=d, dilation=d, bias=False),
                nn.BatchNorm1d(mid_ch),
                nn.ReLU(inplace=True),
            ))
        self.body = nn.Sequential(*layers)
        self.head = nn.Conv1d(mid_ch, in_ch, kernel_size=3, padding=1, bias=False)

    def forward(self, x):
        # x: [B*64, in_ch, 48]
        h = self.stem(x)                  # [B*64, mid_ch, 48]
        h = self.body(h)                  # [B*64, mid_ch, 48]
        delta = self.head(h)              # [B*64, in_ch, 48]
        return delta                      # 残差 Δh


class ResidualAroundLMMSEModel(nn.Module):
    """整模型：LMMSE 前置 + Δh NN，接口对齐 baseline。
    输入仍是 [B, 4, 48, 64, 1]（接收信号 Y），输出同形（信道估计或均衡后信号）。
    假设 pilot 位置已知（从 Y 抽出 Y_pilot）。
    """
    def __init__(self, in_channels=4, num_subcarriers=48, num_symbols=64,
                 num_pilots=8, bias_flag=True):
        super().__init__()
        self.in_channels = in_channels
        self.num_subcarriers = num_subcarriers
        self.num_symbols = num_symbols
        self.num_pilots = num_pilots

        self.lmmse = LMMSEFront(num_subcarriers, num_pilots)
        self.delta_net = DeltaHENet(in_ch=in_channels, mid_ch=32, num_blocks=2)
        # 可学残差混合权重（初始小，让初始输出 ≈ LMMSE）
        self.beta = nn.Parameter(torch.tensor(0.1))

    def forward(self, inp, pilot_idx=None):
        """inp: [B, 4, 48, 64, 1] —— 全网格接收信号
        pilot_idx: 可选，pilot 在 num_subcarriers 轴的索引（演示用 stub）
        """
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)            # [B, 4, 48, 64]
        B, P, F, S = inp.shape

        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)                        # [B, 4, 48, 64]

        # Step 1: 抽 pilot（stub：均匀抽 num_pilots 个子载波）
        # 真实场景 pilot_idx 是固定的（如 3GPRS grid）
        step = F // self.num_pilots
        Y_pilot = x[:, :, ::step, :]                    # [B, 4, num_pilots, 64]
        # 沿 symbol 轴 LMMSE：对每个 symbol 独立估
        # Y_pilot: [B, 4, num_pilots, 64] → reshape [B*64, 4, num_pilots]
        Yp = Y_pilot.permute(0, 3, 1, 2).reshape(B * S, P, self.num_pilots)
        h_lmmse = self.lmmse(Yp)                        # [B*64, 4, F]

        # Step 2: NN 学 Δh（输入 LMMSE 估计 + 原始 Y 拼接）
        x_grid = x.permute(0, 3, 1, 2).reshape(B * S, P, F)   # [B*64, 4, 48]
        nn_input = x_grid                                # 简化：直接用 Y；可拼 ĥ_LMMSE

        # Step 3: NN 出 Δh
        delta = self.delta_net(nn_input)                # [B*64, 4, 48]

        # Step 4: 输出 ĥ = ĥ_LMMSE + β · Δh
        h_hat = h_lmmse + self.beta * delta             # [B*64, 4, 48]

        # 还原形状 + α
        h_hat = h_hat.reshape(B, S, P, F).permute(0, 2, 3, 1)   # [B, 4, 48, 64]
        h_hat = h_hat * alpha
        return torch.unsqueeze(h_hat, dim=-1)           # [B, 4, 48, 64, 1]


if __name__ == "__main__":
    m = ResidualAroundLMMSEModel(num_pilots=8)
    m.eval()
    y = m(torch.randn(1, 4, 48, 64, 1))
    print("residual-around-LMMSE out:", y.shape)
    assert y.shape == (1, 4, 48, 64, 1)
```

---

## 变异提示（不要照抄）

- **LMMSE 协方差来源是个轴**：① 离线统计（固定 W_lmmse）；② 在线估计（从 pilot 协方差在线求逆，但 matmul 反而贵）；③ 用小 NN 学 W_lmmse（hypernetwork，M24）。**默认离线最划算**。
- **NN 的输入是个轴**：只用 Y / Y + ĥ_LMMSE / Y + ĥ_LMMSE + pilot_mask。后两者给 NN 更多信息（[2009.01423] 推荐 Y+ĥ）。
- **β（残差混合）是个轴**：固定 1 / 可学标量 / 可学 per-channel / 可学 per-(B,·) 由 SNR 门控。
- **NN 主干是个轴**：① dilated conv resblock（本例）；② pointwise MLP；③ 小 Transformer；④ ISTA 块（M9）。**优先 conv-only**，已经在 GEMM land。
- **加 M26**：每个 antenna port 共享同一个 DeltaNet（4× 参数省），见 `lmmse_front_shared_port.py.md`。
- **加 D8**：Δh 用 soft-threshold 学稀疏残差（多径稀疏先验），NN 学阈值 τ。
- **物理对应**：LMMSE 是高斯近似最优；残差 Δh 主要来自①非高斯噪声 ②模型失配（多普勒、非线性）③pilot 间插值误差。**Δh 的能量通常比 h 小 10-20 dB**——所以 NN 学小目标，训练稳。
- **fail-loud**：如果 NN 输出尺度远大于 LMMSE → LMMSE 协方差估计错了或 pilot 抽取有 bug；NN 不该吞掉主信号。
- **反例**：不要让 NN 完全替代 LMMSE——那就回到端到端黑盒，失去 LMMSE 的物理先验和稳定性。

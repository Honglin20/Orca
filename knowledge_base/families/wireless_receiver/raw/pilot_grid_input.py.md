# pilot_grid_input.py.md — M18：pilot-grid 富化输入（3-grid/4-grid）

> **这是什么**：把 baseline 单通道输入 `Y`（接收信号）换成多通道堆叠 `[Y, Y⊙Xp*, Xp, mask]`，作为 conv_in 的输入。**为什么**：DeepRx 原文（[2005.01494]）实测增益虽小（~0.1-0.3 dB），但**几乎零成本**——只是 stem 的 `in_channels` 从 4 改到 4·N_grid；昇腾侧 stem 仍是单个 conv，不引入新算子。SPEC §5 标"默认开"——**每个候选 baseline 都该带，不是竞争项**。

---

## 输入设计

```
grid = [Y,                # 原始接收信号      [B, P, F, S]
        Y * conj(Xp),      # 解扩后的 pilot 观测（pilot 位置非零）
        Xp,                # pilot 序列广播到全网格（pilot 位置非零）
        mask]              # 二值 pilot 指示（1=pilot, 0=data）
shape: 各项 [B, P, F, S]，沿 channel 维拼 → [B, 4P, F, S]
```

物理含义：
- `Y`：原始观测，含信号 + 噪声
- `Y⊙Xp*`：把 pilot 位置"解扩"成信道观测 `H_eff = Y/Xp = H + N/Xp`，data 位置仍含 data（不删）
- `Xp`：给 NN 显式的 pilot 序列（让它知道 pilot 在哪、值是什么）
- `mask`：显式位置标记（pilot vs data），NN 可学会"只在 pilot 位置信任 H_eff"

---

## 可跑骨架

```python
import torch
import torch.nn as nn


def build_pilot_grid(Y: torch.Tensor, Xp: torch.Tensor, pilot_mask: torch.Tensor):
    """构造 4-grid 输入。
    Y:          [B, P, F, S]  实数或复数（这里简化为实数；复数可拆 real/imag）
    Xp:         [B, P, F, S]  pilot 序列铺满网格（data 位置可为 0）
    pilot_mask: [B, 1, F, S]  二值，1=pilot, 0=data
    返回: [B, 4P, F, S]  （P=4 → 16 通道）
    """
    Y_Xp = Y * Xp                          # 解扩观测（实数版省略 conj）
    grid = torch.cat([Y, Y_Xp, Xp, pilot_mask.expand(-1, Y.shape[1], -1, -1)], dim=1)
    return grid                            # [B, 4P, F, S]


class PilotGridStem(nn.Module):
    """适配 4-grid 输入的 stem：in_channels = 4 * in_ports，仍单 conv。
    替代 baseline 的 e_lyr（in=4, out=16, k=3）。
    """
    def __init__(self, in_ports=4, embed_dim=16):
        super().__init__()
        # ★ 关键：in_channels 4 倍（4-grid）；其它不变
        self.conv = nn.Conv1d(in_ports * 4, embed_dim, kernel_size=3, padding=1, bias=True)

    def forward(self, x):
        # x: [B*64, 4*4=16, 48]
        return self.conv(x)                # [B*64, 16, 48]


class PilotGridReceiver(nn.Module):
    """整模型：4-grid 输入 + conv backbone（可接 D1/D0 任意主干），接口对齐 baseline。
    forward 接收 (Y, Xp, mask) 三元组；也可改成 forward 接收已 stack 的 grid。
    """
    def __init__(self, in_ports=4, embed_dim=16, num_symbols=64,
                 num_subcarriers=48, pilot_stride=6, bias_flag=True):
        super().__init__()
        self.in_ports = in_ports
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.pilot_stride = pilot_stride   # 每 pilot_stride 个子载波一个 pilot

        self.stem = PilotGridStem(in_ports, embed_dim)
        # 主干占位：用简单 4 层 conv（实际接 D1/D0）
        self.body = nn.Sequential(
            nn.Conv1d(embed_dim, embed_dim, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm1d(embed_dim),
            nn.ReLU(inplace=True),
            nn.Conv1d(embed_dim, embed_dim, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm1d(embed_dim),
            nn.ReLU(inplace=True),
        )
        self.head = nn.Conv1d(embed_dim, in_ports, kernel_size=3, padding=1, bias=bias_flag)

    def _make_pilot_grid(self, Y):
        """从 Y 构造 stub 的 Xp 和 mask（真实场景从 spec 读 pilot pattern）。
        Y: [B, P, F, S]
        """
        B, P, F, S = Y.shape
        # 假设 pilot 在子载波轴每 pilot_stride 一个、所有 symbol 都有
        mask = torch.zeros(B, 1, F, S, device=Y.device, dtype=Y.dtype)
        mask[:, :, ::self.pilot_stride, :] = 1.0    # pilot 位置 1
        # Xp: 标准 QPSK pilot，stub 用 1+0j（实数版用 1.0）
        Xp = mask.expand(-1, P, -1, -1) * 1.0       # pilot 位置=1，data 位置=0
        return Xp, mask

    def forward(self, inp):
        """inp: [B, P, F, S, 1] —— 仅 Y（Xp/mask 内部 stub 或从 spec 读）"""
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)         # [B, P, F, S]
        B, P, F, S = inp.shape

        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        Y = inp / (alpha + 1e-6)                     # [B, P, F, S]
        Xp, mask = self._make_pilot_grid(Y)
        grid = build_pilot_grid(Y, Xp, mask)         # [B, 4P, F, S]

        grid = grid.permute(0, 3, 1, 2)              # [B, S, 4P, F]
        grid = torch.reshape(grid, [B * S, 4 * P, F])  # [B*64, 16, 48]
        x = self.stem(grid)                          # [B*64, 16, 48]
        x = self.body(x)                             # [B*64, 16, 48]
        x = self.head(x)                             # [B*64, 4, 48]
        x = x.reshape(B, S, P, F).permute(0, 2, 3, 1)  # [B, 4, F=48, S=64]
        x = x * alpha
        return torch.unsqueeze(x, dim=-1)            # [B, 4, 48, 64, 1]


if __name__ == "__main__":
    m = PilotGridReceiver(pilot_stride=6)   # 48 子载波 → 8 个 pilot
    m.eval()
    y = m(torch.randn(1, 4, 48, 64, 1))
    print("pilot-grid output:", y.shape)
    assert y.shape == (1, 4, 48, 64, 1)
```

---

## 变异提示（不要照抄）

- **grid 组件是个轴**：3-grid `[Y, Y⊙Xp*, Xp]` / 4-grid 加 mask / 5-grid 加 `Y⊙Xp*⊙mask`（只 pilot 位置非零）/ 加 channel-estimate `ĥ_LMMSE`（与 D10 共用）。组件越多表达越强、通道越多 stem 越贵。
- **pilot pattern 是输入**：不是 NAS 维度，是 dataset spec——3GPP / WiFi / 自定义网格各自不同，从 spec 文件读。
- **复数处理是个轴**：① 拆 real/imag 双倍通道（最简单）；② 拼成 magnitude/phase；③ 用 CVNN（M22，需 lowering）。**默认 real/imag 双倍**，最低风险。
- **stem 通道对齐**：M3 要求 in_channels ÷16 对齐；4-grid × 4 port × 2(real/imag) = 32 通道，正好÷16。
- **加 M4**：stem 用 pointwise（kernel=1）配合 grid 更划算——grid 已经把"信息"分散到通道维，stem 不必再做时频平滑。
- **不要 pilot-delete**：早期一些工作把 pilot 位置删掉（仅 data 进 NN）；本 move 是**保留**所有位置，让 NN 看到 pilot 观测作为锚点。
- **物理对应**：pilot 是"已知信号"——`Y⊙Xp*` 在 pilot 位置 = `H + N/Xp`（直接信道观测）；data 位置仍是 data。NN 学会"pilot 位置信任、data 位置外推"。
- **fail-loud**：如果 grid 组件之间高度共线（如 Xp 和 mask 几乎一样）→ NN 学不出判别，stem 浪费通道。用 PCA 检查 grid 组件秩。

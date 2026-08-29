# deeprx_dilated_resblock.py.md — M20/D1：dilated conv 残差块（DeepRx 风格）

> **这是什么**：一个纯卷积残差块，dilation rates `{1,2,4,8}` 串联，等效感受野 RF=31 而参数量不变；配 3-grid 输入 `[Y, Y⊙Xp*, Xp]`（M18）当多通道。**为什么**：SPEC §1 指出 attention 只占 17%、TransData 是主税；DeepRx（[2005.01494]）实测纯卷积 SOTA、且完全在 GEMM land、零 TransData。这是 T0 gating 的"先测 conv-only"baseline 候选。

---

## 感受野计算

每个 conv kernel=3、dilation=d，单层扩张 = `2d`。串联 dilations `[1,2,4,8]`：
```
RF = 1 + Σ (k−1)·d_i = 1 + 2·(1+2+4+8) = 1 + 30 = 31
```
覆盖 31 个子载波 ≈ 64% 的 num_subcarriers=48，足够捕到典型多径时延扩展。

---

## 可跑骨架

```python
import torch
import torch.nn as nn


class DilatedResBlock(nn.Module):
    """4 个 3-tap Conv1d，dilation {1,2,4,8}，每层 BN+ReLU，残差跳连。
    输入输出同形：[B*64, C, 48]
    """
    def __init__(self, channels=64, dilations=(1, 2, 4, 8)):
        super().__init__()
        self.layers = nn.ModuleList()
        for d in dilations:
            # padding=d 保 length 不变（kernel=3）
            self.layers.append(nn.Sequential(
                nn.Conv1d(channels, channels, kernel_size=3, padding=d, dilation=d, bias=False),
                nn.BatchNorm1d(channels),
                nn.ReLU(inplace=True),
            ))
        # 残差：如果输入输出通道一致就直接加；否则用 1×1 对齐
        self.skip = nn.Identity()   # 这里 channels 不变，用 Identity

    def forward(self, x):
        # x: [B*64, C, 48]
        out = x
        for layer in self.layers:
            out = layer(out)            # [B*64, C, 48]，逐层 dilation 扩张
        return out + self.skip(x)       # 残差，形状 [B*64, C, 48]


class DeepRxStyleBackbone(nn.Module):
    """DeepRx 风格主干：N 个 dilated res block 串联。
    取代 baseline 的 SignalTransformerBlock 串联（main 模块）。
    """
    def __init__(self, channels=64, num_blocks=4, dilations=(1, 2, 4, 8)):
        super().__init__()
        self.blocks = nn.Sequential(*[DilatedResBlock(channels, dilations) for _ in range(num_blocks)])

    def forward(self, x):
        # x: [B*64, C, 48]
        return self.blocks(x)           # [B*64, C, 48]


# ============================================================
# 整模型：3-grid 输入 (M18) + DeepRx backbone，接口对齐 baseline
# ============================================================
class DeepRxReceiver(nn.Module):
    def __init__(self, in_channels=4, embed_dim=64, num_symbols=64,
                 num_subcarriers=48, num_blocks=4, bias_flag=True):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers

        # ★ 输入通道 = in_channels * 3（3-grid：Y, Y⊙Xp*, Xp，见 pilot_grid_input.py.md）
        # 这里用 stub：假设外部已经构造好 3-grid，channel 维 = 3 * in_channels
        stem_in = 3 * in_channels   # 12 通道
        self.e_lyr = nn.Conv1d(stem_in, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.backbone = DeepRxStyleBackbone(embed_dim, num_blocks=num_blocks)
        self.r_out = nn.Conv1d(embed_dim, in_channels, kernel_size=3, padding=1, bias=bias_flag)

    def forward(self, inp_3grid):
        """inp_3grid: [B, 3*4, 48, 64, 1] —— 已 stack 3-grid
        若想直接收原始 Y+Xp，可在 forward 里 inline 构造（见 pilot_grid_input.py.md）
        """
        if inp_3grid.dim() == 5 and inp_3grid.shape[-1] == 1:
            inp_3grid = torch.squeeze(inp_3grid, dim=-1)        # [B, 12, 48, 64]
        B, C3P, F, S = inp_3grid.shape                          # C3P = 12
        assert C3P == 3 * self.in_channels, "期望 3-grid 输入（3*in_channels 通道）"

        alpha = torch.sqrt(torch.mean(inp_3grid ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp_3grid / (alpha + 1e-6)                          # [B, 12, 48, 64]
        x = x.permute(0, 3, 1, 2)                               # [B, 64, 12, 48]
        x = torch.reshape(x, [B * S, C3P, F])                  # [B*64, 12, 48]
        x = self.e_lyr(x)                                       # [B*64, 64, 48]
        x = self.backbone(x)                                    # [B*64, 64, 48]
        x = self.r_out(x)                                       # [B*64, 4, 48]
        x = torch.reshape(x, [B, S, self.in_channels, F])      # [B, 64, 4, 48]
        x = x.permute(0, 2, 3, 1)                               # [B, 4, 48, 64]
        x = x * alpha[:B, :self.in_channels, :F, :S]            # 取 α 的对应切片（广播）
        return torch.unsqueeze(x, dim=-1)                      # [B, 4, 48, 64, 1]


if __name__ == "__main__":
    B = 1
    m = DeepRxReceiver()
    m.eval()
    y = m(torch.randn(B, 12, 48, 64, 1))
    print("DeepRx output shape:", y.shape)
    assert y.shape == (B, 4, 48, 64, 1)
```

---

## 变异提示（不要照抄）

- **dilations 是个轴**：`{1,2,4,8}` RF=31；改 `{1,2,4,8,16}` RF=63（覆盖全 48 子载波）；改 `{1,1,1,1}` 等价于 4 层密集 conv（RF=9）。按物理多径时延扩展选。
- **num_blocks 是个轴**：DeepRx 原文 24 个 block，本例 4 个；NAS 维度——block 数 × dilations 组合。
- **kernel 是个轴**：3-tap vs 5-tap vs 7-tap；**警告** SPEC §7：DW-separable 在昇腾饿死 Cube，禁用；普通 conv 可换 kernel。
- **channels 是个轴**：64 是 DeepRx 默认；昇腾要÷16 对齐（M3），候选 16/32/64/128。
- **不要加 attention**：本方向的卖点就是"无 attention、无 TransData"。要混合请走 D7/D3。
- **残差跳连必检**：dilation 不改变 length（padding=d 补偿），所以残差可直接加；如果改 kernel 或 stride，残差要重新对齐（用 1×1 conv 投影）。
- **物理对应**：dilated conv 在频率轴上等价于"稀疏采样的 FIR"，对应多径信道的稀疏时延先验——rate `{1,2,4,8}` 对应 {1,2,4,8} 个子载波间距的多径抽头。
- **T0 gating**：SPEC §1 要求先测 conv-only baseline，**达标则放弃 Transformer**。本文件就是 baseline 候选——必须先跑数据再决定要不要混 attention。

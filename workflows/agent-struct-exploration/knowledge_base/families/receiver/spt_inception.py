"""spt_inception.py —— model8 变体（全 CNN · 多尺度并行 Inception）。

KD-NAS student 候选：**并行**多核 Inception 风格残差块——3 条支路 k∈{3,5,7} 同时跑（标准
Conv1d，非 DW），各过 BN+ReLU 后**求和**+残差。物理动机：OFDM 信道在频率轴的冲激响应是
多径抽头叠加，不同核宽对应不同多径时延假设（k=3↔短时延、k=7↔长时延城市多径），并行分支让
网络自适应权衡——与单核大核（``spt_largekernel``，密集但单一）和串联 dilation
（``spt_cnn_dilated``，稀疏采样）正交：本变体是**并行多尺度密集采样**极。

昇腾友好性：3 支路全 k>1 标准 Conv1d → IMG2COL 进 Cube GEMM，**零 MATMUL**（无 1×1 边界、
无 attention、无 DW——DW 饿死 Cube 见 KB ``wireless_receiver/failures.md`` #1）。求和是
elementwise（Vector core 友好）。无 TransData 边界抖动。

来源 / 灵感：
- InceptionNeXt（arXiv:2303.16900）——"decompose expensive conv into parallel branches"思想。
- 经典 Inception-v4 / GoogLeNet（Szegedy et al.）——多核并行捕获多尺度特征。
- 本变体去掉 Inception 的 1×1 reduce 与池化支路（避免 MATMUL 与额外格式切换），只保留 3 条
  标准 conv 支路求和——Inception-NeXt 的昇腾友好极简版。

与现有变体的关系（4 极 CNN 家族）：
- ``spt_cnn_dilated`` —— 串联稀疏（hourglass dilation {1,2,4,8}）
- ``spt_largekernel`` —— 单核密集大核（k∈{7..15}）
- ``spt_cnn_pointwise`` —— 全局 pointwise（1×1）
- **``spt_inception``（本）—— 并行多尺度密集（k=3/5/7 同时）**

契约见 ``README.md``。
"""

from __future__ import annotations

import torch
import torch.nn as nn

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

# kernel_set 固定 {3,5,7}（Inception 多尺度；奇数保对称 padding）。
# num_blocks / embed_dim 走标准旋钮。kernel 不进 KNOBS（保多尺度身份）。
KNOBS = {
    "num_blocks": {"default": 4, "min": 1, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 16, "min": 8, "step": -4, "leverage": "medium"},
}

_IN_CHANNELS = 4
_NUM_SYMBOLS = 64
_NUM_SUBCARRIERS = 48
_KERNELS = (3, 5, 7)   # Inception 三支路核宽


class _InceptionBranch(nn.Module):
    """单支路：标准 Conv1d(k) + BN + ReLU。无 DW（Cube 友好）。输入输出 [N, C, F]。"""

    def __init__(self, embed_dim, kernel_size):
        super().__init__()
        assert kernel_size % 2 == 1, f"kernel 须奇数，得到 {kernel_size}"
        self.conv = nn.Conv1d(embed_dim, embed_dim, kernel_size=kernel_size,
                              padding=(kernel_size - 1) // 2, bias=False)
        self.bn = nn.BatchNorm1d(embed_dim)
        self.act = nn.ReLU(inplace=True)

    def forward(self, x):
        return self.act(self.bn(self.conv(x)))


class InceptionBlock(nn.Module):
    """多尺度并行 Inception 残差块：3 支路 k∈{3,5,7} 并行 → 求和 → +残差。

    输入输出 ``[B, num_symbols, embed_dim, num_subcarriers]``。
    内部 reshape 到 ``[B*num_symbols, embed_dim, num_subcarriers]`` 走 Conv1d（F 轴）。
    num_symbols/num_subcarriers 仅 introspection 用（forward 靠输入 shape）。
    """

    def __init__(self, embed_dim, num_symbols, num_subcarriers, kernels=_KERNELS):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.branches = nn.ModuleList([
            _InceptionBranch(embed_dim, k) for k in kernels
        ])

    def forward(self, x):
        # x: [B, S, C, F]
        B, S, C, F_ = x.shape
        h = x.reshape(B * S, C, F_)
        out = sum(branch(h) for branch in self.branches)   # elementwise 求和（Vector core 友好）
        out = out.reshape(B, S, C, F_)
        return out + x


class InceptionReceiver(nn.Module):
    """多尺度并行 CNN 主体：3-tap stem → N×InceptionBlock → 3-tap r_out，alpha 功率归一外壳。"""

    def __init__(self, in_channels=_IN_CHANNELS, embed_dim=16, num_symbols=_NUM_SYMBOLS,
                 num_subcarriers=_NUM_SUBCARRIERS, bias_flag=True, num_blocks=4,
                 kernels=_KERNELS):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.main = nn.Sequential(*[
            InceptionBlock(embed_dim, num_symbols, num_subcarriers, kernels)
            for _ in range(num_blocks)
        ])
        self.r_out = nn.Conv1d(embed_dim, in_channels, kernel_size=3, padding=1, bias=bias_flag)

    def feature_hook_names(self) -> list[str]:
        """恒 2 个（与 teacher 等长）；num_blocks=1 时第二个重复 main.0。"""
        n = len(self.main)
        mid = max(1, n // 2) if n > 1 else 0
        second = f"main.{mid}" if n > 1 else "main.0"
        return ["main.0", second]

    def forward(self, inp: torch.Tensor):
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)
        B, P, F_, S = inp.shape
        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)
        x = x.permute(0, 3, 1, 2).reshape(B * S, P, F_)
        x = self.e_lyr(x)
        x = x.reshape(B, S, -1, F_)
        x = self.main(x)
        x = x.reshape(B * S, -1, F_)
        x = self.r_out(x)
        x = x.reshape(B, S, P, F_).permute(0, 2, 3, 1)
        x = x * alpha
        return torch.unsqueeze(x, dim=-1)


def build_model(**cfg) -> nn.Module:
    """实例化多尺度并行 Inception 变体。cfg 取 num_blocks / embed_dim（kernel_set 固定）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    return InceptionReceiver(embed_dim=embed_dim, num_blocks=num_blocks)

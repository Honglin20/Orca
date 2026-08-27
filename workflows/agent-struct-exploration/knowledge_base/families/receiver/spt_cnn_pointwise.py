"""spt_cnn_pointwise.py —— model8 变体（全 CNN，ConvNeXt pointwise inverted-bottleneck）。

KD-NAS student 候选：纯 pointwise（1×1）卷积 block，ConvNeXt 风格 inverted bottleneck
（1×1 expand C→2C → GELU → 1×1 contract 2C→C + BN）。pointwise 是昇腾 Cube 最佳 workload
（tile 满载、无 Im2Col、无 TransData）。stem/r_out 用 3-tap Conv1d 补跨子载波局部性
（pointwise 本身缺频率局部性）。无 attention、无 DW（KB failures.md #1 禁区）。

与 ``spt_cnn_dilated`` 的差异：dilated 用 3-tap 稀疏采样（局部稀疏极，对应多径），
本变体用纯 pointwise（全局极，对应 per-subchannel 频域组合）。两者构成全 CNN 家族正交两极。

契约见 ``README.md``。
"""

from __future__ import annotations

import torch
import torch.nn as nn

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

KNOBS = {
    "num_blocks": {"default": 4, "min": 1, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 16, "min": 8, "step": -4, "leverage": "medium"},
}

_IN_CHANNELS = 4
_NUM_SYMBOLS = 64
_NUM_SUBCARRIERS = 48


class ConvNeXtPointwiseBlock(nn.Module):
    """Pointwise inverted-bottleneck 残差块：expand→BN→GELU→contract→BN + residual。

    输入输出 ``[B, num_symbols, embed_dim, num_subcarriers]``，内部 reshape 走 Conv1d(k=1)。
    """

    def __init__(self, embed_dim, num_symbols, num_subcarriers):
        super().__init__()
        self.embed_dim = embed_dim
        self.expand = nn.Conv1d(embed_dim, 2 * embed_dim, kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm1d(2 * embed_dim)
        self.act = nn.GELU()
        self.contract = nn.Conv1d(2 * embed_dim, embed_dim, kernel_size=1, bias=False)
        self.bn2 = nn.BatchNorm1d(embed_dim)

    def forward(self, x):
        B, S, C, F_ = x.shape
        h = x.reshape(B * S, C, F_)
        h = self.bn1(self.expand(h))
        h = self.act(h)
        h = self.bn2(self.contract(h))
        h = h.reshape(B, S, C, F_)
        return h + x


class ConvNeXtReceiver(nn.Module):
    """全 CNN pointwise 主体：3-tap stem → N×ConvNeXtPointwiseBlock → 3-tap r_out。"""

    def __init__(self, in_channels=_IN_CHANNELS, embed_dim=16, num_symbols=_NUM_SYMBOLS,
                 num_subcarriers=_NUM_SUBCARRIERS, bias_flag=True, num_blocks=4):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.main = nn.Sequential(*[
            ConvNeXtPointwiseBlock(embed_dim, num_symbols, num_subcarriers)
            for _ in range(num_blocks)
        ])
        self.r_out = nn.Conv1d(embed_dim, in_channels, kernel_size=3, padding=1, bias=bias_flag)

    def feature_hook_names(self) -> list[str]:
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
    """实例化全 CNN pointwise 变体。cfg 取 num_blocks / embed_dim（缺省用 KNOBS.default）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    return ConvNeXtReceiver(embed_dim=embed_dim, num_blocks=num_blocks)

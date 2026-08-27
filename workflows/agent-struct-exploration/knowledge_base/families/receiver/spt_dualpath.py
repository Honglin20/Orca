"""spt_dualpath.py —— model8 变体（全 CNN · 时频双路并行卷积）。

KD-NAS student 候选：**双路并行**卷积块——频域支路（F 轴 Conv1d，子载波方向 3-tap）+ 时域支路
（S 轴 Conv1d，OFDM 符号方向 3-tap，对偶布局）并行跑，各 BN+ReLU 后**求和**+残差。物理动机：
OFDM 信道在时（符号/doppler）频（子载波/multipath）两轴的相关性是**近似可分**的——多径时延
扩展走频域支路（F 轴 FIR 均衡）、多普勒时变走时域支路（S 轴 FIR 平滑），双路并行捕获两轴的
独立物理过程再融合。与 ``spt_2d``（axial **attention**）正交——本变体用**双轴 conv** 替代双轴
attention，给 sweep 补一个"时频可分卷积"维度的 cost-accuracy 点。

昇腾友好性：两支路都是标准 Conv1d(k=3)（IMG2COL 进 Cube），**零 MATMUL**（无 1×1、无 attention、
无 DW）。S 轴支路只需在 Conv1d 前后 permute（TransData 是 layout 转换，Vector core 处理，但只
2 次/block，远少于 attention 的密集 reshape）。求和是 elementwise。整体 cube 占比高。

来源 / 灵感：
- Dual-Path RNN（Luo & Mesgarani, 2018）+ DPCRN（Interspeech 2021, 被引 156+）——dual-path
  沿两轴独立处理再融合的范式（原为语音分离 / 增强；本变体移植到 OFDM 时频）。
- UCLA Dual Path Network（cores.ee.ucla.edu）——blind symbol decoding 的双路结构（无线语境）。
- DRP-SENet（ACM 2025）——time-frequency dual-branch parallel（双路在时/频轴独立处理）。
- 本变体是 dual-path 范式的昇腾友好极简版（去 RNN / attention，纯双轴 conv）。

与现有变体的关系：
- ``spt_2d`` —— axial **attention**（S 轴 MHA + F 轴 MHA），MATMUL 密集
- ``spt_cnn_dilated`` —— 单轴（F 轴）dilation，无时域支路
- **``spt_dualpath``（本）—— 双轴并行 conv（F 轴 + S 轴），零 attention**

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


class _AxisConv(nn.Module):
    """沿指定轴的标准 Conv1d 子步：Conv1d(k=3) + BN + ReLU。

    axis="F"：在子载波轴（F=48）卷积，输入 [B, S, C, F] 直接 reshape [B*S, C, F] 走 Conv1d。
    axis="S"：在符号轴（S=64）卷积，permute 把 S 拉到最后做 Conv1d 维，再 permute 回。
    """

    def __init__(self, embed_dim, kernel_size=3, axis="F"):
        super().__init__()
        assert axis in ("F", "S"), f"axis 须 'F' 或 'S'，得到 {axis}"
        assert kernel_size % 2 == 1, f"kernel 须奇数，得到 {kernel_size}"
        self.axis = axis
        self.conv = nn.Conv1d(embed_dim, embed_dim, kernel_size=kernel_size,
                              padding=(kernel_size - 1) // 2, bias=False)
        self.bn = nn.BatchNorm1d(embed_dim)
        self.act = nn.ReLU(inplace=True)

    def forward(self, x):
        # x: [B, S, C, F]
        B, S, C, F_ = x.shape
        if self.axis == "F":
            h = x.reshape(B * S, C, F_)
            h = self.act(self.bn(self.conv(h)))
            return h.reshape(B, S, C, F_)
        # axis == "S"：把 S 拉到 Conv1d 的最后维（[B, C, F, S] → [B*F, C, S]）
        h = x.permute(0, 2, 3, 1).reshape(B * F_, C, S)         # [B*F, C, S]
        h = self.act(self.bn(self.conv(h)))                     # [B*F, C, S]
        h = h.reshape(B, C, F_, S).permute(0, 3, 1, 2)          # [B, S, C, F]
        return h


class DualPathBlock(nn.Module):
    """双路并行 conv 残差块：F 轴支路 + S 轴支路 → 求和 + 残差。

    输入输出 ``[B, num_symbols, embed_dim, num_subcarriers]``。
    """

    def __init__(self, embed_dim, num_symbols, num_subcarriers, kernel_size=3):
        super().__init__()
        self.f_branch = _AxisConv(embed_dim, kernel_size, axis="F")
        self.s_branch = _AxisConv(embed_dim, kernel_size, axis="S")

    def forward(self, x):
        h = self.f_branch(x) + self.s_branch(x)     # 双路 elementwise 求和
        return h + x


class DualPathReceiver(nn.Module):
    """双路并行 CNN 主体：3-tap stem → N×DualPathBlock → 3-tap r_out，alpha 功率归一外壳。"""

    def __init__(self, in_channels=_IN_CHANNELS, embed_dim=16, num_symbols=_NUM_SYMBOLS,
                 num_subcarriers=_NUM_SUBCARRIERS, bias_flag=True, num_blocks=4,
                 kernel_size=3):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.main = nn.Sequential(*[
            DualPathBlock(embed_dim, num_symbols, num_subcarriers, kernel_size)
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
    """实例化双路并行 conv 变体。cfg 取 num_blocks / embed_dim（kernel 固定 3）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    return DualPathReceiver(embed_dim=embed_dim, num_blocks=num_blocks)

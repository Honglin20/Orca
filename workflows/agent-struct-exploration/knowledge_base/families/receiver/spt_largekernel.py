"""spt_largekernel.py —— model8 变体（全 CNN，大核标准 conv）。

KD-NAS student 候选：纯卷积，大核 Conv1d（k∈{7..15}，**标准 conv 非 DW**）+ 1×1 projection。
物理动机：大核在频率轴权重对应宽带 FIR 滤波器，IFFT 到时域即功率时延谱（PDP），k=15 对应
最大多径时延 14 个子载波间距，覆盖典型城市多径；比 spt_cnn_dilated 的稀疏 dilation 更显式
（密集采样）。im2col 后走 Cube GEMM（昇腾友好；k≤7 满载，k≥9 需 micro-bench）。

与 spt_cnn_dilated / spt_cnn_pointwise 构成全 CNN 家族第三个正交极（局部密集大核）。
来源：KB direction ``student_large_kernel.md``（D20）。契约见 ``README.md``。
"""

from __future__ import annotations

import torch
import torch.nn as nn

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

# kernel_size 是主缩容轴（奇数；step=-2 保奇数 15→13→11→9→7）。
KNOBS = {
    "kernel_size": {"default": 15, "min": 7, "step": -2, "leverage": "high"},
    "num_blocks": {"default": 4, "min": 1, "step": -1, "leverage": "medium"},
    "embed_dim": {"default": 16, "min": 8, "step": -4, "leverage": "low"},
}

_IN_CHANNELS = 4
_NUM_SYMBOLS = 64
_NUM_SUBCARRIERS = 48


class LargeKernelBlock(nn.Module):
    """大核 conv 残差块：Conv1d(k) → BN → GELU → Conv1d(1×1) → BN + residual（标准 conv）。

    num_symbols/num_subcarriers 仅 introspection（与 DilatedResBlock 签名对齐），forward 靠输入 shape。
    """

    def __init__(self, embed_dim, num_symbols, num_subcarriers, kernel_size=15):
        super().__init__()
        assert kernel_size % 2 == 1, f"kernel_size 须奇数（对称 padding），得到 {kernel_size}"
        self.cv1 = nn.Conv1d(embed_dim, embed_dim, kernel_size=kernel_size,
                             padding=(kernel_size - 1) // 2, groups=1, bias=False)
        self.bn1 = nn.BatchNorm1d(embed_dim)
        self.act = nn.GELU()
        self.cv2 = nn.Conv1d(embed_dim, embed_dim, kernel_size=1, bias=False)
        self.bn2 = nn.BatchNorm1d(embed_dim)

    def forward(self, x):
        B, S, C, F_ = x.shape
        h = x.reshape(B * S, C, F_)
        h = self.bn1(self.cv1(h))
        h = self.act(h)
        h = self.bn2(self.cv2(h))
        h = h.reshape(B, S, C, F_)
        return h + x


class LargeKernelReceiver(nn.Module):
    """全 CNN 大核主体：3-tap stem → N×LargeKernelBlock → 3-tap r_out。"""

    def __init__(self, in_channels=_IN_CHANNELS, embed_dim=16, num_symbols=_NUM_SYMBOLS,
                 num_subcarriers=_NUM_SUBCARRIERS, bias_flag=True, num_blocks=4,
                 kernel_size=15):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.main = nn.Sequential(*[
            LargeKernelBlock(embed_dim, num_symbols, num_subcarriers, kernel_size=kernel_size)
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
    """实例化大核 CNN 变体。cfg 取 kernel_size / num_blocks / embed_dim。"""
    kernel_size = int(cfg.get("kernel_size", KNOBS["kernel_size"]["default"]))
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    if kernel_size % 2 == 0:
        raise ValueError(f"spt_largekernel kernel_size={kernel_size} 须奇数（对称 padding）")
    return LargeKernelReceiver(embed_dim=embed_dim, num_blocks=num_blocks, kernel_size=kernel_size)

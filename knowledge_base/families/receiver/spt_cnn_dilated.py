"""spt_cnn_dilated.py —— model8 变体（全 CNN，DeepRx 风格 dilated resblock）。

KD-NAS student 候选：纯卷积主干（无 attention），DeepRx [2005.01494] 风格 hourglass
dilation {1,2,4,8} **标准** Conv1d 残差块（禁 DW——昇腾 Cube 饿死，见 KB failures.md #1）。
物理动机：dilated conv 在频率轴等价稀疏采样 FIR，对应多径时延先验（rate {1,2,4,8} ↔
{1,2,4,8} 子载波间距的多径抽头）；无 attention = 零 TransData、全在 GEMM land。

相对 KB raw ``deeprx_dilated_resblock.py.md`` 的调整：**关 3-grid 输入富化**
（stem_in = in_channels = 4，非 12），保与 teacher 同 input（KD 要求 input 一致）。

契约见 ``README.md``。
"""

from __future__ import annotations

from _model8_blocks import DilatedResBlock  # noqa: F401  (同目录共享积木)
import torch
import torch.nn as nn

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

# 可调旋钮：latency 超阈时按 leverage 高→低缩容。embed_dim DeepRx 风格可放宽到 32。
KNOBS = {
    "num_blocks": {"default": 4, "min": 1, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 16, "min": 8, "step": -4, "leverage": "medium"},
}

_IN_CHANNELS = 4
_NUM_SYMBOLS = 64
_NUM_SUBCARRIERS = 48


class DilatedConvReceiver(nn.Module):
    """全 CNN 主体：3-tap e_lyr → N×DilatedResBlock → 3-tap r_out，alpha 功率归一外壳。"""

    def __init__(self, in_channels=_IN_CHANNELS, embed_dim=16, num_symbols=_NUM_SYMBOLS,
                 num_subcarriers=_NUM_SUBCARRIERS, bias_flag=True, num_blocks=4,
                 dilations=(1, 2, 4, 8)):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.main = nn.Sequential(*[
            DilatedResBlock(embed_dim, num_symbols, num_subcarriers, dilations)
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
    """实例化全 CNN dilated 变体。cfg 取 num_blocks / embed_dim（缺省用 KNOBS.default）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    return DilatedConvReceiver(embed_dim=embed_dim, num_blocks=num_blocks)

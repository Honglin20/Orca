"""spt_channelformer.py —— model8 变体（浅 attention precoder + CNN 主干）。

KD-NAS student 候选：1 层 attention 做 input precoder（全局上下文聚合）+ CNN 主干（占绝大部分
参数）。ChannelFormer 思想：浅 attn 提供全局上下文、conv 主干做局部重活；TransData 边界降到
2 次/前向（teacher 8 次）。与全 CNN（D1/D18/D20）互补——后者零 attn，本变体 1 层 attn。

来源：KB direction ``channelformer_attn_precoder.md``（D5；原 ChannelFormer 是 SISO 下行 CE，
此处只借"浅 attn precoder + CNN 主干"形态，勿当 MIMO 基准）。契约见 ``README.md``。
"""

from __future__ import annotations

from _model8_blocks import DilatedResBlock, SignalAttention1D  # noqa: F401  (共享积木)
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
_NUM_ATTN = 1   # D5 卖点：浅 attn（固定 1 层 precoder，不进 KNOBS）


class _AttnPrecoder(nn.Module):
    """浅 attention precoder：per-channel attention（同 baseline t1）+ 3-tap proj + 残差。"""

    def __init__(self, embed_dim, num_symbols, num_subcarriers):
        super().__init__()
        self.attn = SignalAttention1D(embed_dim, num_symbols, num_subcarriers, m_type="t1")
        self.proj = nn.Conv1d(embed_dim, embed_dim, kernel_size=3, padding=1, bias=False)

    def forward(self, x):
        B, S, C, F_ = x.shape
        x_a = self.attn(x)
        x_p = self.proj(x_a.reshape(B * S, C, F_)).reshape(B, S, C, F_)
        return x_p + x


class ChannelFormerReceiver(nn.Module):
    """浅 attn precoder + CNN 主干：3-tap stem → 1×_AttnPrecoder → N×DilatedResBlock → 3-tap r_out。"""

    def __init__(self, in_channels=_IN_CHANNELS, embed_dim=16, num_symbols=_NUM_SYMBOLS,
                 num_subcarriers=_NUM_SUBCARRIERS, bias_flag=True, num_blocks=4,
                 num_attn=_NUM_ATTN):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.precoder = nn.Sequential(*[
            _AttnPrecoder(embed_dim, num_symbols, num_subcarriers) for _ in range(num_attn)
        ])
        self.main = nn.Sequential(*[
            DilatedResBlock(embed_dim, num_symbols, num_subcarriers) for _ in range(num_blocks)
        ])
        self.r_out = nn.Conv1d(embed_dim, in_channels, kernel_size=3, padding=1, bias=bias_flag)

    def feature_hook_names(self) -> list[str]:
        # precoder（attn 输出，与 teacher attn 同族）+ main（CNN 主干末端），恒 2 个。
        return ["precoder", "main"]

    def forward(self, inp: torch.Tensor):
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)
        B, P, F_, S = inp.shape
        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)
        x = x.permute(0, 3, 1, 2).reshape(B * S, P, F_)
        x = self.e_lyr(x)
        x = x.reshape(B, S, -1, F_)
        x = self.precoder(x)
        x = self.main(x)
        x = x.reshape(B * S, -1, F_)
        x = self.r_out(x)
        x = x.reshape(B, S, P, F_).permute(0, 2, 3, 1)
        x = x * alpha
        return torch.unsqueeze(x, dim=-1)


def build_model(**cfg) -> nn.Module:
    """实例化 channelformer 变体。cfg 取 num_blocks / embed_dim（num_attn 固定 1）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    return ChannelFormerReceiver(embed_dim=embed_dim, num_blocks=num_blocks)

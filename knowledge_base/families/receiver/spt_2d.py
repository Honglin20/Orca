"""spt_2d.py —— model8 变体（2D 时频，axial attention）。

KD-NAS student 候选：标准 multi-head **axial attention**——每个 block 先沿 symbol 轴(64 tokens)
再沿 subcarrier 轴(48 tokens)做 MHA，显式分解 2D 时频网格的联合结构（OFDM 物理：多普勒→时域
相关、多径→频域相关，近似可分）。qkv=Linear（纯 pointwise），无 Conv1d k=3 的 QKV/FFN 投影。

⚠️ 与 teacher（per-channel 64×64 怪 attention）几何差异最大，但 OFD adapter 处理 channel 维 +
``kd/losses._align_spatial`` 兜底空间维，已验证可对齐。手搓 MHA 与现有 baseline 风格一致
（部署 latency 由 latency_provider 在昇腾实测，慢则 sweep 自然惩罚 / 调参）。

来源：KB raw ``axial_attention.py.md``（D7）。契约见 ``README.md``。
"""

from __future__ import annotations

import torch
import torch.nn as nn

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

# embed_dim 须被 _NUM_HEADS=4 整除（KNOBS 16→12→8 均满足）。
KNOBS = {
    "num_blocks": {"default": 4, "min": 1, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 16, "min": 8, "step": -4, "leverage": "medium"},
}

_IN_CHANNELS = 4
_NUM_SYMBOLS = 64
_NUM_SUBCARRIERS = 48
_NUM_HEADS = 4


class _AxialMHA(nn.Module):
    """沿单一轴的标准 MHA。axis='S' 沿 symbol(64 tokens)，'F' 沿 subcarrier(48 tokens)。
    输入输出 ``[B, num_symbols, embed_dim, num_subcarriers]``，每 token 特征维 = embed_dim。
    """

    def __init__(self, embed_dim, num_heads, axis):
        super().__init__()
        assert embed_dim % num_heads == 0, f"embed_dim {embed_dim} 须被 num_heads {num_heads} 整除"
        self.axis = axis
        self.num_heads = num_heads
        self.head_dim = embed_dim // num_heads
        self.scale = self.head_dim ** -0.5
        self.qkv = nn.Linear(embed_dim, 3 * embed_dim)
        self.proj = nn.Linear(embed_dim, embed_dim)
        self.norm = nn.LayerNorm(embed_dim)

    def forward(self, x):
        B, S, C, F_ = x.shape
        if self.axis == "S":
            h = x.permute(0, 3, 1, 2).reshape(B * F_, S, C)    # [B*F, S, C]
            h = self.norm(h)
            h = self._mha(h, S)                                 # [B*F, S, C]
            h = h.reshape(B, F_, S, C).permute(0, 2, 3, 1)     # [B, S, C, F]
        else:  # "F"
            h = x.permute(0, 1, 3, 2).reshape(B * S, F_, C)    # [B*S, F, C]
            h = self.norm(h)
            h = self._mha(h, F_)                                # [B*S, F, C]
            h = h.reshape(B, S, F_, C).permute(0, 1, 3, 2)     # [B, S, C, F]
        return h

    def _mha(self, x, L):
        N = x.shape[0]
        qkv = self.qkv(x).reshape(N, L, 3, self.num_heads, self.head_dim)
        q, k, v = qkv[:, :, 0], qkv[:, :, 1], qkv[:, :, 2]     # [N, L, H, D]
        q = q.transpose(1, 2)                                   # [N, H, L, D]
        k = k.transpose(1, 2)
        v = v.transpose(1, 2)
        attn = torch.matmul(q, k.transpose(-1, -2)) * self.scale
        attn = attn.softmax(dim=-1)
        out = torch.matmul(attn, v).transpose(1, 2).reshape(N, L, -1)  # [N, L, C]
        return self.proj(out)


class _AxialBlock(nn.Module):
    """Axial block：S-axis MHA + F-axis MHA + pointwise FFN，各带残差。"""

    def __init__(self, embed_dim, num_symbols, num_subcarriers, num_heads):
        super().__init__()
        self.attn_s = _AxialMHA(embed_dim, num_heads, "S")
        self.attn_f = _AxialMHA(embed_dim, num_heads, "F")
        self.ffn_cv1 = nn.Conv1d(embed_dim, 2 * embed_dim, kernel_size=1, bias=False)
        self.act = nn.GELU()
        self.ffn_cv2 = nn.Conv1d(2 * embed_dim, embed_dim, kernel_size=1, bias=False)

    def forward(self, x):
        B, S, C, F_ = x.shape
        x = x + self.attn_s(x)
        x = x + self.attn_f(x)
        h = x.reshape(B * S, C, F_)
        h = self.ffn_cv2(self.act(self.ffn_cv1(h))).reshape(B, S, C, F_)
        x = x + h
        return x


class AxialReceiver(nn.Module):
    """2D axial 主体：3-tap stem → N×_AxialBlock → 3-tap r_out。"""

    def __init__(self, in_channels=_IN_CHANNELS, embed_dim=16, num_symbols=_NUM_SYMBOLS,
                 num_subcarriers=_NUM_SUBCARRIERS, bias_flag=True, num_blocks=4,
                 num_heads=_NUM_HEADS):
        super().__init__()
        assert embed_dim % num_heads == 0, f"embed_dim {embed_dim} 须被 num_heads {num_heads} 整除"
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3, padding=1, bias=bias_flag)
        self.main = nn.Sequential(*[
            _AxialBlock(embed_dim, num_symbols, num_subcarriers, num_heads)
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
    """实例化 2D axial 变体。cfg 取 num_blocks / embed_dim（须被 num_heads=4 整除，否则 fail loud）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    if embed_dim % _NUM_HEADS != 0:
        raise ValueError(f"spt_2d embed_dim={embed_dim} 须被 num_heads={_NUM_HEADS} 整除")
    return AxialReceiver(embed_dim=embed_dim, num_blocks=num_blocks)

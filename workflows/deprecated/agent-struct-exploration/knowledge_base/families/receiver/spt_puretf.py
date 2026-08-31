"""spt_puretf.py —— model8 变体（全 Transformer，选择性 pointwise 化）。

KD-NAS student 候选：**保留 attention**（与 teacher 同族 → KD 特征对齐最容易），做**选择性
pointwise 化**——只把 conv↔attention 边界处的 conv（p_lyr QKV / proj）改成 1×1（砍 TransData
触发点），非边界层（cv1/cv2 FFN）保留 3-tap（conv-land 内保频率平滑），stem 用 dilated conv
注入宽感受野，每 block 插 delay-domain soft-threshold（τ→0 identity）补 pointwise 丢的频率选择性。

来源：KB raw ``pointwise_selective_dilated.py.md``（M4+M20+M9 组合）。纯全 pointwise
（``pointwise_qkv_ffi.diff.md``）会泛化掉点，选择性版更稳。

契约见 ``README.md``。
"""

from __future__ import annotations

from _model8_blocks import DelayDomainSoftThreshold  # noqa: F401  (共享积木)
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
_INIT_TAU = 0.0        # M9 起步 identity（fail-forward，训练后可学非零）
_STEM_DILATION = 2     # stem dilated 注入宽频率感受野（等效 RF=5）


class _PointwiseAttention(nn.Module):
    """Attention with pointwise QKV（边界层 k=1，砍 TransData）。余同 baseline per-channel 写法。"""

    def __init__(self, embed_dim, num_symbols, num_subcarriers, m_type="t1"):
        super().__init__()
        self.embed_dim = embed_dim
        self.m_type = m_type
        self.s = num_subcarriers ** -0.5 if m_type == "t1" else embed_dim ** -0.5
        self.ln = nn.LayerNorm([embed_dim, num_symbols, num_subcarriers], elementwise_affine=False)
        self.sm = nn.Softmax(dim=-1)
        self.p_lyr = nn.Conv1d(embed_dim, 3 * embed_dim, kernel_size=1, padding=0, bias=True)

    def forward(self, x):
        B, S, C, F_ = x.shape
        x = x.permute(0, 2, 1, 3)
        x = self.ln(x)
        x = x.permute(0, 2, 1, 3)
        x_f = x.reshape(B * S, C, F_)
        qkv = self.p_lyr(x_f).reshape(B, S, 3 * C, F_)
        q = qkv[:, :, 0:C, :]
        k = qkv[:, :, C:2 * C, :]
        v = qkv[:, :, 2 * C:, :]
        if self.m_type == "t1":
            q, k, v = q.permute(0, 2, 1, 3), k.permute(0, 2, 1, 3), v.permute(0, 2, 1, 3)
            dots = torch.matmul(q, k.transpose(-1, -2)) * self.s
            out = torch.matmul(self.sm(dots), v).permute(0, 2, 1, 3)
        else:
            q, k, v = q.permute(0, 3, 1, 2), k.permute(0, 3, 1, 2), v.permute(0, 3, 1, 2)
            dots = torch.matmul(q, k.transpose(-1, -2)) * self.s
            out = torch.matmul(self.sm(dots), v).permute(0, 2, 3, 1)
        return out


class _FFN(nn.Module):
    """FFN，cv1/cv2 保留 3-tap（非边界层，conv-land 内保频率平滑）。"""

    def __init__(self, embed_dim, num_symbols, num_subcarriers):
        super().__init__()
        self.ln = nn.LayerNorm([num_symbols, embed_dim, num_subcarriers], elementwise_affine=False)
        self.cv1 = nn.Conv1d(embed_dim, 2 * embed_dim, kernel_size=3, padding=1, bias=True)
        self.act = nn.GELU()
        self.cv2 = nn.Conv1d(2 * embed_dim, embed_dim, kernel_size=3, padding=1, bias=True)

    def forward(self, x):
        B, S, C, F_ = x.shape
        x = self.ln(x)
        x_f = x.reshape(B * S, C, F_)
        x_f = self.cv2(self.act(self.cv1(x_f)))
        return x_f.reshape(B, S, C, F_)


class _SelectivePointwiseBlock(nn.Module):
    """选择性 pointwise block：pointwise attn+proj / 3-tap FFN / M9 soft-threshold。"""

    def __init__(self, embed_dim, num_symbols, num_subcarriers, m_type="t1",
                 use_soft_threshold=True, init_tau=_INIT_TAU):
        super().__init__()
        self.m_a = _PointwiseAttention(embed_dim, num_symbols, num_subcarriers, m_type=m_type)
        self.proj = nn.Conv1d(embed_dim, embed_dim, kernel_size=1, padding=0, bias=False)
        self.soft_thr = (
            DelayDomainSoftThreshold(embed_dim, init_tau=init_tau)
            if use_soft_threshold else nn.Identity()
        )
        self.m_c = _FFN(embed_dim, num_symbols, num_subcarriers)

    def forward(self, x):
        B, S, C, F_ = x.shape
        x_a = self.m_a(x)
        x_p = self.proj(x_a.reshape(B * S, C, F_)).reshape(B, S, C, F_)
        x = x_p + x
        x = self.soft_thr(x)
        x_m_c = self.m_c(x)
        x = x_m_c + x
        return x


class PointwiseSelectiveTransformer(nn.Module):
    """选择性 pointwise 主体（M4）：dilated stem + N×_SelectivePointwiseBlock + 3-tap r_out。"""

    def __init__(self, in_channels=_IN_CHANNELS, embed_dim=16, num_symbols=_NUM_SYMBOLS,
                 num_subcarriers=_NUM_SUBCARRIERS, bias_flag=True, num_blocks=4,
                 stem_dilation=_STEM_DILATION, use_soft_threshold=True, init_tau=_INIT_TAU):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.e_lyr = nn.Conv1d(in_channels, embed_dim, kernel_size=3,
                               padding=stem_dilation, dilation=stem_dilation, bias=bias_flag)
        self.main = nn.Sequential(*[
            _SelectivePointwiseBlock(embed_dim, num_symbols, num_subcarriers, m_type="t1",
                                     use_soft_threshold=use_soft_threshold, init_tau=init_tau)
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
    """实例化选择性 pointwise 变体。cfg 取 num_blocks / embed_dim（init_tau/stem_dilation 固定）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    return PointwiseSelectiveTransformer(embed_dim=embed_dim, num_blocks=num_blocks)

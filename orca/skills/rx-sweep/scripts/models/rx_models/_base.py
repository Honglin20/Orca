"""_base.py —— rx_models 全方案共享积木。

含：
- ``RxModelBase``：共享壳（squeeze 尾维 → alpha 功率归一 → 子类 ``_forward_core``
  → ``*alpha`` → unsqueeze）。子类只实现 ``_forward_core(x:[B,P,F,S]) -> [B,P,F,S]``，
  I/O 逐位对齐 model8（KD/对比实验可比性命门）。
- ``DualAxisConvBlock``：pure_cnn 双轴 dilated dense Conv1d 残差块（[B,S,C,F]）。
- ``SignalAttention1D`` / ``SignalFeedForward1D`` / ``SignalTransformerBlock``：
  model8 attention 块（[B,S,C,F]）。作 baseline 与 CNN+TRF 混合的 TRF 分量。
- ``ComplexConv1d``：复数卷积原语（B1 前端积木），全程 dense Conv，**无 atan2/缠绕**，
  昇腾 Cube 友好。
- ``StackedBackbone``：``stem Conv1d → N×(异构 block) → out Conv1d``，``[B,P,F,S]→[B,P,F,S]``
  自洽（自理 permute/reshape）。CNN/TRF/Hybrid 三类主干共享此实现（DRY）。
- ``build_cnn_blocks`` / ``build_trf_blocks`` / ``build_hybrid_blocks``：block 列表工厂。
- ``FeatureFrontend`` / ``IdentityFrontend``：特征前端基类 + 无前端占位。

设计约束（why）：
- 全 **dense Conv1d**，禁 depthwise/group（昇腾 Cube 饿死）。
- 所有 block I/O = ``[B, S, embed_dim, F]``，可任意混排（Hybrid 的前提）。
- fail loud：shape 不符 / 维度不对齐 → raise（静默兜底会让 gate 看似 PASS 但实验全错）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

import torch
import torch.nn as nn

if TYPE_CHECKING:
    from .config import RxConfig


# ---------------------------------------------------------------------------
# RxModelBase —— 全方案共享壳（alpha 归一 + squeeze/unsqueeze）
# ---------------------------------------------------------------------------
class RxModelBase(nn.Module):
    """全方案共享壳。子类实现 ``_forward_core``，I/O 逐位对齐 model8。

    forward 流（与原 pure_cnn_model / model8_baseline 完全一致）::

        [B,P,F,S,1] → squeeze → [B,P,F,S]
                    → alpha = sqrt(mean(x², dim=[1,2,3])·2)（功率归一）
                    → x = inp / (alpha + 1e-6)
                    → y = self._forward_core(x)       # 子类主干/前端
                    → y = y * alpha                    # 还原功率尺度
                    → unsqueeze → [B,P,F,S,1]

    子类**必须**保证 ``_forward_core`` 输入输出同形 ``[B,P,F,S]``；本类对输出 shape
    做 fail-loud 断言，防子类误改维度。
    """

    def __init__(self, cfg: "RxConfig"):
        super().__init__()
        self.cfg = cfg

    def _forward_core(self, x: torch.Tensor) -> torch.Tensor:  # pragma: no cover
        raise NotImplementedError("子类须实现 _forward_core(x:[B,P,F,S]) -> [B,P,F,S]")

    def forward(self, inp: torch.Tensor) -> torch.Tensor:
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)
        if inp.dim() != 4:
            raise ValueError(
                f"期望 4D [B,P,F,S]（或 5D 带尾 1），got shape={tuple(inp.shape)}"
            )
        P, F_, S = self.cfg.num_ports, self.cfg.num_subcarriers, self.cfg.num_symbols
        if inp.shape[1:] != (P, F_, S):
            raise ValueError(
                f"输入 shape {tuple(inp.shape)} 与 cfg (P,F,S)=({P},{F_},{S}) 不符"
            )

        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)
        y = self._forward_core(x)
        if y.shape != inp.shape:
            raise ValueError(
                f"_forward_core 改了 shape：{tuple(y.shape)} vs 输入 {tuple(inp.shape)}；"
                "子类须保 [B,P,F,S] 同形 I/O"
            )
        y = y * alpha
        return torch.unsqueeze(y, dim=-1)


# ---------------------------------------------------------------------------
# DualAxisConvBlock —— pure_cnn 双轴 dilated dense Conv1d 残差块
# ---------------------------------------------------------------------------
class DualAxisConvBlock(nn.Module):
    """双轴 Conv1d 残差块：频率分支（局部子载波）+ 时间分支（dilated 波束）+ 残差。

    输入输出同形 ``[B, S, embed_dim, F]``（与 model8 的 ``SignalTransformerBlock``
    完全一致，可在 ``StackedBackbone.main`` 里平替/混排）。

    - 频率分支：沿子载波轴 F，dense k=3 conv（承重局部先验）。
    - 时间分支：沿波束轴 S，dense k=3 dilation=d conv（替 attention 全局相关；
      dilation 跨 block 翻倍，RF 覆盖整个 S 轴）。
    - 标准 dense Conv1d，禁 depthwise/group（昇腾 Cube）。
    """

    def __init__(self, embed_dim: int, num_symbols: int,
                 num_subcarriers: int, dilation: int = 1):
        super().__init__()
        if embed_dim % 16 != 0:
            raise ValueError(
                f"embed_dim 必须 ÷16（昇腾 Cube 对齐），got embed_dim={embed_dim}"
            )
        if dilation < 1:
            raise ValueError(f"dilation 须 ≥ 1，got {dilation}")
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.dilation = dilation

        # 频率分支：F 为长度轴，dense k=3 pad=1（保长度）。
        self.freq_branch = nn.Sequential(
            nn.Conv1d(embed_dim, 2 * embed_dim, kernel_size=3, padding=1),
            nn.BatchNorm1d(2 * embed_dim),
            nn.ReLU(inplace=True),
            nn.Conv1d(2 * embed_dim, embed_dim, kernel_size=3, padding=1),
        )
        # 时间分支：S 为长度轴，dense k=3 dilation=d pad=d（保长度）。
        self.time_branch = nn.Sequential(
            nn.Conv1d(embed_dim, 2 * embed_dim, kernel_size=3,
                      padding=dilation, dilation=dilation),
            nn.BatchNorm1d(2 * embed_dim),
            nn.ReLU(inplace=True),
            nn.Conv1d(2 * embed_dim, embed_dim, kernel_size=3,
                      padding=dilation, dilation=dilation),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, S, C, F_ = x.shape
        # 频率分支：[B,S,C,F] → [B*S,C,F] → conv → 还原
        h_f = self.freq_branch(x.reshape(B * S, C, F_)).reshape(B, S, C, F_)
        # 时间分支：[B,S,C,F] → permute[B,F,C,S] → [B*F,C,S] → conv → 还原
        h_t = x.permute(0, 3, 2, 1).contiguous().reshape(B * F_, C, S)
        h_t = self.time_branch(h_t)
        h_t = h_t.reshape(B, F_, C, S).permute(0, 3, 2, 1).contiguous()
        return x + h_f + h_t


# ---------------------------------------------------------------------------
# model8 attention 块（baseline + CNN+TRF 混合的 TRF 分量）
# ---------------------------------------------------------------------------
class SignalAttention1D(nn.Module):
    """model8 attention。``m_type="t1"`` = 波束轴 attention（scale=F^-0.5）；
    ``"t2"`` = 子载波轴 attention（scale=embed_dim^-0.5）。I/O [B,S,C,F]。"""

    def __init__(self, embed_dim, num_symbols, num_subcarriers,
                 b_flg=True, m_type="t1"):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.m_type = m_type
        self.s = num_subcarriers ** -0.5 if m_type == "t1" else embed_dim ** -0.5
        self.ln = nn.LayerNorm([embed_dim, num_symbols, num_subcarriers],
                               elementwise_affine=False)
        self.sm = nn.Softmax(dim=-1)
        self.p_lyr = nn.Conv1d(embed_dim, 3 * embed_dim,
                               kernel_size=3, padding=1, bias=b_flg)

    def forward(self, x):
        batch, num_syms, embed_dim, num_subs = x.shape
        x = x.permute(0, 2, 1, 3)
        x = self.ln(x)
        x = x.permute(0, 2, 1, 3)
        x_f = torch.reshape(x, [batch * num_syms, embed_dim, num_subs])
        qkv = self.p_lyr(x_f)
        qkv = torch.reshape(qkv, [batch, num_syms, 3 * self.embed_dim, num_subs])
        q = qkv[:, :, 0:self.embed_dim, :]
        k = qkv[:, :, self.embed_dim:2 * self.embed_dim, :]
        v = qkv[:, :, 2 * self.embed_dim:, :]
        if self.m_type == "t1":
            q = q.permute(0, 2, 1, 3)
            k = k.permute(0, 2, 1, 3)
            v = v.permute(0, 2, 1, 3)
            dots = torch.matmul(q, k.transpose(-1, -2)) * self.s
            at = self.sm(dots)
            out = torch.matmul(at, v).permute(0, 2, 1, 3)
        else:
            q = q.permute(0, 3, 1, 2)
            k = k.permute(0, 3, 1, 2)
            v = v.permute(0, 3, 1, 2)
            dots = torch.matmul(q, k.transpose(-1, -2)) * self.s
            at = self.sm(dots)
            out = torch.matmul(at, v).permute(0, 2, 3, 1)
        return out


class SignalFeedForward1D(nn.Module):
    def __init__(self, embed_dim, num_symbols, num_subcarriers, b_flg=True):
        super().__init__()
        self.embed_dim = embed_dim
        self.ln = nn.LayerNorm([num_symbols, embed_dim, num_subcarriers],
                               elementwise_affine=False)
        self.cv1 = nn.Conv1d(embed_dim, 2 * embed_dim,
                             kernel_size=3, padding=1, bias=b_flg)
        self.act = nn.GELU()
        self.cv2 = nn.Conv1d(2 * embed_dim, embed_dim,
                             kernel_size=3, padding=1, bias=b_flg)

    def forward(self, x):
        batch, num_syms, embed_dim, num_subs = x.shape
        x = self.ln(x)
        x_f = torch.reshape(x, [batch * num_syms, embed_dim, num_subs])
        x = self.cv1(x_f)
        x = self.act(x)
        x = self.cv2(x)
        return torch.reshape(x, [batch, num_syms, embed_dim, num_subs])


class SignalTransformerBlock(nn.Module):
    """model8 transformer block：attention + proj + FFN，两处残差。I/O [B,S,C,F]。

    与 ``DualAxisConvBlock`` I/O 同形 → 可在 ``StackedBackbone`` 里任意混排（CNN+TRF 交替）。
    """

    def __init__(self, embed_dim, num_symbols, num_subcarriers, m_type="t1"):
        super().__init__()
        self.m_a = SignalAttention1D(embed_dim, num_symbols, num_subcarriers,
                                     m_type=m_type)
        self.proj = nn.Conv1d(embed_dim, embed_dim,
                              kernel_size=3, padding=1, bias=False)
        self.m_c = SignalFeedForward1D(embed_dim, num_symbols, num_subcarriers)

    def forward(self, x):
        x_a = self.m_a(x)
        batch, num_syms, embed_dim, num_subs = x.shape
        x_p = torch.reshape(
            self.proj(torch.reshape(x_a, [batch * num_syms, -1, num_subs])),
            [batch, num_syms, embed_dim, num_subs],
        )
        x = x_p + x
        x_m_c = self.m_c(x)
        x = x_m_c + x
        return x


# ---------------------------------------------------------------------------
# ComplexConv1d —— 复数卷积原语（B1 前端积木）
# ---------------------------------------------------------------------------
class ComplexConv1d(nn.Module):
    """复数卷积：``(a+bi)·(Wr+Wi·i) = (a·Wr − b·Wi) + (a·Wi + b·Wr)·i``。

    输入 ``(xre, xim)`` 各 ``[B, C_in, L]``；输出 ``(yre, yim)`` 各 ``[B, C_out, L]``。
    内部两个实 dense Conv1d（``conv_re``=Wr，``conv_im``=Wi），昇腾 Cube 友好，
    **无 atan2、无相位缠绕** —— 一次复乘同时处理幅相域，比拆幅相再拼通道更优雅、参数更省。
    """

    def __init__(self, in_channels: int, out_channels: int,
                 kernel_size: int, padding: int = 0, bias: bool = True):
        super().__init__()
        self.conv_re = nn.Conv1d(in_channels, out_channels, kernel_size,
                                 padding=padding, bias=bias)
        self.conv_im = nn.Conv1d(in_channels, out_channels, kernel_size,
                                 padding=padding, bias=bias)

    def forward(self, xre: torch.Tensor, xim: torch.Tensor):
        yre = self.conv_re(xre) - self.conv_im(xim)
        yim = self.conv_re(xim) + self.conv_im(xre)
        return yre, yim


# ---------------------------------------------------------------------------
# StackedBackbone —— stem + N×(异构 block) + out，[B,P,F,S] 自洽
# ---------------------------------------------------------------------------
class StackedBackbone(nn.Module):
    """``stem Conv1d → N×(异构 block) → out Conv1d``，``[B,P,F,S]→[B,P,F,S]`` 自洽。

    内部 permute/reshape：``[B,P,F,S] → [B,S,P,F] → [B·S,P,F] → e_lyr →
    [B,S,emb,F] → main(blocks) → [B·S,emb,F] → r_out → [B,S,out,F] → [B,P,F,S]``。

    ``blocks`` 是外部传入的 ``list[nn.Module]``，元素 I/O 须为 ``[B,S,embed_dim,F]``
    （``DualAxisConvBlock`` / ``SignalTransformerBlock`` 都满足）→ 可任意混排（Hybrid 前提）。
    ``in_channels``/``out_channels`` 解耦前端扩通道（前端方案 in=P'，out=P 还原）。
    """

    def __init__(self, cfg: "RxConfig", in_channels: int, out_channels: int,
                 blocks: Sequence[nn.Module]):
        super().__init__()
        if not blocks:
            raise ValueError("StackedBackbone: blocks 不能为空")
        embed_dim = cfg.embed_dim
        self.num_symbols = cfg.num_symbols
        self.num_subcarriers = cfg.num_subcarriers
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.embed_dim = embed_dim

        self.e_lyr = nn.Conv1d(in_channels, embed_dim,
                               kernel_size=3, padding=1, bias=True)
        self.main = nn.Sequential(*blocks)
        self.r_out = nn.Conv1d(embed_dim, out_channels,
                               kernel_size=3, padding=1, bias=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, P, F, S]
        B, P, F_, S = x.shape
        x = x.permute(0, 3, 1, 2)                 # [B, S, P, F]
        x = torch.reshape(x, [B * S, P, F_])
        x = self.e_lyr(x)                          # [B*S, emb, F]
        x = torch.reshape(x, [B, S, self.embed_dim, F_])
        x = self.main(x)                           # [B, S, emb, F]
        x = torch.reshape(x, [B * S, self.embed_dim, F_])
        x = self.r_out(x)                          # [B*S, out, F]
        x = torch.reshape(x, [B, S, self.out_channels, F_])
        x = x.permute(0, 2, 3, 1)                  # [B, out, F, S]
        return x


def build_cnn_blocks(cfg: "RxConfig") -> list[nn.Module]:
    """N 个 DualAxisConvBlock，dilation 按 cfg.dilations 轮换。"""
    dilations = cfg.dilations
    return [
        DualAxisConvBlock(
            cfg.embed_dim, cfg.num_symbols, cfg.num_subcarriers,
            dilation=dilations[i % len(dilations)],
        )
        for i in range(cfg.num_blocks)
    ]


def build_trf_blocks(cfg: "RxConfig", m_type: str = "t1") -> list[nn.Module]:
    """N 个 SignalTransformerBlock（默认 t1 = 波束轴 attention）。"""
    return [
        SignalTransformerBlock(
            cfg.embed_dim, cfg.num_symbols, cfg.num_subcarriers, m_type=m_type
        )
        for _ in range(cfg.num_blocks)
    ]


def build_hybrid_blocks(cfg: "RxConfig") -> list[nn.Module]:
    """按 ``cfg.cnn_trf_pattern`` 循环填 ``num_blocks`` 个 block（CNN/TRF 交替）。

    例：pattern=("cnn","trf"), num_blocks=4 → [CNN, TRF, CNN, TRF]。
    """
    pattern = cfg.cnn_trf_pattern
    blocks: list[nn.Module] = []
    for i in range(cfg.num_blocks):
        tag = pattern[i % len(pattern)]
        if tag == "cnn":
            d = cfg.dilations[i % len(cfg.dilations)]
            blocks.append(DualAxisConvBlock(
                cfg.embed_dim, cfg.num_symbols, cfg.num_subcarriers, dilation=d))
        elif tag == "trf":
            blocks.append(SignalTransformerBlock(
                cfg.embed_dim, cfg.num_symbols, cfg.num_subcarriers, m_type="t1"))
        else:  # __post_init__ 已挡，防御
            raise ValueError(f"hybrid pattern 非法 tag: {tag!r}")
    return blocks


# ---------------------------------------------------------------------------
# 特征前端基类
# ---------------------------------------------------------------------------
class FeatureFrontend(nn.Module):
    """特征前端基类。``forward(x:[B,P,F,S]) → [B, P', F, S]``。

    子类在 ``__init__`` 设 ``self.out_channels = P'``（扩通道后的通道数），供
    ``StackedBackbone(in_channels=P', out_channels=P)`` 接续。特征变换在模型**内部**
    完成 → I/O 仍是 ``[B,P,F,S,1]``，不动训练/数据代码。
    """

    def __init__(self, out_channels: int):
        super().__init__()
        self.out_channels = int(out_channels)

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # pragma: no cover
        raise NotImplementedError("子类须实现 forward(x:[B,P,F,S]) -> [B,P',F,S]")


class IdentityFrontend(FeatureFrontend):
    """无前端占位：直接透传，``out_channels = num_ports``。"""

    def __init__(self, num_ports: int):
        super().__init__(num_ports)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x

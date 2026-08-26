"""feat_complex —— B1 复数卷积前端 + CNN 主干。

前端把 P=4 实通道折成 in_cplx=P//2 复通道(polar × {re,im}),沿子载波轴 F 做 dense
``ComplexConv1d(k=3, pad=1)``(保长度)→ 复→实展开为 ``out_channels = 2·out_cplx``
(默认 ``embed_dim//2 = 8`` → 16 通道);接 ``StackedBackbone`` CNN 主干还原回 P=4。

一次复乘 ``(a+bi)·(Wr+Wi·i)`` 同时处理幅相域,**无 atan2、无相位缠绕**,昇腾 Cube 友好
(ComplexConv1d 内部两个实 dense Conv1d)。S 折 batch 沿 F 卷积:每个 OFDM 符号
独立做子载波邻域复相关,再还原回 [B, 2·out_cplx, F, S]。

I/O 由 ``RxModelBase`` 统一为 ``[B,P,F,S,1]``;``_forward_core`` 在 ``[B,P,F,S]`` 上做
"前端扩通道 → 主干映射"。
"""
from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# 自举:直接脚本运行(python rx_models/feat_complex.py)时,重定向为包模块,
# 让下面的相对 import(from ._base / from .config)可解。
#   - __package__ 空 = 直接脚本运行 → 自举后 sys.exit
#   - __package__ == "rx_models" = -m 或包内 import → 跳过
# ---------------------------------------------------------------------------
if __package__ in (None, ""):
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _PARENT = os.path.dirname(_HERE)
    if _PARENT not in sys.path:
        sys.path.insert(0, _PARENT)
    import importlib
    sys.exit(importlib.import_module("rx_models.feat_complex")._smoke())

import torch

from ._base import (
    RxModelBase,
    StackedBackbone,
    build_cnn_blocks,
    ComplexConv1d,
    FeatureFrontend,
)
from .config import RxConfig
from . import register


class ComplexFrontend(FeatureFrontend):
    """B1 复数卷积前端:``P`` 实通道 → ``in_cplx=P//2`` 复通道 → ``out_cplx`` 复通道
    → 复→实展开 ``2·out_cplx`` 通道。

    forward(x:[B,P,F,S])::

        xre, xim = x[:, 0::2], x[:, 1::2]        # [B, in_cplx, F, S]
        # S 折 batch 沿 F 卷积(每符号独立):
        xre, xim → [B*S, in_cplx, F] → ComplexConv1d → [B*S, out_cplx, F]
        yre, yim → [B, out_cplx, F, S]
        return torch.stack([yre, yim], dim=2).reshape(B, 2*out_cplx, F, S)

    要求 ``cfg.num_ports`` 为偶数(实/虚成对)。
    """

    def __init__(self, cfg: RxConfig, out_cplx: int | None = None):
        if cfg.num_ports % 2 != 0:
            raise ValueError(
                f"num_ports 须为偶数(实/虚成对),got num_ports={cfg.num_ports}"
            )
        out_cplx = out_cplx or (cfg.embed_dim // 2)
        super().__init__(out_channels=out_cplx * 2)
        self.in_cplx = cfg.num_ports // 2
        self.out_cplx = out_cplx
        self.conv = ComplexConv1d(self.in_cplx, out_cplx, 3, padding=1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, P, F, S]
        B, P, F_, S = x.shape
        if P != self.in_cplx * 2:
            raise ValueError(
                f"ComplexFrontend: P={P} 与 in_cplx*2={self.in_cplx * 2} 不符"
            )
        # 实/虚拆分(P → in_cplx 复通道)
        xre = x[:, 0::2, :, :]      # [B, in_cplx, F, S]
        xim = x[:, 1::2, :, :]      # [B, in_cplx, F, S]
        # S 折 batch:[B, in_cplx, F, S] → [B, S, in_cplx, F] → [B*S, in_cplx, F]
        xre = xre.permute(0, 3, 1, 2).reshape(B * S, self.in_cplx, F_)
        xim = xim.permute(0, 3, 1, 2).reshape(B * S, self.in_cplx, F_)
        # 复卷积:[B*S, out_cplx, F]
        yre, yim = self.conv(xre, xim)
        # 还原 [B, out_cplx, F, S]
        yre = yre.reshape(B, S, self.out_cplx, F_).permute(0, 2, 3, 1)
        yim = yim.reshape(B, S, self.out_cplx, F_).permute(0, 2, 3, 1)
        # 复→实展开:[B, out_cplx, 2, F, S] → [B, 2*out_cplx, F, S]
        out = torch.stack([yre, yim], dim=2).reshape(B, 2 * self.out_cplx, F_, S)
        return out


@register("feat_complex")
class FeatComplex(RxModelBase):
    """B1 复数卷积前端 + CNN 主干。"""

    def __init__(self, cfg: RxConfig):
        super().__init__(cfg)
        self.frontend = ComplexFrontend(cfg)
        self.net = StackedBackbone(
            cfg, self.frontend.out_channels, cfg.num_ports, build_cnn_blocks(cfg)
        )

    def _forward_core(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(self.frontend(x))


def _smoke() -> int:
    import torch
    from .config import RxConfig
    cfg = RxConfig(num_symbols=32)            # P=4, F=48, S=32
    m = FeatComplex(cfg).eval()
    x = torch.randn(*cfg.io_shape)            # [1,4,48,32,1]
    y = m(x)
    assert list(y.shape) == cfg.io_shape, (tuple(y.shape), cfg.io_shape)
    m.train(); m(torch.randn(*cfg.io_shape)).sum().backward()
    print("feat_complex SMOKE_OK", tuple(y.shape))
    return 0


if __name__ == "__main__":
    sys.exit(_smoke())

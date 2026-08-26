"""feat_adjbeam —— B4 邻波束拼接前端 + Conv2d 主干。

相邻 beam 信道高度相关：前端把相邻 ``k=cfg.adjbeam_k`` 个波束滑窗拼到通道轴
（显式注入角域先验，``out_channels = P·k``），主干用 dense Conv2d 把 ``(F, S)``
当 2D 图同时混合。**不复用** ``_base`` 的 ``StackedBackbone``——那是 Conv1d 的，
本方案主干独立走 Conv2d（全 dense，昇腾 Cube 友好）。

I/O 由 ``RxModelBase`` 统一为 ``[B,P,F,S,1]``；``_forward_core`` 在
``[B,P,F,S]`` 上做主干映射。
"""
from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# 自举:直接脚本运行(python rx_models/feat_adjbeam.py)时,重定向为包模块,
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
    sys.exit(importlib.import_module("rx_models.feat_adjbeam")._smoke())

import torch
import torch.nn as nn

from ._base import FeatureFrontend, RxModelBase
from .config import RxConfig
from . import register


# ---------------------------------------------------------------------------
# AdjBeamFrontend —— 相邻 k 个 beam 滑窗拼到通道(P·k)
# ---------------------------------------------------------------------------
class AdjBeamFrontend(FeatureFrontend):
    """B4 邻波束前端：``[B,P,F,S] → [B, P·k, F, S]``。

    对最后一维 S（波束轴）前后各 pad ``k//2`` 个，再取 ``k`` 个长 S 的相邻滑窗，
    沿通道轴(dim=1)拼成 ``P·k`` 通道 → 把"相邻 beam 相关"显式喂给后续 Conv2d。
    """

    def __init__(self, cfg: RxConfig):
        k = cfg.adjbeam_k
        super().__init__(out_channels=cfg.num_ports * k)
        self.k = k
        self.pad = k // 2
        self.num_symbols = cfg.num_symbols

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if x.dim() != 4:
            raise ValueError(
                f"AdjBeamFrontend 期望 4D [B,P,F,S]，got shape={tuple(x.shape)}"
            )
        # 最后一维 S 前后各 pad 个（反射比零填充更保信道边缘连续，但 spec 要求
        # F.pad 默认零填充；保持与 spec 一致，便于对照）。
        xpad = torch.nn.functional.pad(x, (self.pad, self.pad))  # [B,P,F,S+2·pad]
        outs = [
            xpad[:, :, :, i:i + self.num_symbols]
            for i in range(self.k)
        ]                                                          # 每个 [B,P,F,S]
        return torch.cat(outs, dim=1)                              # [B, P·k, F, S]


# ---------------------------------------------------------------------------
# Conv2dBlock —— dense Conv2d 残差块([B, embed, F, S] 同形 I/O)
# ---------------------------------------------------------------------------
class Conv2dBlock(nn.Module):
    """dense Conv2d 残差块：``embed → 2·embed → embed`` 的 (3,3) 卷积 + BN + ReLU。

    全 dense（groups=1 默认），昇腾 Cube 友好；I/O 同形 ``[B, embed, F, S]``，
    残差连接。与 ``_base.StackedBackbone`` 的 Conv1d 主干解耦——本方案在 (F,S) 二维
    上同时混合，故独立定义。
    """

    def __init__(self, embed: int):
        super().__init__()
        self.branch = nn.Sequential(
            nn.Conv2d(embed, 2 * embed, kernel_size=(3, 3), padding=(1, 1)),
            nn.BatchNorm2d(2 * embed),
            nn.ReLU(inplace=True),
            nn.Conv2d(2 * embed, embed, kernel_size=(3, 3), padding=(1, 1)),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.branch(x)


# ---------------------------------------------------------------------------
# FeatAdjBeam —— B4 邻波束 + Conv2d 主干
# ---------------------------------------------------------------------------
@register("feat_adjbeam")
class FeatAdjBeam(RxModelBase):
    """B4 邻波束拼接前端 + Conv2d 主干。

    - 前端：``AdjBeamFrontend`` 把 ``[B,P,F,S] → [B, P·k, F, S]``
    - 主干：``stem Conv2d → N×Conv2dBlock → out Conv2d``，全 dense，把 (F,S) 当 2D 图
    """

    def __init__(self, cfg: RxConfig):
        super().__init__(cfg)
        self.frontend = AdjBeamFrontend(cfg)
        embed = cfg.embed_dim
        self.stem = nn.Conv2d(
            self.frontend.out_channels, embed,
            kernel_size=(3, 3), padding=(1, 1),
        )
        self.blocks = nn.Sequential(
            *[Conv2dBlock(embed) for _ in range(cfg.num_blocks)]
        )
        self.out = nn.Conv2d(
            embed, cfg.num_ports, kernel_size=(3, 3), padding=(1, 1),
        )

    def _forward_core(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, P, F, S]
        h = self.frontend(x)      # [B, P·k, F, S] ≡ [B, C, H=F, W=S]
        h = self.stem(h)          # [B, embed, F, S]
        h = self.blocks(h)        # [B, embed, F, S]
        return self.out(h)        # [B, P, F, S]


def _smoke() -> int:
    import torch
    from .config import RxConfig
    cfg = RxConfig(num_symbols=32)            # P=4,F=48,S=32
    m = FeatAdjBeam(cfg).eval()
    x = torch.randn(*cfg.io_shape)            # [1,4,48,32,1]
    y = m(x)
    assert list(y.shape) == cfg.io_shape, (tuple(y.shape), cfg.io_shape)
    m.train(); m(torch.randn(*cfg.io_shape)).sum().backward()
    print("feat_adjbeam SMOKE_OK", tuple(y.shape))
    return 0


if __name__ == "__main__":
    sys.exit(_smoke())

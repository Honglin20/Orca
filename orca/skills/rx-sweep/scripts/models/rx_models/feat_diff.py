"""feat_diff —— B2 差分先验前端 + CNN 主干。

前端把原信号 P 通道 + 沿子载波轴 F 的每阶中心差分 P 通道,沿通道维拼接 →
``out_channels = P·(1 + len(diff_orders))``(默认 ``P=4, orders=(1,2)`` → 12 通道)。
一阶 ``[-1, 0, 1]``、二阶 ``[1, -2, 1]`` 中心差分核,显式编码边缘/纹理先验,给 CNN
主干补一个"已提炼过"的视角。

差分核用 dense ``Conv1d`` 实现(``weight`` 设成对角差分核,其余位置 0)——退化为
per-channel 差分先验,但**仍走昇腾 Cube**(dense conv),不退化为 Vector 算子。
``bias=False`` 保证纯差分(无直流偏置)。

I/O 由 ``RxModelBase`` 统一为 ``[B,P,F,S,1]``;``_forward_core`` 在 ``[B,P,F,S]`` 上做
"前端扩通道 → 主干映射"。
"""
from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# 自举:直接脚本运行(python rx_models/feat_diff.py)时,重定向为包模块,
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
    sys.exit(importlib.import_module("rx_models.feat_diff")._smoke())

import torch
import torch.nn as nn

from ._base import (
    RxModelBase,
    StackedBackbone,
    build_cnn_blocks,
    FeatureFrontend,
)
from .config import RxConfig
from . import register


# 对角差分核(沿长度轴 conv k=3);扩新阶在此加项后无需改 _init_diff。
_DIFF_KERNELS: dict[int, list[float]] = {
    1: [-1.0, 0.0, 1.0],   # 一阶中心差分(边缘)
    2: [1.0, -2.0, 1.0],   # 二阶差分(曲率/纹理)
}


class DiffFrontend(FeatureFrontend):
    """B2 差分先验前端:``P`` 原通道 + 每阶差分 ``P`` 通道 → ``P·(1+orders)`` 通道。

    每个 ``orders[i]`` 对应一个 dense ``Conv1d(P, P, k=3, pad=1, bias=False)``,
    其 ``weight`` 在 ``_init_diff`` 中初始化为对角差分核:出通道 ``c`` 只看入通道
    ``c``(其余 ``weight[c, j!=c, :] = 0``)→ 功能上 per-channel,但走 dense Cube。
    """

    def __init__(self, cfg: RxConfig):
        super().__init__(out_channels=cfg.num_ports * (1 + len(cfg.diff_orders)))
        self.P = cfg.num_ports
        self.orders = tuple(cfg.diff_orders)
        self.convs = nn.ModuleList([
            nn.Conv1d(self.P, self.P, 3, padding=1, bias=False)
            for _ in self.orders
        ])
        for conv, order in zip(self.convs, self.orders):
            self._init_diff(conv, order)

    @staticmethod
    def _init_diff(conv: nn.Conv1d, order: int) -> None:
        """把 ``conv.weight`` 设为 per-channel 对角差分核(其余位置 0)。

        out通道 ``c`` 只看 in通道 ``c`` 的差分核 → 功能 per-channel,但走 dense Cube。
        fail loud:未知 order / kernel_size 与核长度不符 → raise。
        """
        kernel = _DIFF_KERNELS.get(order)
        if kernel is None:
            raise ValueError(
                f"未知 diff order={order!r}(支持 {sorted(_DIFF_KERNELS)});"
                "扩 _DIFF_KERNELS 后再试"
            )
        ksize = conv.weight.shape[2]
        if ksize != len(kernel):
            raise ValueError(
                f"conv kernel_size={ksize} 与 order-{order} 差分核长度 "
                f"{len(kernel)} 不符"
            )
        with torch.no_grad():
            conv.weight.zero_()   # weight[c, j!=c, :] = 0
            t_kernel = torch.tensor(kernel, dtype=conv.weight.dtype)
            for c in range(conv.weight.shape[0]):
                conv.weight[c, c, :] = t_kernel

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, P, F, S]
        B, P, F_, S = x.shape
        if P != self.P:
            raise ValueError(
                f"DiffFrontend: P={P} 与 self.P={self.P} 不符"
            )
        outs = [x]   # 原信号
        # S 折 batch 沿 F 轴卷积:[B, P, F, S] → [B, S, P, F] → [B*S, P, F]
        xf = x.permute(0, 3, 1, 2).reshape(B * S, self.P, F_)
        for conv in self.convs:
            d = conv(xf)                                          # [B*S, P, F]
            d = d.reshape(B, S, self.P, F_).permute(0, 2, 3, 1)   # [B, P, F, S]
            outs.append(d)
        return torch.cat(outs, dim=1)


@register("feat_diff")
class FeatDiff(RxModelBase):
    """B2 差分先验前端 + CNN 主干。"""

    def __init__(self, cfg: RxConfig):
        super().__init__(cfg)
        self.frontend = DiffFrontend(cfg)
        self.net = StackedBackbone(
            cfg, self.frontend.out_channels, cfg.num_ports, build_cnn_blocks(cfg)
        )

    def _forward_core(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(self.frontend(x))


def _smoke() -> int:
    import torch
    from .config import RxConfig
    cfg = RxConfig(num_symbols=32)            # P=4, F=48, S=32
    m = FeatDiff(cfg).eval()
    x = torch.randn(*cfg.io_shape)            # [1,4,48,32,1]
    y = m(x)
    assert list(y.shape) == cfg.io_shape, (tuple(y.shape), cfg.io_shape)
    m.train(); m(torch.randn(*cfg.io_shape)).sum().backward()
    print("feat_diff SMOKE_OK", tuple(y.shape))
    return 0


if __name__ == "__main__":
    sys.exit(_smoke())

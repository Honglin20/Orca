"""pure_cnn —— 纯 CNN 主干(双轴 dilated dense Conv1d 残差块)。

用 ``DualAxisConvBlock``(频率轴局部先验 + 时间轴 dilated 全局相关 + 残差)替代
model8 的 attention:全程 dense Conv1d,昇腾 Cube 友好,时延更优。dilation 跨 block
按 ``cfg.dilations`` 轮换,RF 覆盖整个波束轴 S。

I/O 由 ``RxModelBase`` 统一为 ``[B,P,F,S,1]``;``_forward_core`` 在
``[B,P,F,S]`` 上做主干映射。
"""
from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# 自举:直接脚本运行时重定向为包模块,让相对 import 可解(同 export_onnx.py)。
# ---------------------------------------------------------------------------
if __package__ in (None, ""):
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _PARENT = os.path.dirname(_HERE)
    if _PARENT not in sys.path:
        sys.path.insert(0, _PARENT)
    import importlib
    sys.exit(importlib.import_module("rx_models.pure_cnn")._smoke())

from ._base import RxModelBase, StackedBackbone, build_cnn_blocks
from .config import RxConfig
from . import register


@register("pure_cnn")
class PureCNN(RxModelBase):
    """纯 CNN 主干:``N × DualAxisConvBlock``。"""

    def __init__(self, cfg: RxConfig):
        super().__init__(cfg)
        self.net = StackedBackbone(
            cfg, cfg.num_ports, cfg.num_ports, build_cnn_blocks(cfg)
        )

    def _forward_core(self, x):
        return self.net(x)


def _smoke() -> int:
    import torch
    from .config import RxConfig
    cfg = RxConfig(num_symbols=32)            # P=4,F=48,S=32
    m = PureCNN(cfg).eval()
    x = torch.randn(*cfg.io_shape)            # [1,4,48,32,1]
    y = m(x)
    assert list(y.shape) == cfg.io_shape, (tuple(y.shape), cfg.io_shape)
    m.train(); m(torch.randn(*cfg.io_shape)).sum().backward()
    print("pure_cnn SMOKE_OK", tuple(y.shape))
    return 0


if __name__ == "__main__":
    sys.exit(_smoke())

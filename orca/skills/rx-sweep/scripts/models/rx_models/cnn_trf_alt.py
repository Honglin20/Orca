"""cnn_trf_alt —— CNN + TRF 交替堆叠主干。

按 ``cfg.cnn_trf_pattern``(默认 ``("cnn","trf")``)在 ``StackedBackbone`` 主干里
交替插入 ``DualAxisConvBlock``(局部先验 + dilated 时间相关)与 ``SignalTransformerBlock``
(全局波束轴 attention),兼得 CNN 的时延优势和 TRF 的全局建模。两类块 I/O 同形
``[B,S,embed_dim,F]`` → ``StackedBackbone`` 可任意混排(Hybrid 前提)。

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
    sys.exit(importlib.import_module("rx_models.cnn_trf_alt")._smoke())

from ._base import RxModelBase, StackedBackbone, build_hybrid_blocks
from .config import RxConfig
from . import register


@register("cnn_trf_alt")
class CNNTRFAlt(RxModelBase):
    """CNN+TRF 交替主干:按 ``cfg.cnn_trf_pattern`` 循环填 ``num_blocks`` 个块。"""

    def __init__(self, cfg: RxConfig):
        super().__init__(cfg)
        self.net = StackedBackbone(
            cfg, cfg.num_ports, cfg.num_ports, build_hybrid_blocks(cfg)
        )

    def _forward_core(self, x):
        return self.net(x)


def _smoke() -> int:
    import torch
    from .config import RxConfig
    cfg = RxConfig(num_symbols=32)            # P=4,F=48,S=32
    m = CNNTRFAlt(cfg).eval()
    x = torch.randn(*cfg.io_shape)            # [1,4,48,32,1]
    y = m(x)
    assert list(y.shape) == cfg.io_shape, (tuple(y.shape), cfg.io_shape)
    m.train(); m(torch.randn(*cfg.io_shape)).sum().backward()
    print("cnn_trf_alt SMOKE_OK", tuple(y.shape))
    return 0


if __name__ == "__main__":
    sys.exit(_smoke())

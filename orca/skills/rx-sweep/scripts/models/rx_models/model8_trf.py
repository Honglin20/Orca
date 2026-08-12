"""model8_trf —— attention 主干 baseline。

原 model8 的 ``SignalTransformerBlock`` 堆叠主干,作为 CNN/TRF 消融实验的对照基线:
全 ``embed_dim`` 通道走波束轴 self-attention(全局相关),无双轴 Conv 的局部先验。
backbone 由 ``build_trf_blocks(cfg)`` 构造,num_blocks 个 t1 块串行。

I/O 由 ``RxModelBase`` 统一为 ``[B,P,F,S,1]``;``_forward_core`` 在
``[B,P,F,S]`` 上做主干映射。
"""
from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# 自举:直接脚本运行(python rx_models/model8_trf.py)时,重定向为包模块,
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
    sys.exit(importlib.import_module("rx_models.model8_trf")._smoke())

from ._base import RxModelBase, StackedBackbone, build_trf_blocks
from .config import RxConfig
from . import register


@register("model8_trf")
class Model8TRF(RxModelBase):
    """attention 主干 baseline:``N × SignalTransformerBlock``。"""

    def __init__(self, cfg: RxConfig):
        super().__init__(cfg)
        self.net = StackedBackbone(
            cfg, cfg.num_ports, cfg.num_ports, build_trf_blocks(cfg)
        )

    def _forward_core(self, x):
        return self.net(x)


def _smoke() -> int:
    import torch
    from .config import RxConfig
    cfg = RxConfig(num_symbols=32)            # P=4,F=48,S=32
    m = Model8TRF(cfg).eval()
    x = torch.randn(*cfg.io_shape)            # [1,4,48,32,1]
    y = m(x)
    assert list(y.shape) == cfg.io_shape, (tuple(y.shape), cfg.io_shape)
    m.train(); m(torch.randn(*cfg.io_shape)).sum().backward()
    print("model8_trf SMOKE_OK", tuple(y.shape))
    return 0


if __name__ == "__main__":
    sys.exit(_smoke())

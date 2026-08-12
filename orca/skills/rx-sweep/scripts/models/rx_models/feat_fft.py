"""feat_fft —— B3 频域 FFT 前端 + CNN 主干。

前端在指定轴(默认 F = 子载波,可切 S = 波束)做 ``torch.fft.fft``,把
``[x, fft(x).real, fft(x).imag]`` 沿通道维拼接 → ``out_channels = 3P``(默认 12 通道),
给 CNN 主干补充频域稀疏视角(子载波轴 FFT 凸出梳齿结构,波束轴 FFT 凸出角度谱)。

注意:
- **FFT 是 Vector 算子(昇腾非 Cube)** —— 走 AICore 的 Vector 单元,不像 dense Conv
  那样吃满 Cube。时延上不占优,本方案验的是"频域稀疏性能否补精度",**不指望降时延**。
- **ONNX 导出受限**(实测 2026-08-12):``torch.onnx.export`` 标准路径不支持
  ``aten::fft_fft`` —— opset 13 / 17 均 ``UnsupportedOperatorError``。本方案仅在
  PyTorch 验精度;若需 ``.om`` 部署,要么改离线 FFT(数据预处理,但动数据管道),
  要么用 DFT 矩阵实现(等价但引入 MatMul,部分抵消降时延)。

I/O 由 ``RxModelBase`` 统一为 ``[B,P,F,S,1]``;``_forward_core`` 在 ``[B,P,F,S]`` 上做
"前端扩通道 → 主干映射"。
"""
from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# 自举:直接脚本运行(python rx_models/feat_fft.py)时,重定向为包模块,
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
    sys.exit(importlib.import_module("rx_models.feat_fft")._smoke())

import torch

from ._base import (
    RxModelBase,
    StackedBackbone,
    build_cnn_blocks,
    FeatureFrontend,
)
from .config import RxConfig
from . import register


class FFTFrontend(FeatureFrontend):
    """B3 频域前端:``[x, fft(x).real, fft(x).imag]`` 沿通道维拼接。

    FFT 在 ``cfg.fft_axis`` 指定轴(``"F"`` 子载波 / ``"S"`` 波束)做,实/虚部各 P
    通道,加原信号 P 通道 → ``3P`` 通道。无学习参数(纯数值变换),增益全靠后续
    CNN 主干从频域分量里挑有用信号。

    Note:
        FFT 是昇腾 Vector 算子(非 Cube);torch.onnx 不支持 ``aten::fft_fft``
        (opset 13/17 实测均失败),无法走 ONNX/.om 部署 —— 仅 PyTorch 验精度用。
    """

    def __init__(self, cfg: RxConfig):
        super().__init__(out_channels=cfg.num_ports * 3)
        self.axis = cfg.fft_axis

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, P, F, S];axis "F"→dim 2,"S"→dim 3
        dim = 2 if self.axis == "F" else 3
        xf = torch.fft.fft(x, dim=dim)
        return torch.cat([x, xf.real, xf.imag], dim=1)


@register("feat_fft")
class FeatFFT(RxModelBase):
    """B3 频域 FFT 前端 + CNN 主干。"""

    def __init__(self, cfg: RxConfig):
        super().__init__(cfg)
        self.frontend = FFTFrontend(cfg)
        self.net = StackedBackbone(
            cfg, self.frontend.out_channels, cfg.num_ports, build_cnn_blocks(cfg)
        )

    def _forward_core(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(self.frontend(x))


def _smoke() -> int:
    import torch
    from .config import RxConfig
    cfg = RxConfig(num_symbols=32)            # P=4, F=48, S=32
    m = FeatFFT(cfg).eval()
    x = torch.randn(*cfg.io_shape)            # [1,4,48,32,1]
    y = m(x)
    assert list(y.shape) == cfg.io_shape, (tuple(y.shape), cfg.io_shape)
    m.train(); m(torch.randn(*cfg.io_shape)).sum().backward()
    print("feat_fft SMOKE_OK", tuple(y.shape))
    return 0


if __name__ == "__main__":
    sys.exit(_smoke())

"""demo_tiny_cnn_dil.py —— kd-nas-demo KB 变体（全 CNN，dilated conv 残差块）。

从仓库真实变体 ``spt_cnn_dilated.py`` 改编精简：用 ``_demo_blocks.DilatedResBlock``（DeepRx 风格
hourglass dilation ``(1,2)`` 标准 Conv1d 残差块，去 BatchNorm 以避开 train-mode batch=1 边界 +
简化 ONNX 导出），无 attention。dilated conv 在频率轴等价稀疏采样 FIR，对应多径时延先验。
默认 ``num_blocks=2 / embed_dim=8``。

I/O 契约同真实变体（见 ``workflows/agents/_kd_scripts/CONTRACTS.md`` §1）。
"""

from __future__ import annotations

import torch.nn as nn

from _demo_blocks import DilatedResBlock, ReceiverShell

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

KNOBS = {
    # min=2：保 feature_hook_names 两 hook 落在 distinct block（main.0 / main.1），
    # 避免 num_blocks=1 时 hook 重复致 KD OFD 特征对齐退化（tune_latency 不缩到 1 块）。
    "num_blocks": {"default": 2, "min": 2, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 8, "min": 4, "step": -2, "leverage": "medium"},
}


class DemoTinyCNNDil(ReceiverShell):
    """全 CNN dilated 主体：N×DilatedResBlock。"""

    def __init__(self, num_blocks: int = 2, embed_dim: int = 8):
        super().__init__(embed_dim=embed_dim)
        self.main = nn.Sequential(*[DilatedResBlock(embed_dim) for _ in range(num_blocks)])


def build_model(**cfg) -> nn.Module:
    """实例化全 CNN dilated 变体。cfg 取 num_blocks / embed_dim（缺省用 KNOBS.default）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    return DemoTinyCNNDil(num_blocks=num_blocks, embed_dim=embed_dim)

"""02_model8_ln3relu.py —— kd-nas-demo KB 变体（原始 model8 + LayerNorm + 3层 + ReLU）。

隔离「缩层 + ReLU」组合（保留原始 LayerNorm），与主变体 ``00_model8_bn3relu`` 对照，
供 sweep 拆分 BN vs (缩层+ReLU) 各自的时延-精度贡献。架构同主变体（原始 model8），
唯一差异：``norm_type="ln"``（保留原始 LayerNorm，elementwise_affine=False）。

I/O 契约同所有变体（见 ``workflows/agents/_kd_scripts/CONTRACTS.md`` §1）。
"""

from __future__ import annotations

import torch.nn as nn

from _model8_student_blocks import SignalProcessingTransformer  # 同目录共享积木

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

KNOBS = {
    "num_blocks": {"default": 3, "min": 2, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 16, "min": 8, "step": -4, "leverage": "medium"},
}

_IN_CHANNELS = 4
_NUM_SYMBOLS = 64
_NUM_SUBCARRIERS = 48
_NORM_TYPE = "ln"
_ACT_TYPE = "relu"


def build_model(**cfg) -> nn.Module:
    """实例化 LN + 3层 + ReLU 变体（cfg 取 num_blocks / embed_dim，缺省用 KNOBS.default）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    block_mtypes = ["t1"] * num_blocks
    return SignalProcessingTransformer(
        block_mtypes=block_mtypes,
        in_channels=_IN_CHANNELS,
        embed_dim=embed_dim,
        num_symbols=_NUM_SYMBOLS,
        num_subcarriers=_NUM_SUBCARRIERS,
        norm_type=_NORM_TYPE,
        act_type=_ACT_TYPE,
    )

"""spt_alt.py —— model8 变体（t1/t2 attention 交替）。

KD-NAS student 候选：``SignalProcessingTransformer``，block 的 attention 在 symbol 轴
（``t1``）与 subcarrier 轴（``t2``）间交替——与 teacher 同模式但更浅更窄。可调旋钮：
``num_blocks`` / ``embed_dim``。

契约同 ``spt_t1.py``（见 ``README.md``）。
"""

from __future__ import annotations

from _model8_blocks import SignalProcessingTransformer  # noqa: F401  (同目录共享积木)
import torch.nn as nn

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

KNOBS = {
    "num_blocks": {"default": 3, "min": 1, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 16, "min": 8, "step": -4, "leverage": "medium"},
}

_IN_CHANNELS = 4
_NUM_SYMBOLS = 64
_NUM_SUBCARRIERS = 48


def build_model(**cfg) -> nn.Module:
    """实例化 t1/t2 交替变体。cfg 取 num_blocks / embed_dim（缺省用 KNOBS.default）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    block_mtypes = ["t1" if i % 2 == 0 else "t2" for i in range(num_blocks)]
    return SignalProcessingTransformer(
        block_mtypes=block_mtypes,
        in_channels=_IN_CHANNELS,
        embed_dim=embed_dim,
        num_symbols=_NUM_SYMBOLS,
        num_subcarriers=_NUM_SUBCARRIERS,
    )

"""spt_t1.py —— model8 变体（全 t1 attention）。

KD-NAS student 候选：``SignalProcessingTransformer``，所有 block 用 symbol 轴 attention
（``m_type="t1"``）。可调旋钮：``num_blocks`` / ``embed_dim``（latency 超阈时由
``tune_latency.py`` 最小缩量）。

契约（见 ``README.md``）：
  - ``build_model(**cfg)`` → ``nn.Module``
  - ``BUILD_FN`` / ``DUMMY_INPUT``（用户真实 I/O 维度）
  - ``KNOBS`` 声明可调旋钮（``step<0``、``leverage∈{high,medium,low}``）
"""

from __future__ import annotations

from _model8_blocks import SignalProcessingTransformer  # noqa: F401  (同目录共享积木)
import torch.nn as nn

# 用户指定（真实模型 I/O；禁硬编码回退——改模型时同步改这里）。
DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

# 可调旋钮：latency 超阈时按 leverage 高→低、step 缩容，刚跨 target 即停。
# embed_dim 须为合理通道数；min 是地板（再缩就破坏结构）。
KNOBS = {
    "num_blocks": {"default": 3, "min": 1, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 16, "min": 8, "step": -4, "leverage": "medium"},
}

# 固定结构参数（非旋钮）。
_IN_CHANNELS = 4
_NUM_SYMBOLS = 64
_NUM_SUBCARRIERS = 48


def build_model(**cfg) -> nn.Module:
    """实例化全 t1 变体。cfg 取 num_blocks / embed_dim（缺省用 KNOBS.default）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    block_mtypes = ["t1"] * num_blocks
    return SignalProcessingTransformer(
        block_mtypes=block_mtypes,
        in_channels=_IN_CHANNELS,
        embed_dim=embed_dim,
        num_symbols=_NUM_SYMBOLS,
        num_subcarriers=_NUM_SUBCARRIERS,
    )

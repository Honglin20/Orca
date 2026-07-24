"""demo_tiny_tf.py —— kd-nas-demo KB 变体（简化 model8，全 t1 transformer）。

从仓库真实变体 ``spt_t1.py`` 改编精简：用 ``_demo_blocks.TinyTransformerBlock``（更小更快、
pointwise QKV/proj、去 LayerNorm），默认 ``num_blocks=2 / embed_dim=8`` → CPU 分钟级 E2E。
所有 block 走 symbol 轴 attention（``m_type="t1"``）。

I/O 契约同真实变体（见 ``workflows/agents/_kd_scripts/CONTRACTS.md`` §1）。
"""

from __future__ import annotations

import torch.nn as nn

from _demo_blocks import NUM_SUBCARRIERS, NUM_SYMBOLS, ReceiverShell, TinyTransformerBlock

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

KNOBS = {
    # min=2：保 feature_hook_names 两 hook 落在 distinct block（main.0 / main.1），
    # 避免 num_blocks=1 时 hook 重复致 KD OFD 特征对齐退化（tune_latency 不缩到 1 块）。
    "num_blocks": {"default": 2, "min": 2, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 8, "min": 4, "step": -2, "leverage": "medium"},
}


class DemoTinyTF(ReceiverShell):
    """全 t1 transformer：N×TinyTransformerBlock(m_type="t1")。"""

    def __init__(self, num_blocks: int = 2, embed_dim: int = 8):
        super().__init__(embed_dim=embed_dim)
        self.main = nn.Sequential(*[
            TinyTransformerBlock(embed_dim, NUM_SYMBOLS, NUM_SUBCARRIERS, m_type="t1")
            for _ in range(num_blocks)
        ])


def build_model(**cfg) -> nn.Module:
    """实例化全 t1 变体。cfg 取 num_blocks / embed_dim（缺省用 KNOBS.default）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    return DemoTinyTF(num_blocks=num_blocks, embed_dim=embed_dim)

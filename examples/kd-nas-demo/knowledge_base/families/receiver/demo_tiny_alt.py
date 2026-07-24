"""demo_tiny_alt.py —— kd-nas-demo KB 变体（简化 model8，t1/t2 attention 交替）。

从仓库真实变体 ``spt_alt.py`` 改编精简：用 ``_demo_blocks.TinyTransformerBlock``，block 的
attention 在 symbol 轴（t1）与 subcarrier 轴（t2）间交替（与 teacher 同模式但更浅更窄）。
默认 ``num_blocks=3 / embed_dim=8``（中等 block 数，与 ``demo_tiny_tf`` 拉开结构差异）。

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
    "num_blocks": {"default": 3, "min": 2, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 8, "min": 4, "step": -2, "leverage": "medium"},
}


class DemoTinyAlt(ReceiverShell):
    """t1/t2 交替 transformer：block i 的 m_type = t1（i 偶）/ t2（i 奇）。"""

    def __init__(self, num_blocks: int = 3, embed_dim: int = 8):
        super().__init__(embed_dim=embed_dim)
        self.main = nn.Sequential(*[
            TinyTransformerBlock(
                embed_dim, NUM_SYMBOLS, NUM_SUBCARRIERS,
                m_type="t1" if i % 2 == 0 else "t2",
            )
            for i in range(num_blocks)
        ])


def build_model(**cfg) -> nn.Module:
    """实例化 t1/t2 交替变体。cfg 取 num_blocks / embed_dim（缺省用 KNOBS.default）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    return DemoTinyAlt(num_blocks=num_blocks, embed_dim=embed_dim)

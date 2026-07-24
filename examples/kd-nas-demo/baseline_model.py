"""baseline_model.py —— kd-nas-demo 原始 baseline（4 层 model8 风格，时延参考 + gpu_probe representative）。

**契约文件**（CONTRACTS §1）：暴露 ``DUMMY_INPUT`` / ``BUILD_FN`` / ``KNOBS`` / ``build_model``。
setup 节点用它：
  - 测 baseline latency（导 ONNX + 用户 latency_provider，参考线，不卡门）；
  - gpu_probe 的 representative_variant（CUDA 机上探测 per-variant 训练显存）。

与 KB 变体**共享** ``_demo_blocks``（经相对 sys.path 引用，不复制积木代码）：4 层全 t1 transformer，
``embed_dim=12``（比 student 变体略宽，作合理 baseline）。随机数据下不追求收敛——latency 是结构属性，
权重随机即可测（见 tune_latency 哲学）。
"""

from __future__ import annotations

import os
import sys

import torch.nn as nn

# 共享 demo KB 的简化积木（不复制 _demo_blocks / _model8_blocks 的代码）。
_KB_RECEIVER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "knowledge_base", "families", "receiver",
)
if _KB_RECEIVER not in sys.path:
    sys.path.insert(0, _KB_RECEIVER)

from _demo_blocks import NUM_SUBCARRIERS, NUM_SYMBOLS, ReceiverShell, TinyTransformerBlock  # noqa: E402

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

KNOBS = {
    "num_blocks": {"default": 4, "min": 2, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 12, "min": 4, "step": -2, "leverage": "medium"},
}


class BaselineModel(ReceiverShell):
    """4 层全 t1 transformer（baseline 时延参考）。"""

    def __init__(self, num_blocks: int = 4, embed_dim: int = 12):
        super().__init__(embed_dim=embed_dim)
        self.main = nn.Sequential(*[
            TinyTransformerBlock(embed_dim, NUM_SYMBOLS, NUM_SUBCARRIERS, m_type="t1")
            for _ in range(num_blocks)
        ])


def build_model(**cfg) -> nn.Module:
    """实例化 baseline。cfg 取 num_blocks / embed_dim（缺省用 KNOBS.default）。"""
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    return BaselineModel(num_blocks=num_blocks, embed_dim=embed_dim)


if __name__ == "__main__":
    # smoke：前向 + 输出 shape 校验。
    import torch
    m = build_model()
    m.eval()
    x = torch.randn(1, 4, 48, 64, 1)
    with torch.no_grad():
        y = m(x)
    assert y.shape == x.shape, (y.shape, x.shape)
    print(f"OK baseline: {len(m.main)} blocks, out={tuple(y.shape)}")

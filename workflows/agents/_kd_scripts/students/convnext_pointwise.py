"""Student family: **ConvNeXt-pointwise** — inverted-bottleneck blocks with
pointwise expand/contract + GELU; norm is BN (foldable), not LayerNorm.

Per block:
  ``y = x + pw2(gelu(pw1(norm(x))))``
  ``pw1: C -> C*expand_ratio`` (1x1 conv)
  ``pw2: C*expand_ratio -> C`` (1x1 conv)

This is the ConvNeXt block with the **7x7 depthwise conv removed** (Ascend
hostile).  All ops are 1x1 Conv1d → GEMM, except an optional k=3 standard
(groups=1) conv at the stem/head for locality.

Taxes removed vs teacher
------------------------
* **softmax attention** — none; cross-position interaction is implicit via
  pointwise expansion (data-driven nonlinearity, no QK^T).
* **im2col** — block body is pure 1x1; only stem/head uses k=3 standard conv.
* **LayerNorm** — replaced by foldable BN.

Ascend: groups=1, channels ÷16, GEMM-only block body.
"""

from __future__ import annotations

import torch
import torch.nn as nn

from _common import (
    BUILD_FN,
    DUMMY_INPUT,
    pointwise_conv,
    signal_forward_wrap,
    standard_conv1d,
    to_per_symbol,
    from_per_symbol,
)


class ConvNeXtPointwiseBlock(nn.Module):
    def __init__(self, channels: int, expand_ratio: int = 4) -> None:
        super().__init__()
        hidden = channels * expand_ratio
        self.norm = nn.BatchNorm1d(channels)
        self.pw1 = pointwise_conv(channels, hidden)
        self.act = nn.GELU()
        self.pw2 = pointwise_conv(hidden, channels)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        y = self.norm(x)
        y = self.pw1(y)
        y = self.act(y)
        y = self.pw2(y)
        return x + y


class ConvNeXtPointwiseBackbone(nn.Module):
    def __init__(
        self,
        num_blocks: int = 3,
        embed_dim: int = 16,
        expand_ratio: int = 4,
    ) -> None:
        super().__init__()
        if embed_dim % 16 != 0:
            raise ValueError(f"embed_dim must be ÷16 aligned, got {embed_dim}")
        self.stem = standard_conv1d(4, embed_dim, k=3)
        self.stem_bn = nn.BatchNorm1d(embed_dim)
        self.blocks = nn.ModuleList(
            [ConvNeXtPointwiseBlock(embed_dim, expand_ratio) for _ in range(num_blocks)]
        )
        self.head = standard_conv1d(embed_dim, 4, k=3)

    def feature_hook_names(self) -> list[str]:
        return ["blocks.0", f"blocks.{len(self.blocks) - 1}"]

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x, shape = to_per_symbol(x)                          # [B*64, 4, 48]
        x = torch.relu(self.stem_bn(self.stem(x)))
        for blk in self.blocks:
            x = blk(x)
        x = self.head(x)
        return from_per_symbol(x, shape)


def build_model(**cfg) -> nn.Module:
    backbone = ConvNeXtPointwiseBackbone(
        num_blocks=cfg.get("num_blocks", 3),
        embed_dim=cfg.get("embed_dim", 16),
        expand_ratio=cfg.get("expand_ratio", 4),
    )

    class _Wrapper(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.backbone = backbone

        def feature_hook_names(self) -> list[str]:
            return [f"backbone.{n}" for n in self.backbone.feature_hook_names()]

        def forward(self, inp: torch.Tensor) -> torch.Tensor:
            return signal_forward_wrap(inp, self.backbone)

    return _Wrapper()

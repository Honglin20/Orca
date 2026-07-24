"""Student family: **DeepRx-dilated** — dilated 1D conv stack in the DeepRx lineage.

Taxes removed vs teacher
------------------------
* **softmax attention** — none; global receptive field built from a geometric
  dilation sequence (1, 2, 4, 8) instead of QK^T.
* **Transpose / QKV layout shuffles** — single per-symbol reshape up-front,
  everything else is channels-first Conv1d.
* **im2col on large kernels** — only k=3 convs with escalating dilation, so
  im2col footprint stays tiny while receptive field covers all 48 subcarriers.

Ascend friendliness
-------------------
* groups=1 everywhere (no DW / grouped conv — hostile on Ascend).
* channels aligned to 16/32 (÷16 rule).
* BN (foldable) not LN.
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


class DilatedResBlock(nn.Module):
    def __init__(self, channels: int, dilation: int) -> None:
        super().__init__()
        self.conv = standard_conv1d(channels, channels, k=3, dilation=dilation)
        self.bn = nn.BatchNorm1d(channels)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return torch.relu(self.bn(self.conv(x)) + x)


class DeepRxDilatedBackbone(nn.Module):
    def __init__(
        self,
        num_blocks: int = 3,
        embed_dim: int = 16,
        dilation_pattern: tuple[int, ...] = (1, 2, 4, 8),
    ) -> None:
        super().__init__()
        if embed_dim % 16 != 0:
            raise ValueError(f"embed_dim must be ÷16 aligned, got {embed_dim}")
        self.stem = standard_conv1d(4, embed_dim, k=3)
        self.stem_bn = nn.BatchNorm1d(embed_dim)
        self.blocks = nn.ModuleList(
            [DilatedResBlock(embed_dim, dilation_pattern[i % len(dilation_pattern)])
             for i in range(num_blocks)]
        )
        self.head = standard_conv1d(embed_dim, 4, k=3)

    def feature_hook_names(self) -> list[str]:
        return ["blocks.0", f"blocks.{len(self.blocks) - 1}"]

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, 4, 48, 64]
        x, shape = to_per_symbol(x)           # [B*64, 4, 48]
        x = torch.relu(self.stem_bn(self.stem(x)))
        for blk in self.blocks:
            x = blk(x)
        x = self.head(x)
        return from_per_symbol(x, shape)      # [B, 4, 48, 64]


def build_model(**cfg) -> nn.Module:
    class _Wrapper(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.backbone = DeepRxDilatedBackbone(
                num_blocks=cfg.get("num_blocks", 3),
                embed_dim=cfg.get("embed_dim", 16),
                dilation_pattern=tuple(cfg.get("dilation_pattern", (1, 2, 4, 8))),
            )

        def feature_hook_names(self) -> list[str]:
            return [f"backbone.{n}" for n in self.backbone.feature_hook_names()]

        def forward(self, inp: torch.Tensor) -> torch.Tensor:
            return signal_forward_wrap(inp, self.backbone)

    return _Wrapper()

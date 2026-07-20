"""Student family: **Large-kernel 1D conv** — global receptive field built from
large (k=7..15) standard Conv1d with dilation, replacing attention.

A single k=13 dilated conv spans 25 subcarriers; two stacked cover the full 48.
We force standard (groups=1) conv to stay Ascend-friendly — **no depthwise**.

Taxes removed vs teacher
------------------------
* **softmax attention** — none; global context comes from large kernels with
  explicit, local-constraint weights (inductive bias favours smooth channel
  response — exactly what we want for OFDM).
* **QKV / Transpose layout shuffles** — single per-symbol reshape, Conv1d
  channels-first throughout.
* **GELU inside attention FFN** — ReLU only.

Ascend: groups=1; channels ÷16; pad dilated so output shape matches input.
"""

from __future__ import annotations

import torch
import torch.nn as nn

from _common import (
    BUILD_FN,
    DUMMY_INPUT,
    signal_forward_wrap,
    standard_conv1d,
    to_per_symbol,
    from_per_symbol,
)


class LargeKernelBlock(nn.Module):
    def __init__(self, channels: int, kernel: int, dilation: int) -> None:
        super().__init__()
        self.conv = standard_conv1d(channels, channels, k=kernel, dilation=dilation)
        self.bn = nn.BatchNorm1d(channels)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return torch.relu(self.bn(self.conv(x)) + x)


class LargeKernelBackbone(nn.Module):
    def __init__(
        self,
        kernel: int = 15,
        dilation: int = 2,
        num_blocks: int = 2,
        embed_dim: int = 16,
    ) -> None:
        super().__init__()
        if embed_dim % 16 != 0:
            raise ValueError(f"embed_dim must be ÷16 aligned, got {embed_dim}")
        self.stem = standard_conv1d(4, embed_dim, k=3)
        self.stem_bn = nn.BatchNorm1d(embed_dim)
        self.blocks = nn.ModuleList(
            [LargeKernelBlock(embed_dim, kernel, dilation) for _ in range(num_blocks)]
        )
        self.head = standard_conv1d(embed_dim, 4, k=3)

    def feature_hook_names(self) -> list[str]:
        return ["blocks.0", f"blocks.{len(self.blocks) - 1}"]

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x, shape = to_per_symbol(x)
        x = torch.relu(self.stem_bn(self.stem(x)))
        for blk in self.blocks:
            x = blk(x)
        x = self.head(x)
        return from_per_symbol(x, shape)


def build_model(**cfg) -> nn.Module:
    backbone = LargeKernelBackbone(
        kernel=cfg.get("kernel", 15),
        dilation=cfg.get("dilation", 2),
        num_blocks=cfg.get("num_blocks", 2),
        embed_dim=cfg.get("embed_dim", 16),
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

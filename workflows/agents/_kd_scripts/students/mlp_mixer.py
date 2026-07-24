"""Student family: **MLP-Mixer** — token-/channel-mixing MLPs, fully pointwise.

Per block:
  ``y = x + MLP_token(norm(x))``      (Linear along mixer_axis)
  ``y = y + MLP_channel(norm(y))``    (Linear over embed_dim)

All linears are pointwise: token-mixing is a 1x1 conv on the spatial axis,
channel-mixing is a 1x1 conv on the channel axis.  Zero im2col.  The only
permute is a static axis swap (Ascend compiler folds it).

Taxes removed vs teacher
------------------------
* **softmax attention** — none; global token mixing is a fixed Linear, not
  data-dependent QK^T.
* **Transpose broadcasts** — at most one static permute per block, foldable.
* **GELU inside FFN** — ReLU only (cheaper on Ascend).

Ascend: 1x1 conv (GEMM) + BN; channels ÷16; mixer_axis ∈ {symbol, subcarrier, both}.
"""

from __future__ import annotations

import torch
import torch.nn as nn

from _common import BUILD_FN, DUMMY_INPUT, pointwise_conv, signal_forward_wrap


class ChannelMix(nn.Module):
    """Pointwise 1x1 Conv1d channel mixer."""

    def __init__(self, channels: int, mlp_ratio: int = 2) -> None:
        super().__init__()
        hidden = channels * mlp_ratio
        self.fc1 = pointwise_conv(channels, hidden)
        self.fc2 = pointwise_conv(hidden, channels)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, C, L]
        return self.fc2(torch.relu(self.fc1(x)))


class MixerBlock(nn.Module):
    def __init__(
        self,
        embed_dim: int,
        axis_len: int,
        mlp_ratio: int = 2,
    ) -> None:
        super().__init__()
        self.norm1 = nn.BatchNorm1d(embed_dim)
        self.token_mix = pointwise_conv(axis_len, axis_len)
        self.norm2 = nn.BatchNorm1d(embed_dim)
        self.channel_mix = ChannelMix(embed_dim, mlp_ratio)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, embed_dim, axis_len]
        # token-mix along the axis (1x1 conv on that axis)
        t = self.token_mix(self.norm1(x).permute(0, 2, 1)).permute(0, 2, 1)
        x = x + t
        # channel-mix
        x = x + self.channel_mix(self.norm2(x))
        return x


class MLPMixerBackbone(nn.Module):
    def __init__(
        self,
        num_blocks: int = 2,
        embed_dim: int = 16,
        mlp_ratio: int = 2,
        mixer_axis: str = "subcarrier",
    ) -> None:
        super().__init__()
        if mixer_axis not in {"symbol", "subcarrier", "both"}:
            raise ValueError(f"bad mixer_axis: {mixer_axis}")
        if embed_dim % 16 != 0:
            raise ValueError(f"embed_dim must be ÷16 aligned, got {embed_dim}")
        self.mixer_axis = mixer_axis
        self.embed_dim = embed_dim

        self.stem = pointwise_conv(4, embed_dim)
        self.stem_bn = nn.BatchNorm1d(embed_dim)

        sub_len, sym_len = 48, 64
        if mixer_axis == "subcarrier":
            self.blocks_sub = nn.ModuleList(
                [MixerBlock(embed_dim, sub_len, mlp_ratio) for _ in range(num_blocks)]
            )
            self.blocks_sym = nn.ModuleList()
        elif mixer_axis == "symbol":
            self.blocks_sub = nn.ModuleList()
            self.blocks_sym = nn.ModuleList(
                [MixerBlock(embed_dim, sym_len, mlp_ratio) for _ in range(num_blocks)]
            )
        else:  # both — alternate subcarrier and symbol mixing
            self.blocks_sub = nn.ModuleList(
                [MixerBlock(embed_dim, sub_len, mlp_ratio) for _ in range(num_blocks)]
            )
            self.blocks_sym = nn.ModuleList(
                [MixerBlock(embed_dim, sym_len, mlp_ratio) for _ in range(num_blocks)]
            )
        self.head = pointwise_conv(embed_dim, 4)

    def feature_hook_names(self) -> list[str]:
        names: list[str] = []
        if len(self.blocks_sub) > 0:
            names += ["blocks_sub.0", f"blocks_sub.{len(self.blocks_sub) - 1}"]
        if len(self.blocks_sym) > 0:
            names += ["blocks_sym.0"]
        return names or ["stem"]

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, 4, 48, 64]; unfold to [B, 4, 48*64] for pointwise along axis
        B, P, S, M = x.shape
        # We work in channels-first 3D: [B, C, L] where L is the *other* axis.
        x = x.reshape(B, P, S * M)
        x = torch.relu(self.stem_bn(self.stem(x)))            # [B, embed_dim, S*M]
        # For subcarrier mixing: reshape to [B, embed_dim, S, M] -> per-symbol mixer
        # Operate per M-axis so token_mix is 1x1 along S (subcarrier).
        if self.mixer_axis in ("subcarrier", "both"):
            x = x.reshape(B, self.embed_dim, S, M).permute(0, 3, 1, 2).reshape(B * M, self.embed_dim, S)
            for blk in self.blocks_sub:
                x = blk(x)
            x = x.reshape(B, M, self.embed_dim, S).permute(0, 2, 3, 1).reshape(B, self.embed_dim, S * M)
        if self.mixer_axis in ("symbol", "both"):
            x = x.reshape(B, self.embed_dim, S, M).permute(0, 2, 1, 3).reshape(B * S, self.embed_dim, M)
            for blk in self.blocks_sym:
                x = blk(x)
            x = x.reshape(B, S, self.embed_dim, M).permute(0, 2, 1, 3).reshape(B, self.embed_dim, S * M)
        x = self.head(x).reshape(B, 4, S, M)
        return x


def build_model(**cfg) -> nn.Module:
    backbone = MLPMixerBackbone(
        num_blocks=cfg.get("num_blocks", 2),
        embed_dim=cfg.get("embed_dim", 16),
        mlp_ratio=cfg.get("mlp_ratio", 2),
        mixer_axis=cfg.get("mixer_axis", "subcarrier"),
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

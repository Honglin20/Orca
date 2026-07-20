"""Student family: **EqDeepRx-shared** — LMMSE + RZF parallel front ends feeding
a pointwise DetectorNN shared across all 4 ports, followed by a 1D denoise head.

Pipeline
--------
1. Front ends (constant buffers):
   * ``h_lmmse = W_lmmse · Y`` (closed-form LMMSE, identity default).
   * ``h_rzf   = W_rzf   · Y`` (regularised zero-forcing, scaled-identity default).
2. Stack the two estimates along the port axis -> 8 channels.
3. Pointwise DetectorNN: per-symbol 1x1 Conv1d stack — **zero im2col** —
   shared across all 4 ports (``shared_detector=True``).  When disabled,
   per-port detectors run and a pointwise reduce merges them back to
   ``embed_dim``.
4. 1D k=3 denoise Conv1d (standard, groups=1) to restore local smoothness,
   projecting back to 4 channels.

Taxes removed vs teacher
------------------------
* **softmax attention** — none; multi-front-end diversity replaces QK^T.
* **im2col** — pointwise DetectorNN is pure 1x1 (GEMM); only the final denoise
  uses k=3 standard conv.
* **Transpose** — single per-symbol reshape, channels-first throughout.

Ascend: groups=1 everywhere; channels ÷16; all pointwise ops lower to GEMM.
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


class LinearFrontEnd(nn.Module):
    """Closed-form linear estimator (LMMSE or RZF). W is a constant buffer."""

    def __init__(self, num_subcarriers: int = 48, scale: float = 1.0) -> None:
        super().__init__()
        W = torch.eye(num_subcarriers) * scale
        self.register_buffer("W", W)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, P, S, M]; out[b,p,i,m] = sum_j W[i,j] x[b,p,j,m]
        return torch.einsum("ij,bpjm->bpim", self.W, x)


class PointwiseDetector(nn.Module):
    """1x1 Conv1d stack — pure GEMM, zero im2col."""

    def __init__(self, in_ch: int, embed_dim: int, num_blocks: int = 2) -> None:
        super().__init__()
        self.stem = pointwise_conv(in_ch, embed_dim)
        self.stem_bn = nn.BatchNorm1d(embed_dim)
        self.blocks = nn.ModuleList(
            [nn.Sequential(pointwise_conv(embed_dim, embed_dim),
                           nn.BatchNorm1d(embed_dim),
                           nn.ReLU()) for _ in range(num_blocks)]
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = torch.relu(self.stem_bn(self.stem(x)))
        for blk in self.blocks:
            x = blk(x) + x
        return x


class EqDeepRxSharedBackbone(nn.Module):
    def __init__(
        self,
        shared_detector: bool = True,
        embed_dim: int = 16,
        num_subcarriers: int = 48,
        num_blocks: int = 2,
    ) -> None:
        super().__init__()
        if embed_dim % 16 != 0:
            raise ValueError(f"embed_dim must be ÷16 aligned, got {embed_dim}")
        self.lmmse = LinearFrontEnd(num_subcarriers, scale=1.0)
        self.rzf = LinearFrontEnd(num_subcarriers, scale=0.9)   # scaled-identity proxy
        self._shared = shared_detector
        if shared_detector:
            self.detector = PointwiseDetector(4 * 2, embed_dim, num_blocks)
        else:
            self.detector = nn.ModuleList(
                [PointwiseDetector(2, embed_dim, num_blocks) for _ in range(4)]
            )
            self.merge = pointwise_conv(embed_dim * 4, embed_dim)
            self.merge_bn = nn.BatchNorm1d(embed_dim)
        self.denoise = standard_conv1d(embed_dim, 4, k=3)
        self.denoise_bn = nn.BatchNorm1d(4)

    def feature_hook_names(self) -> list[str]:
        if self._shared:
            return ["detector.stem", f"detector.blocks.{len(self.detector.blocks) - 1}"]
        return ["detector.0.stem", "merge"]

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, 4, 48, 64]
        feat = torch.cat([self.lmmse(x), self.rzf(x)], dim=1)   # [B, 8, 48, 64]
        per_sym, shape = to_per_symbol(feat)                    # [B*64, 8, 48]
        if self._shared:
            per_sym = self.detector(per_sym)                    # [B*64, embed_dim, 48]
        else:
            # 4 ports × 2 front ends -> 4 detectors
            port_chunks = [per_sym[:, i * 2:(i + 1) * 2, :] for i in range(4)]
            port_outs = [self.detector[i](port_chunks[i]) for i in range(4)]
            per_sym = torch.relu(self.merge_bn(self.merge(torch.cat(port_outs, dim=1))))
        per_sym = torch.relu(self.denoise_bn(self.denoise(per_sym)))   # [B*64, 4, 48]
        return from_per_symbol(per_sym, (shape[0], 4, shape[2], shape[3]))


def build_model(**cfg) -> nn.Module:
    backbone = EqDeepRxSharedBackbone(
        shared_detector=cfg.get("shared_detector", True),
        embed_dim=cfg.get("embed_dim", 16),
        num_blocks=cfg.get("num_blocks", 2),
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

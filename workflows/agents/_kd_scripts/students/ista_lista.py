"""Student family: **ISTA / LISTA** — unfolded soft-thresholding network.

Each of ``unfold_steps`` layers does:

    z = W · x                       (pointwise Linear)
    z = soft_threshold(z, τ)        (τ learnable per step)
    x = x + z                       (residual)

When ``cfg["use_fft"] = True`` the thresholding is done in the delay (FFT)
domain — ``rfft`` along subcarrier, threshold, ``irfft`` back.  Default is
``use_fft = False`` for **ONNX / Ascend safety** (FFT ops are not universally
supported by Ascend ONNX-RT), in which case the layer degenerates to a pure
Linear + soft-threshold — still a valid LISTA cell.

Taxes removed vs teacher
------------------------
* **softmax attention** — none; sparsity-inducing soft-threshold replaces
  data-dependent QK^T.  This is closer to the true channel-estimation
  prior (sparse multipath in delay domain).
* **Transpose shuffles** — single per-symbol reshape, Conv1d channels-first.
* **GELU / FFN** — replaced by soft-threshold (piecewise-linear, cheap).

Ascend: groups=1, channels ÷16, default path is pure Linear + ReLU-like op;
FFT path is opt-in via cfg.
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


def _soft_threshold_real(z: torch.Tensor, tau: torch.Tensor) -> torch.Tensor:
    return torch.sign(z) * torch.relu(z.abs() - tau)


def _soft_threshold_complex(Z: torch.Tensor, tau: torch.Tensor) -> torch.Tensor:
    mag = Z.abs()
    scale = torch.relu(mag - tau) / (mag + 1e-8)
    return Z * scale


class ISTAStep(nn.Module):
    def __init__(self, channels: int, use_fft: bool, default_tau: float = 0.1) -> None:
        super().__init__()
        self.lin = pointwise_conv(channels, channels)
        self.norm = nn.BatchNorm1d(channels)
        self.tau = nn.Parameter(torch.tensor(default_tau))
        self.use_fft = use_fft

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [N, C, L]
        z = self.norm(self.lin(x))
        if self.use_fft:
            Z = torch.fft.rfft(z, dim=-1)
            Z = _soft_threshold_complex(Z, self.tau)
            z = torch.fft.irfft(Z, n=z.shape[-1], dim=-1)
        else:
            z = _soft_threshold_real(z, self.tau)
        return x + z


class ISTALISTABackbone(nn.Module):
    def __init__(
        self,
        unfold_steps: int = 3,
        embed_dim: int = 16,
        use_fft: bool = False,
        learnable_tau: bool = True,
    ) -> None:
        super().__init__()
        if embed_dim % 16 != 0:
            raise ValueError(f"embed_dim must be ÷16 aligned, got {embed_dim}")
        self.stem = standard_conv1d(4, embed_dim, k=3)
        self.stem_bn = nn.BatchNorm1d(embed_dim)
        self.steps = nn.ModuleList(
            [ISTAStep(embed_dim, use_fft) for _ in range(unfold_steps)]
        )
        if not learnable_tau:
            for s in self.steps:
                s.tau.requires_grad_(False)
        self.head = standard_conv1d(embed_dim, 4, k=3)

    def feature_hook_names(self) -> list[str]:
        return ["steps.0", f"steps.{len(self.steps) - 1}"]

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x, shape = to_per_symbol(x)
        x = torch.relu(self.stem_bn(self.stem(x)))
        for step in self.steps:
            x = step(x)
        x = self.head(x)
        return from_per_symbol(x, shape)


def build_model(**cfg) -> nn.Module:
    backbone = ISTALISTABackbone(
        unfold_steps=cfg.get("unfold_steps", 3),
        embed_dim=cfg.get("embed_dim", 16),
        use_fft=cfg.get("use_fft", False),
        learnable_tau=cfg.get("learnable_tau", True),
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

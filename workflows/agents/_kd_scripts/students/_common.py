"""Shared scaffolding for all kd-nas student architectures.

Teacher (``SignalProcessingTransformer``) I/O contract that **every** student
must reproduce verbatim (see CONTRACTS.md §1):

* input  ``[B, num_ports=4, num_subcarriers=48, num_symbols=64, 1]``
* squeeze the trailing 1 -> ``[B, 4, 48, 64]``
* alpha normalisation:
    ``alpha = sqrt(mean(inp**2, dim=[1,2,3], keepdim=True) * 2)``
    ``x     = inp / (alpha + 1e-6)``
* backbone(x) -> ``[B, 4, 48, 64]``
* ``out = backbone(x) * alpha``
* unsqueeze back -> ``[B, 4, 48, 64, 1]``

This module centralises that boilerplate so each family only implements its
backbone.  It also exposes the two Conv1d building blocks mandated by the
contract (``pointwise_conv`` and ``standard_conv1d``) plus the shared
``DUMMY_INPUT`` / ``BUILD_FN`` constants.
"""

from __future__ import annotations

import torch
import torch.nn as nn

# ---------------------------------------------------------------------------
# Shared constants — every family re-exports these verbatim.
# ---------------------------------------------------------------------------
DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

NUM_PORTS = 4
NUM_SUBCARRIERS = 48
NUM_SYMBOLS = 64


# ---------------------------------------------------------------------------
# Alpha normalisation — identical to teacher's first/last ops.
# ---------------------------------------------------------------------------
class AlphaNorm(nn.Module):
    """Replicates ``SignalProcessingTransformer.forward`` head/tail ops.

    On forward: accepts ``[B,4,48,64,1]`` (or ``[B,4,48,64]``), squeezes the
    trailing dim if present, computes ``alpha`` over the three signal dims,
    returns ``(x_normalised, alpha, squeeze_flag)``.  Pair with
    :func:`signal_unscale` to rebuild the output tensor.
    """

    def forward(self, inp: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, bool]:
        squeeze = inp.dim() == 5 and inp.shape[-1] == 1
        if squeeze:
            inp = torch.squeeze(inp, dim=-1)
        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)
        return x, alpha, squeeze


def signal_forward_wrap(inp: torch.Tensor, backbone: nn.Module) -> torch.Tensor:
    """Run the full teacher-shaped forward around an arbitrary backbone.

    ``backbone`` receives the normalised ``[B, 4, 48, 64]`` tensor and must
    return the same shape; this helper rescales by ``alpha`` and restores the
    trailing dim.
    """
    x, alpha, squeeze = AlphaNorm()(inp)
    out = backbone(x)
    if out.shape[-3:] != (NUM_PORTS, NUM_SUBCARRIERS, NUM_SYMBOLS):
        raise ValueError(
            f"student backbone returned shape {tuple(out.shape)}; "
            f"expected [..., {NUM_PORTS}, {NUM_SUBCARRIERS}, {NUM_SYMBOLS}]"
        )
    out = out * alpha
    if squeeze:
        out = torch.unsqueeze(out, dim=-1)
    return out


# ---------------------------------------------------------------------------
# Conv1d building blocks (Ascend-friendly: k=1 / k=3 standard, no DW / group).
# ---------------------------------------------------------------------------
def pointwise_conv(in_c: int, out_c: int, bias: bool = True) -> nn.Conv1d:
    """1x1 Conv1d — zero im2col, pure matrix-matrix, fastest path on Ascend."""
    return nn.Conv1d(in_c, out_c, kernel_size=1, bias=bias)


def standard_conv1d(
    in_c: int,
    out_c: int,
    k: int = 3,
    pad: int | None = None,
    dilation: int = 1,
    bias: bool = True,
) -> nn.Conv1d:
    """Vanilla kx3 Conv1d (groups=1, no depthwise). Pad defaults to ``dilation*(k//2)``."""
    if pad is None:
        pad = (k // 2) * dilation
    return nn.Conv1d(in_c, out_c, kernel_size=k, padding=pad, dilation=dilation, bias=bias)


# ---------------------------------------------------------------------------
# Per-symbol reshape helpers — most students borrow the teacher's
# ``[B, num_symbols, ports, num_subcarriers]`` -> ``[B*M, C, S]`` layout so
# Conv1d runs along the subcarrier axis (Ascend-friendly, no Transpose).
# ---------------------------------------------------------------------------
def to_per_symbol(x: torch.Tensor) -> torch.Tensor:
    """``[B, 4, 48, 64]`` -> ``[B*64, 4, 48]`` (channels-first, subcarrier-length)."""
    B, P, S, M = x.shape
    x = x.permute(0, 3, 1, 2).contiguous()      # [B, M, P, S]
    return x.reshape(B * M, P, S), (B, P, S, M)


def from_per_symbol(x: torch.Tensor, shape: tuple[int, int, int, int]) -> torch.Tensor:
    """Inverse of :func:`to_per_symbol`."""
    B, P, S, M = shape
    x = x.reshape(B, M, P, S)
    return x.permute(0, 2, 3, 1).contiguous()    # [B, P, S, M]

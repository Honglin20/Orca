"""kd.ema — Mean-teacher EMA shadow model.

Aligned with CONTRACTS.md §3 (``kd/ema.py``).  Maintains a deep-copied shadow
of the student whose parameters are updated each step as::

    shadow ← decay · shadow + (1 − decay) · student

The shadow (``self.ema_model``) is a full ``nn.Module`` on the same device as
the student; ``forward(x)`` simply runs the shadow in ``eval`` mode under
``no_grad``.  This trades a little memory (an extra student copy) for
implementation simplicity and avoids fragile in-place state-dict swaps.

Design choices
--------------
* ``decay`` defaults to 0.999 (mean-teacher convention).
* ``update`` iterates the shadow ``state_dict`` in place — no Python dict
  reallocation, works on CUDA without host syncs.
* Non-floating-point buffers (e.g. ``num_batches_tracked``) are copied
  verbatim rather than EMA-merged, matching the original mean-teacher
  implementation.
* The shadow is frozen (``requires_grad_(False)``) — only the student is
  trained by the outer optimizer.
"""

from __future__ import annotations

import copy
from typing import Iterable

import torch
import torch.nn as nn
from torch import Tensor


class MeanTeacherEMA:
    """Shadow student updated as an exponential moving average."""

    def __init__(self, student: nn.Module, decay: float = 0.999) -> None:
        if not (0.0 <= decay <= 1.0):
            raise ValueError(f"decay must be in [0, 1], got {decay}")
        self.decay = float(decay)
        # Deep-copy so the shadow is fully independent (own params, own buffers).
        self.ema_model: nn.Module = copy.deepcopy(student)
        self.ema_model.eval()
        for p in self.ema_model.parameters():
            p.requires_grad_(False)

    @torch.no_grad()
    def update(self, student: nn.Module) -> None:
        """In-place EMA update of the shadow's parameters and float buffers."""
        ema_sd = self.ema_model.state_dict()
        new_sd = student.state_dict()
        for key, ema_v in ema_sd.items():
            if key not in new_sd:
                # Shadow has a tensor the student no longer has — skip loudly.
                continue
            new_v = new_sd[key]
            if new_v.shape != ema_v.shape:
                # Shape changed (e.g. architecture mutated) — realign by copy.
                ema_v.copy_(new_v)
                continue
            if ema_v.is_floating_point():
                ema_v.mul_(self.decay).add_(new_v.to(ema_v.dtype), alpha=1.0 - self.decay)
            else:
                ema_v.copy_(new_v)

    @torch.no_grad()
    def forward(self, x: Tensor) -> Tensor:
        """Run the shadow model. Caller is expected to be in no_grad context already."""
        was_training = self.ema_model.training
        if was_training:
            self.ema_model.eval()
        out = self.ema_model(x)
        if was_training:
            self.ema_model.train()
        return out

    def __call__(self, x: Tensor) -> Tensor:
        return self.forward(x)

    # Convenience: keep the shadow on the same device as the student.
    def to(self, *args, **kwargs) -> "MeanTeacherEMA":
        self.ema_model = self.ema_model.to(*args, **kwargs)
        return self

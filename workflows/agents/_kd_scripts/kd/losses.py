"""kd.losses — KD loss primitives, model-agnostic, pure PyTorch.

Aligned with CONTRACTS.md §3 (``kd/losses.py``).  Every function detaches the
teacher tensor internally; callers never need to remember ``.detach()``.
Where student/teacher feature shapes differ, internal 1x1 adapter modules
(``HintRegressor`` / ``OFDAdapter`` / ``FitNetsAdapter``) project the student
feature onto the teacher's channel dim.  Adapter parameters live only during
training — at deployment the student is exported alone and the adapters are
discarded.

The functional forms (``ofd_feature_loss`` / ``fitnets_hint_loss``) are thin
wrappers around freshly-allocated adapter modules: they are convenient for
quick experiments / sanity tests but **the adapters they allocate are not
trained**.  For real training, use ``OFDAdapter`` / ``FitNetsAdapter`` as
``nn.Module`` instances (owned by :class:`kd.compose.KDComposite`) so their
parameters land in the optimizer — see ``kd/compose.py``.
"""

from __future__ import annotations

import random
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor


# ---------------------------------------------------------------------------
# Shape helpers
# ---------------------------------------------------------------------------
def _flatten_feat(feat: Tensor) -> Tensor:
    """Flatten any [..., C] tensor to 2-D ``[N, C]`` for pairwise losses."""
    if feat.dim() <= 2:
        return feat
    return feat.flatten(1)


def _channel_of(feat: Tensor) -> tuple[int, bool]:
    """Return ``(channel_dim, is_spatial)``.

    Convention: if ``feat.dim() >= 3`` the channel axis is ``1`` and the
    remaining axes are spatial; otherwise the tensor is a 2-D ``[N, C]`` stack
    with channel on the last axis.
    """
    if feat.dim() >= 3:
        return int(feat.shape[1]), True
    return int(feat.shape[-1]), False


def _align_spatial(s: Tensor, t_shape: torch.Size) -> Tensor:
    """Best-effort align of student spatial dims to teacher's.

    Channel adapter already equalised the channel axis; if the remaining
    spatial dims still differ we interpolate (area for 2-D, linear for 1-D,
    repeat/truncate for anything weirder).  Same shape → returned unchanged.
    """
    if s.shape == t_shape:
        return s
    if s.dim() != len(t_shape):
        # different rank: flatten both spatial tails and interpolate in 1-D
        b = s.shape[0]
        c = s.shape[1] if s.dim() >= 3 else s.shape[-1]
        s_flat = s.reshape(b, c, -1)                       # [B, C, M]
        t_target = 1
        for d in t_shape[2:] if s.dim() >= 3 else t_shape:
            t_target *= d
        s_flat = F.interpolate(s_flat, size=t_target, mode="linear", align_corners=False)
        # reshape to teacher shape if rank matches, else leave flat
        try:
            return s_flat.reshape(t_shape)
        except RuntimeError:
            return s_flat
    # same rank, different spatial sizes
    if s.dim() == 4 and len(t_shape) == 4:
        return F.interpolate(s, size=tuple(t_shape[2:]), mode="area")
    if s.dim() == 3 and len(t_shape) == 3:
        return F.interpolate(s, size=tuple(t_shape[2:]), mode="linear", align_corners=False)
    return s


# ---------------------------------------------------------------------------
# Core losses
# ---------------------------------------------------------------------------
def mse_kd(s_out: Tensor, t_out: Tensor) -> Tensor:
    """Vanilla output MSE; teacher is detached."""
    return F.mse_loss(s_out, t_out.detach())


def ema_consistency_loss(s_out: Tensor, ema_out: Tensor) -> Tensor:
    """Mean-teacher consistency: student vs. EMA-of-student. EMA target detached."""
    return F.mse_loss(s_out, ema_out.detach())


def _pairwise_dist(x: Tensor) -> Tensor:
    """Normalised pairwise L2 distance matrix of rows of ``x`` ([N, D]).

    Distances are divided by the max distance in the batch so the scale is
    comparable across layers and batch sizes — this is the standard RKD
    normalisation (Tian et al., 2020) and keeps ``smooth_l1`` well-behaved.
    """
    dot = x @ x.t()
    sq_norm = (x * x).sum(dim=1, keepdim=True)
    d2 = sq_norm + sq_norm.t() - 2.0 * dot
    d2 = d2.clamp(min=0.0)
    d = torch.sqrt(d2 + 1e-12)
    max_d = d.max().clamp(min=1e-12)
    return d / max_d


def rkd_distance_loss(s_feat: Tensor, t_feat: Tensor) -> Tensor:
    """RKD distance loss (pairwise distance alignment)."""
    s = _flatten_feat(s_feat)
    t = _flatten_feat(t_feat.detach())
    if s.shape[0] != t.shape[0]:
        n = min(s.shape[0], t.shape[0])
        s, t = s[:n], t[:n]
    d_s = _pairwise_dist(s)
    d_t = _pairwise_dist(t)
    return F.smooth_l1_loss(d_s, d_t.detach())


def _sin_between(a: Tensor, b: Tensor) -> Tensor:
    """Per-row ``|sin(theta)|`` between corresponding rows of ``a`` and ``b``.

    Uses ``sin = sqrt(1 - cos^2)`` (clamped) so it generalises to arbitrary
    feature dimensionality — there is no cross product in high-D.
    """
    dot = (a * b).sum(dim=1)
    na = a.norm(dim=1).clamp(min=1e-12)
    nb = b.norm(dim=1).clamp(min=1e-12)
    cos = (dot / (na * nb)).clamp(-1.0 + 1e-7, 1.0 - 1e-7)
    return torch.sqrt(1.0 - cos * cos)


def rkd_angle_loss(
    s_feat: Tensor,
    t_feat: Tensor,
    max_triplets: int = 1024,
    generator: Optional[torch.Generator] = None,
) -> Tensor:
    """RKD angle loss over sampled within-batch triplets.

    For each triplet ``(i, j, k)`` we measure the sine of the angle at the
    anchor ``i`` between the two arms ``(j-i)`` and ``(k-i)``, on both student
    and teacher features, and align them with ``smooth_l1``.  Triplets are
    capped at ``max_triplets`` (default 1024) to bound memory regardless of
    batch size.
    """
    s = _flatten_feat(s_feat)
    t = _flatten_feat(t_feat.detach())
    n = min(s.shape[0], t.shape[0])
    if n < 3:
        # Too few rows to form a triplet — contribute zero rather than crashing.
        return s.new_zeros(())
    s, t = s[:n], t[:n]

    triplets = _sample_triplets(n, max_triplets, generator, device=s.device)
    if triplets.numel() == 0:
        return s.new_zeros(())

    I = triplets[:, 0]
    J = triplets[:, 1]
    K = triplets[:, 2]

    a_s, b_s = s[J] - s[I], s[K] - s[I]
    a_t, b_t = t[J] - t[I], t[K] - t[I]

    sin_s = _sin_between(a_s, b_s)
    sin_t = _sin_between(a_t, b_t)
    return F.smooth_l1_loss(sin_s, sin_t.detach())


def _sample_triplets(
    n: int,
    max_triplets: int,
    generator: Optional[torch.Generator],
    device: torch.device,
) -> Tensor:
    """Return a ``[T, 3]`` long tensor of distinct triplets (i != j != k != i).

    Enumerates when the full triplet count is small enough; otherwise samples
    uniformly without replacement until ``max_triplets`` distinct ones are
    collected.
    """
    full = n * (n - 1) * (n - 2)
    if full <= max_triplets:
        idx = torch.cartesian_prod(
            torch.arange(n, device=device),
            torch.arange(n, device=device),
            torch.arange(n, device=device),
        )
        mask = (idx[:, 0] != idx[:, 1]) & (idx[:, 0] != idx[:, 2]) & (idx[:, 1] != idx[:, 2])
        return idx[mask]
    seen = set()
    out = []
    # Python-side RNG keeps it deterministic if the caller passes a torch.Generator
    # via manual seeding of ``random``; otherwise this is best-effort.
    while len(out) < max_triplets:
        i = random.randint(0, n - 1)
        j = random.randint(0, n - 1)
        k = random.randint(0, n - 1)
        if i == j or i == k or j == k:
            continue
        key = (i, j, k)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    if not out:
        return torch.empty((0, 3), dtype=torch.long, device=device)
    return torch.tensor(out, dtype=torch.long, device=device)


# ---------------------------------------------------------------------------
# Hint adapter (1x1 channel projection)
# ---------------------------------------------------------------------------
class HintRegressor(nn.Module):
    """1x1 channel projection used by OFD / FitNets.

    A single :class:`nn.Linear` (no bias) implements the projection; for
    spatial features ``[B, C, *spatial]`` we move the channel axis to the end,
    apply the linear, and move it back.  Functionally equivalent to a 1x1
    convolution but uniform across feature ranks.

    The adapter is initialised lazily — the caller typically constructs it
    only after seeing the first batch's feature shapes (see ``OFDAdapter``).
    """

    def __init__(self, in_dim: int, out_dim: int) -> None:
        super().__init__()
        self.in_dim = int(in_dim)
        self.out_dim = int(out_dim)
        self.proj = nn.Linear(self.in_dim, self.out_dim, bias=False)

    def forward(self, x: Tensor) -> Tensor:
        if x.dim() <= 2:
            return self.proj(x)
        # [B, C, *spatial] -> [B, *spatial, C] -> project -> back.
        x_perm = x.movedim(1, -1)
        y_perm = self.proj(x_perm)
        return y_perm.movedim(-1, 1)


# ---------------------------------------------------------------------------
# Multi-stage OFD adapter (trainable)
# ---------------------------------------------------------------------------
class OFDAdapter(nn.Module):
    """Overhaul Feature Distribution (OFD) adapter.

    Holds one :class:`HintRegressor` per stage; builds lazily on first forward
    once the student/teacher feature shapes are known.  Owns trainable
    parameters — register with the optimizer via
    ``OFDAdapter.parameters()`` (kd.compose.KDComposite does this for you).

    The loss is per-stage MSE between adapted student features and (detached)
    teacher features.
    """

    def __init__(self) -> None:
        super().__init__()
        self.adapters: Optional[nn.ModuleList] = None
        self._built_for: Optional[tuple] = None

    def _build(self, s_feats: list[Tensor], t_feats: list[Tensor]) -> None:
        if len(s_feats) != len(t_feats):
            raise ValueError(
                f"OFDAdapter: student/teacher feature lists must have equal length "
                f"(got {len(s_feats)} vs {len(t_feats)})"
            )
        adapters = []
        for s, t in zip(s_feats, t_feats):
            s_c, _ = _channel_of(s)
            t_c, _ = _channel_of(t)
            adapters.append(HintRegressor(s_c, t_c))
        self.adapters = nn.ModuleList(adapters)
        # Migrate to the student feature's device/dtype.
        ref = s_feats[0]
        self.adapters = self.adapters.to(device=ref.device, dtype=ref.dtype)
        self._built_for = tuple((s.shape, t.shape) for s, t in zip(s_feats, t_feats))

    def forward(self, s_feats: list[Tensor], t_feats: list[Tensor]) -> Tensor:
        if not s_feats or not t_feats:
            raise ValueError("OFDAdapter: empty feature list")
        if self.adapters is None or len(self.adapters) != len(s_feats):
            self._build(s_feats, t_feats)
        total = s_feats[0].new_zeros(())
        for adapter, s, t in zip(self.adapters, s_feats, t_feats):
            s_aligned = adapter(s)
            t_d = t.detach()
            if s_aligned.shape != t_d.shape:
                s_aligned = _align_spatial(s_aligned, t_d.shape)
            total = total + F.mse_loss(s_aligned, t_d)
        return total


# ---------------------------------------------------------------------------
# FitNets single-stage hint adapter (trainable)
# ---------------------------------------------------------------------------
class FitNetsAdapter(nn.Module):
    """Single-point FitNets hint: one :class:`HintRegressor` over one stage.

    The stage index is chosen by the caller (typically the deepest stage where
    student and teacher shapes are compatible).  Loss is MSE between the
    adapted student feature and the (detached) teacher feature.
    """

    def __init__(self) -> None:
        super().__init__()
        self.adapter: Optional[HintRegressor] = None

    def _build(self, s_feat: Tensor, t_feat: Tensor) -> None:
        s_c, _ = _channel_of(s_feat)
        t_c, _ = _channel_of(t_feat)
        self.adapter = HintRegressor(s_c, t_c).to(device=s_feat.device, dtype=s_feat.dtype)

    def forward(self, s_feat: Tensor, t_feat: Tensor) -> Tensor:
        if self.adapter is None:
            self._build(s_feat, t_feat)
        s_aligned = self.adapter(s_feat)
        t_d = t_feat.detach()
        if s_aligned.shape != t_d.shape:
            s_aligned = _align_spatial(s_aligned, t_d.shape)
        return F.mse_loss(s_aligned, t_d)


# ---------------------------------------------------------------------------
# Functional wrappers (non-trained adapters; for quick tests only)
# ---------------------------------------------------------------------------
def ofd_feature_loss(s_feats: list[Tensor], t_feats: list[Tensor]) -> Tensor:
    """Functional OFD loss.

    Allocates a fresh :class:`OFDAdapter` on the fly and runs it.  The
    adapter is **not** retained, so its parameters are never trained — use
    this only for sanity checks.  Real training must go through
    :class:`kd.compose.KDComposite` which owns a persistent adapter.
    """
    return OFDAdapter()(s_feats, t_feats)


def fitnets_hint_loss(s_feat: Tensor, t_feat: Tensor) -> Tensor:
    """Functional FitNets hint loss (non-trained adapter; see OFD caveat)."""
    return FitNetsAdapter()(s_feat, t_feat)


# ---------------------------------------------------------------------------
# Weight scheduler
# ---------------------------------------------------------------------------
class KDWeightScheduler:
    """Three-stage anneal for a single KD term.

    * ``current < start``                → 0
    * ``start ≤ current < start+warmup`` → linear ramp 0 → ``target_weight``
    * ``current ≥ start + warmup``       → ``target_weight``

    The shape mirrors common warmup schedules used for distillation terms so
    the user task loss can dominate early training and KD contribution ramps
    in only once the student has stabilised.
    """

    def __init__(
        self,
        target_weight: float,
        start: int = 0,
        warmup_length: int = 0,
    ) -> None:
        if warmup_length < 0:
            raise ValueError(f"warmup_length must be >= 0, got {warmup_length}")
        if start < 0:
            raise ValueError(f"start must be >= 0, got {start}")
        self.target_weight = float(target_weight)
        self.start = int(start)
        self.warmup_length = int(warmup_length)

    def get_weight(self, current: int) -> float:
        if current < self.start:
            return 0.0
        if self.warmup_length == 0:
            return self.target_weight
        elapsed = current - self.start
        if elapsed >= self.warmup_length:
            return self.target_weight
        return self.target_weight * (elapsed / self.warmup_length)

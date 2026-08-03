"""kd.compose — assemble user task loss + KD terms into one callable.

Aligned with CONTRACTS.md §3 (``kd/compose.py``).

The composite loss is::

    loss = user_loss_fn(s_out, y)
         + Σ_{name in kd_losses}  scheduler_name(epoch) · weight_name · term_name(...)

where ``scheduler_name`` is a :class:`kd.losses.KDWeightScheduler` built from
``kd_config["scheduler"]``.  If ``kd_config["ema"]`` is true, an additional
``ema_consistency_loss`` term is appended (gated by its own scheduler).

Adapter parameters (:class:`OFDAdapter`, :class:`FitNetsAdapter`) are owned
by this composite and exposed via :meth:`KDComposite.kd_parameters` so the
outer optimizer can include them.  Adapters are built lazily on the first
feature-bearing forward — callers that want their parameters registered with
the optimizer **before** the first step should call :meth:`prepare` with a
sample feature batch (the ``train_adapter_template.py`` does this).
"""

from __future__ import annotations

from typing import Callable, Optional

import torch
from torch import Tensor

from .losses import (
    FitNetsAdapter,
    KDWeightScheduler,
    OFDAdapter,
    ema_consistency_loss,
    fitnets_hint_loss,
    mse_kd,
    ofd_feature_loss,
    rkd_angle_loss,
    rkd_distance_loss,
)


VALID_KD_LOSSES = ("mse", "rkd", "ofd", "fitnets")

# 特征蒸馏项（依赖 s_feats/t_feats 非空）。compose 守卫：含特征项且 feats 空 → fail loud
# （distill agent 默认 KD_CONFIG 已 AST 条件化按 student.feature_hook_names 决定是否启用）。
# 从 VALID_KD_LOSSES 派生（DRY）：未来加新特征项只需改 VALID_KD_LOSSES。
_FEATURE_TERMS = tuple(n for n in VALID_KD_LOSSES if n != "mse")


class KDComposite:
    """Callable KD loss assembled from a user task loss + a kd_config block.

    Calling convention (matches CONTRACTS §3)::

        kd_loss(s_out, y, s_feats, t_out, t_feats, ema_out, epoch) -> Tensor
    """

    def __init__(self, user_loss_fn: Callable[[Tensor, Tensor], Tensor], kd_config: dict) -> None:
        if not callable(user_loss_fn):
            raise TypeError(f"user_loss_fn must be callable, got {type(user_loss_fn)}")
        self.user_loss_fn = user_loss_fn
        self.kd_config = dict(kd_config or {})

        self.kd_losses: list[str] = list(self.kd_config.get("kd_losses", []))
        unknown = [n for n in self.kd_losses if n not in VALID_KD_LOSSES]
        if unknown:
            raise ValueError(
                f"unknown kd_losses entries {unknown}; allowed {VALID_KD_LOSSES}"
            )

        self.weights: dict[str, float] = dict(self.kd_config.get("weights", {}))

        self.use_ema: bool = bool(self.kd_config.get("ema", False))

        # distill 模式铁律：student 训练必须加载 KD loss（软标签/特征对齐项）。
        # 空 kd_losses 且未开 ema = 纯 task loss，这不是蒸馏，fail loud。
        # build_kd_loss 仅在 train_pipeline --mode distill 被调（teacher/eval 模式不构造），
        # 故此断言天然只约束 distill。
        if not self.kd_losses and not self.use_ema:
            raise ValueError(
                "KDComposite 构造时 kd_losses 为空且 ema=False —— 这是纯 task loss，"
                "不是蒸馏。distill 模式必须给非空 kd_losses（如 ['mse']）或开启 ema。"
                "（teacher/eval 模式不构造 KDComposite，天然不触发。）"
            )

        sched_cfg: dict = dict(self.kd_config.get("scheduler", {}))
        self.schedulers: dict[str, KDWeightScheduler] = {}
        target_weights = sched_cfg.get("target_weights", {}) or {}
        for name in self.kd_losses:
            tw = target_weights.get(name, self.weights.get(name, 1.0))
            self.schedulers[name] = KDWeightScheduler(
                target_weight=float(tw),
                start=int(sched_cfg.get("start", 0)),
                warmup_length=int(sched_cfg.get("warmup_length", 0)),
            )
        if self.use_ema:
            ema_tw = target_weights.get("ema", self.weights.get("ema", 1.0))
            self.schedulers["ema"] = KDWeightScheduler(
                target_weight=float(ema_tw),
                start=int(sched_cfg.get("start", 0)),
                warmup_length=int(sched_cfg.get("warmup_length", 0)),
            )

        # Lazy adapters — populated by ``prepare`` or on first feature-bearing call.
        self.ofd_adapter: Optional[OFDAdapter] = None
        self.fitnets_adapter: Optional[FitNetsAdapter] = None

    # ------------------------------------------------------------------ API
    def kd_parameters(self):
        """Return all trainable adapter parameters (OFD + FitNets)."""
        params: list[Tensor] = []
        if self.ofd_adapter is not None:
            params.extend(list(self.ofd_adapter.parameters()))
        if self.fitnets_adapter is not None:
            params.extend(list(self.fitnets_adapter.parameters()))
        return params

    def prepare(
        self,
        s_feats_sample: Optional[list[Tensor]] = None,
        t_feats_sample: Optional[list[Tensor]] = None,
    ) -> None:
        """Pre-build adapters from a feature sample so their parameters land
        in the optimizer before the first backward.

        Call this **after** the first student/teacher forward and **before**
        constructing the optimizer.  No-op for configs without OFD/FitNets.
        """
        if s_feats_sample is None or t_feats_sample is None:
            return
        if len(s_feats_sample) != len(t_feats_sample):
            raise ValueError(
                f"prepare: student/teacher feature sample length mismatch "
                f"({len(s_feats_sample)} vs {len(t_feats_sample)})"
            )
        if "ofd" in self.kd_losses and self.ofd_adapter is None and s_feats_sample:
            self.ofd_adapter = OFDAdapter()
            # Trigger lazy build on the sample's device/dtype.
            _ = self.ofd_adapter(s_feats_sample, t_feats_sample)
        if (
            "fitnets" in self.kd_losses
            and self.fitnets_adapter is None
            and s_feats_sample
        ):
            self.fitnets_adapter = FitNetsAdapter()
            idx = len(s_feats_sample) // 2
            _ = self.fitnets_adapter(s_feats_sample[idx], t_feats_sample[idx])

    # ----------------------------------------------------------------- call
    def __call__(
        self,
        s_out: Tensor,
        y: Tensor,
        s_feats: Optional[list[Tensor]],
        t_out: Tensor,
        t_feats: Optional[list[Tensor]],
        ema_out: Optional[Tensor],
        epoch: int,
    ) -> Tensor:
        loss = self.user_loss_fn(s_out, y)
        # 注：空 kd_losses ∧ ema off 已在 __init__ 拒绝（distill 必加载 KD），此处必非空。
        s_feats_list = list(s_feats) if s_feats else []
        t_feats_list = list(t_feats) if t_feats else []

        # fail-loud 守卫（SPEC §1.2(1)）：配置含特征项但运行时 feats 空 → 抛 ValueError。
        # 旧逻辑 _compute_term 对 ofd/fitnets/rkd 在 feats 空时静默 return None，主调 continue
        # → 配置声称 ofd 在跑、实际只跑 mse，违反 Rule 12。守卫看运行时 feats（不看 prepare 历史）。
        # 仅当含特征项且 feats 空才抛；mse / ema-only 不受影响（mse 不依赖 feats）。
        feature_requested = [n for n in self.kd_losses if n in _FEATURE_TERMS]
        if feature_requested and (not s_feats_list or not t_feats_list):
            raise ValueError(
                f"kd_losses 含特征项 {feature_requested} 但无 feature feats"
                f"（s_feats={len(s_feats_list)}, t_feats={len(t_feats_list)}）。"
                f"特征蒸馏需模型暴露 feature_hook_names() 且 forward 经过该层。"
                f"给 student 加 feature_hook_names()，或从 kd_losses 去掉特征项（只留 mse）。"
            )

        for name in self.kd_losses:
            weight = float(self.weights.get(name, 1.0))
            scheduler = self.schedulers.get(name)
            sched_w = scheduler.get_weight(epoch) if scheduler else 1.0
            if sched_w <= 0.0:
                continue
            term = self._compute_term(name, s_out, t_out, s_feats_list, t_feats_list)
            if term is None:
                continue
            loss = loss + sched_w * weight * term

        if self.use_ema and ema_out is not None:
            weight = float(self.weights.get("ema", 1.0))
            sched_w = self.schedulers["ema"].get_weight(epoch)
            if sched_w > 0.0:
                loss = loss + sched_w * weight * ema_consistency_loss(s_out, ema_out)

        return loss

    # ------------------------------------------------------------- internals
    def _compute_term(
        self,
        name: str,
        s_out: Tensor,
        t_out: Tensor,
        s_feats: list[Tensor],
        t_feats: list[Tensor],
    ) -> Optional[Tensor]:
        if name == "mse":
            return mse_kd(s_out, t_out)

        if name == "rkd":
            if not s_feats or not t_feats:
                return None
            if len(s_feats) != len(t_feats):
                raise ValueError(
                    f"rkd: feature list length mismatch {len(s_feats)} vs {len(t_feats)}"
                )
            # Use the deepest stage for richer relational structure.
            s_f = s_feats[-1]
            t_f = t_feats[-1]
            return rkd_distance_loss(s_f, t_f) + rkd_angle_loss(s_f, t_f)

        if name == "ofd":
            if not s_feats or not t_feats:
                return None
            if len(s_feats) != len(t_feats):
                raise ValueError(
                    f"ofd: feature list length mismatch {len(s_feats)} vs {len(t_feats)}"
                )
            if self.ofd_adapter is None:
                self.ofd_adapter = OFDAdapter()
            return self.ofd_adapter(s_feats, t_feats)

        if name == "fitnets":
            if not s_feats or not t_feats:
                return None
            if len(s_feats) != len(t_feats):
                raise ValueError(
                    f"fitnets: feature list length mismatch {len(s_feats)} vs {len(t_feats)}"
                )
            if self.fitnets_adapter is None:
                self.fitnets_adapter = FitNetsAdapter()
            idx = len(s_feats) // 2
            return self.fitnets_adapter(s_feats[idx], t_feats[idx])

        raise ValueError(f"unknown kd_loss entry: {name}")


def build_kd_loss(user_loss_fn: Callable[[Tensor, Tensor], Tensor], kd_config: dict) -> KDComposite:
    """Factory required by CONTRACTS §3 — returns a callable KD loss."""
    return KDComposite(user_loss_fn, kd_config)

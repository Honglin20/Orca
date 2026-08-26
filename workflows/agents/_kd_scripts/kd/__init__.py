"""kd — model-agnostic knowledge-distillation primitives (pure PyTorch).

Implements the public surface declared in CONTRACTS.md §3:

* :mod:`kd.losses`     — KD loss terms + weight scheduler + hint adapter.
* :mod:`kd.wrapper`    — TeacherCache (teacher forward + hook capture) and
                         KDStudentWrapper (student forward + hook capture).
* :mod:`kd.compose`    — Assembles user task loss + KD terms into one callable.
* :mod:`kd.ema`        — Mean-teacher EMA shadow model.

All teacher tensors are auto-detached inside this package.  Dimension
misalignment between student/teacher intermediate features is absorbed by
internal 1x1 adapters (``HintRegressor`` / ``OFDAdapter`` / ``FitNetsAdapter``)
that live only for training and are dropped at deployment.

Importing ``kd`` is side-effect-free; submodules import lazily so the package
can be loaded without CUDA / without the user's training module present.
"""

from __future__ import annotations

from .losses import (
    KDWeightScheduler,
    HintRegressor,
    OFDAdapter,
    FitNetsAdapter,
    mse_kd,
    rkd_distance_loss,
    rkd_angle_loss,
    ofd_feature_loss,
    fitnets_hint_loss,
    ema_consistency_loss,
)
from .wrapper import KDStudentWrapper, TeacherCache
from .compose import KDComposite, build_kd_loss
from .ema import MeanTeacherEMA

__all__ = [
    # losses
    "KDWeightScheduler",
    "HintRegressor",
    "OFDAdapter",
    "FitNetsAdapter",
    "mse_kd",
    "rkd_distance_loss",
    "rkd_angle_loss",
    "ofd_feature_loss",
    "fitnets_hint_loss",
    "ema_consistency_loss",
    # wrapper
    "KDStudentWrapper",
    "TeacherCache",
    # compose
    "KDComposite",
    "build_kd_loss",
    # ema
    "MeanTeacherEMA",
]

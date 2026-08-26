"""kd.wrapper — TeacherCache + KDStudentWrapper.

Aligned with CONTRACTS.md §3 (``kd/wrapper.py``).  Two responsibilities:

1. :class:`TeacherCache` — loads the (frozen) teacher model + its state dict
   and registers forward hooks on the named intermediate submodules so a
   single ``forward(x)`` returns ``(logits, [feat_stage0, ...])``.  The
   teacher stays resident in memory for the duration of training — per the
   task spec, the teacher is never exported to ONNX and only used during
   distillation.

2. :class:`KDStudentWrapper` — wraps the student so its forward returns
   ``(logits, [feat_stage0, ...])`` captured from the student's own
   ``feature_hook_names`` submodules.  The wrapper is a thin ``nn.Module``
   and the inner student is accessible via ``.student`` so the existing
   training loop (optimizer, EMA, etc.) keeps working unchanged.

.. note::

   The teacher's hook names are supplied by the caller (typically the
   ``kd-teacher-setup`` agent picks the 6 hint layers).  The student's hook
   names come from ``student.feature_hook_names()`` (CONTRACTS §1).  The two
   lists must have the same length and correspond stage-for-stage for any
   feature-based KD term (OFD / FitNets / RKD); ``kd.compose`` will raise
   loudly if they do not.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from typing import Optional

import torch
import torch.nn as nn
from torch import Tensor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_submodule(root: nn.Module, dotted_name: str) -> nn.Module:
    """Walk a dotted attribute path from ``root``; fail loud on miss."""
    mod = root
    for part in dotted_name.split("."):
        if not hasattr(mod, part):
            raise AttributeError(
                f"cannot resolve submodule '{dotted_name}': '{part}' not on {type(mod).__name__}"
            )
        mod = getattr(mod, part)
    if not isinstance(mod, nn.Module):
        raise TypeError(
            f"'{dotted_name}' resolved to {type(mod).__name__}, not nn.Module"
        )
    return mod


def _import_from_path(path: str, module_name: str) -> object:
    """Import a .py file by absolute path; insert into sys.modules for reuse."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"model file not found: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# TeacherCache
# ---------------------------------------------------------------------------
class TeacherCache(nn.Module):
    """Frozen teacher + forward hooks.

    Built either programmatically (:meth:`build`) or from a persisted cache
    file (:meth:`load`).  Persisted cache blob shape::

        {
          "teacher_model_path": "<abs path to teacher model .py>",
          "state_dict":         <OrderedDict>,
          "hook_names":         [<dotted submodule name>, ...],
          "dummy_input_shape":  [B, P, S, M, 1],
        }
    """

    DEFAULT_BUILD_FN = "build_model"

    def __init__(
        self,
        teacher: nn.Module,
        teacher_model_path: str,
        hook_names: list[str],
        dummy_input_shape: list[int],
    ) -> None:
        super().__init__()
        self.teacher = teacher
        self.teacher_model_path = teacher_model_path
        self.hook_names = list(hook_names)
        self.dummy_input_shape = list(dummy_input_shape)
        self._feat_dict: dict[str, Tensor] = {}
        self._handles: list[torch.utils.hooks.RemovableHandle] = []
        self._register_hooks()
        # Sanity warmup — also catches shape mismatches early.
        with torch.no_grad():
            dummy = torch.zeros(*self.dummy_input_shape)
            _ = self.teacher(dummy)

    # ------------------------------------------------------------------ build
    @classmethod
    def build(
        cls,
        teacher_model_path: str,
        teacher_state_dict,
        hook_names: list[str],
        dummy_input_shape: list[int],
        build_fn: Optional[str] = None,
    ) -> "TeacherCache":
        """Construct from a model .py path and a state dict.

        ``teacher_state_dict`` may be either an actual ``state_dict`` or a
        path to a ``.pt`` / ``.ckpt`` file we should ``torch.load``.
        """
        module = _import_from_path(teacher_model_path, "_kd_teacher_model")
        fn_name = build_fn or getattr(module, "BUILD_FN", cls.DEFAULT_BUILD_FN)
        if not hasattr(module, fn_name):
            raise AttributeError(
                f"teacher model module has no build function '{fn_name}' "
                f"(looked in {teacher_model_path})"
            )
        teacher = getattr(module, fn_name)()

        if isinstance(teacher_state_dict, str):
            sd = torch.load(teacher_state_dict, map_location="cpu")
        else:
            sd = teacher_state_dict
        # Be lenient about 'module.' prefixes from DataParallel-trained ckpts.
        if any(k.startswith("module.") for k in sd):
            sd = {k.replace("module.", "", 1): v for k, v in sd.items()}
        teacher.load_state_dict(sd)
        teacher.eval()
        for p in teacher.parameters():
            p.requires_grad_(False)
        return cls(
            teacher=teacher,
            teacher_model_path=teacher_model_path,
            hook_names=hook_names,
            dummy_input_shape=dummy_input_shape,
        )

    @classmethod
    def load(cls, path: str) -> "TeacherCache":
        """Read ``teacher_cache.pt`` (4-field blob) and call :meth:`build`."""
        if not os.path.isfile(path):
            raise FileNotFoundError(f"teacher_cache not found: {path}")
        blob = torch.load(path, map_location="cpu")
        for key in ("teacher_model_path", "state_dict", "hook_names", "dummy_input_shape"):
            if key not in blob:
                raise KeyError(f"teacher_cache blob missing '{key}': {path}")
        return cls.build(
            teacher_model_path=blob["teacher_model_path"],
            teacher_state_dict=blob["state_dict"],
            hook_names=blob["hook_names"],
            dummy_input_shape=blob["dummy_input_shape"],
        )

    def save(self, path: str) -> None:
        """Persist the 4-field cache blob. Teacher stays in memory after save."""
        torch.save(
            {
                "teacher_model_path": self.teacher_model_path,
                "state_dict": self.teacher.state_dict(),
                "hook_names": list(self.hook_names),
                "dummy_input_shape": list(self.dummy_input_shape),
            },
            path,
        )

    # ------------------------------------------------------------------ hooks
    def _register_hooks(self) -> None:
        for name in self.hook_names:
            sub = _resolve_submodule(self.teacher, name)

            def make_hook(nm: str):
                def hook(_m, _i, out):
                    # Out may be a tuple (rare) — take the first tensor.
                    if isinstance(out, tuple):
                        out = out[0]
                    self._feat_dict[nm] = out
                return hook

            self._handles.append(sub.register_forward_hook(make_hook(name)))

    # ----------------------------------------------------------------- forward
    def forward(self, x: Tensor) -> tuple[Tensor, list[Tensor]]:
        """Run teacher forward under no_grad; return (logits, [feat per hook])."""
        self._feat_dict = {}
        with torch.no_grad():
            out = self.teacher(x)
        feats: list[Tensor] = []
        for name in self.hook_names:
            if name not in self._feat_dict:
                raise RuntimeError(
                    f"teacher hook '{name}' did not fire — verify the submodule "
                    f"is on the forward path of the teacher"
                )
            feats.append(self._feat_dict[name])
        return out, feats


# ---------------------------------------------------------------------------
# KDStudentWrapper
# ---------------------------------------------------------------------------
class KDStudentWrapper(nn.Module):
    """Wrap a student so ``forward`` returns ``(logits, [feat per hook])``.

    The student's intermediate features are captured via forward hooks
    registered on the submodules named by ``hook_names`` (typically
    ``student.feature_hook_names()``).  The inner student is exposed as
    ``.student`` so user training code (optimizer, schedulers, EMA) can keep
    referencing it directly.
    """

    def __init__(self, student: nn.Module, hook_names: Optional[list[str]] = None) -> None:
        super().__init__()
        self.student = student
        if hook_names is None:
            fn = getattr(student, "feature_hook_names", None)
            if not callable(fn):
                raise ValueError(
                    "hook_names not provided and student has no feature_hook_names()"
                )
            hook_names = list(fn())
        self.hook_names = list(hook_names)
        self._feat_dict: dict[str, Tensor] = {}
        self._handles: list[torch.utils.hooks.RemovableHandle] = []
        for name in self.hook_names:
            sub = _resolve_submodule(self.student, name)

            def make_hook(nm: str):
                def hook(_m, _i, out):
                    if isinstance(out, tuple):
                        out = out[0]
                    self._feat_dict[nm] = out
                return hook

            self._handles.append(sub.register_forward_hook(make_hook(name)))

    def feature_hook_names(self) -> list[str]:
        """Pass-through: prefer student's own ``feature_hook_names`` if present."""
        fn = getattr(self.student, "feature_hook_names", None)
        if callable(fn):
            return list(fn())
        return list(self.hook_names)

    def forward(self, x: Tensor) -> tuple[Tensor, list[Tensor]]:
        self._feat_dict = {}
        out = self.student(x)
        feats: list[Tensor] = []
        for name in self.hook_names:
            if name not in self._feat_dict:
                raise RuntimeError(
                    f"student hook '{name}' did not fire — verify the submodule "
                    f"is on the forward path"
                )
            feats.append(self._feat_dict[name])
        return out, feats

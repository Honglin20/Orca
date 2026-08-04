"""kd.trainer — fixed KD-NAS training engine.

Three modes:

* ``teacher`` — pure task-loss training, save a teacher ckpt consumable by
  ``teacher_setup.py`` (schema: ``{state_dict, build_cfg, variant_id, epochs,
  final_loss, mode}``).
* ``distill`` — task-loss + KD composite loss (hot-order: prepare →
  kd_parameters → optimizer), save a student ckpt (schema:
  ``{student_state_dict, variant_id, student_cfg, kd_config, epochs,
  proxy_mse, mode}``).
* ``eval`` — load a ckpt, run ``leaves.eval_metric``, emit the accuracy
  protocol block.  Read-only — no ckpt written.

Discipline:

* The engine only ``print``s to stdout; the caller redirects to
  ``runs/<exp>/train.log``.  No FileHandler, no log file owned here.
* **Dual protocol**:

    - stdout keys ``TEACHER_CKPT`` / ``STUDENT_CKPT`` / ``KD_LOSS_FINAL`` /
      ``KD_PROXY_MSE`` / ``STUDENT_ACCURACY`` / ``STUDENT_ACCURACY_KIND`` /
      ``MET_ACCURACY`` / ``ACCURACY_CONFIDENCE`` for the agent to parse.
    - **log prefix protocol** anchored on ``metrics_tail._LOSS_LINE_RE``
      (metrics_tail.py:72-75): teacher prints
      ``[train_pipeline:teacher] epoch=N loss_avg=F`` per epoch, distill
      prints ``[train_pipeline:distill] epoch=N kd_loss_avg=F``.  Eval mode
      emits **no** such line.
* **distill order**: ``wrapper.eval()`` + ``no_grad`` forward →
  ``kd_loss.prepare(...)`` → ``opt_params = wrapper.parameters() +
  kd_loss.kd_parameters()`` → optimizer construction.  Reordering breaks
  OFD/FitNets adapter registration.
* **scheduler None guard**: ``if sch is not None: sch.step()`` at epoch end.
* **proxy_mse**: ``.to(device)`` each batch before forward;
  ``max_batches=3``; dataloader with fewer batches → use what we saw,
  StopIteration is the normal iterator termination, never raised past the
  for-loop.
* **live-push degrade**: ``orca.chart`` is lazy-imported inside
  :func:`_make_live_push`; on any failure training continues and stdout
  protocol keys are still emitted.
* **resume**: ``latest.pt`` atomic tmp+replace, sort_keys sha16 hashes
  of ``build_cfg`` + ``kd_config``; mode/hash mismatch → fail loud (handled
  in :mod:`kd._resume`).
* **early stop**: patience epochs without metric improvement → break;
  tracked via :meth:`_on_epoch_end`.
* Persisted ``latest.pt`` / ``best.pt`` carry only the resume-schema
  keys (no absolute paths).  Verified by a unit test.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import torch
import torch.nn as nn
import torch.nn.functional as F

from . import _leaves, _resume


# ---------------------------------------------------------------------------
# TrainConfig
# ---------------------------------------------------------------------------
@dataclass
class TrainConfig:
    """All knobs the engine needs.  Built by the entry script (argparse / yaml)."""

    mode: Literal["teacher", "distill", "eval"]
    artifacts_dir: Path                       # per-run user/ parent (leaves loader scans <artifacts_dir>/user)

    # model construction
    build_fn: str = "build_model"
    build_cfg: dict = field(default_factory=dict)
    model_path: Path | None = None            # teacher mode (teacher wrapper .py)
    student_model_path: Path | None = None    # distill / eval mode

    # distill-only
    teacher_cache: Path | None = None
    kd_config: dict = field(
        default_factory=lambda: {"kd_losses": ["mse"], "weights": {"mse": 1.0}}
    )

    # eval-only
    student_ckpt: Path | None = None
    metric_baseline: float | None = None
    metric_kind: str | None = None

    # optimization
    epochs: int = 3
    lr: float = 1e-3
    batch_size: int = 4
    device: str = "auto"                      # "auto" | "cpu" | "cuda" | ...
    seed: int = 0

    # identity / outputs
    variant_id: str = "model"                 # = experiment id; drives runs/<exp>/ + ckpt variant_id
    out_ckpt: Path | None = None              # best.pt (or latest) is copied here at the end

    # resume + early-stop
    resume_ckpt: Path | None = None
    eval_every: int = 1                       # 0 ⇒ never mid-train eval (disables early-stop)
    early_stop_patience: int = 0              # 0 ⇒ disabled

    # environment (best-effort)
    project_root: str | None = None
    env_anchor: str = ""


# ---------------------------------------------------------------------------
# KDTrainer
# ---------------------------------------------------------------------------
Mode = Literal["teacher", "distill", "eval"]


class KDTrainer:
    """Fixed training engine.  Construct with a :class:`TrainConfig`, call :meth:`train`."""

    def __init__(self, cfg: TrainConfig) -> None:
        self.cfg = cfg
        self._device_obj: torch.device | None = None
        # runs_dir = <artifacts_dir>/runs/<variant_id>/  (latest.pt + best.pt live here)
        self.runs_dir = Path(cfg.artifacts_dir) / "runs" / (cfg.variant_id or "model")
        self.latest_path = self.runs_dir / "latest.pt"
        self.best_path = self.runs_dir / "best.pt"

    # ----------------------------------------------------------------- public
    def train(self) -> int:
        cfg = self.cfg
        _maybe_bootstrap_env(cfg.env_anchor)
        if cfg.project_root and cfg.project_root not in sys.path:
            sys.path.insert(0, cfg.project_root)
        torch.manual_seed(int(cfg.seed))

        if cfg.mode == "eval":
            return self._run_eval()
        if cfg.mode == "teacher":
            return self._run_teacher()
        return self._run_distill()

    # --------------------------------------------------------- teacher mode
    def _run_teacher(self) -> int:
        cfg = self.cfg
        if cfg.model_path is None:
            raise SystemExit("[teacher mode] --model_path is required")
        leaves = _leaves.load(Path(cfg.artifacts_dir) / "user")
        device = self._device()
        teacher = _load_model_by_path(cfg.model_path, cfg.build_fn, cfg.build_cfg).to(device)
        teacher.train()

        opt = leaves.build_optimizer(teacher.parameters(), cfg.lr) or torch.optim.Adam(
            teacher.parameters(), lr=cfg.lr
        )
        sch = leaves.build_scheduler(opt, cfg.epochs)
        dl = leaves.build_dataloader(cfg.batch_size)

        build_cfg_hash = _resume.config_hash(cfg.build_cfg)
        kd_cfg_hash = ""  # teacher mode has no kd_config

        start_epoch, best = self._maybe_resume(
            teacher, opt, sch, "teacher", build_cfg_hash, kd_cfg_hash
        )

        live_push = _make_live_push(cfg.variant_id, "teacher")
        loss_curve: list[dict[str, float]] = []
        last_avg = float("nan")
        epoch = start_epoch
        while epoch < cfg.epochs:
            teacher.train()
            epoch_loss, n_batches = self._step_teacher(teacher, opt, leaves, dl, device)
            if n_batches == 0:
                print(
                    f"[train_pipeline:teacher] epoch={epoch}: dataloader empty, stopping",
                    file=sys.stderr,
                )
                break
            if sch is not None:                                   # M3 None guard
                sch.step()
            last_avg = epoch_loss / max(n_batches, 1)
            print(f"[train_pipeline:teacher] epoch={epoch} loss_avg={last_avg:.6f}", flush=True)
            loss_curve.append({"epoch": epoch, "loss": last_avg})
            live_push(loss_curve)

            best = self._on_epoch_end("teacher", teacher, opt, sch, epoch, last_avg, best,
                                       leaves, build_cfg_hash, kd_cfg_hash)
            if self._should_break_early(epoch, best):
                break
            epoch += 1

        if not math.isfinite(last_avg):
            raise SystemExit(
                "[teacher mode] no training batches were yielded (last_loss is NaN); "
                "aborting before ckpt save. Check build_dataloader() — must be re-iterable."
            )

        # write final ckpt (downstream schema) + copy best.pt (or latest) → out_ckpt.
        self._write_teacher_out_ckpt(teacher, last_avg)
        print(f"TEACHER_CKPT: {self._resolved_out_ckpt()}")
        print(f"TASK_LOSS_FINAL: {last_avg:.6f}")
        return 0

    def _step_teacher(
        self,
        teacher: nn.Module,
        opt: torch.optim.Optimizer,
        leaves: _leaves.Leaves,
        dl: Any,
        device: torch.device,
    ) -> tuple[float, int]:
        epoch_loss = 0.0
        n_batches = 0
        for x, y in iter(dl):
            x = x.to(device)
            y = y.to(device)
            out = teacher(x)
            loss = leaves.compute_loss(out, y)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            epoch_loss += float(loss.detach())
            n_batches += 1
        return epoch_loss, n_batches

    # --------------------------------------------------------- distill mode
    def _run_distill(self) -> int:
        cfg = self.cfg
        if cfg.student_model_path is None:
            raise SystemExit("[distill mode] --student_model_path is required")
        if cfg.teacher_cache is None:
            raise SystemExit("[distill mode] --teacher_cache is required")

        # Lazy KD imports so teacher/eval runs don't require kd/ on sys.path.
        from kd.wrapper import KDStudentWrapper, TeacherCache
        from kd.compose import build_kd_loss
        from kd.ema import MeanTeacherEMA

        leaves = _leaves.load(Path(cfg.artifacts_dir) / "user")
        device = self._device()

        student_raw = _load_model_by_path(cfg.student_model_path, cfg.build_fn, cfg.build_cfg)
        hook_fn = getattr(student_raw, "feature_hook_names", None)
        hook_names = list(hook_fn()) if callable(hook_fn) else []
        wrapper = KDStudentWrapper(student_raw, hook_names).to(device)

        teacher = TeacherCache.load(cfg.teacher_cache).to(device)
        kd_loss = build_kd_loss(leaves.compute_loss, cfg.kd_config)

        ema: MeanTeacherEMA | None = None
        if cfg.kd_config.get("ema"):
            ema = MeanTeacherEMA(
                wrapper.student, decay=float(cfg.kd_config.get("ema_decay", 0.999))
            ).to(device)

        dl = leaves.build_dataloader(cfg.batch_size)

        # ----- distill hot-order: materialise one batch → eval fwd → prepare → opt.
        x0 = _first_batch_x(dl, device)
        wrapper.eval()
        with torch.no_grad():
            _, t_feats0 = teacher(x0)
            _, s_feats0 = wrapper(x0)
        wrapper.train()
        kd_loss.prepare(s_feats0, t_feats0)            # OFDAdapter lazy-built here

        opt_params = list(wrapper.parameters()) + list(kd_loss.kd_parameters())
        opt = leaves.build_optimizer(opt_params, cfg.lr) or torch.optim.Adam(
            opt_params, lr=cfg.lr
        )
        sch = leaves.build_scheduler(opt, cfg.epochs)

        build_cfg_hash = _resume.config_hash(cfg.build_cfg)
        kd_cfg_hash = _resume.config_hash(cfg.kd_config)

        start_epoch, best = self._maybe_resume(
            wrapper.student, opt, sch, "distill", build_cfg_hash, kd_cfg_hash
        )

        live_push = _make_live_push(cfg.variant_id, "distill")
        loss_curve: list[dict[str, float]] = []
        last_avg = float("nan")
        epoch = start_epoch
        while epoch < cfg.epochs:
            wrapper.train()
            epoch_loss, n_batches = self._step_distill(
                wrapper, teacher, kd_loss, opt, ema, leaves, dl, device, epoch
            )
            if n_batches == 0:
                print(
                    f"[train_pipeline:distill] epoch={epoch}: dataloader empty, stopping",
                    file=sys.stderr,
                )
                break
            if sch is not None:                                          # M3 None guard
                sch.step()
            last_avg = epoch_loss / max(n_batches, 1)
            print(
                f"[train_pipeline:distill] epoch={epoch} kd_loss_avg={last_avg:.6f}",
                flush=True,
            )
            loss_curve.append({"epoch": epoch, "loss": last_avg})
            live_push(loss_curve)

            best = self._on_epoch_end(
                "distill", wrapper.student, opt, sch, epoch, last_avg, best,
                leaves, build_cfg_hash, kd_cfg_hash,
            )
            if self._should_break_early(epoch, best):
                break
            epoch += 1

        if not math.isfinite(last_avg):
            raise SystemExit(
                "[distill mode] no training batches were yielded (last_loss is NaN); "
                "aborting before ckpt save. Check build_dataloader() — must be re-iterable."
            )

        proxy_mse = _compute_proxy_mse(wrapper, teacher, dl, device)
        self._write_distill_out_ckpt(wrapper.student, proxy_mse)
        print(f"STUDENT_CKPT: {self._resolved_out_ckpt()}")
        print(f"KD_LOSS_FINAL: {last_avg:.6f}")
        print(f"KD_PROXY_MSE: {proxy_mse:.6f}")
        return 0

    def _step_distill(
        self,
        wrapper: nn.Module,
        teacher: nn.Module,
        kd_loss: Callable[..., Any],
        opt: torch.optim.Optimizer,
        ema: Any,
        leaves: _leaves.Leaves,
        dl: Any,
        device: torch.device,
        epoch: int,
    ) -> tuple[float, int]:
        epoch_loss = 0.0
        n_batches = 0
        for x, y in iter(dl):
            x = x.to(device)
            y = y.to(device)
            s_out, s_feats = wrapper(x)
            with torch.no_grad():
                t_out, t_feats = teacher(x)
            ema_out = ema(x) if ema is not None else None
            loss = kd_loss(s_out, y, s_feats, t_out, t_feats, ema_out, epoch)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            if ema is not None:
                ema.update(wrapper.student)
            epoch_loss += float(loss.detach())
            n_batches += 1
        return epoch_loss, n_batches

    # ------------------------------------------------------------- eval mode
    def _run_eval(self) -> int:
        cfg = self.cfg
        if cfg.student_model_path is None:
            raise SystemExit("[eval mode] --student_model_path is required")
        if cfg.student_ckpt is None:
            raise SystemExit("[eval mode] --student_ckpt is required")

        leaves = _leaves.load(Path(cfg.artifacts_dir) / "user")
        device = self._device()
        student = _load_model_by_path(cfg.student_model_path, cfg.build_fn, cfg.build_cfg).to(device)
        ck = torch.load(cfg.student_ckpt, map_location=device)
        sd = _extract_state_dict(ck)
        missing, unexpected = student.load_state_dict(sd, strict=False)
        if missing:
            print(
                f"[train_pipeline:eval] WARN missing keys (top5): {list(missing)[:5]}",
                file=sys.stderr,
            )
        if unexpected:
            # Unexpected keys are a strong signal of a wrong/ckpt-model
            # mismatch (silent eval on misaligned weights would produce a
            # garbage metric — Rule 12).  Fail loud instead of WARN.
            raise SystemExit(
                f"[eval mode] ckpt has unexpected keys (top5): "
                f"{list(unexpected)[:5]} — wrong ckpt for this model? Aborting."
            )
        student.eval()

        with torch.no_grad():
            value, kind = leaves.eval_metric(student, device)
        if not math.isfinite(float(value)):
            raise SystemExit(
                f"[eval mode] eval_metric returned non-finite value={value}; aborting "
                "(a fake metric would mask a broken eval pipeline — CLAUDE.md Rule 12)."
            )

        # Direction + met judgment via kd_common.accuracy_direction (lazy import so
        # eval-only runs stay kd_common-free when _kd_scripts is off sys.path).
        confidence = "high"
        met = False
        baseline = cfg.metric_baseline
        kind_override = cfg.metric_kind
        if baseline is not None and kind_override:
            direction = _kd_accuracy_direction(kind_override)
            if direction == "max":
                met = bool(float(value) >= float(baseline))
            elif direction == "min":
                met = bool(float(value) <= float(baseline))
            else:
                confidence = "low"
                print(
                    f"[train_pipeline:eval] WARN: accuracy_baseline_kind="
                    f"{kind_override!r} unknown direction; met_accuracy=false, confidence=low.",
                    file=sys.stderr,
                )
        else:
            confidence = "low"
            print(
                "[train_pipeline:eval] WARN: --accuracy_baseline / "
                "--accuracy_baseline_kind not given; met_accuracy=false (low).",
                file=sys.stderr,
            )

        # eval mode does NOT emit the [train_pipeline:<mode>] loss line.
        print(f"STUDENT_ACCURACY: {value}")
        print(f"STUDENT_ACCURACY_KIND: {kind}")
        print(f"MET_ACCURACY: {str(met).lower()}")
        print(f"ACCURACY_CONFIDENCE: {confidence}")
        return 0

    # ----------------------------------------------- mid-train eval / resume
    def _on_epoch_end(
        self,
        mode: Mode,
        model: nn.Module,
        opt: torch.optim.Optimizer,
        sch: torch.optim.lr_scheduler.LRScheduler | None,
        epoch: int,
        last_loss: float,
        best: dict | None,
        leaves: _leaves.Leaves,
        build_cfg_hash: str,
        kd_cfg_hash: str,
    ) -> dict | None:
        """Save latest.pt; optionally evaluate + update best.pt; return new best."""
        cfg = self.cfg
        # mid-train eval (single source of truth = leaf kind).
        metric_value: float | None = None
        metric_kind: str | None = None
        if cfg.eval_every > 0 and (epoch + 1) % cfg.eval_every == 0:
            try:
                with torch.no_grad():
                    metric_value, metric_kind = leaves.eval_metric(model, self._device())
            except Exception as e:  # eval failure is non-fatal to training
                print(
                    f"[train_pipeline:{mode}] WARN: mid-train eval failed at epoch "
                    f"{epoch}: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )

        # latest.pt — always saved (resume contract). R1: schema keys only.
        _resume.save_latest(
            self.latest_path,
            state_dict=model.state_dict(),
            optimizer_state=opt.state_dict(),
            scheduler_state=sch.state_dict() if sch is not None else None,
            epoch=epoch,
            best_metric=best,
            mode=mode,
            build_cfg_hash=build_cfg_hash,
            kd_config_hash=kd_cfg_hash,
        )

        if metric_value is None or metric_kind is None or best is None:
            improved = best is None  # first eval always wins
        else:
            improved = _metric_improved(metric_value, metric_kind, best)

        if metric_value is not None and metric_kind is not None and improved:
            best = {
                "epoch": int(epoch),
                "value": float(metric_value),
                "kind": str(metric_kind),
            }
            # best.pt = latest.pt + the new best metadata (atomic, schema-clean).
            _resume.save_latest(
                self.best_path,
                state_dict=model.state_dict(),
                optimizer_state=opt.state_dict(),
                scheduler_state=sch.state_dict() if sch is not None else None,
                epoch=epoch,
                best_metric=best,
                mode=mode,
                build_cfg_hash=build_cfg_hash,
                kd_config_hash=kd_cfg_hash,
            )
        return best

    def _should_break_early(self, epoch: int, best: dict | None) -> bool:
        cfg = self.cfg
        if cfg.early_stop_patience <= 0 or best is None:
            return False
        last_improvement = int(best.get("epoch", epoch))
        return (epoch - last_improvement) >= cfg.early_stop_patience

    def _maybe_resume(
        self,
        model: nn.Module,
        opt: torch.optim.Optimizer,
        sch: torch.optim.lr_scheduler.LRScheduler | None,
        mode: Mode,
        build_cfg_hash: str,
        kd_cfg_hash: str,
    ) -> tuple[int, dict | None]:
        """Try to resume from cfg.resume_ckpt; restore model/optim/scheduler state.

        Returns ``(start_epoch, best)``.  ``start_epoch = 0`` when no resume.
        """
        cfg = self.cfg
        rs = _resume.maybe_load(
            cfg.resume_ckpt,
            expected_mode=mode,
            expected_build_cfg_hash=build_cfg_hash,
            expected_kd_config_hash=kd_cfg_hash,
        )
        if rs is None:
            return 0, None

        # Restore model + optimizer.  Optimizer state restore is best-effort:
        # param identity has to match; if it doesn't, torch raises — surface loud.
        try:
            model.load_state_dict(rs.state_dict, strict=True)
        except Exception as e:
            raise _resume.ResumeMismatchError(
                f"resume: cannot restore model state_dict from {cfg.resume_ckpt}: "
                f"{type(e).__name__}: {e}"
            ) from e
        if rs.optimizer_state is not None:
            try:
                opt.load_state_dict(rs.optimizer_state)
            except Exception as e:
                print(
                    f"[kd.trainer] WARN: optimizer state restore failed "
                    f"({type(e).__name__}: {e}); continuing with fresh optimizer.",
                    file=sys.stderr,
                )
        # B4 — scheduler state drop: persisted scheduler but current run has none.
        if rs.scheduler_state is not None and sch is None:
            _resume.warn_scheduler_drop(
                cfg.resume_ckpt,
                "leaves.build_scheduler returned None this run",
            )
        elif rs.scheduler_state is not None and sch is not None:
            try:
                sch.load_state_dict(rs.scheduler_state)
            except Exception as e:
                print(
                    f"[kd.trainer] WARN: scheduler state restore failed "
                    f"({type(e).__name__}: {e}); continuing with fresh scheduler.",
                    file=sys.stderr,
                )

        print(
            f"[kd.trainer] resume: restored from {cfg.resume_ckpt} "
            f"(epoch={rs.epoch}, start_epoch={rs.start_epoch})",
            file=sys.stderr,
        )
        return rs.start_epoch, rs.best

    # -------------------------------------------------- final ckpt emission
    def _write_teacher_out_ckpt(self, teacher: nn.Module, final_loss: float) -> None:
        out = self._resolved_out_ckpt()
        # Prefer the best snapshot (if mid-train eval produced one) — same schema,
        # just different weights.  When no best, save the current state directly.
        if self.best_path.is_file():
            blob = torch.load(self.best_path, map_location="cpu")
            state_dict = blob["state_dict"]
        else:
            state_dict = teacher.state_dict()
        torch.save(
            {
                "state_dict": state_dict,
                "build_cfg": self.cfg.build_cfg,
                "variant_id": self.cfg.variant_id,
                "epochs": self.cfg.epochs,
                "final_loss": float(final_loss),
                "mode": "teacher",
            },
            out,
        )

    def _write_distill_out_ckpt(self, student: nn.Module, proxy_mse: float) -> None:
        out = self._resolved_out_ckpt()
        if self.best_path.is_file():
            blob = torch.load(self.best_path, map_location="cpu")
            state_dict = blob["state_dict"]
        else:
            state_dict = student.state_dict()
        torch.save(
            {
                "student_state_dict": state_dict,
                "variant_id": self.cfg.variant_id,
                "student_cfg": self.cfg.build_cfg,
                "kd_config": self.cfg.kd_config,
                "epochs": self.cfg.epochs,
                "proxy_mse": float(proxy_mse),
                "mode": "distill",
            },
            out,
        )

    def _resolved_out_ckpt(self) -> Path:
        if self.cfg.out_ckpt is None:
            raise SystemExit(
                "[kd.trainer] out_ckpt is None — caller must provide --out_ckpt"
            )
        out = Path(self.cfg.out_ckpt)
        out.parent.mkdir(parents=True, exist_ok=True)
        return out

    # ----------------------------------------------------------- utilities
    def _device(self) -> torch.device:
        if self._device_obj is None:
            name = self.cfg.device
            if name == "auto":
                self._device_obj = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            else:
                self._device_obj = torch.device(name)
        return self._device_obj


# ===========================================================================
# Module-level helpers (not part of the public class surface so they can be
# unit-tested in isolation).
# ===========================================================================
def _load_model_by_path(
    model_path: Path | str, build_fn: str, cfg: dict
) -> nn.Module:
    """Import a model ``.py`` by absolute path and call ``build_fn(**cfg)``.

    Inserts the model file's directory into ``sys.path`` so shared-block
    imports (e.g. ``from _model8_blocks import ...``) resolve for KD-NAS
    variants living in ``knowledge_base/families/receiver/``.
    """
    model_path = os.path.abspath(model_path)
    if not os.path.isfile(model_path):
        raise FileNotFoundError(f"model_path not found: {model_path}")
    model_dir = os.path.dirname(model_path)
    if model_dir not in sys.path:
        sys.path.insert(0, model_dir)
    module_name = Path(model_path).stem
    spec = importlib.util.spec_from_file_location(module_name, model_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot construct import spec for {model_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    if not hasattr(mod, build_fn):
        raise AttributeError(
            f"{model_path} has no build fn {build_fn!r}; available: "
            f"{[n for n in dir(mod) if not n.startswith('_')]}"
        )
    return getattr(mod, build_fn)(**cfg)


def _maybe_bootstrap_env(env_anchor: str) -> None:
    """Best-effort ORCA env bootstrap from per-run artifacts anchor."""
    if not env_anchor:
        return
    try:
        from orca.chart._env import load_run_env_from_artifacts  # type: ignore

        load_run_env_from_artifacts(env_anchor)
    except Exception as e:
        print(
            f"[kd.trainer] WARN: env bootstrap failed (live chart may not push): "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )


def _make_live_push(variant_id: str, mode: str) -> Callable[[list], None]:
    """Per-epoch live chart push (degrade-safe).

    Lazy-imports ``orca.chart.render_chart``; on any failure the push degrades
    to a no-op or stderr WARN.  **Never** raises — training must continue.
    """
    try:
        from orca.chart import render_chart  # type: ignore
    except Exception:
        render_chart = None  # type: ignore
    label = f"kd-{mode}-{variant_id}"
    title = f"{mode} loss — {variant_id}"

    def push(curve: list) -> None:
        if render_chart is None:
            return
        try:
            render_chart(
                chart_type="line",
                data=curve,
                label=label,
                title=title,
                x="epoch",
                y="loss",
                x_label="epoch",
                y_label="loss（越低越好）",
                caption=f"{mode} training loss per-epoch (variant={variant_id})",
            )
        except Exception as e:  # best-effort sidecar: never abort training
            print(
                f"[train_pipeline:{mode}] WARN: render_chart failed (ignored): "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )

    return push


def _first_batch_x(dl: Any, device: torch.device) -> torch.Tensor:
    """Materialise one batch from the dataloader; return ``x`` on ``device``.

    Used by distill's hot-order (materialise → eval forward → prepare).
    Fails loud on an empty dataloader — symmetric with ``_compute_proxy_mse``
    (Rule 12: a bare ``StopIteration`` here would surface as a stacktrace
    rather than an actionable error).
    """
    try:
        x0, _ = next(iter(dl))
    except StopIteration as e:
        raise SystemExit(
            "[distill] dataloader yielded no batch — cannot run prepare "
            "forward. Check build_dataloader() is re-iterable and yields at "
            "least one batch."
        ) from e
    return x0.to(device)


@torch.no_grad()
def _compute_proxy_mse(
    wrapper: nn.Module,
    teacher: nn.Module,
    dataloader: Any,
    device: torch.device,
    max_batches: int = 3,
) -> float:
    """Soft MSE between student and teacher outputs — short-training proxy.

    Behaviour:

    * ``max_batches=3`` bounds cost.
    * Each batch is ``.to(device)`` **before** the forward pass (otherwise
      device mismatch).
    * Dataloader yielding fewer than ``max_batches`` batches is graceful: we
      average over what we saw.  StopIteration is the for-loop's normal
      termination, never raised past it.
    * Fails loud on an empty dataloader rather than silently returning 0.0.
    """
    wrapper.eval()
    total = 0.0
    seen = 0
    for x, _ in dataloader:
        x = x.to(device)
        s_out, _ = wrapper(x)
        t_out, _ = teacher(x)
        total += float(F.mse_loss(s_out, t_out).detach())
        seen += 1
        if seen >= max_batches:
            break
    if seen == 0:
        raise SystemExit(
            "_compute_proxy_mse: dataloader yielded no batch — cannot compute "
            "proxy MSE. Check that build_dataloader() is re-iterable."
        )
    return total / seen


def _extract_state_dict(ck: Any) -> dict:
    """Accept distill / teacher / bare state_dict ckpt payloads (eval mode)."""
    if isinstance(ck, dict) and isinstance(ck.get("student_state_dict"), dict):
        return ck["student_state_dict"]
    if isinstance(ck, dict) and isinstance(ck.get("state_dict"), dict):
        return ck["state_dict"]
    if isinstance(ck, dict):
        return ck
    return ck


_KD_ACCURACY_DIRECTION_WARNED: bool = False


def _kd_accuracy_direction(kind: str) -> str:
    """Lazy-import kd_common.accuracy_direction so eval runs stay decoupled.

    Falls back to ``""`` (unknown direction) when ``kd_common`` is not on
    sys.path — but emits a single stderr WARN so the operator can see that
    early-stop / direction-aware comparison has been disabled (F3).
    """
    global _KD_ACCURACY_DIRECTION_WARNED
    try:
        from kd_common import accuracy_direction  # type: ignore

        return accuracy_direction(kind)
    except ImportError as e:
        if not _KD_ACCURACY_DIRECTION_WARNED:
            print(
                f"[kd.trainer] WARN: kd_common unreachable ({type(e).__name__}: {e}); "
                f"accuracy_direction falls back to '' — early stopping will not "
                f"fire and metric direction is treated as unknown.",
                file=sys.stderr,
            )
            _KD_ACCURACY_DIRECTION_WARNED = True
        return ""


def _metric_improved(value: float, kind: str, best: dict) -> bool:
    """Compare a new metric against the current best using its stored direction.

    Strict comparison (``>`` for max, ``<`` for min) — equal values do NOT
    count as improvement, otherwise best.epoch would ratchet every epoch on a
    flat metric and early-stop would never fire.

    Falls back to kd_common.accuracy_direction; unknown kind → treat as not
    improved (fail-safe — never falsely claim a new best).
    """
    direction = _kd_accuracy_direction(kind)
    prev = float(best.get("value", value))
    if direction == "max":
        return float(value) > prev
    if direction == "min":
        return float(value) < prev
    return False


__all__ = ["TrainConfig", "KDTrainer"]

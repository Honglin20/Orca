"""train_pipeline.py — KD-NAS unified training script (teacher + distill + eval modes).

**Skeleton — NOT a runnable intermediate.** This file is the generation
starting point of the ``kd-train-script`` agent, not a gold example.  The five
``user_*`` slots below (``user_compute_loss`` / ``user_build_dataloader`` /
``user_eval_metric`` / ``build_user_optimizer`` / ``build_user_scheduler``)
raise ``NotImplementedError`` until the agent ports the user's own code into
them verbatim.  Before that specialisation, **any** mode run fails loud with
``NotImplementedError`` — there is no placeholder fallback, no dummy loss, no
dummy loader, no dummy eval metric.

Once specialised the generated script is self-contained: the user's loss /
dataloader / optimizer / scheduler / eval-metric logic is copied in inline
(never imported from the user's project at runtime), teacher/student models
are loaded **by path** via :mod:`importlib.util`, and training runs on a
single device (no distributed data-parallel launch, no torchrun, no
architecture sampling — KD-NAS uses a ``ThreadPoolExecutor`` round-robin
pool at the workflow level, not inside the training script).

Three modes
-----------
* ``--mode teacher``   — train a teacher with pure ``user_compute_loss``, save
  a checkpoint consumable by ``teacher_setup`` (state_dict + build_cfg).
* ``--mode distill``   — train a student with ``user_compute_loss`` + KD loss
  (assembled via :func:`kd.compose.build_kd_loss`; the composite calls
  ``user_compute_loss`` internally), save the student ckpt.
* ``--mode eval``      — load a student ckpt, run ``user_eval_metric`` (ported
  by the agent from the user repo's eval script, e.g. ``test_student.py``),
  and emit the accuracy protocol consumed by ``train_pool``.  Read-only: no
  checkpoint is written; this mode replaces the old
  ``measure_student --eval_command`` path.

Generation contract (verifier cross-checks this)
------------------------------------------------
* Stdout keys (teacher mode): ``TEACHER_CKPT: <path>`` + ``TASK_LOSS_FINAL: <float>``
* Stdout keys (distill mode): ``STUDENT_CKPT: <path>`` + ``KD_LOSS_FINAL: <float>``
  + ``KD_PROXY_MSE: <float>``
* Stdout keys (eval mode): ``STUDENT_ACCURACY: <float>`` +
  ``STUDENT_ACCURACY_KIND: <nmse|mse|ber|snr|acc>`` + ``MET_ACCURACY: <bool>``
  + ``ACCURACY_CONFIDENCE: high|low`` (no ckpt written — read-only evaluation).
* Teacher ckpt schema: ``{state_dict, build_cfg, variant_id, epochs, final_loss, mode}``
* Student ckpt schema: ``{student_state_dict, variant_id, student_cfg, kd_config,
  epochs, proxy_mse, mode}``
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sys
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F

# ---------------------------------------------------------------------------
# sys.path bootstrap for ``kd/`` imports + optional user project root.
#
# The generated script may live in ``<output_dir>`` (not ``_kd_scripts/``),
# so prefer ``ORCA_KD_SCRIPTS_DIR`` (injected by the kd-train workflow) and
# fall back to this file's own directory so the template runs standalone.
# ---------------------------------------------------------------------------
_KD_SCRIPTS_DIR = Path(
    os.environ.get("ORCA_KD_SCRIPTS_DIR", Path(__file__).resolve().parent)
)
if str(_KD_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_KD_SCRIPTS_DIR))

_PROJECT_ROOT = os.environ.get("ORCA_PROJECT_ROOT", "")
if _PROJECT_ROOT and _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ===========================================================================
# FIXED USER INTERFACE SLOTS — kd-train-script agent must port the user's own
# code into these five functions verbatim (function body + its module-level
# dependency closure).  Unfilled slots raise NotImplementedError -> any mode
# run fails loud (this is the gate, not a silent placeholder fallback).
# ===========================================================================
def user_compute_loss(s_out: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """kd-train-script: port the user's train.py loss (compute_loss or the
    semantically equivalent ``(output, target) -> scalar`` function) verbatim:
    same ops, same reduction, same shape assumptions."""
    raise NotImplementedError(
        "kd-train-script must port the user's compute_loss function body "
        "into user_compute_loss before running"
    )


def user_build_dataloader(batch_size: int = 4):
    """kd-train-script: port the user's build_dataloader (or the equivalent
    data-loading logic found in the training loop) verbatim, including its
    module-level dependency closure.  Must be re-iterable: every epoch's
    ``iter(dl)`` yields a fresh batch stream (wrap one-shot generators in a
    re-iterable adapter)."""
    raise NotImplementedError(
        "kd-train-script must port the user's build_dataloader function body "
        "into user_build_dataloader before running"
    )


def user_eval_metric(student: nn.Module, device) -> tuple[float, str]:
    """kd-train-script: port the metric computation + eval data loading from
    the user's eval script verbatim (self-contained).  Returns
    ``(value, kind)`` with ``kind`` in {nmse, mse, ber, snr, acc}."""
    raise NotImplementedError(
        "kd-train-script must port the user's eval metric from the user repo's "
        "eval script into user_eval_metric before running"
    )


def build_user_optimizer(params, lr) -> torch.optim.Optimizer | None:
    """kd-train-script: port the user's optimizer constructor verbatim (same
    class, same hyperparameters) when train.py defines one; return None when it
    does not (the training loop then uses the annotated Adam fallback)."""
    return None


def build_user_scheduler(optimizer, epochs):
    """kd-train-script: port the user's scheduler constructor verbatim (step
    cadence must match the user's — per-epoch vs per-batch) when train.py
    defines one; return None when it does not (do not invent one)."""
    return None


# ---------------------------------------------------------------------------
# Model load by path (KD-NAS teacher/student contract: build_model + DUMMY_INPUT).
# ---------------------------------------------------------------------------
def _load_model_by_path(model_path: str, build_fn: str, cfg: dict) -> nn.Module:
    """Import a model ``.py`` by absolute path and call ``build_fn(**cfg)``.

    Inserts the model file's directory into ``sys.path`` so shared-block
    imports (e.g. ``from _model8_blocks import ...``) resolve for KD-NAS
    variants living in ``knowledge_base/families/receiver/``.  Re-registers
    the module in ``sys.modules`` so downstream ``importlib`` hits cache.
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


def _resolve_device(name: str) -> torch.device:
    if name == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return torch.device(name)


def _make_live_push(variant_id: str, mode: str):
    """Per-epoch live chart push (best-effort, never kills the training loop).

    Mirrors ``train_adapter_template._make_live_push``: lazy import
    ``orca.chart.render_chart``; on any failure (import error, push error)
    degrade to a no-op or stderr warning.  Same ``label``+``title`` re-push
    = refresh semantics (dedup).
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
                f"[train_pipeline:{mode}] WARN: render_chart failed "
                f"(ignored): {type(e).__name__}: {e}",
                file=sys.stderr,
            )

    return push


def _maybe_bootstrap_env(env_anchor: str) -> None:
    """best-effort ORCA env bootstrap from per-run artifacts anchor.

    Prevents the agent-spawned bash losing ``ORCA_CHART_SOCK`` (which would
    make live chart pushes silently no-op).  Failure is non-fatal — only a
    stderr warning is emitted.
    """
    if not env_anchor:
        return
    try:
        from orca.chart._env import load_run_env_from_artifacts  # type: ignore

        load_run_env_from_artifacts(env_anchor)
    except Exception as e:
        print(
            f"[train_pipeline] WARN: env bootstrap failed "
            f"(live chart may not push): {type(e).__name__}: {e}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="KD-NAS unified training pipeline (teacher + distill + eval modes)"
    )
    # --- mode + shared runtime -------------------------------------------
    p.add_argument(
        "--mode",
        required=True,
        choices=["teacher", "distill", "eval"],
        help="mode: teacher (task_loss only) / distill (task_loss + KD) / eval (read-only metric)",
    )
    p.add_argument("--out_ckpt", required=True, help="output checkpoint path")
    p.add_argument("--epochs", type=int, default=3, help="training epochs")
    p.add_argument("--lr", type=float, default=1e-3, help="learning rate")
    p.add_argument("--batch_size", type=int, default=4, help="batch size")
    p.add_argument(
        "--device",
        default="auto",
        help="cuda / cpu / auto (default auto: cuda if available else cpu)",
    )
    p.add_argument("--seed", type=int, default=0, help="reproducibility seed")
    p.add_argument(
        "--variant_id",
        default="model",
        help="variant id used in chart label/title and ckpt metadata",
    )

    # --- model loading (shared; teacher uses --model_path, distill uses --student_model_path)
    p.add_argument(
        "--build_fn", default="build_model", help="build function name in the model module"
    )
    p.add_argument(
        "--build_cfg",
        default="{}",
        help="JSON dict passed to build_model(**cfg) (teacher build_cfg / student_cfg)",
    )

    # --- teacher mode ----------------------------------------------------
    p.add_argument(
        "--model_path",
        default=None,
        help="[teacher mode] teacher model .py path (importlib-loaded by path)",
    )

    # --- distill mode ----------------------------------------------------
    p.add_argument(
        "--student_model_path",
        default=None,
        help="[distill mode] student model .py path (KD-NAS variant in KB)",
    )
    p.add_argument(
        "--teacher_cache",
        default=None,
        help="[distill mode] teacher_cache.pt path (produced by teacher_setup.py)",
    )
    p.add_argument(
        "--kd_config",
        default='{"kd_losses": [], "weights": {}}',
        help="[distill mode] JSON kd_config for kd.compose.build_kd_loss",
    )

    # --- eval mode -------------------------------------------------------
    p.add_argument(
        "--student_ckpt",
        default=None,
        help="[eval mode] student checkpoint to load (student_state_dict consumed)",
    )
    p.add_argument(
        "--accuracy_baseline",
        default=None,
        help="[eval mode] absolute accuracy baseline (user-provided)",
    )
    p.add_argument(
        "--accuracy_baseline_kind",
        default=None,
        help="[eval mode] nmse/mse/ber/db (lower better) | snr/acc (higher better)",
    )

    p.add_argument(
        "--project_root",
        default=None,
        help="user project root (data file / path resolution); added to sys.path",
    )

    # --- env -------------------------------------------------------------
    p.add_argument(
        "--env_anchor",
        default="",
        help="per-run $ORCA_ARTIFACTS_DIR anchor for env bootstrap",
    )
    return p.parse_args()


# ===========================================================================
# Teacher mode — pure task_loss training, save ckpt for teacher_setup.
# ===========================================================================
def run_teacher_mode(args: argparse.Namespace) -> int:
    if not args.model_path:
        raise SystemExit("[teacher mode] --model_path is required")

    cfg = json.loads(args.build_cfg)
    torch.manual_seed(args.seed)
    device = _resolve_device(args.device)

    teacher = _load_model_by_path(args.model_path, args.build_fn, cfg).to(device).train()

    # kd-train-script: build_user_optimizer returns the user's ported optimizer
    # verbatim when train.py defines one; the Adam fallback below is used ONLY
    # when the user's train.py has no optimizer (build_user_optimizer is None).
    optimizer = build_user_optimizer(teacher.parameters(), args.lr)
    if optimizer is None:
        optimizer = torch.optim.Adam(teacher.parameters(), lr=args.lr)
    scheduler = build_user_scheduler(optimizer, args.epochs)

    dl = user_build_dataloader(batch_size=args.batch_size)

    live_push = _make_live_push(args.variant_id, "teacher")
    loss_curve: list = []
    last_avg = float("nan")
    for epoch in range(args.epochs):
        teacher.train()
        epoch_loss = 0.0
        n_batches = 0
        for x, y in iter(dl):
            x = x.to(device)
            y = y.to(device)
            out = teacher(x)
            loss = user_compute_loss(out, y)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()
            epoch_loss += float(loss.detach())
            n_batches += 1
        if scheduler is not None:
            scheduler.step()
        if n_batches == 0:
            print(
                f"[train_pipeline:teacher] epoch={epoch}: dataloader empty, stopping",
                file=sys.stderr,
            )
            break
        last_avg = epoch_loss / max(n_batches, 1)
        print(f"[train_pipeline:teacher] epoch={epoch} loss_avg={last_avg:.6f}", flush=True)
        loss_curve.append({"epoch": epoch, "loss": last_avg})
        live_push(loss_curve)

    if not math.isfinite(last_avg):
        # Dataloader produced zero batches across all epochs (e.g. user
        # build_dataloader returned an empty loader / one-shot generator that
        # exhausted in a prior step). Saving a NaN ckpt would silently
        # propagate NaN through teacher_setup → distill → proxy_mse with
        # returncode=0 — fail loud instead (CLAUDE.md Rule 12).
        raise SystemExit(
            "[teacher mode] no training batches were yielded (last_loss is NaN); "
            "aborting before ckpt save. Check user_build_dataloader() — must be "
            "re-iterable and yield at least one batch per epoch."
        )

    out_path = Path(args.out_ckpt)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": teacher.state_dict(),
            "build_cfg": cfg,
            "variant_id": args.variant_id,
            "epochs": args.epochs,
            "final_loss": last_avg,
            "mode": "teacher",
        },
        out_path,
    )
    print(f"TEACHER_CKPT: {out_path}")
    print(f"TASK_LOSS_FINAL: {last_avg:.6f}")
    return 0


# ===========================================================================
# Distill mode — task_loss + KD composite, save student ckpt.
# ===========================================================================
def run_distill_mode(args: argparse.Namespace) -> int:
    if not args.student_model_path:
        raise SystemExit("[distill mode] --student_model_path is required")
    if not args.teacher_cache:
        raise SystemExit("[distill mode] --teacher_cache is required")

    # Lazy import so teacher-mode runs don't require ``kd/`` on sys.path.
    from kd.wrapper import KDStudentWrapper, TeacherCache
    from kd.compose import build_kd_loss
    from kd.ema import MeanTeacherEMA

    student_cfg = json.loads(args.build_cfg)
    kd_config = json.loads(args.kd_config)

    torch.manual_seed(args.seed)
    device = _resolve_device(args.device)

    # --- student --------------------------------------------------------
    student = _load_model_by_path(args.student_model_path, args.build_fn, student_cfg)
    hook_fn = getattr(student, "feature_hook_names", None)
    hook_names = list(hook_fn()) if callable(hook_fn) else []
    wrapper = KDStudentWrapper(student, hook_names).to(device)

    # --- teacher cache (resident in memory; frozen) --------------------
    teacher = TeacherCache.load(args.teacher_cache).to(device)

    # --- KD composite loss (owns OFD/FitNets adapters) -----------------
    # The composite calls user_compute_loss(s_out, y) internally (kd.compose
    # contract unchanged) and adds the selected KD terms.
    kd_loss = build_kd_loss(user_compute_loss, kd_config)

    # --- EMA (mean teacher) --------------------------------------------
    ema = None
    if kd_config.get("ema"):
        ema = MeanTeacherEMA(
            student, decay=float(kd_config.get("ema_decay", 0.999))
        ).to(device)

    # --- dataloader -----------------------------------------------------
    dl = user_build_dataloader(batch_size=args.batch_size)

    # Materialise one batch so KD adapters (OFD/FitNets) can pre-build their
    # parameters *before* the optimizer is constructed (so they get registered).
    # Only x0 is needed (features drive adapter shapes); y0 is unused here.
    dl_iter = iter(dl)
    x0, _ = next(dl_iter)
    x0 = x0.to(device)

    wrapper.eval()
    with torch.no_grad():
        _, t_feats0 = teacher(x0)
        _, s_feats0 = wrapper(x0)
    wrapper.train()
    kd_loss.prepare(s_feats0, t_feats0)

    # --- optimizer ------------------------------------------------------
    # kd-train-script: build_user_optimizer must include BOTH the student
    # parameters and the KD adapter parameters (kd_loss.kd_parameters()) so
    # OFD/FitNets adapters train.  The Adam fallback below is used ONLY when
    # the user's train.py has no optimizer.
    opt_params = list(wrapper.parameters()) + list(kd_loss.kd_parameters())
    optimizer = build_user_optimizer(opt_params, args.lr)
    if optimizer is None:
        optimizer = torch.optim.Adam(opt_params, lr=args.lr)
    scheduler = build_user_scheduler(optimizer, args.epochs)

    # --- training loop --------------------------------------------------
    live_push = _make_live_push(args.variant_id, "distill")
    loss_curve: list = []
    last_avg = float("nan")
    for epoch in range(args.epochs):
        wrapper.train()
        epoch_loss = 0.0
        n_batches = 0
        for x, y in iter(dl):
            x = x.to(device)
            y = y.to(device)

            s_out, s_feats = wrapper(x)
            with torch.no_grad():
                t_out, t_feats = teacher(x)
            ema_out = ema(x) if ema is not None else None

            # kd_loss internally calls user_compute_loss(s_out, y) + KD terms.
            loss = kd_loss(s_out, y, s_feats, t_out, t_feats, ema_out, epoch)

            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()

            if ema is not None:
                ema.update(wrapper.student)

            epoch_loss += float(loss.detach())
            n_batches += 1

        if scheduler is not None:
            scheduler.step()
        if n_batches == 0:
            print(
                f"[train_pipeline:distill] epoch={epoch}: dataloader empty, stopping",
                file=sys.stderr,
            )
            break
        last_avg = epoch_loss / max(n_batches, 1)
        print(
            f"[train_pipeline:distill] epoch={epoch} kd_loss_avg={last_avg:.6f}",
            flush=True,
        )
        loss_curve.append({"epoch": epoch, "loss": last_avg})
        live_push(loss_curve)

    if not math.isfinite(last_avg):
        # See teacher mode for rationale: a NaN final loss means no batch was
        # processed — fail loud rather than silently emit a NaN ckpt.
        raise SystemExit(
            "[distill mode] no training batches were yielded (last_loss is NaN); "
            "aborting before ckpt save. Check user_build_dataloader() — must be "
            "re-iterable and yield at least one batch per epoch."
        )

    proxy_mse = _compute_proxy_mse(wrapper, teacher, dl, device)

    out_path = Path(args.out_ckpt)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "student_state_dict": wrapper.student.state_dict(),
            "variant_id": args.variant_id,
            "student_cfg": student_cfg,
            "kd_config": kd_config,
            "epochs": args.epochs,
            "proxy_mse": proxy_mse,
            "mode": "distill",
        },
        out_path,
    )
    print(f"STUDENT_CKPT: {out_path}")
    print(f"KD_LOSS_FINAL: {last_avg:.6f}")
    print(f"KD_PROXY_MSE: {proxy_mse:.6f}")
    return 0


# ===========================================================================
# Eval mode — load student ckpt, run user eval metric, emit accuracy protocol.
# Read-only: no checkpoint written. Replaces measure_student's accuracy path.
# ===========================================================================
def run_eval_mode(args: argparse.Namespace) -> int:
    if not args.student_model_path:
        raise SystemExit("[eval mode] --student_model_path is required")
    if not args.student_ckpt:
        raise SystemExit("[eval mode] --student_ckpt is required")

    cfg = json.loads(args.build_cfg)
    device = _resolve_device(args.device)

    student = _load_model_by_path(args.student_model_path, args.build_fn, cfg).to(device)
    ck = torch.load(args.student_ckpt, map_location=device)
    if isinstance(ck, dict) and isinstance(ck.get("student_state_dict"), dict):
        sd = ck["student_state_dict"]            # distill ckpt key
    elif isinstance(ck, dict) and isinstance(ck.get("state_dict"), dict):
        sd = ck["state_dict"]                    # teacher / generic ckpt key
    elif isinstance(ck, dict):
        sd = ck                                  # bare state_dict dict (layer -> tensor)
    else:
        sd = ck
    missing, unexpected = student.load_state_dict(sd, strict=False)
    if missing:
        print(
            f"[train_pipeline:eval] WARN missing keys (top5): {list(missing)[:5]}",
            file=sys.stderr,
        )
    if unexpected:
        print(
            f"[train_pipeline:eval] WARN unexpected keys (top5): {list(unexpected)[:5]}",
            file=sys.stderr,
        )
    student.eval()

    # user_eval_metric (ported by the agent from the user repo's eval script):
    # self-contained data loading + metric computation -> (value, kind). A
    # non-finite result must surface loudly (mirrors _compute_proxy_mse
    # fail-loud), never a silent 0.0.
    with torch.no_grad():
        value, kind = user_eval_metric(student, device)
    if not math.isfinite(float(value)):
        raise SystemExit(
            f"[eval mode] user eval returned non-finite value={value}; aborting "
            "(a fake metric would mask a broken eval pipeline — CLAUDE.md Rule 12)."
        )

    # Direction + met judgment via kd_common.accuracy_direction (lazy import so
    # teacher/eval-only runs stay kd-free when _kd_scripts is off sys.path;
    # mirrors distill mode's lazy kd.wrapper import).
    confidence = "high"
    met = False
    if args.accuracy_baseline is not None and args.accuracy_baseline_kind:
        try:
            from kd_common import accuracy_direction  # type: ignore
            direction = accuracy_direction(args.accuracy_baseline_kind)
        except ImportError:
            direction = ""
        baseline = float(args.accuracy_baseline)
        if direction == "max":
            met = bool(value >= baseline)
        elif direction == "min":
            met = bool(value <= baseline)
        else:
            confidence = "low"
            print(
                f"[train_pipeline:eval] WARN: accuracy_baseline_kind="
                f"{args.accuracy_baseline_kind!r} unknown direction; "
                f"met_accuracy=false, confidence=low.",
                file=sys.stderr,
            )
    else:
        confidence = "low"
        print(
            "[train_pipeline:eval] WARN: --accuracy_baseline / "
            "--accuracy_baseline_kind not given; met_accuracy=false (low).",
            file=sys.stderr,
        )

    print(f"STUDENT_ACCURACY: {value}")
    print(f"STUDENT_ACCURACY_KIND: {kind}")
    print(f"MET_ACCURACY: {str(met).lower()}")
    print(f"ACCURACY_CONFIDENCE: {confidence}")
    return 0


@torch.no_grad()
def _compute_proxy_mse(wrapper, teacher, dataloader, device, max_batches: int = 3) -> float:
    """Soft MSE between student and teacher outputs — short-training proxy.

    Mirrors ``train_adapter_template._compute_proxy_mse``: capped at
    ``max_batches`` to bound cost; averages MSE over seen batches. Fails loud
    on an empty dataloader (returns no batch to average) rather than silently
    returning 0.0 — proxy_mse is a downstream-consumed signal and a fake 0.0
    would mask a broken pipeline (CLAUDE.md Rule 12).
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
            "proxy MSE. Check that user_build_dataloader() is re-iterable."
        )
    return total / seen


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    args = parse_args()

    _maybe_bootstrap_env(args.env_anchor)

    if args.project_root and args.project_root not in sys.path:
        sys.path.insert(0, args.project_root)

    # All user logic is ported into the five fixed slots above; the slot
    # functions are self-contained in this module (no runtime loading of the
    # user's project).  An unfilled slot raises NotImplementedError — fail loud.
    if args.mode == "eval":
        return run_eval_mode(args)
    if args.mode == "teacher":
        return run_teacher_mode(args)
    return run_distill_mode(args)


if __name__ == "__main__":
    sys.exit(main())

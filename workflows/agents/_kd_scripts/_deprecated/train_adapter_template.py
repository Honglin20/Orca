"""train_kd.py adapter template — kd-train-script agent fills the placeholders.

Aligned with CONTRACTS.md §5 (``train_kd.py`` adapter CLI).

This file is the **template** the kd-train-script agent starts from.  It is
also runnable as-is so the kd-train-script agent / test harness can sanity
check the KD pipeline end-to-end against a placeholder user loss + dummy
data.  When generating a real ``train_kd.py`` the agent:

1. Reads the user's ``train.py``.
2. Replaces the ``{{USER_TRAIN_MODULE}}`` / ``{{USER_LOSS_FN}}`` placeholders
   (and optionally the loader/optimizer builders) with the user's actual
   import paths.
3. Adapts the per-batch loop under ``# TODO(kd-train-script)`` to preserve
   the user's optimizer / scheduler / dataloader / grad-accumulation logic,
   swapping only the per-batch loss for ``kd_loss(...)``.

Fixed CLI (CONTRACTS §5)::

    python3 train_kd.py \\
      --student_family <family> --student_cfg '<json>' \\
      --kd_config '<json>' --teacher_cache <teacher_cache.pt> \\
      --student_model_path <path> --build_fn <fn> \\
      --epochs <N> --out_ckpt <path> \\
      [--user_train_import '<module.path>' --user_loss_fn '<name>']
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Callable

import torch
import torch.nn as nn
import torch.nn.functional as F

# ---------------------------------------------------------------------------
# Make _kd_scripts/ importable so ``import kd`` and ``from students import
# <family>`` work regardless of CWD. 本模板生成物 train_kd.py 可能落盘到
# output_dir（非 _kd_scripts/），故优先读 ORCA_KD_SCRIPTS_DIR 环境变量（kd_train_script_gen
# / kd_trainer 注入），回退到本文件所在目录。
# ---------------------------------------------------------------------------
_KD_SCRIPTS_DIR = Path(os.environ.get("ORCA_KD_SCRIPTS_DIR", Path(__file__).resolve().parent))
if str(_KD_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_KD_SCRIPTS_DIR))
_STUDENTS_DIR = _KD_SCRIPTS_DIR / "students"
if str(_STUDENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_STUDENTS_DIR))

# 用户项目根（train.py 所在目录）——用户 train 模块常 `from model import build_model`，
# 需 project_root 在 sys.path。优先 env ORCA_PROJECT_ROOT（kd_trainer 注入），可被 --project_root 覆盖。
_PROJECT_ROOT = os.environ.get("ORCA_PROJECT_ROOT", "")
if _PROJECT_ROOT and _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from kd.wrapper import KDStudentWrapper, TeacherCache        # noqa: E402
from kd.compose import build_kd_loss                          # noqa: E402
from kd.ema import MeanTeacherEMA                             # noqa: E402

# ===========================================================================
# USER PLACEHOLDERS — kd-train-script agent replaces these when generating
# a project-specific train_kd.py.  Leaving them as ``{{...}}`` strings keeps
# the template self-runnable via the placeholder fallback below.
# ===========================================================================
USER_TRAIN_MODULE = "{{USER_TRAIN_MODULE}}"   # e.g. "train" or "/abs/path/train.py"
USER_LOSS_FN = "{{USER_LOSS_FN}}"             # e.g. "compute_loss"


# ---------------------------------------------------------------------------
# Placeholder fallbacks — used when USER_TRAIN_MODULE is still ``{{...}}`` so
# the template can be smoke-tested without the user's code present.
# ---------------------------------------------------------------------------
def _placeholder_user_loss(s_out: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """Default user loss — MSE to target y (same shape as student output)."""
    return F.mse_loss(s_out, y)


def _placeholder_user_dataloader(
    batch_size: int = 4,
    shape=(1, 4, 48, 64, 1),
    n_batches: int = 8,
):
    """Dummy infinite-ish generator yielding (x, y) batches for smoke tests."""
    inner_shape = tuple(shape[1:])
    for _ in range(n_batches):
        x = torch.randn(batch_size, *inner_shape)
        y = torch.randn(batch_size, *inner_shape)
        yield x, y


def _load_user_loss() -> tuple[Callable, Callable]:
    """Resolve user loss + dataloader builders.

    Returns ``(loss_fn, build_dataloader)``.  When the placeholders are
    unexpanded, returns the placeholder fallbacks so the template runs.
    """
    if USER_TRAIN_MODULE.startswith("{{"):
        return _placeholder_user_loss, _placeholder_user_dataloader

    if os.path.isfile(USER_TRAIN_MODULE):
        import importlib.util
        spec = importlib.util.spec_from_file_location("_user_train", USER_TRAIN_MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(USER_TRAIN_MODULE)

    if not hasattr(module, USER_LOSS_FN):
        raise AttributeError(
            f"user train module {USER_TRAIN_MODULE!r} has no loss fn {USER_LOSS_FN!r}"
        )
    loss_fn = getattr(module, USER_LOSS_FN)
    build_dl = getattr(module, "build_dataloader", _placeholder_user_dataloader)
    return loss_fn, build_dl


# ---------------------------------------------------------------------------
# Student build — load variant .py **by path**（KD-NAS 变体在 KB / worktree，不再 import
# students/<family>）。变体可 ``from _model8_blocks import ...``，故加其目录到 sys.path。
# ---------------------------------------------------------------------------
def _build_student(student_model_path: str, build_fn: str, cfg: dict) -> nn.Module:
    student_model_path = os.path.abspath(student_model_path)
    if not os.path.isfile(student_model_path):
        raise FileNotFoundError(f"student_model_path 不存在: {student_model_path}")
    model_dir = os.path.dirname(student_model_path)
    if model_dir not in sys.path:
        sys.path.insert(0, model_dir)
    module_name = Path(student_model_path).stem
    spec = importlib.util.spec_from_file_location(module_name, student_model_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {student_model_path} 构造 import spec")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    if not hasattr(mod, build_fn):
        raise AttributeError(
            f"{student_model_path} 无 build fn {build_fn!r}；可用: "
            f"{[n for n in dir(mod) if not n.startswith('_')]}"
        )
    return getattr(mod, build_fn)(**cfg)


def _make_live_push(variant_id: str):
    """每-epoch 把累积 loss 曲线推 web（U-4：每变体一张图，label kd-distill-<variant_id>）。

    镜像 ``run_qat.py:127-176``：lazy import ``orca.chart``，不可用→no-op；每次 try/except
    stderr-only，**永不让推图失败杀掉训练循环**。同 label+title 再推 = 刷新（dedup 语义）。
    """
    try:
        from orca.chart import render_chart  # type: ignore
    except Exception:
        render_chart = None  # type: ignore
    label = f"kd-distill-{variant_id}"
    title = f"KD loss — {variant_id}"

    def push(curve: list[dict]) -> None:
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
                y_label="KD loss（越低越好）",
                caption=f"蒸馏训练每-epoch loss（实时刷新；variant={variant_id}）",
            )
        except Exception as e:  # 推图异常不阻断训练
            print(f"[train_kd] WARN: render_chart 失败（不阻断训练）：{type(e).__name__}: {e}",
                  file=sys.stderr)

    return push


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="KD adapter training script")
    p.add_argument("--student_cfg", required=True,
                   help="JSON build_cfg dict passed to build_model(**cfg)")
    p.add_argument("--kd_config", required=True,
                   help="JSON kd_config dict (kd_losses / weights / ema / scheduler)")
    p.add_argument("--teacher_cache", required=True,
                   help="path to teacher_cache.pt produced by teacher_setup.py")
    p.add_argument("--student_model_path", required=True,
                   help="变体 model .py 绝对路径（按路径 import 加载）")
    p.add_argument("--build_fn", default="build_model",
                   help="build function name in the student module")
    p.add_argument("--variant_id", default="student",
                   help="变体 id（实时图 label/title 参数化，U-4）")
    p.add_argument("--epochs", type=int, default=3,
                   help="number of short-training epochs (distillation is short)")
    p.add_argument("--out_ckpt", required=True,
                   help="path to write the distilled student checkpoint")
    p.add_argument("--user_train_import", default=None,
                   help="override USER_TRAIN_MODULE placeholder")
    p.add_argument("--user_loss_fn", default=None,
                   help="override USER_LOSS_FN placeholder")
    p.add_argument("--batch_size", type=int, default=4,
                   help="batch size for the placeholder fallback dataloader")
    p.add_argument("--lr", type=float, default=1e-3,
                   help="learning rate for the placeholder fallback optimizer")
    p.add_argument("--device", default=None,
                   help="cuda / cpu (auto-detected if omitted)")
    p.add_argument("--project_root", default=None,
                   help="用户项目根（含 train.py/model.py）；注入 sys.path 使 from model import ... 生效")
    p.add_argument("--env_anchor", default="",
                   help="BLK-5：自举 ORCA env 的锚点路径（per-run $ORCA_ARTIFACTS_DIR，orca_env.sh 祖先）")
    p.add_argument("--seed", type=int, default=0,
                   help="HI-2：复现种子（训练起始 + 权重 init）")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    args = parse_args()

    # BLK-5：自举 ORCA env（防 agent 拆 bash 丢 ORCA_CHART_SOCK → render_chart 推不上）。
    # env_anchor 指向 per-run $ORCA_ARTIFACTS_DIR（orca_env.sh 的祖先）。
    if args.env_anchor:
        try:
            from orca.chart._env import load_run_env_from_artifacts  # type: ignore
            load_run_env_from_artifacts(args.env_anchor)
        except Exception as e:
            print(f"[train_kd] WARN: env 自举失败（实时图可能推不上）：{type(e).__name__}: {e}",
                  file=sys.stderr)
    # HI-2：复现种子（训练起始 + build_model 权重 init）。
    torch.manual_seed(args.seed)

    student_cfg = json.loads(args.student_cfg)
    kd_config = json.loads(args.kd_config)

    # 注入 project_root 到 sys.path（用户 train 模块 `from model import ...` 需要）
    if args.project_root and args.project_root not in sys.path:
        sys.path.insert(0, args.project_root)

    # Override placeholders if the CLI provided user module / loss fn.
    global USER_TRAIN_MODULE, USER_LOSS_FN
    if args.user_train_import:
        USER_TRAIN_MODULE = args.user_train_import
    if args.user_loss_fn:
        USER_LOSS_FN = args.user_loss_fn

    device = torch.device(args.device or ("cuda" if torch.cuda.is_available() else "cpu"))

    # --- student ------------------------------------------------------------
    student = _build_student(args.student_model_path, args.build_fn, student_cfg)
    hook_fn = getattr(student, "feature_hook_names", None)
    hook_names = list(hook_fn()) if callable(hook_fn) else []
    wrapper = KDStudentWrapper(student, hook_names).to(device)

    # --- teacher cache (resident in memory) --------------------------------
    teacher = TeacherCache.load(args.teacher_cache).to(device)

    # --- user task loss + dataloader ---------------------------------------
    user_loss, build_dataloader = _load_user_loss()

    # --- KD composite -------------------------------------------------------
    kd_loss = build_kd_loss(user_loss, kd_config)

    # --- EMA (mean teacher) -------------------------------------------------
    ema = None
    if kd_config.get("ema"):
        ema = MeanTeacherEMA(student, decay=float(kd_config.get("ema_decay", 0.999)))
        ema = ema.to(device)

    # --- dataloader ---------------------------------------------------------
    if callable(build_dataloader):
        try:
            dl = build_dataloader()
        except TypeError:
            # Fallback signature: build_dataloader(batch_size=...)
            dl = build_dataloader(batch_size=args.batch_size)
    else:
        # Already a loader object.
        dl = build_dataloader

    # Materialise one batch so we can pre-build KD adapters (their parameters
    # must exist *before* we construct the optimizer).
    dl_iter = iter(dl)
    x0, y0 = next(dl_iter)
    x0 = x0.to(device)
    y0 = y0.to(device)

    wrapper.eval()
    with torch.no_grad():
        _, t_feats0 = teacher(x0)
        _, s_feats0 = wrapper(x0)
    wrapper.train()
    kd_loss.prepare(s_feats0, t_feats0)

    # --- optimizer ----------------------------------------------------------
    # NOTE(kd-train-script): when adapting the user's train.py, replace this
    # with the user's optimizer builder (e.g. ``build_optimizer(student.parameters())``)
    # and extend its param group with ``kd_loss.kd_parameters()``.
    optimizer = torch.optim.Adam(
        list(wrapper.parameters()) + list(kd_loss.kd_parameters()),
        lr=args.lr,
    )

    # =======================================================================
    # Training loop
    #
    # TODO(kd-train-script): adapt the user's train.py loop here.  Preserve
    # the user's optimizer / scheduler / dataloader / grad accumulation /
    # logging.  The ONLY mandatory change is the per-batch loss:
    #
    #     loss = kd_loss(s_out, y, s_feats, t_out, t_feats, ema_out, epoch)
    #
    # The default loop below is a self-contained fallback that runs the
    # template end-to-end on the placeholder dataloader so this file is
    # smoke-testable before the agent specialises it.
    # =======================================================================
    last_avg = float("nan")
    live_push = _make_live_push(args.variant_id)
    loss_curve: list[dict] = []
    for epoch in range(args.epochs):
        wrapper.train()
        epoch_loss = 0.0
        n_batches = 0
        # BLK-4：不复用 placeholder dataloader（其硬编码 shape 会污染真实训练）。
        # 每 epoch 重新迭代用户 dataloader（可重复迭代的 DataLoader）；空则停（不续假数据）。
        for batch_idx, (x, y) in enumerate(iter(dl)):
            x = x.to(device)
            y = y.to(device)

            s_out, s_feats = wrapper(x)
            with torch.no_grad():
                t_out, t_feats = teacher(x)
            ema_out = ema(x) if ema is not None else None

            loss = kd_loss(s_out, y, s_feats, t_out, t_feats, ema_out, epoch)

            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()

            if ema is not None:
                ema.update(wrapper.student)

            epoch_loss += float(loss.detach())
            n_batches += 1

        if n_batches == 0:
            print(f"[train_kd] epoch={epoch}: dataloader 空（用户提供可重复迭代的 loader），停止",
                  file=sys.stderr)
            break
        last_avg = epoch_loss / max(n_batches, 1)
        print(f"[train_kd] epoch={epoch} kd_loss_avg={last_avg:.6f}", flush=True)
        loss_curve.append({"epoch": epoch, "loss": last_avg})
        live_push(loss_curve)

    # --- proxy MSE (soft-MSE vs teacher on a few final batches) ------------
    proxy_mse = _compute_proxy_mse(wrapper, teacher, dl, device)

    # --- checkpoint ---------------------------------------------------------
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
        },
        out_path,
    )

    # --- stdout keys for downstream agent nodes ----------------------------
    # CONTRACTS §5: STUDENT_CKPT / KD_LOSS_FINAL / KD_PROXY_MSE.
    print(f"STUDENT_CKPT: {out_path}")
    print(f"KD_LOSS_FINAL: {last_avg:.6f}")
    print(f"KD_PROXY_MSE: {proxy_mse:.6f}")
    return 0


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _chain_iterators(first, fallback):
    """Yield from ``first``; once exhausted, continue with ``fallback``.

    Keeps the loop running across epochs when the user's dataloader is a
    finite one-shot generator (placeholder fallback).
    """
    try:
        for item in first:
            yield item
    except StopIteration:
        pass
    for item in fallback:
        yield item


@torch.no_grad()
def _compute_proxy_mse(
    wrapper: KDStudentWrapper,
    teacher: TeacherCache,
    dataloader,
    device: torch.device,
    max_batches: int = 3,
) -> float:
    """Soft MSE between student and teacher outputs — short-training proxy."""
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
    return total / max(seen, 1)


if __name__ == "__main__":
    sys.exit(main())

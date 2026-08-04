"""train_pipeline.py — KD-NAS fixed engine entry.

This file is the **engine entry**: it builds a :class:`kd.trainer.TrainConfig`
from CLI flags (+ optional ``--config`` YAML) and calls
:meth:`kd.trainer.KDTrainer.train`. It is fixed library code — the LLM-driven
``kd-train-script`` codegen produces four leaves (``loss/data/eval/optim.py``)
under ``<artifacts_dir>/user/`` and this engine loads them via
:mod:`kd._leaves`.

Phase 2 (atomic switch, plan §5): ``gen_train_script``'s output schema and the
five downstream call sites (train-teacher / distill / finalize) all point at
this fixed entry. No per-project ``train_pipeline.py`` is generated anymore.

Priority (plan §3.3): ``CLI --flag`` > ``--config run_config.yaml`` > engine default.

Stdout/log contract — owned by the engine (``kd.trainer``), not this entry:

* ``[train_pipeline:teacher] epoch=N loss_avg=F``  (per teacher epoch)
* ``[train_pipeline:distill] epoch=N kd_loss_avg=F``  (per distill epoch)
* eval mode emits no loss line
* ``TEACHER_CKPT`` / ``STUDENT_CKPT`` / ``KD_LOSS_FINAL`` / ``KD_PROXY_MSE`` /
  ``STUDENT_ACCURACY`` / ``STUDENT_ACCURACY_KIND`` / ``MET_ACCURACY`` /
  ``ACCURACY_CONFIDENCE`` protocol keys

The engine prints to stdout only; **the caller redirects** stdout to
``runs/<exp>/train.log`` (M1).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# sys.path bootstrap: prefer ``ORCA_KD_SCRIPTS_DIR`` (injected by the workflow);
# fall back to this file's parent so the engine runs standalone.
_KD_SCRIPTS_DIR = Path(
    os.environ.get("ORCA_KD_SCRIPTS_DIR", Path(__file__).resolve().parent)
)
if str(_KD_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_KD_SCRIPTS_DIR))

from kd.trainer import KDTrainer, TrainConfig  # noqa: E402


def _load_yaml_config(path: str | os.PathLike) -> dict:
    """Load ``--config`` YAML to a dict (pyyaml).  Empty / None → {}."""
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"--config {path}: file not found")
    import yaml

    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"--config {path}: top-level must be a mapping, got {type(data)}")
    return data


def _coerce_path(v: str | None, base: Path | None = None) -> Path | None:
    if v is None or v == "":
        return None
    p = Path(v)
    if base is not None and not p.is_absolute():
        p = base / p
    return p


def _json_or_none(v: str | None) -> dict | None:
    if v is None:
        return None
    try:
        out = json.loads(v)
    except json.JSONDecodeError as e:
        raise SystemExit(f"--build_cfg/--kd_config JSON parse failed: {e}") from e
    if not isinstance(out, dict):
        raise SystemExit(f"expected JSON object, got {type(out)}")
    return out


def _resolve_cfg(args: argparse.Namespace) -> TrainConfig:
    """Merge ``--config`` yaml + CLI flags (CLI wins) into a TrainConfig."""
    yml = _load_yaml_config(args.config) if args.config else {}

    def pick(cli_val, yaml_key, default):
        if cli_val is not None:
            return cli_val
        return yml.get(yaml_key, default)

    artifacts_dir = Path(args.artifacts_dir)
    build_cfg = _json_or_none(args.build_cfg) or yml.get("build_cfg") or {}
    kd_config = _json_or_none(args.kd_config) or yml.get("kd_config") or {
        "kd_losses": ["mse"], "weights": {"mse": 1.0}
    }
    metric_baseline = pick(args.accuracy_baseline, "accuracy_baseline", None)
    metric_kind = pick(args.accuracy_baseline_kind, "accuracy_baseline_kind", None)

    return TrainConfig(
        mode=args.mode,
        artifacts_dir=artifacts_dir,
        build_fn=args.build_fn,
        build_cfg=build_cfg,
        model_path=_coerce_path(args.model_path),
        student_model_path=_coerce_path(args.student_model_path),
        teacher_cache=_coerce_path(args.teacher_cache),
        kd_config=kd_config,
        student_ckpt=_coerce_path(args.student_ckpt),
        metric_baseline=float(metric_baseline) if metric_baseline is not None else None,
        metric_kind=metric_kind,
        epochs=int(pick(args.epochs, "epochs", 3)),
        lr=float(pick(args.lr, "lr", 1e-3)),
        batch_size=int(pick(args.batch_size, "batch_size", 4)),
        device=args.device,
        seed=int(pick(args.seed, "seed", 0)),
        variant_id=args.experiment or args.variant_id or args.mode,
        out_ckpt=_coerce_path(args.out_ckpt),
        resume_ckpt=_coerce_path(args.resume),
        eval_every=int(pick(args.eval_every, "eval_every", 1)),
        early_stop_patience=int(pick(args.early_stop_patience, "early_stop_patience", 0)),
        project_root=args.project_root,
        env_anchor=args.env_anchor,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "KD-NAS fixed training engine entry (Phase 1 orphan). "
            "Builds TrainConfig + calls KDTrainer.train()."
        )
    )
    p.add_argument("--mode", required=True, choices=["teacher", "distill", "eval"])
    p.add_argument(
        "--config", default=None,
        help="optional run_config.yaml; CLI flags override yaml values",
    )
    p.add_argument(
        "--artifacts_dir", required=True,
        help="per-run artifacts dir (leaves live under <artifacts_dir>/user/)",
    )
    p.add_argument(
        "--experiment", default=None,
        help="experiment id (= variant_id); drives runs/<exp>/ + ckpt variant_id",
    )
    p.add_argument("--variant_id", default=None, help="alias of --experiment")

    p.add_argument("--out_ckpt", default=None, help="output checkpoint path")
    p.add_argument("--resume", default=None, help="resume from latest.pt path")
    p.add_argument(
        "--early_stop_patience", type=int, default=None,
        help="stop after N epochs without metric improvement (0 = disabled)",
    )
    p.add_argument("--eval_every", type=int, default=None)

    p.add_argument("--build_fn", default="build_model")
    p.add_argument("--build_cfg", default=None, help="JSON dict for build_fn(**cfg)")
    p.add_argument("--kd_config", default=None, help="JSON kd_config (distill only)")

    p.add_argument("--model_path", default=None, help="[teacher] teacher model .py path")
    p.add_argument("--student_model_path", default=None, help="[distill/eval] student .py path")
    p.add_argument("--teacher_cache", default=None, help="[distill] teacher_cache.pt path")
    p.add_argument("--student_ckpt", default=None, help="[eval] student ckpt path")

    p.add_argument("--accuracy_baseline", default=None, help="[eval] absolute baseline")
    p.add_argument(
        "--accuracy_baseline_kind", default=None,
        help="[eval] nmse/mse/ber/db (lower) | snr/acc (higher)",
    )

    p.add_argument("--epochs", type=int, default=None)
    p.add_argument("--lr", type=float, default=None)
    p.add_argument("--batch_size", type=int, default=None)
    p.add_argument("--device", default="auto")
    p.add_argument("--seed", type=int, default=None)

    p.add_argument("--project_root", default=None)
    p.add_argument("--env_anchor", default="")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = _resolve_cfg(args)
    return KDTrainer(cfg).train()


if __name__ == "__main__":
    sys.exit(main())

"""train_variants_parallel.py —— 多变体并行蒸馏训练（独立工具，复用 distill agent 逻辑）。

为什么有它：kd-nas workflow 是**串行** sweep（一轮一个变体）。本脚本把「distill agent 的
单变体流水线」（tune_latency → distill_dispatch → train_kd → measure → record）**并发**到
一批变体上，跑完把结果 append 进**共享 ledger.jsonl**——串行 workflow 下次启动会把这些变体
当 done 跳过（跨工具进度共享）。

每变体流水线（与 kd-distill agent.md 完全一致）：
  1. tune_latency.py → accepted_cfg（或 FAIL_latency）；
  2. distill_dispatch.py → noop|train；
  3. train：FAIL_latency→不训；ACCEPTED→train_adapter_template.py（完整 KD + 每-epoch 实时图）
     + measure_student.py（绝对基线精度）；
  4. 组 ledger 行（含 variant_sha256/latency_provider_id/cfg_hash 等跨 run 身份字段）。

并发：``--concurrency`` 个 worker 线程，每个 worker spawn 子进程（train_kd 是重活）。
**ledger 写串行**（主进程收齐所有行后，取 orca.lock 一次性 append）——并行 worker 不写账。
单变体失败不杀整批（记 FAIL 行）。

CLI（多数与 distill agent 同款）::
    python3 train_variants_parallel.py \\
      --receiver_dir <KB/receiver> --ledger <kd_artifacts_dir/ledger.jsonl> \\
      --target_latency_ms <f> --latency_provider <path::func> \\
      --accuracy_baseline <f> [--accuracy_baseline_kind <k>] --test_command "<cmd>" \\
      --teacher_cache <teacher_cache.pt> --kd_scripts_dir <_kd_scripts> \\
      --artifacts_dir <kd_artifacts_dir> --per_run_artifacts_dir <$ORCA_ARTIFACTS_DIR> \\
      --project_root <user project> [--variants <id1,id2>] [--concurrency 2] \\
      [--epochs 50] [--device auto] [--seed 0] [--user_train_import .. --user_loss_fn ..] \\
      [--force_rerun]

stdout:: 每变体进度行 + 末尾 SUMMARY 表；exit 0。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import subprocess
import sys
import traceback
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from kd_common import (  # noqa: E402
    acquire_run_lock,
    is_variant_done,
    provider_id,
    read_ledger,
    sha256_file,
)
from pick_variant import _list_variants, _load_variant, _validate_variant  # noqa: E402


def _run_subproc(argv: list[str]) -> tuple[int, str, str]:
    """跑子进程，返 (rc, stdout, stderr)。"""
    p = subprocess.run(argv, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _parse(stdout: str, key: str) -> str | None:
    """从 `KEY: value` 行取值。"""
    for line in stdout.splitlines():
        if line.startswith(key + ":"):
            return line.split(":", 1)[1].strip()
    return None


def _process_one(ctx: dict[str, Any], variant_path: str) -> dict[str, Any]:
    """单变体 distill 流水线（与 kd-distill agent.md 一致）。返回 ledger 行。"""
    vid = os.path.splitext(os.path.basename(variant_path))[0]
    vsha = sha256_file(variant_path)
    mod = _load_variant(variant_path)
    dummy_input, knobs = _validate_variant(mod, variant_path)
    build_fn = getattr(mod, "BUILD_FN", "build_model")
    dummy_json = json.dumps(dummy_input)
    knobs_json = json.dumps(knobs)
    cfg_hash_seed = hashlib.sha256(knobs_json.encode()).hexdigest()[:16]  # 占位，下面用真实 cfg

    base = {
        "variant_id": vid, "variant_path": os.path.abspath(variant_path),
        "variant_sha256": vsha, "run_id": os.environ.get("ORCA_RUN_ID", ""),
        "latency_provider_id": ctx["provider_id"],
        "target_latency_ms": float(ctx["target_latency_ms"]),
        "accuracy_baseline": float(ctx["accuracy_baseline"]),
    }

    # done 谓词（除非 force_rerun）——已处理则跳过。
    if not ctx["force_rerun"]:
        rows_for_v = [r for r in ctx["ledger_rows"] if r.get("variant_id") == vid]
        if is_variant_done(rows_for_v, float(ctx["target_latency_ms"]), ctx["provider_id"], vsha):
            return {**base, "status": "SKIPPED_DONE", "accepted_cfg": {}, "cfg_hash": cfg_hash_seed,
                    "latency_ms_median": -1, "latency_ms_std": 0, "accuracy": 0, "accuracy_kind": "",
                    "met_latency": False, "met_accuracy": False, "ckpt": "", "fail_reason": "already done"}

    # 1. tune_latency（最小缩量；HI-2 seed / HI-5 cache / HI-13 median+std 都在 tune 内部）
    rc, out, err = _run_subproc([
        sys.executable, os.path.join(ctx["kd_scripts_dir"], "tune_latency.py"),
        "--variant_path", variant_path, "--build_fn", build_fn,
        "--dummy_input", dummy_json, "--knobs", knobs_json,
        "--target_latency_ms", str(ctx["target_latency_ms"]),
        "--latency_provider", ctx["latency_provider"],
        "--artifacts_dir", ctx["artifacts_dir"],
        "--max_measurements", str(ctx["max_measurements"]),
        "--measure_repeats", str(ctx["measure_repeats"]),
        "--device", ctx["device"], "--seed", str(ctx["seed"]),
    ])
    if rc != 0:
        return {**base, "status": "FAIL_train", "accepted_cfg": {}, "cfg_hash": cfg_hash_seed,
                "latency_ms_median": -1, "latency_ms_std": 0, "accuracy": 0, "accuracy_kind": "",
                "met_latency": False, "met_accuracy": False, "ckpt": "",
                "fail_reason": f"tune_latency rc={rc}: {err[-300:]}"}
    tune_status = _parse(out, "TUNE_STATUS") or "FAIL_latency"
    cfg_str = _parse(out, "ACCEPTED_CFG") or _parse(out, "BEST_EFFORT_CFG") or "{}"
    lat_med = float(_parse(out, "LATENCY_MS_MEDIAN") or -1)
    lat_std = float(_parse(out, "LATENCY_MS_STD") or 0)
    accepted_cfg = json.loads(cfg_str)
    cfg_hash = hashlib.sha256(json.dumps(accepted_cfg, sort_keys=True).encode()).hexdigest()[:16]

    # 2. dispatch（BLK-17 确定性门）
    rc, out, err = _run_subproc([
        sys.executable, os.path.join(ctx["kd_scripts_dir"], "distill_dispatch.py"),
        "--tune_status", tune_status,
    ])
    action = _parse(out, "DISTILL_ACTION") if rc == 0 else None
    if action is None:
        return {**base, "status": "FAIL_train", "accepted_cfg": accepted_cfg, "cfg_hash": cfg_hash,
                "latency_ms_median": lat_med, "latency_ms_std": lat_std, "accuracy": 0, "accuracy_kind": "",
                "met_latency": tune_status == "ACCEPTED", "met_accuracy": False, "ckpt": "",
                "fail_reason": f"distill_dispatch rc={rc}: {err[-300:]}"}

    # noop（FAIL_latency）→ 不训练
    if action == "noop":
        return {**base, "status": "FAIL_latency", "accepted_cfg": accepted_cfg, "cfg_hash": cfg_hash,
                "latency_ms_median": lat_med, "latency_ms_std": lat_std, "accuracy": 0, "accuracy_kind": "",
                "met_latency": False, "met_accuracy": False, "ckpt": "", "fail_reason": "latency over target"}

    # 3. train（完整 KD + 每-epoch 实时图；--env_anchor 自举）
    ckpt = os.path.join(ctx["artifacts_dir"], "ckpts", f"{vid}.pt")
    rc, out, err = _run_subproc([
        sys.executable, os.path.join(ctx["kd_scripts_dir"], "train_adapter_template.py"),
        "--student_cfg", cfg_str,
        "--kd_config", json.dumps({"kd_losses": ["mse", "ofd"], "weights": {"mse": 1.0, "ofd": 0.3}, "ema": True}),
        "--teacher_cache", ctx["teacher_cache"],
        "--student_model_path", variant_path, "--build_fn", build_fn,
        "--variant_id", vid, "--env_anchor", ctx["per_run_artifacts_dir"],
        "--epochs", str(ctx["epochs"]), "--out_ckpt", ckpt,
        "--user_train_import", ctx["user_train_import"], "--user_loss_fn", ctx["user_loss_fn"],
        "--device", ctx["device"], "--seed", str(ctx["seed"]),
    ])
    if rc != 0:
        return {**base, "status": "FAIL_train", "accepted_cfg": accepted_cfg, "cfg_hash": cfg_hash,
                "latency_ms_median": lat_med, "latency_ms_std": lat_std, "accuracy": 0, "accuracy_kind": "",
                "met_latency": True, "met_accuracy": False, "ckpt": "",
                "fail_reason": f"train_kd rc={rc}: {err[-400:]}"}
    # BLK-11：ckpt 完整性
    if not (os.path.isfile(ckpt) and os.path.getsize(ckpt) > 0):
        return {**base, "status": "FAIL_train", "accepted_cfg": accepted_cfg, "cfg_hash": cfg_hash,
                "latency_ms_median": lat_med, "latency_ms_std": lat_std, "accuracy": 0, "accuracy_kind": "",
                "met_latency": True, "met_accuracy": False, "ckpt": "",
                "fail_reason": f"ckpt 缺失/空: {ckpt}"}

    # 4. measure（绝对基线；--skip_latency 复用 tune 的 latency，HI-1）
    rc, out, err = _run_subproc([
        sys.executable, os.path.join(ctx["kd_scripts_dir"], "measure_student.py"),
        "--student_model_path", variant_path, "--student_ckpt", ckpt,
        "--build_fn", build_fn, "--build_cfg", cfg_str,
        "--eval_command", ctx["test_command"],
        "--accuracy_baseline", str(ctx["accuracy_baseline"]),
        "--accuracy_baseline_kind", ctx["accuracy_baseline_kind"],
        "--output_dir", ctx["per_run_artifacts_dir"],
        "--project_root", ctx["project_root"], "--skip_latency",
    ])
    if rc != 0:
        return {**base, "status": "FAIL_accuracy", "accepted_cfg": accepted_cfg, "cfg_hash": cfg_hash,
                "latency_ms_median": lat_med, "latency_ms_std": lat_std, "accuracy": 0, "accuracy_kind": "",
                "met_latency": True, "met_accuracy": False, "ckpt": ckpt,
                "fail_reason": f"measure rc={rc}: {err[-300:]}"}
    acc = float(_parse(out, "STUDENT_ACCURACY") or 0)
    kind = _parse(out, "STUDENT_ACCURACY_KIND") or ""
    met_acc = (_parse(out, "MET_ACCURACY") or "false").lower() == "true"
    status = "SUCCESS" if met_acc else "FAIL_accuracy"
    return {**base, "status": status, "accepted_cfg": accepted_cfg, "cfg_hash": cfg_hash,
            "latency_ms_median": lat_med, "latency_ms_std": lat_std, "accuracy": acc, "accuracy_kind": kind,
            "met_latency": True, "met_accuracy": met_acc, "ckpt": ckpt, "fail_reason": ""}


def _main() -> int:
    p = argparse.ArgumentParser(description="多变体并行蒸馏训练（复用 distill agent 逻辑，写共享 ledger）")
    p.add_argument("--receiver_dir", default="", help="默认 $ORCA_KB_DIR/families/receiver")
    p.add_argument("--variants", default="", help="逗号分隔 variant id；空=全部未 done")
    p.add_argument("--ledger", required=True, help="共享 ledger.jsonl")
    p.add_argument("--target_latency_ms", required=True)
    p.add_argument("--latency_provider", required=True)
    p.add_argument("--accuracy_baseline", required=True)
    p.add_argument("--accuracy_baseline_kind", default="")
    p.add_argument("--test_command", required=True)
    p.add_argument("--teacher_cache", required=True)
    p.add_argument("--kd_scripts_dir", required=True)
    p.add_argument("--artifacts_dir", required=True, help="稳定 kd_artifacts_dir（ckpts/tune_cache/lock）")
    p.add_argument("--per_run_artifacts_dir", required=True, help="$ORCA_ARTIFACTS_DIR（env_anchor）")
    p.add_argument("--project_root", default=os.environ.get("ORCA_PROJECT_ROOT", "."))
    p.add_argument("--concurrency", type=int, default=2, help="并行变体数（受 GPU 显存限制）")
    p.add_argument("--epochs", type=int, default=50)
    p.add_argument("--device", default="auto")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--max_measurements", type=int, default=40)
    p.add_argument("--measure_repeats", type=int, default=3)
    p.add_argument("--user_train_import", default="")
    p.add_argument("--user_loss_fn", default="")
    p.add_argument("--force_rerun", action="store_true")
    args = p.parse_args()

    receiver_dir = args.receiver_dir or os.path.join(
        os.environ.get("ORCA_KB_DIR", ""), "families", "receiver")

    # 单写者锁（与 workflow 互斥；并行 worker 不写账，仅主进程末尾写）。
    acquire_run_lock(args.artifacts_dir, os.environ.get("ORCA_RUN_ID", ""))
    ledger_rows = read_ledger(args.ledger)
    all_variants = _list_variants(receiver_dir)

    want = set(v for v in args.variants.split(",") if v.strip()) if args.variants.strip() else None
    targets = [v for v in all_variants
               if (want is None or os.path.splitext(os.path.basename(v))[0] in want)]

    ctx = {
        "target_latency_ms": args.target_latency_ms, "latency_provider": args.latency_provider,
        "provider_id": provider_id(args.latency_provider),
        "accuracy_baseline": args.accuracy_baseline, "accuracy_baseline_kind": args.accuracy_baseline_kind,
        "test_command": args.test_command, "teacher_cache": args.teacher_cache,
        "kd_scripts_dir": args.kd_scripts_dir, "artifacts_dir": args.artifacts_dir,
        "per_run_artifacts_dir": args.per_run_artifacts_dir, "project_root": args.project_root,
        "epochs": args.epochs, "device": args.device, "seed": args.seed,
        "max_measurements": args.max_measurements, "measure_repeats": args.measure_repeats,
        "user_train_import": args.user_train_import, "user_loss_fn": args.user_loss_fn,
        "force_rerun": args.force_rerun, "ledger_rows": ledger_rows,
    }

    print(f"[parallel] {len(targets)} 变体，并发 {args.concurrency}，写共享 ledger {args.ledger}",
          file=sys.stderr)
    results: list[dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futs = {pool.submit(_process_one, ctx, v): v for v in targets}
        for fut in cf.as_completed(futs):
            v = futs[fut]
            try:
                row = fut.result()
            except Exception as e:  # 单变体崩不杀整批
                row = {"variant_id": os.path.splitext(os.path.basename(v))[0],
                       "status": "FAIL_train", "fail_reason": f"{type(e).__name__}: {e}",
                       "ckpt": "", "latency_ms_median": -1, "accuracy": 0,
                       "met_latency": False, "met_accuracy": False}
                traceback.print_exc(file=sys.stderr)
            results.append(row)
            print(f"[parallel] done {row.get('variant_id')}: {row.get('status')}", file=sys.stderr)

    # 串行 append 共享 ledger（仅非 SKIPPED_DONE 的行落账）。
    to_write = [r for r in results if r.get("status") != "SKIPPED_DONE"]
    if to_write:
        os.makedirs(os.path.dirname(os.path.abspath(args.ledger)) or ".", exist_ok=True)
        with open(args.ledger, "a", encoding="utf-8") as f:
            for r in to_write:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # SUMMARY 表
    print("SUMMARY:")
    print(f"{'variant':16} {'status':14} {'lat_ms':>8} {'acc':>10} {'met':>5}  ckpt/fail")
    for r in sorted(results, key=lambda x: x.get("variant_id", "")):
        print(f"{r.get('variant_id',''):16} {r.get('status',''):14} "
              f"{r.get('latency_ms_median',-1):>8.3f} {r.get('accuracy',0):>10.5g} "
              f"{str(r.get('met_accuracy',False)):>5}  "
              f"{r.get('ckpt','') or r.get('fail_reason','')[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

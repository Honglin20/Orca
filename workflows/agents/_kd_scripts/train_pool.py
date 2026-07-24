"""train_pool.py —— KD-NAS 训练阶段：有界并发池 + 增量账本（吃 gate 的 accepted manifest）。

前身是 ``train_variants_parallel.py``（每 worker 独立全流水线），重构后**只做训练阶段**：
  - latency gate 已在 ``gate_all.py`` 完成（FAIL_latency 已落账、ACCEPTED 进 manifest）
  - 本脚本读 gate manifest + setup 的 ``concurrency / device_plan / per_variant_vram_bytes``
  - Phase 启动 VRAM 再校验（setup→train 之间显存可能被别进程抢）：不够则降级 WARN；连 1 都放不下
    → fail loud 非零
  - ``ThreadPoolExecutor(max_workers=concurrency)``，device_plan round-robin 绑卡
    （传 ``--device cuda:i`` 给 train_adapter）
  - 每 worker：``train_adapter_template.py`` + ``measure_student.py --skip_latency``（复用 gate 的干净
    latency，HI-1）
  - 增量账本：``as_completed`` 主线程（已持 ``orca.lock``）逐行 ``write+flush``；单 worker 失败
    try/except → FAIL_train 行，**不杀整批**
  - 末尾 ``viz_kd.py`` 推 sweep 散点

并发数权威 = setup（gpu_probe 算）；本脚本信任 ``--concurrency``，仅做 Phase B VRAM 再校验防护。

CLI::

    python3 train_pool.py \\
      --manifest <gate_manifest.json> --ledger <ledger.jsonl> \\
      --teacher_cache <teacher_cache.pt> --kd_scripts_dir <_kd_scripts> \\
      --artifacts_dir <kd_artifacts_dir> --per_run_artifacts_dir <$ORCA_ARTIFACTS_DIR> \\
      --project_root <user project> \\
      --test_command "<cmd>" --accuracy_baseline <f> [--accuracy_baseline_kind <k>] \\
      --concurrency <N> --device_plan '<json list>' --per_variant_vram_bytes <B> \\
      [--epochs 50] [--seed 0] [--user_train_import .. --user_loss_fn ..] [--safety 0.8]

stdout::

    VARIANTS_DONE: <int>            # ledger 总行数（含历史 + 本次 + FAIL_*)
    VARIANTS_TOTAL: <int>           # KB 变体总数
    SWEEP_STATUS: SUCCESS|FAIL      # 全批跑完无异常 = SUCCESS
    FAIL_REASON: <空|描述>          # SWEEP_STATUS=FAIL 时填
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
    append_ledger_row,
    parse_key,
    provider_id,
    read_ledger,
    run_subproc,
)


def _base_row_from_entry(entry: dict[str, Any], *, provider_id_str: str) -> dict[str, Any]:
    """从 gate manifest entry 构造 ledger 公共身份字段（target/baseline 由 caller 注入）。"""
    return {
        "variant_id": entry["variant_id"],
        "variant_path": entry["variant_path"],
        "variant_sha256": entry["variant_sha256"],
        "run_id": os.environ.get("ORCA_RUN_ID", ""),
        "latency_provider_id": provider_id_str,
    }


def revalidate_vram(
    device_plan: list[str], per_variant_vram_bytes: int, safety: float = 0.8,
) -> tuple[int, str]:
    """Phase B 启动前 VRAM 再校验（确定性，纯函数便于单测）。

    返回 ``(effective_concurrency, warn_msg)``。``effective=0`` 且 device_plan 含 cuda → caller
    fail loud。无 CUDA device 在 plan / per_variant<=0 / torch 不可用 / CUDA 不可用 → 信任 setup
    （effective = len(device_plan)，warn 描述跳过原因）。
    """
    cuda_devs = sorted({d for d in device_plan if d.startswith("cuda:")})
    if not cuda_devs:
        return len(device_plan), ""
    if per_variant_vram_bytes <= 0:
        return len(device_plan), "per_variant_vram_bytes<=0，跳过 VRAM 再校验"

    try:
        import torch
    except Exception as e:
        return len(device_plan), f"torch import 失败（{type(e).__name__}），跳过 VRAM 再校验"

    if not torch.cuda.is_available():
        return len(device_plan), "CUDA 不可用，跳过 VRAM 再校验"

    total_capacity = 0
    for dev in cuda_devs:
        try:
            idx = int(dev.split(":")[1])
            free, _ = torch.cuda.mem_get_info(idx)
        except Exception as e:
            return len(device_plan), f"mem_get_info 失败（{type(e).__name__}），跳过 VRAM 再校验"
        total_capacity += int((free * safety) // per_variant_vram_bytes)

    return total_capacity, ""


def _train_one(ctx: dict[str, Any], entry: dict[str, Any], device: str) -> dict[str, Any]:
    """单 ACCEPTED 变体的训练流水线（train_adapter + measure --skip_latency）。返回 ledger 行。"""
    vid = entry["variant_id"]
    variant_path = entry["variant_path"]
    build_fn = entry["build_fn"]
    accepted_cfg = entry["accepted_cfg"]
    cfg_str = json.dumps(accepted_cfg, sort_keys=True)
    cfg_hash = hashlib.sha256(cfg_str.encode()).hexdigest()[:16]
    lat_med = float(entry["latency_ms_median"])
    lat_std = float(entry["latency_ms_std"])

    base = _base_row_from_entry(entry, provider_id_str=ctx["provider_id"])
    base["target_latency_ms"] = float(ctx["target_latency_ms"])
    base["accuracy_baseline"] = float(ctx["accuracy_baseline"])

    common = {
        "accepted_cfg": accepted_cfg, "cfg_hash": cfg_hash,
        "latency_ms_median": lat_med, "latency_ms_std": lat_std,
    }

    # 1. train（完整 KD + 每-epoch 实时图；--device 绑卡；--env_anchor 自举 ORCA env）
    ckpt = os.path.join(ctx["artifacts_dir"], "ckpts", f"{vid}.pt")
    rc, out, err = run_subproc([
        sys.executable, os.path.join(ctx["kd_scripts_dir"], "train_adapter_template.py"),
        "--student_cfg", cfg_str,
        "--kd_config", json.dumps({"kd_losses": ["mse", "ofd"], "weights": {"mse": 1.0, "ofd": 0.3},
                                   "ema": True}),
        "--teacher_cache", ctx["teacher_cache"],
        "--student_model_path", variant_path, "--build_fn", build_fn,
        "--variant_id", vid, "--env_anchor", ctx["per_run_artifacts_dir"],
        "--epochs", str(ctx["epochs"]), "--out_ckpt", ckpt,
        "--user_train_import", ctx["user_train_import"], "--user_loss_fn", ctx["user_loss_fn"],
        "--device", device, "--seed", str(ctx["seed"]),
    ])
    if rc != 0:
        return {**base, **common, "status": "FAIL_train",
                "accuracy": 0, "accuracy_kind": "",
                "met_latency": True, "met_accuracy": False, "ckpt": "",
                "fail_reason": f"train_kd rc={rc}: {err[-400:]}"}
    # BLK-11：ckpt 完整性
    if not (os.path.isfile(ckpt) and os.path.getsize(ckpt) > 0):
        return {**base, **common, "status": "FAIL_train",
                "accuracy": 0, "accuracy_kind": "",
                "met_latency": True, "met_accuracy": False, "ckpt": "",
                "fail_reason": f"ckpt 缺失/空: {ckpt}"}

    # 2. measure（绝对基线；--skip_latency 复用 gate latency，HI-1）
    rc2, out2, err2 = run_subproc([
        sys.executable, os.path.join(ctx["kd_scripts_dir"], "measure_student.py"),
        "--student_model_path", variant_path, "--student_ckpt", ckpt,
        "--build_fn", build_fn, "--build_cfg", cfg_str,
        "--eval_command", ctx["test_command"],
        "--accuracy_baseline", str(ctx["accuracy_baseline"]),
        "--accuracy_baseline_kind", ctx["accuracy_baseline_kind"],
        "--output_dir", ctx["per_run_artifacts_dir"],
        "--project_root", ctx["project_root"], "--skip_latency",
        "--device", ctx["measure_device"],
    ])
    if rc2 != 0:
        return {**base, **common, "status": "FAIL_accuracy",
                "accuracy": 0, "accuracy_kind": "",
                "met_latency": True, "met_accuracy": False, "ckpt": ckpt,
                "fail_reason": f"measure rc={rc2}: {err2[-300:]}"}
    acc = float(parse_key(out2, "STUDENT_ACCURACY") or 0)
    kind = parse_key(out2, "STUDENT_ACCURACY_KIND") or ""
    met_acc = (parse_key(out2, "MET_ACCURACY") or "false").lower() == "true"
    status = "SUCCESS" if met_acc else "FAIL_accuracy"
    return {**base, **common, "status": status,
            "accuracy": acc, "accuracy_kind": kind,
            "met_latency": True, "met_accuracy": met_acc, "ckpt": ckpt, "fail_reason": ""}


def _main() -> int:
    p = argparse.ArgumentParser(description="KD-NAS 训练阶段：有界并发池 + 增量账本（吃 gate manifest）")
    p.add_argument("--manifest", required=True, help="gate_all.py 产的 accepted manifest json")
    p.add_argument("--ledger", required=True, help="共享 ledger.jsonl（增量 append）")
    p.add_argument("--teacher_cache", required=True)
    p.add_argument("--kd_scripts_dir", required=True)
    p.add_argument("--artifacts_dir", required=True, help="稳定 kd_artifacts_dir（ckpts/lock）")
    p.add_argument("--per_run_artifacts_dir", required=True, help="$ORCA_ARTIFACTS_DIR（env_anchor）")
    p.add_argument("--project_root", default=os.environ.get("ORCA_PROJECT_ROOT", "."))
    p.add_argument("--test_command", required=True)
    p.add_argument("--accuracy_baseline", required=True)
    p.add_argument("--accuracy_baseline_kind", default="")
    p.add_argument("--latency_provider", required=True,
                   help="落账 latency_provider_id 字段（与 gate/setup 同串，跨 run 身份）")
    p.add_argument("--target_latency_ms", required=True, help="落账 target 字段")
    p.add_argument("--concurrency", type=int, required=True, help="setup gpu_probe 算的并发数（权威）")
    p.add_argument("--device_plan", required=True,
                   help="JSON list，setup gpu_probe 算的 round-robin 设备列表")
    p.add_argument("--per_variant_vram_bytes", type=int, required=True,
                   help="setup gpu_probe 测的 per-variant 占用（VRAM 再校验用）")
    p.add_argument("--epochs", type=int, default=50)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--safety", type=float, default=0.8, help="VRAM 再校验安全系数")
    p.add_argument("--user_train_import", default="")
    p.add_argument("--user_loss_fn", default="")
    p.add_argument("--receiver_dir", default="",
                   help="算 variants_total 用；默认 $ORCA_KB_DIR/families/receiver")
    args = p.parse_args()

    # 单写者锁（与 gate / workflow 其它阶段互斥）。
    acquire_run_lock(args.artifacts_dir, os.environ.get("ORCA_RUN_ID", ""))

    # 读 manifest
    with open(args.manifest, "r", encoding="utf-8") as f:
        manifest: list[dict[str, Any]] = json.load(f)
    if not isinstance(manifest, list):
        print(f"[train_pool] FAIL: manifest 不是 list（得到 {type(manifest).__name__}）", file=sys.stderr)
        return 2

    # 解析 device_plan
    try:
        device_plan = json.loads(args.device_plan)
        if not isinstance(device_plan, list):
            raise ValueError("device_plan 不是 JSON list")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[train_pool] FAIL: device_plan 解析失败：{e}", file=sys.stderr)
        return 2

    n_accepted = len(manifest)
    requested_concurrency = max(1, args.concurrency)

    # ── Phase B 启动前 VRAM 再校验（防护 setup→train 之间显存被抢）──────────────────
    effective, warn = revalidate_vram(device_plan, args.per_variant_vram_bytes, args.safety)
    fail_reason = ""
    sweep_status = "SUCCESS"

    if n_accepted == 0:
        # gate 全 FAIL_latency（路由 $end）；空批不算错
        concurrency = 1
        print("[train_pool] WARN: manifest 空（n_accepted=0），无训练任务", file=sys.stderr)
    elif effective == 0 and any(d.startswith("cuda:") for d in device_plan):
        # 显存被抢光，连 1 都放不下 → fail loud
        msg = (f"VRAM 再校验失败：free VRAM 连 1 个 variant（需 {args.per_variant_vram_bytes}B）都放不下；"
               f"effective_concurrency=0（device_plan={device_plan}）")
        print(f"[train_pool] FAIL: {msg}", file=sys.stderr)
        print("VARIANTS_DONE: 0")
        print(f"VARIANTS_TOTAL: {n_accepted}")
        print("SWEEP_STATUS: FAIL")
        print(f"FAIL_REASON: {msg}")
        return 2  # fail loud
    elif 0 < effective < requested_concurrency:
        concurrency = effective
        print(f"[train_pool] WARN: VRAM 再校验降级并发 {requested_concurrency}->{concurrency}（{warn}）",
              file=sys.stderr)
        fail_reason = warn or f"VRAM 降级 {requested_concurrency}->{concurrency}"
    else:
        concurrency = requested_concurrency
        if warn:
            print(f"[train_pool] WARN: {warn}", file=sys.stderr)

    ctx = {
        "target_latency_ms": args.target_latency_ms,
        # 落账 latency_provider_id 用 gate/setup 的同一 provider 串（跨 run done 谓词身份匹配）。
        "provider_id": provider_id(args.latency_provider),
        "accuracy_baseline": args.accuracy_baseline,
        "accuracy_baseline_kind": args.accuracy_baseline_kind,
        "test_command": args.test_command,
        "teacher_cache": args.teacher_cache,
        "kd_scripts_dir": args.kd_scripts_dir,
        "artifacts_dir": args.artifacts_dir,
        "per_run_artifacts_dir": args.per_run_artifacts_dir,
        "project_root": args.project_root,
        "epochs": args.epochs,
        "seed": args.seed,
        "user_train_import": args.user_train_import,
        "user_loss_fn": args.user_loss_fn,
        # measure --skip_latency：不导 ONNX、不测 latency → 不绑 GPU 卡（避免与 train worker 抢）。
        "measure_device": "cpu",
    }

    # ── 并发池 ─────────────────────────────────────────────────────────────────
    if n_accepted > 0:
        print(f"[train_pool] {n_accepted} ACCEPTED 变体，并发 {concurrency}，device_plan={device_plan}",
              file=sys.stderr)
        with cf.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = {
                pool.submit(_train_one, ctx, entry, device_plan[i % max(len(device_plan), 1)]):
                    entry
                for i, entry in enumerate(manifest)
            }
            for fut in cf.as_completed(futs):
                entry = futs[fut]
                vid = entry["variant_id"]
                try:
                    row = fut.result()
                except Exception as e:  # 单 worker 崩不杀整批
                    traceback.print_exc(file=sys.stderr)
                    base = _base_row_from_entry(entry, provider_id_str=ctx["provider_id"])
                    base["target_latency_ms"] = float(ctx["target_latency_ms"])
                    base["accuracy_baseline"] = float(ctx["accuracy_baseline"])
                    row = {
                        **base,
                        "accepted_cfg": entry["accepted_cfg"],
                        "cfg_hash": hashlib.sha256(
                            json.dumps(entry["accepted_cfg"], sort_keys=True).encode()
                        ).hexdigest()[:16],
                        "latency_ms_median": float(entry["latency_ms_median"]),
                        "latency_ms_std": float(entry["latency_ms_std"]),
                        "status": "FAIL_train", "accuracy": 0, "accuracy_kind": "",
                        "met_latency": True, "met_accuracy": False, "ckpt": "",
                        "fail_reason": f"worker exception: {type(e).__name__}: {e}",
                    }
                    if fail_reason == "":
                        fail_reason = f"{vid} worker exception"
                # 主线程逐行增量落账（已持 orca.lock）
                append_ledger_row(args.ledger, row)
                print(f"[train_pool] done {vid}: {row['status']}", file=sys.stderr)

    # ── 末尾 viz（sidecar，异常不阻断）─────────────────────────────────────────
    if n_accepted > 0:
        viz_argv = [
            sys.executable, os.path.join(args.kd_scripts_dir, "viz_kd.py"),
            "--ledger", args.ledger,
            "--target_latency_ms", args.target_latency_ms,
            "--accuracy_baseline", args.accuracy_baseline,
            "--accuracy_baseline_kind", args.accuracy_baseline_kind,
            "--env_anchor", args.per_run_artifacts_dir,
        ]
        try:
            viz_proc = subprocess.run(viz_argv, capture_output=True, text=True, check=False)
        except Exception as e:
            print(f"[train_pool] WARN: viz_kd 异常（不阻断）：{type(e).__name__}: {e}", file=sys.stderr)
        else:
            # viz 失败不阻断 sweep（viz 是 sidecar），但**不静默吞**（code-reviewer R2）：
            # 把 stderr 尾部 300 字打出来让用户能定位「图为什么没推」。
            if viz_proc.returncode != 0:
                print(
                    f"[train_pool] WARN: viz_kd rc={viz_proc.returncode}（不阻断）："
                    f"{viz_proc.stderr[-300:].strip()}",
                    file=sys.stderr,
                )

    # ── 统计 + emit ─────────────────────────────────────────────────────────────
    # variants_total 优先用 --receiver_dir（setup 探测经 output 传下来，cwd 无关）。
    # 旧实现仅 fallback $ORCA_KB_DIR——但 ORCA_KB_DIR 在 in-session ``orca next`` 链里被
    # 重置成默认 ``~/.orca/knowledge_base``（不存在）→ glob 0（BUG-3）。
    # 三级 fallback：--receiver_dir → $ORCA_KB_DIR/families/receiver → ledger+manifest 推断。
    receiver_dir = args.receiver_dir or os.path.join(
        os.environ.get("ORCA_KB_DIR", ""), "families", "receiver")
    variants_total = 0
    try:
        if os.path.isdir(receiver_dir):
            variants_total = len([n for n in os.listdir(receiver_dir)
                                  if n.endswith(".py") and not n.startswith("_")])
    except OSError:
        pass
    if variants_total == 0:
        # receiver_dir 不可用（in-session ORCA_KB_DIR 重置 / setup 未传）→ 用 ledger +
        # manifest 推断：「已接触过的变体数下界」（ledger 行数含历史 FAIL_*，本批 ACCEPTED 是 manifest 大小）。
        # 注：跨 run 累积下可能 > KB 真实变体数，仅作诊断字段（不卡门），优于静默 0（BUG-3）。
        ledger_n = len(read_ledger(args.ledger))
        inferred = ledger_n + n_accepted
        if inferred > 0:
            print(
                f"[train_pool] WARN: receiver_dir={receiver_dir} 无变体或不可访问"
                f"（ORCA_KB_DIR 重置？），variants_total fallback 到 ledger+n_accepted={inferred}",
                file=sys.stderr,
            )
            variants_total = inferred

    rows = read_ledger(args.ledger)
    variants_done = len(rows)

    if fail_reason:
        sweep_status = "FAIL"

    print(f"VARIANTS_DONE: {variants_done}")
    print(f"VARIANTS_TOTAL: {variants_total}")
    print(f"SWEEP_STATUS: {sweep_status}")
    print(f"FAIL_REASON: {fail_reason}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

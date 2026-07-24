"""gate_all.py —— KD-NAS 确定性 gate（一个节点内**串行**遍历全部变体）。

为什么是它：原 ``kd-nas.yaml`` 用 workflow 循环（selector→distill→recorder→…）一个变体一轮地跑，
LLM 编排开销 ×N 变体很贵；且并发训练对 latency 测量 contention 敏感。新 DAG 把 latency gate 收进
**一个节点一个脚本**：本脚本串行遍历所有变体，每变体 ``_validate_variant`` + ``tune_latency`` +
``distill_dispatch``，时延读数干净无 contention。ACCEPTED 进 manifest 给 train 节点并发训练；FAIL_latency
当场落账（增量持久）。

确定性：变体序 = ``pick_variant._list_variants``（文件名排序）；每变体 tune 用固定 seed。
fail loud：脚本非零退出仅在输入契约不符；单变体 tune/export 异常 → 该变体记 FAIL_train 行（不杀整批）。

**ledger 写**：FAIL_latency 行 + 异常 FAIL_train 行各自完成立即 append（主线程持 ``orca.lock``，逐行
``write+flush``，crash 不丢已完成行）。

CLI::

    python3 gate_all.py \\
      --receiver_dir <KB/receiver> --ledger <ledger.jsonl> \\
      --target_latency_ms <f> --latency_provider <path::func> \\
      --artifacts_dir <kd_artifacts_dir> --kd_scripts_dir <_kd_scripts> \\
      --latency_tune_budget <int> [--measure_repeats 3] [--device auto] [--seed 0] \\
      [--accuracy_baseline <f>] [--force_rerun] [--manifest_out <path>]

stdout::

    ACCEPTED_MANIFEST_PATH: <abs path>      # 空 manifest 文件路径也返回（n_accepted=0 时）
    N_ACCEPTED: <int>
    N_FAIL_LATENCY: <int>
    ALL_VARIANTS_COUNT: <int>
    ALL_PROCESSED: true|false               # false = 中途某变体异常未走完
    SKIPPED_DONE: <int>                      # 已 done 被跳过的变体数（诊断）
"""

from __future__ import annotations

import argparse
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
    is_variant_done,
    parse_key,
    provider_id,
    read_ledger,
    run_subproc,
    sha256_file,
)
from pick_variant import _list_variants, _load_variant, _validate_variant  # noqa: E402


def _base_row(
    *, vid: str, variant_path: str, vsha: str, provider_id_str: str,
    target_latency_ms: float, accuracy_baseline: float,
) -> dict[str, Any]:
    """ledger 行的公共身份字段（与原 train_variants_parallel 一致，跨 run 复用真相源）。"""
    return {
        "variant_id": vid,
        "variant_path": os.path.abspath(variant_path),
        "variant_sha256": vsha,
        "run_id": os.environ.get("ORCA_RUN_ID", ""),
        "latency_provider_id": provider_id_str,
        "target_latency_ms": float(target_latency_ms),
        "accuracy_baseline": float(accuracy_baseline),
    }


def _fail_latency_row(
    base: dict[str, Any], *, accepted_cfg: dict[str, Any], lat_med: float,
    lat_std: float, fail_reason: str,
) -> dict[str, Any]:
    cfg_hash = hashlib.sha256(
        json.dumps(accepted_cfg, sort_keys=True).encode()
    ).hexdigest()[:16]
    return {
        **base,
        "status": "FAIL_latency",
        "accepted_cfg": accepted_cfg,
        "cfg_hash": cfg_hash,
        "latency_ms_median": lat_med,
        "latency_ms_std": lat_std,
        "accuracy": 0,
        "accuracy_kind": "",
        "met_latency": False,
        "met_accuracy": False,
        "ckpt": "",
        "fail_reason": fail_reason,
    }


def _run_gate_for_variant(
    ctx: dict[str, Any], variant_path: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """单变体 gate 处理。

    返回 ``(manifest_entry_or_None, ledger_row_or_None, status)``：
      - ACCEPTED → ``(entry, None, "ACCEPTED")``（不落账，交 train 训完落）
      - FAIL_latency → ``(None, fail_latency_row, "FAIL_latency")``（当场落账）
      - SKIPPED_DONE → ``(None, None, "SKIPPED_DONE")``
      - 异常 → ``(None, fail_train_row, "FAIL_train")``（也当场落账）
    """
    vid = os.path.splitext(os.path.basename(variant_path))[0]
    vsha = sha256_file(variant_path)
    mod = _load_variant(variant_path)
    dummy_input, knobs = _validate_variant(mod, variant_path)
    build_fn = getattr(mod, "BUILD_FN", "build_model")

    base = _base_row(
        vid=vid, variant_path=variant_path, vsha=vsha,
        provider_id_str=ctx["provider_id"],
        target_latency_ms=ctx["target_latency_ms"],
        accuracy_baseline=ctx["accuracy_baseline"],
    )

    # done 谓词（force_rerun 时跳过判断）
    if not ctx["force_rerun"]:
        rows_for_v = [r for r in ctx["ledger_rows"] if r.get("variant_id") == vid]
        if is_variant_done(rows_for_v, float(ctx["target_latency_ms"]),
                           ctx["provider_id"], vsha):
            return None, None, "SKIPPED_DONE"

    dummy_json = json.dumps(dummy_input)
    knobs_json = json.dumps(knobs)

    # tune_latency（最小缩量；HI-2 seed / HI-5 cache / HI-13 median+std）
    rc, out, err = run_subproc([
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
        row = {
            **base,
            "status": "FAIL_train", "accepted_cfg": {}, "cfg_hash": "",
            "latency_ms_median": -1, "latency_ms_std": 0,
            "accuracy": 0, "accuracy_kind": "",
            "met_latency": False, "met_accuracy": False, "ckpt": "",
            "fail_reason": f"tune_latency rc={rc}: {err[-300:]}",
        }
        return None, row, "FAIL_train"

    tune_status = parse_key(out, "TUNE_STATUS") or "FAIL_latency"
    cfg_str = parse_key(out, "ACCEPTED_CFG") or parse_key(out, "BEST_EFFORT_CFG") or "{}"
    lat_med = float(parse_key(out, "LATENCY_MS_MEDIAN") or -1)
    lat_std = float(parse_key(out, "LATENCY_MS_STD") or 0)
    accepted_cfg = json.loads(cfg_str)

    # distill_dispatch（BLK-17 确定性门）
    rc2, out2, err2 = run_subproc([
        sys.executable, os.path.join(ctx["kd_scripts_dir"], "distill_dispatch.py"),
        "--tune_status", tune_status,
    ])
    action = parse_key(out2, "DISTILL_ACTION") if rc2 == 0 else None
    if action is None:
        row = _fail_latency_row(
            base, accepted_cfg=accepted_cfg, lat_med=lat_med, lat_std=lat_std,
            fail_reason=f"distill_dispatch rc={rc2}: {err2[-300:]}",
        )
        # dispatch 异常 → 记 FAIL_train（语义更准），用 base row
        row["status"] = "FAIL_train"
        row["met_latency"] = tune_status == "ACCEPTED"
        return None, row, "FAIL_train"

    if action == "noop":  # FAIL_latency → 当场落账
        row = _fail_latency_row(
            base, accepted_cfg=accepted_cfg, lat_med=lat_med, lat_std=lat_std,
            fail_reason="latency over target",
        )
        return None, row, "FAIL_latency"

    # ACCEPTED → 进 manifest
    entry = {
        "variant_id": vid,
        "variant_path": os.path.abspath(variant_path),
        "variant_sha256": vsha,
        "accepted_cfg": accepted_cfg,
        "latency_ms_median": lat_med,
        "latency_ms_std": lat_std,
        "build_fn": build_fn,
        "dummy_input": dummy_input,
        "knobs": knobs,
    }
    return entry, None, "ACCEPTED"


def _main() -> int:
    p = argparse.ArgumentParser(description="KD-NAS 确定性 gate（串行遍历全部变体）")
    p.add_argument("--receiver_dir", default="", help="默认 $ORCA_KB_DIR/families/receiver")
    p.add_argument("--ledger", required=True, help="共享 ledger.jsonl")
    p.add_argument("--target_latency_ms", required=True)
    p.add_argument("--latency_provider", required=True)
    p.add_argument("--artifacts_dir", required=True, help="稳定 kd_artifacts_dir（写 manifest + lock）")
    p.add_argument("--kd_scripts_dir", required=True, help="_kd_scripts 绝对路径")
    p.add_argument("--accuracy_baseline", default="0.0", help="落账基线字段（train 实际测）")
    p.add_argument("--latency_tune_budget", type=int, default=40, help="tune_latency --max_measurements")
    p.add_argument("--measure_repeats", type=int, default=3)
    p.add_argument("--device", default="auto")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--force_rerun", action="store_true")
    p.add_argument("--manifest_out", default="",
                   help="manifest 输出路径；默认 <artifacts_dir>/gate_manifest.json")
    args = p.parse_args()

    receiver_dir = args.receiver_dir or os.path.join(
        os.environ.get("ORCA_KB_DIR", ""), "families", "receiver")

    # 单写者锁（与 workflow 其它阶段互斥；主线程持锁串行落账）。
    acquire_run_lock(args.artifacts_dir, os.environ.get("ORCA_RUN_ID", ""))
    ledger_rows = read_ledger(args.ledger)
    all_variants = _list_variants(receiver_dir)

    ctx = {
        "target_latency_ms": args.target_latency_ms,
        "latency_provider": args.latency_provider,
        "provider_id": provider_id(args.latency_provider),
        "accuracy_baseline": args.accuracy_baseline,
        "artifacts_dir": args.artifacts_dir,
        "kd_scripts_dir": args.kd_scripts_dir,
        "max_measurements": args.latency_tune_budget,
        "measure_repeats": args.measure_repeats,
        "device": args.device,
        "seed": args.seed,
        "force_rerun": args.force_rerun,
        "ledger_rows": ledger_rows,
    }

    manifest: list[dict[str, Any]] = []
    n_accepted = 0
    n_fail_latency = 0
    n_skipped_done = 0
    n_fail_train = 0
    all_processed = True

    print(f"[gate] {len(all_variants)} 变体，串行 gate 开始", file=sys.stderr)
    if len(all_variants) == 0:
        # 空 KB 静默 SUCCESS 是隐藏 bug（code-reviewer R3）：用户 99% 是 ORCA_KB_DIR 指错 /
        # families/receiver/ 无 .py。stderr WARN 让用户能定位，不静默「N_ACCEPTED:0」误以为 workflow 健康。
        print(
            f"[gate] WARN: receiver_dir={receiver_dir} 下无 .py 变体（ORCA_KB_DIR 指错？"
            f"families/receiver/ 为空？）→ N_ACCEPTED:0，workflow 将路由 $end 跳过 train。",
            file=sys.stderr,
        )
    for variant_path in all_variants:
        vid = os.path.splitext(os.path.basename(variant_path))[0]
        try:
            entry, row, status = _run_gate_for_variant(ctx, variant_path)
        except Exception as e:  # 单变体崩不杀整批：记 FAIL_train 行
            traceback.print_exc(file=sys.stderr)
            vsha = ""
            try:
                vsha = sha256_file(variant_path)
            except Exception:
                pass
            base = _base_row(
                vid=vid, variant_path=variant_path, vsha=vsha,
                provider_id_str=ctx["provider_id"],
                target_latency_ms=ctx["target_latency_ms"],
                accuracy_baseline=ctx["accuracy_baseline"],
            )
            row = {
                **base, "status": "FAIL_train", "accepted_cfg": {}, "cfg_hash": "",
                "latency_ms_median": -1, "latency_ms_std": 0,
                "accuracy": 0, "accuracy_kind": "",
                "met_latency": False, "met_accuracy": False, "ckpt": "",
                "fail_reason": f"gate exception: {type(e).__name__}: {e}",
            }
            entry, status = None, "FAIL_train"
            all_processed = False  # 有异常 → all_processed=false（warn 但不阻塞 train）

        if status == "SKIPPED_DONE":
            n_skipped_done += 1
            print(f"[gate] {vid}: SKIPPED_DONE", file=sys.stderr)
            continue
        if status == "ACCEPTED":
            manifest.append(entry)
            n_accepted += 1
            print(f"[gate] {vid}: ACCEPTED (latency={entry['latency_ms_median']:.3f}ms)",
                  file=sys.stderr)
            continue
        # FAIL_latency / FAIL_train → 当场增量落账
        if row is not None:
            append_ledger_row(args.ledger, row)
        if status == "FAIL_latency":
            n_fail_latency += 1
            print(f"[gate] {vid}: FAIL_latency (latency={row['latency_ms_median']:.3f}ms) -> ledger",
                  file=sys.stderr)
        else:  # FAIL_train
            n_fail_train += 1
            print(f"[gate] {vid}: FAIL_train during gate -> ledger", file=sys.stderr)

    # 写 manifest 文件（即使空也写，train 据此判 n_accepted=0）
    manifest_path = args.manifest_out or os.path.join(args.artifacts_dir, "gate_manifest.json")
    manifest_path = os.path.abspath(manifest_path)
    os.makedirs(os.path.dirname(manifest_path) or ".", exist_ok=True)
    tmp = manifest_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, manifest_path)  # 原子替换

    print(f"[gate] done: accepted={n_accepted} fail_latency={n_fail_latency} "
          f"fail_train={n_fail_train} skipped_done={n_skipped_done} "
          f"total={len(all_variants)} all_processed={all_processed}", file=sys.stderr)

    print(f"ACCEPTED_MANIFEST_PATH: {manifest_path}")
    print(f"N_ACCEPTED: {n_accepted}")
    print(f"N_FAIL_LATENCY: {n_fail_latency}")
    print(f"ALL_VARIANTS_COUNT: {len(all_variants)}")
    print(f"ALL_PROCESSED: {'true' if all_processed else 'false'}")
    print(f"SKIPPED_DONE: {n_skipped_done}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

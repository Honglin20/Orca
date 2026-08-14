"""gate_report.py —— Puzzle AC gate 终态断言（U6 适配器）。

U6 改造：
  - root cause I：读 ``adapters.METRIC_DIRECTION``（higher-better / lower-better）。
    higher-better ACC 容差 L12 baseline-dependent（design §0 L12，闭环 LV-1 BLOCKER）：
    baseline ≥ 0.5 → ``final ≥ max(baseline−0.5, baseline×0.99)``（取严，等价精度损失 <1%）；
    baseline < 0.5 → ``final ≥ baseline×0.9``（比例保护，近随机 baseline 不被绝对容差放过）。
    lower-better：``final ≤ baseline × (1 + tol)``（loss「不升太多」）。
    gate AC = 验收 AC（单一真相源，禁双标准）。
  - LAT AC 参数化（coordinator）：``--latency_reduction_target``（默认 0.5），
    判 ``latency_ratio ≤ (1 - reduction)``。如 reduction=0.7 → 要求 ratio ≤ 0.3。
  - root cause J：落盘 ``final_status.json``（统一终态，对齐 ns3_report first-match）。
  - root cause A/K/H：读 ``adapters.evaluate(model)``；不再 resolve_eval_fn / eval_kind 分支。

读 final_model + baseline_metrics + adapters → 测 final acc + latency →
``gate_result.json`` + ``final_report.md`` + ``final_status.json``。

stdout：单行 JSON（pz_report 是 zero-LLM 确定性节点）。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

import torch

from puzzle_common import (
    build_latency_dummy,
    load_optimized_flat,
    load_puzzle_adapters,
    measure_whole_model_latency,
    write_final_status,
)

# L12 ACC 容差（design `puzzle-layer-variant-design-draft.md` §0 L12，闭环
# spec-reviewer LV-1 BLOCKER）。**gate AC = 验收 AC**：单一真相源，禁双标准
# （不再并存 v2 D5 绝对容差 0.5 与 <1% 验收）。
_ACC_ABS_TOL = 0.5              # 高 baseline 绝对容差（L12 max() 的绝对分支）
_ACC_REL_FACTOR_HIGH = 0.01     # 高 baseline 相对容差（精度损失 <1%）
_ACC_REL_FACTOR_LOW = 0.1       # 低 baseline 比例保护（近随机 baseline 不被绝对容差放过）
_ACC_BASELINE_BOUNDARY = 0.5    # 高低 baseline 分界


def _acc_threshold_higher_better(acc_base: float) -> tuple[float, str]:
    """higher-better metric：返回 (final_acc 下限, 容差种类)。

    L12（design §0 L12，闭环 spec-reviewer LV-1 BLOCKER）：
    - acc_base ≥ 0.5：threshold = max(acc_base − 0.5, acc_base × 0.99)
      绝对容差 0.5 与相对 1% 取严。target 0.9919→max(0.4919, 0.9819)=0.9819
      （等价精度损失 <1%，与验收单一真相源对齐，不再 v2 final≥0.4919 歧义）。
    - acc_base < 0.5：threshold = acc_base × 0.9（比例保护；近随机 baseline
      0.085→0.0765，不被绝对容差放过）。

    Note：默认 ``_ACC_ABS_TOL=0.5`` 时，对 accuracy ∈ [0.5, 1.0] 绝对支恒被相对支
    取严（baseline−0.5 > baseline×0.99 ⟺ baseline>50，accuracy 域外）——max() 是防御性
    floor：``--accuracy_tolerance`` CLI 调小（如 0）才让绝对支激活收紧 gate。
    """
    if acc_base >= _ACC_BASELINE_BOUNDARY:
        abs_thr = acc_base - _ACC_ABS_TOL
        rel_thr = acc_base * (1.0 - _ACC_REL_FACTOR_HIGH)
        return max(abs_thr, rel_thr), "l12-strict"
    return acc_base * (1.0 - _ACC_REL_FACTOR_LOW), "proportional"


def _acc_pass_higher_better(acc_base: float, acc_opt: float) -> tuple[bool, str, float]:
    threshold, kind = _acc_threshold_higher_better(acc_base)
    return acc_opt >= threshold, kind, threshold


def _lower_better_pass(base: float, final: float, rel_tol: float) -> bool:
    """lower-better metric pass：``final ≤ base × (1 + rel_tol)``。"""
    return final <= base * (1.0 + rel_tol)


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Puzzle U6 gate report")
    parser.add_argument("--final_model", required=True, help="final_model.pt 路径")
    parser.add_argument("--baseline_metrics", required=True)
    parser.add_argument(
        "--optimized_flat", required=True,
        help="<base>_optimized_flat.py（pz_materialize 产出；student 执行基底）",
    )
    parser.add_argument(
        "--adapters", required=True,
        help="puzzle_adapters.py 路径（U6 §2.1：脚本唯一项目接口）",
    )
    parser.add_argument(
        "--manifest", default="",
        help="manifest.yaml 路径（metadata 用；脚本不解析）",
    )
    parser.add_argument("--latency_unit", default="ms", choices=["ms", "us", "s"])
    parser.add_argument("--latency_script_path", default="")
    parser.add_argument(
        "--latency_reduction_target", type=float, default=0.5,
        help="LAT AC 参数化（与 mip_select 同源）：要求时延降幅比例（0.5=降一半）。"
        "判 latency_ratio ≤ (1 - reduction)",
    )
    parser.add_argument(
        "--metric_rel_tol_lower_better", type=float, default=0.1,
        help="lower-better metric 相对容差（final ≤ baseline × (1 + tol)）；默认 10%",
    )
    parser.add_argument(
        "--accuracy_tolerance", type=float, default=0.5,
        help="[advanced] 高 baseline 绝对容差 floor（baseline ≥ 0.5 时 L12 max() 的绝对分支："
        "threshold = max(baseline − accuracy_tolerance, baseline × 0.99)）。默认 0.5 时"
        "该支对 accuracy ∈ [0.5,1.0] 恒被相对支（baseline×0.99）取严（防御性 floor，no-op）；"
        "调小（如 0）才激活收紧 gate（要求 final 近 baseline）",
    )
    parser.add_argument("--output_dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    global _ACC_ABS_TOL
    _ACC_ABS_TOL = float(args.accuracy_tolerance)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        adapters = load_puzzle_adapters(args.adapters)
        with open(args.baseline_metrics, encoding="utf-8") as f:
            baseline = json.load(f)
        baseline_metric = float(baseline["baseline_acc"])  # 通用 metric（acc/loss/...）
        baseline_latency = float(baseline["baseline_latency"])
        # metric_direction 优先读 adapters；baseline_metrics 的记法兼容（审计回填）
        metric_direction = adapters.METRIC_DIRECTION

        final_ckpt = torch.load(
            args.final_model, map_location="cpu", weights_only=False
        )
        if not isinstance(final_ckpt, dict) or not final_ckpt.get("selected_arch"):
            raise ValueError(f"{args.final_model} 缺 selected_arch 字段")
        final_state_dict = final_ckpt.get("state_dict", {})
        if not final_state_dict:
            raise RuntimeError(
                f"{args.final_model} state_dict 为空——final_model 损坏,"
                f"无法 gate（禁用随机 init student 假装通过）"
            )

        device = torch.device("cpu")

        # student = optimized_flat.build_model()（唯一执行基底）+ strict 载 final_model.pt。
        # materialize 自检已保证 optimized_flat 与 build_student_from_arch 逐 key 对齐 →
        # final_model（GKD 在同结构上训练产出）strict 可载。
        opt_flat = load_optimized_flat(args.optimized_flat)
        student = opt_flat.build_model()
        student.load_state_dict(final_state_dict, strict=True)
        student.eval().to(device)

        # U6 root cause A/K/H：evaluate 经 adapters（不再 resolve_eval_fn / eval_kind）
        final_metric_raw = adapters.evaluate(student)
        if isinstance(final_metric_raw, bool) or not isinstance(final_metric_raw, (int, float)):
            raise TypeError(
                f"adapters.evaluate 返回非数值：{type(final_metric_raw).__name__}"
            )
        final_metric = float(final_metric_raw)

        # latency（per-inference batch-1：与 baseline measure_baseline + per-block latency_table 同尺度）
        lat_dummy = build_latency_dummy(adapters, device=device)
        final_latency = measure_whole_model_latency(
            student, adapters.forward_model, lat_dummy, device, args.latency_script_path,
            convention=adapters.FORWARD_CALLING_CONVENTION,
        )

        metric_delta = abs(final_metric - baseline_metric)
        latency_ratio = (
            final_latency / baseline_latency if baseline_latency > 0 else float("inf")
        )

        # U6 root cause I：方向感知
        if metric_direction == "higher-better":
            metric_met, metric_tol_kind, metric_threshold = _acc_pass_higher_better(
                baseline_metric, final_metric
            )
            metric_pass_formula = (
                f"final({final_metric:.4f}) ≥ threshold({metric_threshold:.4f}) "
                f"[{metric_tol_kind} tol]"
            )
        else:  # lower-better
            rel_tol = float(args.metric_rel_tol_lower_better)
            metric_met = _lower_better_pass(baseline_metric, final_metric, rel_tol)
            metric_tol_kind = "relative-lower"
            metric_threshold = baseline_metric * (1.0 + rel_tol)
            metric_pass_formula = (
                f"final({final_metric:.4f}) ≤ baseline×(1+{rel_tol})="
                f"{metric_threshold:.4f}"
            )

        # LAT AC 参数化（coordinator）：ratio ≤ (1 - reduction)
        reduction = max(0.0, min(1.0, float(args.latency_reduction_target)))
        lat_ratio_threshold = 1.0 - reduction
        lat_met = latency_ratio <= lat_ratio_threshold

        if metric_met and lat_met:
            gate_reason = "both-met"
            gate_status = "pass"
        elif not metric_met and not lat_met:
            gate_reason = "both-miss"
            gate_status = "fail"
        elif not metric_met:
            gate_reason = "metric-miss"
            gate_status = "fail"
        else:
            gate_reason = "latency-miss"
            gate_status = "fail"

        gate_result = {
            "gate_status": gate_status,
            "metric_direction": metric_direction,
            "final_metric": final_metric,
            "baseline_metric": baseline_metric,
            "metric_delta": metric_delta,
            "metric_tolerance_kind": metric_tol_kind,
            "metric_threshold": metric_threshold,
            "metric_pass_formula": metric_pass_formula,
            "final_latency": final_latency,
            "baseline_latency": baseline_latency,
            "latency_ratio": latency_ratio,
            "latency_ratio_threshold": lat_ratio_threshold,
            "latency_reduction_target": reduction,
            "latency_unit": args.latency_unit,
            "gate_reason": gate_reason,
            "report_path": "",
        }

        report_path = output_dir / "final_report.md"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(_render_report(gate_result))
        gate_result["report_path"] = str(report_path)

        gate_result_path = output_dir / "gate_result.json"
        with open(gate_result_path, "w", encoding="utf-8") as f:
            json.dump(gate_result, f, ensure_ascii=False, indent=2)

        # U6 root cause J：统一终态 final_status.json（同 schema 给 Wave 2 terminate 路径用）
        write_final_status(
            output_dir,
            stage="pz_report",
            status=gate_status,
            reason=gate_reason,
            metrics={
                "baseline_metric": baseline_metric,
                "final_metric": final_metric,
                "baseline_latency": baseline_latency,
                "final_latency": final_latency,
                "latency_ratio": latency_ratio,
                "metric_direction": metric_direction,
            },
        )

        print(json.dumps(gate_result, ensure_ascii=False))
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERROR: gate_report 失败 — {type(e).__name__}: {e}\n{tb}", file=sys.stderr)
        return 2


def _render_report(g: dict[str, Any]) -> str:
    return (
        "# Puzzle Final Report\n\n"
        f"- gate_status: **{g['gate_status']}**\n"
        f"- gate_reason: `{g['gate_reason']}`\n"
        f"- metric_direction: `{g['metric_direction']}`\n\n"
        "## Metrics\n\n"
        "| metric | baseline | final | delta / ratio |\n"
        "|---|---|---|---|\n"
        f"| metric ({g['metric_direction']}) | {g['baseline_metric']:.4f} "
        f"| {g['final_metric']:.4f} | Δ={g['metric_delta']:.4f} "
        f"({g['metric_tolerance_kind']} tol, threshold={g['metric_threshold']:.4f}) |\n"
        f"| latency ({g['latency_unit']}) | {g['baseline_latency']:.4f} "
        f"| {g['final_latency']:.4f} | ratio={g['latency_ratio']:.4f} "
        f"(≤{g['latency_ratio_threshold']:.4f}, reduction_target="
        f"{g['latency_reduction_target']:.2f}) |\n\n"
        "## Verdict\n\n"
        + (
            f"AC 双达标（metric 方向 {g['metric_direction']} 容差内 + 时延降幅 "
            f"{g['latency_reduction_target']:.0%} 达成）。"
            if g["gate_status"] == "pass"
            else f"AC 未达标（{g['gate_reason']}）。"
        )
        + "\n"
    )


if __name__ == "__main__":
    sys.exit(main())

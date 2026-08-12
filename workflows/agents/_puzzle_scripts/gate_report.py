"""gate_report.py —— Puzzle P2.9：AC gate 终态断言。

读 final_model + baseline_metrics + eval_fn → 测 final acc + latency →
``gate_result.json``：
  - ACC AC（SPEC v2 §12.1 D5）：相对容差 + 绝对 floor，baseline-dependent：
    - acc_base ≥ 0.5：绝对容差 0.5（final ≥ acc_base − 0.5；高 baseline 保绝对）
    - acc_base < 0.5：相对容差 10%（final ≥ 0.9·acc_base；低 baseline 比例保护）
  - LAT AC：latency_ratio = final_latency / baseline_latency ≤ 0.5
  - gate_status = pass if acc_met AND lat_met
  - gate_reason: both-met / acc-miss / latency-miss / both-miss

写 ``final_report.md``。
stdout：``GATE_STATUS: <pass|fail>`` / ``RESULT_JSON: {...}``。

注：D5 SPEC 文字「``δ = max(0.5, 0.1·acc_base)`` 取更严者」与具体示例
（mnist 0.97 → final≥0.47；target 0.085 → final≥0.0765）内部不一致——示例
匹配 baseline-dependent 规则（高 baseline 绝对、低 baseline 相对），与用户
显式 intent「低 baseline 比例保护，高 baseline 绝对容差」一致。本实现按示例
（baseline-dependent）落地。
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
    BlockMap,
    build_student_from_arch,
    get_module_dummy_input,
    measure_whole_model_latency,
    resolve_eval_fn,
)

# D5 容差参数（SPEC v2 §12.1）
_ACC_ABS_TOL = 0.5      # 高 baseline 绝对容差 floor
_ACC_REL_FACTOR = 0.1   # 低 baseline 相对容差（final ≥ acc_base·(1−rel_factor)）
_ACC_BASELINE_BOUNDARY = 0.5  # 高低 baseline 分界（= abs tol floor）


def _acc_threshold(acc_base: float) -> tuple[float, str]:
    """D5 baseline-dependent：返回 (final_acc 下限, 容差种类)。

    - acc_base ≥ 0.5：绝对容差 → threshold = acc_base − 0.5（保留 floor 保护）
    - acc_base < 0.5：相对容差 → threshold = acc_base·0.9（比例保护，近随机会 fail）

    理由（vs SPEC 文字「max(0.5, 0.1·acc_base)」）：SPEC 的公式与示例内部矛盾
    （mnist 用 abs 0.47，target 用 rel 0.0765——max 公式对两者都给 0.5 abs）。
    本规则与 SPEC §0 D5 intent + §12.1 示例一致。

    Boundary cliff（已知设计特征，非 bug）：baseline=0.500 → threshold=0.0
    （绝对，可降到 0% pass）；baseline=0.499 → threshold=0.4491（相对，仅允许 10%
    降）。0.001 baseline 差异导致 threshold 跳跃——这是 baseline-dependent 切换的
    固有特征。SPEC formula 修订（消除 max 文字矛盾）应同步消除该 cliff；当前实现
    忠实匹配示例 + 用户 intent。
    """
    if acc_base >= _ACC_BASELINE_BOUNDARY:
        return acc_base - _ACC_ABS_TOL, "absolute"
    return acc_base * (1.0 - _ACC_REL_FACTOR), "relative"


def _acc_pass(acc_base: float, acc_opt: float) -> tuple[bool, str, float]:
    """D5：返回 (是否达标, 容差种类, 阈值)。"""
    threshold, kind = _acc_threshold(acc_base)
    return acc_opt >= threshold, kind, threshold


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puzzle P2.9 gate report")
    parser.add_argument("--final_model", required=True, help="final_model.pt 路径")
    parser.add_argument("--baseline_metrics", required=True)
    parser.add_argument("--flat_model", required=True)
    parser.add_argument("--build_fn", required=True)
    parser.add_argument("--build_cfg", default="")
    parser.add_argument("--block_map", required=True)
    parser.add_argument("--block_library", required=True)
    parser.add_argument("--eval_fn", required=True)
    parser.add_argument(
        "--eval_kind",
        required=True,
        choices=["classification", "embedding", "regression"],
    )
    parser.add_argument("--latency_unit", default="ms", choices=["ms", "us", "s"])
    parser.add_argument("--latency_script_path", default="")
    parser.add_argument(
        "--accuracy_tolerance",
        type=float,
        default=0.5,
        help="[DEPRECATED, 被 D5 baseline-dependent 容差取代] 旧绝对容差入参，"
        "保留仅为兼容 yaml/launcher 不破坏——实际 ACC AC 由 _acc_pass 按 baseline "
        "高/低自动选绝对 0.5 或相对 10%（SPEC v2 §12.1 D5）",
    )
    parser.add_argument("--output_dir", required=True)
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with open(args.baseline_metrics, encoding="utf-8") as f:
            baseline = json.load(f)
        baseline_acc = float(baseline["baseline_acc"])
        baseline_latency = float(baseline["baseline_latency"])

        final_ckpt = torch.load(
            args.final_model, map_location="cpu", weights_only=False
        )
        if not isinstance(final_ckpt, dict) or not final_ckpt.get("selected_arch"):
            raise ValueError(f"{args.final_model} 缺 selected_arch 字段")
        selected_arch = final_ckpt["selected_arch"]
        final_state_dict = final_ckpt.get("state_dict", {})

        block_map = BlockMap.from_json(args.block_map)
        device = torch.device("cpu")

        # student = build_student_from_arch 不传 father_state_path：final_model.pt 的
        # state_dict（由 gkd_retrain 保存的完整 student.state_dict()）会覆盖 base arch，
        # 其中 identity（passthrough）slot 的权重经 build_selected → gkd 链已携带预训练
        # father 权重。本节点不构建 baseline father——baseline acc/latency 直接读
        # baseline_metrics.json（pz_expand 用预训练 father 测得）。
        student = build_student_from_arch(
            flat_model_path=args.flat_model,
            build_fn=args.build_fn,
            build_cfg=args.build_cfg,
            block_map=block_map,
            selected_arch=final_ckpt,
            block_library_dir=Path(args.block_library).resolve(),
            device=device,
        )
        if not final_state_dict:
            raise RuntimeError(
                f"{args.final_model} state_dict 为空——final_model 损坏,"
                f"无法 gate（禁用随机 init student 假装通过）"
            )
        missing, unexpected = student.load_state_dict(
            final_state_dict, strict=False
        )
        if unexpected:
            raise RuntimeError(
                f"final_model state_dict 有 {len(unexpected)} 个 unexpected key："
                f"{list(unexpected)[:5]}"
            )
        student.eval().to(device)

        eval_fn = resolve_eval_fn(args.eval_fn, args.flat_model)
        final_acc_raw = eval_fn(student)
        if not isinstance(final_acc_raw, (int, float)):
            raise TypeError(f"eval_fn 返回非数值：{type(final_acc_raw).__name__}")
        final_acc = float(final_acc_raw)

        dummy_meta = get_module_dummy_input(args.flat_model)
        shape = list(dummy_meta["shape"])
        dtype = getattr(torch, str(dummy_meta.get("dtype", "float32")))
        dummy_input = torch.randn(*shape, dtype=dtype)
        final_latency = measure_whole_model_latency(
            student, dummy_input, device, args.latency_script_path
        )

        acc_delta = abs(final_acc - baseline_acc)
        latency_ratio = (
            final_latency / baseline_latency if baseline_latency > 0 else float("inf")
        )
        # D5：ACC AC 相对容差 + floor（baseline-dependent）
        acc_met, acc_tol_kind, acc_threshold = _acc_pass(baseline_acc, final_acc)
        lat_met = latency_ratio <= 0.5
        if acc_met and lat_met:
            gate_reason = "both-met"
            gate_status = "pass"
        elif not acc_met and not lat_met:
            gate_reason = "both-miss"
            gate_status = "fail"
        elif not acc_met:
            gate_reason = "acc-miss"
            gate_status = "fail"
        else:
            gate_reason = "latency-miss"
            gate_status = "fail"

        gate_result = {
            "gate_status": gate_status,
            "final_acc": final_acc,
            "final_latency": final_latency,
            "baseline_acc": baseline_acc,
            "baseline_latency": baseline_latency,
            "acc_delta": acc_delta,
            "acc_tolerance_kind": acc_tol_kind,
            "acc_threshold": acc_threshold,
            "latency_ratio": latency_ratio,
            "latency_unit": args.latency_unit,
            "gate_reason": gate_reason,
            "report_path": "",
        }

        report_path = output_dir / "final_report.md"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(_render_report(gate_result, acc_tol_kind, acc_threshold))
        gate_result["report_path"] = str(report_path)

        gate_result_path = output_dir / "gate_result.json"
        with open(gate_result_path, "w", encoding="utf-8") as f:
            json.dump(gate_result, f, ensure_ascii=False, indent=2)

        # stdout：单行 JSON（pz_report 是 zero-LLM 确定性节点，stdout 直接转发为节点 output）
        print(json.dumps(gate_result, ensure_ascii=False))
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERROR: gate_report 失败 — {type(e).__name__}: {e}\n{tb}", file=sys.stderr)
        return 2


def _render_report(
    g: dict[str, Any], acc_tol_kind: str, acc_threshold: float
) -> str:
    return (
        "# Puzzle Final Report\n\n"
        f"- gate_status: **{g['gate_status']}**\n"
        f"- gate_reason: `{g['gate_reason']}`\n\n"
        "## Metrics\n\n"
        "| metric | baseline | final | delta / ratio |\n"
        "|---|---|---|---|\n"
        f"| accuracy | {g['baseline_acc']:.4f} | {g['final_acc']:.4f} "
        f"| Δ={g['acc_delta']:.4f} (D5 {acc_tol_kind} tol, threshold={acc_threshold:.4f}) |\n"
        f"| latency ({g['latency_unit']}) | {g['baseline_latency']:.4f} "
        f"| {g['final_latency']:.4f} | ratio={g['latency_ratio']:.4f} (≤0.5) |\n\n"
        "## Verdict\n\n"
        + (
            "AC 双达标（精度损失在容差内 + 时延降达一半）。"
            if g["gate_status"] == "pass"
            else f"AC 未达标（{g['gate_reason']}）。"
        )
        + "\n"
    )


if __name__ == "__main__":
    sys.exit(main())

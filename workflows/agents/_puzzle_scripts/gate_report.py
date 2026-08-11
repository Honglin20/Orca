"""gate_report.py —— Puzzle P2.9：AC gate 终态断言。

读 final_model + baseline_metrics + eval_fn → 测 final acc + latency →
``gate_result.json``：
  - acc_delta = |final_acc - baseline_acc|
  - latency_ratio = final_latency / baseline_latency
  - gate_status = pass if acc_delta ≤ accuracy_tolerance AND latency_ratio ≤ 0.5
  - gate_reason: both-met / acc-miss / latency-miss / both-miss

写 ``final_report.md``。
stdout：``GATE_STATUS: <pass|fail>`` / ``RESULT_JSON: {...}``。
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
    load_external_callable,
    resolve_eval_fn,
)


def _measure_latency(
    model,
    dummy_input: torch.Tensor,
    device: torch.device,
    latency_script_path: str,
) -> float:
    if latency_script_path:
        fn = load_external_callable(latency_script_path)
        return float(fn(model, dummy_input))
    from nas_agent.latency import measure_module_latency
    return float(measure_module_latency(model, dummy_input, device, repetitions=100, warmup=30))


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
    parser.add_argument("--accuracy_tolerance", type=float, default=0.5)
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
        final_latency = _measure_latency(
            student, dummy_input, device, args.latency_script_path
        )

        acc_delta = abs(final_acc - baseline_acc)
        latency_ratio = (
            final_latency / baseline_latency if baseline_latency > 0 else float("inf")
        )
        acc_met = acc_delta <= args.accuracy_tolerance
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
            "latency_ratio": latency_ratio,
            "latency_unit": args.latency_unit,
            "gate_reason": gate_reason,
            "report_path": "",
        }

        report_path = output_dir / "final_report.md"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(_render_report(gate_result, args.accuracy_tolerance))
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


def _render_report(g: dict[str, Any], tolerance: float) -> str:
    return (
        "# Puzzle Final Report\n\n"
        f"- gate_status: **{g['gate_status']}**\n"
        f"- gate_reason: `{g['gate_reason']}`\n\n"
        "## Metrics\n\n"
        "| metric | baseline | final | delta / ratio |\n"
        "|---|---|---|---|\n"
        f"| accuracy | {g['baseline_acc']:.4f} | {g['final_acc']:.4f} "
        f"| Δ={g['acc_delta']:.4f} (tol={tolerance}) |\n"
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

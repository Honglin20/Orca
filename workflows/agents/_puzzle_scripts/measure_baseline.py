"""measure_baseline.py —— Puzzle U6：确定性测量 + fidelity smoke（适配器架构）。

U6 范式（SPEC puzzle-u6-design-draft §3）：脚本不再假设任何用户代码形态——所有项目
相关性由 ``adapters`` 暴露的能力 API 消化（forward_model / calib_iter / evaluate /
load_pretrained / METRIC_DIRECTION / EVAL_NOISE_ATOL）。

职责（确定性，fail loud）——pz_expand 的「测量」部分（判断由 LLM 做）：
  1. 读 LLM 产的 ``search_space.yaml`` → slot 声明（path/kind/证据）。
  2. fidelity smoke（SPEC U6 §3 改造）：
     - **ckpt 宽松**（root cause C）：删 strict-load 双零硬 raise；加载走
       ``adapters.load_pretrained``，记 ``_LoadResult`` 进 baseline_metrics（前缀剥离 /
       多字段 dict / module./_orig_mod./ema 由适配器负责，脚本不假设 schema）。
       ckpt 非双零不 fatal（WARN + 记录）；flatten 阶段对齐 ns3 只跑前向 dummy smoke。
     - **forward-determinism**：``adapters.forward_model(model, batch)`` 两次 torch.equal。
     - **eval-stability**（root cause B）：``adapters.evaluate(model)`` 跑两次，atol 读
       ``adapters.EVAL_NOISE_ATOL``（不再硬编码 1e-9）。
     - **per-slot identity allclose**：hook 每个 slot，forward 两次逐元素 allclose。
  3. trace 每个 slot 的 in/out 末维 → 回填 search_space.yaml + 落 block_map.json。
  4. 测 baseline acc + latency → baseline_metrics.json。

empty slots（E22）→ exit 2（terminate_unsupported，确定性 post-check）。
任一 smoke 失败 → exit 2 + stderr 写明哪道 smoke（fail loud，不静默吞）。

stdout 关键行：
    BASELINE_ACC: <value>
    BASELINE_LATENCY: <value>
    BLOCK_MAP: <path>
    SEARCH_SPACE: <path>
    RESULT_JSON: {...}
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

import torch
import torch.nn as nn

from puzzle_common import (
    _LoadResult,
    build_latency_dummy,
    build_pretrained_model,
    load_puzzle_adapters,
    measure_block_zero_floor_latency,
    measure_whole_model_latency,
)
from search_space_io import (
    load_search_space_yaml,
    save_search_space_yaml,
    to_block_map,
)

# per-slot identity allclose 容差（slot-level 复现性，与 EVAL_NOISE_ATOL 语义不同）
_ALLCLOSE_ATOL = 1e-5

# exit code 约定：0 = 成功；2 = unsupported（空 slots / smoke 失败）；
# 3 = latency 结构性不可达（block 替换最大 reduction < latency_reduction_target，早退不进 BLD）。
# 区别：3 不是异常——模型可优化、smoke 全绿，只是目标过高；2 才是真 unsupported/异常。
_EXIT_LATENCY_INFEASIBLE = 3

# max_achievable_reduction 与 latency_reduction_target 比较的数值容差（避免浮点边界误判）。
_FEASIBILITY_TOL = 1e-6


# ── smoke 1: ckpt 宽松加载（root cause C）─────────────────────────────────────

def load_father_ckptrck(model: nn.Module, adapters: Any) -> _LoadResult:
    """通过 ``adapters.load_pretrained(model)`` 注入预训练权重，返回 ``_LoadResult``。

    U6 root cause C：脚本不再做 strict-load 双零硬门——前缀剥离 / 多字段 dict /
    module./_orig_mod./ema. 由适配器消化。结果记入 baseline_metrics；
    ``from_scratch=True`` 时 WARN（baseline 可能近随机 init）但不 fatal（对齐 ns3）。
    """
    result = adapters.load_pretrained(model)
    if not isinstance(result, _LoadResult):
        # _LoadResult 是 NamedTuple（duck-typed 兼容：支持属性访问即可）
        if not (hasattr(result, "missing") and hasattr(result, "unexpected")
                and hasattr(result, "from_scratch")):
            raise TypeError(
                f"adapters.load_pretrained 须返 _LoadResult，得到 {type(result).__name__}"
            )
    if result.from_scratch:
        print(
            f"[measure_baseline] WARN: adapters.load_pretrained 标记 from_scratch=True"
            f"（missing={len(result.missing)}, unexpected={len(result.unexpected)}）"
            f"——baseline 可能近随机 init。后续 AC 判定按 baseline 原样计算",
            file=sys.stderr,
        )
    return result


# ── smoke 2 + 4: forward-determinism + per-slot identity allclose ─────────────

def _hook_slot_outputs(
    model: nn.Module, paths: list[str], batch: Any,
    forward_fn: Any, device: torch.device,
) -> dict[str, torch.Tensor]:
    """forward 一次，hook 抓每个 slot path 的 output tensor（detach）。"""
    model.eval().to(device)
    captured: dict[str, torch.Tensor] = {}
    handles: list[Any] = []

    def make_hook(path: str):
        def hook(_mod: nn.Module, _inputs: tuple, output: Any):
            if path in captured:
                return
            out_t = output[0] if isinstance(output, (tuple, list)) else output
            captured[path] = out_t.detach()
        return hook

    try:
        for path in paths:
            try:
                mod = model.get_submodule(path)
            except AttributeError as e:
                raise AttributeError(
                    f"slot path {path!r} 定位失败（get_submodule）：{e}——检查 search_space path"
                ) from e
            handles.append(mod.register_forward_hook(make_hook(path)))
        with torch.no_grad():
            forward_fn(model, batch)
    finally:
        for h in handles:
            h.remove()
    missing_paths = [p for p in paths if p not in captured]
    if missing_paths:
        raise RuntimeError(
            f"hook 未捕获 slot output：{missing_paths[:3]}（共 {len(missing_paths)} 个）"
        )
    return captured


def forward_determinism_and_identity_allclose(
    model: nn.Module,
    slot_paths: list[str],
    batch: Any,
    forward_fn: Any,
    device: torch.device,
) -> None:
    """smoke 2 + smoke 4 合并跑（两次 forward 复用）。

    - smoke 2（forward-determinism）：whole-output torch.equal（捕获未固定 RNG）。
    - smoke 4（per-slot identity allclose）：每个 slot 两次 forward output 逐元素 allclose
      （father-loaded 模块在 zero intervention 下输出稳定可复现——identity 契约的前置条件）。
    """
    model.eval().to(device)
    with torch.no_grad():
        out1 = forward_fn(model, batch)
        out2 = forward_fn(model, batch)

    def _equal(a: Any, b: Any, i: int | None = None) -> bool:
        if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
            if len(a) != len(b):
                return False
            return all(_equal(x, y) for x, y in zip(a, b))
        if isinstance(a, torch.Tensor) and isinstance(b, torch.Tensor):
            return torch.equal(a, b)
        return False

    if not _equal(out1, out2):
        label = "whole-output" if not isinstance(out1, (tuple, list)) else "tuple output"
        raise RuntimeError(
            f"forward-determinism smoke 失败：{label} torch.equal=False"
            f"（forward 内含未固定 RNG / 无序算子）"
        )

    if not slot_paths:
        return
    cap1 = _hook_slot_outputs(model, slot_paths, batch, forward_fn, device)
    cap2 = _hook_slot_outputs(model, slot_paths, batch, forward_fn, device)
    for path in slot_paths:
        a, b = cap1[path], cap2[path]
        if not isinstance(a, torch.Tensor) or not isinstance(b, torch.Tensor):
            raise RuntimeError(
                f"per-slot identity allclose smoke 失败：slot {path!r} output 非 tensor"
                f"（{type(a).__name__}）——puzzle 契约 slot 须输出 tensor"
            )
        if not torch.allclose(a, b, atol=_ALLCLOSE_ATOL):
            max_diff = (a - b).abs().max().item()
            raise RuntimeError(
                f"per-slot identity allclose smoke 失败：slot {path!r} 两次 forward"
                f" max|Δ|={max_diff:.2e} > atol={_ALLCLOSE_ATOL:.0e}（father-loaded 模块"
                f" 在 zero intervention 下逐 slot 输出不可复现——检查 flat 是否漏 register_buffer"
                f" / 含未固定 RNG）"
            )


# ── smoke 3: eval-stability（root cause B：atol 读 adapters.EVAL_NOISE_ATOL）──

def eval_stability(
    model: nn.Module, adapters: Any, atol: float
) -> float:
    """``adapters.evaluate(model)`` 跑两次 → acc 一致（atol 读 EVAL_NOISE_ATOL）。"""
    acc1 = adapters.evaluate(model)
    acc2 = adapters.evaluate(model)
    for acc in (acc1, acc2):
        if isinstance(acc, bool) or not isinstance(acc, (int, float)):
            raise TypeError(
                f"adapters.evaluate 返回非数值：{type(acc).__name__}"
            )
    if abs(float(acc1) - float(acc2)) > atol:
        raise RuntimeError(
            f"eval-stability smoke 失败：两次 eval acc 不一致（{acc1} vs {acc2}，"
            f"Δ > {atol:.0e}）——evaluate 含未固定 RNG / 采样路径； adapters.EVAL_NOISE_ATOL"
            f" 需放大到能容下评估协议噪声"
        )
    return float(acc1)


# ── trace slot I/O shape ──────────────────────────────────────────────────────

def trace_slot_shapes(
    model: nn.Module, slot_paths: list[str], batch: Any,
    forward_fn: Any, device: torch.device,
) -> dict[str, tuple[int, int]]:
    """hook 每个 slot 抓 in/out 末维 → ``{path: (in_dim, out_dim)}``。"""
    model.eval().to(device)
    captured: dict[str, tuple[int, int]] = {}
    handles: list[Any] = []

    def make_hook(path: str):
        def hook(_mod: nn.Module, inputs: tuple, output: Any):
            if path in captured:
                return
            in_t = inputs[0] if isinstance(inputs, tuple) and inputs else inputs
            if isinstance(in_t, (list, tuple)):
                in_t = in_t[0]
            out_t = output[0] if isinstance(output, (tuple, list)) else output
            if not isinstance(in_t, torch.Tensor) or in_t.dim() < 1:
                raise RuntimeError(
                    f"trace slot {path!r} 的输入非 ≥1D tensor（{type(in_t).__name__}）——"
                    f"puzzle 契约 slot 输入须 tensor，无法 trace in_dim"
                )
            if not isinstance(out_t, torch.Tensor) or out_t.dim() < 1:
                raise RuntimeError(
                    f"trace slot {path!r} 的输出非 ≥1D tensor（{type(out_t).__name__}）——"
                    f"puzzle 契约 slot 须输出 tensor，无法 trace out_dim"
                )
            captured[path] = (int(in_t.shape[-1]), int(out_t.shape[-1]))
        return hook

    try:
        for path in slot_paths:
            try:
                mod = model.get_submodule(path)
            except AttributeError as e:
                raise AttributeError(
                    f"trace slot {path!r} 定位失败：{e}"
                ) from e
            handles.append(mod.register_forward_hook(make_hook(path)))
        with torch.no_grad():
            forward_fn(model, batch)
    finally:
        for h in handles:
            h.remove()
    missing = [p for p in slot_paths if p not in captured]
    if missing:
        raise RuntimeError(
            f"trace 未捕获 slot shape：{missing[:3]}（共 {len(missing)} 个）"
        )
    return captured


# ── CLI ────────────────────────────────────────────────────────────────────────

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Puzzle U6 measure_baseline：适配器架构 + 4 道 smoke + baseline 测量"
    )
    p.add_argument("--flat_path", required=True, help="flat model .py（架构源：build_model + DUMMY_INPUT）")
    p.add_argument("--build_fn", required=True, help="flat 内 build 函数名")
    p.add_argument("--build_cfg", default="", help="build_fn 的 JSON kwargs（空则零参）")
    p.add_argument(
        "--adapters", required=True,
        help="puzzle_adapters.py 路径（U6 §2.1：脚本唯一项目接口）",
    )
    p.add_argument(
        "--manifest", default="",
        help="manifest.yaml 路径（metadata 用；脚本不解析，agent 桥接）",
    )
    p.add_argument(
        "--eval_stability_atol", type=float, default=None,
        help="[override] 默认读 adapters.EVAL_NOISE_ATOL；显式传入则覆盖（agent 不应常规使用）",
    )
    p.add_argument("--search_space_path", required=True, help="LLM 产的 search_space.yaml 绝对路径")
    p.add_argument("--latency_unit", default="ms", choices=["ms", "us", "s"])
    p.add_argument("--latency_script_path", default="", help="外部 latency 脚本 path::func")
    p.add_argument(
        "--latency_reduction_target", type=float, default=0.5,
        help="时延降低目标比例（与 pz_select mip_select / gate_report 同源）。"
             "measure_baseline 测 block-zero floor latency 后算 max_achievable_reduction"
             " = 1 - floor/baseline；若 < target - 1e-6 → exit 3（结构性不可达，不进 BLD）",
    )
    p.add_argument("--output_dir", required=True, help="产物输出目录绝对路径")
    p.add_argument("--seed", type=int, default=0, help="复现性种子")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    torch.manual_seed(args.seed)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    flat_path = Path(args.flat_path).resolve()

    try:
        adapters = load_puzzle_adapters(args.adapters)
        eval_atol = (
            args.eval_stability_atol
            if args.eval_stability_atol is not None
            else float(adapters.EVAL_NOISE_ATOL)
        )

        # 1) 读 search_space.yaml（LLM 产物）
        slot_dicts, candidates = load_search_space_yaml(args.search_space_path)

        # E22：empty slots → terminate_unsupported（确定性 post-check）
        if not slot_dicts:
            result = {
                "output_dir": str(output_dir),
                "model_type": "No supported match",
                "model_type_supported": False,
                "flat_model_path": str(flat_path),
                "block_map_path": "",
                "search_space_path": str(Path(args.search_space_path).resolve()),
                "baseline_metrics_path": "",
                "baseline_acc": 0.0,
                "baseline_latency": 0.0,
                "latency_unit": args.latency_unit,
                "fidelity_passed": False,
                "smokes_passed": [],
                "error": "empty search_space.slots (terminate_unsupported)",
                "generated_artifacts": [str(flat_path)],
            }
            print(f"BASELINE_ACC: 0")
            print(f"BASELINE_LATENCY: 0")
            print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
            return 2

        # 2) 加载 flat model（架构骨架）+ 注入预训练权重（适配器）
        #    adapters.build_model 是项目接口；flat.build_model 是架构真相源——
        #    二者应产等价架构（adapter 内部典型地 from <flat> import build_model）。
        #    这里走 adapters.build_model()（SPEC §2.1 canonical），load_pretrained 注入权重。
        model = build_pretrained_model(adapters)
        if not isinstance(model, nn.Module):
            raise TypeError(
                f"adapters.build_model() 返回非 nn.Module：{type(model).__name__}"
            )

        # 3) smoke 1: ckpt 宽松加载（root cause C）
        load_result = load_father_ckptrck(model, adapters)
        father_state_path = output_dir / "father_state_dict.pt"
        torch.save(model.state_dict(), father_state_path)
        smokes_passed = ["ckpt-load"]

        # 4) 准备 native batch：取 calib_iter 首个 batch（U6：不再假设单 tensor）
        device = torch.device("cpu")
        model.eval().to(device)
        forward_fn = adapters.forward_model
        try:
            calib_iter = adapters.calib_iter(device=device)
            batch = next(iter(calib_iter))
        except StopIteration as e:
            raise RuntimeError(
                "adapters.calib_iter() 返回空迭代——无法取 baseline batch（检 manifest 数据入口）"
            ) from e

        # 5) smoke 2 + smoke 4（两次 forward 复用）
        slot_paths = [str(d["path"]) for d in slot_dicts]
        forward_determinism_and_identity_allclose(
            model, slot_paths, batch, forward_fn, device
        )
        smokes_passed.append("forward-determinism")
        smokes_passed.append("per-slot-identity-allclose")

        # 6) smoke 3: eval-stability（root cause B：atol 读 EVAL_NOISE_ATOL）
        baseline_acc = eval_stability(model, adapters, eval_atol)
        smokes_passed.append("eval-stability")

        # 7) trace slot I/O shape → 回填 slot_dicts
        shapes = trace_slot_shapes(model, slot_paths, batch, forward_fn, device)
        for d in slot_dicts:
            in_dim, out_dim = shapes[str(d["path"])]
            d["in_dim"] = in_dim
            d["out_dim"] = out_dim

        # block_map 早建（floor 测量需要 slots 的 parent_module_path）
        block_map = to_block_map(slot_dicts)

        # 8) latency（per-inference batch-1：与 per-block latency_table 同尺度，避免混 batch 缩放）
        latency_dummy = build_latency_dummy(adapters, device=device)
        baseline_latency = measure_whole_model_latency(
            model, forward_fn, latency_dummy, device, args.latency_script_path,
            convention=adapters.FORWARD_CALLING_CONVENTION,
        )

        # 9) block-zero floor latency + 可行性判定（早退：结构性不可达 → exit 3，不进 BLD）
        #    floor = 全 block 置零后的整模 latency；max_reduction = 1 - floor/baseline
        #    = block 替换能达到的物理上限。低于用户目标 → 此模型 block 占比过低，目标过高。
        floor_latency = measure_block_zero_floor_latency(
            adapters, block_map, device, args.latency_script_path
        )
        if baseline_latency <= 0:
            raise RuntimeError(
                f"baseline_latency={baseline_latency} 非正——无法算 max_achievable_reduction"
                f"（检 latency 测量返回值）"
            )
        max_reduction = 1.0 - floor_latency / baseline_latency
        # 争用噪声容错：floor 理论上 ≤ baseline（block 非负开销）；CPU 争用/min 残差噪声
        # 可能让 floor 微超 baseline → max_reduction 微负。clamp 到 0 避免负值进下游显示/MIP。
        # 不静默：在 infeasible reason 里如实标注「测量噪声」让 agent/用户感知。
        measurement_noisy = max_reduction < 0
        if measurement_noisy:
            max_reduction = 0.0
        latency_target_feasible = max_reduction >= (
            args.latency_reduction_target - _FEASIBILITY_TOL
        )

        # 10) 写产物
        block_map_path = output_dir / "block_map.json"
        block_map.to_json(block_map_path)

        search_space_out = output_dir / "search_space.yaml"
        save_search_space_yaml(search_space_out, slot_dicts, candidates)

        if latency_target_feasible:
            reason = ""
        elif measurement_noisy:
            # floor > baseline：测量噪声 / 争用残差导致 max_reduction clamp 到 0。区别于
            # 「block 占比过低」——此时 floor 数值本身不可信（理论 floor ≤ baseline），需重跑
            # 或检 latency_script_path。
            reason = (
                f"测量噪声疑似：latency_floor={floor_latency:.4f} > baseline_latency="
                f"{baseline_latency:.4f} {args.latency_unit}（理论上 floor ≤ baseline——CPU "
                f"争用 / min 残差 / 外部 latency 脚本不稳定均可能触发）；max_reduction 钳为 0 "
                f"< latency_reduction_target={args.latency_reduction_target:.4f}。建议重跑 "
                f"measure_baseline（争用减弱），或检查 latency_script_path 的稳定性"
            )
        else:
            # 通用表述（不假设 transformer）：「非 block 构件」泛指 embedding/投影/归一化/
            # residual/cache 等任何 block 之外的算子——对 CNN/RNN/MoE 均中性。
            reason = (
                f"block 替换最大 reduction={max_reduction:.4f}（floor={floor_latency:.4f} / "
                f"baseline={baseline_latency:.4f} {args.latency_unit}）< "
                f"latency_reduction_target={args.latency_reduction_target:.4f}——"
                f"该模型 block 占比过低（非 block 构件：embedding / 投影 / 归一化 / residual "
                f"等占 {floor_latency/baseline_latency:.2%}）。需降 latency_reduction_target，"
                f"或该模型 block 占比本身过低不适合 puzzle 流水线"
            )

        baseline_metrics = {
            "baseline_acc": baseline_acc,
            "baseline_latency": baseline_latency,
            "latency_floor": floor_latency,
            "max_achievable_reduction": max_reduction,
            "latency_target_feasible": latency_target_feasible,
            "latency_reduction_target": args.latency_reduction_target,
            "latency_unit": args.latency_unit,
            "metric_direction": adapters.METRIC_DIRECTION,
            "metric_atol": eval_atol,
            "ckpt_load": {
                "missing_count": len(load_result.missing),
                "unexpected_count": len(load_result.unexpected),
                "from_scratch": bool(load_result.from_scratch),
            },
            "seed": args.seed,
            "smokes_passed": smokes_passed,
        }
        if reason:
            baseline_metrics["latency_infeasible_reason"] = reason
        baseline_metrics_path = output_dir / "baseline_metrics.json"
        with open(baseline_metrics_path, "w", encoding="utf-8") as f:
            json.dump(baseline_metrics, f, ensure_ascii=False, indent=2)

        generated = [
            str(flat_path),
            str(father_state_path),
            str(block_map_path),
            str(search_space_out),
            str(baseline_metrics_path),
        ]
        result = {
            "output_dir": str(output_dir),
            "model_type": _infer_model_type(block_map),
            "model_type_supported": True,
            "latency_target_feasible": latency_target_feasible,
            "max_achievable_reduction": max_reduction,
            "latency_floor": floor_latency,
            "flat_model_path": str(flat_path),
            "block_map_path": str(block_map_path),
            "search_space_path": str(search_space_out),
            "baseline_metrics_path": str(baseline_metrics_path),
            "baseline_acc": baseline_acc,
            "baseline_latency": baseline_latency,
            "latency_unit": args.latency_unit,
            "fidelity_passed": True,
            "smokes_passed": smokes_passed,
            "ckpt_from_scratch": bool(load_result.from_scratch),
            "error": reason,
            "generated_artifacts": generated,
        }
        print(f"BLOCK_MAP: {block_map_path}")
        print(f"SEARCH_SPACE: {search_space_out}")
        print(f"BASELINE_ACC: {baseline_acc}")
        print(f"BASELINE_LATENCY: {baseline_latency}")
        print(f"FLOOR_LATENCY: {floor_latency}")
        print(f"MAX_REDUCTION: {max_reduction}")
        print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")

        if not latency_target_feasible:
            # 结构性不可达：模型可优化（smoke 全绿）、只是目标过高——区别于 exit 2 unsupported。
            print(
                f"[measure_baseline] LATENCY-INFEASIBLE: {reason}",
                file=sys.stderr,
            )
            return _EXIT_LATENCY_INFEASIBLE
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERROR: measure_baseline 失败 — {type(e).__name__}: {e}\n{tb}", file=sys.stderr)
        return 2


def _infer_model_type(block_map) -> str:
    """粗标签（agent 可改写）。"""
    n_att = sum(1 for s in block_map.slots if s.kind == "attention")
    n_ffn = sum(1 for s in block_map.slots if s.kind == "ffn")
    if n_att >= 2 and n_ffn >= 2:
        return "isotropic_transformer"
    if n_att >= 1:
        return "hierarchical_transformer"
    return "unknown_transformer"


if __name__ == "__main__":
    sys.exit(main())

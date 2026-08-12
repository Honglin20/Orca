"""mip_select.py —— Puzzle pulp grouped-knapsack MIP 选架构（U6 适配器）。

形式化（SPEC §4 / P2.6）：
    max  Σ score[l, v] · x[l, v]
    s.t. Σ latency[l, v] · x[l, v] ≤ target_latency
         Σ_v x[l, v] = 1  ∀ layer-group l   （每 (layer,kind) 组恰选一个）
         x[l, v] ∈ {0, 1}

分组键：``(layer_idx, kind)``（kind 替代 v1 slot_type，E3）。每组恰选一 variant。

U6 改造（root cause G + LAT AC 参数化）：
  - 删 ``target-too-aggressive`` 早警硬 terminate：MIP 只报 feasibility，LAT AC 由 gate 判。
  - ``--target-latency`` 改可选（空 → ``baseline × (1 - latency_reduction_target)``
    软目标；默认 reduction=0.5 → baseline×0.5）。
  - ``--latency_reduction_target`` 与 gate 同源：specifies 要求的时延降幅比例（0.5 = 降一半）。
  - select_reason enum 收缩到 ``mip-optimal`` / ``infeasible`` / ``none``。

stdout 单行 JSON：``{selected_arch, total_score, selected_latency, feasible, select_reason}``。
- selected_arch: ``{layer_idx: {kind: variant_name}}``

scores/latency 缺 → exit 2。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"jsonl 不存在：{path}")
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for ln, raw in enumerate(f, start=1):
            s = raw.strip()
            if not s:
                continue
            try:
                rows.append(json.loads(s))
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{path} 第 {ln} 行非合法 JSON：{e}\n原文：{s[:200]!r}"
                ) from e
    if not rows:
        raise ValueError(f"{path} 空文件（无行）")
    return rows


def _extract_latency(row: dict[str, Any]) -> float:
    """从 latency_table 行提取 latency 值（兼容 latency_ms / latency_us / latency_s）。"""
    for k in ("latency_ms", "latency_us", "latency_s", "latency"):
        if k in row:
            return float(row[k])
    raise KeyError(
        f"latency 行缺 latency_* 字段：{row!r}"
    )


def _solve_mip(
    scores_rows: list[dict[str, Any]],
    latency_rows: list[dict[str, Any]],
    target_latency: float,
    baseline_whole_latency: float | None = None,
    measured_floor: float | None = None,
) -> dict[str, Any]:
    """pulp grouped-knapsack 求解(Design A: standalone 单块 + floor overhead)。

    整模 latency 模型：
        selected_whole ≈ floor + Σ chosen_block_latency
    约束 selected_whole ≤ target。baseline_whole_latency 必须给（从 baseline_metrics.json）；
    缺则回退纯 block-sum。
    """
    import pulp

    score_map: dict[tuple[int, str, str], float] = {}
    valid_map: dict[tuple[int, str, str], bool] = {}
    for r in scores_rows:
        key = (int(r["layer"]), str(r["kind"]), str(r["variant"]))
        score_map[key] = float(r["score"])
        valid_map[key] = bool(r.get("valid", True))
    latency_map: dict[tuple[int, str, str], float] = {}
    for r in latency_rows:
        key = (int(r["layer"]), str(r["kind"]), str(r["variant"]))
        latency_map[key] = _extract_latency(r)

    groups: dict[tuple[int, str], list[tuple[int, str, str]]] = defaultdict(list)
    for key in score_map:
        if not valid_map.get(key, True):
            continue
        if key not in latency_map:
            raise ValueError(
                f"(layer={key[0]}, kind={key[1]}, variant={key[2]}) 在 latency 表缺"
            )
        groups[(key[0], key[1])].append(key)

    if not groups:
        return {
            "selected_arch": {},
            "total_score": 0.0,
            "selected_latency": 0.0,
            "feasible": False,
            "select_reason": "none",
        }

    # floor = 非 block 固定开销。优先实测；缺则用 baseline − Σ identity 估算；都缺 → raise。
    if measured_floor is not None:
        floor = measured_floor
    elif baseline_whole_latency is not None:
        identity_sum = 0.0
        for gkey, members in groups.items():
            ident = [m for m in members if m[2] == "identity"]
            if not ident:
                raise ValueError(
                    f"组 (layer={gkey[0]}, kind={gkey[1]}) 无 identity variant——"
                    "latency 模型需 identity 作 baseline 参照"
                )
            identity_sum += latency_map[ident[0]]
        floor = baseline_whole_latency - identity_sum
        if floor < 0:
            print(
                f"WARN: floor 估算为负({floor:.4f}, identity 单块 standalone 偏大), clamp 到 0",
                file=sys.stderr,
            )
            floor = 0.0
    else:
        raise ValueError(
            "mip_select 缺 latency_floor.json 与 baseline_metrics——无法建立整模 latency "
            "模型。latency_table 必须产 latency_floor.json, 或传 --baseline-metrics。"
        )
    effective_target = target_latency - floor

    prob = pulp.LpProblem("puzzle_select", pulp.LpMaximize)
    x: dict[tuple[int, str, str], pulp.LpVariable] = {}
    for gkey, members in groups.items():
        for m in members:
            x[m] = pulp.LpVariable(
                f"x_{m[0]}_{m[1]}_{m[2]}", cat=pulp.LpBinary
            )

    prob += pulp.lpSum(score_map[m] * x[m] for m in x)

    for gkey, members in groups.items():
        prob += pulp.lpSum(x[m] for m in members) == 1, f"one_per_L{gkey[0]}_{gkey[1]}"

    prob += (
        pulp.lpSum(latency_map[m] * x[m] for m in x) <= effective_target,
        "latency_budget",
    )

    solver = pulp.PULP_CBC_CMD(msg=False)
    status = prob.solve(solver)

    if pulp.LpStatus[status] != "Optimal":
        # 加性 latency 模型判 infeasible（target 过紧）。不返回空 arch 让 E2E 死在 select——
        # 改返 **best-effort arch**让 pz_retrain GKD + pz_report gate 实测裁决。
        # 🔴 保逻辑铁律：best-effort **排除 no_op**（no_op=跳过整块=删计算=改深度，破坏原模型
        # 逻辑，非「同功能更快」）。只在仍做计算的功能候选（mixer 变体 / 剪枝 FFN 等）里选 latency
        # 最小——这样每 block 仍履行职能、acc 应 ≈ baseline（真保逻辑），latency 降到功能替换可达。
        # 若某 group 除 no_op 外无功能候选，退回 identity（保留原块，不动逻辑）。
        selected_arch: dict[str, dict[str, str]] = defaultdict(dict)
        total_score = 0.0
        chosen_block_latency = 0.0
        for gkey, members in groups.items():
            layer_idx, kind = gkey
            functional = [m for m in members if m[2] != "no_op"]
            if not functional:
                # 无功能候选 → identity（原块，保逻辑）
                ident = [m for m in members if m[2] == "identity"]
                m_pick = ident[0] if ident else min(members, key=lambda m: latency_map[m])
            else:
                m_pick = min(functional, key=lambda m: latency_map[m])
            selected_arch[str(layer_idx)][kind] = m_pick[2]
            total_score += score_map[m_pick]
            chosen_block_latency += latency_map[m_pick]
        selected_latency = floor + chosen_block_latency
        return {
            "selected_arch": dict(selected_arch),
            "total_score": float(total_score),
            "selected_latency": float(selected_latency),
            "feasible": bool(selected_latency <= target_latency + 1e-9),
            "select_reason": "best-effort",
        }

    selected_arch: dict[str, dict[str, str]] = defaultdict(dict)
    total_score = 0.0
    chosen_block_latency = 0.0
    for m, var in x.items():
        if var.value() is not None and var.value() > 0.5:
            layer_idx, kind, variant = m
            selected_arch[str(layer_idx)][kind] = variant
            total_score += score_map[m]
            chosen_block_latency += latency_map[m]

    selected_latency = floor + chosen_block_latency

    return {
        "selected_arch": dict(selected_arch),
        "total_score": float(total_score),
        "selected_latency": float(selected_latency),
        "feasible": bool(selected_latency <= target_latency + 1e-9),
        "select_reason": "mip-optimal",
    }


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Puzzle U6 MIP grouped-knapsack")
    parser.add_argument("--scores", required=True, help="scores.jsonl 路径")
    parser.add_argument("--latency-table", required=True, help="latency_table.jsonl 路径")
    parser.add_argument(
        "--target-latency", type=float, default=None,
        help="显式 latency 预算；空（默认）→ 由 baseline × (1 - latency_reduction_target) 推导",
    )
    parser.add_argument(
        "--latency_reduction_target", type=float, default=0.5,
        help="LAT AC 参数化（与 gate_report 同源）：要求时延降幅比例（0.5 = 降一半）。"
        "--target-latency 缺省时按 baseline × (1 - reduction) 取软目标",
    )
    parser.add_argument("--baseline-metrics", default="",
                        help="baseline_metrics.json 路径（提供则用整模 latency 模型）")
    parser.add_argument("--latency-unit", default="ms", choices=["ms", "us", "s"],
                        help="latency 单位（透传到输出字段，不换算数值）")
    parser.add_argument("--output_dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        scores_rows = _read_jsonl(Path(args.scores))
        latency_rows = _read_jsonl(Path(args.latency_table))

        baseline_whole = None
        if args.baseline_metrics:
            with open(args.baseline_metrics, encoding="utf-8") as f:
                baseline_whole = float(json.load(f)["baseline_latency"])

        # U6 root cause G：删 target-too-aggressive 早警；target 缺省 → reduction 推导软目标。
        if args.target_latency is not None:
            target_latency = float(args.target_latency)
        elif baseline_whole is not None:
            reduction = max(0.0, min(1.0, float(args.latency_reduction_target)))
            target_latency = baseline_whole * (1.0 - reduction)
            print(
                f"[mip_select] target_latency 缺省 → baseline({baseline_whole:.4f}) "
                f"× (1 - reduction({reduction})) = {target_latency:.4f}",
                file=sys.stderr,
            )
        else:
            raise ValueError(
                "mip_select 需 --target-latency 或 --baseline-metrics（给 baseline 才能按 "
                "latency_reduction_target 推导软目标）"
            )

        # 实测 floor（优先）：latency_table 产出 latency_floor.json
        measured_floor = None
        floor_path = Path(args.latency_table).resolve().parent / "latency_floor.json"
        if floor_path.is_file():
            with open(floor_path, encoding="utf-8") as f:
                fd = json.load(f)
            floor_unit = fd.get("unit", args.latency_unit)
            if floor_unit != args.latency_unit:
                raise ValueError(
                    f"latency_floor.json unit({floor_unit}) != --latency-unit({args.latency_unit}),"
                    "数量级可能差 1000×——同源 latency_table 必须用同一 latency_unit"
                )
            measured_floor = float(fd["floor_latency"])

        result = _solve_mip(
            scores_rows, latency_rows, target_latency, baseline_whole, measured_floor
        )
        result["latency_unit"] = args.latency_unit
        result["target_latency"] = target_latency
        result["latency_reduction_target"] = float(args.latency_reduction_target)

        arch_path = output_dir / "selected_arch.json"
        with open(arch_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERROR: mip_select 失败 — {type(e).__name__}: {e}\n{tb}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

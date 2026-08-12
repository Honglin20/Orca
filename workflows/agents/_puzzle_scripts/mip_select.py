"""mip_select.py —— Puzzle P2.6：pulp grouped-knapsack MIP 选架构。

形式化（SPEC §4 / P2.6）：
    max  Σ score[l, v] · x[l, v]
    s.t. Σ latency[l, v] · x[l, v] ≤ target_latency
         Σ_v x[l, v] = 1  ∀ layer-group l   （每 (layer,kind) 组恰选一个）
         x[l, v] ∈ {0, 1}

分组键：``(layer_idx, kind)``（kind 替代 v1 slot_type，E3）。每组恰选一 variant。

stdout 单行 JSON：``{selected_arch, total_score, selected_latency, feasible, select_reason}``。
- selected_arch: ``{layer_idx: {kind: variant_name}}``
- select_reason: ``mip-optimal`` / ``infeasible`` / ``none`` / ``target-too-aggressive``

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
    """pulp grouped-knapsack 求解(Design A:standalone 单块 + floor overhead)。

    latency_table 报的是**单块 standalone** latency(可靠且加性)。整模 latency 模型:
        selected_whole ≈ floor + Σ chosen_block_latency
        floor = baseline_whole − Σ identity_block_latency(非 block 固定开销:
        patch_embed/head/norm/residual 等;实测 ≈ 全 block drop 后的整模 latency)。
    约束 selected_whole ≤ target(=baseline_whole/2 对接 LAT AC)。
    baseline_whole_latency 必须给(从 baseline_metrics.json);缺则回退纯 block-sum。
    """
    import pulp

    # 组装 (layer, kind, variant) -> score / latency
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

    # 分组（(layer, kind) 为组键——kind 替代 v1 slot_type，E3）
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

    # floor = 非 block 固定开销。优先实测(全 block→no_op 整模 latency, latency_floor.json);
    # 缺则用 baseline_whole − Σ identity 单块估算(因 standalone 欠计上下文会偏高,偏保守)。
    # 两者都缺 → raise(target_latency 是整模尺度,floor=0 会让约束 silent 退化为纯 block-sum,
    # 几乎恒可行,破坏 Design A 整模尺度模型——Rule 12 fail loud)。
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
                f"WARN: floor 估算为负({floor:.4f},identity 单块 standalone 偏大),clamp 到 0",
                file=sys.stderr,
            )
            floor = 0.0
    else:
        raise ValueError(
            "mip_select 缺 latency_floor.json 与 baseline_metrics——无法建立整模 latency "
            "模型。latency_table 必须产 latency_floor.json,或传 --baseline-metrics。"
        )
    # Σ chosen_block ≤ target − floor
    effective_target = target_latency - floor

    prob = pulp.LpProblem("puzzle_select", pulp.LpMaximize)
    x: dict[tuple[int, str, str], pulp.LpVariable] = {}
    for gkey, members in groups.items():
        for m in members:
            x[m] = pulp.LpVariable(
                f"x_{m[0]}_{m[1]}_{m[2]}", cat=pulp.LpBinary
            )

    # 目标：max Σ score · x
    prob += pulp.lpSum(score_map[m] * x[m] for m in x)

    # 约束 1：每组恰选一
    for gkey, members in groups.items():
        prob += pulp.lpSum(x[m] for m in members) == 1, f"one_per_L{gkey[0]}_{gkey[1]}"

    # 约束 2：floor + Σ chosen_block ≤ target(整模尺度)
    prob += (
        pulp.lpSum(latency_map[m] * x[m] for m in x) <= effective_target,
        "latency_budget",
    )

    solver = pulp.PULP_CBC_CMD(msg=False)
    status = prob.solve(solver)

    if pulp.LpStatus[status] != "Optimal":
        return {
            "selected_arch": {},
            "total_score": 0.0,
            "selected_latency": 0.0,
            "feasible": False,
            "select_reason": "infeasible",
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

    selected_latency = floor + chosen_block_latency  # 整模估计

    return {
        "selected_arch": dict(selected_arch),
        "total_score": float(total_score),
        "selected_latency": float(selected_latency),
        "feasible": bool(selected_latency <= target_latency + 1e-9),
        "select_reason": "mip-optimal",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puzzle P2.6 MIP grouped-knapsack")
    parser.add_argument("--scores", required=True, help="scores.jsonl 路径")
    parser.add_argument("--latency-table", required=True, help="latency_table.jsonl 路径")
    parser.add_argument("--target-latency", type=float, required=True)
    parser.add_argument("--baseline-metrics", default="",
                        help="baseline_metrics.json 路径(提供则用整模 latency 模型: "
                        "overhead + Σ chosen_block,与 gate 同尺度;空则回退纯 block-sum)")
    parser.add_argument("--latency-unit", default="ms", choices=["ms", "us", "s"],
                        help="latency 单位（透传到输出字段，不换算数值）")
    parser.add_argument("--output_dir", required=True)
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        scores_rows = _read_jsonl(Path(args.scores))
        latency_rows = _read_jsonl(Path(args.latency_table))

        baseline_whole = None
        if args.baseline_metrics:
            with open(args.baseline_metrics, encoding="utf-8") as f:
                baseline_whole = float(json.load(f)["baseline_latency"])

        # E12 LAT 早警：target_latency > baseline_latency/2 → LAT AC 结构性不可达。
        # 放在 mip_select（而非 gate）的原因：(1) mip_select 已有 target+baseline 同框；
        # (2) 早 fail 省 build_selected + gkd_retrain（最长的 GKD 分钟~小时级）；(3) 具体
        # select_reason=target-too-aggressive 让下游 assessment 标注根因；路由守卫仍走
        # terminate_select_failed 兜底（selected_arch 空 + feasible=false 双条件不成立）。
        # 缺 baseline_metrics 时跳过（不强制—— degraded 模式留给纯 block-sum 回退）。
        if baseline_whole is not None:
            lat_early_threshold = baseline_whole / 2.0
            if args.target_latency > lat_early_threshold:
                early = {
                    "selected_arch": {},
                    "total_score": 0.0,
                    "selected_latency": 0.0,
                    "feasible": False,
                    "select_reason": "target-too-aggressive",
                    "latency_unit": args.latency_unit,
                    "infeasible_reason": (
                        f"LAT 早警：target_latency={args.target_latency:.4f} "
                        f"> baseline_latency/2={lat_early_threshold:.4f}（baseline="
                        f"{baseline_whole:.4f}）——LAT AC 要求 latency_opt ≤ baseline/2，"
                        f"目标预算已超 AC 上限，结构性不可达；禁浪费 build_selected/retrain 算力。"
                        f"调高 target_latency 或换更小模型"
                    ),
                }
                arch_path = output_dir / "selected_arch.json"
                with open(arch_path, "w", encoding="utf-8") as f:
                    json.dump(early, f, ensure_ascii=False, indent=2)
                print(json.dumps(early, ensure_ascii=False))
                return 0

        # 实测 floor(优先):latency_table 产出 latency_floor.json(与 latency_table.jsonl 同目录)
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
            scores_rows, latency_rows, args.target_latency, baseline_whole, measured_floor
        )
        result["latency_unit"] = args.latency_unit

        # 落 selected_arch.json（持久化，便于 build_selected 读）
        arch_path = output_dir / "selected_arch.json"
        with open(arch_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        # stdout：单行 JSON（pz_select 是 zero-LLM 确定性节点，stdout 直接转发为节点 output）
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERROR: mip_select 失败 — {type(e).__name__}: {e}\n{tb}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

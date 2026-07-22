#!/usr/bin/env python3
"""tail_metrics.py —— NAS 训练/搜索指标 sidecar（被 nas-train-runner agent 周期调用）。

设计依据（viz 方案共识，B-1 + F1 + D1 + P2）：
  - 不改 nas-agent 仓库：本脚本只读已落盘的 jsonl（train_metrics.jsonl / search.jsonl），
    调 orca.chart.render_chart 重推（同 label+title 替换 = 刷新语义）。
  - train 模式：读 <output_dir>/runs/train/train_metrics.jsonl → C3a loss / C3b val。
  - search 模式：读 <output_dir>/runs/search/search.jsonl → C4a-{obj} 收敛 / C4b 种群&缓存 / C5-live 帕累托。
  - 目标分类（D1 通用化）：objs 值恒 ≤0 → 负向化的质量指标（显示 -v，越大越好）；
    恒 ≥0 → 成本（显示 v，越小越好）。靠符号判定，不写死 acc/latency。
  - F1：首次读到非空 jsonl 时校验 schema；违规推一张 ERROR 表图（fail-soft：推图异常不阻断主流程，
    但 schema 错必须可见，杜绝静默空图）。
  - P2：C5-live 用 chart_type=pareto（小数据，前端算前沿）；finalize 由 push_pareto_final.py 自算。

退出码：0 = 跑完（即使部分图推失败也算，单次 render_chart 失败仅 stderr loud）；非 0 = 致命（如 output_dir 不存在）。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

# render_chart 仅在 Orca 编排的 script 子进程内可用（env 含 ORCA_*）。
# sidecar 由 nas-train-runner agent 的 Bash spawn，ORCA_* 沿 env 链继承到此。
try:
    from orca.chart import render_chart  # type: ignore
except Exception as e:  # pragma: no cover
    sys.stderr.write(f"[tail_metrics] 无法 import orca.chart.render_chart：{e}\n")
    sys.stderr.write(
        "[tail_metrics] 必须在 Orca run 上下文（env 含 ORCA_*）内由 agent spawn 运行。\n"
    )
    sys.exit(2)


# ── jsonl 读取 ────────────────────────────────────────────────────────────────


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                # 跳过半写的尾巴行（tail 时文件正在被写）；下次重读会拿到完整行。
                continue
    return out


def _push_error(label: str, title: str, expected: list[str], actual: list[str], sample: str) -> None:
    """F1：schema 不符时推一张 ERROR 表图（可见，不静默）。"""
    rows = [
        {"key": "expected_columns", "value": ", ".join(expected)},
        {"key": "actual_columns", "value": ", ".join(actual) if actual else "(空)"},
        {"key": "sample_line", "value": sample[:200]},
        {"key": "hint", "value": "生成的脚本未按约定写结构化指标；请检查 SKILL 检查清单 [MAJOR] 项。"},
    ]
    render_chart(
        chart_type="table",
        data=rows,
        label=label,
        title=f"⚠ {title} schema error",
        columns=["key", "value"],
        caption=(
            "训练指标 schema 校验失败（F1 可见诊断，不静默）。"
            "expected/actual 列对照见上；sample 为日志首行截断。"
        ),
    )


# ── train 模式 ────────────────────────────────────────────────────────────────

_TRAIN_REQUIRED = ["global_step", "phase", "loss"]


def _mode_train(output_dir: Path) -> None:
    path = output_dir / "runs" / "train" / "train_metrics.jsonl"
    recs = _read_jsonl(path)
    if not recs:
        return  # 文件还没出现 / 还空 → 静默跳过（下次轮询再读）

    cols = set().union(*(r.keys() for r in recs))
    sample = json.dumps(recs[0], ensure_ascii=False)
    if not all(c in cols for c in _TRAIN_REQUIRED):
        _push_error("nas/training", "train_metrics.jsonl", _TRAIN_REQUIRED, sorted(cols), sample)
        return

    # C3a：训练 loss（hue=phase，train/val 同图便于对比 loss 走势）
    loss_points: list[dict[str, Any]] = []
    for r in recs:
        try:
            loss_points.append(
                {"global_step": int(r["global_step"]), "loss": float(r["loss"]), "phase": str(r.get("phase", "train"))}
            )
        except (TypeError, ValueError):
            continue
    if loss_points:
        loss_points.sort(key=lambda d: d["global_step"])
        render_chart(
            chart_type="line",
            data=loss_points,
            label="nas/training",
            title="Training Loss",
            x="global_step",
            y="loss",
            hue="phase",
            x_label="全局训练步（global_step）",
            y_label="loss（越低越好）",
            caption="每 log_interval 步采样的训练 loss；hue=phase 区分 train/val。",
        )

    # C3b：验证指标（val 子集；acc/metric 字段名宽松：取 acc / val_acc / metric）
    val_recs = [r for r in recs if str(r.get("phase", "")).lower().startswith("val")]
    metric_key = next(
        (k for k in ("val_acc", "acc", "metric", "val_metric") if any(k in r for r in val_recs)),
        None,
    )
    if val_recs and metric_key:
        val_points: list[dict[str, Any]] = []
        for r in val_recs:
            try:
                val_points.append({"global_step": int(r["global_step"]), "metric": float(r[metric_key])})
            except (TypeError, ValueError, KeyError):
                continue
        if val_points:
            val_points.sort(key=lambda d: d["global_step"])
            render_chart(
                chart_type="line",
                data=val_points,
                label="nas/training",
                title="Validation Metric",
                x="global_step",
                y="metric",
                x_label="全局训练步（global_step）",
                # y_label/caption 与 01_training.md checklist C3b byte 对齐：inline 推图
                # （train_supernet.py 按 checklist 生成）与 tail 共享 label+title，dedup 替换
                # 下 last-writer-wins，y_label/caption 不一致会在两次 tail 轮询间闪烁。
                # 字段名（val_acc/acc/...）仍由 metric_key 决定数据读取，不进显示串。
                y_label="metric（验证集指标）",
                caption="验证集指标；每 eval 周期一个点。",
            )


# ── search 模式 ───────────────────────────────────────────────────────────────

_SEARCH_REQUIRED = ["generation", "objs"]


def _classify_obj(values: list[float]) -> str:
    """按符号判定目标性质：质量（被负向化，显示 -v，越大越好）vs 成本（显示 v，越小越好）。"""
    vals = [v for v in values if isinstance(v, (int, float)) and not math.isnan(v)]
    if not vals:
        return "cost"
    return "quality" if sum(1 for v in vals if v <= 0) >= len(vals) / 2 else "cost"


def _mode_search(output_dir: Path) -> None:
    path = output_dir / "runs" / "search" / "search.jsonl"
    recs = _read_jsonl(path)
    if not recs:
        return

    cols = set().union(*(r.keys() for r in recs))
    sample = json.dumps(recs[0], ensure_ascii=False)
    if not all(c in cols for c in _SEARCH_REQUIRED):
        _push_error("nas/search", "search.jsonl", _SEARCH_REQUIRED, sorted(cols), sample)
        return

    # 收集每个 obj 的全部值，分类
    obj_keys: list[str] = []
    for r in recs:
        o = r.get("objs") or {}
        for k in o:
            if k not in obj_keys:
                obj_keys.append(k)
    obj_kind = {k: _classify_obj([float((r.get("objs") or {}).get(k, float("nan"))) for r in recs]) for k in obj_keys}

    # 按代分组
    gens: dict[int, list[dict[str, Any]]] = {}
    for r in recs:
        try:
            g = int(r["generation"])
        except (TypeError, ValueError, KeyError):
            continue
        gens.setdefault(g, []).append(r)
    gen_sorted = sorted(gens)

    # C4a：每个目标一张收敛图（best/mean）。质量目标显示 -v（正），成本目标显示 v。
    for k in obj_keys:
        kind = obj_kind[k]
        rows: list[dict[str, Any]] = []
        for g in gen_sorted:
            vals = []
            for r in gens[g]:
                v = (r.get("objs") or {}).get(k)
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                disp = -v if kind == "quality" else v
                vals.append(disp)
            if not vals:
                continue
            best = max(vals) if kind == "quality" else min(vals)
            mean = sum(vals) / len(vals)
            rows.append({"generation": g, "best": best, "mean": mean})
        if rows:
            unit = "" if k != "latency" and "lat" not in k.lower() else " (ms)"
            # best/mean 双线（长表，hue=stat）。同 label+title → 刷新语义。
            rows_long = []
            for row in rows:
                rows_long.append({"generation": row["generation"], "value": row["best"], "stat": "best"})
                rows_long.append({"generation": row["generation"], "value": row["mean"], "stat": "mean"})
            render_chart(
                chart_type="line",
                data=rows_long,
                label="nas/search",
                title=f"Search Convergence — {k}{unit}",
                x="generation",
                y="value",
                hue="stat",
                x_label="进化代数（generation）",
                y_label=f"{k}（{'显示 -原值，越大越好' if kind == 'quality' else '越小越好'}）",
                caption=(
                    f"每代 best/mean。"
                    f"{'质量目标已取负显示（-v），故全轴越大越好；' if kind == 'quality' else ''}"
                    "best=该代最优，mean=该代均值。"
                ),
            )

    # C4b：每代种群/缓存/帕累托计数
    pop_rows: list[dict[str, Any]] = []
    for g in gen_sorted:
        pop = gens[g]
        cached = sum(1 for r in pop if r.get("cached"))
        pareto = sum(1 for r in pop if r.get("pareto"))
        evaluated = len(pop) - cached
        pop_rows.append({"generation": g, "count": evaluated, "kind": "evaluated"})
        pop_rows.append({"generation": g, "count": cached, "kind": "cached"})
        pop_rows.append({"generation": g, "count": pareto, "kind": "pareto"})
    if pop_rows:
        render_chart(
            chart_type="bar",
            data=pop_rows,
            label="nas/search",
            title="Population & Cache per Gen",
            x="generation",
            y="count",
            hue="kind",
            x_label="代数",
            y_label="个体数",
            caption="每代 evaluated（实算）/cached（命中缓存免算）/pareto（入当前前沿）三者计数。",
        )

    # C5-live：帕累托前沿散点（取当前 pareto=true 子集；finalize 由 push_pareto_final 自算全局）
    x_obj, y_obj = _pick_pareto_axes(obj_keys, obj_kind)
    if x_obj and y_obj:
        pts: list[dict[str, float]] = []
        for r in recs:
            if not r.get("pareto"):
                continue
            o = r.get("objs") or {}
            try:
                xv = float(o[x_obj])
                yv = float(o[y_obj])
            except (TypeError, ValueError, KeyError):
                continue
            x_disp = xv if obj_kind[x_obj] == "cost" else -xv
            y_disp = yv if obj_kind[y_obj] == "cost" else -yv
            pts.append({x_obj: x_disp, y_obj: y_disp})
        if pts:
            render_chart(
                chart_type="pareto",
                data=pts,
                label="nas/search",
                title="Pareto Front (live)",
                x=x_obj,
                y=y_obj,
                pareto_x_direction="min",
                pareto_y_direction="max",
                x_label=f"{x_obj}（越小越好{'+，质量类已取负显示' if obj_kind[x_obj] == 'quality' else ''}）",
                y_label=f"{y_obj}（越大越好{'+，质量类已取负显示' if obj_kind[y_obj] == 'quality' else ''}）",
                caption=(
                    "当前 per-generation pareto 子集；finalize 全局前沿见 Pareto Front (final)。"
                    f"x={x_obj}（{'成本类，越小越好' if obj_kind[x_obj] == 'cost' else '质量类，取负后越大越好'}），"
                    f"y={y_obj}（{'成本类，越小越好' if obj_kind[y_obj] == 'cost' else '质量类，取负后越大越好'}）。"
                ),
            )


def _pick_pareto_axes(obj_keys: list[str], obj_kind: dict[str, str]) -> tuple[str | None, str | None]:
    """选帕累托两轴：x=成本（优先 latency-like），y=质量。不足两个目标返回 (None,None)（不推 C5）。"""
    cost = [k for k in obj_keys if obj_kind[k] == "cost"]
    quality = [k for k in obj_keys if obj_kind[k] == "quality"]
    x = next((k for k in cost if "lat" in k.lower()), cost[0] if cost else None)
    y = quality[0] if quality else None
    if x and y and x != y:
        return x, y
    # 退化：任意两个不同目标
    if len(obj_keys) >= 2:
        return obj_keys[0], obj_keys[1]
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["train", "search"], required=True)
    ap.add_argument("--output_dir", required=True)
    args = ap.parse_args()
    output_dir = Path(args.output_dir)
    if not output_dir.is_dir():
        sys.stderr.write(f"[tail_metrics] output_dir 不存在：{output_dir}\n")
        return 1
    try:
        if args.mode == "train":
            _mode_train(output_dir)
        else:
            _mode_search(output_dir)
    except Exception as e:  # fail-soft：sidecar 异常不阻断训练/搜索主流程
        sys.stderr.write(f"[tail_metrics] {args.mode} 模式异常（已吞，不阻断主流程）：{type(e).__name__}: {e}\n")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

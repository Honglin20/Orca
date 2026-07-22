#!/usr/bin/env python3
"""push_pareto_final.py —— C5 终态帕累托前沿图（viz_finalize 调用）。

P2 共识：finalize **sidecar 自算全局非支配前沿**（读全部 evaluated 记录，非 per-gen pareto 标志——
后者是 per-population，gen1 点会被 gen10 支配），规避前端 _aggregate_by_x 在 >2000 点时
「按 x 求和 y」破坏前沿的陷阱。确定性优先于前端/模型判断（rule 5）。

产出一张 chart_type=pareto（plan §4.3 方案 A，2026-07-22 由 scatter 升级）：全部前沿点 +
降采样 dominated 背景云，前端据 pareto_x/y_direction 自绘前沿连线。selected 高亮牺牲
（已在 Selection Funnel 体现）。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

try:
    from orca.chart import render_chart  # type: ignore
except Exception as e:  # pragma: no cover
    sys.stderr.write(f"[push_pareto_final] 无法 import orca.chart：{e}\n")
    sys.exit(2)


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
                continue
    return out


def _is_dominated(i: int, pts: list[tuple[float, float]]) -> bool:
    """x 越小越好、y 越大越好下的支配判定。"""
    xi, yi = pts[i]
    for j, (xj, yj) in enumerate(pts):
        if j == i:
            continue
        if xj <= xi and yj >= yi and (xj < xi or yj > yi):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output_dir", required=True)
    ap.add_argument(
        "--selection_summary",
        default="",
        help="selection_summary.json 路径（缺省推断 <output_dir>/runs/retrain/selected/selection_summary.json）",
    )
    args = ap.parse_args()
    output_dir = Path(args.output_dir)

    recs = _read_jsonl(output_dir / "runs" / "search" / "search.jsonl")
    if not recs:
        sys.stderr.write("[push_pareto_final] search.jsonl 为空或不存在，跳过 C5\n")
        return 0

    obj_keys: list[str] = []
    for r in recs:
        for k in (r.get("objs") or {}):
            if k not in obj_keys:
                obj_keys.append(k)

    def kind(k: str) -> str:
        vals = [(r.get("objs") or {}).get(k) for r in recs]
        vals = [v for v in vals if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v))]
        return "quality" if vals and sum(1 for v in vals if v <= 0) >= len(vals) / 2 else "cost"

    obj_kind = {k: kind(k) for k in obj_keys}
    cost = [k for k in obj_keys if obj_kind[k] == "cost"]
    quality = [k for k in obj_keys if obj_kind[k] == "quality"]
    x_obj = next((k for k in cost if "lat" in k.lower()), cost[0] if cost else None)
    y_obj = quality[0] if quality else None
    if not (x_obj and y_obj):
        sys.stderr.write(
            f"[push_pareto_final] 无法确定帕累托两轴（需至少 1 成本 + 1 质量目标），obj_keys={obj_keys}；跳过 C5\n"
        )
        return 0

    # 选中的 gene 集合（按 gene 列表匹配）
    sel_path = (
        Path(args.selection_summary)
        if args.selection_summary
        else output_dir / "runs" / "retrain" / "selected" / "selection_summary.json"
    )
    selected_genes: set[str] = set()
    if sel_path.is_file():
        try:
            summ = json.loads(sel_path.read_text(encoding="utf-8"))
            for s in summ.get("selected", []):
                g = s.get("gene")
                if isinstance(g, list):
                    selected_genes.add(json.dumps(g, separators=(",", ":")))
        except Exception as e:
            sys.stderr.write(f"[push_pareto_final] 解析 selection_summary 失败（忽略 selected 高亮）：{e}\n")

    # 构造点（去 NaN）
    pts_raw: list[tuple[float, float, str]] = []  # (x_disp, y_disp, gene_key)
    for r in recs:
        o = r.get("objs") or {}
        try:
            xv = float(o[x_obj])
            yv = float(o[y_obj])
        except (TypeError, ValueError, KeyError):
            continue
        if math.isnan(xv) or math.isnan(yv):
            continue
        x_disp = xv if obj_kind[x_obj] == "cost" else -xv
        y_disp = yv if obj_kind[y_obj] == "cost" else -yv
        g = r.get("gene")
        gkey = json.dumps(g, separators=(",", ":")) if isinstance(g, list) else ""
        pts_raw.append((x_disp, y_disp, gkey))

    if not pts_raw:
        sys.stderr.write("[push_pareto_final] 无有效点，跳过 C5\n")
        return 0

    coords = [(p[0], p[1]) for p in pts_raw]
    front_idx = {i for i in range(len(pts_raw)) if not _is_dominated(i, coords)}

    # 方案 A（plan §4.3）：切 chart_type=pareto——前端自绘前沿连线 + 消费
    # pareto_x/y_direction。data 只留 {x, y} 两列（去 status/hue）：
    # ① 全部前沿点必须保留（前端据 direction 重算前沿，丢真前沿点会出错）；
    # ② dominated 仅作背景云，过多则降采样控 payload。selected 高亮牺牲
    #   （终态图里 selected 已在 Selection Funnel 体现）。
    front_pts = [pts_raw[i] for i in front_idx]
    sel_pts = [p for p in pts_raw if p[2] in selected_genes]
    dom_pts = [pts_raw[i] for i in range(len(pts_raw)) if i not in front_idx]
    if len(dom_pts) > 1200:
        step = math.ceil(len(dom_pts) / 1200)
        dom_pts = dom_pts[::step]

    x_title = x_obj + (" (ms)" if "lat" in x_obj.lower() else "")
    y_title = y_obj

    chart_pts = front_pts + dom_pts
    data = [{x_title: p[0], y_title: p[1]} for p in chart_pts]
    render_chart(
        chart_type="pareto",
        data=data,
        label="nas/search",
        title="Pareto Front (final)",
        x=x_title,
        y=y_title,
        pareto_x_direction="min",
        pareto_y_direction="max",
        x_label=f"{x_obj}（↓better{'+，已取负显示' if obj_kind[x_obj] == 'quality' else ''}）",
        y_label=f"{y_obj}（↑better{'+，已取负显示' if obj_kind[y_obj] == 'quality' else ''}）",
        caption=(
            "全局非支配前沿（sidecar 据 cost/quality 符号自算，非 per-gen 标志）。"
            "x=成本类（越小越好），y=质量类（取负后越大越好）；selected 见 Selection Funnel。"
        ),
    )
    n_sel = len({p[2] for p in sel_pts})
    print(
        f"[push_pareto_final] C5 pushed: {len(front_pts)} front / {len(dom_pts)} dominated(thinned) / "
        f"{n_sel} selected; axes {y_obj}(↑better) vs {x_obj}(↓better)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

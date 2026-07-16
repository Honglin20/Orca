"""viz_struct.py —— 草稿 §12 四张静态 HTML 图（self-contained · 幂等 · 确定性）。

契约（docs/specs/agent-structural-exploration-design-draft.md §12 / §11）：
  读 ledger.jsonl + champions.jsonl，幂等覆盖渲染 4 个 plotly 内嵌 JS 的 HTML：
    1. champion_trace.html   —— 主图（Karpathy autoresearch 风格 champion 轨迹）
    2. pareto.html           —— latency vs accuracy，status 着色 + Pareto 前沿
    3. exploration_tree.html —— parent-DAG，按 path 着色、按 status 标记
    4. ledger_table.html     —— 每轮汇总表 + champion latency waterfall

CLI：
    viz_struct.py \\
      --ledger <path> \\
      --champions <path> \\
      --baseline_latency_ms <float> \\
      --baseline_accuracy <float> \\
      --target_latency_ms <float> \\
      --accuracy_target <float> \\
      --out_dir <run_dir>          # HTML 落 <run_dir>/viz/

纪律：
  - 数据不足（ledger < 2 行 / 必备字段缺失 / 无有效数据点）→ **该图跳过**（stderr WARN，不报错、不阻断）。
  - 幂等：每次重渲染覆盖同名 HTML。
  - self-contained：plotly `include_plotlyjs='inline'` + `full_html=True`，无外部文件依赖。
  - 确定性脚本：无 LLM、无网络、不读时钟、不读随机。
  - fail loud：仅当 I/O 或参数硬错时非零退出；数据问题永远 exit 0。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

import plotly.graph_objects as go
from plotly.subplots import make_subplots


# ── 常量 / 主题 ──────────────────────────────────────────────────────────────

# §11.1 / §9.2 status 合法值。颜色固定为 Karpathy autoresearch 风格的克低饱和度色盘。
STATUS_COLOR: dict[str, str] = {
    "SUCCESS": "#2ca02c",        # 绿（达标 / 赢家）
    "FAIL_latency": "#ff7f0e",   # 橙（时延门未过）
    "FAIL_accuracy": "#d62728",  # 红（精度掉）
    "FAIL_export": "#9467bd",    # 紫（ONNX 导不出）
    "REJECT_struct": "#7f7f7f",  # 灰（严格模式 hyperparam-only reject）
}
STATUS_MISSING = "#cccccc"  # ledger 出现未知 status 时的兜底色。

# ledger.jsonl 每行视觉/计算所需字段（§11.1；timestamp 已由 reducer 写 null）。
_LEDGER_FIELDS = (
    "id", "parent", "path", "round", "status", "tag",
    "latency_ms", "accuracy", "delta_latency_ms", "met_accuracy",
    "snapshot", "onnx", "diff_summary", "hypothesis",
)
_CHAMPION_FIELDS = ("round", "id", "latency_ms", "accuracy", "delta_vs_baseline_ms", "snapshot")

# 数据不足的下限：ledger 少于这么多**有效行** → 该图跳过（§12 纪律）。
_MIN_ROWS = 2

# Karpathy 风格主图配色（深底霓虹强调色被刻意避开——克低饱和度更可读）。
_BG = "#ffffff"
_GRID = "#e9ecef"
_TEXT = "#1f2328"
_ALL_CAND_FILL = "rgba(110, 118, 129, 0.35)"
_ALL_CAND_LINE = "rgba(110, 118, 129, 0.65)"
_CHAMP_COLOR = "#0969da"   # 主轨迹：克制的蓝（autoresearch champion line）
_BASELINE_COLOR = "#1f2328"  # baseline：黑虚线（不可忽视的参照）
_TARGET_COLOR = "#cf222e"    # target：红虚线（目标 / 阈值）
_PARETO_COLOR = "#0969da"
_TREE_EDGE = "rgba(110, 118, 129, 0.55)"


# ── I/O ──────────────────────────────────────────────────────────────────────


def _read_jsonl(path: str, *, kind: str) -> list[dict[str, Any]]:
    """读 jsonl。文件不存在 / 行非 JSON → 视为空（容错：viz 是 sidecar，不阻断主循环）。"""
    p = Path(path)
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        s = raw.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as e:
            print(
                f"[viz_struct] WARN: {kind} {path} 第 {lineno} 行非合法 JSON，跳过该行：{e}",
                file=sys.stderr,
            )
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _to_float(v: Any) -> float | None:
    """宽松转 float；None / 非数字 → None（FAIL_export 时 latency_ms=-1 也视为缺失）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if v != v:  # NaN
            return None
        return float(v)
    return None


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _write_html(fig: go.Figure, out_path: Path) -> None:
    """幂等覆盖写 self-contained HTML（plotly JS 全内嵌）。"""
    _ensure_dir(out_path.parent)
    fig.write_html(
        str(out_path),
        include_plotlyjs="inline",
        full_html=True,
        config={"displayModeBar": True, "displaylogo": False},
    )


# ── 共用 hover 文本 ──────────────────────────────────────────────────────────


def _hover_for_candidate(e: dict[str, Any]) -> str:
    """champion_trace / tree 共用的 candidate hover HTML。"""
    latency = _to_float(e.get("latency_ms"))
    accuracy = _to_float(e.get("accuracy"))
    lat_s = f"{latency:.2f}ms" if latency is not None else "n/a"
    acc_s = f"{accuracy:.4f}" if accuracy is not None else "n/a"
    diff = str(e.get("diff_summary") or "").strip() or "(no diff_summary)"
    return (
        f"id={e.get('id','?')}<br>"
        f"status={e.get('status','?')} · tag={e.get('tag','?')}<br>"
        f"latency={lat_s} · accuracy={acc_s}<br>"
        f"<span>{diff}</span>"
    )


def _apply_base_layout(fig: go.Figure, *, title: str, x_title: str, y_title: str, height: int = 560) -> None:
    fig.update_layout(
        title=dict(text=title, font=dict(size=18, color=_TEXT), x=0.02, xanchor="left"),
        paper_bgcolor=_BG,
        plot_bgcolor=_BG,
        font=dict(color=_TEXT, family="ui-sans-serif, system-ui, -apple-system, Segoe UI, Helvetica, Arial"),
        height=height,
        margin=dict(l=60, r=30, t=70, b=50),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0.0),
    )
    fig.update_xaxes(title_text=x_title, gridcolor=_GRID, zerolinecolor=_GRID)
    fig.update_yaxes(title_text=y_title, gridcolor=_GRID, zerolinecolor=_GRID)


# ── 图 1：champion_trace（主图 · Karpathy autoresearch 风格）──────────────────


def render_champion_trace(
    ledger: list[dict[str, Any]],
    champions: list[dict[str, Any]],
    baseline_latency_ms: float,
    baseline_accuracy: float,
    target_latency_ms: float,
    out_path: Path,
) -> bool:
    """§12 图1：所有候选灰散点 + champion 轨迹（高亮线+点）+ baseline/target 虚线 + ★ 达标点。

    x = candidate 在 ledger 中的行序（0-indexed），y = latency_ms。
    champion 轨迹：从 champions.jsonl 取每条 (id, latency)，id → ledger 序号；baseline → x=-1。
    """
    # 数据门槛：至少 2 行 ledger（§12 纪律）。
    if len(ledger) < _MIN_ROWS:
        print(f"[viz_struct] WARN: 跳过 champion_trace：ledger 行数 {len(ledger)} < {_MIN_ROWS}", file=sys.stderr)
        return False

    # 过滤出有时延的有效候选（排除 FAIL_export 的 -1 / 字段缺失）。
    pts: list[tuple[int, dict[str, Any]]] = []
    for i, e in enumerate(ledger):
        lat = _to_float(e.get("latency_ms"))
        if lat is None or lat < 0:
            continue
        pts.append((i, e))
    if not pts:
        print("[viz_struct] WARN: 跳过 champion_trace：ledger 无有效 latency 数据点", file=sys.stderr)
        return False

    fig = go.Figure()

    # ── 所有候选：灰散点 ──
    fig.add_trace(
        go.Scatter(
            x=[i for i, _ in pts],
            y=[e["latency_ms"] for _, e in pts],
            mode="markers",
            marker=dict(color=_ALL_CAND_FILL, size=9, line=dict(color=_ALL_CAND_LINE, width=0.5)),
            name="all candidates",
            hovertext=[_hover_for_candidate(e) for _, e in pts],
            hoverinfo="text+x+y",
        )
    )

    # ── Champion 轨迹：高亮线 + 菱形点 ──
    if champions:
        id_to_idx = {e.get("id"): i for i, e in enumerate(ledger) if isinstance(e, dict)}
        ch_x: list[int] = []
        ch_y: list[float] = []
        ch_hover: list[str] = []
        for ch in champions:
            cid = ch.get("id")
            if cid is None:
                continue
            lat = _to_float(ch.get("latency_ms"))
            if lat is None:
                continue
            # baseline（不在 ledger 中）→ x = -1（视觉上位于所有候选之前）。
            x = id_to_idx.get(cid, -1)
            ch_x.append(x)
            ch_y.append(lat)
            acc = _to_float(ch.get("accuracy"))
            acc_s = f"{acc:.4f}" if acc is not None else "n/a"
            ch_hover.append(
                f"champion · round={ch.get('round','?')} · id={cid}<br>"
                f"latency={lat:.2f}ms · accuracy={acc_s}<br>"
                f"Δbaseline={ch.get('delta_vs_baseline_ms','?')}ms"
            )
        if ch_x:
            fig.add_trace(
                go.Scatter(
                    x=ch_x,
                    y=ch_y,
                    mode="lines+markers",
                    line=dict(color=_CHAMP_COLOR, width=2.8, shape="hv"),
                    marker=dict(
                        color=_CHAMP_COLOR,
                        size=13,
                        symbol="diamond",
                        line=dict(color="#ffffff", width=2),
                    ),
                    name="champion trace (running min)",
                    hovertext=ch_hover,
                    hoverinfo="text+x+y",
                )
            )

            # ── ★ 首次 ≤ target 处 ──
            for x, y, ch in zip(ch_x, ch_y, champions):
                if y <= target_latency_ms:
                    fig.add_annotation(
                        x=x,
                        y=y,
                        text="★ target met",
                        showarrow=True,
                        arrowhead=3,
                        arrowsize=1.2,
                        arrowcolor=_TARGET_COLOR,
                        ax=20,
                        ay=-40,
                        font=dict(color=_TARGET_COLOR, size=13),
                    )
                    break

    # ── baseline / target 水平虚线 ──
    fig.add_hline(
        y=baseline_latency_ms,
        line=dict(dash="dash", color=_BASELINE_COLOR, width=1.5),
        annotation_text=f"baseline {baseline_latency_ms:.1f}ms",
        annotation_position="top left",
        annotation_font=dict(color=_BASELINE_COLOR, size=11),
    )
    fig.add_hline(
        y=target_latency_ms,
        line=dict(dash="dash", color=_TARGET_COLOR, width=1.5),
        annotation_text=f"target {target_latency_ms:.1f}ms",
        annotation_position="bottom left",
        annotation_font=dict(color=_TARGET_COLOR, size=11),
    )

    _apply_base_layout(
        fig,
        title="Latency Champion Trace — running-min trajectory across candidates",
        x_title="candidate index (ledger order)",
        y_title="latency (ms)",
        height=620,
    )
    fig.update_yaxes(rangemode="tozero")  # 时延从 0 起，更直观。
    _write_html(fig, out_path)
    return True


# ── 图 2：pareto（latency vs accuracy）──────────────────────────────────────


def render_pareto(
    ledger: list[dict[str, Any]],
    baseline_latency_ms: float,
    baseline_accuracy: float,
    target_latency_ms: float,
    accuracy_target: float,
    out_path: Path,
) -> bool:
    """§12 图2：latency(x) vs accuracy(y) 散点，按 status 着色；Pareto 前沿高亮。"""
    if len(ledger) < _MIN_ROWS:
        print(f"[viz_struct] WARN: 跳过 pareto：ledger 行数 {len(ledger)} < {_MIN_ROWS}", file=sys.stderr)
        return False

    # 收集候选：有时延即入图（精度缺失时落 y=0 strip，标"未训练"）。
    pts_full: list[tuple[float, float, dict[str, Any]]] = []   # (lat, acc) 都有效
    pts_lat_only: list[tuple[float, dict[str, Any]]] = []       # 只有 lat（FAIL_latency / FAIL_export）
    for e in ledger:
        lat = _to_float(e.get("latency_ms"))
        if lat is None or lat < 0:  # latency 哨兵 -1（FAIL_export）→ 完全无信息，跳过
            continue
        acc = _to_float(e.get("accuracy"))
        if acc is None or acc < 0:
            pts_lat_only.append((lat, e))
        else:
            pts_full.append((lat, acc, e))
    if not pts_full and not pts_lat_only:
        print("[viz_struct] WARN: 跳过 pareto：无有效 latency 数据点", file=sys.stderr)
        return False

    fig = go.Figure()

    # ── 按 status 分组散点（含 lat-only 点落 y=0 strip）──
    by_status: dict[str, list[tuple[float, float, dict[str, Any]]]] = {}
    for lat, acc, e in pts_full:
        by_status.setdefault(str(e.get("status", "?")), []).append((lat, acc, e))
    for lat, e in pts_lat_only:
        # 落 y=0 strip，hover 说明"未训练"。
        e2 = dict(e)
        e2["_strip"] = True
        by_status.setdefault(str(e.get("status", "?")), []).append((lat, 0.0, e2))
    # 稳定顺序：按 STATUS_COLOR 的 key 顺序，未知垫底。
    ordered = [s for s in STATUS_COLOR if s in by_status] + [
        s for s in by_status if s not in STATUS_COLOR
    ]
    for status in ordered:
        bucket = by_status[status]
        # 分两组：有精度的实心点 + 落 strip 的空心点（同一 status 两个 trace，便于区分）。
        solid = [(la, ac, e) for la, ac, e in bucket if not e.get("_strip")]
        strip = [(la, ac, e) for la, ac, e in bucket if e.get("_strip")]
        if solid:
            fig.add_trace(
                go.Scatter(
                    x=[b[0] for b in solid],
                    y=[b[1] for b in solid],
                    mode="markers",
                    marker=dict(
                        color=STATUS_COLOR.get(status, STATUS_MISSING),
                        size=11,
                        line=dict(color="#ffffff", width=0.8),
                        opacity=0.85,
                    ),
                    name=status,
                    hovertext=[_hover_for_candidate(b[2]) for b in solid],
                    hoverinfo="text+x+y",
                )
            )
        if strip:
            fig.add_trace(
                go.Scatter(
                    x=[b[0] for b in strip],
                    y=[b[1] for b in strip],
                    mode="markers",
                    marker=dict(
                        color=STATUS_COLOR.get(status, STATUS_MISSING),
                        size=10,
                        symbol="diamond-open",
                        line=dict(color=STATUS_COLOR.get(status, STATUS_MISSING), width=1.5),
                        opacity=0.7,
                    ),
                    name=f"{status} (no accuracy · y=0)",
                    hovertext=[
                        (_hover_for_candidate(b[2]) + "<br><i>(no accuracy · not trained)</i>")
                        for b in strip
                    ],
                    hoverinfo="text+x+y",
                )
            )

    # ── Pareto 前沿（min latency s.t. accuracy ≥ target）──
    # 标准 Pareto：在 (latency ↓, accuracy ↑) 二维下不被任何其他候选支配。
    # 与 spec §12 一致：仅考虑 accuracy ≥ target 的候选（不达精度的不算"可行"）。
    feasible = [(lat, acc, e) for lat, acc, e in pts_full if acc >= accuracy_target]
    frontier = _pareto_frontier(feasible)
    if frontier:
        frontier.sort(key=lambda t: t[0])  # 按 latency 升序连线
        fig.add_trace(
            go.Scatter(
                x=[t[0] for t in frontier],
                y=[t[1] for t in frontier],
                mode="lines+markers",
                line=dict(color=_PARETO_COLOR, width=2.2, dash="dot"),
                marker=dict(color=_PARETO_COLOR, size=10, symbol="circle-open"),
                name="Pareto frontier (min latency s.t. acc≥target)",
                hovertext=[_hover_for_candidate(t[2]) for t in frontier],
                hoverinfo="text+x+y",
            )
        )

    # ── baseline 点 ──
    fig.add_trace(
        go.Scatter(
            x=[baseline_latency_ms],
            y=[baseline_accuracy],
            mode="markers+text",
            marker=dict(color=_BASELINE_COLOR, size=16, symbol="star", line=dict(color="#ffffff", width=1)),
            text=["baseline"],
            textposition="middle right",
            name="baseline",
            showlegend=False,
            hoverinfo="x+y+text",
        )
    )

    # ── target 十字虚线 ──
    fig.add_vline(
        x=target_latency_ms,
        line=dict(dash="dash", color=_TARGET_COLOR, width=1.2),
        annotation_text=f"target latency {target_latency_ms:.1f}ms",
        annotation_position="top",
        annotation_font=dict(color=_TARGET_COLOR, size=10),
    )
    fig.add_hline(
        y=accuracy_target,
        line=dict(dash="dash", color=_TARGET_COLOR, width=1.2),
        annotation_text=f"accuracy target {accuracy_target:.4f}",
        annotation_position="top right",
        annotation_font=dict(color=_TARGET_COLOR, size=10),
    )

    _apply_base_layout(
        fig,
        title="Latency vs Accuracy Pareto",
        x_title="latency (ms)",
        y_title="accuracy",
        height=580,
    )
    # 若有 lat-only strip 点（y=0），标一行注解；并使 y 轴 range 包住实际精度簇。
    if pts_lat_only and pts_full:
        acc_min = min(acc for _, acc, _ in pts_full)
        acc_max = max(acc for _, acc, _ in pts_full)
        pad = max((acc_max - acc_min) * 0.15, 0.01)
        fig.update_yaxes(range=[-pad * 0.4, acc_max + pad])
        fig.add_annotation(
            xref="paper", yref="paper", x=1.0, y=0.0, xanchor="right", yanchor="bottom",
            text="◇ on y=0 strip = latency-only (not trained)",
            showarrow=False, font=dict(size=10, color="#6e7681"),
        )
    _write_html(fig, out_path)
    return True


def _pareto_frontier(pts: list[tuple[float, float, dict[str, Any]]]) -> list[tuple[float, float, dict[str, Any]]]:
    """计算 (latency ↓, accuracy ↑) 意义下的 Pareto 前沿。O(n^2)（n 小，可读优先）。"""
    frontier: list[tuple[float, float, dict[str, Any]]] = []
    for i, (la, ac, e) in enumerate(pts):
        dominated = False
        for j, (lb, ab, _) in enumerate(pts):
            if i == j:
                continue
            # j 支配 i：latency 不大 且 accuracy 不小，至少一项严格。
            if lb <= la and ab >= ac and (lb < la or ab > ac):
                dominated = True
                break
        if not dominated:
            frontier.append((la, ac, e))
    return frontier


# ── 图 3：exploration_tree（parent-DAG）─────────────────────────────────────


def render_exploration_tree(
    ledger: list[dict[str, Any]],
    out_path: Path,
) -> bool:
    """§12 图3：节点 = candidate，边 = parent；按 path 着色、按 status 标记。

    布局：x = round，y = path lane（每条 path 一条水平泳道）；baseline 虚节点放 round=0。
    parent → child 用半透明灰线连接，跨泳道亦然。
    """
    if len(ledger) < _MIN_ROWS:
        print(f"[viz_struct] WARN: 跳过 exploration_tree：ledger 行数 {len(ledger)} < {_MIN_ROWS}", file=sys.stderr)
        return False

    # 收集 path → lane index（稳定：按首次出现顺序）。
    path_lanes: dict[str, int] = {}
    for e in ledger:
        p = str(e.get("path", "?"))
        if p not in path_lanes:
            path_lanes[p] = len(path_lanes)
    # id → (x, y) 坐标。baseline 虚节点放 round=0、中央。
    baseline_id = "baseline"
    n_lanes = max(len(path_lanes), 1)
    center_lane = (n_lanes - 1) / 2.0
    coords: dict[str, tuple[float, float]] = {baseline_id: (0.0, center_lane)}
    for e in ledger:
        cid = str(e.get("id", "?"))
        rnd = _to_float(e.get("round")) or 0.0
        p = str(e.get("path", "?"))
        coords[cid] = (rnd, float(path_lanes.get(p, 0)))

    fig = go.Figure()

    # ── 边：parent → child（半透明灰线）──
    edges_drawn = 0
    for e in ledger:
        cid = str(e.get("id", "?"))
        parent = str(e.get("parent", ""))
        if not parent or parent not in coords or cid not in coords:
            continue
        px, py = coords[parent]
        cx, cy = coords[cid]
        fig.add_trace(
            go.Scatter(
                x=[px, cx],
                y=[py, cy],
                mode="lines",
                line=dict(color=_TREE_EDGE, width=1.2),
                hoverinfo="skip",
                showlegend=False,
            )
        )
        edges_drawn += 1

    # ── 节点：按 path 分组（颜色）、按 status 标记（marker symbol）──
    status_symbol = {
        "SUCCESS": "circle",
        "FAIL_latency": "triangle-down",
        "FAIL_accuracy": "x",
        "FAIL_export": "diamond",
        "REJECT_struct": "hexagon",
    }
    # 按 path 分桶；颜色用克制的色板。
    path_palette = [
        "#0969da", "#1f883d", "#bf3989", "#9333ea", "#d97706",
        "#0891b2", "#65a30d", "#dc2626", "#7c3aed", "#0d9488",
    ]
    by_path: dict[str, list[dict[str, Any]]] = {}
    for e in ledger:
        by_path.setdefault(str(e.get("path", "?")), []).append(e)

    for pi, path in enumerate(by_path):
        bucket = by_path[path]
        color = path_palette[pi % len(path_palette)]
        xs = [coords[str(e.get("id", "?"))][0] for e in bucket]
        ys = [coords[str(e.get("id", "?"))][1] for e in bucket]
        symbols = [status_symbol.get(str(e.get("status", "?")), "circle-open") for e in bucket]
        # plotly 单 trace 内 symbol 必须 scalar → 拆成多 trace（每个 status 一条）。
        for status in sorted({str(e.get("status", "?")) for e in bucket}):
            sub = [e for e in bucket if str(e.get("status", "?")) == status]
            sx = [coords[str(e.get("id", "?"))][0] for e in sub]
            sy = [coords[str(e.get("id", "?"))][1] for e in sub]
            fig.add_trace(
                go.Scatter(
                    x=sx,
                    y=sy,
                    mode="markers+text",
                    marker=dict(
                        color=color,
                        size=16,
                        symbol=status_symbol.get(status, "circle-open"),
                        line=dict(color="#ffffff", width=1.2),
                    ),
                    text=[str(e.get("id", "?")) for e in sub],
                    textposition="top center",
                    textfont=dict(size=9, color=color),
                    name=f"path={path} · {status}",
                    hovertext=[_hover_for_candidate(e) for e in sub],
                    hoverinfo="text+x+y",
                )
            )

    # ── baseline 虚节点 ──
    fig.add_trace(
        go.Scatter(
            x=[0.0],
            y=[center_lane],
            mode="markers+text",
            marker=dict(color=_BASELINE_COLOR, size=14, symbol="star", line=dict(color="#ffffff", width=1)),
            text=["baseline"],
            textposition="middle right",
            name="baseline (root)",
            showlegend=False,
            hoverinfo="x+y+text",
        )
    )

    # y 轴用 path 名作为刻度。
    lane_to_path = {v: k for k, v in path_lanes.items()}
    yticks = sorted(lane_to_path.keys())
    ytick_text = [lane_to_path[t] for t in yticks]

    _apply_base_layout(
        fig,
        title=f"Exploration Tree — {len(ledger)} candidates, {edges_drawn} parent edges, {len(path_lanes)} paths",
        x_title="round",
        y_title="path",
        height=max(480, 130 * n_lanes + 200),
    )
    fig.update_yaxes(
        tickvals=yticks,
        ticktext=ytick_text,
        gridcolor=_GRID,
        zerolinecolor=_GRID,
    )
    fig.update_xaxes(dtick=1)
    _write_html(fig, out_path)
    return True


# ── 图 4：ledger_table + waterfall ────────────────────────────────────────────


def render_ledger_table(
    ledger: list[dict[str, Any]],
    champions: list[dict[str, Any]],
    baseline_latency_ms: float,
    baseline_accuracy: float,
    out_path: Path,
) -> bool:
    """§12 图4：每轮汇总表 + champion latency waterfall（baseline → 每冠军步 shaved ms）。"""
    if len(ledger) < _MIN_ROWS:
        print(f"[viz_struct] WARN: 跳过 ledger_table：ledger 行数 {len(ledger)} < {_MIN_ROWS}", file=sys.stderr)
        return False

    rounds = sorted({(e.get("round") if isinstance(e.get("round"), int) else 0) for e in ledger})
    if not rounds:
        print("[viz_struct] WARN: 跳过 ledger_table：ledger 无有效 round 字段", file=sys.stderr)
        return False

    # ── 每轮汇总 ──
    champion_by_round: dict[int, dict[str, Any]] = {}
    for ch in champions:
        r = ch.get("round")
        if isinstance(r, int):
            # 同 round 多 entry 取最后一条（champions 是 append-only，最后是最新的）。
            champion_by_round[r] = ch

    rows: list[dict[str, Any]] = []
    for r in rounds:
        bucket = [e for e in ledger if e.get("round") == r]
        proposed = len(bucket)
        passed_gate = sum(1 for e in bucket if e.get("status") in {"SUCCESS", "FAIL_accuracy"})
        trained = passed_gate  # 过时延门 → 训练（§4）；FAIL_export 不计
        met = sum(
            1
            for e in bucket
            if e.get("status") == "SUCCESS" and e.get("met_accuracy") is True
        )
        ch = champion_by_round.get(r)
        ch_lat = _to_float(ch.get("latency_ms")) if ch else None
        delta = (ch_lat - baseline_latency_ms) if ch_lat is not None else None
        rows.append(
            {
                "round": r,
                "proposed": proposed,
                "passed_latency_gate": passed_gate,
                "trained": trained,
                "met_target": met,
                "champion_latency_ms": ch_lat,
                "delta_vs_baseline_ms": delta,
            }
        )

    # ── 双子图：表 + waterfall ──
    fig = make_subplots(
        rows=2,
        cols=1,
        specs=[[{"type": "table"}], [{"type": "waterfall"}]],
        row_heights=[0.42, 0.58],
        vertical_spacing=0.09,
    )

    # Table
    def _fmt(v: Any, *, ms: bool = False) -> str:
        if v is None:
            return "—"
        if isinstance(v, float):
            s = f"{v:.2f}"
            return f"{s}ms" if ms else s
        return str(v)

    fig.add_trace(
        go.Table(
            header=dict(
                values=[
                    "<b>round</b>",
                    "<b>proposed</b>",
                    "<b>passed gate</b>",
                    "<b>trained</b>",
                    "<b>met target</b>",
                    "<b>champion latency</b>",
                    "<b>Δbaseline</b>",
                ],
                fill_color="#1f2328",
                font=dict(color="#ffffff", size=12),
                align="center",
                height=28,
            ),
            cells=dict(
                values=[
                    [row["round"] for row in rows],
                    [row["proposed"] for row in rows],
                    [row["passed_latency_gate"] for row in rows],
                    [row["trained"] for row in rows],
                    [row["met_target"] for row in rows],
                    [_fmt(row["champion_latency_ms"], ms=True) for row in rows],
                    [_fmt(row["delta_vs_baseline_ms"], ms=True) for row in rows],
                ],
                fill_color=[["#f6f8fa", "#ffffff"] * ((len(rows) + 1) // 2)],
                align="center",
                font=dict(size=12, color=_TEXT),
                height=24,
            ),
        ),
        row=1,
        col=1,
    )

    # Waterfall：baseline → 每个冠军步 shaved ms。
    # champions.jsonl 首行通常是 round=0 的 baseline seed；之后每个新冠军是一步。
    wf_labels: list[str] = []
    wf_values: list[float] = []
    wf_measures: list[str] = []
    if champions:
        # 第一条作为 absolute 锚点（baseline latency），其余作为 relative delta。
        prev_lat = _to_float(champions[0].get("latency_ms"))
        if prev_lat is None:
            prev_lat = baseline_latency_ms
        wf_labels.append(str(champions[0].get("id", "baseline")))
        wf_values.append(prev_lat)
        wf_measures.append("absolute")
        for ch in champions[1:]:
            cur_lat = _to_float(ch.get("latency_ms"))
            if cur_lat is None:
                continue
            delta = cur_lat - prev_lat  # 负数 = 下降（shaved）
            wf_labels.append(str(ch.get("id", "?")))
            wf_values.append(delta)
            wf_measures.append("relative")
            prev_lat = cur_lat
        # 终结 total bar
        wf_measures.append("total")
        wf_labels.append("final")
        wf_values.append(0)
    else:
        wf_labels = ["baseline"]
        wf_values = [baseline_latency_ms]
        wf_measures = ["absolute"]

    increasing_color = "#cf222e"   # 时延上升：红
    decreasing_color = "#1f883d"   # 时延下降：绿（shaved，好）
    total_color = "#0969da"

    fig.add_trace(
        go.Waterfall(
            x=wf_labels,
            measure=wf_measures,
            y=wf_values,
            connector={"line": {"color": "rgba(110,118,129,0.55)", "width": 1}},
            increasing={"marker": {"color": increasing_color}},
            decreasing={"marker": {"color": decreasing_color}},
            totals={"marker": {"color": total_color}},
            textposition="outside",
            text=[f"{v:+.2f}ms" if m == "relative" else f"{v:.2f}ms" for v, m in zip(wf_values, wf_measures)],
            name="champion latency waterfall",
            showlegend=False,
        ),
        row=2,
        col=1,
    )

    fig.update_layout(
        title=dict(
            text="Round Ledger Summary + Champion Latency Waterfall",
            font=dict(size=18, color=_TEXT),
            x=0.02,
            xanchor="left",
        ),
        paper_bgcolor=_BG,
        plot_bgcolor=_BG,
        font=dict(color=_TEXT, family="ui-sans-serif, system-ui, -apple-system, Segoe UI, Helvetica, Arial"),
        height=820,
        margin=dict(l=60, r=30, t=70, b=50),
    )
    fig.update_yaxes(title_text="latency (ms)", row=2, col=1, gridcolor=_GRID)
    fig.update_xaxes(title_text="champion step", row=2, col=1, gridcolor=_GRID)

    _write_html(fig, out_path)
    return True


# ── 主入口 ─────────────────────────────────────────────────────────────────


def render_all(
    *,
    ledger_path: str,
    champions_path: str,
    baseline_latency_ms: float,
    baseline_accuracy: float,
    target_latency_ms: float,
    accuracy_target: float,
    out_dir: str,
) -> dict[str, Any]:
    """幂等渲染 4 图到 <out_dir>/viz/。返回 {chart: rendered_bool, out: path}。"""
    ledger = _read_jsonl(ledger_path, kind="ledger")
    champions = _read_jsonl(champions_path, kind="champions")

    viz_dir = Path(out_dir) / "viz"
    _ensure_dir(viz_dir)

    results: dict[str, Any] = {
        "ledger_rows": len(ledger),
        "champion_rows": len(champions),
        "out_dir": str(viz_dir),
        "charts": {},
    }

    # 过滤掉缺必备字段的 ledger 行（仅容错 WARN，不整体 fail）。
    clean_ledger: list[dict[str, Any]] = []
    for i, e in enumerate(ledger):
        missing = [k for k in ("id", "parent", "path", "round", "status", "latency_ms", "accuracy") if k not in e]
        if missing:
            print(
                f"[viz_struct] WARN: ledger 第 {i + 1} 行缺字段 {missing}，该行从可视化剔除",
                file=sys.stderr,
            )
            continue
        clean_ledger.append(e)

    charts = [
        ("champion_trace", "champion_trace.html", lambda op: render_champion_trace(
            clean_ledger, champions, baseline_latency_ms, baseline_accuracy, target_latency_ms, op
        )),
        ("pareto", "pareto.html", lambda op: render_pareto(
            clean_ledger, baseline_latency_ms, baseline_accuracy, target_latency_ms, accuracy_target, op
        )),
        ("exploration_tree", "exploration_tree.html", lambda op: render_exploration_tree(
            clean_ledger, op
        )),
        ("ledger_table", "ledger_table.html", lambda op: render_ledger_table(
            clean_ledger, champions, baseline_latency_ms, baseline_accuracy, op
        )),
    ]
    for name, fname, fn in charts:
        out_path = viz_dir / fname
        try:
            ok = fn(out_path)
        except Exception as e:  # 单图异常不影响其他图（sidecar 不阻断主循环）。
            print(f"[viz_struct] WARN: 渲染 {name} 异常，跳过：{type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            ok = False
        results["charts"][name] = {
            "rendered": bool(ok),
            "path": str(out_path) if ok else None,
        }

    return results


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="草稿 §12 四张静态 HTML 图（self-contained · 幂等 · 确定性）。"
    )
    parser.add_argument("--ledger", required=True, help="ledger.jsonl 路径")
    parser.add_argument("--champions", required=True, help="champions.jsonl 路径")
    parser.add_argument("--baseline_latency_ms", type=float, required=True)
    parser.add_argument("--baseline_accuracy", type=float, required=True)
    parser.add_argument("--target_latency_ms", type=float, required=True)
    parser.add_argument("--accuracy_target", type=float, required=True)
    parser.add_argument(
        "--out_dir",
        required=True,
        help="run 目录；HTML 落 <out_dir>/viz/",
    )
    args = parser.parse_args()

    try:
        result = render_all(
            ledger_path=args.ledger,
            champions_path=args.champions,
            baseline_latency_ms=args.baseline_latency_ms,
            baseline_accuracy=args.baseline_accuracy,
            target_latency_ms=args.target_latency_ms,
            accuracy_target=args.accuracy_target,
            out_dir=args.out_dir,
        )
    except Exception as e:
        # 仅 I/O / 参数硬错 → 非零退出（fail loud）。数据不足已在内部 WARN 兜底。
        print(f"[viz_struct] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

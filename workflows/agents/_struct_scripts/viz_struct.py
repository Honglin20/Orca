"""viz_struct.py —— 草稿 §12 五张图，经 orca.chart render_chart 推送（无 HTML 产物）。

契约（docs/specs/agent-structural-exploration-design-draft.md §12 / §11）：
  读 ledger.jsonl + champions.jsonl，把五张图的数据推送到 Orca Web 面板：
    1. Champion Trace      —— line（候选灰点 + champion 轨迹，hue=series）
    2. Latency-Accuracy Pareto —— pareto（hue=status，min/max 方向）
    3. Exploration Tree    —— scatter（x=round, y=path, hue=status）
    4. Round Ledger        —— table（每轮汇总 + champion 时延/Δbaseline）
    5. Candidate Ledger    —— table（逐候选：改了什么/accuracy/时延/status）

纪律：
  - 仅用 ``orca.chart.render_chart``；**不输出任何 HTML 文件**（用户共识 2026-07-18：
    workflow 不产 HTML，所有可视化走 orca 原生 render_chart）。
  - 数据不足（ledger < 2 行 / 必备字段缺失 / 无有效数据点）→ **该图跳过**（stderr WARN，
    不报错、不阻断）。
  - 同 label="struct-explore" 下每图用唯一 title；同 title 再推 = 刷新（实时更新语义）。
  - 不在 Orca 子进程内（无 ORCA_* env → import orca.chart 失败）→ 整体跳过，stderr 提示。
  - 确定性脚本：无 LLM、无网络、不读时钟、不读随机。
  - fail loud：仅 I/O 或参数硬错时非零退出；数据问题永远 exit 0。

CLI：
    viz_struct.py \\
      --ledger <path> --champions <path> \\
      --baseline_latency_ms <f> --baseline_accuracy <f> \\
      --target_latency_ms <f> --accuracy_target <f>
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

# orca.chart web push（phase-13 render_chart API）。不在 Orca 子进程内跑时为 None。
try:
    from orca.chart import render_chart as _orca_render_chart
except ImportError:
    _orca_render_chart = None


# ── 常量 ─────────────────────────────────────────────────────────────────────

# 数据不足的下限：ledger 少于这么多**有效行** → 该图跳过（§12 纪律）。
_MIN_ROWS = 2

# 同一群组 label：五张图共用，title 区分；同 title 再推 = 刷新。
_LABEL = "struct-explore"

# ledger.jsonl 每行必备字段（缺则该行从可视化剔除，仅 WARN）。
_LEDGER_REQUIRED = ("id", "parent", "path", "round", "status", "latency_ms", "accuracy")


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
    """宽松转 float；None / 非数字 / NaN → None（FAIL_export 时 latency_ms=-1 也视为缺失）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if v != v:  # NaN
            return None
        return float(v)
    return None


def _clean_ledger(ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """剔除缺必备字段的行（容错 WARN，不整体 fail）。"""
    clean: list[dict[str, Any]] = []
    for i, e in enumerate(ledger):
        missing = [k for k in _LEDGER_REQUIRED if k not in e]
        if missing:
            print(
                f"[viz_struct] WARN: ledger 第 {i + 1} 行缺字段 {missing}，该行从可视化剔除",
                file=sys.stderr,
            )
            continue
        clean.append(e)
    return clean


# ── 五张图 → render_chart ────────────────────────────────────────────────────


def _push_champion_trace(
    ledger: list[dict[str, Any]],
    champions: list[dict[str, Any]],
    target_latency_ms: float,
) -> bool:
    """图1：候选灰点 + champion 轨迹（running min）。x=ledger 行序, y=latency_ms, hue=series。"""
    if len(ledger) < _MIN_ROWS:
        print(f"[viz_struct] WARN: 跳过 champion_trace：ledger 行数 {len(ledger)} < {_MIN_ROWS}", file=sys.stderr)
        return False

    data: list[dict[str, Any]] = []
    for i, e in enumerate(ledger):
        lat = _to_float(e.get("latency_ms"))
        if lat is None or lat < 0:
            continue
        data.append({
            "index": i,
            "latency": lat,
            "series": "candidate",
            "id": str(e.get("id", "?")),
            "status": str(e.get("status", "?")),
        })

    id_to_idx = {e.get("id"): i for i, e in enumerate(ledger)}
    for ch in champions:
        cid = ch.get("id")
        if cid is None:
            continue
        lat = _to_float(ch.get("latency_ms"))
        if lat is None:
            continue
        acc = _to_float(ch.get("accuracy"))
        data.append({
            "index": id_to_idx.get(cid, -1),
            "latency": lat,
            "series": "champion",
            "id": str(cid),
            "status": "champion",
            "accuracy": acc,
            "round": ch.get("round"),
        })

    if not data:
        print("[viz_struct] WARN: 跳过 champion_trace：无有效 latency 数据点", file=sys.stderr)
        return False

    _orca_render_chart(
        chart_type="line",
        data=data,
        label=_LABEL,
        title="Champion Trace",
        x="index",
        y="latency",
        hue="series",
        x_label="候选序号(账本行)",
        y_label="时延 (ms)",
        caption="每轮 champion 的实测时延变化；★=达标",
    )
    return True


def _push_pareto(
    ledger: list[dict[str, Any]],
    baseline_latency_ms: float,
    baseline_accuracy: float,
    accuracy_target: float,
) -> bool:
    """图2：latency(x) vs accuracy(y)，hue=status。pareto_x=min, pareto_y=max。"""
    if len(ledger) < _MIN_ROWS:
        print(f"[viz_struct] WARN: 跳过 pareto：ledger 行数 {len(ledger)} < {_MIN_ROWS}", file=sys.stderr)
        return False

    data: list[dict[str, Any]] = []
    for e in ledger:
        lat = _to_float(e.get("latency_ms"))
        if lat is None or lat < 0:
            continue
        acc = _to_float(e.get("accuracy"))
        data.append({
            "latency": lat,
            "accuracy": acc if (acc is not None and acc >= 0) else None,
            "status": str(e.get("status", "?")),
            "id": str(e.get("id", "?")),
        })
    if not data:
        print("[viz_struct] WARN: 跳过 pareto：无有效 latency 数据点", file=sys.stderr)
        return False

    _orca_render_chart(
        chart_type="pareto",
        data=data,
        label=_LABEL,
        title="Latency-Accuracy Pareto",
        x="latency",
        y="accuracy",
        hue="status",
        pareto_x_direction="min",
        pareto_y_direction="max",
    )
    return True


def _push_exploration_tree(ledger: list[dict[str, Any]]) -> bool:
    """图3：parent-DAG 投影。x=round, y=path, hue=status。"""
    if len(ledger) < _MIN_ROWS:
        print(f"[viz_struct] WARN: 跳过 exploration_tree：ledger 行数 {len(ledger)} < {_MIN_ROWS}", file=sys.stderr)
        return False

    data: list[dict[str, Any]] = []
    for e in ledger:
        rnd = e.get("round")
        if not isinstance(rnd, int):
            rnd = 0
        data.append({
            "round": rnd,
            "path": str(e.get("path", "?")),
            "id": str(e.get("id", "?")),
            "status": str(e.get("status", "?")),
            "parent": str(e.get("parent", "")),
        })
    if not data:
        return False

    _orca_render_chart(
        chart_type="scatter",
        data=data,
        label=_LABEL,
        title="Exploration Tree",
        x="round",
        y="path",
        hue="status",
    )
    return True


def _push_ledger_table(
    ledger: list[dict[str, Any]],
    champions: list[dict[str, Any]],
    baseline_latency_ms: float,
) -> bool:
    """图4：每轮汇总表。列 = round/proposed/passed_gate/met_target/champion_latency/Δbaseline。"""
    if len(ledger) < _MIN_ROWS:
        print(f"[viz_struct] WARN: 跳过 ledger_table：ledger 行数 {len(ledger)} < {_MIN_ROWS}", file=sys.stderr)
        return False

    rounds = sorted({e["round"] for e in ledger if isinstance(e.get("round"), int)})
    if not rounds:
        print("[viz_struct] WARN: 跳过 ledger_table：ledger 无有效 round 字段", file=sys.stderr)
        return False

    rows: list[dict[str, Any]] = []
    for r in rounds:
        bucket = [e for e in ledger if e.get("round") == r]
        proposed = len(bucket)
        passed_gate = sum(1 for e in bucket if e.get("status") in {"SUCCESS", "FAIL_accuracy"})
        met = sum(1 for e in bucket if e.get("status") == "SUCCESS" and e.get("met_accuracy") is True)
        ch_lat = None
        for ch in champions:
            if ch.get("round") == r:
                ch_lat = _to_float(ch.get("latency_ms"))
                break
        rows.append({
            "round": r,
            "proposed": proposed,
            "passed_gate": passed_gate,
            "met_target": met,
            "champion_latency_ms": round(ch_lat, 4) if ch_lat is not None else None,
            "delta_vs_baseline_ms": round(ch_lat - baseline_latency_ms, 4) if ch_lat is not None else None,
        })

    _orca_render_chart(
        chart_type="table",
        data=rows,
        label=_LABEL,
        title="Round Ledger",
        columns=["round", "proposed", "passed_gate", "met_target", "champion_latency_ms", "delta_vs_baseline_ms"],
    )
    return True


def _push_candidate_table(ledger: list[dict[str, Any]]) -> bool:
    """图5：逐候选明细表。列 = round/id/hypothesis/accuracy/latency_ms/status/tag。

    补 Round Ledger 表缺失的「每候选改了什么 + accuracy + 时延」维度。
    字段全部来自 ledger 现有字段（hypothesis 缺则退到 diff_summary）。
    """
    if len(ledger) < _MIN_ROWS:
        print(f"[viz_struct] WARN: 跳过 candidate_table：ledger 行数 {len(ledger)} < {_MIN_ROWS}", file=sys.stderr)
        return False

    rows: list[dict[str, Any]] = []
    for e in ledger:
        rnd = e.get("round")
        if not isinstance(rnd, int):
            rnd = 0
        rows.append({
            "round": rnd,
            "id": str(e.get("id", "?")),
            "hypothesis": str(e.get("hypothesis", e.get("diff_summary", ""))),
            "accuracy": _to_float(e.get("accuracy")),
            "latency_ms": _to_float(e.get("latency_ms")),
            "status": str(e.get("status", "?")),
            "tag": str(e.get("tag", "")),
        })
    rows.sort(key=lambda r: r["round"])

    _orca_render_chart(
        chart_type="table",
        data=rows,
        label=_LABEL,
        title="Candidate Ledger (per change)",
        columns=["round", "id", "hypothesis", "accuracy", "latency_ms", "status", "tag"],
    )
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
) -> dict[str, Any]:
    """推五张图到 Orca Web 面板。返回 {chart: pushed_bool}。无 HTML 产物。"""
    if _orca_render_chart is None:
        print("[viz_struct] WARN: orca.chart 不可用，跳过全部 web push（非 Orca 子进程？）", file=sys.stderr)

    ledger = _clean_ledger(_read_jsonl(ledger_path, kind="ledger"))
    champions = _read_jsonl(champions_path, kind="champions")

    results: dict[str, Any] = {
        "ledger_rows": len(ledger),
        "champion_rows": len(champions),
        "charts": {},
    }

    if _orca_render_chart is None or not ledger:
        return results

    pushers = [
        ("champion_trace", lambda: _push_champion_trace(ledger, champions, target_latency_ms)),
        ("pareto", lambda: _push_pareto(ledger, baseline_latency_ms, baseline_accuracy, accuracy_target)),
        ("exploration_tree", lambda: _push_exploration_tree(ledger)),
        ("ledger_table", lambda: _push_ledger_table(ledger, champions, baseline_latency_ms)),
        ("candidate_table", lambda: _push_candidate_table(ledger)),
    ]
    for name, fn in pushers:
        try:
            ok = fn()
        except Exception as e:  # 单图异常不影响其他图（sidecar 不阻断主循环）。
            print(f"[viz_struct] WARN: 推送 {name} 异常，跳过：{type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            ok = False
        results["charts"][name] = {"pushed": bool(ok)}

    return results


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="草稿 §12 五张图（orca.chart render_chart 推送，无 HTML 产物）。"
    )
    parser.add_argument("--ledger", required=True, help="ledger.jsonl 路径")
    parser.add_argument("--champions", required=True, help="champions.jsonl 路径")
    parser.add_argument("--baseline_latency_ms", type=float, required=True)
    parser.add_argument("--baseline_accuracy", type=float, required=True)
    parser.add_argument("--target_latency_ms", type=float, required=True)
    parser.add_argument("--accuracy_target", type=float, required=True)
    parser.add_argument(
        "--out_dir",
        required=False,
        default=None,
        help="(已废弃) 历史遗留参数，不再写 HTML；保留以兼容旧调用。",
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

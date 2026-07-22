"""viz_struct.py —— 草稿 §12 三张图，经 orca.chart render_chart 推送（无 HTML 产物）。

P7 重设计（2026-07-22）：
  原五张图里有大量噪声 / 误导根因。精简到三张，每张都有可读的语义：
    1. Champion Trace       —— line（候选灰点 + champion 轨迹，hue=series，已带 x/y label）
    2. Latency-Accuracy Pareto —— pareto（hue=status，min/max 方向）。
                                  **过滤 accuracy is None（FAIL_latency/FAIL_export 行）→
                                  之前 None 被前端渲染成 0 导致 y=0 根因**。
    3. Candidate Ledger     —— table（逐候选短字段：round/id/tag/latency_ms/
                                  accuracy/status/one_line_summary；长 hypothesis 留 hover）。

  删除（P7 根因清理）：
    - Round Ledger（每轮 1 候选无聚合价值，混淆 candidate 维度）
    - Exploration Tree（scatter 充数；path 恒 p1，零信息量）

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

# 同一群组 label：三张图共用，title 区分；同 title 再推 = 刷新。
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


# ── 三张图 → render_chart ────────────────────────────────────────────────────


def _push_champion_trace(
    ledger: list[dict[str, Any]],
    champions: list[dict[str, Any]],
    target_latency_ms: float,
) -> bool:
    """图1：候选灰点 + champion 轨迹（running min）。x=ledger 行序, y=latency_ms, hue=series。

    P1 已加 x_label/y_label/caption（候选序号 / 时延 ms / 图下说明）。
    """
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
            "latency_ms": lat,
            "series": "candidate",
            "candidate_id": str(e.get("id", "?")),
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
            "latency_ms": lat,
            "series": "champion",
            "candidate_id": str(cid),
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
        y="latency_ms",
        hue="series",
        x_label="候选序号（账本行）",
        y_label="时延 (ms)",
        caption=(
            f"每轮候选的实测时延（灰点）与 champion 轨迹（彩线，running min）。"
            f" 目标时延 {target_latency_ms} ms；★=达标。"
        ),
    )
    return True


def _push_pareto(
    ledger: list[dict[str, Any]],
    baseline_latency_ms: float,
    baseline_accuracy: float,
    accuracy_target: float,
) -> bool:
    """图2：latency(x) vs accuracy(y)，hue=status。pareto_x=min, pareto_y=max。

    P7 修复 y=0 根因：过滤 accuracy is None 行（FAIL_latency 候选 accuracy=-1 → None，
    之前被前端渲染成 0 导致 y=0）。FAIL_export 的 latency=-1 已在 lat<0 过滤剔除。
    """
    if len(ledger) < _MIN_ROWS:
        print(f"[viz_struct] WARN: 跳过 pareto：ledger 行数 {len(ledger)} < {_MIN_ROWS}", file=sys.stderr)
        return False

    data: list[dict[str, Any]] = []
    none_acc_skipped = 0
    for e in ledger:
        lat = _to_float(e.get("latency_ms"))
        if lat is None or lat < 0:
            continue
        acc = _to_float(e.get("accuracy"))
        if acc is None or acc < 0:
            # accuracy is None / 负数 = FAIL_latency（未训练）/ FAIL_export → 剔除（防 y=0 伪点）。
            none_acc_skipped += 1
            continue
        data.append({
            "latency_ms": lat,
            "accuracy": acc,
            "status": str(e.get("status", "?")),
            "candidate_id": str(e.get("id", "?")),
        })
    if none_acc_skipped:
        print(
            f"[viz_struct] WARN: pareto 剔除 {none_acc_skipped} 行 (accuracy 缺失/负数，"
            "多为 FAIL_latency/FAIL_export 未训练) 防 y=0 伪点",
            file=sys.stderr,
        )
    if not data:
        print("[viz_struct] WARN: 跳过 pareto：无有效 (latency, accuracy) 数据点", file=sys.stderr)
        return False

    _orca_render_chart(
        chart_type="pareto",
        data=data,
        label=_LABEL,
        title="Latency-Accuracy Pareto",
        x="latency_ms",
        y="accuracy",
        hue="status",
        pareto_x_direction="min",
        pareto_y_direction="max",
        x_label="时延 (ms，越低越好)",
        y_label="精度 (越高越好)",
        caption=(
            f"每候选实测 latency vs accuracy；Pareto 前沿（双最优）。"
            f" baseline={baseline_latency_ms} ms / acc={baseline_accuracy}；"
            f" 目标 acc≥{accuracy_target}。"
        ),
    )
    return True


def _push_candidate_table(ledger: list[dict[str, Any]]) -> bool:
    """图3：逐候选明细表（P7 精简）。

    列 = round / id / tag / latency_ms / accuracy / status / one_line_summary。
    - **短字段** 留 table 列；长 hypothesis 不直接展（前端 wrap 难读），放 hover 字段
      ``hypothesis``（前端 tooltip）。
    - one_line_summary = 取 hypothesis / diff_summary 首行 / 截断前 80 字符。
    """
    if len(ledger) < _MIN_ROWS:
        print(f"[viz_struct] WARN: 跳过 candidate_table：ledger 行数 {len(ledger)} < {_MIN_ROWS}", file=sys.stderr)
        return False

    def _one_line(s: str, n: int = 80) -> str:
        s = (s or "").strip()
        if not s:
            return ""
        first = s.splitlines()[0]
        return first if len(first) <= n else first[: n - 1] + "…"

    rows: list[dict[str, Any]] = []
    for e in ledger:
        rnd = e.get("round")
        if not isinstance(rnd, int):
            rnd = 0
        hypo = str(e.get("hypothesis") or e.get("diff_summary") or "")
        rows.append({
            "round": rnd,
            "id": str(e.get("id", "?")),
            "tag": str(e.get("tag", "")),
            "latency_ms": _to_float(e.get("latency_ms")),
            "accuracy": _to_float(e.get("accuracy")),
            "status": str(e.get("status", "?")),
            "one_line_summary": _one_line(hypo),
            # hover 字段：长 hypothesis 完整保留（前端 tooltip / expand 用）。
            "hypothesis": hypo,
            "candidate_id": str(e.get("id", "?")),
        })
    rows.sort(key=lambda r: r["round"])

    _orca_render_chart(
        chart_type="table",
        data=rows,
        label=_LABEL,
        title="Candidate Ledger (per change)",
        columns=["round", "id", "tag", "latency_ms", "accuracy", "status", "one_line_summary"],
        caption=(
            "每候选一行（短字段）。hypothesis 完整文本在 hover 字段；"
            "accuracy 为 None/-1 = 未训练（FAIL_latency/FAIL_export）。"
        ),
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
    """推三张图到 Orca Web 面板。返回 {chart: pushed_bool}。无 HTML 产物。"""
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
        description="草稿 §12 三张图（P7 精简自原五张；orca.chart render_chart 推送，无 HTML 产物）。"
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

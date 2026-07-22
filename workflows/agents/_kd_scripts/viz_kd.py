"""viz_kd.py —— kd-nas workflow 四张图（orca.chart render_chart 推送，无 HTML 产物）。

修复背景：原 ``viz_round`` 复用 ``viz_struct.py``，但 KD ledger schema
(``candidate_id/family/proxy_mse/db_gap/met_*/phase/...``) 与 viz_struct 要求的
``id/parent/path/status/accuracy`` 完全不匹配 → 每行被 ``_clean_ledger`` 剔除
→ 4 图全静默跳过。本脚本是 KD 专属，直接读 KD 账本真实字段。

契约（docs/plans/2026-07-21-workflow-viz-overhaul.md §2）：
  读 ledger.jsonl + champions.jsonl + teacher_meta.json，推四张图到 Orca Web：
    1. 候选轨迹         —— line（x=round, y=proxy_mse, hue=series candidate/champion）
    2. latency–proxy 帕累托 —— pareto（x=latency_ms, y=proxy_mse, 双 min, hue=met_latency）
    3. 逐轮汇总表       —— table（每行一个 ledger 条目，含失败行；change = family + build_cfg 摘要）
    4. 终态对比         —— bar × 2（仅 finalize：teacher vs champion vs final 的 latency + db_gap）

数据语义（必读）：
  - ``proxy_mse`` 是**短训精度代理**（soft-MSE-vs-teacher，KD 短训不跑 eval），
    不是真实精度；真实 dB gap 推迟到 finalize 阶段。图表 title/注释必须标注。
  - ``db_gap`` 在短训阶段为占位 0.0；finalize 行（champions.jsonl 或 --final_db_gap）才有真实值。
  - ``latency_ms`` 由 measure_student 真测 ONNX；失败候选（FAIL_export）为 -1。
  - ``proxy_mse < 0`` 表示失败候选（FAIL_train），坐标点剔除但其余图仍可用。

纪律（对齐 viz_struct.py）：
  - 仅用 ``orca.chart.render_chart``；不输出任何 HTML 文件。
  - 数据不足（< 2 有效行 / 必备字段缺失 / 无有效数据点）→ **该图跳过**（stderr WARN，
    不报错、不阻断）。
  - 同 label="kd-nas" 下每图用唯一 title；同 title 再推 = 刷新（实时更新语义）。
  - 不在 Orca 子进程内（无 ORCA_* env → import orca.chart 失败）→ 整体跳过，stderr 提示。
  - 确定性脚本：无 LLM、无网络、不读时钟、不读随机。
  - fail loud：仅 I/O 或参数硬错时非零退出；数据问题永远 exit 0。
  - 单图异常不影响其他图（sidecar 不阻断主循环）。

CLI：
    viz_kd.py --mode round|finalize \\
      --ledger <dir/ledger.jsonl> --champions <dir/champions.jsonl> \\
      --teacher_meta <dir/teacher_meta.json> \\
      [--final_latency_ms <f>] [--final_db_gap <f>]
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

# orca.chart web push（render_chart API）。不在 Orca 子进程内跑时为 None。
try:
    from orca.chart import render_chart as _orca_render_chart
except ImportError:
    _orca_render_chart = None


# ── 常量 ─────────────────────────────────────────────────────────────────────

# 数据不足的下限：少于这么多**有效行/点** → 该图跳过（sidecar 纪律）。
_MIN_ROWS = 2

# 同一群组 label：四张图共用，title 区分；同 title 再推 = 刷新。
_LABEL = "kd-nas"


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
                f"[viz_kd] WARN: {kind} {path} 第 {lineno} 行非合法 JSON，跳过该行：{e}",
                file=sys.stderr,
            )
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _is_candidate_row(e: dict[str, Any]) -> bool:
    """区分 KD ledger 的两种行类型。

    - 候选评估行：无 ``type`` 字段，有 ``candidate_id`` + 度量字段（latency_ms / proxy_mse / ...）。
    - 控制标记行：``type == "finalized_failed_mark"``（kd-curator §6），仅记录哪个 champion
      finalize 失败过，不含度量数据 → 从所有图表剔除（显示成 None 行会误导）。
    """
    t = e.get("type")
    if t is None:
        return True
    return t != "finalized_failed_mark"


def _read_json(path: str) -> dict[str, Any]:
    """读单个 JSON 文件。文件不存在 / 非法 JSON → 空字典（容错）。"""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[viz_kd] WARN: teacher_meta {path} 解析失败，视为空：{e}", file=sys.stderr)
        return {}
    return obj if isinstance(obj, dict) else {}


def _to_float(v: Any) -> float | None:
    """宽松转 float；None / bool / NaN / 非数字 → None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    return None


def _to_int(v: Any) -> int | None:
    """宽松转 int；非整数 → None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if not v.is_integer():
            return None
        return int(v)
    return None


def _build_cfg_summary(build_cfg_raw: Any, family: str) -> str:
    """``change`` 列：family + build_cfg 摘要。例 ``lmmse_front{blocks=2,k=3}``。

    build_cfg 是 JSON 串（或已解包 dict）。解析失败或非 dict → 只用 family。
    确定性：key 按字母序，value 用 ``str`` 短串；空 dict → 只用 family。
    """
    cfg: Any = build_cfg_raw
    if isinstance(cfg, str):
        s = cfg.strip()
        if not s:
            return str(family)
        try:
            cfg = json.loads(s)
        except json.JSONDecodeError:
            return str(family)
    if not isinstance(cfg, dict) or not cfg:
        return str(family)
    parts = [f"{k}={cfg[k]}" for k in sorted(cfg.keys())]
    return f"{family}{{{','.join(parts)}}}"


def _is_valid_point(lat: float | None, proxy: float | None) -> bool:
    """有效数据点判定：latency 与 proxy_mse 都解析成功且 ≥ 0。

    失败候选（FAIL_export → latency=-1 / FAIL_train → proxy_mse=-1）的点被坐标图剔除，
    但仍保留在汇总表里（原样显示 -1）。
    """
    return lat is not None and proxy is not None and lat >= 0 and proxy >= 0


# ── 四张图 → render_chart ────────────────────────────────────────────────────


def _push_candidate_trace(
    ledger: list[dict[str, Any]],
    champions: list[dict[str, Any]],
) -> bool:
    """图1：候选 + champion 轨迹。x=round, y=proxy_mse, hue=series。

    proxy_mse 越低越好。每个 ledger 有效行 = candidate 点；每个 champion 行 = champion 点。
    """
    data: list[dict[str, Any]] = []
    for e in ledger:
        rnd = _to_int(e.get("round"))
        proxy = _to_float(e.get("proxy_mse"))
        lat = _to_float(e.get("latency_ms"))
        if rnd is None or not _is_valid_point(lat, proxy):
            continue
        data.append({
            "round": rnd,
            "proxy_mse": proxy,
            "series": "candidate",
            "id": str(e.get("candidate_id", "?")),
            "family": str(e.get("family", "")),
        })
    for ch in champions:
        rnd = _to_int(ch.get("round"))
        proxy = _to_float(ch.get("proxy_mse"))
        if rnd is None or proxy is None or proxy < 0:
            continue
        data.append({
            "round": rnd,
            "proxy_mse": proxy,
            "series": "champion",
            "id": str(ch.get("champion_id", "?")),
            "family": str(ch.get("family", "")),
        })

    if len(data) < _MIN_ROWS:
        print(
            f"[viz_kd] WARN: 跳过 candidate_trace：有效点数 {len(data)} < {_MIN_ROWS}",
            file=sys.stderr,
        )
        return False

    _orca_render_chart(
        chart_type="line",
        data=data,
        label=_LABEL,
        title="Candidate Trace (proxy_mse, lower is better)",
        x="round",
        y="proxy_mse",
        hue="series",
        x_label="搜索轮次（round）",
        y_label="proxy_mse（短训代理，越低越好）",
        caption=(
            "proxy_mse = student vs teacher 的 soft-MSE，是短训精度代理，"
            "非真实精度（真实 dB gap 推迟 finalize）。champion 轨迹=每轮 ratchet 最优。"
        ),
    )
    return True


def _push_pareto(ledger: list[dict[str, Any]]) -> bool:
    """图2：latency(x) vs proxy_mse(y) 帕累托。双 min（时延低 & 短训精度代理低）。

    hue=met_latency 把达标/未达标的时延点区分开（KD 核心权衡：时延 vs 短训代理）。
    """
    data: list[dict[str, Any]] = []
    for e in ledger:
        lat = _to_float(e.get("latency_ms"))
        proxy = _to_float(e.get("proxy_mse"))
        if not _is_valid_point(lat, proxy):
            continue
        data.append({
            "latency_ms": lat,
            "proxy_mse": proxy,
            "met_latency": str(bool(e.get("met_latency"))),
            "id": str(e.get("candidate_id", "?")),
        })

    if len(data) < _MIN_ROWS:
        print(
            f"[viz_kd] WARN: 跳过 pareto：有效点数 {len(data)} < {_MIN_ROWS}",
            file=sys.stderr,
        )
        return False

    _orca_render_chart(
        chart_type="pareto",
        data=data,
        label=_LABEL,
        title="Latency–Proxy Pareto (both min)",
        x="latency_ms",
        y="proxy_mse",
        hue="met_latency",
        pareto_x_direction="min",
        pareto_y_direction="min",
        x_label="时延 ms（越低越好）",
        y_label="proxy_mse（短训代理，越低越好）",
        caption=(
            "双 min 帕累托：左下=又快又贴近 teacher。hue=met_latency 标时延达标与否。"
            "proxy_mse 非真实精度，仅短训排序用。"
        ),
    )
    return True


def _push_ledger_table(ledger: list[dict[str, Any]]) -> bool:
    """图3：逐轮汇总表。每个 ledger 条目一行（含失败行，-1 原样显示）。

    P7：短训阶段 db_gap / met_acc 为占位（curator 不消费，真实精度推迟到 finalize）→
    从默认列移除，避免前端展示误导（deferred 语义：真实值仅在 finalize 行有意义）。
    列 = [round, family, change, proxy_mse, latency_ms, met_lat, phase]。
    ``change`` = family + build_cfg 摘要；``proxy_mse`` 是短训精度代理（非真实精度）。
    db_gap / met_acc 仍在 row data 里（保留 hover / 后续若需可还原），但不进默认 columns。
    """
    if not ledger:
        print("[viz_kd] WARN: 跳过 ledger_table：ledger 为空", file=sys.stderr)
        return False

    rows: list[dict[str, Any]] = []
    for e in ledger:
        rnd = _to_int(e.get("round"))
        if rnd is None:
            # 无 round 的行无法在表里定位 → 剔除（仅 WARN，不影响其他图）。
            print(
                f"[viz_kd] WARN: ledger 行缺 round 字段（candidate_id="
                f"{e.get('candidate_id', '?')}），该行不入表",
                file=sys.stderr,
            )
            continue
        rows.append({
            "round": rnd,
            "family": str(e.get("family", "")),
            "change": _build_cfg_summary(e.get("build_cfg"), str(e.get("family", ""))),
            "proxy_mse": _to_float(e.get("proxy_mse")),
            "latency_ms": _to_float(e.get("latency_ms")),
            "met_lat": str(bool(e.get("met_latency"))),
            "phase": _to_int(e.get("phase")),
            # 短训占位字段（deferred）——保留在 row data 供 hover / finalize 行还原，
            # 但不进默认 columns（P7 root-cause 清理）。
            "db_gap": _to_float(e.get("db_gap")),
            "met_acc": str(bool(e.get("met_accuracy"))),
            "candidate_id": str(e.get("candidate_id", "?")),
        })

    if not rows:
        print("[viz_kd] WARN: 跳过 ledger_table：无有效 round 的行", file=sys.stderr)
        return False

    rows.sort(key=lambda r: (r["round"], r["family"]))

    _orca_render_chart(
        chart_type="table",
        data=rows,
        label=_LABEL,
        title="Candidate Ledger (proxy_mse = short-train acc proxy)",
        columns=[
            "round",
            "family",
            "change",
            "proxy_mse",
            "latency_ms",
            "met_lat",
            "phase",
        ],
        caption=(
            "短训阶段：proxy_mse 是 soft-MSE-vs-teacher 精度代理（非真实精度）。"
            " 真实 dB gap 推迟到 finalize 全量裁定（db_gap/met_acc 已从默认列移除，"
            "短训行为占位；hover 可查看原值）。"
        ),
    )
    return True


def _push_final_compare(
    teacher_meta: dict[str, Any],
    champions: list[dict[str, Any]],
    final_latency_ms: float | None,
    final_db_gap: float | None,
) -> bool:
    """图4：终态对比（仅 finalize）。推两张 bar：latency + db_gap。

    stage ∈ {teacher, champion, final}。teacher 的 db_gap = 0.0（baseline）。
    final 缺席（--final_* 未提供）→ 只画 teacher + champion。

    P7 根因修复（R4 + L7）：
    - **champion 短训阶段无真实 dB gap**（latency-first 哲学：真实精度推迟 finalize）→ champion 行
      不写 db_gap（viz_kd 之前 silently 漏掉 champion，让标题误导 "teacher vs champion vs final"
      实际只画 teacher+final）。现在显式：champion 不进 db_gap bar，title + caption 标明。
    - **teacher_accuracy_known=false**（teacher_setup.py 解析失败）→ teacher 的 dB gap=0.0 也是占位
      （teacher accuracy 本身未知），整个 db_gap bar 不可信 → caption 加警告。
    """
    # 真实 teacher_meta.json schema（teacher_setup.py 写盘）：字段名带 ``teacher_`` 前缀。
    teacher_lat = _to_float(teacher_meta.get("teacher_latency_ms"))
    teacher_acc = _to_float(teacher_meta.get("teacher_accuracy"))
    teacher_acc_known = teacher_meta.get("teacher_accuracy_known", True)
    if isinstance(teacher_acc_known, str):
        teacher_acc_known = teacher_acc_known.lower() == "true"
    teacher_acc_known = bool(teacher_acc_known)

    # 最后一个 champion（champions.jsonl 是追加写，末尾 = 最新 ratchet）。
    # P7：champion 行 schema 不含 db_gap（短训阶段未跑 eval）。
    last_ch = champions[-1] if champions else {}
    ch_lat = _to_float(last_ch.get("latency_ms"))

    latency_rows: list[dict[str, Any]] = []
    db_rows: list[dict[str, Any]] = []

    if teacher_lat is not None:
        latency_rows.append({"stage": "teacher", "latency_ms": teacher_lat})
    if ch_lat is not None:
        latency_rows.append({"stage": "champion", "latency_ms": ch_lat})
    if final_latency_ms is not None:
        latency_rows.append({"stage": "final", "latency_ms": final_latency_ms})

    # db_gap：teacher(=0 baseline) + final（champion 短训阶段无真实 dB gap，**不进图**）。
    # teacher_accuracy_known=false 时 teacher 自己的 0.0 也是占位 → 整图不可信。
    db_rows.append({"stage": "teacher", "db_gap": 0.0})
    if final_db_gap is not None:
        db_rows.append({"stage": "final", "db_gap": final_db_gap})

    pushed_any = False

    if len(latency_rows) >= _MIN_ROWS:
        _orca_render_chart(
            chart_type="bar",
            data=latency_rows,
            label=_LABEL,
            title="Final Latency Compare (teacher vs champion vs final)",
            x="stage",
            y="latency_ms",
            x_label="阶段",
            y_label="时延 (ms)",
            caption="teacher / champion（搜索中 ratchet）/ final（全量训练后）的实测时延。",
        )
        pushed_any = True
    else:
        print(
            f"[viz_kd] WARN: 跳过 final_latency bar：行数 {len(latency_rows)} < {_MIN_ROWS}",
            file=sys.stderr,
        )

    if len(db_rows) >= _MIN_ROWS:
        db_caption = (
            "teacher=0 baseline（teacher 是精度基准）；final=全量训练后真实 dB gap。"
            " champion 不进图（短训阶段无真实 dB gap，推迟 finalize）。"
        )
        if not teacher_acc_known:
            db_caption = (
                "⚠ teacher_accuracy 未知（teacher_setup 解析失败），dB gap 不可信。"
                + db_caption
            )
            print(
                "[viz_kd] WARN: teacher_accuracy_known=false，dB gap bar 不可信（caption 已标）",
                file=sys.stderr,
            )
        _orca_render_chart(
            chart_type="bar",
            data=db_rows,
            label=_LABEL,
            title="Final dB Gap Compare (teacher=0 baseline; champion deferred)",
            x="stage",
            y="db_gap",
            x_label="阶段",
            y_label="dB gap（越低越好）",
            caption=db_caption,
        )
        pushed_any = True
    else:
        print(
            f"[viz_kd] WARN: 跳过 final_db_gap bar：行数 {len(db_rows)} < {_MIN_ROWS}",
            file=sys.stderr,
        )

    # teacher_acc 仅作日志：目前不入图（bar 只对比 latency/db_gap 两维）。
    if teacher_acc is not None:
        print(f"[viz_kd] teacher accuracy (meta, 不入图) = {teacher_acc}", file=sys.stderr)

    return pushed_any


# ── 主入口 ─────────────────────────────────────────────────────────────────


def render_all(
    *,
    mode: str,
    ledger_path: str,
    champions_path: str,
    teacher_meta_path: str,
    final_latency_ms: float | None,
    final_db_gap: float | None,
) -> dict[str, Any]:
    """推图到 Orca Web 面板。返回 {mode, ledger_rows, champion_rows, charts}。

    - ``mode=round`` → 推候选轨迹 / 帕累托 / 汇总表三张。
    - ``mode=finalize`` → 同 round 三张 + 终态对比两张 bar。
    """
    if _orca_render_chart is None:
        print(
            "[viz_kd] WARN: orca.chart 不可用，跳过全部 web push（非 Orca 子进程？）",
            file=sys.stderr,
        )

    ledger_raw = _read_jsonl(ledger_path, kind="ledger")
    # 剔除 finalized_failed_mark 控制行（非候选评估，不含度量数据）。
    skipped_marks = sum(1 for e in ledger_raw if not _is_candidate_row(e))
    if skipped_marks:
        print(
            f"[viz_kd] ledger 含 {skipped_marks} 行控制标记（finalized_failed_mark），"
            "已从图表剔除",
            file=sys.stderr,
        )
    ledger = [e for e in ledger_raw if _is_candidate_row(e)]
    champions = _read_jsonl(champions_path, kind="champions")
    teacher_meta = _read_json(teacher_meta_path)

    results: dict[str, Any] = {
        "mode": mode,
        "ledger_rows": len(ledger),
        "ledger_control_rows_skipped": skipped_marks,
        "champion_rows": len(champions),
        "charts": {},
    }

    if _orca_render_chart is None:
        return results

    # round + finalize 共享的三张图。
    pushers: list[tuple[str, Any]] = [
        ("candidate_trace", lambda: _push_candidate_trace(ledger, champions)),
        ("pareto", lambda: _push_pareto(ledger)),
        ("ledger_table", lambda: _push_ledger_table(ledger)),
    ]
    if mode == "finalize":
        pushers.append(
            (
                "final_compare",
                lambda: _push_final_compare(
                    teacher_meta, champions, final_latency_ms, final_db_gap
                ),
            )
        )

    for name, fn in pushers:
        try:
            ok = fn()
        except Exception as e:  # 单图异常不影响其他图（sidecar 不阻断主循环）。
            print(
                f"[viz_kd] WARN: 推送 {name} 异常，跳过：{type(e).__name__}: {e}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            ok = False
        results["charts"][name] = {"pushed": bool(ok)}

    return results


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="kd-nas 四张图（orca.chart render_chart 推送，无 HTML 产物）。"
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["round", "finalize"],
        help="round = 推三张共享图；finalize = 再加终态对比 bar。",
    )
    parser.add_argument("--ledger", required=True, help="ledger.jsonl 路径")
    parser.add_argument("--champions", required=True, help="champions.jsonl 路径")
    parser.add_argument("--teacher_meta", required=True, help="teacher_meta.json 路径")
    parser.add_argument(
        "--final_latency_ms",
        type=float,
        required=False,
        default=None,
        help="finalize 阶段全量训练后的真实 latency（仅 finalize 用）",
    )
    parser.add_argument(
        "--final_db_gap",
        type=float,
        required=False,
        default=None,
        help="finalize 阶段全量训练后的真实 dB gap（仅 finalize 用）",
    )
    args = parser.parse_args()

    try:
        result = render_all(
            mode=args.mode,
            ledger_path=args.ledger,
            champions_path=args.champions,
            teacher_meta_path=args.teacher_meta,
            final_latency_ms=args.final_latency_ms,
            final_db_gap=args.final_db_gap,
        )
    except Exception as e:
        # 仅 I/O / 参数硬错 → 非零退出（fail loud）。数据不足已在内部 WARN 兜底。
        print(f"[viz_kd] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

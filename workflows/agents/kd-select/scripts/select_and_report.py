#!/usr/bin/env python3
"""select_and_report.py —— KD-NAS 第六步：脚本化 student 选择 + 最终报告（零 LLM）。

读 ``ledger.jsonl``（所有变体的 latency / accuracy / met_* / status），按**显式**
``accuracy_baseline_kind`` 判定 best 方向（kd_common.accuracy_direction，单一真相源），
挑最优 student + 列 latency-accuracy 帕累托前沿 + 模板填空 ``final_report.md``，推一张
``chart_type=pareto`` 前沿图（sidecar，失败不阻断）。

为什么零 LLM：选择是确定性逻辑（方向 + 排序 + 非支配），rule 5（deterministic 逻辑用代码）。
LLM 判断会引入「-20dB 误判比 -22dB 好」式的方向反转风险——本脚本经显式 kind 锁方向，杜绝之。

fail loud（hard 校验）：
  - ledger 读不了 / 空 / 非 list 行 → ``kd_common.read_ledger`` raise / 空判 → 写失败报告 +
    非零退出（不假装选完）。
  - ``accuracy_baseline_kind`` 未知方向 → fail loud（非零退出 + 报告标注）；**不** auto 猜方向。
  - 无达标 student → **不**假装选出；报告标「无 student 达标」，``N_SELECTED: 0``，正常退出（非错误）。

CLI::

    python3 select_and_report.py \\
      --ledger <ledger.jsonl> --kd_artifacts_dir <stable kd_artifacts_dir/> \\
      --accuracy_baseline <f> --accuracy_baseline_kind <nmse|mse|ber|db|snr|acc> \\
      --target_latency_ms <f> \\
      [--teacher_latency_ms <f>] [--baseline_latency_ms <f>] [--env_anchor <path>]

stdout::

    N_SELECTED: <int>          # 达标（met_latency ∧ met_accuracy）student 数
    ALL_VARIANTS_COUNT: <int>  # ledger 记录的变体总数
    BEST_VARIANT: <str>        # 达标项里精度最优 student 的 variant_id；无达标时空串
    PARETO_FRONT: <int>        # latency-accuracy 非支配前沿点数
    SELECTION_OK: <bool>       # N_SELECTED >= 1
    FINAL_REPORT: <path>       # final_report.md 绝对路径
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

# KD-NAS 共享 helper（read_ledger fail loud + accuracy_direction 单一真相源）。
# 本脚本是 folder-agent 自包含脚本，按相对路径注入 _kd_scripts 到 sys.path：
# scripts/ → kd-select/ → agents/，_kd_scripts 是 agents/ 的同级共享目录。
_KD_SCRIPTS = Path(__file__).resolve().parents[2] / "_kd_scripts"
if str(_KD_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_KD_SCRIPTS))
from kd_common import (  # noqa: E402
    accuracy_direction,
    is_measured_row,
    read_ledger,
    to_float,
)


# ── 数据筛选（纯函数，便于单测）─────────────────────────────────────────────────


def _measured_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """携带**真实** accuracy 测量且 latency/accuracy 数值合法的行。

    经 ``kd_common.is_measured_row`` 剔除哨兵行（``accuracy=0`` 但 ``accuracy_kind`` 为空的
    FAIL_latency / FAIL_train / measure-fail-FAIL_accuracy）——这些行在 min 方向 kind 下会以
    ``accuracy=0`` 虚假占据帕累托前沿（C1 防假）。再校验 latency(>=0) 与 accuracy 是合法数值。
    """
    out = []
    for r in rows:
        if not is_measured_row(r):
            continue
        lat = to_float(r.get("latency_ms_median"))
        acc = to_float(r.get("accuracy"))
        if lat is None or lat < 0 or acc is None:
            continue
        out.append(r)
    return out


def _qualified_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """达标行：met_latency ∧ met_accuracy（status=SUCCESS 等价集合，但以布尔字段为真相源）。"""
    return [r for r in rows if bool(r.get("met_latency")) and bool(r.get("met_accuracy"))]


def _best_student(qualified: list[dict[str, Any]], direction: str) -> dict[str, Any] | None:
    """达标行里精度最优者（方向显式：max→取最大 accuracy；min→取最小 accuracy）。

    平局（同精度）取 latency 更小的（成本更优）；仍平则取 variant_id 字典序最小（确定性）。
    用统一 ``min`` + 多键元组实现（同位元素同型，避免混型符号运算的 TypeError）。
    """
    if not qualified:
        return None

    def key(r: dict[str, Any]) -> tuple[float, float, str]:
        acc = to_float(r.get("accuracy"))
        acc = acc if acc is not None else 0.0
        lat = to_float(r.get("latency_ms_median"))
        lat = lat if lat is not None else float("inf")
        vid = str(r.get("variant_id", ""))
        # max 方向：-acc 使 min→最大 acc；lat 升序（越小越好）；vid 升序（确定性兜底）。
        # min 方向：acc 升序（越小越好）；lat 升序；vid 升序。
        return (-acc, lat, vid) if direction == "max" else (acc, lat, vid)

    return min(qualified, key=key)


def _pareto_front(measured: list[dict[str, Any]], acc_dir: str) -> list[int]:
    """latency(min) vs accuracy(direction) 非支配前沿的下标列表。

    统一转换成「两轴都越小越好」：latency 原样；accuracy 在 max 方向取负（-acc），min 方向原样。
    点 i 被支配 ⟺ 存在 j（j≠i）使 norm_j 两轴都 ≤ norm_i 且至少一轴 <。
    """
    pts = [(to_float(r.get("latency_ms_median")), to_float(r.get("accuracy"))) for r in measured]
    norm = [(x, -y if acc_dir == "max" else y) for x, y in pts]
    front = []
    for i in range(len(norm)):
        xi, yi = norm[i]
        dominated = False
        for j, (xj, yj) in enumerate(norm):
            if j == i:
                continue
            if xj <= xi and yj <= yi and (xj < xi or yj < yi):
                dominated = True
                break
        if not dominated:
            front.append(i)
    return front


# ── 报告生成（模板填空，零 LLM）────────────────────────────────────────────────


def _fmt_kind_note(kind: str) -> str:
    d = accuracy_direction(kind)
    if d == "max":
        return f"{kind}（越高越好，best=max）"
    if d == "min":
        return f"{kind}（越低越好，best=min）"
    return f"{kind or '未知'}（方向未知）"


def _build_report(
    *,
    rows: list[dict[str, Any]],
    measured: list[dict[str, Any]],
    qualified: list[dict[str, Any]],
    best: dict[str, Any] | None,
    front_idx: list[int],
    accuracy_baseline: float,
    acc_kind: str,
    target_latency_ms: float,
    teacher_latency_ms: float | None,
    baseline_latency_ms: float | None,
    error_reason: str,
) -> str:
    lines = ["# KD-NAS Final Report", ""]
    if error_reason:
        lines += [
            "## ⚠ Selection FAILED (fail loud)",
            "",
            f"原因：{error_reason}",
            "",
            "select 节点未选出任何 student（不假装完成）。请检查 ledger 是否完整、"
            "accuracy_baseline_kind 是否声明（nmse/mse/ber/db | snr/acc）。",
            "",
        ]
    lines += [
        f"- accuracy_baseline: `{accuracy_baseline}` （方向：{_fmt_kind_note(acc_kind)}）",
        f"- target_latency_ms: `{target_latency_ms}`",
        f"- variants in ledger: {len(rows)}（measured={len(measured)}, qualified={len(qualified)}）",
    ]
    if teacher_latency_ms is not None:
        lines.append(f"- teacher_latency_ms（参考，teacher 仅作 KD 软标签源）: `{teacher_latency_ms}`")
    if baseline_latency_ms is not None:
        lines.append(f"- baseline_latency_ms（flatten 原始模型）: `{baseline_latency_ms}`")

    lines += ["", "## Teacher vs Students", ""]
    if teacher_latency_ms is not None or baseline_latency_ms is not None:
        lines.append("| model | latency_ms | accuracy | met_acc |")
        lines.append("|---|---|---|---|")
        if baseline_latency_ms is not None:
            lines.append(f"| baseline (flatten) | {baseline_latency_ms:.4g} | — | — |")
        if teacher_latency_ms is not None:
            lines.append(f"| teacher (KD source) | {teacher_latency_ms:.4g} | — | — |")
        for r in measured:
            vid = str(r.get("variant_id", "?"))
            lat = to_float(r.get("latency_ms_median"))
            acc = to_float(r.get("accuracy"))
            met = str(bool(r.get("met_accuracy")))
            lines.append(f"| student {vid} | {lat:.4g} | {acc:.4g} | {met} |")
    else:
        lines.append("(teacher/baseline latency 未提供，仅列 students)")
        lines.append("")
        lines.append("| variant_id | latency_ms | accuracy | met_lat | met_acc | status |")
        lines.append("|---|---|---|---|---|---|")
        for r in measured:
            vid = str(r.get("variant_id", "?"))
            lat = to_float(r.get("latency_ms_median"))
            acc = to_float(r.get("accuracy"))
            lines.append(
                f"| {vid} | {lat:.4g} | {acc:.4g} | {bool(r.get('met_latency'))} | "
                f"{bool(r.get('met_accuracy'))} | {r.get('status', '')} |"
            )

    lines += ["", "## Selection", ""]
    if best is not None:
        vid = str(best.get("variant_id", "?"))
        acc = to_float(best.get("accuracy"))
        lat = to_float(best.get("latency_ms_median"))
        lines.append(f"- **最优 student：`{vid}`** — accuracy={acc:.4g}（{_fmt_kind_note(acc_kind)}），"
                     f"latency={lat:.4g}ms ≤ target {target_latency_ms}。")
        lines.append("- 选择依据：在达标（met_latency ∧ met_accuracy）的 student 中，按显式 kind "
                     f"方向取精度最优；平局取 latency 更小者（成本更优）。方向由 "
                     f"kd_common.accuracy_direction 判定（单一真相源，禁符号 auto 猜）。")
    else:
        lines.append("- **无 student 达标**（met_latency ∧ met_accuracy 全为 false）。")
        lines.append(f"- 已 measured 的 {len(measured)} 个变体均未同时满足 latency 与精度基线；"
                     "不假装选出。建议放宽 target_latency_ms / accuracy_baseline 或扩大变体池。")

    lines += ["", "## Latency-Accuracy Pareto Front", ""]
    if front_idx:
        lines.append("非支配前沿（latency 越小越好；accuracy 方向按 kind）：")
        for i in front_idx:
            r = measured[i]
            vid = str(r.get("variant_id", "?"))
            lat = to_float(r.get("latency_ms_median"))
            acc = to_float(r.get("accuracy"))
            mark = " ← selected" if (best is not None and str(r.get("variant_id")) ==
                                     str(best.get("variant_id"))) else ""
            lines.append(f"- `{vid}` latency={lat:.4g}ms, accuracy={acc:.4g}{mark}")
    else:
        lines.append("-（无 measured 点，无法算前沿）")

    return "\n".join(lines) + "\n"


# ── sidecar 推图（失败不阻断）──────────────────────────────────────────────────


def _push_pareto_chart(
    measured: list[dict[str, Any]], front_idx: list[int], acc_kind: str,
    acc_baseline: float, target_latency_ms: float, env_anchor: str,
) -> None:
    """推 latency-accuracy 帕累托前沿图（chart_type=pareto；sidecar，不 raise）。"""
    try:
        if env_anchor:
            try:
                from orca.chart._env import load_run_env_from_artifacts  # type: ignore
                load_run_env_from_artifacts(env_anchor)
            except Exception as e:  # noqa: BLE001
                print(f"[select_and_report] WARN: env_anchor 自举失败：{type(e).__name__}: {e}",
                      file=sys.stderr)
        from orca.chart import render_chart  # type: ignore
    except Exception as e:  # 非 Orca 子进程 → 跳过（不阻断选择）
        print(f"[select_and_report] WARN: orca.chart 不可用，跳过 pareto 推图："
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return
    pts = []
    for i, r in enumerate(measured):
        lat = to_float(r.get("latency_ms_median"))
        acc = to_float(r.get("accuracy"))
        if lat is None or acc is None:
            continue
        pts.append({"latency_ms": lat, "accuracy": acc,
                    "on_front": str(i in set(front_idx))})
    if len(pts) < 2:
        print(f"[select_and_report] WARN: 跳过 pareto 推图：有效点 {len(pts)} < 2", file=sys.stderr)
        return
    y_dir = accuracy_direction(acc_kind) or "min"
    d_phrase = "越高越好" if y_dir == "max" else "越低越好"
    try:
        render_chart(
            chart_type="pareto",
            data=pts,
            label="kd-nas",
            title="KD-NAS Final Pareto — latency vs accuracy",
            x="latency_ms",
            y="accuracy",
            pareto_x_direction="min",
            pareto_y_direction=y_dir,
            x_label="时延 ms（越小越好）",
            y_label=f"accuracy（{acc_kind or '未知'}，{d_phrase}）",
            caption=(
                "终态 latency-accuracy 非支配前沿。x=时延（成本，越小越好）；"
                f"y=accuracy（{d_phrase}，方向由 accuracy_baseline_kind={acc_kind!r} 显式锁定）。"
                f"参考：accuracy_baseline={acc_baseline}, target_latency_ms={target_latency_ms}。"
            ),
        )
    except Exception as e:  # sidecar：不阻断
        print(f"[select_and_report] WARN: render_chart 异常（不阻断）：{type(e).__name__}: {e}",
              file=sys.stderr)


# ── main ────────────────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description="KD-NAS 脚本化 student 选择 + 最终报告（零 LLM）")
    p.add_argument("--ledger", required=True, help="ledger.jsonl 路径（跨 run 真相源）")
    p.add_argument("--kd_artifacts_dir", required=True,
                   help="稳定 kd_artifacts_dir（写 final_report.md）")
    p.add_argument("--accuracy_baseline", required=True)
    p.add_argument("--accuracy_baseline_kind", required=True,
                   help="nmse/mse/ber/db(越低越好) | snr/acc(越高越好)；未知 → fail loud")
    p.add_argument("--target_latency_ms", required=True)
    p.add_argument("--teacher_latency_ms", default=None)
    p.add_argument("--baseline_latency_ms", default=None)
    p.add_argument("--env_anchor", default="", help="per-run $ORCA_ARTIFACTS_DIR（自举 ORCA env）")
    args = p.parse_args()

    accuracy_baseline = float(args.accuracy_baseline)
    target_latency_ms = float(args.target_latency_ms)
    teacher_latency_ms = float(args.teacher_latency_ms) if args.teacher_latency_ms else None
    baseline_latency_ms = float(args.baseline_latency_ms) if args.baseline_latency_ms else None

    report_dir = args.kd_artifacts_dir.rstrip(os.sep)
    os.makedirs(report_dir or ".", exist_ok=True)
    report_path = os.path.join(report_dir or ".", "final_report.md")

    error_reason = ""
    n_selected = 0
    best: dict[str, Any] | None = None
    front_idx: list[int] = []
    measured: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    # 1) kind 方向校验（fail loud：未知方向绝不 auto 猜）。
    direction = accuracy_direction(args.accuracy_baseline_kind)
    if not direction:
        error_reason = (f"accuracy_baseline_kind={args.accuracy_baseline_kind!r} 方向未知"
                        f"（合法：nmse/mse/ber/db | snr/acc）；select 不 auto 猜方向。")
        print(f"[select_and_report] FAIL: {error_reason}", file=sys.stderr)

    # 2) 读 ledger（fail loud：read_ledger 坏行 raise；空/缺 → 视为错误）。
    if not error_reason:
        try:
            rows = read_ledger(args.ledger)
        except (ValueError, OSError) as e:
            error_reason = f"ledger 读不了：{type(e).__name__}: {e}"
            print(f"[select_and_report] FAIL: {error_reason}", file=sys.stderr)
        else:
            if not rows:
                error_reason = f"ledger 为空或不存在：{args.ledger}（无变体可选）"
                print(f"[select_and_report] FAIL: {error_reason}", file=sys.stderr)

    # 3) 选择（仅在有 measured 行 + 已知方向时）。
    if not error_reason:
        measured = _measured_rows(rows)
        qualified = _qualified_rows(measured)
        best = _best_student(qualified, direction)
        n_selected = len(qualified)
        front_idx = _pareto_front(measured, direction)

    # 4) 写报告（即便失败也写一份，标注原因）。
    report = _build_report(
        rows=rows, measured=measured,
        qualified=[r for r in measured if bool(r.get("met_latency")) and bool(r.get("met_accuracy"))],
        best=best, front_idx=front_idx,
        accuracy_baseline=accuracy_baseline, acc_kind=args.accuracy_baseline_kind,
        target_latency_ms=target_latency_ms,
        teacher_latency_ms=teacher_latency_ms, baseline_latency_ms=baseline_latency_ms,
        error_reason=error_reason,
    )
    Path(report_path).write_text(report, encoding="utf-8")

    # 5) sidecar 推图（失败不阻断；仅在有数据时）。
    if measured and not error_reason:
        _push_pareto_chart(measured, front_idx, args.accuracy_baseline_kind,
                           accuracy_baseline, target_latency_ms, args.env_anchor)

    best_vid = str(best.get("variant_id", "")) if best else ""
    selection_ok = bool(best is not None)
    print(f"N_SELECTED: {n_selected}")
    print(f"ALL_VARIANTS_COUNT: {len(rows)}")
    print(f"BEST_VARIANT: {best_vid}")
    print(f"PARETO_FRONT: {len(front_idx)}")
    print(f"SELECTION_OK: {str(selection_ok).lower()}")
    print(f"FINAL_REPORT: {report_path}")
    return 2 if error_reason else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # 兜底 fail loud（不静默吞）
        print(f"[select_and_report] FAIL: 未捕获异常：{type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(2)

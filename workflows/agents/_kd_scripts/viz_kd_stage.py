"""viz_kd_stage.py —— KD-NAS 串行迭代**每节点 web 推送 sidecar**（SPEC §8）。

每个节点末尾以对应 ``--stage`` 调用本脚本，推**阶段汇总图**（与 metrics_tail.py 的
log-tail 推送互补——loss 曲线由 metrics_tail 摘 train log；本脚本只做静态阶段汇总）。

设计纪律（sidecar，与 viz_kd.py 同源）：
  - 仅用 ``orca.chart.render_chart``；不输出 HTML。
  - 数据不足（<必要点 / 必备字段缺失）→ 该图跳过（stderr WARN，不阻断）。
  - 同 ``label="kd-nas"`` 下每图唯一 title；同 title 再推 = 刷新。
  - 不在 Orca 子进程内（无 ORCA_* env）且 ``--env_anchor`` 自举失败 → 整体跳过 + stderr 提示。
  - 单图异常不影响其他图。
  - ``_main`` 兜底永远 emit 合法 JSON（agent dumb copy 进 viz_status，**必填字段**）。

阶段（``--stage``）→ 推图清单：
  baseline        : flatten — baseline latency bar。
  baseline_seed   : setup — baseline champion seed 表。
  teacher         : gen_teacher — teacher vs baseline latency bar。
  student         : gen_student — 每轮 hypothesis 表（含 status）。
  distill_table   : distill — 每轮 student latency/accuracy 表（含 met_*）。
  decide          : decide — champion 轨迹 line + 逐轮汇总表。
  final           : finalize — baseline/teacher/champion latency bar + 终态对比表。

CLI::
    viz_kd_stage.py --stage <name> \\
      [--ledger <path>] [--champions <path>] \\
      [--baseline_latency_ms <f>] [--baseline_accuracy <f>] \\
      [--target_latency_ms <f>] [--accuracy_baseline_kind <kind>] \\
      [--teacher_latency_ms <f>] [--champion_latency_ms <f>] [--champion_accuracy <f>] \\
      [--round_hypothesis '<json>'] \\
      [--env_anchor <path>]

stdout JSON（dumb copy 进 agent.viz_status）::
    {
      "viz_env_status": "ok|env_loaded_from_file|env_missing|import_failed|generic",
      "charts": {"<图名>": {"pushed": <bool>, "reason": "<str>"}, ...}
    }
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_LABEL = "kd-nas"
_orca_render_chart: Callable | None = None


# ── env bootstrap（viz_kd 同款）─────────────────────────────────────────────


def _bootstrap_render_chart(env_anchor: str) -> str:
    """惰性 import orca.chart.render_chart；env_anchor 自举 ORCA env。

    Returns: env_status code（ok / env_loaded_from_file / env_missing / import_failed / generic）。
    """
    global _orca_render_chart
    env_status = "env_missing"
    if env_anchor:
        try:
            from orca.chart._env import load_run_env_from_artifacts  # type: ignore
            load_run_env_from_artifacts(env_anchor)
            env_status = "env_loaded_from_file"
        except Exception as e:  # noqa: BLE001
            print(
                f"[viz_kd_stage] WARN: env_anchor 自举失败：{type(e).__name__}: {e}",
                file=sys.stderr,
            )
    else:
        # 没有 anchor：靠子进程已继承的 env（in-session spawn 路径）。
        env_status = "ok" if any(k.startswith("ORCA_") for k in __import__("os").environ) else "env_missing"
    try:
        from orca.chart import render_chart  # type: ignore
        _orca_render_chart = render_chart
        if env_status != "env_loaded_from_file":
            env_status = "ok" if _orca_render_chart is not None else "import_failed"
    except Exception as e:  # noqa: BLE001
        print(
            f"[viz_kd_stage] WARN: orca.chart import 失败：{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        env_status = "import_failed"
        _orca_render_chart = None
    return env_status


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    """读 jsonl（sidecar 容错：坏行 WARN 跳过，不 raise）。"""
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
                f"[viz_kd_stage] WARN: {path} 第 {lineno} 行非合法 JSON，跳过：{e}",
                file=sys.stderr,
            )
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _to_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f else f
    return None


# ── 单图推送 helpers ──────────────────────────────────────────────────────────


def _push_baseline_latency_bar(baseline_lat: float | None) -> tuple[bool, str]:
    if baseline_lat is None or baseline_lat < 0:
        return False, f"baseline_latency_ms 缺失/无效={baseline_lat!r}"
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="bar",
        data=[{"stage": "baseline", "latency_ms": baseline_lat}],
        label=_LABEL,
        title="Baseline Latency (flatten)",
        x="stage",
        y="latency_ms",
        x_label="阶段",
        y_label="时延 ms（越低越好）",
        caption=f"flatten __main__ 实测 baseline latency 中位数 = {baseline_lat:.4g}ms（用户 latency_provider 真测）。",
    )
    return True, "ok"


def _push_baseline_seed_table(baseline_lat: float | None, baseline_acc: float | None) -> tuple[bool, str]:
    if baseline_lat is None and baseline_acc is None:
        return False, "baseline_latency_ms / baseline_accuracy 全缺"
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="table",
        data=[{
            "round": 0,
            "id": "baseline",
            "latency_ms": baseline_lat if baseline_lat is not None else "",
            "accuracy": baseline_acc if baseline_acc is not None else "",
            "met_latency": "false",
            "met_accuracy": "false",
        }],
        label=_LABEL,
        title="Baseline Champion Seed (round=0)",
        columns=["round", "id", "latency_ms", "accuracy", "met_latency", "met_accuracy"],
        caption="setup 节点 seed 的 baseline champion（round=0，met_*=false，仅作 ratchet 起点）。",
    )
    return True, "ok"


def _push_teacher_vs_baseline_bar(
    baseline_lat: float | None, teacher_lat: float | None
) -> tuple[bool, str]:
    rows: list[dict[str, Any]] = []
    if baseline_lat is not None:
        rows.append({"stage": "baseline", "latency_ms": baseline_lat})
    if teacher_lat is not None:
        rows.append({"stage": "teacher", "latency_ms": teacher_lat})
    if len(rows) < 2:
        return False, f"teacher/baseline 行数 {len(rows)} < 2（缺 latency_ms）"
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="bar",
        data=rows,
        label=_LABEL,
        title="Teacher vs Baseline Latency",
        x="stage",
        y="latency_ms",
        x_label="阶段",
        y_label="时延 ms（越低越好）",
        caption="teacher-gen 派生 teacher（深×3/宽×2） vs baseline 实测 latency（用户 latency_provider 真测）。",
    )
    return True, "ok"


def _push_student_hypothesis_table(round_hyps: list[dict[str, Any]]) -> tuple[bool, str]:
    if not round_hyps:
        return False, "round_hypothesis 空"
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="table",
        data=round_hyps,
        label=_LABEL,
        title="Student Hypotheses per Round",
        columns=["round", "variant_id", "hypothesis", "direction_id", "status"],
        caption="gen_student 每轮结构假设（首轮固定规则缩1层+FFN→pointwise；迭代轮 KB+perf 驱动）。",
    )
    return True, "ok"


def _push_distill_round_table(ledger: list[dict[str, Any]]) -> tuple[bool, str]:
    rows: list[dict[str, Any]] = []
    for e in ledger:
        if e.get("round") in (None, 0):  # baseline seed skip
            continue
        rows.append({
            "round": e.get("round"),
            "variant_id": str(e.get("variant_id", "?")),
            "latency_ms": _to_float(e.get("latency_ms")),
            "accuracy": _to_float(e.get("accuracy")),
            "met_latency": str(bool(e.get("met_latency"))),
            "met_accuracy": str(bool(e.get("met_accuracy"))),
            "status": str(e.get("status", "")),
        })
    if not rows:
        return False, "ledger 无 student 行"
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="table",
        data=rows,
        label=_LABEL,
        title="Distill Results per Round",
        columns=["round", "variant_id", "latency_ms", "accuracy", "met_latency", "met_accuracy", "status"],
        caption="distill 每轮 student 蒸馏结果（latency 来自 tune_latency；accuracy 来自 train_pipeline --mode eval）。",
    )
    return True, "ok"


def _push_champion_trajectory(champions: list[dict[str, Any]]) -> tuple[bool, str]:
    """champions.jsonl 全量行 → champion 轨迹 line（latency + accuracy 双轴用两张）。"""
    pts: list[dict[str, Any]] = []
    for c in champions:
        lat = _to_float(c.get("latency_ms"))
        acc = _to_float(c.get("accuracy"))
        if lat is None:
            continue
        pts.append({
            "round": c.get("round", 0),
            "champion_latency_ms": lat,
            "champion_id": str(c.get("id", "?")),
            "champion_accuracy": acc if acc is not None else "",
        })
    if len(pts) < 1:
        return False, "champions 空"
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="line",
        data=pts,
        label=_LABEL,
        title="Champion Latency Trajectory",
        x="round",
        y="champion_latency_ms",
        x_label="round",
        y_label="时延 ms（越低越好）",
        caption="每轮 champion latency 轨迹（min-latency ratchet + FIFO tiebreak，SPEC §13）。",
    )
    return True, "ok"


def _push_champion_summary_table(champions: list[dict[str, Any]]) -> tuple[bool, str]:
    rows = []
    for c in champions:
        rows.append({
            "round": c.get("round", 0),
            "id": str(c.get("id", "?")),
            "latency_ms": _to_float(c.get("latency_ms")),
            "accuracy": _to_float(c.get("accuracy")),
            "delta_vs_baseline_ms": _to_float(c.get("delta_vs_baseline_ms")),
        })
    if not rows:
        return False, "champions 空"
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="table",
        data=rows,
        label=_LABEL,
        title="Champion Ratchet History",
        columns=["round", "id", "latency_ms", "accuracy", "delta_vs_baseline_ms"],
        caption="champions.jsonl 全量（每次 ratchet 追加；首行 round=0 = baseline seed）。",
    )
    return True, "ok"


def _push_final_compare_bar(
    baseline_lat: float | None,
    teacher_lat: float | None,
    champion_lat: float | None,
) -> tuple[bool, str]:
    rows: list[dict[str, Any]] = []
    if baseline_lat is not None:
        rows.append({"stage": "baseline", "latency_ms": baseline_lat})
    if teacher_lat is not None:
        rows.append({"stage": "teacher", "latency_ms": teacher_lat})
    if champion_lat is not None:
        rows.append({"stage": "champion", "latency_ms": champion_lat})
    if len(rows) < 2:
        return False, f"final compare 行数 {len(rows)} < 2"
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="bar",
        data=rows,
        label=_LABEL,
        title="Final Latency Compare",
        x="stage",
        y="latency_ms",
        x_label="阶段",
        y_label="时延 ms（越低越好）",
        caption="终态对比：baseline / teacher / champion。champion 来自 min-latency ratchet（SPEC §13）。",
    )
    return True, "ok"


# ── stage 派发 ───────────────────────────────────────────────────────────────


_STAGES: dict[str, str] = {
    "baseline": "baseline_latency_bar",
    "baseline_seed": "baseline_seed_table",
    "teacher": "teacher_vs_baseline_bar",
    "student": "student_hypothesis_table",
    "distill_table": "distill_round_table",
    "decide": "champion_trajectory",
    "final": "final_compare_bar",
}


def render_stage(
    *,
    stage: str,
    ledger_path: str,
    champions_path: str,
    baseline_latency_ms: float | None,
    baseline_accuracy: float | None,
    target_latency_ms: float | None,
    accuracy_baseline_kind: str,
    teacher_latency_ms: float | None,
    champion_latency_ms: float | None,
    champion_accuracy: float | None,
    round_hypothesis: str,
    env_anchor: str,
) -> dict[str, Any]:
    env_status = _bootstrap_render_chart(env_anchor)
    result: dict[str, Any] = {"viz_env_status": env_status, "charts": {}}
    if _orca_render_chart is None:
        print(
            "[viz_kd_stage] WARN: orca.chart 不可用，跳过全部 web push（非 Orca 子进程？）",
            file=sys.stderr,
        )
        return result

    # 通用 helper：跑一个 pusher + catch 异常 + 记 chart 状态。
    def _run(name: str, fn: Callable[[], tuple[bool, str]]) -> None:
        try:
            ok, reason = fn()
        except Exception as e:  # noqa: BLE001
            print(
                f"[viz_kd_stage] WARN: 推送 {name} 异常，跳过：{type(e).__name__}: {e}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            ok, reason = False, f"generic:{type(e).__name__}:{e}"
        result["charts"][name] = {"pushed": bool(ok), "reason": reason}

    if stage == "baseline":
        _run("baseline_latency_bar", lambda: _push_baseline_latency_bar(baseline_latency_ms))
    elif stage == "baseline_seed":
        _run(
            "baseline_seed_table",
            lambda: _push_baseline_seed_table(baseline_latency_ms, baseline_accuracy),
        )
    elif stage == "teacher":
        _run(
            "teacher_vs_baseline_bar",
            lambda: _push_teacher_vs_baseline_bar(baseline_latency_ms, teacher_latency_ms),
        )
    elif stage == "student":
        # round_hypothesis JSON: [{round, variant_id, hypothesis, direction_id, status}, ...]
        rh: list[dict[str, Any]] = []
        if round_hypothesis:
            try:
                parsed = json.loads(round_hypothesis)
                if isinstance(parsed, list):
                    rh = [x for x in parsed if isinstance(x, dict)]
            except json.JSONDecodeError as e:
                print(
                    f"[viz_kd_stage] WARN: --round_hypothesis 非合法 JSON：{e}",
                    file=sys.stderr,
                )
        _run("student_hypothesis_table", lambda: _push_student_hypothesis_table(rh))
    elif stage == "distill_table":
        ledger = _read_jsonl(ledger_path)
        _run("distill_round_table", lambda: _push_distill_round_table(ledger))
    elif stage == "decide":
        champions = _read_jsonl(champions_path)
        _run("champion_trajectory", lambda: _push_champion_trajectory(champions))
        _run("champion_summary_table", lambda: _push_champion_summary_table(champions))
    elif stage == "final":
        champions = _read_jsonl(champions_path)
        # champion latency 优先取 CLI 入参（finalize 实测算出）；缺则取 champions 最后一行。
        champ_lat = champion_latency_ms
        if champ_lat is None and champions:
            champ_lat = _to_float(champions[-1].get("latency_ms"))
        _run(
            "final_compare_bar",
            lambda: _push_final_compare_bar(baseline_latency_ms, teacher_latency_ms, champ_lat),
        )
        _run("champion_summary_table", lambda: _push_champion_summary_table(champions))
    else:
        print(f"[viz_kd_stage] WARN: 未知 stage={stage!r}", file=sys.stderr)
        result["charts"]["_unknown_stage"] = {"pushed": False, "reason": f"unknown stage: {stage!r}"}

    return result


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="KD-NAS 每节点 web 推送 sidecar（SPEC §8）"
    )
    parser.add_argument(
        "--stage",
        required=True,
        choices=sorted(_STAGES.keys()),
        help="阶段名（决定推哪些图）",
    )
    parser.add_argument("--ledger", default="", help="ledger.jsonl 路径（distill_table 用）")
    parser.add_argument("--champions", default="", help="champions.jsonl 路径（decide/final 用）")
    parser.add_argument("--baseline_latency_ms", type=float, default=None)
    parser.add_argument("--baseline_accuracy", type=float, default=None)
    parser.add_argument("--target_latency_ms", type=float, default=None)
    parser.add_argument("--accuracy_baseline_kind", default="")
    parser.add_argument("--teacher_latency_ms", type=float, default=None)
    parser.add_argument("--champion_latency_ms", type=float, default=None)
    parser.add_argument("--champion_accuracy", type=float, default=None)
    parser.add_argument(
        "--round_hypothesis",
        default="",
        help="JSON list：[{round, variant_id, hypothesis, direction_id, status}, ...]",
    )
    parser.add_argument("--env_anchor", default="", help="per-run $ORCA_ARTIFACTS_DIR 锚点")
    args = parser.parse_args()

    try:
        result = render_stage(
            stage=args.stage,
            ledger_path=args.ledger,
            champions_path=args.champions,
            baseline_latency_ms=args.baseline_latency_ms,
            baseline_accuracy=args.baseline_accuracy,
            target_latency_ms=args.target_latency_ms,
            accuracy_baseline_kind=args.accuracy_baseline_kind,
            teacher_latency_ms=args.teacher_latency_ms,
            champion_latency_ms=args.champion_latency_ms,
            champion_accuracy=args.champion_accuracy,
            round_hypothesis=args.round_hypothesis,
            env_anchor=args.env_anchor,
        )
    except Exception as e:  # noqa: BLE001
        # _main 兜底：永远 emit 合法 JSON（agent dumb copy 必填字段，缺 → output_schema fail）。
        print(f"[viz_kd_stage] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        result = {
            "viz_env_status": "generic",
            "charts": {
                "_stage_failed": {
                    "pushed": False,
                    "reason": f"generic:{type(e).__name__}:{e}",
                }
            },
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

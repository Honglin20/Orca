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
  final           : finalize — baseline/teacher/champion latency bar + 终态对比表
                     + all_models_table + pareto_front（latency×accuracy 非支配前沿）+
                     fail_status_bar（status 分布）。

CLI::
    viz_kd_stage.py --stage <name> \\
      [--ledger <path>] [--champions <path>] \\
      [--baseline_latency_us <f>] [--baseline_accuracy <f>] \\
      [--target_latency_us <f>] [--accuracy_baseline_kind <kind>] \\
      [--teacher_latency_us <f>] [--champion_latency_us <f>] [--champion_accuracy <f>] \\
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
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from kd_common import accuracy_direction, is_measured_row  # noqa: E402

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
        return False, f"baseline_latency_us 缺失/无效={baseline_lat!r}"
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="bar",
        data=[{"stage": "baseline", "latency_us": baseline_lat}],
        label=_LABEL,
        title="Baseline Latency (flatten)",
        x="stage",
        y="latency_us",
        x_label="阶段",
        y_label="时延 us（越低越好）",
        caption=f"flatten __main__ 实测 baseline latency 中位数 = {baseline_lat:.4g}us（用户 latency_provider 真测）。",
    )
    return True, "ok"


def _push_baseline_seed_table(baseline_lat: float | None, baseline_acc: float | None) -> tuple[bool, str]:
    if baseline_lat is None and baseline_acc is None:
        return False, "baseline_latency_us / baseline_accuracy 全缺"
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="table",
        data=[{
            "round": 0,
            "id": "baseline",
            "latency_us": baseline_lat if baseline_lat is not None else "",
            "accuracy": baseline_acc if baseline_acc is not None else "",
            "met_latency": "false",
            "met_accuracy": "false",
        }],
        label=_LABEL,
        title="Baseline Champion Seed (round=0)",
        columns=["round", "id", "latency_us", "accuracy", "met_latency", "met_accuracy"],
        caption="setup 节点 seed 的 baseline champion（round=0，met_*=false，仅作 ratchet 起点）。",
    )
    return True, "ok"


def _push_teacher_vs_baseline_bar(
    baseline_lat: float | None, teacher_lat: float | None
) -> tuple[bool, str]:
    rows: list[dict[str, Any]] = []
    if baseline_lat is not None:
        rows.append({"stage": "baseline", "latency_us": baseline_lat})
    if teacher_lat is not None:
        rows.append({"stage": "teacher", "latency_us": teacher_lat})
    if len(rows) < 2:
        return False, f"teacher/baseline 行数 {len(rows)} < 2（缺 latency_us）"
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="bar",
        data=rows,
        label=_LABEL,
        title="Teacher vs Baseline Latency",
        x="stage",
        y="latency_us",
        x_label="阶段",
        y_label="时延 us（越低越好）",
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
            "latency_us": _to_float(e.get("latency_us")),
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
        columns=["round", "variant_id", "latency_us", "accuracy", "met_latency", "met_accuracy", "status"],
        caption="distill 每轮 student 蒸馏结果（latency 来自 tune_latency；accuracy 来自 train_pipeline --mode eval）。",
    )
    return True, "ok"


def _push_champion_trajectory(champions: list[dict[str, Any]]) -> tuple[bool, str]:
    """champions.jsonl 全量行 → champion 轨迹 line（latency + accuracy 双轴用两张）。"""
    pts: list[dict[str, Any]] = []
    for c in champions:
        lat = _to_float(c.get("latency_us"))
        acc = _to_float(c.get("accuracy"))
        if lat is None:
            continue
        pts.append({
            "round": c.get("round", 0),
            "champion_latency_us": lat,
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
        y="champion_latency_us",
        x_label="round",
        y_label="时延 us（越低越好）",
        caption="每轮 champion latency 轨迹（min-latency ratchet + FIFO tiebreak，SPEC §13）。",
    )
    return True, "ok"


def _push_champion_summary_table(champions: list[dict[str, Any]]) -> tuple[bool, str]:
    rows = []
    for c in champions:
        rows.append({
            "round": c.get("round", 0),
            "id": str(c.get("id", "?")),
            "latency_us": _to_float(c.get("latency_us")),
            "accuracy": _to_float(c.get("accuracy")),
            "delta_vs_baseline_us": _to_float(c.get("delta_vs_baseline_us")),
        })
    if not rows:
        return False, "champions 空"
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="table",
        data=rows,
        label=_LABEL,
        title="Champion Ratchet History",
        columns=["round", "id", "latency_us", "accuracy", "delta_vs_baseline_us"],
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
        rows.append({"stage": "baseline", "latency_us": baseline_lat})
    if teacher_lat is not None:
        rows.append({"stage": "teacher", "latency_us": teacher_lat})
    if champion_lat is not None:
        rows.append({"stage": "champion", "latency_us": champion_lat})
    if len(rows) < 2:
        return False, f"final compare 行数 {len(rows)} < 2"
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="bar",
        data=rows,
        label=_LABEL,
        title="Final Latency Compare",
        x="stage",
        y="latency_us",
        x_label="阶段",
        y_label="时延 us（越低越好）",
        caption="终态对比：baseline / teacher / champion。champion 来自 min-latency ratchet（SPEC §13）。",
    )
    return True, "ok"


def _push_all_models_table(
    ledger: list[dict[str, Any]],
    champions: list[dict[str, Any]],
    baseline_latency_us: float | None,
    baseline_accuracy: float | None,
    teacher: dict[str, Any] | None,
) -> tuple[bool, str]:
    """全模型总表：baseline + teacher + 全部 student variant + 每轮 champion，各带 latency+accuracy。

    数据源（只读）：ledger.jsonl（student 行）+ champions.jsonl（round=0 = baseline seed）+
    teacher_meta.json（teacher 行）。一行一架构，列：
    round / id / role / latency_us / accuracy / met_latency / met_accuracy / status。
    role ∈ {baseline, teacher, champion, student}。
    """
    rows: list[dict[str, Any]] = []

    # baseline 行：优先 champions[0]（round=0 seed），缺则 CLI fallback。
    base = champions[0] if champions and champions[0].get("round") == 0 else None
    b_lat = _to_float((base or {}).get("latency_us")) if base else baseline_latency_us
    if b_lat is None:
        b_lat = baseline_latency_us
    b_acc = _to_float((base or {}).get("accuracy")) if base else baseline_accuracy
    if b_acc is None:
        b_acc = baseline_accuracy
    if b_lat is not None or b_acc is not None:
        rows.append({
            "round": 0,
            "id": "baseline",
            "role": "baseline",
            "latency_us": b_lat if b_lat is not None else "",
            "accuracy": b_acc if b_acc is not None else "",
            # baseline 是 latency/accuracy 参考线本身，不参与达标判定（与 teacher 行一致）。
            "met_latency": "",
            "met_accuracy": "",
            "status": "baseline",
        })

    # teacher 行：teacher_meta.json（latency + 真实 eval accuracy）。
    if teacher:
        t_lat = _to_float(teacher.get("teacher_latency_us"))
        t_acc = _to_float(teacher.get("teacher_accuracy"))
        known = bool(teacher.get("teacher_accuracy_known", False))
        rows.append({
            "round": "",
            "id": "teacher",
            "role": "teacher",
            "latency_us": t_lat if t_lat is not None else "",
            "accuracy": t_acc if t_acc is not None else "",
            "met_latency": "",   # teacher 不卡 target，仅作 KD 源
            "met_accuracy": "",
            "status": "teacher" if known else "teacher(unknown acc)",
        })

    # champion id 集合（非 baseline）→ student 行命中即标 role=champion。
    champ_ids = {
        str(c.get("id")) for c in champions
        if c.get("round") != 0 and c.get("id") and c.get("id") != "baseline"
    }

    # student 行：ledger 全量（含 FAIL_*，诚实呈现）。
    # latency 字段：gate_all 写 latency_us_median（onnx 实测中位数）；reducer 归一化别名 latency_us。
    for e in ledger:
        if e.get("round") in (None, 0):  # baseline seed / 无 round 行不重复（与 _push_distill_round_table 对齐）
            continue
        vid = str(e.get("variant_id", "?"))
        lat = _to_float(e.get("latency_us_median"))
        if lat is None:
            lat = _to_float(e.get("latency_us"))
        acc = _to_float(e.get("accuracy"))
        rows.append({
            "round": e.get("round", ""),
            "id": vid,
            "role": "champion" if vid in champ_ids else "student",
            "latency_us": lat if lat is not None else "",
            "accuracy": acc if acc is not None else "",
            "met_latency": str(bool(e.get("met_latency"))) if e.get("met_latency") is not None else "",
            "met_accuracy": str(bool(e.get("met_accuracy"))) if e.get("met_accuracy") is not None else "",
            "status": str(e.get("status", "")),
        })

    if not rows:
        return False, "无 baseline/teacher/student 行（ledger+champions+teacher 全空）"
    assert _orca_render_chart is not None
    n_base = sum(1 for r in rows if r["role"] == "baseline")
    n_teacher = sum(1 for r in rows if r["role"] == "teacher")
    n_student = sum(1 for r in rows if r["role"] in ("student", "champion"))
    _orca_render_chart(
        chart_type="table",
        data=rows,
        label=_LABEL,
        title="All Models (accuracy × latency)",
        columns=["round", "id", "role", "latency_us", "accuracy",
                 "met_latency", "met_accuracy", "status"],
        caption=(
            f"全架构总表：baseline({n_base}) + teacher({n_teacher}) + "
            f"students/champions({n_student})。latency 单位 us；accuracy 来自 "
            f"train_pipeline --mode eval（非 training loss）。"
        ),
    )
    return True, "ok"


def _push_pareto_front(
    ledger: list[dict[str, Any]],
    baseline_latency_us: float | None,
    baseline_accuracy: float | None,
    accuracy_baseline_kind: str,
) -> tuple[bool, str]:
    """终态帕累托前沿：latency(x, min) vs accuracy(y, max after display 变换)。

    Port viz_kd._push_pareto 语义（非 import——为 §2 followup 删 viz_kd 铺路，重实现在 stage sidecar）。
    方向门 + display 变换 + sentinel 过滤三不变量经 ``kd_common``（DRY，单一真相源）。

    - 有效点：``is_measured_row``（SUCCESS / FAIL_accuracy+accuracy_kind 非空）∧ latency 非None≥0 ∧
      accuracy 非 None。**不**按值 ``!= -1`` 过滤（db-kind 下 -1.0 dB 是合法真测；sentinel 已由
      ``is_measured_row`` 经 accuracy_kind 非空门覆盖——FAIL_latency/FAIL_train 的 accuracy_kind 空
      → 自动剔除）。FAIL_accuracy+accuracy_kind 非空 = 真测值，**计入**前沿（与 viz_kd 一致）。
    - 方向门：``accuracy_direction(kind)``；空串（unknown kind）→ WARN-skip（**不** auto 猜方向，
      防 -20dB/-22dB 反转）。display 变换：min 方向 → y 取负（误差型越低越好 → 轴上越大越好统一）。
    - chart_type=pareto + pareto_x_direction=min（latency 恒为成本）+ pareto_y_direction=max
      （display 后数据恒「越大越好」）。
    - latency 字段双 fallback：``latency_us_median`` → ``latency_us``（与 ``_push_all_models_table``
      一致；reducer 归一化别名 latency_us 兼容）。
    - hue：student 行 ``str(bool(met_accuracy))``（"True"/"False"）；baseline 参考点 ``met_accuracy="ref"``
      （与 viz_kd ``_push_accuracy_compare`` 的 "ref" 约定一致，避免空串成第三类目）。
    """
    direction = accuracy_direction(accuracy_baseline_kind)
    if not direction:
        print(
            f"[viz_kd_stage] WARN: 跳过 pareto_front：accuracy_baseline_kind="
            f"{accuracy_baseline_kind!r} 方向未知（请声明 nmse/mse/ber/db | snr/acc）",
            file=sys.stderr,
        )
        return False, f"unknown kind: {accuracy_baseline_kind!r}"
    pts: list[dict[str, Any]] = []
    for e in ledger:
        if e.get("round") in (None, 0):  # baseline seed 行不参与
            continue
        if not is_measured_row(e):
            continue
        lat = _to_float(e.get("latency_us_median"))
        if lat is None:
            lat = _to_float(e.get("latency_us"))
        acc = _to_float(e.get("accuracy"))
        if lat is None or lat < 0 or acc is None:
            continue
        disp = -acc if direction == "min" else acc
        pts.append({
            "latency_us": lat,
            "accuracy": disp,
            "met_accuracy": str(bool(e.get("met_accuracy"))),
        })
    if len(pts) < 2:
        print(
            f"[viz_kd_stage] WARN: 跳过 pareto_front：有效点 {len(pts)} < 2",
            file=sys.stderr,
        )
        return False, f"有效点 {len(pts)} < 2"
    # baseline 参考点（latency×accuracy 同 dict 结构，hue="ref"）——让前端在前沿图绘参考标记。
    if baseline_latency_us is not None and baseline_accuracy is not None:
        b_disp = -baseline_accuracy if direction == "min" else baseline_accuracy
        pts.append({
            "latency_us": baseline_latency_us,
            "accuracy": b_disp,
            "met_accuracy": "ref",
        })
    assert _orca_render_chart is not None
    y_label = (
        "显示 -原值，越大越好（原指标越低越好）" if direction == "min" else "越高越好"
    )
    _orca_render_chart(
        chart_type="pareto",
        data=pts,
        label=_LABEL,
        title="Pareto Front — latency vs accuracy",
        x="latency_us",
        y="accuracy",
        pareto_x_direction="min",
        pareto_y_direction="max",
        x_label="时延 us（越小越好）",
        y_label=f"accuracy（{accuracy_baseline_kind}，{y_label}）",
        hue="met_accuracy",
        caption=(
            "latency-accuracy 非支配前沿（前端据 direction 自绘）。x=时延（成本，越小越好）；"
            f"y=accuracy（{accuracy_baseline_kind}，{'min 方向已取负显示使轴上越大越好' if direction == 'min' else '原值越大越好'}）。"
            "hue=met_accuracy 标命中用户精度基线；baseline 行 met_accuracy=ref 作参考点。"
            f"方向由 accuracy_baseline_kind={accuracy_baseline_kind!r} 锁定。"
        ),
    )
    return True, "ok"


def _push_fail_status_bar(ledger: list[dict[str, Any]]) -> tuple[bool, str]:
    """终态 status 分布 bar：SUCCESS / FAIL_latency / FAIL_train / FAIL_build /
    FAIL_accuracy / FAIL_export / 其它 计数。

    与 viz_kd._push_progress 同口径（status 分组），但标题/语义聚焦「distill outcome 分布」
    （终态看搜索卡哪）。固定 status 序 + 杂项兜底「其它」。空 ledger → WARN 跳过。
    """
    if not ledger:
        print(
            "[viz_kd_stage] WARN: 跳过 fail_status_bar：ledger 为空",
            file=sys.stderr,
        )
        return False, "ledger 空"
    counts: dict[str, int] = {}
    for e in ledger:
        if e.get("round") in (None, 0):  # baseline seed 不计
            continue
        st = str(e.get("status") or "其它")
        counts[st] = counts.get(st, 0) + 1
    if not counts:
        return False, "无 student 行"
    order = ["SUCCESS", "FAIL_latency", "FAIL_train", "FAIL_build",
             "FAIL_accuracy", "FAIL_export"]
    known = {s: counts.get(s, 0) for s in order}
    others = {k: v for k, v in counts.items() if k not in order and v > 0}
    # 过滤零计数行（未见 status 不渲染空柱；固定项中 SUCCESS 等 0 计数保留是注释承诺的「一眼全貌」，
    # 但 FAIL_build/FAIL_export 等少见项 0 计数画空柱会污染视觉——这里全部 0 都过滤，简洁优先）。
    rows: list[dict[str, Any]] = [
        {"status": k, "count": v} for k, v in known.items() if v > 0
    ]
    if others:
        rows.append({"status": "其它", "count": sum(others.values())})
    assert _orca_render_chart is not None
    _orca_render_chart(
        chart_type="bar",
        data=rows,
        label=_LABEL,
        title="Distill Outcome (status counts)",
        x="status",
        y="count",
        x_label="变体终态",
        y_label="变体数",
        caption=(
            "搜索结果分布：SUCCESS=达标；FAIL_latency=tune 不过；FAIL_train=训练/eval 崩；"
            "FAIL_build=validate_contract 3 strikes；FAIL_accuracy=训练完但精度未达；"
            "FAIL_export=ONNX 导出失败；其它=未知 status（须诊断）。"
        ),
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
    baseline_latency_us: float | None,
    baseline_accuracy: float | None,
    target_latency_us: float | None,
    accuracy_baseline_kind: str,
    teacher_latency_us: float | None,
    champion_latency_us: float | None,
    champion_accuracy: float | None,
    teacher_meta_path: str = "",
    round_hypothesis: str = "",
    env_anchor: str = "",
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
        _run("baseline_latency_bar", lambda: _push_baseline_latency_bar(baseline_latency_us))
    elif stage == "baseline_seed":
        _run(
            "baseline_seed_table",
            lambda: _push_baseline_seed_table(baseline_latency_us, baseline_accuracy),
        )
    elif stage == "teacher":
        _run(
            "teacher_vs_baseline_bar",
            lambda: _push_teacher_vs_baseline_bar(baseline_latency_us, teacher_latency_us),
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
        ledger = _read_jsonl(ledger_path)
        # champion latency 优先取 CLI 入参（finalize 实测算出）；缺则取 champions 最后一行。
        champ_lat = champion_latency_us
        if champ_lat is None and champions:
            champ_lat = _to_float(champions[-1].get("latency_us"))
        _run(
            "final_compare_bar",
            lambda: _push_final_compare_bar(baseline_latency_us, teacher_latency_us, champ_lat),
        )
        _run("champion_summary_table", lambda: _push_champion_summary_table(champions))
        # 全模型总表：baseline + teacher + students + champions（读 teacher_meta.json）。
        teacher_obj: dict[str, Any] | None = None
        if teacher_meta_path and os.path.isfile(teacher_meta_path):
            try:
                teacher_obj = json.loads(Path(teacher_meta_path).read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                print(f"[viz_kd_stage] WARN: teacher_meta 不可读：{e}", file=sys.stderr)
        _run("all_models_table", lambda: _push_all_models_table(
            ledger, champions, baseline_latency_us, baseline_accuracy, teacher_obj,
        ))
        # 终态帕累托前沿 + FAIL 分布（SPEC §3）：补 viz_kd 的核心语义到 active 串行 sidecar。
        # 需 accuracy_baseline_kind 显式声明方向；ledger 已读，复用。
        _run("pareto_front", lambda: _push_pareto_front(
            ledger, baseline_latency_us, baseline_accuracy, accuracy_baseline_kind,
        ))
        _run("fail_status_bar", lambda: _push_fail_status_bar(ledger))
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
    parser.add_argument("--baseline_latency_us", type=float, default=None)
    parser.add_argument("--baseline_accuracy", type=float, default=None)
    parser.add_argument("--target_latency_us", type=float, default=None)
    parser.add_argument("--accuracy_baseline_kind", default="")
    parser.add_argument("--teacher_latency_us", type=float, default=None)
    parser.add_argument("--champion_latency_us", type=float, default=None)
    parser.add_argument("--champion_accuracy", type=float, default=None)
    parser.add_argument(
        "--teacher_meta", default="",
        help="teacher_meta.json 路径（final 全模型总表读 teacher latency+accuracy）",
    )
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
            baseline_latency_us=args.baseline_latency_us,
            baseline_accuracy=args.baseline_accuracy,
            target_latency_us=args.target_latency_us,
            accuracy_baseline_kind=args.accuracy_baseline_kind,
            teacher_latency_us=args.teacher_latency_us,
            champion_latency_us=args.champion_latency_us,
            champion_accuracy=args.champion_accuracy,
            teacher_meta_path=args.teacher_meta,
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

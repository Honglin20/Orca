"""viz_kd.py —— KD-NAS 蒸馏 sweep 可视化（orca.chart render_chart 推送，无 HTML 产物）。

# ⚠️ DEPRECATED —— 旧并行 sweep 路径，活跃串行 kd-nas.yaml 不调用；保留供历史测试，删除见 followup SPEC。

读 ``ledger.jsonl``（变体行：variant_id/accepted_cfg/latency_us_median/
latency_us_std/accuracy/met_latency/met_accuracy/status/...），推：
  1. Distill Sweep 散点 —— latency_us_median(x) vs accuracy(y)，hue=met_accuracy。
     caption 标 baseline_latency 参考线 + target_latency 阈值 + accuracy_baseline 基线（sweep 单图）。
  2. Candidate Ledger 表 —— variant_id/status/latency/accuracy/met_lat/met_acc/cfg。
  3. Latency Compare bar —— baseline / target / 各变体 latency。
  4. Sweep Progress —— status 计数（SUCCESS/FAIL_accuracy/FAIL_train/FAIL_latency）+ n_done/n_total。
  5. Pareto Front —— latency(x,min) vs accuracy(y,方向按 kind)；chart_type=pareto，前端绘前沿。
  6. Accuracy Compare bar —— 各变体 accuracy + accuracy_baseline 参考线；方向按 kind（越低/越高越好）。

指标方向：单一真相源 = ``kd_common.accuracy_direction``。
  - kind ∈ {nmse, mse, ber, db}（越低越好，best=min）
  - kind ∈ {snr, acc}（越高越好，best=max）
  - kind 未声明 → accuracy 坐标图（scatter/pareto/accuracy_compare）fail loud WARN 跳过，**不 auto 猜**
    （取负显示需已知方向；防 -20dB 误判优于 -22dB 的反转）。
取负显示：min 方向 kind 的 accuracy 坐标图对 y 值取负（``_acc_display``），使「轴上
越大越好」统一——防 bar/scatter 图 -20dB 视觉高于 -22dB 的强误导（goal：坐标轴方向不能让人误判）。
**display 变换**对齐 ``nas-train-runner/scripts/tail_metrics.py`` 的 ``disp = -v if quality``；但 kind
检测相反——tail_metrics 按符号 auto-guess，viz_kd 要求显式 kind（禁 auto 猜）。pareto_y_direction
随之恒为 ``'max'``（displayed 数据越大越好；min 取负后与原 raw+min 前沿等价）。
viz_kd 不判门（不 kill 也不 fail），只标方向；方向门禁在 measure_student / select。

数据语义：
  - ``latency_us_median`` 由 tune_latency 真测（用户 latency 脚本，median+std）。
  - ``accuracy`` 由 measure_student 测（绝对值），``met_accuracy`` 对比用户精度基线。
  - 失败行（status=FAIL_*）latency/accuracy 可能 -1 → 坐标图剔除、表中原样显示。

纪律（sidecar）：
  - 仅用 ``orca.chart.render_chart``；不输出 HTML。
  - 数据不足（<2 有效点 / 必备字段缺失）→ 该图跳过（stderr WARN，不阻断）。
  - 同 label="kd-nas" 下每图唯一 title；同 title 再推 = 刷新。
  - 不在 Orca 子进程内（无 ORCA_* env）且 ``--env_anchor`` 自举失败 → 整体跳过 + stderr 提示。
  - 单图异常不影响其他图（sidecar 不阻断主循环）。

CLI：
    viz_kd.py --ledger <ledger.jsonl> \\
      [--baseline_latency_us <f>] [--target_latency_us <f>] [--variants_total <int>] \\
      [--accuracy_baseline <f>] [--accuracy_baseline_kind <s>] [--env_anchor <path>]
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from kd_common import accuracy_direction, is_measured_row, to_float  # noqa: E402

_LABEL = "kd-nas"
_MIN_POINTS = 2

# orca.chart web push。先尝试 env_anchor 自举（防 ledger 在稳定根、向上搜不到 orca_env.sh）。
_orca_render_chart = None


def _bootstrap_render_chart(env_anchor: str) -> None:
    """惰性 import orca.chart.render_chart；env_anchor 非空则先自举 ORCA env。"""
    global _orca_render_chart
    if env_anchor:
        try:
            from orca.chart._env import load_run_env_from_artifacts  # type: ignore
            load_run_env_from_artifacts(env_anchor)
        except Exception as e:  # noqa: BLE001
            print(f"[viz_kd] WARN: env_anchor 自举失败：{type(e).__name__}: {e}", file=sys.stderr)
    try:
        from orca.chart import render_chart  # type: ignore
        _orca_render_chart = render_chart
    except Exception:
        _orca_render_chart = None


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
            print(f"[viz_kd] WARN: {path} 第 {lineno} 行非合法 JSON，跳过：{e}", file=sys.stderr)
            continue
        if not isinstance(obj, dict):
            # ledger 行应为 dict；非 dict（list/scalar）属异常，sidecar 容错跳过但大声 WARN 便于诊断。
            print(f"[viz_kd] WARN: {path} 第 {lineno} 行非 dict（{type(obj).__name__}），跳过",
                  file=sys.stderr)
            continue
        out.append(obj)
    return out


def _acc_point(row: dict[str, Any]) -> tuple[float, float] | None:
    """从 ledger 行提取 ``(latency, accuracy)`` 坐标，仅对**真实测量**行返回非 None。

    经 ``kd_common.is_measured_row`` 剔除 ``accuracy=0`` 哨兵行（FAIL_latency / FAIL_train /
    measure-fail-FAIL_accuracy）——这些行在 min 方向 kind 下会以 ``accuracy=0`` 虚假占据帕累托前沿
    / accuracy 轴最优位（C1 防假，与 select_and_report._measured_rows 同源）。
    """
    if not is_measured_row(row):
        return None
    lat = to_float(row.get("latency_us_median"))
    acc = to_float(row.get("accuracy"))
    if lat is None or lat < 0 or acc is None:
        return None
    return lat, acc


def _cfg_summary(cfg_raw: Any, variant_id: str) -> str:
    cfg = cfg_raw
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except json.JSONDecodeError:
            return str(variant_id)
    if not isinstance(cfg, dict) or not cfg:
        return str(variant_id)
    parts = [f"{k}={cfg[k]}" for k in sorted(cfg.keys())]
    return f"{variant_id}{{{','.join(parts)}}}"


def _direction_phrase(kind: str) -> str:
    """kind → 中文方向短语（描述**原指标**方向，caption 用）。未知 kind → 显式标「方向未知」不 auto 猜。"""
    d = accuracy_direction(kind)
    if d == "max":
        return "越高越好"
    if d == "min":
        return "越低越好"
    return "方向未知（kind 未声明）"


def _acc_display(acc: float, kind: str) -> float:
    """accuracy 显示值：min 方向 kind（db/nmse/mse/ber）取负显示（``-acc``），max 方向原值。

    取负使「displayed 轴上越大越好」统一——防 bar/scatter 图上 -20dB 视觉高于 -22dB 的强误导
    （goal：坐标轴方向不能让人误判）。**display 变换**对齐 ``tail_metrics.py`` 的
    ``disp = -v if quality``；但 **kind 检测相反**——tail_metrics 按符号 auto-guess（``_classify_obj``），
    本模块要求 caller 显式声明 kind（未知 kind fail loud 跳过，**不** auto 猜方向）。
    caller 须保证 kind 已知方向（未知 kind 由 caller fail loud 跳过——不知是否该取负，禁 auto 猜）。
    """
    return -acc if accuracy_direction(kind) == "min" else acc


def _acc_y_label(kind: str) -> str:
    """accuracy 轴 y_label 短语（取负显示后）。

    min → 「显示 -原值，越大越好（原指标越低越好）」（数据层消除方向歧义，最强）；
    max → 「越高越好」（原值，无变换）。caller 须保证 kind 已知（未知已 skip）。
    """
    if accuracy_direction(kind) == "min":
        return "显示 -原值，越大越好（原指标越低越好）"
    return "越高越好"


def _push_sweep_scatter(
    ledger: list[dict[str, Any]],
    baseline_lat: float | None,
    target_lat: float | None,
    acc_baseline: float | None,
    acc_kind: str,
) -> bool:
    """图1：latency(x) vs accuracy(y) sweep 散点，hue=met_accuracy。

    min 方向 kind（db/nmse/mse/ber）对 accuracy 取负显示（``_acc_display``），使「轴上越大越好」
    统一，防 -20dB 视觉高于 -22dB 的强误导。未知 kind → 方向不可靠（不知是否该取负）→
    fail loud WARN 跳过（不 auto 猜，与 pareto 同口径）。
    """
    if not accuracy_direction(acc_kind):
        print(f"[viz_kd] WARN: 跳过 sweep scatter：accuracy_baseline_kind={acc_kind!r} 方向未知"
              f"（取负显示需已知方向；请声明 nmse/mse/ber/db | snr/acc）", file=sys.stderr)
        return False
    data: list[dict[str, Any]] = []
    for e in ledger:
        pt = _acc_point(e)
        if pt is None:
            continue
        lat, acc = pt
        data.append({
            "latency_us": lat,
            "accuracy": _acc_display(acc, acc_kind),
            "met_accuracy": str(bool(e.get("met_accuracy"))),
            "id": str(e.get("variant_id", "?")),
        })
    if len(data) < _MIN_POINTS:
        print(f"[viz_kd] WARN: 跳过 sweep scatter：有效点 {len(data)} < {_MIN_POINTS}",
              file=sys.stderr)
        return False
    ref_parts = []
    if baseline_lat is not None:
        ref_parts.append(f"baseline_latency={baseline_lat:.4g}")
    if target_lat is not None:
        ref_parts.append(f"target_latency={target_lat:.4g}")
    if acc_baseline is not None:
        ref_parts.append(
            f"accuracy_baseline={_acc_display(acc_baseline, acc_kind):.4g}（{acc_kind}，显示值）")
    caption = (
        "每变体 latency(中位数) vs accuracy。hue=met_accuracy 标命中用户精度基线与否。"
        + ("参考线：" + " / ".join(ref_parts) + "。" if ref_parts else "")
        + f"accuracy 原指标方向：{_direction_phrase(acc_kind)}。"
        + ("min 方向已取负显示（轴上越大=原值越低=越好）。"
           if accuracy_direction(acc_kind) == "min" else "")
    )
    _orca_render_chart(
        chart_type="scatter",
        data=data,
        label=_LABEL,
        title="Distill Sweep — latency vs accuracy",
        x="latency_us",
        y="accuracy",
        hue="met_accuracy",
        x_label="时延 us（越低越好）",
        y_label=f"accuracy（{acc_kind}，{_acc_y_label(acc_kind)}）",
        caption=caption,
    )
    return True


def _push_ledger_table(ledger: list[dict[str, Any]]) -> bool:
    """图2：变体账本表。"""
    if not ledger:
        print("[viz_kd] WARN: 跳过 ledger table：ledger 为空", file=sys.stderr)
        return False
    rows = []
    for e in ledger:
        vid = str(e.get("variant_id", "?"))
        rows.append({
            "variant_id": vid,
            "status": str(e.get("status", "")),
            "latency_us": to_float(e.get("latency_us_median")),
            "accuracy": to_float(e.get("accuracy")),
            "met_lat": str(bool(e.get("met_latency"))),
            "met_acc": str(bool(e.get("met_accuracy"))),
            "cfg": _cfg_summary(e.get("accepted_cfg"), vid),
        })
    _orca_render_chart(
        chart_type="table",
        data=rows,
        label=_LABEL,
        title="Distill Ledger (per variant)",
        columns=["variant_id", "status", "latency_us", "accuracy", "met_lat", "met_acc", "cfg"],
        caption="每变体蒸馏结果：status/latency(中位数)/accuracy/met_*。FAIL_* 行原样显示。",
    )
    return True


def _push_latency_bar(
    ledger: list[dict[str, Any]],
    baseline_lat: float | None,
    target_lat: float | None,
) -> bool:
    """图3：latency 对比 bar —— baseline / target / 各变体。"""
    rows: list[dict[str, Any]] = []
    if baseline_lat is not None:
        rows.append({"stage": "baseline", "latency_us": baseline_lat})
    if target_lat is not None:
        rows.append({"stage": "target", "latency_us": target_lat})
    for e in ledger:
        lat = to_float(e.get("latency_us_median"))
        if lat is not None and lat >= 0:
            rows.append({"stage": str(e.get("variant_id", "?")), "latency_us": lat})
    if len(rows) < _MIN_POINTS:
        print(f"[viz_kd] WARN: 跳过 latency bar：行数 {len(rows)} < {_MIN_POINTS}",
              file=sys.stderr)
        return False
    _orca_render_chart(
        chart_type="bar",
        data=rows,
        label=_LABEL,
        title="Latency Compare (baseline / target / variants)",
        x="stage",
        y="latency_us",
        x_label="阶段 / 变体",
        y_label="时延 us（越低越好）",
        caption="baseline=原始模型时延参考；target=用户阈值；余为各蒸馏变体实测（中位数）。",
    )
    return True


def _push_progress(ledger: list[dict[str, Any]], variants_total: int | None) -> bool:
    """图4：sweep 进度 —— 各 status 计数 + n_done/n_total。"""
    if not ledger:
        print("[viz_kd] WARN: 跳过 progress：ledger 为空", file=sys.stderr)
        return False
    counts: dict[str, int] = {}
    for e in ledger:
        # ``status: null`` / 空串 / 缺失 → "UNKNOWN"（原 ``str(...`` or ``"UNKNOWN"`` 对 null
        # 会得到字符串 "None" 不回退，进而在 progress 图占一个 "None" 类目）。
        st = str(e.get("status") or "UNKNOWN")
        counts[st] = counts.get(st, 0) + 1
    # 固定 status 显示序（KD ledger 全集），未见的仍以 0 呈现给 operator 一眼全貌。
    # 仅对 order 之外的杂项 status 过滤 0 计数——固定项保留 0 是注释承诺的「一眼全貌」
    # （原 `if counts.get(k,0)>0` 把未见固定项也滤掉，与注释矛盾：operator 看不到全貌）。
    order = ["SUCCESS", "FAIL_accuracy", "FAIL_train", "FAIL_latency", "FAIL_export"]
    extra = [k for k in counts if k not in order and counts.get(k, 0) > 0]
    rows = [{"status": k, "count": counts.get(k, 0)} for k in (order + extra)]
    if not rows:
        print("[viz_kd] WARN: 跳过 progress：无 status 计数", file=sys.stderr)
        return False
    n_done = len(ledger)
    total_str = str(variants_total) if variants_total and variants_total > 0 else "未知"
    _orca_render_chart(
        chart_type="bar",
        data=rows,
        label=_LABEL,
        title="Sweep Progress (status counts)",
        x="status",
        y="count",
        x_label="变体终态",
        y_label="变体数",
        caption=(
            f"实验进度：ledger 记录 {n_done} 个变体（variants_total={total_str}）。"
            "SUCCESS=达标；FAIL_accuracy=训练完但精度未达标；FAIL_train=训练异常；"
            "FAIL_latency=gate 阶段时延未达标（未进训练池）。"
        ),
    )
    return True


def _push_pareto(ledger: list[dict[str, Any]], acc_kind: str, acc_baseline: float | None) -> bool:
    """图5：latency(x, min) vs accuracy(y) 帕累托前沿。chart_type=pareto。

    latency 恒为成本（越小越好）；accuracy 方向由 kind 决定：
      - min 方向 kind（db/nmse/mse/ber）取负显示（``_acc_display``），使「displayed 越大越好」，
        ``pareto_y_direction='max'``——前端据 negated data + max 自绘前沿，与原 raw+min 等价
        （-20<-22 翻转为 20<22，max 前沿同 raw 的 min 前沿；防 -20dB 视觉高于 -22dB 误导）。
      - max 方向 kind（snr/acc）原值，``pareto_y_direction='max'``。
    故取负显示后 displayed 数据恒「越大越好」→ ``pareto_y_direction`` 恒 ``'max'``（known kind）。
    未知 kind → 方向不可靠（不知是否该取负）→ fail loud WARN 跳过（不用保守值兜底，与 select 同精神）。
    """
    if not accuracy_direction(acc_kind):
        print(f"[viz_kd] WARN: 跳过 pareto：accuracy_baseline_kind={acc_kind!r} 方向未知"
              f"（请声明 nmse/mse/ber/db | snr/acc）", file=sys.stderr)
        return False
    pts: list[dict[str, float]] = []
    for e in ledger:
        pt = _acc_point(e)
        if pt is None:
            continue
        lat, acc = pt
        pts.append({"latency_us": lat, "accuracy": _acc_display(acc, acc_kind)})
    if len(pts) < _MIN_POINTS:
        print(f"[viz_kd] WARN: 跳过 pareto：有效点 {len(pts)} < {_MIN_POINTS}", file=sys.stderr)
        return False
    ref = ""
    if acc_baseline is not None:
        ref = f"参考线：accuracy_baseline={_acc_display(acc_baseline, acc_kind):.4g}（显示值）。"
    _orca_render_chart(
        chart_type="pareto",
        data=pts,
        label=_LABEL,
        title="Pareto Front — latency vs accuracy",
        x="latency_us",
        y="accuracy",
        pareto_x_direction="min",
        # 取负显示后 displayed 数据恒「越大越好」→ y_direction 恒 max（min 方向 kind 的取负
        # 使 -20<-22 翻转为 20<22，max 前沿与原 raw+min 前沿等价；防 -20dB 视觉高于 -22dB）。
        pareto_y_direction="max",
        x_label="时延 us（越小越好）",
        y_label=f"accuracy（{acc_kind}，{_acc_y_label(acc_kind)}）",
        caption=(
            "latency-accuracy 非支配前沿（前端据 direction 自绘）。x=时延（成本，越小越好）；"
            f"y=accuracy（原指标{_direction_phrase(acc_kind)}；min 方向 kind 已取负显示使轴上越大越好）。"
            f"方向由 accuracy_baseline_kind={acc_kind!r} 锁定。{ref}"
        ),
    )
    return True


def _push_accuracy_compare(ledger: list[dict[str, Any]], acc_kind: str,
                           acc_baseline: float | None) -> bool:
    """图6：各变体 accuracy 横向对比 bar + accuracy_baseline 参考线（作为 data 行）。方向按 kind。

    仅画**真实测量**行（经 ``_acc_point`` 剔除 accuracy=0 哨兵行，防 FAIL_* 行以 0 误导坐标轴）。
    min 方向 kind（db/nmse/mse/ber）取负显示（``_acc_display``），防 bar 图 -20dB 高于 -22dB 的视觉
    强误导（goal：坐标轴方向不能让人误判；数据层消除歧义，最强）。
    baseline 作为一行加入 data（``met_accuracy="ref"``），对齐 ``_push_latency_bar`` 把 baseline/target
    加 data 的做法——原实现 caption 承诺「虚线=baseline」但 data 无 baseline 行，前端无数据画不出。
    未知 kind → fail loud WARN 跳过（取负显示需已知方向，不 auto 猜，与 pareto/scatter 同口径）。
    """
    if not accuracy_direction(acc_kind):
        print(f"[viz_kd] WARN: 跳过 accuracy compare：accuracy_baseline_kind={acc_kind!r} 方向未知"
              f"（取负显示需已知方向；请声明 nmse/mse/ber/db | snr/acc）", file=sys.stderr)
        return False
    rows: list[dict[str, Any]] = []
    for e in ledger:
        pt = _acc_point(e)
        if pt is None:
            continue
        _, acc = pt
        rows.append({"variant_id": str(e.get("variant_id", "?")),
                     "accuracy": _acc_display(acc, acc_kind),
                     "met_accuracy": str(bool(e.get("met_accuracy")))})
    if len(rows) < 1:
        print(f"[viz_kd] WARN: 跳过 accuracy compare：有效行 {len(rows)} < 1", file=sys.stderr)
        return False
    # baseline 作为参考行加入 data（前端据 met_accuracy="ref" 区分画参考标记 / bar）。
    if acc_baseline is not None:
        rows.append({"variant_id": "baseline",
                     "accuracy": _acc_display(acc_baseline, acc_kind),
                     "met_accuracy": "ref"})
    is_min = accuracy_direction(acc_kind) == "min"
    ref = ""
    if acc_baseline is not None:
        ref = (f"baseline 行=accuracy_baseline（显示 {_acc_display(acc_baseline, acc_kind):.4g}，"
               f"原值 {acc_baseline:.4g}，原指标{_direction_phrase(acc_kind)}）。")
    _orca_render_chart(
        chart_type="bar",
        data=rows,
        label=_LABEL,
        title="Accuracy Compare (per variant)",
        x="variant_id",
        y="accuracy",
        hue="met_accuracy",
        x_label="变体",
        y_label=f"accuracy（{acc_kind}，{_acc_y_label(acc_kind)}）",
        caption=(
            "各 student 变体精度横向对比（仅真实测量行）；hue=met_accuracy 标命中基线与否。"
            + ("baseline 行为参考（met_accuracy=ref）。" if acc_baseline is not None else "")
            + ("min 方向已取负显示（轴上越大=原值越低=越好）。" if is_min else "")
            + ref
        ),
    )
    return True


def render_all(
    *,
    ledger_path: str,
    baseline_latency_us: float | None,
    target_latency_us: float | None,
    variants_total: int | None = None,
    accuracy_baseline: float | None,
    accuracy_baseline_kind: str,
    env_anchor: str,
) -> dict[str, Any]:
    _bootstrap_render_chart(env_anchor)
    ledger = _read_jsonl(ledger_path)
    results: dict[str, Any] = {"ledger_rows": len(ledger), "charts": {}}
    if _orca_render_chart is None:
        print("[viz_kd] WARN: orca.chart 不可用，跳过全部 web push（非 Orca 子进程？）",
              file=sys.stderr)
        return results
    pushers = [
        ("sweep_scatter", lambda: _push_sweep_scatter(
            ledger, baseline_latency_us, target_latency_us, accuracy_baseline, accuracy_baseline_kind)),
        ("ledger_table", lambda: _push_ledger_table(ledger)),
        ("latency_bar", lambda: _push_latency_bar(ledger, baseline_latency_us, target_latency_us)),
        ("progress", lambda: _push_progress(ledger, variants_total)),
        ("pareto", lambda: _push_pareto(ledger, accuracy_baseline_kind, accuracy_baseline)),
        ("accuracy_compare", lambda: _push_accuracy_compare(
            ledger, accuracy_baseline_kind, accuracy_baseline)),
    ]
    for name, fn in pushers:
        try:
            ok = fn()
        except Exception as e:  # 单图异常不影响其他图
            print(f"[viz_kd] WARN: 推送 {name} 异常，跳过：{type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            ok = False
        results["charts"][name] = {"pushed": bool(ok)}
    return results


def _main() -> int:
    parser = argparse.ArgumentParser(description="KD-NAS 蒸馏 sweep 可视化（render_chart 推送）")
    parser.add_argument("--ledger", required=True, help="ledger.jsonl 路径")
    parser.add_argument("--baseline_latency_us", type=float, default=None)
    parser.add_argument("--target_latency_us", type=float, default=None)
    parser.add_argument("--variants_total", type=int, default=None,
                        help="KB 变体总数（progress 图分母；train_pool 算后透传）")
    parser.add_argument("--accuracy_baseline", type=float, default=None)
    parser.add_argument("--accuracy_baseline_kind", default="")
    parser.add_argument("--env_anchor", default="",
                        help="自举 ORCA env 锚点（per-run $ORCA_ARTIFACTS_DIR）")
    args = parser.parse_args()
    try:
        result = render_all(
            ledger_path=args.ledger,
            baseline_latency_us=args.baseline_latency_us,
            target_latency_us=args.target_latency_us,
            variants_total=args.variants_total,
            accuracy_baseline=args.accuracy_baseline,
            accuracy_baseline_kind=args.accuracy_baseline_kind or "",
            env_anchor=args.env_anchor,
        )
    except Exception as e:
        print(f"[viz_kd] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

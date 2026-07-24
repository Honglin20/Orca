"""viz_kd.py —— KD-NAS 蒸馏 sweep 可视化（orca.chart render_chart 推送，无 HTML 产物）。

新设计（KD-NAS 重构后）：读 ``ledger.jsonl``（变体行：variant_id/accepted_cfg/latency_ms_median/
latency_ms_std/accuracy/met_latency/met_accuracy/status/...），推：
  1. Distill Sweep 散点 —— latency_ms_median(x) vs accuracy(y)，hue=met_accuracy。
     caption 标 baseline_latency 参考线 + target_latency 阈值 + accuracy_baseline 基线（U-4 sweep 单图）。
  2. Candidate Ledger 表 —— variant_id/status/latency/accuracy/met_lat/met_acc/cfg。
  3. Latency Compare bar —— baseline / target / 各变体 latency。

数据语义：
  - ``latency_ms_median`` 由 tune_latency 真测（用户 latency 脚本，median+std）。
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
      [--baseline_latency_ms <f>] [--target_latency_ms <f>] \\
      [--accuracy_baseline <f>] [--accuracy_baseline_kind <s>] [--env_anchor <path>]
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

_LABEL = "kd-nas"
_MIN_POINTS = 2

# orca.chart web push。BLK-5：先尝试 env_anchor 自举（防 ledger 在稳定根、向上搜不到 orca_env.sh）。
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
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _to_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f else f  # NaN → None
    return None


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


def _valid_point(lat: float | None, acc: float | None) -> bool:
    return lat is not None and acc is not None and lat >= 0  # acc 可负（如 SNR/dB），不卡


def _push_sweep_scatter(
    ledger: list[dict[str, Any]],
    baseline_lat: float | None,
    target_lat: float | None,
    acc_baseline: float | None,
    acc_kind: str,
) -> bool:
    """图1：latency(x) vs accuracy(y) sweep 散点，hue=met_accuracy。"""
    data: list[dict[str, Any]] = []
    for e in ledger:
        lat = _to_float(e.get("latency_ms_median"))
        acc = _to_float(e.get("accuracy"))
        if not _valid_point(lat, acc):
            continue
        data.append({
            "latency_ms": lat,
            "accuracy": acc,
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
        ref_parts.append(f"accuracy_baseline={acc_baseline:.4g} ({acc_kind or 'auto'})")
    caption = (
        "每变体 latency(中位数) vs accuracy。hue=met_accuracy 标命中用户精度基线与否。"
        + ("参考线：" + " / ".join(ref_parts) + "。" if ref_parts else "")
    )
    _orca_render_chart(
        chart_type="scatter",
        data=data,
        label=_LABEL,
        title="Distill Sweep — latency vs accuracy",
        x="latency_ms",
        y="accuracy",
        hue="met_accuracy",
        x_label="时延 ms（越低越好）",
        y_label=f"accuracy（{acc_kind or 'auto-detected'}）",
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
            "latency_ms": _to_float(e.get("latency_ms_median")),
            "accuracy": _to_float(e.get("accuracy")),
            "met_lat": str(bool(e.get("met_latency"))),
            "met_acc": str(bool(e.get("met_accuracy"))),
            "cfg": _cfg_summary(e.get("accepted_cfg"), vid),
        })
    _orca_render_chart(
        chart_type="table",
        data=rows,
        label=_LABEL,
        title="Distill Ledger (per variant)",
        columns=["variant_id", "status", "latency_ms", "accuracy", "met_lat", "met_acc", "cfg"],
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
        rows.append({"stage": "baseline", "latency_ms": baseline_lat})
    if target_lat is not None:
        rows.append({"stage": "target", "latency_ms": target_lat})
    for e in ledger:
        lat = _to_float(e.get("latency_ms_median"))
        if lat is not None and lat >= 0:
            rows.append({"stage": str(e.get("variant_id", "?")), "latency_ms": lat})
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
        y="latency_ms",
        x_label="阶段 / 变体",
        y_label="时延 ms（越低越好）",
        caption="baseline=原始模型时延参考；target=用户阈值；余为各蒸馏变体实测（中位数）。",
    )
    return True


def render_all(
    *,
    ledger_path: str,
    baseline_latency_ms: float | None,
    target_latency_ms: float | None,
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
            ledger, baseline_latency_ms, target_latency_ms, accuracy_baseline, accuracy_baseline_kind)),
        ("ledger_table", lambda: _push_ledger_table(ledger)),
        ("latency_bar", lambda: _push_latency_bar(ledger, baseline_latency_ms, target_latency_ms)),
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
    parser.add_argument("--baseline_latency_ms", type=float, default=None)
    parser.add_argument("--target_latency_ms", type=float, default=None)
    parser.add_argument("--accuracy_baseline", type=float, default=None)
    parser.add_argument("--accuracy_baseline_kind", default="")
    parser.add_argument("--env_anchor", default="",
                        help="BLK-5：自举 ORCA env 锚点（per-run $ORCA_ARTIFACTS_DIR）")
    args = parser.parse_args()
    try:
        result = render_all(
            ledger_path=args.ledger,
            baseline_latency_ms=args.baseline_latency_ms,
            target_latency_ms=args.target_latency_ms,
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

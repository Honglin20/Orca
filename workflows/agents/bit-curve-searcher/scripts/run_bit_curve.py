#!/usr/bin/env python3
"""run_bit_curve.py —— 混合精度 Pareto 位宽-精度曲线（bit-curve-searcher 节点调用）。

流程：
1. import adapter → FP teacher + calib + eval loaders + eval_fn（默认 teacher-student mse）
2. base qconfig + q_layers（TSQuantizer.prepare）
3. MixPrecisionSearchConfig(strategy=m0_pareto, mode, candidate_format_space=QConfig 列表, ...）
4. search_mix_precision → (best_configs, report)；SDK 自落盘 bit_trend.json / frontier.json /
   best_under_constraints.json / report.json 到 search_output_dir
5. 解析 report（frontier.points / final / eval_calls）→ 聚合 bit_curve_summary.json（脚本自己的摘要，
   避免覆盖 SDK 的 report.json）
6. bake（可选）：final.layer_configs 每层 dict → QConfig.from_dict → quantize_model(qconfig_dict=...) →
   best_mixed_model.pt
7. render_chart（容错不阻断）：line（Pareto 位宽-精度曲线）+ bar（选中候选格式分布）+ table（前沿候选）
8. stdout JSON 摘要（agent 原样回显，对齐 output_schema）

铁律：
- search 全局失败 → fail loud（exit 3）
- bake 失败 → stderr + 空串 baked_model_path，不阻断（曲线是核心产出）
- 推图失败 → stderr 提示但不阻断（bit_curve_summary.json 是核心产出）
- 全 mxint/int 基：候选格式 INT8/W4A8/INT4/MX4/MX8（MX 家族即 mxint 基）

注：QConfig 字段、report 结构按 ts_quant.auto_quant.mix_precision（探针 2026-07-20 实证）组织；
若 ts_quant API 调整，相应核对。
"""

from __future__ import annotations

import argparse
import copy
import gc
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

# 一次性 import ts_quant 关键依赖：缺包/未配置 → fail loud exit 2（环境错），
# 而非走搜索 try 内 → exit 3（业务错，根因被埋）。
try:
    from ts_quant import (  # noqa: F401
        MixPrecisionSearchConfig,
        MetricSpec,
        QConfig,
        TSQuantizer,
        quantize_model,
        search_mix_precision,
    )
    from ts_quant.eval import build_teacher_student_eval_fn  # noqa: F401
    _TS_QUANT_OK = True
    _TS_QUANT_IMPORT_ERROR: str | None = None
except Exception as _e:  # ImportError / 二次依赖（torch 等）失败都兜住
    _TS_QUANT_OK = False
    _TS_QUANT_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

CHART_LABEL = "quant/bit-curve"
_TRUE_TOKENS = {"true", "1", "yes", "y", "on"}
_FALSE_TOKENS = {"false", "0", "no", "n", "off"}

# search 自落盘目录（区别于 workflow output_dir）：放 SDK 原始 frontier.json/bit_trend.json 等。
_SEARCH_SUBDIR = "search_artifacts"


# ─────────────────────────────────────────────────────────────────
# 格式别名 → QConfig（复刻 SDK _default_format_aliases，全 mxint/int 基）
# search_mix_precision 的 candidate_format_space 只吃 QConfig 对象（不吃字符串别名），
# 故脚本侧维护此映射；bake 时 final.layer_configs 已是真 QConfig dict，走 from_dict。
# ─────────────────────────────────────────────────────────────────
def _format_qconfigs(granularity: str) -> dict[str, Any]:
    return {
        "INT8": QConfig(method="int", n_bits=8, granularity=granularity),
        "W4A8": QConfig(
            method="int", n_bits=8, w_n_bits=4, a_n_bits=8, granularity=granularity
        ),
        "INT4": QConfig(method="int", n_bits=4, granularity=granularity),
        "MX4": QConfig(method="mx", n_bits=4, granularity=granularity),
        "MX8": QConfig(method="mx", n_bits=8, granularity=granularity),
    }


def _load_adapter(path: str):
    """按文件路径动态 import adapter 模块。"""
    p = Path(path).resolve()
    if not p.is_file():
        sys.stderr.write(f"[run_bit_curve] adapter 不存在: {p}\n")
        sys.exit(2)
    spec = importlib.util.spec_from_file_location("ts_quant_bit_curve_adapter", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _load_env_file(path: str) -> None:
    """自加载 orca_env.sh（`export K=V` 行）到 os.environ——opencode bash 工具不跨调用保 env，
    若子代理把 `source orca_env.sh` 和 `python3` 拆成两次调用，脚本运行的 shell 就没有
    `ORCA_CHART_SOCK` → render_chart raise 被静默吞 → 图不推。已存在的 env 不覆盖（显式 env 优先）。
    """
    import re

    if not path:
        return
    p = Path(path).expanduser()
    if not p.is_file():
        sys.stderr.write(f"[run_bit_curve] --env_file 不存在: {p}（跳过自加载）\n")
        return
    cnt = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
        os.environ.setdefault(k, v)
        cnt += 1
    sys.stderr.write(f"[run_bit_curve] 自加载 {cnt} 个 env from {p}\n")


def _parse_csv(raw: str, fallback: list[str]) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return list(fallback)
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


# ─────────────────────────────────────────────────────────────────
# Eval 流水（与 W2 run_ptq_sweep._resolve_eval 同源契约）
# ─────────────────────────────────────────────────────────────────
def _resolve_eval(adapter, fp_model, eval_loader, forward_fn) -> tuple[Callable, str, bool]:
    """返回 (eval_fn, metric_kind, higher_is_better)。

    - adapter.get_eval_fn() 存在且返回非 None → 业务路径，需 adapter.get_metric_spec() 告知
      metric/direction
    - 否则 → 默认 teacher-student mse（lower is better）；forward_fn 必填 → fail loud
    """
    get_eval_fn = getattr(adapter, "get_eval_fn", None)
    business_fn = get_eval_fn() if callable(get_eval_fn) else None
    if business_fn is not None:
        get_metric_spec = getattr(adapter, "get_metric_spec", None)
        if not callable(get_metric_spec):
            sys.stderr.write(
                "[run_bit_curve] 业务 eval_fn 路径需要 adapter.get_metric_spec() "
                "返回 {primary_metric: str, higher_is_better: bool}\n"
            )
            sys.exit(2)
        spec = get_metric_spec() or {}
        metric_kind = spec.get("primary_metric")
        if not metric_kind:
            sys.stderr.write(
                f"[run_bit_curve] get_metric_spec() 缺 primary_metric: {spec}\n"
            )
            sys.exit(2)
        return business_fn, str(metric_kind), bool(spec.get("higher_is_better", False))

    if forward_fn is None:
        sys.stderr.write(
            "[run_bit_curve] 默认 teacher-student eval 路径需要 adapter.forward_fn "
            "（按模型 forward 解包 batch）—— 异构 batch 会让 SDK fallback 误算\n"
        )
        sys.exit(2)
    eval_fn = build_teacher_student_eval_fn(
        teacher_model=fp_model,
        dataloader=eval_loader,
        forward_fn=forward_fn,
    )
    return eval_fn, "mse", False


def _dump_json(obj: dict[str, Any], path: Path) -> None:
    """原子落盘 JSON（写 tmp → os.replace，中断不留半个文件）。"""
    payload = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def _free_model(q_model) -> None:
    """显式释放量化模型，触发 Python GC + CUDA cache 回收。"""
    if q_model is None:
        return
    try:
        del q_model
    except Exception:
        pass
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────
# report 解析
# ─────────────────────────────────────────────────────────────────
def _format_counts_summary(format_counts: dict[str, Any] | None) -> str:
    """{INT8: 40, MX4: 10} → 'INT8×40+MX4×10'（按 count 降序，None → ''）。"""
    if not isinstance(format_counts, dict) or not format_counts:
        return ""
    items = sorted(
        ((str(k), int(v)) for k, v in format_counts.items() if v),
        key=lambda kv: (-kv[1], kv[0]),
    )
    return "+".join(f"{k}×{v}" for k, v in items) if items else ""


def _point_bit(point: dict[str, Any]) -> float:
    """Pareto 点的 x 轴 bit 值（selection_bit_score 优先，次 avg_wa_bit_proxy，次 avg_bit_raw）。"""
    for key in ("selection_bit_score", "avg_wa_bit_proxy", "avg_bit_raw"):
        v = point.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def _point_metric(point: dict[str, Any]) -> float:
    for key in ("primary_metric", "score"):
        v = point.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


# ─────────────────────────────────────────────────────────────────
# 可视化（容错不阻断，复用 W2 render_chart 调用形态）
# ─────────────────────────────────────────────────────────────────
def _push_table(render_chart, rows: list[dict[str, Any]], title: str) -> None:
    try:
        render_chart(
            chart_type="table",
            data=rows,
            label=CHART_LABEL,
            title=title,
            columns=list(rows[0].keys()) if rows else [],
        )
        sys.stderr.write(f"[run_bit_curve] pushed table: {len(rows)} rows\n")
    except Exception as e:
        sys.stderr.write(f"[run_bit_curve] table 推送失败（不阻断）: {e}\n")


def _push_charts(
    render_chart,
    frontier_points: list[dict[str, Any]],
    selected_id: str | None,
    metric_kind: str,
    selected_format_counts: dict[str, Any] | None,
) -> None:
    # line：Pareto 位宽-精度曲线（x=bit, y=metric；selected 点单独成 series 高亮）
    line_data: list[dict[str, Any]] = []
    pts_sorted = sorted(frontier_points, key=_point_bit)
    for p in pts_sorted:
        cid = str(p.get("candidate_id", ""))
        line_data.append({
            "bit": round(_point_bit(p), 4),
            "metric": _point_metric(p),
            "series": "selected" if cid and cid == selected_id else "frontier",
        })
    try:
        if line_data:
            render_chart(
                chart_type="line",
                data=line_data,
                label=CHART_LABEL,
                title=f"Bit-Width vs Accuracy Pareto Frontier ({metric_kind})",
                x="bit",
                y="metric",
                hue="series",
            )
            sys.stderr.write(f"[run_bit_curve] pushed line: {len(line_data)} points\n")
    except Exception as e:
        sys.stderr.write(f"[run_bit_curve] line 推送失败（不阻断）: {e}\n")

    # bar：选中候选的格式分布（mxint 混合）
    if isinstance(selected_format_counts, dict) and selected_format_counts:
        bar_data = [
            {"format": str(k), "layers": int(v)}
            for k, v in selected_format_counts.items()
            if v
        ]
        bar_data.sort(key=lambda r: (-r["layers"], r["format"]))
        try:
            if bar_data:
                render_chart(
                    chart_type="bar",
                    data=bar_data,
                    label=CHART_LABEL,
                    title="Selected Candidate — Format Mix (mxint base)",
                    x="format",
                    y="layers",
                )
                sys.stderr.write(
                    f"[run_bit_curve] pushed bar: {len(bar_data)} formats\n"
                )
        except Exception as e:
            sys.stderr.write(f"[run_bit_curve] bar 推送失败（不阻断）: {e}\n")

    # table：前沿候选明细
    rows = [
        {
            "candidate": str(p.get("candidate_id", "")),
            "bit": round(_point_bit(p), 4),
            metric_kind: round(_point_metric(p), 6),
            "accuracy_loss": round(float(p.get("accuracy_loss") or 0.0), 6),
            "formats": _format_counts_summary(p.get("format_counts")),
        }
        for p in pts_sorted
    ]
    _push_table(render_chart, rows, "Pareto Frontier Candidates")


def _render_charts(
    frontier_points: list[dict[str, Any]],
    selected_id: str | None,
    metric_kind: str,
    selected_format_counts: dict[str, Any] | None,
) -> None:
    try:
        from orca.chart import render_chart
    except Exception as e:
        sys.stderr.write(f"[run_bit_curve] orca.chart 不可用（不阻断）: {e}\n")
        return
    _push_charts(
        render_chart, frontier_points, selected_id, metric_kind, selected_format_counts
    )


# ─────────────────────────────────────────────────────────────────
# bake：final.layer_configs → quantize_model(qconfig_dict=...) → best_mixed_model.pt
# ─────────────────────────────────────────────────────────────────
def _bake_selected(
    fp_model,
    layer_configs: dict[str, Any],
    base_qconfig: Any,
    calib_loader,
    forward_fn,
    baked_path: Path,
) -> str:
    """把选中候选的 per-layer 格式 assignment 烤成可部署 state_dict。失败返空串 + stderr。"""
    import torch  # noqa: F401

    try:
        qconfig_dict: dict[str, Any] = {}
        for name, cfg_dict in layer_configs.items():
            if isinstance(cfg_dict, dict):
                qconfig_dict[str(name)] = QConfig.from_dict(cfg_dict)
        if not qconfig_dict:
            sys.stderr.write(
                "[run_bit_curve] bake: layer_configs 为空 → skip bake\n"
            )
            return ""
        model_copy = copy.deepcopy(fp_model)
        q_model = quantize_model(
            model=model_copy,
            qconfig=base_qconfig,
            qconfig_dict=qconfig_dict,
            calib_data=calib_loader,
            forward_fn=forward_fn,
            inplace=True,
        )
        torch.save(q_model.state_dict(), baked_path)
        _free_model(q_model)
        sys.stderr.write(f"[run_bit_curve] baked selected → {baked_path}\n")
        return str(baked_path)
    except Exception as e:
        sys.stderr.write(
            f"[run_bit_curve] bake 失败（不阻断，曲线是核心产出）: "
            f"{type(e).__name__}: {e}\n"
        )
        return ""


# ─────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True, help="adapter.py 路径")
    ap.add_argument("--model_path", required=True, help="原始模型入口路径（回显用）")
    ap.add_argument("--project_root", required=True)
    ap.add_argument("--calib_data_ref", required=True, help="校准 loader dotted-path（可空串）")
    ap.add_argument("--eval_data_ref", required=True, help="评估 loader dotted-path（可空串）")
    ap.add_argument("--eval_fn_ref", required=True, help="业务 eval_fn dotted-path（可空串）")
    ap.add_argument("--mode", required=True, help="explore / constrained_select / minimize_bit_under_accuracy")
    ap.add_argument("--candidate_format_space", required=True, help="逗号分隔格式别名（可空串→默认全集）")
    ap.add_argument("--bit_objective", required=True, help="weight_activation_proxy / weight_only")
    ap.add_argument("--accuracy_tolerance", required=True, help="absolute 精度损失容忍（float 字符串）")
    ap.add_argument("--avg_bit_budget", required=True, help="硬 bit 上限（可空串→null）")
    ap.add_argument("--max_evals", required=True, help="主搜索预算（int 字符串）")
    ap.add_argument("--granularity", required=True, help="per_tensor / per_token / per_channel")
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--bake", required=True, help="true / false")
    ap.add_argument(
        "--env_file",
        default="",
        help="本 run 的 orca_env.sh 路径；脚本启动自加载 ORCA_* env（兜底：opencode bash 拆调用会丢 env）",
    )
    args = ap.parse_args()
    _load_env_file(args.env_file)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "bit_curve_summary.json"
    search_dir = output_dir / _SEARCH_SUBDIR
    search_dir.mkdir(parents=True, exist_ok=True)

    if not _TS_QUANT_OK:
        sys.stderr.write(
            f"[run_bit_curve] ts_quant import 失败（环境错，exit 2）: {_TS_QUANT_IMPORT_ERROR}\n"
        )
        sys.exit(2)

    mode = (args.mode or "explore").strip().lower()
    if mode not in {"explore", "constrained_select", "minimize_bit_under_accuracy"}:
        sys.stderr.write(
            f"[run_bit_curve] mode 非法 '{mode}'（支持 explore/constrained_select/minimize_bit_under_accuracy）\n"
        )
        sys.exit(2)

    granularity = (args.granularity or "per_tensor").strip().lower()
    all_fmts = _format_qconfigs(granularity)
    fmt_tokens = _parse_csv(args.candidate_format_space, fallback=list(all_fmts.keys()))
    fmt_tokens = [t.upper() for t in fmt_tokens]
    invalid = [t for t in fmt_tokens if t not in all_fmts]
    if invalid:
        sys.stderr.write(
            f"[run_bit_curve] 未知格式别名 {invalid}（支持：{sorted(all_fmts)}）\n"
        )
        sys.exit(2)
    candidate_space = [all_fmts[t] for t in fmt_tokens]

    bit_objective = (args.bit_objective or "weight_activation_proxy").strip().lower()
    try:
        tolerance_value = float(args.accuracy_tolerance or "0.01")
    except ValueError:
        sys.stderr.write(
            f"[run_bit_curve] accuracy_tolerance 非法 '{args.accuracy_tolerance}'\n"
        )
        sys.exit(2)
    try:
        max_evals = int(args.max_evals or "32")
    except ValueError:
        sys.stderr.write(f"[run_bit_curve] max_evals 非法 '{args.max_evals}'\n")
        sys.exit(2)
    budget_raw = (args.avg_bit_budget or "").strip()
    avg_bit_budget = float(budget_raw) if budget_raw else None

    # adapter → fp teacher + calib + eval + forward
    adapter = _load_adapter(args.adapter)
    fp_model = adapter.load_model()
    calib_loader = adapter.get_calib_loader()
    forward_fn = getattr(adapter, "forward_fn", None)
    get_eval_loader = getattr(adapter, "get_eval_loader", None)
    eval_loader = get_eval_loader() if callable(get_eval_loader) else calib_loader

    eval_fn, metric_kind, higher_is_better = _resolve_eval(
        adapter, fp_model, eval_loader, forward_fn
    )
    metric_spec = MetricSpec(primary_metric=metric_kind, higher_is_better=higher_is_better)

    # base qconfig + q_layers（prepare 即可拿到 layer map；search 内部自做 candidate eval/calib）
    base_qconfig = all_fmts["INT8"]
    quantizer = TSQuantizer(fp_model, base_qconfig)
    quantizer.prepare()
    q_layers = quantizer.q_layers
    if not q_layers:
        sys.stderr.write(
            "[run_bit_curve] TSQuantizer.prepare() 未发现可量化层（q_layers 空）→ exit 3\n"
        )
        sys.exit(3)

    sys.stderr.write(
        f"[run_bit_curve] mode={mode} formats={fmt_tokens} q_layers={len(q_layers)} "
        f"bit_objective={bit_objective} max_evals={max_evals} "
        f"metric_kind={metric_kind} higher_is_better={higher_is_better}\n"
    )

    # 搜索
    search_config = MixPrecisionSearchConfig(
        strategy="m0_pareto",
        mode=mode,
        base_qconfig=base_qconfig,
        candidate_format_space=candidate_space,
        bit_objective=bit_objective,
        accuracy_tolerance={"mode": "absolute", "value": tolerance_value},
        avg_bit_budget=avg_bit_budget,
        max_evals=max_evals,
        output_dir=str(search_dir),
    )

    t0 = time.time()
    try:
        best_configs, report = search_mix_precision(
            fp_model,
            q_layers=q_layers,
            eval_fn=eval_fn,
            metric_spec=metric_spec,
            search_config=search_config,
            return_report=True,
        )
    except Exception as e:
        sys.stderr.write(
            f"[run_bit_curve] search_mix_precision 失败（exit 3）: {type(e).__name__}: {e}\n"
        )
        sys.exit(3)
    elapsed = round(time.time() - t0, 3)

    if not isinstance(report, dict):
        sys.stderr.write(
            f"[run_bit_curve] report 非 dict（exit 3）: {type(report).__name__}\n"
        )
        sys.exit(3)

    # 解析 report
    frontier = report.get("frontier") or {}
    frontier_points = frontier.get("points") if isinstance(frontier, dict) else None
    if not isinstance(frontier_points, list):
        frontier_points = []
    final = report.get("final") if isinstance(report.get("final"), dict) else {}
    selected_id = final.get("selected_candidate_id")
    best_metric = final.get("score")
    best_bit = final.get("selection_bit_score")
    selected_format_counts = final.get("format_counts") if isinstance(final.get("format_counts"), dict) else None
    layer_configs = final.get("layer_configs") if isinstance(final.get("layer_configs"), dict) else {}
    eval_calls = report.get("eval_calls")
    final_status = final.get("status")

    best_config_label = (
        f"{selected_id} [{_format_counts_summary(selected_format_counts)}]"
        if selected_id
        else _format_counts_summary(selected_format_counts)
    )

    summary: dict[str, Any] = {
        "mode": mode,
        "strategy": "m0_pareto",
        "metric_kind": metric_kind,
        "higher_is_better": higher_is_better,
        "candidate_format_space": fmt_tokens,
        "bit_objective": bit_objective,
        "granularity": granularity,
        "max_evals": max_evals,
        "avg_bit_budget": avg_bit_budget,
        "accuracy_tolerance": tolerance_value,
        "model_path": args.model_path,
        "final_status": final_status,
        "selected": {
            "candidate_id": selected_id,
            "score": best_metric,
            "selection_bit_score": best_bit,
            "avg_wa_bit_proxy": final.get("avg_wa_bit_proxy"),
            "format_counts": selected_format_counts,
            "selection_reason": final.get("selection_reason"),
        },
        "eval_calls": eval_calls,
        "frontier_n": len(frontier_points),
        "elapsed_seconds": elapsed,
        "search_artifacts_dir": str(search_dir),
        "baked_model_path": None,
    }
    _dump_json(summary, summary_path)

    # bake
    baked_path_str = ""
    bake_token = (args.bake or "").strip().lower()
    if bake_token in _TRUE_TOKENS:
        if layer_configs:
            baked_path_str = _bake_selected(
                fp_model,
                layer_configs,
                base_qconfig,
                calib_loader,
                forward_fn,
                output_dir / "best_mixed_model.pt",
            )
        else:
            sys.stderr.write(
                "[run_bit_curve] bake: final.layer_configs 缺失 → skip bake\n"
            )
        summary["baked_model_path"] = baked_path_str or None
        _dump_json(summary, summary_path)
    elif bake_token in _FALSE_TOKENS:
        sys.stderr.write("[run_bit_curve] bake=false → skip bake\n")
    else:
        sys.stderr.write(
            f"[run_bit_curve] --bake='{args.bake}' 非法（期望 true/false/1/0/yes/no）\n"
        )
        sys.exit(2)

    # charts
    _render_charts(frontier_points, selected_id, metric_kind, selected_format_counts)

    # stdout JSON 摘要（agent 原样回显）
    out = {
        "output_dir": str(output_dir),
        "report_path": str(summary_path),
        "model_path": args.model_path,
        "baked_model_path": baked_path_str,
        "best_config": best_config_label,
        "best_metric": float(best_metric) if isinstance(best_metric, (int, float)) else 0.0,
        "best_bit": float(best_bit) if isinstance(best_bit, (int, float)) else 0.0,
        "candidates_evaluated": int(eval_calls) if isinstance(eval_calls, (int, float)) else 0,
        "mode": mode,
        "metric_kind": metric_kind,
    }
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

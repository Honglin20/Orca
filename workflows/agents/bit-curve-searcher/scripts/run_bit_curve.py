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

# 共享 device / seed 逻辑（plan §P5 硬约束：单一真相源）。
_HERE = Path(__file__).resolve()
_QUANT_SCRIPTS = _HERE.parent.parent.parent / "_quant_scripts"
if str(_QUANT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_QUANT_SCRIPTS))
from _device import (  # noqa: E402
    add_device_seed_args,
    resolve_device_and_seed,
    wrap_forward_with_device,
)

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
    # WARN 不静默（plan §P5）：未提供业务 eval_fn → 退 teacher-student mse，精度仅自洽性参考。
    sys.stderr.write(
        "[run_bit_curve] WARN: 未提供业务 eval_fn（eval_fn_ref 空）→ 退 teacher-student mse。"
        "该指标仅自洽性参考（量化模型 vs FP teacher 的 mse），不代表业务精度。\n"
    )
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
def _push_table(render_chart, rows: list[dict[str, Any]], title: str,
                caption: str = "") -> None:
    try:
        render_chart(
            chart_type="table",
            data=rows,
            label=CHART_LABEL,
            title=title,
            columns=list(rows[0].keys()) if rows else [],
            caption=caption,
        )
        sys.stderr.write(f"[run_bit_curve] pushed table: {len(rows)} rows\n")
    except Exception as e:
        sys.stderr.write(f"[run_bit_curve] table 推送失败（不阻断）: {e}\n")


def _infer_pareto_y_direction(
    report_frontier: Any, higher_is_better: bool, metric_kind: str
) -> str:
    """chart1 pareto 的 y 方向：``max``（higher-is-better）/``min``（lower-is-better）。

    优先读 ``report.frontier.metric_spec.higher_is_better``（SDK 回显，权威）；
    缺失 → 用 ``_resolve_eval`` 的本地 ``higher_is_better``（同一 MetricSpec，二者一致）；
    若两者都不可用（不应发生）→ 按 ``metric_kind`` 名字推断：mse/loss/error/nll → min，其余 → max。
    """
    if isinstance(report_frontier, dict):
        ms = report_frontier.get("metric_spec")
        if isinstance(ms, dict) and isinstance(ms.get("higher_is_better"), bool):
            return "max" if ms["higher_is_better"] else "min"
    if isinstance(higher_is_better, bool):
        return "max" if higher_is_better else "min"
    kind_lower = (metric_kind or "").lower()
    # 最后防线：mse/loss/error 类显式判 min（lower-is-better），其余默认 max。
    if any(t in kind_lower for t in ("mse", "loss", "error", "nll")):
        return "min"
    return "max"


def _push_charts(
    render_chart,
    frontier_points: list[dict[str, Any]],
    selected_id: str | None,
    metric_kind: str,
    selected_format_counts: dict[str, Any] | None,
    pareto_y_direction: str,
    archive_records: list[dict[str, Any]] | None,
) -> None:
    # chart 1：真 Pareto 前沿（chart_type=pareto；x=bit 恒 min 越小越好，y=metric 按方向）。
    # 用 frontier_points，去掉旧的 hue="series"——pareto 前端自动绘前沿线，
    # 选中点的标识走 scatter（coral 高亮）+ table，这里不重复编码。
    # 标题用 metric_kind 而非「Accuracy」（原写死 Accuracy 但 y 轴常是 mse，名实不符）。
    pts_sorted = sorted(frontier_points, key=_point_bit)
    pareto_data: list[dict[str, Any]] = [
        {"bit": round(_point_bit(p), 4), "metric": _point_metric(p)}
        for p in pts_sorted
    ]
    try:
        if pareto_data:
            render_chart(
                chart_type="pareto",
                data=pareto_data,
                label=CHART_LABEL,
                title=f"Bit-Width vs {metric_kind} Pareto Frontier",
                x="bit",
                y="metric",
                x_label="avg bit-width (lower is better)",
                y_label=f"{metric_kind} (direction: {pareto_y_direction})",
                caption=(
                    f"x=avg bit-width（越小越好）；y={metric_kind}，方向={pareto_y_direction}"
                    f"（mse 口径下低=好，业务 accuracy 口径下高=好）。coral 点=前沿。"
                ),
                pareto_x_direction="min",  # bit 越小越好
                pareto_y_direction=pareto_y_direction,
            )
            sys.stderr.write(
                f"[run_bit_curve] pushed pareto: {len(pareto_data)} points "
                f"(y_direction={pareto_y_direction})\n"
            )
    except Exception as e:
        sys.stderr.write(f"[run_bit_curve] pareto 推送失败（不阻断）: {e}\n")

    # chart 1.5：全候选 scatter（coral=前沿/选中，钢蓝=其余）。
    # 数据来自 report["archive"]["records"]——非仅 frontier，含所有 evaluated 候选，
    # 看「前沿 vs 噪声点」分布。color 字段驱动 scatter per-row fill（hue 会拆 series，不用）。
    if archive_records:
        highlight_ids = {
            str(p.get("candidate_id", ""))
            for p in frontier_points
            if p.get("candidate_id")
        }
        if selected_id:
            highlight_ids.add(str(selected_id))
        scatter_data: list[dict[str, Any]] = [
            {
                "bit": round(_point_bit(rec), 4),
                "metric": _point_metric(rec),
                "color": "#D4605A"
                if str(rec.get("candidate_id", "")) in highlight_ids
                else "#5B8DB8",
            }
            for rec in archive_records
        ]
        try:
            render_chart(
                chart_type="scatter",
                data=scatter_data,
                label=CHART_LABEL,
                title="All Evaluated Candidates (coral=frontier)",
                x="bit",
                y="metric",
                color="color",
                x_label="avg bit-width（越低越好）",
                y_label=f"{metric_kind}（方向 {pareto_y_direction}）",
                caption="全部 evaluated 候选云；珊瑚=前沿/选中（与上图 Pareto Frontier 同前沿，此图加噪声点背景）。",
            )
            sys.stderr.write(
                f"[run_bit_curve] pushed scatter: {len(scatter_data)} candidates "
                f"({len(highlight_ids)} highlighted)\n"
            )
        except Exception as e:
            sys.stderr.write(f"[run_bit_curve] scatter 推送失败（不阻断）: {e}\n")
    else:
        sys.stderr.write(
            "[run_bit_curve] report.archive.records 缺失 → skip all-candidates scatter\n"
        )

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
                    x_label="量化格式",
                    y_label="该格式的层数",
                    caption="选中候选的混合精度构成（mxint 基）：INT8/MX8=高精度档，INT4/MX4=低位宽档。",
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
    _push_table(
        render_chart, rows, "Pareto Frontier Candidates",
        caption="前沿候选明细；accuracy_loss=相对 FP baseline 的损失。",
    )


def _render_charts(
    frontier_points: list[dict[str, Any]],
    selected_id: str | None,
    metric_kind: str,
    selected_format_counts: dict[str, Any] | None,
    pareto_y_direction: str,
    archive_records: list[dict[str, Any]] | None,
) -> None:
    try:
        from orca.chart import render_chart
    except Exception as e:
        sys.stderr.write(f"[run_bit_curve] orca.chart 不可用（不阻断）: {e}\n")
        return
    _push_charts(
        render_chart,
        frontier_points,
        selected_id,
        metric_kind,
        selected_format_counts,
        pareto_y_direction,
        archive_records,
    )


# bake 后对账容差（plan §P5 N7：相对 1e-4；baked 重 eval metric 与 search final.score 对比）。
_BAKE_METRIC_REL_TOL = 1e-4
_BAKE_METRIC_ABS_FLOOR = 1e-12  # 防 |final|=0 时除零


def _compute_bake_metric_relative_diff(
    reeval_metric: float | None,
    search_final_score: float | None,
) -> float | None:
    """bake 重 eval metric 与 search final.score 的相对差（pure math，无 torch 依赖）。

    返回 None 表示无法对账（任一为 None / 类型错 / 解析失败）；
    否则返回 ``|reev - final| / max(|final|, _BAKE_METRIC_ABS_FLOOR)``。
    抽成独立函数便于单测（Rule 9：关键 fail-loud 防线必须有测试钉死）。
    """
    if reeval_metric is None or search_final_score is None:
        return None
    try:
        diff = abs(float(reeval_metric) - float(search_final_score))
        denom = max(abs(float(search_final_score)), _BAKE_METRIC_ABS_FLOOR)
        return diff / denom
    except (TypeError, ValueError):
        return None


def _bake_selected(
    fp_model,
    layer_configs: dict[str, Any],
    base_qconfig: Any,
    calib_loader,
    forward_fn,
    baked_path: Path,
    eval_fn: Callable,
    metric_kind: str,
) -> tuple[str, float | None]:
    """把选中候选的 per-layer 格式 assignment 烤成可部署 state_dict。

    改动生效（plan §P5 核心修复）：bake 后**reload baked state_dict + 重 eval**，
    返回 ``(path, reeval_metric)``。失败返 ``("", None)``——曲线产出不受阻断
    （spec-review N7：bake 失败跳过对账、不阻断）。

    reload 路径：再 deepcopy fp_model → quantize_model 出同拓扑空壳 → load_state_dict
    （从落盘的 best_mixed_model.pt）→ eval。最贴近「用户拿到 .pt 后会看到的精度」。
    """
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
            return "", None
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

        # 改动生效（plan §P5 核心修复）：reload 落盘 state_dict 到同拓扑空壳，重 eval。
        # 这才反映「用户拿到 best_mixed_model.pt 后会看到的真实精度」——search 内部
        # final.score 用的是搜索时的 in-memory eval，与 bake artifact 可能因 state_dict
        # 序列化丢 observer/calib buffer 而漂移。
        # strict=True（code-reviewer 🔴）：键失配必须 fail loud——丢 observer state
        # 是 bake 真坏了的信号，不该静默。
        reeval_metric: float | None = None
        try:
            reload_q_model = quantize_model(
                model=copy.deepcopy(fp_model),
                qconfig=base_qconfig,
                qconfig_dict=qconfig_dict,
                calib_data=calib_loader,
                forward_fn=forward_fn,
                inplace=True,
            )
            reload_q_model.load_state_dict(
                torch.load(baked_path, map_location="cpu"), strict=True
            )
            metrics = eval_fn(reload_q_model)
            if isinstance(metrics, dict) and metric_kind in metrics:
                reeval_metric = float(metrics[metric_kind])
            _free_model(reload_q_model)
        except Exception as ree:
            sys.stderr.write(
                f"[run_bit_curve] bake 后 reload + 重 eval 失败"
                f"（不阻断 bake，但 reeval_metric=None，对账将跳过）: "
                f"{type(ree).__name__}: {ree}\n"
            )
            reeval_metric = None

        _free_model(q_model)
        sys.stderr.write(
            f"[run_bit_curve] baked selected → {baked_path} (reeval_metric={reeval_metric})\n"
        )
        return str(baked_path), reeval_metric
    except Exception as e:
        sys.stderr.write(
            f"[run_bit_curve] bake 失败（不阻断，曲线是核心产出）: "
            f"{type(e).__name__}: {e}\n"
        )
        return "", None


def _check_bake_metric_consistency(
    reeval_metric: float | None,
    search_final_score: float | None,
    metric_kind: str,
) -> None:
    """bake 后重 eval metric 与 search 内部 final.score 对账（副作用层：stderr + exit）。

    plan §P5 N7：``|baked - final| / max(|final|, abs_floor) > rel_tol(1e-4)`` → fail loud。
    任一为 None / 解析失败 → 跳过对账（不阻断，打 WARN）。
    spec-review N7：bake 失败不阻断曲线产出。

    数学部分抽到 ``_compute_bake_metric_relative_diff`` 便于单测（无副作用层）。
    """
    rel = _compute_bake_metric_relative_diff(reeval_metric, search_final_score)
    if rel is None:
        sys.stderr.write(
            f"[run_bit_curve] WARN: bake 对账跳过（reeval={reeval_metric}, "
            f"final_score={search_final_score}）—— bake 重 eval 或 search final.score 不可用"
            f" 或类型不可解析。\n"
        )
        return
    sys.stderr.write(
        f"[run_bit_curve] bake 对账: reeval={reeval_metric:.8f} final_score={search_final_score:.8f}"
        f" rel_diff={rel:.2e} (tol={_BAKE_METRIC_REL_TOL:.0e})\n"
    )
    if rel > _BAKE_METRIC_REL_TOL:
        sys.stderr.write(
            f"[run_bit_curve] FAIL LOUD: baked metric 与 search final.score 对账超 tol"
            f"（rel_diff={rel:.2e} > tol={_BAKE_METRIC_REL_TOL:.0e}, metric_kind={metric_kind}）。"
            f"这意味着 bake 出的 best_mixed_model.pt 实际 eval ≠ 报告 metric，用户拿到的是错的交付物。"
            f"请检查 quantize_model 的 bake 路径是否漏掉 observer / calib state。\n"
        )
        sys.exit(3)


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
    add_device_seed_args(ap)
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

    # device + seed 解析（单一真相源：_device.resolve_device_and_seed）
    device, seed = resolve_device_and_seed(
        args.device, args.seed, log_prefix="[run_bit_curve] "
    )

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
    # device 搬移（plan §P5）：adapter.load_model() 返回 CPU 模型，脚本统一搬到 device。
    # 必须在 TSQuantizer / search_mix_precision 之前，让 search 内部 eval 走 GPU。
    fp_model = fp_model.to(device)
    calib_loader = adapter.get_calib_loader()
    raw_forward_fn = getattr(adapter, "forward_fn", None)
    forward_fn = wrap_forward_with_device(raw_forward_fn, device)
    get_eval_loader = getattr(adapter, "get_eval_loader", None)
    if callable(get_eval_loader):
        eval_loader = get_eval_loader()
    else:
        # fail loud（plan §P5 + user brief「复用 calib 当 eval」是禁掉的造假口径）：
        # Pareto 搜索依赖业务 eval 分布选最低位宽；用 calib（代表性少量样本）会让 Pareto
        # 前沿选错。P4 哨兵到位后可退「问用户」，当前直接 exit 2。
        sys.stderr.write(
            "[run_bit_curve] FAIL LOUD: adapter 未实现 get_eval_loader（eval_data_ref 空）"
            "→ 缺评估数据。复用 calib_loader 做 eval 是禁掉的造假口径（plan §P5："
            "「复用 calib 当 eval」）——会让 Pareto 前沿在错的 metric 上选最低位宽，"
            "交付物 bit 分配有偏差。请在用户代码里找 eval loader，或在 workflow inputs "
            "显式提供 eval_data_ref。\n"
        )
        sys.exit(2)

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

    # 全候选（非仅 frontier）：从 report.archive.records 取，供 scatter 用。
    archive = report.get("archive") if isinstance(report.get("archive"), dict) else {}
    archive_records_raw = archive.get("records")
    archive_records: list[dict[str, Any]] | None = (
        archive_records_raw if isinstance(archive_records_raw, list) else None
    )
    # chart1 pareto y 方向：按 report.frontier.metric_spec.higher_is_better（权威），
    # fallback 到 _resolve_eval 本地值（同 MetricSpec）。
    pareto_y_direction = _infer_pareto_y_direction(
        frontier, higher_is_better, metric_kind
    )

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

    # bake + 改动生效对账（plan §P5 核心修复）。
    # bake 成功 → reload + 重 eval → best_metric 取 baked 实测值（非 search final.score）。
    # bake 失败/跳过 → 不阻断曲线产出（spec-review N7），best_metric 仍报 search final.score。
    # 持久化顺序（code-reviewer 🔴）：先 summary.dump(含 baked_model_path) 再对账，
    # 保证对账 fail loud exit(3) 时磁盘上 best_mixed_model.pt 与 summary 一致，
    # 不留「summary=None 但 .pt 已落盘」的孤儿态。
    baked_path_str = ""
    baked_reeval_metric: float | None = None
    bake_token = (args.bake or "").strip().lower()
    if bake_token in _TRUE_TOKENS:
        if layer_configs:
            baked_path_str, baked_reeval_metric = _bake_selected(
                fp_model,
                layer_configs,
                base_qconfig,
                calib_loader,
                forward_fn,
                output_dir / "best_mixed_model.pt",
                eval_fn,
                metric_kind,
            )
            # 持久化：bake 完成立即写 summary（含 baked_model_path + reeval_metric），
            # 再做对账。对账失败 exit(3) 时 summary 已反映磁盘真相。
            summary["baked_model_path"] = baked_path_str or None
            summary["baked_reeval_metric"] = baked_reeval_metric
            _dump_json(summary, summary_path)
            # 改动生效对账（plan §P5）：bake 成功才对账，超 tol fail loud；失败/None 跳过不阻断。
            if baked_path_str:
                _check_bake_metric_consistency(
                    baked_reeval_metric, best_metric, metric_kind
                )
        else:
            sys.stderr.write(
                "[run_bit_curve] bake: final.layer_configs 缺失 → skip bake\n"
            )
            summary["baked_model_path"] = None
            summary["baked_reeval_metric"] = None
            _dump_json(summary, summary_path)
    elif bake_token in _FALSE_TOKENS:
        sys.stderr.write("[run_bit_curve] bake=false → skip bake\n")
        summary["baked_model_path"] = None
        summary["baked_reeval_metric"] = None
        _dump_json(summary, summary_path)
    else:
        sys.stderr.write(
            f"[run_bit_curve] --bake='{args.bake}' 非法（期望 true/false/1/0/yes/no）\n"
        )
        sys.exit(2)

    # best_metric 取值优先级（plan §P5）：bake 成功且 reeval 可用 → 取 baked 实测值；
    # 否则取 search 内部 final.score（bake=false 或 bake 重 eval 失败）。
    reported_best_metric: float | None
    if baked_path_str and baked_reeval_metric is not None:
        reported_best_metric = baked_reeval_metric
        sys.stderr.write(
            f"[run_bit_curve] best_metric 取 baked 重 eval 实测值: {reported_best_metric:.8f}"
            f"（非 search final.score={best_metric}）\n"
        )
    else:
        reported_best_metric = (
            float(best_metric) if isinstance(best_metric, (int, float)) else None
        )

    # charts
    _render_charts(
        frontier_points,
        selected_id,
        metric_kind,
        selected_format_counts,
        pareto_y_direction,
        archive_records,
    )

    # stdout JSON 摘要（agent 原样回显）
    out = {
        "output_dir": str(output_dir),
        "report_path": str(summary_path),
        "model_path": args.model_path,
        "baked_model_path": baked_path_str,
        "best_config": best_config_label,
        "best_metric": reported_best_metric if reported_best_metric is not None else 0.0,
        "best_bit": float(best_bit) if isinstance(best_bit, (int, float)) else 0.0,
        "candidates_evaluated": int(eval_calls) if isinstance(eval_calls, (int, float)) else 0,
        "mode": mode,
        "metric_kind": metric_kind,
    }
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

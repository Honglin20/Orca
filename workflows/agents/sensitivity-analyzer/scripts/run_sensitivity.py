#!/usr/bin/env python3
"""run_sensitivity.py —— 量化敏感层分析 + 可视化（sensitivity_analyzer 节点调用）。

流程：import adapter → 按 method 组装参数 → 调
ts_quant.trainable.analyze_low_precision_sensitive_layers → 落盘 report.json（含模型原始层序）
→ orca.chart.render_chart 推 bar+table（按模型原始程序顺序/原始层名）→ stdout 输出 JSON 摘要。

铁律：
- method∈{ptq_binary_sensitivity, mix_precision_search} 必须有 adapter.get_eval_fn()，否则 fail loud。
- 推图 try/except 容错：report.json 是核心产出，图失败仅 stderr 提示、不阻断、不影响退出码。

注：QConfig 预设字段、后两 method 的 eval_fn/high_precision_qconfig 参数按 ts_quant
README_TRAINING.md §8 与源码 sensitivity.py 的签名组织；若 ts_quant API 调整，相应核对。
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable

COMPLEX_METHODS = {"ptq_binary_sensitivity", "mix_precision_search"}


def _load_adapter(path: str):
    """按文件路径动态 import adapter 模块。"""
    p = Path(path).resolve()
    if not p.is_file():
        sys.stderr.write(f"[run_sensitivity] adapter 不存在: {p}\n")
        sys.exit(2)
    spec = importlib.util.spec_from_file_location("ts_quant_adapter", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _qconfig(preset: str):
    """低/高精度预设字符串 → ts_quant.QConfig。"""
    from ts_quant import QConfig

    preset = (preset or "").strip().lower()
    if preset in ("w4a4-mx", "w4a4mx"):
        # 见 README_TRAINING.md §8 示例（MX-W4A4）
        return QConfig(
            method="mx",
            w_elem_format="fp4_e2m1",
            a_elem_format="fp4_e2m1",
            block_size=16,
            weight_solver="rtn",
            post_correction="none",
        )
    if preset == "int4":
        return QConfig(method="int", n_bits=4, weight_solver="rtn", post_correction="none")
    if preset == "w4a16":
        return QConfig(method="int", n_bits=4, a_elem_format="fp16",
                       weight_solver="rtn", post_correction="none")
    if preset == "w8a8":
        return QConfig(method="int", n_bits=8, weight_solver="rtn", post_correction="none")
    sys.stderr.write(
        f"[run_sensitivity] 未知预设 '{preset}'（支持 w4a4-mx / int4 / w4a16 / w8a8）\n"
    )
    sys.exit(2)


def _row_score(r: dict[str, Any]) -> float:
    """ranked_layers 每条记录的敏感度分数（多键兜底，字段名依 ts_quant 实际）。"""
    for k in ("score", "baseline_score", "sensitivity", "mse", "metric"):
        v = r.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def _row_name(r: dict[str, Any]) -> str | None:
    # 实测：ranked_layers 每条字段名为 "name"（sensitivity.py 实际产出）
    for k in ("name", "layer_name", "module", "layer"):
        v = r.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _push_charts(auto_sensitive: list[str], ranked: list[dict[str, Any]],
                 module_order: list[str]) -> None:
    """render_chart 推 bar（按模型原始顺序）+ table（入选层明细）。全程容错。"""
    try:
        from orca.chart import render_chart
    except Exception as e:  # orca 未装 / 不在 run 上下文
        sys.stderr.write(f"[run_sensitivity] 无法 import orca.chart（跳过推图）: {e}\n")
        return

    sensitive_set = set(auto_sensitive)
    score_by_name = {_row_name(r): _row_score(r) for r in ranked if _row_name(r)}

    # bar：按模型原始程序顺序（module_order），缺则退化为 ranked 顺序
    order = module_order or [_row_name(r) for r in ranked]
    bar_data = [
        {"layer": name, "score": score_by_name.get(name, 0.0),
         "status": "sensitive" if name in sensitive_set else "normal"}
        for name in order if name
    ]
    try:
        if bar_data:
            render_chart(
                chart_type="bar",
                data=bar_data,
                label="quant/sensitivity",
                title="Layer Sensitivity (by model order)",
                x="layer",
                y="score",
                hue="status",
            )
            sys.stderr.write(f"[run_sensitivity] pushed bar: {len(bar_data)} layers\n")
    except Exception as e:
        sys.stderr.write(f"[run_sensitivity] bar 推送失败（不阻断）: {e}\n")

    # table：入选敏感层明细（层名/分数/rank）
    table_rows = [
        {"layer": name, "score": score_by_name.get(name, 0.0), "rank": i}
        for i, name in enumerate(auto_sensitive, 1)
    ]
    try:
        if table_rows:
            render_chart(
                chart_type="table",
                data=table_rows,
                label="quant/sensitivity",
                title="Selected Sensitive Layers",
                columns=["layer", "score", "rank"],
            )
            sys.stderr.write(
                f"[run_sensitivity] pushed table: {len(table_rows)} selected\n"
            )
    except Exception as e:
        sys.stderr.write(f"[run_sensitivity] table 推送失败（不阻断）: {e}\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True, help="adapter.py 路径")
    ap.add_argument("--method", required=True)
    ap.add_argument("--ratio", required=True, help="0~1 浮点字符串")
    ap.add_argument("--low_bits", required=True)
    ap.add_argument("--high_bits", required=True)
    ap.add_argument("--output_dir", required=True)
    args = ap.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    method = (args.method or "").strip()
    try:
        ratio = float(args.ratio)
    except (TypeError, ValueError):
        sys.stderr.write(f"[run_sensitivity] --ratio 非浮点: {args.ratio!r}\n")
        sys.exit(2)

    # 1. adapter
    adapter = _load_adapter(args.adapter)
    model = adapter.load_model()
    calib_loader = adapter.get_calib_loader()
    forward_fn: Callable | None = getattr(adapter, "forward_fn", None)
    get_eval_fn = getattr(adapter, "get_eval_fn", None)

    low_qconfig = _qconfig(args.low_bits)

    # adapter 可选：覆盖默认分析的模块类型（默认仅 Linear；CNN/Transformer 常需加 Conv）
    get_module_types = getattr(adapter, "get_module_types", None)
    module_types = get_module_types() if callable(get_module_types) else None

    # 2. 组装参数（method 分支）
    from ts_quant.trainable import analyze_low_precision_sensitive_layers

    kwargs: dict[str, Any] = dict(model=model, low_qconfig=low_qconfig, ratio=ratio, method=method)
    if module_types:
        kwargs["module_types"] = module_types
    if method in COMPLEX_METHODS:
        eval_fn = get_eval_fn() if callable(get_eval_fn) else None
        if eval_fn is None:
            sys.stderr.write(
                f"[run_sensitivity] method={method} 需要 adapter.get_eval_fn() 返回业务评估函数\n"
            )
            sys.exit(2)
        kwargs["eval_fn"] = eval_fn
        kwargs["high_precision_qconfig"] = _qconfig(args.high_bits)
    else:
        kwargs["calib_data"] = calib_loader
        if forward_fn is not None:
            kwargs["forward_fn"] = forward_fn

    analysis = analyze_low_precision_sensitive_layers(**kwargs)

    # 3. 模型原始层序 + 落盘 report
    ranked = list(getattr(analysis, "ranked_layers", []) or [])
    auto_sensitive = list(getattr(analysis, "auto_sensitive_layers", []) or [])
    candidate_set = {_row_name(r) for r in ranked if _row_name(r)}
    module_order = [n for n, _ in model.named_modules() if n in candidate_set]

    report = {
        "method": method,
        "ratio": ratio,
        "selected_count": getattr(analysis, "selected_count", len(auto_sensitive)),
        "num_candidate_layers": getattr(analysis, "num_candidate_layers", len(ranked)),
        "auto_sensitive_layers": auto_sensitive,
        "ranked_layers": ranked,
        "module_order": module_order,
    }
    report_path = output_dir / "report.json"
    # default=str：兜底 metric_spec 等可能不可直接序列化的对象
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    # 4. 推图（容错，不阻断）
    _push_charts(auto_sensitive, ranked, module_order)

    # 5. stdout JSON 摘要（agent 原样回显）
    summary = {
        "output_dir": str(output_dir),
        "report_path": str(report_path),
        "sensitive_layers": auto_sensitive,
        "selected_count": report["selected_count"],
        "method": method,
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

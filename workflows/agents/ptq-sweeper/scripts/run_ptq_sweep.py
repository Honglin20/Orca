#!/usr/bin/env python3
"""run_ptq_sweep.py —— 粗粒度 PTQ 扫描 + bake + 可视化（ptq_sweeper 节点调用）。

流程（plan §run_ptq_sweep.py 八步）：
1. import adapter → FP teacher + calib + eval loaders + eval_fn（默认 teacher-student mse）
2. 候选网格（mode 分支）：
   - lightweight：4 条累积路径（S/Q/A/R）按 (pre, solver, post) 去重 → ~11 unique 候选
   - full：(None/Smooth/QuaRot) × (RTN/GPTQ/AutoRound) × (none/q2n) 全枚举，按 SDK §9.4
     拒绝表过滤 rtn+q2n；每个 bit_width 一份（默认 3 个）→ ~45 候选
3. 每候选 try/except 隔离：quantize_model → eval_fn → 记录 (label, bw, pre, solver, post, metric)
4. 选 best（metric_kind↓ 或 业务↑，higher_is_better 由 eval_fn 路径决定）
5. bake：torch.save(best.state_dict(), output_dir/best_quant_model.pt)
6. report.json（全候选 + best）每候选评完增量落盘
7. render_chart（容错不阻断）：lightweight=line+bar+table，full=heatmap+scatter+table
8. stdout JSON 摘要（agent 原样回显，对齐 output_schema）

铁律：
- 单候选失败不拖垮全扫（try/except 隔离 + stderr 提示 + report 增量落盘）
- 全部候选失败 → fail loud（exit 3）
- 推图失败 → stderr 提示但不阻断（report.json 是核心产出）
- AutoRound 缺 auto-round 包 → 跳过该候选 + stderr 提示，不阻断

注：QConfig 字段、granularity 约束、SmoothQuant 两遍校准按 ts_quant/README_SDK.md §9.4 +
源码 qconfig.py 组织；若 ts_quant API 调整，相应核对。
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
# 而非走 _eval_candidate try 内 → 全候选失败 exit 3（业务错，根因被埋）。
try:
    from ts_quant import QConfig, quantize_model  # noqa: F401
    from ts_quant.eval import build_teacher_student_eval_fn  # noqa: F401
    from ts_quant.plugins import QuaRotPlugin, SmoothQuantPlugin  # noqa: F401
    _TS_QUANT_OK = True
    _TS_QUANT_IMPORT_ERROR: str | None = None
except Exception as _e:  # ImportError / 二次依赖（torch 等）失败都兜住
    _TS_QUANT_OK = False
    _TS_QUANT_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

CHART_LABEL = "quant/ptq-sweep"
DEFAULT_MAX_STEPS = 64  # plan §run_ptq_sweep.py：Smooth 组合两遍校准兜底；其它路径同样限制无害
_TRUE_TOKENS = {"true", "1", "yes", "y", "on"}
_FALSE_TOKENS = {"false", "0", "no", "n", "off"}

# ─────────────────────────────────────────────────────────────────
# lightweight 4 累积路径（plan §run_ptq_sweep.py §2）
# 每条目：(step_label, pre, solver, post)
#   pre ∈ {"none","smooth","quarot"} —— 逻辑标签，等于对应 plugin
#   solver/post 对应 QConfig.weight_solver / post_correction
# ─────────────────────────────────────────────────────────────────
_LW_PATHS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    ("S", [  # Smooth 派
        ("rtn",             "none",   "rtn", "none"),
        ("rtn+smooth",      "smooth", "rtn", "none"),
        ("smooth+gptq",     "smooth", "gptq", "none"),
        ("smooth+gptq+q2n", "smooth", "gptq", "q2n"),
    ]),
    ("Q", [  # QuaRot 派
        ("rtn",              "none",   "rtn",  "none"),
        ("rtn+quarot",       "quarot", "rtn",  "none"),
        ("quarot+gptq",      "quarot", "gptq", "none"),
        ("quarot+gptq+q2n",  "quarot", "gptq", "q2n"),
    ]),
    ("A", [  # AutoRound 派
        ("rtn",           "none", "rtn",      "none"),
        ("autoround",     "none", "autoround", "none"),
        ("autoround+q2n", "none", "autoround", "q2n"),
    ]),
    ("R", [  # 纯求解派
        ("rtn",      "none", "rtn",  "none"),
        ("gptq",     "none", "gptq", "none"),
        ("gptq+q2n", "none", "gptq", "q2n"),
    ]),
]

_VALID_PRES = {"none", "smooth", "quarot"}
_VALID_SOLVERS = {"rtn", "gptq", "autoround"}
_VALID_POSTS = {"none", "q2n"}

# bit_width 预设 → QConfig 构造字段（不含 weight_solver/post_correction/granularity，
# 这些由 _make_qconfig 按候选动态填）。复用并扩展 W1 _qconfig 的语义。
_BITWIDTH_PRESETS: dict[str, dict[str, Any]] = {
    "w4a4-mx": {
        "method": "mx",
        "w_elem_format": "fp4_e2m1",
        "a_elem_format": "fp4_e2m1",
        "block_size": 16,
    },
    "w4a8-mx": {
        "method": "mx",
        "w_n_bits": 4,
        "a_n_bits": 8,
        "w_elem_format": "fp4_e2m1",
        "a_elem_format": "fp8_e4m3",
        "block_size": 16,
    },
    "w8a8-mx": {
        "method": "mx",
        "w_elem_format": "fp8_e4m3",
        "a_elem_format": "fp8_e4m3",
        "block_size": 16,
    },
    "w8a8-int": {
        "method": "int",
        "n_bits": 8,
    },
    # w4a16 真正语义：weight-only INT4 + 激活保持 FP16（a_quant_enabled=False）。
    # W1 的 w4a16 写 `a_elem_format="fp16"` 但 method=int 的 a_elem_format 不被消费
    # （to_act_quant_specs 仅看 effective_a_n_bits），实际行为退化成 w4a4。
    # 这里修正为 a_quant_enabled=False 让激活 bypass fake-quant，名副其实。
    "w4a16": {
        "method": "int",
        "n_bits": 4,
        "w_n_bits": 4,
        "a_quant_enabled": False,
    },
}


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _load_adapter(path: str):
    """按文件路径动态 import adapter 模块。"""
    p = Path(path).resolve()
    if not p.is_file():
        sys.stderr.write(f"[run_ptq_sweep] adapter 不存在: {p}\n")
        sys.exit(2)
    spec = importlib.util.spec_from_file_location("ts_quant_ptq_sweep_adapter", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _load_env_file(path: str) -> None:
    """自加载 orca_env.sh（`export K=V` 行）到 os.environ——opencode bash 工具不跨调用保 env，
    若子代理把 `source orca_env.sh` 和 `python3` 拆成两次调用，脚本运行的 shell 就没有
    `ORCA_CHART_SOCK` → render_chart raise 被静默吞 → 图不推。本兜底从 `--env_file` 指定的
    orca_env.sh 把缺失的 ORCA_* 补进 os.environ，使 render_chart 无论子代理怎么拆 bash 都能连上
    chart daemon。已存在的 env 不覆盖（显式 env 优先）。
    """
    import re
    if not path:
        return
    p = Path(path).expanduser()
    if not p.is_file():
        sys.stderr.write(f"[run_ptq_sweep] --env_file 不存在: {p}（跳过自加载）\n")
        return
    cnt = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
        os.environ.setdefault(k, v)
        cnt += 1
    sys.stderr.write(f"[run_ptq_sweep] 自加载 {cnt} 个 env from {p}\n")


def _autoround_available() -> bool:
    """auto-round 是否安装（README_SDK §9.4：autoround 需可选依赖 auto-round）。"""
    try:
        import auto_round  # noqa: F401
        return True
    except ImportError:
        return False


def _make_qconfig(bit_width: str, solver: str, post: str):
    """bit_width 预设 + solver + post → ts_quant.QConfig。

    SDK §9.4 合法性约束（qconfig.py __post_init__）：
      - INT + gptq → granularity∈{per_token, per_channel}（默认 per_tensor 会被拒）
      - INT + autoround → granularity=per_token（默认 per_tensor 会被拒）
      - MX 三 solver 默认 per_tensor 都合法（MX 有自己的 block 方案）
      - post=q2n 只接 gptq/autoround（rtn+q2n 不会到这里——候选构建已过滤）
    """
    from ts_quant import QConfig

    preset = _BITWIDTH_PRESETS.get(bit_width)
    if preset is None:
        sys.stderr.write(
            f"[run_ptq_sweep] 未知位宽预设 '{bit_width}'"
            f"（支持：{sorted(_BITWIDTH_PRESETS)}）\n"
        )
        sys.exit(2)

    fields = dict(preset)
    fields["weight_solver"] = solver
    fields["post_correction"] = post
    # INT + 非 rtn solver 必须改 granularity（否则 QConfig raise，candidate 整个失败）
    if fields.get("method") == "int" and solver in ("gptq", "autoround"):
        fields["granularity"] = "per_token"
    return QConfig(**fields)


def _select_lw_paths(recipes_filter: list[str]) -> list[tuple[str, list[tuple[str, str, str, str]]]]:
    """lightweight 路径过滤：recipes_filter 空或无匹配 → 全 4 条；否则按 S/Q/A/R 子集。

    build_candidate 与 push_chart 共用，避免双写不一致。
    """
    if not recipes_filter:
        return list(_LW_PATHS)
    sel = set(recipes_filter) & {p for p, _ in _LW_PATHS}
    if not sel:
        sys.stderr.write(
            f"[run_ptq_sweep] recipes 过滤无匹配路径 {recipes_filter} → fallback 全 4 条\n"
        )
        return list(_LW_PATHS)
    return [(p, s) for p, s in _LW_PATHS if p in sel]


# LW_PATHS 原序（A/Q/R/S 字母序不自然，按定义序 S→Q→A→R 用于图例/柱状排序）
_LW_PATH_ORDER = {p: i for i, (p, _) in enumerate(_LW_PATHS)}


def _build_plugins(pre: str) -> list[Any]:
    """pre 标签 → plugins 列表（SmoothQuant / QuaRot；none → 空）。"""
    if pre == "smooth":
        return [SmoothQuantPlugin()]
    if pre == "quarot":
        return [QuaRotPlugin()]
    if pre == "none":
        return []
    sys.stderr.write(f"[run_ptq_sweep] 未知 pre='{pre}'（支持 none/smooth/quarot）\n")
    sys.exit(2)


# ─────────────────────────────────────────────────────────────────
# 候选构建
# ─────────────────────────────────────────────────────────────────

def _parse_csv(raw: str, fallback: list[str]) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return list(fallback)
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def _build_lw_candidates(bit_widths: list[str], recipes_filter: list[str]) -> list[dict[str, Any]]:
    """Lightweight 4 路径候选，按 (pre, solver, post) 去重。

    rtn 在 4 条 path 都出现 → 全局只跑一次；line 图数据点由 path_step 反查映射。
    """
    if not bit_widths:
        bit_widths = ["w4a4-mx"]
    bw = bit_widths[0]
    if len(bit_widths) > 1:
        sys.stderr.write(
            f"[run_ptq_sweep] lightweight 模式只用首个位宽 '{bw}'，忽略 {bit_widths[1:]}\n"
        )

    paths = _select_lw_paths(recipes_filter)

    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for _, steps in paths:
        for step_label, pre, solver, post in steps:
            key = (pre, solver, post)
            if key in unique:
                continue
            unique[key] = {
                "label": f"{step_label}@{bw}",
                "bit_width": bw,
                "pre": pre,
                "solver": solver,
                "post": post,
            }
    return list(unique.values())


def _build_full_candidates(bit_widths: list[str], recipes_filter: list[str]) -> list[dict[str, Any]]:
    """Full 全枚举：(None/Smooth/QuaRot) × (RTN/GPTQ/AutoRound) × (none/q2n)。

    SDK §9.4 拒绝表过滤：rtn + q2n（后处理只接 gptq/autoround）。
    AutoRound 与 GPTQ 互斥是同一 QConfig 字段的取值，这里单选不会撞。
    FP8/SMX 不在本 workflow 范围（只做 MX/INT）。
    """
    if not bit_widths:
        bit_widths = ["w4a4-mx", "w4a8-mx", "w8a8-mx"]

    # recipes_filter 解析：'all' 或空 → 全集；否则按 pre/solver 子集过滤
    filter_pres: set[str] | None = None
    filter_solvers: set[str] | None = None
    if recipes_filter and recipes_filter != ["all"]:
        filter_pres = set()
        filter_solvers = set()
        for tok in recipes_filter:
            if tok in _VALID_PRES:
                filter_pres.add(tok)
            elif tok in _VALID_SOLVERS:
                filter_solvers.add(tok)
            else:
                sys.stderr.write(
                    f"[run_ptq_sweep] recipes token '{tok}' 无法识别（忽略）\n"
                )
        if not filter_pres:
            filter_pres = None
        if not filter_solvers:
            filter_solvers = None

    candidates: list[dict[str, Any]] = []
    for bw in bit_widths:
        for pre in ("none", "smooth", "quarot"):
            if filter_pres is not None and pre not in filter_pres:
                continue
            for solver in ("rtn", "gptq", "autoround"):
                if filter_solvers is not None and solver not in filter_solvers:
                    continue
                for post in ("none", "q2n"):
                    if solver == "rtn" and post == "q2n":
                        continue  # SDK 拒绝：RTN 后处理仅支持 none
                    parts = []
                    if pre != "none":
                        parts.append(pre)
                    parts.append(solver)
                    if post == "q2n":
                        parts.append(post)
                    candidates.append({
                        "label": "+".join(parts) + f"@{bw}",
                        "bit_width": bw,
                        "pre": pre,
                        "solver": solver,
                        "post": post,
                    })
    return candidates


def _build_candidates(mode: str, bit_widths: list[str], recipes_filter: list[str]) -> list[dict[str, Any]]:
    if mode == "lightweight":
        return _build_lw_candidates(bit_widths, recipes_filter)
    if mode == "full":
        return _build_full_candidates(bit_widths, recipes_filter)
    sys.stderr.write(
        f"[run_ptq_sweep] mode 非法 '{mode}'（支持 lightweight / full）\n"
    )
    sys.exit(2)


# ─────────────────────────────────────────────────────────────────
# Eval 流水
# ─────────────────────────────────────────────────────────────────

def _resolve_eval(adapter, fp_model, eval_loader, forward_fn) -> tuple[Callable, str, bool]:
    """返回 (eval_fn, metric_kind, higher_is_better)。

    - adapter.get_eval_fn() 存在且返回非 None → 业务路径，需 adapter.get_metric_spec() 告知
      metric/direction
    - 否则 → 默认 teacher-student mse（lower is better）；forward_fn 必填（否则 SDK 用
      build_auto_forward_fn 对异构 batch dict/tuple 静默误算）→ fail loud
    """
    get_eval_fn = getattr(adapter, "get_eval_fn", None)
    business_fn = get_eval_fn() if callable(get_eval_fn) else None
    if business_fn is not None:
        get_metric_spec = getattr(adapter, "get_metric_spec", None)
        if not callable(get_metric_spec):
            sys.stderr.write(
                "[run_ptq_sweep] 业务 eval_fn 路径需要 adapter.get_metric_spec() "
                "返回 {primary_metric: str, higher_is_better: bool}\n"
            )
            sys.exit(2)
        spec = get_metric_spec() or {}
        metric_kind = spec.get("primary_metric")
        if not metric_kind:
            sys.stderr.write(
                f"[run_ptq_sweep] get_metric_spec() 缺 primary_metric: {spec}\n"
            )
            sys.exit(2)
        return business_fn, str(metric_kind), bool(spec.get("higher_is_better", False))

    if forward_fn is None:
        sys.stderr.write(
            "[run_ptq_sweep] 默认 teacher-student eval 路径需要 adapter.forward_fn "
            "（按模型 forward 解包 batch）—— 异构 batch 会让 SDK fallback 误算\n"
        )
        sys.exit(2)
    eval_fn = build_teacher_student_eval_fn(
        teacher_model=fp_model,
        dataloader=eval_loader,
        forward_fn=forward_fn,
    )
    return eval_fn, "mse", False


def _is_better(new_metric: float, cur_metric: float, higher_is_better: bool) -> bool:
    if higher_is_better:
        return new_metric > cur_metric
    return new_metric < cur_metric


def _eval_candidate(
    fp_model,
    cand: dict[str, Any],
    calib_loader,
    forward_fn,
    eval_fn,
    metric_kind: str,
) -> dict[str, Any]:
    """单候选 quantize_model + eval。返回结果 dict（含 _q_model 内部字段，dump 前剥离）。"""
    import torch  # noqa: F401  —— eval_fn 内部依赖 torch；早 import 利于 ImportError 显式失败

    label = cand["label"]
    t0 = time.time()
    result: dict[str, Any] = {
        "config_label": label,
        "bit_width": cand["bit_width"],
        "pre_transform": cand["pre"],
        "weight_solver": cand["solver"],
        "post_correction": cand["post"],
        "metric": None,
        "metric_kind": metric_kind,
        "status": "error",
        "error": None,
        "elapsed_seconds": 0.0,
    }

    q_model = None
    try:
        qconfig = _make_qconfig(cand["bit_width"], cand["solver"], cand["post"])
        plugins = _build_plugins(cand["pre"])
        # deepcopy：quantize_model(inplace=True) 会改模型，跨候选必须从干净 FP 开始
        model_copy = copy.deepcopy(fp_model)
        q_model = quantize_model(
            model=model_copy,
            qconfig=qconfig,
            calib_data=calib_loader,
            forward_fn=forward_fn,
            plugins=plugins,
            max_steps=DEFAULT_MAX_STEPS,  # smooth 两遍校准兜底；其它路径无害
            inplace=True,
        )
        metrics = eval_fn(q_model)
        if not isinstance(metrics, dict) or metric_kind not in metrics:
            raise KeyError(
                f"eval_fn 返回的 metrics 缺 '{metric_kind}' 键（得到 "
                f"{sorted(metrics.keys()) if isinstance(metrics, dict) else type(metrics).__name__}）"
            )
        result["metric"] = float(metrics[metric_kind])
        result["status"] = "ok"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        sys.stderr.write(f"[run_ptq_sweep] candidate {label} failed: {result['error']}\n")
        q_model = None  # 失败 → 释放，不留句柄

    result["elapsed_seconds"] = round(time.time() - t0, 3)
    result["_q_model"] = q_model  # 内部字段（dump 前剥离）
    return result


def _free_q_model(q_model) -> None:
    """显式释放 quantized model，触发 Python GC + CUDA cache 回收。"""
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


def _dump_report(report: dict[str, Any], path: Path) -> None:
    """原子落盘 report.json（写 tmp → os.replace，中断不留半个 JSON；plan §6 核心产出）。"""
    payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


# ─────────────────────────────────────────────────────────────────
# 可视化
# ─────────────────────────────────────────────────────────────────

def _recipe_label(r: dict[str, Any]) -> str:
    """full 模式：把 pre/solver/post 合成 recipe 标签（去 bit_width）。"""
    parts = []
    if r["pre_transform"] != "none":
        parts.append(r["pre_transform"])
    parts.append(r["weight_solver"])
    if r["post_correction"] == "q2n":
        parts.append(r["post_correction"])
    return "+".join(parts)


def _push_table(render_chart, rows: list[dict[str, Any]], title: str) -> None:
    try:
        render_chart(
            chart_type="table",
            data=rows,
            label=CHART_LABEL,
            title=title,
            columns=list(rows[0].keys()) if rows else [],
        )
        sys.stderr.write(f"[run_ptq_sweep] pushed table: {len(rows)} rows\n")
    except Exception as e:
        sys.stderr.write(f"[run_ptq_sweep] table 推送失败（不阻断）: {e}\n")


def _push_lw_charts(render_chart, candidates: list[dict[str, Any]],
                     all_results: list[dict[str, Any]],
                     ok_results: list[dict[str, Any]], metric_kind: str,
                     recipes_filter: list[str]) -> None:
    """lightweight 模式：line（累积曲线）+ bar（终点对比）+ table（全部步）。

    table 用 all_results（含 failed/skipped）便于诊断「哪些 recipe 因依赖缺失被跳过」；
    line/bar 仍用 ok_results（无 metric 的失败候选画不进 y 轴）。
    """
    # 反查表：(pre, solver, post) → unique 候选 label（line + bar 共用，避免双构造）
    by_key: dict[tuple[str, str, str], str] = {
        (c["pre"], c["solver"], c["post"]): c["label"] for c in candidates
    }
    paths = _select_lw_paths(recipes_filter)
    sel_paths = {p for p, _ in paths}
    metric_by_label = {r["config_label"]: r["metric"] for r in ok_results}

    # line：每条 path 一条 series，x=step_idx（共享累积步序 0,1,2,...），y=metric, hue=path。
    # 用 step_idx 而非 step_label：step_label 是路径私有的（S 有 "smooth+gptq"、R 有 "gptq"），
    # 4 条路径只在 baseline "rtn" 共享 x 位 → 画成散段不是累积曲线。step_idx 让所有路径
    # 左→右按「累积了几项技术」对齐（0=baseline，1=加第一项，…），才是 ablation 对比语义。
    # step_label 仍留在 record 里供 tooltip 展示具体技术名。
    line_data: list[dict[str, Any]] = []
    for path_label, steps in paths:
        for step_idx, (step_label, pre, solver, post) in enumerate(steps):
            key = (pre, solver, post)
            label = by_key.get(key)
            if label is None or label not in metric_by_label:
                continue  # 该步被过滤或对应候选失败 → 跳过该点
            line_data.append({
                "path": path_label,
                "step_idx": step_idx,
                "step_label": step_label,
                "metric": metric_by_label[label],
            })
    try:
        if line_data:
            render_chart(
                chart_type="line",
                data=line_data,
                label=CHART_LABEL,
                title=f"Cumulative PTQ Path Ablation ({metric_kind})",
                x="step_idx",
                y="metric",
                hue="path",
            )
            sys.stderr.write(f"[run_ptq_sweep] pushed line: {len(line_data)} points\n")
    except Exception as e:
        sys.stderr.write(f"[run_ptq_sweep] line 推送失败（不阻断）: {e}\n")

    # bar：每条 path 终点（step_idx 最大）metric 对比
    final_by_path: dict[str, dict[str, Any]] = {}
    for path_label, steps in paths:
        for step_idx, (step_label, pre, solver, post) in enumerate(steps):
            key = (pre, solver, post)
            label = by_key.get(key)
            if label is None or label not in metric_by_label:
                continue
            cur = final_by_path.get(path_label)
            if cur is None or step_idx > cur["step_idx"]:
                final_by_path[path_label] = {
                    "step_idx": step_idx,
                    "path": path_label,
                    "metric": metric_by_label[label],
                    "final_config": label,
                }
    # 按 _LW_PATHS 原序（S→Q→A→R）而非字母序（A→Q→R→S）。单 series——曾用 hue="final_config"
    # 但每 path 独一份 final_config → 每 x 单独着色 + 一堆无意义图例（同 sensitivity hue 反模式）。
    # final_config 字段保留在 record 里供 tooltip 展示具体技术组合。
    bar_data = sorted(final_by_path.values(), key=lambda x: _LW_PATH_ORDER.get(x["path"], 99))
    try:
        if bar_data:
            render_chart(
                chart_type="bar",
                data=bar_data,
                label=CHART_LABEL,
                title=f"Final-step Comparison by Path ({metric_kind})",
                x="path",
                y="metric",
            )
            sys.stderr.write(f"[run_ptq_sweep] pushed bar: {len(bar_data)} paths\n")
    except Exception as e:
        sys.stderr.write(f"[run_ptq_sweep] bar 推送失败（不阻断）: {e}\n")

    # table：全候选明细（含 failed/skipped）。原仅展示 ok_results 致诊断时看不出
    # 「哪些 recipe 因依赖缺失（如 auto-round 未装）被跳过」—— 改用 all_results。
    table_rows = sorted(all_results, key=lambda r: r["config_label"])
    _push_table(render_chart, [
        {"config": r["config_label"], "pre": r["pre_transform"],
         "solver": r["weight_solver"], "post": r["post_correction"],
         "metric": r["metric"], "elapsed_s": r["elapsed_seconds"],
         "status": r["status"], "error": r["error"] or ""}
        for r in table_rows
    ], "Lightweight Sweep — All Steps (incl. failed/skipped)")


def _push_full_charts(render_chart, all_results: list[dict[str, Any]],
                       ok_results: list[dict[str, Any]], metric_kind: str,
                       best_label: str | None) -> None:
    """full 模式：heatmap（recipe×bitwidth）+ scatter（best 高亮）+ table（全部候选）。

    heatmap/scatter 需数值 metric，仍用 ok_results；table 用 all_results 含失败/跳过。
    scatter 用 per-row ``color`` 高亮 best 候选——``_render`` 契约：hue 非空时 color 被
    忽略，故 best 高亮时必须去 ``hue=recipe`` 改 ``color="color"``（heatmap 已表达 recipe 维度）。
    """
    # heatmap / scatter 公共数据 shape：{recipe, bitwidth, metric, config_label}
    # config_label 仅供 scatter 匹配 best 行；heatmap 不消费该字段（只看 x/y/value）。
    matrix_data: list[dict[str, Any]] = [
        {"recipe": _recipe_label(r), "bitwidth": r["bit_width"],
         "metric": r["metric"], "config_label": r["config_label"]}
        for r in ok_results
    ]
    try:
        if matrix_data:
            render_chart(
                chart_type="heatmap",
                data=matrix_data,
                label=CHART_LABEL,
                title=f"PTQ Recipe × Bitwidth Matrix ({metric_kind})",
                x="bitwidth",
                y="recipe",
                value="metric",
            )
            sys.stderr.write(f"[run_ptq_sweep] pushed heatmap: {len(matrix_data)} cells\n")
    except Exception as e:
        sys.stderr.write(f"[run_ptq_sweep] heatmap 推送失败（不阻断）: {e}\n")

    # scatter：统一单 series + per-row color。best 点珊瑚色，其余钢蓝色。
    _BEST_COLOR = "#D4605A"
    _DEFAULT_COLOR = "#5B8DB8"
    for row in matrix_data:
        row["color"] = _BEST_COLOR if row["config_label"] == best_label else _DEFAULT_COLOR
    try:
        if matrix_data:
            render_chart(
                chart_type="scatter",
                data=matrix_data,
                label=CHART_LABEL,
                title=f"PTQ Metric by Bitwidth ({metric_kind}, coral=best)",
                x="bitwidth",
                y="metric",
                color="color",
            )
            sys.stderr.write(f"[run_ptq_sweep] pushed scatter: {len(matrix_data)} points\n")
    except Exception as e:
        sys.stderr.write(f"[run_ptq_sweep] scatter 推送失败（不阻断）: {e}\n")

    # table：全候选（含 failed/skipped），与 lightweight table 同理。
    table_rows = sorted(all_results, key=lambda r: (r["bit_width"], _recipe_label(r)))
    _push_table(render_chart, [
        {"bitwidth": r["bit_width"], "recipe": _recipe_label(r),
         "config": r["config_label"], "metric": r["metric"],
         "elapsed_s": r["elapsed_seconds"],
         "status": r["status"], "error": r["error"] or ""}
        for r in table_rows
    ], "Full Sweep — All Combos (incl. failed/skipped)")


def _push_charts(mode: str, candidates: list[dict[str, Any]],
                  report: dict[str, Any], metric_kind: str,
                  recipes_filter: list[str]) -> None:
    try:
        from orca.chart import render_chart
    except Exception as e:
        sys.stderr.write(f"[run_ptq_sweep] 无法 import orca.chart（跳过推图）: {e}\n")
        return

    all_results = report["candidates"]
    ok_results = [c for c in all_results if c.get("status") == "ok"]
    if not ok_results:
        sys.stderr.write("[run_ptq_sweep] 无成功候选 → 不推图\n")
        return

    # best_label：full scatter 高亮用（main 已按 higher_is_better 选好，这里直接读不重算）。
    best = report.get("best") or {}
    best_label = best.get("config_label")

    if mode == "lightweight":
        _push_lw_charts(render_chart, candidates, all_results, ok_results,
                        metric_kind, recipes_filter)
    else:
        _push_full_charts(render_chart, all_results, ok_results, metric_kind,
                          best_label)


# ─────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True, help="adapter.py 路径")
    ap.add_argument("--model_path", required=True, help="原始模型入口路径（仅用于回显摘要）")
    ap.add_argument("--project_root", required=True, help="用户 PyTorch 项目根目录")
    ap.add_argument("--calib_data_ref", required=True, help="校准 loader dotted-path（可空串）")
    ap.add_argument("--eval_data_ref", required=True, help="评估 loader dotted-path（可空串）")
    ap.add_argument("--eval_fn_ref", required=True, help="业务 eval_fn dotted-path（可空串）")
    ap.add_argument("--mode", required=True, help="lightweight / full")
    ap.add_argument("--bit_widths", required=True, help="逗号分隔位宽预设（可空串）")
    ap.add_argument("--recipes", required=True, help="路径/配方子集（可空串）")
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--bake", required=True, help="true / false")
    ap.add_argument(
        "--env_file",
        default="",
        help="本 run 的 orca_env.sh 路径；脚本启动自加载 ORCA_* env（兜底：opencode bash 拆调用会丢 env，致 render_chart 连不上 chart daemon）",
    )
    args = ap.parse_args()
    _load_env_file(args.env_file)  # 兜底：防 bash 拆调用丢 ORCA_CHART_SOCK → render_chart 静默失败

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "report.json"

    # 顶层校验：ts_quant 缺包 → exit 2（环境错）而非走到候选 try 内 → exit 3（业务错）
    if not _TS_QUANT_OK:
        sys.stderr.write(
            f"[run_ptq_sweep] ts_quant import 失败（环境错，exit 2）: {_TS_QUANT_IMPORT_ERROR}\n"
        )
        sys.exit(2)

    mode = (args.mode or "lightweight").strip().lower()
    bit_widths = _parse_csv(args.bit_widths, fallback=[])
    recipes_filter = _parse_csv(args.recipes, fallback=[])
    candidates = _build_candidates(mode, bit_widths, recipes_filter)
    if not candidates:
        sys.stderr.write(
            f"[run_ptq_sweep] 候选网格为空（mode={mode} bw={bit_widths} recipes={recipes_filter}）\n"
        )
        sys.exit(2)

    # 1. adapter → fp teacher + calib + eval + forward
    adapter = _load_adapter(args.adapter)
    fp_model = adapter.load_model()
    calib_loader = adapter.get_calib_loader()
    forward_fn = getattr(adapter, "forward_fn", None)

    get_eval_loader = getattr(adapter, "get_eval_loader", None)
    if callable(get_eval_loader):
        eval_loader = get_eval_loader()
    else:
        eval_loader = calib_loader  # plan：eval_data_ref 空则复用 calib

    eval_fn, metric_kind, higher_is_better = _resolve_eval(
        adapter, fp_model, eval_loader, forward_fn
    )

    sys.stderr.write(
        f"[run_ptq_sweep] mode={mode} candidates={len(candidates)} "
        f"metric_kind={metric_kind} higher_is_better={higher_is_better}\n"
    )

    autoround_ok = _autoround_available()
    if not autoround_ok:
        sys.stderr.write(
            "[run_ptq_sweep] auto-round 未安装 → 含 autoround 的候选将被跳过（stderr 提示）\n"
        )

    # 3-4. 候选扫描 + best 选择（增量 dump）
    report: dict[str, Any] = {
        "mode": mode,
        "metric_kind": metric_kind,
        "higher_is_better": higher_is_better,
        "bit_widths": sorted({c["bit_width"] for c in candidates}),
        "recipes_filter": recipes_filter,
        "model_path": args.model_path,
        "candidates": [],
        "best": None,
        "baked_model_path": None,
    }
    _dump_report(report, report_path)

    best: dict[str, Any] | None = None  # {label, metric, q_model, candidate}
    for cand in candidates:
        # AutoRound 缺包 → 标记 skipped 不入量化
        if cand["solver"] == "autoround" and not autoround_ok:
            skipped = {
                "config_label": cand["label"],
                "bit_width": cand["bit_width"],
                "pre_transform": cand["pre"],
                "weight_solver": cand["solver"],
                "post_correction": cand["post"],
                "metric": None,
                "metric_kind": metric_kind,
                "status": "skipped",
                "error": "auto-round package not installed",
                "elapsed_seconds": 0.0,
            }
            report["candidates"].append(skipped)
            _dump_report(report, report_path)
            sys.stderr.write(
                f"[run_ptq_sweep] skipped {cand['label']} (auto-round 未安装)\n"
            )
            continue

        result = _eval_candidate(
            fp_model, cand, calib_loader, forward_fn, eval_fn, metric_kind
        )
        q_model = result.pop("_q_model", None)
        report["candidates"].append(result)
        _dump_report(report, report_path)  # 增量落盘：崩了能看到已扫部分

        if result["status"] == "ok":
            if best is None or _is_better(result["metric"], best["metric"], higher_is_better):
                old_q = best["q_model"] if best else None
                best = {
                    "label": result["config_label"],
                    "metric": result["metric"],
                    "q_model": q_model,
                    "candidate": dict(result),
                }
                q_model = None  # 转交 best
                if old_q is not None:
                    _free_q_model(old_q)
                sys.stderr.write(
                    f"[run_ptq_sweep] new best: {best['label']} → {best['metric']:.6f}\n"
                )
            else:
                _free_q_model(q_model)
        else:
            # 失败候选：显式释放
            _free_q_model(q_model)

    if best is None:
        sys.stderr.write(
            "[run_ptq_sweep] 无成功候选（全部失败/跳过）→ fail loud (exit 3)\n"
        )
        _dump_report(report, report_path)
        sys.exit(3)

    # 6. report best 字段先写入 + dump（即使 bake 失败 report 仍完整）
    report["best"] = {
        "config_label": best["label"],
        "metric": best["metric"],
        **{k: v for k, v in best["candidate"].items()},
    }
    _dump_report(report, report_path)

    # 5. bake（在 best 字段落盘之后，失败不丢 report）
    baked_path_str: str | None = None
    bake_token = (args.bake or "").strip().lower()
    if bake_token in _TRUE_TOKENS:
        import torch
        baked_path = output_dir / "best_quant_model.pt"
        torch.save(best["q_model"].state_dict(), baked_path)
        baked_path_str = str(baked_path)
        report["baked_model_path"] = baked_path_str
        _dump_report(report, report_path)
        sys.stderr.write(f"[run_ptq_sweep] baked best → {baked_path_str}\n")
    elif bake_token in _FALSE_TOKENS:
        sys.stderr.write("[run_ptq_sweep] bake=false → skip bake\n")
    else:
        sys.stderr.write(
            f"[run_ptq_sweep] --bake='{args.bake}' 非法（期望 true/false/1/0/yes/no）\n"
        )
        sys.exit(2)

    # 7. charts
    _push_charts(mode, candidates, report, metric_kind, recipes_filter)

    # 显式释放 best q_model（caller 引用置 None，配合 _free_q_model 的 gc/cuda 回收）
    best_q = best["q_model"]
    best["q_model"] = None
    _free_q_model(best_q)

    # 8. stdout JSON 摘要（agent 原样回显）
    summary = {
        "output_dir": str(output_dir),
        "report_path": str(report_path),
        "model_path": args.model_path,
        # bake 交付物路径（bake=false 或 bake 失败时为空串）。明指交付物，原先只埋在 report.json。
        "baked_model_path": report.get("baked_model_path") or "",
        "best_config": best["label"],
        "best_metric": best["metric"],
        "candidates_evaluated": len(report["candidates"]),
        "mode": mode,
        "metric_kind": metric_kind,
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""run_qat.py —— 量化感知训练（QAT）+ CAGE 后校正（qat-trainer 节点调用）。

流程：
1. import adapter → FP teacher + calib + train + eval loaders + eval_fn（默认 teacher-student mse）
2. scheme 集合（rtn / duquantpp / both）
3. 逐 scheme（try/except 隔离，单 scheme 失败不拖垮）：
   a. prepare_trainable_fakequant_model(copy(fp), scheme, qconfig, [duquantpp: calib/forward_fn/DuQuantPPConfig])
   b. eval BEFORE（fake-quant 基线 ≈ PTQ 精度）
   c. optimizer=Adam(q_model.parameters(), lr) → prepare_trainable_qat(q_model, optimizer, total_steps, cage)
   d. 训练 loop：teacher-student mse（loss=mse(q(batch), fp(batch).detach())，无需真实 label，
      distillation-style QAT）；opt.step() 后 qat.step()；每 period 步 eval_fn 记曲线点
   e. eval AFTER → recovery = after − before
4. 选 best（after_metric 最优）
5. bake best q_model.state_dict() → best_qat_model.pt
6. report.json（per-scheme before/after/recovery/curve + best）
7. render_chart（容错不阻断）：line（per-step 收敛，每 scheme 一条）+ bar（前/后精度）+ table
8. stdout JSON 摘要（agent 原样回显，对齐 output_schema）

铁律：
- 全 scheme 失败 → fail loud（exit 3）
- bake 失败 → stderr + 空串 baked_model_path，不阻断（曲线/对比是核心产出）
- 推图失败 → stderr 提示但不阻断（report.json 是核心产出）
- 训练 loss 用 teacher-student mse（默认 eval 也是 teacher-student mse，同口径）；
  业务 eval_fn 路径时训练仍用 teacher-student mse（label-free QAT），eval 用业务指标

注：API 按 ts_quant.trainable（探针 2026-07-20 实证）组织；若 ts_quant API 调整，相应核对。
"""

from __future__ import annotations

import argparse
import copy
import gc
import importlib.util
import itertools
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

# 一次性 import ts_quant 关键依赖：缺包/未配置 → fail loud exit 2（环境错）。
try:
    import torch
    import torch.nn as nn
    from ts_quant import QConfig
    from ts_quant.duquantpp import DuQuantPPConfig
    from ts_quant.eval import build_teacher_student_eval_fn
    from ts_quant.trainable import prepare_trainable_fakequant_model
    from ts_quant.trainable.qat import prepare_trainable_qat
    _TS_QUANT_OK = True
    _TS_QUANT_IMPORT_ERROR: str | None = None
except Exception as _e:  # ImportError / 二次依赖（torch 等）失败都兜住
    _TS_QUANT_OK = False
    _TS_QUANT_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

CHART_LABEL = "quant/qat"
_TRUE_TOKENS = {"true", "1", "yes", "y", "on"}
_FALSE_TOKENS = {"false", "0", "no", "n", "off"}

# bit_width 预设 → QConfig 构造字段（与 W2 _BITWIDTH_PRESETS 同源语义；QAT 默认 w8a8-mx）。
_BITWIDTH_PRESETS: dict[str, dict[str, Any]] = {
    "w4a4-mx": {"method": "mx", "w_elem_format": "fp4_e2m1", "a_elem_format": "fp4_e2m1", "block_size": 16},
    "w4a8-mx": {"method": "mx", "w_n_bits": 4, "a_n_bits": 8,
                "w_elem_format": "fp4_e2m1", "a_elem_format": "fp8_e4m3", "block_size": 16},
    "w8a8-mx": {"method": "mx", "w_elem_format": "fp8_e4m3", "a_elem_format": "fp8_e4m3", "block_size": 16},
    "w8a8-int": {"method": "int", "n_bits": 8},
    "w4a16": {"method": "int", "n_bits": 4, "w_n_bits": 4, "a_quant_enabled": False},
}


def _make_qconfig(bit_width: str):
    preset = _BITWIDTH_PRESETS.get(bit_width)
    if preset is None:
        sys.stderr.write(
            f"[run_qat] 未知位宽预设 '{bit_width}'（支持：{sorted(_BITWIDTH_PRESETS)}）\n"
        )
        sys.exit(2)
    return QConfig(**preset)


def _load_adapter(path: str):
    p = Path(path).resolve()
    if not p.is_file():
        sys.stderr.write(f"[run_qat] adapter 不存在: {p}\n")
        sys.exit(2)
    spec = importlib.util.spec_from_file_location("ts_quant_qat_adapter", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _load_env_file(path: str) -> None:
    """自加载 orca_env.sh 到 os.environ（兜底 opencode bash 拆调用丢 ORCA_CHART_SOCK）。"""
    import re

    if not path:
        return
    p = Path(path).expanduser()
    if not p.is_file():
        sys.stderr.write(f"[run_qat] --env_file 不存在: {p}（跳过自加载）\n")
        return
    cnt = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
        os.environ.setdefault(k, v)
        cnt += 1
    sys.stderr.write(f"[run_qat] 自加载 {cnt} 个 env from {p}\n")


def _resolve_eval(adapter, fp_model, eval_loader, forward_fn) -> tuple[Callable, str, bool]:
    """返回 (eval_fn, metric_kind, higher_is_better)。契约同 W2/W3。"""
    get_eval_fn = getattr(adapter, "get_eval_fn", None)
    business_fn = get_eval_fn() if callable(get_eval_fn) else None
    if business_fn is not None:
        get_metric_spec = getattr(adapter, "get_metric_spec", None)
        if not callable(get_metric_spec):
            sys.stderr.write(
                "[run_qat] 业务 eval_fn 路径需要 adapter.get_metric_spec() "
                "返回 {primary_metric: str, higher_is_better: bool}\n"
            )
            sys.exit(2)
        spec = get_metric_spec() or {}
        metric_kind = spec.get("primary_metric")
        if not metric_kind:
            sys.stderr.write(f"[run_qat] get_metric_spec() 缺 primary_metric: {spec}\n")
            sys.exit(2)
        return business_fn, str(metric_kind), bool(spec.get("higher_is_better", False))

    if forward_fn is None:
        sys.stderr.write(
            "[run_qat] 默认 teacher-student eval 路径需要 adapter.forward_fn\n"
        )
        sys.exit(2)
    # WARN 不静默（plan §P5）：未提供业务 eval_fn → 退 teacher-student mse，精度仅自洽性参考。
    sys.stderr.write(
        "[run_qat] WARN: 未提供业务 eval_fn（eval_fn_ref 空）→ 退 teacher-student mse。"
        "该指标仅自洽性参考（q_model vs FP teacher 的 mse），不代表业务精度。\n"
    )
    eval_fn = build_teacher_student_eval_fn(
        teacher_model=fp_model, dataloader=eval_loader, forward_fn=forward_fn
    )
    return eval_fn, "mse", False


def _dump_json(obj: dict[str, Any], path: Path) -> None:
    payload = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def _free_model(q_model) -> None:
    if q_model is None:
        return
    try:
        del q_model
    except Exception:
        pass
    gc.collect()
    try:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _is_better(new_metric: float, cur_metric: float, higher_is_better: bool) -> bool:
    if higher_is_better:
        return new_metric > cur_metric
    return new_metric < cur_metric


def _eval_metric(eval_fn, q_model, metric_kind: str) -> float:
    """跑一次 eval_fn，抽 metric_kind 字段。失败 raise。"""
    metrics = eval_fn(q_model)
    if not isinstance(metrics, dict) or metric_kind not in metrics:
        raise KeyError(
            f"eval_fn 返回缺 '{metric_kind}' 键（得到 "
            f"{sorted(metrics.keys()) if isinstance(metrics, dict) else type(metrics).__name__}）"
        )
    return float(metrics[metric_kind])


def _train_cycle(loader):
    """无限循环产出 batch 的迭代器（QAT 步数可能 > 一个 epoch）。"""
    return itertools.cycle(loader)


# ─────────────────────────────────────────────────────────────────
# 单 scheme QAT
# ─────────────────────────────────────────────────────────────────
def _run_scheme(
    scheme: str,
    fp_model,
    qconfig,
    calib_loader,
    train_loader,
    forward_fn,
    eval_fn,
    metric_kind: str,
    total_steps: int,
    lr: float,
    cage_mode: str,
) -> dict[str, Any]:
    """跑一个 scheme 的 prepare → before → train → after。失败 raise（caller 隔离）。"""
    result: dict[str, Any] = {
        "scheme": scheme,
        "metric_kind": metric_kind,
        "before": None,
        "after": None,
        "recovery": None,
        "steps": total_steps,
        "cage": cage_mode,
        "curve": [],  # [{step, metric}]
        "status": "error",
        "error": None,
        "_q_model": None,
    }

    # prepare trainable fake-quant（duquantpp 需 calib_data + forward_fn + DuQuantPPConfig）
    prepare_kwargs: dict[str, Any] = {"scheme": scheme, "qconfig": qconfig}
    if scheme == "duquantpp":
        # DuQuant++ V1 要求显式 target_patterns（防误替换）；module_types=linear 已预过滤，
        # 故 ".*" = 选中全部 Linear 层，与 rtn 默认替换范围一致（公平对比）。
        # block_size 必须与 qconfig.block_size 一致（block 格式约束），否则 calib raise。
        dq_block = getattr(qconfig, "block_size", None) or 16
        prepare_kwargs.update({
            "duquant_config": DuQuantPPConfig(target_patterns=(".*",), block_size=dq_block),
            "calib_data": calib_loader,
            "forward_fn": forward_fn,
        })
    q_model, _rep = prepare_trainable_fakequant_model(
        copy.deepcopy(fp_model), **prepare_kwargs
    )

    # before
    result["before"] = _eval_metric(eval_fn, q_model, metric_kind)
    result["curve"].append({"step": 0, "metric": result["before"]})

    # train loop（teacher-student mse，label-free distillation-style QAT）
    optimizer = torch.optim.Adam(q_model.parameters(), lr=lr)
    qat = prepare_trainable_qat(
        q_model, optimizer, total_steps=total_steps, cage=cage_mode
    )
    period = max(1, total_steps // 16)  # 曲线取 ~17 个点
    cycle = _train_cycle(train_loader)
    for step in range(1, total_steps + 1):
        batch = next(cycle)
        teacher_out = forward_fn(fp_model, batch).detach()
        optimizer.zero_grad()
        student_out = forward_fn(q_model, batch)
        loss = torch.nn.functional.mse_loss(student_out, teacher_out)
        loss.backward()
        optimizer.step()
        qat.step()
        if step % period == 0 or step == total_steps:
            # 训练 loss 已算（teacher-student mse），零成本采样做训练曲线
            # （独立于 eval try/except：eval 失败不丢 loss 点）
            result.setdefault("loss_curve", []).append(
                {"step": step, "loss": float(loss.item())}
            )
            try:
                m = _eval_metric(eval_fn, q_model, metric_kind)
                result["curve"].append({"step": step, "metric": m})
            except Exception as e:
                sys.stderr.write(
                    f"[run_qat] {scheme} step={step} eval 失败（曲线跳过该点）: {e}\n"
                )

    # after
    result["after"] = _eval_metric(eval_fn, q_model, metric_kind)
    result["recovery"] = round(result["after"] - result["before"], 6)
    result["status"] = "ok"
    result["_q_model"] = q_model
    return result


# ─────────────────────────────────────────────────────────────────
# 可视化（容错不阻断）
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
        sys.stderr.write(f"[run_qat] pushed table: {len(rows)} rows\n")
    except Exception as e:
        sys.stderr.write(f"[run_qat] table 推送失败（不阻断）: {e}\n")


def _push_charts(
    render_chart,
    ok_results: list[dict[str, Any]],
    all_results: list[dict[str, Any]],
    metric_kind: str,
) -> None:
    # line：per-step 收敛曲线（每 scheme 一条 series；失败 scheme 无 curve，不参与）
    line_data: list[dict[str, Any]] = []
    for r in ok_results:
        for pt in r.get("curve", []):
            line_data.append({
                "scheme": r["scheme"],
                "step": pt["step"],
                "metric": pt["metric"],
            })
    try:
        if line_data:
            render_chart(
                chart_type="line",
                data=line_data,
                label=CHART_LABEL,
                title=f"QAT Convergence ({metric_kind}, per scheme)",
                x="step",
                y="metric",
                hue="scheme",
                x_label="QAT training step",
                y_label=f"{metric_kind} (lower is better)" if metric_kind == "mse" else metric_kind,
                caption=(
                    f"每 scheme 的 eval metric 收敛（每约 16 等分步采样）。"
                    f"{'mse 口径下下行=改善。' if metric_kind == 'mse' else ''}"
                ),
            )
            sys.stderr.write(f"[run_qat] pushed line: {len(line_data)} points\n")
    except Exception as e:
        sys.stderr.write(f"[run_qat] line 推送失败（不阻断）: {e}\n")

    # loss line：per-step 训练 loss（teacher-student mse，越小越好；失败 scheme 无 loss_curve）
    loss_line_data: list[dict[str, Any]] = []
    for r in ok_results:
        for pt in r.get("loss_curve", []):
            loss_line_data.append({
                "scheme": r["scheme"],
                "step": pt["step"],
                "loss": pt["loss"],
            })
    try:
        if loss_line_data:
            render_chart(
                chart_type="line",
                data=loss_line_data,
                label=CHART_LABEL,
                title="QAT Training Loss",
                x="step",
                y="loss",
                hue="scheme",
                x_label="QAT 训练步",
                y_label="teacher-student MSE loss（越低越好）",
                caption="label-free 蒸馏 loss（student 拟合 teacher 输出），与 eval metric 同向但不等价。",
            )
            sys.stderr.write(f"[run_qat] pushed loss line: {len(loss_line_data)} points\n")
    except Exception as e:
        sys.stderr.write(f"[run_qat] loss line 推送失败（不阻断）: {e}\n")

    # bar：每 scheme before/after（ melted 成两行每 scheme，hue=phase）
    bar_data: list[dict[str, Any]] = []
    for r in ok_results:
        bar_data.append({"scheme": r["scheme"], "phase": "before", "metric": r["before"]})
        bar_data.append({"scheme": r["scheme"], "phase": "after", "metric": r["after"]})
    try:
        if bar_data:
            render_chart(
                chart_type="bar",
                data=bar_data,
                label=CHART_LABEL,
                title=f"QAT Before vs After ({metric_kind})",
                x="scheme",
                y="metric",
                hue="phase",
                x_label="QAT scheme",
                y_label=metric_kind,
                caption=(
                    f"每 scheme 训练前后 metric 对比。"
                    f"{'mse 口径下 after<before=QAT 有效（见 Recovery 图的方向感知版本）。' if metric_kind == 'mse' else ''}"
                ),
            )
            sys.stderr.write(f"[run_qat] pushed bar: {len(ok_results)} schemes\n")
    except Exception as e:
        sys.stderr.write(f"[run_qat] bar 推送失败（不阻断）: {e}\n")

    # recovery bar：recovery = after − before（QAT 最核心指标，原只在 table；失败 scheme 无数值）。
    # mse 口径下负值=改善（after<before）。caption 显式标注方向，避免读图者误以为正=好。
    recovery_bar_data: list[dict[str, Any]] = [
        {"scheme": r["scheme"], "recovery": r["recovery"]}
        for r in ok_results
        if r.get("recovery") is not None
    ]
    try:
        if recovery_bar_data:
            render_chart(
                chart_type="bar",
                data=recovery_bar_data,
                label=CHART_LABEL,
                title=f"QAT Recovery (after−before, {metric_kind})",
                x="scheme",
                y="recovery",
                x_label="QAT 方案（scheme）",
                y_label=f"after − before ({metric_kind})",
                caption=(
                    f"recovery = after − before；{metric_kind} 口径下负值=改善（QAT 把 metric 降下来了）。"
                    if metric_kind == "mse"
                    else f"recovery = after − before；正值={metric_kind} 提升。"
                ),
            )
            sys.stderr.write(
                f"[run_qat] pushed recovery bar: {len(recovery_bar_data)} schemes\n"
            )
    except Exception as e:
        sys.stderr.write(f"[run_qat] recovery bar 推送失败（不阻断）: {e}\n")

    # table：全集（含失败 scheme），失败行 before/after/recovery 填 "—"
    def _num(v, nd=6):
        return "—" if v is None else round(float(v), nd)

    rows = [
        {
            "scheme": r["scheme"],
            "before": _num(r.get("before")),
            "after": _num(r.get("after")),
            "recovery": _num(r.get("recovery")),
            "steps": r["steps"],
            "cage": r["cage"],
            "status": r.get("status") or "—",
            "error": r.get("error") or "",
        }
        for r in all_results
    ]
    _push_table(
        render_chart, rows, "QAT Scheme Comparison (all schemes)",
        caption="全集含失败 scheme；recovery=after−before（mse 下负=改善）。",
    )


def _render_charts(
    ok_results: list[dict[str, Any]],
    all_results: list[dict[str, Any]],
    metric_kind: str,
) -> None:
    try:
        from orca.chart import render_chart
    except Exception as e:
        sys.stderr.write(f"[run_qat] orca.chart 不可用（不阻断）: {e}\n")
        return
    _push_charts(render_chart, ok_results, all_results, metric_kind)


# ─────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--model_path", required=True, help="原始模型入口路径（回显用）")
    ap.add_argument("--output_dir", required=True)
    # Tier C 固化默认（P9a：自 workflow inputs 下沉，SPEC §5；agent 不再透传，改默认即改全局）：
    ap.add_argument("--scheme", default="both", help="rtn / duquantpp / both（默认 both）")
    ap.add_argument("--bit_width", default="w8a8-mx", help="QAT fake-quant 位宽预设（默认 w8a8-mx，高位宽起步更稳）")
    ap.add_argument("--cage", default="auto", help="CAGE 后校正开关 auto/true/false（默认 auto）")
    ap.add_argument("--bake", default="true", help="true / false（默认 true）")
    # Tier B best-effort 推断（P9a / SPEC §5：agent 读用户 train.py/config 拿真值；找不到传空→smoke 兜底）：
    ap.add_argument("--lr", default="", help="Adam 学习率（float 字符串；空→脚本 smoke 兜底 1e-4）")
    ap.add_argument("--total_steps", default="", help="QAT 训练步数（int 字符串；空→脚本 smoke 兜底 64）")
    add_device_seed_args(ap)
    ap.add_argument("--env_file", default="", help="本 run 的 orca_env.sh 路径（env 兜底）")
    args = ap.parse_args()
    _load_env_file(args.env_file)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "report.json"

    if not _TS_QUANT_OK:
        sys.stderr.write(
            f"[run_qat] ts_quant import 失败（环境错，exit 2）: {_TS_QUANT_IMPORT_ERROR}\n"
        )
        sys.exit(2)

    # device + seed 解析（单一真相源：_device.resolve_device_and_seed）
    device, seed = resolve_device_and_seed(
        args.device, args.seed, log_prefix="[run_qat] "
    )

    scheme_arg = (args.scheme or "both").strip().lower()
    if scheme_arg == "both":
        schemes = ["rtn", "duquantpp"]
    elif scheme_arg in {"rtn", "duquantpp"}:
        schemes = [scheme_arg]
    else:
        sys.stderr.write(
            f"[run_qat] scheme 非法 '{scheme_arg}'（支持 rtn/duquantpp/both）\n"
        )
        sys.exit(2)

    cage_mode = (args.cage or "auto").strip().lower()
    if cage_mode not in {"auto", "true", "false"}:
        sys.stderr.write(f"[run_qat] cage 非法 '{args.cage}'（支持 auto/true/false）\n")
        sys.exit(2)

    try:
        # Tier B best-effort（P9a / SPEC §5）：agent 读用户 train.py/config 拿真值；找不到传空。
        # 空→smoke 兜底必须可见（SPEC §0「绝不静默产出错误交付物」+ Rule 12 fail loud）：
        # smoke 不是生产精度，用户须看到降级信号，否则会把 64 步 1e-4 的短训当正式 QAT 结果。
        _ts_provided = (args.total_steps or "").strip()
        total_steps = int(_ts_provided or "64")
        if total_steps <= 0:
            raise ValueError
        if not _ts_provided:
            sys.stderr.write(
                "[run_qat] WARN: total_steps 未提供（agent 未在用户代码找到）→ smoke 兜底 64。"
                "短训恢复用，非生产精度。真实 QAT 请显式传更大值。\n"
            )
    except ValueError:
        sys.stderr.write(f"[run_qat] total_steps 非法 '{args.total_steps}'\n")
        sys.exit(2)
    try:
        _lr_provided = (args.lr or "").strip()
        lr = float(_lr_provided or "1e-4")
        if not _lr_provided:
            sys.stderr.write(
                "[run_qat] WARN: lr 未提供（agent 未在用户代码找到）→ smoke 兜底 1e-4。"
                "非生产精度。真实 QAT 请显式传训练用 lr。\n"
            )
    except ValueError:
        sys.stderr.write(f"[run_qat] lr 非法 '{args.lr}'\n")
        sys.exit(2)

    qconfig = _make_qconfig(args.bit_width)

    # adapter → fp teacher + loaders + forward
    adapter = _load_adapter(args.adapter)
    fp_model = adapter.load_model()
    # device 搬移（plan §P5）：adapter.load_model() 返回 CPU 模型，脚本统一搬到 device。
    fp_model = fp_model.to(device)
    calib_loader = adapter.get_calib_loader()
    raw_forward_fn = getattr(adapter, "forward_fn", None)
    forward_fn = wrap_forward_with_device(raw_forward_fn, device)

    # 训练 loader：QAT 没真实训练数据 = 烧算力跑无意义短训 → fail loud（plan §P5）。
    # 老版本「复用 calib_loader 做最小 smoke QAT」是数据泄漏 + 误指标，已删除。
    get_train_loader = getattr(adapter, "get_train_loader", None)
    if callable(get_train_loader):
        train_loader = get_train_loader()
    else:
        sys.stderr.write(
            "[run_qat] adapter 未实现 get_train_loader（train_data_ref 空）→ fail loud。"
            "QAT 需真实训练数据：复用 calib 是数据泄漏 + 烧算力跑无意义短训。\n"
        )
        sys.exit(2)

    # eval loader：未提供 → fail loud（plan §1-c + plan §P5）。
    # eval=train 是数据泄漏口径（train loss != eval metric，短训后必然 overfit train），
    # 复用会让 best_scheme 选到 overfit 候选。P4 哨兵到位后可退「问用户」，当前 exit 2。
    get_eval_loader = getattr(adapter, "get_eval_loader", None)
    if callable(get_eval_loader):
        eval_loader = get_eval_loader()
    else:
        sys.stderr.write(
            "[run_qat] FAIL LOUD: adapter 未实现 get_eval_loader（eval_data_ref 空）"
            "→ 缺评估数据。复用 train_loader 做 eval 是禁掉的造假口径（plan §1-c + §P5："
            "「复用 train 当 eval」）——train=eval 是数据泄漏口径，会让 best_scheme 选到 "
            "overfit 候选。请在用户代码里找 eval loader，或在 workflow inputs 显式提供 "
            "eval_data_ref。\n"
        )
        sys.exit(2)

    eval_fn, metric_kind, higher_is_better = _resolve_eval(
        adapter, fp_model, eval_loader, forward_fn
    )

    sys.stderr.write(
        f"[run_qat] schemes={schemes} bit_width={args.bit_width} cage={cage_mode} "
        f"steps={total_steps} lr={lr} metric_kind={metric_kind} higher_is_better={higher_is_better}\n"
    )

    report: dict[str, Any] = {
        "scheme_arg": scheme_arg,
        "bit_width": args.bit_width,
        "cage": cage_mode,
        "total_steps": total_steps,
        "lr": lr,
        "metric_kind": metric_kind,
        "higher_is_better": higher_is_better,
        "model_path": args.model_path,
        "schemes": [],
        "best": None,
        "baked_model_path": None,
    }
    _dump_json(report, report_path)

    results: list[dict[str, Any]] = []
    for scheme in schemes:
        t0 = time.time()
        try:
            r = _run_scheme(
                scheme, fp_model, qconfig, calib_loader, train_loader,
                forward_fn, eval_fn, metric_kind, total_steps, lr, cage_mode,
            )
            r["elapsed_seconds"] = round(time.time() - t0, 3)
            sys.stderr.write(
                f"[run_qat] {scheme} ok: before={r['before']:.6f} "
                f"after={r['after']:.6f} recovery={r['recovery']}\n"
            )
        except Exception as e:
            r = {
                "scheme": scheme, "metric_kind": metric_kind,
                "before": None, "after": None, "recovery": None,
                "steps": total_steps, "cage": cage_mode, "curve": [],
                "status": "error", "error": f"{type(e).__name__}: {e}",
                "elapsed_seconds": round(time.time() - t0, 3), "_q_model": None,
            }
            sys.stderr.write(f"[run_qat] {scheme} failed: {r['error']}\n")
        # 增量落盘（剥离 _q_model）
        dump_r = {k: v for k, v in r.items() if k != "_q_model"}
        report["schemes"].append(dump_r)
        _dump_json(report, report_path)
        results.append(r)

    ok_results = [r for r in results if r["status"] == "ok"]
    if not ok_results:
        sys.stderr.write("[run_qat] 全 scheme 失败 → fail loud (exit 3)\n")
        sys.exit(3)

    # 选 best（after_metric 最优）；保留 best 的 q_model，其余释放
    best = max(ok_results, key=lambda r: r["after"]) if higher_is_better else \
        min(ok_results, key=lambda r: r["after"])
    for r in ok_results:
        if r is not best:
            _free_model(r["_q_model"])
            r["_q_model"] = None

    report["best"] = {
        "scheme": best["scheme"],
        "before": best["before"],
        "after": best["after"],
        "recovery": best["recovery"],
    }
    _dump_json(report, report_path)

    # bake
    baked_path_str = ""
    bake_token = (args.bake or "").strip().lower()
    if bake_token in _TRUE_TOKENS:
        try:
            import torch
            baked_path = output_dir / "best_qat_model.pt"
            torch.save(best["_q_model"].state_dict(), baked_path)
            baked_path_str = str(baked_path)
            report["baked_model_path"] = baked_path_str
            _dump_json(report, report_path)
            sys.stderr.write(f"[run_qat] baked best ({best['scheme']}) → {baked_path_str}\n")
        except Exception as e:
            sys.stderr.write(f"[run_qat] bake 失败（不阻断）: {type(e).__name__}: {e}\n")
    elif bake_token in _FALSE_TOKENS:
        sys.stderr.write("[run_qat] bake=false → skip bake\n")
    else:
        sys.stderr.write(f"[run_qat] --bake='{args.bake}' 非法（期望 true/false）\n")
        sys.exit(2)

    _free_model(best["_q_model"])
    best["_q_model"] = None

    # charts（table 用全集含失败 scheme；line/bar/loss/recovery 用 ok_results）
    _render_charts(ok_results, results, metric_kind)

    # stdout JSON 摘要
    summary = {
        "output_dir": str(output_dir),
        "report_path": str(report_path),
        "model_path": args.model_path,
        "baked_model_path": baked_path_str,
        "best_scheme": best["scheme"],
        "best_metric": best["after"],
        "best_metric_before": best["before"],
        "recovery": best["recovery"],
        "schemes_evaluated": [r["scheme"] for r in ok_results],
        "total_steps": total_steps,
        "cage": cage_mode,
        "metric_kind": metric_kind,
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

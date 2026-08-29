#!/usr/bin/env python3
"""run_prune_sweep.py —— 结构化通道剪枝扫描 + 蒸馏式短微调 + bake + 可视化（prune_sweeper 节点调用）。

流程（八步）：
1. import adapter → FP teacher + eval loader（+ 可选 train loader + 业务 eval_fn）
2. 候选网格 = 稀疏度比例列表（默认 [0.3, 0.5, 0.7]，Tier C 固化）。
   若给了 --target_compression_ratio，先按「理论 FLOPs 压缩比 ≥ 目标」过滤候选。
3. 每候选 try/except 隔离：deepcopy FP → 按 criterion（L1 范数默认）对每个 Conv2d 输出通道
   排序、mask 掉底部 ratio 比例（结构化零掩码，物理裁剪是下游独立步骤，见 mask_meta）→
   计理论 FLOPs 加速比 → （若提供 train_loader 且 finetune_steps>0）蒸馏式短微调 → eval_fn → 记录。
4. 选 best（metric_kind↓ 或 业务 higher_is_better；无候选达标 → fail loud exit 3）。
5. bake：torch.save(best.state_dict(), output_dir/best_pruned_model.pt) + 落盘 pruning_mask.json。
6. report.json（全候选 + best）每候选评完增量落盘。
7. render_chart（容错不阻断）：bar（ratio vs metric）+ scatter（加速比 vs metric，best 高亮）+ table。
8. stdout JSON 摘要（agent 原样回显，对齐 output_schema）。

铁律：
- 单候选失败不拖垮全扫（try/except 隔离 + stderr 提示 + report 增量落盘）。
- 全部候选失败 → fail loud（exit 3）。
- eval_loader 缺失（adapter 未实现 get_eval_loader）→ exit 2（业务硬要求，造假口径禁掉）。
- 推图失败 → stderr 提示但不阻断（report.json 是核心产出）。

适用范围（产品边界）：
- 本脚本做**结构化通道 mask 剪枝**——按比例把 Conv2d 输出通道权重置零（torch.nn.utils.prune）。
- **物理裁剪**（实际缩小 Conv2d 的 out_channels、调整下一层 in_channels、处理 skip-connection 形状）
  是依赖 DepGraph 的独立步骤，不在本扫描 workflow 内——mask 元数据（pruning_mask.json）供下游
  物理裁剪脚本消费。理论 FLOPs 加速比按 mask 后的非零通道数估算（=物理裁剪后的真实加速比下界）。
- 1×1 卷积 / 分组卷积 / depthwise 同样按 out_channels L1 排序剪（mask 形态安全）。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable

_LOG_PREFIX = "[run_prune_sweep] "
CHART_LABEL = "prune/channel-sweep"

_TRUE_TOKENS = {"true", "1", "yes", "y", "on"}
_FALSE_TOKENS = {"false", "0", "no", "n", "off"}
_VALID_CRITERIA = {"l1", "random"}


# ─────────────────────────────────────────────────────────────────
# 通用 helpers（device / seed / adapter / eval —— 自包含，不跨域引 _quant_scripts）
# ─────────────────────────────────────────────────────────────────

def _load_env_file(path: str) -> None:
    """启动期自加载 orca_env.sh：opencode bash 拆调用会丢 ORCA_CHART_SOCK，--env_file 兜底。"""
    if not path:
        return
    p = Path(path)
    if not p.is_file():
        sys.stderr.write(f"{_LOG_PREFIX}env_file 不存在，跳过: {path}\n")
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _set_seed(seed: int) -> None:
    import torch  # 延迟 import：缺 torch 早 import 也利于显式失败
    random.seed(seed)
    try:
        import numpy as np  # type: ignore
        np.random.seed(seed)
    except ImportError:
        pass
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def resolve_device_and_seed(dev_arg: str, seed_arg: str) -> tuple[str, int]:
    """device 解析：空 → cuda 优先，cpu 兜底（无 npu 自动探测，npu 需显式传）。"""
    import torch
    dev = (dev_arg or "").strip().lower()
    if not dev:
        if torch.cuda.is_available():
            dev = "cuda"
        else:
            dev = "cpu"
            sys.stderr.write(f"{_LOG_PREFIX}device 自动探测 → cpu（无 cuda）\n")
    # 合法性
    if dev not in ("cuda", "cpu", "npu"):
        sys.stderr.write(f"{_LOG_PREFIX}device 非法 '{dev_arg}'（支持 cuda/npu/cpu）\n")
        sys.exit(2)
    try:
        seed = int(seed_arg)
    except (TypeError, ValueError):
        sys.stderr.write(f"{_LOG_PREFIX}seed 非法 '{seed_arg}'（必须整数）\n")
        sys.exit(2)
    return dev, seed


def wrap_forward_with_device(forward_fn: Callable | None, device: str) -> Callable:
    """包一层把 batch 搬到 device 再喂给 adapter forward_fn（adapter 不感知 device）。"""
    import torch
    if forward_fn is None:
        return None

    def _to_device(batch):
        if isinstance(batch, torch.Tensor):
            return batch.to(device)
        if isinstance(batch, dict):
            return {k: _to_device(v) for k, v in batch.items()}
        if isinstance(batch, (list, tuple)):
            t = type(batch)
            return t(_to_device(x) for x in batch)
        return batch

    def _wrapped(module, batch):
        return forward_fn(module, _to_device(batch))

    return _wrapped


def load_adapter(path: str):
    """按 dotted-path 加载 adapter 模块（路径锚定：本脚本在 .../prune-channel-sweeper/scripts/）。"""
    import importlib.util
    p = Path(path).resolve()
    if not p.is_file():
        sys.stderr.write(f"{_LOG_PREFIX}adapter 不存在: {path}\n")
        sys.exit(2)
    spec = importlib.util.spec_from_file_location("orca_prune_channel_sweep_adapter", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    for required in ("load_model", "forward_fn"):
        if not callable(getattr(mod, required, None)):
            sys.stderr.write(
                f"{_LOG_PREFIX}adapter 缺必填 callable '{required}'（adapter 契约）\n"
            )
            sys.exit(2)
    if not callable(getattr(mod, "get_eval_loader", None)):
        # 与 agent.md 哨兵段对齐：eval_loader 缺失 = 业务硬要求，不造假
        sys.stderr.write(
            f"{_LOG_PREFIX}adapter 未实现 get_eval_loader（eval_data_ref 空）→ 缺评估数据。"
            "复用 train / torch.randn 当 eval 是禁掉的造假口径——会让 best_metric 选错候选。"
            "请在用户代码里找 eval loader，或让 agent 走 ask-user 哨兵。\n"
        )
        sys.exit(2)
    sys.stderr.write(f"{_LOG_PREFIX}adapter loaded: {p}\n")
    return mod


def _resolve_eval(adapter, fp_model, eval_loader, forward_fn) -> tuple[Callable, str, bool]:
    """eval_fn 解析：业务 eval_fn → 用之；否则 fallback teacher-student mse（自洽性诊断）。"""
    import torch
    get_eval_fn = getattr(adapter, "get_eval_fn", None)
    if callable(get_eval_fn):
        eval_fn = get_eval_fn()
        spec = getattr(adapter, "get_metric_spec", lambda: {})()
        metric_kind = spec.get("primary_metric", "metric")
        higher_is_better = bool(spec.get("higher_is_better", False))
        sys.stderr.write(
            f"{_LOG_PREFIX}用业务 eval_fn（metric_kind={metric_kind}, higher_is_better={higher_is_better}）\n"
        )
        return eval_fn, metric_kind, higher_is_better

    # fallback：teacher-student mse（FP teacher 对照剪枝后 student）。精度仅自洽性参考。
    teacher = copy.deepcopy(fp_model).eval()
    for p_ in teacher.parameters():
        p_.requires_grad_(False)

    def _mse_eval(student_model):
        student_model.eval()
        squares = []
        with torch.no_grad():
            for batch in eval_loader:
                out_s = forward_fn(student_model, batch)
                out_t = forward_fn(teacher, batch)
                if isinstance(out_s, dict):
                    out_s = list(out_s.values())
                if isinstance(out_t, dict):
                    out_t = list(out_t.values())
                if isinstance(out_s, (list, tuple)) and isinstance(out_t, (list, tuple)):
                    for a, b in zip(out_s, out_t):
                        if torch.is_tensor(a) and torch.is_tensor(b) and a.shape == b.shape:
                            squares.append(torch.mean((a.float() - b.float()) ** 2).item())
                elif torch.is_tensor(out_s) and torch.is_tensor(out_t) and out_s.shape == out_t.shape:
                    squares.append(torch.mean((out_s.float() - out_t.float()) ** 2).item())
        if not squares:
            raise RuntimeError("teacher-student mse：无任何可对齐 batch 输出（检查 forward_fn / batch 形态）")
        return {"mse": sum(squares) / len(squares)}

    sys.stderr.write(
        f"{_LOG_PREFIX}未提供业务 eval_fn → fallback teacher-student mse（精度仅自洽性参考）\n"
    )
    return _mse_eval, "mse", False


def is_better(a: float, b: float, higher_is_better: bool) -> bool:
    return a > b if higher_is_better else a < b


def dump_report(report: dict[str, Any], path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ─────────────────────────────────────────────────────────────────
# 结构化通道剪枝核心
# ─────────────────────────────────────────────────────────────────

def _conv_layers(model) -> list[tuple[str, Any]]:
    """枚举所有 Conv2d（含 1×1 / 分组 / depthwise），按 qualified name 返回。"""
    import torch.nn as nn
    out: list[tuple[str, Any]] = []
    for name, mod in model.named_modules():
        if isinstance(mod, nn.Conv2d):
            out.append((name, mod))
    return out


def _l1_score(conv) -> Any:
    """每输出通道的 L1 范数（Cout,）。越大越重要。"""
    import torch
    w = conv.weight.detach()
    # Cout = w.shape[0]；沿 (Cin, kH, kW) 求 L1
    return w.abs().sum(dim=(1, 2, 3))


def _apply_structured_mask(conv, keep_mask_1d) -> None:
    """把 (Cout,) 的 True/False keep mask 作为结构化剪枝 apply 到 conv.weight（零掩码）。"""
    import torch
    import torch.nn.utils.prune as prune
    keep = keep_mask_1d.to(conv.weight.device).bool()
    # prune.mask 需与 weight 同 shape：扩到 (Cout, Cin, kH, kW)
    weight_mask = keep.view(-1, 1, 1, 1).expand_as(conv.weight)
    # CustomFromMask 是 class（不是 callable 工厂），直接构造会 TypeError（签名只接 mask）。
    # 用同名函数 prune.custom_from_mask（副作用：注册 forward_pre_hook 零掩码）。
    prune.custom_from_mask(conv, "weight", mask=weight_mask)


def _prune_model(model, ratio: float, criterion: str, rng: random.Random):
    """对 model 所有 Conv2d 做结构化通道剪枝（inplace mask）。

    返回 per-layer mask 元数据：[{name, total_channels, kept_channels, pruned_channels}]
    """
    import torch
    metas: list[dict[str, Any]] = []
    for name, conv in _conv_layers(model):
        Cout = conv.out_channels
        if Cout <= 1:
            continue  # 单通道（含 depthwise 多数情况）剪了=整层废，跳过
        n_prune = max(0, min(Cout - 1, int(round(Cout * ratio))))
        if n_prune == 0:
            metas.append({"name": name, "total_channels": Cout,
                          "kept_channels": Cout, "pruned_channels": 0})
            continue
        if criterion == "l1":
            scores = _l1_score(conv)
            keep = torch.ones(Cout, dtype=torch.bool)
            # 分组卷积：同组内排序才合法（组间不能跨组剪）。group>=2 时按 group 分块处理。
            g = max(1, conv.groups)
            if g == 1:
                idx = torch.argsort(scores)
                prune_idx = idx[:n_prune]
                keep[prune_idx] = False
            else:
                # 每组等量剪（depthwise: g==Cout, 每组 1 通道 → n_prune 跨组分配）
                chans_per_group = Cout // g
                if chans_per_group < 1:
                    continue
                per_group_prune = max(0, min(chans_per_group - 1,
                                             n_prune // g if n_prune >= g else 0))
                if per_group_prune == 0 and n_prune > 0:
                    # 不够每组剪一个 → 按组顺序剪满 n_prune（best-effort）
                    pruned_so_far = 0
                    for gi in range(g):
                        if pruned_so_far >= n_prune:
                            break
                        lo, hi = gi * chans_per_group, (gi + 1) * chans_per_group
                        sg = scores[lo:hi]
                        local_idx = torch.argsort(sg)
                        take = min(per_group_prune or 1, n_prune - pruned_so_far)
                        for li in local_idx[:take]:
                            keep[lo + int(li)] = False
                            pruned_so_far += 1
                else:
                    for gi in range(g):
                        lo, hi = gi * chans_per_group, (gi + 1) * chans_per_group
                        sg = scores[lo:hi]
                        local_idx = torch.argsort(sg)
                        for li in local_idx[:per_group_prune]:
                            keep[lo + int(li)] = False
        elif criterion == "random":
            keep = torch.ones(Cout, dtype=torch.bool)
            chosen = set(rng.sample(range(Cout), n_prune))
            for i in chosen:
                keep[i] = False
        else:
            raise ValueError(f"未知 criterion '{criterion}'（支持 l1/random）")

        _apply_structured_mask(conv, keep)
        kept = int(keep.sum().item())
        metas.append({"name": name, "total_channels": Cout,
                      "kept_channels": kept, "pruned_channels": Cout - kept})
    return metas


def _theoretical_speedup(metas: list[dict[str, Any]], model) -> float:
    """估算理论 FLOPs 加速比：mask 后非零通道 → 等价物理裁剪后 FLOPs 占比。

    粗口径：每 Conv2d 的 FLOPs ∝ Cout * Cin * kH * kW * Hout * Wout。mask 后等价
    Cout'=kept_out，下一层 Cin'=kept_out（通道对齐）。本估算用「全模型非零通道加权和」
    作分子/分母比例，给出加速比下界（≥1.0；1.0=未压缩）。
    """
    import torch.nn as nn
    # 建 name → kept 映射
    kept_map = {m["name"]: m["kept_channels"] for m in metas}
    total_before = 0.0
    total_after = 0.0
    for name, conv in _conv_layers(model):
        Cout, Cin = conv.out_channels, conv.in_channels
        kH, kW = conv.kernel_size
        # 该层 Cout 实际保留
        kept_out = kept_map.get(name, Cout)
        # 该层 Cin 取决于上游 conv 的 kept_out（若上游是 conv）；否则原值
        # best-effort：直接用原 Cin 作近似（保守，偏低估加速比）
        cin_eff = Cin
        flops_before = Cout * cin_eff * kH * kW
        flops_after = kept_out * cin_eff * kH * kW
        total_before += flops_before
        total_after += flops_after
    if total_before <= 0:
        return 1.0
    return float(total_before / max(total_after, 1e-9))


def _finetune_distill(student, teacher, train_loader, forward_fn, device, steps: int) -> None:
    """teacher-student 蒸馏式短微调（MSE on outputs）。无需用户 loss——generic 恢复精度。"""
    import torch
    teacher = teacher.eval()
    for p_ in teacher.parameters():
        p_.requires_grad_(False)
    student.train()
    opt = torch.optim.Adam([p_ for p_ in student.parameters() if p_.requires_grad], lr=1e-4)
    it = iter(train_loader)
    for _step in range(max(0, steps)):
        try:
            batch = next(it)
        except StopIteration:
            it = iter(train_loader)
            batch = next(it)
        opt.zero_grad(set_to_none=True)
        with torch.no_grad():
            out_t = forward_fn(teacher, batch)
        out_s = forward_fn(student, batch)
        # 对齐多输出
        if isinstance(out_t, dict) or isinstance(out_s, dict):
            keys = (out_s.keys() if isinstance(out_s, dict) else range(len(out_s)))
            loss = torch.tensor(0.0, device=device)
            for k in keys:
                a = out_s[k] if isinstance(out_s, dict) else out_s[k]
                b = out_t[k] if isinstance(out_t, dict) else out_t[k]
                if torch.is_tensor(a) and torch.is_tensor(b) and a.shape == b.shape:
                    loss = loss + torch.mean((a.float() - b.float()) ** 2)
        elif isinstance(out_s, (list, tuple)) and isinstance(out_t, (list, tuple)):
            loss = torch.tensor(0.0, device=device)
            for a, b in zip(out_s, out_t):
                if torch.is_tensor(a) and torch.is_tensor(b) and a.shape == b.shape:
                    loss = loss + torch.mean((a.float() - b.float()) ** 2)
        elif torch.is_tensor(out_s) and torch.is_tensor(out_t) and out_s.shape == out_t.shape:
            loss = torch.mean((out_s.float() - out_t.float()) ** 2)
        else:
            continue
        if not torch.is_tensor(loss) or not loss.requires_grad:
            continue
        loss.backward()
        opt.step()
    student.eval()


# ─────────────────────────────────────────────────────────────────
# 单候选评估
# ─────────────────────────────────────────────────────────────────

def _eval_candidate(
    fp_model,
    ratio: float,
    criterion: str,
    rng: random.Random,
    train_loader,
    forward_fn,
    eval_fn,
    metric_kind: str,
    finetune_steps: int,
    teacher_model,
    device: str,
) -> dict[str, Any]:
    """单候选：deepcopy → mask 剪枝 → 蒸馏式微调（可选）→ eval。"""
    import torch
    t0 = time.time()
    result: dict[str, Any] = {
        "ratio": ratio,
        "criterion": criterion,
        "metric": None,
        "metric_kind": metric_kind,
        "theoretical_speedup": None,
        "finetuned": False,
        "status": "error",
        "error": None,
        "elapsed_seconds": 0.0,
        "_model": None,  # 内部字段（dump 前剥离）
        "_metas": None,
    }
    try:
        pruned = copy.deepcopy(fp_model)
        metas = _prune_model(pruned, ratio, criterion, rng)
        speedup = _theoretical_speedup(metas, pruned)
        finetuned = False
        if train_loader is not None and finetune_steps > 0:
            _finetune_distill(pruned, teacher_model, train_loader, forward_fn, device, finetune_steps)
            finetuned = True
        metrics = eval_fn(pruned)
        if not isinstance(metrics, dict) or metric_kind not in metrics:
            raise KeyError(
                f"eval_fn 返回的 metrics 缺 '{metric_kind}' 键（得到 "
                f"{sorted(metrics.keys()) if isinstance(metrics, dict) else type(metrics).__name__}）"
            )
        result["metric"] = float(metrics[metric_kind])
        result["theoretical_speedup"] = round(speedup, 4)
        result["finetuned"] = finetuned
        result["status"] = "ok"
        result["_model"] = pruned
        result["_metas"] = metas
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        sys.stderr.write(
            f"{_LOG_PREFIX}candidate ratio={ratio} failed: {result['error']}\n"
        )
        # 显式释放失败候选
        try:
            del locals()["pruned"]
        except Exception:
            pass
        if "cuda" in device:
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass

    result["elapsed_seconds"] = round(time.time() - t0, 3)
    return result


# ─────────────────────────────────────────────────────────────────
# 可视化
# ─────────────────────────────────────────────────────────────────

def _push_charts(render_chart, report: dict[str, Any], metric_kind: str,
                 higher_is_better: bool, best_label: str | None) -> None:
    ok_results = [c for c in report["candidates"] if c.get("status") == "ok"]
    if not ok_results:
        sys.stderr.write(f"{_LOG_PREFIX}无成功候选 → 不推图\n")
        return

    _BEST_COLOR = "#D4605A"
    _DEFAULT_COLOR = "#5B8DB8"

    # bar：ratio vs metric
    bar_data = sorted(
        [{"ratio": r["ratio"], "metric": r["metric"]} for r in ok_results],
        key=lambda x: x["ratio"],
    )
    try:
        render_chart(
            chart_type="bar",
            data=bar_data,
            label=CHART_LABEL,
            title=f"Pruning Sweep — Ratio vs {metric_kind}",
            x="ratio",
            y="metric",
            x_label="稀疏度比例（被剪通道占比）",
            y_label=f"{metric_kind}（{'越高越好' if higher_is_better else '越低越好'}）",
            caption="每稀疏度比例一条柱（评估指标越接近未剪枝越好）。",
        )
        sys.stderr.write(f"{_LOG_PREFIX}pushed bar: {len(bar_data)} ratios\n")
    except Exception as e:
        sys.stderr.write(f"{_LOG_PREFIX}bar 推送失败（不阻断）: {e}\n")

    # scatter：加速比 vs metric，best 高亮
    scatter_data = []
    for r in ok_results:
        ratio_str = f"ratio={r['ratio']}"
        scatter_data.append({
            "speedup": r["theoretical_speedup"],
            "metric": r["metric"],
            "ratio": r["ratio"],
            "config": ratio_str,
            "color": _BEST_COLOR if ratio_str == best_label else _DEFAULT_COLOR,
        })
    try:
        render_chart(
            chart_type="scatter",
            data=scatter_data,
            label=CHART_LABEL,
            title=f"Pruning Trade-off — Speedup vs {metric_kind} (coral=best)",
            x="speedup",
            y="metric",
            color="color",
            x_label="理论 FLOPs 加速比（≥1.0）",
            y_label=f"{metric_kind}（{'越高越好' if higher_is_better else '越低越好'}）",
            caption=f"每候选一个点；珊瑚色=best（{best_label}）。理想区域：高加速比 + 指标接近 baseline。",
        )
        sys.stderr.write(f"{_LOG_PREFIX}pushed scatter: {len(scatter_data)} points\n")
    except Exception as e:
        sys.stderr.write(f"{_LOG_PREFIX}scatter 推送失败（不阻断）: {e}\n")

    # table：全候选（含 failed）
    table_rows = sorted(report["candidates"], key=lambda r: r.get("ratio", 0.0))
    try:
        render_chart(
            chart_type="table",
            data=[
                {"ratio": r["ratio"], "criterion": r["criterion"],
                 "metric": r["metric"], "speedup": r["theoretical_speedup"],
                 "finetuned": r["finetuned"], "elapsed_s": r["elapsed_seconds"],
                 "status": r["status"], "error": r["error"] or ""}
                for r in table_rows
            ],
            label=CHART_LABEL,
            title="Pruning Sweep — All Candidates (incl. failed)",
            columns=["ratio", "criterion", "metric", "speedup", "finetuned",
                     "elapsed_s", "status", "error"],
            caption="每候选一行（含 failed 便于诊断依赖缺失）。",
        )
        sys.stderr.write(f"{_LOG_PREFIX}pushed table: {len(table_rows)} rows\n")
    except Exception as e:
        sys.stderr.write(f"{_LOG_PREFIX}table 推送失败（不阻断）: {e}\n")


# ─────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────

def _parse_ratios(raw: str, fallback: list[float]) -> list[float]:
    raw = (raw or "").strip()
    if not raw:
        return list(fallback)
    out: list[float] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            v = float(tok)
        except ValueError:
            sys.stderr.write(f"{_LOG_PREFIX}忽略非法 ratio token '{tok}'\n")
            continue
        if not (0.0 <= v < 1.0):
            sys.stderr.write(
                f"{_LOG_PREFIX}ratio {v} 越界（需 0 ≤ r < 1），已裁剪到合法区间\n"
            )
            v = min(max(v, 0.0), 0.99)
        out.append(v)
    # 去重 + 排序
    return sorted(set(out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True, help="adapter.py 路径")
    ap.add_argument("--model_path", required=True, help="原始模型入口路径（仅用于回显摘要）")
    ap.add_argument("--output_dir", required=True)
    # Tier C 固化默认（自 workflow inputs 下沉，agent 不再透传，改默认即改全局）：
    ap.add_argument("--criterion", default="l1", help="通道重要性准则：l1 / random（默认 l1）")
    ap.add_argument("--ratios", default="",
                    help="逗号分隔稀疏度比例（空 → 默认 0.3,0.5,0.7；范围 [0,1)）")
    ap.add_argument("--finetune_steps", default="0",
                    help="蒸馏式短微调步数（默认 0；>0 需 adapter 提供 get_train_loader）")
    ap.add_argument("--bake", default="true", help="true / false（默认 true，bake 最佳 mask 模型）")
    ap.add_argument("--target_compression_ratio", default="",
                    help="业务压缩 KPI：候选过滤阈值（如 0.5 = 仅留理论压缩比 ≥ 0.5 的候选）")
    ap.add_argument("--device", default="")
    ap.add_argument("--seed", default="0")
    ap.add_argument(
        "--env_file",
        default="",
        help="本 run 的 orca_env.sh 路径；脚本启动自加载 ORCA_* env（兜底：opencode bash 拆调用会丢 env）",
    )
    args = ap.parse_args()
    _load_env_file(args.env_file)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "report.json"
    mask_meta_path = output_dir / "pruning_mask.json"

    criterion = (args.criterion or "l1").strip().lower()
    if criterion not in _VALID_CRITERIA:
        sys.stderr.write(
            f"{_LOG_PREFIX}criterion 非法 '{args.criterion}'（支持 {sorted(_VALID_CRITERIA)}）\n"
        )
        sys.exit(2)

    device, seed = resolve_device_and_seed(args.device, args.seed)
    _set_seed(seed)
    rng = random.Random(seed)

    ratios = _parse_ratios(args.ratios, fallback=[0.3, 0.5, 0.7])
    if not ratios:
        sys.stderr.write(f"{_LOG_PREFIX}候选稀疏度网格为空\n")
        sys.exit(2)

    try:
        finetune_steps = int(args.finetune_steps)
    except ValueError:
        sys.stderr.write(
            f"{_LOG_PREFIX}finetune_steps 非法 '{args.finetune_steps}'（必须整数）\n"
        )
        sys.exit(2)
    if finetune_steps < 0:
        finetune_steps = 0

    # 解析 target_compression_ratio（业务 KPI 过滤阈值；空 = 不过滤）
    target_ratio: float | None = None
    tc_raw = (args.target_compression_ratio or "").strip()
    if tc_raw:
        try:
            target_ratio = float(tc_raw)
        except ValueError:
            sys.stderr.write(
                f"{_LOG_PREFIX}target_compression_ratio 非法 '{args.target_compression_ratio}'（需 float 如 0.5）→ 忽略\n"
            )

    # 1. adapter → fp teacher + eval (+ train) + forward
    adapter = load_adapter(args.adapter)
    fp_model = adapter.load_model()
    fp_model = fp_model.to(device)
    eval_loader = adapter.get_eval_loader()
    raw_forward_fn = getattr(adapter, "forward_fn", None)
    forward_fn = wrap_forward_with_device(raw_forward_fn, device)

    get_train_loader = getattr(adapter, "get_train_loader", None)
    train_loader = None
    if callable(get_train_loader):
        train_loader = get_train_loader()
    if train_loader is None:
        sys.stderr.write(
            f"{_LOG_PREFIX}未提供 train_loader → 跳过短微调（finetuned=false，仅剪枝后直接 eval，降级诊断模式）\n"
        )
        finetune_steps = 0  # 强制 0，避免无意义尝试

    eval_fn, metric_kind, higher_is_better = _resolve_eval(
        adapter, fp_model, eval_loader, forward_fn
    )

    # teacher 用于蒸馏微调 + mse fallback eval（统一一个 deepcopy）
    import torch  # noqa: F401
    teacher_model = copy.deepcopy(fp_model).eval()
    for p_ in teacher_model.parameters():
        p_.requires_grad_(False)

    sys.stderr.write(
        f"{_LOG_PREFIX}ratios={ratios} criterion={criterion} "
        f"finetune_steps={finetune_steps} target_compression_ratio={target_ratio} "
        f"metric_kind={metric_kind} higher_is_better={higher_is_better}\n"
    )

    # 3-4. 候选扫描 + best 选择（增量 dump）
    report: dict[str, Any] = {
        "metric_kind": metric_kind,
        "higher_is_better": higher_is_better,
        "ratios": ratios,
        "criterion": criterion,
        "finetune_steps": finetune_steps,
        "target_compression_ratio": target_ratio,
        "model_path": args.model_path,
        "candidates": [],
        "best": None,
        "baked_model_path": None,
        "mask_meta_path": None,
    }
    dump_report(report, report_path)

    best: dict[str, Any] | None = None
    for ratio in ratios:
        result = _eval_candidate(
            fp_model, ratio, criterion, rng, train_loader, forward_fn,
            eval_fn, metric_kind, finetune_steps, teacher_model, device,
        )
        report["candidates"].append(result)
        dump_report(report, report_path)  # 增量落盘：崩了能看到已扫部分

        if result["status"] != "ok":
            continue

        # 业务 KPI 过滤：理论压缩比（=1 - 1/speedup）≥ target_ratio 才入选
        speedup = result["theoretical_speedup"]
        compression = 1.0 - 1.0 / speedup if speedup > 0 else 0.0
        if target_ratio is not None and compression < target_ratio:
            sys.stderr.write(
                f"{_LOG_PREFIX}ratio={ratio} 理论压缩 {compression:.3f} < 目标 {target_ratio} → 不入选 best\n"
            )
            continue

        if best is None or is_better(result["metric"], best["metric"], higher_is_better):
            best = {
                "label": f"ratio={result['ratio']}",
                "ratio": result["ratio"],
                "metric": result["metric"],
                "theoretical_speedup": result["theoretical_speedup"],
                "finetuned": result["finetuned"],
                "model": result["_model"],
                "metas": result["_metas"],
                "candidate": {k: v for k, v in result.items() if not k.startswith("_")},
            }
            sys.stderr.write(
                f"{_LOG_PREFIX}new best: {best['label']} → {best['metric']:.6f} "
                f"(speedup={best['theoretical_speedup']})\n"
            )

    if best is None:
        sys.stderr.write(
            f"{_LOG_PREFIX}无候选入选 best（全部失败 或 全部未达 target_compression_ratio={target_ratio}）"
            "→ fail loud (exit 3)\n"
        )
        dump_report(report, report_path)
        sys.exit(3)

    # best 字段写入 report
    report["best"] = {
        "label": best["label"],
        "ratio": best["ratio"],
        "metric": best["metric"],
        "theoretical_speedup": best["theoretical_speedup"],
        "finetuned": best["finetuned"],
    }
    dump_report(report, report_path)

    # 落盘 mask 元数据（下游物理裁剪消费）
    try:
        mask_meta = {
            "model_path": args.model_path,
            "best_ratio": best["ratio"],
            "criterion": criterion,
            "layers": best["metas"],
        }
        mask_meta_path.write_text(
            json.dumps(mask_meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report["mask_meta_path"] = str(mask_meta_path)
        dump_report(report, report_path)
        sys.stderr.write(f"{_LOG_PREFIX}mask meta → {mask_meta_path}\n")
    except Exception as e:
        sys.stderr.write(f"{_LOG_PREFIX}mask 元数据落盘失败（不阻断）: {e}\n")

    # bake（state_dict of best mask-pruned model）
    baked_path_str: str | None = None
    bake_token = (args.bake or "").strip().lower()
    if bake_token in _TRUE_TOKENS:
        import torch
        baked_path = output_dir / "best_pruned_model.pt"
        torch.save(best["model"].state_dict(), baked_path)
        baked_path_str = str(baked_path)
        report["baked_model_path"] = baked_path_str
        dump_report(report, report_path)
        sys.stderr.write(f"{_LOG_PREFIX}baked best → {baked_path_str}\n")
    elif bake_token in _FALSE_TOKENS:
        sys.stderr.write(f"{_LOG_PREFIX}bake=false → skip bake\n")
    else:
        sys.stderr.write(
            f"{_LOG_PREFIX}--bake='{args.bake}' 非法（期望 true/false/1/0/yes/no）\n"
        )
        sys.exit(2)

    # charts
    try:
        from orca.chart import render_chart
        _push_charts(render_chart, report, metric_kind, higher_is_better, best["label"])
    except Exception as e:
        sys.stderr.write(f"{_LOG_PREFIX}无法 import orca.chart（跳过推图）: {e}\n")

    # 显式释放
    try:
        del best["model"]
    except Exception:
        pass
    if "cuda" in device:
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass

    # 8. stdout JSON 摘要（agent 原样回显）
    summary = {
        "output_dir": str(output_dir),
        "report_path": str(report_path),
        "model_path": args.model_path,
        "baked_model_path": report.get("baked_model_path") or "",
        "mask_meta_path": report.get("mask_meta_path") or "",
        "best_ratio": best["ratio"],
        "best_metric": best["metric"],
        "best_theoretical_speedup": best["theoretical_speedup"],
        "candidates_evaluated": len(report["candidates"]),
        "metric_kind": metric_kind,
        "finetuned": bool(best["finetuned"]),
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

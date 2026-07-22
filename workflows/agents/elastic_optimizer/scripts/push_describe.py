#!/usr/bin/env python3
"""push_describe.py —— C1 基线→elastic 结构对比表（viz_describe / elastic_optimizer 末尾调用）。

设计（用户共识 2026-07-18）：
  - 一张 chart_type=table，行 = **baseline 层**（源码顺序的 nn.Conv2d/Linear/...）。
  - 列：[name, 替换前 (baseline), 替换后 (elastic)]。删 model_type / source_project /
    eval_paradigm / training_viable 等元信息（用户不关心）。
  - 「替换前」= AST 静态解析 *_flat.py 的 nn.* 调用（零 import 副作用——flat 文件常
    import 用户项目模块，实例化会失败）。
  - 「替换后」匹配规则（baseline 层 → elastic stage，确定性，以 out_ch 为准）：
      * conv 的 out_channels == stage_widths[i] → 归入 stage i，取其 elastic 配置。
      * out_channels 不属任何 stage_width（产出中间宽度的入口 conv）→ stem（固定，非 elastic）。
      * out_channels 非常量（变量）→ 显 `—`，不编造。
      * Linear → ElasticLinear（head）。
  - 全程 best-effort + fail-soft：supernet.py 由 LLM 生成、结构因 model_type 而异；
    import / 字段缺失 / AST 解析失败 → 推一张 ERROR 表（F1），绝不静默空图。
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import sys
from pathlib import Path
from typing import Any

try:
    from orca.chart import render_chart  # type: ignore
except Exception as e:  # pragma: no cover
    sys.stderr.write(f"[push_describe] 无法 import orca.chart：{e}\n")
    sys.exit(2)


# ── AST 解析 baseline *_flat.py ────────────────────────────────────────────────

# 仅这些 nn.* 调用视为「结构层」参与对比；ReLU/MaxPool 等非替换目标跳过。
_STRUCT_CALLS = {
    "nn.Conv1d", "nn.Conv2d", "nn.Conv3d",
    "nn.Linear", "nn.LazyLinear",
    "nn.ConvTranspose1d", "nn.ConvTranspose2d", "nn.ConvTranspose3d",
}

# conv 类 → (in 位置, out 位置, kernel 位置)
_CONV_ARGPOS = {
    "nn.Conv1d": (0, 1, 2), "nn.Conv2d": (0, 1, 2), "nn.Conv3d": (0, 1, 2),
    "nn.ConvTranspose1d": (0, 1, 2), "nn.ConvTranspose2d": (0, 1, 2),
    "nn.ConvTranspose3d": (0, 1, 2),
}


def _func_name(node: ast.AST) -> str:
    """ast.Call.func → 限定名（如 nn.Conv2d）。非属性/Name 链 → ""。"""
    if isinstance(node, ast.Attribute):
        return f"{_func_name(node.value)}.{node.attr}" if node.value else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _literal(node: ast.AST) -> Any:
    """安全取常量；非常量（变量/表达式）→ None。"""
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _extract_layer(call_name: str, node: ast.Call) -> dict[str, Any]:
    """从 ast.Call 抽 in/out/kernel（None 表示非常量）。"""
    pos = [_literal(a) for a in node.args]
    kw = {kw.arg: _literal(kw.value) for kw in node.keywords if kw.arg is not None}
    info: dict[str, Any] = {}
    if call_name in _CONV_ARGPOS:
        i_in, i_out, i_k = _CONV_ARGPOS[call_name]
        info["in_ch"] = pos[i_in] if i_in < len(pos) else kw.get("in_channels")
        info["out_ch"] = pos[i_out] if i_out < len(pos) else kw.get("out_channels")
        info["kernel"] = pos[i_k] if i_k < len(pos) else kw.get("kernel_size")
    elif call_name in ("nn.Linear", "nn.LazyLinear"):
        info["in_feat"] = pos[0] if len(pos) > 0 else kw.get("in_features")
        info["out_feat"] = pos[1] if len(pos) > 1 else kw.get("out_features")
    return info


def _parse_baseline_layers(flat_path: Path) -> list[tuple[str, dict[str, Any]]]:
    """AST 解析 *_flat.py → [(call_name, layer_info), ...]，按源码顺序。

    零 import 副作用：只读文本 + ast，不执行 flat 文件（其常 import 用户项目模块）。
    """
    tree = ast.parse(flat_path.read_text(encoding="utf-8"))
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _func_name(node.func)
            if name in _STRUCT_CALLS:
                calls.append((node.lineno, node.col_offset, name, node))
    calls.sort(key=lambda t: (t[0], t[1]))  # 源码顺序
    return [(name, _extract_layer(name, node)) for _, _, name, node in calls]


def _conv_before(info: dict[str, Any]) -> str:
    in_ch = info.get("in_ch")
    out_ch = info.get("out_ch")
    k = info.get("kernel")
    in_s = "?" if in_ch is None else in_ch
    out_s = "?" if out_ch is None else out_ch
    k_s = "" if k is None else f", k={k}"
    return f"Conv2d({in_s}→{out_s}{k_s})"


def _linear_before(info: dict[str, Any]) -> str:
    in_f = info.get("in_feat")
    out_f = info.get("out_feat")
    in_s = "?" if in_f is None else in_f
    out_s = "?" if out_f is None else out_f
    return f"Linear({in_s}→{out_s})"


# ── elastic 侧（SearchSpace）──────────────────────────────────────────────────

def _import_supernet(output_dir: Path):
    sys.path.insert(0, str(output_dir))
    try:
        import importlib
        if "supernet" in sys.modules:
            importlib.reload(sys.modules["supernet"])
        import supernet as sn_mod  # type: ignore
        return sn_mod
    finally:
        pass


def _fmt_cands(cands: Any) -> str:
    """候选元组 → '{3,5}'；单值 → '3'。"""
    if isinstance(cands, (tuple, list)):
        if len(cands) == 1:
            return str(cands[0])
        return "{" + ",".join(str(c) for c in cands) + "}"
    return str(cands)


def _elastic_stage_repr(d: dict[str, Any], i: int) -> str | None:
    """stage i 的 compact elastic 描述；i 越界或字段缺 → None。"""
    depth_cands = d.get("stage_depth_candidates") or []
    layer_cfgs = d.get("stage_layer_configs") or []
    if i >= len(depth_cands):
        return None
    parts: list[str] = []
    depth = depth_cands[i]
    if isinstance(depth, (tuple, list)) and len(depth) > 1:
        parts.append(f"depth∈{_fmt_cands(depth)}")
    elif isinstance(depth, (tuple, list)) and len(depth) == 1:
        parts.append(f"depth={depth[0]}")
    # block 选择 + 各参数候选
    cfg = layer_cfgs[i] if i < len(layer_cfgs) else {}
    if isinstance(cfg, dict) and cfg:
        block_strs = []
        for blk, params in cfg.items():
            if isinstance(params, dict) and params:
                pstr = ", ".join(f"{p}∈{_fmt_cands(v)}" for p, v in params.items())
                block_strs.append(f"{blk}({pstr})")
            else:
                block_strs.append(str(blk))
        parts.append("blocks: " + " | ".join(block_strs))
    return ", ".join(parts) if parts else None


# ── 组装对比表 ────────────────────────────────────────────────────────────────

def _build_rows(baseline: list[tuple[str, dict[str, Any]]], d: dict[str, Any]) -> list[dict[str, str]]:
    stage_widths = list(d.get("stage_widths") or d.get("stage_emb_dims") or [])
    width_set = set(stage_widths)
    rows: list[dict[str, str]] = []
    conv_idx = 0
    lin_idx = 0
    for name, info in baseline:
        if name.startswith("nn.Conv") or name.startswith("nn.ConvTranspose"):
            conv_idx += 1
            before = _conv_before(info)
            out_ch = info.get("out_ch")
            if out_ch is None:
                after = "—"
            elif out_ch in width_set:
                after = _elastic_stage_repr(d, stage_widths.index(out_ch)) or "—"
            else:
                after = "stem（固定）"
            rows.append({"name": f"conv{conv_idx}", "替换前": before, "替换后": after})
        elif name in ("nn.Linear", "nn.LazyLinear"):
            lin_idx += 1
            before = _linear_before(info)
            after = "ElasticLinear"
            nm = "head" if lin_idx == 1 and len(baseline) > 0 and (name, info) == baseline[-1] else f"fc{lin_idx}"
            rows.append({"name": nm, "替换前": before, "替换后": after})
    return rows


def _err(msg: str) -> None:
    render_chart(
        chart_type="table",
        data=[{"key": "error", "value": msg[:300]}],
        label="nas/structure",
        title="⚠ Baseline → Elastic",
        columns=["key", "value"],
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output_dir", required=True)
    args = ap.parse_args()
    output_dir = Path(args.output_dir)
    if not output_dir.is_dir():
        sys.stderr.write(f"[push_describe] output_dir 不存在：{output_dir}\n")
        return 1

    flat = next(output_dir.glob("*_flat.py"), None)
    if flat is None:
        _err(f"未找到 *_flat.py（baseline）于 {output_dir}")
        return 0

    # baseline 侧（AST，零副作用）
    try:
        baseline = _parse_baseline_layers(flat)
    except Exception as e:
        _err(f"解析 {flat.name} 失败：{type(e).__name__}: {e}")
        return 0
    if not baseline:
        _err(f"{flat.name} 未解析出任何结构层（Conv/Linear）")
        return 0

    # elastic 侧（import supernet）
    try:
        sn_mod = _import_supernet(output_dir)
        SearchSpace = getattr(sn_mod, "SearchSpace", None)
        if SearchSpace is None:
            _err("supernet.py 无 SearchSpace，无法取 elastic 侧配置")
            return 0
        d = dataclasses.asdict(SearchSpace())
    except Exception as e:
        _err(f"无法 import/解析 supernet.py：{type(e).__name__}: {e}")
        return 0

    rows = _build_rows(baseline, d)
    if not rows:
        _err("组装对比表为空（baseline 层未匹配）")
        return 0

    render_chart(
        chart_type="table",
        data=rows,
        label="nas/structure",
        title="Baseline → Elastic（per baseline layer）",
        columns=["name", "替换前", "替换后"],
        caption=(
            "每个 baseline 结构层对应的 elastic 替换。"
            "stem=固定不可变；depth∈{...}=深度候选；「—」=非常量无法静态推断（不编造）。"
        ),
    )
    print(f"[push_describe] pushed {len(rows)} rows", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

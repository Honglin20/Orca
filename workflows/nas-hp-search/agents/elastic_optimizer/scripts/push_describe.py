#!/usr/bin/env python3
"""push_describe.py —— C1 基线→elastic 结构对比表（pytorch-model-optimizer / elastic_optimizer 末尾调用）。

设计（用户共识 2026-07-18；2026-07-31 富化：真名 + 解析维度 + 超网维度 + 组件候选）：
  - 一张 chart_type=table，行 = **baseline 层**（源码顺序的 nn.Conv2d/Linear/...）。
  - 列：[层名, 替换前, 替换后, 超网维度(后), 组件/深度/核候选]。
    * 层名：AST 赋值目标真名（`self.head`→head；`self.features = nn.Sequential(...)`
      内的层→features[0]/[3]/...）。匹配不到赋值目标才 fallback conv{idx}/fc{idx}。
    * 替换前：baseline 类型 + 维度。维度走符号表消解变量名（`in_channels`/`num_classes`
      等）——优先级 `__main__` 实例化 kwargs > `__init__` 形参默认 > 模块级常量；仍非常量才显 `?`（不编造）。
    * 替换后：elastic 类型（`stem（固定）` / `ElasticConv2d` / `ElasticLinear`）。
    * 超网维度(后)：conv→匹配 stage 的 `stage_widths[i]`（super_out_ch）；head→
      `super_in`(末级 stage 宽度)→`super_out`(num_classes)。stem / 非常量 → `—`。
    * 组件/深度/核候选：`stage_depth_candidates[i]` + `stage_layer_configs[i]` 的 block 选择与参数候选。
  - 「替换前」= AST 静态解析 *_flat.py（零 import 副作用——flat 文件常 import 用户项目模块，实例化会失败）。
  - 「替换后」匹配规则（baseline 层 → elastic stage，确定性，以 out_ch 为准）：
      * conv 的 out_channels == stage_widths[i] → 归入 stage i，取其 elastic 配置。
      * out_channels 不属任何 stage_width（产出中间宽度的入口 conv）→ stem（固定，非 elastic）。
      * out_channels 非常量（变量且符号表消解失败）→ 显 `—`，不编造。
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

# 容器构造：其 positional args 里的结构层按下标命名（self.feat = nn.Sequential(...)）。
_CONTAINER_CALLS = {"nn.Sequential", "nn.ModuleList"}


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


def _resolve(node: ast.AST, symbols: dict[str, Any]) -> Any:
    """先查符号表（消解变量名），再 fallback literal_eval。"""
    if isinstance(node, ast.Name) and node.id in symbols:
        return symbols[node.id]
    return _literal(node)


# ── 符号表：消解 in_channels / num_classes 等变量名为常量 ──────────────────────

def _collect_init_defaults(func: ast.FunctionDef, symbols: dict[str, Any]) -> None:
    """`__init__` 形参默认值 → symbols（positional defaults 对齐末尾 + kwonly defaults）。"""
    a = func.args
    n_pos, n_def = len(a.args), len(a.defaults)
    for j, dft in enumerate(a.defaults):
        arg = a.args[n_pos - n_def + j]
        v = _literal(dft)
        if v is not None:
            symbols[arg.arg] = v
    for arg, dft in zip(a.kwonlyargs, a.kw_defaults):
        if dft is None:
            continue
        v = _literal(dft)
        if v is not None:
            symbols[arg.arg] = v


def _is_main_guard(test: ast.AST) -> bool:
    """识别 `if __name__ == "__main__":`。"""
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1:
        if isinstance(test.ops[0], ast.Eq) and isinstance(test.left, ast.Name) and test.left.id == "__name__":
            return _literal(test.comparators[0]) == "__main__"
    return False


def _main_call_kwargs(tree: ast.Module, class_names: set[str]) -> dict[str, Any]:
    """`__main__` 块里实例化模型类的 kwargs（最高优先级，真实运行值）。"""
    out: dict[str, Any] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and _func_name(sub.func) in class_names:
                    for kw in sub.keywords:
                        if kw.arg is None:
                            continue
                        v = _literal(kw.value)
                        if v is not None:
                            out[kw.arg] = v
            break
    return out


def _build_symbols(tree: ast.Module) -> dict[str, Any]:
    """变量名 → 常量。优先级：__main__ kwargs > __init__ 默认 > 模块级常量（后者先写入被覆盖）。"""
    symbols: dict[str, Any] = {}
    # 1. 模块级常量（最低）
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            v = _literal(stmt.value)
            if v is None:
                continue
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    symbols[t.id] = v
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            v = _literal(stmt.value)
            if v is not None:
                symbols[stmt.target.id] = v
    # 2. 各类 __init__ 形参默认
    class_names: set[str] = set()
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef):
            class_names.add(cls.name)
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == "__init__":
                    _collect_init_defaults(fn, symbols)
    # 3. __main__ 实例化 kwargs（最高，覆盖默认）
    symbols.update(_main_call_kwargs(tree, class_names))
    return symbols


# ── 层名：AST 赋值目标真名 ─────────────────────────────────────────────────────

def _self_attr(target: ast.AST) -> str | None:
    """`self.foo` → 'foo'；其它 → None。"""
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
        return target.attr
    return None


def _map_target(target: ast.AST, value: ast.AST, nm: dict[tuple[int, int], str]) -> None:
    """`self.X = <Call>` → 结构层命名：直接结构层用 X；容器内按全部 positional 下标 X[i]。"""
    attr = _self_attr(target)
    if attr is None or not isinstance(value, ast.Call):
        return
    fname = _func_name(value.func)
    if fname in _STRUCT_CALLS:
        nm[(value.lineno, value.col_offset)] = attr
        return
    if fname in _CONTAINER_CALLS:
        for idx, arg in enumerate(value.args):  # 下标对所有 positional（含 ReLU/Pool）计位
            if isinstance(arg, ast.Call) and _func_name(arg.func) in _STRUCT_CALLS:
                nm[(arg.lineno, arg.col_offset)] = f"{attr}[{idx}]"


def _build_name_map(tree: ast.Module) -> dict[tuple[int, int], str]:
    """{(lineno, col_offset): 层名} —— 扫所有类的所有方法体里的 self.X 赋值。"""
    nm: dict[tuple[int, int], str] = {}
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for fn in cls.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            for stmt in ast.walk(fn):
                if isinstance(stmt, ast.Assign):
                    for tgt in stmt.targets:
                        _map_target(tgt, stmt.value, nm)
                elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                    _map_target(stmt.target, stmt.value, nm)
    return nm


def _extract_layer(call_name: str, node: ast.Call, symbols: dict[str, Any]) -> dict[str, Any]:
    """从 ast.Call 抽 in/out/kernel（None 表示符号表也消解不出的非常量）。"""
    pos = [_resolve(a, symbols) for a in node.args]
    kw = {kw.arg: _resolve(kw.value, symbols) for kw in node.keywords if kw.arg is not None}
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


def _collect_baseline(tree: ast.Module, symbols: dict[str, Any]) -> list[dict[str, Any]]:
    """AST 解析 *_flat.py → [{call, info, attr}, ...]，按源码顺序。零 import 副作用。"""
    name_map = _build_name_map(tree)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            cname = _func_name(node.func)
            if cname in _STRUCT_CALLS:
                calls.append((node.lineno, node.col_offset, cname, node))
    calls.sort(key=lambda t: (t[0], t[1]))  # 源码顺序
    return [
        {"call": cname, "info": _extract_layer(cname, node, symbols), "attr": name_map.get((ln, co))}
        for ln, co, cname, node in calls
    ]


def _conv_before(call_name: str, info: dict[str, Any]) -> str:
    typ = call_name.split(".")[-1]  # Conv2d / ConvTranspose1d / ...
    in_ch, out_ch, k = info.get("in_ch"), info.get("out_ch"), info.get("kernel")
    in_s = "?" if in_ch is None else in_ch
    out_s = "?" if out_ch is None else out_ch
    k_s = "" if k is None else f", k={k}"
    return f"{typ}({in_s}→{out_s}{k_s})"


def _linear_before(info: dict[str, Any]) -> str:
    in_f, out_f = info.get("in_feat"), info.get("out_feat")
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


def _width_label(d: dict[str, Any]) -> str | None:
    """stage 宽度字段的语义标签：conv→super_out_ch；transformer→super_emb_dim。"""
    if d.get("stage_widths") is not None:
        return "super_out_ch"
    if d.get("stage_emb_dims") is not None:
        return "super_emb_dim"
    return None


def _stage_candidates_str(d: dict[str, Any], i: int) -> str | None:
    """stage i 的 depth + 组件/参数候选；i 越界或字段缺 → None。"""
    depth_cands = d.get("stage_depth_candidates") or []
    layer_cfgs = d.get("stage_layer_configs") or []
    if i >= len(depth_cands) and i >= len(layer_cfgs):
        return None
    parts: list[str] = []
    if i < len(depth_cands):
        depth = depth_cands[i]
        if isinstance(depth, (tuple, list)):
            if len(depth) > 1:
                parts.append(f"depth∈{_fmt_cands(depth)}")
            elif len(depth) == 1:
                parts.append(f"depth={depth[0]}")
    cfg = layer_cfgs[i] if i < len(layer_cfgs) else {}
    if isinstance(cfg, dict) and cfg:
        comp_strs = []
        for blk, params in cfg.items():
            if isinstance(params, dict) and params:
                pstr = ", ".join(f"{p}∈{_fmt_cands(v)}" for p, v in params.items())
                comp_strs.append(f"{blk}({pstr})")
            else:
                comp_strs.append(str(blk))
        parts.append("组件: " + " | ".join(comp_strs))
    return "\n".join(parts) if parts else None


# ── 组装对比表 ────────────────────────────────────────────────────────────────

def _build_rows(baseline: list[dict[str, Any]], d: dict[str, Any]) -> list[dict[str, str]]:
    stage_widths = list(d.get("stage_widths") or d.get("stage_emb_dims") or [])
    width_set = set(stage_widths)
    wlabel = _width_label(d)
    rows: list[dict[str, str]] = []
    conv_idx = 0
    lin_idx = 0
    for item in baseline:
        cname, info, attr = item["call"], item["info"], item["attr"]
        is_last = item is baseline[-1]
        if cname.startswith("nn.Conv") or cname.startswith("nn.ConvTranspose"):
            conv_idx += 1
            name = attr or f"conv{conv_idx}"
            before = _conv_before(cname, info)
            out_ch = info.get("out_ch")
            if out_ch is None:
                rows.append({"层名": name, "替换前": before, "替换后": "—（out_ch 非常量，无法匹配 stage）",
                             "超网维度(后)": "—", "组件/深度/核候选": "—"})
            elif out_ch in width_set:
                si = stage_widths.index(out_ch)
                super_w = stage_widths[si]
                super_dim = f"{wlabel}={super_w}" if wlabel else f"super_dim={super_w}"
                rows.append({"层名": name, "替换前": before, "替换后": "ElasticConv2d",
                             "超网维度(后)": super_dim,
                             "组件/深度/核候选": _stage_candidates_str(d, si) or "—"})
            else:
                rows.append({"层名": name, "替换前": before, "替换后": "stem（固定）",
                             "超网维度(后)": "—", "组件/深度/核候选": "—"})
        elif cname in ("nn.Linear", "nn.LazyLinear"):
            lin_idx += 1
            name = attr or f"fc{lin_idx}"
            before = _linear_before(info)
            out_f = info.get("out_feat")
            out_s = str(out_f) if out_f is not None else "?"
            if is_last:
                # head（末个结构层）：super_in=末级 stage 宽度（cheatsheet 契约 ElasticLinear(super_in_dim=最后 stage 宽度)）。
                # 若 baseline in_feat 已解析且与末级宽度不一致（transformer / 带 pooling 的非直连 head）
                # → 暴露矛盾（?(baseline=X,last_stage=Y)），不静默选一个；无 stage 则 ?。
                in_f = info.get("in_feat")
                if stage_widths:
                    last_w = stage_widths[-1]
                    if in_f is not None and in_f != last_w:
                        in_s = f"?(baseline={in_f},last_stage={last_w})"
                    else:
                        in_s = str(last_w)
                else:
                    in_s = "?"
                super_dim = f"super_in={in_s}→super_out={out_s}"
            else:
                # 非 head Linear：SearchSpace 不标准化其超网维度 → 显 —，不用 baseline 维度冒充超网维度。
                super_dim = "—"
            rows.append({"层名": name, "替换前": before, "替换后": "ElasticLinear",
                         "超网维度(后)": super_dim, "组件/深度/核候选": "—"})
    return rows


def _err(msg: str) -> None:
    render_chart(
        chart_type="table",
        data=[{"key": "error", "value": msg[:300]}],
        label="nas/structure",
        title="⚠ Baseline → Elastic",
        columns=["key", "value"],
        caption="诊断/error 兜底：baseline 结构解析失败或对比表为空；详见 value 列。",
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
        tree = ast.parse(flat.read_text(encoding="utf-8"))
        symbols = _build_symbols(tree)
        baseline = _collect_baseline(tree, symbols)
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

    try:
        rows = _build_rows(baseline, d)
    except Exception as e:
        _err(f"组装对比表失败：{type(e).__name__}: {e}")
        return 0
    if not rows:
        _err("组装对比表为空（baseline 层未匹配）")
        return 0

    render_chart(
        chart_type="table",
        data=rows,
        label="nas/structure",
        title="Baseline → Elastic（per baseline layer）",
        columns=["层名", "替换前", "替换后", "超网维度(后)", "组件/深度/核候选"],
        caption=(
            "每个 baseline 结构层对应的 elastic 替换。"
            "层名取自源码赋值目标（Sequential 内带下标）；"
            "替换前维度解析自 __main__ 实例化/__init__ 默认/模块常量；"
            "超网维度(后)=stage 宽度（super_out_ch / super_emb_dim）；head 的 super_in=末级宽度→super_out=num_classes，"
            "若 baseline head in_feat 与末级宽度冲突则显 ?(baseline=…,last_stage=…) 暴露矛盾；"
            "「—」=非常量无法静态推断、stem 固定层、或非 head Linear（SearchSpace 不标准化，不臆造）。"
        ),
    )
    print(f"[push_describe] pushed {len(rows)} rows", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

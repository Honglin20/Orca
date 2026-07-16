"""ast_diff.py —— module-level AST diff（草稿 §9.1 确定性事实摘要）。

契约（草稿 §9.1）：Curator 在结构门跑 module-level AST diff（父 vs 子 model.py），
客观列出"算子类型/拓扑是否变 / 哪些是纯数值改"，作为**参考输入**喂 LLM 终判 tag
（LLM 在事实面前无法把自己的超参微调硬标成 structural）。

AST 不是唯一判据、也不直接驳回；它是防自凑配额的**事实参考**。

入参：
    --parent <path> 父 model.py（champion 快照）
    --child  <path> 子 model.py（candidate 快照）
    [--format json|text]  默认 json

出参（json，stdout）：
    {
      "topology_changed": bool,            # 是否有算子类型/结构原语变化
      "operator_changes": [ ... ],          # 算子类型/Call 函数名变化列表
      "numeric_changes": [ ... ],           # 同算子但数值参数变化（hidden_dim/lr/...）
      "added":   [ "顶层级名", ... ],        # 子新增的 top-level 函数/类/赋值
      "removed": [ "顶层级名", ... ],        # 子删除的
      "summary":  "一句话事实摘要"           # 给 LLM 的参考文本
    }

纯确定性（无 LLM、无网络）；用 Python `ast` 模块逐 top-level 函数/类比对，类内按方法
+ 数值字面量做算子类型 vs 纯数值改的区分。

fail loud：文件缺失 / 语法错 → 非零退出 + stderr。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── 分类表：哪些 Call 函数名视为"算子类型"（变即 structural）─────────────────
# nn.* / torch.nn.* / F.* 的层构造器；触一个 = 算子类型变化（不只是数值改）。
_OPERATOR_PREFIXES = (
    "nn.",
    "torch.nn.",
    "F.",
    "torch.nn.functional.",
    "torch.",
)
# 纯数值参数名（出现在关键字参数且仅改值不算结构变化）。
_NUMERIC_KW_HINTS = (
    "hidden",
    "dim",
    "size",
    "layers",
    "depth",
    "lr",
    "rate",
    "dropout",
    "channels",
    "heads",
    "num_",
    "epochs",
    "batch",
    "momentum",
    "weight_decay",
    "alpha",
    "beta",
    "gamma",
)


def _top_level_decls(tree: ast.Module) -> dict[str, ast.stmt]:
    """收集 top-level FunctionDef / AsyncFunctionDef / ClassDef / Assign 名字 → 节点。

    Assign 用首个 target 的 id（Name 节点）；其它（Import/ImportFrom/Expr/...）忽略。
    """
    out: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = node
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out.setdefault(tgt.id, node)
    return out


def _qualified_call_name(node: ast.Call) -> str:
    """取 Call.func 的可读名（如 nn.Conv2d / torch.randn / self.proj）。无法判定 → ""。"""
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        parts: list[str] = []
        cur: ast.expr = fn
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        else:
            # self.xxx / xxx.attr.attr — 形如 self.proj，取 attr 链。
            parts.append(ast.dump(cur)[:1] if False else "self")
        return ".".join(reversed(parts))
    return ast.dump(fn)[:40]


def _collect_calls(tree: ast.AST) -> list[tuple[str, str]]:
    """遍历子树收集所有 Call（fn 名 + 来源 class.method 标签用于分组）。"""
    calls: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            calls.append((_qualified_call_name(node), "call"))
    return calls


def _collect_numeric_kwargs(tree: ast.AST) -> dict[str, list[Any]]:
    """收集所有 Call 里数值/字符串关键字参数（按 kw 名分组，存原值）。

    只看键名"像超参"的关键字（_NUMERIC_KW_HINTS）。值取 Constant 字面量。
    """
    by_name: dict[str, list[Any]] = defaultdict(list)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg is None:
                continue
            if not any(h in kw.arg.lower() for h in _NUMERIC_KW_HINTS):
                continue
            v = kw.value
            if isinstance(v, ast.Constant) and isinstance(
                v.value, (int, float, str, bool)
            ):
                by_name[kw.arg].append(v.value)
    return dict(by_name)


def _collect_constants(tree: ast.AST) -> list[Any]:
    """收集子树内所有数值/字符串/bool Constant 字面量（按 walk 序，含重复）。

    用于"签名一致但数值不同"的纯数值改检测（如 ``HIDDEN = 256`` → ``HIDDEN = 128``、
    ``num_heads=8`` → ``num_heads=4``、``nn.Linear(784, 256)`` → ``nn.Linear(784, 128)``）。
    bool 是 int 的子类，单独排除以免 True/False 被当 1/0 误比。
    """
    out: list[Any] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant):
            v = n.value
            if isinstance(v, bool):
                out.append(v)  # 保留 bool；不让 True==1 混淆
            elif isinstance(v, (int, float, str)):
                out.append(v)
    return out


def _collect_top_level_assign_constants(decl: ast.stmt) -> dict[str, Any]:
    """top-level ``NAME = <Constant>`` 形式的赋值，按 target 名 → 值收集。

    专门捕捉 ``HIDDEN = 256`` / ``LR = 1e-3`` 这类模块级超参赋值（不被 Call kw 检测覆盖）。
    """
    out: dict[str, Any] = {}
    if isinstance(decl, ast.Assign):
        for tgt in decl.targets:
            if isinstance(tgt, ast.Name) and isinstance(decl.value, ast.Constant):
                v = decl.value.value
                if isinstance(v, (int, float, str, bool)):
                    out[tgt.id] = v
    return out


def _operator_set(decl: ast.stmt) -> set[str]:
    """从单个 decl 体里提"算子类型"集合（被识别为 nn/torch 系的 Call 名）。"""
    ops: set[str] = set()
    for name, _ in _collect_calls(decl):
        if not name:
            continue
        if any(name.startswith(p) for p in _OPERATOR_PREFIXES):
            ops.add(name)
        # 装饰器/方法调用 self.<op> 不算结构算子（除非它就是 nn 层的引用，AST 判不出）。
    return ops


def _decl_signature(decl: ast.stmt) -> str:
    """decl 的结构签名（剥数值 Constant → 占位），用于判定"拓扑是否变"。

    把所有 Constant 替换为 <C>、所有 Name id 规范化 → 比较签名即可判断：
    若签名一致但数值不同 → 纯数值改；若签名不同 → 结构/拓扑改。
    """
    # 深拷贝后 walk 替换 Constant；用 ast.unparse 取签名。
    tree = ast.parse(ast.dump(decl))  # round-trip：dump → parse 得纯结构树
    # dump 后的 ast 已不含原行号；Constant 节点 attr = value。把所有 value 屏蔽。
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant):
            n.value = "<C>"
    # ast.dump(tree) 是结构 hash 的好代表；但带 Constant value=’<C>’ 已统一。
    return ast.dump(tree, annotate_fields=True, indent=None)


@dataclass
class DiffResult:
    topology_changed: bool
    operator_changes: list[dict[str, Any]] = field(default_factory=list)
    numeric_changes: list[dict[str, Any]] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "topology_changed": self.topology_changed,
            "operator_changes": self.operator_changes,
            "numeric_changes": self.numeric_changes,
            "added": self.added,
            "removed": self.removed,
            "summary": self.summary,
        }


def diff(parent_path: str, child_path: str) -> DiffResult:
    """主 diff。纯函数：读两文件 → ast.parse → 结构对比 → 返回 DiffResult。"""
    parent_text = Path(parent_path).read_text(encoding="utf-8")
    child_text = Path(child_path).read_text(encoding="utf-8")
    parent_tree = ast.parse(parent_text, filename=parent_path)
    child_tree = ast.parse(child_text, filename=child_path)

    parent_decls = _top_level_decls(parent_tree)
    child_decls = _top_level_decls(child_tree)

    added = sorted(set(child_decls) - set(parent_decls))
    removed = sorted(set(parent_decls) - set(child_decls))

    operator_changes: list[dict[str, Any]] = []
    numeric_changes: list[dict[str, Any]] = []
    topology_changed = bool(added or removed)

    common = sorted(set(parent_decls) & set(child_decls))
    for name in common:
        p_decl = parent_decls[name]
        c_decl = child_decls[name]

        # 1) 结构签名（数值屏蔽）不同 → 拓扑改。
        sig_p = _decl_signature(p_decl)
        sig_c = _decl_signature(c_decl)
        if sig_p != sig_c:
            topology_changed = True

        # 2) 算子集合差（新增/弃用的 nn.* Call）。
        ops_p = _operator_set(p_decl)
        ops_c = _operator_set(c_decl)
        new_ops = ops_c - ops_p
        gone_ops = ops_p - ops_c
        if new_ops or gone_ops:
            operator_changes.append(
                {
                    "decl": name,
                    "added_operators": sorted(new_ops),
                    "removed_operators": sorted(gone_ops),
                }
            )
            topology_changed = True

        # 3) 纯数值变化（签名相同才算"纯数值改"，不含结构/算子变化）。
        if sig_p == sig_c:
            # 3a) Call kwargs（命名超参，如 num_heads=8 → 4）
            n_p = _collect_numeric_kwargs(p_decl)
            n_c = _collect_numeric_kwargs(c_decl)
            for kw in sorted(set(n_p) | set(n_c)):
                vp = n_p.get(kw, [])
                vc = n_c.get(kw, [])
                if vp != vc:
                    numeric_changes.append(
                        {"decl": name, "kw": kw, "parent": vp, "child": vc}
                    )
            # 3b) top-level Assign 字面量（HIDDEN=256 / LR=1e-3）
            a_p = _collect_top_level_assign_constants(p_decl)
            a_c = _collect_top_level_assign_constants(c_decl)
            for k in sorted(set(a_p) | set(a_c)):
                if a_p.get(k) != a_c.get(k):
                    numeric_changes.append(
                        {
                            "decl": name,
                            "kw": k,
                            "parent": a_p.get(k),
                            "child": a_c.get(k),
                        }
                    )
            # 3c) 兜底：decl 体内所有数值/字符串 Constant 多集合差（捕捉位置参数
            #     如 nn.Linear(784, 256) → nn.Linear(784, 128)，不被上面命名检测覆盖）。
            c_p = _collect_constants(p_decl)
            c_c = _collect_constants(c_decl)
            if c_p != c_c and not any(
                nc["decl"] == name for nc in numeric_changes
            ):
                numeric_changes.append(
                    {
                        "decl": name,
                        "kw": "<constants>",
                        "parent": c_p,
                        "child": c_c,
                    }
                )

    # 摘要：给 LLM 终判做 grounding 的一句话。
    parts: list[str] = []
    if added:
        parts.append(f"新增 top-level: {','.join(added)}")
    if removed:
        parts.append(f"删除 top-level: {','.join(removed)}")
    if operator_changes:
        flat = []
        for oc in operator_changes:
            if oc["added_operators"]:
                flat.append(f"+{oc['decl']}:{','.join(oc['added_operators'])}")
            if oc["removed_operators"]:
                flat.append(f"-{oc['decl']}:{','.join(oc['removed_operators'])}")
        parts.append(f"算子类型变化: {'; '.join(flat)}")
    if numeric_changes:
        flat = [f"{nc['kw']} {nc['parent']}→{nc['child']}" for nc in numeric_changes]
        parts.append(f"纯数值改: {', '.join(flat)}")
    if not parts:
        parts.append("无 detectable 变化（AST 同结构同数值）")
    summary = (
        f"拓扑/算子变化={'是' if topology_changed else '否'}；"
        + "；".join(parts)
    )

    return DiffResult(
        topology_changed=topology_changed,
        operator_changes=operator_changes,
        numeric_changes=numeric_changes,
        added=added,
        removed=removed,
        summary=summary,
    )


def _format_text(r: DiffResult) -> str:
    lines = [
        f"topology_changed: {r.topology_changed}",
        f"added:   {r.added}",
        f"removed: {r.removed}",
        f"operator_changes:",
    ]
    for oc in r.operator_changes:
        lines.append(
            f"  - {oc['decl']}: +{oc['added_operators']} -{oc['removed_operators']}"
        )
    lines.append("numeric_changes:")
    for nc in r.numeric_changes:
        lines.append(
            f"  - {nc['decl']}.{nc['kw']}: {nc['parent']} -> {nc['child']}"
        )
    lines.append(f"summary: {r.summary}")
    return "\n".join(lines)


def _main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "module-level AST diff（草稿 §9.1）：父 vs 子 model.py，"
            "出算子/拓扑变化 vs 纯数值改的客观摘要，供 LLM 结构门终判 grounding。"
        )
    )
    parser.add_argument("--parent", required=True, help="父 model.py（champion）")
    parser.add_argument("--child", required=True, help="子 model.py（candidate）")
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="输出格式（默认 json）",
    )
    args = parser.parse_args()

    try:
        for p in (args.parent, args.child):
            if not Path(p).is_file():
                raise FileNotFoundError(f"文件不存在: {p}")
        result = diff(args.parent, args.child)
    except Exception as e:
        print(f"[ast_diff] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_format_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

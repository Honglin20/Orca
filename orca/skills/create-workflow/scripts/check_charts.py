"""check_charts.py —— workflow 图表脚本静态校验（render_chart 调用契约）。

扫 workflow 目录下 ``agents/**/scripts/*.py`` 里的 ``render_chart`` 调用，规则全
error 级（图表的 registry 查重与静态校验都依赖**调用点可识别 + 关键参数为字符
串字面量**）：

1. **字面量强制**：label / title / chart_type 必须以关键字参数传**字符串字面量**
   （变量、f-string、``**kwargs`` 展开、位置参数/缺省均无法静态校验 → 违规）；
2. **调用形态**：须 ``from orca.chart import render_chart`` 规范名直呼——别名
   import / 属性调用 / 重绑定都使调用点对静态校验不可见 → 违规；
3. **全局唯一**：label+title 组合跨全部扫描文件去重（前端按该组合替换更新，
   撞组合 = 图表互相覆盖）；
4. **heatmap**：``x`` / ``y`` / ``value`` 三参必在；
5. **pareto**：``pareto_x_direction`` / ``pareto_y_direction`` 必须显式传；
6. **try 包裹**：调用必须在 try 块内（推送失败只允许降级为 stderr，不阻断主流程）。

零 call site 是合法态（无图表 workflow）。用法::

    python3 check_charts.py <workflow 目录>...

- stdout：每个输入一行扫描清单 ``<path> → N files / M call sites``，随后逐条
  finding ``<file>:<line> [<rule>] <excerpt>``。
- exit：0 无 error / 1 有 error / 2 用法错（路径不存在 / 输入不是目录，stderr）。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# ── 常量 ──────────────────────────────────────────────────────────────────────

_CHART_FUNC = "render_chart"
_CHART_MODULE = "orca.chart"
_LITERAL_KEYS = ("label", "title", "chart_type")
_HEATMAP_KEYS = ("x", "y", "value")
_PARETO_KEYS = ("pareto_x_direction", "pareto_y_direction")

Finding = tuple[Path, int, str, str]  # (file, line, rule, excerpt)


class _ReadError(Exception):
    """文件读取失败（权限/IO 等环境错）——main 捕获后 stderr + exit 2。"""


# ── 文本读取与 AST 工具 ───────────────────────────────────────────────────────


def _read_text(path: Path) -> tuple[str, bool]:
    """读文件；非 UTF-8 时按替换字符读入并返回标记（调用方打 [warn]，不中断）。"""
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise _ReadError(f"读取文件失败 {path}：{e}") from e
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), True


def _is_chart_call(node: ast.Call) -> bool:
    """render_chart 调用：规范名直呼（Name）或属性访问（attr 名命中，仍可静态识别）。"""
    func = node.func
    return (isinstance(func, ast.Name) and func.id == _CHART_FUNC) or (
        isinstance(func, ast.Attribute) and func.attr == _CHART_FUNC
    )


def _in_try(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Call 的祖先链上是否有 Try（含 except* 形态）——AST 判定，非文本缩进猜测。"""
    try_types: tuple[type, ...] = (
        (ast.Try, ast.TryStar) if hasattr(ast, "TryStar") else (ast.Try,)
    )
    p = parents.get(node)
    while p is not None:
        if isinstance(p, try_types):
            return True
        p = parents.get(p)
    return False


# ── 单调用点检查 ──────────────────────────────────────────────────────────────


def _check_call(
    node: ast.Call,
    path: Path,
    parents: dict[ast.AST, ast.AST],
    findings: list[Finding],
    seen_pairs: dict[tuple[str, str], tuple[Path, int]],
) -> None:
    if isinstance(node.func, ast.Attribute):
        findings.append(
            (
                path,
                node.lineno,
                "chart-call-form",
                f"{ast.unparse(node.func)}——属性调用形态，须 from {_CHART_MODULE} import {_CHART_FUNC} 后直呼",
            )
        )

    kw_values: dict[str, ast.AST] = {}
    has_star = False
    for kw in node.keywords:
        if kw.arg is None:
            has_star = True
        else:
            kw_values[kw.arg] = kw.value

    literal: dict[str, str] = {}
    for key in _LITERAL_KEYS:
        value = kw_values.get(key)
        if value is None:
            findings.append(
                (path, node.lineno, "chart-literal", f"{key} 未以关键字字面量传入（registry 查重依赖字面量）")
            )
        elif isinstance(value, ast.Constant) and isinstance(value.value, str):
            literal[key] = value.value
        else:
            findings.append(
                (path, value.lineno, "chart-literal", f"{key}={ast.unparse(value)}——须字符串字面量")
            )
    if has_star:
        findings.append(
            (path, node.lineno, "chart-literal", "**kwargs 展开——label/title/chart_type 无法静态校验")
        )

    chart_type = literal.get("chart_type")
    if chart_type == "heatmap" and not has_star:
        missing = [k for k in _HEATMAP_KEYS if k not in kw_values]
        if missing:
            findings.append(
                (path, node.lineno, "chart-heatmap", f"heatmap 缺必传参数：{'、'.join(missing)}（x/y/value 三参必在）")
            )
    if chart_type == "pareto" and not has_star:
        missing = [k for k in _PARETO_KEYS if k not in kw_values]
        if missing:
            findings.append(
                (path, node.lineno, "chart-pareto", f"pareto 缺显式轴方向：{'、'.join(missing)}")
            )

    if not _in_try(node, parents):
        findings.append(
            (path, node.lineno, "chart-try", "调用不在 try 块内——推送失败必须降级为 stderr，不阻断主流程")
        )

    if "label" in literal and "title" in literal:
        pair = (literal["label"], literal["title"])
        first = seen_pairs.get(pair)
        if first is not None:
            findings.append(
                (
                    path,
                    node.lineno,
                    "chart-dup",
                    f"label={pair[0]!r} + title={pair[1]!r} 与 {first[0]}:{first[1]} 重复（前端会互相覆盖）",
                )
            )
        else:
            seen_pairs[pair] = (path, node.lineno)


# ── 单文件扫描 ────────────────────────────────────────────────────────────────


def _scan_file(
    path: Path,
    findings: list[Finding],
    seen_pairs: dict[tuple[str, str], tuple[Path, int]],
) -> int:
    """单脚本扫描 → render_chart 调用点数（语法错时 fail loud 记 [parse] finding）。"""
    text, replaced = _read_text(path)
    if replaced:
        findings.append((path, 1, "warn", "非 UTF-8 文件，已按替换字符读入——请检查编码"))
    # CPython 执行时按 PEP 263 剥 UTF-8 BOM；decode 后其残留会令 ast.parse 假红。
    text = text.lstrip('\ufeff')
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        findings.append((path, e.lineno or 1, "parse", f"Python 语法错误：{e.msg}"))
        return 0

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    calls = 0
    for node in ast.walk(tree):
        # 别名 import：调用点变成不可识别的裸名，静态校验失效。
        if isinstance(node, ast.ImportFrom) and node.module == _CHART_MODULE:
            for alias in node.names:
                if alias.name == _CHART_FUNC and alias.asname:
                    findings.append(
                        (
                            path,
                            node.lineno,
                            "chart-call-form",
                            f"import {alias.name} as {alias.asname}——别名使调用点不可识别，须规范名直呼",
                        )
                    )
        # 重绑定：x = render_chart 之后的 x(...) 同样不可识别。
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Name)
            and node.value.id == _CHART_FUNC
        ):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id != _CHART_FUNC:
                    findings.append(
                        (
                            path,
                            node.lineno,
                            "chart-call-form",
                            f"{target.id} = {_CHART_FUNC}——重绑定使调用点不可识别",
                        )
                    )
        if isinstance(node, ast.Call) and _is_chart_call(node):
            calls += 1
            _check_call(node, path, parents, findings, seen_pairs)
    return calls


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="check_charts.py",
        description="workflow 图表脚本静态校验（扫 agents/**/scripts/*.py 的 render_chart 调用）",
    )
    ap.add_argument(
        "dirs",
        nargs="+",
        metavar="DIR",
        help="workflow 目录（其下 agents/**/scripts/*.py 为扫描对象）",
    )
    args = ap.parse_args(argv)
    # 控制台编码与 locale 不一致时降级为替换字符输出（不因一个字符崩掉退出码语义）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")

    inputs = [Path(p) for p in args.dirs]
    missing = [p for p in inputs if not p.exists()]
    not_dir = [p for p in inputs if p.exists() and not p.is_dir()]
    if missing or not_dir:
        for p in missing:
            print(f"路径不存在：{p}", file=sys.stderr)
        for p in not_dir:
            print(f"输入必须是 workflow 目录（含 agents/），收到文件：{p}", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    seen_pairs: dict[tuple[str, str], tuple[Path, int]] = {}
    try:
        for d in inputs:
            agents = d / "agents"
            files = (
                sorted(p for p in agents.rglob("*.py") if p.parent.name == "scripts")
                if agents.is_dir()
                else []
            )
            calls = 0
            for f in files:
                calls += _scan_file(f, findings, seen_pairs)
            print(f"{d} → {len(files)} files / {calls} call sites")
    except _ReadError as e:
        print(str(e), file=sys.stderr)
        return 2
    for f, line, rule, excerpt in findings:
        print(f"{f}:{line} [{rule}] {excerpt}")
    return 1 if any(rule != "warn" for _, _, rule, _ in findings) else 0


if __name__ == "__main__":
    sys.exit(main())

"""test_status_literal.py —— ADR §8.1 守门：Status Literal 全覆盖 + widget 无 status 字面量比较。

INTENT（接口收敛 v2 §8.1）：canonical ``Status`` Literal 是节点状态的**唯一权威**。
消费层（widget / app）不许自造 status 字符串（``== "blocked"`` 等字面量比较）——
blocked 等派生态必须经 ``projections.node_status`` 派生（P4）。

本测试双守门：
  1. ``Status`` Literal 全覆盖：``NODE_STATUS_ICONS`` 的 key 与 ``Status`` 完全一致
     （icon 映射是合法 UI 渲染——``_icons.py`` 是 Status 字符串到 icon 的纯查表，不违规）。
  2. AST 检查 widget 代码无 ``== "blocked"`` / ``!= "blocked"`` / ``== "running"`` 等
     status 字面量比较（消费层应走 projections / RunState 派生值，不字面量比较）。
"""

from __future__ import annotations

import ast
import typing
from pathlib import Path

import pytest

from orca.iface.cli.widgets._icons import NODE_STATUS_ICONS
from orca.schema.state import Status


# ── 1. Status Literal 与 NODE_STATUS_ICONS keys 一致 ──────────────────────


class TestStatusLiteralCoverage:
    def test_status_literal_includes_blocked(self):
        """ADR §4.3：Status Literal 必须含 blocked（批 1 落地）。"""
        values = set(typing.get_args(Status))
        assert "blocked" in values, (
            "Status Literal 缺 blocked——ADR §4.3 要求 canonical 含 blocked 派生态"
        )

    def test_status_literal_canonical_values(self):
        """Status Literal 全集 = {pending, running, done, failed, skipped, blocked}。"""
        values = set(typing.get_args(Status))
        assert values == {
            "pending", "running", "done", "failed", "skipped", "blocked",
        }

    def test_node_status_icons_keys_match_status_literal(self):
        """``NODE_STATUS_ICONS`` keys 必须与 ``Status`` Literal 完全一致（无遗漏 / 越界）。

        icon 是 UI 渲染（Status 字符串 → icon 的纯查表），不违规 P4；但 icon 表漏 key
        会让某些 Status 值渲染回退到默认 icon，遮蔽真实状态。
        """
        icon_keys = set(NODE_STATUS_ICONS.keys())
        status_values = set(typing.get_args(Status))
        assert icon_keys == status_values, (
            f"NODE_STATUS_ICONS keys 与 Status Literal 不匹配："
            f"icon 多 {icon_keys - status_values} / 漏 {status_values - icon_keys}"
        )


# ── 2. AST 守门：widget 代码无 status 字面量比较 ──────────────────────────


# Status 字面量候选（所有 Status Literal 值）。
_STATUS_LITERALS = set(typing.get_args(Status))


def _collect_status_literal_compares(node: ast.AST, filepath: str) -> list[str]:
    """递归扫描 AST，找 ``== "status_str"`` / ``!= "status_str"`` 形式的比较。

    只报 status 字符串字面量比较（如 ``== "blocked"``）；不报 status 字符串赋值给变量
    或字典 key（如 ``{"blocked": "icon"}``）——后者是 icon 查表的合法用法。
    """
    violations: list[str] = []

    for sub in ast.walk(node):
        # 只看 Compare 节点（== / !=）
        if not isinstance(sub, ast.Compare):
            continue
        # 只看 Eq / NotEq 操作
        has_eq = any(isinstance(op, (ast.Eq, ast.NotEq)) for op in sub.ops)
        if not has_eq:
            continue
        # 任一 comparator 是 status 字符串字面量 → 违规
        for comparator in sub.comparators + [sub.left]:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                if comparator.value in _STATUS_LITERALS:
                    violations.append(
                        f"{filepath}:{sub.lineno}: 发现 status 字面量比较 "
                        f"({ast.dump(sub)})"
                    )
    return violations


@pytest.fixture(scope="module")
def widget_sources():
    """收集所有 widget / app 源码 AST。"""
    # ``__file__`` = ``tests/iface/cli/test_status_literal.py``；
    # ``parents[3]`` = 仓库根（4 级 parent：tests/iface/cli → tests/iface → tests → root）。
    # 原先少一级 parent 会得到 ``tests/orca/iface/cli``（不存在）→ rglob 空 → 守门失效。
    root = Path(__file__).resolve().parents[3] / "orca" / "iface" / "cli"
    assert root.exists(), f"widget 源码目录不存在：{root}（fixture 路径算错）"
    files = list(root.rglob("*.py"))
    assert files, f"widget 源码目录为空：{root}"
    sources = []
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            sources.append((str(f), tree))
        except SyntaxError:
            # 跳过无法解析的文件（不应有，但防御）
            continue
    return sources


class TestWidgetNoStatusLiteralCompare:
    def test_widgets_no_status_string_compare(self, widget_sources):
        """ADR §8.1 守门：widget / app 代码不许出现 ``== "blocked"`` 等 status 字面量比较。

        violation 即返工：消费层应走 ``projections.node_status()`` 派生 Status 值，
        不字面量比较（P4）。
        """
        all_violations: list[str] = []
        for filepath, tree in widget_sources:
            violations = _collect_status_literal_compares(tree, filepath)
            all_violations.extend(violations)

        assert all_violations == [], (
            "发现 widget/app 代码含 status 字面量比较（违反 P4 / ADR §8.1 守门）：\n"
            + "\n".join(all_violations)
            + "\n消费层应走 projections.node_status() 派生 Status 值，不字面量比较。"
        )

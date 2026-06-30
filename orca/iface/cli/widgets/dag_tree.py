"""dag_tree.py —— 左侧 DAG 节点状态 Tree widget（SPEC §4.1）。

回答「整个 DAG 现在到哪了？」：把 workflow 的全部 node 列成一棵 Tree（parallel 组
为父节点，branches 为子节点），每个 node 前缀状态图标（✓/✽/⏸/!/○）。

设计原则：
  - **壳无真相**：widget 持有的只是「node 名 → 状态图标」的渲染投影，由 app 从
    EventBus 分发事件后调 ``set_status`` 更新。widget 自己不订阅 bus、不解析 Event。
  - **idempotent**：``set_status`` 多次同名同状态幂等（重放一致）。
  - **parallel 组进度**：父节点 label 形如 ``⏸ deploy_group (1/3)``，子节点各带图标。
"""

from __future__ import annotations

from typing import Iterable

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from orca.iface.cli.widgets._icons import NODE_STATUS_ICONS

# parallel 组的「父节点」用 ``#group:<name>`` 作为 tree node key（与普通 node 区分，
# 避免组名与 node 名同名时撞 key）。普通 node 的 key 就是 node 名本身。
_GROUP_PREFIX = "#group:"


def _icon(status: str) -> str:
    """状态 → 图标。未知状态 fallback 到 pending（防御，fail silent 这里可接受）。"""
    return NODE_STATUS_ICONS.get(status, NODE_STATUS_ICONS["pending"])


def _label_text(name: str, status: str, suffix: str = "") -> str:
    """构造 tree node 的纯文本 label：``<icon> <name>[suffix]``。"""
    return f"{_icon(status)} {name}{suffix}"


class DagTree(Tree):
    """DAG 节点状态树（SPEC §4.1）。

    用法（由 OrcaApp 驱动）::

        tree = app.query_one(DagTree)
        tree.build_from_workflow(wf)         # 初始化（全部 pending）
        tree.set_status("fetch", "done")     # 事件驱动更新
        tree.set_group_progress("deploy_group", done=1, total=3)
    """

    DEFAULT_CSS = """
    DagTree {
        width: 1fr;
        border: round $primary;
        padding: 0 1;
        background: $surface;
    }
    DagTree > .tree--label {
        height: 1;
    }
    """

    def __init__(self) -> None:
        # 根 label 空白（自带 ``DAG OUTLINE`` 由 border title 表达更优雅）。
        super().__init__(label="DAG", id="dag-tree")
        self._node_status: dict[str, str] = {}
        self._group_status: dict[str, str] = {}
        self._group_progress: dict[str, tuple[int, int]] = {}
        self._tree_nodes: dict[str, TreeNode] = {}

    # ── 初始化 ──────────────────────────────────────────────────────────

    def build_from_workflow(
        self,
        node_names: Iterable[str],
        parallel_groups: Iterable[tuple[str, list[str]]] | None = None,
    ) -> None:
        """从 workflow 拓扑构造 tree。所有 node 初始 pending。

        Args:
            node_names: 顶层 node 名列表。
            parallel_groups: [(group_name, [branch_names]), ...] —— parallel 组渲染为
                父节点，branches 为子节点。group 名与 node 名共享命名空间（compile 层保证）。
        """
        self.clear()
        self._node_status.clear()
        self._group_status.clear()
        self._group_progress.clear()
        self._tree_nodes.clear()
        # 根节点 ``self.root`` 是 Tree 自带的；我们在其下挂真实节点。
        groups = list(parallel_groups or [])
        group_names = {g for g, _ in groups}
        for name in node_names:
            if name in group_names:
                continue  # 组作为父节点单独挂
            node = self.root.add(_label_text(name, "pending"), expand=False)
            self._tree_nodes[name] = node
            self._node_status[name] = "pending"
        for gname, branches in groups:
            gnode = self.root.add(_label_text(gname, "pending"), expand=True)
            self._tree_nodes[_GROUP_PREFIX + gname] = gnode
            self._group_status[gname] = "pending"
            self._group_progress[gname] = (0, len(branches))
            for b in branches:
                bnode = gnode.add_leaf(_label_text(b, "pending"))
                self._tree_nodes[b] = bnode
                self._node_status[b] = "pending"
        self.root.expand_all()

    # ── 事件驱动更新（由 app 分发）──────────────────────────────────────

    def set_status(self, name: str, status: str) -> None:
        """更新某 node 的状态图标。

        幂等：同名同状态多次调用结果一致（replay 安全）。未知 name 静默忽略
        （防御：parallel 组的 branches 已在 build 时挂，但安全优先于崩溃）。
        """
        if status not in NODE_STATUS_ICONS:
            return  # 未知状态字符串：忽略（防御）
        self._node_status[name] = status
        tnode = self._tree_nodes.get(name)
        if tnode is not None:
            tnode.set_label(_label_text(name, status))

    def set_group_status(self, group_name: str, status: str) -> None:
        """更新 parallel 组父节点的状态图标。"""
        if status not in NODE_STATUS_ICONS:
            return
        self._group_status[group_name] = status
        suffix = self._progress_suffix(group_name)
        tnode = self._tree_nodes.get(_GROUP_PREFIX + group_name)
        if tnode is not None:
            tnode.set_label(_label_text(group_name, status, suffix))

    def set_group_progress(self, group_name: str, done: int, total: int) -> None:
        """更新 parallel 组的进度计数（``1/3``）。父节点 label 重渲染。"""
        self._group_progress[group_name] = (done, total)
        status = self._group_status.get(group_name, "pending")
        suffix = self._progress_suffix(group_name)
        tnode = self._tree_nodes.get(_GROUP_PREFIX + group_name)
        if tnode is not None:
            tnode.set_label(_label_text(group_name, status, suffix))

    def _progress_suffix(self, group_name: str) -> str:
        done, total = self._group_progress.get(group_name, (0, 0))
        return f" ({done}/{total})" if total else ""

    # ── 查询（单测用）────────────────────────────────────────────────────

    def status_of(self, name: str) -> str:
        """读某 node 当前状态（测试用 + ActiveNode 显示选中态）。"""
        return self._node_status.get(name, "pending")

    def label_of(self, name: str) -> str:
        """读某 node 当前 label 文本（测试断言用）。"""
        if name in self._node_status:
            return _label_text(name, self._node_status[name])
        # 组？
        if name in self._group_status:
            return _label_text(name, self._group_status[name], self._progress_suffix(name))
        return ""

"""dag_graph.py —— DAG 拓扑图 widget（phase-12 SPEC §1.1 §4.1 §6.2，替换 dag_tree.py）。

回答「整个 DAG 走到哪了？拓扑长啥样？」：左侧画**带边的拓扑图**（节点=盒子，
parallel 组画成「扇出→汇聚」），状态烤进节点（图标+颜色），可键盘/鼠标选中。
宽度收窄到 ~1/4（CSS max-width: 33%）。

设计原则：
  - **壳无真相**：widget 只持 ``node->status`` 投影 + 拓扑，由 app ``set_status`` 更新；
    不订阅 bus、不读 tape、不解析 Event。
  - **布局可替换（OCP）**：``render()`` 委托 ``DagLayout`` 策略；超 ``cols_budget``
    → 切 ``CompactOutlineLayout`` fallback（不崩、不溢出）。
  - **依赖单向**：仅 import textual + stdlib + 本包常量 + ``dag_layout``；**不** import
    ``orca.exec`` / ``orca.run`` / ``orca.iface.mcp`` / chart-producer（SPEC §0.3）。
"""

from __future__ import annotations

from typing import Iterable

from textual.binding import Binding
from textual.widgets import Static

from orca.iface.cli.widgets._icons import NODE_STATUS_ICONS
from orca.iface.cli.widgets.dag_layout import (
    CompactOutlineLayout,
    CycleDetected,
    DagLayout,
    LayeredDagLayout,
    LayoutIR,
    Topology,
    build_topology,
    detect_cycle,
)

# 图表渲染用的列预算（与 CSS ``max-width: 33%`` 对齐，120 列终端约 32~40 列）。
_COLS_BUDGET = 32


class DagGraph(Static):
    """DAG 拓扑图（SPEC §1.1 / §4.1）。

    用法（由 OrcaApp 驱动）::

        graph = app.query_one(DagGraph)
        graph.build_from_workflow(node_names, parallel_groups, routes)
        graph.set_status("fetch", "done")
        graph.set_group_progress("deploy_group", done=1, total=3)
        graph.select("analyze")   # 用户 j/k 或点击选中

    壳无真相：``_node_status`` / ``_group_progress`` / ``_selected`` 都是渲染投影，
    由 app 从事件流注入。重放同段事件必然一致（SPEC §6.0 铁律 1）。
    """

    DEFAULT_CSS = """
    DagGraph {
        width: 32;
        min-width: 24;
        max-width: 33%;
        border: round $primary;
        padding: 0 1;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("j", "select_next", "下一节点", show=False),
        Binding("k", "select_prev", "上一节点", show=False),
    ]

    def __init__(self) -> None:
        super().__init__("", id="dag-graph")
        self._topo: Topology | None = None
        # node 名 → 状态（与 DagTree 同模式，幂等 set_status）。
        self._node_status: dict[str, str] = {}
        self._group_status: dict[str, str] = {}
        self._group_progress: dict[str, tuple[int, int]] = {}
        self._selected: str | None = None
        # 可替换布局策略（OCP）：默认 LayeredDagLayout，overflow 切 CompactOutlineLayout。
        self._layout: DagLayout = LayeredDagLayout()
        self._fallback: DagLayout = CompactOutlineLayout()

    # ── 初始化 ──────────────────────────────────────────────────────────

    def build_from_workflow(
        self,
        node_names: Iterable[str],
        parallel_groups: Iterable[tuple[str, list[str]]] | None = None,
        routes: dict[str, list[str]] | None = None,
    ) -> None:
        """从 workflow 拓扑构造（API 与 DagTree 对齐 + 新增 routes 派生边）。

        Args:
            node_names: 顶层 node 名列表。
            parallel_groups: ``[(group_name, [branch_names]), ...]``。
            routes: ``{node_name_or_group: [target, ...]}``（含 ``$end``，build_topology 内忽略）。
                用于派生连边；DagTree 不需要这个参数（它是列表），DagGraph 需要它画边。

        ``routes`` 缺省（None）时按无 route 边处理（仅 parallel 扇出/汇聚）；正常 app.py
        会从 ``wf`` 派生 routes 传入。
        """
        self._node_status.clear()
        self._group_status.clear()
        self._group_progress.clear()
        groups = list(parallel_groups or [])
        group_names = {g for g, _ in groups}
        for name in node_names:
            if name in group_names:
                continue
            self._node_status[name] = "pending"
        for gname, branches in groups:
            self._group_status[gname] = "pending"
            self._group_progress[gname] = (0, len(branches))
            for b in branches:
                self._node_status[b] = "pending"

        # 构造 Topology（含环检测）。routes 形如 {node: [targets]}——为复用 build_topology
        # （它从 Workflow 派生），此处直接手工拼 Topology（避免强依赖 Workflow 类型）。
        from orca.iface.cli.widgets.dag_layout import (
            EDGE_PARALLEL_FANOUT,
            EDGE_PARALLEL_MERGE,
            EDGE_ROUTE,
            Edge,
        )

        edges: list[Edge] = []
        node_list = [n for n in node_names if n not in group_names]
        # 顶层 node routes。
        for src, targets in (routes or {}).items():
            for tgt in targets:
                if not tgt or tgt == "$end":
                    continue
                edges.append(Edge(src=src, dst=tgt, kind=EDGE_ROUTE))
        # parallel 组 fanout + merge。
        for gname, branches in groups:
            grp_routes = (routes or {}).get(gname, [])
            for b in branches:
                edges.append(Edge(src=gname, dst=b, kind=EDGE_PARALLEL_FANOUT))
                for tgt in grp_routes:
                    if not tgt or tgt == "$end":
                        continue
                    edges.append(Edge(src=b, dst=tgt, kind=EDGE_PARALLEL_MERGE))

        # entry = node_list[0]（routes 派生用；环检测用 Kahn）。
        entry = node_list[0] if node_list else ""
        self._topo = Topology(
            nodes=node_list + [b for _, brs in groups for b in brs],
            entry=entry,
            edges=edges,
            parallel_groups=groups,
        )
        # 构造期做一次环检测（fail loud；手工拼的 Topology 不经 build_topology，故在此复检）。
        self._assert_acyclic(self._topo)
        self._rerender()

    @staticmethod
    def _assert_acyclic(topo: Topology) -> None:
        """环检测（复用 ``detect_cycle`` 纯函数，DRY；含环 → fail loud 抛 CycleDetected 含环路径）。"""
        cycle = detect_cycle(topo.edges, topo.nodes)
        if cycle is not None:
            raise CycleDetected(cycle)

    # ── 事件驱动更新（由 app 分发）──────────────────────────────────────

    def set_status(self, name: str, status: str) -> None:
        """更新某 node 的状态图标。幂等（replay 一致）。未知状态忽略（防御）。"""
        if status not in NODE_STATUS_ICONS:
            return
        if name in self._node_status:
            self._node_status[name] = status
        elif name in self._group_status:
            # parallel 组名也接受（DagTree 兼容）。
            self._group_status[name] = status
        else:
            return
        self._rerender()

    def set_group_status(self, group_name: str, status: str) -> None:
        """更新 parallel 组状态图标。"""
        if status not in NODE_STATUS_ICONS:
            return
        self._group_status[group_name] = status
        self._rerender()

    def set_group_progress(self, group_name: str, done: int, total: int) -> None:
        """更新 parallel 组进度计数（``1/3``）。幂等。"""
        self._group_progress[group_name] = (done, total)
        self._rerender()

    # ── 选中（j/k / click）─────────────────────────────────────────────────

    def select(self, name: str | None) -> None:
        """选中某节点 + pin（设 ``_selected``，调 ``app._on_node_selected``）。

        SPEC §1.4：用户 j/k 或点选 → ``_auto_follow=False``；``_selected_node=picked``。
        本 widget 只设本地 ``_selected`` + 通知 app（app 负责 ``_auto_follow`` + 驱动 NodeDetail）。
        """
        self._selected = name
        self._rerender()
        # 通知 app（避免反向 import：用 duck-typing 拿 app 句柄）。
        app = self.app
        handler = getattr(app, "_on_node_selected", None)
        if handler is not None and name is not None:
            handler(name)

    @property
    def selected(self) -> str | None:
        return self._selected

    @property
    def status_of(self) -> dict[str, str]:
        """node->status 投影（只读视图，测试用）。"""
        return dict(self._node_status)

    def status_of_node(self, name: str) -> str:
        """读某 node 当前状态（DagTree.status_of 兼容）。"""
        return self._node_status.get(name, "pending")

    # ── Textual actions（j/k 绑定）─────────────────────────────────────────

    def _ordered_nodes(self) -> list[str]:
        """选中导航用的节点线性序（拓扑序）。"""
        if self._topo is None:
            return []
        return list(self._topo.nodes)

    def action_select_next(self) -> None:
        nodes = self._ordered_nodes()
        if not nodes:
            return
        cur = self._selected
        if cur is None or cur not in nodes:
            self.select(nodes[0])
        else:
            idx = nodes.index(cur)
            self.select(nodes[(idx + 1) % len(nodes)])

    def action_select_prev(self) -> None:
        nodes = self._ordered_nodes()
        if not nodes:
            return
        cur = self._selected
        if cur is None or cur not in nodes:
            self.select(nodes[-1])
        else:
            idx = nodes.index(cur)
            self.select(nodes[(idx - 1) % len(nodes)])

    # ── 渲染 ──────────────────────────────────────────────────────────

    def _rerender(self) -> None:
        """重渲染（委托 DagLayout；overflow 切 fallback）。"""
        if self._topo is None:
            self.update("(no workflow loaded)")
            return
        # 合并 node + group 状态投影（layout 只用 node 状态，group 进度单独画在 title）。
        ir = self._layout.layout(
            self._topo, self._node_status, self._selected, _COLS_BUDGET,
        )
        if ir.overflow:
            ir = self._fallback.layout(
                self._topo, self._node_status, self._selected, _COLS_BUDGET,
            )
        # 标题：parallel 组进度（如 ``◎ split (2/2)``）。
        title_parts = []
        for gname, _ in (self._topo.parallel_groups or []):
            done, total = self._group_progress.get(gname, (0, 0))
            status = self._group_status.get(gname, "pending")
            icon = NODE_STATUS_ICONS.get(status, "○")
            title_parts.append(f"{icon} {gname} ({done}/{total})")
        body = "\n".join(ir.lines)
        if title_parts:
            header = "  ".join(title_parts) + "\n" + ("─" * min(_COLS_BUDGET, 32)) + "\n"
        else:
            header = ""
        # 选中提示（C 全屏 / a 跟随）放底部。
        footer = "\n[j/k 选中 · a 跟随]"
        self.update(header + body + footer)

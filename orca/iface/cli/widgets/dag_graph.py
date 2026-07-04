"""dag_graph.py —— DAG 拓扑图 widget（phase-12 SPEC §1.1 §4.1 §6.2，替换 dag_tree.py；
tui-redesign v1.1 §4 升级 3 行盒子 + fan-in + after=None section）。

回答「整个 DAG 走到哪了？拓扑长啥样？」：左侧画**带边的拓扑图**（节点=3 行盒子，
parallel 组画成「扇出→汇聚」），状态烤进节点（图标+iter+耗时/tok），可键盘/鼠标选中。
宽度占 50%（spec v1.1 §7.2）。

设计原则：
  - **壳无真相**：widget 只持 ``node->NodeProjection`` 投影 + 拓扑，由 app
    ``update_node_projection`` 更新；不订阅 bus、不读 tape、不解析 Event。
  - **布局可替换（OCP）**：``render()`` 委托 ``DagLayout`` 策略；超 ``cols_budget``
    → 切 ``CompactOutlineLayout`` fallback（不崩、不溢出）；同层并行 ≥ 5 切 outline（spec §4.3）。
  - **依赖单向**：仅 import textual + stdlib + 本包常量 + ``dag_layout`` + ``_dag_render``；
    **不** import ``orca.exec`` / ``orca.run`` / ``orca.iface.mcp`` / chart-producer（SPEC §0.3）。
"""

from __future__ import annotations

from typing import Iterable

from textual.binding import Binding
from textual.widgets import Static

from orca.iface.cli.widgets._dag_render import (
    NodeProjection,
    render_after_none_section,
    render_main_branch_nodes,
    should_fallback_to_outline,
    split_main_and_after_none,
)
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

# 图表渲染用的列预算（spec v1.1 §7.2：DagGraph 占 50%，120 列终端约 50~60 列）。
_COLS_BUDGET = 50


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
        width: 1fr;
        min-width: 30;
        max-width: 50%;
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
        # spec v1.1 §4.4：3 行盒子投影（iter/elapsed/tokens/error/fan_in）。
        self._node_projections: dict[str, NodeProjection] = {}
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
        self._node_projections.clear()
        groups = list(parallel_groups or [])
        group_names = {g for g, _ in groups}
        for name in node_names:
            if name in group_names:
                continue
            self._node_status[name] = "pending"
            # spec v1.1 §4.4：初始 NodeProjection（status=pending, iter=1, fan_in=0）。
            # fan_in_total 在下面 edges 构造完后补齐。
            self._node_projections[name] = NodeProjection(name=name, status="pending")
        for gname, branches in groups:
            self._group_status[gname] = "pending"
            self._group_progress[gname] = (0, len(branches))
            for b in branches:
                self._node_status[b] = "pending"
                self._node_projections[b] = NodeProjection(name=b, status="pending")

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
        # spec v1.1 §4.5 O2=a：fan_in_total = 静态拓扑入边数（含 parallel merge 边）。
        indeg: dict[str, int] = {n: 0 for n in self._topo.nodes}
        for e in self._topo.edges:
            if e.dst in indeg:
                indeg[e.dst] += 1
        for n, total in indeg.items():
            if n in self._node_projections:
                self._node_projections[n].fan_in_total = total
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

    # ── spec v1.1 §4.4：3 行盒子投影更新 ─────────────────────────────────

    def update_node_projection(
        self,
        name: str,
        *,
        status: str | None = None,
        iter_n: int | None = None,
        elapsed: float | None = None,
        tokens: int | None = None,
        error_msg: str | None = None,
        fan_in_arrived: int | None = None,
    ) -> None:
        """更新单节点的渲染投影（spec v1.1 §4.4 字段级定义）。

        只更新显式传入的字段（None 表示不修改）。幂等：相同调用序列产相同投影。

        ``fan_in_total`` 是静态拓扑派生（在 ``build_from_workflow`` 时一次性算），
        本方法不接收——它的更新走 ``set_fan_in_arrived``（M 动态）。

        reducer 派生 fold 性质（spec §4.4.1）：重放同 tape 必产相同投影。
        """
        if name not in self._node_projections:
            return  # 未知节点防御（与 set_status 同语义）
        proj = self._node_projections[name]
        if status is not None and status in NODE_STATUS_ICONS:
            proj.status = status
            # 同步老 _node_status（既有 _rerender 兼容路径用）
            if name in self._node_status:
                self._node_status[name] = status
        if iter_n is not None:
            proj.iter_n = iter_n
        if elapsed is not None:
            proj.elapsed = elapsed
        if tokens is not None:
            proj.tokens = tokens
        if error_msg is not None:
            proj.error_msg = error_msg
        if fan_in_arrived is not None:
            proj.fan_in_arrived = fan_in_arrived
        self._rerender()

    def projection_of(self, name: str) -> NodeProjection | None:
        """读单节点当前投影（测试用，DRY 通道）。"""
        return self._node_projections.get(name)

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
        """重渲染：spec v1.1 §4 主流走 3 行盒子渲染；fallback 走 CompactOutlineLayout。

        决策树：
          1. 同层并行 ≥ 5（spec §4.3）或窄屏 → fallback ``CompactOutlineLayout``（既有）
          2. 否则用新 ``_dag_render`` 的 3 行盒子 + fan-in 副标 + after=None section
        """
        if self._topo is None:
            self.update("(no workflow loaded)")
            return
        # 主流分层（用既有 LayeredDagLayout 算拓扑序，仅取 layers + edges 派生）。
        layered = self._layout.layout(
            self._topo, self._node_status, self._selected, _COLS_BUDGET,
        )
        # fallback 决策（spec §4.3）
        layer_counts = [len(layer) for layer in layered.layers]
        if should_fallback_to_outline(layer_counts, _COLS_BUDGET) or layered.overflow:
            ir = self._fallback.layout(
                self._topo, self._node_status, self._selected, _COLS_BUDGET,
            )
            self._render_with_title("\n".join(ir.lines))
            return
        # 主流：3 行盒子渲染（spec §4.4 §4.5 §4.6）
        main_layer_names = [[nb.name for nb in layer] for layer in layered.layers]
        # 平展到主流节点列表 + 旁支（after=None）
        flat_nodes: list[str] = [n for layer in main_layer_names for n in layer]
        edges_simple = [(e.src, e.dst) for e in self._topo.edges]
        main_nodes, after_none_nodes, merge_target = split_main_and_after_none(
            flat_nodes, edges_simple,
        )
        # 主流按 layered 分层 reflow（保持 layer 算法的同层横向）
        # 但 split_main_and_after_none 返回的是平展列表；需要把 main_nodes 重映射回分层。
        main_set = set(main_nodes)
        main_layers_filtered = [
            [n for n in layer if n in main_set]
            for layer in main_layer_names
        ]
        main_layers_filtered = [l for l in main_layers_filtered if l]
        body_lines = render_main_branch_nodes(self._node_projections, main_layers_filtered)
        if after_none_nodes:
            body_lines.extend(render_after_none_section(
                self._node_projections, after_none_nodes, merge_target,
            ))
        body = "\n".join(body_lines) if body_lines else "(empty topology)"
        self._render_with_title(body)

    def _render_with_title(self, body: str) -> None:
        """渲染顶层：title（parallel 组进度）+ body + footer（keybinding hint）。"""
        title_parts = []
        if self._topo is not None:
            for gname, _ in (self._topo.parallel_groups or []):
                done, total = self._group_progress.get(gname, (0, 0))
                status = self._group_status.get(gname, "pending")
                icon = NODE_STATUS_ICONS.get(status, "○")
                title_parts.append(f"{icon} {gname} ({done}/{total})")
        if title_parts:
            header = "  ".join(title_parts) + "\n" + ("─" * min(_COLS_BUDGET, 50)) + "\n"
        else:
            header = ""
        footer = "\n[j/k 选中 · a 跟随 · f 过滤]"
        self.update(header + body + footer)

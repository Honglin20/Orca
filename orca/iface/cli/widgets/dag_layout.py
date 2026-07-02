"""dag_layout.py —— DAG 拓扑布局策略（phase-12 SPEC §1.1 §4.1 §6.2）。

把 Workflow 的节点+边投影成可渲染的 ``LayoutIR``（分层盒子 + 连边）。

设计原则：
  - **纯函数 / 数据类**：本模块不依赖 Textual widget，可独立单测（S1 P0 spike）。
  - **OCP / 可替换渲染器**：``DagLayout`` Protocol 是策略接口；``LayeredDagLayout``
    （默认，纵向分层 Sugiyama-lite）与 ``CompactOutlineLayout``（fallback 紧凑 outline）
    同接口。换布局 = 换策略类，**不动 widget / dispatch**（SPEC §1.1 决策）。
  - **fail loud**：拓扑含环 → 抛 ``CycleDetected``（不无限递归）。
  - **依赖单向**：仅 import stdlib + ``orca.schema.workflow``（只读拓扑）；**不** import
    ``orca.exec`` / ``orca.run`` / ``orca.iface.mcp`` / chart-producer（SPEC §0.3 解耦边界）。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from orca.iface.cli.widgets._icons import NODE_STATUS_ICONS

if TYPE_CHECKING:
    from orca.schema.workflow import Workflow

# parallel 组的「扇出节点」/「汇聚点」用此哨兵表达在 ``Topology.edges`` 的 kind：
#   "route"            —— 普通 node.routes 出边（含 group.routes）
#   "parallel-fanout"  —— parallel 组 → 其 branch（扇出）
#   "parallel-merge"   —— branch → parallel 组的下游汇聚点（组 routes.target）
# SPEC §4.1 ``Edge.kind``。
EDGE_ROUTE = "route"
EDGE_PARALLEL_FANOUT = "parallel-fanout"
EDGE_PARALLEL_MERGE = "parallel-merge"

# workflow 级 chart 桶名（与 app.py ``_dispatch_to_widgets`` 的 ``__workflow__`` 对齐，
# SPEC §3.3）。此处不处理 chart，仅作为拓扑不涉及的概念占位注释。


class CycleDetected(Exception):
    """拓扑含环 → fail loud（SPEC §6.2 m3 / 铁律 12）。

    Orca 控制流是 routes 单指针 first-match + parallel 组显式并行，正常经 compile/
    层校验后拓扑无环。但 ``DagLayout`` 是独立策略，不能假设上游已校验——含环时最长路径
    分层会无限递归 / 不收敛，故显式检测 + 抛此异常（不静默吞）。
    """

    def __init__(self, cycle: list[str] | None = None) -> None:
        self.cycle = cycle or []
        super().__init__(
            f"workflow topology has a cycle: {' -> '.join([*self.cycle, self.cycle[0]])}"
            if self.cycle else "workflow topology has a cycle"
        )


# ── 数据类（SPEC §4.1 契约逐字）──────────────────────────────────────────────


@dataclass
class NodeBox:
    """布局产出的单个节点盒子。"""

    name: str
    layer: int
    status: str
    selected: bool
    label: str = ""


@dataclass
class Edge:
    """布局产出的连边。``kind`` 见模块顶部常量。"""

    src: str
    dst: str
    kind: str = EDGE_ROUTE


@dataclass
class LayoutIR:
    """布局中间表示（widget 把它渲染成 Textual 渲染对象）。

    ``layers``：自上而下，每层一组 ``NodeBox``。每个节点恰出现在一层一次。
    ``edges``：层间连边（含 parallel 扇出/汇聚）。
    ``overflow``：最宽层超出 ``cols_budget``（缩写后仍超）→ True，widget 切 fallback。
    ``fallback_outline``：overflow=True 时的紧凑 outline 文本（CompactOutlineLayout 产出）。
    """

    layers: list[list[NodeBox]]
    edges: list[Edge]
    overflow: bool
    fallback_outline: str | None = None
    # 渲染行（box-drawing 文本，每层一行 + 层间连边行）。widget 直接显示即可。
    # 由 ``_render_to_lines`` 派生；LayoutIR 是数据，lines 是便利投影。
    lines: list[str] = field(default_factory=list)


# ── Topology：从 Workflow 派生（含环检测）──────────────────────────────────


@dataclass
class Topology:
    """Workflow 的纯拓扑投影（节点名 + 边 + 入口 + parallel 组）。

    由 ``build_topology(wf)`` 一次性派生。``DagGraph.build_from_workflow`` 缓存它，
    后续 ``set_status`` 只改 status 投影、不动拓扑。含环 → ``build_topology`` 抛
    ``CycleDetected``（构造期 fail loud，不延迟到 layout）。
    """

    nodes: list[str]                          # 全部节点名（拓扑序）
    entry: str                                # 入口节点
    edges: list[Edge]                         # 全部连边（route + parallel fanout/merge）
    parallel_groups: list[tuple[str, list[str]]]  # (group_name, [branch_names])


def build_topology(wf: Workflow) -> Topology:
    """从 ``Workflow`` 派生 ``Topology``（SPEC §4.1 / §6.2 m3）。

    边来源：
      1. ``node.routes[].to``：``$end`` 忽略；其余为目标节点名（``EDGE_ROUTE``）。
      2. parallel 组：
         - ``group → branch``（每个 branch 一条，``EDGE_PARALLEL_FANOUT``）；
         - ``branch → group.routes[].to``（组完成后汇聚到其 route target，
           ``EDGE_PARALLEL_MERGE``）。组名本身不出现在 edges 的 dst（它是逻辑扇出点）。

    环检测：对构造出的有向图做 Kahn 拓扑排序；剩余节点 > 0 → 含环，fail loud。

    节点序：拓扑序（Kahn 输出）；同层按 workflow.nodes 声明序稳定。
    """
    # 1. 收集全部节点名（顶层 nodes；parallel 组名是逻辑扇出点，不作为独立 node 出现在
    #    layers——它由 branch 的 fanout/merge 边表达。但 ``Topology.nodes`` 含组名，供
    #    layout 把组画成「组节点盒子 + 扇出」可选——S1 spike 决策：组名不进 layers，
    #    只 branches 进；组进度由 DagGraph.set_group_progress 单独维护。故此处 nodes
    #    不含组名。但 build 仍把组名记入 parallel_groups 供 layout 用）。
    node_names: list[str] = []
    group_names: set[str] = set()
    for g in wf.parallel:
        group_names.add(g.name)
    for n in wf.nodes:
        if n.name and n.name not in group_names:
            node_names.append(n.name)
    # parallel branches 也是节点（已在 wf.nodes 里），去重保序。
    seen: set[str] = set(node_names)
    parallel_groups: list[tuple[str, list[str]]] = []
    for g in wf.parallel:
        branches = list(g.branches)
        parallel_groups.append((g.name, branches))
        for b in branches:
            if b not in seen:
                node_names.append(b)
                seen.add(b)

    # 2. 构造边 + 邻接表（用于环检测 + 分层）。
    edges: list[Edge] = []
    adj: dict[str, list[str]] = defaultdict(list)
    # 节点 routes（顶层 node，含 foreach/wait/terminate 等；terminate 经 compile 层
    # routes 必空，此处也安全）。
    for n in wf.nodes:
        if not n.name:
            continue
        for r in n.routes:
            tgt = r.to
            if tgt == "$end" or not tgt:
                continue
            edges.append(Edge(src=n.name, dst=tgt, kind=EDGE_ROUTE))
            adj[n.name].append(tgt)
    # parallel 组：fanout（group→branch）+ merge（branch→group.routes.target）。
    for g in wf.parallel:
        for b in g.branches:
            edges.append(Edge(src=g.name, dst=b, kind=EDGE_PARALLEL_FANOUT))
            adj[g.name].append(b)
        # 组的 routes：branch 全部完成后，单指针推进到组 routes.target。
        # 汇聚语义：每个 branch → group.routes.target（merge 边）。若无 routes（组到尾），
        # 则无 merge 边（branch 即叶子）。
        merge_targets = [r.to for r in g.routes if r.to and r.to != "$end"]
        for tgt in merge_targets:
            for b in g.branches:
                edges.append(Edge(src=b, dst=tgt, kind=EDGE_PARALLEL_MERGE))
                adj[b].append(tgt)
        # group 自身作为 fanout 源时，它需要能被 route 命中：上游 node.routes.to=group_name
        # 已在上面 node.routes 循环里加为 EDGE_ROUTE（src=node, dst=group_name）。group 本身
        # 无显式 node.routes 之外的入边——保持。

    # 3. 环检测（复用 detect_cycle 纯函数，DRY；含环 → fail loud 抛 CycleDetected）。
    all_nodes_in_graph: list[str] = list(
        set(node_names) | {g for g, _ in parallel_groups}
        | {e.src for e in edges} | {e.dst for e in edges}
    )
    cycle = detect_cycle(edges, all_nodes_in_graph)
    if cycle is not None:
        raise CycleDetected(cycle)

    # 4. 拓扑序（Kahn，用于分层松弛的稳定遍历序）。
    indeg: dict[str, int] = {n: 0 for n in all_nodes_in_graph}
    for src, dsts in adj.items():
        for d in dsts:
            indeg[d] = indeg.get(d, 0) + 1
    queue: deque[str] = deque([n for n, d in indeg.items() if d == 0])
    topo_order: list[str] = []
    local_indeg = dict(indeg)
    while queue:
        n = queue.popleft()
        topo_order.append(n)
        for m in adj.get(n, []):
            local_indeg[m] -= 1
            if local_indeg[m] == 0:
                queue.append(m)

    return Topology(
        nodes=node_names,
        entry=wf.entry,
        edges=edges,
        parallel_groups=parallel_groups,
    )


def detect_cycle(
    edges: list[Edge], all_nodes: list[str],
) -> list[str] | None:
    """Kahn 环检测（纯函数，``build_topology`` 与 ``DagGraph._assert_acyclic`` 共用，DRY）。

    返回：含环 → 具体环路径 ``[a, b, c]``（表示 a→b→c→a）；无环 → ``None``。
    """
    adj: dict[str, list[str]] = defaultdict(list)
    indeg: dict[str, int] = {n: 0 for n in all_nodes}
    for e in edges:
        adj[e.src].append(e.dst)
        indeg[e.src] = indeg.get(e.src, 0)
        indeg[e.dst] = indeg.get(e.dst, 0) + 1
    # 补全缺失节点入度 key。
    for n in all_nodes:
        indeg.setdefault(n, 0)
    q: deque[str] = deque([n for n, d in indeg.items() if d == 0])
    seen = 0
    li = dict(indeg)
    while q:
        n = q.popleft()
        seen += 1
        for m in adj.get(n, []):
            li[m] -= 1
            if li[m] == 0:
                q.append(m)
    if seen == len(indeg):
        return None
    # 含环：恢复环路径。
    remaining = {n for n, d in li.items() if d > 0}
    return _find_cycle(list(adj.keys()), adj, remaining)


def _find_cycle(
    node_keys: list[str], adj: dict[str, list[str]], in_cycle: set[str]
) -> list[str]:
    """在含环子图里找一条具体环路径（给 ``CycleDetected`` 可读消息用）。

    DFS + 栈；遇到栈内节点即成环。限制在 ``in_cycle`` 子集内搜（Kahn 已定位的环节点）。
    """
    color: dict[str, int] = {}  # 0=white, 1=gray(in stack), 2=done
    stack: list[str] = []

    def dfs(u: str) -> list[str] | None:
        color[u] = 1
        stack.append(u)
        for v in adj.get(u, []):
            if v not in in_cycle:
                continue
            cv = color.get(v, 0)
            if cv == 1:
                # 找到环：从 stack 里 v 的位置截取到末尾 + v。
                idx = stack.index(v)
                return stack[idx:]
            if cv == 0:
                found = dfs(v)
                if found is not None:
                    return found
        stack.pop()
        color[u] = 2
        return None

    for start in node_keys:
        if start in in_cycle and color.get(start, 0) == 0:
            found = dfs(start)
            if found is not None:
                return found
    return list(in_cycle)


# ── DagLayout Protocol（策略接口，OCP）──────────────────────────────────────


class DagLayout(Protocol):
    """拓扑 → ``LayoutIR`` 的纯策略（SPEC §1.1 / §4.1，可替换渲染器）。

    所有实现必须：
      - 含全部 node 名且每个恰一次（``layers`` flatten 后 = ``Topology.nodes`` 集合）。
      - 宽度治理：超 ``cols_budget`` → ``overflow=True``（不崩）。
      - 幂等：同 (topo, status, selected, cols_budget) → 同 LayoutIR。
    """

    def layout(
        self,
        topo: Topology,
        status: dict[str, str],
        selected: str | None,
        cols_budget: int,
    ) -> LayoutIR: ...


# ── LayeredDagLayout（默认：纵向分层，Sugiyama-lite）────────────────────────


# 节点盒子最小宽度（含图标 + 边距）。超过 cols_budget 时先缩写节点名。
_MIN_BOX_WIDTH = 6
# 缩写后单盒子最大宽度（节点名截断到这个长度 + …）。
_ABBREV_WIDTH = 8
# 单层相邻盒子间最小间隔（空格）。
_BOX_GAP = 2


class LayeredDagLayout:
    """纵向分层布局（SPEC §1.1 算法 1-5）。

    1. 分层：最长路径分层（``layer(n)=max(layer(pred)+1)``）；同组 branches 同层。
    2. 同层排序：同组 branches 相邻（贪心最小化交叉）。
    3. 宽度治理：最宽层决定宽度；超 ``cols_budget`` → 先缩写节点名；仍超 → ``overflow=True``。
    """

    def layout(
        self,
        topo: Topology,
        status: dict[str, str],
        selected: str | None,
        cols_budget: int,
    ) -> LayoutIR:
        # parallel 组名 → branches 映射（用于 fanout 同层）。
        group_branches: dict[str, list[str]] = dict(topo.parallel_groups)
        # branch → group 反查。
        branch_group: dict[str, str] = {}
        for gname, branches in topo.parallel_groups:
            for b in branches:
                branch_group[b] = gname

        # 1. 构造用于分层的「有效邻接」：
        #    - 普通 route 边（node→node, node→group）。
        #    - parallel fanout（group→branch）：但 group 不进 layers（见 build_topology 注），
        #      故把 group 当作 branch 的「逻辑前驱」参与分层计算——branch 的 layer 由
        #      group 的 layer 决定（同组 branches 同层）。
        #    - parallel merge（branch→target）：branch 的下游。
        # group 作为「虚拟分层节点」参与计算（有 layer），但不进最终 layers 输出。
        all_layer_nodes: set[str] = set(topo.nodes)
        for gname, _ in topo.parallel_groups:
            all_layer_nodes.add(gname)
        # 入口 = wf.entry（若是 group 名则用 group 名）。
        roots: list[str] = [topo.entry] if topo.entry else []

        # 邻接（分层用）：把 edges 平铺成 src->dst（含 group/branch）。
        adj: dict[str, list[str]] = defaultdict(list)
        for e in topo.edges:
            adj[e.src].append(e.dst)

        # 2. 最长路径分层：从每个 root 做 BFS/松弛。
        #    layer(root)=0；layer(dst)=max(layer(dst), layer(src)+1)。
        #    用拓扑序松弛（已无环，build_topology 保证）——先 Kahn 拿序。
        indeg: dict[str, int] = {n: 0 for n in all_layer_nodes}
        for src, dsts in adj.items():
            for d in dsts:
                indeg[d] = indeg.get(d, 0) + 1
        # 补全缺失 key。
        for n in all_layer_nodes:
            indeg.setdefault(n, 0)
        q: deque[str] = deque([n for n in all_layer_nodes if indeg[n] == 0])
        order: list[str] = []
        li = dict(indeg)
        while q:
            n = q.popleft()
            order.append(n)
            for m in adj.get(n, []):
                li[m] -= 1
                if li[m] == 0:
                    q.append(m)
        # 若 roots 不在 order 头部也无妨——松弛用拓扑序遍历即可。
        layer: dict[str, int] = {n: 0 for n in all_layer_nodes}
        # 入口强制 layer=0（若 entry 是 group 也一样）。
        for r in roots:
            layer[r] = 0
        for n in order:
            for m in adj.get(n, []):
                if layer[m] < layer[n] + 1:
                    layer[m] = layer[n] + 1

        # 同组 branches 强制同层 = max(branches' layers)。group 的 layer 已是
        # branch 的逻辑前驱层，branches 自然 ≥ group layer + 1，但不同 branch 可能因
        # merge 后向边？无后向边（DAG）。仍对齐：取组内 branch 最大 layer。
        for gname, branches in topo.parallel_groups:
            if not branches:
                continue
            mx = max((layer.get(b, 0) for b in branches), default=0)
            for b in branches:
                layer[b] = mx

        # 3. 分层 buckets（排除 group——组不进 layers 输出）。
        max_layer = max((v for k, v in layer.items() if k not in group_branches), default=0)
        buckets: list[list[str]] = [[] for _ in range(max_layer + 1)]
        for n in topo.nodes:
            buckets[layer.get(n, 0)].append(n)
        # 同层排序：同组 branches 相邻（按 group 出现序），其余按声明序。
        # 用声明序作稳定二级 key。
        decl_index = {n: i for i, n in enumerate(topo.nodes)}
        for i, layer_nodes in enumerate(buckets):
            def sort_key(name: str) -> tuple:
                g = branch_group.get(name)
                # 同组 branches 聚在一起：用 (group 出现序, 组内序)。
                if g is not None:
                    g_order = list(group_branches.keys()).index(g)
                    b_order = group_branches[g].index(name)
                    return (0, g_order, b_order)
                # 非组节点：放在组之前（(0,...) < (1,...)），保持声明序。
                return (1, 0, decl_index.get(name, 0))
            buckets[i] = sorted(layer_nodes, key=sort_key)

        # 4. 宽度治理：估算最宽层宽度，决定是否缩写 / overflow。
        def box_label(name: str, abbrev: bool) -> str:
            icon = _status_icon(status.get(name, "pending"))
            short = name
            if abbrev and len(name) > _ABBREV_WIDTH:
                short = name[: _ABBREV_WIDTH - 1] + "…"
            return f"{icon} {short}"

        # 先试不缩写。
        def layer_width(abbrev: bool) -> int:
            widths = []
            for layer_nodes in buckets:
                w = sum(len(box_label(n, abbrev)) for n in layer_nodes)
                w += _BOX_GAP * max(0, len(layer_nodes) - 1)
                widths.append(w)
            return max(widths) if widths else 0

        abbrev = layer_width(abbrev=False) > cols_budget
        overflow = layer_width(abbrev=True) > cols_budget

        # 5. 构造 NodeBox layers + 渲染 lines。
        node_box_layers: list[list[NodeBox]] = []
        for i, layer_nodes in enumerate(buckets):
            node_box_layers.append([
                NodeBox(
                    name=n, layer=i,
                    status=status.get(n, "pending"),
                    selected=(n == selected),
                    label=box_label(n, abbrev),
                )
                for n in layer_nodes
            ])

        lines = _render_layered_lines(node_box_layers, topo, abbrev)

        return LayoutIR(
            layers=node_box_layers,
            edges=list(topo.edges),
            overflow=overflow,
            fallback_outline=None,
            lines=lines,
        )


# ── CompactOutlineLayout（fallback：带边指示的紧凑 outline）──────────────────


class CompactOutlineLayout:
    """紧凑 outline 布局（SPEC §1.1 备选策略 / §6.2 超宽回退）。

    介于列表与全图：缩进表达分层 + 每节点后缀前驱/后继指示符。比现状（DagTree 列表）
    多了「拓扑邻接」信息，又不像 LayeredDagLayout 那样占宽。同 ``DagLayout`` 接口。
    """

    def layout(
        self,
        topo: Topology,
        status: dict[str, str],
        selected: str | None,
        cols_budget: int,
    ) -> LayoutIR:
        # 复用 LayeredDagLayout 的分层算法（共用最长路径），但渲染为缩进 outline。
        layered = LayeredDagLayout().layout(topo, status, selected, cols_budget)
        # 取 layered.layers（已排除 group），转成 outline 文本。
        lines: list[str] = []
        # 后继映射（给指示符用）。
        succ: dict[str, list[str]] = defaultdict(list)
        for e in topo.edges:
            if e.kind == EDGE_PARALLEL_FANOUT:
                continue  # group→branch 不在 outline 显示（group 已不在 layers）
            succ[e.src].append(e.dst)
        for i, layer_nodes in enumerate(layered.layers):
            indent = "  " * i
            for nb in layer_nodes:
                icon = _status_icon(nb.status)
                marker = "▶" if nb.selected else " "
                nxt = succ.get(nb.name, [])
                tail = ""
                if nxt:
                    shown = ",".join(nxt[:3])
                    tail = f"  → {shown}" + (" …" if len(nxt) > 3 else "")
                line = f"{marker}{icon} {nb.name}{tail}"
                line = indent + line
                # 单行宽度治理：超 cols_budget 截断（outline 总能塞下，单行截断不崩）。
                if len(line) > cols_budget:
                    line = line[: cols_budget - 1] + "…"
                lines.append(line)
        # CompactOutline 永不 overflow（它就是 fallback）。
        return LayoutIR(
            layers=layered.layers,
            edges=list(topo.edges),
            overflow=False,
            fallback_outline="\n".join(lines),
            lines=lines,
        )


# ── 渲染辅助（box-drawing 文本）─────────────────────────────────────────────


def _status_icon(status: str) -> str:
    """状态 → 图标（复用 ``_icons.NODE_STATUS_ICONS`` 单真相源，避免双写 drift）。"""
    return NODE_STATUS_ICONS.get(status, NODE_STATUS_ICONS["pending"])


def _render_layered_lines(
    node_box_layers: list[list[NodeBox]],
    topo: Topology,
    abbrev: bool,
) -> list[str]:
    """把分层盒子渲染成 box-drawing 文本行（每层一行 + 层间连边行）。

    简化策略（S1 spike 范围：过断言、可读、不崩）：
      - 每层一行：``  ┌──┐ ┌──┐`` 风格的圆角盒子并排。
      - 层间一行连边：用 ``│`` 竖线连接（不画精确的 ┐┌└┴ 多对多——那是 Sugiyama 全图；
        S1 spike 用简化竖线连边，过断言即可，视觉 sanity 留给截图）。
    """
    if not node_box_layers:
        return ["(empty topology)"]

    lines: list[str] = []
    # 后继邻接（仅 layer 相邻的，画竖线）。
    # node -> layer index
    node_layer: dict[str, int] = {}
    for i, lns in enumerate(node_box_layers):
        for nb in lns:
            node_layer[nb.name] = i

    # 计算每层各 box 的水平起始列（用于连边对齐）。
    layer_starts: list[list[int]] = []
    for layer_nodes in node_box_layers:
        starts: list[int] = []
        col = 0
        for nb in layer_nodes:
            starts.append(col)
            col += len(nb.label) + 2 + _BOX_GAP  # +2 for padding inside box marker
        layer_starts.append(starts)

    # 渲染每层 + 层间连边。
    # node -> (layer index, box-start column)（用于在层间行对齐画 │ 竖线）。
    node_pos: dict[str, tuple[int, int]] = {}
    for i, (layer_nodes, starts) in enumerate(zip(node_box_layers, layer_starts)):
        for nb, start in zip(layer_nodes, starts):
            # │ 画在 box label 的首字符列（box 起始 + 1，避开选中标记列）。
            node_pos[nb.name] = (i, start + 1)

    for i, layer_nodes in enumerate(node_box_layers):
        starts = layer_starts[i]
        # 选中态用 ▶ 标记（反色边框简化为前缀）。
        mid_parts: list[str] = []
        for j, nb in enumerate(layer_nodes):
            start = starts[j]
            pad = start - sum(len(p) for p in mid_parts)
            mid_parts.append(" " * max(0, pad))
            sel = "▶" if nb.selected else " "
            mid_parts.append(f"{sel}{nb.label} ")
        lines.append("".join(mid_parts).rstrip())
        # 层间连边行（非末层）：在本层 box 与下层 box 间画 │。
        if i < len(node_box_layers) - 1:
            edge_cols: set[int] = set()
            for nb in layer_nodes:
                src_layer, src_col = node_pos[nb.name]
                # 该节点的出边（仅画到相邻下层 node 的；group 扇出跳过因 group 不在 layers）。
                for e in topo.edges:
                    if e.src != nb.name:
                        continue
                    dst_pos = node_pos.get(e.dst)
                    if dst_pos is None:
                        continue  # dst 是 group（不在 layers）或更下层——spike 只画相邻层。
                    dst_layer, dst_col = dst_pos
                    if dst_layer == i + 1:
                        edge_cols.add(src_col)
                        edge_cols.add(dst_col)
            if edge_cols:
                # 在 min..max 列范围画 │ 连边行（简化：竖线 + 必要的 ┐┌ 横连）。
                lo, hi = min(edge_cols), max(edge_cols)
                row = [" "] * (hi + 1)
                for c in range(lo, hi + 1):
                    row[c] = "│"
                lines.append("".join(row).rstrip())
            else:
                lines.append("")  # 空行保持层间距
    return lines


__all__ = [
    "CycleDetected",
    "NodeBox",
    "Edge",
    "LayoutIR",
    "Topology",
    "DagLayout",
    "LayeredDagLayout",
    "CompactOutlineLayout",
    "build_topology",
    "detect_cycle",
    "EDGE_ROUTE",
    "EDGE_PARALLEL_FANOUT",
    "EDGE_PARALLEL_MERGE",
]

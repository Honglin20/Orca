# 阶段 12 SPEC —— CLI TUI 重设计（拓扑图 + NodeDetail + 终端图表渲染）

> **状态**：草稿 v2（经 `spec-review-adversarial` 闭环；2 blocker + 9 major + 6 minor 已并入；待监工确认后写实施计划）。本 SPEC 只描述 **TUI 侧渲染**，不含任何后端/生产者改动。
> **依据**：[shells-design-draft.md](shells-design-draft.md) §3 §4.3 §4.5 · [phase-7-cli.md](phase-7-cli.md) §2 §3 §4 · [phase-9d-web-gate-chart.md](phase-9d-web-gate-chart.md) §2（图表契约，**文本落后于代码，见 §2.1 注**）· [phase-3-events.md](phase-3-events.md) §6.0（一条读路径）
> **范围**：① 左侧 `DagTree` 列表 → `DagGraph` 拓扑图（更窄，布局独立成可替换渲染器）；② 右上 `ActiveNode` → `NodeDetail`（tab 化，**6 种节点 kind 永不空白**）；③ 终端图表渲染（消费 `custom(kind=chart)` 事件）。
> **不是**：`render_chart` MCP 工具实现、agent-push 后端、executor 改动——这些由并行 session 负责，**与本 SPEC 解耦**（见 §0.3）。

---

## 0. 阶段目标

### 0.1 动机（用户实测反馈）

当前 TUI（phase 7）三个框存在三个具体问题：

1. **左侧 DAG 占太宽且是列表非图**：`DagTree`（`widgets/dag_tree.py`）是 `Tree` 平铺，看不出节点间的拓扑/路由关系；CSS `width: 1fr`，实测占到屏宽 ~1/2。
2. **右上 `ActiveNode` 跑起来常空白**：它只 append `agent_message`/`agent_thinking`/`agent_tool_call`/`agent_tool_result`（`app.py:550`）。**`script`/`set` 节点不产流式事件 → 面板只剩标题**。用户跑 `demo_parallel`（全 script/set）时「什么都没显示」即此因。且对 `foreach`/`wait`/`terminate` 也无定义。
3. **图表在 TUI 完全不可见**：`_dispatch_to_widgets`（`app.py:494`）无 `custom`/chart 分支，`custom(kind=chart)` 事件落到 `else` 只变日志一行。

### 0.2 目标态

| 区域 | 现在 | 改成 | 回答 |
|---|---|---|---|
| 左 | `DagTree`（列表，~1/2 宽） | `DagGraph`（**拓扑图**，~1/4 宽，可选中节点；布局可替换） | 「DAG 走到哪了？拓扑长啥样？」 |
| 右上 | `ActiveNode`（agent 专属，常空） | `NodeDetail`（流式/输出/图表 tab，**6 kind 永不空**） | 「这个节点在干嘛/产出了啥？」 |
| 右下 | `LogStream` | `LogStream`（加 `/` 过滤、`j/k` 滚动；**高度低于 NodeDetail**） | 「发生过什么？」 |

图表是**节点产物**，挂在 `NodeDetail · 图表 tab`（按 `event.node` 过滤），与 Web 的 `<ChartRenderer nodeId={selected} />`（phase-9d §2.6）对齐；workflow 级（`node=None`）图表归 `__workflow__` 桶（§3.3，对齐 Web Output Panel）。

### 0.3 解耦边界（最重要——与并行后端 session 的隔离）

**铁律不变**：TUI 只是真相源的渲染，只把 tape 上的事件推送给用户。本 SPEC **不新增任何真相源、不订阅新通道、不碰生产者**。

**TUI 的全部外部依赖（仅这三样，且都已存在）**：
1. `Event` 形状（`schema/event.py`：`type`/`data`/`node`/`session_id`/`seq`/`timestamp`）。
2. `custom(kind="chart")` 事件 + `ChartPayload` 契约——**逐字复用 `web/frontend/src/components/chart/types.ts`（source of truth）**。phase-9d §2.2 文本只列 5 种 `chart_type`，**落后于代码 7 种**（line/bar/area/scatter/pareto/radar/table）；以 `types.ts` 为准，phase-12 不改前序 SPEC（决策 D3）。
3. `Workflow` 拓扑（既有 `wf.nodes` / `wf.parallel` / `node.routes`）+ 各 node 的 `kind`（`schema/workflow.py` `AnnotatedNode`：agent/script/set/foreach/wait/terminate）。

**TUI 显式不依赖**：`render_chart` MCP 工具是否存在、agent-push 后端怎么改、用哪个 executor（claude/opencode）、executor 内部、图表事件何时/由谁产出。**agent 流式事件种类因 executor 而异**（claude 发 `agent_thinking`；opencode 不发，`agent_message` 为整块非增量）——NodeDetail 的流式 tab 必须 **executor-agnostic**（见 §4.2/§6.3）。

**给并行后端 session 的契约（整页就这一句）**：
> 只要往 bus/tape 上 emit 满足 `types.ts` 的 `custom(kind="chart")` 事件（`data.chart` = `ChartPayload`，事件顶层带 `node`；`node=None` 表 workflow 级），TUI 就会渲染。**无需为 TUI 做任何额外适配。**

**解耦验收**（§6.1 必测，作用域严格限定）：
- headless：在 `async with app.run_test()` 内、**无生产者/无 executor**，`await app.bus.emit("custom", {"kind":"chart","chart":<payload>}, node="x")` → NodeDetail 图表 tab 出现该图。
- **零后端 import 断言**：`grep` 限定在 **phase-12 新增/替换的 6 个文件**（`widgets/dag_graph.py`、`dag_layout.py`、`node_detail.py`、`chart_panel.py`、`chart_canvas.py`、`screens/chart_browser.py`）——不得 import `orca.exec` / `orca.run` / `orca.iface.mcp` / `render_chart`。**`app.py` 既有的 exec/gates/dialog import（`:337` `AgentToolsMcpServer`、`:721` `RunContext`、`:723` `DialogHandler`）属 phase-11，不在本 SPEC 解耦范围**，不动。

---

## 1. 关键技术决策

### 1.1 DagTree → DagGraph：画成拓扑图（纵向分层，布局可替换）

**决策**：左侧用 `DagGraph` widget 画**带边的拓扑图**（节点=盒子，`│┐┌└┴` 连边，parallel 组画成「扇出→汇聚」），状态烤进节点（图标+颜色），可键盘/鼠标选中。宽度收窄到 ~1/4（CSS §3.2）。

**封装：布局 = 可替换渲染器（OCP）**。布局算法独立成 `DagLayout` 策略（`widgets/dag_layout.py`，纯函数式：`topology + status 投影 + selected + cols_budget → LayoutIR`），`DagGraph` widget 只持拓扑/状态投影，`render()` 委托 `DagLayout`。换布局（layered → compact-outline 降级，或未来别的风格）= 换策略类，**不动 widget、不动 dispatch、不动其他 widget**。`LayoutIR` 契约见 §4.1。

**布局算法（LayeredDagLayout，纵向分层，Sugiyama-lite）**：
1. **建边**：`node.routes[].to`（目标节点；`$end` 忽略）+ parallel 组（`group→branch` 扇出，`branch→group.routes.target` 汇聚）。先做**环检测**：含环 → fail loud 抛 `CycleDetected`（不无限递归，§6.2 m3）。
2. **分层**：`layer(entry)=0`；`layer(n)=max(layer(pred)+1)`（最长路径分层）。同组 branches 同层。
3. **同层排序**：同组 branches 相邻（最小化交叉，贪心）。
4. **绘制**：自上而下逐层一行；节点盒子；层间 box-drawing 连边；parallel 扇出走「组节点下方水平总线 → 各 branch 顶部」。
5. **宽度治理**：最宽层决定最小宽度。超出 `cols_budget`（§3.2 `max-width: 33%`）→ 先缩写节点名；仍超 → `LayoutIR.overflow=True` 并填 `fallback_outline`，`DagGraph` 渲染时切到 `CompactOutlineLayout`，**不崩、不溢出**。

**适用范围**：只覆盖 Orca 结构化拓扑（线性 + parallel 组 + 条件 routes first-match）。**不是通用 DAG 布局器**。复杂/超宽拓扑走 `CompactOutlineLayout` 备选策略（带边指示符的紧凑 outline，介于列表与全图，仍比现状好）。

**风险与去风险**：连边绘制是本 SPEC 最大实现风险。**P0 spike**（§6.2 三条硬断言 + 四条边界）：先在 `demo_linear`/`demo_parallel`/`demo_conditional` 验证 `LayeredDagLayout` 过断言，再铺开。任一不过 → 切 `CompactOutlineLayout` + §9 记 ADR。两策略同接口（都是 `DagLayout`），spike 即「策略 A 不过就用策略 B」。

### 1.2 图表终端渲染：plotext 主依赖 + 开发期降级测试

**决策（已定）**：line/area/bar/scatter/pareto 用 `textual-plotext-ext`（braille 渲染）；`table` 用 Textual 原生 `DataTable`；`radar` 终端性价比低 → 降级为 `DataTable`（原始 records）+「见 Web」提示。未知 `chart_type` **fail loud**（显示「未知 chart_type: X」，对齐 web `ChartWidget.tsx:30`）。

**plotext 是主依赖**（监工已授权「效果好+工作量低即可装新依赖」），写入 `pyproject.toml` 主依赖、CI 完整 install。`ChartCanvas` 在 import 时探测一次（缓存）：line/bar/area/scatter/pareto 走 braille 渲染。**生产路径不降级**——§6.1 必测「完整 install 下 line chart 必须 braille 渲染、不退化」。

**开发期鲁棒性测试（非生产路径）**：§6.4 用 `monkeypatch.setitem(sys.modules, "plotext...", None)` 模拟缺包，断言退到 `DataTable` + 提示、`table` 永远可用、TUI 不崩。这是防御性测试，不表示生产会缺包。

### 1.3 NodeDetail：tab 化，6 种节点 kind 永不空白

**决策**：`ActiveNode` → `NodeDetail`，三段 tab：`流式` / `输出` / `图表(n)`。每种 node kind 都有至少一个 tab 有内容：

| node kind | 流式 tab 数据源 | 输出 tab 数据源 |
|---|---|---|
| **agent** | `agent_message`/`agent_tool_call`/`agent_tool_result`/`agent_thinking`（**种类因 executor 而异**，claude 发 thinking、opencode 不发；渲染按收到的 N 条事件出 N 行，不预设种类） | `node_completed.data.output` |
| **script** | stdout/stderr 尾部（从 `agent_tool_result` 或 `node_completed.data.output.stdout` 取）；running 时显「running…」 | `node_completed.data.output`（`{stdout,stderr,exit_code}`；`parse_json=True` 时附 `output`） |
| **set** | running 时「computing…」；完成后 values 摘要 | `node_completed.data.output`（求值后的 values） |
| **foreach** | `foreach_started`/`foreach_completed` 进度（`done/total/errors`）+ body 流按 `data._index` 折叠分组（**不展开成 per-item 子 tab**，决策 D1-a；对齐 web `topology.ts` foreach 作单节点） | `foreach_completed.data.{outputs,errors,count}` |
| **wait** | `wait_started{duration,reason}` / `wait_completed{elapsed,interrupted}` | `wait_completed.data` |
| **terminate** | `node_started{kind:terminate,status}` | `node_completed.data.outputs`(success) / `reason`(failed) |

- 图表 tab：该节点产出的图（`ChartPanel`，按 `event.node == selected` 过滤）；无图时显「暂无图表」（非空白）。
- **● 徽标语义（定锚，确定性）**：置位 = `upsert_chart`/`append_stream`/`set_output` 调入 **且** 该 tab ≠ 当前 `active_tab`；清除 = 该 tab 被 `Tab.Activated` 切到的瞬间（监听 Textual `Tab.Activated`）。`图表(n)` 的 `n` = 该节点 chart 数。默认 tab = `流式`。
- **永不空白**：选中任何 kind 的节点，至少 `流式` 或 `输出` tab 有内容（running 且无输出时显「(running, 尚无输出)」）。

### 1.4 selected_node + _auto_follow = 临时 UI 交互态（不是业务真相）

**决策**：新增 `OrcaApp._selected_node: str | None` 与 `_auto_follow: bool = True`，均属 shells 草稿 §4.3 铁律 #2 的「临时 UI 交互态」，**不写 tape、不算业务真相**（与既有 `_active_modal`/`_current_node` 同类）。

- **auto-follow（默认）**：`node_started` → `if self._auto_follow: self._selected_node = node`（与现状 `app.py:504-505` 一致）。
- **pin**：用户 `j/k` 或点 DagGraph 节点 → `_auto_follow = False`；`_selected_node = picked`。pin 后 `node_started` **不再覆盖**选中。
- **恢复跟随**：按 `a` → `_auto_follow = True`；`_selected_node = 当前 running 节点`（无 running 则不变）。
- `_selected_node` 驱动 `NodeDetail` 全部内容（流式/输出/图表都按它过滤）。

> 兑现 phase-7 §6.3「↑↓ 切换选中节点 → ActiveNode 更新」原始验收点（当时只做 auto-follow，没键导航 + pin 语义）。

---

## 2. 数据契约（只复用，不新定义）

### 2.1 图表事件契约 = `types.ts`（逐字引用，DRY）

TUI **不定义** ChartPayload。source of truth = [`orca/iface/web/frontend/src/components/chart/types.ts`](../../orca/iface/web/frontend/src/components/chart/types.ts)。phase-9d §2.2 文本只列 5 种、**落后于代码 7 种**；以 `types.ts` 为准，phase-12 不改前序 SPEC（D3-b）。摘录关键字段供 dispatch 判定：

```python
# custom 事件 data 形状（TUI 只读不定义）
{
  "kind": "chart",
  "chart": {
    "chart_type": "line"|"bar"|"area"|"scatter"|"pareto"|"radar"|"table",  # 7 种
    "label": str,            # 分组键
    "title": str,            # 同 label 下唯一键
    "data": list[dict],      # 扁平 record array
    "x"?: str, "y"?: str, "hue"?: str, "columns"?: list[str],
    "pareto_direction"?: ..., "pareto_x_direction"?: ..., "pareto_y_direction"?: ...,
  }
}
```

**实时更新语义**（phase-9d §2.7）：同 `label+title` 的事件 → **替换不堆积**。TUI 必须复刻（对齐 web `dedupeByLabelTitle`）。

### 2.2 TUI 渲染投影（内部数据结构，非对外契约）

`ChartPanel` 内部持有一个**确定性 fold**（与 `DagTree._node_status` 同模式，壳无真相）：

```python
# ChartPanel 内部（非契约，可随实现调整）
# node -> label -> title -> ChartPayload（同 label+title 幂等替换）
_projection: dict[str, dict[str, dict[str, ChartPayload]]]
```

- 由 `_dispatch_to_widgets` 的 chart 分支调 `panel.upsert(node, payload)` 维护（幂等 upsert：同输入同输出）。
- **真相永远在 tape**；投影是 `tape.replay()` 过滤 `custom(chart)` 的**确定性 fold**——同输入同输出，可整条重放重建。TUI 当前无 replay UI（phase-7 §3.4 边界），但按确定性 fold 构造，未来加 replay 零改动（一条读路径）。§6.0.3 用「清空投影→重放→一致」单测证伪（不借用 web 不可测的 replay 词汇）。

---

## 3. 架构设计

### 3.1 文件结构（新增/替换）

```
orca/iface/cli/widgets/
├── dag_layout.py     # 新：DagLayout 策略 + LayoutIR 契约（LayeredDagLayout / CompactOutlineLayout）
├── dag_graph.py      # 新：DagGraph widget（持拓扑/状态投影，render 委托 DagLayout；替换 dag_tree.py）
├── node_detail.py    # 新：NodeDetail（替换 active_node.py）
├── chart_panel.py    # 新：ChartPanel（投影 + label 分组 + 同键替换 + all_charts() 公共 API）
├── chart_canvas.py   # 新：ChartCanvas（渲染单个 ChartPayload：plotext/DataTable/降级/fail loud）
├── header.py         # 不变
└── log_stream.py     # 不变（仅 UI 加 / 过滤、j/k 滚动）
orca/iface/cli/screens/
└── chart_browser.py  # 新：ChartBrowser（ModalScreen，C 全屏跨节点多图浏览）
```

- `dag_tree.py` / `active_node.py`：**删除**（compose 改 yield 新 widget；测试迁移/重写到新类）。
- `_icons.py`：复用（`NODE_STATUS_ICONS` 给 DagGraph 用）。
- **kind 来源**：`OrcaApp.__init__` 静态构建 `_node_kinds: dict[str, str]`（`{n.name: n.kind for n in wf.nodes}`，同 `self._agent_node_names` 模式 `app.py:374`），经 `NodeDetail.set_node(name, kind)` 透传。**不读 `node_started.data.kind`**（foreach 无顶层该事件，`run/foreach.py:73`）。

### 3.2 OrcaApp 布局 CSS（完整，替换既有散落规则）

`OrcaApp.CSS` classvar（布局规则；widget 自身的 border/padding 仍在各 widget `DEFAULT_CSS`）：

```css
Screen { layout: vertical; }
#main-row { height: 1fr; }                     /* Header/Footer 之外的主区 */
DagGraph { width: 32; min-width: 24; max-width: 33%; }   /* 左图窄，硬上限 1/3 */
#right-col { width: 1fr; }                      /* 右列吃掉剩余 */
NodeDetail { height: 3fr; }                     /* 右上详情高于右下日志（非均分，详情优先）*/
LogStream  { height: 2fr; }
```

```python
# orca/iface/cli/app.py  compose()
def compose(self) -> ComposeResult:
    yield Header()
    with Horizontal(id="main-row"):
        yield DagGraph()                       # 左：拓扑图（窄，布局可替换）
        with Vertical(id="right-col"):
            yield NodeDetail()                 # 右上：tab 化详情（3fr）
            yield LogStream()                  # 右下：日志（2fr，矮于详情）
    yield Footer()
```

> 完整 install + 120 列终端下，左图 ~32 列（~1/4），NodeDetail:LogStream ≈ 3:2。

### 3.3 _dispatch_to_widgets 新增 chart 分支

```python
# orca/iface/cli/app.py  _dispatch_to_widgets()  新增分支（不改既有分支）
elif etype == "custom" and data.get("kind") == "chart":
    payload = data.get("chart")
    if not isinstance(payload, dict):
        return  # 防御：非 dict 静默跳过 + 记 warning（与 §6.4 残缺 payload 同语义）
    node_key = node if node is not None else "__workflow__"   # workflow 级图表归此桶（D2-a）
    self.query_one(NodeDetail).upsert_chart(node_key, payload)
    # workflow 级 chart 不属于任何节点 → 仅 ChartBrowser 顶层可见（§4.5）
```

- 只此一处新增；既有 `node_*`/`agent_*`/gate/终态分支**不动**。
- `custom` 且 `kind != "chart"`（如未来 `table`/`image`）仍落 `else` → LogStream（本 SPEC 不处理，§8）。
- **`node is None` 显式归 `__workflow__`**（不再用 `node or ""` 静默塞空串桶）；该桶在 ChartBrowser 永远顶层（§4.5，对齐 phase-9d §2.6 Output Panel）。

### 3.4 单一读路径（不变，重申）

所有 widget 状态仍由 `_consume_events`（`app.py:484`）→ `_dispatch_to_widgets` 单路径注入。**widget 不订阅 bus、不解析 Event、不读 tape**。ChartPanel 投影只是这条单路径的一个落点。三壳/多 widget 视觉必然同步（shells 草稿 §4.3 铁律 1）。

---

## 4. Widget 设计

### 4.1 DagGraph + DagLayout（替换 DagTree）

**LayoutIR 契约（`widgets/dag_layout.py`）**：

```python
from dataclasses import dataclass, field
from typing import Protocol

@dataclass
class NodeBox:
    name: str; layer: int; status: str; selected: bool; label: str = ""
@dataclass
class Edge:
    src: str; dst: str; kind: str = "route"   # "route" | "parallel-fanout" | "parallel-merge"
@dataclass
class LayoutIR:
    layers: list[list[NodeBox]]               # 自上而下，每层一组 box
    edges: list[Edge]
    overflow: bool                             # 超出 cols_budget
    fallback_outline: str | None = None        # overflow=True 时的紧凑 outline 文本

class DagLayout(Protocol):
    """拓扑 → LayoutIR 的纯策略（可替换，OCP）。"""
    def layout(self, topo: "Topology", status: dict[str, str],
               selected: str | None, cols_budget: int) -> LayoutIR: ...
```

两实现：`LayeredDagLayout`（默认）、`CompactOutlineLayout`（fallback）。`Topology` 由 `DagGraph.build_from_workflow(wf)` 从 `wf.nodes`/`wf.parallel`/`routes` 一次性派生（含环检测）。

**DagGraph widget**：

```python
class DagGraph(Widget):
    """DAG 拓扑图（§1.1）。壳无真相：只持 node->status 投影，由 app.set_status 更新；render 委托 DagLayout。"""
    def build_from_workflow(self, node_names, parallel_groups, routes) -> None: ...  # 派生 Topology
    def set_status(self, name: str, status: str) -> None: ...      # 幂等（沿用 DagTree 语义）
    def set_group_progress(self, group: str, done: int, total: int) -> None: ...
    def select(self, name: str | None) -> None: ...                 # 设 _selected_node + pin（调 NodeDetail.set_node）
    @property
    def selected(self) -> str | None: ...
    def render(self):                                               # 委托 self._layout.layout(...) → 渲染 LayoutIR
        ir = self._layout.layout(self._topo, self._status, self._selected, self._cols_budget())
        if ir.overflow: ir = self._fallback.layout(...)             # 切 CompactOutlineLayout
        return _render_ir(ir)                                        # → Textual renderable
```

- **API 对齐 DagTree**：保留 `set_status`/`set_group_progress`/`build_from_workflow` 签名，`_dispatch_to_widgets` 的 `node_*` 分支零改动（只把 `query_one(DagTree)` 换成 `query_one(DagGraph)`）。
- 交互：`on_click`/聚焦时 `j/k` → `select()`；选中态高亮（反色边框 `▶ x ◀`）。`select()` 调 `app._on_node_selected(name)`（设 `_selected_node`、`_auto_follow=False`、`NodeDetail.set_node(name, kind)`）。

### 4.2 NodeDetail（替换 ActiveNode）

```python
class NodeDetail(Widget):
    """选中节点详情：流式/输出/图表 tab（§1.3）。壳无真相。"""
    def set_node(self, name: str | None, kind: str | None = None) -> None: ...  # DagGraph.select / auto-follow 调
    def append_stream(self, node: str, line: str) -> None: ...   # 流式 tab（仅当 node==_selected 才显示）
    def set_output(self, node: str, output: Any) -> None: ...     # node_completed 时调
    def upsert_chart(self, node_key: str, payload: dict) -> None: ...  # custom(chart) 分支调（转发内部 ChartPanel）
    @property
    def active_tab(self) -> str: ...                              # 流式|输出|图表
    def all_charts(self) -> list[tuple[str, list[dict]]]: ...     # 转发 ChartPanel.all_charts()（ChartBrowser 用）
```

- **kind 透传**：`set_node(name, kind)` 由 `app` 传 `_node_kinds[name]`（§3.1）；NodeDetail 按 kind 选数据源派发（§1.3 表）。
- **流式按 node 过滤**：`append_stream(node, line)` 只在 `node == _selected` 时入流式 tab（别节点的事件不混入）；同时若 `流式 != active_tab` → 置 `●`。
- **输出 tab**：`set_output(node, output)`；仅 `node == _selected` 显示；若 `输出 != active_tab` → 置 `●`。
- **图表 tab**：内部 `ChartPanel`，`upsert_chart` 转发；图表 tab 显该节点（`_selected`）的图；新图到且 `图表 != active_tab` → 置 `●`。
- **● 清除**：监听 `on_tabbed_menu_tab_activated`（Textual `Tabs.TabActivated`）→ 切到的 tab 清 `●`。
- **agent_* 种类 executor-agnostic**：流式 tab 把收到的 `agent_*` 事件逐条成行（不区分 thinking/message 是否齐备）；claude 多 thinking 行、opencode 仅 message 行，都正确显示（§6.3 断言 N 事件 → N 行）。
- 兼容：保留 `set_active(name)` / `append_line(line)` 作为旧名别名，减小 `_dispatch_to_widgets` 现有 `agent_*` 分支 diff（行为等价）。

### 4.3 ChartPanel + ChartCanvas（NodeDetail 图表 tab 的内容）

```python
class ChartPanel(Widget):
    """图表集合：按 label 分组、同 label+title 幂等替换（§2.1/§2.2）。确定性 fold 投影。"""
    def upsert(self, node_key: str, payload: dict) -> None: ...   # 幂等替换；payload 残缺（缺 chart_type / data 非 list）→ 跳过+warning
    def charts_for(self, node_key: str) -> dict[str, list[dict]]: ...  # label -> [ChartPayload]（去重后）
    def all_charts(self) -> Iterator[tuple[str, dict[str, list[dict]]]]: ...  # (node_key, label->[charts])；ChartBrowser 公共 API
    # 渲染：label 折叠组 + 焦点大图（聚焦时 j/k 切）；空 →「暂无图表」

class ChartCanvas(Widget):
    """渲染单个 ChartPayload（§1.2）。plotext import 探测一次（缓存）。"""
    def render_payload(self, payload: dict) -> None: ...
    # chart_type 分派：line/bar/area/scatter/pareto -> plotext braille；table -> DataTable；
    # radar -> DataTable 降级 +「见 Web」；未知 -> fail loud「未知 chart_type: X」
```

- **多图（「多副图」）**：同节点多图按 `label` 折叠组；组内聚焦时 `j/k` 选焦点图，焦点图在 `ChartCanvas` 大图渲染，其余为列表项（规整排列）。图多到面板装不下 → `C` 进 `ChartBrowser`（§4.5）全屏。
- **ChartBrowser 数据源 = `ChartPanel.all_charts()`**（公共 API，不读 `_projection` 私有）。

### 4.4 LogStream（基本不变）

- 沿用 `RichLog` + `format_event`（纯函数，既有单测保留）。
- 新增：`/` 触发过滤输入（按 event_type / node / 文本过滤）；聚焦时 `j/k` 滚动。仅 UI，不改 `format_event` 契约。

### 4.5 ChartBrowser（ModalScreen，C 全屏跨节点）

```python
class ChartBrowser(ModalScreen):
    """全屏图表浏览：跨所有节点 + workflow 级图，按 node_key/label 树状导航 + 大图预览。"""
```

- 数据源：`app.query_one(NodeDetail).all_charts()`（含 `__workflow__` 桶，**永远顶层**）。
- 用途：单节点图太多、或横向对比多节点图。`C` 进入，`Esc/q` 退出。

---

## 5. 布局与键位

终态主屏（与已确认 mockup 一致；NodeDetail 高于 LogStream）：

```
┌─ Header: Orca · <wf> · run <id> · ▰▰▰▰▱ n/m · gates k · $cost · elapsed ──────────────────────┐
├────────────────────────┬──────────────────────────────────────────────────────────────────────┤
│                        │  NodeDetail: <node> · <kind> · <exec> · <status>                    ▲ │
│      ╭──────────╮      │  [ ●流式 ] [ 输出 ] [ ◆图表 n ]   ← ●新内容 / ◆当前                  │
│      │ ✓ start  │      │  ┌ charts · <label> ────────────────────────────────────────────┐  ░ │
│      ╰─────┬────╯      │  │  <焦点大图：plotext braille / DataTable>                     │  ░ │
│     ╭──────┴──────╮    │  │  ◦ <其他 title>   [聚焦时 j/k 切图 · C 全屏]                  │  ▼ │
│     │ ◎ split 2/2 │    │  └──────────────────────────────────────────────────────────────┘    │
│     ╰──┬──────┬───╯    ├──────────────────────────────────────────────────────────────────────┤
│   ┌────┴───┐┌───┴────┐ │ LogStream (2fr，矮于详情)                       [/ 过滤 · j/k 滚动]    │
│   │✓ b_a   ││✓ b_b   │ │ HH:MM:SS [sess] <event 行>                                         │
│   └────┬───┘└───┬────┘ │ ...                                                                │
│        └──┬───┘        │                                                                    │
│     ╭─────┴──────╮     │                                                                    │
│     │▶ analyze ◀ │ sel │                                                                    │
│     ╰─────┬──────╯     │                                                                    │
│     ╭─────┴─────╮      │                                                                    │
│     │ ○ report  │      │                                                                    │
│     ╰───────────╯      │                                                                    │
├────────────────────────┴──────────────────────────────────────────────────────────────────────┤
│ q 退出 · Tab 切面板 · j/k 选中/切图 · a 跟随活跃 · c 图表 · C 全屏图表 · d 对话 · i 中断 · / 过滤 │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**键位消解规则（focus-based，避免冲突）**：`j/k` 由**当前聚焦 widget** 的 `BINDINGS` 处理——DagGraph 聚焦 → 上下选节点；ChartPanel 聚焦（图表 tab active）→ 切焦点图；LogStream 聚焦 → 滚动。app 级 `Tab` 走 Textual `focus_next`（DagGraph → NodeDetail → LogStream 循环）；NodeDetail **内部** `Tab`（`TabbedContent`）仅在 NodeDetail 聚焦时切流式/输出/图表。`c` = 聚焦 NodeDetail + 切图表 tab。

| 键 | 作用 | 备注 |
|---|---|---|
| `Tab`（app） | `focus_next` 循环聚焦三 widget | — |
| `Tab`（NodeDetail 内） | 切流式/输出/图表 tab | 仅 NodeDetail 聚焦时 |
| `j`/`k` | 上下文相关（选节点 / 切图 / 滚日志） | focus-based |
| `a` | 恢复 auto-follow 当前 running 节点 | 清 pin（`_auto_follow=True`） |
| `c` | 聚焦 NodeDetail + 切「图表」tab | — |
| `C` | 全屏 ChartBrowser | Esc/q 退 |
| `/` | LogStream 过滤 | — |
| `q` / `d` / `i` | 退出 / 对话 / 中断 | 既有（phase 11） |

---

## 6. 验收标准

### 6.0 验收总则（铁律，逐条必过）

1. **壳无真相**：DagGraph/NodeDetail/ChartPanel/ChartCanvas 均不订阅 bus、不读 tape、不解析 Event；状态只由 `_dispatch_to_widgets` 注入。
2. **单一读路径**：chart 投影与 DAG 状态走同一条 `_consume_events`；无第二条订阅。
3. **确定性 fold**（原「replay-safe」）：ChartPanel 投影 = `tape.replay()` 过滤 `custom(chart)` 的确定性 fold——清空投影→重放同一段事件→投影完全一致（单测证伪）。
4. **临时交互态不污染真相**：`_selected_node` 与 `_auto_follow` 不写 tape（单测：选中 + pin → tape 里无选中/跟随痕迹）。
5. **幂等**：`set_status`/`upsert_chart` 多次同名同值结果一致。

### 6.1 解耦验收（核心，§0.3）

- [ ] headless：在 `async with app.run_test()` 内、**无生产者/无 executor**，`await app.bus.emit("custom", {"kind":"chart","chart":<line payload>}, node="x")` → NodeDetail 图表 tab 出现该图。
- [ ] 同上换 `chart_type` 为 7 种（line/bar/area/scatter/pareto/radar/table）各一 → 各自正确渲染/降级。
- [ ] **完整 install 下**（plotext 在）：line chart 必须 **braille 渲染、不退化**（断言渲染输出含 braille 字符）。
- [ ] **零后端 import 断言**：`grep` 限定 6 个新文件（§0.3 清单），不得出现 `import orca.exec` / `orca.run` / `orca.iface.mcp` / `render_chart`。`app.py` 既有 exec/dialog import 不在范围。

### 6.2 DagGraph + DagLayout

- [ ] `demo_parallel` 渲染拓扑图：`start→split` 扇出到 `branch_a/b` 汇聚到 `merger`，含连边。
- [ ] 节点状态图标+颜色正确（✓/▶/○/✗）；parallel 组 `◎ split (2/2)` 进度。
- [ ] 聚焦时 `j/k` 选中节点 → NodeDetail 切到该节点 + `_auto_follow=False`（pin）；选中节点高亮。
- [ ] 宽度 ≤ 33% 屏宽；超宽走 `CompactOutlineLayout`（不崩、不溢出、有「C 全屏」提示）。
- [ ] **DagLayout 可替换**：`LayeredDagLayout` 与 `CompactOutlineLayout` 实现同一 `DagLayout` 接口；切策略不改 DagGraph/dispatch（单测：换策略类，widget 持有状态不变）。
- [ ] **P0 spike 硬断言**（替换「不崩」）：① `layout()` 在 100 个 seeded 随机拓扑上不抛异常；② 结果 `layers` 含全部 node 名且每个恰一次；③ 渲染宽度 ≤ `cols_budget`（超则 `overflow=True` 不崩）。截图仅人类 sanity，不作 pass/fail。
- [ ] **边界拓扑**：单节点 workflow 不崩；`entry→$end`（无中间节点）不崩；foreach 作**单 box**（body 不展开）；含环路由 → fail loud 抛 `CycleDetected`（不无限递归）。

### 6.3 NodeDetail（治「右上空白」，executor-agnostic）

- [ ] **agent**：发 N 个 `agent_*` 事件 → 流式 tab 有 N 行（不预设 thinking/message 齐备；claude/opencode 都过）。
- [ ] **script**：流式 tab 有 stdout 尾行，输出 tab 有 `{stdout,stderr,exit_code}`。**不再空白**。
- [ ] **set**：流式/输出 tab 有求值 values。
- [ ] **foreach**：流式 tab 有 `foreach_started/completed` 进度 + body 流按 `_index` 折叠；输出 tab 有 `{outputs,errors,count}`。**不展开 per-item 子 tab**。
- [ ] **wait**：流式 tab 有 `wait_started/completed`；输出 tab 有 `wait_completed.data`。
- [ ] **terminate**：流式 tab 有 `node_started{kind:terminate}`；输出 tab 有 outputs/reason。
- [ ] ● 徽标：新内容到非当前 tab → 置位；`Tab.Activated` 切到该 tab → 清除（单测：`Tab.Activated(图表)` → `_dirty["图表"]==False`）。
- [ ] `_auto_follow`：默认 True；`j/k` 选中 → False（pin，后续 `node_started` 不覆盖）；`a` → True。

### 6.4 图表（ChartPanel/Canvas）

- [ ] 同 `label+title` 两次 emit → 只 1 个图（替换不堆积，phase-9d §2.7）。
- [ ] 按 `label` 分组；组内聚焦 `j/k` 切焦点大图；多副图规整排列（单测：注入 3 label×3 title → 9 图按 label 分 3 组）。
- [ ] **完整 install**：line/bar/area/scatter/pareto → braille；`table` → DataTable；未知 `chart_type` → fail loud 提示（不静默崩）。
- [ ] **缺包模拟**（`monkeypatch.setitem(sys.modules,"plotext",None)`）：line/... 退 DataTable + 提示；`table` 仍可用；TUI 不崩。
- [ ] 残缺 payload（缺 `chart_type` / `data` 非 list）→ 静默跳过 + warning。
- [ ] workflow 级 chart（`node=None`）→ 归 `__workflow__` 桶，ChartBrowser 顶层可见；NodeDetail 图表 tab 不显示它。

### 6.5 ChartBrowser（C 全屏）

- [ ] `C` 进全屏；列出所有节点 + `__workflow__` 的图（按 node_key/label 树状，`__workflow__` 顶层）；选图大图预览。
- [ ] 数据源 = `NodeDetail.all_charts()`（不读 `_projection`）；`Esc/q` 退出。

### 6.6 回归与测试

- [ ] `tests/iface/cli/test_widgets.py`：迁移到 DagGraph/NodeDetail；新增 DagLayout（layout 纯函数 + LayoutIR + 两策略可替换）、ChartPanel（upsert/替换/分组/all_charts）、ChartCanvas（分派/降级/fail loud）、NodeDetail（6 kind 派发 + ● 徽标 + auto_follow）。
- [ ] `tests/iface/cli/test_app.py`：compose 产出新 widget；chart 分支分发（含 `node=None`→`__workflow__`）；解耦验收（§6.1）；`_selected_node`/`_auto_follow` 不写 tape。
- [ ] **既有 gate/interrupt 流程不回归**（phase-7/11 的 gate modal / interrupt / dialog 用例全过——这些走 `_dispatch_to_widgets` 既有分支，本 SPEC 不动）。
- [ ] headless SVG 截图：`demo_parallel` + 注入 chart 事件 → 主屏含拓扑图 + 图表（视觉存档）。
- [ ] **e2e（test-coverage-e2e）**：真 agent workflow，**opencode 后端**跑通；每面板按 SPEC 推送；`render_chart` 渲染；多副图规整（详见实施计划 + e2e 任务）。

---

## 7. 给并行后端 session / 后续阶段的契约

**给后端（agent-push / render_chart 生产者 / opencode profile）**：
- 产出 `custom(kind="chart")` 事件，`data.chart` 满足 `types.ts` 的 `ChartPayload`，事件顶层带 `node`（`None` = workflow 级）。**仅此**。TUI 不需任何其他适配、字段、信号。
- executor 用 claude 或 opencode 都行：TUI 流式 tab executor-agnostic（按收到的 `agent_*` 事件出 N 行）。
- 后端扩展 `custom` 其他 `kind`（`table`/`image`）→ 本 SPEC dispatch 只认 `kind=="chart"`，其余落日志；新 kind 需另开 SPEC，**互不阻塞**。

**给 Web（已实现）**：无契约变化。TUI 与 Web 读同一 `ChartPayload`，互不影响。

**给后续阶段**：
- replay UI（若 TUI 加时间旅行）：ChartPanel 投影是确定性 fold，套 `events[0..pos]` 切片即可。
- 新 `chart_type`：在 `ChartCanvas` 分派表加一行（OCP）。

---

## 8. 不做的事（边界）

- ❌ `render_chart` MCP 工具 / 任何生产者 / opencode profile 本身（并行 session 负责）。
- ❌ agent-push 后端、executor、CLIRunner 改动（零后端耦合）。
- ❌ 通用 DAG 布局器（只覆盖 Orca 结构化拓扑；超宽切 `CompactOutlineLayout`）。
- ❌ TUI 时间旅行 replay（phase-7 §3.4 边界不变；投影仅按确定性 fold 构造）。
- ❌ `radar` 图精细终端渲染（降级 DataTable）。
- ❌ `custom` 非 chart kind 的渲染（留后续）。
- ❌ foreach per-item 子 tab（D1-a：聚合进度 + 折叠 body 流）。
- ❌ 改 phase-9d SPEC 文本（D3-b：只在 phase-12 注其 stale，引用 `types.ts`）。

---

## 9. 关键决策备忘（防 drift）

1. **左图 = 拓扑图不是列表**（分层布局，scoped 到 Orca 拓扑，超宽切策略 B）。
2. **图表挂 NodeDetail 图表 tab**（节点产物，按 `event.node` 过滤，对齐 web NodeDetail）。
3. **chart 契约逐字复用 `types.ts`**（DRY；TUI 不定义 ChartPayload；phase-9d §2.2 文本 stale 不改）。
4. **plotext 主依赖**（装 `textual-plotext-ext`；完整 install 下必须 braille 渲染；缺包仅开发期 monkeypatch 测试降级）。
5. **`_selected_node` + `_auto_follow` = 临时 UI 交互态**（shells 草稿 §4.3 铁律 #2；不写 tape；auto-follow 默认开，j/k pin，a 恢复）。
6. **解耦验收 = 6 新文件零后端 import + 无生产者直 emit 即可渲染**（§6.1；app.py 既有 exec import 属 phase-11 不动）。
7. **NodeDetail 6 kind 永不空白**（治空白 bug；foreach 聚合进度+折叠不展开；wait/terminate 各有源）。
8. **一条读路径不变**（chart 投影是 `_dispatch_to_widgets` 的一个落点，确定性 fold，不新增订阅）。
9. **DagLayout = 可替换渲染器**（`LayoutIR` 契约 + Protocol；换布局不动 widget/dispatch；OCP；spike 即策略 A 不过换 B）。
10. **NodeDetail 高于 LogStream**（3fr/2fr，非均分；详情优先于日志）。
11. **workflow 级 chart → `__workflow__` 桶**（`node=None`；ChartBrowser 顶层；对齐 phase-9d §2.6 Output Panel）。
12. **流式 tab executor-agnostic**（N 事件→N 行；不假设 thinking/message 齐备；claude/opencode 都过）。

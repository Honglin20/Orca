# 实施计划 —— phase 12 CLI TUI 重设计

> **SPEC**：[`docs/specs/phase-12-cli-tui-redesign.md`](../specs/phase-12-cli-tui-redesign.md)（v2，对抗审闭环）。本计划**逐字实现 SPEC**，不自作主张加字段/改契约。每步标注对应的 SPEC § + 验收点。
> **铁律**：① 6 个新文件零后端 import（SPEC §0.3/§6.1）；② widget 壳无真相（只由 `_dispatch_to_widgets` 注入）；③ 确定性 fold / 幂等 / fail loud；④ 临时交互态（`_selected_node`/`_auto_follow`）不写 tape。
> **依赖**：新增 `textual-plotext-ext`（主依赖，SPEC §1.2）。

---

## 步骤总览（依赖序）

```
S0 依赖            →  S1 DagLayout(spike)  →  S2 DagGraph
                                              ↓
S3 ChartCanvas  →  S4 ChartPanel  →  S5 NodeDetail
                                              ↓
                   S6 app.py 接线（dispatch + CSS + 键位 + compose）
                                              ↓
                   S7 ChartBrowser  →  S8 清理+迁移测试  →  S9 自审+提交
                                              ↓
                   S10 e2e（opencode 后端，test-coverage-e2e；没好就等）
```

S1 是最大风险（spike）；先过 spike 硬断言再铺开 widget。

---

## S0 — 依赖与脚手架

- [ ] `pyproject.toml`：主依赖加 `textual-plotext-ext`（+ 其 `plotext` 依赖）。`pip install -e .` 验证 import OK。
- [ ] 建 6 个空文件骨架（`widgets/dag_layout.py`/`dag_graph.py`/`node_detail.py`/`chart_panel.py`/`chart_canvas.py`、`screens/chart_browser.py`），各写模块 docstring（指向 SPEC §）。
- **验收**：`python -c "import plotext; import textual_plotext"` 不报错。

---

## S1 — DagLayout（P0 spike，纯函数，全单测）— SPEC §1.1 §4.1 §6.2

文件：`orca/iface/cli/widgets/dag_layout.py`。**纯函数/数据类，不依赖 Textual widget**（可独立单测）。

- [ ] `NodeBox`/`Edge`/`LayoutIR` dataclass（SPEC §4.1 契约逐字）。
- [ ] `DagLayout` `Protocol`：`layout(topo, status, selected, cols_budget) -> LayoutIR`。
- [ ] `Topology` 派生函数 `build_topology(wf)`：从 `wf.nodes`/`wf.parallel`/`node.routes` 抽节点+边（`$end` 忽略；parallel `group→branch` 扇出 + `branch→group.routes.target` 汇聚）。
- [ ] **环检测**：`build_topology` 含环 → `raise CycleDetected(...)`（fail loud，不无限递归）。
- [ ] `LayeredDagLayout`：最长路径分层 + 同组 branches 同层 + 贪心排序 + 宽度治理（超 `cols_budget` → 缩写节点名 → 仍超置 `overflow=True` + 填 `fallback_outline`）。
- [ ] `CompactOutlineLayout`：备选策略（带边指示符的紧凑 outline），同 `DagLayout` 接口。
- [ ] **Spike 硬断言（SPEC §6.2，必过才继续）**：
  - 100 个 seeded 随机拓扑 `layout()` 不抛异常（`random.Random(seed)`，禁用 `Math.random`——用 seed）。
  - 结果 `layers` 含全部 node 名且每个恰一次。
  - 渲染宽度 ≤ `cols_budget`（超则 `overflow=True`，不崩）。
  - 边界：单节点 / `entry→$end` / foreach 单 box（body 不展开）/ 含环抛 `CycleDetected`。
- [ ] 若 `LayeredDagLayout` 过不了 spike → 切 `CompactOutlineLayout` 为默认，并在 SPEC §9 补 ADR（决策 9）。
- **验收**：`tests/iface/cli/test_dag_layout.py` 全绿；截图（`demo_linear`/`parallel`/`conditional`）存档（仅 sanity）。

---

## S2 — DagGraph widget — SPEC §4.1 §6.2

文件：`orca/iface/cli/widgets/dag_graph.py`。

- [ ] `DagGraph(Widget)`：持 `_topo`/`_status`/`_selected`；`build_from_workflow(node_names, parallel_groups, routes)` 派生 Topology；`set_status`/`set_group_progress`（**API 与 DagTree 对齐**，幂等）；`select(name)` 设 `_selected` + 调 `app._on_node_selected`。
- [ ] `render()`：委托 `_layout.layout(...)`；`ir.overflow` → 切 `_fallback`；`_render_ir(ir)` → Textual renderable（盒子/连边/状态色/选中高亮）。
- [ ] 交互：`on_click` + 聚焦 `j/k`（BINDINGS）→ `select()`。
- [ ] `DEFAULT_CSS`：`width:32; min-width:24; max-width:33%; border: round $primary;`（SPEC §3.2）。
- **验收**：`demo_parallel` 渲染拓扑图（headless SVG）；`set_status` 幂等；`j/k` 选中触发 NodeDetail 切换 + pin。

---

## S3 — ChartCanvas — SPEC §1.2 §6.4

文件：`orca/iface/cli/widgets/chart_canvas.py`。

- [ ] import 探测（模块级缓存 `_PLOTEXT_OK`）。
- [ ] `render_payload(payload)`：按 `chart_type` 分派——line/bar/area/scatter/pareto → plotext braille（`_PLOTEXT_OK` 时）；table → `DataTable`；radar → DataTable +「见 Web」；未知 → fail loud「未知 chart_type: X」。
- [ ] 缺包分支：`_PLOTEXT_OK=False` → line/... 退 DataTable + 提示。
- [ ] 残缺 payload 防御在 ChartPanel 层（此处假设 payload 已校验）。
- **验收**：7 种 chart_type 各渲染正确；完整 install 下 line 含 braille 字符；`monkeypatch` 缺包 → 降级不崩；未知 fail loud。

---

## S4 — ChartPanel — SPEC §2.2 §4.3 §6.4

文件：`orca/iface/cli/widgets/chart_panel.py`。

- [ ] `_projection: dict[node_key, dict[label, dict[title, ChartPayload]]]`（确定性 fold）。
- [ ] `upsert(node_key, payload)`：校验（缺 `chart_type`/`data` 非 list → 跳过 + `logger.warning`）；同 `label+title` **幂等替换**。
- [ ] `charts_for(node_key) -> dict[label, list[ChartPayload]]`（去重后，保持插入顺序）。
- [ ] `all_charts() -> Iterator[(node_key, dict[label, list[ChartPayload]])]`（**公共 API**，ChartBrowser 用）。
- [ ] 渲染：label 折叠组 + 焦点大图（聚焦 `j/k` 切焦点图）；空 →「暂无图表」。
- **验收**：同 label+title 两次 → 1 图；3 label×3 title → 9 图分 3 组规整；确定性 fold（清空→重放→一致）；残缺 payload 跳过+warning。

---

## S5 — NodeDetail — SPEC §1.3 §4.2 §6.3

文件：`orca/iface/cli/widgets/node_detail.py`。

- [ ] `NodeDetail(Widget)`：`TabbedContent`（流式/输出/图表）；持 `_selected`/`_kind`/`_dirty`/`_stream_lines`/`_output`/内部 `ChartPanel`。
- [ ] `set_node(name, kind)`：切换选中节点 → 重渲染（按 kind 派发数据源，SPEC §1.3 表）；`流式` tab 用该节点缓存行。
- [ ] `append_stream(node, line)`：`node==_selected` 才入流式 tab；`流式!=active` → 置 `●`。
- [ ] `set_output(node, output)`：`node==_selected` 才显示；`输出!=active` → 置 `●`。
- [ ] `upsert_chart(node_key, payload)`：转发内部 ChartPanel；`图表!=active` → 置 `●`；`图表(n)` 显 n。
- [ ] `on_tabs_tab_activated`（Textual `Tabs.TabActivated`）→ 清该 tab `●`（SPEC §6.3 确定性语义）。
- [ ] **6 kind 派发**（SPEC §1.3 表）：agent/script/set/foreach/wait/terminate 各有流式+输出源；agent_* executor-agnostic（N 事件→N 行）。
- [ ] 兼容别名：`set_active(name)`→`set_node`、`append_line(line)`→`append_stream`（减小 app.py diff）。
- **验收**：6 kind 各至少一 tab 非空；N agent_* → N 行；● 置位/`Tab.Activated` 清除；foreach 聚合+折叠不展开。

---

## S6 — app.py 接线（dispatch + CSS + 键位 + compose）— SPEC §3.2 §3.3 §1.4 §5

文件：`orca/iface/cli/app.py`（surgical 改动）。

- [ ] `__init__`：加 `_selected_node: str|None`、`_auto_follow: bool=True`、`_node_kinds: dict[str,str]`（from `wf.nodes`，SPEC §3.1）。
- [ ] `compose()`：yield `DagGraph()` / `NodeDetail()` / `LogStream()`（SPEC §3.2）；`OrcaApp.CSS` 替换为新布局规则（含 `NodeDetail 3fr`/`LogStream 2fr`/`DagGraph width:32`）。
- [ ] `_dispatch_to_widgets`：
  - 新增 `elif etype=="custom" and data.get("kind")=="chart"`：`node_key = node if node is not None else "__workflow__"`；`self.query_one(NodeDetail).upsert_chart(node_key, payload)`（SPEC §3.3）。payload 非 dict → return + warning。
  - `node_started`：`if self._auto_follow: self._selected_node=node`；调 `NodeDetail.set_node(node, _node_kinds.get(node))`（SPEC §1.4）。
  - `node_completed`：`NodeDetail.set_output(node, data.get("output"))`。
  - foreach/wait/terminate 事件 → `NodeDetail` 对应方法（按 §1.3 源）。
  - 把 `query_one(DagTree)`→`query_one(DagGraph)`、`query_one(ActiveNode)`→`query_one(NodeDetail)`（API 对齐，零逻辑改）。
  - **既有 gate/interrupt/终态分支不动**（§6.6 回归）。
- [ ] `_on_node_selected(name)`：`_selected_node=name; _auto_follow=False; NodeDetail.set_node(name, _node_kinds.get(name))`。
- [ ] `BINDINGS`：`Tab`→`focus_next`；`a`→`action_follow_active`（`_auto_follow=True; _selected_node=current_running`）；`c`→聚焦 NodeDetail + 切图表 tab；`C`→`push_screen(ChartBrowser)`；`/`→LogStream 过滤。既有 `q`/`d`/`i` 保留。`j/k` 由各 widget 自己 BINDINGS（focus-based）。
- **验收**：compose 产出新 widget；chart 分发（含 `node=None`→`__workflow__`）；`_selected_node`/`_auto_follow` 不写 tape（单测）；既有 gate/interrupt 用例不回归。

---

## S7 — ChartBrowser — SPEC §4.5 §6.5

文件：`orca/iface/cli/screens/chart_browser.py`。

- [ ] `ChartBrowser(ModalScreen)`：`compose` 树状导航（node_key/label，`__workflow__` 顶层）+ 大图预览（`ChartCanvas`）。
- [ ] 数据源：`app.query_one(NodeDetail).all_charts()`（**不读 `_projection`**）。
- [ ] `C` 进入，`Esc/q` 退出。
- **验收**：列出所有节点 + `__workflow__` 图；选图大图预览；`Esc/q` 退。

---

## S8 — 清理 + 测试迁移 — SPEC §6.6

- [ ] 删 `widgets/dag_tree.py`、`widgets/active_node.py`；更新 `widgets/__init__.py` 导出。
- [ ] `tests/iface/cli/test_widgets.py`：旧 DagTree/ActiveNode 用例 → 迁移到 DagGraph/NodeDetail（**重写非回归**，SPEC §6.3）；新增 DagLayout/ChartPanel/ChartCanvas/NodeDetail（6 kind + ● + auto_follow）单测。
- [ ] `tests/iface/cli/test_app.py`：compose 新 widget；chart 分发；解耦验收（§6.1 headless emit + 6 文件 grep）；`_selected_node`/`_auto_follow` 不写 tape。
- [ ] 新增 `tests/iface/cli/test_dag_layout.py`（S1 spike 断言）。
- [ ] headless SVG 截图脚本（`demo_parallel` + chart 事件）存档 `docs/` 或 test assets。
- **验收**：全量 `pytest tests/iface/cli/` 绿；`grep` 6 文件零后端 import。

---

## S9 — 自审 + 提交（clean-code-builder 内置）

- [ ] `code-reviewer` 审：依赖铁律（schema/run/exec/events/iface 单向）、壳无真相、DRY、fail loud、测试覆盖意图。
- [ ] 修全部 review 反馈。
- [ ] commit（信息含本计划链接 + `Co-Authored-By: Claude`）。
- [ ] release note `docs/releases/2026-07-03-phase12-tui-redesign.md` + CHANGELOG 索引 + CURRENT.md 更新。
- **验收**：reviewer 0 🔴；1031+ 既有测试 0 回归。

---

## S10 — e2e 验收（test-coverage-e2e，**opencode 后端**）— SPEC §6.6

> **硬约束（goal）**：必须用 **opencode 后端**跑真 agent workflow。opencode profile 由并行 session 开发中——**若未就绪则等待其完成再跑**（轮询 `orca executor list`/profile 可用性，不可用 claude 替代顶替最终验收）。

- [ ] 先探测 opencode 是否就绪（`orca executor list` 含 opencode 且 `test` 过）。**未就绪 → 轮询等待**（ScheduleWakeup 长间隔，不空转），就绪再继续。
- [ ] 准备一个会产出图表的 demo workflow（agent 节点调 `render_chart`，≥2 副 图、不同 label）。若 `render_chart` 生产者也未就绪 → 同样等待（它是 opencode 后端 session 的一部分）。
- [ ] `orca executor set opencode <cmd>`（或既定 profile）→ `orca run <demo>`（opencode 后端）。
- [ ] **逐项验收（SPEC §6）**：
  - 左 DagGraph：拓扑图正确、状态图标/颜色、`j/k` 选中驱动 NodeDetail、宽度 ≤1/3。
  - 右上 NodeDetail：流式（opencode 的 `agent_*`，无 thinking 也正确 N 行）、输出、图表 tab；6 kind 至少 demo 覆盖 agent。
  - 右下 LogStream：`/` 过滤、`j/k` 滚动；矮于 NodeDetail（3:2）。
  - 图表：`render_chart` 渲染（braille）；**多副图按 label 分组规整**；同 label+title 替换；`C` 全屏 ChartBrowser 含 `__workflow__`。
  - 键位：Tab/j/k/a/c/C/`/`/q/d/i 全符 SPEC §5。
  - 解耦：opencode 后端跑通即证明 TUI executor-agnostic（不依赖 claude 特定事件）。
- [ ] headless + 真跑截图存档。
- **验收**：opencode 后端真跑，每位置按 SPEC 推送，图表渲染+多图规整，全键位/功能/渲染符 SPEC。**未用 opencode 不算完成**。

---

## 风险与回退

| 风险 | 触发 | 回退 |
|---|---|---|
| `LayeredDagLayout` 连边绘制崩/丑 | S1 spike 硬断言不过 | 切 `CompactOutlineLayout` 为默认 + SPEC §9 ADR |
| plotext 渲染效果差/冲突 | S3 视觉不达标 | 该类型降级 DataTable（生产仍可用，不阻塞） |
| opencode 后端 / render_chart 生产者未就绪 | S10 探测不到 | **等待**（goal 硬约束）；不顶替、不绕过 |
| 6 kind 事件字段与实际 emit 不符 | S5/S6 联调失败 | 以 `replay_state`/既有事件源为准校正派发（不改契约，只补 kind 源） |

---

## 完成定义（DoD）

1. SPEC §6 全部验收点过（单测 + headless + e2e）。
2. 6 新文件零后端 import（grep 断言）。
3. 既有 gate/interrupt/dialog/executor 测试 0 回归。
4. e2e 用 **opencode 后端**真跑通过，图表渲染 + 多图规整 + 全键位符 SPEC。
5. release note + CHANGELOG + CURRENT 更新。

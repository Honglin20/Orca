# Release Note — 2026-08-11：huge 模式（超大 run）渲染路径修复

## 问题

远程真机复现：一个 nas-supernet run 跑到一半"突然"详情页 agent 信息全空白 + 图表页显示
**「773 个 chart 数据格式异常（后端 schema 漂移？）」**。

排查定位（非后端 schema 漂移）：run 越过 huge 阈值（`event_count > 50_000` 或
`byte_size > 5MB`，`orca/iface/web/run_manager.py:977`）→ 前端进入 **huge 模式**
（SPEC `web-attach-and-default-spec.md` §3 M3/M4）。huge 模式的前端渲染路径有 3 处没接对：

1. **agent 信息全空白**：huge 模式只拉最后 500 条事件（tail）。`workflow_started`
   （seq 1，携带 topology）不在 tail → `workflowDef = null` → `selectAgents` 拿不到拓扑。
   `AgentsRail` 又没把 `huge/serverOverview/hugeFullyLoaded` 传进 selector 用的最小 state
   （`AgentsRail.tsx:91`），导致 `selectAgents` 的 huge 分支（`selectors.ts:77`）永远走不到
   → 只能从 tail 的 node 事件反推 → 尾段全是 chart/usage 事件 → agents 列表几乎为空。
2. **773 个 chart 数据格式异常**：huge 模式图表来自 `/meta` 的 `serverOverview.charts`
   ——服务端 fold 出的**目录**，只有 `{label, title, chart_type}`，**故意不带 data**
   （`run_manager.py:2735-2741`，省内存）。前端 `selectCharts` 据此造占位 entry
   （`selectors.ts:500`），随后 `partitionCharts`（`ChartRenderer.tsx:49`）对每个 entry 校验
   `Array.isArray(p.data)` → **全部 reject** → 误报「N 个 chart 数据格式异常」。773 =
   目录里 chart 事件条数。tape 数据本身完好（写入端 `_validate.py` 落盘前强制 chart_type + data list）。
3. **无恢复通道**：store 定义了 `loadFull`/`loadEarlierChunk`（huge 模式恢复路径），但
   **没有任何组件调用** → huge 模式一进去就卡死，无法加载全量恢复显示。

## 修复（前端 4 处 + 测试）

- **`selectors.ts`**：`ChartEntry` 加 `placeholder?: boolean`；`selectCharts` huge 分支给
  目录占位 entry 打 `placeholder: true`（无 data，不是 schema 漂移）。
- **`ChartRenderer.tsx`**：`partitionCharts` 拆三桶 `valid / placeholders / rejected`——
  huge 目录占位**不 reject**（INV-5 仍只针对真实 payload）；huge 模式渲染
  「超大 run：图表仅显示目录（N 张）」banner + **「加载全部」按钮**（调 `store.loadFull`，
  拉全量回 client-fold 恢复真实图表）。
- **`ChartGroup.tsx`**：占位 entry 渲染为目录卡（chart_type + title + 「加载全部后显示」），
  不喂给 `LazyChartWidget`（后者要求 data 数组）。
- **`AgentsRail.tsx`**：订阅 `huge/serverOverview/hugeFullyLoaded` 并传入
  `selectAgentGroups` 的 state → `selectAgents` huge 分支生效，agents 栏渲染服务端 fold 的
  agent 清单（name + status），根治空白。

## 验证

- 前端 521 passed（30 文件，除 pre-existing topbar Router 失败）+ `tsc --noEmit` 干净。
- 新增 7 条测试：
  - `huge-mode.test.ts`：selectCharts huge 占位 entry 带 `placeholder:true`。
  - `chart-renderer.test.tsx`：huge 模式 → 无 schema-warning + 占位卡 + load-full 按钮、
    点「加载全部」→ 全量 client-fold → 占位消失真实 chart 渲染、占位不把 tail 内脏数据误计。
  - `agents-rail.test.tsx`：huge 模式从 serverOverview 渲染 agent 行（无 topology 不空白）、
    loadFull 后回退 client-fold。

## 遗留（另行决策，非本修复范围）

- **Conversation/Log 上滚增量懒加载**（`loadEarlierChunk` 接线）：huge 模式对话仍只显示
  tail 窗口，上滚到窗口顶不会拉更早历史。`loadFull` 可整体恢复。
- 后端 `overview.agents` 只含 `{name, status}`（SPEC 写的 `elapsed/tokens` 未派生）；
  前端 huge 模式 agent 行无 elapsed/tokens 展示。
- `TopBar` runId 复制在非 secure context（http 非 localhost）下 `navigator.clipboard` 为
  undefined → console.error（UI 不崩，pre-existing）。

# 2026-07-08 —— Web Shell v2 Chunk C：ChartsView 全渲染 + LogStream auto-scroll + TopBar/AgentsRail + useElapsedTick

按 [`web-shell-v2-spec.md`](../specs/web-shell-v2-spec.md) §5.1/§5.2/§5.4/§5.5/§5.6/§5.7 + §0 D5/D9 实现 Web Shell v2 前端 Chunk C。Chunk A（单 store fold + selectors + RAF streaming + WS resume）/ B（ConversationView 全渲染）已就位；本 chunk 把 6 个面板的「占位 / 部分实现」换成 SPEC 闭 AC 的完整渲染。

## 交付

### C1 ChartsView 全渲染（SPEC §5.4）
- **LazyChartWidget**（新 `src/components/chart/LazyChartWidget.tsx`）：IntersectionObserver + 300px skeleton，进入视口一次后 `disconnect` 永久挂载（防反复 measure 抖动）。
- **ChartGroup 重写**（`src/components/chart/ChartGroup.tsx`）：
  - 删除 `dedupeByLabelTitle`——**去重真相出口唯一在 `selectCharts`**（identity = `title || chart_type+seq`，upsert）。ChartGroup 不再二次去重，否则空 title 多 chart 会被压成最后一个（违反 identity 契约 + 铁律 1）。
  - 响应式 grid：`grid-template-columns: repeat(auto-fit, minmax(300px, 1fr))`。
- **ChartPayload 契约扩展**（`src/components/chart/types.ts`）：加 `size?: string`（气泡图）+ `series?: string`（备用扩展位，SPEC §5.4 列入契约但当前 7 widget 均用 hue 表达多系列；保留供未来双轴 chart）。
- **ScatterChartWidget 升级**（`src/components/chart/widgets/ScatterChartWidget.tsx`）：消费 `size` 字段 → ZAxis `dataKey="z"` + `range=[50,400]` 转气泡图（参考 AH BubbleChartWidget）；hue + size 组合支持；缺失 size 列值 → z=1 鲁棒回退。

### C2 LogStream auto-scroll 真实化（SPEC §5.5）
- 用 react-window v2 `useListRef` + `scrollToRow`（替换 Chunk A 的 hash anchor 占位）。
- pinned 状态机（最小可预测）：初始 pinned；wheel 上滚 unset；点「跳最新」按钮 re-pin。
- 删除原「onRowsRendered 自动恢复 pinned」设计——在事件少、全部可见场景下 stopIndex 总是末行，自动恢复会让 wheel 上滚立即被覆盖（HIG：predictable over magic）。
- live 标识 + 「跳最新 (N)」按钮（pendingJump 计数）。

### C3 TopBar 全功能（SPEC §5.1 + §0 D5）
- status icon 5 档（●/✓/✗/⊘/⏸）+ 颜色。
- D5 elapsed：
  - running → live wall-clock tick（`now - workflowStartedAt`）
  - completed → snap `workflow_completed.data.elapsed`
  - **failed/cancelled → snap `terminalTs - workflowStartedAt`**（SPEC §0 D5 字面只点名 completed；本 chunk 扩展到所有终态——避免 failed workflow 的 elapsed 显示 `—`；通过读 `state.events` 末条 `workflow_*` 事件 ts 推算，纯 tape 读不重派生）
  - idle → `—`
- cost：fold 全 `agent_usage.cost_usd`。

### C4 AgentsRail 全功能（SPEC §5.2 + §0 D2/D5/D9）
- 显示全部 topology 节点（含 pending）+ 运行态 node 覆盖 pending 占位（`selectAgents` 增强：先拓扑序 + 补 state.nodes 漏）。
- D5 per-agent elapsed：running live tick；completed snap `node_completed.data.elapsed`。
- D9 stall：running node 静默 > 5s（`WEB_STALL_THRESHOLD_MS`）→ 琥珀「思考中 Ns」/「💭」（最后事件是 `agent_thinking` → thinking=true）。
- token 小字（`formatTokens`：`1.5k/800`）+ foreach 进度。
- 点行切 `selectedNode`（D2 中栏切换）。DAG 按钮浮层 lazy 挂载（已有，C6 验证）。

### C5 Gate 模态（SPEC §5.6 + §0 D4）
- 现有 `GateDialog`/`PermissionGate`/`AskGate`/`ResolvedToast`/`post-gate-respond` 已完整覆盖 D4 中心模态 + 三通道竞速 + 不乐观更新 + `gate_response` POST。本 chunk 仅做验证（不重写）。
- 测试：`test/gate.test.tsx` 10 测覆盖：source 分派 / POST body / 不乐观更新 / resolved 自动关 + toast。

### C6 DAG overlay（SPEC §5.7）
- `AgentsRail` 内 `showDag` state + 点 DAG 按钮 → lazy 挂全屏浮层复用 `WorkflowGraph`（React Flow + dagre）。已就位，本 chunk 补测试（按钮点击 → 浮层挂、背景点击 → 关闭）。

### C7 useElapsedTick 单一共享 tick + stall（SPEC §0 D5/D9 / §6）
- **新 hook**（`src/hooks/use-elapsed-tick.ts`）：模块级 singleton + `useSyncExternalStore`（React 18 tearing-free）。
  - `useElapsedTickActive(active)`：页根控制启停；引用计数——N consumer active 只开 1 个 setInterval。
  - `useElapsedNow()`：消费者订阅；返回 Unix **秒**（与 `WebEvent.timestamp` 一致）。
- RunDetailPage 在页根 mount `useElapsedTickActive(status === "running")`——TopBar / AgentsRail 共用同一 timer（SPEC §5.2「N agent = 1 timer」）。
- `selectStall` selector：D9 5s 阈值；单位对齐（events ts 秒 → sinceMs 转毫秒与 `WEB_STALL_THRESHOLD_MS` 比较）。

### C8 测试（baseline 170 → 223，+53 新测）
- `test/elapsed-tick.test.tsx`（17）：singleton 引用计数 / `__testTick` 触发订阅刷新 / D5 snap 三态（completed/failed/cancelled）/ per-node snap / D9 阈值单位对齐 / React 集成共享 tick。
- `test/topbar.test.tsx`（12）：status 5 档 / D5 running live tick（数值递增断言）/ completed snap（tick 后不变）/ failed snap / cost 累计。
- `test/agents-rail.test.tsx`（10）：topology 节点显示 / 选中切换 / per-agent elapsed（running+completed）/ **单一 timer 断言**（AgentsRail 不自启 setInterval）/ token 小字 / DAG 浮层 lazy 挂 + 背景关闭。
- `test/log-stream.test.tsx`（7）：行数 == tape 事件数 / 每事件 readable 摘要 / wheel 上滚取消 pinned / jump-latest 显式恢复 / pinned 通道末行渲染。
- `test/chart.test.tsx`（+6 共 28）：ChartGroup 响应式 grid 断言 / IO stub lazy 挂载 / scatter size 字段（气泡图）/ hue+size 组合 / selectCharts D7 序无关（`selectCharts(T)==selectCharts(sort(T))==selectCharts(reverse(T))`）/ 空 title 多 chart 共存（identity 契约）。
- `test/setup.ts`：注入 IntersectionObserver stub（happy-dom 提供构造器但不触发 callback；stub 让所有元素立即 intersecting）。

## 闭环 review

`code-reviewer` 双 pass：
- **Pass 1**：1 BLOCKER（LogStream `onRowsRendered` 死代码）+ 4 MAJOR（ChartGroup 双重去重 / failed elapsed 丢失 / formatElapsed DRY / LogStream 测试假信心）+ 6 MINOR（注释漂移 / TICK_INTERVAL_MS 顺序 / 测试 helper prod 暴露 / ScatterChartWidget z fallback）全闭环。
- **Pass 2**（验证）：11 项 finding 全 CLOSED，1 项 NEW（log-stream 测试文件头注释漂移）已 opportunistic 修复。

## 关键设计决策（Rule 7 surface conflicts）

1. **ChartGroup 删 dedupe（vs 保留）**：选删——selectCharts 是铁律 1「唯一真相出口」；ChartGroup 二次去重引入空 title chart 压成 1 的契约违反。Surgical fix。
2. **failed/cancelled 也 snap（vs SPEC 字面只 completed）**：选扩展——SPEC §0 D5 字面只点名 completed，但 §5.1 TopBar AC 是 elapsed 语义；failed workflow 显示 `—` 是 UX 退化。snap 来源 = `state.events` 末条 `workflow_failed/cancelled` 事件 ts，**纯 tape 读不重派生**。
3. **LogStream 不自动恢复 pinned（vs onRowsRendered 自动恢复）**：选简化——在事件少、全部可见场景下 stopIndex 总是末行，自动恢复会让 wheel 上滚立即被覆盖；显式按钮更可预测（HIG：predictable over magic）。

## 验证

- 全前端测试：`13 file / 223 test 全绿`（baseline 170 + 53 新增）。
- 构建：`npm run build` 绿（仅 chunk size warning，已知遗留 Chunk B）。
- 铁律 AC：grep `replayPosition|formatLogLine|RunsSidebar|use-runs-list` 全仓 0 命中；Zustand `create()` 调用 ≤1。

## 遗留 follow-up（Chunk D）

- 🔵 image URL rewrite（D10）：markdown renderer 重写相对/`file://` 到 `/api/runs/<id>/assets/<hash>`
- 🔵 resume-fallback watchdog：D6 WS resume 失败 → 全量 re-fetch + re-fold + 丢弃 `_textBuf`（hook 已有 callback，缺真链路 e2e）
- 🔵 Playwright 逐屏 DOM 视觉断言（含折叠展开 / ▎ 消失 / chart 渲染 / gate 模态真浏览器）
- 🔵 bundle size 警告（~2MB）：可考虑 dynamic import / manualChunks

## Commit

`01af451`

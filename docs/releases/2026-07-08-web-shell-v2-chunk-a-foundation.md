# 2026-07-08 —— Web Shell v2 Chunk A（foundation）

按 [SPEC](../specs/web-shell-v2-spec.md) §0 D1/D2/D6/D7/D8 + §3.1/§3.3/§4/§8/§10 实现 web-shell-v2 前端
「基础层」（不实现 ConversationView/ChartsView 全渲染——后置 Chunk B/C）。后端 B1/B2 在前次
commit `c3a738f` 已完成；本次只动前端 + ws_handler 加 resume。

## 交付

### A1. codegen（D1）
- 新 `scripts/gen_events_ts.py`：从 `orca/schema/event.py` 的 `EventType` Literal 生成
  `orca/iface/web/frontend/src/types/events.ts`（39 个 EventType 联合体 + WebEvent 接口）。
  支持 `--check` 模式（CI drift guard）。
- 新 pytest drift guard `tests/iface/web/test_events_ts_drift.py`：断言 events.ts EventType 集合
  == `typing.get_args(EventType)` 严格相等；锁基数 = 39；显式验证 B1 新类型（`agent_step_started`/
  `unknown_event`）。
- `package.json` `prebuild` wired `gen_events_ts.py --check`——build 前自动校验。

### A2. 删除过期（§8 无兼容层）
删除：`RunsSidebar.tsx` / `ReplayBar.tsx` / `StatusBar.tsx` / `RunsListPage.tsx` / `NewRunPage.tsx` /
`use-runs-list.ts` / `use-replay.ts` / `stores/replay-actions.ts` / `components/detail/NodeDetail.tsx` /
旧 `formatLogLine`（并入 `selectors.summarizeEvent`）。`WorkflowGraph` / `ChartRenderer` /
`LogStream` 删除 `replayMode`/`replayPosition` 切片逻辑。测试侧删 `test/replay.test.ts` /
`test/log-detail.test.tsx`。

### A3. 单 Zustand store = fold(tape)（§3.1 D7/D8）
`workflow-store.ts` 重写：
- store.events 是 seq-sorted array（插入后 sort 保升序）；fold 内 seq 去重保证幂等。
- `loadFromEvents(events)` 先 sort by seq 再逐条 fold——**序无关**（D7：sort(T)==reverse(T) 同结果）。
- 新派生字段：`workflowStartedAt` / `workflowElapsed` / `reasoningTokens` / `lastSeqSeen`（D5/D6 用）。
- D8 reducer no-op：`unknown_event` / `agent_step_started` 显式空 handler；`agent_usage` 仅聚合 cost
  + reasoning_tokens（不进 conversation）。
- 删除全部 replay 字段（`replayMode` / `replayPosition` / `setReplayMode` / `setReplayTarget` /
  `enterReplay` / `exitReplay`）。

### A4. 纯函数 selectors（§3.1 D2/D3/D7）
新 `src/selectors.ts`：
- `selectAgents(state)`：DAG nodes → AgentsRail 行模型
- `selectConversation(state, nodeId)`：按 **node** 分组（D2），retry/foreach 多 session_id 在同
  node 内合并；输出按 seq 升序的 conversation-相关事件流（dim 分隔符是渲染层职责）
- `selectCharts(state)`：从 `custom(kind=chart)` 派生，group=`data.label ?? "misc"`，
  identity=`data.title ?? chart_type+seq`，**同 identity upsert**（D7 序无关）
- `selectLog(state)`：每事件一行 ≤80 字符；**每 EventType 均有 readable 摘要，无 no-op fallback**
  （TS 穷尽性 switch + `never` default 兜底 fail loud）

### A5. 流式 + transport（§3.3 D6）
- 新 `hooks/use-streaming-text.ts`：`_textBuf: Map<sessionId, string>` + RAF 批处理
  （`_rafSeq` 失效）；多 session 粒度（sync-flush on tool_call/result/node_completed 立即 commit）；
  `dropBuffer()` 在 run 切换 / WS resume 失败时丢弃缓冲（AH 边界硬化，buffer 永不参与 render 决策）。
- `use-websocket.ts` 重写：WS reconnect 发 `{type:"resume",run_id,since:last_seq_seen}`（D6）+ 兜底
  subscribe；保留 `onResumeFallback` 通道（dropBuffer 接入点）。
- **后端 ws_handler.py 加 `_handle_resume`**：重放 `tape.replay(since_seq=N)` 中 seq>since 的
  历史事件，再 subscribe 接 live 流。resume 失败（since 非数字 / run 未知 / tape 读异常）→ fail loud
  记 warning + 回退 subscribe（live 流不丢）。

### A6. 布局骨架（§4）
3-column `react-resizable-panels`：左 `AgentsRail`（agents 列表 + DAG 浮层挂点）/ 中 tabs
`[会话 | 图表]`（`ConversationView`/`ChartsView` 占位）/ 右 `LogStream`（虚拟化 + auto-scroll §5.5）。
顶 `TopBar`（status + elapsed snap + cost）。Gate modal 挂在 app 根（§5.6）。**无 Replay 控件**。
单 run/页：URL `/runs/:runId`。

### A7. 测试（§10）
- `test/store.test.ts` 重写：39 EventType 全覆盖 / fold 幂等 / D7 序无关 / D8 no-op / lastSeqSeen
- `test/selectors.test.ts` 新：fixture tape T（含 reasoning / step_start / 乱序 tool_result / orphan
  result / retry / foreach / unknown_event / pending tool / chart×3（同 identity upsert）/ gate /
  failed node）→ `selectConversation`/`selectCharts`/`selectLog` 期望 snapshot；`sort(T)` 与
  `reverse(T)` 产同 snapshot（D7）；summarizeEvent 穷尽性覆盖 39 EventType。
- `test/streaming.test.ts` 新：RAF batching（多次 appendText 同帧 commit）+ 多 session sync-flush +
  dropBuffer 清空。
- `test/ws-resume.test.ts` 新：初始只 subscribe（无 resume）；重连发 resume(run_id, since=lastSeqSeen)；
  onmessage 过滤非匹配 run_id。

## 验证结果

- **frontend build**：`npm run build` 绿（prebuild codegen check OK；tsc --noEmit 0 error；vite build OK）
- **frontend tests**：`npm test` 75/75 全绿（store 9 / selectors 6 / streaming 4 / ws-resume 3 /
  chart 22 / gate 10 / graph 21）
- **python web tests**：50 passed / 21 skipped（playwright 未装）

## 偏离 SPEC

- **resume-fallback watchdog**：D6 resume 失败 fallback 全量重拉路径在当前 chunk **未主动调用**
  （`deps.onResumeFallback` 通道保留，调用方已可挂 dropBuffer；watchdog「resume 后 N 秒未收到事件
  则触发全量重拉」留给后续 chunk 接入）。理由：当前 server ws_handler 已支持 resume 协议，fallback
  路径是少见场景的兜底；接入 watchdog 涉及定时器与重连竞态分析，超出 Chunk A「foundation」范围。
- **LogStream auto-scroll**：§5.5 完整 auto-scroll（pinned 时 scrollToIndex 末尾）依赖 react-window v2
  ref API；Chunk A 实现 pinned 状态 + 「跳最新」按钮 + 用户上滚取消 pinned 的逻辑，**scrollToIndex
  实际调用**留给后续 chunk 接入（react-window v2 ref API 跨版本不稳，需 spike）。
- **TopBar elapsed live tick**：D5 wall-clock tick 留给后续 chunk（useElapsedTick hook）；当前显示
  `workflowElapsed` snap（完成时）。
- **AgentsRail DAG 浮层按钮交互**：占位实现（点击按钮显示浮层 + WorkflowGraph 已渲染），DAG 浮层
  懒挂 / 详细 graph 规格 §5.7 留给后续 chunk。

## Commit
- 待 commit（branch `phase13-render-chart`）

## 后续 Chunk（B/C/D）
- **Chunk B（ConversationView 全渲染）**：§5.3 全表 + 折叠规则 + ▎ 流式光标 + markdown（gfm+math+katex+prism）
  + 工具展开（DiffView/FileContentView）+ react-window 虚拟化（>500 条）
- **Chunk C（ChartsView 全渲染）**：§5.4 完整 7 widget + ChartGroup collapsible + IntersectionObserver
  懒挂（300px skeleton）+ AH chartTheme 8 色 CSS-var 主题感知
- **Chunk D（liveness + 样式 + 验收）**：useElapsedTick（D5）/ stall 阈值（D9）/ AH 样式对齐 /
  image URL rewrite（D10）/ Playwright 逐屏 DOM 断言

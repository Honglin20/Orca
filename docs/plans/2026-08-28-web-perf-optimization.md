# Web 界面性能优化（传输层 / markdown chunk / 渲染路径 / 虚拟化）

> 状态：已批准计划 + Phase 1 评审环轮 1 修订（2026-08-28）。契约以 `docs/specs/2026-08-28-web-perf-optimization.md` 为准；本计划与 SPEC 同步修订，冲突时以 SPEC 为契约、按 IMPL_STATUS 上报。
> 环境事实（评审已锁定）：starlette 1.3.1（`file_response` 可覆盖）、uvicorn 0.51（不通告 pathsend）、react-window 2.2.7（`useDynamicRowHeight` 存在）、happy-dom 15（有 ResizeObserver 无布局）、`rehype-prism-plus/common` = 36 门。

## Context

用户报告 web 界面三症状：整体加载慢、workflow/run 查看页慢、markdown 渲染慢。调查（两级 Explore + 精读 + Plan agent 对抗审查 + spec-reviewer 评审环）确认根因，用户拍板：**huge-mode tail-first（#4）不做，其余全做**；AgentsRail 每事件重渲染显式遗留（UD-1）。

关键事实（实现时可直接引用）：
- 后端无 GZipMiddleware、无 Cache-Control（`orca/iface/web/server.py:89-138`）；hashed 资产只靠默认 ETag/304。`static/assets` 新旧 hash 共存已 5.5MB（`vite.config.ts:20` `emptyOutDir: false` 永不清理）。
- MarkdownText chunk **1018KB**：大头是 `rehype-prism-plus` **根入口**绑定 refractor/all（297 门语言）。根入口 `dist/index.es.js:1` 无条件 `import "refractor/all"`，且 refractor 声明 `sideEffects: ["lib/all.js","lib/common.js"]` → **打包器无法摇除**；瘦身唯一路径是子路径导入 `rehype-prism-plus/common`（36 门）。
- `main.tsx:5` 首屏同步 import `katex/dist/katex.min.css`（23KB CSS + 59 字体引用进 index chunk），违背 D5 split 意图。KaTeX woff/ttf **不裁剪**。
- **`use-streaming-text.ts` RAF 缓冲路径未接线**（`ingestEvent` 无生产调用点）——opencode 块级推送，无流式 re-parse。「markdown 慢」真实根因：每条 message 到达/首挂载时 ReactMarkdown 同步全管线 parse + 297 门语言注册成本 + 每条 WS 事件全树 reconcile（无 memo）。
- 全 store 订阅 3 处：`ConversationView.tsx:71`、`LogStream.tsx:71`、`ChartRenderer.tsx:88`；`WorkflowGraph.tsx:48` 订阅 s.events 做全量扫描。每条 WS 事件触发 O(N)×多 selector 重算。`RunDetailPage.tsx:60-68` **已是逐字段订阅**（无全订阅义务）。
- react-window v2.2.7 有官方 `useDynamicRowHeight`（ResizeObserver 自动测量，`key` 变化清缓存）；现用固定估高（message=160px 失真）；阈值 500 过高（≤500 全量渲染，且虚拟化分支**零测试覆盖**）。

不做：huge-mode tail-first（#4）；KaTeX 字体裁剪；WS 批量推送；logLines 进 store（会造成 store→selectors 运行时环，违反 `conversation-types.ts:7-11` 分层）；**AgentsRail 订阅收窄**（UD-1 显式遗留，需 stall 派生索引，独立后续任务）。

## 批次总览（每批独立可验证、独立 commit）

| 批 | 内容 |
|---|---|
| 1 | 传输层：gzip + Cache-Control + 产物清理 |
| 2 | markdown chunk 瘦身：`rehype-prism-plus/common` 子路径导入 + katex CSS 移位 |
| 3+4 | fold 派生扩展 + 细粒度订阅 + memo + 虚拟化（10 步顺序，见下） |

---

## 批 1：传输层（后端 + 构建脚本）

1. **GZipMiddleware**（`server.py` `create_app` 内，`install_auth_middleware` 附近）：`app.add_middleware(GZipMiddleware, minimum_size=1024)`。覆盖静态资产 + API JSON（events JSON 压缩比 ~85%）。
2. **hashed 资产 immutable 缓存**：StaticFiles 子类覆盖 `file_response`（starlette 1.3.1 可覆盖），加 `Cache-Control: public, max-age=31536000, immutable`；SPA fallback 的 `FileResponse(index.html)`（`server.py:129-138`）加 `Cache-Control: no-cache`。
3. **产物清理**：新脚本 `orca/iface/web/frontend/scripts/clean-assets.mjs`（`fs.rmSync(<static>/assets, {recursive:true, force:true})`）；`package.json` build 改 `tsc --noEmit && node scripts/clean-assets.mjs && vite build`（**clean 在 tsc 之后、vite build 之前**——tsc 拦截大部分失败前置）。git 侧旧 hash 产物由提交 `git add -A` 自然删除。
4. **后端测试**：新增静态 mount 用例（tests/iface/web 新建或 test_integration.py，**非** test_routes.py 的 D10 run 资产用例）断言 gzip / Cache-Control 头。前置（SPEC A4）：static/assets 非空 StaticFiles 挂载才成立——库内现有旧产物已满足，跑该测试前确认 assets 目录非空即可（clean 脚本只在 build 时触发）。

## 批 2：markdown chunk 瘦身

1. `MarkdownTextImpl.tsx:21`：`import rehypePrism from "rehype-prism-plus"` → `import rehypePrism from "rehype-prism-plus/common"`（**子路径默认导出**，36 门）。**禁止**根路径导入（含具名 `rehypePrismCommon`）——根入口无条件 `import "refractor/all"` + refractor sideEffects 声明，297 门无法摇除、chunk 不瘦身。`ignoreMissing: true` 保持；`tsx` 不在 common 36 门（原样渲染，fail-soft）。预期 chunk 1018KB → <700KB。
2. `main.tsx:5` 删 `import "katex/dist/katex.min.css"` → 移入 `MarkdownTextImpl.tsx` 顶部。
3. 验证：`npm run build` 对比 chunk 尺寸（判定式见 SPEC A3：单文件 <700KB + assets 总量前后对比 + build 日志清单对账）；首屏 index CSS 无 katex `@font-face`（grep `static/assets/index-*.css`）；`markdown-text.test.tsx` + `conversation-coverage.test.tsx` G6 全绿，并补 `.token.*` 级断言（仅 class 存在断言在 ignoreMissing 下 0 门也绿）。

---

## 批 3+4：fold 派生 + 细粒度订阅 + memo + 虚拟化

> Plan agent 对抗审查 + spec-reviewer 评审环已过（immer draft push 安全且 autoFreeze 冻结是防线、Set.add 在 immer 下可行、append-only 使引用复用成立）。cwd = `orca/iface/web/frontend`；类型检查靠 `npx tsc --noEmit`。

| 步 | 文件 | 内容 |
|---|---|---|
| 0 | `test/conversation-virtualized.test.tsx`（新）、`test/_helpers.ts` | **先建回归网**：虚拟化分支测试用 **>500 条事件**（现阈值 500 下即绿，步 9 降 100 后仍绿——回归网对步 1-8 恒有效；断言 `conv-vrow-0` testid 存在 + 首行内容；happy-dom 无布局，**禁断言测量行高**）；**120 条终态口径**（SPEC edge case）由步 9 阈值切换时补断言；**7 处**测试局部 `resetStore` 副本收敛到 `_helpers.resetStore`（`_helpers.ts:74`、`selectors.test.ts:46`、`conversation.test.tsx:41`、`conversation-coverage.test.tsx:40`、`node-output.test.tsx:36`、`ws-resume.test.ts:63`、`ws-resume-fallback.test.ts:59`）——**排除** `store-fail-loud.test.ts` 的 `resetStoreIdle`（它是 idle variant 非副本） |
| 1 | `src/conversation-types.ts` | 加 `conversationTargetNode(e)`（e.node 优先，workflow_failed 按 data.node），`eventMatchesNode` 改为薄封装——消除 filter 双命中 vs 索引单命中的语义洞；钉死测试：wf_failed 带 `node:"X", data.node:"Y"` 只归 X |
| 2 | `src/types/store-types.ts`、`src/stores/workflow-store.ts` | `NodeSessionIndex` 加单子对象 `ev: NodeEventIndex { all: WebEvent[], bySession: Record<string, WebEvent[]>, last: WebEvent \| null }`（注释标 readonly 禁 mutate；all seq 升序；bySession key 含 "main" 哨兵——null session_id 归 main）；`indexConversationEvent` 同步维护（refold 与增量两路径已共用）；新派生 `takenEdgeKeys: Set<string>` 走 `indexRouteEvent`——**同 nodesIndex 模式：不进 handler 表**，route_taken handler 保持 no-op；route_taken 索引接线是**独立** `event.type === "route_taken"` 判断（不在 CONVERSATION_TYPES，不得并入现有分支），语义与 `WorkflowGraph.tsx:76-79` 逐字符等价（`String(e.data?.from ?? "")` + `from && to` 守卫）；接线 refold / resetDerived / processEvent in-order 三处；初始 state 补字段；**同步两处手写字段枚举**：`_helpers.resetStore`（`test/_helpers.ts:75-104`）+ `store-fail-loud.test.ts:26` `resetStoreIdle`（漏同步跨测试泄漏且 vitest 照绿）；新建 `src/route-edge.ts` 导出 `routeEdgeKey(from,to)`。增注：ev 引入至步 10（C6）之间 dev 期 canary 序列化成本上升（fixture 小、prod 无 canary，可接受） |
| 3 | `test/agents-rail.test.tsx`、`test/_helpers.ts` | `makeNodeIndex` helper 替换 `agents-rail.test.tsx:460-466`、`:528-534` 两处字面量（否则 `tsc` 报错）；补测试：增量 vs refold 的 `ev.all` seq 序列等价、`ev.last`、route_taken 幂等（同事件两次 apply size 不变）、unloadRun 清空、**in-order route_taken → `selectTakenEdgeKeys` 命中**、**畸形 route_taken（缺 from/to / 非string）不入集合**；D7 等价断言对 Set 用内容比较（`toEqual`/`.size`），**禁 JSON 序列化**（Set → `{}` 恒等假绿） |
| 4 | `src/selectors.ts` | `selectConversation`/`selectStreamingCursor` 改走索引（**state 版签名不变**，内部委托 idx 变体；测试不组装假 state）；`selectConversation(null)` 保持 `{node:"", events:<空>}`；空结果返回模块级 `EMPTY_EVENTS = Object.freeze([])`（防每次新引用）；新增 `selectTakenEdgeKeys(state)`（「selector 唯一 view 输入」铁律）；改写 `:9-10`（输出不再"每次新建"）与 `:342-347`（已实现）注释 |
| 5 | `src/components/views/ConversationView.tsx`、`src/components/pages/RunDetailPage.tsx` | ConversationView 删全订阅 → 逐字段订 `s.nodesIndex[nodeId]` / `s.status` / `s.selectedSession` / `s.activeRunId`（**禁止** selector 返回新对象——zustand v5 会无限渲染；hook 全部置于 `if (!nodeId)` 之前）；`onChartClick` 改 `useCallback`（**memo BLOCKER**：内联箭头会让全部 EntryRenderer memo 失效）。RunDetailPage 无全订阅（`:60-68` 已逐字段），唯一义务 onChartClick useCallback（`:130`） |
| 6 | `src/components/conversation/reuse-entries.ts`（新）、`ConversationView.tsx` | `reuseEntries(prev, next, keyOf)`（按 entry.kind 全字段比较**含 `stepMarker`**（尾部 step-marker 会被后续 thinking/message 改写）；ToolPair 级 = `tool_call_id` + call/result **事件对象引用**（`pairToolEvents` 每次 build 重建 pair 对象，**禁以 pair 对象引用判等**）；**禁 JSON.stringify**；全复用返回 prev 引用）+ `memo(EntryRenderer)`；`entryKey` 从 ConversationView 导出复用；memo 放 EntryRenderer（VirtualizedRow 因 index/style 每帧变**不可 memo**）；单测：前缀不变尾部追加 / orphan / tool-group pair 增长 / **step→thinking 尾部改写（stepMarker undefined→有值）** / **pending→done result 后到** |
| 7 | `src/components/detail/LogStream.tsx`、`src/components/chart/ChartRenderer.tsx` | 订阅收窄：LogStream → `s.events` + `s.nodes`（selectLog 的 nodeElapsed resolver 读 nodes，只订 events 不够）；ChartRenderer → **`events` / `huge` / `serverOverview` / `hugeFullyLoaded` / `activeRunId` / `loadFull`**（漏 activeRunId → load-full 按钮静默失效）；selector 变体：`selectLog._from(events, nodes)` 双参、`selectCharts._from(events, huge, serverOverview, hugeFullyLoaded)` **四参**，state 版薄委托 |
| 8 | `src/components/graph/WorkflowGraph.tsx`、`graph-layout.ts` | 删 `s.events` 订阅 + takenEdgeKeys useMemo 扫描 → `selectTakenEdgeKeys`；`routeEdgeKey` 收敛 5 处内联 `\`${from}->${to}\`` |
| 9 | `ConversationView.tsx` | 虚拟化分支：`rowHeight` 函数 → `useDynamicRowHeight({ defaultRowHeight: 96, key: \`${activeRunId}:${nodeId}:${selectedSession}\` })` + `<List key>` 同 key 重挂（防滚动偏移残留）；hook 置于早 return 前；`VIRTUALIZATION_THRESHOLD` 500→100；`estimateRowHeight` 删除；步 0 虚拟化测试补 **120 条终态口径**断言（阈值 100 下 120 条必入虚拟化分支，`conv-vrow-0` 存在） |
| 10 | `src/stores/workflow-store.ts` | canary 修复（独立 commit；**①②③同一 commit 不拆步**）：① `snapshot()` 的 nodesIndex 改**投影复制值**（`sessions` 数组拷贝 + `sessionEventCounts`/`sessionFirstTs` 浅拷贝对象；必须复制值——现 snapshot 持引用 `:754-769` + 比较在二次 apply 后 `:771-774`，持引用检测力零增益；**禁止**共享引用剥离变体——会 mutate immer 冻结的 baseline；投影**不含 ev**）；② `__foldTwiceRun` 的 baseline clone（`:732`）对 nodesIndex 同样投影化（**复用①投影**，否则 ev 引入后每事件把事件集序列化两份）；③ 删 `__foldTwiceRun` 内两处 `indexConversationEvent`（`:736/:742`）。`store-fail-loud.test.ts` **无断言改动**——负例（node_started 双 apply → warn toBe(0)，`:470-481`）修复后依旧成立，**禁止**改成「会 warn」；验收动作 = 跑该文件确认 17 用例全绿 |

**顺序依据**：0 建回归网 → 1-4 等价重构（现有 selector 测试兜底）→ 5-8 消费方逐个切换 → 9 阈值（依赖 0 的覆盖）→ 10 独立收尾。

**关键语义红线**（审查结论）：
- autoFreeze 保持不关：selector 返回 store 内部冻结数组，冻结是防 mutate 的唯一防线（已验证 src/test 无事件就地 mutate）。
- `ev.bySession` / `takenEdgeKeys` 引用直返 = useMemo 零拷贝命中；每 node 自身事件仍触发自身 idx 引用变化（immer copy-on-write，兄弟 node 稳定——**仅 in-order 增量路径成立；refold 整体重建 nodesIndex 属预期**）——这是相对全订阅的核心收益。
- store.test.ts:28 grep 守门（`\bcreate<` 计 1）不受影响；`selectors.test.ts:223` 要求保留 `{ node, events }` 返回形状。

---

## 收尾（全批完成后）

1. `npx vitest run` + `npx tsc --noEmit` 全绿；`npm run build` 重建产物并提交（产物入库惯例，git log 003a98e）。
2. E2E 冒烟（SPEC A6，不设硬 KPI）：`tars serve`（端口 7428）+ 浏览器走：列表页 → run 详情（大 tape run：playground 真机 run 或新造 ≥1000 事件 run）→ charts → `/workflows` browse 点 `.md`；含**部署后刷新**；确认响应头 `Content-Encoding: gzip` / `Cache-Control: immutable`（gzip 断言打 ≥1024B 资产，index.html 686B 不压缩仅验 Cache-Control）+ 请求 `/` → `Cache-Control: no-cache`（SPEC A5）；后端 pytest 相关测试跑通；可选非阻塞观测：DevTools 首屏传输量前后对比。
3. 任务完成流程：release note（`docs/releases/`）→ CHANGELOG 索引 → CURRENT.md 更新。

## 验证命令备忘

```bash
cd orca/iface/web/frontend
npx vitest run test/<file>          # 单文件
npx tsc --noEmit                    # 类型（vitest 不查类型）
npm run build                       # 构建 + chunk 尺寸对比
curl -sI -H "Accept-Encoding: gzip" http://127.0.0.1:7428/assets/<index.js> | grep -iE "content-encoding|cache-control"
```

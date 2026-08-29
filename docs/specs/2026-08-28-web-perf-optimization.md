# SPEC — Web 界面性能优化（传输层 / markdown chunk / 渲染路径 / 虚拟化）

> 状态：Phase 1 评审环轮 1 修订后（SDD-LOOP，2026-08-28）；UD-1/UD-2 已由用户拍板
> 来源：用户报告三症状（整体加载慢 / workflow·run 查看页慢 / markdown 渲染慢），调查与方案对抗审查结论固化于此。
> 配套计划：`docs/plans/2026-08-28-web-perf-optimization.md`

## 1. 背景与根因（调查结论，作为契约依据）

- 后端无 GZipMiddleware、无 Cache-Control（`orca/iface/web/server.py`）；`static/assets` 新旧 hash 产物共存（`emptyOutDir: false` 永不清理）。
- MarkdownText chunk 1018KB：大头是 `rehype-prism-plus` **根入口**绑定的 refractor/all（297 门语言）——根入口 `dist/index.es.js:1` 无条件 `import "refractor/all"`，且 refractor 声明 `sideEffects: ["lib/all.js","lib/common.js"]`，打包器**无法摇除**。
- `main.tsx` 首屏同步 import `katex/dist/katex.min.css`（23KB CSS + 59 字体引用进 index chunk）。
- 全 store 订阅 3 处（`ConversationView.tsx:71` / `LogStream.tsx:71` / `ChartRenderer.tsx:88`）；`WorkflowGraph.tsx:48` 与 `AgentsRail.tsx:76` 订阅 `s.events` 做全量扫描（后者 UD-1 遗留）；每条 WS 事件触发 O(N)×多 selector 重算（selectConversation 全量 filter / selectStreamingCursor 倒序扫 / selectLog 全量 map / takenEdgeKeys 扫描）。
- 虚拟化行高固定估高（message=160px）失真；阈值 500 过高且虚拟化分支零测试覆盖。
- 明确**不做**：
  - huge-mode tail-first（用户拍板）；
  - KaTeX 字体 woff/ttf 裁剪（src 首选 woff2，无传输收益）；
  - WS 批量推送；
  - logLines 进 store（会引入 store→selectors 运行时环，违反分层）；
  - **AgentsRail 每事件重渲染遗留**（UD-1 用户拍板显式遗留）：`AgentsRail.tsx:76` 订阅 `s.events` + `:107` `selectAgentGroups` 无 useMemo，每事件仍重渲染。其已是逐字段订阅（与本次要修的 3 处全订阅性质不同），C3 加索引后其重渲染频率与现状相同、不会恶化；收窄需另做 stall 派生索引，属独立后续任务，本次不扩范围。

### 已知项（记录，不阻塞）

- katex CSS 移入 markdown chunk 后，首屏直连 `.md` 预览存在一次性 FOUC（CSS 随 chunk 异步加载）——单用户工具可接受。
- GZipMiddleware 不自动加 `Vary: Accept-Encoding`——单用户 127.0.0.1 无代理无实害；未来置于反代后需补。
- prism 高亮能力自 297 门收窄为 common 36 门——产品级已知变化。

## 2. 契约

### C1 传输层（后端）

- C1.1 所有响应经 `GZipMiddleware(minimum_size=1024)`：请求带 `Accept-Encoding: gzip` 且响应体 ≥1024 字节 → 响应必带 `Content-Encoding: gzip`；未带 Accept-Encoding 的客户端行为不变（明文）。
- C1.2 `/assets/*`（vite hashed 产物）响应头 `Cache-Control: public, max-age=31536000, immutable`。
- C1.3 SPA fallback 返回的 `index.html` 响应头 `Cache-Control: no-cache`。
- C1.4 API JSON 响应不新增自定义 Cache-Control（保持框架默认）。
- C1.5 `npm run build` 执行前清空 `static/assets` 目录（旧 hash 产物不残留）；clean 脚本在 `tsc --noEmit` 之后、`vite build` 之前执行。
- C1.6 `emptyOutDir` 保持 false（`static/.gitignore`/`.gitkeep` 必须存活——清理只针对 `assets/` 子目录）。

### C2 markdown 渲染管线（chunk 瘦身）

- C2.1 MarkdownText 的代码高亮经 `import rehypePrism from "rehype-prism-plus/common"` 引入（**子路径默认导出**，36 门）。**禁止**从根路径 `rehype-prism-plus` 导入（含具名 `rehypePrismCommon`）——根入口无条件 `import "refractor/all"` + refractor sideEffects 声明，打包器无法摇除 297 门，chunk 不瘦身。未收录语言（含 `tsx`，common 36 门不含）的 fenced code 原样渲染不报错（`ignoreMissing` 语义保持，fail-soft）。
- C2.2 katex CSS 不进首屏 index chunk；随 markdown chunk 加载；KaTeX 公式渲染行为不变（`.katex` 元素照常产出）。首屏直连 `.md` 预览的一次性 FOUC 记已知项（§1）。
- C2.3 尺寸验收：`static/assets/MarkdownText-*.js` 单文件 < 700KB（现状 1018KB），附本次 assets 总量前后对比；首屏 index CSS 不含 katex `@font-face`。
- C2.4 视觉契约：markdown 常用元素（p/ul/ol/h1-h3/code/pre/table/a/blockquote/img）不变；`language-python`/`language-ts` 渲染结果必须存在 `.token.*` 级元素（如 `.token.keyword`）——仅断言 `code.language-*` class 存在在 `ignoreMissing` 下 0 门也绿，不足为凭（现有 `markdown-text.test.tsx` 断言原样通过并补 token 级断言）。

### C3 fold 派生与索引（数据契约）

- C3.1 `NodeSessionIndex` 新增单子对象 `ev: NodeEventIndex`：
  - `all: WebEvent[]` —— 该 node 全部 conversation 事件引用，seq 升序；
  - `bySession: Record<string, WebEvent[]>` —— key 含 "main" 哨兵（null session_id 归 main）；
  - `last: WebEvent | null` —— 该 node 最后一条 conversation 事件。
  - **readonly 契约**：消费方禁止 mutate；数组内容被 immer autoFreeze 冻结，mutate 即 throw（fail loud 防线，**禁止用 setAutoFreeze(false) 换性能**）。
- C3.2 事件归属唯一化：新增 `conversationTargetNode(e)`——`e.node` 优先；否则 `type === "workflow_failed"` 且 `data.node` 为 string → `data.node`；其余 null。`eventMatchesNode(e, nodeId) := conversationTargetNode(e) === nodeId`。索引与 filter 同源，**有意收紧**旧 filter 对「e.node 与 data.node 同时存在」的双命中语义（后端契约保证 workflow_failed 顶层 node=null——`orchestrator.py:685` / `step.py:667-668` 两 emit 点均不传顶层 node，已实证；双字段是理论洞，钉死测试固化）。
- C3.3 `selectConversation` 语义等价重构：返回形状 `{ node, events }` 不变；输出与旧 filter 实现「同集合同序」；`"all"`/缺省 → `ev.all` 引用直返；`selectConversation(null)` 保持现状 `{ node: "", events: <空> }`；空结果（无索引 / session 无事件）返回**模块级冻结空数组** `EMPTY_EVENTS = Object.freeze([])`（引用稳定，禁每次新建）。
- C3.4 `selectStreamingCursor` 语义等价：`status === "running"` 且 `ev.last` 为 agent_message/agent_thinking。
- C3.5 新派生 `takenEdgeKeys: Set<string>`：
  - key 格式走统一 helper `routeEdgeKey(from, to)`（新模块 `src/route-edge.ts`，避免 store 反向依赖 graph 组件）；
  - **派生语义与 `WorkflowGraph.tsx:76-79` 现状逐字符等价**：`String(e.data?.from ?? "")` + `String(e.data?.to ?? "")` + `from && to` 守卫，缺任一不入集合；
  - `route_taken` 不在 CONVERSATION_TYPES 集合——索引接线必须是**独立** `event.type === "route_taken"` 判断（不得并入现有 conversation 分支）；`route_taken` handler **保持 no-op**（同 nodesIndex 模式：索引维护不进 handler 表 / FoldDraft）；
  - `refold` / `resetDerived` / processEvent in-order 三路径一致维护；refold 可从零重建（「store = fold(tape)」不破坏）。
- C3.6 新增 `selectTakenEdgeKeys(state)` 作为 WorkflowGraph 的唯一读入口（「selector 是唯一 view 输入」铁律）。
- C3.7 幂等红线：同事件只经 `processEvent` 应用一次（`seenSeqs` 挡重复）；增量 fold 与全量 refold 终态等价——D7 等价断言扩展覆盖 `ev.all` seq 序列、`ev.last`、`takenEdgeKeys` 三方一致；**Set 断言用内容比较（`toEqual` / `.size`），禁 JSON 序列化**（Set 序列化为 `{}` 恒等，假绿）。
- C3.8 单 store 铁律：不新增 store；`store.test.ts` 现有 grep 守门（`\bcreate<` 计 1）不破；新增顶层字段 `takenEdgeKeys` 须同步 `test/_helpers.ts` `resetStore` 与 `store-fail-loud.test.ts` `resetStoreIdle` 两处手写字段枚举（漏同步会跨测试泄漏且 vitest 照绿）。

### C4 渲染路径（行为契约）

- C4.1 ConversationView / LogStream / ChartRenderer 取消全 store 订阅，改逐字段订阅（**禁止** selector 返回新建对象——zustand v5 引用不等会无限渲染）；对用户可见的渲染输出与现状一致（现有渲染测试**不改断言**通过）。
- C4.2 `EntryRenderer` 加 `React.memo`：`entry`/`showCursor`/`onChartClick` 引用均不变 → 跳过重渲染；`onChartClick` 经 `useCallback` 稳定（消除内联箭头导致的 memo 全失效）；memo 放 EntryRenderer，`VirtualizedRow`（index/style 每帧变）不可 memo。
- C4.3 `reuseEntries(prev, next, keyOf)`：
  - 逐字段 = 按 `entry.kind` 全字段比较，**含 `stepMarker`**（尾部 step-marker 会被后续 thinking/message 改写，乱序补发可使既有 entry 的 stepMarker undefined→有值）；
  - ToolPair 级 = `tool_call_id` + `call`/`result` **事件对象引用**（`pairToolEvents` 每次 build 重建 pair 对象，**禁以 pair 对象引用判等**）；
  - **禁 JSON.stringify**；全复用返回 prev 数组引用；依赖 buildEntries 的 append-only 语义（已产出 entry 不回写）。
- C4.4 订阅收窄字段清单：
  - LogStream → `s.events` + `s.nodes`（selectLog 的 nodeElapsed resolver 读 nodes，只订 events 不够）；selector 加 `_from(events, nodes)` 双参变体，state 版薄委托；
  - ChartRenderer → `events` / `huge` / `serverOverview` / `hugeFullyLoaded` / `activeRunId` / `loadFull`（漏 `activeRunId` → load-full 按钮静默失效）；selector 加 `_from(events, huge, serverOverview, hugeFullyLoaded)` **四参**变体，state 版薄委托。
- C4.5 store→selectors 依赖方向不变（selectors 对 store 仅 type import）；本次不把任何视图派生（entries/logLines）搬进 store。

### C5 虚拟化

- C5.1 `VIRTUALIZATION_THRESHOLD` 500 → 100。
- C5.2 行高改 `useDynamicRowHeight({ defaultRowHeight: 96, key: "<activeRunId>:<nodeId>:<selectedSession>" })`；`<List key>` 同 key 重挂（防上下文切换滚动偏移残留）；hook 置于组件早 return 之前。
- C5.3 虚拟化分支测试**先于**阈值切换落地；测试断言 `conv-vrow-0` testid 存在（三分支共享 `conversation-view` testid，只断后者对阈值回归失明）；happy-dom 无布局 → ResizeObserver 不触发 → 行高恒 defaultRowHeight，**禁断言测量行高**。
- C5.4 ResizeObserver 环境：happy-dom 15 **提供** ResizeObserver；react-window 行测量 observer 有内建守卫、容器测量 hook 无守卫；测量不触发时回退 defaultRowHeight 96，不炸。

### C6 canary 修复（独立 commit；①②③同一 commit，不拆步）

- C6.1 `workflow-store.ts` canary 三处改动：
  - ① `snapshot()` 的 nodesIndex 改**投影复制值**：`sessions`（数组拷贝）+ `sessionEventCounts`/`sessionFirstTs`（浅拷贝对象）——必须复制值：现 snapshot 持引用（`:754-769`）且比较发生在二次 apply 之后（`:771-774`），原地 mutate 型漂移结构性不可见，持引用的投影检测力零增益；**禁止**共享引用剥离变体（会对 immer 冻结的 baseline state 产生 mutate）；投影**不含 ev** 派生（canary 不再触碰 ev——目的 = 杜绝 `ev.all` 事件引用数组进 JSON 双快照 + 防未来接线回退，非「修复检测缺陷」）；
  - ② `__foldTwiceRun` 的 baseline clone（`:732`）对 nodesIndex 同样投影化（**复用①投影**，否则 ev 引入后每事件把事件集序列化两份）；
  - ③ 删除 `__foldTwiceRun` 内两处 `indexConversationEvent`（`:736/:742`）——其非幂等性属已知契约，等价性由 D7 测试守护；不拆步原因：投影复制值先落而 index 调用未删的中间态会使负例红灯且无解释。
- C6.2 `store-fail-loud.test.ts` **无断言改动**——负例（node_started 双 apply → warn `toBe(0)`，`:470-481`）修复后依旧成立。**禁止**改成「会 warn」。验收动作 = 修复后跑该文件确认 17 用例全绿。

## 3. 验收标准（全部可客观验证）

| # | 标准 |
|---|---|
| A1 | `npx vitest run` 全绿（既有 32 测试文件 + 新增测试） |
| A2 | `npx tsc --noEmit` 零错误 |
| A3 | `npm run build` 成功；`static/assets/MarkdownText-*.js` 单文件 < 700KB（现状 1018KB）+ 本次 assets 总量前后对比；「无旧 hash 残留」判定 = assets 目录文件清单与 **vite build 日志输出的 chunk+css 文件清单**一致（不可用 index.html 引用集对账——异步 chunk 不进 index.html，实测 80 文件 vs 2 引用）；首屏 index CSS 无 katex @font-face |
| A4 | 后端 `iface/web` 相关 pytest 全绿（含新增 gzip / Cache-Control 头断言）。前置：static/assets 非空（已 build，保证 StaticFiles 挂载成立）；新断言落 tests/iface/web 静态 mount 用例（新建或 test_integration.py，非 test_routes.py 的 D10 run 资产用例） |
| A5 | HTTP 验证（在 A3 部署后执行）：`Accept-Encoding: gzip` 请求 `/assets/<≥1024B 的 js>` → `Content-Encoding: gzip` + `Cache-Control: ...immutable`（gzip 断言打 ≥1024B 资产——index.html 686B 必不压缩，仅验 Cache-Control）；请求 `/` → `Cache-Control: no-cache`。环境耦合注记：该保证依赖 ASGI server 不通告 pathsend（当前 uvicorn 0.51 不通告；starlette 1.3.1 对 pathsend 响应跳过 gzip） |
| A6 | 浏览器冒烟（tars serve :7428，**不设硬性 KPI**——UD-2 用户拍板；性能凭证由 A3 字节口径 + C1.1 gzip 头断言承载）：列表页 → run 详情（大 tape run，数据源 = 仓库上一级 playground 真机 run 或新造 ≥1000 事件 run）→ charts → `/workflows` 点 `.md` 全部正常渲染、无 console 报错；步骤含**部署后刷新**；可选非阻塞观测：DevTools 记录首屏传输量前后对比（不作为 pass/fail 依据） |

## 4. 失败路径

- 高亮未收录语言 → `ignoreMissing` 原样渲染，不 throw（fail-soft）。
- selector 返回数组被消费方 mutate → immer 冻结 throw（fail loud）。
- 虚拟化行未测量 → 回退 defaultRowHeight 96 + ResizeObserver 自动修正。
- 无 gzip 能力的客户端 → 中间件语义自动明文回退。
- selector 语义等价性破坏（输出集/序变化）→ `selectors.test.ts` / `conversation*.test.tsx` 红灯拦截（回归网）。
- **部署后旧 tab lazy-load 旧 hash chunk 404** → 无路由级 ErrorBoundary 时白屏；index.html no-cache 使刷新即恢复（新 index 引用新产物）——单用户工具显式接受。
- **build 失败发生在 clean 之后** → assets 空目录 + StaticFiles 挂载条件不成立（`server.py:120-121`），重启后全站 chunk 404；恢复路径 = 修复后重跑 build，或 `git checkout orca/iface/web/static/assets` 回到库内「旧产物 + 旧 index.html」一致组合（clean 在 tsc 之后，tsc 拦截大部分失败前置）。

## 5. 验收用例

- **happy path**：completed run 详情页打开 → 会话/图表渲染完整；`.md` 文件预览含公式与代码高亮（`.token.*` 级）；gzip/缓存头按 C1 命中。
- **sad path**：不存在的 runId → 现有 RunLoadError 路径不回归；未收录语言（含 `tsx`）fenced code → 原样文本。
- **edge case**：`workflow_failed` 带 `node:"X"+data.node:"Y"` → 只归 X；空 node 索引 → 冻结空数组引用稳定；120+ 条事件 → 虚拟化分支挂载（`conv-vrow-0` 存在）+ 滚动；同事件 processEvent 两次 → 派生不变（seenSeqs）；WS out-of-order → refold 重建与增量终态一致（D7 扩展：ev.all 序列 / ev.last / takenEdgeKeys 内容比较）；in-order route_taken → selectTakenEdgeKeys 命中；畸形 route_taken（缺 from/to / 非string）不入集合。

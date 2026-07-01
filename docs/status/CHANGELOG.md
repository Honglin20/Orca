# CHANGELOG —— 任务索引

> 每个任务完成后，在**顶部**加一条索引（1-2 句话 + commit SHA + release note 链接）。
> 最近的在上面。**不积累、不延后**——完成即记。

---

## 模板

```
## [日期] 阶段名 —— 一句话描述
- commit: <SHA>
- 详情：[release note](releases/<date>-<name>.md)
```

---

<!-- 新条目加在这里（本行下方）-->

## [2026-07-01] 阶段 10 iface/mcp 壳（外部 MCP 服务）—— 单进程多壳共存（MCP stdio + Web HTTP 共享 RunManager，gc 启动 assert 保护）+ HandleId 四件套工具（start_workflow / get_task_status / resolve_gate / cancel_task，每 tool 秒级返回规避 CC 60s 超时）+ tape-only query path（pending_gates_from_tape 纯函数派生 + RunManager.run_summary 合并，禁读 handler._pending/_gates_meta，反 AgentHarness 多真相源）+ source="mcp" 复用 handler.resolve（零新 resolve 路径，first-wins + broadcaster 与 Web 同款）+ workflow_cancelled 事件类型（cancel 写 tape 才是唯一真相）+ stdio 每消息 flush（FlushingStdoutWriter 兜底，规避 opencode #21516）+ stdin EOF 双行为（无 --with-web 随 CC 生灭 / 有 --with-web 转 daemon）+ orca mcp 命令（--with-web / --web-port / --max-concurrent / --idle-timeout / --runs-dir）；5 个 E2E 闭环（demo_linear 真 stdio round-trip / 合成 gate + source="mcp" 端到端 / MCP+Web first-wins + 广播写 tape / opencode flush 并发不丢 / 真 claude integration）+ 53 passed 2 skipped（tests/iface/mcp/）+ 652 passed 默认套件零回归 0 warnings；七铁律 grep 全过；6 个透明偏离（emit-before-cancel 顺序 / mcp<1.28 cryptography<49 构建地狱 / 慢 script 替 demo_linear 防 tape close race / 真 RunManager 替 mock 证 HandleId / daemon 60s tick 改 mock 单测 / 加 --runs-dir 测试隔离）；路径 A（CC agent + skill）明确不做留后续。Commit: `4860def`→`ca5ca4b`→`20472b1`→`c26307c`→`2cf5c66`。详见 [release note](../releases/2026-07-01-phase10-mcp.md)。

## [2026-07-01] phase 9 浏览器 E2E 修复 —— SPA fallback(深链 404) + live_server fixture + 测试 bug(run_id/WS/playwright API/async)
phase 9 前端浏览器实测可用但 playwright E2E 套件有测试代码 bug + 一个真实后端 bug：`server.py` 加 SPA fallback（catch-all GET → index.html，修深链 `/runs/<id>` 刷新返回 404 的生产 bug，注册在 API/WS 之后且仅 GET 不吞 `/api/*` `/ws` `/gate`）；4 个测试文件的 `live_server` fixture 端口轮询替代坏掉的 sleep；WS live 推送测试改慢 workflow（sleep 5）+ 三重断言（事件数/run_id 标签/真编排 type）确定性证明 pump 真推送；`test_new_run_form` 修错误的 `run-*` URL 模式为 `demo-*-*`（贴合 `gen_run_id` 真实格式）；`test_cyclic_layout_no_overlap` 修不存在的 `allBoundingBoxes()` → `evaluate_all` getBoundingClientRect；`test_playwright_9d.py` 6 个 async 测试改 sync `def` + `asyncio.run` + chart 测试导航到 RunDetailPage output tab（ChartRenderer 仅在 output tab 挂载，首页注入无组件消费）。验收：playwright E2E **20 passed**（3+6+5+6）、默认套件 599 passed 0 warnings、vitest 84 passed。Commit: `4f891e8`。详见 [release note](../releases/2026-07-01-phase9-browser-e2e-fix.md)。

## [2026-07-01] Tape 写句柄惰性打开 —— 消除 ~30 条 ResourceWarning（root-cause fix）
`orca/events/tape.py::Tape` 写句柄由 `__init__` eager-open 改为首次 `append()` 在 `async with self._lock` 内惰性打开（race-free）+ `close()` 对只读 Tape 幂等 + `__del__` leak 安全网；只读构造（replay/inspect）不再泄漏未关闭的 append handle。顺带修 `tests/gates/test_hook_bridge.py` 9 处 mock server 漏补 `server_close()`（不同根因、同属 ResourceWarning 卫生类、trivial）。验收：`-W "error::ResourceWarning"` 全绿（30→0）、RuntimeWarning 全绿、599 passed 零回归、vitest 84 passed。Commit: `f85bc48`。详见 [release note](../releases/2026-07-01-tape-lazy-open.md)。

## [2026-07-01] 阶段 9d iface/web gate 弹窗 + render_chart —— gate 富交互弹窗（两 source：tool_permission 4 按钮 / agent_ask radio|textarea）全读 store.gate（零本地 gate state）+ 走 backend POST /gate/respond（前端纯 forward 不决策）+ 不乐观更新（答后等 human_decision_resolved 才关，保唯一真相源）+ 三通道竞速广播（别壳先答 → store.gate=null + lastResolved → ResolvedToast「已被 [source] 答」）+ render_chart 迁移 AgentHarness 学术配色 chartTheme（PALETTE 8 色逐字）+ 扁平 record-array spec + 5 种 recharts widget（line/bar/scatter/pareto/table）+ chart 是事件（custom kind=chart 从 store.events filter 无独立通道）+ 同 label+title 替换（实时更新）+ replay 同步（chart ≤ replayPosition）+ hue pivot 共享 helper（DRY）+ ?debug=1 opt-in 调试入口（playwright 集成用，prod 默认不暴露）+ happy-dom 尺寸打桩（recharts ResponsiveContainer 渲染所需）；vitest 84 passed（gate 10 + chart 16 + 既有 58 零回归）+ build 成功 + 595 Python 全绿零回归 0 RuntimeWarning；review 全修复 3 建议（hue pivot 去重 / pareto 前沿线测试 / AskGate selected 重置）+ 1 可选；6 playwright integration。**phase 9 全部子阶段 9a/9b/9c/9d 完成，分支 phase9-web 可合并 master**。Commit: `6d0c5e1`。详见 [release note](../releases/2026-06-30-phase9d-web-gate-chart.md)。

## [2026-07-01] 阶段 9c iface/web DAG 可视化 + tape replay —— ReactFlow 12 + @dagrejs/dagre：拓扑进 workflow_started.data（tape 单一真相源，live+历史 replay 都从事件拿）+ findBackEdges DFS 三色识别回环边（反向喂 dagre，渲染保持原方向）+ 5 种 node widget（Agent/Script/Set/Foreach/End 共享 NodeShell，NODE_STATUS_HEX 5 色）+ WorkflowGraph 三 effect 增量（拓扑全量 build / 节点状态只改变化节点 data 未变保持引用 / route_taken 标记走过边）+ replay setReplayTarget 前进 apply / 后退 checkpoint restore（每 20 事件存 snapshot，enterReplay 建 -1 空态 checkpoint 消除全量重置分支）+ 单路径 fold（replay applyOne 复用 foldEvent 同一 handler 表，反双路径）+ live==replay byte-identical 断言（含 cost/gate/foreach 富流）+ react-window v2 虚拟日志（1000 事件 < 50 DOM row，session 分组）+ NodeDetail + ReplayBar（play/pause/速度 1×-20×）；后端 surgical：lifecycle.make_workflow_started 加 topology 摘要（非破坏）；vitest 58 passed（store 13 + graph 15 + replay 12 + hooks 9 + log-detail 9）+ build 成功 + 595 Python 全绿零回归 0 RuntimeWarning；review 全修复 3 Must-fix（progress 透传 / live==replay 富流断言 / checkpoint-1 消除全量重置）+ 5 Minor + Nit；5 playwright integration。**分支 phase9-web**。Commit: `adc856c`。详见 [release note](../releases/2026-06-30-phase9c-web-dag-replay.md)。

## [2026-07-01] 阶段 9b iface/web 前端骨架 —— React 19 + Vite 6 + TypeScript SPA：react-router v6 BrowserRouter（`/`·`/runs/new`·`/runs/:runId`，navigate push，后退 = 浏览器原生）+ Zustand 单 store（全 src 唯一 create()，immer middleware 锁不可变）+ eventHandlers 表覆盖全部 21 个 EventType（live/replay 共用 processEvent，seq 去重 + last-writer-wins 保证 fold 幂等）+ 懒加载（useRunsList 只轮询 /api/runs 元数据，useRunEvents mount 才拉 /events，unloadRun 清不累积）+ useWebSocket（按需 subscribe + run_id 过滤 + 指数退避重连，重连才全量重拉避免双拉竞态）+ 三页面骨架（RunDetailPage tab 占位 dag/log/output/yaml 给 9c/9d）；TS 类型逐字对齐后端 Event/RunMeta/RunStatus；vitest 22 passed（store 13 + hooks 9，含单 store 正则断言 + fold 幂等显式测试）+ build 到 static/ + 6 playwright integration（后退语义/懒加载网络/URL 直达）；review 全修复（immer / 单一加载路径 / WorkflowStatus 导出 / fail loud / cleanup callbacks / build 产物），n4 双轮询 deferred 9c；594 Python 全绿零回归 0 RuntimeWarning。**分支 phase9-web**。Commit: `0347a66`。详见 [release note](../releases/2026-06-30-phase9b-web-frontend-core.md)。

## [2026-07-01] 阶段 9a iface/web 后端 —— FastAPI（单进程同引擎 uvicorn）+ RunManager 真并发（asyncio.Semaphore 默认 3，每个 run 独立 bus+tape+gate_handler 隔离）+ 懒加载 REST（`/api/runs` 只元数据无 events，事件走 `/api/runs/<id>/events` tape.replay）+ WebSocket 单通道按需订阅（subscribe(run_id) 只推该 run，切 run cancel pump，反向 gate_response）+ 多 run gate 分发（session_id→registry→run_id→handle.gate_handler，复用 phase-6 共享 helper DRY）；五条铁律 grep 全过；review 全修复（shutdown 超时兜底 / EventBus.close 幂等 / has_pending 公开 / N+1 优化 / gate 路由 8 测试补齐）；37 web 单测全绿（0 RuntimeWarning 0 ResourceWarning），594 全量全绿（零回归）。**分支 phase9-web**。Commit: `b34c87d`。详见 [release note](../releases/2026-06-30-phase9a-web-backend.md)。

## [2026-07-01] 阶段 7 iface/cli CLI 壳 —— Textual TUI（DAG 进度 + 流式日志 + gate ModalScreen）+ typer 命令绑定（run/validate/list，parse_inputs 类型推断，退出码 0/1/2）+ OrcaApp @work 编排 worker + _GateHttpBridge（uvicorn 独立线程跑 hook 桥 /gate，socket 预 bind deterministic 就绪）+ GateModal 双 source 渲染（tool_permission/agent_ask）+ 广播输家哨兵；壳无业务真相（事件流驱动渲染）+ 依赖单向铁律（grep 验证）；fold 进 hook_script.py sys.path 阴影 surgical 修复（phase 6 hook 桥 9 测试由此转绿）；79 单测净增，557 全绿（零回归）。**里程碑：Orca 已是可用 CLI 工具**。Commit: `69a905e`。详见 [release note](../releases/2026-06-30-phase7-cli.md)。

## [2026-07-01] 阶段 6 gates/ HMIL 层 —— HumanGate 统一原语（tool_permission + agent_ask 共模型）+ HumanGateHandler（request/resolve + _broadcaster 广播协程）+ PreToolUse hook HTTP 桥（stdlib only，安全优先 exit 2 语义）+ /gate & /gate/respond FastAPI 端点 + SessionContextRegistry（claude session_id → run_id/node 映射）+ ask_user；session_id 透传 event 顶层；36 单元 + 4 integration 测试，478 全绿（+36 净增，零回归）
- commit: `2edcefc`
- 详情：[release note](../releases/2026-06-30-phase6-gates.md)

## [2026-07-01] 阶段 5-R follow-up —— 集合 bug 修复（补 `tests/__init__.py` 让 `tests.run` 可绝对导入，三个 run 测试文件原本 collection 失败）+ code-review 修复（foreach `max_concurrent<1` 编译期 fail loud / `resolve_max_iter` 非法值 fail loud 不静默降级 / 补 parallel+foreach continue_on_error 部分失败聚合透传下游的端到端测试）；442 测试全绿（+7 净增），零回归
- commit: `7bf0f97`
- 详情：[release note](../releases/2026-06-30-phase5-run.md)（§4.1 / §4.2）

## [2026-07-01] 阶段 5-R run/ 编排层 —— Orchestrator 单指针主循环（entry→…→$end）+ Router first-match-wins 纯函数 + ExecutorAdapter（executor AsyncIterator → bus.emit 拆四参桥接）+ parallel 组（asyncio.gather + 幂等 + failure_mode 三态）+ foreach（Semaphore + locals 注入 + 聚合）+ lifecycle（run_id / 生命周期事件 / max_iter）；扩展 RunContext 加 locals/task、ExecError 加 node 字段、validator 允许 inputs/parallel 组名作 Jinja2 root；9 demo 端到端（6 零 token + 3 agent）+ 439 测试全绿（353 基线 + 86 净增，零回归），5 条铁律全过
- commit: `6fa171b`
- 详情：[release note](../releases/2026-06-30-phase5-run.md)

## [2026-06-30] 阶段 5-M schema 单轨化迁移 —— 废除 `Node.after` 双轨制，统一为 routes 单指针 + `ParallelGroup` 显式并行（diamond）；validator 9 项重排（删 ③⑤ after 校验，加 ⑩ parallel 组结构 / ⑪ 兜底 route 位置 / ⑬ entry 非组）；3 examples + 9 fixtures + 3 测试文件全改 + 文档全覆盖；353 测试全绿（323 基线 + 30 净增，零回归），零 after 字段残留
- commit: `f0d7e99`
- 详情：[release note](../releases/2026-06-30-phase5-migration.md)

## [2026-06-30] 阶段 4 exec/ 执行内核 —— Executor 接口（AsyncIterator[Event]）+ ClaudeExecutor（claude -p 子进程 + 真 translator）+ ScriptExecutor / SetExecutor + CLIRunner（asyncio subprocess + stdin pump + 超时 SIGTERM→SIGKILL）+ Jinja2 渲染；3 条架构决策覆盖（translator 归 profiles / seq 占位 / result_extractor 拆半），322 测试全绿（196 基线 + 126 新增，零回归）
- commit: `c891f75`（feat(exec): phase 4 执行内核 — ClaudeExecutor + ScriptExecutor + SetExecutor + CLIRunner + translator 真实现）
- 详情：[release note](../releases/2026-06-30-phase4-exec.md)

## [2026-06-30] 阶段 3 events/ + profiles/ + capability 校验闭环 —— Tape 唯一真相源（append-only JSONL + Lock 覆盖 seq+write+flush + resume 清残行）+ EventBus（异步 fan-out + session_id 透传）+ 幂等 reducer + CliProfile/ProviderCapabilities 命令替换层 + compile `_check_profiles`（⑨），195 测试全绿（103 基线 + 92 新增，零回归）
- commit: `1b86019`（feat(events): phase 3 事件层 + profiles 命令替换层 + capability 校验闭环）
- 详情：[release note](../releases/2026-06-30-phase3-events-profiles.md)

## [2026-06-30] 阶段 2 compile/ 解析校验层 —— YAML→Workflow + 两层校验（结构 pydantic + 语义 8 项 + warnings），103 测试全绿
- commit: `5b5ba06`（feat(compile): phase 2 解析与校验层）
- 详情：[release note](../releases/2026-06-30-phase2-compile.md)

## [2026-06-29] 阶段 1 schema/ 数据层 —— 纯数据结构地基（workflow/event/state），50 测试全绿
- commit: `d69c47c`（实现）+ `6d7dfea`（二次 review 修复：SPEC 25→21 + 测试加固）
- 详情：[release note](../releases/2026-06-29-phase1-schema.md)


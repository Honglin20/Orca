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

## [2026-07-02] phase 11 P2.1 —— Semantic Output Validator（LLM 二次语义校验 agent output）
agent 产出后 spawn 第二个 claude -p 做 LLM 语义校验（非 shape/type），失败时 issues 作 guidance
反馈重 spawn，直到通过或预算用尽（fail-safe：validator 自身崩 → 当作 passed）。`validate_output`
纯函数不持 bus（Rule 7 化解铁律 2），三类 validator_* 事件由 orchestrator loop 统一 emit；validator
与 retry 独立预算（SPEC §11.6 deviation）。822 → 852 passed（+30，0 回归）。Commit: 6238dc9。
详见 [release note](releases/2026-07-02-phase11-validator.md)。

## [2026-07-02] phase 11 P3.1 —— Wait Node（asyncio.sleep 节点，Ctrl+G 可打断）
SPEC §9.7：新 `kind: wait` 节点（`asyncio.sleep(duration)`，`interruptible=True` 时可被 Ctrl+G 打断）。新增 `orca/exec/wait.py`（`WaitExecutor` + `parse_duration` + `WaitHandleRegistry` Protocol）+ `WaitNode` schema（加入 `AnnotatedNode` 判别联合，5 kind）+ `wait_started`/`wait_completed` 事件 + `EventBus.register_wait_handle`/`unregister_wait_handle`/`notify_all_waits`（SPEC §9.7.6 公开契约，`threading.Lock` 保护集合）+ `make_executor` 加 `bus` 参（仅 wait 分支透传）+ `InterruptHandler.resolve`/`record_resolved` 双路径调 `notify_all_waits`（Ctrl+G 立即打断正在 sleep 的 wait）+ `_PHASE_TO_ERROR_TYPE` 登记 `config`/`ConfigError` + LogStream 描述。**关键设计**：`WaitHandleRegistry` Protocol 化解「WaitExecutor 需 bus 访问」与「铁律 2 禁 exec 持 bus」的张力（ISP/DIP，能力裁剪到最小，executor 无法写 tape/emit，契约测试全绿）。SPEC §11.5 记 3 处偏离。**全量 822 passed / 1 skipped**（基线 784 + 38 新测试，0 回归）。Commit: `3921c89`。详见 [release note](../releases/2026-07-02-phase11-wait-node.md)。

## [2026-07-02] phase 11 P1.2 —— ask_user MCP 工具挂载（被编排 claude 主动问用户）
SPEC §5：Orca 进程内嵌 socket SSE MCP server（`AgentToolsMcpServer`，`mcp.server.fastmcp`），注册 `ask_user` 工具；被编排的 claude -p 经 `--mcp-config` 连上，调 ask_user 触发 `HumanGate(source=agent_ask)` → 等壳 resolve → 返回 answer。**SSE spike 双轮全 PASS**（in-memory ClientSession round-trip + real claude `-p --mcp-config` 连通性 + 工具调用）。确定性 tool-params 路由（D4：`orca_run_id`/`orca_node`，**不**依赖 MCP session 反查）+ spike 实证 claude -p 默认不给 MCP 工具授权（自动 append `--allowed-tools mcp__orca-agent-tools__ask_user`，SPEC §11.3）。register 债补完（B2）+ gates `RunContext`→`SessionLoc` 改名（B2）+ `unregister_run` 按 run 批清（SPEC §6）+ orchestrator `run()`/`run_from_state()` lazy start/stop server（start 失败 → workflow_failed fail loud）+ `_append_ask_user_instruction` 把路由参值拼进 prompt。**两轮 code-reviewer 全反馈闭环**（🔴 tape 配对断言 + unregister 接线 + start fail loud + 4 个测试 gap）。SPEC §11.2-§11.4 记 3 处偏离。**全量 773 passed / 1 skipped**（基线 753 + 20 新测试，0 回归）。Commit: `dcc3e63`。详见 [release note](../releases/2026-07-02-phase11-ask-user-mcp.md)。

## [2026-07-02] phase 11 P0.3 —— Retry Policy（节点级自动重试 transient claude 失败）
SPEC §9.5：agent node 声明 `RetryPolicy`（max_attempts/backoff/retry_on/jitter）→ transient 失败（spawn_error/timeout/api_error/http_429）自动重试，带 exponential/linear/constant backoff + ±20% jitter 防雪崩。新增 `orca/run/retry.py::execute_with_retry`（核心 loop：was_interrupted 短路 + retry_on 白名单过滤 + retry_started/succeeded/exhausted 事件可观测）+ `_compute_delay`（DRY 单点 delay 计算）+ `_classify_for_retry`（**error_type 对齐层**：桥接 ClaudeExecutor 的 `CliExitNonZero`/`ExecTimeout`/`ClaudeStreamError` 到 retry_on 的 `spawn_error`/`timeout`/`api_error`/`http_429` 语义短名，SPEC §9.5.2 对齐表）+ `RetryPolicy` schema（`Field(ge=1)` 下界校验）+ `ExecError.from_failed_data` classmethod（DRY：retry loop 与 execute_and_emit 共享）+ orchestrator `_dispatch` 集成（agent+retry 走 retry loop，否则既有路径）+ reducer retry_* no-op + LogStream 描述。validator（wave 3）将复用本 loop。**全量 753 passed / 1 skipped**（基线 726 + 27 新测试，0 回归）。Commit: `95cdae4`。详见 [release note](../releases/2026-07-02-phase11-retry-policy.md)。

## [2026-07-02] phase 11 —— `interrupt_resolved` 同步写 Tape 修复（wave-1 e2e 审计）
wave-1 e2e 审计发现 critical bug：CLI 单壳中断路径 abort/skip（continue 偶发）分支的 `interrupt_resolved` 被 async broadcaster 与 `run()` 的 `bus.close()` 竞态丢失（Tape 缺配对事件，违反单 Tape 唯一真相源）。Option A 修复：`record_resolved` 改同步 `await bus.emit` 写 Tape，async broadcaster 仅留给同步 `resolve()` 入口。6 个 xfail(strict=True) 全转 PASS + 新增 emit-on-closed-bus fail-loud 契约测试。全量 726 passed / 1 skipped / 0 xfailed，0 回归。Commit: `a3ae691`。详见 [release note](../releases/2026-07-02-phase11-interrupt-resolved-fix.md)。

## [2026-07-02] phase 11 P2.2 —— Checkpoint Resume（`orca resume` 崩溃续跑）
SPEC §7：Orca 的 Tape 天生是 checkpoint（append-only JSONL，无需 Conductor 的独立状态序列化系统）。新增 `orca run/resume.py`（typed exceptions + 纯辅助：中段损坏检测/outputs aggregate 重建/parallel mid-crash 检测）+ `Orchestrator.from_tape` classmethod + `run_from_state`（emit `workflow_resumed{from_tape,resumed_node,replayed_events}` 后续跑）+ `_drive_loop` 抽出 `_drive_from(start_node, initial_outputs)` 让 `run()`/`run_from_state()` 共享（DRY）+ `workflow_resumed` 事件类型 + reducer no-op 分类（interrupt_*/prompt_rendered/workflow_resumed）+ CLI `resume` 子命令（参数解析 + 6 种失败模式 → exit code，headless 不启动 TUI）+ LogStream 描述。**code-reviewer 全部反馈闭环**：`_bare_instance` 字段漂移安全网（`_DRIVE_REQUIRED_FIELDS` + `_assert_drive_fields_complete`）/ `_find_first_corrupt_line` position-aware（末尾残行不算 corrupt，from_tape 不依赖调用方先截断）/ fallback 分支测试 / 消除冗余 tape 读（单遍扫描返 valid_count）/ `_inputs_from_tape` 空 inputs warning / Event-schema 损坏测试。parallel 组中间崩溃不支持（SPEC §7 risk，exit 1）。**全量 712 passed / 1 skipped**（基线 697 + 15 新测试，0 回归）。Commit: `0d53eed`。详见 [release note](../releases/2026-07-02-phase11-checkpoint-resume.md)。

## [2026-07-02] phase 11 P1.1 Step B —— mid-run Guidance 注入 + SIGINT + review §2.1 critical 修复
SPEC §4 Step B：RunContext 加 `user_guidance`/`interrupt_history` + `with_guidance`/`guidance_prompt_section`（逐字对齐 Conductor `[User Guidance]` 段）+ render_prompt 拼 guidance section + orchestrator `_make_ctx` 注入累积 guidance（SPEC §10.3 C3：走既有 _make_ctx）+ CLIRunner.send_sigint/was_interrupted + ClaudeExecutor SIGINT 优先判定（emit node_failed{was_interrupted}，不 raise，SPEC §9.5.2 retry 短路前置）+ spawn 前 emit prompt_rendered（preview ≤200 字符，guidance 注入可观测，SPEC §10.2 item3 B5）。**code-reviewer 发现 critical 时序死锁（§2.1）**：Step A 的 action_interrupt「登记 pending + 立即 resolve」连调，但 handler.request 要等 node 边界才注册 future → resolve 落空 + workflow 卡死。修复：CLI 单壳路径 `request_interrupt(ireq, answer=)` + 新 `InterruptHandler.record_resolved`（emit requested + 入队 resolved，不经 await-future）；多壳 await-future 路径保留给 P3。SPEC §11.1 记此偏离。**全量 697 passed / 1 skipped**（Step A 后 674 + 23 新测试，0 回归）。Commit: `<TBD>`。详见 [release note](../releases/2026-07-02-phase11-guidance-injection.md)。

## [2026-07-01] phase 11 P1.1 Step A —— 优雅中断 UI（InterruptHandler + InterruptModal + Orchestrator wiring）
SPEC §3 Step A：抽出 `orca/gates/_broadcaster_mixin.py`（HumanGateHandler/InterruptHandler 共享 start/stop/_broadcaster，DRY）+ 新增 `InterruptHandler`（request/resolve/first-wins/跨线程 broadcaster emit `interrupt_resolved`）+ `InterruptRequest` 原语 + 3 个新事件类型（interrupt_requested/interrupt_resolved/prompt_rendered）+ `WorkflowAborted` 异常 + Orchestrator `request_interrupt`/`_handle_interrupt`/node 边界 pending 检查（可选注入，None 向后兼容）+ Textual `InterruptModal`（CONTINUE/SKIP/ABORT + guidance textarea + Esc=abort）+ OrcaApp Ctrl+G 绑定 + LogStream format_event。**全量 674 passed / 1 skipped**（基线 652 + 22 新测试，0 回归）。本 commit 同时合入先前未提交的 mxint 端到端实测 bugfix 基线（orchestrator default-fill / app.py on_mount kickoff / log_stream agent_usage / commands.py，见下条），因 Step A 的 `_drive_loop` 改造建立在 mxint default-fill 循环之上、同 hunk 不可分。Commit: `9db57f4`。详见 [release note](../releases/2026-07-01-phase11-interrupt-ui.md)。

## [2026-07-01] phase 11 P0.1 CI —— GitHub Actions 双 workflow（gate + opt-in integration）
新建 `.github/workflows/test.yml`（gate：push/PR(master) → matrix Python 3.10/3.11/3.12 → `uv run pytest -m "not integration"`）+ `.github/workflows/integration.yml`（opt-in：PR comment 含 `/integration` → guard 校验 PR-only + write 权限 + 非 fork PR + API key 非空 → 真 claude E2E）。基线 `uv run pytest tests/ -m "not integration"` = **652 passed / 1 skipped / 37 deselected** 绿。code-reviewer 0 critical，2 major + 2 minor + 2 nit 全闭环（trigger 改 contains / fork 拒绝 / API key fail-loud / timeout-minutes / 注释订正）。Commit: `120085f`。详见 [release note](../releases/2026-07-01-phase11-ci.md)。

## [2026-07-01] 端到端实测 `orca run` 修 3 个真实 bug —— CLI 跑不起来 / inputs.default 缺失 / agent_usage 显示简陋
迁移 AgentHarness 的 mxint-analysis（5 agent 链：analyzer→configurator→runner→diagnostic_saver→report_painter，保骨架换内容无 torch/bitx 依赖）做端到端实测，**首次 `orca run` 撞 3 个真实问题**，全部是 phase 7/5 的功能 gap 且单测零覆盖：(1) **架构 bug**：`commands._run_workflow` 在 `tui.run()` 前调 `kickoff()`，`@work` decorator 需 loop running，撞 `RuntimeError: no running event loop` —— 真实 `orca run` 完全跑不起来；测试 mock 回避故未发现。修：commands 不调 kickoff，挪到 `OrcaApp.on_mount` 末尾（与既有 `_consume_events` 同 pattern）。(2) **功能缺失**：yaml 声明的 `inputs.x.default` 从未被消费（除 `iterations` 特例），render 时 UndefinedError；schema/执行层契约断裂。修：`Orchestrator.__init__` 添加 default 填充循环 + required 缺失 fail loud。(3) **UX 改进**：LogStream `agent_usage` 仅显示字面值，未展示 token 数。修：`format_event` 加 agent_usage case 显示 `usage: in=.. out=.. cache=.. cost=$..`。**实跑验收**：209s 全绿 exit 0，5 个 agent 全部按要求完成结构化输出（schema 100% 匹配），落盘 adapter.py / results.json / diagnostic/*.json / REPORT.md(126 行) 齐全。**tape 完整性 8 项校验全过**：seq 连续无空洞 / 5 个 node 生命周期完整 / tool_call-result 30/30 完美配对 / agent_usage 在 node_completed 前 / workflow 闭环 / tape replay 还原 RunState 全部 5 个 output。**全量回归 683 passed / 0 failed**。反思：phase 7 CLI 壳虽写了 24 个测试但**真实 `orca run` 路径无端到端覆盖**，建议未来每 phase 完成至少跑一次真实 `orca run examples/<demo>.yaml` 作 acceptance 硬条件。Commit: `9db57f4`。详见 [release note](../releases/2026-07-01-e2e-mxint-bugfix.md)。

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


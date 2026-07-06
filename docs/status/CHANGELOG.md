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

## [2026-07-07] phase-10 MCP v4（9 工具 + setup/execute 分相 + Result 信封）
server.py 重写：6 旧工具（含 resolve_gate）→ 9 v4 工具（Discovery 4 + Lifecycle 3 + History 2）；setup/execute 分相（workflow.setup 字段 + compile validator execute phase 拦截 ask_user/gate + setup phase 结构约束）；三重杠杆防跳过 setup；Result 信封（kind 是 ErrorKind 值，无 layer）；新增 catalog / setup_phase / agent_catalog / tape_index 模块。Commit: df563f4。详见 [release note](releases/2026-07-07-phase-10-mcp-v4.md)。

## [2026-07-07] TUI v2 review remediation + 批 1 backend（Status.blocked + projections.py）
- 修 commit 5562e5e 回归（j/k hoist 后 down/up 无绑定，Enter 展开非末条 entry 失效）：
  App 级 BINDINGS 加 `down`/`up`（`priority=True` 覆盖 RichLog scroll）+ 3 pilot 测试。
- 批 1（ADR §4.3/§4.3.1）：Status Literal 加 `blocked`；`orca/run/projections.py` 单一
  派生算法源（node_status / node_usage / node_session_ids / node_iter），apply_event
  扩展 blocked fold（gate/interrupt 同源），TUI 删独立 fold 副本（`_node_session_ids` /
  `_per_node_last_usage_seq`）全部改调 projections（DRY）；`agents_list.py` 类型收紧
  Status + 删 `== "failed"` 字面量比较（P4）；AST 守门（`test_status_literal.py`）。
- 1596 passed / 0 回归（baseline 1558 + 38 新增）。
- commit: 见 `git log`（commit message 末尾含 Claude+Happy co-author）。
- 详情：[release note](releases/2026-07-07-tui-v2-review-batch1-projections.md)。

## [2026-07-07] phase-11-process-lifecycle —— 子进程生命周期管理（ProcessRegistry DI + 进程组 cancel + 退出码 5 档）
新增 `orca/exec/registry.py`（ProcessRegistry DI + 三段式 cancel SIGTERM→SIGKILL→cleanup + 平台分支 POSIX killpg/Windows CTRL_BREAK）+ `orca/iface/exit_codes.py`（ExitCode 5 档 0/1/2/3/130 + `exit_for_terminal_status` 纯函数派生）；runner.py / script.py 接入 `start_new_session=True` 进程组隔离（推翻 phase-3 §2.5 旧决策）+ registry.acquire/release；orchestrator.py 加 `shutdown()` 方法（不动 phase-11-error except 链）；run/__main__.py SIGTERM handler 只设 `threading.Event`（signal-safe，SPEC §1.3）+ 退出码经权威派生。code-reviewer 2 🔴 + 5 🟡 闭环（script.py 铁律 1+2 违规修复 / DI 闭环留 phase-12 follow-up / `_handle_timeout` 加 2s 超时防御 / singleton 测试复位 / asyncio.run+signal 交互注释 / script.py try/finally 覆盖 CancelledError）；test-coverage-e2e 真跑 5 项验证全过（退出码 0/1/2 / pgid==pid 证 start_new_session / shutdown 3 次幂等 / grep 守门 clean）。**1558 passed 0 回归**（baseline 1525 + 33 新增）。Commit：`cdc3469`。详见 [release note](releases/2026-07-07-phase-11-process-lifecycle.md)。

## [2026-07-07] TUI Redesign v2 —— 取消 DAG + agent 输出可见 + 切换 agent 看历史（三块布局重写）
TUI 三块布局重写：左 30% AgentsList + 右上 70% AgentHistory + 右下 30% LogStream。真删 v1.1.1 widget（DagGraph / dag_layout / _dag_render / activity_stream）+ display:none 双写兼容路径。用户核心需求闭环（last message 默认展开 + j/k 切换 + Log Stream 5 level icon）。
SPEC：[tui-redesign-v2-design-draft.md](../specs/tui-redesign-v2-design-draft.md) · release：[2026-07-07-tui-redesign-v2.md](../releases/2026-07-07-tui-redesign-v2.md) · commits：59021c9 + 5f9988c + e252653 + ab3b254 + 0e9e877 + 77f5685 + 85ecb61

## [2026-07-07] phase-11-error-handling —— 统一错误处理（ErrorKind 11 分类 + Result 信封 + classifier 双入口）
ExecError 字段集改 `{kind,message,phase,node,raw}`（kind 必填唯一分类轴）；新增 4 个 exec/ 层模块（`error_kinds.py` / `result.py` / `classifier.py` / `retry.py`）；`WorkflowAborted/MaxIter/RouteError` 改 ExecError 子类（固定 kind,phase），`WorkflowTerminated` 保留独立；error_type→kind 全量迁移（emit 写 kind + 读兼容期保留 error_type）；retry_started.data 扩展 layer/kind/reason/next_retry_at；编排 exception 子类化 + orchestrator except 顺序（WorkflowTerminated 先于 ExecError）。code-reviewer 3 个 🔴 + 8 个 🟡 闭环（wait.py 走标准 ExecError 路径 / `_classify_error` 用 ErrorKind.X.value / classifier profile 钩子加 warning log / DRY `_with_retryable` helper / 补 transport retry 测试）；test-coverage-e2e 真跑 demo_max_iter + opencode bad model 发现 2 处 emit defect（**Defect A**：orchestrator retry path 漏写 `next_retry_at` / **Defect B**：`layer` 与 `kind` 经两份派生表不一致）→ 已修 + 加 regression test。**1525 passed 0 回归**（baseline 1386 + 139 新增）。Commit：`451dd39`。详见 [release note](releases/2026-07-07-phase-11-error-handling.md)。

## [2026-07-04] TUI 重设计 v1.1.1 —— 真用户验证 4 GAP 收口（A/B/C/E）
修 test-coverage-e2e 真跑发现的 4 个 spec 违规：(1) **GAP-A** `app.py` agent_usage 同步投 `DagGraph.update_node_projection(tokens=...)`（DAG 行 3 由 `-- tok` 变实际数字，spec §4.4 acceptance）；(2) **GAP-B** Activity Stream 维护 `tool_call_id → (tool, args, call_ts)` cache，`agent_tool_result` 反查派生 tool/args（canonical Event result data 仅含 `{tool_call_id, result}`），summary 由 `?  {}` 变 `glob **/*.py` 等（spec §5.4「与 call 同 entry」语义）；(3) **GAP-C** elapsed 从 `call.timestamp + result.timestamp` 派生（顶层 Event 字段，spec §3），spec §5.4 订正为 `<N> lines · <elapsed>s`（exit_code 可选，canonical 不支持）；(4) **GAP-E** `DagGraph.build_from_workflow` 允许 self-loop（loop workflow `counter → counter` 重入语义），多节点环仍 fail loud。新增 8 测试 + 真 TUI 重放脚本（`_tui_gap_verify.py`），**1392 passed 0 回归**（baseline 1380 + 12 新断言），mxint tape 重放 5/5 节点 tokens 全非 None + 60/60 tool_result summary 含 tool name + meta 含 elapsed，demo_loop tape 重放 counter iter=3 与 node_started 次数一致。
Commit：`225933e`。详见 [release note](releases/2026-07-04-tui-redesign-v1-gaps-abce.md)。

## [2026-07-04] TUI 重设计 v1（spec v1.1 全 P0 闭环：3 行盒子 DAG + Activity Stream 双行 entry + EVENT_VISIBILITY 噪音治理 + 取消 NodeDetail + `f` 键 filter）
TUI 整体重设计对齐 spec v1.1（spec-review-adversarial conditional-pass → 5 P0 + 3 用户决策闭环）。新增 `_event_filter.EVENT_VISIBILITY`（7 tag 全 32 EventType 覆盖 + 完整性测试守门）+ `_dag_render` 独立渲染 helper（3 行盒子 + fan-in `(N inputs · M/N arrived)` 副标 + `after=None` 单独 section + ≥5 并行 fallback）+ `activity_stream` 双行 entry + 折叠详情（32 EventType per-type 字段级映射，复用 phase-15 `render_tool`/`render_message`/`render_thinking`）+ Header footer per-node usage（横向滚动 + running 优先）+ `f` 键 filter 模式（O1=c 取消 NodeDetail 但保留实例兼容）。reducer 派生 fold：iter 号 `node_session_ids`（重放产相同值，retry/skip/interrupt 不算新 iter）；fan_in arrived（dst 节点 node_completed 累加）。**单向依赖守住**（新模块零 orca.exec/run/events.bus 反向 import）。**1380 passed 0 回归**（baseline 1333 + 47 新测试），mxint 真跑 tape 重放 SVG 截屏（186 events → 152 进 Activity Stream，filter 掉 17 prompt_rendered + 17 agent_usage）。
Commit：`7bd43ef`。详见 [release note](releases/2026-07-04-tui-redesign-v1.md)。

## [2026-07-04] mxint_analysis 真实 bitx 量化分析迁移（替 stub + 5 agent prompts 真版）
将 `examples/mxint_analysis.yaml` + 5 个 agent prompts + `tests/e2e_mxint/` 从**简化 stub**（伪 SimpleNet + fake JSON，2 分钟跑完）迁移到**真实 bitx 量化分析**：target 换成 `ConfigurableMLP`（8970 params，sklearn digits 8x8，~90% eval_acc）+ 真调 bitx `Session` + 5 observers + `StudyReport.save` + `run_diagnostic_pipeline` 三阶段；2 个 driver script（`run_analysis.py` / `run_diagnostic.py`，后者含 bitx 1.1.1.dev395 `DistOverlayData.to_chart_data` bug 的进程内 monkey-patch）。**foreground 真跑 185s**（>2 分钟 stub baseline），5 张 chart（accuracy/bottleneck/sensitivity/qsnr_depth/recovery）真推 tape，76 行 REPORT.md 含真 QSNR 数据（51.37 dB avg，weight-dominated，recovery 31.7%）。**1333 passed 0 回归**。已知 follow-up：`_run_workflow_headless` 不起 chart ingestor，但 env 仍透传死 sock 路径（background 模式 chart 不通，prompt 让 agent 优雅 fallback）。
Commit：`838695f`。详见 [release note](releases/2026-07-04-mxint-real-bitx.md)。

## [2026-07-04] phase-15 render layer v1 —— e2e gaps 闭环（GAP#1 opencode read 文件 envelope + GAP#2 file_write subtitle）
修真跑发现的 2 个用户可见视觉异常：(1) opencode `read` **文件** result 同样是 XML envelope（与目录同形），原 `_normalize_file_read` 只检测 directory，file 走兜底 → envelope tag 泄漏 + opencode 自带 `N:` 前缀与 Rich Syntax 双重行号 + `(End of file)` marker 漏出；抽统一 `_parse_opencode_xml_envelope` helper（DRY），剥三层修饰（envelope 起手换行 + `N:` 前缀 + EOF marker）+ 仅 `<path>` 起手式才尝试 XML 解析（避免 claude Read 普通 HTML/XML 文件误判）+ fail visible（解析失败/未知 type/缺字段 → warning + 降级原文，§13）。(2) `_make_subtitle` 加 `file_write` 分支 → `new, NB`（spec §8.1）。spec §6.3 同步订正（原"opencode read 文件：同 claude"与实测不符）。**1333 passed** 0 回归（baseline 1327 + 6 新增）；真跑 tape seq=5 验证 72 行 TOML 干净渲染。Commit：`900fcfd`。详见 [release note](releases/2026-07-04-render-layer-v1-e2e-gaps.md)。

## [2026-07-04] phase-15 render layer v1（TUI 端）
实现 render-layer-design-draft §11.1 v1：在 canonical Event 之上加 iface 层纯函数渲染抽象（`normalize_tool` → RenderItem → `render_tool` → Rich renderable）。新增 `orca/schema/render_item.py` + `orca/iface/cli/widgets/tool_render/`（normalize/kinds/registry/reduce，单向依赖 only schema+rich+stdlib）+ `tests/e2e_phase15/_artifacts/render_tool_cases.json` 11 case fixtures + `tests/iface/cli/test_tool_render.py` 32 test（snapshot + fail loud + reducer + claude-code 对齐 acceptance §14.1）。迁移：log_stream 工具事件摘要共享 `describe_tool_event`（DRY，行为不变）；node_detail 流式 tab 工具事件升级为 Rich tool card（opencode read 目录现渲染为 17 条目树，不再 XML 一坨）+ thinking dim+italic 纯文本 + `t` 键切可见性（§12.8）。**1327 passed 0 回归**（baseline 1276）。Web 端 / shiki 流式 / 复制按钮 / codex 显式不做（v1 外）。
Commit：`ae0126b` + `edd738f`。详见 [release note](releases/2026-07-04-render-layer-v1.md)。

## [2026-07-03] examples 整理（固化 opencode 后端 + description + render_chart example + 全跑通 e2e）
13 agent example 固化 `executor: opencode` + `model: "deepseek/deepseek-v4-flash"`（with_ask_user 保留 claude——ask_user 需 mcp_tools=True）；补全 21 example description（TUI 信息明确）；`examples/README.md` 分类（纯 script / agent workflow / claude-only 例外）；新建 render_chart example（**文件夹化 agent** plotter + scripts/chart_demo.py 资源，演示 phase-14 `ORCA_AGENT_RESOURCES` + phase-13 chart 链路）；parallel_research 迁移 phase-14 `agent: <name>` 显式引用（消除旧约定 warn）。**验证**：8 script + 13 agent + render_chart 全跑通（opencode+deepseek-v4-flash **真跑不 mock**）；with_ask_user 例外（claude-only）。tests: test_examples_script + test_examples_opencode。
Commit：`c5c13b1`。详见 `examples/README.md`。

## [2026-07-03] phase 14 Agent 一等化（agent 池 + 文件夹化 + 统一解析层）+ Route 输出变换（批 1）
agent 从内嵌 prompt 升级为可命名/可复用/可携带资源的一等公民：新增 `orca/compile/agents.py` 统一解析层（`AgentResolver` Protocol + `LocalPoolResolver`，**删 `_load_prompts` + `_load_agent_md` 双加载债**）→ `AgentNode.agent` 显式引用 + 文件夹化（`<name>/agent.md` + 资源子目录）+ frontmatter 元数据 + `Route.output` 终点输出变换 + MCP `list_agents`/`get_agent`。**spec-review-adversarial 对抗审闭环**（2 P0 + 5 P1：warn 通道/skip end_route 统一/tools None 消歧/is_folder/frontmatter 精确算法/空串防御）。实现期修 SPEC 隐含缺陷（互斥预检须物化前）。**opencode+deepseek-v4-flash 真跑 e2e**：E2E-1 agent 引用（GREETER_OK）+ E2E-2 文件夹化 resources（`$ORCA_AGENT_RESOURCES` → SECRET_FLAG_42）。顺带修 executor capability guard（opencode + tools 不注 `--allowed-tools`）。**1276 passed 0 回归**。批 2（包分发 + workspace-instruction）留 phase-15。
Commit：`74d65b3`。详见 [release note](releases/2026-07-03-phase14-agent-first-class.md)。

## [2026-07-03] phase 13 script-side render_chart 接入（env 身份路由 + per-run Unix socket + 大数据三道关 + opencode+deepseek e2e）
让 claude/opencode/script 节点 spawn 的 script 子进程调 `orca.chart.render_chart` 推图：env 注入 4 个 ORCA_*（ClaudeExecutor + ScriptExecutor 都接，**executor-agnostic S5 闭环**）→ subprocess 链自然继承 → per-run Unix socket 传输 → tape 落 custom(chart) → 三壳零改动渲染。**对抗审闭环 16 处修订**（4 blocker + 9 major + 3 minor，含 ack timeout / sock 路径长度 / resume 边界 / opencode env 继承 / envelope 含义 / hue 分组降采样 / table 取前 N 等）。**大数据三道关**：自动降采样（max_points=2000，6 chart_type 各自策略）+ 2MB 硬上限 + ingestor 复核。**E2E-5 压测**：3 run × 10 chart 无丢失/串扰；**E2E-6 opencode+deepseek-v4-flash 真跑**：4 验证点（agent_message 完整性 / TUI 各面板合理 / render_chart 推送 / 图表排布）逐条通过；TUI snapshot 留档。**1224 passed 0 回归**（baseline 1208→1224，新增 16 测试）。S5 顺带修 2 实施 gap：ScriptExecutor 漏 chart env（违反 SPEC §11 #9）+ OrcaApp CLI shell 漏起 ingestor。
Commit：`1740a98`（S1-S4）+ `f260935`（S5 实施 gap 补丁）+ `b562a12`（S5 e2e）。详见 [release note](releases/2026-07-03-phase13-render-chart.md)。

## [2026-07-03] phase 12 CLI TUI 重设计（拓扑图 + NodeDetail + 终端图表 + opencode e2e）
重设计三面板：左 DagTree→DagGraph 拓扑图（分层+连边，max 33%）、右上 ActiveNode→NodeDetail（流式/输出/图表 tab，6 kind 永不空白）、新增终端图表渲染（plotext braille）+ ChartBrowser 全屏。6 新文件零后端 import、壳无真相、确定性 fold、`_selected_node`/`_auto_follow` 不写 tape（全有单测守护）。LayeredDagLayout spike 全过（未 fallback）。**S10 e2e：opencode 后端（glm-4.6v）真跑驱动 TUI 端到端通过**（SPEC §6 逐项 + 断言证据；图表渲染走解耦注入真路径——braille + 多图分组规整；`render_chart` 生产者未实现，待 phase-10）。e2e 顺带修真 bug：`ClaudeExecutor` 无条件注 `--allowed-tools`/`--mcp-config` → opencode spawn 失败，gate 到 `capabilities.mcp_tools` 修复。**1133 passed 0 回归**（基线 1082→1133，净增 51 测试）。
Commit: `38fd78c`（S0-S9）+ `cd6c1ee`（opencode spawn fix）+ `81d2f93`（S10 e2e）。详见 [release note](releases/2026-07-03-phase12-tui-redesign.md)。

## [2026-07-03] 后端统一抽象 + opencode 后端接入
把"后端怎么信号 done+result+usage+错误"下沉成 profile 字段 `TerminalContract`（`result_line` /
`events` 两模式）+ 共享 `RunAccumulator`，executor 保留一处小分支，runner 不动。加 opencode =
加 translator + profile 两文件（events 模式，prompt_channel=argv）。E2E 发现并修 runner 的
argv-channel stdin 不关闭导致 opencode 永久挂死的真实 bug。真实 orca CLI 双后端 E2E 跑通
（opencode glm-4.6v + claude/deepseek，均 completed）。688 passed 0 回归。
Commit: `f3129d1`。详见 [release note](releases/2026-07-03-opencode-backend.md)。

## [2026-07-02] orca executor —— 持久化后端二进制配置 + 健康检查
新增 `orca executor set/show/unset/list/test` 命令组：`~/.orca/config.json` 持久化 per-profile
binary override，`orca` 启动期 `os.environ.setdefault` 注入，复用既有 `resolve_cli_path()` 运行时
读 env——**exec/profile/registry 零核心改动**（OCP）。`pip install` 后 `orca executor set claude
"ccr code"` 一次设、全局生效；`executor test` 真起子进程自检协议兼容性（两层超时 + spawn 失败
fail loud）。顺带把 ccr profile 的 dummy translator 接上 `claude_translator`（ccr 协议兼容）。
config.py + executor_cmds.py（含纯函数 classify）+ 35 单测 + 9 e2e（假脚本走完整 spawn 链路，
不 mock CLIRunner）+ 2 integration。终审 0 🔴 1 🟡（已修）/ 2 🟢（跳过）。1031 passed 0 回归。
Commit: `ce559b6`。详见 [release note](releases/2026-07-02-executor-config.md)。

## [2026-07-02] agent 可观测性 + TUI 闪退 + 子进程泄漏修复（4 bug）
排查 demo_mixed 529 闪退时定位的 4 个 Orca 自身 bug：① OnResult 加 `api_error_status` 第 5 参
（全仓 11 处同步），executor `_result_diag()` 让 529 等 API 错误详情落到 `node_failed`（原只带空 stderr）；
② translator ApiRetry 对齐真实字段 `attempt`/`retry_delay_ms`/`error_status`（原读 `retry_count`/`wait_seconds`
永远 null，显示「第 ? 次」）；③ TUI 终态后停留 + notify 提示「按 q 退出」（原 `self.exit()` 闪退）；
④ `CLIRunner.stream()` finally terminate proc（原中途 q 强退留孤儿 claude）。7 新测试，985 passed 0 回归。
Commit: `f422d98`。详见 [release note](releases/2026-07-02-agent-observability-tui-fixes.md)。

## [2026-07-02] terminate step —— 新增 node kind `terminate`（业务级显式工作流终止节点）
新增第 6 个 node kind：触达即终止，`status=success` → `workflow_completed`（用 terminate.outputs），
`status=failed` → `workflow_failed{error_type=WorkflowTerminated, message=reason}`。补 `TerminateExecutor`
（仿 set_node 模板）+ factory 分派 + orchestrator 终态分发（新 `WorkflowTerminated` 异常 + `_finalize_terminated`
helper）+ compile 层 4 项 fail loud 校验（routes 空 / 非entry / 非parallel branch / 非foreach body）。
零 EventType/reducer 改动（复用既有 `node_completed`）；19 新测试，1013 passed 0 回归。
Commit: `41a5936`。详见 [release note](releases/2026-07-02-terminate-step.md)。

## [2026-07-02] phase 11 收官 —— CLI feature 补全全部完成（11 feature，652→959 测试，0 回归）
对抗评审（fail→conditional-pass，22 真问题闭环）→ 4 wave clean-code-builder + 4 wave test-coverage-e2e →
code-reviewer 横切审计（0 🔴 0 🟡）。交付 CI / Interrupt+Guidance / Resume / Retry / ask_user MCP /
Wait / Validator / Dialog / Skip / daemon 共 11 feature；e2e 审计狩猎并修复 2 个单 Tape 不变量
critical bug（interrupt_resolved 丢事件 / Ctrl+G 打不断 wait）；9 处 SPEC 偏离全部 Rule 7 裁定双落。
Budget（D3）/ attach（D2）descoped。commit: `120085f`→`d295922`（见各条）。
- 详情：[release note](releases/2026-07-02-phase11-complete.md)

## [2026-07-02] phase 11 P3.2 —— daemon `--background` 模式 + ps/logs/wait（attach descoped）
长跑 workflow 不占终端：`orca run --background` fork detached child（headless Orchestrator，
非 TUI——detached 无 TTY Textual 会崩，SPEC §11.9 裁定），父进程立即返回 run_id + pid；
配合 `ps`（dead pid 标 crashed，fail loud）/ `logs <id> [-f]` / `wait <id>` 三件套。
`daemonize` 5-callback seam 可测（CI 不留孤儿）；run_id 经 env 父子一致（metadata/tape/orchestrator
三处对齐，resume 可接）。code-reviewer 1 🔴（BaseException 漏 SIGTERM）+ 6 🟡 + 2 🟢 全修。
904→956（+52），0 回归。Commit: 见 git log。
- 详情：[release note](releases/2026-07-02-phase11-daemon.md)

## [2026-07-02] phase 11 P4 —— Skip to Agent（显式 skip 目标 + NodeSelectModal + §9.2 route 容错）
wave-1 SKIP 只能沿 route 跳，无兜底 route 时 NoRouteMatch 崩溃（SPEC §10.2 item12）。本 wave 补齐：
`request_interrupt` 加 `skip_target` 参数 → `_drive_loop` 直接跳该 node（不经 route 求值）；
`NodeSelectModal`（iface/cli/screens/）让用户选目标（pattern A：InterruptModal → app 推选择器）；
router §9.2 容错（skipped node 的 None output 让 when 求值失败走兜底，非崩溃）；`_validate_skip_target`
fail loud（ValueError，非 NoRouteMatch）；`interrupt_resolved.data.skip_target` 写 tape 可观测。
code-reviewer 1 🔴（验证顺序致脏 tape）+ 3 🟡 全修。888→904 零回归。Commit: 见 git log。
- 详情：[release note](releases/2026-07-02-phase11-skip-to-agent.md)

## [2026-07-02] phase 11 fix —— Ctrl+G 立即唤醒 sleeping wait node（wave-3 e2e 审计 bugfix）
wave-3 e2e 审计发现 SPEC §9.7.6 + §10.2 item9 承诺的「Ctrl+G 打断 wait node」实际不工作：
`notify_all_waits` 原本只在 node 边界 `_handle_interrupt` 触发，wait sleep 期间 drive_loop 阻塞
在 `_dispatch` 到不了边界 → 对 sleeping wait 是死代码。修复：`Orchestrator.request_interrupt`
登记 pending 的同时即时调 `bus.notify_all_waits()`（保留 record_resolved/resolve 里的同一调用
作 defense-in-depth）。xfail 复现测试翻转 pass + 8 新 wave-3 e2e 测试采纳，879→888 零回归。
- commit: 89b23ab
- 详情：[release note](releases/2026-07-02-phase11-wait-interrupt-fix.md)

## [2026-07-02] phase 11 P2.2 —— Dialog（agent 跑完后多轮追问，重 spawn claude 拼历史）
用户按 `d` 键就已完成 agent 的 output 多轮追问：`DialogHandler` 3-method split（start/send/end），
每轮重 spawn claude 把「output + 完整历史 + 本轮问题」拼进 prompt（`-p` 路线无 in-process
session，靠 prompt 拼历史）。Rule 7 裁定 3-method split（SPEC §6.2 单一 run_dialog 无法在轮间
交还 UI 控制）；`ctx.dialog_history` 是 web shell replay 预留位（真相在 tape）；抽
`orca/exec/env.py` 化解三处 `_build_env_overlay` 重复（Rule 6 DRY）。+27 测试断言 INTENT
（含历史累积核心契约 + send 失败 fail loud + 按钮复位），852→879 零回归。
- commit: caa3943
- 详情：[release note](releases/2026-07-02-phase11-dialog.md)

## [2026-07-02] phase 11 P2.1 —— Semantic Output Validator（LLM 二次语义校验 agent output）
agent 产出后 spawn 第二个 claude -p 做 LLM 语义校验（非 shape/type），失败时 issues 作 guidance
反馈重 spawn，直到通过或预算用尽（fail-safe：validator 自身崩 → 当作 passed）。`validate_output`
纯函数不持 bus（Rule 7 化解铁律 2），三类 validator_* 事件由 orchestrator loop 统一 emit；validator
与 retry 独立预算（SPEC §11.6 deviation）。822 → 852 passed（+30，0 回归）。Commit: e4eb07c。
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


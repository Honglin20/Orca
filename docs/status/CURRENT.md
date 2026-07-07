# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

> **接口整理前置（铁律）**：每个阶段实施前必须先做接口整理与规划——涉及接口改造的（schema / 事件 / 错误信封 / 能力声明 / 状态机 / 退出码 / widget API 等）**必须先把接口定义讨论清楚并写进 SPEC 或 ADR**，明确「真相源在哪一层、其他层只翻译不重新分类」，不允许实施期临时定义、不允许新旧接口并存。已识别的接口风险见各 phase 待办前置 ADR（如 phase-11 错误接口三层映射 / phase-12 capabilities 替换清单）。

---

## ✅ 侧任务完成（2026-07-08）：web-shell-v2 Chunk D（completion + polish + bundle split）

按 SPEC §0 D10/D6 / §5.3/§5.6/§5.7/§6/§7/§9 完成 Web Shell v2 前端**所有剩余项**——
前端实现**全部完成**，进入「ready for test-coverage-e2e 真链路验证」状态。

**交付**：
- **D1 Gate modal**：Chunk C 已完整接入；补 e2e 测试（requested → modal → answer → POST
  → resolved → close + toast 单测贯穿）。
- **D2 DAG overlay**：`AgentsRail` 用 `React.lazy` + `Suspense` 包装 `WorkflowGraph`；
  xyflow chunk（~217 KB）懒挂，首屏不下载。新增 lazy 解析测试。
- **D3 image URL rewrite**：backend `GET /api/runs/<id>/assets/<path>`（三重守卫：未知 run /
  path traversal / symlink / missing）+ 前端 `rewriteImageSrc`（相对 / `file://` / 裸文件名
  → endpoint；绝对 URL / data: / blob: 直通）。RunManager 新增 `runs_dir` property +
  `resolve_asset_path` 方法（SRP）。
- **D4 resume-fallback watchdog**：`use-websocket.ts` resume 后启 3s watchdog；超时无事件 →
  全量 re-fetch + re-fold + dropBuffer。**协议补丁**（review BLOCKER 闭环）：server
  `_handle_resume` 重放完毕发 `{type:"resume_ok", run_id, last_seq}` ack（仅 resume 协议真正
  执行时发）→ client 收到清 watchdog，消除 idle 误触发。
- **D5 bundle split**：`ConversationView`（~1MB markdown）/ `ChartsView`（~440KB recharts）/
  `WorkflowGraph`（~217KB xyflow）在 `RunDetailPage` 用 `React.lazy` 拆独立 chunk。
  **initial bundle 2,035 KB → 290 KB（gzip 93.65 KB，-86%）**。
- **D6 AH theme polish**：`chartTheme.ts` 8-color palette + 状态色 + 排版已就位（Chunk C）。
  **lucide 偏离**记录：选 unicode/emoji（零依赖 / 跨平台一致 / 现有达意），SPEC §7 是建议
  非强约束。
- **D7 StatusLine 修正**：Chunk B 单行不可折叠 → 可折叠（默认折叠 + chevron + 展开 JSON；
  `validator_failed` 默认展开）。DiffView 保留 index-diff（rationale 在文件头注释）。

**闭环 review**（`code-reviewer`）：1 BLOCKER（D4 idle 误触发）+ 3 MAJOR（AgentsRail 全 store
订阅 / MarkdownText 无渲染测试 / file:// 语义）+ 5 MINOR（注释措辞 / symlink 防御 / 测试名
强化 / dropBuffer 时序断言 / resume_ok 不发条件）全闭环。详见
[release note](../releases/2026-07-08-web-shell-v2-chunk-d-completion-polish.md)。

**验证**：249 npm tests（baseline 223 → +26 新增）+ 64 backend tests 双绿。`npm run build`
双绿，bundle split 实证（initial 290 KB / gzip 93.65 KB）。AC grep：禁用模式命中 0；
Zustand store 1 个；events.ts codegen 同步。

**遗留 follow-up（移交 test-coverage-e2e）**：
- 🔵 Playwright 真浏览器逐屏 DOM 视觉断言（折叠展开 / ▎ 消失 / chart 渲染 / gate 模态 /
  DAG 浮层 / image rewrite 真链路）。
- 🔵 agent 真链路写图片到 `<runs_dir>/<run_id>/assets/` 后引用相对路径（D3 端到端）。
- 🔵 ConversationView lazy chunk 1MB+ 仍偏大：未来 manualChunks 细分 katex/prism 独立 chunk。
- 🟡 lucide 图标（SPEC §7 建议）偏离已记录，择期 polish。

**前端实现 COMPLETE，ready for e2e。**

---

## ✅ 侧任务完成（2026-07-08）：web-shell-v2 Chunk C（ChartsView + LogStream + TopBar + AgentsRail + useElapsedTick）

按 SPEC §5.1/§5.2/§5.4/§5.5/§5.6/§5.7 + §0 D5/D9 实现 Web Shell v2 前端 Chunk C
（Chunk A 单 store fold + selectors + RAF streaming + WS resume / B ConversationView
全渲染 已就位）。

**交付**：
- **C1 ChartsView 全渲染（§5.4）**：新 `LazyChartWidget`（IntersectionObserver +
  300px skeleton + 进入视口一次后 disconnect 永久挂载）；ChartGroup 重写 删
  `dedupeByLabelTitle`（selectCharts 是唯一去重真相出口，铁律 1）+ 响应式 grid
  `repeat(auto-fit, minmax(300px, 1fr))`；ChartPayload 加 `size`/`series` 字段；
  ScatterChartWidget 升级消费 `size` → ZAxis 气泡图（参考 AH BubbleChartWidget）。
- **C2 LogStream auto-scroll 真实化（§5.5）**：react-window v2 `useListRef` +
  `scrollToRow`（替换 hash anchor 占位）；pinned 状态机简化（初始 pinned / wheel 上滚
  unset / 点跳最新 re-pin）；删「onRowsRendered 自动恢复 pinned」——事件少全可见时
  stopIndex 总是末行会让 wheel 上滚立即被覆盖（HIG：predictable over magic）。
- **C3 TopBar 全功能（§5.1 + D5）**：status 5 档 + D5 elapsed（running tick / completed
  snap / **failed+cancelled 也 snap**——读 tape 末条 workflow_* 事件 ts 推算，纯 tape 读）+
  cost（agent_usage fold）。
- **C4 AgentsRail 全功能（§5.2 + D2/D5/D9）**：显示 topology 全节点（含 pending）+
  per-agent elapsed（running tick / completed snap）+ D9 stall（>5s 琥珀「思考中 Ns」/
  💭）+ token 小字 + 选中切 D2。
- **C5 Gate（§5.6）**：现有 GateDialog/PermissionGate/AskGate/ResolvedToast 已完整覆盖
  D4 + 三通道 + 不乐观更新 + gate_response POST；本 chunk 验证不重写。
- **C6 DAG overlay（§5.7）**：现有 AgentsRail `showDag` + WorkflowGraph 浮层已就位；
  补 lazy 挂 + 背景关闭测试。
- **C7 useElapsedTick（§0 D5/D9/§6）**：新 hook `src/hooks/use-elapsed-tick.ts`——
  模块级 singleton + useSyncExternalStore（React 18 tearing-free）；`useElapsedTickActive`
  在页根引用计数（N consumer active 只开 1 setInterval）；`useElapsedNow` 消费者订阅，
  返回 Unix 秒（与 WebEvent.timestamp 一致）。RunDetailPage mount `useElapsedTickActive(
  status === "running")`。selectStall 单位对齐（events ts 秒 → sinceMs 毫秒 与
  `WEB_STALL_THRESHOLD_MS` 比较）。

**闭环 review**（`code-reviewer` 双 pass）：1 BLOCKER（LogStream onRowsRendered 死代码）+
4 MAJOR（ChartGroup 双重去重 / failed elapsed 丢失 / formatElapsed DRY / LogStream 测试
假信心）+ 6 MINOR（注释漂移 / TICK_INTERVAL_MS 顺序 / 测试 helper prod 暴露 /
ScatterChartWidget z fallback）+ 1 NEW（log-stream 文件头注释漂移）全闭环。

**验证**：53 新测（baseline 170 → **223 passed**，13 test file）。`npm run build` +
`npm test` 双绿。详见 [release note](../releases/2026-07-08-web-shell-v2-chunk-c-charts-log-tb-rail-tick.md) + [SPEC §5.1/§5.2/§5.4/§5.5/§5.6/§5.7](../specs/web-shell-v2-spec.md)。
**Commit**：`01af451`。

**遗留 follow-up（Chunk D）**：
- 🔵 Chunk D（liveness + 样式 + 验收）：image URL rewrite（D10）/ resume-fallback
  watchdog / Playwright 逐屏 DOM 视觉断言（含折叠展开 / ▎ 消失 / chart 渲染 / gate
  模态真浏览器）
- 🟡 bundle size 警告（~2MB）：react-markdown 全家桶；可考虑 dynamic import / manualChunks
- 🟡 StatusLine 折叠偏离 SPEC（Chunk B 遗留，单行无 payload，YAGNI；择期与 SPEC 作者对齐）
- 🟡 DiffView 用逐行 index diff（Chunk B 遗留，非 LCS）：编辑噪声大；可换 `diff` npm 包

---

## ✅ 侧任务完成（2026-07-08）：web-shell-v2 Chunk B（ConversationView 全渲染）

---

## ✅ 侧任务完成（2026-07-08）：in-session shell v8.1 —— 修 5 bug + 签名契约测试

按 SPEC v8 + e2e `/tmp/orca-e2e-v8/` 实证，修 shipped plugin 5 bug（builder 上一轮从 spike 回退）：
A transform 签名（单参→两参 `(input, out)`）/ B event payload（裸 event→`input?.event ?? input`）/
F SDK message-fetch 非 list 改 REST fetch / G bootstrap+next prompt 未 prepend Task-tool 指令
（cli.py 单一常量 `_TASK_TOOL_INSTRUCTION`）/ E plugin 不透传 --model（动态抽 `info.model`）。
加 6 签名契约测试（`tests/iface/in_session/test_in_session_v8.py:668-803`）防再回退 —— 根因教训
「TS 纯单测验不出运行时签名 bug」写进测试注释。baseline 83 → after 89 全绿，0 回归。守门 grep
（8 禁词）clean。Commit: `8bea9dd`。详见 [release note](../releases/2026-07-08-in-session-shell-v8.1-bugfixes.md)。

**e2e 复验闭环（`test-coverage-e2e`，`/tmp/orca-e2e-v81/`，8/8 PASS）**：shipped v8.1 开箱即用、零 patch 零 mock
—— `orca in-session start` 落模板 → 真 opencode 1.14.22 → `/orca doctor` 一键自检（plugin/marker/CLI 3 项 PASS）
+ `/orca run` 3-agent 端到端跑通到 `workflow_completed`（真 tape 11 事件 ws/ns/nc/rt ×3/wc，每节点 task 子代理、
子 session 过滤生效）。G2 编排骨架对齐（修订契约）+ 合规 fail loud + 6 签名契约测试 + model 透传全 PASS。
复现脚本 `/tmp/orca-e2e-v81/repro.sh`。**in-session shell v8.1 全流程（spec→3 轮 review→impl→e2e）闭环落地。**

**follow-up（非阻塞，e2e 发现）**：validator/router 语义不一致——非 entry 节点无 `routes` 时 `orca validate`
通过（当隐式终态）但 runtime router raise `RouteError: 无 route 匹配`。择期：validator 拒绝无 routes 的非 entry
节点，或 router 把无 routes 当 `$end`。

---

## ✅ 侧任务完成（2026-07-07）：web-shell-v2 B1/B2 —— opencode translator lossless + reasoning exposure

按 SPEC §3.2 + §11 step1 实现 web-v2 后端硬前置（shell 无关）。

**B1 translator lossless**（`orca/profiles/translators/opencode.py`）：reasoning→`agent_thinking`；
step_start→新 `agent_step_started`；step_finish 扩 `reasoning_tokens`；未知 envelope→新
`unknown_event{raw, source}`。EventType 加 2 项（37→39）。新类型在 reducer 显式 no-op
（D8：agent_step_started / unknown_event 绝不投影 RunState）。

**EventType grep 审计（SPEC §11 step1）**：reducer / projections / accumulator / TUI app.py /
LogStream / EVENT_VISIBILITY / AgentHistory / _event_summary 全部加 arm 或确认 default-no-op
安全；新加回归测试 `TestWebV2B1NewTypesThroughConsumers`（tape 含新类型经全消费者无 crash
+ 幂等）。

**B2 reasoning 暴露**：`ProviderCapabilities` +`supports_reasoning: bool = False`（opt-in）；
`CliProfile` +`reasoning_flags_env` + `resolve_reasoning_args()`（三态 env 注入）；
`builtin/opencode.py` 设 `supports_reasoning=True` +
`reasoning_flags_env="ORCA_OPENCODE_REASONING_FLAGS"`；`_build_spawn_config` 追加到 extra_args
末尾（与 `--model` 同路径，顺序：--model → reasoning）。

**fixture**（`tests/profiles/fixtures/opencode_sample.jsonl`）：7→9 行，含 reasoning capture
（deepseek-v4-flash --thinking 真抓取脱敏）+ experimental envelope。

**测试**：1758 passed / 0 新回归（baseline 1751 + 7 新增；唯一 fail 是预存 B-8
`daemon.py:105`，与本任务无关）。详见 [release note](../releases/2026-07-07-web-b1-b2-translator-lossless.md)
+ [SPEC §3.2](../specs/web-shell-v2-spec.md)。**Commit**：`c3a738f`（branch
`phase13-in-session-v8`）。

**遗留 follow-up**（不阻塞下一阶段）：
- 🔵 `reasoning_tokens` aggregation：B1 仅 capture；`UsageSummary` 加字段 + projections
  读取 + TUI Header 显累加留给后续阶段
- 🔵 D1 codegen（event.py → events.ts）+ CI grep：前端任务，B1 后置
- 🔵 `--thinking` 真链路 E2E（opencode+deepseek-v4-flash 实跑验证）

---

## ✅ 侧任务完成（2026-07-07）：in-session shell v8 —— 入口换 messages.transform + doctor 自检

按 SPEC v8 §2.6/§2.6.1/§2.6.2/§2.7 实现 v7→v8 增量。v7 CLI 大脑零改；重写 plugin 模板（flat
hooks + ctx.client + Bun.spawnSync + `experimental.chat.messages.transform` 入口 —— spike
实证 v7 的 `command.execute.before` 在 opencode 1.14.22 runtime 不触发）、加 `orca in-session
doctor` 3 项自检（plugin 加载/marker 派发/CLI imports，B3 自检盲区标注 idle 需跑 `/orca run`
验证）、统一 `/orca <sub>` 单 slash 命令、`start` 落 `.opencode/` 模板；CLI `status --json`
（MAJOR-1）+ `stop --owner`（MAJOR-2）+ plugin `spawnCli` fail loud（MAJOR-3）全部闭环。
52 新测全绿（in_session 31→83），全 unit 1775/1776（唯一 fail 预存 B-8 `daemon.py:105`，
与本任务无关）。详情见 [release note](../releases/2026-07-07-in-session-shell-v8.md) + [SPEC
v8](../specs/in-session-shell-design-draft.md)。**Commit**：`56083c1`（branch `phase13-in-session-v8`）。

**未实证项**（SPEC §9.2，留 `test-coverage-e2e` 真链路验）：transform await 外部进程时序 / sessionID
路径 / multi-session 绑定（M3）/ bootstrap 端到端 / 子 session 过滤 e2e / `/orca doctor` 真链路。

---

## ✅ 侧任务完成（2026-07-07）：`orca executor` CLI 扩展 —— 命令唯一真相源 + spawn 参数全可改

`show` 打印完整生效 argv + 每字段来源（env/项目/用户/default）；`set --binary/--flags/--prompt-channel/--scope` 三维可改 + 项目/用户两层 config；接通 phase-14 遗留 `resolve_flags` 死通道 + 新增 `resolve_prompt_channel`。142 测试全绿。详情见 [release note](../releases/2026-07-07-executor-cli-extend.md) + [plan](../plans/2026-07-07-executor-cli-extend.md)。**未 commit**（等用户确认）。

---

## ✅ 侧任务完成（2026-07-07）：in-session shell v7 —— 薄 CLI 唯一大脑 + plugin/hook 哑传输

按 SPEC v7 + ADR v3 实现。CLI `bootstrap/next/stop/status/start` = 唯一大脑（per-call flock +
`Tape.append_batch` 单次 write 原子化 B1 + `--output` 空串 normalize B2 + 失败 taxonomy F6 +
合规计数 F11 + marker RMW 在 flock 临界区内 N2）；plugin / CC hook 模板 = 哑传输（grep 守门
零业务逻辑）；daemon 降级无头 CI（I3.3a）。43 新测全绿，子集 1591 passed / 0 回归。详情见
[release note](../releases/2026-07-07-in-session-shell-v7.md) + SPEC
[`in-session-shell-design-draft.md`](../specs/in-session-shell-design-draft.md) v7。

**未 spike 项**（SPEC §9.2 明示，留 `test-coverage-e2e` 真链路验）：`/orca` 命令的
`command.execute.before` 拦截真链路 / `bootstrap` 命令端到端 / 多 session 绑定（M3）/
CC output cache 端到端（真 `claude -p` + hook 全链路）/ G2 序列对齐 vs `orca run` 同 wf tape。

**e2e 验证结果（test-coverage-e2e，`/tmp/orca-e2e-v7/`）**：
- ✅ **P0 CLI 核心**全过：G2 序列对齐（修 `step.py` route_taken.node 对齐 drive_loop 后逐 seq 全等）/ append_batch 原子+崩溃恢复 / 合规 fail loud / B2 空串 normalize / 失败 taxonomy。CLI 唯一大脑设计成立。
- ✅ 子 session idle 过滤（plugin event hook 层）实证正确。
- 🔴 **P1 opencode plugin 链路 BLOCKED（架构 gap）**：shipped 模板 4 缺陷——①`import @opencode/core/client`（npm 不存在，加载失败）②nested hooks 结构错③`Bun.spawn({stdout:"string"})` 非法④**`command.execute.before` hook 在 opencode 1.14.22 runtime 根本不存在**（只有 post 的 `command.executed`）→ slash 命令无法 BEFORE 拦截。spike 复核 `chat.message` 能触发但 `ignored` flag 不生效（无法抑制 user 文本）。**v7 §2.6 的 `/orca` 拦截入口机制在 1.14.22 不可实现，需重设入口**（待用户定方向）。

**遗留 follow-up**：daemon.py 推进循环仍逐条 emit（B-8，ADR I2 跨进程写者语义一致性），
留 daemon 迁 ExitCode + emit_batch 时一并改（与 `test_exit_codes` 长红同 PR）。

---

## 当前状态：create-workflow skill + install + benchmark 完成（2026-07-07）；下一模块待定

### ✅ 已完成：create-workflow skill + `orca skill install` + headless benchmark

通用 workflow 生成/转换 skill（吃描述或既有素材 → 归一化 DAG → Orca YAML+agent md，强制 `orca validate` 闭环）。`orca skill install` 显式装 CC + opencode 两边（排除 benchmark/ 防泄露）。16 case 公平 headless benchmark + harness（opencode 后端真跑），评测闭环 8/16 → 16/16，抽象 H1-H7 通用规则。详见 [release note](../releases/2026-07-07-create-workflow-skill.md) + [CHANGELOG](CHANGELOG.md)。

**交付**：`orca/skills/create-workflow/`（SKILL.md + reference + examples + benchmark）、`orca/iface/cli/skill_cmds.py`、`scripts/run_skill_benchmark.py`、3 个测试文件（34 测试全过，0 回归）。

**诚实交代**：LLM 生成有固有非确定性，单次 full run 通常 14-16/16，偶发个别 case flake（每次不同，每条失效模式都有对应通用规则）。

---

## 当前状态：CLI/MCP list 统一 + setup_outputs 注入完成（2026-07-07）；下一模块待定

### ✅ 已完成：phase-16 AgentHistory 单流重构（CC 风格 inline + 工具配对折叠）

AgentHistory 从「两区」（RichLog 摘要 + 独立 detail 面板）重构为**单条 RichLog inline 流**：tool_call+tool_result 配对成一条 entry（就地升级保 seq/位置）；message bold+主题色 / tool `✓/…/✗` icon 视觉分级；Enter 全量 reflow；删 `#agent-history-detail*` DOM（铁律 #7）；reducer fold 顺序无关（`_pending_results` 缓冲）。**详见** [release note](../releases/2026-07-07-phase-16-agent-history-single-stream.md) + [CHANGELOG](CHANGELOG.md)。

**遗留 follow-up**（移交下一 agent `test-coverage-e2e`）：
- 🔵 SPEC §5.1 九行按键矩阵完整 E2E（↓↑/jk/C/a/L/t 每键 §5.0 元 AC + state + 双向渲染文本）→ `tests/e2e_phase16/test_tui_buttons_e2e.py`
- 🔵 SPEC §5.3 Console.capture ANSI bold/主题色断言（message 视觉分级）
- 🔵 SPEC §5.5 render layer 字节契约（本阶段零改动，无需验；若后续动了再补）

### ✅ 已完成：CLI `list` 与 MCP `list_workflows` 统一（catalog 同源）

CLI `list` 委托 MCP 同源 `catalog.list_workflows()`（按 `wf.name` 扫 `./workflows` + `~/.orca/workflows`），删旧 `--dir` 扫 `./examples` 按文件名逻辑。**Commit**：`b8e5581`。详见 [release note](../releases/2026-07-07-cli-list-mcp-unify.md)。

### ✅ 已完成：setup_outputs 注入 runtime context（phase-10 🔴 技术债回填）

MCP `start_workflow(setup_outputs=...)` 真注入 RunManager → orchestrator → RunContext.setup → render `{{ setup.* }}`；resume+setup fail loud；review 🔴 `with_locals` 改 `dataclasses.replace`。范围：只解注入，resume 持久化 / TUI 自动执行 setup agent 声明不做。详见 [release note](../releases/2026-07-07-setup-outputs-injection.md) + [计划](../plans/2026-07-07-setup-outputs-injection.md)。

**遗留 follow-up**：
- 🔵 resume + setup：`workflow_started.data` 未持久化 setup_outputs（本次 fail loud 拦截）。
- 🔵 TUI/Web 进程内自动跑 setup agent：orchestrator 不遍历 `wf.setup`，本次只解注入。
- 🟡 `catalog.py` 物理位置在 `iface/mcp/`，CLI 跨子包 lazy import——择期迁 `orca/compile/catalog.py`。

---

## 当前状态：phase-10 MCP v4 实现完成（2026-07-07）；下一模块待定

### ✅ 已完成：phase-10 MCP v4（9 工具 + setup/execute 分相 + Result 信封）

**Commit**：`df563f4`。详见 [release note](../releases/2026-07-07-phase-10-mcp-v4.md) + [CHANGELOG](CHANGELOG.md)。

**交付**：
- server.py 重写：6 旧工具（含 resolve_gate）→ 9 v4 工具（Discovery 4 + Lifecycle 3 + History 2）
- setup/execute 分相：workflow.setup 字段 + compile validator（execute phase 拦截 ask_user/gate + setup phase 结构约束）
- 三重杠杆防跳过 setup：list_workflows has_setup / start_workflow setup_required 强校验 / tool description 引导
- Result 信封（ADR §4.1）：所有 tool 返 `{ok, data?, error?, _hint?}`，error.kind 是 ErrorKind 值（无 layer）
- 新模块：catalog.py / setup_phase.py / agent_catalog.py / tape_index.py + hints.py 扩展
- 1638 passed / 0 回归（baseline 1596 + 42 新增）

**遗留技术债**（详见 release note）：
- 🔴 setup_outputs 校验通过但不注入 RunManager runtime context（任务约束"不动 RunManager"）
- CLI 挂载（orca mcp subcommand）在 executor_cmds.py（dirty，并行工作持有）
- RunManager.start_run 签名扩展 + orchestrator setup_context 消费待后续 phase

---

## 当前状态：TUI v2 review remediation + 批 1 backend 完成（2026-07-07）；下一模块 phase-12-capabilities

### ✅ 已完成：TUI v2 review remediation + 批 1 backend（Status.blocked + projections.py）

**Commit**：见 `git log`（commit message 末尾含 Claude+Happy co-author）。详见
[release note](../releases/2026-07-07-tui-v2-review-batch1-projections.md) + [CHANGELOG](CHANGELOG.md)。

**交付**：
- 🔴 **Enter 展开回归修复**（commit 5562e5e 引入）：App 级 BINDINGS 加 `down`/`up`
  （`priority=True` 覆盖 RichLog scroll）转发到 `AgentHistory.action_cursor_down/up`；
  3 pilot 测试走真实 `pilot.press` 路径（不直设私有属性）
- 🟡 **批 1 ADR §4.3/§4.3.1**：`Status` Literal 加 `blocked`；`orca/run/projections.py`
  单一派生算法源（4 函数 batch fold，委托 apply_event）；`apply_event` 扩展 blocked
  派生（gate/interrupt 同源，None/running/terminal 三路径）；TUI 删独立 fold 副本全部
  改调 projections（DRY）；`agents_list.py` 类型收紧 + 删 `== "failed"` 字面量比较（P4）
- ADR §8.1 守门：`tests/iface/cli/test_status_literal.py` AST 检查 widget 无 Status
  字面量比较（含 fixture 路径 `parents[3]` + `.exists()` 断言防路径回归）
- 1596 passed / 0 回归（baseline 1558 + 38 新增）

**遗留 follow-up**（详见 release note）：
- 性能 O(N²)：`_dispatch_to_widgets` 每事件全量 refold `_all_events`（批 4 增量化）
- 多 gate 同时 active 的精确计数（批 4 给 RunState 加 active_blockers 字段）
- ADR §8.1 表述订正（"无 `== blocked`" → "无 Status 字面量比较"，batch 2 PR 一并）

---

## 当前状态：phase-11-process-lifecycle 实现完成（2026-07-07）；下一模块 phase-12-capabilities

### ✅ 已完成：phase-11-process-lifecycle 实现（批 3a，exec/iface）

**Commit**：`cdc3469`。详见 [release note](../releases/2026-07-07-phase-11-process-lifecycle.md) + [CHANGELOG](CHANGELOG.md)。

**交付**：
- 新增 `orca/exec/registry.py`（ProcessRegistry DI + 三段式 cancel + 平台分支）+ `orca/iface/exit_codes.py`（ExitCode 5 档）
- runner.py / script.py 接入 `start_new_session=True` + registry.acquire/release（推翻 phase-3 §2.5）
- orchestrator.py 加 `shutdown()` 方法（不动 phase-11-error except 链）
- run/__main__.py SIGTERM handler 只设 Event（signal-safe）+ 退出码经 `exit_for_terminal_status` 派生
- 1558 passed 0 回归（baseline 1525 + 33 新增）

**遗留技术债**（详见 release note §5）：
- DI 传递链未完全闭合（5 处 CLIRunner 调用点未传 run_id/node_id；正确性不受影响；phase-12 Adapter Protocol 一并设计）
- gates/hook_script.py 退出码未迁移（批 4）
- 真实孙子进程 E2E 需非沙箱环境（mock 时序单测覆盖契约）
- Windows test_cancel_windows.py 缺失（平台分支代码已就位）

### ✅ 已完成：phase-11-error-handling 实现（批 2，纯 exec/run/schema/iface）

**Commit**：`451dd39`。详见 [release note](../releases/2026-07-07-phase-11-error-handling.md) + [CHANGELOG](CHANGELOG.md)。

### 🔥 进行中：接口收敛 → 各 phase SPEC 回填 → 实现（goal 2026-07-07）

**goal 工作流**：每个模块依次 `设计 → spec-review-adversarial 审视 → 回填 SPEC → clean-code-builder 实现+清理 → test-coverage-e2e 验证`。范围：接口模块（phase-11-error / phase-11-process / phase-12 / phase-10）+ CURRENT 剩余任务（web/tui/codex **排除**）。

**ADR v2 已定稿**：[`docs/specs/2026-07-06-interface-convergence-adr.md`](../specs/2026-07-06-interface-convergence-adr.md)（spec-review-adversarial 审视通过，5 blocker + 10 major 全闭环）。核心决策：
- D1 错误：ErrorKind 单一分类权威；ExecError 字段集 `{kind,message,phase,node,raw}`；WorkflowTerminated 保留独立（非 ExecError 子类）；Error 删 layer；error_type→kind 读兼容期迁移；retry_on 解耦不改名
- D2 能力：CapabilitySet 全量替换 ProviderCapabilities；补 supports_concurrent_spawn / supports_usage_tracking / structured_output_mode 三态
- D3 节点状态：Status 加 blocked（projections 派生，不入 tape）；projections.py 提前到批 1
- D6 退出码：`orca/iface/exit_codes.py` 5 档 0/1/2/3/130
- D7 ProcessRegistry 用 DI（非 singleton）

**任务依赖图**（见 TaskList）：ADR(✅) → phase-11-error(#2 ✅) → phase-11-process(#3 ✅) → phase-12(#4 ✅) → phase-10(#5 ✅) → 依次实现(#6 进行中) → 剩余任务(#7) / TUI review(#8)

### ✅ 接口设计阶段完成（goal 第一任务）

ADR v2 + 4 phase SPEC 全部回填并对齐（spec-review-adversarial 审视通过）：
- [`docs/specs/2026-07-06-interface-convergence-adr.md`](../specs/2026-07-06-interface-convergence-adr.md) v2（5 blocker + 10 major 闭环）
- [`docs/specs/phase-11-error-handling.md`](../specs/phase-11-error-handling.md) v2.1（7 blocker 闭环：classifier 双入口 / 子类构造器契约 / 反向映射表 / retry_on 强制 retryable / raise 点 kind 表 等）
- [`docs/specs/phase-11-process-lifecycle.md`](../specs/phase-11-process-lifecycle.md) v2（exit_codes 位置 iface/ + ProcessRegistry DI + grep 精确）
- [`docs/specs/phase-12-capabilities.md`](../specs/phase-12-capabilities.md) v2（CapabilitySet 7 字段全量去留）
- [`docs/specs/phase-10-mcp.md`](../specs/phase-10-mcp.md) §2.4b v2（Result 信封无 layer / kind 是 ErrorKind 值）

### 🔥 实现阶段（goal 第二任务）—— 按 ADR §7 批次，避开并行 TUI 工作树

**实现顺序**（每模块 clean-code-builder 实现+清理 → test-coverage-e2e 验证 → 独立 commit）：
1. **phase-11-error-handling**（批 2，纯 exec/run/schema/iface，不碰 TUI）：ExecError 字段集 + Error/ErrorKind/classifier/retry 新模块 + error_type→kind 全量迁移（含 29 fixture）+ retry_started.data 扩展 + 编排 exception 子类化
2. **phase-11-process-lifecycle**（批 3a，exec/iface）：ProcessRegistry DI + 进程组 cancel + orca/iface/exit_codes.py
3. **phase-12-capabilities**（批 3b，profiles/compile）：CapabilitySet 全量替换 ProviderCapabilities + Adapter Protocol + 编译期校验
4. **phase-10 MCP**（批 3c，iface/mcp）：9 工具 Result 信封 + setup/execute 分相

**projections.py + Status.blocked**（批 1 backend 部分）：Status 加 blocked 可立即做（schema 层）；projections.py 抽取需等并行 TUI 重构落地后再动 app.py fold（避免工作树冲突），暂列 follow-up。

**未 commit**：本 session 改动为 docs（ADR + 4 SPEC + CURRENT），等实现首批落地后一并 commit，或按用户指示。

### 与并行 TUI 进程的边界（不变）
- TUI v2 重构在 `phase13-render-chart` 分支进行，动 `orca/iface/cli/widgets/` + `app.py`。实现阶段**不碰**这些文件。
- phase-11-error 实现动 `exec/` + `run/errors.py` + `run/orchestrator.py`（except 链）+ `schema/event.py`，与 TUI widgets 无交集。

---

## 当前状态：phase-10 MCP SPEC v4 设计敲定（setup/execute 分相）；TUI 任务等并行

### 🔥 进行中：phase-10 MCP 壳 SPEC v4（2026-07-04）

**v4 核心设计**：workflow setup/execute 分相消费（统一性原则）
- workflow schema 加 `setup: list[AgentNode]` 字段（setup phase，可选；复用 phase-14 AgentNode 三态）
- 三壳跑同一份 yaml，差异只在 setup phase 的"消费方式"：
  - **TUI/Web**：setup agent 在 workflow 内实跑，自动配 `ask_user` + `gate` 工具，弹窗交互
  - **MCP**：主 session 调 `get_agent_prompt` 借 prompt，主 session 替 setup agent 跑（用自己的工具对话），结果作为 `setup_outputs` 传给 `start_workflow`，workflow 跳过 setup agent 实际执行
- **execute phase 永不中断**：execute phase 的 agent 不配 `ask_user` / `gate` 工具
- **MCP 工具集 9 个**（v3 的 10 个删 resolve_gate）：Discovery 4 + Lifecycle 3 + History 2
- **三重杠杆防跳过 setup**：list_workflows 标记 has_setup / start_workflow 强校验 setup_required / tool description

**已更新文档**（本 session 完成）：
- [docs/specs/phase-10-mcp.md](../specs/phase-10-mcp.md) —— 整体重写为 v4（9 个工具 + setup/execute 分相 + 三重杠杆 + 失败处理 + 完整 user journey）
- [docs/specs/shells-design-draft.md](../specs/shells-design-draft.md) §5 —— MCP 壳设计 v4 重写（§5.1 协议约束修正 + §5.2 setup/execute 分相 + §5.4 三重杠杆 + §5.5 工具签名 + §8.3 端到端 user journey）

**协议约束调研修正**（2026-07-04 再核实，原 v1/v3 多处过时）：
- elicitation CC **已支持**（PR #2799），但有边界 bug（#56243 cowork cancelled / #62319 form-mode auto-decline）—— 仅可作 setup 轻交互，workflow gate 不依赖
- progress notification CC **不支持**（#4157 Anthropic 直接确认）—— server mid-tool-call 主动推进度不行
- 60s 是**默认超时**不是硬 kill（#424 / #43791 timeout 字段被忽略）
- Tasks (2025-11-25 spec) 已落地，CC 未实现（#52137）

**待落地（下一阶段实施）**：
1. schema 改动：`orca/schema/workflow.py` 加 `setup: list[AgentNode] = []`
2. 新文件：`orca/iface/mcp/{server,catalog,agent_catalog,setup_phase,tape_index,transport}.py`
3. compile validator：强制 execute phase 的 AgentNode 不配 ask_user/gate 工具
4. setup_outputs 校验逻辑（§5.9）
5. RunManager 加 `run_summary` / `list_runs` / `cancel_run` 方法
6. schema 扩展 `workflow_cancelled` 事件类型
7. `orca mcp` subcommand
8. 单元测试 + E2E（§6.3 五个 E2E 用例）

**v4 vs v3 差异**：
- v3 `setup_agent: str | None`（引用单个 agent）→ v4 `setup: list[AgentNode]`（setup phase 一段）
- v3 setup 在 workflow 外（前置）→ v4 setup 在 workflow 内（一段 phase）
- v3 仍保留 `resolve_gate` → v4 删除（execute phase 永不中断）

### TUI Redesign v2 完成（2026-07-07）—— 取消 DAG + agent 输出可见

TUI 三块布局重写（左 AgentsList / 右上 AgentHistory / 右下 LogStream）。用户核心痛点闭环：① 看到每个 agent 输出（last message 默认展开）② j/k 切换 agent 看历史（_node_events 分桶）③ 取消 DAG 图 ④ LogStream 高层节点事件 + 5 level icon + 完整失败原因。

**完成 commits**：59021c9 + 5f9988c + e252653 + ab3b254 + 0e9e877 + 77f5685 + 85ecb61

详见 [release note](../releases/2026-07-07-tui-redesign-v2.md) + [v2 spec](../specs/tui-redesign-v2-design-draft.md)。

v1.1.1 widget 全部删除（DagGraph / dag_layout / _dag_render / activity_stream）+ display:none 双写兼容路径清掉。1396+ 测试全过。

## 与并行进程的边界
- TUI v1 / v1.1.1 commit（`7bd43ef` / `225933e`）只动 `orca/iface/cli/widgets/` + `app.py` + 对应测试 + status docs。
- phase-10 v4 SPEC 改动只动 `docs/specs/{phase-10-mcp,shells-design-draft}.md` + 本文件。
- 留工作树（并行进程持有）：`profiles/builtin/*` + `terminal.py` + `gates/dialog.py`
  + `exec/validator.py` + `executor_cmds.py` + `config.py` + `iface/cli/widgets/tool_render/
  normalize.py` + `run/orchestrator.py` + `run/router.py` + 它们测试
  + `examples/demo_task.yaml` + `pyproject.toml` + `uv.lock`
  + `tests/e2e_phase{13,14}/_artifacts/*.jsonl`（_tape）+ `_tui.svg`。

## 已知 follow-up（v2 路线，不阻塞本任务）
- TUI live timer 走 wall clock（spec §4.4：「不进 tape」UI 交互态）
- DAG 节点 hover tooltip（spec §13.7 v2 评估）
- Activity Stream 流式 markdown shiki 增量高亮（render layer v2）
- 全局 thinking 可见性切换
- 双写 LogStream/NodeDetail 兼容路径在 v2 移除

## 待办（等用户指示方向）
1. **phase-10 MCP 实施**（v4 SPEC 已就位，待用户拍板后写实施计划开工）—— **前置：实施前必读 [`phase-11-error-handling.md`](../specs/phase-11-error-handling.md) §1 工具返回形状（统一错误信封） + [`phase-11-process-lifecycle.md`](../specs/phase-11-process-lifecycle.md) §3 退出码语义**
2. phase-12 / 13 / 14 / 15 / TUI 重设计 v1 分支 merge / PR（分支 `phase13-render-chart`）。
3. **批 2（phase-16）**：轻量本地包分发（多 pool + `name@source`）+ workspace-instruction。
4. code-reviewer M2/M3（resolve_flags setdefault 文档交叉引用 + stacklevel 指向）+ N3。
5. **render layer v1.5**：codex 接入（apply_patch 解析 + shell/read_file 映射）—— **前置：[`phase-12-capabilities.md`](../specs/phase-12-capabilities.md) 落地（codex `supports_apply_patch=True` 由 CapabilitySet 声明，render layer 据此分支）**
6. **render layer v2**：Web 端 TS 镜像 + 流式 shiki 增量高亮 + 千行 diff 虚拟化。
7. **background chart gap**（mxint follow-up）：让 `--background` 模式 chart 可用。
8. **agent interrupt 独立 feature**（见下文「agent interrupt 独立 feature」段，待立项 SPEC）
9. **phase-11-error-handling 实施**（SPEC v1 已就位 [`phase-11-error-handling.md`](../specs/phase-11-error-handling.md)）：统一错误信封 `{ok,data?,error?,_hint?}` + ErrorKind 11 分类 + 三层重试不互相吞错 + classifier 纯函数。可与 phase-10 并行，phase-10 先硬编码 Result 落地，phase-11 回填横切抽象。

   **前置 ADR（2026-07-06 接口统一性审计）**：错误接口当前已 5 套并存（canonical Event 3 个 type 字段不一 + `ExecError` phase 8 类 + 3 个编排 exception + phase-11 提议 `Error/ErrorKind` 11 分类）。phase-11 SPEC §1 落地前必须先写 ADR 明确：
   - `Error.kind` (11 分类) ↔ `ExecError.phase` (8 类) 映射规则（多对一 / 一对一 / 漏洞怎么补）
   - canonical Event `node_failed.data.error_type` 字段值最终取 `Error.kind` 还是 `ExecError.error_type`
   - 三层错误表达（持久层 Event / 运行时层 Result / exception 层）的**单一权威**：每个错只在一层有真相，其他层只翻译不重新分类
   - 不留 ExecError 与 Error 双 exception 并存（违反用户底线「不存在多套并存」）
   - 详见 [`tui-redesign-v2-design-draft.md`](../specs/tui-redesign-v2-design-draft.md) §11.4 风险 A
10. **phase-11-process-lifecycle 实施**（SPEC v1 已就位 [`phase-11-process-lifecycle.md`](../specs/phase-11-process-lifecycle.md)）：子进程全局注册表 + 进程组 cancel（推翻 phase-3 §2.5 旧决策）+ 退出码契约 0/1/2/3/130。F2 已并入此 SPEC。
11. **phase-12-capabilities 实施**（SPEC v1 已就位 [`phase-12-capabilities.md`](../specs/phase-12-capabilities.md)）：CapabilitySet 数据模型（部分抄 mco）+ async ProviderAdapter Protocol + 编译期能力校验。**阻塞 render layer v1.5（codex 接入）**。phase-11 完成后开工。
12. **TUI fold DRY follow-up**（v2 follow-up，0.5d）：v1.1.1 fold 字段（`_node_iter` / `_node_status` / `_node_usage`）与 `RunState.node_status` 是两份派生状态——抽到 `orca/run/projections.py` 让 RunState + TUI + 未来 Web/MCP 都消费同一份 reducer。详见 [`tui-redesign-v2-design-draft.md`](../specs/tui-redesign-v2-design-draft.md) §11.2 / §11.4 风险 C。

## agent interrupt 独立 feature（2026-07-04 讨论，待立项）

**已开 design draft**：[`docs/specs/agent-interrupt-design-draft.md`](../specs/agent-interrupt-design-draft.md)（9 章完整骨架 + 9 条决策备忘 + 6 个遗留问题）

**需求**：agent 执行中（execute phase），用户主动打断 + 注入 guidance（方向纠偏）+ agent 继续。

**三种能力对比**（用户视角几乎等价，实现差异大）：

| 能力 | 触发时机 | agent 当前调用 | 实现复杂度 |
|---|---|---|---|
| **node 边界 interrupt** | agent 自然跑完后 | 不 cancel，重跑带 guidance | ✅ 已实现（`orca/gates/interrupt.py` InterruptHandler，TUI Ctrl+G / Web 按钮）|
| **mid-stream cancel + resume** | agent 跑到一半 | cancel subprocess + `claude -p --resume <session_id>` + guidance | ⚠️ 三壳都未实现，但**技术可行**（cancel 已有 + resume 是 claude/opencode 内置），不需要 executor 大改造 |
| **真 streaming interrupt** | agent streaming token 中 | 不重启，stdin 推 guidance 到当前 turn | ❌ 三壳都不支持，需要 executor 双通道 streaming 改造 |

**关键判断**：mid-stream cancel+resume 与 真 streaming interrupt **用户视角效果几乎等价**（都是"介入 + guidance + 调整输出"）。差别在内部：
- cancel+resume：agent 看到自己之前的输出（部分进 history），基于"原始 prompt + 自己部分输出 + guidance"重新生成。**更连贯**，但 token 消耗高
- 真 streaming：agent 在当前 turn 内调整，不知道自己之前输出。**省 token**，但实现复杂

**推荐路径**：mid-stream cancel+resume（性价比最高），不做真 streaming。

**实现拆解**（独立 SPEC 待写）：
1. **executor 层**：
   - `claude/exec/runner.py` 加 `cancel_and_resume(session_id, guidance) -> str` 方法（cancel 当前 subprocess + 新起 `claude -p --resume <session_id>` + guidance 作为新 user message）
   - opencode executor 同款接口
   - 复用 Orca 已有 session_id（HumanGate / InterruptHandler 已有）
2. **InterruptHandler 扩展**（`orca/gates/interrupt.py`）：
   - 现有 InterruptHandler 已支持 continue + guidance / skip / abort（node 边界）
   - 扩展 action：`continue_immediately`（mid-stream cancel + resume，不等当前 turn 完成）
   - 触发时机：从"node 边界"扩展为"任意时刻"（asyncio.Event 立刻唤醒）
3. **三壳集成**：
   - **TUI**：现有 Ctrl+G（node 边界）保留；新增强制打断快捷键（如 Ctrl+C 或 Shift+G）触发 mid-stream cancel+resume
   - **Web**：现有"中断"按钮（node 边界）保留；新增强制打断按钮
   - **MCP**：新工具 `interrupt_task(task_id, action="continue_immediately", guidance="...")`
4. **tape 记录**：interrupt_requested / interrupt_resolved 已有事件类型；扩展 data 加 `interrupt_kind: "node_boundary" | "mid_stream_cancel_resume"`

**与 phase-10 的关系**：phase-10 §8 标为独立 feature，不在 phase-10 范围内。phase-10 完成后可立项 phase-X（暂定 phase-17 或 phase-9e，待规划）。

**遗留问题**（独立 SPEC 立项时讨论）：
- guidance 是否传递给后续 node？（当前 InterruptHandler 只给当前 agent）
- mid-stream cancel 时 agent 已生成的部分输出怎么处理？（丢弃 vs 进 tape vs 进 history）
- resume 失败时（session_id 找不到）怎么 fallback？
- 用户 cancel 的 token 消耗算谁的？（cost accounting）

## 必读文件（下一任务开工前按需）
- [`docs/specs/phase-10-mcp.md`](../specs/phase-10-mcp.md)（v4 SPEC 全文，setup/execute 分相 + 9 工具 + 三重杠杆）
- [`docs/specs/shells-design-draft.md`](../specs/shells-design-draft.md) §5（MCP 壳设计 v4）
- [`docs/releases/2026-07-04-tui-redesign-v1.md`](../releases/2026-07-04-tui-redesign-v1.md)（TUI 重设计 v1 全貌）
- [`docs/releases/2026-07-04-tui-redesign-v1-gaps-abce.md`](../releases/2026-07-04-tui-redesign-v1-gaps-abce.md)（v1.1.1 4 GAP 收口）
- [`docs/specs/tui-redesign-draft.md`](../specs/tui-redesign-draft.md)（v1.1.1 spec 全文）
- [`docs/releases/2026-07-04-render-layer-v1.md`](../releases/2026-07-04-render-layer-v1.md)（phase-15 v1 全貌）+ [`docs/specs/render-layer-design-draft.md`](../specs/render-layer-design-draft.md) §3/§5/§6/§8/§12
- [`orca/iface/cli/widgets/`](../../orca/iface/cli/widgets/)（_event_filter / _dag_render / activity_stream / dag_graph / dag_layout / header 实现）

## 参考仓调研发现 follow-up（2026-07-05 / 2026-07-06 更新）

调研 CCW + mco 后整理 5 个可借鉴设计点（F1-F5）。完整分析与落地建议见 [`docs/plans/2026-07-05-reference-repos-borrow.md`](../plans/2026-07-05-reference-repos-borrow.md)。

**立项状态**：
- ✅ **F2**（进程组 cancel）已提升为 [`phase-11-process-lifecycle.md`](../specs/phase-11-process-lifecycle.md) §2
- 📋 **F1 / F3 / F4 / F5** 仍独立待立项（不阻塞 phase-10/11/12），立项顺序：F3（高优先 + 小成本）→ F4 → F5 → F1

**2026-07-06 宏观借鉴补充**：在 F1-F5 之上新增 G1-G7（CapabilitySet / 状态机 / 错误信封 / ErrorKind / 进程组 / 子进程注册表 / 退出码）。G2-G7 已落入 phase-11-error-handling + phase-11-process-lifecycle；G1（CapabilitySet）落入 phase-12-capabilities。详见各 SPEC。

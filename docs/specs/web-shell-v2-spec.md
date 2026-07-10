# Web Shell v2 —— 重写 SPEC（rev 2，闭环 spec-review 12 BLOCKER + 13 MAJOR）

> 当前 Web 很差（agent 输出仅 60 字截断 log、无 markdown、无流式、工具不折叠、`prompt_rendered` 被忽略、多 run 外壳冗余、Replay 无用）。**推倒重写前端**；tape 唯一真相源**机制**不动（schema 做 sanctioned 扩展，见 §3.2）。风格对齐 **AgentHarness**（`/Users/mozzie/Desktop/Projects/AgentHarness`，只读）——抄其**渲染层设计**，**严禁照搬其多 store 结构**。
>
> 流程：本 SPEC → `spec-review-adversarial` 审（rev1 conditional-fail，已闭环）→ 复审 → `clean-code-builder` 实现 → `test-coverage-e2e` 验每屏 → 不达标打回 → 循环至尽善尽美。

---

## 0. 决策（闭环 review 的 3 个用户决策点 + gate/elapsed 等）

- **D1 EventType 同步 = codegen**：build 时从 `orca/schema/event.py` 的 `EventType` Literal 生成 `orca/iface/web/frontend/src/types/events.ts`（根治 21↔31 漂移）；CI 加 grep 测试断言两边类型集合相等作 backstop。
- **D2 conversation 分组键 = `node`**：transcript 按 DAG node 分组；retry/foreach 多 `session_id` 在同 node 内合并展示，session 边界作细分隔符（不切走）。
- **D3 charts = ChartsView 全量渲染 + ConversationView 紧凑引用行**：`render_chart` 是 **script 子进程 API（非 agent 工具）**，经 ingestor socket → `custom(kind=chart)` 入 tape（`orca/chart/__init__.py` 实证）；agent 节点通过其 tool 跑 script（继承 `ORCA_*` env）也能产图并归属该 node（mxint report_painter 实证）。→ ChartsView 按 label 分组全量 Recharts 渲染；ConversationView 内 `custom(chart)` 渲染为紧凑 `📊 <title>` 引用行（点击跳 ChartsView），**不双开整图**。
- **D4 gate（HMIL）= 中心模态浮层**：`human_decision_requested` 到达 → 屏幕中心模态（prompt + options），阻塞会话视图；`human_decision_resolved` → 关闭 + toast；保留 `WsClientMessage gate_response` 通道。
- **D5 elapsed = running 时 wall-clock tick，完成 snap 到 tape**：running 时 `wall − started.ts`；`node_completed`/`workflow_completed` 到达 → display snap 到 `data.elapsed` + 停 tick（防 wall-clock 成前端真相，铁律）。单一共享 `useElapsedTick()` hook 在页根。
- **D6 WS reconnect = resume by seq**：client 记 `last_seq_seen`；重连发 `{type:"resume",run_id,since}`；server 重放 `seq>since`；不可用 → client 全量 re-fetch + re-fold + **丢弃 `_textBuf`**。
- **D7 reducer = seq 升序 fold**：seq-indexed sorted map（非 append-list）；tiebreaker `max(seq)` 胜（保 `ChartsView(T)==ChartsView(sort(T))==ChartsView(reverse(T))`）。
- **D8 `unknown_event` = reducer MUST no-op**：tape 级 escape hatch；仅 LogStream 渲染；**绝不** project 进 RunState/视图真相。
- **D9 stall 阈值 = 5s**（可配，`WEB_STALL_THRESHOLD_MS`）。
- **D10 image URL scheme = 后端 endpoint** `/api/runs/<id>/assets/<hash>`；markdown renderer 重写相对/`file://` 路径到该 endpoint。

---

## 1. 铁律（不可违背）

1. **后端唯一真相源**：tape 唯一事实；前端只是 tape 的渲染。前端**无独立状态真相**——所有展示 = `fold(tape events)` 派生。**tape 机制不动**；schema 做 §3.2 列出的 sanctioned 扩展（受控，非"不动"）。
2. **接口统一 + 过期即删**：前端 EventType 与后端 **D1 codegen 一处同步**；多 run 外壳、Replay、旧 `NodeDetail`/`formatLogLine` **删除不留兼容**。
3. **设计先行，禁打补丁**：新增能力先设计再编码；SOLID/DRY/OCP；禁多套并存、禁兼容层。

> 既有铁律：[`phase-3-events.md`](phase-3-events.md) §3（单 tape + 幂等 reducer + 一条读路径）、[`shells-design-draft.md`](shells-design-draft.md)。

---

## 2. 范围

**删除**（§8 全清单）。**保留复用**：`orca/iface/web/frontend/src/components/graph/*`（DAG，降浮层）、`chart/widgets/*`（Recharts，契约精化）、`hooks/use-websocket.ts`+`use-run-events.ts`、后端 `ws_handler.py`/`server.py`/`run_manager.py`/`routes/`。**新建**：`DiffView`/`FileContentView`（轻量自建，参考 AH `conversation/DiffView.tsx`+`FileContentView.tsx` 的设计，不抄多 store 依赖）。**单 run/页**：无 run 列表/历史（后置）。

---

## 3. 架构

### 3.1 数据流（单向，单真相源）

```
opencode subprocess → translator → Event → bus.emit → tape.append（唯一真相）
                                                        │ WS（每事件透传）
                                  frontend processEvent → store = fold(events)
                                                        │ selector（纯函数）
                                  selectAgents / selectConversation / selectCharts / selectLog
```

- **一个 Zustand store**：`state = reducer(tape events)`（D7 seq 升序 fold，seq-indexed sorted map）。**严禁** AH 式 conversation/toolCall/chart/span/agentIO 多 store + per-workflow scoped clone + `_cache` + HTTP write-through（反模式）。
- **selector 纯函数**：`selectAgents`/`selectConversation(state,nodeId)`/`selectCharts`/`selectLog`。reducer 幂等（同事件应用 N 次=1 次）。
- **无 Replay 功能**：state 永远 = `fold(全量 events)`；删 `ReplayBar`/`replayPosition`/时间旅行 UI（event-sourced fold 是不可见机制，保留）。
- **新 client join**：`GET /api/runs/<id>/events` 全量 tape → fold → 订阅 WS 增量（[`2026-07-05-reference-repos-borrow.md`](../plans/2026-07-05-reference-repos-borrow.md) F5）。
- **WS reconnect**（D6）：resume by `last_seq_seen`；失败 fallback 全量 re-fetch + re-fold + 丢弃 `_textBuf`。

### 3.2 tape 事件契约（后端 B1 sanctioned 扩展 + 前端 codegen）

后端 translator lossless（`orca/profiles/translators/opencode.py`）：

| opencode envelope | → canonical Event | data | reducer 语义 |
|---|---|---|---|
| `reasoning` | `agent_thinking`（已有） | `{text}` | 进 conversation thinking |
| `step_start` | **新 `agent_step_started`** | `{step_reason?}` | conversation 步标记（D2） |
| `step_finish`（扩展） | `agent_usage`（加字段） | `+reasoning_tokens`（←`tokens.reasoning`）；旧 tape `data.get('reasoning_tokens',0)` 默认 0 | 聚合 TopBar/agent 行，不进 conversation |
| 未知 envelope | **新 `unknown_event`** | `{raw, source:"opencode"}` | **MUST no-op**（D8），仅 LogStream |

**codegen（D1）**：`orca/schema/event.py` `EventType`（现 31）+ 上述 2 新 = 33 → build 生成 `events.ts`；CI grep 断言两边集合相等。**render_chart（D3）**：data 进 tape → Recharts 渲染；否决 PNG（破坏单一真相）。**image URL（D10）**：后端 `/api/runs/<id>/assets/<hash>`；renderer 重写相对路径。

### 3.3 流式（RAF 批处理，抄 AH 机制不抄 store）

- 文本增量缓冲 `_textBuf: Map<sessionId, str>`，`requestAnimationFrame` 每帧一次 `setState`（抄 AH `_textBuf`+`_rafSeq` 失效）。
- **多 session 粒度**（foreach 并发）：sync-flush on event E 只 flush `_textBuf[E.session_id]`；RAF tick flush 全部 session；`agent_tool_call` flush 限本 session_id。
- **AH 边界硬化**：RAF buffer 在独立 hook/module；store 只见 committed frame；buffer **永不参与 render 决策**；run 切换丢弃 buffer。
- 适配 opencode 块级（整块到即渲染）；块间静默靠 §6 liveness 派生诚实呈现；为未来 token 级（claude/serve）预留同一套 UX。

---

## 4. 布局

```
┌─ TopBar ──────────────────────────────────────────────────────────┐
│ ● <run>   <status>   ⏱<elapsed live>   🪙<cost>                    │
├────────────┬──────────────────────────────────────┬───────────────┤
│ Agents     │ [ 会话 Conversation | 图表 Charts ]   │ LogStream     │
│ rail       │ ──────────────────────────────────── │ (常驻最右,    │
│ ●code_gen  │  （页签内容；gate 模态浮于其上）      │  虚拟化 live) │
│  ⏱45s 1.2k│                                      │ seq·type·摘要 │
│ ○validator │                                      │ ...           │
│  [DAG]     │                                      │               │
└────────────┴──────────────────────────────────────┴───────────────┘
```
- `react-resizable-panels` 三栏。左 rail 窄、中宽、右 LogStream 中等可调。
- gate（D4）：`human_decision_requested` → 中心模态浮层覆盖中栏。
- 无 Replay 控件。

---

## 5. 组件规格

### 5.1 TopBar
run 名 + status icon（`●running`/`✓completed`/`✗failed`/`cancelled`/`blocked`）+ **elapsed**（D5：running tick / 完成 snap `workflow_completed.data.elapsed` 停 tick）+ 累计 token/成本（fold 全 `agent_usage`）。**无 Replay**。

### 5.2 AgentsRail
每 agent = status icon + 名 + `⏱Ns`（D5：`node_started`→`node_completed.data.elapsed` snap）+ token 小字。选中切中栏会话（D2 按 node 分组）。DAG 切换按钮（浮层，懒挂）。**单一 `useElapsedTick()`**（D5），N agent 不开 N timer。

### 5.3 ConversationView（会话页签，核心）

**per-EventType 渲染规则**（穷举后端 `EventType` 全 37 + B1 新增 2 = 39；每成员均有归宿，闭 review #9）：

会话内（conversation）：

| EventType | 渲染 |
|---|---|
| `prompt_rendered` | `▸ user prompt`（默认折叠，展开看 `data.preview` 渲染后全文）|
| `agent_thinking` | 琥珀折叠 `💭 Thinking`，markdown，流式 `…` 脉动 |
| `agent_message` | 完整 markdown（gfm+math+katex+prism），流式 `▎`，**永不折叠**（含最终 report）|
| `agent_tool_call` | 工具行；无 result→`⟳ Tool …`（pending）|
| `agent_tool_result` | 与 call 配对→`✓ Tool …`；orphan（无 call）→**LogStream warn，不进 conversation**；空 result→`✓ Tool (no output)` |
| `agent_step_started` | 附同 session 下一个 thinking/message 作"第 N 步"标记；无→dim `· step` 分隔 |
| `dialog_message` | 多轮追问的 user/agent turn，按 `agent_message` 同款 markdown 渲染，带 turn 标记 |
| `dialog_started`/`dialog_ended` | 细分隔符 `── dialog ──` |
| `custom`(`kind=chart`) | 紧凑 `📊 <title>` 引用行（点击跳 ChartsView，D3）|
| `custom`（非 chart kind） | dim `◆ custom(<kind>)` 可展开看 raw |
| `node_started`/`completed`/`skipped` | 细分隔符 |
| `node_failed`/`workflow_failed` | **红色 error block**（kind+message+phase）|
| `retry_started`/`succeeded`/`exhausted` | dim 状态行 `↻ retry N/Max (kind)` |
| `interrupt_requested`/`resolved` | dim 状态行 |
| `validator_started`/`passed`/`failed` | dim 状态行 |
| `wait_started`/`completed` | dim 状态行 |
| `foreach_*` | 聚合到 AgentsRail 进度，conversation 内 dim |
| `unknown_event` | dim `? unknown` 可展开看 raw（D8，不 project）|

不进 conversation（显式归宿）：

| EventType | 归宿 |
|---|---|
| `workflow_started`/`completed`/`resumed` | TopBar status + LogStream；`workflow_completed` 终态 snap elapsed（D5） |
| `workflow_cancelled` | TopBar 终态 `cancelled`（用户取消可见）+ LogStream |
| `workflow_failed` | 见上（conversation 红 block + TopBar status） |
| `route_taken` | DAG 浮层边 + LogStream |
| `human_decision_requested`/`resolved` | D4 中心模态（不进 conversation 流） |
| `agent_usage` | 聚合 TopBar 累计成本/agent 行 token（不进 conversation） |
| `error`（bare） | LogStream；具体错误信息已由 `node_failed`/`workflow_failed` 携带并红 block 渲染，bare `error` 不重复进 conversation |

**折叠规则**（闭 review #3，显式）：
- 默认折叠：`prompt_rendered`、`agent_thinking`、`custom(chart)` 引用行、retry/interrupt/validator/wait 状态行。
- 成组：连续 `agent_tool_call`+`agent_tool_result` 对（中间无 `agent_message`/`agent_thinking`）→ `▸ N tools`（可展开）。
- **永不折叠**：`agent_message`（含最终 report）。
- AC：对 fixture tape T，断言 `collapsed-set` 与 `expanded-set` 精确相等。

**▎ 流式光标**（闭 review #4）：存在 **IFF** `run.status==running` 且该 session 最后事件为 `agent_message`/`agent_thinking` 且其后无 `agent_tool_call`/`agent_tool_result`/`node_completed`。**finished tape → ▎ 必须消失**（fixture 断言）。

**工具展开**：一行 `✓ <Tool> <smart arg> <duration>`；smart arg（抄 AH `ToolCallMessage`）：bash→`$ <cmd>` 截断、read/write/edit→basename、其余→`k=v,…` 截断 60。展开：args + 流式输出 + result；`write`/`edit`→`DiffView`、`read`→`FileContentView`。

**虚拟化**（闭 review #17）：>500 条 `react-window` 虚拟化。

**错误转录**（闭 review #29）：`workflow_failed`/`node_failed` → 红 error block。

### 5.4 ChartsView（图表页签）
从 `custom(kind=chart)` 派生（**同一 store**，D7 seq 升序 fold）。group=`data.label ?? "misc"`；组内 identity=`data.title`，缺 title→`chart_type+seq`；同 identity upsert（实时更新）。`IntersectionObserver` 懒挂（300px skeleton）。Recharts 7 widget（已有）+ 契约加 `x/y/hue/size/series`。抄 AH `chartTheme.ts`（8 色，CSS-var 主题感知）+ `ChartGroup`（collapsible，`repeat(auto-fit,minmax(300px,1fr))`）。

### 5.5 LogStream（最右常驻）
`react-window` 虚拟化全事件尾，live。每行 `{seq}·{type}·{一行摘要≤80字}`；**每个 EventType 均有 readable 摘要，无 no-op fallback**；live 行数 == tape 事件数。**auto-scroll 策略**（闭 review #36）：用户上滚→暂停 auto-scroll + 显示"跳最新"按钮；pinned-to-bottom→新事件滚到末 seq。

### 5.6 Gate 模态（D4）
`human_decision_requested` → 中心模态（prompt + options + context）；`human_decision_resolved` → 关闭 + ResolvedToast。`WsClientMessage gate_response` 通道保留。

### 5.7 DAG 浮层（闭 review #26）
AgentsRail `[DAG]` 按钮 → 懒挂全屏浮层（复用 `graph/*`，React Flow+dagre 已成熟）；不在三栏常驻。详细 graph 规格 defer（成熟组件，overlay 容器即可）。

---

## 6. Liveness（前端派生 + B1 心跳，无新 UI 态入 tape）

- elapsed（D5）。"思考中"：当前 step 内无新事件 > D9 阈值(5s) → 琥珀"思考中 Ns"；`agent_thinking` 在流则显式更准。进度心跳=`agent_step_started`（B1）。LogStream 在流=活着。**诚实**：opencode 单步内纯生成静默不可解（块级），靠 elapsed+stall 呈现，不伪装。

---

## 7. 样式系统（对齐 AgentHarness）
Tailwind（已有）+ 暗色优先。颜色：抄 AH `chartTheme.ts` 8 色 + 状态色（success/amber-thinking/danger/neutral）。排版：等宽（tool/log/code）+ prose（markdown）。icon：lucide（`Loader2` 脉动/`Check`/`X`/`ChevronRight`）。**严禁抄**：AH 多 store / scoped clone / router HTTP write-through / `_cache`。

---

## 8. 删除清单（闭 review #25，过期即删不留兼容）

`orca/iface/web/frontend/src/`：
- `App.tsx` 多 run 路由、`pages/RunsListPage.tsx`、`pages/NewRunPage.tsx`（新建入口若留则单独最小化）。
- `components/layout/RunsSidebar.tsx`、`hooks/use-runs-list.ts`。
- `hooks/use-replay.ts`、`stores/replay-actions.ts`（若存在）、`ReplayBar` + `replayPosition` 及 DAG/Log/Chart 切片逻辑。
- 旧 `components/detail/NodeDetail.tsx`（flat `<ul>` + `formatLogLine`）→ 被 ConversationView 取代。
- `components/layout/StatusBar.tsx`（并入 TopBar）、`components/layout/TopBar.tsx`（原地重写）。
- `components/pages/RunDetailPage.tsx`（容器重写为单 run 根）。
- 任何 per-workflow scoped store clone、`_cache`、HTTP write-through。
- **保留**：`components/graph/*`（浮层用）、`chart/widgets/*`、`hooks/use-websocket.ts`/`use-run-events.ts`。

---

## 9. 验收标准（每条配 oracle，闭 review #32）

**Must（用户点名）**：
1. **功能正确 + 折叠 + 美化**：oracle = 对 fixture tape T（§10），`selectConversation` 产出的 `ConversationMessage[]` 与期望 snapshot 逐条相等（含折叠状态、▎ IFF、markdown 渲染节点存在）。断言：工具对成组、prompt/thinking 默认折叠、message 永不折叠、finished tape 无 ▎。
2. **所有图正确渲染**：oracle = `selectCharts(T)` 集合 == tape 中所有 `custom(kind=chart)` 事件（按 group/identity 去重 upsert）；ChartsView DOM 含每个 chart 的 Recharts 节点；`sort(T)`/`reverse(T)` 产同集（D7）。
3. **LogStream 全信息**：oracle = `selectLog(T)` 行数 == tape 事件数；每行 type∈EventType 有 readable 摘要；无 type 落 no-op。

**每屏 AC**：TopBar（elapsed live→snap、cost 累计、status）、AgentsRail（状态/elapsed/token、切换、DAG 浮层）、ConversationView（§5.3 全表 + 折叠 + ▎ IFF + 虚拟化 + error block）、ChartsView（§5.4 + D7 序无关）、LogStream（§5.5 + auto-scroll）、Gate（§5.6）。

**铁律 AC**：`grep -r "replayPosition\|formatLogLine\|RunsSidebar\|use-runs-list"` 命中 0；全仓 Zustand store 定义 ≤1（`workflow-store`）；`events.ts` EventType 集合 == `event.py`（CI）。

---

## 10. 测试计划（fixture tape + snapshot oracle）

- **selector fixture**：构造 tape T（含 reasoning、step_start、乱序 tool_result、orphan result、retry、foreach、unknown_event、pending tool、chart、gate、failed node），`selectConversation/selectCharts/selectLog` 期望 snapshot 写死；`sort(T)`/`reverse(T)` 同 snapshot（D7）。
- **真后端**：opencode+deepseek 开 `--thinking` 跑真 workflow（mxint/tsquant）。
- **真浏览器**（Playwright）逐屏 DOM/视觉断言（含折叠展开交互、▎ 消失、chart 渲染、gate 模态）。
- **单元**：RAF 批处理（多 session 粒度）、`useElapsedTick` snap、工具配对 fold、reconnect resume。
- **删除验证**：§8 grep AC。

---

## 11. 实施顺序

1. **B1/B2 后端**（shell 无关，硬前置）：translator lossless + `--thinking`/`--variant` + fixture 扩（含 reasoning capture）+ **EventType grep 审计**（闭 review #12：grep 所有 EventType match/switch，新类型加 arm 或确认 default-no-op，每消费者加回归）→ 独立 commit。
2. **codegen（D1）**：event.py → events.ts 生成 + CI grep。
3. **前端重写**：§8 删旧 → 单 store + selector 骨架（§10 fixture 单测**硬依赖 step1+step2 完成**）→ 各 view → markdown/流式/折叠 → 样式对齐 AH。
4. **test agent 逐屏** → 不达标打回 clean-code → 循环至全绿。

---

## 前置阅读
- [`phase-3-events.md`](phase-3-events.md) §3（单 tape + 幂等 reducer）、[`shells-design-draft.md`](shells-design-draft.md)、[`2026-07-05-reference-repos-borrow.md`](../plans/2026-07-05-reference-repos-borrow.md)（F3/F5）。
- **AH 参考**（只读，抄设计不抄结构）：`frontend/src/components/conversation/{MarkdownText,AgentMessage,ToolCallMessage,ToolCallGroup,ThinkingBlock,DiffView,FileContentView}.tsx` + `output/{ChartWidget,ChartGroup,charts/chartTheme}.tsx` + `stores/conversationStore.ts`（**仅 `_textBuf`+`_rafSeq` 机制，弃多 store**）。

# Orca — 开发计划与决策固化

> claude code 子进程为后端的 workflow 编排框架。
> 本文件是所有架构讨论的最终决策记录（ADR），开发时以此为准。

---

## 0. 项目定位（一句话）

**vendor-neutral、event-sourced、可视化的 coding-agent 编排控制平面。**

不是"又一个 claude code 编排器"。核心差异化三件套：
1. **跨工具**：一个 DAG 里混用 claude/codex/opencode，typed handoff（Anthropic 永远不会原生做）
2. **事件 tape**：streaming = replay = read tape，时间旅行调试
3. **hooks 桥**：实时观察 + 控制 claude 的工具调用循环（竞品都是单向 fire-and-forget）

---

## 1. 架构分层与依赖铁律

```
①描述 →  model    （纯数据：Workflow/Node/Event）
②结构 →  compile  （YAML → DAG，纯解析零运行时）
③执行 →  run      （编排：单指针推进/parallel 组/路由，后端无关）
③'执行 → exec     （执行：claude/ccr/codex，可扩展）
④事件 →  events   （EventBus + tape，唯一真相源）
⑤交互 →  gates    （暂停+决策：human/interrupt，作为 extension）
        mcp      （工具供给 claude）
        iface    （cli/web/replay，被动消费 event）
```

**依赖铁律**：
- `model` 最底层，零依赖（纯 dataclass）
- `run` 调 `exec`（通过 Executor 接口）和 `events`，`exec` 不知道 `run` 存在
- `exec` 实现只依赖 `model` + `events`，绝不依赖 `run`
- `iface` 依赖 `events` + `model`，被动消费，绝不反向调用
- **禁止任何反向依赖**

---

## 2. exec 双层抽象（可扩展性 + 鲁棒性）

```
Layer 1: Executor 接口（后端无关）
  async def exec(node, context) -> AsyncIterator[Event]
  （单一接口，不拆 prep/exec/post）

Layer 2: 每个实现 = CLIRunner（通用子进程，共用） + Translator（纯函数）
  ClaudeExecutor  = CLIRunner + ClaudeTranslator
  CodexExecutor   = CLIRunner + CodexTranslator
  OpenCodeExecutor= CLIRunner + OpenCodeTranslator
```

行业共识：claude/codex/opencode 全是 "headless 子进程 + 每行 JSON 事件流到 stdout" 范式（已查证）。

---

## 3. 多入口架构：核心引擎 + 入口壳（"逻辑相同形式不同"）

**核心原则**：引擎和外界只通过两个接口耦合，入口壳无限扩展零逻辑重复。

```
┌──────────────────────────────────────────────────────────┐
│  入口壳层（Entry Shells）—— 形式不同，都调同一个核心      │
│                                                          │
│   orca run (CLI)    Web UI    claude code 里调 orca     │
│   Rich 渲染         浏览器     (作为 MCP 工具/plugin)    │
└────────┬──────────────┬───────────────┬─────────────────┘
         └──────────────┼───────────────┘
                        ▼
┌──────────────────────────────────────────────────────────┐
│  核心引擎层（Core Engine）—— 逻辑相同，唯一真相源        │
│                                                          │
│   Orchestrator + Executor + EventBus + HumanGateHandler  │
│   引擎不知道"谁在调它"，只通过两个接口和外界交互：        │
│     ① EventBus（出）—— 产出事件，谁订阅都行             │
│     ② HumanGateHandler（入）—— 需要决策时调它，等答案    │
└──────────────────────────────────────────────────────────┘
```

**只要 EventBus 和 HumanGateHandler 两个接口不变**，入口壳可无限扩展（CLI/Web/MCP/plugin），零逻辑重复。

### 3.1 三壳形态对照（详见 [`docs/specs/shells-design-draft.md`](specs/shells-design-draft.md)）

| 维度 | CLI 壳 | Web 壳 | MCP 壳 |
|---|---|---|---|
| **入口** | `orca run wf.yaml` | 浏览器 | Claude Code 对话 |
| **实现** | `orca/iface/cli/` (Rich TUI) | `orca/iface/web/` (FastAPI+WS+SPA) | `orca/iface/mcp/` (MCP server, stdio JSON-RPC) |
| **gate UX** | 同步 `input()` 阻塞 | WS 推前端 → 弹窗 | **HandleId pattern**（start/status/resolve） |
| **角色** | 开发期主交互面 | gate UX 主战场 + tape replay | 便捷触发入口（不主交互） |
| **阶段** | phase 7 | phase 9 | phase 10 |
| **依赖前置** | engine + gates（5-6） | engine + gates（5-6） | engine + gates（5-6）+ CLI 或 Web 已能用 |

### 3.2 MCP 壳的协议约束（CC 客观事实，2026-06-30 核实）

- **CC 对 MCP tool call 有 60s 硬超时**（`DEFAULT_REQUEST_TIMEOUT_MSEC = 60000`，[issue #52137](https://github.com/anthropics/claude-code/issues/52137)）→ **长轮询 `wait_for_event` 不可行**。
- **MCP elicitation 在 CC 未支持**（spec 2025-06-18 标准化了，CC feature request 阶段，[issue #7108](https://github.com/anthropics/claude-code/issues/7108)）→ server **不能** mid-tool-call 反向问用户。
- **Claude 不"监控"MCP server**（JSON-RPC，Claude 只在 tool call 时主动）→ server 不能 push 给 Claude，只能被查询。
- **Claude Code 没有内置 polling/wait 工具** → `start_workflow / get_task_status / resolve_gate` 全部由 Orca MCP server 暴露，靠 tool description + `_hint` 引导 chain 调用。

→ 结论：MCP 壳必须用 **HandleId pattern**（Call-Now, Fetch-Later）。详见 shells-design-draft §5。

### 3.3 三壳定位（重要认知）

- **CLI/Web 是主战场**，MCP 是"用户已在 Claude Code 里，顺手触发一个 Orca workflow"的便捷入口。
- **不要把 Orca 的 DAG 可视化塞进 Claude 对话流**（MCP progress token 表达不了；Claude Agent View 面板接入架构上不可能，见 §11.2）。
- **三壳都跑同一个引擎**，差异只在"事件怎么渲染给人"和"决策怎么从人喂回引擎"。

---

## 4. HMIL（Human-in-the-Loop）—— 统一原语 HumanGate

### 实测铁证（2026-06-29）

PreToolUse hook 在 `claude -p` headless 下**完全能同步阻塞**：
```
claude -p 想调 Bash → spawn hook → hook 收 stdin JSON → hook sleep 15s
→ claude 整整等了 15s 什么都不做 → hook exit 0 → claude 继续
```
hook stdin 含完整决策上下文：`{session_id, tool_name, tool_input, tool_use_id, permission_mode}`。

### 两个决策来源，统一汇入 HumanGate

```
来源①：工具权限决策（PreToolUse hook 管）
  "claude 想调 Bash 删文件，允许吗？"
  → hook 收 stdin → 问 Orca → 人决策 → exit 0/2

来源②：agent 主动问（ask_user 工具管）
  "我需要用户提供数据库连接串"
  → agent 调 MCP ask_user → 触发 HumanGate
```

**两者本质同源**（暂停 + 等人决策 + 继续），统一为 `HumanGate` 原语。这正是"审批和 hook 很像"的本质。

### HumanGate 原语（gates/ extension，不进核心编排循环）

```python
@dataclass
class HumanGate:
    id: str
    prompt: str                 # 给人看的问题
    options: list[str] | None   # 选项（或自由文本）
    context: dict               # 哪个 node / 哪个工具 / 什么参数
    source: Literal["tool_permission", "agent_ask"]

class HumanGateHandler:
    async def request(self, gate: HumanGate) -> str:
        """统一入口：emit 事件 + 暂停 + 等答案"""
        future = asyncio.Future()
        self._pending[gate.id] = future
        self._bus.emit("human_decision_requested", gate)
        return await future     # 任一壳 resolve 唤醒

    def resolve(self, gate_id: str, answer: str):
        self._pending[gate_id].set_result(answer)
```

### 三通道竞速（学 Conductor gates/human.py）

三个壳收到 `human_decision_requested` 事件后各自渲染：
- **CLI 壳**：Rich 同步 input()
- **Web 壳**：WebSocket 推前端 → 弹窗
- **MCP 壳**：**HandleId pattern**——`get_task_status` 返回 `needs_decision` 状态 + gate 详情，Claude 转述给用户，用户答后 Claude 调 `resolve_gate` tool。详见 [`shells-design-draft.md`](specs/shells-design-draft.md) §5。

任一壳调 `resolve()`。`asyncio.wait(FIRST_COMPLETED)`，**谁先答谁赢**——避免"开着 web 但人在 claude code 里答"的冲突。输家收到"已被别的壳答"信号（CLI: prompt 中断；Web: 弹窗自动关闭；MCP: `resolve_gate` 返回 `ok: false` 或 `get_task_status` 跳过 needs_decision）。

> ⚠️ **MCP 壳不使用长轮询 `wait_for_event`**（CC 60s 硬超时，§3.2 / §11.12）；也不使用 elicitation（CC 未支持，§11.13）。HandleId pattern 是唯一可行方案。

### hook → Orca 通信：HTTP

hook 是 claude spawn 的独立短命进程，用 **HTTP** 把"需要决策"送到 Orca + 阻塞等答案：
- hook 收 stdin → POST /gate 到 Orca server → 阻塞等响应 → exit code 由响应决定
- Orca 本来就起 Web server，HTTP hook 天然复用
- Claude Code 官方支持 HTTP hooks（POST 到端点）

---

## 5. claude 执行模式：双模式

| 模式 | 命令 | 特点 | 适用 |
|---|---|---|---|
| **fire-and-forget** | `claude -p` | 一次性、跑完即退 | 默认、批处理、CI、简单 workflow |
| **持久会话** | `claude --bg` | supervisor 托管、状态持久化 state.json、可 attach/peek/stop | HMIL 重场景、长跑、断点续跑 |

**第一期先做 `-p` + hook HMIL，`--bg` 留第二期。** Agent View 用的是 `--bg` 模型，但接入 Agent View 面板本身**架构上不可能**（封闭面板，只显示 claude 自己 dispatch 的会话，无第三方注入 API）。

---

## 6. workflow 引擎设计

| 维度 | 决策 | 依据 |
|---|---|---|
| 路由 | routes 列表 + 单一 Jinja2 表达式 + first-match-wins | Conductor 验证，去掉双引擎 wart |
| 完成判定 | 同步 await executor.exec()，流式 emit 但路由在完成后判 | Conductor 模型 |
| 数据流 | accumulate 模式 + Jinja2 `{{ agent.output.field }}` | 只保留一种模式 |
| 并行 | `parallel:` + asyncio.gather + 三态失败模式 | 无 LangGraph |
| 循环 | `for_each:` + 分批 gather | 唯一 loop 机制 |
| 状态 | 纯 dataclass（RunState），显式不外包 | 摒弃 LangGraph HarnessState dict |

**核心哲学**：编排逻辑自己写（轻量、显式、可调试），执行智能外包给 claude code，事件流是唯一真相源。

**Session 身份**：每次 agent 调用（retry / for_each / parallel）产生独立 `session_id`（独立 context）——这是 Orca 比 Conductor（共享 context + 计数器）更解耦之处。事件按 session_id 分组，前端按 session 懒加载；attempt（重试序号）派生不入库。run_id 用 composite（`<slug>-<ts>-<nanoid6>`）即历史名。详见 phase-3 SPEC §3.5。

---

## 7. 权限模型（修正：去 skip-permissions）

不用 `--dangerously-skip-permissions`（危险 + CCR/托管环境失败点）。用：
- `--allowedTools "Bash,Read,Edit,..."` 白名单（按 agent.tools 精确授权）
- `--permission-mode auto`（Claude 按任务自选）
- `--bare`（服务器确定性，跳过 hooks/skills/MCP 自动发现）
- **HMIL 工具权限通过 PreToolUse hook 实现**（实测验证）

---

## 8. 参考来源对照（留/借/丢）

### 🟢 从 AgentHarness 迁移（已验证资产）
| 资产 | 处理 |
|---|---|
| `_cli_subprocess.py` (CLIRunner) | 直接迁移 |
| `translator/stream_json.py` (ClaudeTranslator) | 直接迁移 |
| `cli_profile.py` (多后端抽象) | 迁移，降级到 exec/claude/ |
| `run_store.py` (持久化) | 迁移，改造成 tape |
| `DAGPreview.tsx` + `DAGPreviewNode.tsx` | 直接迁移（前端视觉资产）|
| `routeEvent` reducer | 迁移，简化 |
| `mcp/server.py` + `proxy.py` | 迁移 |
| `render_chart` 工具 | 迁移 |
| `ask_user` 的 emit+register+wait+resolve 模式 | 抽象成 HumanGate |
| `eval/` EvalJudge (LLM 评判 + 重试循环) | 迁移核心 ~50 行 |
| `tui/` MainPanel + SidebarPanel | 迁移 ~600 行渲染器 |
| `base.py` 扩展契约 (observe/mutate/rewrite-graph) | 迁移 ~150 行 |
| `envelope.py` 预算控制 | 迁移（claude 跨 node 不做预算）|

### 🔵 从 Conductor / 其他项目借鉴设计
| 设计 | 来源 |
|---|---|
| 单 tape 唯一真相源 | Conductor events.py |
| 确定性路由 (Jinja2 first-match) | Conductor engine/router.py |
| WebSocket 单通道 | Conductor web/server.py |
| Executor ABC | Conductor providers/base.py |
| 能力声明 (ProviderCapabilities) | Conductor capabilities.py |
| 双 future 竞速 gate | Conductor gates/human.py |
| plugin 打包 | Conductor plugins/ |
| 事件溯源 replay | OpenHands event log |
| agent-as-node + skill 编译 | skillfold (YAML→SKILL.md) |
| typed-artifact-flow | tutti |
| AI-legible 极简 | PocketFlow |
| durable execution 语义 | Temporal/Restate |
| asset-lineage | Dagster |

### 🔴 丢弃
LangGraph 全部、pydantic-ai 全部、双 store/4 replay、node_factory 巨石、Conductor workflow.py 5846 行 god-class、根目录 scratch 文件、`compact/`（claude 自己 auto-compact）、`memory/`（用 CLAUDE.md）、`cache/`（claude 有原生 prompt caching）、`guardrail/`（错误层）、`output_compactor`（无效）、`plugins/` 5/6（重新派生事件）。

---

## 9. AgentHarness 扩展裁决（哪些值得做）

裁决标准：**claude 子进程已原生提供的能力 → DROP；DAG 拓扑/多进程边界 claude 看不到的 → KEEP。**

| 扩展 | 裁决 | 理由 |
|---|---|---|
| `compact/` | 🔴 DROP | claude 自己有 auto-compact |
| `memory/` | 🔴 DROP | 用 claude 的 CLAUDE.md |
| `cache/` | 🔴 DROP | claude 有原生 prompt caching |
| `guardrail/` | 🔴 DROP | 防注入在 orchestrator 层是错的 |
| `output_compactor` | 🔴 DROP | 只对小 MCP 工具有效 |
| `plugins/` 6个 | 🔴 DROP 5 | 重新派生事件/正则垃圾/废弃 |
| `eval/` EvalJudge | 🟢 KEEP | **最有价值**，跨 node 评判 + 重试，claude 没有 |
| `tui/` 面板 | 🟡 REBUILD | MainPanel+SidebarPanel 干净，~600 行 |
| `base.py` 扩展契约 | 🟡 REBUILD | 三类扩展抽象对，~150 行 |
| `envelope.py` 预算 | 🟢 KEEP | claude 跨 node 不做预算，已实现 80% |
| `approval/` HMIL 审批 | 🟢 后期 | headless 下 claude 权限提示不触发，真实缺口 |

**原则**：凡是 DAG 拓扑或多进程边界上 claude 看不到的，才值得做。

---

## 10. 开发阶段（按 2026-06-30 生态调研后重排）

> **重排原则**：① 先打通"端到端跑 workflow + HMIL"（phase 4-7，关键路径）；
> ② 跨工具护城河优先于 UI 扩展（phase 8 在 Web/MCP 前，早验证护城河）；
> ③ MCP 是便捷触发入口不是主战场（phase 10，依赖 Web 已能用）。
>
> **phase 编号对齐实际**：原 §10 的 0-7 编号已废（与各 SPEC 的 phase-N-name.md 不一致）。
> 现以实际 SPEC 编号为准：phase 1 schema / phase 2 compile / phase 3 events / phase 4 exec / ...
>
> **入口壳相关 phase（7/9/10）开工前必读**：[`docs/specs/shells-design-draft.md`](specs/shells-design-draft.md)。

### ✓ 已完成

- **phase 1**：schema/（纯数据模型）—— commit `d69c47c`
- **phase 2**：compile/（YAML→DAG + 两层校验）—— commit `5b5ba06`
- **phase 3**：events/ + profiles/ + capability 校验闭环 —— commit `1b86019`

### → 进行中

- **phase 4**：exec/（单 node 执行内核，仅 claude -p）
  - SPEC：[`phase-4-exec.md`](specs/phase-4-exec.md)
  - 双层抽象（Executor + CLIRunner/Translator），三层叶子 kind（agent/script/set）
  - 5 步拆解（A 契约 → B 共享基础设施 → C ClaudeExecutor → D script/set → E 集成）

### 🎯 关键路径（phase 5-7：端到端可用）

- **phase 5**：run/（编排层）
  - Orchestrator（单指针推进 / parallel 组并行 asyncio.gather / foreach 分批 / 循环控制）
  - Router（Jinja2 first-match-wins，确定性条件）
  - EventBus 写 tape（每个事件 emit 后 append）
  - 验证：`orca run nas.yaml` 端到端跑完整 workflow，事件全部入 tape

- **phase 6**：gates/（HMIL 统一原语 HumanGate）
  - HumanGateHandler（emit + register + wait + resolve）
  - PreToolUse hook（HTTP 桥：hook → POST /gate → 阻塞等响应 → exit code）
  - ask_user MCP 工具（agent 主动问，第二个 decision 来源）
  - **三通道竞速**（CLI/Web/MCP，`asyncio.wait(FIRST_COMPLETED)`）—— 详见 [`shells-design-draft.md`](specs/shells-design-draft.md) §6
  - 验证：claude 调危险工具 → hook 拦 → 任一壳答 → claude 继续

- **phase 7**：CLI 壳（`iface/cli/`）⭐ 第一个端到端入口
  - **开工前必读**：[`shells-design-draft.md`](specs/shells-design-draft.md) §3
  - Rich TUI（DAG 进度 + log 流 + gate prompt）
  - gate = 同步 `input()` 阻塞
  - 验证：`orca run nas.yaml` 完整 user journey（含 HMIL gate）
  - **里程碑**：phase 7 完成后 Orca 已是可用工具（单 backend + 单 shell）

### 🛡️ 护城河（phase 8：跨工具——必须在 shells 之前）

> **为什么 phase 8 在 Web/MCP 之前**：① phase 4 的 CliProfile+Translator 抽象刚验证完，加新 backend 是机械活；
> ② 这是 vs Claude 原生 dynamic workflows 的**结构性差异**（Anthropic 永远不会编排 codex/opencode）；
> ③ 早验证早知道，免得 Web/MCP 都做完了才发现护城河不成立。

- **phase 8**：跨工具 executor（codex / opencode）
  - CodexExecutor = CLIRunner + CodexTranslator（共享 phase 4 抽象）
  - OpenCodeExecutor = CLIRunner + OpenCodeTranslator
  - profile 系统已在 phase 3 就位（`get_profile(name)`），加 backend = 加 translator + 加 profile
  - typed handoff（一个 DAG 里混用 claude/codex/opencode）—— **Anthropic 原生结构性不会做**
  - 验证：一个 workflow 里 `plan(claude) → code(codex) → review(claude)` 跑通

### 🎨 入口扩展（phase 9-10：phase 6 后可并行）

- **phase 9**：Web 壳（`iface/web/`）⭐ gate UX 主战场
  - **开工前必读**：[`shells-design-draft.md`](specs/shells-design-draft.md) §4
  - FastAPI + WebSocket 单通道 + SPA（React + 单 store + routeEvent reducer）
  - gate = WS 推前端弹窗（富交互：选项/自由文本/上下文/取消）
  - tape replay UI（时间旅行调试，**Orca 独有**）
  - **必须避免**：多 store / 非幂等 reducer / 多 sidecar（AgentHarness 教训）
  - 验证：浏览器看 DAG 进度 + gate 弹窗 + 历史 run replay

- **phase 10**：MCP 壳（`iface/mcp/`）—— 便捷触发入口
  - **开工前必读**：[`shells-design-draft.md`](specs/shells-design-draft.md) §5（**最棘手，必读**）
  - **HandleId pattern 三工具**：`start_workflow` / `get_task_status` / `resolve_gate`（秒级返回，无 60s 超时风险）
  - 引导 Claude chain 调用：tool description 强指令 + 返回值 `_hint`
  - **不**做长轮询 `wait_for_event`（CC 60s 超时）；**不**做 elicitation（CC 未支持）
  - gate 主交互面留给 Web；MCP 仅作"用户在 Claude Code 里顺手触发"入口
  - 验证：Claude Code 里 `start_workflow` → chain `get_task_status` → gate 在 Web 答 → 完成

### 📦 发行（phase 11）

- **phase 11**：生态化
  - plugin 打包（subagents + skills + MCP 一键装，参考 Conductor plugins/）
  - workflow registry
  - PyPI 发包（注意 `orca` 名被 stablyai 占用，需改名 —— §11.8）

---

## 11. 关键技术结论备忘

1. **`claude --bg` 是真实的一等公民**（纠正之前"放弃 --bg"的错误）。Agent View 用的是 supervisor 托管的完整进程 + state.json 持久化 + attach/peek/stop。第一期用 `-p`，第二期做 `--bg`。
2. **Agent View 面板接入架构上不可能**（封闭，只显示 claude 自己 dispatch 的会话，无第三方注入 API）——不追求。
3. **hook 在 `-p` 下能同步阻塞**（实测铁证），是 HMIL 的完美接入点。
4. **claude SDK 也支持程序化 hook**（进程内 Python 回调，非 shell），但 `-p` 子进程路线用 shell hook 更契合。
5. **HMIL 两种来源（工具权限/agent 主动问）本质同源**，统一 HumanGate 原语。
6. **Conductor 用双 future 竞速 gate**（CLI/Web），Orca 扩展到三通道竞速（CLI/Web/MCP）。
7. **多入口"逻辑相同形式不同"**：引擎只通过 EventBus + HumanGateHandler 两个接口耦合外界，入口壳零逻辑重复。
8. **PyPI `orca` 已被占用**（stablyai/orca 同领域），未来发包需改名；本地开发用 orca 无碍。
9. **session_id 是 agent 调用的身份原语**（不是 iteration 计数）：每次 agent 调用（retry/for_each/parallel）一个 session_id，独立 context；attempt 派生。比 AgentHarness 的 `+iters+<node>+<n>` sidecar（反模式②投影分裂）更解耦——session 是「分区」不是「投影」，判据：事件不重复写。
10. **run_id 用 composite**（`<workflow_slug>-<YYYYMMDDHHMMSS UTC>-<nanoid6>`）即历史名，单 tape 无 manifest；`ls runs/` 可读。

> **以下 11-18 条为 2026-06-30 生态调研讨论后追加**（详见 [`shells-design-draft.md`](specs/shells-design-draft.md) §7）。

11. **三壳在 phase 6 完成后相互独立可并行**（CLI/Web/MCP 互不阻塞）—— TASK.md §3 已保证引擎/壳解耦。
12. **CC 对 MCP tool call 有 60s 硬超时**（`DEFAULT_REQUEST_TIMEOUT_MSEC = 60000`，[anthropics/claude-code#52137](https://github.com/anthropics/claude-code/issues/52137)）—— **长轮询 `wait_for_event` 不可行**，MCP 壳必须用 HandleId pattern。
13. **MCP elicitation 在 CC 未支持**（spec 2025-06-18 标准化了，CC feature request 阶段，[anthropics/claude-code#7108](https://github.com/anthropics/claude-code/issues/7108)）—— server 不能 mid-tool-call 反向问用户；即使支持，长 workflow 多 gate 弹窗体验崩坏。
14. **HandleId / Call-Now-Fetch-Later 是业界标准** —— [Temporal MCP+HITL](https://learn.temporal.io/tutorials/ai/building-mcp-tools-with-temporal/adding-hitl-to-mcp-tools/) / [WorkOS MCP Async Tasks](https://workos.com/blog/mcp-async-tasks-ai-agent-workflows) / [Agnost](https://agnost.ai/blog/long-running-tasks-mcp) / [DEV.to](https://dev.to/aws/fix-mcp-timeouts-async-handleid-pattern-8ek) 一致；Orca MCP 壳采用此模式（start 立即返回 task_id + get_task_status 秒级轮询）。
15. **SEP-1686 (Tasks) 是 MCP 长任务的未来标准化方向**，CC 未实现；当前 Orca 自实现 HandleId，未来 SEP-1686 落地后或迁移。
16. **Conductor 不解决 MCP gate 问题**（它是 standalone CLI = path A，不是 MCP server）—— 它只教 path A 双壳竞速（§4 已吸收），不教 MCP gate；MCP+HITL 真正的参考是 Temporal tutorial。
17. **Claude 原生 Dynamic Workflows（2026/06 research preview）是真正威胁** —— 单 claude 后端编排会被原生吃掉。**跨工具（phase 8）+ event tape + hooks 桥是结构性护城河**，必须做实。
18. **Claude Code 没有内置 polling/wait 工具** —— `start_workflow / get_task_status / resolve_gate` 全部由 Orca MCP server 暴露；引导 Claude chain 调用靠 tool description 强指令 + 返回值 `_hint`。

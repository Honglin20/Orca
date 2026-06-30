# 入口壳设计（CLI / Web / MCP）

> **状态**：定稿（2026-06-30 收敛）—— 进入 phase 7 / 9 / 10 **前必读**，读完再写对应阶段的完整 SPEC。
> **依据**：[TASK.md](../TASK.md) §3 §4 + 2026-06-30 生态调研（CC 协议约束 / HandleId pattern / 竞品对照 / Conductor 前端范式 / TUI 框架选型）。
> **范围**：三壳的形态、共同契约、关键技术约束、端到端 user journey。
> **不是**：最终 SPEC。接口签名 / 数据契约 / 验收标准由对应 phase 的 SPEC 文档落实（如 `phase-7-cli.md`、`phase-9-web.md`、`phase-10-mcp.md`），本设计只给设计骨架 + 决策依据。

> **2026-06-30 定稿收敛的决策**（替换原草稿的开放项）：
> 1. **三通道竞速取消语义 = 广播**（详见 §6.3，已锁定）。
> 2. **hook HTTP 桥 = 安全优先，超时/不可达即拒绝**（详见 phase-6 SPEC §hook）。
> 3. **CLI 壳技术栈 = Textual**（不是 Rich Live，详见 §3，gate prompt 是硬约束）。
> 4. **Web 壳技术栈 = React+Vite+ReactFlow+Zustand**（Conductor 实际验证，详见 §4）。
> 5. **tape replay = 单路径 fold（apply_event），不是双路径**（详见 §4.3 + phase-3 SPEC §6.0 铁律 3）。
> 6. **唯一真相源铁律**：tape 是唯一真相，三壳无自己的业务真相，UI 只是真相的推送（详见 §4.3）。

---

## 0. 草稿定位（写给未来写 SPEC 的人）

- **必读时机**：phase 7（CLI 壳）/ phase 9（Web 壳）/ phase 10（MCP 壳）开工前。
- **必读顺序**：CLAUDE.md → TASK.md（§3 §4 §10）→ **本草稿** → 写对应 phase 的 SPEC。
- **本草稿解决**：①三壳共同契约；②每壳形态要点；③MCP 壳的协议约束（最棘手）；④三通道竞速机制；⑤端到端验证场景。
- **本草稿不解决**：①具体 API 形状（→ phase SPEC）；②前端组件树（→ phase 9 SPEC）；③MCP 工具的入参出参 JSON schema 细节（→ phase 10 SPEC）。

---

## 1. 共同契约：引擎只通过两个接口和壳耦合

复述 TASK.md §3 的核心原则——**引擎和外界只通过两个接口耦合**：

```
┌──────────────────────────────────────────────────────────┐
│  核心引擎层（Orchestrator + Executor + EventBus +         │
│              HumanGateHandler）                          │
│                                                          │
│  ① EventBus（出）—— 产出事件流，谁订阅都行              │
│  ② HumanGateHandler（入）—— 需要决策时调它，等答案       │
└──────────────────────────────────────────────────────────┘
              ↑ ②                    ↓ ①
       resolve(decision)        subscribe(events)
              │                          │
   ┌──────────┴──────────┐    ┌──────────┴──────────┐
   │   三壳各自渲染       │    │  三壳各自订阅       │
   │   ─────────────      │    │                     │
   │   CLI: Rich input()  │    │  CLI: Rich 渲染     │
   │   Web: WS 弹窗       │    │  Web: WS 推 SPA     │
   │   MCP: HandleId      │    │  MCP: 写状态机      │
   └─────────────────────┘    └─────────────────────┘
```

**只要这两个接口稳定**，三壳可独立开发（phase 7/9/10 互相不阻塞），零逻辑重复。

### 1.1 EventBus（出，壳订阅）

壳订阅事件流（如 `node_started` / `agent_message` / `human_decision_requested` / `node_completed` / `run_completed`）。事件结构来自 phase 1 schema + phase 3 events，本草稿不重述。

### 1.2 HumanGateHandler（入，壳 resolve）

```python
class HumanGateHandler:
    async def request(self, gate: HumanGate) -> str:
        """emit human_decision_requested + 暂停 + 等任一壳 resolve。"""
    def resolve(self, gate_id: str, answer: str, source: str) -> bool:
        """任一壳调它喂答案。返回是否是赢家（FIRST_COMPLETED）。"""
```

三壳竞速详见 §6。

---

## 2. 三壳总览

| 维度 | CLI 壳 | Web 壳 | MCP 壳 |
|---|---|---|---|
| **入口** | `orca run wf.yaml` | 浏览器 | Claude Code 对话 |
| **实现位置** | `orca/iface/cli/` | `orca/iface/web/` | `orca/iface/mcp/` |
| **技术栈** | Rich TUI | FastAPI + WebSocket + SPA | MCP server (stdio JSON-RPC) |
| **gate UX** | 同步 `input()` 阻塞 | WS 推前端 → 弹窗 | **HandleId pattern**（start/status/resolve） |
| **生命周期** | 一次性（跑完退出） | 长跑 server（多人/多 run） | 长跑 daemon（CC session 内常驻） |
| **角色** | 开发期主交互面 | gate UX 主战场 + tape replay UI | 便捷触发入口（不主交互） |
| **阶段** | phase 7 | phase 9 | phase 10 |
| **依赖前置** | engine + gates（5-6） | engine + gates（5-6） | engine + gates（5-6）+ 至少 CLI 或 Web 已能用 |

**关键认知**（来自 2026-06-30 调研）：
- **CLI/Web 是主战场**，MCP 是"用户已经在 Claude Code 里，想顺手触发一个 Orca workflow"的便捷入口。
- **不要试图把 Orca 的 DAG 可视化塞进 Claude 对话流**（MCP progress token 表达不了；Claude Agent View 面板接入架构上不可能，TASK.md §11.2）。
- **三壳都跑同一个引擎**，差异只在"事件怎么渲染给人"和"决策怎么从人喂回引擎"。

---

## 3. CLI 壳（phase 7）

### 3.1 技术栈决策：Textual（不是 Rich Live）

**决策**：CLI 壳用 **Textual**（基于 Rich 的完整 app 框架），不是 Rich Live。

**理由（2026-06-30 调研，硬证据）**：CLI 壳需求三件套——①DAG 节点状态面板 ②实时滚动日志流 ③**阻塞式 gate prompt**。Rich Live 能做 ①②，但 **③是 Rich 的硬限制**：Rich 官方确认 Live 渲染期间无法接收输入（[Discussion #1791](https://github.com/Textualize/rich/discussions/1791)）。Textual 的 `ModalScreen` + `push_screen_wait` 原生支持「DAG 在跑 + 中央弹出 gate 模态 + 阻塞等答案 + 背景不冻结」，正好覆盖三件套。同作者（Will McGugan），Textual 基于 Rich，渲染同样漂亮。

### 3.2 布局（融合 claude agent view + Dagger + lazygit）

**主屏（DAG 在跑）**：
```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Orca Run #42 · nas · sonnet · 3/7 nodes · ⏸ 2 awaiting gate                   │ Header
├────────────────┬─────────────────────────────────────────────────────────────┤
│ DAG OUTLINE    │ ACTIVE NODE: research                                        │
│ (左侧 Tree)     │ ───────────────────────────────────────────                  │
│ ✓ fetch        │ ┃ researcher_a  ✽ working 12s  [claude]                      │
│ ✓ parse        │ │  ⠋ searching "rich layout"                                 │
│ ◐ research     │ ┃ researcher_b  ✽ working 12s  [claude]                      │
│ ⏸ review       │ │  ✓ found 8 results                                          │
│ ○ test         │ ◐ parallel group: 1/2 done                                   │
│ ○ deploy       ├─────────────────────────────────────────────────────────────┤
│                │ LOG STREAM (RichLog 自动滚动)                                │
│ ⏸=blocked ✽=run│ 14:02:11 [r_a] tool: WebSearch("rich …")                    │
│ ✓=done ○=wait  │ 14:02:12 [r_a] → 8 results                                  │
├────────────────┴─────────────────────────────────────────────────────────────┤
│ > <派发新任务 / ! shell / g 跳到 gate>           ↑↓选 Space peek Enter attach │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Gate 触发时（ModalScreen 覆盖中央，DAG 继续跑）**：
```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Orca Run #42 · ... · ⏸ 2 awaiting gate                            ░░░░░░░░░░ │
├────────────────┬────────────────────────────────────░──────────────────────┤
│ ⏸ review       │                     ░ ┌──────────────────────────────┐  │
│                │  LOG STREAM ...     ░ │  🔒 GATE: review               │  │
│                │  14:02:20 [review]  ░ │  Claude wants to run:         │  │
│                │  needs input       ░ │  Bash("rm -rf node_modules")  │  │
│                │                     ░ │   [批准]  [拒绝]  [编辑]      │  │
├────────────────┴─────────────────────░─└──────────────────────────────╝─┤
│ > ...                                                  ░░░░░░░░░░░░░░░░░░ │
└──────────────────────────────────────────────────────────────────────────────┘
```

布局来源：Header/awaiting 计数（仿 claude agent view tab 标题）+ 左侧 DAG Tree 状态图标（仿 agent view 行状态）+ 右上 Active Node 行摘要 + 并行子 agent 进度列（仿 Dagger）+ 右下 RichLog + 底部 input/footer（仿 lazygit 三段式）+ Gate ModalScreen。

### 3.3 设计要点

- **gate = ModalScreen**：HumanGateHandler.request() 在 CLI 壳里 `await push_screen_wait(GateModal)` 阻塞当前 worker，用户答后 `resolve()`。DAG/日志继续刷新（Textual 决定性优势）。
- **编排主流程 = `@work` 协程**：pipeline 写成顺序代码，到 gate 节点 `await push_screen_wait`，UI 全程不冻结。
- **事件流渲染**：左侧 DAG Tree 状态编码 + 右下 RichLog 滚动渲染 agent_message/tool_call/tool_result。
- **退出语义**：workflow 终态（completed/failed）后 CLI 退出，exit code 反映成功失败。
- **tape 同步落盘**：每个事件 emit 后 tape append（phase 3 已实现），CLI 退出时 tape 已完整。
- **HMIL 三通道竞速**：CLI 壳参与（用户在终端答）。

### 3.4 边界

- ❌ 不做时间旅行 replay（CLI 是一次性，要看历史走 Web）。
- ❌ 不做多 run 并发（CLI 一次只跑一个 workflow；多 run 走 Web）。

---

## 4. Web 壳（phase 9）

### 4.1 技术栈决策：React + Vite + ReactFlow + Zustand（2026-06-30 定稿）

**决策**：Web 壳后端 **FastAPI + uvicorn**（单进程同引擎 asyncio 循环），前端 **React 19 + Vite + ReactFlow（DAG 渲染）+ dagre（自动布局）+ Zustand（单 store）+ Tailwind**，图表用 **recharts**（render_chart）。

**理由（2026-06-30 调研 Conductor 实际实现）**：
1. **Conductor 实际用的就是这套**（不是它过时 design.md 说的「单 HTML+Cytoscape」）——ReactFlow+dagre 是 DAG 可视化成熟方案，Conductor 还解决了回环边反向喂图的难题（直接抄）。
2. **Zustand 单 store + eventHandlers 表**就是事件溯源前端范式，契合唯一真相源（§4.3）。
3. **后端单进程 uvicorn**（同引擎事件循环），零 IPC、零序列化开销（Conductor 设计决策 D4，验证过）。
4. **图表库 recharts**：轻量，React 生态最简单，做 render_chart。

> Go/Rust 后端对 Orca **无参考价值**——单个编排器事件量小（几百到几千），Python asyncio 足够；用 Go/Rust 要重写 exec/CLIRunner + 跨语言 IPC + 失去 Python 生态。Orca 全栈 Python + 前端 React 独立是正确选择。

### 4.2 形态

浏览器打开 `http://localhost:7428`：

```
┌──────────────┬────────────────────────────────────┬──────────────┐
│ Runs         │ ┌─── DAG Preview (ReactFlow) ───┐ │ Log Stream   │
│ • deploy-abc │ │   plan → evaluator → deploy    │ │ agent_msg... │
│ • review-def │ │   ⏸ deploy (gate)              │ │ tool_call... │
│ (history)    │ └────────────────────────────────┘ │              │
│              │ ┌─── Replay Bar (历史 run) ──────┐ │              │
│              │ │ ◄ ▶ ████░░░░░░ Event 12/47  5x│ │              │
│              │ └────────────────────────────────┘ │              │
│              │ ┌─── Gate 弹窗 ──────┐            │              │
│              │ │ 批准部署到 staging? │            │              │
│              │ │ [Yes] [No] [Edit]  │            │              │
│              │ └────────────────────┘            │              │
│              │ ┌─── Chart (render_chart) ──────┐ │              │
│              │ │ ▆▆▆ token cost over nodes     │ │              │
│              │ └────────────────────────────────┘ │              │
└──────────────┴────────────────────────────────────┴──────────────┘
```

### 4.3 唯一真相源铁律（2026-06-30 定稿，最重要）

**Tape 是唯一真相源。三壳无自己的业务真相，UI 只是真相的推送。** 这是根治 AgentHarness 多 store 投影分裂灾难的根本。

**5 条具体铁律**（phase 9 SPEC 必须逐条验收）：
1. **Tape 唯一真相**：所有 UI 状态都是 tape 的 fold 派生物（Conductor 验证：前端 handler 表，派生状态随时可重建）。
2. **前端无业务真相**：前端只有事件 handler 表（fold）+ 临时 UI 交互态（selectedNode 等，不算业务真相）。
3. **重连 = 全量重放**（Conductor 验证最简单正确）：WS 断了重连，`GET /api/state` 拿全量事件 replay，状态必然一致。
4. **gate 状态写 tape**：requested/resolved 都是事件，三壳从同一份 tape 读。
5. **WS 单通道**（反 AgentHarness 双 WS）：所有事件/gate/决策走一条 WS；反向通道同 WS 收 gate_response。

### 4.4 tape replay UI（单路径，不是双路径）

**tape replay = 时间旅行调试**：选历史 run → 拖时间轴 → 回到「第 N 个事件发生时」的状态看 DAG/输出。**它不是新路径，是同一个 fold 的两种数据注入时机**：

- **live**：WS 实时推事件 → `apply_event` fold
- **replay**：HTTP 拿历史事件，按时间戳推进 → `apply_event` fold
- **fold 函数只有一份**（phase 3 `replay.py` 的 `apply_event`），live 和 replay 的状态计算永远一致。

**这是避免 AgentHarness 双路径灾难的关键**：AgentHarness 用两套渲染代码（live 一套 + replay 一套）→ 漂移。Orca 用同一份 `apply_event`（phase 3 SPEC §6.0 铁律 3「一条读路径：streaming = replay = 同一个 apply_event」已锁定）。

**性能优化（避免 Conductor 的缺点）**：Conductor 的 replay 每次拖滑块全量重置+全量重放，长 workflow 卡。Orca 用**增量 apply**（前进 apply N..M，后退 rollback）或 checkpoint 快照。phase 9 SPEC 落实。

### 4.5 render_chart 接入（phase 9 独立 feature）

**机制**：Claude 调 MCP 工具 `render_chart(spec)` → 产出 `custom` 事件（`data.kind="chart"`）→ 写 tape → 前端按 `data.kind` 分发渲染（recharts）。

- schema 已支持：`custom` 事件 + `data.kind: "chart"|"table"|"image"|...`（phase 1 已定义）。
- `render_chart` MCP 工具本身在 phase 10 MCP 壳实现；phase 9 Web 壳**只做前端渲染**（订阅 `custom` 事件，按 kind 分发到 recharts/表格/图片组件）。
- **phase 9 验收**：能渲染一个测试用的 `custom(chart)` 事件（手动注入或 demo workflow 产出）。
- render_chart 工具的完整实现（让 claude 能调）归 phase 10。

### 4.6 设计要点

- **gate UX 主战场**：弹窗可富（选项 / 自由文本 / 上下文 / 取消）。
- **HMIL 三通道竞速**：Web 壳参与（用户在浏览器答）。
- **多 run 并发**：server 模式天然支持。

### 4.7 必须避免的反模式（来自 AgentHarness 教训）

- ❌ 多 store（run store / event store / message store 各一份真相）。
- ❌ 非幂等 reducer（同事件重放结果不一致）。
- ❌ 多 sidecar（每个 store 配一个 sidecar 文件，多真相源漂移）。
- ❌ 双 WS / 双激活管线。
- ❌ live 和 replay 两套渲染代码。
- → **单 tape 唯一真相源 + 幂等 reducer + 一条读路径**（CLAUDE.md 底线）。

---

## 5. MCP 壳（phase 10）⭐ 最棘手，重点设计

### 5.1 协议约束（CC 客观事实，2026-06 核实）

| 约束 | 来源 | 影响 |
|---|---|---|
| **CC 对 MCP tool call 有 60s 硬超时**（`DEFAULT_REQUEST_TIMEOUT_MSEC = 60000`）| [GitHub anthropics/claude-code#52137](https://github.com/anthropics/claude-code/issues/52137) | **长轮询 `wait_for_event(timeout=300)` 不可行**——60s 强 kill |
| **MCP elicitation 在 CC 未支持**（spec 2025-06-18 标准化了，CC 还在 feature request）| [GitHub anthropics/claude-code#7108](https://github.com/anthropics/claude-code/issues/7108) | server 不能 mid-tool-call 反向问用户 |
| **Claude 不"监控"MCP server** | MCP 是 JSON-RPC，Claude 只在 tool call 时主动 | server 不能 push 给 Claude，只能被查询 |
| **Claude 按 tool description 决定调用** | [Claude 平台文档](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools) | description + 返回值 `_hint` 是引导 Claude 的唯一杠杆 |

→ 结论：**必须用 HandleId pattern**（业界标准，[Temporal MCP+HITL tutorial](https://learn.temporal.io/tutorials/ai/building-mcp-tools-with-temporal/adding-hitl-to-mcp-tools/) / [WorkOS MCP Async Tasks](https://workos.com/blog/mcp-async-tasks-ai-agent-workflows) / [DEV.to HandleId Pattern](https://dev.to/aws/fix-mcp-timeouts-async-handleid-pattern-8ek) 全部一致）。

### 5.2 HandleId pattern（Call-Now, Fetch-Later）

```
submit  ──立即返回 task_id──▶  server 后台 async task 跑引擎
                                │
                                ▼
                              引擎 emit 事件 / 触发 gate
                                │
                                ▼
get_status(task_id)  ◀──秒级返回当前状态（不阻塞）
                                │
                                ▼  status=needs_decision
resolve_gate(task_id, gate_id, decision)
                                │
                                ▼
                              引擎 resume → 继续跑
                                │
                                ▼  status=completed
                              返回最终 output
```

每次 tool call **秒级返回**，无超时风险；Claude 在多个 turn / 同一 turn 内多次 chain 调用直到终态。

### 5.3 工具签名（草稿，phase 10 SPEC 细化）

```python
# orca/iface/mcp/server.py

@mcp.tool()
def start_workflow(
    yaml_path: str,
    inputs: dict | None = None,
) -> dict:
    """启动一个 workflow，立即返回 task_id（不阻塞）。
    
    启动后必须调用 get_task_status 轮询直到终态（completed/failed）。
    """
    run_id = orchestrator.start(yaml_path, inputs)
    return {
        "task_id": run_id,
        "status": "running",
        "_hint": "Workflow started in background. "
                 "Call get_task_status(task_id=...) to poll progress.",
    }

@mcp.tool()
def get_task_status(task_id: str) -> dict:
    """查询 task 当前状态。秒级返回，不阻塞。
    
    返回 status: running | needs_decision | completed | failed。
    needs_decision 时含 gate 详情（需调 resolve_gate）；
    completed/failed 时含最终 output / error。
    """
    state = orchestrator.snapshot(task_id)
    return {
        "status": state.status,        # running | needs_decision | completed | failed
        "current_node": state.current,  # 当前 node 名
        "progress": state.progress,     # "3/5"
        "gate": state.gate_payload,      # 仅 needs_decision 时
        "output": state.final_output,    # 仅 completed 时
        "error": state.error,            # 仅 failed 时
        "_hint": _next_hint(state),      # 引导 Claude 下一步
    }

@mcp.tool()
def resolve_gate(
    task_id: str,
    gate_id: str,
    decision: str,
) -> dict:
    """对 needs_decision 状态的 task 提交人的决策。
    
    decision 通常是选项之一（gate.options 里给）或自由文本。
    """
    ok = human_gate_handler.resolve(gate_id, decision, source="mcp")
    return {
        "ok": ok,
        "status": "running" if ok else "needs_decision",
        "_hint": "Call get_task_status to continue." if ok 
                 else "Gate already resolved by another shell.",
    }
```

### 5.4 让 Claude 主动 chain 调用（无内部 polling 工具）

Claude Code **没有** `wait` / `poll` 内置工具——`start_workflow / get_task_status / resolve_gate` **全部由 Orca MCP server 暴露**。引导 Claude chain 调用靠三个杠杆：

1. **Tool description 强指令**（最主要）：
   ```python
   """启动 workflow，立即返回 task_id。
   **Always call get_task_status after this returns**, and again
   after each resolve_gate, until status is completed/failed."""
   ```
2. **返回值 `_hint` 字段**：每个 tool result 显式告诉 Claude 下一步。
3. **Workflow 的 agent prompt**（启动前的用户消息）：可在 prompt 里写明"this workflow has human gates, poll status until completion"。

### 5.5 Loop detection 警惕

[GitHub anthropics/claude-code#4317](https://github.com/anthropics/claude-code/issues/4317) 提到 CC 有"重复循环检测"。若 Claude 在一个 turn 内连续多次调 `get_task_status` 且每次都 `running`，可能被误判死循环。

**缓解策略**（phase 10 SPEC 细化）：
- `get_task_status` 在 `running` 状态时返回"建议结束 turn"的 hint（如"workflow still running, check back later"），让 Claude 报告给用户后结束 turn。
- `needs_decision` / `completed` / `failed` 是天然跳出点。
- 不引入 Bash `sleep`（不优雅，且 CC 可能限 Bash）。

### 5.6 边界

- ❌ 不把 DAG 可视化塞进对话流（无 UI 能力，Claude Agent View 接入不可能 §11.2）。
- ❌ 不做长轮询 `wait_for_event`（60s 超时）。
- ❌ 不做 elicitation（CC 未支持；即使支持，长 workflow 多 gate 弹窗体验崩坏）。
- ✅ gate 主交互面留给 Web（用户开着浏览器就走 Web 弹窗）。

---

## 6. HMIL 三通道竞速（TASK.md §4 已定，本草稿细化）

### 6.1 机制

引擎 emit `human_decision_requested(gate)` 事件 → 三壳各自渲染 → 任一壳调 `human_gate_handler.resolve(gate_id, answer, source)` → `asyncio.wait(FIRST_COMPLETED)` 唤醒所有等待者。

```python
# orca/gates/handler.py（phase 6 实现）
class HumanGateHandler:
    def __init__(self, bus: EventBus):
        self._bus = bus
        self._pending: dict[str, asyncio.Future] = {}

    async def request(self, gate: HumanGate) -> str:
        fut = asyncio.Future()
        self._pending[gate.id] = fut
        self._bus.emit("human_decision_requested", gate)
        return await fut  # 任一壳 resolve 唤醒

    def resolve(self, gate_id: str, answer: str, source: str) -> bool:
        fut = self._pending.pop(gate_id, None)
        if fut is None or fut.done():
            return False  # 已被别的壳答了
        fut.set_result((answer, source))
        return True  # 我是赢家
```

### 6.2 三壳各自的 resolve 路径

| 壳 | resolve 来源 | 调用方式 |
|---|---|---|
| CLI | Rich `input()` | 同步 `human_gate_handler.resolve(...)` |
| Web | 浏览器点按钮 → HTTP POST | HTTP handler → `human_gate_handler.resolve(...)` |
| MCP | Claude 调 `resolve_gate` tool | MCP handler → `human_gate_handler.resolve(...)` |

### 6.3 竞速取消语义：广播（2026-06-30 定稿）

**语义**：任一壳 resolve → gate 立即变 `resolved`，把答案**广播**给所有订阅该 gate 的壳。

- **赢家**：`resolve()` 返回 `True`，gate 状态变 resolved，引擎 resume。
- **其他壳（输家）**：通过订阅的 `human_decision_resolved` 事件收到广播（事件含 `gate_id, answer, source`）→ 各自关闭输入界面，显示「已被 [source] 答：[answer]」。
- **晚到的输入**：在 resolved 之后到达 → `resolve()` 返回 `False`（已 resolved），输入**静默丢弃 + 记 warning**（fail loud：可见，但不冲突）。

**为什么是广播而不是纯取消**（决策记录）：广播让所有壳**视觉上同步**——不会出现「Web 已答了 CLI 还在傻等」。这正体现唯一真相源：gate 的 resolved 状态写进 tape，所有壳从同一个 `human_decision_resolved` 事件读状态，必然一致。

**各壳的广播接收路径**：
- CLI：ModalScreen 收到 `human_decision_resolved` 事件 → 自动 dismiss 并显示「已被 [source] 答」（Textual 事件驱动，无需中断 input）。
- Web：弹窗收到 `human_decision_resolved` WS 消息 → 自动关闭，显示「已被 [source] 答」。
- MCP：`resolve_gate` 返回 `{ok: false, _hint: "Gate already resolved by [source]"}`；后续 `get_task_status` 的 status 跳过 needs_decision 直接到 running/completed。

> **安全约束**：竞速只决定「谁的答案生效」，不影响 gate 事件本身——`human_decision_requested` 和 `human_decision_resolved` 都写进同一个 tape，是唯一真相。三壳读同一份 tape，无投影分裂。

---

## 7. 关键技术结论备忘（写进 TASK.md §11）

调研得来的客观事实，必须落到 SPEC 防止后续 drift：

1. **CC 对 MCP tool call 有 60s 硬超时**（`DEFAULT_REQUEST_TIMEOUT_MSEC = 60000`，[issue #52137](https://github.com/anthropics/claude-code/issues/52137)）—— 长轮询不可行。
2. **MCP elicitation 在 CC 未支持**（spec 2025-06-18 标准化了，CC feature request 阶段，[issue #7108](https://github.com/anthropics/claude-code/issues/7108)）—— server 不能 mid-tool-call 反向问用户。
3. **HandleId / Call-Now-Fetch-Later 是业界标准** —— [Temporal MCP+HITL](https://learn.temporal.io/tutorials/ai/building-mcp-tools-with-temporal/adding-hitl-to-mcp-tools/) / [WorkOS MCP Async Tasks](https://workos.com/blog/mcp-async-tasks-ai-agent-workflows) / [Agnost](https://agnost.ai/blog/long-running-tasks-mcp) / [DEV.to](https://dev.to/aws/fix-mcp-timeouts-async-handleid-pattern-8ek) 一致。
4. **SEP-1686 (Tasks) 是 MCP 长任务的未来标准化方向**，CC 未实现；当前 Orca 自实现 HandleId，未来或迁移。
5. **Conductor 不解决 MCP gate 问题**（它是 standalone CLI = path A，不是 MCP server）—— 它只教 path A 双壳竞速（TASK.md §4 已吸收）。
6. **Claude 原生 Dynamic Workflows（2026/06 research preview）是真正威胁**——单 claude 后端编排会被原生吃掉。**跨工具（phase 8）+ event tape + hooks 桥是结构性护城河**。
7. **三壳在 phase 6 完成后相互独立**，可并行开发（TASK.md §3 已保证引擎/壳解耦）。
8. **Claude Code 内置工具没有 polling/wait** —— `start_workflow / get_task_status / resolve_gate` 全部由 Orca MCP server 暴露；引导 Claude chain 调用靠 tool description + `_hint`。

---

## 8. 端到端验证场景（每壳一个 user journey）

### 8.1 CLI（phase 7 验收）

```
$ orca run deploy.yaml --inputs env=staging
... node plan (agent_message, tool_call, tool_result) ...
⏸ GATE: 批准部署到 staging 吗？ [y/N]: y
... node deploy (agent_message) ...
✓ run completed in 47s, cost $0.12
$ echo $?
0
```

### 8.2 Web（phase 9 验收）

```
1. 浏览器打开 localhost:7428
2. 点 "New Run" → 选 deploy.yaml → 输入 env=staging → Start
3. 看 DAG 进度：plan ▆▆▆▆ → evaluator ▆▆ → deploy ⏸
4. 弹窗 "批准部署到 staging?" → 点 Yes
5. deploy ▆▆▆▆ → ✓ completed
6. 切到 History 选刚跑完的 run → replay 时间旅行（拖时间轴看每步状态）
```

### 8.3 MCP（phase 10 验收）

```
[用户在 Claude Code]
用户: 帮我跑 deploy workflow 到 staging
Claude: 调 start_workflow(yaml="deploy.yaml", inputs={env:"staging"})
        ← {task_id: "abc", status: "running"}
Claude: 调 get_task_status(task_id="abc")
        ← {status: "running", progress: "1/5", ...}
Claude: 调 get_task_status(task_id="abc")
        ← {status: "needs_decision", gate: {id: "g1", prompt: "..."}}
Claude: "Orca 想知道：批准部署到 staging 吗？"

用户: 批准
Claude: 调 resolve_gate(task_id="abc", gate_id="g1", decision: "yes")
        ← {ok: true, status: "running"}
Claude: 调 get_task_status(task_id="abc")
        ← {status: "completed", output: {...}}
Claude: "完成了，部署成功，结果..."
```

### 8.4 三通道竞速验收

同一 run，CLI 启动后同时开 Web：
- gate 触发 → CLI 终端 prompt + Web 弹窗同时出现
- 用户在 Web 点 Yes → CLI prompt 自动消失显示"已被 web 答"
- 或反过来：CLI 先答 → Web 弹窗自动关闭

---

## 9. 与 TASK.md 的衔接（更新清单）

本草稿落地后，TASK.md 已做以下更新（2026-06-30）：

- **§3 多入口壳**：补三壳形态对照表 + MCP 协议约束（60s 超时 / elicitation / 不监控）。
- **§4 HMIL**：替换 "MCP 壳：claude 对话里显示" 的 hand-wave 为 HandleId pattern 具体协议。
- **§10 开发阶段**：按 2026-06-30 调研后重排（4-7 关键路径 / 8 护城河 / 9-10 入口扩展 / 11 发行），phase 编号对齐实际。
- **§11 关键技术结论**：追加 7 条（CC 60s 超时、elicitation 未支持、HandleId 标准、SEP-1686、Conductor 不教 MCP、Claude 原生 dynamic workflows 威胁、三壳可并行）。

后续进入 phase 7/9/10 的 SPEC 撰写人：以 TASK.md §3/§4/§10/§11 + 本草稿为依据，写具体接口 / 数据契约 / 验收。

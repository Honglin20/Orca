# Orca —— 参考资料

> 多 agent 编排 / 实时可观测 / 人工介入 / 打断 的相关开源仓库与设计参考。
> 用于决定 Orca 的执行模型、介入通道、打断机制。
> 维护：发现新仓库追加到对应分类，不要删旧的。

---

## 调研结论速览（详见各分类）

**字段级共识（8 条）**：
1. **tmux + git worktree 是隔离/观测的事实标准栈**——claude-squad / agent_farm / stablyai-orca / amux / vibe-kanban 全都收敛到这个模式
2. **YAML 主导 DAG 配置**（dagu / conductor / Orca 也采用）
3. **介入几乎都是"tmux send-keys"或"重写 task spec 文件"**——没人有结构化的介入协议（这是 Orca 的差异化机会）
4. **工具调用中的打断是未解难题**——hook 在 subagent 委派时被静默绕过（Issue #34692）；现实是 kill pane / SIGINT
5. **coding-agent 项目普遍缺持久化**——claude-squad 等不抗崩溃；Temporal/Restate 有持久化但不懂 agent
6. **"Claude 自己编排自己"是反模式**——Ralph Loop / 动态 workflow 是涌现的、非确定性的；Orca 价值就是让编排确定化
7. **per-provider 抽象已成标配**——headless-coder-sdk / vibe-kanban / conductor 都跨 Claude/Codex/Gemini
8. **UX 两层共存**：TUI（claude-squad）vs Web dashboard（amux/vibe-kanban）；Orca 选 DAG 可视化 = Web 路线

---

## A. 多 agent workflow 编排器（YAML/DAG）

| 仓库 | 星 | 特点 | 执行 | 观测 | 介入 | 打断 | Orca 可借鉴 |
|---|---|---|---|---|---|---|---|
| [microsoft/conductor](https://github.com/microsoft/conductor) | ~13k | YAML DAG + Copilot/Anthropic SDK | SDK `query()`（**不是** claude -p）| **block/turn 级**（非 token 级）| gate 选路由 + guidance 追加 | SDK message 间（非工具调用中）| 声明式 DAG + per-node provider 抽象 |
| [dagucloud/dagu](https://github.com/dagucloud/dagu) | ~14k | Go 单二进制 YAML DAG | step 子进程 | 内置 web UI + live stdout | 改参数重跑 | stop 按钮 | **最佳单二进制 YAML DAG 引擎** |
| [patoles/agent-flow](https://github.com/patoles/agent-flow) | 小 | 多 agent 图 | 图执行 | **实时图可视化** | 有限 | 有限 | 证明实时 per-node 可视化是可行且被期望的 UX |
| [ruvnet/ruflo](https://github.com/ruvnet/ruflo)（ex claude-flow）| ~12k | 54+ swarm agents | swarm | terminal + memory | task 分配 | agent 终止 | 大规模 swarm 拓扑参考 |
| [snarktank/ralph](https://github.com/snarktank/ralph) | 小中 | **Ralph Loop**：bash 循环跑 claude -p | bash 子进程 | 日志文件 | 重写 task spec | kill 进程 | **反模式参考**：非确定编排 |

---

## B. 并行 coding-agent session 管理器（tmux/worktree）

> 这是和 Orca 最相关的分类。tmux+worktree 是事实标准。

| 仓库 | 星 | 特点 | 执行 | 观测 | 介入 | 打断 | Orca 可借鉴 |
|---|---|---|---|---|---|---|---|
| [smtg-ai/claude-squad](https://github.com/smtg-ai/claude-squad) | ~6.9k | Go TUI | **tmux session + git worktree 每 agent** | TUI pane 列表 | **send-keys 到 pane** | kill/skip pane | **黄金标准架构**——先研究这个 |
| [stablyai/orca](https://github.com/stablyai/orca) | ~7.6k | ADE，worktree 并行 claude/codex | worktree 每 agent | dashboard | ADE 编辑流 | worktree stop | **⚠️ 同名冲突**——Orca 要改名 |
| [mixpeek/amux](https://github.com/mixpeek/amux) | 中 | 开源 multiplexer | tmux | **web dashboard + kanban + CRM** | web 操作 | session stop | **dashboard 产品面参考** |
| [BloopAI/vibe-kanban](https://github.com/BloopAI/vibe-kanban) | 中 | Kanban 驱动 | worktree 每 task | kanban + logs | board 拖拽 | task cancel | **多 provider**（Claude/Codex/Gemini）|
| [Dicklesworthstone/claude_code_agent_farm](https://github.com/Dicklesworthstone/claude_code_agent_farm) | 小 | **20+ 并发 agent** | tmux + 文件锁 | tmux panes + logs | send-keys | kill pane | **高并发锁参考** |
| [nutthouse/tutti](https://github.com/nutthouse/tutti) | 小 | Rust CLI | config 驱动 | 结构化输出 | config 编辑 | 进程停 | **typed-artifact-flow**（强类型 DAG）|

---

## C. Headless claude code / SDK 包装器

| 仓库 | 星 | 特点 | Orca 可借鉴 |
|---|---|---|---|
| [OhadAssulin/headless-coder-sdk](https://github.com/OhadAssulin/headless-coder-sdk) | 小 | 统一 SDK 包 Codex/Claude/Gemini | **provider 无关的 headless runner 抽象** |
| [Claude Code `claude -p` headless](https://docs.claude.com/en/docs/claude-code/sdk) | n/a | 子进程每 node | **Orca 的基础原语**：stream-json + stdin + SIGINT |
| Claude Code SDK（TS/Python）| n/a | 编程式 SDK | event stream + abort signal——备选执行后端 |

---

## D. 持久化执行引擎（架构思想，非 agent）

| 仓库 | 星 | 特点 | Orca 可借鉴 |
|---|---|---|---|
| [temporalio/temporal](https://github.com/temporalio/temporal) | ~13k | 持久 workflow + activity | **THE 参考**：activity cancel / signal / heartbeat / event-sourced replay |
| [inngest/inngest](https://github.com/inngest/inngest) | ~5k | event-driven 持久函数 | event-triggered 持久 step + fan-out |
| [PrefectHQ/prefect](https://github.com/PrefectHQ/prefect) | ~18k | Python dataflow | **paused-task / human-approval 模式** → 直接映射 Orca 介入通道 |
| [dagster-io/dagster](https://github.com/dagster-io/dagster) | ~12k | asset-centric | asset-lineage + sensor（"output = git artifact"建模）|
| [restatedev/restate](https://github.com/restatedev/restate) | ~8k | durable logs | **"Awakeables" = 持久 suspend/resume 等待点** → 最接近持久人工 gate |
| [dbos-inc/dbos](https://github.com/dbos-inc/dbos) | 小 | Postgres-backed | 比 Temporal 简单的持久化 |

---

## E. Claude Code 原生功能（Orca 要嵌入的生态）

| 功能 | 文档 | 在 Orca 的作用 |
|---|---|---|
| **Hooks**（PreToolUse/PostToolUse/UserPromptSubmit/Stop）| https://code.claude.com/docs/en/hooks | **主要介入/打断面**——但见下方 Issue #34692 限制 |
| **Subagents**（`.claude/agents/*.md`）| https://docs.claude.com/en/docs/claude-code/sub-agents | 声明式 agent 定义；model-initiated 委派 |
| **MCP servers** | https://docs.claude.com/en/docs/claude-code/mcp | 工具/资源供给；**人工输入工具的 side-channel**（inbox/queue）|
| **Headless stream-json**（`claude -p`）| https://docs.claude.com/en/docs/claude-code/sdk | **Orca 每 node spawn 的原语**：token 级实时流 |
| **`--resume` / session 连续性** | 同上 | 每 node 持久 resume |
| **`--bg` / `--tmux` / `--worktree`** | https://code.claude.com/docs/en/changelog | 原生后台/tmux 执行（不再是第三方 hack）|
| **Dynamic workflows**（claude 写自己的编排脚本）| sub-agents 文档 | **反模式**：涌现、非确定；Orca 存在就是为了让它确定 |

---

## ⚠️ 关键限制（影响 Orca 设计）

### Issue #34692：subagent 委派时 hook 被静默绕过
https://github.com/anthropics/claude-code/issues/34692

**事实**：PreToolUse/PostToolUse hook 在工具调用委派给 subagent 时**被静默跳过**。
**影响**：任何依赖 hook 触发介入/打断的设计在 subagent 执行时有盲区。
**缓解**：① orchestrator 层做工具级 gating（不靠 hook）；② 每个 agent 作为顶层 `claude -p` session 跑，而非 Claude subagent。

### Issue #17466：工具执行中 ESC/Ctrl+C 不可靠
https://github.com/anthropics/claude-code/issues/17466

**事实**：Esc/Ctrl+C 在工具执行期间经常不生效。安全打断窗口只在**工具之间**。
**影响**：mid-tool-call 打断只能靠 kill（方案 A），不能靠软打断。

### Issue #30492：优先级消息通道（未发布）
https://github.com/anthropics/claude-code/issues/30492

**事实**：实时 steering（高优先级消息打断执行注入重定向）**还没发布**。
**影响**：mid-execution redirect 目前只能靠 hook+kill 组合近似。**关注这个 issue，一旦发布就是单一原语替代。**

---

## Top 5 最相关仓库（Orca 执行/介入/打断设计，按相关性排序）

1. **[smtg-ai/claude-squad](https://github.com/smtg-ai/claude-squad)** —— 最接近的架构：tmux 每 agent + worktree 隔离 + send-keys 介入 + pane kill 打断。Orca 应研究它的 tmux 生命周期，然后**加 DAG 拓扑 + 持久化**（它缺这俩）。把它作为"要超越的基线"。

2. **[temporalio/temporal](https://github.com/temporalio/temporal)** —— 不是 agent 项目，但**per-agent 打断/重定向 + 持久化的标杆答案**。activity cancel / signal / heartbeat / event-sourced replay 提供了 pause/resume/cancel 的成熟词汇和 API 形状。借语义不借引擎。

3. **[mixpeek/amux](https://github.com/mixpeek/amux)** —— 开源 multiplexer + 真 web dashboard + kanban + CRM。展示 Orca 想要的观测+介入 UX 层。dashboard 产品面参考。

4. **[microsoft/conductor](https://github.com/microsoft/conductor)** —— YAML DAG + 可插拔 LLM SDK。**声明式 DAG-as-config + per-node provider 抽象**的参考——正是 Orca "spawn claude -p per node" 的泛化版。

5. **[restatedev/restate](https://github.com/restatedev/restate)** —— "Awakeables"（持久 suspend/resume 等待点）是**最接近持久人工 gate 的现成类比**。如果 Orca 要抗崩溃的 mid-run 审批，Restate 模型最相关。

---

## 策展 meta-list

- [awesome-agent-orchestrators](https://github.com/andyrewlee/awesome-agent-orchestrators)
- [awesome-multi-agent-orchestrators](https://github.com/Agent-Analytics/awesome-multi-agent-orchestrators)

---

## Conductor 深度调研结论（补充）

Conductor 的可观测/介入/打断机制（代码实证）：

| 能力 | Conductor 做了吗？ | 机制（file:line）或缺口 |
|---|---|---|
| 执行（claude SDK，非 -p）| ✅ | `claude_agent_sdk.py:254` `async for message in query(...)` |
| 实时观测 dashboard | ✅ block/turn 级 | provider 在 `async for` 里 emit → `_emit` → WS |
| **token 级流** | ❌ **缺口** | SDK 返回整 message，最小单元是 content block |
| 实时工具调用观测 | ✅ | `agent_tool_start` / `agent_tool_complete` |
| 用户触发打断（CLI）| ✅ | `interrupt/listener.py` Esc/Ctrl+G → `interrupt_event.set()` |
| 用户触发打断（web）| ✅ | `POST /api/stop` → `server.py:223` |
| 打断检查点（Claude SDK）| **只在 message 间** | `claude_agent_sdk.py:255-264` |
| 打断检查点（Copilot）| 激进，race | `copilot.py:1880-1913` `asyncio.wait(FIRST_COMPLETED)` → `_abort_session` |
| 打断后恢复（Claude SDK）| **重新执行** | `workflow.py:3663-3669` 清 event 重跑 |
| 打断后恢复（Copilot）| ✅ 会话内 follow-up | `copilot.py:1243` `send_followup` 保活会话 |
| workflow 级 checkpoint 恢复 | ✅（node 级）| `engine/checkpoint.py:171` + `cli/run.py:2023` |
| **attach 到运行中 agent 打字** | ❌ **缺口** | 无 attach API；只能 interrupt-then-resume（重执行）|
| Human gate（选路由 + 可选文本）| ✅ | `gates/human.py:75`；web `POST /api/gate-respond` |
| Dialog（多轮 post-run 聊天）| ✅ **仅 Copilot** | `gates/dialog.py:169`；`execute_dialog_turn` 仅 copilot 实现 |
| `asyncio.wait FIRST_COMPLETED` race | ✅ 但**不在** gates/human.py | `workflow.py:2371`（resume/kill/disconnect）+ `copilot.py:1886` |
| 并行 agent | ✅ | `asyncio.gather` workflow.py:4653 |
| 后台/headless dashboard | ✅（`--web-bg`）| `cli/run.py:1556,1756`；grace timer `server.py:1011` |
| **mid-run 自由消息注入 agent context** | ❌ **缺口** | 只有 guidance-append-on-reexecute 或 Copilot follow-up |

**Conductor 的核心教训**：
- 它**不是 token 级流**（SDK 限制）→ Orca 用 `claude -p` 反而更强（token 级实测验证）
- 它**打断只在 message 间**（工具调用中不行）→ Orca 必须接受 #17466 硬约束
- 它**attach 介入靠重执行**（非真正 mid-run 注入）→ 这是 Orca 差异化机会
- **`asyncio.wait(FIRST_COMPLETED)` race 是给"多输入源竞速"用的**（resume/kill/disconnect 谁先到），不是给 mid-run 注入用的——这个我之前误读了，纠正

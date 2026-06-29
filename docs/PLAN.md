# Orca 整体开发计划

> 10 个阶段，每个阶段独立可验证。`✅` 已完成，`⬜` 待开发。
> 架构决策见 [TASK.md](TASK.md)，每阶段细节见 `specs/phase-N-*.md`。

---

## 阶段总览

| 阶段 | 模块 | 做什么 | 状态 |
|---|---|---|---|
| **0** | 骨架 | pyproject + 目录 + SDD 模板 | ✅ |
| **1** | `schema/` | 纯数据结构（workflow/event/state）| ✅ |
| **2** | `compile/` | YAML→Workflow 解析 + 两层校验 | ✅ |
| **3** | `events/` | EventBus + tape 持久化 | ⬜ |
| **4** | `exec/` | 执行内核（CLIRunner + Executor + Translator）| ⬜ |
| **5** | `run/` | 编排（Orchestrator + Router）| ⬜ |
| **6** | `iface/cli` | `orca run/validate` + Rich 渲染 | ⬜ |
| **7** | `iface/web` | FastAPI + WebSocket + 前端 SPA | ⬜ |
| **8** | `gates/` | HMIL（HumanGate + PreToolUse hook + 三通道）| ⬜ |
| **9** | `mcp/` | MCP server（render_chart/ask_user/控制）| ⬜ |
| **10** | ecosystem | registry + plugin 打包 | ⬜ |

## 依赖链

```
schema ─► compile ─► run ─► iface(cli/web)
   │                    ▲
   ├──► events ◄────────┤
   │          ▲
   └──► exec ─┘
   └──► gates (依赖 exec + events)
   └──► mcp (依赖 events + run)
```

## 各阶段验证标准

### Phase 0 ✅ 骨架
pyproject.toml（uv + hatchling + pydantic）+ docs 目录 + SDD 模板。

### Phase 1 ✅ schema/
workflow.py / event.py / state.py，纯 pydantic 模型，43 测试绿。
SPEC：[`specs/phase-1-schema.md`](specs/phase-1-schema.md)

### Phase 2 ✅ compile/
**做什么**：YAML 文件 → 校验过的 Workflow model。两层校验：① pydantic 结构（phase 1 已做）② 语义校验（name 唯一/entry 存在/引用有效/after 无环/可达/Jinja2 引用）。
**对外接口极简**：`load_workflow(path) -> Workflow` 一个函数（用户/LLM 只需知道这个）。内部校验要全（errors 收集 + warnings）。
**验证**：nas.yaml 解析通过；错误 YAML 被拒且错误信息精确。103 测试全绿（schema 50 不回归 + compile 53）。
SPEC：[`specs/phase-2-compile.md`](specs/phase-2-compile.md)

### Phase 3 ⬜ events/
**做什么**：EventBus（同步 pub/sub）+ tape（append-only JSONL 持久化）。
**核心**：event tape 是唯一真相源。`emit(event) → 写 tape + 通知订阅者`；`replay(tape) → 重建 RunState`。
**验证**：emit N 个事件→tape 有 N 行；replay→重建出正确 RunState。

### Phase 4 ⬜ exec/
**做什么**：CLIRunner（spawn claude -p，stdin/流式/超时/SIGTERM）+ Executor 接口（`async exec(node, context) -> AsyncIterator[Event]`）+ ClaudeExecutor + ClaudeTranslator（stream-json→Event）。
**迁移**：AgentHarness 的 `_cli_subprocess.py` + `translator/stream_json.py`。
**验证**：单个 AgentNode 能跑，stdout 流出 agent_message/agent_tool_call/agent_usage 事件。

### Phase 5 ⬜ run/
**做什么**：Orchestrator（拓扑排序 + 并行 asyncio.gather + foreach 分批）+ Router（Jinja2 first-match-wins）。
**不引入 LangGraph**。
**验证**：`orca run nas.yaml` 跑完整 workflow，event 写进 tape，outputs 正确。

### Phase 6 ⬜ iface/cli
**做什么**：`orca run <yaml>` / `orca validate <yaml>` / `orca list` + Rich 实时渲染（进度/log）。
**迁移**：AgentHarness 的 tui/ MainPanel + SidebarPanel。
**验证**：命令行跑 workflow + 实时显示进度和 log。

### Phase 7 ⬜ iface/web
**做什么**：FastAPI（REST + WebSocket）+ 前端 SPA（单 store reducer + DAGPreview + 对话 + chart）。
**迁移**：AgentHarness 的 DAGPreview.tsx + routeEvent reducer。
**验证**：浏览器看 DAG 进度 + log + chart + HMIL 弹窗。

### Phase 8 ⬜ gates/
**做什么**：HumanGateHandler + PreToolUse hook（HTTP 桥）+ ask_user MCP 工具 + 三通道竞速（CLI/Web/MCP）。
**实测已验证**：hook 能阻塞 claude -p。
**验证**：claude 调危险工具→hook 拦→UI 答→claude 继续。

### Phase 9 ⬜ mcp/
**做什么**：MCP server 暴露 render_chart / ask_user / orca_run / orca_status 给 claude 子进程。
**迁移**：AgentHarness 的 mcp/server.py + proxy.py + render_chart。
**验证**：claude 子进程能调这些工具。

### Phase 10 ⬜ ecosystem
**做什么**：workflow registry（`orca run nas@official`）+ Claude Code plugin 打包（subagents + skills + MCP 一个包）。
**验证**：`/plugin install orca` 能装。

---

## 核心原则（贯穿所有阶段）

1. **每个阶段独立可验证**——不积累"写一大半跑不起来"
2. **SPEC 驱动**——每阶段先确认 SPEC（数据契约/接口），再实现
3. **依赖铁律**——schema 最低层，单向依赖，禁止反向
4. **事件唯一真相源**——streaming = replay = read tape
5. **fail loud**——校验不过绝不放行，错误信息精确
6. **对外接口极简，内部校验要全**（compile/ 的设计原则）

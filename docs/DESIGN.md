# Orca 设计文档

> claude code 子进程为后端的 workflow 编排框架。
> 设计原则：高内聚低耦合、严格分层、单向依赖、事件唯一真相源。

## 架构分层

```
①描述 →  model    （纯数据：Workflow/Node/Event）
②结构 →  compile  （YAML → DAG，纯解析零运行时）
③执行 →  run      （编排：拓扑/并行/路由，后端无关）
③'执行 → exec     （执行：claude/ccr/codex，可扩展）
④事件 →  events   （EventBus + tape，唯一真相源）
⑤交互 →  gates    （暂停+决策：human/interrupt）
        mcp      （工具供给 claude）
        iface    （cli/web/replay，被动消费 event）
```

## 模块依赖规则（铁律）

- `model` 最底层，零依赖（纯 dataclass）
- `run` 调 `exec`（通过 Executor 接口）和 `events`，`exec` 不知道 `run` 存在
- `exec` 实现只依赖 `model` + `events`，绝不依赖 `run`
- `iface` 依赖 `events` + `model`，被动消费，绝不反向调用
- **禁止任何反向依赖**

## exec 双层抽象（可扩展性 + 鲁棒性）

```
Layer 1: Executor 接口（后端无关）
  run(node, context) -> AsyncIterator[Event]

Layer 2: 每个实现 = CLIRunner（通用子进程，共用） + Translator（纯函数）
  ClaudeExecutor  = CLIRunner + ClaudeTranslator
  CodexExecutor   = CLIRunner + CodexTranslator
  OpenCodeExecutor= CLIRunner + OpenCodeTranslator
```

行业共识：claude/codex/opencode 全是 "headless 子进程 + 每行 JSON 事件流到 stdout" 范式。

## workflow 定义格式：YAML

## 开发阶段

### 阶段 0：项目骨架（半天）
建目录、pyproject.toml（uv+hatchling）、CI。

### 阶段 1：数据模型（1-2 天）⭐根基
`model/` 全部 dataclass：Workflow/Node/Event/Result。
关键决策：Event 类型全集、Node 间数据传递、路由表达式。
参考：Conductor events.py（~40 事件类型取精华）、config/schema.py AgentDef。
验证：写 nas.yaml，compile 能解析成 DAG 并校验。

### 阶段 2：执行内核（3-5 天）⭐最难
`exec/`：CLIRunner + Executor 接口 + ClaudeExecutor + ClaudeTranslator。
迁移：_cli_subprocess.py、translator/stream_json.py、cli_profile.py。
参考设计：Conductor base.py（ABC）、capabilities.py（能力声明）。
丢弃：pydantic-ai、node_factory 巨石。
验证：ClaudeExecutor().run(node) 能跑通，stdout 流出 event。

### 阶段 3：编排层（2-3 天）
`run/`：Orchestrator（拓扑/并行/路由）+ Router（确定性条件路由）。
`events/`：EventBus + tape 持久化。
迁移：run_store.py → tape。
参考设计：Conductor engine/router.py、单 tape 思想。
丢弃：LangGraph 全部。
验证：orca run nas.yaml 跑完整 workflow。

### 阶段 4：表现层（5-7 天）
`iface/`：CLI(Rich) + Web(FastAPI+WS) + 前端 SPA。
迁移：DAGPreview.tsx、routeEvent reducer。
参考设计：Conductor web/server.py、workflow-store.ts、三栏布局。
丢弃：双 store/4 replay/benchmark/portal/outline/12 chart/history 侧栏。
验证：dashboard 显示 DAG 进度 + log + chart。

### 阶段 5：生态化（2-3 天）
`mcp/`：MCP server（render_chart/ask_user）+ registry + plugin 打包。
迁移：mcp/server.py + proxy.py、render_chart。
参考设计：Conductor plugins/、registry/。

## 参考来源对照

| 功能点 | 来源 | 处理 |
|---|---|---|
| 子进程管理 | AgentHarness _cli_subprocess.py | 🟢 迁移 |
| stream-json 翻译 | AgentHarness translator/stream_json.py | 🟢 迁移 |
| CLI profile 抽象 | AgentHarness cli_profile.py | 🟢 迁移（降级到 exec/claude/）|
| 持久化 | AgentHarness run_store.py | 🟢 迁移（改造成 tape）|
| DAG 前端组件 | AgentHarness DAGPreview.tsx | 🟢 迁移 |
| 事件 reducer | AgentHarness routeEvent | 🟢 迁移（简化）|
| MCP server | AgentHarness mcp/ | 🟢 迁移 |
| render_chart | AgentHarness chart 工具 | 🟢 迁移 |
| 单 tape 架构 | Conductor | 🔵 借鉴设计 |
| 确定性路由 | Conductor engine/router.py | 🔵 借鉴设计 |
| WebSocket 单通道 | Conductor web/server.py | 🔵 借鉴设计 |
| Executor ABC | Conductor providers/base.py | 🔵 借鉴设计 |
| 能力声明 | Conductor capabilities.py | 🔵 借鉴设计 |
| plugin 打包 | Conductor plugins/ | 🔵 借鉴设计 |
| LangGraph | AgentHarness | 🔴 丢弃 |
| pydantic-ai | AgentHarness | 🔴 丢弃 |
| 双 store/4 replay | AgentHarness | 🔴 丢弃 |
| god-class node_factory | AgentHarness | 🔴 丢弃 |
| workflow.py 5846行 | Conductor | 🔴 反面教材，不学 |
| 根目录 scratch | AgentHarness | 🔴 丢弃 |

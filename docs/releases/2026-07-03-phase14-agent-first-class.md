# phase-14 release note —— Agent 一等化 + Route 输出变换（批 1）

> 日期：2026-07-03 ｜ 分支：`phase14-agent-first-class` ｜ SPEC：[`phase-14-agent-first-class.md`](../specs/phase-14-agent-first-class.md)（对抗审闭环 v2）｜ plan：`~/.claude/plans/ethereal-baking-toucan.md`

## 做了什么

把 agent 从「内嵌 prompt 字符串」升级为**可命名、可复用、可携带资源的一等公民**，并给 Route 加终点输出变换。批 1（本地池），子 workflow / 包分发 / workspace-instruction 留批 2（phase-15）。

### 1. 统一解析层 `orca/compile/agents.py`（新）
- `AgentResolver`（Protocol）+ `LocalPoolResolver`（默认实现）+ `AgentHandle`/`AgentMeta`/`ResolveContext`。
- 删除双加载债：`compile/_load_prompts`（yaml 父目录）+ `exec/_load_agent_md`（cwd）两处不一致的加载，统一到 `_resolve_agents → resolver.resolve`。render 层从此**零文件 I/O**。
- 查找顺序 first-wins：`<workflow_dir>/agents/` → `<cwd>/agents/` → `extra_roots`（phase-15）；文件夹 `<name>/agent.md` 优先，单文件 `<name>.md` 兼容兜底。
- frontmatter（YAML 头 + body）精确解析（C6：body 内 `---` 水平线不误判）；坏头/未知字段/类型错 fail loud。
- 为 phase-15 多 pool / registry 预留：`AgentResolver` 接口锁定，`node.agent` 字符串未来扩展 `name@source`，schema 不变。

### 2. schema 改动（`orca/schema/workflow.py`）
- `AgentNode.agent: str | None`（agent 引用，与 `prompt` 互斥）+ `AgentNode.resources_root: str | None`（compile 物化 runtime cache，标注非 yaml 契约）。
- `Route.output: dict[str, str] | None`（到 `$end` 的输出变换）。
- resources_root 归属裁定（SPEC §0.4）：方案 A（进 AgentNode，与 prompt 物化同模式），承诺仅这两个 runtime cache。

### 3. Route 输出变换（`orca/run/router.py` + `orchestrator.py`）
- `router.resolve` 改返回命中的 `Route` 对象（`target = route.to`）。
- `_next_node_after` 返回 `(target, route)`；drive loop 捕获命中 `$end` 的 route → `_evaluate_outputs(end_route=...)`：有 `output` 用它，否则 fallback `wf.outputs`。skip 到 `$end` 经 router 容错命中兜底 route（C2 统一语义，无特殊分支）。

### 4. MCP 池暴露（`orca/iface/mcp/server.py`）
- 新增 `list_agents`（扫 agents/ 池，返回 name/description/has_resources）+ `get_agent`（prompt 截断 + frontmatter meta + resources）。复用 `LocalPoolResolver`。

### 5. 物化时序修正（实现期发现 SPEC 隐含缺陷）
互斥预检（prompt+agent 同时非空）+ foreach body 双 None 必须在**物化前**（物化会填 prompt 致互斥误报），故这两个与物化时序强相关的预检放 `_resolve_agents` 同一遍历，`validate_workflow` 只做物化后语义校验。

### 6. deprecation warn（C1）
旧约定（prompt 省略 + name 匹配 md）→ Python `warnings.warn(DeprecationWarning)`（零签名破坏，CLI validate 用 `catch_warnings` 捕获展示）。

### 7. 顺带修复
- `executor` capability guard 漏洞：opencode（`mcp_tools=False`）在无 agent_tools_server 时无脑注 `--allowed-tools`（node.tools 非 None）→ opencode dump help exit 1。frontmatter `tools:` 合并暴露此 bug。修：else 分支也检查 `supports_mcp`。
- `profiles/base.py` 的 `resolve_flags`（executor 依赖，与并行进程共享 executor.py 无法分离，一并 commit base.py 保自洽）。

## 验证（每功能点完整 E2E，opencode 真跑不 mock）

- **单测**：`tests/compile/test_agents.py`（21：resolver 查找顺序/形态优先/frontmatter fail loud/合并优先级/互斥/foreach body/deprecation/聚合）+ `tests/run/test_route_output.py`（4：route.output 命中/fallback/分类器/死代码 warn）+ test_router/test_render/test_skip_to_agent 适配新签名。
- **opencode e2e**（`tests/e2e_phase14/`，真 spawn opencode + deepseek-v4-flash）：
  - E2E-1：`agent: greeter` 显式引用 → agent_message='GREETER_OK' + agent_usage in=258/out=4（真 API）。
  - E2E-2：文件夹化 agent（`filebot/agent.md` + `scripts/flag.txt`）+ frontmatter → agent 经 `$ORCA_AGENT_RESOURCES` 读到 'SECRET_FLAG_42'（resources env 注入链：executor spawn → opencode → Bash → cat 全通）。
- **回归**：非 integration 全量 1276 passed / 0 failed（baseline 1224 → +52，含并行进程测试）。

## 与并行进程的边界
工作树有并行进程改后端协议（profiles builtin/terminal + executor_cmds/config/dialog/exec.validator）。本 commit 含 `executor.py`（共享：我的 agent_resources/else + 它的 flags=resolve_flags）+ `profiles/base.py`（resolve_flags 定义，executor 依赖）。builtin/terminal/dialog/validator/executor_cmds/config 留工作树由并行进程 commit。

## 已知局限 / 后续
- 批 2（phase-15）：轻量本地包分发（多 pool + `name@source`）+ workspace-instruction。
- ValidationResult.warnings 展示通道（C1 余项）：route.output 死代码 warn 等走 ValidationResult，load_workflow 丢弃；展示靠广义修复（phase-14 scope 外，单测直接调 validate_workflow 覆盖）。

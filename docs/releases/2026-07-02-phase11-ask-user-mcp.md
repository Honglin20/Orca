# 2026-07-02 — phase 11 P1.2：ask_user MCP 工具挂载

## 背景

被 Orca 编排的 claude agent 执行中想主动问用户（如要一个数据库连接串、确认选项）——之前
无通道。phase 11 §5 设计 ask_user：Orca 进程内嵌一个 socket SSE MCP server，注册 ask_user
工具；claude -p 经 `--mcp-config` 连上，调 ask_user → 触发 `HumanGate(source=agent_ask)` →
等任一壳 resolve → 返回 answer。

review（spec-review-adversarial + 实施后 code-reviewer 双轮）暴露的关键约束：
- **C2 / spike 前置**：`from mcp.server.fastmcp import FastMCP`（非第三方 fastmcp 包）；
  SSE spike 必须先 PASS 否则 feature 推迟（D4，不 fallback stdio）。
- **item4 / D4**：确定性 tool-params 路由（`orca_run_id` / `orca_node`），不依赖 MCP session
  反查（claude -p 不主动报 MCP session）。
- **B2**：gates `RunContext` NamedTuple 与 `exec.context.RunContext` 同名混淆 → 改 `SessionLoc`；
  phase 6 `registry.register` 调用缺失 → 本 phase 补。

## STEP 0 — SSE spike（GATE，已 PASS）

spike 双轮验证（throwaway 脚本，验证后删）：
1. **server half（in-memory）**：`mcp.server.fastmcp.FastMCP.run_sse_async()` 起 SSE server →
   in-memory `ClientSession` 经 `sse_client` 连上 → list_tools 看到 echo → call_tool 返回
   `echo:hello`。**PASS**。
2. **real claude 连通性**：真实 `claude -p --mcp-config <sse.json> --allowed-tools
   mcp__spike-echo__echo -p "call echo with 'claude-connects'"` → claude 经 `--mcp-config`
   连上 SSE server，调通 echo，返回 `echo:claude-connects`。**PASS**。

关键发现：**claude -p 默认不给 MCP 工具授权**——首次 spike 因 `--allowed-tools` 未传被
`The tool call was blocked — permission hasn't been granted` 拒绝；加 `--allowed-tools
mcp__<server>__<tool>` 后才放行。本发现写入 SPEC §11.3 + 实现（`_build_spawn_config`
自动 append ask_user 工具名进 `--allowed-tools`）。

裁定：spike 全 PASS（server half + real claude），**PROCEED 实现**。real claude SSE 连通性
属已验证（非 manual）。

## 改动点

### STEP 1 — gates `RunContext` → `SessionLoc`（review B2）
- `orca/gates/context_registry.py`：NamedTuple 改名（字段 `run_id`/`node` 不变），加
  `unregister_run(run_id)`（SPEC §6 清理契约，按 run 批清）。
- 跨阶段契约变更，release note 记录。blast radius：仅 `context_registry.py`（tests 用 `.run_id`/
  `.node` 字段访问，NamedTuple 兼容，无需改）。

### STEP 2 — `orca/exec/mcp_tools/server.py`（NEW）
- `AgentToolsMcpServer`：内嵌 SSE FastMCP server，暴露 `ask_user` 工具。
- 确定性路由（D4）：`ask_user(prompt, options=None, orca_run_id="", orca_node="")`——缺失
  路由参 → raise RuntimeError（fail loud）。
- 生命周期：`start()`（lazy，找空闲 loopback port）/ `stop()`（幂等）/ `write_config()`
  （写 `runs/<run_id>/mcp_<session>.json` SSE config）/ `register_session` / `unregister_run`。
- **依赖单向**：仅 import `mcp.server.fastmcp` + `orca.gates`（不碰 iface/run）。

### STEP 3-5 — wiring
- `make_executor(node, agent_tools_server=None)`（SPEC §5.4）：仅 agent 分支透传，foreach/
  parallel 也透传（body/branch 是 agent 时可用 ask_user）。
- `ClaudeExecutor(profile, agent_tools_server=None)`：
  - `_build_spawn_config` 注入 `--mcp-config <path>` + 自动 append
    `mcp__orca-agent-tools__ask_user` 进 `--allowed-tools`（spike 实证必须，SPEC §11.3）。
  - spawn 前 `register_session`（register debt，review B2）。
  - `run_id`/`session_id` 空 → RuntimeError（fail loud）。
- `Orchestrator(..., agent_tools_server=None)`：
  - `run()`/`run_from_state()` 内 `_start_agent_tools` / `_stop_agent_tools`（start 失败 →
    workflow_failed fail loud；stop 含 `unregister_run` 清理）。
  - `_dispatch` 把 server 透传给 make_executor / run_foreach / run_parallel_group。
- `OrcaApp` 构造 `AgentToolsMcpServer` 注入 Orchestrator。
- `None` 路径 == 既有行为（向后兼容，753 baseline 保持）。

### STEP 6 — `render_prompt` 路由 instruction
- `_append_ask_user_instruction`：server 注入时，prompt 末尾拼一条 instruction 告诉 claude
  调 ask_user 必带 `orca_run_id=<run_id>` / `orca_node=<node>`（具体值填进去，降低 claude
  省略路由参的概率）。

## 偏离 SPEC（SPEC §11.2-§11.4 记录）

- **§11.2 路由参名去下划线前缀**：SPEC §5.3 写 `_orca_run_id`/`_orca_node`，但 FastMCP 拒绝
  下划线开头的参数（`InvalidSignature`）。改 `orca_run_id`/`orca_node`（语义不变）。Rule 7。
- **§11.3 必须显式 --allowed-tools**：SPEC §5.4 未提 claude -p 默认拒 MCP 工具。spike 实证后
  `_build_spawn_config` 自动 append ask_user 工具名。
- **§11.4 register 时机 + 按 run 批清**：register 前移到 spawn 前（D4 副产物）；session 路由
  按 run 批清（session_id 由 executor uuid 生成，orchestrator 不持有，无法逐 node 精确清；
  Rule 2 简单）。

## 测试（共 20 新测试，断言 INTENT）

`tests/exec/mcp_tools/test_server.py`（9）：
- 生命周期：starts_on_free_port / start_idempotent / stop_idempotent。
- write_config：valid_sse_json / before_start_raises。
- register/cleanup：register_and_lookup / unregister_run_clears_all。
- ask_user 工具：routes_via_params_and_calls_handler（含 SPEC §10.2 item4 tape 配对断言：
  恰好一对 `human_decision_requested(source=agent_ask)` + `human_decision_resolved`，
  gate_id 一致）/ missing_routing_params_raises（3 种缺失组合）/ signature_has_routing_params。

`tests/exec/claude/test_executor_mcp.py`（6）：
- passes_mcp_config_flag_when_server_present / no_mcp_config_when_server_absent（含负向
  prompt 断言）/ appends_ask_user_to_declared_tools / registers_session_when_server_present /
  appends_ask_user_instruction / build_spawn_config_raises_when_run_id_or_session_id_empty。

`tests/run/test_orchestrator_agent_tools.py`（4）：
- starts_and_stops_agent_tools_server / no_agent_tools_server_when_none /
- stops_server_on_workflow_failure / propagates_agent_tools_start_failure（start 失败 →
  workflow_failed fail loud）。

外加 8 个既有测试文件的 `make_executor` fake 签名更新（`(node)` → `(node, agent_tools_server=None)`）：
test_orchestrator / test_parallel / test_foreach / test_resume / test_retry / test_interrupt_e2e /
test_interrupt_orchestrator / test_resume_e2e。纯兼容 shim，无行为弱化（回归保护成立）。

## 验证结果

- `uv run pytest tests/ -m "not integration"`：**773 passed, 1 skipped**（baseline 753 + 20 新）。
  **0 回归**。
- examples/with_ask_user.yaml：单 agent prompt 指示调 ask_user（real claude E2E 已由 spike
  验证 SSE 连通性 + 工具调用；自动化测试覆盖 in-memory Client round-trip + 路由 + tape 配对）。

## review 闭环

两轮 code-reviewer（impl + coverage）共提：
- 🔴 2 critical：tape 配对零覆盖（已补 §10.2 item4 断言）、unregister 孤儿（已接线 unregister_run
  + 清理测试）。
- 🟡 4 major：start 异常吞错（已改 fail loud → workflow_failed）、build_spawn_config 守卫无测
  （已补）、tools 白名单 append 无测（已补）、负向 prompt 断言（已补）。
- TOCTOU 同步探测：uvicorn bind 失败调 sys.exit 撕裂 loop，无法在 task 内干净捕获——改为
  orchestrator 级 fail loud（start 异常 → workflow_failed），start() docstring 记录 TOCTOU
  说明 + 兜底路径。SPEC §11 / release note 记录。

全部 fixed。

## Commit

`dcc3e63`

## 必读文件更新

`docs/status/CURRENT.md`：ask_user 完成，下一项 Validator(P2.1)。

## SSE spike verdict

- **server half（in-memory）**：PASS（FastMCP SSE + ClientSession round-trip）。
- **real claude SSE 连通性**：PASS（claude -p `--mcp-config` 连 SSE server 调通 echo）。
- **real claude ask_user E2E**：连通性已验证；ask_user 端到端（agent 调 ask_user → CLI 弹
  AskGate → 用户答 → agent 收答案）的完整 real-claude E2E 留作 manual（自动化测试已覆盖
  in-memory round-trip + 路由 + tape 配对，是可自动化的证明）。

# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 11 第二波 P1.2 ask_user MCP 工具挂载落地 —— 全绿（0 回归）。**

wave 2 第二项：ask_user。Orca 进程内嵌 SSE MCP server（`AgentToolsMcpServer`），被编排的
claude -p 经 `--mcp-config` 连上调 `ask_user` → `HumanGate(source=agent_ask)` → 壳答 → 返 answer。
确定性 tool-params 路由（D4）+ spike 双轮 PASS（in-memory + real claude）+ register 债补完（B2）+
gates `RunContext`→`SessionLoc` 改名（B2）。20 新测试断言 INTENT（含 SPEC §10.2 item4 tape 配对）。

- **最新 release note**：[`2026-07-02-phase11-ask-user-mcp.md`](../releases/2026-07-02-phase11-ask-user-mcp.md)
- **SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §5 / §11.2-§11.4

## 待办

1. **wave 3**：Validator(P2.1，复用 execute_with_retry loop) → Dialog(P2.2) → Wait(P3.1)。
2. **人工 E2E（待真 claude）**：`orca run examples/with_ask_user.yaml`（ask_user 端到端，连通性已由 spike 验证）。
3. **后续 wave**：daemon(P3.2) → Skip(P4)。

## 必读文件

1. [`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §5 / §9.6 / §10.3
2. [`docs/releases/2026-07-02-phase11-ask-user-mcp.md`](../releases/2026-07-02-phase11-ask-user-mcp.md)
3. [`orca/exec/mcp_tools/server.py`](../../orca/exec/mcp_tools/server.py)（AgentToolsMcpServer）+ [`orca/exec/claude/executor.py`](../../orca/exec/claude/executor.py) `_build_spawn_config` / `_append_ask_user_instruction`

## 裁定的决策（不再讨论）

1. 保持 `claude -p` CLI 子进程路线（SPEC §1.1）；D1 wave 顺序；D2 descope attach；D3 Budget 不做；D4 ask_user 确定性 tool-params 路由。
2. CLI 单壳中断不经 await-future（SPEC §11.1）；多壳路径保留给 P3。
3. Tape 是唯一 checkpoint：不另起状态序列化系统（反 Conductor）；`replay_state` 复用即 checkpoint。
4. parallel 组中间崩溃不支持 resume（SPEC §7 risk，歧义状态，exit 1）。
5. ask_user 路由参名 `orca_run_id`/`orca_node`（非 `_orca_*`，FastMCP 拒下划线前缀，SPEC §11.2）；
   register 时机前移到 spawn 前 + 按 run 批清（SPEC §11.4）。

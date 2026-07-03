# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前状态：phase-14（批 1）+ examples 整理完成；无进行中任务

**phase-14 Agent 一等化 + Route 输出变换（批 1）完成**（commit `74d65b3` + backfill `7befc2c` + M1 fixup `956015f`）
- agent 一等化（agent 池 + 文件夹化 + 统一解析层 `AgentResolver`）+ Route.output 终点输出变换 + MCP list_agents/get_agent。
- 走完整流程：SPEC → spec-review-adversarial 对抗审（闭环 2 P0 + 5 P1）→ 实现 11 文件 → code-reviewer 审通过（可合并，无 blocker/major）→ opencode+deepseek-v4-flash 真跑 e2e（E2E-1 agent 引用 + E2E-2 文件夹化 resources `$ORCA_AGENT_RESOURCES`）→ commit。**1276 passed 0 回归**。

**examples 整理完成**（commit `c5c13b1`）
- 13 agent example 固化 `executor: opencode` + `model: "deepseek/deepseek-v4-flash"`（with_ask_user 保留 claude——ask_user 需 mcp_tools）。
- description 补全（21 example，TUI 信息明确）+ examples/README.md 分类（纯 script / agent workflow / claude-only 例外）。
- 新建 render_chart example：文件夹化 agent plotter（agent.md + scripts/chart_demo.py 资源，演示 phase-14 `ORCA_AGENT_RESOURCES` + phase-13 chart 链路）。
- parallel_research 迁移到 phase-14 `agent: <name>` 显式引用（消除旧约定 deprecation warn）。
- **验证**：8 script + 13 agent + render_chart 全跑通（opencode+deepseek-v4-flash 真跑，**不 mock**）；with_ask_user 例外（claude-only，需 ANTHROPIC_API_KEY）。

## 与并行进程的边界
- phase-14 commit 含 `executor.py`（共享）+ `profiles/base.py`（resolve_flags 定义，executor 依赖）。
- examples commit 只 `examples/` + 新建 `tests/`。
- 留工作树（并行进程持有）：`profiles/builtin/*` + `terminal.py` + `gates/dialog.py` + `exec/validator.py` + `executor_cmds.py` + `config.py` + `tests/e2e_mxint/` + 它们的测试。

## 待办（等用户指示方向）
1. phase-14 / examples 分支 merge / PR（分支 `phase13-render-chart`）。
2. **批 2（phase-15）**：轻量本地包分发（多 pool + `name@source`）+ workspace-instruction（SPEC 已预留 `AgentResolver` 接口 + `ResolveContext.extra_roots`）。
3. phase-12 / phase-13 分支 merge / PR（仍待）。
4. code-reviewer M2/M3（resolve_flags setdefault 文档交叉引用 + stacklevel 指向）+ N3（tape artifact 含开发机路径，可 sanitize）—— minor/nit，下个 commit 顺手。

## 必读文件（下一任务开工前按需）
- [`docs/releases/2026-07-03-phase14-agent-first-class.md`](../releases/2026-07-03-phase14-agent-first-class.md)（phase-14 全貌 + 物化时序修正 + executor capability guard 修复 + 与并行进程边界）
- [`docs/specs/phase-14-agent-first-class.md`](../specs/phase-14-agent-first-class.md) §0.1 八铁律 + §3 解析层 + §5 Route 信息流 + §0.4 resources_root 裁定
- [`orca/compile/agents.py`](../../orca/compile/agents.py)（`AgentResolver`/`LocalPoolResolver`/`AgentHandle`，批 2 扩展点）
- [`examples/README.md`](../../examples/README.md)（examples 分类索引）

# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前状态：phase-14 Agent 一等化（批 1）完成；正在整理 examples

**phase-14 Agent 一等化 + Route 输出变换（批 1）收官**：agent 从内嵌 prompt 升级为可命名/可复用/可携带资源的一等公民（统一解析层 + 文件夹化 + frontmatter + Route.output + MCP list/get）。
- **SPEC**：[`phase-14-agent-first-class.md`](../specs/phase-14-agent-first-class.md)（对抗审闭环 v2，2 P0 + 5 P1 修订）｜**release**：[`2026-07-03-phase14-agent-first-class.md`](../releases/2026-07-03-phase14-agent-first-class.md)
- **验证**：**1276 passed 0 回归**；**opencode+deepseek-v4-flash 真跑 e2e**（E2E-1 agent 引用 GREETER_OK + E2E-2 文件夹化 resources `$ORCA_AGENT_RESOURCES` → SECRET_FLAG_42）。
- **顺带修**：executor capability guard（opencode + frontmatter tools 不注 `--allowed-tools` → dump help exit 1）。
- 分支：`phase13-render-chart`（phase-14 在此 commit；未 merge）。
- **与并行进程边界**：commit 含 `executor.py`（共享）+ `profiles/base.py`（resolve_flags 定义，executor 依赖）；builtin/terminal/dialog/validator/executor_cmds/config 留工作树由并行进程 commit。

## 进行中：整理 examples（goal 硬要求）

收尾任务，要求：
1. 每个 example **有意义** + TUI 有明确信息（让人知道在干什么）
2. **script example 与 agent example 分开**（目录或命名）
3. **agent example 必须用 opencode 真跑过**（不 mock），全部跑通
4. 补 **render-chart** example（当前无）
5. 涵盖全面、功能正确

## 待办（等用户指示方向）

1. **phase-14 分支 merge / PR**（等用户决定）。
2. **批 2（phase-15）**：轻量本地包分发（多 pool + `name@source`）+ workspace-instruction（SPEC 已预留 `AgentResolver` 接口 + `ResolveContext.extra_roots`）。
3. phase-12 / phase-13 分支 merge / PR（仍待）。

## 必读文件（下一任务开工前按需）

- [`docs/releases/2026-07-03-phase14-agent-first-class.md`](../releases/2026-07-03-phase14-agent-first-class.md)（phase-14 全貌 + 物化时序修正 + executor capability guard 修复 + 与并行进程边界）
- [`docs/specs/phase-14-agent-first-class.md`](../specs/phase-14-agent-first-class.md) §0.1 八铁律 + §3 解析层 + §5 Route 信息流 + §0.4 resources_root 裁定
- [`orca/compile/agents.py`](../../orca/compile/agents.py)（`AgentResolver`/`LocalPoolResolver`/`AgentHandle`，批 2 扩展点）

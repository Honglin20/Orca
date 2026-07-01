# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 1-10 全部完成并合并 master**。最近一次提交是 phase 10 MCP 壳：

- **状态**：✅ phase 10 MCP 壳全绿。MCP server（FastMCP + stdio）+ HandleId 四件套工具
  （start_workflow / get_task_status / resolve_gate / cancel_task）+ tape-only query path
  （pending_gates_from_tape + run_summary + cancel_run + workflow_cancelled 事件）+
  `orca mcp [--with-web]` 单进程多壳共存 + stdin EOF 双行为。任意 MCP 兼容客户端
  （CC / opencode / Cursor）可接入。
- **release note**：[`docs/releases/2026-07-01-phase10-mcp.md`](../releases/2026-07-01-phase10-mcp.md)
- **验收**：默认套件 652 passed / 1 skipped / 0 warnings（零回归）+ tests/iface/mcp/ 53 passed / 2 skipped
  + 5 个 E2E 闭环（4 CI + 1 integration skip 因无 API key）

## 下一步

phase 10 完成，Orca 三壳（CLI / Web / MCP）全部就位。可选后续方向（监工拍板）：

### 候选 A：路径 A（CC agent + skill 编排）
- phase 10 MCP server 已就绪作为 skill 驾驶对象
- 写一个 skill `/orca <yaml>` 教 Claude 调用 start_workflow → poll → resolve_gate
- 外置 cron（CronCreate / ScheduleWakeup）做定时汇报
- **较小工作量**（仅 skill + 模板，无核心代码改动）

### 候选 B：`render_chart` / `ask_user` MCP 工具
- 独立 SPEC，挂到 phase 10 server 骨架
- 9d 前端 chart 渲染就位，补 MCP 工具让 claude 能实际产出 chart/ask 事件
- 中等工作量（4 个 MCP 工具 + executor emit custom 事件 + 测试）

### 候选 C：9c deferred 根治
- n4 双轮询：`RunsListPage` + `RunsSidebar` 各自 `useRunsList`（2×/2s 元数据轮询）
- 根治需 React Context 提升 `useRunsList` 单实例
- 非阻塞，纯优化

### 候选 D：phase 8 跨工具（vendor-neutral 护城河）
- exec 层抽象第二个 agent backend（非 claude，如 codex / opencode）
- 验证 Orca 作为 vendor-neutral 编排框架的核心价值
- 大工作量

## 必读文件（下一阶段开工前）

1. [`docs/specs/shells-design-draft.md`](../specs/shells-design-draft.md)（三壳共同契约 + MCP 约束）
2. [`docs/releases/2026-07-01-phase10-mcp.md`](../releases/2026-07-01-phase10-mcp.md)（**最新**，phase 10 release note）
3. [`docs/specs/phase-10-mcp.md`](../specs/phase-10-mcp.md)（MCP 壳 SPEC，七铁律 + 跨客户端兼容）
4. [`orca/iface/mcp/server.py`](../../orca/iface/mcp/server.py)（OrcaMcpServer，候选 A/B 都要改这里）
5. [`orca/iface/web/run_manager.py`](../../orca/iface/web/run_manager.py)（RunManager，所有壳共享）

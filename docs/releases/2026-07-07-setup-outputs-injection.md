# Release Note —— setup_outputs 注入 runtime context（phase-10 🔴 技术债回填）

**日期**：2026-07-07
**背景**：phase-10 MCP v4（commit `df563f4`）遗留技术债——`setup_outputs` 在 MCP 层校验通过但**不注入** RunManager/orchestrator runtime context，render 层 `{{ setup.<agent>.output.<field> }}` 渲染不出。
**计划**：[docs/plans/2026-07-07-setup-outputs-injection.md](../plans/2026-07-07-setup-outputs-injection.md)
**范围**：只解 MCP 注入闭环；resume + setup、TUI/Web 自动执行 setup agent 声明为本次不做。

## 改动点（5 实现 + review 修复）

注入链：MCP 边界校验后的 `setup_outputs`（`{agent: {field: val}}`）→ `RunManager.start_run` → `_run_with_sem` → `Orchestrator.__init__` 包成 `{agent: {"output": raw}}` 存 `RunContext.setup` → render `_namespace` 暴露 `{{ setup.<agent>.output.<field> }}`。`_make_ctx`（node 间派生新 ctx）透传 setup。

| 文件 | 改动 |
|---|---|
| `orca/exec/context.py` | `RunContext` 加 `setup: dict` 字段（frozen dataclass 末位，default 空 dict）；**`with_locals` 改用 `dataclasses.replace`**（review 🔴 修复——原手工列字段漏传 setup，foreach body 引用 `{{ setup.* }}` 会静默拿空 dict） |
| `orca/exec/render.py` | `_namespace` 暴露 `"setup": ctx.setup` 根 |
| `orca/run/orchestrator.py` | `__init__` 加 `setup_outputs` 参数，包 `{"output": ...}` 存 `ctx.setup`；`_make_ctx` 透传 `setup=self.ctx.setup` |
| `orca/iface/web/run_manager.py` | `start_run` + `_run_with_sem` 穿透 `setup_outputs`；**resume + setup phase → fail loud**（边界声明） |
| `orca/iface/mcp/server.py` | `start_workflow` 把校验后 `setup_outputs` 真传 `start_run`（删 TODO）；清理过期 docstring |

所有新参数 keyword + default None → 老调用方（Web route / CLI / 现有测试）零影响。

## 数据形状契约

- MCP 入参：`{agent_name: {field: value}}`（裸 outputs，`setup_phase.validate_setup_outputs`）
- 模板：`{{ setup.<agent>.output.<field> }}`（compile validator 已放行 `setup` root）
- RunContext 存储：`{agent_name: {"output": {field: value}}}`（跟随 node outputs 约定，包装在 orchestrator 存储时做）

## code-reviewer 闭环

- 🔴 **`with_locals` 漏传 setup** → 改 `dataclasses.replace`（与 `with_guidance`/`with_dialog_turn` 派生 pattern 统一，根治未来加字段易漏）+ 补 foreach + setup 组合测试。
- 🟡 server.py 过期 docstring → 已清理。
- 🟡 catalog 物理位置跨子包耦合 → 保留 + 登记 follow-up。

## 测试

- `tests/exec/test_render.py`：`{{ setup.* }}` 渲染正例 + 空 setup 向后兼容（+2）
- `tests/run/test_orchestrator.py`：注入 → completed + None 不破普通 workflow（+2）
- `tests/run/test_foreach.py`：foreach body ctx 携带 setup（review 🔴 回归，+1）
- `tests/iface/web/test_run_manager.py`：端到端注入到 completed + resume+setup fail loud（+2）
- `tests/iface/mcp/test_e2e_setup_workflow.py`：强化 E2E——deploy 命令真消费 `{{ setup.collector.output.host }}:{{ ...port }}`，断言 `prod.example.com:22` 出现（之前命令没引用 setup，注入前也能过）
- `tests/iface/mcp/test_unit_tools.py`：mock 的 `_run_with_sem` 签名加 `setup_outputs`（+1 修）

## 验收

1. ✅ MCP setup workflow 给定合法 `setup_outputs` → start → poll completed，execute phase 真消费 `{{ setup.* }}`（E2E 实证 `deploy to prod.example.com:22`）
2. ✅ resume + setup → fail loud
3. ✅ foreach body + setup → setup 不丢失（review 修复）
4. ✅ 无 setup workflow 不受影响（回归）
5. ✅ **1688 passed / 0 回归**

## 声明的边界（follow-up，不在本次）

- 🔵 resume + setup：`workflow_started.data` 未持久化 `setup_outputs`，本次 fail loud 拦截。待 resume 路径回填持久化解锁。
- 🔵 TUI/Web 进程内自动跑 setup agent：orchestrator 仍不遍历 `wf.setup`。本次只解注入；注入到位后，TUI/Web 未来收集 setup_outputs 也能跑通。

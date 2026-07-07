# 计划 —— setup_outputs 注入 runtime context（phase-10 🔴 技术债回填）

**日期**：2026-07-07
**背景**：phase-10 MCP v4（commit `df563f4`）遗留：`setup_outputs` 在 MCP 层校验通过但**不注入**
RunManager/orchestrator runtime context，render 层 `{{ setup.<agent>.output.<field> }}` 渲染不出。
**目标**：让 MCP 壳 `start_workflow(setup_outputs=...)` 真正驱动 setup workflow 跑到 completed。

---

## 1. 范围（已与用户确认）

**本次做**：MCP 注入闭环——`setup_outputs` 从 MCP 边界一路穿透到 render，`{{ setup.* }}` 能渲染。

**本次不做（声明边界，留 follow-up）**：
- ❌ **resume + setup**：`start_run(resume=True)` 从 tape 恢复，`workflow_started.data` 目前不带
  `setup_outputs` → setup workflow 的 run resume 后 `{{ setup.* }}` 丢失。**本次声明：setup workflow
  暂不支持 resume**（fail loud：resume 模式下若 workflow 有 setup phase → 报错提示）。
- ❌ **TUI/Web 进程内自动跑 setup agent**（SPEC §0.1 rule 6 设想的 TUI/Web 路径）：orchestrator
  目前不遍历 `wf.setup`。本次只解「注入」，**不解「setup agent 自动执行」**。注入做完后，
  TUI/Web 若手动传 `setup_outputs`（未来 UI 收集）也能跑通。

## 2. 数据形状契约（已核对源码，无需新决策）

| 边界 | 形状 | 来源 |
|---|---|---|
| MCP 入参 | `{agent_name: {field: value}}`（裸 outputs） | `setup_phase.py:68` |
| 模板 | `{{ setup.<agent>.output.<field> }}` | `validator.py:591`（已放行 `setup` root） |
| RunContext 存储 | `{agent_name: {"output": {field: value}}}` | 跟随 node outputs 约定（`render._namespace`） |

**包装位置**：**orchestrator 存储**时包 `{"output": ...}`（与 node outputs 一致，render 只暴露不包）。

## 3. 改动清单（5 文件，下游单向依赖）

| 层 | 文件 | 改动 |
|---|---|---|
| exec | `exec/context.py` | `RunContext` 加 `setup: dict = field(default_factory=dict)`（frozen，末位） |
| exec | `exec/render.py` | `_namespace` 加 `ns["setup"] = ctx.setup` |
| run | `run/orchestrator.py` | `__init__` 加 `setup_outputs: dict | None = None`；包 `{"output": ...}` 存入 `RunContext.setup` |
| iface/web | `iface/web/run_manager.py` | `start_run` + `_run_with_sem` 加 `setup_outputs` 参数穿透；resume + setup phase → fail loud |
| iface/mcp | `iface/mcp/server.py` | `start_workflow` 把已校验 `setup_outputs` 传给 `start_run`（删 TODO） |

所有新参数 keyword + default None → 老调用方（Web route / CLI / 测试）零影响。

## 4. 测试

- `tests/exec/test_render.py`（或同位）：`{{ setup.x.output.y }}` 渲染正例 + 空 setup 不影响
- `tests/exec/test_context.py`：RunContext.setup 默认空 dict
- `tests/run/test_orchestrator*.py`：setup_outputs 注入 → render 能取到（用纯 script workflow + setup phase）
- `tests/iface/mcp/test_e2e_setup_workflow.py`：补**正例**——setup_outputs 给全 → 跑到 completed（现有是三重杠杆 B 拦截负例）
- 回归：`tests/iface/` + 全量套件

## 5. 验收

1. MCP setup workflow（`has_setup=true`）给定合法 `setup_outputs` → `start_workflow` 返 task_id →
   `get_task_status` poll 到 `completed`，execute phase 节点能消费 `{{ setup.* }}`。
2. resume + setup workflow → fail loud（边界声明）。
3. 无 setup phase 的 workflow → 不受影响（回归）。
4. 全量测试 0 回归。

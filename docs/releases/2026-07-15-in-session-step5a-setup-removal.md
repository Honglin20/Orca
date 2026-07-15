# Release: in-session v5 §8 step 5a —— 删 setup phase 全栈 + MCP migration note（A2 gate 保留）

**日期**: 2026-07-15
**Spec**: [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5 §6.1 / §6.2 / §8 step 5a
**Plan**: [`docs/plans/2026-07-15-in-session-step5a-setup-removal.md`](../plans/2026-07-15-in-session-step5a-setup-removal.md)
**Branch**: `in-session-unified-backend`
**Commit**: `<本 commit，single-commit；SHA 见 git log / CHANGELOG>`
**前置**: step 1 / 2b / 4 已 DONE　|　**解锁**: step 3b（catalog 物理迁）

## 做了什么

彻底删除 setup phase（旧路径 A 的「编排器在执行 agent 前先跑 `setup:` 段收集 outputs」机制）。
路径 B（主 session 驱动）下 setup 是**死代码**——其 outputs 由主 session 经 `orca next --output`
直接产出，setup 命名空间 / `setup_outputs` 参数 / `RunContext.setup` 都是无人消费的派生态。删 setup
= 消除与单 tape 真相源并存的平行派生数据路径（架构净化，非功能裁剪）。

**A2 铁律不破**：execute-phase gate 校验（`_check_execute_phase_no_gate_tools` /
`_INTERRUPT_TOOL_NAMES` / `_check_no_interrupt_tools`）保留，仅清理措辞中的 setup 引用。

按依赖方向自底向上（schema → compile → exec → run → iface(mcp/web/cli) → tests → 清理）：

### schema（`orca/schema/workflow.py`）
- 删 `Workflow.setup: list[AgentNode] = []` 字段。
- 改类 docstring：去 setup phase 描述段，补「setup phase 已在 in-session v5 §6.1 删除，旧 `setup:`
  段由 `extra="forbid"` 拒绝」。
- **保留** `model_config = ConfigDict(extra="forbid")` —— 删字段后 YAML `setup:` 段被它拒绝 =
  §6.2 m13 的 fail loud（零新代码）。

### compile（`orca/compile/validator.py`）
- 删 `validate_workflow` 对 `_check_setup_phase_constraints` 的调用。
- `_check_jinja2_refs`：`valid_roots` 去 `"setup"`（合法根只剩 `names | {"workflow","inputs"} | extras`）；
  docstring 去 setup 段。
- 删 `_check_setup_phase_constraints` 函数整体（+ 注释块）。
- **【A2 保留】** `_INTERRUPT_TOOL_NAMES` / `_check_execute_phase_no_gate_tools` /
  `_check_no_interrupt_tools` 函数体保留；仅清理 docstring / error message / 注释里的 setup 措辞
  （行为不变）。
- `compile/parser.py` 无 setup 代码，零改动（见「与计划的偏差」DISCREPANCY 1）。

### exec（`orca/exec/context.py` + `orca/exec/render.py`）
- `context.py`：删 `RunContext.setup` 字段；`with_locals` docstring 去 setup 措辞（`dataclasses.replace`
  机制保留）。
- `render.py`：删 `ns["setup"] = ctx.setup`；`_namespace` docstring 去 setup。

### run（`orca/run/orchestrator.py`）
- 删 `Orchestrator.__init__` 的 `setup_outputs` 形参。
- 删 `__init__` 内 setup_ns 注入块 + `RunContext(setup=setup_ns, ...)` 透传。
- 删 `_make_ctx` 内 `setup=self.ctx.setup, ...`。

### iface/mcp（核心，含 SPEC 未显式列的连带断裂点）
- **`server.py`**：
  - 删 `tool_get_agent_prompt` 方法 + 注册行（**MCP breaking change**，见末节 migration note）。
  - `tool_start_workflow`：删 `setup_outputs` 形参 + `from ...setup_phase import` + setup 校验块 +
    `validated` 透传（**MCP breaking change**）。
  - 删 4 个 setup hint 导入 + `get_setup_agent_prompt` 导入（agent_catalog 随之删）。
  - **连带（SPEC 未列，必改否则 KeyError / 断签）**：`tool_describe_workflow` 删 `has_setup` 读 +
    `for_describe_workflow(has_setup=...)` 调用签名；`tool_list_workflows` docstring 去 `has_setup` /
    `setup_outputs`；模块 docstring 去 setup。
- **`setup_phase.py`（124 行）整模块删**：导出 `SetupRequired` / `SetupOutputsMismatch` /
  `SetupOutputsInvalid` / `validate_setup_outputs` / `_validate_json_schema`，仅被 server.py import。
- **`agent_catalog.py`（103 行）整模块删**：核验 `_make_resolver_context` / `_extract_short_desc` 无
  非 setup 消费者（全仓 grep 确认仅 server.py import），整删安全（R1 闭环）。
- **`hints.py`**：删 `for_get_agent_prompt` / `for_setup_required` / `for_setup_outputs_mismatch` /
  `for_setup_outputs_invalid` + 注释；`for_list_workflows` / `for_describe_workflow` 去 `has_setup`
  参数与 setup 分支；保留 `after_start` / `by_status` / `after_cancel` / `unknown_task` /
  `for_get_task_history`。
- **`catalog.py`**：删 `list_workflows` 返回的 `"has_setup": bool(wf.setup)`；删 `describe_workflow`
  返回的 `setup` list / `has_setup` / `estimated_runtime`；删 `_estimate_runtime`；docstring 去 setup。
- **`__init__.py`**：tool 计数 9→8、Discovery 列表去 `get_agent_prompt`、setup 措辞清理（review N1）。

### iface/web（`orca/iface/web/run_manager.py`）
- 删 `start_run` 的 `setup_outputs` 形参。
- 删 resume + setup guard（`if resume and getattr(wf,"setup",None): raise`）。
- `_run_with_sem` 删 `setup_outputs` 形参 + 传给 Orchestrator 的透传。

### iface/cli（`orca/iface/cli/commands.py`）
- 删 `run_list` 的 `⚙setup` marker（`marker = " ⚙setup" if it.get("has_setup") else ""`）——catalog 删
  `has_setup` 后恒 None。**无 run setup 透传可删**（见 DISCREPANCY 2）。
- `iface/in_session/cli.py` 注释清理（review 增量修）。

### 死码清理 + 契约 doc
- **作者契约 doc 漂移（review MAJOR M1）**：`orca/skills/create-workflow/reference/orca-workflow-contract.md`
  —— 契约表删 `setup` 字段行、加「无 setup 字段 / `extra=forbid` 拒绝」段；「`ask_user`/`gate` 只允许在
  setup 阶段」改「execute phase agent 禁用」（setup 删后旧陈述变假，作者照旧写会被 fail-loud 拒）；
  Jinja2 合法根表 / 校验清单去 `setup`。
- `mcp/__init__.py` tool 计数对齐。

### gate 测试搬迁（关键，防覆盖丢失）
`tests/iface/mcp/test_setup_phase.py` 内 3 个 execute-phase gate 测试是
`_check_execute_phase_no_gate_tools` 的**唯一覆盖**（`tests/compile/test_validator.py` 当前 0 覆盖）。
删文件前**先搬**到 `tests/compile/test_validator.py`（逻辑归属 compile 层），去 setup 专属上下文使其
compile 自洽：
- `test_compile_rejects_ask_user_in_execute_phase`
- `test_compile_rejects_gate_in_execute_phase`
- `test_compile_allows_tools_none_in_execute_phase`

### MCP breaking change migration note（§6.2）
MCP 工具表是代码注册的契约，本步是 breaking change：
- **删 `get_agent_prompt` 工具**（setup 专用，无路径 B 消费者）。
- **`start_workflow` 去 `setup_outputs` 参数**（签名里的死参数）。

旧客户端迁移：不再调 `get_agent_prompt`；`start_workflow` 调用去 `setup_outputs`。
m13 fail loud 靠 pydantic `extra="forbid"`（零新代码，错误信息「Extra inputs are not permitted: setup」
足够清晰）；parser pre-scan friendly-error defer（见 DISCREPANCY 1）。

## 与计划的偏差

| # | 偏差 | 决策 |
|---|---|---|
| 1 | `compile/parser.py` 实读 **0 setup 代码**；SPEC §6.1「可选 pre-scan」= 可选*新增* friendly-error（m13），非删除项 | **defer** m13 parser pre-scan。pydantic `extra=forbid` 已 fail loud，pre-scan 是 UX 优化非必需，scope 纪律不做。 |
| 2 | `iface/cli/commands.py` 实读 run 命令路径**不传 setup_outputs**（SPEC §6.1 写「teams run setup 透传」）；唯一命中是 `⚙setup` marker | 只删 marker。SPEC 描述与代码不符，以代码为准。 |
| 3 | gate 测试在 `test_setup_phase.py` 内（唯一覆盖） | **先搬迁**到 `tests/compile/test_validator.py` 再删文件，防 A2 覆盖丢失。 |
| 4 | SPEC 未列的连带断裂（describe/list 工具读 `has_setup`、hints 签名） | 一并改（否则 KeyError / 断签）。 |
| 5 | gate 函数内 setup 措辞 | 清理（行为不变）。 |

**review 追加（计划未列、review 发现必做）**：
- 删 `tests/run/test_foreach.py::test_foreach_body_ctx_carries_setup`（review BLOCKER B1：setup 字段除后
  dataclass 构造抛 `TypeError: unexpected keyword argument 'setup'`，pytest 硬红；"透传不丢" 前提已不存在）。
- 契约 doc 漂移修正（review MAJOR M1，见上）。
- `tests/iface/mcp/test_unit_tools.py` fixture 含 `setup:` 段在 `extra=forbid` 下 load 失败，不能「改断言」
  只能删相关测试（review MAJOR M2）；保留并改 `test_list_workflows_returns_has_setup_flag` 断言 key 不存在。
- code-reviewer 增量修：`cli.py` 注释清理 + test 命名对齐。

## 验证

### 单测
- **1526 单测 passed**，8 failed 全 **pre-existing env-blocked**（缺 uv / 真 claude / env），stash 对比 clean
  HEAD 复现，**0 回归**。
- affected 套件：in_session + mcp + compile + exec + run + web + cli。

### test-agent 真机 E2E（opencode + deepseek-v4-flash，逐场景全绿）
1. `orca --help` / `orca list --json` 契约（无 `has_setup` / 无 `get_agent_prompt`）。
2. 3 节点 workflow bootstrap → next → completed，真 tape 事件序列闭环。
3. setup YAML 段 **fail loud**（`extra=forbid` 拒绝，exit 1）。
4. **A2 gate fail loud**：execute phase agent 配 `ask_user` → compile 拒绝。
5. `orca doctor` ok。
6. MCP 8 工具（原 9），无 `get_agent_prompt`。

### 未覆盖项（留后续）
- opencode skill 真跑完整 workflow（跨 WSL/Windows 部署，非代码，留用户侧）。
- teams nga/cac nudge 机制真机验证（step 6 范围）。

### code-reviewer
两轮审计：**0 BLOCKER / 0 MAJOR**（第二轮）。首轮 BLOCKER B1（test_foreach.py dataclass 构造硬红）+
MAJOR M1（契约 doc 漂移）+ MAJOR M2（test_unit_tools fixture）+ N1/N2（mcp __init__ tool 计数 / type-check
时序）全闭环。

## 已知 follow-up（非本步）

- **CLI traceback UX 瑕疵**（pre-existing，非 5a 引入）：某些错误路径输出对用户不友好的 traceback，留
  后续 UX 打磨。
- **step 5b**：daemon batch emit + 错误信封统一（计划已备
  [`docs/plans/2026-07-15-in-session-step5b-daemon-error-envelope.md`](../plans/2026-07-15-in-session-step5b-daemon-error-envelope.md)）。
- **step 6**：teams install nga/cac nudge 机制真机验证（留用户侧）。
- **m13 parser pre-scan**（defer，见 DISCREPANCY 1）。
- **合并推迟**：决策核心合并（`advance_step`↔`Orchestrator`），见 unified-backend draft，等触发条件。

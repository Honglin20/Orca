# Plan: in-session v5 §8 step 5a —— 删 setup phase 全栈 + MCP migration note

> SPEC：[`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5 §6.1 / §6.2 / §8 step 5a
> 侦察地图：见本计划 §2（基于 2026-07-15 代码实读，行号已核，与 SPEC 吻合）
> 分支：`in-session-unified-backend`　|　前置：step 1/2b/4 已 DONE　|　解锁：step 3b

---

## 0. 目标与成功标准

**目标**：彻底删除 setup phase（旧路径 A 的「编排器在执行 agent 前先跑 setup 段收集 outputs」机制）。路径 B（主 session 驱动）下 setup 是死代码——它的 outputs 由主 session 经 `orca next --output` 直接产出，setup 命名空间 / setup_outputs 参数 / RunContext.setup 都是无人消费的派生态。

**成功标准**：
1. `grep -rn "setup" orca/`（剔除 logging 的 `_setup_logging` 与历史措辞）= 仅注释/docstring 的解释性提及，**零活代码**。
2. `grep -rn "wf.setup\|RunContext.setup\|setup_outputs\|setup_ns\|has_setup\|tool_get_agent_prompt\|validate_setup_outputs" orca/` = 0。
3. `orca/iface/mcp/setup_phase.py` + `orca/iface/mcp/agent_catalog.py`（确认无活消费者后）整模块删除。
4. **A2 铁律不破**：`_check_execute_phase_no_gate_tools` / `_INTERRUPT_TOOL_NAMES` / `_check_no_interrupt_tools` 保留且其测试覆盖**不丢**（搬迁到 `tests/compile/test_validator.py`）。
5. YAML 含 `setup:` 段 → **fail loud**（pydantic `extra="forbid"` 拒绝，§6.2 m13）。
6. MCP breaking change 有 migration note（§6.2）：旧客户端不调 `get_agent_prompt`，`start_workflow` 去 `setup_outputs`。
7. 全量单测 0 回归 + in-session E2E（opencode + deepseek-v4-flash）绿。

---

## 1. 架构审视（改前必读）

### 1.1 setup 为什么该删 —— 单事实源论证

Orca 底线是「单 tape 唯一真相源 + 幂等 reducer + 一条读路径」。setup phase 违反它：
- `RunContext.setup`（exec 层）+ `setup_ns`（orchestrator 注入）+ `wf.setup`（schema）+ `setup_outputs`（跨 run/iface 透传）= **一条平行的派生数据路径**，与 tape 真相源并存。
- 路径 B 已让主 session 直接产 output（`orca next --output`），setup 的「收集 outputs」职责**无消费者**——它是死路径，不是备用路径。

删 setup = 消除平行数据路径，回归单真相源。**这是架构净化，不是功能裁剪。**

### 1.2 接口面影响 —— 单接口论证

MCP 工具表 = `iface/mcp/server.py` 注册（**代码即契约**，§6.2）。setup 在接口面留下两处死接口：
- `tool_get_agent_prompt`（整方法 + 注册行）——setup 专用，无路径 B 消费者。
- `tool_start_workflow` 的 `setup_outputs` 参数——签名里的死参数。

删它们 = 接口面不留死工具 / 死参数。**MCP 客户端可见面收窄到活的契约**，migration note 兜底旧客户端。

### 1.3 依赖方向影响

Orca 单向依赖铁律：`schema → compile → exec → run → iface(mcp/web/cli)`。setup 跨全层，删除须**按依赖方向自底向上**改（schema 先，iface 后），保证每一层删完后其下游不再被引用。本计划 §7 给出顺序。无反向依赖风险（侦察确认 setup_phase.py 仅被 server.py import；agent_catalog.py 仅被 server.py import；RunContext.setup 仅被 render.py + orchestrator 读）。

### 1.4 A2 铁律（不能误删）

`compile/validator.py` 的 `_check_execute_phase_no_gate_tools` + `_INTERRUPT_TOOL_NAMES` + `_check_no_interrupt_tools` 是 **execute-phase gate 校验**（execute phase 的 agent 禁用 `ask_user`/`gate` 工具），与 setup phase **正交**。保留函数本身；仅清理其 docstring/error message 里提及「setup phase」的措辞（行为不变）。`exec/mcp_tools/` grep 0 setup 命中，保留。

---

## 2. 删除范围（精确，基于侦察地图）

> 行号为 2026-07-15 实读值，与 SPEC §6.1 给值吻合（无漂移）。

### 2.1 schema —— `orca/schema/workflow.py` [改]
- **删** L289 `setup: list[AgentNode] = []` 字段。
- **改** L279-283 类 docstring 里 setup phase 描述段。
- **保留** L285 `model_config = ConfigDict(extra="forbid")` —— 删字段后 YAML `setup:` 段被它拒绝 = §6.2 m13 的 fail loud（无需新代码）。

### 2.2 compile —— `orca/compile/validator.py` [改]
- **删** L135 `validate_workflow` 对 `_check_setup_phase_constraints` 的调用。
- **改** L632 `valid_roots = names | {"workflow","inputs","setup"} | extras` → 去 `"setup"`；改 L622-623 docstring。
- **删** L772-819 `_check_setup_phase_constraints` 函数 + 注释块。
- **【保留 A2】** L728 `_INTERRUPT_TOOL_NAMES` / L731-754 `_check_execute_phase_no_gate_tools` / L757-769 `_check_no_interrupt_tools`。
- **【措辞清理】** L727 注释「自动注入 setup phase agent」+ L740-742 docstring + L767-768 error message 提「setup phase」——函数留，措辞改（去 setup 引用，行为不变）。

### 2.3 compile —— `orca/compile/parser.py` [**DISCREPANCY：无改动**]
- SPEC §6.1 列「可选 pre-scan」，但实读 **0 setup 代码**。SPEC 的「可选」= 可选*新增* friendly-error（m13），非删除项。**本步删除范围 = 空**。m13 的 parser pre-scan 见 §6 决策（defer）。

### 2.4 exec —— `orca/exec/context.py` + `orca/exec/render.py` [改]
- `context.py`：删 L72 `setup: dict[str, Any] = field(default_factory=dict)` 字段；改 L78-79 `with_locals` docstring（去 setup 措辞，`dataclasses.replace` 机制保留）。
- `render.py`：删 L60 `ns["setup"] = ctx.setup`；改 L50-51/58-59 `_namespace` docstring。

### 2.5 run —— `orca/run/orchestrator.py` [改]
- 删 L115 `setup_outputs` 形参（`__init__`）。
- 删 L156-164 setup_ns 注入块（comment + `setup_ns = {...}`）。
- 删 L170 `setup=setup_ns,`（RunContext 构造）。
- 删 L852-853 `_make_ctx` 内 `setup=self.ctx.setup,`（+ comment）。
- `_bare_instance`(L500-542) 未设 setup（默认空），无需改。`_DRIVE_REQUIRED_FIELDS` 不含 setup。

### 2.6 iface/mcp —— `orca/iface/mcp/server.py` + setup_phase.py + agent_catalog.py + hints.py + catalog.py [改/删]

**server.py**（核心，含 SPEC 未显式列的连带断裂点）：
- 删 L237-316 `tool_get_agent_prompt` 方法 + L551 注册行。
- `tool_start_workflow`(L320-401)：删 L324 `setup_outputs` 形参 / L351-356 `from ...setup_phase import` / L368-390 setup 校验块 / L395-397 `validated` 透传。
- 删 L42-52 的 4 个 setup hint 导入（L47 `for_get_agent_prompt`、L50-52 `for_setup_*`）。
- 删 L256 `get_setup_agent_prompt` 导入（agent_catalog 随之删）。
- **连带（SPEC 未列，必改否则 KeyError/断签）**：
  - `tool_describe_workflow`(L162-191)：删 L187 `has_setup=detail["has_setup"]` 读 + `for_describe_workflow(has_setup=...)` 调用签名；docstring L165-172 去 setup。
  - `tool_list_workflows`(L148-160) docstring 去 `has_setup`/`setup_outputs`（L152,154-156）。
  - 模块 docstring L1/9-10/20-21/27 去 setup。

**setup_phase.py**（124 行）：**整模块删**。导出 `SetupRequired`/`SetupOutputsMismatch`/`SetupOutputsInvalid`/`validate_setup_outputs`/`_validate_json_schema`，仅被 server.py L351-355 import（侦察确认 `__init__.py` 不导出，全仓无其它 import）。

**agent_catalog.py**（103 行）：**整模块删——但 coder 须先核验** `_make_resolver_context`(L34) / `_extract_short_desc`(L93) 无非 setup 消费者（`_extract_short_desc` 名字像通用 helper，名实不符时要抽走而非整删）。仅 server.py L256 import。核验通过则整删。

**hints.py**（155 行）：删 setup 段（`for_get_agent_prompt` L105-114 / `for_setup_required` L128-138 / `for_setup_outputs_mismatch` L141-146 / `for_setup_outputs_invalid` L149-154 / 注释 L125）；改 `for_list_workflows`(L79-84) 与 `for_describe_workflow`(L87-102，去 `has_setup` 参数 + setup 分支)；保留 `after_start`/`by_status`/`after_cancel`/`unknown_task`/`for_get_task_history`；改 docstring L9。

**catalog.py**（201 行）：删 L76 `"has_setup": bool(wf.setup)`；删 `describe_workflow` 返回的 L97-104 `setup` list / L105 `has_setup` / L106 `estimated_runtime`；删 L187-200 `_estimate_runtime`；改 docstring L48-49/89-91/190。保留 `list_workflows`/`describe_workflow`/`find_workflow*`/`_inputs_to_schema*`。

### 2.7 iface/web —— `orca/iface/web/run_manager.py` [改]
- 删 L264 `start_run` 的 `setup_outputs` 形参。
- 删 L282-289 resume + setup guard（`if resume and getattr(wf,"setup",None): raise`）。
- 改 L328 `self._run_with_sem(...)` 去 `setup_outputs` 透传。
- 删 L1210 `_run_with_sem` 的 `setup_outputs` 形参；删 L1228 传给 Orchestrator 的 `setup_outputs=setup_outputs`。

### 2.8 iface/cli —— `orca/iface/cli/commands.py` [**DISCREPANCY：只删 marker**]
- SPEC §6.1 写「teams run setup 透传」，实读 `run` 命令路径 **不传 setup_outputs**（`_serve_and_run_inprocess` L1323 / `_run_workflow_headless` L924 均无此参数）。**无透传可删。**
- 唯一 setup 命中 = L360 `marker = " ⚙setup" if it.get("has_setup") else ""`（`run_list` 显示）。catalog 删 has_setup 后恒 None → **删该 marker 逻辑**。

---

## 3. 测试改动

### 3.1 整删
- `tests/iface/mcp/test_e2e_setup_workflow.py`（169 行，全 setup E2E）—— 删整文件。
- `tests/iface/mcp/test_setup_phase.py` 的 setup 部分（L1-167 section 1-5 / L208-224 / L242-299）—— 删。
- **`tests/run/test_foreach.py`：L268-312** section 注释 + `test_foreach_body_ctx_carries_setup`（构造 `RunContext(setup=...)` + 断言 setup 经 `with_locals`/`dataclasses.replace` 透传）—— **删**（review BLOCKER B1：setup 字段除后 dataclass 构造抛 `TypeError: unexpected keyword argument 'setup'`，pytest 硬红；"透传不丢" 前提已不存在，无独立保留价值）。

### 3.2 **搬迁（关键，防覆盖丢失）**
`tests/iface/mcp/test_setup_phase.py` 内 3 个 execute-phase gate 测试是 `_check_execute_phase_no_gate_tools` 的**唯一覆盖**（`tests/compile/test_validator.py` 当前 0 覆盖）。删文件前**先搬**到 `tests/compile/test_validator.py`（逻辑归属 compile 层）：
- L172-188 `test_compile_rejects_ask_user_in_execute_phase`
- L191-205 `test_compile_rejects_gate_in_execute_phase`
- L227-239 `test_compile_allows_tools_none_in_execute_phase`

搬迁时去 setup 上下文（原文件 import / fixture 若 setup 专属则重构为 compile 自洽）。

### 3.3 改
- `tests/iface/mcp/test_unit_tools.py`（review MAJOR M2：fixture 含 `setup:` 段在 `extra=forbid` 下 load 失败，不能「改断言」只能删）：
  - **删** `test_get_agent_prompt_*`(L222/267)、`test_start_workflow_setup_*`(L373/393/414/433)、`test_list_workflows_has_setup_true`(L127，fixture 含 `setup:`)、`test_describe_workflow_returns_setup_metadata`(L177，fixture 含 `setup:`)。
  - **保留改** `test_list_workflows_returns_has_setup_flag`(L90，false 分支 fixture 无 `setup:`)—— 断言 has_setup/setup key **不存在** + inputs_schema 存在。
  - **改** L496 mock 形参去 `setup_outputs`（对齐 `_run_with_sem` 新签名）。
- `tests/iface/mcp/test_catalog.py`：改 `test_list_workflows_has_setup_true`(L100)、`test_describe_workflow_*`(L155/170)、L85/93 断言（key 不存在）。
- `tests/exec/test_render.py`：删 `test_render_template_setup_*`(L129/140) + section 注释 L126。
- `tests/run/test_orchestrator.py`：删 `test_setup_outputs_*`(L342/371) + section 注释 L339。
- `tests/iface/web/test_run_manager.py`：删 `_setup_workflow_yaml`(L259) / `test_start_run_injects_setup_outputs*`(L285) / `test_start_run_resume_with_setup_phase_fails_loud`(L310) + section 注释 L256。
- `tests/iface/cli/test_commands.py`：删 `SETUP` 常量(L207) + `test_list_marks_has_setup`(L239)（marker 逻辑删）。
- `tests/iface/in_session/test_v3_step1.py`：L210/249/253 断言「list 无 has_setup」**保留**（B3 守门，方向一致）；L495-497 canned mock 去 `"has_setup": False`（stale 清理）。

### 3.4 保留（无关）
- `tests/exec/test_result.py:115` `"missing setup_outputs"` 字符串 —— Error kind 测试任意 message，留。

---

## 4. 旧代码清理（改后）

侦察地图 §「断裂点」+ §4 已覆盖。改后须**清零**：
- 死模块：`setup_phase.py`、`agent_catalog.py`（核验后）。
- 死 hints：`for_get_agent_prompt` / `for_setup_*`。
- 死常量/fixture：test 里的 `SETUP` / `_setup_workflow_yaml`。
- docstring/注释漂移：`mcp/__init__.py`（L8-14 setup 措辞 **+ tool 计数 9→8 + Discovery 列表去 `get_agent_prompt`**，review N1）、各模块 docstring setup 措辞、validator gate 函数措辞。
- **作者契约 doc 漂移（review MAJOR M1）**：`orca/skills/create-workflow/reference/orca-workflow-contract.md` —— L17 契约表去 `setup` 字段行；L55「`ask_user`/`gate` 只能挂 setup 阶段 agent」改「execute phase agent 禁 `ask_user`/`gate`」（setup 删后陈述变假，作者照旧写会被 `extra=forbid` 拒）；L87/104 jinja2 合法 root 去 `setup`。**否则我们的契约 doc 在教作者写会被我们 fail-loud 拒绝的东西**。
- CLI `⚙setup` marker。

**守门**：§0 成功标准 1/2 的 grep 必须为 0（注释/docstring 解释性提及 OK，活代码 0）。

---

## 5. MCP breaking change migration note（§6.2）

MCP 工具表是代码注册的契约。本步是 **breaking change**：
- 删 `get_agent_prompt` 工具。
- `start_workflow` 去 `setup_outputs` 参数。

**migration note 落点**：
1. release note 专节「MCP breaking change」（旧客户端如何迁：不调 `get_agent_prompt`；`start_workflow` 去 `setup_outputs`）。
2. CHANGELOG 索引标注 breaking。
3. `server.py` 模块 docstring 顶部加一行 breaking note（指向 release note）。
4. **m13 fail loud**：靠 `extra="forbid"`（已存在，零新代码）；parser pre-scan friendly-error 见 §6 决策 defer。

---

## 6. DISCREPANCY 决策

| # | DISCREPANCY | 决策 |
|---|---|---|
| 1 | `parser.py` 无 setup 代码，SPEC「可选 pre-scan」= 新增 friendly-error | **defer** m13 parser pre-scan。pydantic `extra=forbid` 已 fail loud（错误信息「Extra inputs are not permitted: setup」足够清晰）。pre-scan 是 UX 优化，非 fail-loud 必需，本步不做（scope 纪律）。 |
| 2 | `commands.py` 无 run setup 透传，只有 `⚙setup` marker | 只删 marker（§2.8）。SPEC 描述与代码不符，以代码为准。 |
| 3 | gate 测试在 test_setup_phase.py 内，是唯一覆盖 | **先搬迁**到 `tests/compile/test_validator.py` 再删文件（§3.2）。 |
| 4 | SPEC 未列的连带断裂（describe/list 工具读 has_setup、hints 签名） | 一并改（§2.6），否则 KeyError/断签。 |
| 5 | gate 函数内 setup 措辞 | 清理（行为不变，§2.2）。 |

---

## 7. 实施顺序（layer order，单 commit）

按依赖方向自底向上，coder 可增量 type-check：
1. **schema**：删 `Workflow.setup` 字段（此时所有 `wf.setup` 读取点会报错，作为待改清单）。
2. **compile**：validator 删 setup 校验 + 改 valid_roots + 清 gate 措辞。
3. **exec**：context 删字段 + render 删 ns。
4. **run**：orchestrator 删 setup_ns/形参。
5. **iface/mcp**：catalog 删 has_setup/_estimate_runtime → hints 改签名/删 → server 删工具/改 describe+list → 删 setup_phase.py + agent_catalog.py（核验后）。
6. **iface/web**：run_manager 删 setup_outputs/guard。
7. **iface/cli**：commands 删 marker。
8. **tests**：搬迁 gate 测试 → 删 setup 测试 → 改断言测试。
9. **清理**：docstring/注释/mcp __init__。
10. **守门**：跑 §0 grep 1/2 = 0。

> **type-check 时序（review N2）**：删 schema 字段（step 1）会让所有 `wf.setup` 读点同时 type-error，中间态 step 1-4 **不全绿属预期**。type-check 全绿出现在 step 5（iface/mcp，describe/list 不再读 has_setup）之后。coder 按「待改清单」推进，勿把中间态当回归。

## 8. 验收 / 守门

- `grep -rn "wf.setup\|RunContext.setup\|setup_outputs\|setup_ns\|has_setup\|tool_get_agent_prompt\|validate_setup_outputs" orca/` = 0
- `tests/compile/test_validator.py` 含 3 个 execute-phase gate 测试（搬迁后）
- `pytest orca/tests`（或项目 test root）0 回归（affected：in_session + mcp + compile + exec + run + web + cli）
- in-session E2E（opencode + deepseek-v4-flash，demo 复制 workflows/）绿
- SPEC §11 验收：grep `setup`（schema + setup_phase）= 0；`orca list` 无 has_setup

---

## 9. 风险与回滚

- **风险 R1**：`agent_catalog.py` 整删误伤通用 helper（`_extract_short_desc`）。**缓解**：coder 删前 grep 全仓 import；名实不符则抽到 catalog.py 而非整删。
- **风险 R2**：gate 测试搬迁引入回归。**缓解**：搬迁不改测试意图，仅去 setup 上下文；搬迁后单独跑确认绿。
- **风险 R3**：MCP 旧客户端静默依赖 `get_agent_prompt`。**缓解**：migration note 显式 + breaking 标注（无法编译期保护，文档兜底）。
- **回滚**：单 commit，`git revert` 即可。无数据迁移（setup 是内存派生态，无持久化）。

---

## 10. 不在范围（out of scope，记 follow-up）

- **MCP `tool_describe_workflow` 是否与 `tool_list_workflows` 重复**（单接口原则的潜在 follow-up）：本步只清 setup 字段，不删 describe 工具。是否合并留独立 step 评审。
- m13 parser pre-scan friendly-error（defer，§6）。
- step 3b catalog 物理迁（依赖本步完成，独立 step）。

---

## 流程闭环

本计划 → **spec-review** 评审（架构/单事实源/单接口/旧码清理/守门）→ 通过后 **coder** 实现（按 §7 顺序）→ coder 自测单测 → **test-agent** in-session E2E → release note + CHANGELOG + CURRENT → commit。

# In-Session 统一后端合并 Spec（推迟）

> **状态**：Draft（2026-07-13 落，2026-07-14 拆分），**推迟，不在当前工作范围**。
> **推迟理由**：合并是「架构纯洁性投资」而非「解决眼前功能故障」。当前真正制造故障的（入口断链 / 误调 / 死代码）都能用小得多的动作解决（见 [`in-session-entry-and-simplification.md`](in-session-entry-and-simplification.md)）。两套决策核心已部分耦合（#5 已共享），维护成本被高估。
> **触发条件**（任一出现，合并从可选项变必选项）：
> 1. in-session 要加 parallel/foreach——两套各改一遍真痛了（决策 A a→b 升级路径）。
> 2. 两套产出 tape 在 **node 级**也开始分叉（web/TUI 因形态差异出 bug）——「单一真相源」被实际功能问题触碰。
> 3. 改一个编排逻辑要同步改 `advance_step` + `Orchestrator` 两处，且多次踩漏——漂移风险从理论变实际故障。
> **关联**：本次范围（批B收口 / 命令分家 / skill / 删setup / 输出契约 / CAC / 清理）见 [`in-session-entry-and-simplification.md`](in-session-entry-and-simplification.md)。
> **来源**：本 spec 从 2026-07-13 原始 draft 拆分而来，只保留合并主体（§0.1 现状裂缝 + §1 同一后端 core + §1.5 #1/#2）。

---

## 0. 背景：结构性裂缝

in-session（`orca in-session bootstrap/next`）与后端（`orca run`）当前是**两套代码**：

| | in-session（`orca in-session`） | 后端（`orca run`） |
|---|---|---|
| 决策核心 | `advance_step`（纯决策）+ CLI 自管 | `Orchestrator` 循环 |
| 写 tape | CLI 调 `emit_batch`（自己写） | Orchestrator 经 EventBus |
| 节点执行 | 主 session 派 task 子代理（core 进程外） | `Executor.exec` spawn subprocess（core 进程内） |

两套决策路径产出 tape 要**人工对齐**（CURRENT.md 反复纠结的「G2 序列对齐 vs `orca run` 同 wf tape」）。这违反「单一真相源 + 一条读路径」底线。

**但严重度被高估**：差异面清单（§1.1）已实证 #5（resume/next_node）**已经共享**——advance_step 已调 Orchestrator 类方法（`_inputs_from_tape`/`_next_node_for_resume`）。非「两套独立代码硬拼」，而是「主干已共享 + 两套驱动外壳」。#3/#4 是注释自标的 DRY 债，抽共享纯函数即可（本次范围 §7 已列入「防漂移穿插」），**不依赖合并**。

**目标**：同一后端 core，两个入口壳（`teams` / `orca`），消除决策核心 ×2 的存在本身。**G2 对齐问题随决策路径合一自动消失。**

**非目标**：重写已稳定的子模块（reducer / tape / router / translator 复用）。

---

## 1. 同一后端 core

### 1.1 单一决策核心 + 合并差异面

合并 `advance_step`（in-session 纯决策）与 `Orchestrator`（teams 循环）为**唯一决策核心**。

**合并差异面（2026-07-13 源码 diff）**——逐条对齐：

| # | 差异点 | advance_step | Orchestrator | 合并动作 |
|---|---|---|---|---|
| 1 | 节点类型 | 仅 agent（validator 拒 parallel/foreach/gate/ask_user）| 全类型 | **决策 A**（见下）|
| 2 | 驱动模型 | 单步外部驱动 | 内部循环 drive_loop | **决策 B**：core 支持两种驱动（§1.2 start/submit_output_and_advance）|
| 3 | inputs default | `_resolve_inputs`（DRY 债）| `__init__` 内联 | 抽共享纯函数（本次范围 §7 已提前做）|
| 4 | outputs 渲染 | `_final_outputs`（DRY 债）| `_evaluate_outputs` | 抽共享纯函数（本次范围 §7 已提前做）|
| 5 | resume/next_node | ✅ 已复用 `Orchestrator._inputs_from_tape`/`_next_node_for_resume` | 原始 | 已共享，无需动 |
| 6 | 中断/gate/ask_user | 不处理 | InterruptHandler/AgentToolsMcpServer/HumanGate | 随决策 A |
| 7 | max_iter/错误 taxonomy | 终态+InSessionError | max_iter+四类错误 | phase-11 ErrorKind 已部分统一 |

**决策 A（最大，已定 a）**：in-session 节点范围 = **a = 线性 MVP**。
- **MVP 支持**：agent 节点 + **routes 条件路由（含回边/循环）** + max_iter 兜底。「线性」≠ 只能直线——route 的 `to` 指向任意节点（含上游），`router.resolve` 无回边约束，故「校验不通过回上游」这类循环 MVP 即支持。
- **MVP 拒绝**（编译期 validator 拦）：parallel / foreach / gate / ask_user（in-session 下语义要重定义）。
- **扩展路径（a→b）**：ORCA 已有 parallel 完整实现，将来升级 = 把 parallel 决策搬进 core + InSessionExecutor 加「主 session 并发派 task 子代理」语义。

**决策 B（中等）**：驱动统一——core 同时支持 teams 内部循环 / orca 外部单步（§1.2）。

**扩展性预留（OCP，零代码成本，避免将来重写）**：
1. core 决策按 `node.kind` 开放分派（`match`，现仅 `case "agent"`）——加 parallel = 加分支，不改骨架。
2. executor 接口 = 单执行单元（`exec(node)`）——扇出归 core 决策层，executor 不背并发。
3. tape seq 兼容并发（append 时单调分配，已是）。

### 1.2 core API 契约（两壳共调）

```
core.start(wf, inputs, *, executor) -> {run_id, entry_prompt}
core.submit_output_and_advance(run_id, output, *, output_file=None) -> {done, next_prompt, next_prompt_file}
core.status(run_id) -> RunState
core.stop(run_id) -> {}
```

- **`teams` 壳**：`core.start(executor=SubprocessExecutor)` → core 内部驱动循环。
- **`orca` 壳**（in-session）：`core.start(executor=InSessionExecutor)` → core 返回 `entry_prompt` → 主 session 派子代理跑 → 主 session 调 `submit_output_and_advance(output)` → core 返回下一 prompt → 循环。

两壳**唯一差异**：节点由谁执行 + 谁驱动 advance 循环。决策 / 写 tape / router / reducer 完全共享。

### 1.3 executor 策略接口（节点由谁执行）

复用已存在的 `Executor` 抽象（`orca/exec/interface.py`：`exec(node, ctx) -> AsyncIterator[Event]`，executor 产事件流、**不写 tape**）。新增第二种实现：

| executor | 壳 | 节点执行方式 | 驱动方 |
|---|---|---|---|
| `SubprocessExecutor`（现有） | teams | spawn `claude/opencode -p`，stream-json → translator → 事件流 | core 内部循环 |
| `InSessionExecutor`（**新增**） | orca | **协作式**：不 spawn，core 把 node prompt 交给主 session；主 session 派 task 子代理跑完，产出经输出契约回传；core 经 translator 转事件流 | 主 session（外部）|

> ⚠️ **本 spec 的内在张力（合并前必须厘清）**：§1.3 声称两 executor「产出同一形态事件流」（含 `agent_tool_call`/`agent_thinking`），但本次范围 §5.2 已确认 in-session 硬边界——子代理内部 tool 过程不暴露给父 hook。故合并后两种 executor 的 tape **node 级一致，agent_tool_call 层并不一致**。合并的「形态完全一致」卖点因此打折：in-session 拿不到 agent_tool_call 层是固有，不是合并能消除的。合并真正消除的是「决策路径 ×2」，不是「tape agent 层不一致」。

### 1.4 tape 唯一真相源（不变）

web / TUI / CLI / in-session 都读同一 tape。in-session 开 web = `orca open` 复用 Web attach（read-only attach，已实现）。

### 1.5 合并专属清理（随合并一并做）

> 来源：原 draft §1.5 清理清单中**仅依赖决策核心合并**的两项。其余 10 项不依赖合并，已在本次范围 §7 清理。

| 多套并存 | 收口动作 |
|---|---|
| **决策路径 ×2**：`advance_step`（CLI 自管 tape）↔ `Orchestrator`（teams 循环）| 合一为 core（§1.1 差异面七条对齐）|
| **G2 序列对齐**：in-session vs `orca run` tape 人工对齐 | 随决策路径合一自动消失 |

---

## 2. 待定 / 风险

| # | 项 | 说明 |
|---|---|---|
| 1 | `advance_step` ↔ `Orchestrator` 合并差异面 | ✅ 已 diff（§1.1 七条 + #5 已共享）。剩**决策 A**（in-session 节点范围，已定线性 MVP）+ **决策 B**（驱动统一，§1.2）。|
| 2 | §1.3 「同一形态事件流」与 §5.2 in-session 硬边界的张力 | 合并前必须厘清：合并消除的是「决策路径 ×2」+「G2 node 级对齐」，**不是**「agent_tool_call 层不一致」（那是固有）。避免合并时为追求「完全一致」做过度抽象。|
| 3 | 同一后端合并是最大架构动作 | 必须经 `spec-review-adversarial` + `test-coverage-e2e` 真链路验证。触发条件见顶部。|

---

## 3. 决策清单（合并冻结，勿重新讨论）

1. **同一后端 core**，`teams` / `orca` 是入口壳（§1）。合并消除决策路径 ×2 + G2 对齐。
2. **executor 策略可替换**：`SubprocessExecutor`（teams）/ `InSessionExecutor`（orca，新增）。node 级 tape 一致；agent_tool_call 层 in-session 固有不可得。
3. **推迟执行**：合并不在当前工作范围，等触发条件（顶部三条）出现再开工。

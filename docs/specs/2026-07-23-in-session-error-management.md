# SPEC: in-session 错误管理 —— recoverable vs irrecoverable 分级（主 session 自由度）

> **状态**：定稿 **v2**（2026-07-23）。v1 经 spec-reviewer 对抗审视（conditional-pass，15 issue 全部源码核实为真），本版闭环全部 issue（E1–E15）+ 采纳 U1/U2/U3 推荐。进实现。
> **修订对象**：本 SPEC 有意修订 [`in-session-shell-design-draft.md`](./in-session-shell-design-draft.md) §2.5「失败 taxonomy」、D-v7-6（合规计数器）、D-v8.x-2（缺字段 fail loud）。凡与 §2.5 「统一 emit `workflow_failed`、不 emit `node_failed`」字面冲突处，以本 SPEC 为准。
> **范围**：仅 in-session 路径（`orca iface/in_session/`：`step.py` / `cli.py` / `_step_io.py` / TARS skill）。`orca run`（drive_loop）/ executor 路径零改——它们已用 `node_failed`（非终态）模式，本 SPEC 只是让 in-session 与其对齐。

---

## 0. 核心立场（一句话）

**in-session 的唯一调度者是主 session（一个有能力 agent）。引擎对「可恢复错误」不擅自判死 run——只把「不可恢复的 workflow 完整性失败」判终态。可恢复错误退回信封，把决策权交还主 session（重派节点 / 问用户 / `orca stop`）。**

这一立场**反转**了 §2.5 现状：现状把**所有** `InSessionError` 一律走 `workflow_failed`（终态）；本 SPEC 引入分级，让「子代理产出不合 schema」这类最常见的错误可恢复、run 存活。

---

## 1. 问题诊断（为什么现在"直接 fail 而非 resume"）

链路（现状）：

1. 子代理产出不合 `output_schema` → `run/step.py:146/158/164` `_parse_output` 抛 `InSessionError(error_kind=output_schema_mismatch)`。
2. `orca next` 的 `except InSessionError`（`cli.py:1310-1317`）调 `fail_in_session` → `_step_io.py:157-174` **emit `workflow_failed`** + `clear_marker`。
3. `workflow_failed` 落 tape → reducer 置 `state.status = "failed"`（`events/replay.py:125-132`，**终态**）。
4. 此后任何 `orca next --run-id X` 在 `advance_step` 开头被短路：`state.status in (completed/failed/cancelled) → {done:True, reason:"already_failed"}`（`step.py:335-336`）。run 死透。

**为什么 resume 救不了**：resume（续跑）只对 `state.status == "running"` 且有 pending 节点的 run 生效（无 output 调 `next` 幂等重发 prompt）。schema 不匹配走的是 `workflow_failed`（终态、marker 已清）——resume 的适用前提不成立。**不是没触发 resume，是 resume 的前提被现行错误处理提前破坏了。**

**根因**：错误分类被压扁——「可恢复的节点产出错误」与「不可恢复的 workflow 腐败」走同一条 `workflow_failed` 终态路径。§2.5 当时的选择（spike-2 实证「step.py 无 node_failed emit」）是基于「主 session 自己当判官」的设想，但**没有给主 session 任何"重试同一节点"的把手**——一旦 fail，主 session 只能 `stop`+重建，历史断裂。

**spec-reviewer 源码核实的契约前提（全部 TRUE，本 SPEC 成立）**：
- `events/replay.py:152-154`：`node_failed` 只置 `node_status[node]=failed`，**不动 `state.status`**（非终态）。
- `events/replay.py:140-145`：`node_started`「同 node 多 session（retry）取最后写入的 running」——重 arm 合法。
- `run/step.py:368`：`_parse_output` 在 `node_completed` **emit 前**抛（行 369 才 emit）→ recoverable 可改为 emit `node_failed` 而非已落的 `node_completed`。
- `exec/render.py:69-73`：`render_template` 把 Jinja 错包成 `ExecError` 时用 `raise ExecError(...) from e`，原异常作为 `__cause__` 保留（v1 不再需要此事实，见 §8 决议 3 撤销）。
- `run/lifecycle.py`：**无** `make_node_failed` helper（§6 决定 inline 4 字段，不新增 helper）。
- `cli.py:1310-1317,1483-1493`：分流 recoverable 不破坏 flock 临界区 / marker RMW（recoverable 路径在 advance_step 内决策，与现有 emit_batch 同批写）。

引擎里**早就有非终态节点失败的基础设施**，只是 in-session 没用：

- `node_failed` 在 reducer 里**只置 `node_status[node]=failed`，不动 `state.status`**——workflow 仍 `running`。
- `node_started` 重发即合法重试（last-writer-wins）。
- executor / drive_loop 路径一直用此模式（`exec/set_node.py:70`、`exec/claude/executor.py`：节点失败 emit `node_failed`，orchestrator 在 node 边界决定重试）。

---

## 2. 设计原则

- **P1 分级**：`recoverable`（节点级，run 存活）vs `irrecoverable`（workflow 完整性，run 判死）。判据见 §3。
- **P2 主 session 自由度**：recoverable 错误，引擎**不擅自终止** run，回退信封把决策权交主 session。
- **P3 确定性把手**：recoverable 错误，引擎**确定性地重 arm 当前节点**（emit `node_failed` + `node_started` + 重渲染 prompt），主 session 拿确定把手执行——而非靠模型自己推导该做什么。与 [[deterministic-over-model-mediated]] 一致：确定性边界在引擎，恢复决策在模型。
- **P4 有界**：同节点连续 recoverable 失败 ≥ **N=3**（与哨兵 `MAX_ASK=3` 对齐）→ 才升格 `workflow_failed`（防死循环）。主 session 可随时 `orca stop` 提前结束。

---

## 3. 错误分级表（完整 inventory + 改后处理）

> 「现状」列：当前全部 recoverable/irrecoverable 都走 `workflow_failed` 终态（除 bootstrap 期）。

| error_kind | 触发点（file:line） | 分级 | 改后处理 |
|---|---|---|---|
| **`output_schema_mismatch`** | `step.py:146`（非 JSON）/ `:158`（ValidationError）/ `:164`（SchemaError） | **recoverable** | emit `node_failed` + 重 arm 同节点 + 回 recoverable 信封（带错误详情）；主 session 把错误反馈给子代理重派 |
| **`render_error`**（含上游缺字段 / 模板语法错 / `_final_outputs` outputs 模板） | `step.py:181` `_render_or_fail`（Jinja `UndefinedError`/`TemplateSyntaxError`）/ `step.py:260` `_final_outputs` | **irrecoverable** | `workflow_failed`（保持）。**v1 不做 recoverable 化**：`_final_outputs` 在 `$end` 无节点可 re-arm（E4）；per-node 缺字段须把 UndefinedError 变量归因到上游节点（scope creep）；到 render 期才缺的字段本质是 wf-author bug（模板引用上游不产的字段，或上游 schema 过松）——required 字段缺失早被 `output_schema_mismatch`(recoverable) 抓住，到 render 期说明是设计 gap，重跑也修不了。recoverable 化作为 deferred enhancement |
| **`subagent_compliance`** | `cli.py:1487-1493`：`no_output_count ≥ 3` | **recoverable（降级为 warn）** | 不在 ≥3 判死；回 warn 信封（§4.1）提示主 session"连续 N 次无产出，请决策"。保留更高 **hard 上限 = 10** 才 emit `workflow_failed`（防主 session 完全失能的真卡死）。脚注：CC 路径下主 session 受 8-block Stop 上限约束，hard=10 实际不可达（session 先死）；hard=10 主要约束 opencode / 无 session 上限宿主（E15） |
| **`state_corrupt`** | `step.py:108`（多 running）/ `:364`（给 output 但无 running）/ `:393`（无 output 无 running）/ `cli.py:1407,1414,1420`（tape 无 ws / yaml 坏 / catalog miss） | **irrecoverable** | `workflow_failed`（保持）——workflow 完整性已坏，无法安全推进 |
| **`unsupported_node_kind`** | `step.py:409,414`：非 agent 节点（parallel/foreach/script/gate，v1 限制） | **irrecoverable** | `workflow_failed`（保持）；信封建议改用 `orca run` |
| **`internal_error`**（写 prompt / 兜底） | `step.py:203`（写 prompt OSError）/ `_step_io.py:49` 兜底 | **irrecoverable** | `workflow_failed`（保持）——资源/bug，无法在线恢复 |
| **`internal_error`（daemon `_acquire`）** | `daemon.py:86,96`：tape 被另一存活 daemon 占 / NFS 无 flock | **irrecoverable** | `workflow_failed`（保持）——无头 CI 启动护栏失败（E12） |
| **`internal_error`（marker 写失败）** | `cli.py:1121-1132`：bootstrap 写 marker OSError | **mid-bootstrap / irrecoverable** | 保持：tape 已 emit ws+ns 但 marker 写失败 → next 无 marker → 不可恢复；emit workflow_failed best-effort + exit 1（E13） |
| **`inputs_validation_error`** | `cli.py:1011`：bootstrap inputs 不符 type / 缺必填 | **pre-run**（run 未建） | 保持：修 inputs 重 bootstrap（无 run 可 resume，现行行为正确） |
| **`kb_requirement_failed`** | `cli.py:1024`：bootstrap 缺 KB | **pre-run** | 保持：供 KB 重 bootstrap |
| **`duplicate-active-run`** | `cli.py:1058-1068`：同 wf 已有活跃 marker | **非错误** | 保持：回 `reason=duplicate-active-run` + hint（续跑 / stop），**这是正确的引导行为**，不改 |

> **v1 recoverable 集合 = `output_schema_mismatch` + `subagent_compliance(warn)`**。这是 SPEC 主战场（子代理产出坏 JSON/schema 是最常见 case）。`render_error` recoverable 化 deferred。

---

## 4. 数据契约（envelope + event）

### 4.1 信封（`orca next` 回复）—— 三态

**(a) recoverable 信封**（`output_schema_mismatch` 未升格时）：

```json
{
  "done": false,
  "node": "<重 arm 的同一节点>",
  "prompt": "<重渲染的节点指令指针（与正常 next 同形）>",
  "recoverable": true,
  "error_kind": "output_schema_mismatch",
  "retry_count": 1,
  "retry_budget": 2,
  "reason": "节点 X 输出不满足 output_schema：字段 'foo' 缺失（路径 <root>）",
  "hint": "把上面的 reason 反馈给执行本节点的子代理，重派它产出修正后的 output，再 orca next --output"
}
```

**(b) compliance-warn 信封**（`no_output_count` 达 3 但未到 hard 10）（E3）：

```json
{
  "done": false,
  "recoverable": false,
  "warn": true,
  "error_kind": "subagent_compliance",
  "no_output_count": 3,
  "warn_threshold": 3,
  "hard_limit": 10,
  "reason": "subagent 连续 3 次未派 Task/产出 output",
  "hint": "主 session 连续 N 次未派 Task；请决策（继续推进 / orca stop）"
}
```

- `done:false` —— **run 仍 running，未终态**（与现状 `done:true` 的根本区别）。
- `retry_count` / `retry_budget` —— 透出剩余重试空间（`retry_budget = N − retry_count`），主 session 据此决策（接近上限可主动放弃 stop）。
- `error_kind` / `reason` —— 复用现有字段名。
- **warn ≠ recoverable**：warn 是"主 session 没派活"的提醒（无节点产出可反馈），recoverable 是"节点产出坏"的反馈重派。主 session 据 `recoverable:true` 走反馈重派分支，据 `warn:true` 走"决策是否 stop"分支。

### 4.2 事件序列（tape）

**recoverable（重 arm）**：emit 一批 `[node_failed, node_started]`（B1 单次 write 原子化，与现状 next 的 `[nc, rt, ns]` 同批写模式）。

- `node_failed` data **复用 executor 的 4-字段形态** `{kind, error_type, message, phase}`（`exec/interface.py:15`）；但 **`kind` 值是 in-session 专属字符串**（`output_schema_mismatch` 等），**故意不**是 `ErrorKind` 枚举成员（`exec/error_kinds.py:28-52`）——失败本体不同（in-session 是宿主协同错误，executor 是后端协议错误），不强求共享枚举（E6）。4 字段 inline 构造，不新增 lifecycle helper（YAGNI）。
- reducer 投影：`node_failed` → `node_status[node]=failed`；紧接 `node_started` → `node_status[node]=running, current_node=node`。净效果：节点回 running，`state.status` 始终 `running`（从未进 failed）。**幂等可重放**（G2 守门）。

**compliance-warn**：不 emit 任何 tape 事件（仅信封）；marker 的 `no_output_count` 仍按现有 RMW 累加（达 hard 10 才 emit `workflow_failed`）。

**升格（连续失败 ≥ N）**（E8 钉死 emit 顺序）：第 3 次失败**真实发生**，tape 必须记录——故**先 emit 第 3 次 `[node_failed, node_started]`，再 emit `workflow_failed`**。顺序：`nf → ns → workflow_failed`。这保证 count 重建 = `retry_count` 不变量（重放 tape 数当前节点连续 nf 恒 = 已重试次数）。`workflow_failed{kind=output_schema_mismatch, reason="consecutive recoverable exhausted: 节点 X 连续 N 次产出不合 schema"}` —— 终态。

### 4.3 consecutive 计数器（tape 派生，step.py 局部扫描）

- **机制**（E2）：新增 **`step.py` 局部扫描 helper `consecutive_fail_count(tape, node)`**——**不进 reducer fold**（保 `events/replay.py` 零改边界）。`advance_step` 在 recoverable 分支决策时调它。
- **重置谓词**（E1 钉死）：**从 tape 末尾向前扫，计 `node_failed(current_node)`；遇到 `node_completed(任意节点)` 即重置为 0**。v1 顺序单 running 节点下"任意节点 nc"与"当前节点 nc"等价（DAG 前向），但谓词显式写"任意节点"以备未来并行。
- 不入 marker（避免 desync，与「marker 只 3 字段」铁律一致）。
- 单测覆盖「中断后 resume，计数从 tape 正确恢复」（AC9）。

---

## 5. 主 session 恢复协议（TARS skill 改动）

`skills/tars/SKILL.md`「失败处理」段新增两分支：

**(A) recoverable 分支**（`orca next` 回复带 `recoverable:true`）：

1. **不 stop、不重启**。
2. 把信封 `reason` 反馈给节点子代理重派 → 拿新产出 → `orca next --run-id X --output '<新产出>'`。
3. 循环到通过 / 撞 `retry_budget`（主 session 可在撞 engine 升格前主动 `stop` 放弃）。
   - **同 session**（task_id 还在）：CC `SendMessage(task_id)` / opencode `Task(task_id=)` 复用**同一**子代理（与【哨兵处理】同源句柄捕获）。
   - **resume 跨 session**（E7）：原 task_id 已失，派 **fresh 子代理**；`retry_count` 从 tape 派生（跨 session 持续）；**主 session 须把 tape 中累积的 `node_failed` reason 历史注入 fresh 子代理首 prompt**（避免不公平升格——retry_count=2 时 fresh 子代理若只看到本次 reason 不知前两次为何失败，等于只剩 1 次机会）。这是 TARS skill 行为，实现计划须细化注入格式。

**(B) compliance-warn 分支**（回复带 `warn:true`）：

1. 当前节点仍是 pending；主 session **正常派 Task 子代理推进**即可（warn 只是提醒"你连续没派活"），或主动 `orca stop` 放弃。

> 与既有【哨兵处理】是姊妹机制：**哨兵** = 产出**前**缺必填项问用户；**recoverable** = 产出**后**不过校验带反馈重派。两者都"在 `orca next` 真正推进前消化掉坏产出"，区别在触发时机（缺必填 vs 校验不过）。

---

## 6. 受影响文件 / 边界

| 文件 | 改动 |
|---|---|
| `orca/run/step.py` | 新增 `RecoverableInSessionError(InSessionError)` 子类（仅 output_schema_mismatch 用）；新增 `consecutive_fail_count(tape, node)` 局部扫描 helper（§4.3）；`advance_step` 在 `output is not None` 分支 try/except recoverable：catch → 计 count；`< N` → 构 `[node_failed, node_started]` emits + 重渲染 prompt + 返 `StepResult(recoverable=True, retry_count=count)`；`≥ N` → **先构第 N 次 `[nf, ns]` emits 再追加 `workflow_failed` emit**（§4.2 E8），返 `StepResult(done=True, reason="consecutive recoverable exhausted")`。**修订 advance_step docstring**（E10）：从「纯决策：不写 tape」改为「决策 + recoverable 自恢复（emit-only；不写 tape，但走既有 `_deliver` 写 prompt 文件——与 pre-SPEC 行为一致，非新副作用）」 |
| `orca/iface/in_session/_step_io.py` | 新增 recoverable emit_batch + 信封拼装 helper（与 `apply_step_result` / `fail_in_session` 并列，三态：成功 / recoverable / 终态失败） |
| `orca/iface/in_session/cli.py` | `next` except 分流：`RecoverableInSessionError` → recoverable 路径（不清 marker，run 存活）；普通 `InSessionError` → `fail_in_session`（保持）；compliance 计数：≥3 不 emit workflow_failed（回 warn 信封），`≥ hard=10` 才 emit workflow_failed |
| `orca/iface/in_session/daemon.py` | **v1 订正**（E5）：recoverable（output_schema_mismatch）经 `advance_step` 自动复用；**compliance 不降级**（daemon 无 marker / 无计数器，daemon.py:117-140）；D-v7-6 死循环风险由 `_host_stale(idle_timeout_s)` 兜底（已知接受，非回归——daemon 从未有 compliance） |
| `orca/skills/tars/SKILL.md` | 失败处理段加 recoverable 恢复协议 + compliance-warn 分支 + resume 跨 session 注入历史 reason（§5） |
| `docs/specs/in-session-shell-design-draft.md` | §2.5 顶部加「被 2026-07-23-in-session-error-management.md 修订」标记 |

**边界（零改）**：`events/replay.py`（reducer 已支持 node_failed/node_started retry；**count 由 step.py 局部扫描派生，不进 reducer fold**）、`orca run` drive_loop、executor、schema/compile、marker schema（仍 3 字段）。

---

## 7. 验收（AC）

- [ ] **AC1（核心）**：节点产出不合 output_schema → run **不终态**；tape 出现 `node_failed`+`node_started`（节点回 running）；主 session 重派带反馈后 `orca next --output` 能推进到下一节点。
- [ ] **AC2（有界 + emit 顺序）**：同节点连续 **3 次** recoverable 失败 → 终态；**tape 含 3 条 `node_failed` + `workflow_failed`，顺序 `nf → ns → workflow_failed`**（第 3 次失败真实记录后终态）。计数从 tape 派生。
- [ ] **AC3（终态保留）**：`state_corrupt` / `unsupported_node_kind` / `internal_error` / **`render_error`（全部）** → 仍 `workflow_failed`（无回归）。
- [ ] **AC4（resume 不受影响）**：recoverable 不进 failed → 中途断连后 `orca status` 仍见 `resumable:true`，新 session 能续跑（run 还是 running）。
- [ ] **AC5（幂等重放）**：recoverable 路径的 tape 事件序列（含升格的 `nf→ns→workflow_failed`）经 reducer 重放，RunState 与执行时一致（G2 回归）。
- [ ] **AC6（信封契约）**：recoverable 信封含 `done:false, recoverable:true, error_kind, retry_count, retry_budget, reason, hint`；主 session 据此不 stop。
- [ ] **AC7（compliance 降级）**：`no_output_count ≥ 3` 不 emit workflow_failed，回 §4.1(b) warn 信封（含 `warn:true, no_output_count, warn_threshold:3, hard_limit:10`）；`no_output_count ≥ 10` 才 emit workflow_failed。
- [ ] **AC8（单测 + grep）**：(a) 单测：构造每类 recoverable error_kind（output_schema_mismatch），断言 cli next 返 `{done:false, recoverable:true}` 且**不 emit workflow_failed / 不 clear_marker**；(b) grep 守门：`raise.*ERR_OUTPUT_SCHEMA_MISMATCH` in step.py → 须为 `raise RecoverableInSessionError`。
- [ ] **AC9（retry_count 派生单测）**：`consecutive_fail_count` 对 4 类 fixture 正确——(i) 简单连续 nf；(ii) 被他节点 `node_completed` 重置；(iii) 被同节点 `node_completed` 重置；(iv) 跨 `workflow_started` 边界。
- [ ] **AC10（render_error 全 irrecoverable 回归）**：构造 render_error（缺字段 + 语法错 + outputs 模板），断言**不 emit `[nf, ns]`**，直接走 `workflow_failed`（recoverable 收窄验证）。

---

## 8. 决议（用户 2026-07-23「全部同意」+ spec-reviewer U1/U2/U3 推荐，已落定）

1. **升格上限 N = 3**（对齐哨兵 `MAX_ASK=3`）。不可配（YAGNI）。
2. **`subagent_compliance`**：≥3 降级 warn，**hard 上限 = 10** 才判死。
3. ~~**`render_error` 子类区分**（v1 决议）~~ → **撤销**（spec-reviewer E4/U1 闭环后 render_error 统一 irrecoverable，无需区分 Jinja 子类；`exec/render.py` 的 `__cause__` inspect 不再需要，实现简化）。render_error recoverable 化登记为 deferred enhancement（等真实需求 + UndefinedError→node 归因逻辑成熟）。
4. **consecutive 计数器**：**tape 派生**（SSOT，不入 marker）；**step.py 局部扫描 helper `consecutive_fail_count`，不进 reducer fold**（E2）。
5. **daemon 路径**：v1 recoverable 自动复用；**compliance 不降级**（无 marker），`_host_stale` 兜底（U3，E5 订正）。
6. **`retry_budget`**：**入信封**（值 = N − retry_count）。
7. **resume 跨 session**（U2）：retry_count 跨 session 持续（tape-SSOT），主 session 注入累积 reason 历史给 fresh 子代理（E7）。

---

## 9. spec-reviewer 闭环记录（v1→v2）

15 issue 全部闭环：E1（计数器重置谓词钉死）/ E2（step.py 局部扫描，不进 reducer）/ E3（compliance-warn 信封定义）/ E4（render_error 降 irrecoverable）/ E5（daemon 覆盖订正）/ E6（node_failed kind 值空间，非 ErrorKind 枚举）/ E7（resume fresh 子代理 + 注入历史）/ E8（升格 emit 顺序 nf→ns→workflow_failed）/ E9（被 E4 吞并）/ E10（advance_step docstring 修订）/ E11（并入 AC9）/ E12（daemon raise 站点入 inventory）/ E13（marker 写失败标签 mid-bootstrap）/ E14（AC8 拆单测+grep）/ E15（hard=10 跨宿主脚注）。无需第二轮对抗（所有 issue 文本可解，无设计返工）。

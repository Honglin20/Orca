# SPEC: in-session 错误管理 —— recoverable vs irrecoverable 分级（主 session 自由度）

> **状态**：定稿 v1（2026-07-23）。§8 开放问题经用户「全部同意」已落定（见 §8 决议）。进 spec-reviewer 审视。
> **修订对象**：本 SPEC 有意修订 [`in-session-shell-design-draft.md`](./in-session-shell-design-draft.md) §2.5「失败 taxonomy」、D-v7-6（合规计数器）、D-v8.x-2（缺字段 fail loud）。凡与 §2.5 「统一 emit `workflow_failed`、不 emit `node_failed`」字面冲突处，以本 SPEC 为准。
> **范围**：仅 in-session 路径（`orca iface/in_session/`：`step.py` / `cli.py` / `_step_io.py` / TARS skill）。`orca run`（drive_loop）/ executor 路径零改——它们已用 `node_failed`（非终态）模式，本 SPEC 只是让 in-session 与其对齐。

---

## 0. 核心立场（一句话）

**in-session 的唯一调度者是主 session（一个有能力 agent）。引擎对「可恢复错误」不擅自判死 run——只把「不可恢复的 workflow 完整性失败」判终态。可恢复错误退回信封，把决策权交还主 session（重派节点 / 问用户 / `orca stop`）。**

这一立场**反转**了 §2.5 现状：现状把**所有** `InSessionError` 一律走 `workflow_failed`（终态）；本 SPEC 引入分级，让绝大多数错误（含所有「产出不合 schema」）可恢复、run 存活。

---

## 1. 问题诊断（为什么现在"直接 fail 而非 resume"）

链路（现状）：

1. 子代理产出不合 `output_schema` → `run/step.py:146/158/164` `_parse_output` 抛 `InSessionError(error_kind=output_schema_mismatch)`。
2. `orca next` 的 `except InSessionError`（`cli.py:1310-1317`）调 `fail_in_session` → `_step_io.py:157-174` **emit `workflow_failed`** + `clear_marker`。
3. `workflow_failed` 落 tape → reducer 置 `state.status = "failed"`（`events/replay.py:125-132`，**终态**）。
4. 此后任何 `orca next --run-id X` 在 `advance_step` 开头被短路：`state.status in (completed/failed/cancelled) → {done:True, reason:"already_failed"}`（`step.py:335-336`）。run 死透。

**为什么 resume 救不了**：resume（续跑）只对 `state.status == "running"` 且有 pending 节点的 run 生效（无 output 调 `next` 幂等重发 prompt）。schema 不匹配走的是 `workflow_failed`（终态、marker 已清）——resume 的适用前提不成立。**不是没触发 resume，是 resume 的前提被现行错误处理提前破坏了。**

**根因**：错误分类被压扁——「可恢复的节点产出错误」与「不可恢复的 workflow 腐败」走同一条 `workflow_failed` 终态路径。§2.5 当时的选择（spike-2 实证「step.py 无 node_failed emit」）是基于「主 session 自己当判官」的设想，但**没有给主 session 任何"重试同一节点"的把手**——一旦 fail，主 session 只能 `stop`+重建，历史断裂。

引擎里**早就有非终态节点失败的基础设施**，只是 in-session 没用：

- `node_failed` 在 reducer 里**只置 `node_status[node]=failed`，不动 `state.status`**（`replay.py:152-154`）——workflow 仍 `running`。
- `node_started` 注释明写支持「同 node 多 session（retry）取最后写入的 running」（`replay.py:140-145`）——重发 `node_started` 即合法重试。
- executor / drive_loop 路径一直用此模式（`exec/set_node.py:70`、`exec/claude/executor.py`：节点失败 emit `node_failed`，orchestrator 在 node 边界决定重试）。

---

## 2. 设计原则

- **P1 分级**：`recoverable`（节点级，run 存活）vs `irrecoverable`（workflow 完整性，run 判死）。判据见 §3。
- **P2 主 session 自由度**：recoverable 错误，引擎**不擅自终止** run，回退信封把决策权交主 session。
- **P3 确定性把手**：recoverable 错误，引擎**确定性地重 arm 当前节点**（emit `node_failed` + `node_started` + 重渲染 prompt），主 session 拿确定把手执行——而非靠模型自己推导该做什么。与 [[deterministic-over-model-mediated]] 一致：确定性边界在引擎，恢复决策在模型。
- **P4 有界**：同节点连续 recoverable 失败 ≥ **N=3**（与哨兵 `MAX_ASK=3` 对齐）→ 才升格 `workflow_failed`（防死循环）。主 session 可随时 `orca stop` 提前结束。

---

## 3. 错误分级表（完整 inventory + 改后处理）

> 「现状」列：当前全部 recoverable/irrecoverable 都走 `workflow_failed` 终态（除 bootstrap 期 pre-run）。

| error_kind | 触发点（file:line） | 分级 | 改后处理 |
|---|---|---|---|
| **`output_schema_mismatch`** | `step.py:146`（非 JSON）/ `:158`（ValidationError）/ `:164`（SchemaError） | **recoverable** | emit `node_failed` + 重 arm 同节点 + 回 recoverable 信封（带错误详情）；主 session 把错误反馈给**同一**子代理重派 |
| **`render_error`（上游缺字段）** | `step.py:181` `_render_or_fail`：下游 prompt 引用上游 output 缺字段（Jinja `UndefinedError`） | **recoverable** | 同上；信封 `hint` 提示"上游节点 N 未产出所需字段，可能需重做上游" |
| **`render_error`（模板语法错）** | `step.py:181`：wf YAML 模板本身写错（`TemplateSyntaxError`） | **irrecoverable** | `workflow_failed`（wf 作者 bug，须改 YAML）——`UndefinedError` 与 `TemplateSyntaxError` 按异常类型区分 |
| **`render_error`（outputs 模板）** | `step.py:260` `_final_outputs` 渲染 wf.outputs 模板失败 | **看子类** | 同上两分（缺字段→recoverable / 语法错→irrecoverable） |
| **`subagent_compliance`** | `cli.py:1487-1493`：`no_output_count ≥ 3` | **recoverable（降级为 warn）** | 不在 ≥3 判死；回退信封提示主 session"连续 N 次无产出，请决策"。保留一个更高 **hard 上限 = 10**（防真卡死，主 session 完全失能时的最后兜底）。理由：model-driven 下主 session 有能力，compliance 当年的"防 Stop→resend 死循环"前提已弱化 |
| **`state_corrupt`** | `step.py:108`（多 running）/ `:364`（给 output 但无 running）/ `:393`（无 output 无 running）/ `cli.py:1407,1414,1420`（tape 无 ws / yaml 坏 / catalog miss） | **irrecoverable** | `workflow_failed`（保持）——workflow 完整性已坏，无法安全推进 |
| **`unsupported_node_kind`** | `step.py:409,414`：非 agent 节点（parallel/foreach/script/gate，v1 限制） | **irrecoverable** | `workflow_failed`（保持）；信封建议改用 `orca run` |
| **`internal_error`** | `step.py:203`（写 prompt OSError）/ `_step_io.py:49` 兜底 | **irrecoverable** | `workflow_failed`（保持）——资源/bug，无法在线恢复 |
| **`inputs_validation_error`** | `cli.py:1011`：bootstrap inputs 不符 type / 缺必填 | **pre-run**（run 未建） | 保持：修 inputs 重 bootstrap（无 run 可 resume，现行行为正确） |
| **`kb_requirement_failed`** | `cli.py:1024`：bootstrap 缺 KB | **pre-run** | 保持：供 KB 重 bootstrap |
| **`internal_error`（marker 写失败）** | `cli.py:1121-1132`：bootstrap 写 marker OSError | **pre-run/irrecoverable** | 保持：tape 已 emit ws 但 marker 写失败不可恢复，fail loud |
| **`duplicate-active-run`** | `cli.py:1058-1068`：同 wf 已有活跃 marker | **非错误** | 保持：回 `reason=duplicate-active-run` + hint（续跑 / stop），**这是正确的引导行为**，不改 |

> daemon 路径（`daemon.py:86,96`：tape 被另一 daemon 占 / NFS 无 flock）属无头 CI 形态，与主路径同一分级轴；本 SPEC v1 聚焦主路径，daemon 路径随主路径一并套用分级（open question 是否单列）。

---

## 4. 数据契约（envelope + event）

### 4.1 recoverable 信封（`orca next` 回复）

```json
{
  "done": false,
  "node": "<重 arm 的同一节点>",
  "prompt": "<重渲染的节点指令指针（与正常 next 同形）>",
  "recoverable": true,
  "error_kind": "output_schema_mismatch",
  "retry_count": 1,
  "retry_budget": 3,
  "reason": "节点 X 输出不满足 output_schema：字段 'foo' 缺失（路径 <root>）",
  "hint": "把上面的 reason 反馈给执行本节点的子代理，重派它产出修正后的 output，再 orca next --output"
}
```

- `done:false` —— **run 仍 running，未终态**（与现状 `done:true` 的根本区别）。
- `retry_count` / `retry_budget` —— 透出剩余重试空间，主 session 据此决策（接近上限可主动放弃 stop）。
- `error_kind` / `reason` —— 复用现有字段名（与 `fail_in_session` 信封同字段，值不同：recoverable=true 时主 session 不应判死）。

### 4.2 事件序列（tape）

**recoverable（重 arm）**：emit 一批 `[node_failed, node_started]`（B1 单次 write 原子化，与现状 next 的 `[nc, rt, ns]` 同批写模式）。

- `node_failed` data 形态对齐 executor（`exec/interface.py:15`）：`{kind, error_type, message, phase}`（DRY；kind = error_kind，phase = "output_validation" 等）。
- reducer 投影：`node_failed` → `node_status[node]=failed`；紧接 `node_started` → `node_status[node]=running, current_node=node`。净效果：节点回 running，`state.status` 始终 `running`（从未进 failed）。**幂等可重放**（G2 守门）。

**升格（连续失败 ≥ N）**：emit `workflow_failed{kind=<原 error_kind>, reason="consecutive recoverable exhausted: 节点 X 连续 N 次产出不合 schema"}` —— 终态。

### 4.3 consecutive 计数器落点

- **tape 派生**（**已定，SSOT**）：reducer / cli 重放 tape，对当前节点数「自上次 `node_completed` 以来的连续 `node_failed` 数」。不入 marker（避免 desync，与「marker 只 3 字段」铁律一致）。
  - 实现：新增 reducer 辅助或在 `advance_step` 决策时重放当前节点的连续 `node_failed` 计数（reducer 已 fold 全量 tape，可顺手抽出 per-node 最近一段）；单测覆盖「中断后 resume，计数从 tape 正确恢复」。

---

## 5. 主 session 恢复协议（TARS skill 改动）

`skills/tars/SKILL.md`「失败处理」段新增 recoverable 分支：

1. `orca next` 回复带 `recoverable:true` → **不 stop、不重启**。
2. 把信封 `reason` 反馈给**同一**节点子代理（CC `SendMessage(task_id)` / opencode `Task(task_id=)`，与【哨兵处理】复用同一子 agent 的句柄捕获同源）。
3. 子代理据反馈重产出 → `orca next --run-id X --output '<新产出>'`。
4. 循环到通过 / 撞 `retry_budget`（主 session 可在撞 engine 升格前主动 `stop` 放弃）。

> 与既有【哨兵处理】是姊妹机制：**哨兵** = 产出**前**缺必填项问用户；**recoverable** = 产出**后**不过校验带反馈重派。两者都"在 `orca next` 真正推进前消化掉坏产出"，区别在触发时机（缺必填 vs 校验不过）。

---

## 6. 受影响文件 / 边界

| 文件 | 改动 |
|---|---|
| `orca/run/step.py` | 新增 `RecoverableInSessionError(InSessionError)` 子类（recoverable kinds）；`advance_step` 在 `output is not None` 分支 try/except recoverable：catch → 构 `[node_failed, node_started]` emits + 重渲染 prompt + 返 `StepResult(recoverable=True, retry_count=...)`；连续 ≥N → raise 终态 `InSessionError(ERR_RECOVERABLE_EXHAUSTED)` |
| `orca/run/lifecycle.py` | （建议）新增 `make_node_failed(node, kind, message, phase)` helper（SSOT，executor 与 in-session 共享 node_failed data 形态） |
| `orca/iface/in_session/_step_io.py` | `apply_step_result` / 新增 `apply_recoverable_result` 处理 recoverable 的 emit_batch + 信封（与 `fail_in_session` 并列，三态：成功 / recoverable / 终态失败） |
| `orca/iface/in_session/cli.py` | `next` except 分流：`RecoverableInSessionError` → recoverable 路径（不清 marker，run 存活）；普通 `InSessionError` → `fail_in_session`（保持）；compliance 计数降级 warn（不 emit workflow_failed） |
| `orca/iface/in_session/daemon.py` | v1 一并随主路径套用分级（`_acquire` 的 tape 占用 / flock 失败仍 irrecoverable 终态；其 next 路径与 cli 同源经 `_step_io`，recoverable 自动复用） |
| `orca/skills/tars/SKILL.md` | 失败处理段加 recoverable 恢复协议（§5） |
| `docs/specs/in-session-shell-design-draft.md` | §2.5 顶部加「被 2026-07-23-in-session-error-management.md 修订」标记 |

**边界（零改）**：`events/replay.py`（reducer 已支持 node_failed/node_started retry）、`orca run` drive_loop、executor、schema/compile。

---

## 7. 验收（AC）

- [ ] **AC1（核心）**：节点产出不合 output_schema → run **不终态**；tape 出现 `node_failed`+`node_started`（节点回 running）；主 session 重派带反馈后 `orca next --output` 能推进到下一节点。
- [ ] **AC2（有界）**：同节点连续 **3 次** recoverable 失败 → `workflow_failed{reason="consecutive recoverable exhausted"}`（终态）。计数从 tape 派生（AC5 幂等保证）。
- [ ] **AC3（终态保留）**：`state_corrupt` / `unsupported_node_kind` / `internal_error` / 模板语法错 → 仍 `workflow_failed`（无回归）。
- [ ] **AC4（resume 不受影响）**：recoverable 不进 failed → 中途断连后 `orca status` 仍见 `resumable:true`，新 session 能续跑（run 还是 running）。
- [ ] **AC5（幂等重放）**：recoverable 路径的 tape 事件序列经 reducer 重放，RunState 与执行时一致（G2 回归）。
- [ ] **AC6（信封契约）**：recoverable 信封含 `done:false, recoverable:true, error_kind, retry_count, retry_budget, reason, hint`；主 session 据此不 stop。
- [ ] **AC7（compliance 降级）**：`no_output_count ≥ 3` 不再 emit workflow_failed，回 warn 信封（主 session 可决策）；`no_output_count ≥ 10`（hard 上限）才 emit workflow_failed。
- [ ] **AC8（grep 守门）**：recoverable kinds 的 raise 处用 `RecoverableInSessionError`；cli `next` 对其走非终态路径（单测覆盖）。

---

## 8. 决议（用户 2026-07-23「全部同意」，已落定）

1. **升格上限 N = 3**（对齐哨兵 `MAX_ASK=3`）。不可配（YAGNI；需要时再提）。
2. **`subagent_compliance`**：≥3 降级 warn，**hard 上限 = 10** 才判死（防主 session 完全失能的真卡死，最后兜底）。
3. **`render_error` 子类区分**：用 Jinja 异常类型——`UndefinedError`（上游缺字段→recoverable）vs `TemplateSyntaxError`（wf 语法错→irrecoverable）。**实现期必须验**：`orca/exec/render.py` 的 `render_template` 把 Jinza 错包成 `ExecError` 后，原异常是否作为 `__cause__`/`__context__` 保留可 `isinstance` 区分；若 `render_template` 吞掉了子类，则需让其在 `ExecError` 上携带原始异常类型标志（小改 render 层，属本 SPEC 实现范围）。
4. **consecutive 计数器**：**tape 派生**（SSOT，不入 marker）。
5. **daemon 路径**：v1 一并套用分级（与 cli 同源经 `_step_io`）。
6. **`retry_budget`**：**入信封**（透出给主 session 决策）。值 = N − retry_count。

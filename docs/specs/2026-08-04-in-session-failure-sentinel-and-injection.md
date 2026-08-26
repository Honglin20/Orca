# SPEC：in-session 失败哨兵 + 失败历史注入（next 单一校验关口收口）

> **状态**：定稿 **v3**（2026-08-04）。经 spec-reviewer 两轮对抗：round-1 conditional-pass（9 MAJOR + 15 MINOR，0 FATAL，契约前提全 TRUE）→ v2 闭环 → round-2 conditional-pass（1 MAJOR V2-2 + 2 MINOR，R-N2 驳回获 reviewer 认可）→ v3 闭环全部。进实现。
> **扩展**（非推翻）[`2026-07-23-in-session-error-management.md`](./2026-07-23-in-session-error-management.md)：复用其 recoverable 框架（`RecoverableInSessionError` / `consecutive_fail_count` / `_recover_step_result` / 升格 N=3），仅**收窄一个根因缺口**——「子代理报告失败」在 2026-07-23 是未定义盲区（recoverable 集合只含 `output_schema_mismatch`），主 session 因此临场截胡变 executor。
> **范围**：仅 in-session 路径（`orca/run/step.py` / `orca/iface/in_session/` / TARS skill）。`orca run` / executor 路径**零改**（自有 `retry_started`/`retry_exhausted`，`selectors.ts:734-739`）。
> **不在范围**（用户 2026-08-04 明确剔除）：**造假检测**（`torch.randn`/`fake_data` 等）不做。产出语义质量不进引擎判断——本 SPEC 只把「子代理自报失败」结构化，不裁判产出真假。

---

## 0. 核心立场（一句话）

**`orca next` 是「这次 attempt 是否通过」的唯一校验关口。主 session 是哑管道——永远把子代理最终消息喂 `next`（唯一例外：ask-user 哨兵，它是信息请求不是产出）。失败是一等公民：子代理自报失败经结构化哨兵 → 引擎判 `recoverable` → 确定性地把连续失败历史（含本次）注入重 arm 的 prompt。引擎给确定性把手（重 arm + 注入历史 + 信封），重派策略（复用同 agent / fresh）仍归主 session——不侵 in-session 自由度（承 2026-07-23 P2/P3）。**

---

## 1. 问题诊断（为什么主 session「忘了调 next」）

观察（用户 2026-08-04 tape）：setup 子代理返回失败报告 → 主 session 自己判「这是失败」→ **没喂 `next`**，转而 `ls`/`find`/`Read gpu_probe.py`、手搓 baseline.pt、纠结 `SendMessage` 路由 → `orca status` 永远 `running @ setup`（run 孤儿化）→ 重派新 agent 时前端不出第二轮。

根因（两层，均在交互路径，daemon 路径无此病）：

1. **续跑是 model-mediated**：TARS skill 把「驱动循环」写在 prompt 里（`SKILL.md:92`），靠 LLM 记得调 `next`。LLM 一见失败，「debug-and-fix」本能压过「call-next」职责，循环断。daemon 路径的 `_opencode_loop`（`daemon.py:170`）是 deterministic 续跑不变量，交互路径没有。
2. **「产出可不可接受」的裁判权在 LLM**：`SKILL.md:140` 授权「造假痕迹 → 不喂 next」，埋了「主 session 可裁决产出好坏并扣下」的坏先例；而「子代理报告失败」在 2026-07-23 是**未定义盲区**（recoverable 只含 schema 不合），LLM 套最近的「不喂 next」模式 + 临场发挥变 executor。

**前端不出第二轮**是同一根因的症状，非独立 bug：retry 绕过 `next` → 引擎没发 `[node_failed, node_started]` → 无新 `node_started` 分隔轮次；新 agent 的 `agent_*` 事件按 U1 规则（`sidechain_ingestor.py:93`）挂到仍开着的 `setup` → 与第一轮混桶。一旦 retry 走 `next`，`[node_failed, node_started]` 落 tape → 前端 LogStream 出 `node FAILED` + `node started` 两行（`selectors.ts:694,698`）→ 第二轮可见。**前端零改即可恢复刷新**。

**契约前提（spec-reviewer 逐字源码核实，全部 TRUE）**：
- **C1** `events/replay.py:152-154`：`node_failed` 只置 `node_status[node]=failed`，**不读 `data` 任何字段** → `data` 开放 dict，可 additive 扩展。
- **C2** `_step_io.py:70-91` `merge_recoverable_envelope`：generic，与 `error_kind` 取值无关 → 新 kind 零改透传。
- **C3** `cli.py:1433`（+ 升格 `cli.py:1449`）：generic 透传，无 `error_kind` 值分支。
- **C4** `daemon.py:117-149`：经 `advance_step`+`apply_step_result` generic 复用；`except InSessionError` 只捕 irrecoverable，recoverable 在 `advance_step` 自捕。
- **C5** `step.py` 全函数签名/调用点核实（**`_deliver` 有 4 个调用者**：`step.py:401/496/537/551`，非 3）。
- **C6** `_sentinel:"orca_ask_user_v1"` 在 `orca/run`+`orca/exec`+`orca/iface/in_session` **零出现**（仅 skills/docs/tests/web 编译产物）→ ask-user 是纯 skill 侧契约，L1 的 ask-user 例外是既有事实。
- **C7** `selectors.ts:694,698`：`[node_failed,node_started]` 渲染为 LogStream 两行；retry 样式 `selectors.ts:734-739` 属 executor 路径（本 SPEC 不碰）。

---

## 2. 设计原则（扩 2026-07-23 P1–P4，不推翻）

- **P5 单一关口**：`orca next` 是「本次 attempt 通过与否」的唯一裁判。
- **P6 哑管道**：主 session **永不裁判产出质量**。唯一保留的 pre-next 拦截是 ask-user 哨兵（信息请求，引擎无法代行）。其余一切产出**无条件喂 `next`**。
- **P7 失败一等公民（哨兵）**：子代理遇阻 → 返回结构化**失败哨兵**（`orca_node_failed_v1`，仿 ask-user 哨兵）。引擎 `_parse_output` 检测 → `agent_blocked` recoverable → 重 arm。
- **P8 确定性注入**：连续失败历史（**含本次**）由**引擎**从 tape + 当前 exc 合成、格式化、prepend 进重 arm 的 prompt（kind-aware，有界）。**取代** 2026-07-23 §5(A)3「主 session 跨 session 手动注入累积 reason」（model-mediated 脆点）。SSOT 在 tape。
- **承 P2/P3（不侵自由度）**：引擎**只**重 arm + 注入历史 + 回信封；**不**决定 fresh vs 复用、**不**自动 dispatch、**不**改节点自身 prompt 模板语义（注入是 additive prepend/append）。重派策略归主 session。

---

## 3. 失败 taxonomy 扩展（在 2026-07-23 §3 表上**加一行**）

| error_kind | 触发点 | 分级 | 处理 |
|---|---|---|---|
| **`agent_blocked`**（**新**） | `step.py _parse_output`：output 解析为 JSON dict 且 `_sentinel=="orca_node_failed_v1"` | **recoverable** | emit `node_failed{kind:agent_blocked, message, blocked_on?, tried?}` + 重 arm 同节点 + 注入失败历史（含本次）+ 回 recoverable 信封（`error_kind=agent_blocked`）。连续 N=3（与 schema 失败**合计**计数）升格 `workflow_failed` |

> v1 recoverable 集合由 2026-07-23 的 `{output_schema_mismatch}` 扩为 `{output_schema_mismatch, agent_blocked}`。两者共用 `_recover_step_result` + 升格逻辑 + 信封；区别仅在 `node_failed.data` 携带的额外字段与注入块文案。
> **升格 error_kind = 本次（最后）失败 kind**（非固定 `output_schema_mismatch`，见 §6.⑧）。

**检测顺序**（`_parse_output` 内，M3 钉死）：**① 首先**（在 `if not schema: return raw` 早返**之前**，`step.py:195` 之前）try `json.loads(raw)` → 若 dict 且 `_sentinel=="orca_node_failed_v1"` → raise `RecoverableInSessionError(error_kind=ERR_AGENT_BLOCKED, blocked_on=..., tried=...)`；**② 然后**走既有 schema 路径（无 schema 返 raw；有 schema 校验 → 不合则 `output_schema_mismatch`）。**关键：peek 必须在 `if not schema` 早返之前**——否则无 schema 节点（§4.4 核心场景）哨兵检测静默失效。
> **m7 实现注**：peek 成功的 `json.loads` 结果可复用——schema 路径优先用 peeked dict，避免非哨兵输出双解析。

---

## 4. 数据契约

### 4.1 失败哨兵 schema（子代理最终消息，agent-emitted）

```json
{
  "blocked_on": "<一句话：具体卡在哪>",
  "tried": ["<已尝试步骤1>", "<已尝试步骤2>"],
  "reason": "<可选：简短诊断>",
  "_sentinel": "orca_node_failed_v1"
}
```

- **必填**：`_sentinel`（恒 `"orca_node_failed_v1"`）、`blocked_on`（非空 str）。
- **可选**：`tried`（list[str]）、`reason`（str）。
- **检测**：仅靠 `_sentinel` 精确匹配 + `blocked_on` 非空（与 ask-user 哨兵对称：其 payload 是 `_orca_ask_user`，此处 payload 是 `blocked_on`）。**无 `_orca_node_failed` 布尔字段**（v1 误列，v2 删——角色冗余，M4）。
- **ingest 校验**（m3，引擎摄入时，确定性非 LLM）：
  - `tried` 非 list → coerce 为 `[str(tried)]`；元素非 str → `str()` 化。coerce **后**截断。
  - `blocked_on` / `reason` 非 str → `str()` 化。
- **保留键**：`_sentinel` / `_orca_*` 为 Orca 保留；节点 `output_schema` 不得定义（v1 文档声明保留，compile 期硬校验 deferred，§9 R2）。
- **形状校验**：`_sentinel` 精确匹配 + `blocked_on` 非空才认哨兵。`_sentinel` 匹配但 `blocked_on` 缺/空 → 仍判 `agent_blocked`（malformed，§4.2），**不**当成功放行。
- **限长**（ingest 截断）：`blocked_on` ≤200 字符；`tried` ≤5 项 × 每项 ≤120 字符；`reason` ≤200 字符。

### 4.2 `node_failed.data` 扩展（additive，reducer 零影响；承 2026-07-23 §4.2）

2026-07-23 §4.2 钉死的 4-字段形态 `{kind, error_type, message, phase}` 为**下限**，可 additive 扩展（reducer 不读 data 字段，C1 不变量）。`agent_blocked` **额外**携带可选字段：

```json
{"kind": "agent_blocked", "error_type": "agent_blocked",
 "message": "<blocked_on，或 reason，或 malformed 提示>", "phase": "agent_self_report",
 "blocked_on": "<非空时存在>", "tried": ["<非空时存在>"]}
```

- **畸形哨兵存储**（N5 钉死）：`blocked_on` 缺/空时 `message = "malformed sentinel: missing or empty blocked_on"`，且 data 中**省略** `blocked_on` 字段（不存 None）；`tried` 缺则省略。
- **message 优先级**：`message = blocked_on or reason or "malformed sentinel: ..."`（取第一个非空）。
- `output_schema_mismatch` 的 `node_failed.data` 不带 `blocked_on`/`tried`（向后兼容，不变）。

### 4.3 失败历史注入块（引擎 prepend，kind-aware，tape-sourced，bounded）

新增 `step.py` helper `consecutive_failures(tape, node) -> list[dict]`：从 tape 收集当前节点**连续** `node_failed(node)` 的 `data`（reset 谓词同 `consecutive_fail_count`：遇任意 `node_completed` 归零）。`consecutive_fail_count` 改为 `return len(consecutive_failures(...))`（DRY，单一扫描实现；物化 list O(k) 空间，k ≤ 2，可接受——N7）。

新增 `step.py` helper `_render_failure_history(records, retry_count, retry_budget) -> str | None`：`records` 空 → 返 None。非空 → 返有界文本块（最多 **N−1 = 2 条 total（含本次）**；V2-1：最后一次 re-arm 时 tape 上 N−2 条 prior + 本次 1 条 = N−1，N=3 时为 2，绝非 3；m5：第 N 次失败直接终态不 re-arm）：

```
## ⚠️ 本节点前序尝试失败（本次第 {retry_count}/{N} 次，耗尽将终止 run）

### Attempt 1 — failed [agent_blocked]
blocked_on: <blocked_on or message>
tried: [<tried 项 简短拼接>]

### Attempt 2 — failed [output_schema_mismatch]
<message>
```

- **kind-aware**：`agent_blocked` 显示 `blocked_on`（fallback `message`，N5）+ `tried`；`output_schema_mismatch` 显示 `message`。缺字段防御性 `.get(...)`，永不崩（robustness，AC11/13）。
- **纯文本拼接**：不进 Jinja（防注入），作为 literal prepend。
- **时序与「含本次」**（R-N2 收紧，**覆盖 reviewer N2**）：
  - **re-arm 路径**（`_recover_step_result`）：本次 `node_failed` emit 构造**后**、emit_batch 落 tape **前**计算——`records = consecutive_failures(tape, pending) + [本次 nf_emit.data]`。即 history **含本次失败**（从刚构造的 `Emit.data` 取，不从 tape 取——此时本次尚未落 tape）。本次是最 relevant 的失败，fresh agent 必须看到；否则在 L1 哑管道下（主 session 不再注入）本次失败只进信封 `reason`、到不了 fresh agent，形成盲区。
  - **幂等重发路径**（`advance_step` branch 4，M2）：无新失败，`records = consecutive_failures(tape, pending)`（全部 prior，已落 tape）。
  - 下一次失败时 `consecutive_fail_count` 已含本次（已落 tape），不会重复计数——每条失败在 history 中恰好出现一次。

**注入位置**（`_deliver` 内，顺序钉死）：`rendered = _render_or_fail(...)` →（`memory=True` 时）`rendered = inject_memory_prompt(...)` →（`failure_history` 非 None 时）`rendered = failure_history + "\n\n" + rendered` →（恒）append 失败哨兵教学脚注 → 写文件/返回。即最终 prompt = `[失败历史?] + [节点 prompt + 记忆?] + [哨兵教学脚注]`。`_deliver` 新增可选参 `failure_history: str | None = None`（m1：4 个调用者中 advance_step 内 **2 处 `496/537`** 保默认 None；`_recover_step_result:401` + 幂等重发 `551` 传计算值；V2-3：`:551` 不再同时出现在两列表）。

### 4.4 教学脚注（host contract，恒 append，极简）

每个 agent 节点 prompt 末尾由 `_deliver` 恒 append（首 attempt + 重 arm 均带）一段极简 host contract：

```
[Orca 失败协议] 若你无法完成本节点（遇阻 / 缺前置 / 执行出错），不要硬编造产出。
返回恰好这个 JSON 作为最终消息：{"blocked_on": "<具体卡点>", "tried": ["<已试步骤>"],
"reason": "<可选诊断>", "_sentinel": "orca_node_failed_v1"}
```

- 必要性：无 `output_schema` 的节点，agent 若不发哨兵、直接回 plain 失败文本 → 引擎无 schema 可挂 → 静默当成功推进（§9 R1）。教学脚注最大化合规。
- additive：不改节点自身任务指令；与 ask_user routing 脚注（`executor.py:447`）、memory 注入同属 host-contract append 模式。
- 层级归属：in-session host 契约 → 归 in-session 渲染点 `_deliver`（`run/step.py`），与 `exec/claude/executor.py` 的 ask_user 脚注分属两路、互不迁移（承 step.py 既有「daemon 独立实现，不 DRY drive_loop」边界）。

### 4.5 信封（复用 2026-07-23 §4.1(a)，零新字段）

`agent_blocked` recoverable 信封 = 既有形态，`error_kind="agent_blocked"`：

```json
{"done": false, "node": "<重 arm 同节点>", "prompt_file": "<重渲染指针，已含失败历史(含本次)+脚注>",
 "recoverable": true, "error_kind": "agent_blocked", "retry_count": 2, "retry_budget": 1,
 "reason": "节点 X 自报失败：blocked_on: ...", "hint": "<见 §6.⑩>"}
```

经 `merge_recoverable_envelope` generic 合并，cli/daemon 零改（C2/C3/C4）。

---

## 5. 主 session 协议（TARS skill 改动，itemized diff——N3）

`skills/tars/SKILL.md` 逐行改动：

| 位置 | 改动 |
|---|---|
| `:140-141` | **删**「造假痕迹 → 不喂 next」整条（造假检测不做；主 session 不裁判产出质量） |
| 驱动循环（§第 3 步，`:92-108`） | **改**：子代理最终消息**只有 ask-user 哨兵**走拦截小循环；**其余一律喂 `next`**（含失败报告——引擎哨兵检测/schema 校验判 `recoverable`） |
| `:144-150` recoverable 段 | **增** `agent_blocked` kind：信封 `error_kind=="agent_blocked"` 同走 recoverable 重派分支（与 `output_schema_mismatch` 同处理） |
| `:149` | **删/改**「把 tape 里累积的历次 reason 一并注入首 prompt」→ 改为「**历史已由引擎注入重 arm 的 prompt**（见 2026-08-04 §4.3），主 session 不再手动注入」 |
| `:198` success_criteria | **改** 过时表述「跨 session 注入累积 reason」→ 「重 arm prompt 已含失败历史（引擎注入）」 |
| `hint` 措辞 | 引用 §6.⑩（m2：hint 是引擎 `step.py:406-409` 生成，改动落点在 §6 非此处） |

ask-user 哨兵处理（`:110-141`）**保持不变**（L1 唯一例外，C6）。

---

## 6. 受影响文件 / 边界

| 文件 | 改动 |
|---|---|
| `orca/run/step.py` | ① 新常量 `ERR_AGENT_BLOCKED="agent_blocked"`；**②（M1）** `RecoverableInSessionError.__init__` 增可选参 `error_kind: str = ERR_OUTPUT_SCHEMA_MISMATCH, blocked_on: str \| None = None, tried: list[str] \| None = None`；哨兵路径 raise 时传 `error_kind=ERR_AGENT_BLOCKED`+结构字段。**5 个硬编码站点改读 `exc.error_kind`**：`_node_failed_data`（`:344-345`，且 additively 读 `exc.blocked_on`/`exc.tried`）、升格 `make_workflow_failed`（`:390`）、升格 `StepResult.error_kind`（`:397`）、re-arm `StepResult.error_kind`（`:418`）、constructor（`:99-100`）；**③（M3）** `_parse_output` 哨兵 peek 在 `if not schema: return raw` 早返**之前**（`:195` 之前），peek 成功的 dict 复用给 schema 路径（m7）；④ 新 `consecutive_failures(tape,node)`；`consecutive_fail_count` delegate；⑤ 新 `_render_failure_history(...)`；⑥ `_deliver` 增 `failure_history` 参 + 恒 append 教学脚注（4 调用者，m1）；**⑦（R-N2）** `_recover_step_result`：先构 `nf_emit`，`records = consecutive_failures(tape,pending) + [nf_emit.data]` → history → 透传 `_deliver`；**⑧（m4/m6）** 升格 `error_kind = exc.error_kind`，reason = `consecutive recoverable exhausted: 节点 {pending!r} 连续 {N} 次失败（{kind_breakdown}）`，kind_breakdown 由 `consecutive_failures` records 统计（如 `output_schema_mismatch×2, agent_blocked×1`）；**⑨（M2）** `advance_step` 幂等重发分支（`:551`）：`count = consecutive_fail_count(tape,pending)`；`count>0` 时 `failure_history = _render_failure_history(consecutive_failures(tape,pending), count, N-count)` → 透传 `_deliver`。**（V2-2 钉死，差一错修正）**：此分支 `count` 在所有 emits 落 tape **之后**取值——已含触发上次 re-arm 的那次失败（与 re-arm 路径「本次 nf 未落 tape、count 只含 prior」语义不同）。故此处 `count` 恰等于 re-arm 路径的 `this_attempt`（= re-arm 的 `count_before + 1`），retry_count 直接用 `count`（非 `count+1`）、retry_budget 用 `N-count`。验证：2 次失败后 re-arm（this_attempt=2, budget=1）vs 同态幂等重发（count=2, retry_count=2, budget=1）——一致；若误用 `count+1` 则显 budget=0 误导 fresh agent 放弃。；**⑩（m2）** `_recover_step_result` hint 措辞：「节点自报失败/产出不合 schema（第 {N}/{M} 次）。重 arm 的 prompt 已含历次失败原因（含本次）——按你的判断重派（复用同 agent 或 fresh），拿产出再 orca next --output（剩余 {budget} 次）」 |
| `orca/skills/tars/SKILL.md` | §5 itemized diff（6 行项） |
| `orca/iface/in_session/_step_io.py` | **零改**（C2 generic） |
| `orca/iface/in_session/cli.py` | **零改**（C3 generic） |
| `orca/iface/in_session/daemon.py` | **零改**（C4 generic；agent_blocked 同 schema 路径自动复用，同 2026-07-23 E5） |
| `orca/iface/web/frontend/*` | **零改**（核心修复，C7）：retry 走 next 即出第二轮。「attempt N/3」样式化 deferred（§9 R3） |
| `docs/specs/2026-07-23-in-session-error-management.md` | 顶部加「被 2026-08-04 扩展」标记；**§4.2 加注**（N4）：「4 字段为下限，可 additive 扩展（见 2026-08-04 §4.2：agent_blocked 额外 `blocked_on`/`tried`）；reducer 不读 data（C1 不变量）」；§5(A)3 改为「引擎注入（见 2026-08-04 §4.3）」 |

**边界（零改）**：`events/replay.py`（C1/C7）、`orca run` drive_loop、executor、schema/compile、marker schema。

---

## 7. 验收（AC）

- [ ] **AC1（哨兵 → recoverable，核心）**：子代理最终消息为合法失败哨兵 → run **不终态**；tape 出 `node_failed{kind:agent_blocked}` + `node_started`，且 **`node_failed.data` 含 `blocked_on`（=哨兵 blocked_on）+ `tried`（=哨兵 tried）**（AC gap 1）；`next` 回 `{done:false, recoverable:true, error_kind:agent_blocked, retry_count:1, retry_budget:2}`；重派带新产出后推进到下一节点。
- [ ] **AC2（历史注入，含本次）**：同节点第 2 次 recoverable（任意 kind 混合）→ 重 arm 的 prompt（compact 文件 / inline）顶部含「前序尝试失败」块，**列出第 1 次 + 第 2 次（本次）** 的 kind + blocked_on/message（+ tried if agent_blocked）；首 attempt（count=0）的 prompt **无**该块。
- [ ] **AC3（有界 + 升格 + 计数混合 + NC 重置）**：连续 3 次 recoverable（如 2×schema + 1×agent_blocked）→ 终态；tape 含 3 条 `node_failed`（kind 各异）+ `workflow_failed`，顺序 `nf→ns→workflow_failed`；升格 `workflow_failed.data.kind` = 本次 kind（非固定 schema_mismatch）。**增 fixture（N11）**：2×agent_blocked → `node_completed` → 1×schema_mismatch → 断言 count 归零后重新累计（不终态）。
- [ ] **AC4（教学脚注恒在 + additive）**：任意 agent 节点**首次 arm** 的渲染 prompt 末尾含失败协议脚注；**recoverable re-arm 后的 prompt 末尾也含脚注**（AC gap 2）；脚注为 literal append，**不**改节点 `agent.md` 任务指令语义（grep 断言脚注串在渲染产物末尾、节点模板文件本身不含）。
- [ ] **AC5（哨兵检测顺序，M3）**：节点有 `output_schema` 且 agent 发哨兵 → `agent_blocked`（**不**走 schema 校验）；节点**无** `output_schema` 且 agent 发哨兵 → `agent_blocked`（peek 在 `if not schema` 早返之前）。
- [ ] **AC6（畸形哨兵 fail-loud 倾向，N5）**：`_sentinel` 匹配但 `blocked_on` 缺/空 → 仍 `agent_blocked`；`node_failed.data.message` 含「malformed sentinel」；data 省 `blocked_on` 字段。
- [ ] **AC7（保留键冲突不崩）**：节点 output_schema 恰好含 `_sentinel` 字段 → 不崩；哨兵 peek 优先，判 `agent_blocked`（compile 硬校验 deferred §9 R2）。
- [ ] **AC8（前端零改回归，C7）**：构造 `[node_failed,node_started]` tape，断言前端 LogStream 出「node FAILED」+「node started」两行；retry 走 next 后第二轮可见（回归 2026-07-23 AC5 幂等重放）。
- [ ] **AC9（不侵自由度，P2/P3）**：引擎 recoverable 信封**不含**「复用/fresh」指令字段；`_recover_step_result` **不**调用任何 dispatch；主 session 保留决策权（单测断言 StepResult 无 dispatch 副作用）。
- [ ] **AC10（cross-session 重建，M2）**：resume 跨 session，新 session 的 `next`（无 output）走幂等重发分支 → 重发 prompt **已含** tape 重建的失败历史（全部 prior，SSOT 在 tape，不依赖主 session 注入）——取代 2026-07-23 §5(A)3。
- [ ] **AC11（DRY + robustness + 类型 coerce，m3）**：`consecutive_fail_count` delegate `len(consecutive_failures(...))`；`_render_failure_history` 对缺字段 data 不崩；**`tried` wrong-type fixture**（string/dict/int）→ coerce 为 list 不崩、不乱码。
- [ ] **AC12（cli/daemon/_step_io 零改守门，N6）**：grep `_step_io.py`/`cli.py`/`daemon.py` 的**可执行代码**（排除 docstring/comment）无 `agent_blocked` 字面分支。
- [ ] **AC13（`consecutive_failures` 直接覆盖，N8）**：返回 `list[dict]`，含 `node_failed.data`，顺序与 reset 谓词同 `consecutive_fail_count`；4 fixture（简单连续 / nc 重置 / 跨 ws / 缺字段 data 不崩）。
- [ ] **AC14（C1 回归守门，N9）**：AST grep 断言 `events/replay.py` 的 `node_failed` 分支不读 `data.*` 字段（守住 additive 扩展前提）。
- [ ] **AC15（ingest 限长截断，AC gap 3）**：构造超长 `blocked_on`（500 字符）+ 超 `tried`（10 项）→ 断言 `node_failed.data` 对应字段已截断到限长（200 / 5×120）。

---

## 8. 决议（v2，含 spec-reviewer 挑战闭环）

1. **失败信号 = 结构化哨兵**（非 LLM 质量裁判、非造假检测）。单元是 **NODE**（连续失败计数），非 agent。
2. **历史注入 = 引擎 deterministic prepend，含本次**（R-N2 收紧：含本次失败，否则 L1 哑管道下 fresh agent 盲区）。
3. **教学脚注 = 恒 append**（首 attempt + 重 arm），归 `_deliver`。
4. **L1 唯一例外 = ask-user 哨兵**（C6 既有事实）。
5. **重派策略（fresh/复用）归主 session**（P2/P3，引擎只给把手）。
6. **混合 kind 计数**：schema + agent_blocked 连续合计，撞 3 升格；升格 error_kind = 本次 kind。
7. **D-footer（教学脚注）**：spec-reviewer N1 挑战「恒 append 改变所有既有 prompt」→ **维持恒 append**（§8.7 决议）。「仅 re-arm append」的代价（首 attempt 无 schema 节点失败仍静默成功）正是本 SPEC 要修的根因；恒 append 是 tradeoff 正确侧。增 R5 登记 behavior change。

---

## 9. 已知残留 / deferred（显式登记，非回归）

- **R1 无 schema 节点 + 不合规 agent**：agent 忽略教学脚注、直接回 plain 失败文本，且节点无 output_schema → 引擎无抓手，**静默当成功推进**。不可避免（质量裁判被排除；哨兵是契约非强制）。教学脚注最大化合规；有 schema 节点不受影响（plain 失败文本不合 schema → recoverable 兜底）。
- **R2 保留键 compile 硬校验**：`_sentinel`/`_orca_*` 与 output_schema 冲突 → v1 仅文档声明 + 哨兵 peek 优先（AC7）；compile 期 fail-loud 拒绝 deferred。
- **R3 前端 attempt 样式化**：「retry 2/3」样式（对齐 executor 路径 `retry_started`）deferred；v1 仅 LogStream 两行（AC8）。
- **R4 L2 deterministic driver（`orca drive`）**：本 SPEC 是 L1（choke point 收口），不含 L2（交互路径 deterministic 续跑循环，复用 daemon `_opencode_loop` 思路）。L2 单独立项——它是唯一让「永远续跑」成真不变量的改动，scope 更大。
- **R5（N1）恒 append 教学脚注的 behavior change**：改变所有 in-session agent 节点 prompt 文本（含既有 workflow）。风险：已表现良好的子代理可能因新增指令行为漂移（如过度发哨兵）。缓解：脚注极简、不改任务指令语义、与 ask_user/memory 同模式。接受此 tradeoff 换无 schema 节点失败可见性。

---

## 10. spec-reviewer 闭环记录（v1→v2）

**Verdict: conditional-pass →（本版闭环后）可进实现。** 0 FATAL，9 MAJOR + 15 MINOR，全部 SPEC 文档完备性缺口（零设计返工）。7 契约前提逐字源码核实 TRUE。1 轮对抗（reviewer + evaluator），无设计权衡需用户决策。

闭环（MAJOR）：**M1**（`error_kind` 传播路径 + 5 硬编码站点改读 `exc.error_kind`，§6.②）/ **M2**（幂等重发分支注入历史 → AC10 可实现，§6.⑨）/ **M3**（哨兵 peek 钉死在 `if not schema` 早返之前，§3/§6.③）/ **M4**（删 `_orca_node_failed` 冗余字段，§4.1/§4.4）/ **m2→MAJOR**（hint 改动落点 §6.⑩，§5.4 改引用）/ **N2**（见下 R-N2 收紧）/ **N3**（§5 itemized SKILL.md diff）/ **N4**（2026-07-23 §4.2 additive 加注）/ **N5**（畸形哨兵存储钉死 §4.2 + AC6）。

闭环（MINOR）：m1（4 调用者）/ m3（tried 类型 coerce + AC11）/ m4/m6（升格 kind_breakdown §6.⑧）/ m5（≤N−1=2 prior）/ m7（peek 复用避免双解析）/ N6（AC12 排除 docstring）/ N7（物化 list O(k) 注）/ N8（AC13）/ N9（AC14 AST 守门）/ N11（AC3 NC 重置 fixture）/ AC gap 1（AC1 data 字段）/ AC gap 2（AC4 re-arm 脚注）/ AC gap 3（AC15 限长）。

**R-N2（本版对 reviewer N2 的收紧，surface-conflict 不 average）**：reviewer N2 主张「history 排除本次失败，本次只在信封 reason」。**本版驳回**：在 L1 哑管道下主 session 被明确禁止注入，本次失败若不进 prompt 则 fresh agent 盲区—— defeats 注入的目的。改为「history 含本次（re-arm 路径从 `nf_emit.data` 合成；幂等重发路径无本次）」（§4.3 时序与含本次）。下一次失败时 `consecutive_fail_count` 已含本次（已落 tape），不重复计数。

**未闭环需用户决策**：无。唯一 design call（D-footer 恒 append）由 §8.7 自行决议 + evaluator 挑战 + 主审维持 + R5 登记。

### round-2 闭环记录（v2→v3）

**Verdict: conditional-pass →（v3 闭环后）定稿进实现。** reviewer 核实 v1 全部 24 issue 在 v2 **真正**闭环（非仅文字，均有可验证 AC / file:line 落点）。**R-N2 获 reviewer 认可**（让步）：L1 哑管道下引擎是唯一能把本次失败放进 agent prompt 的实体，re-arm 路径覆盖 fresh 调度、幂等重发路径覆盖 cross-session——「含本次」是必要防御，非冗余。攻击 2/3（双计数 / bounds）被逐条证伪。

闭环（v2 新引入，3 项）：
- **V2-2（MAJOR，阻塞，差一错）** §6.⑨ 幂等重发分支 `retry_count` 公式：`count+1, N-(count+1)` → **`count, N-count`**。根因：该分支 `count` 在 emits 落 tape 后取值，已含触发上次 re-arm 的失败，语义不同于 re-arm 路径（count 只含 prior）。误用 `count+1` 会在 2 次失败后显 budget=0 误导 fresh agent 放弃。已附验证等式。
- **V2-1（MINOR）** §4.3 bounds 文本：删「2 条 prior + 本次 1 条 = ≤3」算术错（实际 max = N−1 = 2），改「最多 N−1 = 2 条 total（含本次）」。
- **V2-3（MINOR）** §4.3 `_deliver` 调用者列表：`:551` 原同时出现在「保默认 None」与「传计算值」两列表（M2 应用时未同步 m1）→ 改「advance_step 内 2 处 `496/537` 保 None；`:401` + `:551` 传值」。

reviewer 裁定：**无需第三轮对抗**——剩余 3 项均为文本/算术确定性修复，无设计辩论。v3 定稿，进实现。

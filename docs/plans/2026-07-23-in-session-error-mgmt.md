# 实施计划：in-session 错误管理（recoverable/irrecoverable 分级）

> **对应 SPEC**：[`docs/specs/2026-07-23-in-session-error-management.md`](../specs/2026-07-23-in-session-error-management.md)（定稿 v2，spec-reviewer 闭环）。
> **目标**：in-session 可恢复错误不判死 run，对齐 executor/drive_loop 的 `node_failed` 非终态模式。
> **v1 recoverable 集合**：`output_schema_mismatch`（重 arm）+ `subagent_compliance`（≥3 warn / ≥10 hard）。

---

## 依赖铁律自检（开工前确认）

改动跨 `run/step.py` / `run/lifecycle`（不改） / `iface/in_session/_step_io.py` / `iface/in_session/cli.py` / `iface/in_session/daemon.py`（仅注释订正） / `skills/tars/SKILL.md`。方向：run 层做决策（emit-only），iface 层做 IO 分流 + 信封。`events/replay.py` **零改**（count 走 step.py 局部扫描）。executor / drive_loop / schema / compile **零改**。

---

## 步骤（按依赖序，每步可独立 commit + 自 review）

### S1. `consecutive_fail_count` helper + 单测先行（E1/E2/E11，AC9）

- **文件**：`orca/run/step.py` 新增 `consecutive_fail_count(tape, node) -> int`。
- **逻辑**：从 tape 末尾向前扫；计 `node_failed(current_node)`；遇 `node_completed(任意节点)` 重置为 0（谓词见 SPEC §4.3）。
- **单测**：`tests/`（找现有 step.py 单测位置）4 类 fixture：简单连续 / 被他节点 nc 重置 / 被同节点 nc 重置 / 跨 ws 边界。**先于实现写**（TDD，守 count 语义）。

### S2. `RecoverableInSessionError` 子类（AC8b）

- **文件**：`orca/run/step.py`。
- `class RecoverableInSessionError(InSessionError)`（仅 output_schema_mismatch 用；其余 ERR_* 仍 plain `InSessionError`）。
- `_parse_output` 三处 raise（step.py:146/158/164）改抛 `RecoverableInSessionError(error_kind=ERR_OUTPUT_SCHEMA_MISMATCH)`。
- `render_error`（_render_or_fail / _final_outputs）**保持** plain `InSessionError`（irrecoverable，AC10）。

### S3. `advance_step` recoverable 分支 + 升格（AC1/AC2/AC5/AC10）

- **文件**：`orca/run/step.py` 的 `advance_step`，`output is not None` 分支。
- 把 `parsed = _parse_output(...)`（step.py:368）包 `try/except RecoverableInSessionError`：
  - `count = consecutive_fail_count(tape, pending)`（注意：此时本次失败的 nf **尚未落 tape**，count 是"已发生的前序连续失败数"；本次是第 count+1 次）。
  - **若 count+1 < N(=3)**：构 emits = `[node_failed{kind,reason}, node_started(pending)]`；重渲染 prompt（复用 `_deliver`）；返 `StepResult(done=False, node=pending, prompt/prompt_file, recoverable=True, retry_count=count+1, reason, hint)`。marker 不清。
  - **若 count+1 ≥ N**：升格——emits = `[node_failed, node_started, workflow_failed]`（顺序 SPEC §4.2 E8：nf→ns→workflow_failed）；返 `StepResult(done=True, reason="consecutive recoverable exhausted: ...")`。marker 清。
- `StepResult` 加字段：`recoverable: bool = False`、`retry_count: int | None = None`、`warn: bool = False`（compliance-warn 用，S5）。
- **修订 docstring**（E10）：advance_step 从「纯决策：不写 tape」→「决策 + recoverable 自恢复（emit-only）」。

### S4. `_step_io` recoverable/warn 信封 helper（AC6/AC7）

- **文件**：`orca/iface/in_session/_step_io.py`。
- `apply_step_result` 已处理成功 emits；**新增**：当 `result.recoverable` → 信封拼 `{done:false, node, recoverable:true, error_kind, retry_count, retry_budget=N-retry_count, reason, hint}`（与现有基础信封合并字段）。emit_batch 照常写 `[nf, ns]`（升格时含 workflow_failed）。
- compliance-warn 信封（S5 cli 层拼，不经 advance_step）。

### S5. `cli next` 分流 + compliance 降级（AC7/AC8a）

- **文件**：`orca/iface/in_session/cli.py`。
- `next` 的 `except InSessionError`（cli.py:1310）前**先**窄 catch `RecoverableInSessionError`：走 recoverable 路径——`apply_step_result` 已 emit（在 `_next_in_critical_section` 内 advance_step 已返 recoverable StepResult 并 emit_batch）→ 拼信封 echo + **不 clear_marker / 不 exit(1)**（0 退出，run 存活）。
  - **关键**：recoverable 在 advance_step 内自恢复（S3），不是 raise 出来——故 cli 层 `except` 不触发 recoverable；cli 层只看到 `result.recoverable=True`。**复核**：advance_step catch 自家 RecoverableInSessionError 后返 StepResult（不 re-raise）→ cli 走正常 `result` 路径拼 recoverable 信封。确认此控制流（S3 与 cli 的衔接）。
- compliance 计数（`_next_in_critical_section` cli.py:1483-1493）：`≥3` 不 emit workflow_failed，改**回 warn 信封**（done:false, warn:true, error_kind:subagent_compliance, no_output_count, warn_threshold:3, hard_limit:10）+ 0 退出；`≥ hard=10` 才 emit workflow_failed + exit(1)。
- 把 `_COMPLIANCE_LIMIT = 3` 改为 `_COMPLIANCE_WARN = 3` + `_COMPLIANCE_HARD = 10`。

### S6. daemon 订正（E5）

- **文件**：`orca/iface/in_session/daemon.py`（注释/docstring 订正，非行为）。
- 标注：recoverable 经 advance_step 自动复用；compliance 不降级（daemon 无 marker）；`_host_stale(idle_timeout_s)` 兜底。daemon 的 next 走同款 advance_step → recoverable 自动生效。

### S7. TARS skill 恢复协议（AC6 信封消费方）

- **文件**：`orca/skills/tars/SKILL.md`「失败处理」段。
- 加 (A) recoverable 分支：`recoverable:true` → 不 stop/重启 → 反馈 reason 给子代理重派（同 session SendMessage / resume 跨 session fresh 子代理 + 注入累积 reason 历史）→ `orca next --output`。
- 加 (B) compliance-warn 分支：`warn:true` → 正常派 Task 推进 / 或 stop。
- 与【哨兵处理】标注姊妹关系。

### S8. SPEC 修订标记 + release

- **文件**：`docs/specs/in-session-shell-design-draft.md` §2.5 顶部加「被 2026-07-23-in-session-error-management.md 修订」。
- 自 review（依赖铁律 / DRY / fail loud / 测试覆盖意图）。
- release note + CHANGELOG + CURRENT.md。

---

## 测试计划

### 单测（coder-agent 产出）
- `consecutive_fail_count` 4 fixture（AC9）。
- advance_step recoverable 分支：output_schema_mismatch → StepResult(recoverable=True, retry_count=1)；连续 3 次 → done + 升格 emits（AC1/AC2/AC5）。
- render_error 全 irrecoverable（AC10）。
- cli next recoverable：不 clear_marker / 不 exit(1) / 信封字段（AC6/AC8a）。
- compliance：no_output_count=3 → warn 信封 0 退出；=10 → workflow_failed exit(1)（AC7）。
- G2 回归：recoverable tape（含升格）经 reducer 重放 RunState 一致（AC5）。

### E2E（test-agent headless，AC1/AC2/AC4/AC7）
- 直接驱动 `orca` CLI 模拟主 session（bootstrap → next → next），不依赖真 agent。
- 用一个 2-3 节点、首节点声明 output_schema 的测试 wf。
- 场景：(1) 注入坏 JSON → recoverable 信封、run 存活、tape 有 nf+ns；(2) 再注入正确产出 → 推进下一节点；(3) 连续 3 次坏 → workflow_failed（tape 3×nf+wf）；(4) 中途断连（新 shell）`orca status` 见 resumable + `orca next` 无 output 重 arm；(5) compliance：连续不派 output 触发 warn 信封。
- 产真执行证据日志（每步 stdout JSON + tape 事件）。

---

## 风险

- **R1**：advance_step 自捕自家 raise 改变其"纯决策"契约（E10）——已在 SPEC §6 明示修订，单测守 G2。
- **R2**：cli 与 advance_step 衔接（recoverable 返 StepResult 而非 raise）须仔细——S3/S5 复核控制流，单测守 AC8a。
- **R3**：compliance warn 改动影响既有依赖 `no_output_count≥3→fail` 的测试——需同步改这些测试（grep `_COMPLIANCE_LIMIT` / `subagent_compliance`）。

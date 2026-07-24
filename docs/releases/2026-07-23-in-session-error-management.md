# Release: in-session 错误管理 —— recoverable / irrecoverable 分级

> **日期**：2026-07-23
> **SPEC**：[`docs/specs/2026-07-23-in-session-error-management.md`](../specs/2026-07-23-in-session-error-management.md)（定稿 v2，spec-reviewer 闭环 15 issue）
> **计划**：[`docs/plans/2026-07-23-in-session-error-mgmt.md`](../plans/2026-07-23-in-session-error-mgmt.md)（S1-S8）
> **分支**：`in-session-unified-backend`

---

## TL;DR

in-session 可恢复错误（`output_schema_mismatch`）不再判死 run —— 引擎 emit `[node_failed, node_started]`
重 arm 当前节点、回 recoverable 信封让主 session 重派子代理，连续 3 次同节点失败才升格 `workflow_failed`。
`subagent_compliance` 改双阈值（≥3 warn / ≥10 hard）。`render_error` 保持全 irrecoverable。
**v1 recoverable 集合 = `output_schema_mismatch`（重 arm）+ `subagent_compliance`（warn）**。

立场反转 SPEC §2.5：原「所有 InSessionError 一律 workflow_failed 终态」改为分级，让 in-session
对齐 executor / drive_loop 早就用的 `node_failed`（非终态）模式。

---

## 改动文件

### 引擎层（决策 + emit-only）
- **`orca/run/step.py`**
  - 新增 `RecoverableInSessionError(InSessionError)`（仅 `output_schema_mismatch` 用；其余 ERR_* 仍 plain `InSessionError`）。
  - 新增 `consecutive_fail_count(tape, node) -> int`：SPEC §4.3 局部扫描 helper，**不进 reducer fold**
    （保 `events/replay.py` 零改边界）。重置谓词：遇 `node_completed(任意节点)` 重置为 0。
  - 新增 `_node_failed_data(exc)`：4-字段 inline 构造（SPEC §4.2 E6，不加 lifecycle helper）。
  - 新增 `_recover_step_result(...)`：recoverable 自恢复决策 —— count+1<N 重 arm；count+1≥N 升格
    emit 顺序钉死 `[nf, ns, workflow_failed]`（E8）。
  - `advance_step` 在 `output is not None` 分支 try/except `RecoverableInSessionError`（不 re-raise，
    返 StepResult(recoverable=True)）；`_parse_output` 三处 raise 改抛 `RecoverableInSessionError`；
    `render_error`（`_render_or_fail` / `_final_outputs`）**保持** plain `InSessionError`（AC10）。
  - `StepResult` 加字段：`recoverable / warn / retry_count / retry_budget / error_kind / hint`。
  - docstring 订正（E10）：advance_step 从「纯决策：不写 tape」→「决策 + recoverable 自恢复（emit-only）」。

### iface 层（IO 分流 + 信封拼装）
- **`orca/iface/in_session/_step_io.py`**
  - 新增 `merge_recoverable_envelope(reply, result)`：cli/daemon 两路共用（DRY），合并
    `recoverable:true, error_kind, retry_count, retry_budget, hint`。
  - `apply_step_result` 调上 helper；并在 `result.done + result.error_kind` 时 `reply.setdefault("error_kind", ...)`
    （升格终态 surface error_kind，cli/daemon parity，code-reviewer 🟡#1 闭环）。
- **`orca/iface/in_session/cli.py`**
  - `_COMPLIANCE_LIMIT=3` → `_COMPLIANCE_WARN=3` + `_COMPLIANCE_HARD=10`（SPEC §3）。
  - `next` 控制流（R2 关键）：`RecoverableInSessionError` 在 advance_step 内自捕（不外抛）→ cli 走正常
    `result` 路径拼 recoverable 信封、**不 clear_marker / 不 exit(1)**；plain `InSessionError` 才走
    `except` → `fail_in_session`。
  - `_next_in_critical_section` compliance 计数三态：未达 WARN（计数）/ 达 WARN（warn 信封）/ 撞 HARD（workflow_failed）。
  - `next` 命令末段 exit code 三态：recoverable → 0；normal completed → 0；升格 / compliance hard / plain InSessionError → 1。
- **`orca/iface/in_session/daemon.py`**
  - 仅 docstring/注释订正（E5）：recoverable 经 advance_step 自动复用；compliance 不降级（daemon 无 marker）；
    `_host_stale(idle_timeout_s)` 兜底。零行为改动。

### TARS skill
- **`orca/skills/tars/SKILL.md`** —— 新增「可恢复错误（recoverable，run 不死）」+「合规 warn」两段：
  - (A) recoverable 分支：不 stop / 不重启 → 反馈 reason 给子代理重派（同 session SendMessage / 跨 session
    fresh 子代理 + 注入累积 reason 历史，防不公平升格）→ `orca next --output` 推进；撞 budget 前可主动 stop。
  - (B) compliance-warn 分支：正常派 Task 推进 / 或 stop。
  - 与【哨兵处理】标注姊妹关系。

### SPEC 修订标记
- **`docs/specs/in-session-shell-design-draft.md` §2.5 顶部**：加「被 2026-07-23-in-session-error-management.md
  修订」标记（凡字面冲突处以本 SPEC 为准）。

### 测试
- **`tests/iface/in_session/test_error_management.py`**（新）：SPEC §7 AC1/AC2/AC5/AC8/AC9/AC10 守门
  （15 测试）：`consecutive_fail_count` 4 fixture + 2 额外 / advance_step recoverable 分支 + 连续 3 次升格
  emit 顺序 / 单次 + 升格 tape 幂等重放（G2）/ render_error 全 irrecoverable / `_parse_output` grep 守门 /
  多节点计数不污染 / Jinja 语法错 render_error。
- **`tests/iface/in_session/test_daemon.py`**：补 daemon recoverable 闭环（坏→正解→完成）+ daemon 升格
  surface error_kind（cli/daemon parity）。
- **`tests/iface/in_session/test_in_session_cli.py`**：compliance WARN/HARD + recoverable cli 信封 +
  CLI 升格路径（3x → exit 1）+ AC4 跨 session 续跑 + AC3 internal_error 回归 + AC7 count=2 不 warn 反向断言。

---

## 关键控制流（R2 最易错，单测守）

```
advance_step(output=bad)
  ├─ _parse_output 抛 RecoverableInSessionError
  ├─ except RecoverableInSessionError（自捕，不 re-raise）
  │   └─ return _recover_step_result(...)  ← 返 StepResult(recoverable=True)
  └─ 不外抛 → cli next 走正常 `result` 路径
      ├─ apply_step_result emits = [nf, ns]（或升格含 workflow_failed）
      ├─ merge_recoverable_envelope(reply, result)  ← 拼 recoverable 字段
      ├─ 不 clear_marker（run 存活）
      └─ exit 0（升格才 exit 1）
```

**关键不变量**：recoverable **不经过** cli 的 `except InSessionError`（否则会走 `fail_in_session` →
`workflow_failed` 终态）。AC8a 单测守这条控制流（断言 cli next 返 `{done:false, recoverable:true}` +
不 emit workflow_failed + 不 clear_marker）。

---

## 边界（零改，已 git diff 核实）

`events/replay.py` / `orca run` drive_loop / executor / schema/compile / marker schema（仍 3 字段）
**全部零改**。`consecutive_fail_count` 是 `step.py` 局部扫描 helper，不进 reducer fold（SPEC §4.3 E2）。

---

## AC 覆盖矩阵（SPEC §7）

| AC | 测试 | 文件 |
|----|------|------|
| AC1 核心（不终态 + 重 arm + 重派推进） | `test_recoverable_output_schema_mismatch_re_arms` + `test_recoverable_then_correct_output_advances` + `test_failure_output_schema_mismatch` + `test_daemon_recoverable_then_correct_output_completes` | test_error_management.py / test_in_session_cli.py / test_daemon.py |
| AC2 有界 + emit 顺序 nf→ns→workflow_failed | `test_recoverable_escalation_after_3_consecutive` + `test_cli_recoverable_escalation_3x_exits_1` + `test_daemon_recoverable_escalation_3x_surfaces_error_kind` | 三文件 |
| AC3 终态保留（state_corrupt / unsupported / internal / render_error 全 irrecoverable） | `test_failure_unsupported_node_kind` + `test_failure_render_error_clears_marker` + `test_failure_internal_error_remains_irrecoverable` + `test_render_error_*` | test_in_session_cli.py / test_error_management.py |
| AC4 resume 不受影响（跨 session 续跑） | `test_recoverable_then_new_session_resumes_with_correct_output` + `test_recoverable_single_failure_tape_replays_to_running` | test_in_session_cli.py / test_error_management.py |
| AC5 幂等重放（含升格序列） | `test_recoverable_escalation_tape_idempotent_replay`（终态）+ `test_recoverable_single_failure_tape_replays_to_running`（中间态） | test_error_management.py |
| AC6 recoverable 信封字段 | `test_failure_output_schema_mismatch`（cli）+ `test_recoverable_output_schema_mismatch_re_arms`（advance_step）+ `test_daemon_recoverable_envelope_carries_error_kind`（daemon） | 三文件 |
| AC7 compliance 降级（WARN=3 / HARD=10） | `test_subagent_compliance_3x_no_output_warn_envelope` + `test_subagent_compliance_hard_limit_10x_emits_workflow_failed` | test_in_session_cli.py |
| AC8 (a) 单测 cli next 不 clear_marker / 不 exit + (b) grep `raise.*RecoverableInSessionError` | `test_failure_output_schema_mismatch` + `test_parse_output_raises_recoverable_not_plain` | test_in_session_cli.py / test_error_management.py |
| AC9 retry_count 派生 4 fixture | `test_consecutive_fail_count_*`（4 fixture + 2 额外） | test_error_management.py |
| AC10 render_error 全 irrecoverable | `test_render_error_is_irrecoverable` + `test_render_error_template_syntax_is_irrecoverable` + `test_failure_render_error_clears_marker` | test_error_management.py / test_in_session_cli.py |

---

## code-reviewer 闭环

两轮并行（impl + coverage），全部 issue 闭环：

**impl review** —— 0 🔴 + 2 🟡：
- 🟡 daemon 升格 reply 丢 `error_kind`（cli/daemon parity bug）→ 修：`apply_step_result` 加
  `reply.setdefault("error_kind", result.error_kind)` when `result.done + result.error_kind`。
- 🟡 daemon 升格测试缺 → 补 `test_daemon_recoverable_escalation_3x_surfaces_error_kind`。
- 🟢 `test_error_management.py:222` `and/or` 优先级 → 加括号。
- 🟢 `_recover_step_result` docstring 补 corner case（render_error 抛时已构 emits 被丢弃转 irrecoverable）。
- 🟢 `StepResult.warn` 字段保留（未来 daemon 引入 compliance 可复用）。

**coverage review** —— 2 🔴 + 4 🟡 + 4 🟢 全闭环或登记：
- 🔴 AC4 完全无覆盖 → 补 `test_recoverable_then_new_session_resumes_with_correct_output`。
- 🔴 AC5 缺中间态重放 → 补 `test_recoverable_single_failure_tape_replays_to_running`（1 次 recoverable
  tape 重放 → state.status='running'，AC4 续跑先决条件）。
- 🟡 AC1 daemon 闭环未测 → 补 `test_daemon_recoverable_then_correct_output_completes`。
- 🟡 AC2 CLI 升格未测 → 补 `test_cli_recoverable_escalation_3x_exits_1`。
- 🟡 AC3 internal_error 无 CLI 回归 → 补 `test_failure_internal_error_remains_irrecoverable`（mock os.replace）。
- 🟡 AC10 TemplateSyntaxError 子分支未测 → 补 `test_render_error_template_syntax_is_irrecoverable`。
- 🟢 AC7 count=2 不应 warn 反向断言 → 加 `assert "warn" not in r1/r2`。
- 🟢 多节点 recoverable 集成（用上 dead `_two_node_wf`）→ 补 `test_recoverable_multi_node_count_not_polluted_across_nodes`。
- 🟢 `_two_node_wf` fixture dead code → 通过上一测试启用。
- 🟢 WARN exit code 显式 → 通过 `_next` 默认 `expect_exit=0` 已隐式断言。

---

## 单测结果

- `tests/iface/in_session/test_error_management.py`：**15 passed**
- `tests/iface/in_session/test_daemon.py`：**7 passed**
- `tests/iface/in_session/test_in_session_cli.py`：**107 passed**
- `tests/events/test_replay.py`：**36 passed**（边界零改回归）
- `tests/iface/in_session/test_node_memory.py`：**24 passed**（无回归）

合计 **189 passed**。

## 已知无关失败（非本任务回归）

`tests/iface/in_session/test_v3_step1.py::test_entry_skill_md_has_no_business_logic_keywords`
—— SKILL.md 第 179 行（哨兵段，**非本任务改动**）的 `compile validator 铁律 7 不触发` 含禁词
`compile` 触发 grep 守门。pre-existing 失败（git stash 验证），登记后续修。

---

## 偏离计划

无。所有改动逐字贴合 SPEC v2 + 计划 S1-S8。code-reviewer 反馈全部闭环（含两处 parity bug 修复）。

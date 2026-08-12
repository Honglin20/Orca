# Release — Resume Failed Runs + Configurable Recoverable Escalation

**日期**: 2026-08-11
**SPEC**: [`docs/specs/2026-08-11-resume-failed-and-configurable-escalation.md`](../specs/2026-08-11-resume-failed-and-configurable-escalation.md)
**状态**: SPEC-REVIEW 闭环（12 issue）+ CODER 两轮（核心 + 下游缺口）+ TEST-AGENT 两轮真机 E2E 全 PASS，零产品 bug

---

## 动机（用户两件事）

1. in-session recoverable 失败（`output_schema_mismatch` / `agent_blocked`）连续 3 次硬升格 `workflow_failed`（`_RECOVERABLE_ESCALATE_AT=3` 硬编码"不可配 YAGNI"），跑 nas-supernet 等带 `output_schema` 的 workflow 时太容易把 run 整死。
2. run 一旦 `workflow_failed` 就无法续跑（`advance_step` 终态守卫死锁）。用户要「**通过 run-id resume 任意一个没跑完的 run（包括 fail 的）**」，且 resume 后 run 重新活跃、web 可见，像一直在续跑。

## 交付

### 一、可配升格上限（默认 20）
- `Workflow.recoverable_max_attempts: int = Field(default=20, ge=1)`（`orca/schema/workflow.py`）。同节点**连续** recoverable 失败次数上限；`node_completed`（任意节点）/ `workflow_resumed` 清零。与 `RetryPolicy.max_attempts`（transient 失败，独立预算）正交。
- 删硬编码 `_RECOVERABLE_ESCALATE_AT=3`（`orca/run/step.py`），替换为 `_DEFAULT_RECOVERABLE_MAX_ATTEMPTS=20`（schema default SSOT）+ 运行期读 `wf.recoverable_max_attempts`。全仓引用（含 docstring）清零。
- 老 yaml 无此字段 → 取默认 20，向后兼容。

### 二、resume 任意 failed run（核心）
- **`advance_step`**（`orca/run/step.py`）：终态守卫拆分——`completed`/`cancelled` 仍终态；**`failed` 走新 resume 分支**：target=`state.current_node`（失败节点，escalation + compliance 精确可定位）；非 agent → `failed_no_resumable_node`；否则 emit `[workflow_resumed(reason=recovered_from_failure), node_started]` + 渲染**含历次失败历史**的 prompt + 返 `StepResult(resumed=True, retry_count=0)`。`consecutive_failures` 加 `workflow_resumed` reset 边界（计数清零）。
- **reducer**（`orca/events/replay.py`）：`workflow_resumed` 从 no-op 改为**终态 failed→running 翻转**（幂等；崩溃-resume 时 status 本就 running，翻转 no-op，不破坏既有语义）。
- **CLI marker 重建**（`orca/iface/in_session/cli.py::_next_in_critical_section`）：marker 缺时 peek tape——`failed`→重建 `ActivationMarker(no_output_count=0)`；`cancelled`/`completed`→`done:true already_X`（reply 诚实，不再误导 `no-marker`）；余→`no-marker`。`write_marker` 包 try/except OSError → emit workflow_failed 翻回 failed（**自愈**，防 status=running+marker缺死锁）。

### 三、下游终态判定认 `workflow_resumed`（E2E 第一轮发现的缺口，SPEC §2.4）
`workflow_resumed` 是"重新激活"事件，但下游用「扫终态事件类型」启发式判定终态的消费者不认它 → resumed run 被误判终态。四模块各教自己的判定认 `workflow_resumed`（分层不允许跨模块抽共享 helper，各改是架构诚实）：
- **`chart_daemon._watch_terminal`**（sidechain 复用，一处修两处）：跨 poll `terminated` 标志——terminal 置 True、`workflow_resumed` 置 False、chunk 末才判退。resume 时 `next` 先写 tape 再 respawn daemon，新 daemon 从 offset 0 读 `[wf_failed, wf_resumed, node_started]` → terminated 终值 False → **存活**（修前 ~1ms 秒退，resume run 的 live web 图表全丢）。
- **`run_manager._scan_terminal_type` / `_probe_head_and_terminal` / `_scan_meta_overview`**：扫到 `workflow_resumed` 清 last_terminal → resumed run 判非终态 → attach 起 follow + `meta.status=running`（修前 stale=failed）。
- **`_tape_probe.scan_terminal`**：`workflow_resumed` 全清（terminal_count/types_seen/last_terminal）→ `orca stop <resumed-run>` 不误短路 already-terminal。
- **CLI gate**：见上 `cancelled`/`completed` 诚实 reply。

## 设计决策（SPEC-REVIEW + 用户确认）
- 可配字段挂 **workflow 级**（标量，所有节点共用），非 per-node。
- resume 触发 = **`next` 自动检测 failed 态并 re-arm**（零新命令，最贴合"通过 run-id resume"）。
- resume 后**喂回历次失败原因 + 计数清零**（fresh attempt，agent 能自我纠正）。
- E1 marker 写失败防护取**包裹整个 write_marker**（补 pre-existing gap）。
- E8 首次 resume `retry_count=0` / `failure_history` 非空——判定可接受（agent 需知历史，计数重开）。

## 验证

**SPEC-REVIEW**（spec-reviewer，2 轮对抗）：12 issue 全闭环（2 HIGH + 5 MEDIUM + 6 LOW），含路径订正、marker 写失败死锁自愈、AC11 语义重写、irrecoverable 定位精度限制、headless 行为变化声明。

**单测**（两轮 CODER，全绿）：
- 核心：`test_advance_step_resume_failed`（AC1/3/5/6/7/11）、`test_reducer_workflow_resumed_flip`（AC4 全状态矩阵 + 幂等）、`test_error_management` + `test_failure_sentinel`（fixture 显式设 `recoverable_max_attempts=3`）、`test_replay`（workflow_resumed 条件 no-op 标注）。
- 下游：`test_chart_daemon`（resume 存活 / 纯终态退 / resume 后完成退）、`test_run_manager`（scan/probe/meta_overview resume 翻转）、`test_tape_probe`（resume 重置 + 重复/矛盾防护）、`test_scan_meta_overview_contract`、`test_in_session_cli`（AC6 诚实 reply + tape 不变性）。

**TEST-AGENT 真机 E2E**（两轮，WSL 真 `orca` CLI，隔离 ORCA_HOME）：
- 第一轮：核心 resume 逻辑（AC1/3/4/5/7/9/11）全过、零核心 bug；发现 3 个相邻层缺口（→ 第二轮修复）。
- 第二轮：AC6/AC8/AC10 三处修复全部验证生效（chart daemon resume 后存活、web 函数层 status=running、cancelled/completed 诚实 reply），AC1/3/4/5/7/9/11 无回归，`orca stop <resumed-run>` 正常。**零产品 bug**。
- 真实证据：`_e2e_artifacts/resume_failed/`（测试 workflow yaml + 可复现 `run_e2e*.sh` + `e2e*.log`，含真实 reply/tape/daemon-log 捕获）。

## 已知限制 / 不在范围
- **irrecoverable resume 定位精度**：`fail_in_session` emit `workflow_failed(node=None)` → reducer 不覆盖 current_node → resume target 可能指向上游/stale（escalation + compliance 保证精确）。仍允许 resume（符合"任意 fail"），失败历史 + 立即再败即反馈。
- **headless `orca resume`**：其 `from_tape` 本就无 failed 守卫、偶然已能 resume failed；本 SPEC reducer 翻转使其可观测正确（pre: status 留 failed / post: running），不在测试范围。
- **resume 无视 `--output`**：resume = re-arm（重发 prompt），用户须下一次 next 喂 output（设计选择）。
- **tars serve HTTP 往返**未在隔离 /tmp 项目根测（扫描根不匹配）；AC10 在权威 web 函数层（`_scan_terminal_type` 等，即 tars serve 的 run_manager 计算路径）已全证。

## 改动文件
**源码**：`orca/schema/workflow.py`、`orca/events/replay.py`、`orca/run/step.py`、`orca/iface/in_session/cli.py`、`orca/iface/in_session/chart_daemon.py`、`orca/iface/in_session/_tape_probe.py`、`orca/iface/web/run_manager.py`。
**测试**：`tests/iface/in_session/{test_advance_step_resume_failed,test_reducer_workflow_resumed_flip,test_chart_daemon,test_tape_probe,test_in_session_cli,test_error_management,test_failure_sentinel}.py`、`tests/events/test_replay.py`、`tests/iface/web/{test_run_manager,test_scan_meta_overview_contract}.py`、`tests/iface/in_session/test_daemon.py`。
**清理**：`_RECOVERABLE_ESCALATE_AT` 常量及全部引用（code + docstring）删除。

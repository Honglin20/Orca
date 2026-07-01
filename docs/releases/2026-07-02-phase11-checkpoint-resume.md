# phase 11 P2.2 — Checkpoint Resume（崩溃续跑）

> **日期**：2026-07-02
> **SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §7（Checkpoint Resume）/ §2.2（`workflow_resumed` 事件）/ §7.3（失败模式）/ §10.3（review C6/C8）
> **计划**：[`docs/plans/2026-07-01-phase11-cli-enrichment.md`](../plans/2026-07-01-phase11-cli-enrichment.md) P2.2（Resume）
> **commit**：见 git log（本 release note 对应的 commit）

## 背景

Orca 的 Tape 是 append-only JSONL，天然就是 checkpoint —— 不需要 Conductor 那种 400+ 行的独立
状态序列化系统（`engine/checkpoint.py`）。本 feature 加一个 `orca resume` 命令：读 Tape 重放到
崩溃前状态，emit `workflow_resumed` 事件后从崩溃点续跑。工作量 1 个 commit（vs Conductor 4 周）。

## 改动点

### 新增

- **`orca/run/resume.py`** —— typed exceptions + 纯辅助函数：
  - `ResumeError` 基类 + 5 个子类：`TapeNotFoundError` / `EmptyTapeError` / `AlreadyCompletedError` /
    `ParallelGroupMidCrashError` / `MidFileCorruptError`（CLI 层映射到 exit code，fail loud）。
  - `_find_first_corrupt_line(path) -> (corrupt_info, valid_count)`：**中段**损坏严格检测（区别于
    `Tape.replay` 的 fail-soft）；末尾残行容忍（不报 corrupt，让 `Tape(resume=True)` 截断）。单遍
    扫描顺带返回合法事件数，避免 `from_tape` 多读一遍 tape。
  - `_outputs_acc_from_state(state)`：从 `replay_state` 的 `context` 重建 `_drive_loop` 的
    `outputs_acc` 形状（`{"output": raw}` 包装）。**review C8**：用 replay_state 派生，非手搓
    `_reconstruct_outputs`。
  - `_detect_parallel_mid_crash(state, wf)`：检测崩溃点是否在 parallel 组中间（branch running）。

- **`tests/run/test_resume.py`** —— 9 个测试覆盖 from_tape / run_from_state 全路径：
  - 状态重建（断言 replay_state 派生 aggregate，review C8）
  - completed / empty / mid-file-corrupt / parallel-mid-crash 各失败模式
  - `run_from_state` emit `workflow_resumed{resumed_node, replayed_events}` + 续跑至 `workflow_completed`
  - 末尾残行 fail-soft 截断
  - `_next_node_for_resume` fallback 分支（无 route_taken 的崩溃点）
  - Event-schema 校验失败的中段损坏变体

### 修改

- **`orca/run/orchestrator.py`**：
  - `Orchestrator.from_tape(tape_path, bus, wf)` classmethod：重放 + 校验 + 构造 resume orchestrator。
  - `Orchestrator.run_from_state()`：emit `workflow_resumed` 后调 `_drive_from` 续跑。
  - **重构**：`_drive_loop` 抽出 `_drive_from(start_node, initial_outputs)`，`run()` 与 `run_from_state()`
    共享同一段「node 边界 + dispatch + 路由」逻辑（DRY）。
  - `_DRIVE_REQUIRED_FIELDS` + `_assert_drive_fields_complete()`：`_drive_from` 入口校验实例含全部
    drive 所需字段（`_bare_instance` bypass `__init__` 的字段漂移安全网，review 🔴 建议）。
  - 辅助 staticmethod：`_next_node_for_resume` / `_bare_instance` / `_inputs_from_tape` /
    `_find_last_done_node_name`。

- **`orca/schema/event.py`**：EventType 加 `workflow_resumed`
  （data: `{from_tape, resumed_node, replayed_events}`，SPEC §2.2）。总数 25 → 26。

- **`orca/events/replay.py`**：把 `interrupt_requested` / `interrupt_resolved` / `prompt_rendered` /
  `workflow_resumed` 加入 reducer 的「已知 no-op」列表（之前这些 phase 11 事件落进「未知事件」
  warning 分支，是误报噪声；它们是可观测标记，不改顶层 RunState）。

- **`orca/iface/cli/commands.py`**：`resume` 子命令 + 辅助（`_resolve_tape_path` /
  `_resolve_workflow_yaml` / `_resume_workflow` / `_read_run_id`）。headless 跑（不启动 TUI）。
  - 参数解析：文件路径或 run_id（查 `runs/<run_id>.jsonl`）。
  - yaml 定位：`--yaml` 显式 > 从 tape 的 `workflow_name` 在 `examples/` 推断。
  - 失败模式 → exit code 映射（SPEC §7.3）：missing/empty/mid-corrupt → 2；trailing partial →
    fail-soft 截断 + 继续；completed → 0；parallel mid-crash / 续跑失败 → 1。

- **`orca/iface/cli/widgets/log_stream.py`**：`_describe` 加 `workflow_resumed`（↻ resumed from ...）。

- **`tests/schema/test_event.py`**：EventType 计数 25 → 26。
- **`tests/iface/cli/test_commands.py`**：`TestResumeCommand` 6 个测试（CLI 层参数解析 + exit code）。

## 设计决策

1. **Tape 是唯一 checkpoint**：不另起状态序列化系统（反 Conductor）。`replay_state` 已是纯 reducer
   fold，复用即可。
2. **resume 起点 = `state.current_node`**（reducer 据 `route_taken` 维护）；fallback 到「最后一个
   done node 的下一 node」（用 routes 求值，与 `_next_node_after` 同逻辑，但不 emit `route_taken` ——
   那个 route 在原 run 已 emit 过）。
3. **`_bare_instance` bypass `__init__`**：避免重新 gen run_id / 重跑 inputs default 填充（已完成 node
   不再需要）。Python `__new__` + 显式字段设置是标准 alternate-constructor 模式。配
   `_assert_drive_fields_complete()` 安全网防字段漂移。
4. **parallel 组中间崩溃不支持**（SPEC §7 risk）：歧义状态（部分 branch 已跑、输出一致性未知），
   phase 11 直接拒绝（exit 1），不静默续跑。
5. **yaml 需重新提供**：Tape 的 `topology` 字段是 DAG 摘要（非完整 yaml，保 payload 小），resume
   重建 Workflow 必须重新 load yaml。CLI 用 `--yaml` 或从 `workflow_name` 在 `examples/` 推断。

## review 驱动的修订（code-reviewer 反馈闭环）

| 评审条目 | 严重度 | 修订 |
|---|---|---|
| `_bare_instance` 绕过 `__init__` 字段漂移风险 | 🔴 | 加 `_DRIVE_REQUIRED_FIELDS` + `_assert_drive_fields_complete()` 安全网（`_drive_from` 入口校验） |
| `from_tape` 隐式依赖「tape 已 resume 截断」 | 🟡 | `_find_first_corrupt_line` 改为 position-aware：末尾残行不算 corrupt，from_tape 对「tape 是否已截断」不敏感 |
| `_next_node_for_resume` fallback 分支零测试覆盖 | 🟡 | 加 `test_from_tape_fallback_when_no_route_taken` |
| `event_count = sum(...)` 冗余 tape 全量读（3x 读） | 🟡 | `_find_first_corrupt_line` 单遍扫描顺带返回 valid_count，from_tape 复用 |
| `_inputs_from_tape` 返 `{}` 静默 | 🟡 | 加 `logger.warning` 让归因可见 |
| Event-schema 校验失败路径无独立测试 | 🟢 | 加 `test_from_tape_corrupt_line_with_valid_json_but_bad_event_schema` |

## 验证

- **单测**：`uv run pytest tests/ -m "not integration"` → **712 passed, 1 skipped**（基线 697 + 本
  feature 新增 15：9 resume + 6 CLI；含 review 反馈补的 2 个）。零回归。
- **依赖方向**：`resume.py` 只 import schema；`orchestrator.py` import resume；CLI import resume。
  单向，无环（code-reviewer 确认）。
- **SPEC §7.3 失败模式**：6 个场景全部覆盖且 exit code 正确（code-reviewer 逐条核对 ✓）。

## 人工 E2E（待跑，需真 claude）

```
# 1. 跑长 workflow
orca run examples/mxint_analysis.yaml
# 跑到 configurator 时另一终端：
kill -9 <orca_pid>

# 2. resume
orca resume runs/<run_id>.jsonl --yaml examples/mxint_analysis.yaml
# 预期：emit workflow_resumed{resumed_node=configurator/runner, replayed_events=N}，
# 从崩溃点续跑，最终 workflow_completed，exit 0。

# 3. 验证 tape
tail -20 runs/<run_id>.jsonl  # 含 workflow_resumed + 继续 events + workflow_completed
```

automatable 断言已由 `test_resume_emits_workflow_resumed_and_completes` 覆盖（用 FakeExecutor
模拟崩溃 + 续跑，断言 `workflow_resumed` 在 `workflow_completed` 之前 + `replayed_events` 计数对）。

## 偏离 SPEC 处

无逐字偏离。SPEC §7.2 的 `Orchestrator.from_tape(tape_path, bus)` 签名未含 `wf` 参数 —— 本实现加
了 `wf: Workflow`（Tape 的 topology 字段不足以重建 Workflow，必须重新 load yaml）。这是 SPEC 的
遗漏（§7.2 示例代码是伪码），本实现按「fail loud + 显式依赖」原则补全，CLI 层用 `--yaml` / 推断
透明处理。

## 阻塞 / 后续

- 无阻塞。wave-1 test-coverage-e2e sweep 可接入：`test_resume_emits_workflow_resumed_and_completes`
  已是可自动化的 tape-pairing 测试，符合 wave-1 验收。
- parallel group mid-crash resume 是已知简化（SPEC §7 risk）；未来若要支持需 branch-level 状态
  重建 + 部分输出一致性校验，复杂度高，推迟。

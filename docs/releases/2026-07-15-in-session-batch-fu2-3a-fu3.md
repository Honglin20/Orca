# Release: in-session 批量闭环 FU-2 + 3a + FU-3（status 活跃+结构化 / doctor 删 entry_hook dead / skill 补 error_kind）

**日期**: 2026-07-15
**Spec**: [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) §2.1（status 无参=活跃）/ §2.3（status 结构化 + 信封 error_kind）/ §4.4（idle hook 保留）
**Plan**: [`docs/plans/2026-07-15-in-session-batch-fu2-3a-fu3.md`](../plans/2026-07-15-in-session-batch-fu2-3a-fu3.md)（spec-reviewer CONDITIONAL-PASS，4 处精度修订闭环）
**Branch**: `in-session-unified-backend`
**Commit**: `<本 commit，single-commit；SHA 见 git log / CHANGELOG>`
**前置**: 5b `6d76a19`（3a 补 error_kind 依赖它）+ FU-1 `73a47ea`

## 做了什么

三个独立低复杂度 follow-up 合并单 commit（用户指示：低复杂度 step 合并做）。三者皆 in-session CLI/skill 层、无跨层依赖。

### FU-3：`orca status` 无参 → 活跃 + 结构化

**根因**（`cli.py` status 无参分支）：`glob("*.jsonl")` 列**所有** tape（含 completed）+ 返**裸 stem** `{runs:[stem]}`，违 docstring「活跃」+ SPEC §2.1/§2.3。

改动：
- 活跃枚举改 marker：`markers = sorted(runs_dir.glob("orca-*.json"))` → `read_marker` 派生 `run_id`（marker 存在 ≡ 活跃，SPEC §7.2 完成契约：bootstrap 写 / 终态清）。
- 每活跃 run 经 `replay_state(tape)` 取 `run_id`/`status`/`current_node`；**时间字段用 tape 末事件 `Event.timestamp`**（`last_next_at`），`elapsed = time.time() - last_ts`（语义=距上次活动）。
- `--json`：`{"runs":[{run_id,node,status,last_next_at,elapsed},...]}`；人类可读每行 `- <run_id> [status] node=<current_node> elapsed=<...>`。
- 无活跃 run：`{"runs":[]}`（`--json`，shape 与非空一致）/ `(无活跃 run)`（人类）exit 0。
- **completed（无 marker）不列**（修复核心）。

#### spec-reviewer 时间字段纠正（关键）

`replay_state()` 返 `RunState`（`schema/state.py:49-64`）**零时间字段**——不能从它取时间。时间精确源 = tape `Event.timestamp`（`schema/event.py:101`），经 `tape.replay()` 取末事件 ts。

**时间基一致性决策（Rule 7）**：`Event.timestamp` 由 `time.time()` 填充（`events/bus.py:145`、`_step_io.py:62`，wall-clock epoch 秒）。计划字面写 `elapsed = now_monotonic() - 末事件 ts`，但 `now_monotonic()` 返 `time.monotonic()`（不同时钟基）——两基相减得无意义差值。故 `elapsed` 用 `time.time()`（与 `Event.timestamp` 同基），**非** `now_monotonic()`。这是 spec-reviewer「时间精确源 = Event.timestamp」结论的必然推论。

#### marker mtime 兜底未采用

计划提到「marker mtime 仅兜底近似」，但 `Event.timestamp` 经 `tape.replay()` 末事件已是精确源（marker 每次 next RMW 回写，mtime ≈ 上次 next 但不如事件 ts 精确）。实现直接用事件 ts，不 fallback mtime（YAGNI：精确源已够，不引第二近似源）。

### FU-2：doctor 删 entry_hook dead check

**根因**（`cli.py` doctor ④ entry_hook check）：探 `PROBE_ENTRY_REL` 心跳证 transform plugin 入口活着。但 v5 step 4 整删 orca.ts transform → `PROBE_ENTRY_REL` 心跳**永不再写** → check 永久 unknown/无意义（dead check）。

改动：
- 删 ④ entry_hook check 块 + 连带死代码：
  - `PROBE_ENTRY_NAME` 常量（删 check 后无消费者）。
  - `entry = _read_probe(PROBE_ENTRY_NAME)` 死变量。
  - 报告心跳路径行（列永不会产生的文件，误导）。
- checks 5→4（skill_install / cli_imports_ok / diag_switch / advance_hook）。`ok` 计算（仅 `hard=True` 计数）不受影响（entry_hook 本就 `hard=False`）。
- **advance_hook 保留**（spec-reviewer #4 实读确认不 dead）：idle nudge hook 按 SPEC §4.4 保留（`orca.ts` 仍写 `PROBE_ADVANCE`），check 仍验证 session.idle 接线。

### 3a：SKILL.md 失败处理补 error_kind

5b 给 in-session 失败信封加 `error_kind` 字段（`InSessionError.error_kind` taxonomy）。SKILL.md 失败处理段只教读 `reason`，未提 `error_kind`。

改动（小，一句）：补「失败信封除 `reason` 还带 `error_kind`（如 `output_schema_mismatch`/`state_corrupt`/`unsupported_node_kind`/`subagent_compliance`），可据它给用户更精确失败归类（增强，`reason` 仍可用）」。

已装副本同步：`teams install` 走 `copytree(dirs_exist_ok=True)`（`install_cmds.py:215`）幂等覆盖；手动 `cp` 仓库源到 `~/.claude/skills/orca/SKILL.md` 已同步（`error_kind` 命中确认）。

## 测试

`tests/iface/in_session/test_in_session_v8.py`：

- **FU-3**：
  - `test_status_json_flag_no_run_id_lists_runs_json`（强化：断言结构化 dict 5 键 `run_id/node/status/last_next_at/elapsed` 精确键集；**时间字段钉死**——`last_next_at` 断言等于 tape 末事件 `Event.timestamp`（`pytest.approx`），防回归到 monotonic / marker mtime，code-reviewer Round 1 🟡#1）。
  - `test_status_no_run_id_excludes_completed`（新增：completed run 无 marker 不列）。
  - `test_status_no_run_id_empty_human_readable`（新增：无活跃 run → `(无活跃 run)` exit 0）。
  - `test_status_no_run_id_non_empty_human_readable`（新增：有活跃 run + 人类可读分支输出格式，code-reviewer Round 2 🟡）。
  - `test_status_no_run_id_skips_corrupt_and_orphan_markers`（新增：损坏 marker + 孤儿 marker 被跳过、真 run 仍列、不崩，code-reviewer Round 2 🟡）。
- **FU-2**：
  - `test_doctor_json_structure`（len 5→4、names 去 entry_hook、hard_expected 去 entry_hook）。
  - 删 3 个纯 entry_hook 测试（`test_doctor_diag_on_no_heartbeat_entry_unknown` / `test_doctor_fresh_entry_heartbeat_passes` / `test_doctor_stale_entry_heartbeat_unknown`）。
  - `test_doctor_diag_off_hook_checks_unknown_ok_unaffected`（去 entry 断言行）。
  - `test_doctor_report_describes_b_path`（去 `.orca-probe-entry.json` 路径断言，加 advance 路径 + entry-not-present 守门）。
- **3a**：无独立单测（纯 skill 文档增强，`error_kind` taxonomy 由代码 `_step_io.py`/`step.py` 强约束，不靠 SKILL.md 文本驱动——code-reviewer 认可省略）。

132 passed 0 回归（pre-existing env-blocked 缺 uv/真 claude/env，stash 区分）。

### code-reviewer 两轮闭环

- **Round 1（代码）**：0 🔴 / 0 🟡 实现缺陷。🟢 `time.time()` 提到循环外（多 run 共享同一快照基准，已采纳）；🟢 双遍 tape replay（冷路径，可读性优先，保留）。
- **Round 2（测试覆盖）**：0 🔴。🟡 时间基测试意图未钉死（已补 `last_next_at == tape 末事件 ts` 断言）；🟡 非 empty 人类可读分支 + 损坏/孤儿 marker skip 路径无测试（已补 2 测试）；🟢 精确键集断言（已采纳）。

## 验证

- 单测：`pytest tests/iface/in_session/` → 130 passed 0 回归。
- code-reviewer 两轮（代码 + 测试覆盖）。
- grep 守门：`PROBE_ENTRY_NAME` 仅余删除说明注释（无 live 代码引用）。
- test-agent 真机 E2E（纯 CLI，禁 MCP）由主 session 派，待跑。

## scope

- 不动：status `--run-id` 详情分支 / marker 机制 / replay / advance_hook / bootstrap-stop / MCP。
- 单 commit 含 3 项（+ 测试 + 计划 + release note + CHANGELOG + CURRENT）。

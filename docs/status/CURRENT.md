# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

## 🔥 当前任务（2026-07-15）：in-session spec v5 —— step 5b 完成，待进 step 3a/3b

> **新 session 必读**：本块 + [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) **v5** + [step 5b release note](../releases/2026-07-15-in-session-step5b-daemon-error-envelope.md) + [`docs/specs/in-session-unified-backend-draft.md`](../specs/in-session-unified-backend-draft.md)（合并推迟 spec）。

**2026-07-15 已完成**（7 commits，code-reviewer 多轮全闭环，0 回归）：
- **DEFECT-1**（`2de50e3`）/ **DEFECT-2**（`e763e9e`）/ **step 4**（`52cc9f3`）：见 [step 4 release note](../releases/2026-07-15-in-session-defects-and-step4.md)。
- **step 5a**（`bce29f8`）：删 setup phase 全栈 + MCP migration note（A2 gate 保留）。
- **FU-1**（`73a47ea`）：`stop`/`open` 加 `--run-id` option + 抽 `_merge_run_id` helper。
- **step 5b**（`<本 commit>`）：daemon batch emit + in-session 错误信封统一（×2，MCP 出 scope）；抽 `_step_io` helper；信封加 `error_kind`（tape `data.kind` 不变）。详见 [release note](../releases/2026-07-15-in-session-step5b-daemon-error-envelope.md)。**test-agent 真机 E2E 待跑**。

### 待办（spec v5 §8，step 3a/3b/6 + follow-up）

- **③a** 重型准入门（留 sprint）。
- **③b** catalog 物理迁（**已解锁**——前置 step 5a 完成）。
- **⑥** teams install nga/cac nudge 机制真机验证（留用户侧，无代码）。
- **FU-3** `status` 无参列表契约漂移（test-agent 观察，SPEC §2.3 写 `{runs:[{run_id,node,status,...}]}`，实跑 `{runs:[stem]}`），独立收。
- **FU-2** m13 parser pre-scan friendly-error（defer，`extra=forbid` 已 fail loud）。
- **推迟** 合并同一后端（`advance_step`↔`Orchestrator`），见 merge spec，等触发条件。

### step 5b follow-up / debt（非阻塞）

- **跨阶段 debt**：tape `workflow_failed.data.kind` 是 `ErrorKind`（phase-11 executor 层）/ `error_kind`（in-session 编排层）两值集共享字段。5b 只统一 in-session 两信封**来源**（都读 `exc.error_kind`），不合并值集、不加字段区分。跨阶段，登记不改。
- **无头 daemon 宽捕获**（code-reviewer Round 1 M1 引出）：`daemon.next` `except InSessionError` 窄捕获（与原行为一致，**非回归**，经 `git show HEAD` 核验）；无头 CI 场景契约外 bug 会 crash 留腐败 tape，可考虑 `except Exception → fail_in_session`。独立决策。
- CLI traceback UX 瑕疵 / `test_failure_render_error` 测试体并入 malformed 测试（均 pre-existing，非 5b 引入）。

---

## 跨阶段其他待立项（与 in-session 正交，不影响当前）

- **三壳统一 ADR**（[`2026-07-08-shell-unification-adr.md`](../specs/2026-07-08-shell-unification-adr.md)）：单一读路径 + 渲染契约 + 视觉，待 spec-review。
- **agent interrupt**（[`agent-interrupt-design-draft.md`](../specs/agent-interrupt-design-draft.md)）：mid-stream cancel+resume，待立项 SPEC。
- **render layer v1.5**（codex 接入，前置 phase-12-capabilities）/ **v2**（Web TS 镜像 + 流式 shiki + diff 虚拟化）。
- **TUI fold DRY**：fold 字段抽 `orca/run/projections.py`。
- **phase-16 批 2**：本地包分发（多 pool + `name@source`）+ workspace-instruction。

## 必读文件（下一任务开工前按需）
- [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5
- [`docs/releases/2026-07-15-in-session-step5b-daemon-error-envelope.md`](../releases/2026-07-15-in-session-step5b-daemon-error-envelope.md)（上一任务全貌）
- [`docs/specs/in-session-unified-backend-draft.md`](../specs/in-session-unified-backend-draft.md)（合并推迟 spec + 触发条件）
- [CHANGELOG](CHANGELOG.md)（历史完成项索引，各完成块详细在对应 release note）

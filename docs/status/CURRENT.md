# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

## 🔥 当前任务（2026-07-15）：in-session spec v5 —— 批量 FU-2+3a+FU-3 完成，待进 step 3a/6

> **新 session 必读**：本块 + [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) **v5** + [批量 FU release note](../releases/2026-07-15-in-session-batch-fu2-3a-fu3.md) + [`docs/specs/in-session-unified-backend-draft.md`](../specs/in-session-unified-backend-draft.md)（合并推迟 spec）。

**2026-07-15 已完成**（code-reviewer 多轮全闭环，0 回归）：
- step 3b / step 5a / FU-1 / step 5b / defects+step4：见 CHANGELOG 索引。
- **批量 FU-2+3a+FU-3**（`<本 commit>`）：status 无参→活跃+结构化（时间取 `Event.timestamp`）/ doctor 删 entry_hook dead / SKILL.md 补 `error_kind`。详见 [release note](../releases/2026-07-15-in-session-batch-fu2-3a-fu3.md)。**test-agent 真机（纯 CLI）待跑**。

### 待办（spec v5 §8，step 3a/6 + follow-up）

- **③a** 重型准入门（留 sprint）。
- **⑥** teams install nga/cac nudge 机制真机验证（留用户侧，无代码）。
- **FU-2** m13 parser pre-scan friendly-error（defer，`extra=forbid` 已 fail loud）。
- **推迟** 合并同一后端（`advance_step`↔`Orchestrator`），见 merge spec，等触发条件。

### follow-up / debt（预存，非阻塞）

- `_load_wf_for_run` 的 `catalog.find_workflow` fallback 无测试触达（step 3b code-reviewer Round 2，预存）。
- `tool_describe_workflow` found 分支无 server 层测试（预存）。
- tape `workflow_failed.data.kind` 是 `ErrorKind`/`error_kind` 两值集共享字段（跨阶段 debt，5b 登记）。
- doctor 注释「crash 孤儿 marker 由 doctor 另行检测」与实现不一致（doctor 无孤儿 marker 检测项，pre-existing，可选 backlog）。

---

## 跨阶段其他待立项（与 in-session 正交，不影响当前）

- **三壳统一 ADR**（[`2026-07-08-shell-unification-adr.md`](../specs/2026-07-08-shell-unification-adr.md)）：单一读路径 + 渲染契约 + 视觉，待 spec-review。
- **agent interrupt**（[`agent-interrupt-design-draft.md`](../specs/agent-interrupt-design-draft.md)）：mid-stream cancel+resume，待立项 SPEC。
- **render layer v1.5**（codex 接入）/ **v2**（Web TS 镜像 + 流式 shiki + diff 虚拟化）。
- **TUI fold DRY**：fold 字段抽 `orca/run/projections.py`。
- **phase-16 批 2**：本地包分发 + workspace-instruction。

## 必读文件（下一任务开工前按需）
- [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5
- [`docs/releases/2026-07-15-in-session-batch-fu2-3a-fu3.md`](../releases/2026-07-15-in-session-batch-fu2-3a-fu3.md)（上一任务全貌）
- [`docs/specs/in-session-unified-backend-draft.md`](../specs/in-session-unified-backend-draft.md)（合并推迟 spec + 触发条件）
- [CHANGELOG](CHANGELOG.md)（历史完成项索引，各完成块详细在对应 release note）

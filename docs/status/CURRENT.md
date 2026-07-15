# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

## 🔥 当前任务（2026-07-15）：in-session spec v5 —— step 5a 完成，待进 step 5b

> **新 session 必读**：本块 + [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) **v5** + [step 5a release note](../releases/2026-07-15-in-session-step5a-setup-removal.md) + [step 5b 计划](../plans/2026-07-15-in-session-step5b-daemon-error-envelope.md) + [`docs/specs/in-session-unified-backend-draft.md`](../specs/in-session-unified-backend-draft.md)（合并推迟 spec）。

**2026-07-15 已完成**（4 commits，code-reviewer 多轮全闭环，0 回归）：
- **DEFECT-1**（`2de50e3`）/ **DEFECT-2**（`e763e9e`）/ **step 4**（`52cc9f3`）：见 [step 4 release note](../releases/2026-07-15-in-session-defects-and-step4.md)。
- **step 5a**（`<本 commit>`）：删 setup phase 全栈 + MCP migration note（A2 gate 保留），详见 [release note](../releases/2026-07-15-in-session-step5a-setup-removal.md)。

### 待办（spec v5 §8，step 5b/3a/3b/6 + follow-up）

- **⑤b** daemon batch emit + 错误信封统一（独立 commit，C3）。计划已备 [`step5b`](../plans/2026-07-15-in-session-step5b-daemon-error-envelope.md)（草稿，待 spec-review）。
- **③a** 重型准入门（留 sprint）。
- **③b** catalog 物理迁（**已解锁**——前置 step 5a 完成）。
- **⑥** teams install nga/cac nudge 机制真机验证（留用户侧，无代码）。
- **FU-1** DEFECT-2 同型：`stop` / `open` 加 `--run-id` option（spec/SKILL.md 写 `--run-id`，CLI 用位置参数；每命令独立 commit）。
- **FU-2** m13 parser pre-scan friendly-error（defer，`extra=forbid` 已 fail loud，pre-scan 是 UX 优化）。
- **推迟** 合并同一后端（`advance_step`↔`Orchestrator`），见 merge spec，等触发条件。

### step 5a follow-up（非阻塞）

- CLI traceback UX 瑕疵（pre-existing，非 5a 引入），留后续 UX 打磨。
- opencode skill 真跑 / teams nga-cac 真机验证（留 step 6 / 用户侧）。

---

## 跨阶段其他待立项（与 in-session 正交，不影响当前）

- **三壳统一 ADR**（[`2026-07-08-shell-unification-adr.md`](../specs/2026-07-08-shell-unification-adr.md)）：单一读路径 + 渲染契约 + 视觉，待 spec-review。
- **agent interrupt**（[`agent-interrupt-design-draft.md`](../specs/agent-interrupt-design-draft.md)）：mid-stream cancel+resume，待立项 SPEC。
- **render layer v1.5**（codex 接入，前置 phase-12-capabilities）/ **v2**（Web TS 镜像 + 流式 shiki + diff 虚拟化）。
- **TUI fold DRY**：fold 字段抽 `orca/run/projections.py`（单一 reducer 消费）。
- **phase-16 批 2**：本地包分发（多 pool + `name@source`）+ workspace-instruction。

## 必读文件（下一任务开工前按需）
- [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5（本次范围 SPEC）
- [`docs/plans/2026-07-15-in-session-step5b-daemon-error-envelope.md`](../plans/2026-07-15-in-session-step5b-daemon-error-envelope.md)（下一任务计划，草稿）
- [`docs/releases/2026-07-15-in-session-step5a-setup-removal.md`](../releases/2026-07-15-in-session-step5a-setup-removal.md)（上一任务全貌）
- [`docs/specs/in-session-unified-backend-draft.md`](../specs/in-session-unified-backend-draft.md)（合并推迟 spec + 触发条件）
- [CHANGELOG](CHANGELOG.md)（历史完成项索引，各完成块详细在对应 release note）

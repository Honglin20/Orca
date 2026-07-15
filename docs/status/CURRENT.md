# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

## 🔥 当前任务（2026-07-15）：in-session spec v5 —— step 3b 完成，待进 step 3a/6 + follow-up

> **新 session 必读**：本块 + [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) **v5** + [step 3b release note](../releases/2026-07-15-in-session-step3b-catalog-relocate.md) + [`docs/specs/in-session-unified-backend-draft.md`](../specs/in-session-unified-backend-draft.md)（合并推迟 spec）。

**2026-07-15 已完成**（8 commits，code-reviewer 多轮全闭环，0 回归）：
- **DEFECT-1**（`2de50e3`）/ **DEFECT-2**（`e763e9e`）/ **step 4**（`52cc9f3`）：见 [step 4 release note](../releases/2026-07-15-in-session-defects-and-step4.md)。
- **step 5a**（`bce29f8`）：删 setup phase 全栈 + MCP migration note（A2 gate 保留）。
- **FU-1**（`73a47ea`）：`stop`/`open` 加 `--run-id` option + 抽 `_merge_run_id` helper。
- **step 5b**：daemon batch emit + in-session 错误信封统一（×2，MCP 出 scope）。详见 [release note](../releases/2026-07-15-in-session-step5b-daemon-error-envelope.md)。
- **step 3b**（`<本 commit>`）：catalog 物理迁 `orca/compile/catalog.py`（依赖铁律归位）；7 lazy import → 顶层 module import；9 mock target 同步。详见 [release note](../releases/2026-07-15-in-session-step3b-catalog-relocate.md)。**test-agent 真机三路 list 一致待跑**。

### 待办（spec v5 §8，step 3a/6 + follow-up）

- **③a** 重型准入门（留 sprint）。
- **⑥** teams install nga/cac nudge 机制真机验证（留用户侧，无代码）。
- **FU-3** `status` 无参列表契约漂移（test-agent 观察，SPEC §2.3 写 `{runs:[{run_id,node,status,...}]}`，实跑 `{runs:[stem]}`），独立收。
- **FU-2** m13 parser pre-scan friendly-error（defer，`extra=forbid` 已 fail loud）。
- **推迟** 合并同一后端（`advance_step`↔`Orchestrator`），见 merge spec，等触发条件。

### step 3b follow-up / debt（预存，非阻塞）

- `_load_wf_for_run` 的 `catalog.find_workflow` fallback（`in_session/cli.py`，老 tape/daemon 无 yaml_path 的错误恢复分支）无测试触达（code-reviewer Round 2 指出，预存）。
- `tool_describe_workflow` found 分支（server 装配层 `catalog.describe_workflow`）无 server 层测试，函数本身在 test_catalog.py 有单测（预存）。

### step 5b follow-up / debt（非阻塞）

- **跨阶段 debt**：tape `workflow_failed.data.kind` 是 `ErrorKind`（phase-11 executor 层）/ `error_kind`（in-session 编排层）两值集共享字段。5b 只统一来源（都读 `exc.error_kind`），不合并值集。跨阶段，登记不改。
- **无头 daemon 宽捕获**：`daemon.next` `except InSessionError` 窄捕获（与原行为一致，非回归）；无头 CI 契约外 bug 会 crash 留腐败 tape，可考虑 `except Exception → fail_in_session`。独立决策。
- CLI traceback UX 瑕疵 / `test_failure_render_error` 测试体并入 malformed 测试（均 pre-existing）。

---

## 跨阶段其他待立项（与 in-session 正交，不影响当前）

- **三壳统一 ADR**（[`2026-07-08-shell-unification-adr.md`](../specs/2026-07-08-shell-unification-adr.md)）：单一读路径 + 渲染契约 + 视觉，待 spec-review。
- **agent interrupt**（[`agent-interrupt-design-draft.md`](../specs/agent-interrupt-design-draft.md)）：mid-stream cancel+resume，待立项 SPEC。
- **render layer v1.5**（codex 接入，前置 phase-12-capabilities）/ **v2**（Web TS 镜像 + 流式 shiki + diff 虚拟化）。
- **TUI fold DRY**：fold 字段抽 `orca/run/projections.py`。
- **phase-16 批 2**：本地包分发（多 pool + `name@source`）+ workspace-instruction。

## 必读文件（下一任务开工前按需）
- [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5
- [`docs/releases/2026-07-15-in-session-step3b-catalog-relocate.md`](../releases/2026-07-15-in-session-step3b-catalog-relocate.md)（上一任务全貌）
- [`docs/specs/in-session-unified-backend-draft.md`](../specs/in-session-unified-backend-draft.md)（合并推迟 spec + 触发条件）
- [CHANGELOG](CHANGELOG.md)（历史完成项索引，各完成块详细在对应 release note）

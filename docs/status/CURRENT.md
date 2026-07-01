# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 11 第一波全部落地 + wave-1 e2e 审计闭环 —— 全绿（0 xfail）。**

第一波 feature：CI(P0.1) ✓ + Interrupt/Guidance(P1.1) ✓ + Resume(P2.2) ✓。wave-1 e2e
覆盖审计发现 1 个 critical bug（`interrupt_resolved` 同步写 Tape 修复，已闭环）+ 补齐
e2e 契约测试（中断三分支配对不变量 / SKIP / ABORT / 多壳 await-future / prompt_rendered
不变量 / emit-on-closed-bus fail-loud）。

- **最新 release note**：[`2026-07-02-phase11-interrupt-resolved-fix.md`](../releases/2026-07-02-phase11-interrupt-resolved-fix.md)
- **SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md)

## 待办

1. **人工 E2E**：`orca run examples/mxint_analysis.yaml` → `kill -9` → `orca resume runs/<id>.jsonl
   --yaml examples/mxint_analysis.yaml` → 验证 `workflow_resumed` + 续跑至 `workflow_completed`
   （需真 claude，automatable 断言已由 `test_resume_emits_workflow_resumed_and_completes` 覆盖）。
2. **后续 wave**：Retry(P0.2) → ask_user(P1.2) → Validator/Dialog/Wait → daemon/Skip。

## 必读文件

1. [`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §7 / §10.3
2. [`docs/releases/2026-07-02-phase11-checkpoint-resume.md`](../releases/2026-07-02-phase11-checkpoint-resume.md)
3. [`orca/run/resume.py`](../../orca/run/resume.py)（typed exceptions + 辅助）+ [`orca/run/orchestrator.py`](../../orca/run/orchestrator.py) `from_tape` / `run_from_state` / `_drive_from`

## 裁定的决策（不再讨论）

1. 保持 `claude -p` CLI 子进程路线（SPEC §1.1）；D1 wave 顺序；D2 descope attach；D3 Budget 不做；D4 ask_user 确定性 tool-params 路由。
2. CLI 单壳中断不经 await-future（SPEC §11.1，review §2.1 修复）；多壳路径保留给 P3。
3. Tape 是唯一 checkpoint：不另起状态序列化系统（反 Conductor）；`replay_state` 复用即 checkpoint。
4. parallel 组中间崩溃不支持 resume（SPEC §7 risk，歧义状态，exit 1）。

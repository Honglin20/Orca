# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 11 第二波 P0.3 Retry Policy 落地 —— 全绿（0 回归）。**

wave 2 第一项：Retry Policy（节点级自动重试 transient claude 失败）。`execute_with_retry`
核心 loop + `_classify_for_retry` error_type 对齐层（CliExitNonZero→spawn_error 等，
SPEC §9.5.2）+ RetryPolicy schema（ge 校验）+ ExecError.from_failed_data DRY + orchestrator
集成 + reducer/LogStream 描述。27 新测试覆盖意图（含 5 条对齐层契约）。

- **最新 release note**：[`2026-07-02-phase11-retry-policy.md`](../releases/2026-07-02-phase11-retry-policy.md)
- **SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §9.5

## 待办

1. **wave 2 续**：ask_user(P1.2) —— SSE spike 前置（SPEC §5.3），失败则 feature 推迟。
2. **人工 E2E（待真 claude）**：`orca run examples/with_retry.yaml`（真实 flaky 才触发 retry）。
3. **后续 wave**：Validator(P2.1，复用 execute_with_retry) → Dialog(P2.2) → Wait(P3.1) → daemon(P3.2) → Skip(P4)。

## 必读文件

1. [`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §7 / §10.3
2. [`docs/releases/2026-07-02-phase11-checkpoint-resume.md`](../releases/2026-07-02-phase11-checkpoint-resume.md)
3. [`orca/run/resume.py`](../../orca/run/resume.py)（typed exceptions + 辅助）+ [`orca/run/orchestrator.py`](../../orca/run/orchestrator.py) `from_tape` / `run_from_state` / `_drive_from`

## 裁定的决策（不再讨论）

1. 保持 `claude -p` CLI 子进程路线（SPEC §1.1）；D1 wave 顺序；D2 descope attach；D3 Budget 不做；D4 ask_user 确定性 tool-params 路由。
2. CLI 单壳中断不经 await-future（SPEC §11.1，review §2.1 修复）；多壳路径保留给 P3。
3. Tape 是唯一 checkpoint：不另起状态序列化系统（反 Conductor）；`replay_state` 复用即 checkpoint。
4. parallel 组中间崩溃不支持 resume（SPEC §7 risk，歧义状态，exit 1）。

# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 11 第一波 P1.1 Interrupt UI + Guidance 注入 —— 已完成（Step A `9db57f4` + Step B `2c622b7`）。**

feature 可合并：Ctrl+G → InterruptModal（CONTINUE/SKIP/ABORT + guidance）→ orchestrator node 边界
消费 → guidance 累积进 ctx → 重 spawn prompt 含 `[User Guidance]`。review §2.1 critical 死锁已修。

- **SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §3（中断 UI）/ §4（guidance）/ §11.1（CLI 单壳路径偏离）
- **Step A**：commit `9db57f4`，release note [`2026-07-01-phase11-interrupt-ui.md`](../releases/2026-07-01-phase11-interrupt-ui.md)
- **Step B**：commit `2c622b7`，release note [`2026-07-02-phase11-guidance-injection.md`](../releases/2026-07-02-phase11-guidance-injection.md)

## 待办

1. **人工 E2E**：`orca run examples/mxint_analysis.yaml`，configurator 跑时 Ctrl+G + guidance →
   验证重 spawn prompt 含 `[User Guidance]` + tape 配对（交互测试，自动化已由 E2E 用例覆盖）。
2. **第一波剩余**：Resume（SPEC §7，fail-soft 已在 tape.py）—— 单独 dispatch clean-code-builder。
3. **后续 wave**：Retry(P0.2) → ask_user(P1.2) → Validator/Dialog/Wait → daemon/Skip。

## 必读文件

1. [`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §3 / §4 / §11.1 / §10.3
2. [`docs/releases/2026-07-02-phase11-guidance-injection.md`](../releases/2026-07-02-phase11-guidance-injection.md)（Step B + §2.1 修复）
3. [`orca/gates/interrupt.py`](../../orca/gates/interrupt.py)（record_resolved CLI 路径）+ [`orca/run/orchestrator.py`](../../orca/run/orchestrator.py) `_handle_interrupt`

## 裁定的决策（不再讨论）

1. 保持 `claude -p` CLI 子进程路线（SPEC §1.1）；D1 wave 顺序；D2 descope attach；D3 Budget 不做；D4 ask_user 确定性 tool-params 路由。
2. CLI 单壳中断不经 await-future（SPEC §11.1，review §2.1 修复）；多壳路径保留给 P3。

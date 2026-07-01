# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 11 CLI feature 补全 —— SPEC/PLAN 已过对抗评审并修订，进入实现（wave 制）**。

`spec-review-adversarial` 裁决 fail→conditional-pass（22 条真问题已闭环，见 SPEC §10.3）。SPEC/PLAN 修订完成，4 个决策已裁定。

- **SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md)（**最新**，§10.3 含 review 修订汇总）
- **计划**：[`docs/plans/2026-07-01-phase11-cli-enrichment.md`](../plans/2026-07-01-phase11-cli-enrichment.md)（顶部含 9 处 API 订正 + wave 顺序）

## 裁定的决策（不再讨论）

1. **保持 `claude -p` CLI 子进程路线，不切 SDK**（SPEC §1.1）
2. **D1 wave 顺序**（仍全做 11 feature，只是排序降险）
3. **D2 只读 `attach` descoped**（价值低于 `tail -f`）；daemon 只做 `--background`/`ps`/`logs`/`wait`
4. **D3 Budget 不做**（SPEC §12 是契约，Budget OUT）
5. **D4 ask_user 确定性 tool-params 路由**（`_orca_run_id`/`_orca_node`，不依赖 MCP session）

## Wave 路线图（D1）

| 波 | feature | 前置 | 状态 |
|---|---|---|---|
| 1 | CI(P0.1) + Interrupt UI/Guidance(P1.1) + Resume(P2.3) | SPEC §2.3 request_interrupt | CI✅ 进行中 |
| 2 | Retry(P0.2) + ask_user(P1.2) | error_type 对齐(§9.5.2) + SSE spike(§5.3) + register 债(§5.5) | 待 |
| 3 | Validator(P2.1) + Dialog(P2.2) + Wait(P3.1) | profile 注入(§9.6.4) + wait handle(§9.7.6) | 待 |
| 4 | daemon(P3.2) + Skip(P4) | — | 待 |

每 wave：clean-code-builder 实现 → test-coverage-e2e（含 e2e）→ code-reviewer → COMMIT + release note。

## 下一步

第一波开工，CI 已完成（见上表 ✅），剩余按顺序 dispatch clean-code-builder：
1. **Interrupt UI + Guidance**（SPEC §3/§4）→ 先实现 §2.3 `Orchestrator.request_interrupt`
2. **Resume**（SPEC §7，fail-soft 已在 tape.py）

## 必读文件

1. [`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §10.3（review 修订汇总）+ §2/§3/§4/§5/§9.5/§9.6/§9.7
2. [`docs/plans/2026-07-01-phase11-cli-enrichment.md`](../plans/2026-07-01-phase11-cli-enrichment.md)（顶部订正 + wave 顺序）
3. [`docs/specs/phase-6-gates.md`](../specs/phase-6-gates.md)（gates 契约；phase 11 还 register 债 + SessionLoc 重命名）
4. [`docs/specs/phase-7-cli.md`](../specs/phase-7-cli.md)（CLI 壳，phase 11 扩展点）

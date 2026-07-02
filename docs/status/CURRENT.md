# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前状态：phase-12 TUI 重设计 S0–S9 完成，S10 e2e 待跑

- **SPEC（v2，对抗审闭环）**：[`docs/specs/phase-12-cli-tui-redesign.md`](../specs/phase-12-cli-tui-redesign.md)
- **计划**：[`docs/plans/2026-07-03-phase12-tui-redesign.md`](../plans/2026-07-03-phase12-tui-redesign.md)（S0–S10）
- **S0–S9 已完成**（clean-code-builder）：6 新 widget/screen + app.py 接线 + 单测 + 自审 + 提交。1131 passed 0 回归（基线 1082→1131，净增 49 测试）。详见 [release note](../releases/2026-07-03-phase12-tui-redesign.md)。
- **S10 待跑**（test-coverage-e2e，**opencode 后端**）：需 opencode profile + chart 生产者就绪；探测不到则等待。

## 待办

1. **phase-12 S10 e2e（opencode 后端，test-coverage-e2e）**（当前）—— 探测 opencode + render_chart 就绪后跑真 agent workflow，验收 SPEC §6 全位置。
2. **`orca executor` 真实端到端 manual 验证（待 ccr/claude + key 环境）**。
3. **前序 4-bug-fix 的 TUI 端到端验证**（仍待 manual）。

## 必读文件（下一任务开工前按需）

- [`docs/specs/phase-12-cli-tui-redesign.md`](../specs/phase-12-cli-tui-redesign.md) §6（验收标准）+ [`docs/releases/2026-07-03-phase12-tui-redesign.md`](../releases/2026-07-03-phase12-tui-redesign.md)（S0–S9 做了啥 + 偏差）
- [`docs/specs/phase-9d-web-gate-chart.md`](../specs/phase-9d-web-gate-chart.md) §2 + `orca/iface/web/frontend/src/components/chart/types.ts`（图表契约 source of truth）

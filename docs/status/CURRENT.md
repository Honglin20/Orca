# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**无活跃任务** —— 阶段 6（gates/ HMIL 层）已完成。

- **状态**：✅ 已完成（478 测试全绿：442 基线 + 36 净增，零回归；5 条铁律全过；hook 桥 4 路径安全语义有测试）
- **release note**：[`docs/releases/2026-06-30-phase6-gates.md`](../releases/2026-06-30-phase6-gates.md)
- **CHANGELOG**：[`docs/status/CHANGELOG.md`](CHANGELOG.md)

## 下一步（待启动新 session）

阶段 7：CLI 壳（Textual TUI + 同步 input() gate UX）。
参考 [`docs/specs/phase-7-cli.md`](../specs/phase-7-cli.md) +
[`docs/specs/shells-design-draft.md`](../specs/shells-design-draft.md)（三壳共同契约 §3 CLI 决策）。

phase 6 提供给 phase 7 的契约：
- `HumanGateHandler`（`from orca.gates import HumanGateHandler`）：CLI 壳订阅
  `human_decision_requested` 事件渲染 ModalScreen，用户答后调
  `handler.resolve(gate_id, answer, "cli")`。
- gate 事件流经 EventBus（`subscribe()` → 渲染；`human_decision_resolved` → 自动 dismiss ModalScreen）。
- `HumanGate`（`from orca.gates import HumanGate`）：source 字段驱动渲染分支
  （tool_permission=权限弹窗 / agent_ask=问答弹窗）。

## phase 6 遗留（非阻断，后续可优化）

- gate 持久化恢复（崩溃后未 resolved 的 gate 怎么办）—— SPEC §9 明确留后，phase 6 gate 写
  tape 但崩溃恢复语义未实现。
- 三壳并发真跑竞速的端到端测试归 phase 7+9 集成（phase 6 单壳能 resolve 即可）。

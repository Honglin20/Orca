# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前状态：4 bug 修复完成，无进行中任务

**agent 可观测性 + TUI 闪退 + 子进程泄漏修复**全部完成（4 bug + 7 新测试 + 6 FakeRunner 同步，985 passed 0 回归，code-reviewer 0 🔴 0 🟡）。

- **release note**：[`docs/releases/2026-07-02-agent-observability-tui-fixes.md`](../releases/2026-07-02-agent-observability-tui-fixes.md)
- **CHANGELOG**：顶部 4-bug-fix 索引
- 四个 bug：① OnResult 加 `api_error_status` 第 5 参（全仓 11 处），`_result_diag()` 让 529 落到 `node_failed`；② translator ApiRetry 对齐真实字段 `attempt`/`retry_delay_ms`/`error_status`；③ TUI 终态停留 + notify「按 q 退出」（不闪退）；④ `stream()` finally terminate proc（防孤儿 claude）。

## 待办（等用户指示方向）

1. **Bug3 端到端 TUI 验证（manual，待智谱恢复或换可用模型）**：`uv run orca run examples/demo_mixed.yaml`，failed 时 TUI 应停留显示「按 q 退出」而非闪退；中途 `q` 后 `pgrep -af claude` 应无孤儿。
2. **可选 polish（非阻塞）**：读写 attach（descoped D2，需 UDS）；`_stop_agent_tools` 异常收窄。
3. **真 claude E2E（manual）**：mxint_analysis 全流程实跑；CI `/integration` PR comment 或本地 `pytest -m integration`。
4. **下一阶段（未规划）**：Web phase（前端 InterruptModal/DialogModal/cancel 端点 + terminate 节点 widget）；phase 12+ polish。

## 必读文件（下一任务开工前按需）

- [`docs/releases/2026-07-02-agent-observability-tui-fixes.md`](../releases/2026-07-02-agent-observability-tui-fixes.md)（本次 4 bug 全貌）
- [`docs/releases/2026-07-02-terminate-step.md`](../releases/2026-07-02-terminate-step.md)（terminate step，前一里程碑）

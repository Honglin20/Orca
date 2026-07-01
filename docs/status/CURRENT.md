# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 11 wave-3 e2e 审计 bugfix 完成 —— Ctrl+G 立即打断 sleeping wait node（879→888，0 回归）。**

wave-3 e2e 审计（`test-coverage-e2e` agent）发现 SPEC §9.7.6 + §10.2 item9 承诺的「Ctrl+G →
wait node 立即结束」实际不工作：`notify_all_waits` 原本只在 node 边界 `_handle_interrupt` 触发，
wait sleep 期间 `_drive_loop` 阻塞在 `_dispatch` 到不了边界 → 对 sleeping wait 是死代码。
surgical fix：`Orchestrator.request_interrupt` 登记 pending 的同时即时调 `bus.notify_all_waits()`
（保留 record_resolved/resolve 里的同一调用作 defense-in-depth，覆盖多壳 resolve 路径）。
xfail 复现测试翻转 pass + 8 新 wave-3 e2e 测试采纳。dispatch code-reviewer：无 🔴/🟡。

- **最新 release note**：[`2026-07-02-phase11-wait-interrupt-fix.md`](../releases/2026-07-02-phase11-wait-interrupt-fix.md)
- **SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §9.7.6 / §10.2 item9

## 待办

1. **wave 3 已完成**（Validator + Wait + Dialog 全绿）。
2. **人工 E2E（待真 TTY + ANTHROPIC_API_KEY）**：`orca run examples/with_dialog.yaml`（真 claude
   多轮对话；自动化证明已在 `tests/gates/test_dialog.py::test_send_turn_accumulates_history`）。
3. **后续 wave**：daemon(P3.2) → Skip(P4)。

## 必读文件

1. [`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §6 / §11.7
2. [`docs/releases/2026-07-02-phase11-dialog.md`](../releases/2026-07-02-phase11-dialog.md)
3. [`orca/gates/dialog.py`](../../orca/gates/dialog.py)（DialogHandler 3-method split + Rule 7 裁定）
   + [`orca/iface/cli/screens/dialog_modal.py`](../../orca/iface/cli/screens/dialog_modal.py)（Textual modal）

## 裁定的决策（不再讨论）

1. 保持 `claude -p` CLI 子进程路线（SPEC §1.1）；D1 wave 顺序；D2 descope attach；D3 Budget 不做；D4 ask_user 确定性 tool-params 路由。
2. CLI 单壳中断不经 await-future（SPEC §11.1）；多壳路径保留给 P3。
3. Tape 是唯一 checkpoint：不另起状态序列化系统（反 Conductor）；`replay_state` 复用即 checkpoint。
4. parallel 组中间崩溃不支持 resume（SPEC §7 risk，歧义状态，exit 1）。
5. ask_user 路由参名 `orca_run_id`/`orca_node`（非 `_orca_*`，FastMCP 拒下划线前缀，SPEC §11.2）；
   register 时机前移到 spawn 前 + 按 run 批清（SPEC §11.4）。
6. **WaitExecutor 依赖 `WaitHandleRegistry` Protocol 而非直接持 `EventBus`**（SPEC §11.5，铁律 2 张力化解）。
7. **`validate_output` 不持 bus、不 emit**（SPEC §11.6，铁律 2 张力化解，Rule 7 选 B）；三类
   validator_* 事件由 orchestrator loop 统一 emit；validator 与 retry 独立预算（不共享 max_attempts）。
8. **Dialog 3-method split**（SPEC §11.7，PLAN correction #7）：start/send/end 三方法，非单一 run_dialog
   （Textual modal 轮间交还 UI 控制）。`ctx.dialog_history` 是 web shell replay 预留位（真相在 tape）。
   `_build_env_overlay` 抽到 `orca/exec/env.py`（Rule 6 DRY）。DialogHandler 持 bus emit（gates 层，
   与 InterruptHandler 同 pattern，不违反铁律 2）。

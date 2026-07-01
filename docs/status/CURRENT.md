# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 11 第一波 —— Interrupt UI + Guidance 注入（P1.1）实现中（wave 制，Step A 已完成）**。

Step A（中断 UI + orchestrator wiring）已 commit；Step B（guidance 注入 + SIGINT）进行中。

- **SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §3（中断 UI）/ §4（guidance）/ §2.3（request_interrupt）/ §10.3（review 修订）
- **计划**：[`docs/plans/2026-07-01-phase11-cli-enrichment.md`](../plans/2026-07-01-phase11-cli-enrichment.md) P0.2 + P1.1（顶部 9 处 API 订正）

## Step A 已完成（commit `9db57f4`）

- `orca/gates/_broadcaster_mixin.py`（DRY 抽 mixin）+ `interrupt.py`（InterruptHandler）。
- `orca/gates/types.py`（InterruptRequest）+ `schema/event.py`（+3 事件类型）。
- `orca/run/orchestrator.py`（request_interrupt + _handle_interrupt + WorkflowAborted）。
- `orca/iface/cli/screens/interrupt_modal.py` + `app.py`（Ctrl+G）+ `log_stream.py`（format_event）。
- 测试：tests/gates/test_interrupt.py（9）+ tests/iface/cli/test_interrupt_modal.py（7）+ test_app.py（+6）。
- 全量 674 passed / 1 skipped（基线 652，+22 新测试，0 回归）。

## 下一步（Step B）

1. `RunContext.user_guidance` + `with_guidance` + `guidance_prompt_section`（SPEC §4.1）。
2. `render_prompt` 拼 `[User Guidance]` 段。
3. `CLIRunner.send_sigint` + `was_interrupted`；ClaudeExecutor SIGINT-as-interrupt + emit `prompt_rendered`。
4. orchestrator `_make_ctx` 注入 guidance；continue 分支接 `ctx.with_guidance`。
5. E2E：fake executor + fake interrupt_handler，断言 tape 配对 + `prompt_rendered` 含 `[User Guidance]`。
6. code-reviewer 全 diff review → fix ALL → commit Step B → 更新 CHANGELOG/CURRENT。

## 必读文件

1. [`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §3 / §4 / §10.3
2. [`docs/releases/2026-07-01-phase11-interrupt-ui.md`](../releases/2026-07-01-phase11-interrupt-ui.md)（Step A release note）
3. [`orca/gates/interrupt.py`](../../orca/gates/interrupt.py) + [`orca/run/orchestrator.py`](../../orca/run/orchestrator.py) `_handle_interrupt`


## 裁定的决策（不再讨论）

1. **保持 `claude -p` CLI 子进程路线，不切 SDK**（SPEC §1.1）
2. **D1 wave 顺序**（仍全做 11 feature，只是排序降险）
3. **D2 只读 `attach` descoped**；D3 Budget 不做；D4 ask_user 确定性 tool-params 路由。


# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 11 P4 Skip to Agent 完成 —— 显式 skip 目标 + NodeSelectModal + §9.2 route 容错（888→904，0 回归）。**

wave-1 SKIP 只能沿 route 跳，无兜底 route 时 NoRouteMatch 崩溃（SPEC §10.2 item12）。本 wave 补齐：
`request_interrupt` 加 `skip_target` 参数 → `_drive_loop` 直接跳该 node；`NodeSelectModal`
（pattern A：InterruptModal → app 推选择器）；router §9.2 容错（skipped node None output 走兜底）；
`_validate_skip_target` fail loud（ValueError）；`interrupt_resolved.data.skip_target` 写 tape。
code-reviewer 1 🔴（验证顺序致脏 tape，已修：校验前置到 record_resolved 之前）+ 3 🟡 全修。

- **最新 release note**：[`2026-07-02-phase11-skip-to-agent.md`](../releases/2026-07-02-phase11-skip-to-agent.md)
- **SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §9 / §10.2 item12 / §11.8

## 待办

1. **phase 11 P0-P4 全部完成**（CI / Retry / Interrupt / ask_user / Validator / Dialog / Resume / Wait / Skip）。
2. **唯一剩余**：P3.2 daemon `--background`（task #11，pending）—— 非本 phase 核心闭环，可滚动。
3. **人工 E2E（待真 TTY + ANTHROPIC_API_KEY）**：`orca run examples/demo_skip.yaml`
   （Ctrl+G + SKIP + 选目标 node，自动化证明已在 `tests/run/test_skip_to_agent.py`）。

## 必读文件

1. [`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §9 / §11.8
2. [`docs/releases/2026-07-02-phase11-skip-to-agent.md`](../releases/2026-07-02-phase11-skip-to-agent.md)
3. [`orca/run/orchestrator.py`](../../orca/run/orchestrator.py)（`_handle_interrupt` 返回 tuple +
   `_validate_skip_target`）+ [`orca/iface/cli/screens/node_select_modal.py`](../../orca/iface/cli/screens/node_select_modal.py)

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

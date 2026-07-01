# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 11 全部完成（P0-P4，CI / Retry / Interrupt / ask_user / Validator / Dialog / Resume / Wait / Daemon / Skip）。**
本仓库在 CLI 场景下达到 Conductor 等量功能水平。最后一块 P3.2 daemon `--background` 已合并：
`orca run --background` fork detached child（headless，非 TUI）+ `ps`/`logs`/`wait` 三件套。

- **最新 release note**：[`2026-07-02-phase11-daemon.md`](../releases/2026-07-02-phase11-daemon.md)
- **SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §8 / §11.9（headless 裁定）

## 待办

1. **phase 11 收官**：无剩余子任务。可选 polish（非阻塞）：
   - 读写 attach（descoped D2，需 UDS 控制通道，留后续 phase）
   - 人工 E2E（待真 TTY + ANTHROPIC_API_KEY）：mxint_analysis 全流程实跑
2. **下一步方向**（未规划，等用户指示）：
   - Web phase（前端 InterruptModal/DialogModal/cancel 端点）
   - phase 12+ polish（Self-Update / Workflow Registry / Budget 等推迟项）

## 必读文件

1. [`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §8 / §11.9
2. [`docs/releases/2026-07-02-phase11-daemon.md`](../releases/2026-07-02-phase11-daemon.md)
3. [`orca/iface/cli/bg_runner.py`](../../orca/iface/cli/bg_runner.py)（daemonize seam + BgRunMeta）

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
9. **daemon detached child 走 headless 而非 TUI**（SPEC §11.9，2026-07-02）：detached 无 TTY，
   Textual 会崩；child 检测 `ORCA_BG_RUN_ID` env 走 `_run_workflow_headless`（直接 Orchestrator.run）。
   Tape/metadata 一致性不变（resume 可接）。

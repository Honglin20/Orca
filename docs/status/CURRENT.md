# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 11 第三波 P3.1 Wait Node 实现完成 —— 全绿（0 回归）。**

wave 3 第三项：Wait Node。新 `kind: wait` 节点（`asyncio.sleep`，`interruptible=True` 时可被
Ctrl+G 打断）。`WaitHandleRegistry` Protocol 化解铁律 2 张力（exec 不持 bus，依赖能力 Protocol）。
EventBus wait-handle API（register/unregister/notify_all_waits，`threading.Lock` 保护）。
InterruptHandler.resolve/record_resolved 双路径调 `notify_all_waits`。38 新测试断言 INTENT
（含 Ctrl+G 打断 wait 自动化证明 + parallel group 内两 wait 独立打断）。

- **最新 release note**：[`2026-07-02-phase11-wait-node.md`](../releases/2026-07-02-phase11-wait-node.md)
- **SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §9.7 / §11.5

## 待办

1. **wave 3 余项**：Validator(P2.1，复用 execute_with_retry loop) → Dialog(P2.2)。
2. **人工 E2E（待真 TTY）**：`orca run examples/with_wait.yaml`（wait→script，Ctrl+G 打断 wait 的手动验证；自动化证明已在 `tests/exec/test_wait.py::test_wait_executor_interruptible_can_be_cancelled`）。
3. **后续 wave**：daemon(P3.2) → Skip(P4)。

## 必读文件

1. [`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §9.7 / §11.5
2. [`docs/releases/2026-07-02-phase11-wait-node.md`](../releases/2026-07-02-phase11-wait-node.md)
3. [`orca/exec/wait.py`](../../orca/exec/wait.py)（WaitExecutor + WaitHandleRegistry Protocol）+ [`orca/events/bus.py`](../../orca/events/bus.py)（wait-handle API）

## 裁定的决策（不再讨论）

1. 保持 `claude -p` CLI 子进程路线（SPEC §1.1）；D1 wave 顺序；D2 descope attach；D3 Budget 不做；D4 ask_user 确定性 tool-params 路由。
2. CLI 单壳中断不经 await-future（SPEC §11.1）；多壳路径保留给 P3。
3. Tape 是唯一 checkpoint：不另起状态序列化系统（反 Conductor）；`replay_state` 复用即 checkpoint。
4. parallel 组中间崩溃不支持 resume（SPEC §7 risk，歧义状态，exit 1）。
5. ask_user 路由参名 `orca_run_id`/`orca_node`（非 `_orca_*`，FastMCP 拒下划线前缀，SPEC §11.2）；
   register 时机前移到 spawn 前 + 按 run 批清（SPEC §11.4）。
6. **WaitExecutor 依赖 `WaitHandleRegistry` Protocol 而非直接持 `EventBus`**（SPEC §11.5，铁律 2 张力化解）。

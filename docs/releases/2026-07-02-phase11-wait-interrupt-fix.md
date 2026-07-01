# 2026-07-02 phase 11 —— Ctrl+G 立即打断 sleeping wait node（wave-3 e2e 审计 bugfix）

## 背景

wave-3 e2e 审计（`test-coverage-e2e` agent）发现一条 SPEC §9.7.6 + §10.2 item9 明确承诺的能力
**实际不工作**：

> Ctrl+G → wait node 立即结束（`wait_completed.interrupted=True`）

审计 agent 留下一条 strict-xfail 复现测试
`tests/exec/test_wait.py::test_e2e_ctrl_g_interrupt_breaks_real_wait_node_through_orchestrator`，
Tape 显示 `wait_completed.interrupted==False`（应 True）、`elapsed_seconds≈2.0`（应 ≪2.0）。

## 根因

`bus.notify_all_waits()` 原本**只**在 `InterruptHandler.record_resolved` / `resolve` 里调，
而这两个入口都被 `Orchestrator._handle_interrupt` 在 **node 边界**（`_drive_loop` 顶部）触发。

当 wait node 正在 `asyncio.sleep` 时：

- `_drive_loop` 协程阻塞在 `_dispatch` 内（wait executor 的 `asyncio.wait(...)` 上）；
- 永远到不了 node 边界的 `_handle_interrupt`；
- → `notify_all_waits` **永远不在 wait sleep 期间被调**；
- → wait 睡满额定 duration 才结束，`interrupted=False`。

即：`notify_all_waits` 的接线对「Ctrl+G 打断正在 sleep 的 wait」的 e2e 路径是**死代码**。
既有单元测试（`test_record_resolved_notifies_wait_handles`、`test_wait_executor_interruptible_can_be_cancelled`）
之所以一直绿，是因为它们**绕过 orchestrator drive_loop**，直接戳 handler/bus 层——覆盖盲区
掩盖了接线缺口。

## 修复（架构-clean，surgical）

**`Orchestrator.request_interrupt` 在登记 pending 的同时，立即调 `self.bus.notify_all_waits()`**
——不等 node 边界。让正在 `asyncio.sleep` 的 interruptible wait 当场被 `asyncio.Event.set` 唤醒。

### 职责划分（Rule 7 裁定：两处都保留，defense-in-depth）

| 调用点 | 语义 | 覆盖路径 |
|---|---|---|
| `Orchestrator.request_interrupt`（**新增**） | 即时副作用：唤醒 sleeping wait | CLI 单壳路径（Ctrl+G → modal 答 → request_interrupt） |
| `InterruptHandler.record_resolved`（保留） | resolve 后打断 | CLI 单壳 node 边界 + 二次唤醒场景 |
| `InterruptHandler.resolve`（保留） | future set 后打断 | 多壳路径（web/mcp 直接 `handler.resolve`，不经 request_interrupt） |

**为何保留 `record_resolved` / `resolve` 里的同一调用**：

1. 多壳路径（web/mcp）不经 `request_interrupt`，直接调 `handler.resolve` —— 移除会回归多壳场景。
2. 「resolve 期间又登记了新 wait」的二次唤醒场景仍需 node 边界的 notify 兜底。
3. `notify_all_waits` 幂等（无 handle 返 0），重复调无害。

`request_interrupt` 新增「调用方线程/循环契约」docstring：本方法是同步，必须在 orchestrator
事件循环线程上调用（与 wait handle 注册同源，避免跨线程 `asyncio.Event.set`）。CLI 单壳天然满足；
未来 daemon `--background`（P3.2）若引入跨线程触发需经 `loop.call_soon_threadsafe` 桥接。

## 改动

- `orca/run/orchestrator.py::Orchestrator.request_interrupt`：新增 `self.bus.notify_all_waits()`
  即时调用 + 结构化日志（`woken > 0` 时记 info，含 `ireq.id` 与唤醒计数）+ 线程/循环契约 docstring。
- `tests/exec/test_wait.py`：移除 `test_e2e_ctrl_g_interrupt_breaks_real_wait_node_through_orchestrator`
  的 `@pytest.mark.xfail(strict=True)` marker（xfail → pass）。

### 配套采用的 wave-3 e2e 测试（非本 fix 改动，随 fix 一起 commit）

- `tests/exec/test_wait.py`：wait handle 防泄漏（跨 run / 打断路径）、`wait_started.reason` 落 tape、
  parallel 两 wait 独立唤醒等补充覆盖。
- `tests/gates/test_dialog.py`：dialog 3 轮历史全量回放 + 序列契约 + 边界字符保真。
- `tests/run/test_validator_orchestrator.py`：validator LLM 自身崩 → fail-safe → workflow 继续。

## 验证

```
$ uv run pytest tests/ -m "not integration" -q
888 passed, 1 skipped, 37 deselected in 64.81s
```

- **xfail 翻转**：`test_e2e_ctrl_g_interrupt_breaks_real_wait_node_through_orchestrator` 由
  strict-xfail 转 pass（marker 已删）。断言 `wait_completed.interrupted is True` + `elapsed < 1.5s`
  均通过（额定 2s）。
- **零回归**：全量 888 passed（基线 879 passed / 1 skipped —— diff = +9 = 1 xfail 翻转 + 8 新 e2e 测试）。
- **既有 wait-interrupt 单测仍绿**：`test_record_resolved_notifies_wait_handles`、
  `test_resolve_also_notifies_wait_handles`、`test_wait_executor_interruptible_can_be_cancelled`
  均通过（修复只新增即时调用，未移除 node 边界调用）。

## Commit

- `fix(phase11): Ctrl+G 立即唤醒 sleeping wait node —— request_interrupt 即时调 notify_all_waits（e2e 审计发现 node-boundary 粒度漏 interrupt）`

## Review

dispatch `code-reviewer` on the fix diff。审计结论：**无 🔴 / 🟡 问题**，3 条 🟢 可选优化：
（1）`request_interrupt` 跨线程契约声明 —— **已采纳**（补 docstring）；
（2）注释精简 —— 部分采纳（保留，因 Rule 1「why 非显然」场景符合详注条件）；
（3）e2e 测试 docstring 历史 xfail 叙述精简 —— 未采纳（保留作历史记录，无功能影响）。

# phase 11 —— `interrupt_resolved` 同步写 Tape 修复（wave-1 e2e 审计）

**日期**：2026-07-02
**类型**：critical correctness bug fix（surgical，非架构）
**严重度**：critical（违反单 Tape 唯一真相源 + 中断配对不变量）

---

## 背景

wave-1 e2e 覆盖审计（test-coverage-e2e agent）发现 CLI 单壳中断路径的一个 critical
正确性 bug：**abort/skip 分支（continue 偶发）的 `interrupt_resolved` 事件从 Tape 上丢失**。

实测 abort tape = `[workflow_started, interrupt_requested, workflow_failed]`，缺
`interrupt_resolved`。三壳（CLI/Web/MCP）重放此 Tape 时看不到中断已解决 → UI 卡在
「中断中」；resume 场景无法判定中断已处理。违反 SPEC §3.2 明示契约 + 单 Tape 唯一真相源铁律。

## 根因

`InterruptHandler.record_resolved`（`orca/gates/interrupt.py`）只 `await bus.emit
("interrupt_requested")` 同步写 Tape，然后把 resolved payload `put_nowait` 给 **async
broadcaster**（`_broadcaster_mixin.py`）异步 emit。`_handle_interrupt` 立即返回。

abort 分支 `_drive_loop` 直接 raise `WorkflowAborted` → `run()` 接住 emit
`workflow_failed` → **`bus.close()`**（`orchestrator.py:217`）。此时 broadcaster task
还没被调度出队 emit `interrupt_resolved`，bus 已 close → `bus.emit` 经 `Tape.append`
抛 `RuntimeError("Tape 已 close")` → 被 `_broadcaster` 的 `except` 吞成 error log
（fail loud 的 log 可见，但事件已永久丢失）。

**非确定性**：continue 分支因 fake executor 的 async generator yield 多次让出 event
loop，broadcaster 在间隙被调度 flush 巧合通过；abort 没有任何 executor 执行（直接
raise），竞速必输；skip 偶发。生产环境 SIGINT 真杀 claude 子进程的时序更不可控，丢失
概率更高。

## 修复（Option A —— 同步写 Tape，broadcaster 仅通知订阅者）

`record_resolved` 改为**同步** `await bus.emit("interrupt_resolved")` 写 Tape（+
同步 fan-out 订阅者），**不经 async broadcaster**。

### 关键洞察
- `record_resolved` 是 async 方法，可直接 `await emit`。
- `EventBus.emit` 第一动作 `await tape.append`（落盘 + flush，在 Lock 内），第二动作
  同步 `put_nowait` 给所有订阅者——Tape 写 + 订阅者通知在一次 await 内全部完成，
  与后续 `bus.close()` 无竞态。
- async broadcaster 现仅留给**同步** `resolve()` 入口（它无法 `await emit`，必须靠
  async 代发）。两条路径职责清晰分离：CLI 单壳 = 同步直发；多壳竞速 = async broadcaster。

### 改动文件
- `orca/gates/interrupt.py`：`record_resolved` 重写（同步 emit requested + resolved，
  不再 put_nowait 入队），模块 docstring 更新记录新职责边界。
- `orca/run/orchestrator.py`：`_handle_interrupt` docstring/comment 更新（同步 emit，
  无逻辑改动）。

### 双写风险排除
旧代码 enqueued 给 broadcaster，broadcaster 的 `_emit_resolved` 会 `bus.emit
("interrupt_resolved")` 第二次（重复 Tape 写）。修复后 `record_resolved` 删除入队，
`_handle_interrupt` CLI 分支走 `record_resolved` 后 return（不调 `request()`），无重复
写路径。多壳 `resolve()` 路径仍用 broadcaster，其 `_emit_resolved` 完整保留。

## 偏离说明

无。Option A 完全贴合 SPEC §4.1 `_handle_interrupt` 内同步 `emit("interrupt_resolved",
...)` 的契约描述。未采用 Option B（run() drain broadcaster）/ Option C（bus.close
drain）——它们给 run() 路径加 broadcaster 生命周期耦合（interrupt_handler 可能为 None），
不如同步 emit 直接根治。

## 验证

### 测试闭环
- `tests/run/test_interrupt_e2e.py` 的 **6 个 `xfail(strict=True)` 测试全部转 PASS**，
  markers 移除。其中 2 个测试的断言修正（xfail 掩盖的测试侧 bug）：
  - `test_e2e_skip_advances_to_next_node_without_executing_current`：移除
    `assert "workflow_completed" in types`——该测试驱动 `_drive_loop()`（非 `run()`），
    与现有 continue e2e 同款驱动方式，不触达 `workflow_completed` 生命周期事件。
  - `test_e2e_abort_emits_workflow_failed_with_abort_reason`：`failed.node` 改
    `failed.data["node"]`——`make_workflow_failed` 把 node 放 payload，非 event 顶层。
- 新增 `test_invariant_emit_on_closed_bus_raises_loud`（Gap-3 闭环）：锁定
  `bus.close()` 后 emit 抛 RuntimeError 的 fail-loud 契约——修复的因果前提。若未来
  EventBus/Tape 改成对 closed 状态静默 no-op，配对不变量测试不会捕获竞态回归，故需
  独立守护。

### 全套件
- 修复前 baseline：`719 passed, 1 skipped, 6 xfailed`
- 修复后：`726 passed, 1 skipped, 0 xfailed, 0 failed`（+6 flipped xfails + 1 新增
  Gap-3 契约测试，零回归）

### 多壳路径回归保护
`test_e2e_multishell_await_future_path_through_orchestrator`（`resolve()` + broadcaster
路径）+ `tests/gates/test_interrupt.py::test_broadcaster_emits_resolved_to_tape` 均
PASS，确认 broadcaster 仍正确服务于多壳 `resolve()` 入口。

## code-review

dispatch code-reviewer 两轮：
1. 代码质量审查：无 critical/moderate，可合并。一项 🟡 建议（docstring 补「不登记
   `_interrupts_meta`」说明）已闭环。
2. 测试覆盖完整性：覆盖判定 adequate — YES。一项 🟡 Gap-3（emit-on-closed-bus 契约
   测试）已闭环。

## Commit

- `fix(phase11): 同步写 interrupt_resolved 到 tape —— 修单壳路径 broadcaster/bus.close() 竞态丢事件（e2e 审计发现）`

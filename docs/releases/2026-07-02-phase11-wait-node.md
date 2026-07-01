# Release Note — phase 11 §9.7 Wait Node（P3.1，2026-07-02）

## 背景

phase 11 第三波第三项：Wait Node —— 一个 `kind: wait` 节点，`asyncio.sleep` 一段时长，
`interruptible=True` 时可被 Ctrl+G 打断。典型用途：API rate-limit 退避、轮询间隔、人工节奏控制。

SPEC：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §9.7（§9.7.6
定义 EventBus wait-handle 公开契约，取代 PLAN 虚构的 `register_interrupt_target`）。

## 改动点

### 新增

- **`orca/exec/wait.py`**（新文件）：`WaitExecutor` + `parse_duration` + `WaitHandleRegistry` Protocol。
  - `parse_duration(s)`：`"30s"→30 / "5m"→300 / "2h"→7200 / "1d"→86400 / "30"→30`（纯数字=秒）；
    非法（空/未知单位/非数字/负）→ `ValueError`。
  - `WaitExecutor.exec`：`node_started → wait_started → (sleep / interrupt) → wait_completed → node_completed`。
    `interruptible=True` 注册 `asyncio.Event` 到 bus，`asyncio.wait([sleep, evt.wait()], FIRST_COMPLETED)`
    让 Ctrl+G 立即打断；`interruptible=False` 必须等满。非法 duration → `node_failed{RenderError}`；
    超 24h → `node_failed{ConfigError}`（fail loud）。
  - `WaitHandleRegistry` Protocol（仅 `register/unregister`）：化解「WaitExecutor 需 bus 访问」与
    「铁律 2 禁 exec 持 bus」的张力 —— exec/ 依赖能力 Protocol（ISP/DIP），EventBus 结构化满足。

- **`orca/schema/workflow.py`**：`WaitNode`（kind=wait, duration, reason, interruptible）+ 加入 `AnnotatedNode`
  判别联合（5 kind）。
- **`orca/schema/event.py`**：`wait_started` / `wait_completed` 加入 `EventType` Literal（31 个）。
- **`orca/events/bus.py`**：`register_wait_handle` / `unregister_wait_handle` / `notify_all_waits`
  （`threading.Lock` 保护 wait-handle 集合）。
- **`orca/exec/error.py`**：`_PHASE_TO_ERROR_TYPE` 登记 `"config": "ConfigError"`（WaitExecutor 超上限用）。
- **`examples/with_wait.yaml`**：`wait(2s) → script`，演示 wait output + Ctrl+G 打断（手动 E2E）。

### 修改

- **`orca/exec/factory.py`**：`make_executor` 加第三参 `bus=None`，wait 分支透传给 WaitExecutor
  （缺 bus → `ValueError` fail loud）。script/set/foreach 分支忽略此参（向后兼容）。
- **`orca/run/orchestrator.py`** + **`orca/run/parallel.py`**：`make_executor` 调用透传 `bus=self.bus`。
- **`orca/gates/interrupt.py`**：`resolve`（多壳）与 `record_resolved`（CLI 单壳）两个 chokepoint
  都调 `bus.notify_all_waits()` —— Ctrl+G 时打断所有 interruptible wait。
- **`orca/iface/cli/widgets/log_stream.py`**：`wait_started` / `wait_completed` 描述。
- **`orca/schema/__init__.py`**：导出 `WaitNode`。

### 测试

- **`tests/exec/test_wait.py`**（新，13 用例）：parse_duration 单位/非法、完整生命周期、Jinja2 渲染、
  interruptible 打断（正向 + 反向）、handle 注销、非法/超上限 fail loud、session_id 一致、
  经 orchestrator 主循环集成（wait→script，下游读 `w.output.interrupted`）、parallel group
  内两个 wait 独立打断、`make_executor` 缺 bus fail loud、`MAX_DURATION_SECONDS` 常量契约。
- **`tests/events/test_bus_wait_handles.py`**（新，6 用例）：register/unregister 幂等、notify 计数、
  并发 register+notify 不损坏集合迭代。
- **`tests/gates/test_interrupt.py`**（+2 用例）：`record_resolved` / `resolve` 都调 `notify_all_waits`
  打断注册的 wait handle（Ctrl+G→wait 端到端证据）。
- **`tests/exec/test_contract.py`**：phase 映射表 +1（`config`/`ConfigError`）。
- **`tests/schema/test_event.py`**：EventType 计数 29 → 31。
- **多处 test fake `make_executor` lambda**：签名加 `bus=None`（factory 新参，向后兼容）。

## 与 SPEC 的偏离（已记入 SPEC §11.5）

1. **`WaitHandleRegistry` Protocol 取代直接持 `EventBus`**（铁律 2 张力化解）—— ISP/DIP，能力裁剪到
   最小，executor 无法经此 Protocol 写 tape/emit。`tests/exec/test_contract.py::test_dependency_no_events_bus_no_tape` 全绿佐证。
2. **`elapsed_seconds` 取 `monotonic()` 实测值**（非额定 `duration_seconds`）——被打断时语义更准确。
3. **超上限走 `phase="config"`**（非 SPEC 示例的 `raise ExecError` 无 phase）——区分渲染语法错与值超限，
   `_PHASE_TO_ERROR_TYPE` 正式登记。

## 验证

- `uv run pytest tests/ -m "not integration"`：**822 passed, 1 skipped**（基线 784 + 38 新增），
  **0 回归**。判别联合改动是回归风险最高处，compile/parser/factory/contract 全绿佐证既有 kind 不受影响。
- `examples/with_wait.yaml` 经 `load_workflow` 解析通过（`kind=wait` → `WaitNode`）。
- Ctrl+G 打断 wait 的自动化证明：`tests/exec/test_wait.py::test_wait_executor_interruptible_can_be_cancelled`
  （长 duration + 中途 notify → `interrupted=True`，<< duration 返回）+ `tests/gates/test_interrupt.py`
  两个 `*_notifies_wait_handles` 用例（resolve/record_resolved 双路径）。
- 手动 E2E（`orca run examples/with_wait.yaml` + Ctrl+G）：记录在 `examples/with_wait.yaml` 注释，
  未自动化（需真 TTY 交互）。

## Commit

`3921c89`（`feat(exec): P3.1 Wait Node —— asyncio.sleep 节点，Ctrl+G 可打断（SPEC §9.7）`）。

## 阻塞 / 后续

无阻塞。wave 3 余项：Validator(P2.1) → Dialog(P2.2)。Wait 是 P3.1（计划第三波第三项）。

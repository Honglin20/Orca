# Release Note — 2026-08-10：in-session elapsed 真相修复（web Log 0s / workflow 0.077s 假值）

## 问题

Web 界面 Log 面板对 in-session run 显示两类假值：

1. **每个 node 显示 `(0s)`**：in-session 路径 `advance_step` emit 的
   `node_completed.data` 只含 `output`、无 `elapsed`（exec/interface.py 契约
   `data={output, elapsed}` 被 in-session 违反）。前端 LogStream 摘要直接读
   `d.elapsed` → `num(undefined ?? 0)` = 0s。
2. **`workflow completed (0.077s)`**：per-call CLI `orca next` 每次调用
   `start_ts = now_monotonic()`，推进时传 `elapsed = now_monotonic() - start_ts`
   ——测成「最后一次 next 调用耗时」，而非 run 真实总时长。docstring 契约
   「elapsed 由 daemon 传真实 workflow 总耗时（M5：不撒谎）」被 CLI 路径破坏。

## 修复

### 引擎面（tape 唯一真相源，M5 不撒谎）

- `orca/events/replay.py`：新增 `ReplayFold` dataclass + `_replay_fold(tape)`——
  单次遍历 fold RunState + 抽 inputs + **顺带捕获 elapsed 锚点**（零额外遍历，
  SPEC §3 O1a 单遍历约束不破）：`workflow_started_ts`（首条 ws，与 inputs 抽取
  同语义）/ `node_started_ts`（每 node **最后一条** ns，recoverable 重 arm 取最新
  attempt 起点）。`_replay_state_and_inputs` 零生产调用方后**删除**（E5 惯例，
  测试改直调 `_replay_fold`）。
- `orca/run/step.py`：`advance_step` 移除 `elapsed` 参数（调用方 per-call 计时
  模型已被证明不可行）；`node_completed` 带 `elapsed`（`time.time() − ns.ts`，
  锚点缺失则省略 key，不写 0 假值）；`workflow_completed.data.elapsed` 从 tape
  ws 时间戳差算（真实 run 总耗时）。
- `orca/iface/in_session/cli.py` / `daemon.py`：bootstrap/next 移除 per-call
  `start_ts`/`elapsed` 透传；daemon 删 `_start_ts`（tape 统一派生）。

### 前端（兼容老 tape）

- `orca/iface/web/frontend/src/selectors.ts`：`summarizeEvent` 增可选
  `nodeElapsed` resolver；`selectLog` 传 store 的 D5 派生值
  （`state.nodes[node].elapsed`，node_completed.ts − node_started.ts 差补，
  不重复 fold）。`node_completed` 在 `data.elapsed` 缺失时回退；两路都无
  （老 tape 缺 node_started）→ 省略耗时括号，不显示 0s 假值（与 AgentsRail
  隐藏策略一致）。

## 测试

- `tests/events/test_replay.py`：`_replay_fold` 锚点捕获测试（首条 ws + 每 node
  最后一条 ns）。
- `tests/iface/in_session/test_in_session_cli.py`：`test_in_session_elapsed_from_tape_timestamps`
  ——node/workflow elapsed ≈ 事件 timestamp 差（±1s 容差），旧 buggy 代码必挂。
- `test_node_memory.py`：删 daemon `_start_ts` 残留行。
- 前端 `selectors.test.ts`：无 `data.elapsed` → 差补显示；有 `data.elapsed` →
  事件值优先。

## 验证

- 后端：events + in_session + resume + web（非 Playwright）1072 passed；
  8 个失败（doctor/push-probe/SKILL.md + Playwright 浏览器环境）git stash
  隔离验证为**改动前已存在**，与本改动无关。
- 前端：`tsc --noEmit` 干净；selectors/log-stream 35 passed（全量 535 中 1 个
  lazy 加载超时 flake，单文件复跑 23/23 过）。

## 遗留

- 已落盘老 tape 的 `workflow_completed.data.elapsed`（0.077s 假值）不可追溯修正；
  前端 node 级已兼容（ts 差补），workflow 级老 tape 仍显示 tape 内值。

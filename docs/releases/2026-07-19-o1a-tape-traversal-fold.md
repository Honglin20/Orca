# Release: O1a —— `advance_step` 内合并两次 tape 全遍历为一次（P3）

**日期**:2026-07-19
**范围**:in-session 性能 SPEC v3.1 §3 O1a（包 P3）
**commit**:`<SHA 见 git log>`

---

## 背景

in-session 每次 `orca next` 是新进程，固定性能税主要在 tape 全遍历 + wf parse。`advance_step` 此前**两次全 tape 遍历**：

- `step.py:323 replay_state(tape)` —— reducer fold 派生 RunState。
- `step.py:328 Orchestrator._inputs_from_tape(tape)` —— 抽 `workflow_started.data.inputs` 给 render 用。

大 tape 场景下两次全扫读事件量翻倍。`apply_event` reducer 只存 `workflow_name`（**不存 inputs**）—— inputs 必须从 tape 抽，不能从 RunState 取。

## 设计取舍

SPEC §3 O1a 经 3 轮 spec-reviewer，**方案 C** 已 stable：

- **新增 `_replay_state_and_inputs(tape) -> tuple[RunState, dict]`** 落 `events/replay.py`（与 reducer 同文件，单一算法源）。
- 单次遍历既跑 reducer fold（派生 state）又抽首条 `workflow_started.data.inputs`。
- `advance_step` 直调合并函数（一次调用拿 state + inputs）。
- `_inputs_from_tape` 改为薄封装调本函数取 `[1]` 部分（**保留对外 API**——`Orchestrator.from_tape` 经 `_bare_instance` 仍是调用方）。

**否决方案**：
- 改 reducer 把 inputs 投影进 RunState —— 破坏 reducer 最小性，inputs 是 ws.data 的派生而非状态机一部分。
- 删 `_inputs_from_tape` —— 触发 `from_tape` 路径改动，违反 SPEC §1.4「replay_state 对外 API 保留」精神。
- in-line advance_step 不抽 helper —— 失去 DRY（inputs 抽取逻辑只能在 events/replay.py 一处）。

## 改动

### 源码

- **`orca/events/replay.py`**：新增 `_replay_state_and_inputs(tape)`（49 行含 docstring）——`RunState` 起始 + `ws_seen` flag 单次遍历，首条 ws 抽 inputs（mirror `_inputs_from_tape` 早返语义），非 dict/缺键 WARN + {}，无 ws 静默 {}。**不接受 `since_seq` 参数**（state 增量合理但 inputs 必须从 0 起扫，语义矛盾；YAGNI 砍掉防 footgun）。
- **`orca/run/step.py:329`**：`advance_step` 改调 `_replay_state_and_inputs(tape)`（删 `replay_state(tape)` + `Orchestrator._inputs_from_tape(tape)` 两行，合并为一行）；移除未用 import `replay_state`。
- **`orca/run/orchestrator.py:533-562`**：`_inputs_from_tape` 改薄封装（4 行实现 + 完备 docstring 标注性能 trade-off + 副作用）。

### 测试

- **`tests/events/test_replay.py`**：+11 测试覆盖 `_replay_state_and_inputs` 全语义：
  - 空 tape / 无 ws（静默不 WARN）/ dict inputs / 非 dict inputs（WARN）/ 缺 inputs 键（WARN）/ 多 ws first-wins / snapshot 逐字相等（vs `replay_state` 独立对比 + 固定 inputs 值）/ 幂等 / **首条 ws 坏后续 ws 好仍返 {}**（锁 `ws_seen` invariant）。
  - `Orchestrator._inputs_from_tape` wrapper parity 参数化（4 case：非 dict / 缺 key / 多 ws / 无 ws）。
  - **SPEC §7 O1a AC3 grep 守门**：AST 扫 `orca/`，断言 `_inputs_from_tape` 生产调用点 ≤1（仅 `_bare_instance`）—— 防 wrapper 再次扩散。
- **`tests/iface/in_session/test_in_session_cli.py`**：+2 tape 遍历计数测试（bootstrap 分支 + advance/output 分支），`_spy_tape_replay` helper spy `tape.replay` 单一 choke point；双锁 `len(res.emits)` + `len(calls) == 1` 加速回归定位。

### 不碰

`replay_state` 对外 API / `apply_event` reducer / EventType / emit 序列 / advance_step 决策三分支 / route 求值 / `from_tape` 路径 / cli.py / SKILL.md / 7 命令面 / marker schema。

## 验证

- **SPEC §1 铁律逐条核**：
  - §1.1 不影响功能：state+inputs 逐字相等（snapshot test + wrapper parity test）。
  - §1.3 tape 唯一真相源：inputs 仍从 `workflow_started.data.inputs` 抽。
  - §1.4 emit 序列稳定：改动只在读路径，emit 完全未触。
  - §1.6 fail loud：坏 inputs WARN，空 tape / 无 ws 静默 {} 是 SPEC v3 §7.5 已确立语义（bootstrap 噪声修复）。
- **SPEC §7 O1a AC 逐条达成**：
  - AC1 tape 遍历 2→1：✅ 双分支独立 `len(calls) == 1` 断言。
  - AC2 snapshot 逐字相等：✅ state 走 `replay_state`（独立函数）对比 + inputs 固定值断言。
  - AC3 `_inputs_from_tape` 调用方：✅ AST grep 守门（≤1 调用点）。
  - AC4 既有测试零回归：✅ 654/654 PASS（events + run + iface/in_session 全跑，含 11 新测试）。
- **code-reviewer impl + coverage 两轮**：0 🔴 / 0 🟡（修复后），🟢 minor 5 项全修：
  - 砍 `since_seq` 参数（YAGNI，防 footgun）。
  - snapshot test inputs 半侧去 wrapper-via-wrapper 自证（改固定值）。
  - 加首条 ws 坏后续 ws 好测试（锁 `ws_seen` invariant）。
  - 加 wrapper parity 参数化测试（防 wrapper 偏离 helper）。
  - 加 AC3 AST grep 守门（防调用点扩散）。
  - `_inputs_from_tape` docstring 量化性能 trade-off（典型 tape 下 `from_tape` 读事件量 N+1→2N）+ 副作用（退化 tape reducer WARN 重复）。
- **回归 sanity check**：模拟旧 2-call 行为 → spy 计数 = 2（确认新测试会捕获回归）。

## 已知副作用 / follow-up

- **`Orchestrator.from_tape` 性能 trade-off**（O1a scope 外）：`from_tape` 既调 `replay_state(tape)` 又调 `_inputs_from_tape`（现 wrapper），wrapper 内部又做全 fold → 典型 tape（ws 在首位）下读事件量从 `N+1`（OLD 早返）变为 `2N`。`from_tape` 仅 crash resume 调用（非热路径），可接受；若 resume 成热路径，可在 wrapper 内联 short-circuit 恢复原性能（DRY 轻微违反换性能）。
- **退化 tape reducer WARN 重复**：含未知 EventType 的 tape，`from_tape` 路径 reducer WARN 会触发两次（replay_state 一次 + wrapper fold 一次）。log 噪声非 correctness。
- **SPEC §7 O1a AC3 措辞建议修订**：原措辞「grep 确认 `_inputs_from_tape` 仅 advance_step 调用」与方案 C 的「薄封装保留 API」路径不完全一致（`_bare_instance` 是非 advance_step 调用方，属豁免范围）。建议改为「`_inputs_from_tape` 公共 API 保留（薄封装调 `_replay_state_and_inputs`）；非 `advance_step` 调用方行为零回归」。SPEC 文档归属用户，不在本 PR 修订范围。

## 文件清单（绝对路径）

- `/mnt/d/Projects/Orca/orca/events/replay.py`
- `/mnt/d/Projects/Orca/orca/run/step.py`
- `/mnt/d/Projects/Orca/orca/run/orchestrator.py`
- `/mnt/d/Projects/Orca/tests/events/test_replay.py`
- `/mnt/d/Projects/Orca/tests/iface/in_session/test_in_session_cli.py`

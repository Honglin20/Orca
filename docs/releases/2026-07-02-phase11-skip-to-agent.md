# 2026-07-02 — phase 11 wave 4 P4: Skip to Agent

## 背景

phase 11 P4「Skip to Agent」（SPEC §9 + §10.2 item12 + §10.3）。wave-1 已实现 SKIP 分支
的「沿 route 跳」路径（`_drive_loop` skip 分支调 `_next_node_after` 求 route）。但 SPEC
§10.2 item12（review item12 修正）指出：**无兜底 route 时**（`when=None` 缺失），自动求值会
`NoRouteMatch` 崩溃，必须 fallback 到 node 选择器让用户显式选目标 node。本 wave 补齐这一缺口，
并新增「SKIP 到任意下游 node」（不仅是 route-next）的能力。

## 改动点

### 1. Orchestrator: 显式 skip 目标（`orca/run/orchestrator.py`）

- `request_interrupt(ireq, answer=None, skip_target=None)` 新增 `skip_target` 参数（CLI 单壳
  路径，NodeSelectModal 选定后传入）。
- `_handle_interrupt` 返回值从 `str`（action）改为 `tuple[str, str | None]`（action, skip_target）。
  消费 `self._interrupt_skip_target`，并在 `record_resolved` **之前** 校验目标合法（review 🔴
  修复：避免校验失败时 tape 留下脏 `interrupt_resolved` 孤儿事件）。
- 新增 `_validate_skip_target(target, current)`：目标必须存在于 `wf.nodes` / `wf.parallel`，
  且不能等于当前 node（防死循环）。fail loud → `ValueError`（clear error，**非** NoRouteMatch
  崩溃）。P4 简化：不约束 reachability / cycle（cycle 由 `max_iter` 兜底）。
- `_drive_loop` skip 分支：`skip_target` 非 None 时直接 `current = skip_target`（emit route_taken
  让 reducer 跟踪），不经 route 求值；None 时走 wave-1 的 `_next_node_after`（兜底 route）。
- `_interrupt_skip_target` 字段加入 `__init__` / `_bare_instance` / `_DRIVE_REQUIRED_FIELDS`
  （防 resume bypass __init__ 时字段漂移）。

### 2. Router: §9.2 skip 容错（`orca/run/router.py`）

- `resolve()` 在 `output is None`（= skipped node）时，`when` 表达式求值失败（UndefinedError /
  AttributeError on None）视为「该 route 不匹配」，继续找兜底 route（避免 NoRouteMatch 崩溃）。
- **仅 `output is None` 时启用容错**；非 skip 路径的 when 求值失败仍 fail loud（RouteError）。
- 隐式契约（注释显式化）：`output is None` 当前等价于 skipped node（由 orchestrator skip 分支
  设 `outputs_acc[node]={"output":None,"skipped":True}` 保证；非 skip 路径的 raw output 经
  `{"output": raw}` 包装，router 拿到的不是裸 None）。

### 3. InterruptHandler.record_resolved: skip_target 写 tape（`orca/gates/interrupt.py`）

- `record_resolved(...)` 新增 `skip_target: str | None = None` 参数。
- 仅在非 None 时写入 `interrupt_resolved.data.skip_target`（向后兼容：wave-1 的 skip 无此字段，
  `test_skip_no_target_omits_skip_target_field` 锁定）。reducer 对此字段 no-op（不改顶层 RunState，
  跳转结果由 route_taken / node_skipped 承担状态转换）。

### 4. NodeSelectModal（`orca/iface/cli/screens/node_select_modal.py`，新增）

- `ModalScreen[str | None]`，OptionList 列出 workflow 全部 node 名 + parallel 组名（排除当前 node）。
- 顶部固定「route-default (next)」选项（dismiss None = 走兜底 route / 默认下一 node）。
- Esc = 取消（dismiss None，不 skip，回到 workflow 原状）。
- **UX pattern A**（Rule 7 裁定）：InterruptModal dismiss `("skip", None)` → app 推 NodeSelectModal
  → 用户选 → app 调 `request_interrupt(..., skip_target=picked)`。保持 InterruptModal 单一职责
  （continue/skip/abort 三选一），NodeSelectModal 单一职责（skip 到哪）。

### 5. OrcaApp.action_interrupt（`orca/iface/cli/app.py`）

- InterruptModal dismiss 后，若 action == "skip"，push NodeSelectModal 等用户选目标。
- 候选 node 列表由 `wf.nodes` + `wf.parallel` 派生（app 持 wf，modal 不依赖 schema）。
- 选定后随 `request_interrupt` 一起登记（node 边界一次性消费 action + guidance + skip_target）。

### 6. LogStream format_event（`orca/iface/cli/widgets/log_stream.py`）

- `interrupt_resolved` 在 skip + 显式 target 时显示 `interrupt skip → <target>`（可观测跳转意图）。

## 验证

- **基线**：888 passed, 1 skipped → **904 passed, 1 skipped**（+16：11 `test_skip_to_agent.py` +
  5 `test_node_select_modal.py`）。零回归。
- **dispatch code-reviewer**：1 🔴（验证顺序导致脏 tape）+ 3 🟡（router 隐式契约注释、多壳路径
  skip_target=None 契约测试、parallel 组作为目标测试）+ 若干 🟢。**全部已修**：
  - 🔴 校验前置到 `record_resolved` 之前 + 补 tape 一致性断言（`test_skip_to_nonexistent_target_fails_loud`）。
  - 🟡 router.py:86 加隐式契约注释。
  - 🟡 新增 `test_multishell_path_returns_skip_target_none`（多壳路径退化契约）。
  - 🟡 新增 `test_skip_to_parallel_group_target`（parallel 组作为目标端到端）。

### 测试清单（`tests/run/test_skip_to_agent.py`，11 例）

- `test_skip_to_route_next_when_fallback_exists`：SKIP 无目标 + 兜底 route → 沿 route 跳（wave-1 不变量）。
- `test_skip_to_explicit_target_jumps_there`：SKIP + skip_target="c" → 直跳 c，绕过 b。
- `test_skip_to_nonexistent_target_fails_loud`：不存在目标 → ValueError + tape 无脏 interrupt_resolved。
- `test_skip_to_self_fails_loud`：skip 到自己 → ValueError。
- `test_skipped_node_output_none_tolerated_in_route_evaluation`：§9.2 route 容错端到端。
- `test_skip_target_recorded_on_tape`：interrupt_resolved.data.skip_target 可观测。
- `test_skip_no_target_omits_skip_target_field`：无目标时省略字段（向后兼容）。
- `test_router_resolve_tolerates_skipped_none_output`：router 单元层 §9.2 容错。
- `test_router_resolve_non_skipped_failure_still_fails_loud`：非 skip 路径仍 fail loud。
- `test_multishell_path_returns_skip_target_none`：多壳路径 skip_target 恒 None。
- `test_skip_to_parallel_group_target`：parallel 组作为目标。

### Modal 测试（`tests/iface/cli/test_node_select_modal.py`，5 例）

- compose（含 route-default + 候选排除当前 node）、选具体 node、选 route-default、Esc 取消、
  title 显示当前 node。

## Commit

- `feat(run): phase 11 P4 — Skip to Agent（显式 skip 目标 + NodeSelectModal + §9.2 route 容错）`

## SPEC 偏离

无。本 wave 逐字实现 SPEC §9 + §10.2 item12 + §10.3。signature 选择（`skip_target` 作为
`request_interrupt` 独立参数 vs 折进 `answer` tuple）属 Rule 7 裁定，已在 docstring 详述理由
（`answer=(action, guidance)` 语义 cohesive，`skip_target` 是 SKIP 专属派生信号，两者正交分开
比 3-tuple 清晰）——记入 SPEC §11.8。

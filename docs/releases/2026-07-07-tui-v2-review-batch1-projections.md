# 2026-07-07 TUI v2 review remediation + 批 1 backend 基础（Status.blocked + projections.py）

> ADR §4.3 / §4.3.1 / §8.1 + TUI v2 review 两项 remediation 的合并落地。
> Commit: 见 CHANGELOG。

## 背景

TUI v2 重构全部提交后做了一次 review，发现两项必须修的问题，同时 ADR §4.3/§4.3.1
的批 1 backend 部分（Status.blocked + projections.py）也到了落地时机（TUI 工作树
已干净，可安全编辑 app.py / orchestrator.py / state.py）。本 commit 合并三项：

1. 🔴 **Enter 展开非末条 entry 失效回归**（commit 5562e5e 引入）：j/k hoist 到 App
   级做 agent 切换后，**无键绑定到 `AgentHistory.action_cursor_down/up`**，
   `_selected_seq` 恒 None → `action_toggle_expand` 早 return。单测没发现因直接设
   `_selected_seq` 绕过真实键位。
2. 🟡 **`blocked` 字符串自造**（ADR P4 违规）：canonical `Status` Literal 未含 blocked，
   TUI 用字面量 `"blocked"` 拼 icon。
3. 批 1 follow-up：`Status.blocked` + `projections.py` 单一派生算法源（ADR §4.3.1）。

## 交付

### 修复 1：Enter 展开回归（cursor 导航键补全）

- `orca/iface/cli/app.py` App 级 BINDINGS 加：
  - `Binding("down", "history_cursor_down", ..., priority=True)`
  - `Binding("up", "history_cursor_up", ..., priority=True)`
- 新增 `action_history_cursor_down` / `action_history_cursor_up` 方法，转发到
  `AgentHistory.action_cursor_down/up`（既有 widget action 接口零修改，单测通道保留）。
- **Rationale（键位选择）**：
  - j/k 已被 AgentsList 占用做横向 agent 切换，再用 j/k 做 entry 导航会冲突。
  - down/up 做**同 agent 内 entry 导航**，与 j/k 职责正交（VS Code / Conductor 同款）。
  - `priority=True` 关键：RichLog 继承 ScrollableContainer 默认绑 down/up 滚屏；
    无 priority 时 widget 级 BINDINGS 优先于 App 级，down/up 被 RichLog 吞做滚屏
    （commit 5562e5e 回归根因之一）。priority=True 后 App 级命中优先，RichLog 不再吞；
    用户仍可用 PageDown/PageUp 滚屏（未被占用）。
- 端到端 pilot 测试（`tests/iface/cli/test_app.py::TestHistoryCursorAndExpand`）：
  - `test_down_arrow_selects_first_entry`：down → _selected_seq None→entries[0].seq
  - `test_down_then_enter_toggles_expand`：down → enter → 展开 → enter → 收起
  - `test_down_up_navigate_between_entries`：down → down → up 边界不 wrap

### 修复 2 + 批 1：Status.blocked + projections.py（ADR §4.3/§4.3.1）

**schema 层**（`orca/schema/state.py`）：
- `Status` Literal 加 `blocked`：`pending/running/done/failed/skipped/blocked`

**reducer 层**（`orca/events/replay.py::apply_event`）：
- 扩展 blocked 派生：`human_decision_requested` / `interrupt_requested` → 若 node
  当前 None 或 running → blocked（对齐既有 TUI 行为，覆盖竞态 / 测试场景）
- `human_decision_resolved` / `interrupt_resolved` → blocked 回 running
- 终态（done/failed/skipped）不被 blocked 覆盖
- **已知限制（文档化）**：同 node 多 gate 同时 active 时，首个 resolved 会把 blocked
  回 running（其他 gate 仍 active）。proper 多 gate 计数需 RunState 加 active_blockers
  字段，超出批 1 范围；现有 TUI 手动 update_node 的语义本就如此，本派生算法对齐既有行为。

**projections.py（新文件，`orca/run/projections.py`）**：
- ADR §4.3.1 单一派生算法源。4 个纯函数 batch fold：
  - `node_status(events) -> dict[str, Status]`：含 blocked 派生，**委托 apply_event**
    （与 replay_state 增量 reducer 同源，DRY）
  - `node_usage(events) -> dict[str, UsageSummary]`：last-wins（按 seq），opencode
    per-step 累积值语义
  - `node_session_ids(events) -> dict[str, list[str]]`：retry 时新 session_id append
  - `node_iter(events) -> dict[str, int]`：len(session_ids[node])
- 依赖单向：仅 import `orca.schema` + `orca.events.replay`；不依赖 `orca.run.*`
  其他子模块、不依赖 `orca.iface`（消费层）

**TUI 层**（`orca/iface/cli/app.py`）：
- 新增 `_all_events: list[Event]`（无界；TUI 是交互式工具，典型 workflow ≤ 数千 events）
- `_dispatch_to_widgets` 重构：
  - 每事件先 append 到 `_all_events`
  - status / iter / usage 全部经 projections 派生（消除独立 fold 副本）
  - 删 `_node_session_ids` / `_per_node_last_usage_seq` 字段（projections 内置同算法）
  - gate/interrupt 事件的 blocked 派生走 `projections.node_status().get(node, "running")`
    （不再字面量 `"blocked"`，P4 合规）
- 删 `_node_session_ids` / `_per_node_last_usage_seq` UI 副本字段

**icon 表**（`orca/iface/cli/widgets/_icons.py`）：
- 补 `skipped: ⊘`（ADR §8.1 全覆盖：icon 表 key 必须与 Status Literal 完全一致）

**AgentsList widget**（`orca/iface/cli/widgets/agents_list.py`）：
- `NodeProj.status` 类型从 `str` 收紧为 `Status`（ADR §4.8：入参类型引用 schema 层权威）
- `update_node(status: Status | None)` 同步收紧
- 第 190 行 `if proj.status == "failed" and proj.error_msg:` 改为 `if proj.error_msg:`
  （P4：不字面量比较 status；error_msg 仅 failed 节点由 app dispatch 注入，truthiness 等价）

### 守门机制（ADR §8.1）

**`tests/iface/cli/test_status_literal.py`（新文件）**：
- `Status` Literal 全覆盖（6 值，含 blocked）
- `NODE_STATUS_ICONS` keys 与 Status Literal 完全一致（icon 表不漏 key）
- **AST 守门**：遍历 `orca/iface/cli/**/*.py` 所有源码，找 `== "blocked"` 等 Status
  字面量比较（Compare 节点 Eq/NotEq + Constant），命中即返工
- 守门 fixture 路径用 `parents[3]`（4 级 parent）+ `.exists()` 断言，防回归

**`tests/run/test_projections.py`（新文件，31 测试）**：
- node_status（含 blocked 从 gate/interrupt 派生，None/running/terminal 三路径）
- node_usage（last-wins, 乱序跳过, cache optional, same-seq 幂等）
- node_session_ids / node_iter（retry append, 同 sid 去重, 空 sid 跳过）
- 重放一致性 + apply_event ↔ projections DRY 一致性（gate + interrupt 交叉场景）

## Deviations from plan

- **plan 里说 cursor 键首选 down/up，与 RichLog scroll 冲突时改 ctrl+j/ctrl+k**：
  实际用 `priority=True` 解决了 RichLog 冲突，不需要 ctrl+j/ctrl+k（ctrl+j 在某些
  终端被解释为 LF/Enter，反而不稳）。
- **plan 里 projections.node_status 派生规则**：plan 说 "node 有未 resolved 的
  human_decision_requested 或 interrupt_requested 事件且无对应 resolved"。实现简化为
  simple overlay（首个 resolved 即回 running），多 gate 同时 active 的边缘场景作为
  已知限制文档化（既有 TUI 行为本就如此）。proper counting 需 RunState 加字段，批 4 范围。

## 测试结果

- `tests/run/test_projections.py`：31 passed
- `tests/iface/cli/test_status_literal.py`：4 passed
- `tests/iface/cli/test_app.py`：含 3 新 pilot 测试，全过
- 全仓回归（不含 e2e）：1596 passed, 31 skipped（baseline 1558 + 38 新增 / 0 回归）

## 后续 follow-up

- **性能 O(N²)**：`_dispatch_to_widgets` 每事件全量 refold `_all_events`。批 1 不阻塞
  （典型 workflow ≤ 3000 events 可接受），长跑 workflow（>5000 events）会感知卡顿。
  批 4 增量化或 memoize。
- **多 gate 同时 active 的精确计数**：批 4 给 RunState 加 `active_blockers` 字段。
- **接口收敛 ADR §8.1 表述订正**：ADR 原文「AST 检查 widget 无 `== "blocked"` 字面量
  比较」范围扩为「widget 无 Status 字面量比较」（test_status_literal.py 的实现）。
  batch 2 PR 一并订正 ADR 表述。

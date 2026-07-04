# TUI 重设计 v1.1.1 —— 真用户验证 4 GAP 收口（A/B/C/E）

> **关联**：[TUI Redesign Draft v1.1.1](../specs/tui-redesign-draft.md) / [TUI 重设计 v1](2026-07-04-tui-redesign-v1.md)
> **分支**：`phase13-render-chart`
> **类型**：bug fix（surgical，spec v1.1.1 字段级修订）

---

## 摘要

test-coverage-e2e 重放 mxint + demo_loop 真跑 tape 时发现 TUI 重设计 v1 有 **4 个用户可见 spec 违规 gap**：

- **GAP-A**（critical）：DAG 节点行 3 全显 `-- tok`（应显实际 token 数）
- **GAP-B**（critical）：Activity Stream 60 条 tool_result 全显 `?  {}`（应显 `glob **/*.py` 等）
- **GAP-C**（medium）：Activity Stream tool_result meta 缺 `· <N>s`（elapsed）
- **GAP-E**（critical）：DagGraph 重放 demo_loop tape crash（`CycleDetected: counter -> counter`）

本次提交 surgical 修复 4 个 gap，并订正 spec §5.4 字段定义（tool_result meta 从 `<N> lines · exit <code> · <elapsed>s` 改为 `<N> lines · <elapsed>s`，exit_code 可选——canonical Event 不支持）。

**全跑通过**：1392 passed 0 回归（baseline 1380 + 12 新增断言）；mxint tape 重放 5/5 节点 tokens 全非 None + 60/60 tool_result summary 含 tool name + 60/60 meta 含 elapsed · 0 显 exit；demo_loop tape 重放 counter iter=3（与 node_started 次数一致）+ 不抛 CycleDetected。

---

## 4 个 GAP 修复点

### GAP-A：DAG NodeProjection.tokens 永远 None

**根因**：`orca/iface/cli/app.py:682-694` 的 `agent_usage` 分派块只投 Header footer（`self._per_node_usage`），**没**同步调 `DagGraph.update_node_projection(node, tokens=...)` → DAG 节点 `NodeProjection.tokens` 永远是 None → 行 3 显 `14s · -- tok`。

**修复**（`app.py`，1 行同步）：在 `if event.seq >= last_seq:` 块内补 `self.query_one(DagGraph).update_node_projection(node, tokens=in_tok + out_tok)`。与 Header footer **同源同步**（同一 agent_usage event 投影到 DAG + Header）。

**spec §4.4 acceptance**：「取该 session_id 最后一条 `agent_usage.data.input_tokens + output_tokens`」—— opencode translator per-step 是累积值，故取最后一条覆盖（同 `last_seq` 守卫）。

### GAP-B：tool_result summary 显 `?  {}`

**根因**：`activity_stream.py:158-163` 用 `data.get("tool", "?")`，但 canonical `agent_tool_result` event 的 data 只有 `{tool_call_id, result}`（实测 mxint tape 60 条全无 `tool` 字段）→ 默认 `?` + `args` 默认 `{}` → 显 `?  {}`。

**修复**（`activity_stream.py`）：Activity Stream 内部维护 `tool_call_id → (tool, args, call_ts)` cache：
- `agent_tool_call` 到达时填 cache（key=tool_call_id，value=tool+args+timestamp）
- `agent_tool_result` 到达时反查 cache，**派生** tool/args/elapsed 填进 enriched data
- `build_entry` 用 enriched data 渲染 summary（仍是纯函数，不感知 cache）

实现 spec §5.4「**与 call 同 entry，meta 升级**」语义——虽然 call/result 是两个 Event，但 entry 渲染时通过 cache 合并还原 "同 entry" 语义。

**LRU 上限保护**：cache cap=500（长跑 workflow 不爆内存），超 cap 丢最旧（FIFO，dict 保插入序）。

### GAP-C：tool_result meta 缺 elapsed

**根因**：canonical `agent_tool_result` data 既无 `exit_code` 也无 `elapsed`。原代码假设 translator 会产这些字段 → 永远没值。

**修复**（2 部分）：
1. **elapsed 可派生**：从 `agent_tool_call.timestamp` + `agent_tool_result.timestamp` 差值算（顶层 Event 字段，spec §3 event.py:78）。在 GAP-B 的 cache 里同时存 call_ts，result 时反查并 `max(0.0, ts - call_ts)`（防时钟漂移负数）。
2. **exit_code 不可派生**：canonical Event 无此字段，translator 也不产。**更新 spec §5.4**：tool_result meta 改为 `<N> lines · <elapsed>s`（去掉 `exit <code>`，主路径不显；若未来 translator 补 exit_code 则追加 `· exit N`，forward-compat）。

**`_format_elapsed_sec` 新 helper**：tool_result 普遍 < 1s（实测 opencode glob 0.0003s），需 1 位小数精度；与 node elapsed（普遍整秒）不同语义，故新增独立 helper（不 DRY 复用 `_dag_render.format_elapsed`）。

### GAP-E：DagGraph 重放 loop workflow crash

**根因**：`DagGraph.build_from_workflow` 调 `_assert_acyclic(self._topo)`，对 self-loop（demo_loop 的 `counter → counter`）也抛 `CycleDetected`。但 loop workflow 用 routes 自指表达重入，是合法拓扑。

**修复**（`dag_graph.py`，~10 行）：在 edges 构造时检测 `src == tgt`：
- self-loop 边**不进** `Topology.edges`（避免 detect_cycle 误报 + fan_in 自增）
- self-loop 节点存进 `self._self_loop_nodes: set[str]`（标记用）
- 多节点环（A→B→A）仍 fail loud（真无效拓扑，spec §11 风险表）

**spec §4.4.1 acceptance**：「重放 demo_loop tape（loop 多次同节点）→ 该节点 iter N 与 node_started 次数一致」—— 通过 iter N ≥ 2 作为视觉信号（counter iter=3 与 demo_loop 真跑 3 次 node_started 一致）。

---

## 字段级对齐证据（修复前后对照）

### mxint_analysis tape（186 events）重放断言

| 节点 | 修复前 tokens | 修复后 tokens | spec §4.4 |
|---|---|---|---|
| analyzer | None（显 `-- tok`） | **272** | input(102) + output(170) |
| configurator | None | **207** | input + output |
| runner | None | **21619** | input + output（深链 6 步累积） |
| diagnostic_saver | None | **516** | input + output |
| report_painter | None | **492** | input + output |

### mxint 60 条 tool_result entry（GAP-B/C）

| 项 | 修复前 | 修复后 | spec §5.4 |
|---|---|---|---|
| summary（含 tool name） | `?  {} × 60` | `glob  **/*.py` / `read  /path/...py` / `bash  python -c "..."`（60/60 含 tool name） | title source = "（与 call 同 entry，meta 升级）" |
| meta | `5 lines` × 60（缺 elapsed） | `5 lines · 0.0s` / `1 lines · 0.0s`（60/60 含 elapsed） | meta source = `<N> lines · <elapsed>s`（v1.1.1 GAP-C 修订） |
| exit | 0 条显 | 0 条显（canonical 不支持） | spec §5.4 v1.1.1：exit_code 可选 |

### demo_loop tape（14 events）重放断言

| 项 | 修复前 | 修复后 | spec §4.4.1 |
|---|---|---|---|
| DagGraph 构造 | 抛 `CycleDetected: counter -> counter` | 正常构造，counter ∈ self_loop_nodes | "重放 demo_loop tape → 该节点 iter N 与 node_started 次数一致" |
| counter.iter_n | crash | **3** | node_started × 3 → iter=3 ✓ |
| counter.fan_in_total | crash | **0**（self-loop 不算入边） | fan_in = 静态拓扑入边数 |

---

## 测试覆盖

新增 8 个测试到 `tests/iface/cli/test_tui_redesign.py`（按 GAP 分类）：

- `TestGapADagTokensProjection`
  - `test_agent_usage_updates_dag_projection` —— agent_usage 同步投 Header + DAG
  - `test_multiple_usage_last_seq_wins` —— spec §4.4 acceptance：最后一条覆盖
- `TestGapBToolResultSummaryFromCache`
  - `test_tool_result_uses_call_cache_for_summary` —— GAP-B：tool name + args 显出
  - `test_tool_result_meta_includes_elapsed` —— GAP-C：meta 含 elapsed
  - `test_tool_result_meta_shows_exit_if_translator_provided` —— forward-compat
  - `test_tool_call_id_cache_lru_cap` —— cap=500 不爆内存
  - `test_replay_rebuilds_same_cache` —— fold 性质（重放产相同 cache）
- `TestGapESelfLoopWorkflow`
  - `test_build_from_workflow_allows_self_loop` —— counter → counter 不抛
  - `test_self_loop_excluded_from_fan_in` —— self-loop 不算 fan_in
  - `test_multi_node_cycle_still_raises` —— A→B→A 仍 fail loud
  - `test_loop_workflow_iter_matches_node_started_count` —— counter iter=3（node_started × 3）

**全跑通过**：
- `tests/iface/cli/test_tui_redesign.py`：47 passed（39 baseline + 8 新）
- `tests/iface/cli/`：394 passed 7 skipped
- `tests/`（除 e2e_mxint/e2e_phase14）：**1392 passed 30 skipped 0 回归**

## 真 TUI 重放验证

新增 `tests/iface/cli/_tui_gap_verify.py`（开发期脚本，pytest 不收）：重放 mxint + demo_loop tape，断言关键修复点。

**mxint tape（186 events）输出**：
```
[GAP-A] 5/5 节点 tokens 全非 None（analyzer=272 / configurator=207 / runner=21619 / diagnostic_saver=516 / report_painter=492）
[GAP-B] 60 条 tool_result summary 全含 tool name + 主要参数；0 条显 '?  {}'
[GAP-C] 60/60 含 elapsed · 0 显 exit
```

**demo_loop tape（14 events）输出**：
```
[GAP-E] counter iter=3（与 demo_loop node_started 次数 3 一致）
[GAP-E] counter ∈ self_loop_nodes（不抛 CycleDetected）
```

SVG 截屏：`tests/iface/cli/_artifacts/gap_verify_mxint.svg` + `gap_verify_demo_loop.svg`。

---

## 显式不做（v1.1.1 范围外）

- **不动 canonical Event schema**（spec §11 裁决 12.8）—— exit_code 缺失就更新 spec 而非加字段
- **不改 translator 加 exit_code**（出 v1 范围）—— 但代码 forward-compat（若未来 translator 补了 exit_code，meta 追加 `· exit N`）
- **不动 phase-15 render layer 契约**
- **不重写 DagLayout 算法**（仅 `build_from_workflow` 加 self-loop 特殊处理）

---

## Commit

- `225933e` `fix(tui): GAP-A/B/C/E 真用户验证 spec 违规收口`

## Deviation from plan

无。所有 4 个 gap 按 prompt 中给定的修复路径实现，未偏离。

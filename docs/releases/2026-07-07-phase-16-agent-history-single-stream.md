# phase-16 —— AgentHistory 单流重构（CC 风格 inline + 工具配对折叠）

**日期**：2026-07-07
**范围**：只动 `orca/iface/cli/widgets/agent_history.py` 内部渲染 + `tests/iface/cli/test_widgets.py::TestAgentHistory` 调整 + 新增 `tests/e2e_phase16/test_boot_smoke.py` + 1 处 e2e_phase12 断言回填。
**SPEC**：[`docs/specs/phase-16-agent-history-single-stream.md`](../specs/phase-16-agent-history-single-stream.md)
**前置 design draft**：[`docs/specs/agent-history-single-stream-design-draft.md`](../specs/agent-history-single-stream-design-draft.md)

---

## 1. 做了什么

把 AgentHistory 从**两区**布局（上部 `#agent-history-log` RichLog + 下部 `#agent-history-detail-wrap` 独立 detail 面板）重构为**单流 inline** 模型（Claude-Code 风格）：

1. **单条 RichLog 流**（§2.1）：删 `#agent-history-detail*` DOM + `_detail_view` + `_refresh_detail`（铁律 #7，无兼容路径）。所有内容写进**唯一**一条 `#agent-history-log`；展开的 entry 紧跟其摘要行下方内联 detail（缩进 `⎿` 引导 + 缩进 renderable）。

2. **工具 call+result 配对成一条 ToolEntry**（§2.2，解决 B1「工具混杂在一起」）：
   - `agent_tool_call` 建 running ToolEntry 入 `_entries`，登记 `_tcid_to_entry_idx`；
   - `agent_tool_result` 到达时按 `tool_call_id` O(1) 反查 call 位 index，**就地升级**（`entries[i] = merged`，`merged.seq = call.seq`）——不 remove+append，避免 `_selected_seq` dangling；
   - 配对后 entry 数 ↓（mxint analyzer 30 events → 19 entries，11 对 tool 全 merged）。

3. **视觉分级**（§2.3，解决 B2「message 看不清」）：message 摘要行 bold + `$success`；thinking dim italic；tool 中性色 + status icon `✓/…/✗`。`_HistEntry.kind: Literal["tool","message","thinking","other"]` 派生自 event_type。

4. **Enter = 全量 reflow**（§2.4）：detail 现在内联在同一条 RichLog，toggle 必须 clear + rewrite 整流。

5. **reducer fold 顺序无关**（§5.6，铁律 #1）：配对靠 `tool_call_id` 匹配 + `_pending_results` 缓冲（result 早于 call 到达时暂存，call 到达时补配对），正序/逆序回放产相同 `(seq, kind)` 集合。

---

## 2. 接口契约（零变化，铁律 #4）

公开 API 签名 phase-16 前后逐字相等（`inspect.signature` 对齐）：
- `set_node(name, events)` / `append_event(event)` / `set_executor(executor)`
- `action_cursor_down()` / `action_cursor_up()` / `action_toggle_expand()`

OrcaApp 调用点（`_dispatch_to_widgets` / `_on_node_selected` / `action_*` 转发）**不动**。

---

## 3. 铁律守门

| 铁律 | 守门证据 |
|---|---|
| #1 壳无真相 | 配对/派生是 event list 纯函数；`test_out_of_order_replay_set_equal` + `test_deterministic_replay_same_set` 锁定 |
| #2 render layer 零改动 | `git diff HEAD -- tool_render/ _event_summary.py` 干净（`normalize.py` 改动是 pre-existing dirty，非 phase-16） |
| #3 依赖单向 | 仅 import `orca.schema` + textual + rich + stdlib + 本包 `_event_summary` / `tool_render` |
| #4 接口统一性 | 公开 API 签名零变化（见上） |
| #5 fail loud | `append_event` orphan result（无 call 配对）降级独立 entry（不静默吞）；`test_append_orphan_result_degrades_not_silent` 锁定 |
| #7 不留兼容路径 | `query_one("#agent-history-detail")` 必抛 `NoMatches`；`test_replay_boots_and_pairs_all_tools` 元 AC 锁定 |

---

## 4. 测试结果

- **`tests/iface/cli/test_widgets.py::TestAgentHistory`**：28 passed（含 7 个新增：kind 派生 / 配对 in-place 升级保 seq / 乱序 reducer fold / orphan pending / append orphan 降级 / failed status 派生 / 确定性回放）
- **`tests/e2e_phase16/test_boot_smoke.py`**：3 passed（真 tape `90fd22` replay + 60 对 tool 全配对 + Enter pilot.press 元 AC + tool entry 内联展开双向断言）
- **`tests/iface/cli/` 全量**：419 passed / 7 skipped（skipped 均需真 claude CLI + ANTHROPIC_API_KEY）
- **`tests/e2e_phase12/`**：1 处断言回填（`len(history.entries)` 改为 `events - tool 对数`，phase-16 契约变更）
- **回归**：phase-12/13 e2e 中 `test_opencode_drives_tui_end_to_end` / `test_opencode_deepseek_drives_chart_pipeline_e2e` 因**环境问题**（gate HTTP port 7421 in use + plotext 缺包）跑不通，非 phase-16 引入；断言层已对齐新契约。

---

## 5. Pilot sanity 证据（实施 agent 自证）

用真 tape `runs/mxint_analysis-20260704-105608-90fd22.jsonl`（analyzer 节点 30 events）：

```
analyzer events: 30
  calls=11 results=11 msgs=1
after fold: total entries=19 tool=11 (merged=11) msg=1
unmatched: 0
expanded_seqs={29}      # last message seq 默认展开
first tool entry: tool_name=glob status=completed meta='5 lines · 0.0s'
```

- **配对完整性**：11 对 call+result 全部 merged，0 unmatched（SPEC §5.2 AC）
- **Enter 内联展开双向**（pilot 真键位）：折叠态渲染文本不含 `⎿`；展开 tool entry 后含 `⎿`（内联 detail 引导符）
- **§5.0 元 AC**：monkey-patch `app.action_history_toggle_expand` 计数；`pilot.press("enter")` 后 `call_count == 1`（真实键位派发命中，非直调冒充）

---

## 6. 性能验证

- **mxint report_painter 真实 bucket（79 events）**：`set_node` 全量 fold = **30.9ms**（SPEC §7 mitigation 标准 < 300ms ✓）
- **合成压力（4000 events / 2000 对）**：733ms（stress case；真实 per-node bucket 远小，虚拟化留 v2，SPEC §6）

---

## 7. 已知 dev / 待办（移交下一 agent）

- **SPEC §5.1 九行按键矩阵完整 E2E**：本阶段交付 boot smoke（app boots + reflow + Enter + 工具配对在真 tape 上工作）。完整按键矩阵（↓↑/jk/C/a/L/t 每键 §5.0 元 AC + state + 双向渲染文本）由下一 agent `test-coverage-e2e` 在 `tests/e2e_phase16/test_tui_buttons_e2e.py` 落地。
- **SPEC §5.3 Console.capture ANSI bold 断言**：message 视觉分级的 ANSI bold/主题色码断言留 `test_tui_buttons_e2e`（boot_smoke 仅验 `⎿` 文本存在）。
- **SPEC §5.5 render layer 字节契约**：render layer 本阶段零改动（`normalize.py` pre-existing dirty 非 phase-16）；若后续 render layer 动了再补 Console.capture ANSI 逐字节对比。

---

## 8. Commit

- `feat(tui): phase-16 AgentHistory 单流重构（CC 风格 inline + 工具配对折叠）`（见 git log）

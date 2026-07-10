# 2026-07-07 TUI bugfix 批次 A

> 三块 layout 修复 + AgentHistory 三个体感 bug。code-reviewer review 后回改（闭环 2 🟡 + 1 🔵）。

## 交付

**Layout（上一轮，本提交一并入库）**：
- `NodeDetail` CSS 加 `display: none`（原仅 `height:0+offset`，offset 不移出布局流 → 与 `#right-pane` 抢横向空间，把右侧栏挤到 width=1，AgentHistory/LogStream 全黑）。
- `AgentsList` 加 `height: 1fr`（原 auto-size 只够内容行，多 node 截断）。

**AgentHistory 体感 bug（A 批）**：
- **A.1 Enter 没反应**：`action_toggle_expand` 在 `_selected_seq is None` 时默认作用于最后一条 entry（旧逻辑直接 return，用户不知要先 ↓ 选中）。不移动光标避免全量 reflow。
- **A.2 死键 `c`**：移除 App 级 + NodeDetail widget 级两处 `c` 绑定（NodeDetail display:none 不可见，c 从未触发）。图表统一走 `C`（ChartBrowser）。`action_focus_charts` 方法保留（phase-12 e2e 直调）。
- **A.3 report 看不全**：`#agent-history-detail` 包进 `VerticalScroll`（原 Static + max-height 50% 不可滚动，长 Markdown 截断）。

## code-reviewer 回改

- 🟡 NodeDetail widget 级 `c` 残留 → 已删（接口统一性铁律）。
- 🟡 A.1 docstring 补「默认末条任意 event_type，与 last-message-auto-expand 规则有意区分」。
- 🔵 新增 `test_action_toggle_expand_empty_entries_noop` 显式锁定空 entries 早 return。

## 验证

- `test_widgets.py`：68 passed（含新测试）；`test_app.py`：全绿（116）。
- `e2e_phase12`：2 passed（`action_focus_charts` 直调路径不破）；`e2e_phase13`：8 passed。
- 实测：A.3 VerticalScroll `virtual_size.height=61 > viewport=9`、`max_scroll_y=52`（真滚动非裁剪）。

## 影响范围

`orca/iface/cli/app.py`、`orca/iface/cli/widgets/agent_history.py`、`orca/iface/cli/widgets/node_detail.py`、`tests/iface/cli/test_widgets.py`。canonical Event schema / render layer / orchestrator 不动。

## 后续

phase-16（AgentHistory 单流重构）将在本批次基础上重写 `agent_history.py`：A.1 的 Enter-default-末条逻辑保留，A.3 的 VerticalScroll detail 区将被 inline 流取代。

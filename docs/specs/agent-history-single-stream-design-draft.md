# Agent History 单流重构 Design Draft

> **状态**：design draft（2026-07-07 立项）。只动 AgentHistory 内部渲染，**不改**三块布局 / AgentsList / LogStream / render layer。
> **前置**：[`tui-redesign-v2-design-draft.md`](./tui-redesign-v2-design-draft.md) §2.3（AgentHistory 现状）· [`render-layer-design-draft.md`](./render-layer-design-draft.md) §8（`render_tool` / `render_message` / `render_thinking` 完全复用）
> **目标用户诉求**：① 相邻工具像 Claude Code 一样折叠 ② agent message 清晰可见 ③ 按钮功能正常（看图表 / 展开 agent 输出）④ message 渲染成 markdown ⑤ 不要动画。

---

## §0. TL;DR

当前 AgentHistory 是「**两区**」：上部 `#agent-history-log`（RichLog，每条 entry 一行摘要）+ 下部 `#agent-history-detail`（Static/VerticalScroll，所有展开 entry 的详情叠在一起）。问题：

- 详情不在流里——单独一块、与摘要行分离，长 report 靠 max-height 截断（A 阶段已让它可滚动，但仍是"两块"）。
- 工具事件 = 两条独立 entry（call 一行 + result 一行），全量内容塞进同一个 detail 块 → "工具混杂在一起"。
- message 与 tool 视觉上没区分（都是单行摘要），用户找不到 agent 说了什么。

v3 改造（**单流 inline**，对齐 Claude Code）：

1. **合并两区为一条 RichLog 流**：每条 entry 一行摘要；**展开的 entry 在摘要行下方就地内联详情**（缩进 + `⎿` 引导符），整条流天然可滚动。
2. **工具 call+result 配对成一条 ToolEntry**：默认折叠成一行（`✓ read  path/to/file  3 lines · 0.8s`），Enter 展开内联 tool card（复用 `render_tool`）。
3. **message / thinking 视觉分级**：message 摘要行加粗 + 高亮色（用户最关心），展开内联 `render_message`（Markdown，含代码高亮）；thinking dim italic。
4. **Enter 就地展开光标条**（A 阶段已修无选中默认末条）；`↓/↑` 移光标；`C` 全屏 ChartBrowser 看图表。
5. **零动画**（用户明确不要）。

**不动**：canonical Event schema、phase-15 render layer（`tool_render/`）、AgentsList、LogStream、OrcaApp 三块 compose、`_dispatch_to_widgets` 分桶逻辑。

**复杂度裁决**：~1.5–2 天（design 0.5 + 实现 1 + 测试调整 0.5）。

---

## §1. 背景：两区设计的结构问题

### 1.1 当前渲染路径（v2）

`AgentHistory`（`orca/iface/cli/widgets/agent_history.py`）：

```
compose:
  Static#agent-history-header        # 「── worker · 3 events ──」
  Vertical
    RichLog#agent-history-log        # 每条 entry：摘要行（+ meta 行）
    VerticalScroll#agent-history-detail-wrap   # A 阶段加的可滚动壳
      Static#agent-history-detail    # Group(*expanded details)
```

- `set_node` / `append_event` → 建 `_HistEntry`（seq/type/summary/meta/detail）。
- `_reflow`：clear RichLog → 逐条 `_append_entry_to_log`（写 summary + meta 两行）。
- `_refresh_detail`：把所有 `_expanded_seqs` 里的 entry.detail 打包成 `Group` 写进 detail Static。

### 1.2 三个体感 bug 的根因

| # | 症状 | 根因 |
|---|---|---|
| B1 | "工具混杂在一起" | tool_call + tool_result 是**两条独立 entry**，且展开后它们的 detail Panel **全堆在同一个 detail 块**，与摘要行不在一处 |
| B2 | "agent message 看不清" | message 摘要行与 tool 行视觉同级（都是 plain text）；详情在另一块、被 max-height 截断 |
| B3 | "没法展开看全" | 详情与摘要分离 + 不可滚动（A 已修滚动，但分离仍在） |

---

## §2. 设计：单流 inline + 工具配对

### 2.1 一条 RichLog，entry 内联展开

取消 `#agent-history-detail` 独立区。所有内容写进**一条** `#agent-history-log`：

```
▶▾ 12:34:05  MSG    分析完成，模型是 ConfigurableMLP…        ← 摘要行
  ⎿ <render_message 内联 Markdown，缩进 2 空格>                ← 展开时内联
▶▸ 12:34:06  TOOL → read  tests/e2e_mxint/.../model.py        ← 折叠（一行）
▶▸ 12:34:07  TOOL ← ✓ 3 lines · 0.1s                          ← 配对 result 折叠
  ⎿ <render_tool card 内联，展开时>                            
▶▸ 12:34:08  THINK  先看 model_module…                        ← thinking dim
```

- 摘要行永远在；展开的 entry 紧跟其下内联详情（`⎿` 引导 + 缩进），整流可滚动。
- `Enter` toggle 当前光标条（`▶` 标记）的展开；`↓/↑` 移光标。
- 折叠/展开 = 整流 reflow（clear + rewrite）。**性能**：O(N) per toggle。v1 接受（典型 workflow ≤ 数千 events；mxint ~5000 events 单次 reflow 可接受，<100ms 量级）。虚拟化留 v2（见 §5）。

### 2.2 工具 call+result 配对成 ToolEntry（核心：解决 B1）

`agent_tool_call` 与其 `agent_tool_result`（按 `tool_call_id` 配对）合并成**一条** entry：

- 折叠态（默认）：一行 `✓ <tool>  <_arg_title>  <N> lines · <elapsed>`（result 到达后；result 未到时 `… <tool>  <title>` running 态）。
- 展开态：内联 `render_tool(normalize_tool(call+result))` 的 card（复用 render layer，零改动）。
- 配对逻辑：复用现有 `_tool_call_cache`（已按 `tool_call_id` 缓存 call 的 tool/args/timestamp）。result 到达时合并成 ToolEntry，替换原 call entry（同 seq 位）。

> 这同时消除了「call 一行 + result 一行」的双行噪音——CC 也是一次 tool use 一个块。

### 2.3 message / thinking 视觉分级（解决 B2）

- **message 摘要行**：`MSG` 标签 + **bold + 主题色**（如 `$success` / 蓝），与 tool/thinking 行拉开层级。展开内联 `render_message`（Rich Markdown，代码块自动高亮——render layer 已实现）。
- **thinking 行**：`THINK` + dim italic（折叠态也 dim，区别于 message）。
- **tool 行**：`TOOL` + 中性色，icon `✓/…/✗` 表状态。

### 2.4 按钮功能矩阵（解决 B3 + 用户诉求③）

| 键 | 行为 | 状态 |
|---|---|---|
| `Enter` | toggle 光标条展开（无选中默认末条） | A 阶段已修，B 沿用 |
| `↓`/`↑` | 移光标 | 既有 |
| `j`/`k` | 切 agent（AgentsList） | 既有 |
| `C` | 全屏 ChartBrowser 看图表 | 既有（有 chart 时生效） |
| `a` | 恢复 auto-follow | 既有 |
| `L` | LogStream debug 切换 | 既有 |
| ~~`c`~~ | （A 阶段已移除死键） | — |

---

## §3. 接口变化（最小）

### 3.1 AgentHistory 公开 API（不变）

`set_node` / `append_event` / `set_executor` / `action_*` 签名**零变化**（app.py 调用点不动）。

### 3.2 内部数据结构（变）

- 新增 `_HistEntry.kind` 派生：`"tool"`（call+result 配对）/ `"message"` / `"thinking"` / `"other"`。
- `_entries` 列表：tool call+result 合并后 entry 数 ↓（噪音 ↓）。
- 删 `#agent-history-detail*` DOM 节点 + `_detail_view`；详情内联进 `#agent-history-log`。

### 3.3 render layer（零改动）

`render_tool` / `render_message` / `render_thinking` / `normalize_tool` 原样复用——它们产出的 Panel/Markdown/Text 直接 write 进 RichLog（RichLog 接受任意 Rich renderable）。

---

## §4. 实现切片（~1.5–2 天）

1. **ToolEntry 配对**（0.5d）：`_build_entry_from_event` 改——`agent_tool_result` 到达时反查 cache，把对应 call entry 升级为「已完成 ToolEntry」（合并 result 字段）；`append_event` 维护配对。
2. **单流 inline 渲染**（0.5d）：`_reflow` 重写——逐 entry 写摘要行 + （展开时）内联 detail；删 `_refresh_detail` / detail DOM。`action_toggle_expand` → `_reflow`。
3. **视觉分级**（0.25d）：message bold+色，thinking dim，tool 中性 + status icon。
4. **测试调整**（0.25d）：`test_widgets.py` AgentHistory 用例（~15 个）改断言新渲染模型；`_tui_*_verify.py` 开发脚本同步。

---

## §5. 不做（v2 留）

- **虚拟化**：超长跑（>1 万 events）reflow 卡顿 → 虚拟列表。v1 O(N) reflow 先上。
- **工具分组折叠**：N 个连续 tool 配对一个 "5 tools" 折叠块。v1 先做 call+result 单配对。
- **动画**（用户明确不要）。

---

## §6. 风险

- **reflow 性能**：mxint ~5000 events，每次 Enter 全量 reflow。mitigation：实测 < 300ms 可接受；超了再上 §5 虚拟化。
- **配对乱序**：result 早于 call 到达（防御）。mitigation：result 无 cache 命中时降级为独立 entry（既有兜底）。
- **e2e 回归**：phase-12/13 e2e 若断言 AgentHistory DOM 结构（`#agent-history-detail`）需同步。§4.4 覆盖。

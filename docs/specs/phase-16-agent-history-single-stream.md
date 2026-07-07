# 阶段 16 SPEC —— AgentHistory 单流重构（CC 风格 inline + 工具配对折叠）

> **状态**：SPEC（2026-07-07）。前置 design draft：[`agent-history-single-stream-design-draft.md`](./agent-history-single-stream-design-draft.md)。本文件是契约，逐字实现。
> **前置必读**：[`tui-redesign-v2-design-draft.md`](./tui-redesign-v2-design-draft.md) §2.3（AgentHistory 现状）· [`render-layer-design-draft.md`](./render-layer-design-draft.md) §8（`render_tool` / `render_message` / `render_thinking` 完全复用）· [`phase-12-cli-tui-redesign.md`](./phase-12-cli-tui-redesign.md)（三块布局，**不动**）。
> **范围**：只改 `AgentHistory` 内部渲染。**不动** canonical Event schema、render layer（`tool_render/`）、AgentsList、LogStream、OrcaApp 三块 compose、`_dispatch_to_widgets`。

---

## 0. 阶段目标 + 铁律

### 0.1 目标（用户原话）

跑 mxint 真实 workflow 后体感三个问题，本阶段解决：

1. **相邻工具像 Claude Code 一样折叠**：tool_call + tool_result 配对成一条 entry，默认折叠一行；Enter 展开 card。
2. **agent message 明确出来**：message 摘要行与 tool/thinking 视觉分级；展开内联 Markdown。
3. **按钮功能正常**：每个键位（Enter / ↓↑ / jk / C / a / L）真实生效，可看图表、可展开 agent 输出。
4. **message 渲染成 markdown**（render layer 已支持，本阶段让它「可见」）。
5. **不要动画**。

> **`t` 键现状说明**（目标④衍生）：`action_toggle_thinking` 当前**仅 notify 提示**「按 Enter 展开折叠」，**不**实现 thinking 显隐 toggle（与 `app.py` 现状一致）。本阶段**不动 action 语义**（铁律 #4 接口零变化）；§0.1 目标不含「t 实现 toggle」。

### 0.2 七条铁律（违反即返工）

1. **壳无真相**：AgentHistory 所有内容由 `_dispatch_to_widgets` 注入的事件派生；不订阅 bus、不读 tape（重放/replay 必产相同渲染）。
2. **render layer 零改动**：`normalize_tool` / `render_tool` / `render_message` / `render_thinking` 原样复用。本阶段只改「像素怎么摆」，不改「RenderItem 怎么来」。
3. **依赖单向**：AgentHistory 仅 import `orca.schema` + textual + rich + stdlib + 本包 `_event_summary` / `tool_render`；禁止 `orca.exec` / `orca.run` / `orca.events.bus`。
4. **接口统一性**：AgentHistory 公开 API（`set_node` / `append_event` / `set_executor` / `action_*`）签名**零变化**——OrcaApp 调用点不动。
5. **fail loud**：配对乱序（result 早于 call）—— **测试期**降级即 fail（§5.2 AC：`merged==False` entry 数 == 0）；**生产期**降级记 LogStream warning + 继续渲染。不静默吞。render layer 抛 `NormalizeError` 既有处理保留。
6. **E2E 验收不可替代**（**本阶段核心铁律**，见 §5）：每个按键功能**必须**用 `pilot.press` 真实键位驱动 TUI + 断言**可观测结果**验收。**禁止只用代码 review / 只用直调 `action_*` 的单测冒充验收**。直调单测可保留作快速回归，但**不能作为按钮功能的唯一证据**。
7. **不留兼容路径**：删 `#agent-history-detail*` DOM + `_detail_view` + `_refresh_detail`；新旧渲染模型不并存（接口统一性铁律）。

### 0.3 反模式（必须避免）

- ❌ 用 `widget.action_toggle_expand()` 直调代替 `pilot.press("enter")` 做按钮验收（绕过键位派发，测不到「Enter 是否真绑上、是否被 RichLog 吞」）。
- ❌ 只断言内部 state（`expanded_seqs`）不断言**渲染输出**（用户看到的是像素不是 state）。
- ❌ 为「工具折叠」加新 Event 字段或新 EventKind（真相源是 canonical Event，折叠是渲染派生）。
- ❌ 留 `display:none` 的旧 detail 面板「以防万一」。

### 0.4 与既有契约的关系（零冲突）

- canonical Event schema：**不动**。
- `_dispatch_to_widgets`（app.py）：**不动**（仍按 `event.node` 分桶、仍调 `AgentHistory.append_event`）。
- AgentsList / LogStream / Header：**不动**。
- phase-12/13 e2e：**必须仍绿**（§5.5 回归验收）。

---

## 1. 背景：两区设计的结构问题

当前 AgentHistory（`orca/iface/cli/widgets/agent_history.py`）是「两区」：

```
Static#agent-history-header
Vertical
  RichLog#agent-history-log        # 每条 entry：摘要行 + meta 行
  VerticalScroll#agent-history-detail-wrap   # A 阶段加的可滚动壳
    Static#agent-history-detail    # Group(*expanded details)
```

三个体感 bug 的根因：

| # | 症状 | 根因 |
|---|---|---|
| B1 | 工具混杂在一起 | tool_call + tool_result 是两条独立 entry；展开后 detail Panel 全堆在同一个 detail 块 |
| B2 | message 看不清 | message 摘要行与 tool 行视觉同级；详情在另一块 |
| B3 | 没法展开看全 | 详情与摘要分离（A 阶段已让 detail 可滚动，但「两块」分离仍在） |

---

## 2. 设计：单流 inline + 工具配对

### 2.1 一条 RichLog，entry 内联展开

取消 `#agent-history-detail` 独立区。所有内容写进**一条** `#agent-history-log`：

```
▶▾ 12:34:05  MSG    分析完成，模型是 ConfigurableMLP…        ← 摘要行（message bold+主题色）
  ⎿ <render_message 内联 Markdown，缩进>                       ← 展开时内联
▶▸ 12:34:06  TOOL → read  tests/.../model.py                 ← 折叠（一行，call+result 配对）
▶▸ 12:34:07  TOOL ← ✓ 3 lines · 0.1s                         ← （同一条 entry 的 result 摘要）
  ⎿ <render_tool card 内联，展开时>
▶▸ 12:34:08  THINK  先看 model_module…                       ← thinking dim italic
```

- 摘要行永远在；展开的 entry 紧跟其下内联详情（`⎿` 引导 + 缩进）。
- `Enter` toggle 光标条（`▶`）的展开；`↓/↑` 移光标。
- 折叠/展开 = 整流 reflow（clear + rewrite）。

### 2.2 工具 call+result 配对成一条 ToolEntry（核心：解决 B1）

`agent_tool_call` 与其 `agent_tool_result`（按 `tool_call_id` 配对）合并成**一条** entry：

- 折叠态（默认）：一行 `<status-icon> <tool>  <_arg_title>  <N> lines · <elapsed>`（result 到达后）；result 未到时 `… <tool>  <title>`（running）。
- 展开态：内联 `render_tool(normalize_tool(call+result))` 的 card。
- 配对逻辑：复用既有 `_tool_call_cache`（已按 `tool_call_id` 缓存 call 的 tool/args/timestamp）。result 到达时把对应 call entry **就地升级**——`entries[i] = merged`（保持原 call 的 seq 和列表位置不变，`merged.seq = call.seq`；**不** remove+append，避免 `_selected_seq` dangling 指向已删 entry）。原 call entry **被替换**而非「消失」。

> 消除「call 一行 + result 一行」双行噪音——对齐 Claude Code「一次 tool use 一个块」。

### 2.3 message / thinking 视觉分级（解决 B2）

- **message 摘要行**：`MSG` 标签 + **bold + 主题色**（`$success` 或蓝），与 tool/thinking 行拉开层级。展开内联 `render_message`（Rich Markdown，代码块自动高亮）。
- **thinking 行**：`THINK` + dim italic。
- **tool 行**：`TOOL` + 中性色，状态 icon `✓/…/✗`。

### 2.4 光标 + 展开语义（A 阶段已修，本阶段沿用 + 强化）

- `_selected_seq`：光标条。`↓/↑` 移动；`▶` 标记在摘要行。
- `Enter`：toggle 光标条展开；**无选中时默认作用于最后一条**（A.1 已修，保留）。
- 展开是「整流 reflow」：因为详情现在内联在同一条 RichLog 里，toggle 必须重渲整流（不能只 refresh 一个独立 detail 块了）。

---

## 3. 接口契约

### 3.1 AgentHistory 公开 API（**签名零变化**，铁律 #4）

```python
def set_node(self, name: str | None, events: list[Event]) -> None: ...
def append_event(self, event: Event) -> None: ...
def set_executor(self, executor: str) -> None: ...
def action_cursor_down(self) -> None: ...
def action_cursor_up(self) -> None: ...
def action_toggle_expand(self) -> None: ...
```

OrcaApp 调用点（`_dispatch_to_widgets` / `_on_node_selected` / `action_*` 转发）**不动**。

### 3.2 内部数据结构（变）

- `_HistEntry` 加 `kind: Literal["tool","message","thinking","other"]`（派生自 event_type；tool = 配对后的 call+result）。
- `_entries`：tool call+result 合并后 entry 数 ↓。配对后一条 ToolEntry 同时持有 call 与 result 信息（用于折叠摘要 + 展开 card）。
- **删** `_detail_view` / `_refresh_detail` / `#agent-history-detail*` DOM（铁律 #7）。
- `_reflow` 重写：逐 entry 写「摘要行 + （展开时）内联 detail」到 `#agent-history-log`。

### 3.3 render layer（**零改动**，铁律 #2）

`render_tool(item)` / `render_message(text)` / `render_thinking(text)` 产出的 Rich renderable 直接 `RichLog.write(...)`（RichLog 接受任意 RenderableType）。**不新增 renderer、不改 kinds.py**。

---

## 4. 实现切片

1. **ToolEntry 配对**：`_build_entry_from_event` + `append_event` 改——`agent_tool_call` 建 running ToolEntry 入 `_entries`；`agent_tool_result` 反查 `_tool_call_cache`，把对应 ToolEntry 升级为已完成（合入 result）。无 cache 命中 → 降级独立 entry（兜底）。
2. **单流 inline `_reflow`**：clear `#agent-history-log` → 逐 entry：写摘要行（含 ▶/▾▸ + TYPE-LABEL + 分级样式）；若 `entry.seq in _expanded_seqs` 且 `entry.detail` → 缩进写 `⎿` + detail renderable。
3. **视觉分级**：message bold+主题色；thinking dim italic；tool 中性+status icon。
4. **删旧 detail 区**：compose 去 `VerticalScroll#agent-history-detail-wrap` + `Static#agent-history-detail`；on_mount 去 `_detail_view`；删 `_refresh_detail`；`action_toggle_expand` / `append_event` 改调 `_reflow`。
5. **测试**：§5 全部 E2E 用例 + 既有单测调整。

---

## 5. 验收标准（E2E-first，铁律 #6 —— 本阶段核心）

> **强制要求**：本节每个验收点**必须**有对应的 E2E 测试，用 `pilot.press(<key>)` 真实键位驱动（经 Textual 键位派发 → App BINDINGS → widget action），并断言**可观测结果**。**只有代码 review 通过不算验收；只有直调 `action_*` 的单测通过不算按钮验收**（§5.0 元 AC 强制证伪这一点）。
>
> **可观测结果三件套**（每个按键用例都要有）：
> - **(a) state**：`ah.expanded_seqs` / `ah._selected_seq` / `app._selected_node` / `app.screen_stack` / AgentsList 选中。
> - **(b) 渲染文本**：用 `widget.render_lines(Region)` → `list[Strip]` 提取**确定性**渲染文本（Strip.text / segments），或 `rich.console.Console.capture()` 离线渲染 renderable 取 ANSI。**禁用「SVG 逐字节相等」**（`auto_scroll` / focus ring / 整页渲染含非确定性）。
> - **(c) 视觉 sanity（辅助，非硬性）**：`app.export_screenshot()` SVG 双向子串断言（前不含 X + 后含 X）。
>
> **测试数据**：E2E 必须 replay **真实 tape**：消息/工具用 `runs/mxint_analysis-20260704-105608-90fd22.jsonl`（含 tool_call/result + agent_message + agent_thinking）；图表用同 tape（**已含 5 个 `custom(kind=chart)` 事件**，无需造 fixture）。禁止纯合成事件冒充。

### 5.0 元 AC：pilot.press 真实键位派发证据（铁律级，所有按键用例前置）

每个按键 E2E 用例**必须** monkey-patch 对应 `app.action_*` 记录调用次数，`pilot.press(key)` 后断言 `call_count == 1`。这是「不准直调 `action_*` 冒充验收」（§0.3 反模式）的**唯一可执行证据**——没有它，§5 E2E-first 框架可被静默绕过（实现者在测试里补调 `action_*` 即可骗过 state 断言）。

```python
# 每个按键用例的前置（示例：Enter）
original = app.action_history_toggle_expand
calls = []
def wrapped(*a, **k):
    calls.append(1)
    return original(*a, **k)
app.action_history_toggle_expand = wrapped
await pilot.press("enter")
assert len(calls) == 1, "Enter 未经真实键位派发命中 action（直调冒充？）"
```

> 映射表：`enter`→`action_history_toggle_expand`、`down`→`action_history_cursor_down`、`up`→`action_history_cursor_up`、`j`→`action_agents_next`、`k`→`action_agents_prev`、`C`→`action_open_chart_browser`、`a`→`action_follow_active`、`L`→`action_log_toggle_debug`、`t`→`action_toggle_thinking`。

### 5.1 按键功能 E2E 矩阵（每个按钮都要验）

新增 `tests/e2e_phase16/test_tui_buttons_e2e.py`。每行 = ≥1 个用例，**必须**含 §5.0 元 AC + (a) state + (b) 渲染文本双向断言（前不含 X + 后含 X，关键 fixture 内容子串如已知文件名/工具名/message 关键字）。

| 键 | E2E 步骤 | 断言（state + 双向渲染） |
|---|---|---|
| **Enter**（无选中）| `pilot.press("enter")` | (a) `ah.expanded_seqs` 末条进/出（toggle 两次回原状）；(b) 渲染文本展开后含该条 detail 关键字、收起后不含（**双向**）；(c) 断言 `ah.query_one("#agent-history-detail")` 抛 `NoMatches`（铁律 #7：旧 detail DOM 已删） |
| **Enter**（有选中）| `pilot.press("down")` → `pilot.press("enter")` | (a) 光标条 seq 进/出 `expanded_seqs`；(b) 渲染文本含 ▶ 标记 + 该 entry detail（双向） |
| **↓ / ↑** | `pilot.press("down")` ×3 → `pilot.press("up")` | (a) `ah._selected_seq` 按 entries 序前进/后退；(b) 渲染文本中 ▶ 标记位置随之前移/后移（双向：旧位无 ▶、新位有 ▶） |
| **j / k** | `pilot.press("j")` | (a) `app._selected_node` 变化 + AgentsList 选中行变化；(b) AgentHistory 头部行 agent 名变化（双向：旧名不在、新名在） |
| **C** | `pilot.press("C")`（用含 chart 的 tape）| (a) `app.screen_stack[-1]` 是 `ChartBrowser`；(b) ChartBrowser 渲染文本含图表标题/轴标签关键字（双向：主屏无、browser 有）；Esc → screen pop |
| **a** | `pilot.press("j")`（记下 `_selected_node=X`）→ `pilot.press("a")` | (a) `app._auto_follow == True`；(b) `app._selected_node` **发生变化**（不要求「回到 running」——replay 终态 tape 无 running 节点；回到默认选择逻辑节点即可）+ AgentsList 选中行同步变化（双向） |
| **L** | `pilot.press("L")` | (a) `LogStream.show_debug` flip；(b) LogStream 渲染文本出现 debug 事件行（如 `route_taken`）（双向：前无后有） |
| **t** | `pilot.press("t")` → `pilot.wait_for_idle()` → `pilot.pause(2.5)` | (a) `action_toggle_thinking` 命中（§5.0）；(b) idle 后渲染/notify 含提示文本，pause(2.5) 后不再含（验 notify timeout=2s 生效）。**注**：`t` 仅 notify 提示「按 Enter 展开折叠」，不 toggle 显隐（与 `app.py` 现状一致；本阶段不动 action 语义，铁律 #4） |

> **强制**：上表 9 行（Enter×2 计 2 行）**每行至少 1 个 E2E 用例**，缺一不可；每个用例必须 (§5.0 元) + (a) + (b 双向) 三件齐全。

### 5.2 工具配对 + 折叠验收（解决 B1）

E2E：replay `90fd22`（含多次 tool_call+result）。

- **配对完整性**：replay 后 `ah.entries` 中 `kind=="tool"` entry 数 == tape 中 `agent_tool_call` 数；**且** `kind=="tool" 且 merged==False`（独立兜底 entry）的数量 == 0（强制全部 call+result 配对成功；降级即 fail——§0.2 #5 测试期语义）。
- **折叠默认**：reflow 后渲染文本中每个 tool entry **只一行摘要**（不含 card body，如不含 result 的代码内容）—— 双向断言。
- **展开**：`pilot.press("down")` 选中某 tool entry → `pilot.press("enter")` → 渲染文本含该 tool card（如 `read` 的文件路径关键字、Panel 边框）—— 双向。

### 5.3 message 清晰度 + Markdown 验收（解决 B2 + 诉求④）

E2E：replay 含 agent_message 的 tape（report_painter 的 REPORT 是长 markdown）。

- **视觉分级（强制 Console.capture，非 SVG）**：用 `rich.console.Console.capture()` 离线渲染 message entry 的 renderable，断言 ANSI 含 `\x1b[1m`（bold）+ 主题色码；同样渲染 tool entry 断言**不含** bold。**删除「或 SVG」escape hatch**——SVG 不暴露富文本样式（M10）。
- **展开 markdown**：选中 message → enter → 渲染文本含 REPORT 的代码块关键字（双向：折叠时不含、展开时含）。
- **长 report 可读**：REPORT > viewport 时 `pilot.press("pagedown")` 滚动后渲染文本含后续段落（双向：前后不同段落）。

### 5.4 图表验收（诉求③「可以看图表」）

E2E：replay `90fd22`（**已含 5 个 `custom(kind=chart)` 事件，无需造 fixture**）。chart 事件经 `_dispatch_to_widgets` → `NodeDetail.upsert_chart`（`app.py` 既有路径），ChartBrowser 读 `NodeDetail.all_charts()`。

- `pilot.press("C")` → ChartBrowser 全屏 → 渲染文本含图表标题/轴标签（真实 chart payload 内容，双向）。
- Esc/q → `app.screen_stack` 回主 Screen。
- **前置核实**（实现前）：用 `python3 -c` 验证 `90fd22` 的 5 个 chart 事件能被 `ChartBrowser` 正常加载（防 tape stale / payload schema 漂移）。

### 5.5 回归验收（铁律 #4 接口零变化 + render layer 字节契约 + phase-12/13 不破）

- `tests/iface/cli/test_widgets.py` + `test_app.py`：全绿（既有单测调整后）。
- `tests/e2e_phase12` + `tests/e2e_phase13`：**全绿**（含 `action_focus_charts` 直调路径——方法保留）。
- AgentHistory 公开 API 签名：`inspect.signature` 对比 phase-16 前后逐参相等。
- **render layer 字节契约（H8）**：用 `rich.console.Console.capture()` 离线渲染 `render_tool(<已知 RenderItem>)` 产出的 ANSI，对比 phase-16 前后**逐字节相等**（git diff 干净不够——渲染管线从 `Static.update` 改成 `RichLog.write`，必须验语义未破）。

### 5.6 重放一致性（铁律 #1 —— state 确定性，非 SVG 字节）

- **正序回放**：同一 tape replay 两次（中间切 agent 再切回）→ (a) `ah._entries` 的 `(seq, kind, summary, meta)` 四元组列表逐项相等；(b) `ah.expanded_seqs` 相等；(c) `ah._selected_seq` 相等；(d) `ah.query_one("#agent-history-log").render_lines(<固定 Region>)` 提取的文本逐行相等。
- **乱序回放（reducer fold 性质，Q3=a）**：把 tape events **逆序**灌入 `set_node` → `ah._entries` 的 `(seq, kind, summary)` **集合**与正序回放的集合相等（验证配对/派生是顺序无关的纯函数；若不等 → 配对是顺序敏感的假 fold，铁律 #1 破）。
- SVG 仅作 release note 视觉证据（非硬性 AC）。

---

## 6. 不做（留 v2 / 后续）

- **虚拟化**：>1 万 events reflow 卡顿 → 虚拟列表。本阶段 O(N) reflow 先上（mxint ~5000 events 实测 < 300ms 即可）。
- **多工具分组折叠**：N 个连续 tool 配对一个 "5 tools" 块。本阶段先做 call+result 单配对。
- **动画**（用户明确不要）。

---

## 7. 风险

| 风险 | mitigation |
|---|---|
| reflow 性能（mxint ~5000 events，每次 Enter 全量 reflow）| §5 验收含「Enter 响应 < 300ms」实测断言；超了触发 §6 虚拟化 |
| 配对乱序（result 早于 call）| 既有 `_tool_call_cache` 兜底：无命中 → 独立 entry，不抛（fail-loud 体现在日志 warning）|
| SVG 断言脆弱（text 元素结构随 textual 版本变 / `auto_scroll` 非确定性）| **禁用 SVG 逐字节相等**；硬性 AC 用 state 四元组 + `render_lines(Region)` 确定性文本 + `Console.capture` ANSI；SVG 仅作双向子串 + release note 视觉证据，不作唯一证据 |
| `RichLog.lines` API 不存在（reviewer 误报）| 用 `widget.render_lines(Region) -> list[Strip]`（textual 8.2.8 实测可用）提取确定性渲染文本；勿用不存在的 `RichLog.lines` |
| phase-12/13 e2e 断言旧 detail DOM | §5.5 回归门；若有断言 `#agent-history-detail` 的，随本阶段删同步改 |

---

## 8. 验收清单（提交前逐项打勾）

- [ ] §5.0 元 AC：每个按键用例 monkey-patch `action_*` 断言 `call_count==1`（pilot.press 真派发证据）
- [ ] §5.1 九行按键矩阵各 ≥1 个 E2E 用例（元 AC + state + 双向渲染文本）
- [ ] §5.2 工具配对/折叠 E2E（含 `merged==False` 数==0）
- [ ] §5.3 message 分级（Console.capture ANSI 强制）+ markdown E2E
- [ ] §5.4 图表 E2E（用 `90fd22` 真 chart tape，双向断言）
- [ ] §5.5 回归全绿 + API 签名 `inspect.signature` + render layer 字节契约（Console.capture ANSI）
- [ ] §5.6 重放一致性（state 四元组 + render_lines 文本 + **乱序回放集合相等**）
- [ ] render layer（`tool_render/`）零改动（git diff + 字节契约双证）
- [ ] 删 `#agent-history-detail*` + `_refresh_detail`（`query_one` 抛 NoMatches 验证）
- [ ] release note + CHANGELOG + CURRENT.md 更新

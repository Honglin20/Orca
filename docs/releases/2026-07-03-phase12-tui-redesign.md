# Release Note —— phase 12 CLI TUI 重设计（拓扑图 + NodeDetail + 终端图表渲染）

**日期**：2026-07-03
**分支**：`phase12-tui-redesign`
**计划**：[`docs/plans/2026-07-03-phase12-tui-redesign.md`](../plans/2026-07-03-phase12-tui-redesign.md)（S0–S9）
**SPEC**：[`docs/specs/phase-12-cli-tui-redesign.md`](../specs/phase-12-cli-tui-redesign.md)（v2，对抗审闭环）

## 做了什么

重设计 CLI TUI 三个面板，治 phase-7 三个实测痛点（左图太宽且非拓扑图、右上 ActiveNode 常空白、图表 TUI 完全不可见）：

| 区域 | 改动 | SPEC § |
|---|---|---|
| 左 | `DagTree`（列表）→ `DagGraph`（拓扑图，分层 + 连边，max-width 33%） | §1.1 |
| 右上 | `ActiveNode`（agent 专属常空）→ `NodeDetail`（流式/输出/图表 tab，6 kind 永不空白） | §1.3 |
| 图表 | 新增 `ChartPanel`（确定性 fold 投影）+ `ChartCanvas`（plotext braille 渲染） | §1.2 §2.2 |
| 全屏 | 新增 `ChartBrowser`（ModalScreen，`C` 跨节点浏览，`__workflow__` 顶层） | §4.5 |

**核心 invariant（全部有结构证据 + 显式单测守护）**：
1. **6 新文件零后端 import**（不 import `orca.exec`/`orca.run`/`orca.iface.mcp`/chart-producer）—— `TestZeroBackendImport` grep 断言。
2. **壳无真相**：widget 只持渲染投影，由 `_dispatch_to_widgets` 单路径注入；不订阅 bus/读 tape/解析 Event。
3. **确定性 fold**：ChartPanel 投影 = `custom(chart)` 事件的确定性 fold—— `test_deterministic_fold_clear_replay_equal`（清空→重放→一致）。
4. **`_selected_node`/`_auto_follow` 临时 UI 态不写 tape**—— `test_selected_node_and_auto_follow_not_in_tape`。
5. **DagLayout 可替换（OCP）**：`DagLayout` Protocol + `LayeredDagLayout`（默认）/ `CompactOutlineLayout`（fallback）双策略，换布局不动 widget/dispatch。
6. **executor-agnostic 流式**：N 个 `agent_*` 事件 → N 行（不预设 thinking/message 齐备；claude/opencode 都过）。
7. **fail loud**：未知 chart_type 显式提示；cycle 抛 `CycleDetected`（含环路径）；残缺 payload 跳过 + warning。

## 实施步骤（S0–S9，逐字实现计划）

- **S0**：`pyproject.toml` 加 `textual-plotext>=1.0.1`（主依赖）；6 文件骨架。
- **S1（P0 spike）**：`dag_layout.py`—— `LayoutIR`/`NodeBox`/`Edge` dataclass + `DagLayout` Protocol + `LayeredDagLayout`（最长路径分层）+ `CompactOutlineLayout` + `build_topology(wf)`（含环检测）。**spike 全过**：100 seeded 随机拓扑不抛、layers 含全部 node 恰一次、宽度治理、cycle fail loud。**LayeredDagLayout 过 spike，无需 fallback 到 CompactOutline**（无 ADR 9）。
- **S2**：`dag_graph.py`—— DagGraph widget，render 委托 DagLayout，j/k + click select，CSS width:32 max 33%。
- **S3**：`chart_canvas.py`—— plotext import 探测缓存；line/bar/area/scatter/pareto → braille（`marker="braille"`，plotext 5 `filly` for area）；table → DataTable；radar → 降级 +「见 Web」；未知 fail loud。
- **S4**：`chart_panel.py`—— 确定性 fold 投影 `node->label->title->payload`；幂等 upsert；`all_charts()` 公共 API（`__workflow__` 顶层）。
- **S5**：`node_detail.py`—— TabbedContent（流式/输出/图表）；6 kind 派发；● 徽标（`Tab.Activated` 清除）；executor-agnostic。
- **S6**：`app.py` surgical 接线—— `_selected_node`/`_auto_follow`/`_node_kinds`；compose 新 widget；CSS 3fr/2fr；chart dispatch 分支（`node=None`→`__workflow__`）；键位 `a`/`c`/`C`/`/`。
- **S7**：`chart_browser.py`—— ModalScreen，ListView + ChartCanvas 预览，数据源 = `NodeDetail.all_charts()`。
- **S8**：删 `dag_tree.py`/`active_node.py`；迁移 `test_widgets.py`/`test_app.py` 到 DagGraph/NodeDetail；新增 `test_dag_layout.py`（spike）+ ChartPanel/ChartCanvas/NodeDetail/ChartBrowser 单测 + `TestZeroBackendImport` grep 断言。
- **S9**：`code-reviewer` 自审 → 修全部反馈（见下）→ 提交。

## 与计划的偏差

1. **plotext 包名**：计划写 `textual-plotext-ext`（PyPI 无此包）；实际包名 `textual-plotext`（依赖 `plotext`）。SPEC §1.2 的「plotext 主依赖」决策不变。
2. **plotext 5 API**：area 类型用 `filly="up"`（非 `fill="up"`，后者仅 bar 支持）。
3. **Spike 未触发 fallback**：LayeredDagLayout 全过 spike 硬断言，无需切 CompactOutlineLayout 为默认（无 ADR 9）。
4. **连边绘制简化**：`_render_layered_lines` 画相邻层间 `│` 竖线连边（非完整 Sugiyama `┐┌└┴` 多对多路由）—— S1 spike 范围是「过断言、可读、不崩」，SPEC §6.2 明示「截图仅人类 sanity 不作 pass/fail」。完整连边路由留后续视觉打磨。

## code-reviewer 反馈处理

🔴 blocker（1 项，全修）：
- ChartBrowser `ListItem` 未导入 → NameError：补 import + 补 ChartBrowser 单测（`test_browser_lists_charts_with_workflow_on_top`）。

🟡 major（6 项，全修）：
- Kahn 环检测重复两份 → 抽 `detect_cycle(edges, nodes)` 纯函数共用（DRY）。
- status icon 映射重复 → `dag_layout` 改 import `_icons.NODE_STATUS_ICONS`（单真相源）。
- NodeDetail 8 处 `except Exception: pass` → 收窄为 `except NoMatches`；`action_focus_charts` 失败显式 log warning（fail loud）。
- `_render_layered_lines` 未画连边 → 补相邻层间 `│` 竖线连边。
- `DagGraph._assert_acyclic` 抛 cycle 不带路径 → 复用 `detect_cycle` 返回环路径传入 `CycleDetected`。
- `chart_panel` 跳过缺 label/title → 注释说明依据（types.ts 必填 + §2.7 替换语义）。

🟢 minor（2 项，已修）：
- `ChartCanvas` 暴露 `last_rendered` 公共属性，ChartPanel 不再扒 `_Static__content` 私有。
- braille 测试改用 `canvas.last_rendered` 断言。

## 验证结果

- **CLI 测试**：`pytest tests/iface/cli/` → **268 passed, 7 skipped**（7 skipped 全是需 claude CLI + API key 的集成测试）。
- **全量测试**：`pytest tests/` → **1133 passed, 30 skipped, 0 failed**（含 S10 e2e 2 项；基线 1082 → 1133，净增 51 测试，**0 回归**）。
- **6 文件零后端 import grep**：CLEAN（`TestZeroBackendImport` 守护）。
- **headless SVG 截图**：`docs/assets/phase12_demo_parallel.svg`（含拓扑节点 + 状态图标 + 流式/输出/图表 tab + line chart braille 渲染）。
- **DagLayout spike**：LayeredDagLayout 过全部硬断言（100 seeded 随机拓扑 + 4 边界），**未 fallback**。

## 文件清单

**新增**（6 + 1 screen + 1 test）：
- `orca/iface/cli/widgets/dag_layout.py` / `dag_graph.py` / `node_detail.py` / `chart_panel.py` / `chart_canvas.py`
- `orca/iface/cli/screens/chart_browser.py`
- `tests/iface/cli/test_dag_layout.py`

**修改**：
- `orca/iface/cli/app.py`（dispatch + compose + 键位 + CSS + `_selected_node`/`_auto_follow`/`_node_kinds`）
- `orca/iface/cli/widgets/__init__.py`（导出新 widget）
- `pyproject.toml`（加 `textual-plotext`）+ `uv.lock`
- `tests/iface/cli/test_widgets.py`（迁移 + 新增 ChartPanel/ChartCanvas/NodeDetail/ChartBrowser 测试）
- `tests/iface/cli/test_app.py`（迁移 + chart dispatch / 临时态 / zero-backend-import 测试）

**删除**：
- `orca/iface/cli/widgets/dag_tree.py` / `active_node.py`

## S10 —— opencode 后端 e2e 验收（test-coverage-e2e）

**真跑通过**：opencode 后端（`opencode_translator`、`glm-4.6v`、events 模式、argv 通道）驱动 TUI 端到端 —— `tests/e2e_phase12/test_opencode_e2e.py` **2 passed**（真 `opencode run` 子进程，真回答落 tape，如 `REPORT: Terminal charts visualize data in CLI without GUI overhead.`）。

- **SPEC §6 逐项过**（有断言证据）：DagGraph 拓扑+状态图标、`j/k` 选中 pin（`_auto_follow=False`、NodeDetail 跟随）、NodeDetail 流式 N 行（**opencode 无 `agent_thinking` 也过** = executor-agnostic 铁证）、输出 tab、`c` 聚焦图表 tab、ChartBrowser `__workflow__` 顶层。
- **图表渲染**（goal「render_chart 渲染 + 多副图规整」）：`render_chart` **生产者尚不存在**（phase-10 deferred；`examples/agents/report_painter.md:2` 明示「render_chart 在 Orca CLI 不可用，故全部用 markdown 表格替代」）。按 SPEC §0.3/§6.1 解耦契约——运行中向 `app.bus` emit 合法 `custom(kind=chart)` 事件（ChartPayload 取自 `types.ts`）→ 走 ChartPanel/ChartCanvas **真渲染路径**：line chart **braille**（SVG 含 34 个 braille 字符，非降级）、2 label×2 title 分组规整、同 label+title **替换不堆积**、`node=None`→`__workflow__` 桶。**报告未声称生产者跑过**——只证渲染侧。
- **headless SVG**：`docs/assets/phase12_opencode_e2e.svg`（拓扑节点 + 真回答 + braille 图 + training/eval 分组）。

### S10 顺带修的真 bug（surgical，e2e 的硬 blocker）

`ClaudeExecutor._build_spawn_config` 无条件注入 `--allowed-tools`/`--mcp-config` → opencode（`capabilities.mcp_tools=False`）不认这俩 flag → yargs dump `--help` exit 1 → 每个 opencode agent node spawn 即 `CliExitNonZero`。**不修则 opencode 根本跑不起来**。修：注入 gate 到 `profile.capabilities.mcp_tools`（`orca/exec/claude/executor.py` ~6 行；capability 契约本就为此声明）。`agent_tools_server` 仍透传（session 路由不变），仅压掉不支持的 flag。回归：`tests/iface/cli/ tests/profiles/` 345 passed + `tests/exec/...` 100 passed，**0 回归**；全量 1133 passed。

### 已知外部 gap（不阻塞 phase-12）

`render_chart` MCP 工具（chart 生产者）未实现（phase-10）。TUI 渲染侧已就绪 + 验证（见上）；待生产者落地后，可补一次「agent 真调 render_chart → 图自动出」的回归（无需改 TUI，解耦铁律保证）。

## Commit SHA

- `38fd78c` —— S0-S9 主实现（分支 `phase12-tui-redesign`）
- `cd6c1ee` —— opencode spawn capability-guard fix（S10 的硬 blocker）
- `81d2f93` —— S10 opencode e2e + snapshot + docs

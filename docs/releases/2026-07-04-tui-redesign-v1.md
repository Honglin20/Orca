# 2026-07-04 —— TUI 重设计 v1（spec v1.1 全 P0 闭环）

> 关联：tui-redesign-draft.md v1.1（spec-review-adversarial conditional-pass → 5 P0 全闭环）
> 分支：`phase13-render-chart`
> 验证：1380 passed (test/iface/cli 全过 + 全 unit/e2e_phase12-13-15 0 回归)，1 flaky (test_demo_integration 单独跑过)

## 改动总览

TUI 整体重设计 v1，对齐 spec v1.1 字段级 acceptance criteria：

| 改造点 | spec § | 落地 |
|---|---|---|
| DAG 节点 3 行盒子（name / status+iter / elapsed+tok 或 error） | §4.4 | `_dag_render.box_render` |
| fan-in `(N inputs · M/N arrived)` 副标（N 静态拓扑入边数 + M 动态 arrived） | §4.5 O2=a | `_dag_render.fan_in_annotation` + DagGraph fan_in_total 静态派生 + app 维护 arrived |
| `after=None` 单独 section（旁支节点 + 末端汇聚标注） | §4.6 O3=b | `_dag_render.split_main_and_after_none` + `render_after_none_section` |
| 同层并行 ≥ 5 切 outline fallback | §4.3 | `_dag_render.should_fallback_to_outline`（FALLBACK_PARALLEL_THRESHOLD=5） |
| iter 号 reducer fold（重放产相同 iter；retry / skip / interrupt 不算） | §4.4.1 | OrcaApp `_node_session_ids` reducer |
| Activity Stream 双行 entry + 折叠详情 | §5.3 / §5.5 | `activity_stream.ActivityStream` |
| per-type entry 结构（32 EventType 字段级映射） | §5.4 | `activity_stream._build_summary_line` / `_build_meta_line` / `_build_detail_renderable` |
| 取消 NodeDetail 显示（O1=c，保留实例兼容）+ `f` 键 filter 模式 | §5.1 / §7.2 | OrcaApp.compose + `action_toggle_filter` + CSS height:0 |
| EVENT_VISIBILITY 集中表（7 tag：show/show_dim/show_compact/show_warn/show_error/hide_main/hide_all） | §6.4 | `_event_filter.EVENT_VISIBILITY` |
| prompt_rendered hide_all（仅 tape） | §6.1 | `EVENT_VISIBILITY["prompt_rendered"] == "hide_all"` |
| agent_usage hide_main（收敛 Header footer） | §6.2 | Header `per_node_usage` + `running_node` 优先排序 |
| Header footer 横向滚动 + filter 标签 | §7.2 | `HeaderStats.render_footer_text` |
| DagGraph 占 50%（CSS `width: 1fr`） | §7.2 | dag_graph.DEFAULT_CSS |

## 关键决策（spec §12 + §13 落实）

- **O1=c**：取消 NodeDetail 显示，但保留 widget 实例（CSS height:0 + offset 隐藏）兼容
  既有 e2e_phase12 测试（断言 `nd.active_tab == "charts"`）；用户用 `f` 键过滤"仅选中节点"。
- **O2=a**：fan-in N 静态（拓扑入边数，`build_from_workflow` 时一次性算）+ 副标 M 动态（
  `node_completed` 时按 dst 节点累加）。
- **O3=b**：`after=None` 节点（拓扑入度=0 但非 entry）单独 section，文字标注汇聚目标。

## 单向依赖（spec §8.2）

新模块只 import stdlib + 同包 widgets：
- `_event_filter.py`：纯数据（零 orca import）
- `_dag_render.py`：仅 `_icons`（同包）+ stdlib
- `activity_stream.py`：`_event_filter`（同包）+ `tool_render`（同包）+ `orca.schema.RenderItem`

无 `orca.exec` / `orca.run` / `orca.events.bus` 反向依赖。

## iter fold 性质（spec §4.4.1）

reducer 维护 `node_session_ids: dict[node, list[session_id]]`：
- 新 session_id 触发 append → iter = `session_list.index(sid) + 1`
- retry / skip / interrupt 不 append（同 session_id 沿用）
- loop workflow 重入：每次 node_started 携新 session_id → iter +1
- 重放同 tape 必产相同 iter 列表（fold 性质，已单测）

`_selected_node` / `_auto_follow` 是 UI 交互态（重启清零，不进 tape，与 fold 严格区分）。

## 测试覆盖

新增 2 个测试文件（60 测试）：
- `tests/iface/cli/test_event_visibility.py`：12 测试（完整性 + 派发语义 + noise governance 锁定）
- `tests/iface/cli/test_tui_redesign.py`：36 测试（box_render / iter fold / fan_in / after_none / fallback / Activity Stream / Header footer / DagGraph projection E2E）

修改 1 个测试：
- `tests/iface/cli/test_app.py::test_app_dispatches_prompt_rendered_to_logstream`：
  spec §6.1 反转决议（prompt_rendered TUI 完全不显示），改为反向断言。

## 真 TUI 截屏

重放 mxint tape（`runs/mxint_analysis-20260704-105608-90fd22.jsonl`，186 events）：
- Activity Stream 收到 152 events（filter 掉 17 prompt_rendered + 17 agent_usage + 其他 = 152）
- SVG 截屏：`tests/iface/cli/_artifacts/tui_v1_replay.svg`（59KB）
- 脚本：`tests/iface/cli/_tui_replay_shot.py`

## 不回归证据

- `pytest tests/ -q --ignore=tests/e2e_mxint --ignore=tests/e2e_phase14`：1380 passed, 1 flaky
- `orca validate examples/mxint_analysis.yaml`：PASS
- 既有 dag_layout / dag_graph / widgets / app 测试 0 回归

## 不动契约

- ✅ canonical Event schema（零改动）
- ✅ phase-15 render layer 契约（`tool_render/*` 公共 API 不变，仅消费）
- ✅ DagLayout 算法（`LayeredDagLayout` / `CompactOutlineLayout` 算法不动，只升级 box 渲染）
- ✅ translator / orchestrator / gate / chart 链路不动

## Commit

按 logical chunk 单 commit（14 文件，atomic 改造）：`7bd43ef`
- `orca/iface/cli/widgets/_event_filter.py`（新）
- `orca/iface/cli/widgets/_dag_render.py`（新）
- `orca/iface/cli/widgets/activity_stream.py`（新）
- `orca/iface/cli/widgets/dag_graph.py`（改：update_node_projection API + 3 行盒子渲染）
- `orca/iface/cli/widgets/header.py`（改：footer 区 + per-node usage + filter 标签）
- `orca/iface/cli/app.py`（改：compose + dispatch + f filter + iter reducer）
- `tests/iface/cli/test_event_visibility.py`（新）
- `tests/iface/cli/test_tui_redesign.py`（新）
- `tests/iface/cli/test_app.py`（改：prompt_rendered 反向断言）
- `tests/iface/cli/_tui_replay_shot.py`（新，截屏脚本）
- `tests/iface/cli/_artifacts/tui_v1_replay.svg`（新，截屏产物）
- `docs/status/CURRENT.md`（更新）
- `docs/status/CHANGELOG.md`（更新）
- `docs/releases/2026-07-04-tui-redesign-v1.md`（本文）

## v2 路线（不在本 commit 范围）

- DAG 节点 hover tooltip（spec §13.7 v2 评估）
- Activity Stream 流式 markdown shiki 增量高亮（render layer v2）
- live timer 走 wall clock（spec §4.4：「不进 tape」UI 交互态，v1 跳过）
- Activity Stream 全局 thinking 可见性切换（v1 默认 show_dim，per-entry Tab 折叠）

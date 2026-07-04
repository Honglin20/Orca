# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前状态：TUI 重设计 v1 完成；无进行中任务

**TUI 重设计 v1 完成**（commit `7bd43ef`）
- 对齐 spec v1.1（spec-review-adversarial conditional-pass → 5 P0 + 3 用户决策闭环）
- DAG 3 行盒子（name / status+iter / elapsed+tok 或 error）+ fan-in `(N inputs · M/N arrived)`
  副标 + `after=None` 单独 section + ≥5 并行 outline fallback
- Activity Stream 双行 entry + 折叠详情（32 EventType per-type 字段级映射，复用 phase-15
  `render_tool` / `render_message` / `render_thinking`）
- 取消 NodeDetail 显示（O1=c，保留实例兼容 e2e_phase12 测试）+ `f` 键 filter 模式
- EVENT_VISIBILITY 7 tag 全 32 EventType 覆盖（prompt_rendered hide_all / agent_usage
  hide_main 收敛 Header footer）
- reducer 派生 fold：iter 号 `node_session_ids`（重放产相同 iter，retry/skip/interrupt 不算）；
  fan_in arrived（dst 节点 node_completed 累加）
- **1380 passed 0 回归**（baseline 1333 + 47 新测试），mxint 真跑 tape 重放 SVG 截屏
  （186 events → 152 进 Activity Stream，filter 掉 17 prompt_rendered + 17 agent_usage）

## 与并行进程的边界
- 本次 commit 只动：`orca/iface/cli/widgets/{_event_filter,_dag_render,activity_stream,
  dag_graph,header}.py` + `orca/iface/cli/app.py` + `tests/iface/cli/{test_event_visibility,
  test_tui_redesign,test_app,_tui_replay_shot}.py` + `tests/iface/cli/_artifacts/tui_v1_replay.svg`
  + `docs/status/{CURRENT,CHANGELOG}.md` + `docs/releases/2026-07-04-tui-redesign-v1.md`。
- 留工作树（并行进程持有）：`profiles/builtin/*` + `terminal.py` + `gates/dialog.py`
  + `exec/validator.py` + `executor_cmds.py` + `config.py` + `iface/cli/widgets/tool_render/
  normalize.py` + `run/orchestrator.py` + `run/router.py` + 它们测试
  + `examples/demo_task.yaml` + `pyproject.toml` + `uv.lock`
  + `tests/e2e_phase{13,14}/_artifacts/*.jsonl`（_tape）+ `_tui.svg`。

## 已知 follow-up（v2 路线，不阻塞本任务）
- live timer 走 wall clock（spec §4.4：「不进 tape」UI 交互态，v1 跳过——node_completed 后
  elapsed 静态从 data.elapsed 读，running 时 v1 不显动态秒数）
- DAG 节点 hover tooltip（spec §13.7 v2 评估——Textual Static 不原生支持 hover）
- Activity Stream 流式 markdown shiki 增量高亮（render layer v2）
- 全局 thinking 可见性切换（v1 默认 show_dim，per-entry Tab 折叠；后续 ActivityStream.toggle_thinking_visible）
- 双写 LogStream/NodeDetail 兼容路径在 v2 移除（spec §5 决议 LogStream → ActivityStream 是替换）

## 待办（等用户指示方向）
1. phase-12 / 13 / 14 / 15 / TUI 重设计 v1 分支 merge / PR（分支 `phase13-render-chart`）。
2. **批 2（phase-16）**：轻量本地包分发（多 pool + `name@source`）+ workspace-instruction。
3. code-reviewer M2/M3（resolve_flags setdefault 文档交叉引用 + stacklevel 指向）+ N3。
4. **render layer v1.5**：codex 接入（apply_patch 解析 + shell/read_file 映射）。
5. **render layer v2**：Web 端 TS 镜像 + 流式 shiki 增量高亮 + 千行 diff 虚拟化。
6. **background chart gap**（mxint follow-up）：让 `--background` 模式 chart 可用。

## 必读文件（下一任务开工前按需）
- [`docs/releases/2026-07-04-tui-redesign-v1.md`](../releases/2026-07-04-tui-redesign-v1.md)（TUI 重设计 v1 全貌）
- [`docs/specs/tui-redesign-draft.md`](../specs/tui-redesign-draft.md)（v1.1 spec 全文）
- [`docs/releases/2026-07-04-mxint-real-bitx.md`](../releases/2026-07-04-mxint-real-bitx.md)（mxint 真实 bitx 全貌）
- [`docs/releases/2026-07-04-render-layer-v1.md`](../releases/2026-07-04-render-layer-v1.md)（phase-15 v1 全貌）+ [`docs/specs/render-layer-design-draft.md`](../specs/render-layer-design-draft.md) §3/§5/§6/§8/§12
- [`orca/iface/cli/widgets/`](../../orca/iface/cli/widgets/)（_event_filter / _dag_render / activity_stream / dag_graph / dag_layout / header 实现）

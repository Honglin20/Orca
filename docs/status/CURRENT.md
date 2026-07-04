# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前状态：phase-15 render layer v1（TUI 端）完成；无进行中任务

**phase-15 render layer v1 完成**（commit `ae0126b` + `edd738f`）
- 实现 render-layer-design-draft §11.1 v1：在 canonical Event 之上加 iface 层纯函数渲染抽象
  （`normalize_tool` → RenderItem → `render_tool` → Rich renderable）。
- 新增 `orca/schema/render_item.py`（RenderItem + RenderToolKind + ToolStatus，§5）
- 新增 `orca/iface/cli/widgets/tool_render/`（normalize/kinds/registry/reduce，单向依赖 only schema+rich+stdlib，§7.1）
- 测试：`tests/e2e_phase15/_artifacts/render_tool_cases.json` 11 case fixtures + `tests/iface/cli/test_tool_render.py` 32 test（snapshot + fail loud + reducer + claude-code 对齐 acceptance §14.1 + DRY 守卫 + registry 派发）
- 迁移：log_stream 工具事件摘要共享 `describe_tool_event`（DRY，行为字面不变）；
  node_detail 流式 tab 工具事件升级为 Rich tool card（opencode read 目录现渲染为 17 条目树，
  不再 XML 一坨）；thinking dim+italic 纯文本 + `t` 键切可见性（§12.8）
- **验证**：1327 passed 0 回归（baseline 1276）；`orca validate examples/demo_task.yaml` ✓；
  真实 tape 工具卡片渲染正确（runs/demo_task-20260703-221337-c94151.jsonl → 17 条目树）

**phase-14 Agent 一等化 + Route 输出变换（批 1）完成**（commit `74d65b3` + backfill `7befc2c` + M1 fixup `956015f`）
- agent 一等化（agent 池 + 文件夹化 + 统一解析层 `AgentResolver`）+ Route.output 终点输出变换 + MCP list_agents/get_agent。
- 走完整流程：SPEC → spec-review-adversarial 对抗审（闭环 2 P0 + 5 P1）→ 实现 11 文件 → code-reviewer 审通过（可合并，无 blocker/major）→ opencode+deepseek-v4-flash 真跑 e2e（E2E-1 agent 引用 + E2E-2 文件夹化 resources `$ORCA_AGENT_RESOURCES`）→ commit。**1276 passed 0 回归**。

**examples 整理完成**（commit `c5c13b1`）
- 13 agent example 固化 `executor: opencode` + `model: "deepseek/deepseek-v4-flash"`（with_ask_user 保留 claude——ask_user 需 mcp_tools）。
- description 补全（21 example，TUI 信息明确）+ examples/README.md 分类（纯 script / agent workflow / claude-only 例外）。
- 新建 render_chart example：文件夹化 agent plotter（agent.md + scripts/chart_demo.py 资源，演示 phase-14 `ORCA_AGENT_RESOURCES` + phase-13 chart 链路）。
- parallel_research 迁移到 phase-14 `agent: <name>` 显式引用（消除旧约定 deprecation warn）。
- **验证**：8 script + 13 agent + render_chart 全跑通（opencode+deepseek-v4-flash 真跑，**不 mock**）；with_ask_user 例外（claude-only，需 ANTHROPIC_API_KEY）。

## 与并行进程的边界
- phase-15 commit 只动：`schema/render_item.py` + `schema/__init__.py` + `widgets/tool_render/*` + `widgets/{log_stream,node_detail}.py` + `iface/cli/app.py` + `tests/e2e_phase15/` + `tests/iface/cli/test_tool_render.py` + `tests/e2e_phase13/test_e2e_6_opencode_deepseek.py`（一处 _stream_lines join fix）。
- 留工作树（并行进程持有）：`profiles/builtin/*` + `terminal.py` + `gates/dialog.py` + `exec/validator.py` + `executor_cmds.py` + `config.py` + `tests/e2e_mxint/` + 它们的测试 + `examples/demo_task.yaml`。

## 待办（等用户指示方向）
1. phase-12 / 13 / 14 / 15 分支 merge / PR（分支 `phase13-render-chart`）。
2. **批 2（phase-16）**：轻量本地包分发（多 pool + `name@source`）+ workspace-instruction（SPEC 已预留 `AgentResolver` 接口 + `ResolveContext.extra_roots`）。
3. code-reviewer M2/M3（resolve_flags setdefault 文档交叉引用 + stacklevel 指向）+ N3（tape artifact 含开发机路径，可 sanitize）—— minor/nit，下个 commit 顺手。
4. **render layer v1.5**：codex 接入（apply_patch 解析 + shell/read_file 映射，验证 renderer 零改动 = backend 隔离）。
5. **render layer v2**：Web 端 TS 镜像（types/render_item.ts + tools/normalize.ts + tools/kinds.tsx + tools/registry.ts，照 spec §5/§6/§8 实现并跑通相同 fixtures）+ 流式 shiki 增量高亮 + 千行 diff 虚拟化 + 复制按钮。

## 必读文件（下一任务开工前按需）
- [`docs/releases/2026-07-04-render-layer-v1.md`](../releases/2026-07-04-render-layer-v1.md)（phase-15 全貌 + 模块布局 + 渲染意图 + 测试 + 自验证 + v1 外非目标）
- [`docs/specs/render-layer-design-draft.md`](../specs/render-layer-design-draft.md) §3 四层架构 + §5 RenderItem 契约 + §6 映射表 + §8 渲染意图 + §12 裁决记录
- [`orca/schema/render_item.py`](../../orca/schema/render_item.py)（RenderItem/RenderToolKind/ToolStatus 契约）
- [`orca/iface/cli/widgets/tool_render/`](../../orca/iface/cli/widgets/tool_render/)（normalize/kinds/registry/reduce 实现）
- [`docs/releases/2026-07-03-phase14-agent-first-class.md`](../releases/2026-07-03-phase14-agent-first-class.md)（phase-14 全貌 + 物化时序修正 + executor capability guard 修复 + 与并行进程边界）

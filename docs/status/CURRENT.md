# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前状态：phase-15 render layer v1（TUI 端）完成；无进行中任务

**phase-15 render layer v1 e2e gaps 闭环完成**（commit 见 CHANGELOG）
- GAP#1（P1）：opencode `read` **文件** XML envelope 未解析（与目录同形）→ 抽统一
  `_parse_opencode_xml_envelope` helper（DRY）+ `_strip_opencode_file_content` 剥三层
  修饰（envelope 起手换行 + opencode 自带 `N:` 行号前缀 + EOF marker）+ 仅 `<path>`
  起手式才尝试 XML 解析（避免 claude Read 普通 HTML/XML 文件误判）+ fail visible
  （解析失败/未知 type/缺字段 → warning + 降级原文，§13）
- GAP#2（P2）：`_make_subtitle` 加 `file_write` 分支 → `new, NB`（spec §8.1）
- spec §6.3 同步订正 + 5 新增测试（剥 envelope / 行数守恒 / 真实 tape 72 行回归 /
  file_write subtitle × 2）
- **验证**：1333 passed 0 回归（baseline 1327 + 6 新增）；真跑 tape seq=5 渲染干净
  72 行 TOML（SVG `/tmp/gap1_opencode_read_file.svg`）

**phase-15 render layer v1 完成**（commit `ae0126b` + `edd738f`；详情见 release note）
- 实现 §11.1 v1：`normalize_tool` → RenderItem → `render_tool` → Rich renderable；
  新增 `schema/render_item.py` + `widgets/tool_render/{normalize,kinds,registry,reduce}.py`
  + 11 case fixtures + 32 test；log_stream/node_detail 工具事件共享 `describe_tool_event`（DRY）；
  node_detail 流式 tab 工具事件升级为 Rich tool card；thinking dim+italic + `t` 切可见性。
- **验证**：1327 passed 0 回归（baseline 1276）；真实 tape 工具卡片渲染正确。

## 与并行进程的边界
- phase-15 / 本 gap 修复 commit 只动：`schema/render_item.py` + `schema/__init__.py`
  + `widgets/tool_render/*` + `widgets/{log_stream,node_detail}.py` + `iface/cli/app.py`
  + `tests/e2e_phase15/` + `tests/iface/cli/test_tool_render.py`
  + `tests/e2e_phase13/test_e2e_6_opencode_deepseek.py`（_stream_lines join fix）
  + `docs/specs/render-layer-design-draft.md`（spec §6.3 订正）。
- 留工作树（并行进程持有）：`profiles/builtin/*` + `terminal.py` + `gates/dialog.py`
  + `exec/validator.py` + `executor_cmds.py` + `config.py` + `tests/e2e_mxint/` + 它们测试
  + `examples/demo_task.yaml` + `tests/e2e_phase{13,14}/_artifacts/*.jsonl`（_tape）+ `_tui.svg`。

## 待办（等用户指示方向）
1. phase-12 / 13 / 14 / 15 分支 merge / PR（分支 `phase13-render-chart`）。
2. **批 2（phase-16）**：轻量本地包分发（多 pool + `name@source`）+ workspace-instruction（SPEC 已预留 `AgentResolver` 接口 + `ResolveContext.extra_roots`）。
3. code-reviewer M2/M3（resolve_flags setdefault 文档交叉引用 + stacklevel 指向）+ N3（tape artifact 含开发机路径，可 sanitize）—— minor/nit，下个 commit 顺手。
4. **render layer v1.5**：codex 接入（apply_patch 解析 + shell/read_file 映射，验证 renderer 零改动 = backend 隔离）。
5. **render layer v2**：Web 端 TS 镜像（types/render_item.ts + tools/normalize.ts + tools/kinds.tsx + tools/registry.ts，照 spec §5/§6/§8 实现并跑通相同 fixtures）+ 流式 shiki 增量高亮 + 千行 diff 虚拟化 + 复制按钮。

## 必读文件（下一任务开工前按需）
- [`docs/releases/2026-07-04-render-layer-v1-e2e-gaps.md`](../releases/2026-07-04-render-layer-v1-e2e-gaps.md)（本次 e2e gaps 闭环：GAP#1/#2 + 真跑验证）
- [`docs/releases/2026-07-04-render-layer-v1.md`](../releases/2026-07-04-render-layer-v1.md)（phase-15 v1 全貌）+ [`docs/specs/render-layer-design-draft.md`](../specs/render-layer-design-draft.md) §3/§5/§6/§8/§12
- [`orca/iface/cli/widgets/tool_render/`](../../orca/iface/cli/widgets/tool_render/)（normalize/kinds/registry/reduce 实现）+ [`orca/schema/render_item.py`](../../orca/schema/render_item.py)（契约）
- [`docs/releases/2026-07-03-phase14-agent-first-class.md`](../releases/2026-07-03-phase14-agent-first-class.md)（phase-14 全貌 + 与并行进程边界）

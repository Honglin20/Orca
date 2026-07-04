# 2026-07-04 — Render Layer v1（TUI 端）

> **关联**：[`docs/specs/render-layer-design-draft.md`](../specs/render-layer-design-draft.md) v1.2（已闭环所有 P0）
> **分支**：`phase13-render-chart`
> **Commits**：`ae0126b`（schema + tool_render + 测试）+ `edd738f`（迁移 + t 切 thinking）

---

## 做了什么

实现 render-layer-design-draft §11.1 v1 范围：在 canonical Event 之上加 iface
层纯函数渲染抽象。工具调用（agent_tool_call/result）现渲染为 Rich tool card
（file_read 目录树 / file_edit diff / shell 终端块 / ...），不再是单行 XML 一坨。

### 模块布局（spec §7.1）

```
orca/schema/
  └─ render_item.py             (NEW) RenderItem + RenderToolKind + ToolStatus

orca/iface/cli/widgets/tool_render/    (NEW)
  ├─ __init__.py                公共 API
  ├─ normalize.py               (executor, tool, args, result) → RenderItem 纯函数
  │                             + describe_tool_event 共享单行摘要（DRY）
  ├─ kinds.py                   per-kind Rich renderer + thinking/message
  ├─ registry.py                kind → renderer 派发
  └─ reduce.py                  RenderState + Event 流累积 reducer
```

### 渲染意图（spec §8 / §12）

| kind | 头部 | 体 | 备注 |
|---|---|---|---|
| `file_read` | `📂 <path> (N entries)` / `📄 <path>` | 目录树 / 行号化代码 | opencode read 目录 XML 解析（§6.3） |
| `file_write` | `✏ <path>` | 行号化代码 | |
| `file_edit` | `✏ <path> (+N -M)` | unified diff（+绿/-红/ctx 灰） | difflib SequenceMatcher |
| `shell` | `▶ $ <command>` | 终端块 | |
| `glob` | `༚ pattern: <p> (N matches)` | 路径列表 | |
| `grep` | `🔍 pattern: <p>` | 按文件分组 | |
| `unknown` | `? <tool_name>` | args JSON 美化 + result 截断 | §12.9 |
| `agent_thinking` | (无 header) | dim+italic 纯文本 | §12.8 claude-code 对齐 |
| `agent_message` | (无 header) | Rich Markdown + Syntax | §12.12 代码块高亮 |

### 命令

- `t` 键切 thinking 可见性（spec §12.8）

---

## 测试

- **新增 32 test**（`tests/iface/cli/test_tool_render.py`）：
  - `TestNormalizeSnapshot`（11 case fixtures-driven，跨端一致性 anchor，§10.1）
  - `TestFailLoud`（args 非 dict → NormalizeError；opencode 目录 XML 解析失败降级，§6.2/§13）
  - `TestReducer`（message/thinking 累积 + tool_call/result 配对 + seq 单调，§9）
  - `TestClaudeCodeAlignment`（thinking 不渲染 markdown + message 走 markdown，§14.1）
  - `TestDRYConsistency`（describe_tool_event 共享 log_stream/node_detail，§7.3）
  - `TestRegistryDispatch`（每 kind 渲染为 Panel，§8.2 共性规则）
- **fixtures**：`tests/e2e_phase15/_artifacts/render_tool_cases.json`（11 case，含
  opencode read 目录 XML 真实 shape + malformed 降级 + running 状态）。
- **既有测试**：既有 widget `_stream_lines` 长度断言不变（spec §7.3 边界 a：N 事件 →
  N entries list 守恒）；e2e_phase13 e2e_6 opencode 测试一处 `_stream_lines` join
  改 str() 兼容混合 list。
- **结果**：1327 passed, 30 skipped, 0 failed, 0 回归（baseline 1276）。

---

## 自验证

```
pytest tests/ -q --ignore=tests/e2e_mxint --ignore=tests/e2e_phase14
  → 1327 passed, 30 skipped

orca validate examples/demo_task.yaml
  → ✓ 校验通过

# 工具卡片渲染（真 tape: runs/demo_task-20260703-221337-c94151.jsonl）
opencode read 目录 → 17 条目树（不再 XML 一坨）：

╭─ [✓] 📂 /Users/mozzie/Desktop/Projects/Orca (17 entries) ─────────╮
│ /Users/mozzie/Desktop/Projects/Orca                              │
│ ├── .codegraph/                                                   │
│ ├── .DS_Store                                                     │
│ ├── .git/                                                         │
│ ├── .github/                                                      │
│ ├── .gitignore                                                    │
│ ... (17 entries total)                                            │
╰───────────────────────────────────────────────────────────────────╯
```

---

## 与并行进程的边界

- 仅改 phase-15 范围：`orca/schema/render_item.py` + `orca/schema/__init__.py` +
  `orca/iface/cli/widgets/tool_render/*` + `tests/e2e_phase15/` +
  `tests/iface/cli/test_tool_render.py` + 迁移 `log_stream.py` / `node_detail.py` /
  `app.py` + e2e_phase13 一处 join fix。
- 未触及并行进程持有：`profiles/builtin/*` + `terminal.py` + `gates/dialog.py` +
  `exec/validator.py` + `executor_cmds.py` + `config.py` + `tests/e2e_mxint/`。

---

## 显式不做（v1 范围外，spec §2.2 / §11.2/§11.3）

- ❌ Web 端（TS）任何代码——Web 待重写
- ❌ 流式 shiki 高亮、千行 diff 虚拟化（v2）
- ❌ 复制按钮（v2）
- ❌ codex backend（v1.5）
- ❌ 改 translator、改 canonical Event schema
- ❌ 重写 TUI 整体架构（DAG/gate/chart 不动）

---

## 后续

- Web 重写时照 spec §5/§6/§8 实现 TS 镜像（`types/render_item.ts` + `tools/normalize.ts`
  + `tools/kinds.tsx` + `tools/registry.ts`），跑通相同 fixtures（spec §10.2 硬约束）
- v1.5：codex 接入（apply_patch 解析 + shell/read_file 映射，renderer 零改动验证
  backend 隔离）
- v2：流式 shiki 增量高亮、千行 diff 虚拟化、复制按钮

# 多 Run 列表页设计规范（RunListPage）

> **范围**：landing `/` 的多 run 监控列表页视觉/交互规范。coder 据此实现 `RunListPage.tsx`；
> 配套静态 mockup 见 `orca/iface/web/frontend/src/components/pages/RunListPageMockup.tsx`。
> **视觉一致性**：与 `RunDetailPage`（三栏 + TopBar + `react-resizable-panels` + lucide-react）
> 同一套 design token（`index.css` CSS 变量）+ utility class（`orca-*`）+ tailwind `orca.*` palette，
> **不引入新设计系统、不装新依赖**。
> **功能契约来源**：`docs/specs/2026-07-23-single-port-multi-run-monitoring.md` §6.3 / D10 / D11。

---

## 1. 布局栅格

整页结构自顶向下三段，根容器 `flex h-full flex-col orca-bg-app`（与详情页同底色）：

```
┌─────────────────────────────────────────────────────────────┐
│ ListTopBar  （orca-bg-surface + orca-border border-b, h-14） │
│  "Orca Runs"  [刷新] [🔍 搜索……]  [All|运行中|待决策|已完成|失败]  [分组 ⊜]  [主题] │
├─────────────────────────────────────────────────────────────┤
│  分组区（scroll-y，flex-1 overflow-y-auto，orca-bg-app）       │
│   ▼ 项目名  · /abs/path/to/project  · 3 runs                  │
│       ┌──────────────────────────────────────────────────┐   │
│       │ ● running  workflow-name  <run_id8>  3/7  $0.12  │   │
│       │            started 2m ago · 42 events    [打开] [🗑] │   │
│       └──────────────────────────────────────────────────┘   │
│       …更多 run 行                                            │
│   ▶ 项目名  · /path  · 2 runs（折叠态）                        │
│   ▼ Legacy · ~/.orca/runs · 1 run                              │
├─────────────────────────────────────────────────────────────┤
│ ListFooter  （orca-bg-surface + border-t，h-10）              │
│   显示 12 / 共 34          [加载更多]                          │
└─────────────────────────────────────────────────────────────┘
```

- **栅格**：单列，内容居中 max-w `7xl`（~1280px），左右 `px-6`。行卡片内部用
  flex 横排：`[状态徽章] [主体: workflow + 元信息] [右侧指标] [动作]`。
- **间距**：分组之间 `space-y-4`；分组内 run 行 `space-y-1.5`；卡片 padding `px-4 py-3`。

### 列宽（run 行卡片内部，flex 横排）

| 段 | 宽度策略 | 内容 |
|---|---|---|
| 状态徽章 | 固定 `w-28` | 圆点 + 状态文字（见 §3） |
| 主体 | `flex-1 min-w-0` | workflow 名（truncate）+ run_id 短码（mono、faint） |
| 指标组 | `shrink-0`，gap-6 | 进度 / cost / 耗时 / 事件数（tabular-nums） |
| 动作 | `shrink-0` | `[打开]` 常显；`[删除]` hover-only（见 §4） |

窄屏（< 768px）：指标组换行到主体下方（`flex-wrap`），动作固定右侧。

---

## 2. 组件清单（交给 coder 实现）

| 组件 | 职责 | 备注 |
|---|---|---|
| `RunListPage` | 页根；挂载 `runListStore`（refresh + 4s 轮询 + WS run_changed）；路由跳转 `/runs/:runId` | SPEC §6.2 |
| `ListTopBar` | 标题 + 刷新 + 搜索 + status chips + 分组开关 + 主题 | 复用 `use-theme`；与详情 `TopBar` 同 visual scale（h-12/14、surface 底） |
| `SearchInput` | 受控输入 + debounce（~250ms）写回 store.filter.q | lucide `Search` 图标 |
| `StatusFilterChips` | 五个 chip：全部 / 运行中 / 待决策 / 已完成 / 失败 | 单选；active 态见 §5 |
| `ProjectGroup` | collapsible 分组头 + run 行列表 | 折叠态记忆在组件本地 state（不进 store） |
| `RunRow` | 单 run 卡片；整行 click → navigate；hover 显示删除 | needs-decision 高亮见 §6 |
| `StatusBadge` | 状态圆点 + 文字（DRY 单出口，供 row + 详情复用） | 配色映射见 §3 |
| `DeleteConfirmDialog` | 二次确认弹窗（聚焦"删除"按钮、Esc 取消、Enter 确认） | 见 §4 |
| `ListFooter` | "显示 N / 共 M" + [加载更多] | limit/offset 分页 |
| `EmptyState` | 无 run 引导（图标 + 文案） | 见 §7 |
| `ListSkeleton` | 首次加载骨架（6 行 shimmer） | 见 §8 |

**DRY 约束**：`StatusBadge` 的 status→色/字 映射是单一真相源，不得在 RunRow/Chips 复制；
详情页将来如需一致徽章也从此 import。`statusColor()`（`components/layout/status-style.ts`）
仅返回 text-* class，列表徽章需"圆点+文字+chip 背景"组合，故新增 `StatusBadge` 组件封装。

---

## 3. Status 徽章配色映射

状态语义与 `WorkflowStatus`（`types/store-types.ts`）对齐；SPEC §6.3 的 "needs-decision"
= `blocked`（gate/human_decision_requested 派生，与详情页 `TopBar` 同语义）。

每个徽章 = `<span class="badge badge-{kind}">` 形态：
`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium`。

| status（display） | dot color class | text class | border class | 备注 |
|---|---|---|---|---|
| running（运行中） | `bg-orca-running` | `text-orca-running` | `border-orca-running/30` | 钢蓝品牌色 = `--accent` |
| completed（已完成） | `bg-orca-done` | `text-orca-done` | `border-orca-done/30` | emerald |
| failed（失败） | `bg-orca-failed` | `text-orca-failed` | `border-orca-failed/30` | red |
| cancelled（已取消） | `bg-orca-pending` | `text-orca-pending` | `border-orca-pending/30` | slate 中性 |
| blocked（待决策） | `bg-orca-skipped` | `text-orca-skipped` | `border-orca-skipped/30` | violet；**外加 pulse 动画**（见 §6） |
| queued / idle | `bg-orca-pending` | `text-orca-pending` | `border-orca-pending/30` | 未启动 |

**色值来源**：`tailwind.config.js` `orca.*` palette（与 `status-style.ts` / `TopBar` / `AgentsRail`
同源）。不引新色；`border-orca-*/30` 用 Tailwind 透明度修饰符。

**dot 形态**：`h-1.5 w-1.5 rounded-full`，running 时可选附加 `animate-pulse`（克制：仅 running
加，避免多 run 并列闪烁噪声）。

---

## 4. 行内删除 + 二次确认（SPEC D10/D11）

### 4.1 RunRow hover 行为

- 整行 `group` + `hover:orca-bg-surface-2 cursor-pointer`，左侧补 2px 状态色竖条
  （复用 `NODE_STATUS_HEX` 同色策略：`absolute inset-y-0 left-0 w-0.5`，色 = dot 色）。
- `[打开]` 按钮（lucide `ExternalLink` 或 `ChevronRight`）常显，`orca-text-faint hover:orca-accent`。
- `[删除]` 按钮（lucide `Trash2`）`opacity-0 group-hover:opacity-100 transition-opacity`，
  `orca-text-faint hover:text-orca-failed`。
- **焦点可达性**：RunRow 用 `<button>` 包主体可 tab；删除按钮独立 `<button>`，`stopPropagation`
  避免触发行 click。键盘 Enter/Space 触发 active 元素。

### 4.2 DeleteConfirmDialog

- 覆盖层：`fixed inset-0 z-50 bg-slate-900/40`（与 AgentsRail DAG overlay 同策略，intentional
  inverse）。
- 卡片：`orca-bg-surface orca-border rounded-lg shadow-lg w-full max-w-sm p-5`，居中。
- 文案：`删除该 run？` + 副文 `将永久删除 tape 与产物目录，不可恢复。` + run_id mono 展示。
- 按钮：[取消]（`orca-text-muted`，Esc 触发）+ [删除]（`bg-orca-failed text-white`，Enter 触发，
  默认聚焦）。
- **乐观移除 + 回滚**（SPEC D11）：确认 → `deleteRun(id)` 立刻从 `runs[]` 去掉该行 →
  `DELETE /api/runs/<id>` → 失败回滚（toast 提示）。

---

## 5. Status filter chips（单选）

五个 chip：`全部 | 运行中 | 待决策 | 已完成 | 失败`。chip 形态：
`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs`。

- **inactive**：`orca-border orca-text-muted orca-bg-surface hover:orca-bg-surface-2`。
- **active**：`border-transparent bg-orca-accent text-white`（`orca.accent` = `--accent` 钢蓝，
  active 态用品牌色填充，与详情页 active tab 下划线同语义）。
- 每 chip 前可放状态圆点（faint 化），active 时变白。
- 切换 chip 写回 `store.filter.status`，触发 `refresh()`。

分组开关（最右）不是 chip，是图标 toggle：lucide `GroupBy` / `List`，active 高亮
`orca-accent`，inactive `orca-text-faint`。

---

## 6. needs-decision 高亮（SPEC §6.3 "提示用户去处理 gate"）

`blocked` run 除 violet 徽章外，**整行左侧加 2px violet 竖条**（替代默认 faint 竖条），
且 row 右侧追加小标签 `待决策`（`text-orca-skipped text-[10px] orca-bg-surface-2 px-1.5 py-0.5 rounded`），
点击同样进详情页处理 gate。dot 的 `animate-pulse` 在此状态启用，**仅此一种状态 pulse**，
避免噪声。

---

## 7. 空态（无 run）

`EmptyState`：整页居中，lucide `Inbox` 大图标（size=48, strokeWidth=1, `orca-text-faint`）
+ 主文案 `暂无 run`（`orca-text`，`text-base font-medium`）+ 副文案
`在项目里运行 \`orca run <workflow>\` 即可在此看到。`（`orca-text-faint text-sm`）。
不显示分组/分页/搜索结果计数。

**搜索/过滤空结果**（有 run 但被筛掉）走另一文案：`没有匹配的 run` + 副文`试试调整搜索或过滤条件。`

---

## 8. 加载骨架（首屏 refresh 期间）

`ListSkeleton`：6 个 run 行占位，每行 = `rounded border orca-border orca-bg-surface px-4 py-3`
内嵌 3 条 `h-3 rounded orca-bg-surface-2` 横条（主/副/右指标），宽度 `w-1/3`/`w-1/4`/`w-20`，
加 `animate-pulse`。分组头用 `h-4 w-40 orca-bg-surface-2 rounded animate-pulse`。

刷新按钮（手动）期间在其位置替换为 `Loader2 animate-spin`（与 `TabFallback` 同 icon 同语义）。

---

## 9. 交互细节汇总

- **行 click** → `navigate('/runs/:runId')`（React Router）。中键 click：在新 tab 打开
  （`onAuxClick` + `window.open`），便于对照多 run。
- **刷新按钮**：`RefreshCw` 图标；loading 期 spin；写回 `store.lastFetch`。
- **搜索框**：`Search` 图标 + input；clear 按钮（`X`）仅 q 非空时显示。
- **分组折叠**：lucide `ChevronDown`/`ChevronRight`；折叠态记忆仅本地（不进 store，SPEC R3）。
  `Legacy` 桶默认折叠，其余默认展开。
- **WS run_changed**（SPEC D11）：收到 `action=deleted` 乐观移除；`action=changed` → 立即
  `refresh()`（节流 2s）。控制帧，**不进 processEvent/reducer**。
- **unmount 清空**（SPEC §6.2）：列表 unmount → `runs=[]` + 停轮询 + 关 WS（避免残留）。

---

## 10. 响应式

- **≥ 1024px（桌面）**：§1 栅格，run 行单行铺满。
- **768–1023px（平板）**：指标组保留右侧但 gap 收紧到 `gap-4`；max-w 收 `5xl`。
- **< 768px（窄屏）**：指标组 `flex-wrap` 掉到主体下方；删除按钮常显（hover 在触屏无效）；
  分组头副信息（path）隐藏，仅保留项目名 + run 数。
- **TopBar** 窄屏：搜索框折叠为图标，点击展开 input（`md:hidden` / `hidden md:inline-flex`）。

---

## 11. 可访问性

- 所有按钮 `title` + `aria-label`（与详情 `TopBar` 一致）。
- Dialog：focus trap（首次 mount 聚焦"删除"，Tab 在对话框内循环）；Esc 关闭；
  `role="dialog" aria-modal="true" aria-labelledby`。
- 状态除颜色外附文字（色盲友好，不依赖颜色单一编码）。
- `prefers-reduced-motion: reduce` 时禁用 `animate-pulse`（全局 CSS 可后续补，不在本页 scope）。

---

## 12. 依赖与约束

- **新增依赖**：无。复用 `react-router-dom`、`lucide-react`、`zustand`、Tailwind v3。
- **CSS**：只复用 `index.css` 的 `orca-*` utility + `orca.*` palette；**不改 `index.css`**。
- **store 隔离**：`runListStore` **严禁 import `workflow-store`**（SPEC R3 / AC11）。
- **图标尺寸/线宽**：行内 14 / 标题 16，`strokeWidth=1.5`（与 `icons.tsx` 常量一致）。

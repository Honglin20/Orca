# Web RunListPage 重设计 SPEC（v2）

> **范围**：Web 主页 `/`（`orca/iface/web/frontend/src/components/pages/RunListPage.tsx`）的 UI/交互重设计。
> **形态**：Option A「精炼卡片列表」（用户已确认）——保留 project 分组折叠卡片，顶栏新增 排序/多选/批量删除，折叠 + 排序 localStorage 持久化，删除按钮放大常显，美化项目头，修主题按钮。
> **supersede**：本 SPEC 覆盖 `docs/specs/web-multi-run-list-page-design.md` 中被改写的小节（§1 布局 / §2 组件清单 / §4 删除 / §9 交互 / §10 响应式 / §11 可访问性）；未涉及小节（§3 状态配色 / §5 chips 配色 / §6 needs-decision / §7 空态 / §12 依赖）仍有效，按 DRY 从 `status-badge.tsx` / `status-style.ts` 取色。
> **来源**：D1 缺陷审查 + D2 视觉美术审查 + D3 UX 审查 三份报告综合 + D4 一致性自查。
> **日期**：2026-08-03。

---

## 0. 硬约束（不可逾越）

- **前端唯一**：冻结后端契约——`GET /api/runs?scope=all`、`GET /api/projects/stale`、`DELETE /api/runs/{id}`（200/404/409）、WS `/ws` 控制帧 `{kind:"control",type:"run_changed",run_id,action}`。**禁改** `orca/iface/web/routes/*`、`run_manager.py`、`server.py`、`ws_handler.py`。
- **零新依赖**：只用 React 19 + Tailwind v3 + zustand + lucide-react + react-router v6。不装包。
- **不改 `index.css` token**：复用现有 CSS 变量与 `orca-*` utility。需要透明度时用 arbitrary value `bg-[rgb(var(--token)/<alpha>)]`（**注意**：自定义 `orca-bg-*` utility 不支持 Tailwind `/alpha` 修饰符，必须用 arbitrary `bg-[rgb(...)]`；`orca.*` palette 色 如 `bg-orca-running` 支持 `/alpha`）。
- **铁律 R3**：`run-list-store` 严禁 import `workflow-store`（AC11 grep 守门，提交前必跑）。
- **不碰 `RunDetailPage`**（仅作视觉一致性参照：TopBar `h-12`、卡片语言、token）。

---

## 1. 信息架构与布局

### 1.1 双行顶栏（**覆盖原草图 M1 单行顶栏**——D2 MAJOR：跨页高度一致）

跨页品牌行与详情页 `TopBar` 同高 `h-12`；列表页专属工具折行到 sub-bar `h-10`。

```
┌─ TARS · Orca Runs ──────────────────────── [⟳刷新] [☯主题] ─┐  ← h-12 品牌行（跨页一致）
├─ [🔍 搜索 workflow/run_id/项目…]                            ┤
│  ●全部 ●运行 ●待决 ●完成 ●失败   [↕排序:开始时间↓] [∴分组]  │  ← h-10 工具行（列表页专属）
└─────────────────────────────────────────────────────────────┘
```

- 品牌行：左 `TARS`（`orca-accent text-lg font-semibold tracking-wider`）+ `/ Orca Runs`（`orca-text-faint text-sm`）；右刷新按钮 + 主题按钮。
- 工具行：搜索框（`flex-1 max-w-md`）+ 状态 chips + 排序触发器 + 分组 toggle。
- 选中态非空时，**bulk bar** 作为 main 滚动区首子渲染（`sticky top-0 z-30`，贴 topbar 下沿，见 §5/§4），不在 topbar 内。

### 1.2 分组容器（**覆盖原草图 M1 嵌套卡片**——D2 FATAL：去双 border）

容器**无 border**，靠 `surface-2` 半透底 + 左侧 3px accent 色条表达分组层次；run 行保留 border 卡片。避免「白卡套白卡」。

```tsx
<section className="relative rounded bg-[rgb(var(--surface-2)/0.3)]">
  {/* 左侧色条：默认 accent/40；含 blocked run 时整组变 STATUS_BAR_HEX.blocked（NF1：--skipped 变量不存在，用 STATUS_BAR_HEX）*/}
  <div className="absolute inset-y-0 left-0 w-[3px] rounded-l" style={{ backgroundColor: hasBlocked ? STATUS_BAR_HEX.blocked : "rgb(var(--accent)/0.4)" }} />
  <div className="py-2 pl-4 pr-2">{/* 项目头 */}</div>
  {open && <div className="space-y-1.5 px-2 pb-2">{/* run 行 */}</div>}
</section>
```
（`--skipped` 非 index.css 现有变量——用 `STATUS_BAR_HEX.blocked` = `#a78bfa` 直接 inline，或新增仅本组件用的常量。**采用 inline hex 来自 `STATUS_BAR_HEX`**，符合「行内 hex 仅限 STATUS_BAR_HEX」约束。）

> 注：自定义 `orca-bg-*` utility 不支持 Tailwind `/alpha` 修饰符，故 surface 透明度一律走 arbitrary `bg-[rgb(var(--surface-2)/0.3)]`；`orca.*` palette 色（如 `bg-orca-running`）支持 `/alpha`。SPEC 全文统一此规则（spec-reviewer FATAL-2 闭环）。

### 1.3 页宽与节奏
- 页宽 `mx-auto max-w-7xl px-6`。
- 分组间 `space-y-3`（12px，比原 `space-y-4` 收紧）；行间 `space-y-1.5`；项目头内 `gap-x-2`；metric 间 `gap-x-4`。
- footer `h-10`：`显示 N / 共 M`；选中非空时追加 `· 已选 K`（`orca-accent`）；右侧 `[全部展开]/[全部折叠]`（分组 ≥3 时显示）。

---

## 2. 视觉规范速查表（D2 产出，实现须照抄）

### 2.1 圆角 3 档（**禁 `rounded-lg/xl/2xl`**）
| 档 | class | 用途 |
|---|---|---|
| sm | `rounded` | run 行卡、按钮、icon badge、dialog、stale item |
| md | `rounded-md` | 排序下拉、bulk bar |
| full | `rounded-full` | chips、StatusBadge、状态 dot、mini 状态 pill |

### 2.2 阴影 3 档（**禁 `shadow-xl/2xl`、裸 `shadow`**）
| 档 | class | 语义 |
|---|---|---|
| sm | `shadow-sm` | 静态卡（run 行、stale item） |
| md | `shadow-md` | 浮层（排序下拉、bulk bar） |
| lg | `shadow-lg` | modal dialog |

### 2.3 字号 4 档（**禁 `text-[10px]/text-[11px]/text-[13px]`**）
| 档 | class | 用途 |
|---|---|---|
| xs | `text-xs`(12) | metric、path、聚合、按钮文字、footer |
| sm | `text-sm`(14) | workflow 名、dialog body |
| base | `text-base`(16) | dialog title、空态主文案 |
| lg | `text-lg`(18) | TARS brand |

### 2.4 文字层次（**强制映射**——D2 MAJOR：faint 亮模式 2.8:1 不达 AA）
| class | 用途 |
|---|---|
| `orca-text` | 主体：项目名、workflow 名、dialog title |
| `orca-text-muted` | **可读副文**：path、run_id 短码、聚合 N runs、metric value、按钮文字 |
| `orca-text-faint` | **仅装饰**：icon、placeholder、`·` 分隔、空态副文案、未 hover 删除 icon |

> 凡「需要读」的信息一律 `orca-text-muted`，不得 `orca-text-faint`。

### 2.5 选中态 3 级层次
| 级 | 用途 | class |
|---|---|---|
| 强 | filter chip active | `border-transparent bg-orca-accent text-[rgb(var(--app-bg))]`（暗模式自动反相，避免 `text-white` 在浅 accent 上 AA 失败） |
| 中 | 分组 toggle active、排序触发器 active | `border-orca-accent/30 bg-[rgb(var(--accent)/0.08)] orca-accent` |
| 弱 | 下拉项 hover/选中、run row hover | `bg-[rgb(var(--accent)/0.08)]` / `orca-bg-surface-2` |

### 2.6 图标
- 行内 `size={14} strokeWidth={1.5} aria-hidden`；标题/品牌 `size={16}`；**打开/删除按钮强制 `size={16}`**（D2：两按钮同档，避免跷跷板）；metric icon `size={12}`。复用 `icons.tsx` 常量。

### 2.7 modal 遮罩（**不新增 slate-\*，不改 index.css**）
- 用 arbitrary value：`bg-[rgb(var(--text)/0.4)]`（亮模式 text 深→深遮罩；暗模式 text 浅→浅雾，反相自适应）。**禁用** 新增 `bg-slate-900/40`。

### 2.8 骨架（**禁新动效/扫光**）
- 仅 `animate-pulse` + `orca-bg-surface-2`。分组头骨架：chevron 位 + 项目名条 + path 条；行骨架 ×4：badge 位 + workflow 条 + run_id 条 + 4 metric 条。刷新按钮 loading 期 `Loader2 size={14} animate-spin`。

---

## 3. 状态契约（store + view-state）

### 3.1 `run-list-store.ts` 增量（不违 R3）
- **新增 `deleteRuns(ids: string[]): Promise<{deleted:string[]; failed:{id:string;reason:string}[]}>`**：
  - 乐观：先把 `ids` 全部从 `runs` 移除，并入 `pendingDeletes` Set。
  - 逐个 `DELETE /api/runs/{id}`（`Promise.allSettled`）；非 2xx/非 404/网络错 → 该 id 计入 `failed`。
  - 任一失败 → `await refresh()` 与后端对账（`pendingDeletes` 守卫见 §3.3）；成功 id 从 `pendingDeletes` 移除。
  - 返回 `{deleted, failed}` 供 UI 出聚合 toast。
  - `deleteRun`（单条，保留）与 `deleteRuns`（批量，新增）**共存**；二者共用 `pendingDeletes` 守卫。
- **新增 inflight guard**（D1 M2）：模块级 `let inflightSeq = 0`；`refresh()` 入口 `const seq = ++inflightSeq`，响应回来 `if (seq !== inflightSeq) return` 丢弃过期响应（防 stale 覆盖 fresh）。**与既有 `lastFetch` 节流正交叠加**：节流 gate 入口（2s 内拒入），seq gate 出口（过期响应丢弃），二者不冲突。
- **新增 `pendingDeletes: Set<string>`**（D1 M4，防幽灵 run）：`refresh()` 成功后 `set({ runs: data.filter(r => !pendingDeletes.has(r.run_id)) })`；`deleteRun/deleteRuns` 成功后移除 id，失败回滚时移除并恢复。
- **error 显示契约**（D1 M3）：`error` 字段已存在，**页面必须消费**——`error && !loading` → 渲染错误条。
- **`reset()` 清理 `pendingDeletes`**。

### 3.2 view-state（组件内，不入 store）
- **selection**：`Set<run_id>`（D1 M7）。只存字符串 id，不存引用/index。refresh/WS 后自动求交（不在 `runs` 的 id 移除）。
- **sort**：`{ field: SortField; dir: "asc"|"desc" }`，`SortField = "started_at"|"workflow_name"|"status"|"cost"|"elapsed"|"event_count"`。默认 `{field:"started_at",dir:"desc"}`。**持久化** localStorage `orca-runlist-sort-v1`（D3 M1，与折叠一致性）。
- **collapsed**：`Set<projectName>`，持久化 localStorage `orca-runlist-collapsed-v1`（D1 M9：版本后缀 + try/catch 降级 + 惰性清理未知 project）。
- **filter**：`q` + `status`（沿用）。
- **ws status**：`{ connected: boolean; reconnects: number }` 组件态（D1 M1/D3 M4）。

### 3.3 关键时序不变量
- 选择集 / 排序 / 折叠 三类 view-state **互不重置**：切排序、切 chip、切 groupBy、清搜索 → **选择全保留**；仅「用户点取消选择」「删除完成」「页面刷新（非持久化）」清空选择。
- 排序与分组叠加：**全局排序 → 再按 project 分桶**（组间按各组最新 run 的 started_at desc，组内按用户 sort field）。`groupBy=false` 退化为单组全列表排序。sort 必须 stable（ES2019+ Array.sort 已 stable，SPEC 显式要求 + 单测守门）。

---

## 4. 组件清单（建 `components/runlist/`）

| 组件 | 职责 | 关键点 |
|---|---|---|
| `RunListPage.tsx`（页壳，拆薄） | 编排：mount refresh+轮询+WS、过滤、分组、排序、选择、bulk、dialog | 不直接渲染细节，组合子组件 |
| `ListTopBar.tsx` | 双行顶栏（品牌行+工具行） | 主题按钮调 `use-theme`（§6.1）；刷新 loading spinner |
| `SearchInput.tsx` | 搜索（debounce ~250ms） | 范围含 `project_name`（D3 m1）；匹配高亮（§6.2）；`aria-label` |
| `StatusFilterChips.tsx` | 状态 chips | dot 色取 `STATUS_DOT_BG`（DRY，D1 m2）；「待决策」chip 激活时强制展开含 blocked 分组（§6.3） |
| `SortMenu.tsx` | 排序下拉 | 触发器反映当前字段+方向（§6.4）；portal 到 body 避裁剪 |
| `ProjectGroup.tsx` | 分组容器（无 border）+ 头 + run 列表 | 头：folder icon + 名 + path + 聚合（运行中/待决策/总花费/最近）+ 三态全选；折叠头仍显 blocked 计数（§6.5） |
| `RunRow.tsx` | 单行（**DOM 重构**：checkbox 不嵌 button，D1 M5） | 左 checkbox + 主体可点区 + 操作组；状态竖条；待决策 ring |
| `BulkActionBar.tsx` | 选中非空时 main 首子 `sticky top-0 z-30`（`-mx-6 px-6 py-2 orca-bg-surface orca-border border-b shadow-md`） | `已选 N · [🗑删除(N)] · [✕取消]`；首次 hint（Shift 范围选） |
| `DeleteConfirmDialog.tsx` | 单条/批量确认 | focus trap + Esc + Enter + aria-describedby + 背景 `inert`（§6.6）；批量预览 ≤5 + 「还有 N」 |
| `EmptyState.tsx` | 空 / 筛选空 | 大 icon + 主副文案 |
| `ListSkeleton.tsx` | 首屏骨架 | §2.8 |
| `ErrorBanner.tsx` | refresh 失败 | `error && !loading` → 红字 + [重试] |
| `hooks/use-collapsed-projects.ts` | localStorage 折叠态 | 版本 key + 降级 + 惰性清理 |
| `hooks/use-list-selection.ts` | selection Set + 求交 + 范围选 | 订阅 store runs 自动求交 |
| `hooks/use-list-sort.ts` | sort state + 持久化 | localStorage `orca-runlist-sort-v1` |
| `hooks/use-ws-runlist.ts`（**必须**，承载 AC-14） | WS 连接 + 重连 + 状态 | onclose/onerror 指数退避（§6.7） |

---

## 5. 交互规范（D3 产出，照抄）

### 5.1 首屏三态（D3 FATAL F3 / D1 m3）
- `loading && runs.length===0` → `ListSkeleton`（最小展示期 ~600ms 防闪烁）。
- `!loading && error` → `ErrorBanner`。
- `!loading && runs.length===0` → `EmptyState`。
- `runs.length>0` → 列表；刷新中 `opacity-60` + 顶栏 spinner。

### 5.2 搜索穿透折叠（D3 FATAL F1）
- `q` 非空 → 含匹配 run 的分组**强制展开**（搜索态优先于持久化折叠）；分组头右侧显「搜索：X · 命中 N」。
- `q` 清空 → 恢复 localStorage 折叠态。
- 0 命中（有数据但被筛光）→ 分组区上方行内提示「未匹配任何 run」，**不**跳全屏空态。

### 5.3 blocked 不被埋（D3 FATAL F2）
- 折叠态分组头**必须显待决策计数**：`▶ 📁 X · N runs · ⚠ Y 待决策`（Y>0 时紫色 mini pill 高亮，Y=0 不显）。
- 状态 chip 切「待决策」→ 自动展开含 blocked run 的分组（同 5.2 规则）。
- 运行中 chip label 注明「含排队中」（D3 m4）。

### 5.4 排序可见 + 持久（D3 M1）
- 触发器文案随状态：默认 `↕ 排序`；选定后 `↕ 开始时间 ↓`（字段名+方向箭头）。
- 点击字段名 → 切到该字段（默认 desc）；**同字段二次点击反转方向**；不循环回「无排序」。箭头仅显示不可点。
- 排序态持久 localStorage。

### 5.5 多选可发现（D3 M2）
- **checkbox 常显**（非 hover-only）：默认 `opacity-40`，行 hover `opacity-100`，选中实心 `opacity-100`。命中区 ≥32px。
- 分组头三态 checkbox（未选/半选/全选）；footer「全选当前筛选（N 项）」。
- **Shift+点击范围选**：同分组内 A→Shift+B 选中区间。首次使用 bulk bar 显 hint（3s 淡出，localStorage 记忆）。
- 点 checkbox `stopPropagation`，不触发行跳转。

### 5.6 删除反馈（D3 M3）
- 成功 → 右下 toast `已删除 <name>` + 行 `opacity/height` 200ms 过渡消失。
- 失败 → toast（**废 `alert()`**）`删除失败：<原因>` + 行回滚动画。
- 批量 → toast `已删除 N 项` 或 `已删除 X 项，Y 项失败：[详情]`。
- DELETE in-flight 期间行 `opacity-40`（视觉「删除中」）。

### 5.7 删除确认对话框 a11y（D1 M10 / D3 M7）
- Esc → 取消；Enter（焦点在框内）→ 确认；focus trap（Tab 循环限框内）；`aria-describedby` 关联描述 `<p>`；关闭后焦点回触发元素；背景容器加 `inert`。
- 批量预览列表 ≤5 项 +「…还有 N 项」（可展开，modal 内滚动）；项显 `workflow_name + run_id 短码`。
- 执行期禁用确认按钮 + `Loader2` + 「删除中…」。

### 5.8 WS 断线告知 + 重连（D1 M1 / D3 M4）
- `onclose`（非主动）/`onerror` → 指数退避重连 1/2/4/8/16s 封顶 30s。
- footer 或品牌行非阻塞提示：`实时连接已断开（轮询兜底）`（`text-orca-failed` 小字）；重连成功淡出 + refresh。
- 重连 >3 次仍失败 → 升级提示 + 手动「重试连接」按钮。
- 非 JSON 帧 `console.warn` 留诊断（D1 m1，不静默）。

### 5.9 全部展开/折叠（D3 M5）
- footer 右侧 `[全部展开]/[全部折叠]`，分组 ≥3 时显示。

---

## 6. 专项交互细节

### 6.1 主题按钮修复（D1 FATAL F1）
`ListTopBar` 主题按钮改为：
```ts
import { currentTheme, nextTheme, setTheme } from "@/hooks/use-theme";
const [theme, setThemeState] = useState(currentTheme());
const onToggle = () => { const t = nextTheme(theme); setTheme(t); setThemeState(t); };
```
icon `Sun/Moon/Monitor` 按 `theme` 渲染；删除本地 `cycleTheme`。

### 6.2 搜索匹配高亮（D3 m2）
`q` 非空时，匹配子串在 `workflow_name` 用 `<mark className="bg-[rgb(var(--accent)/0.3)] rounded px-0.5">` 高亮；run_id 不高亮。非搜索态零开销。

### 6.3 状态 chip dot 配色 DRY（D1 m2）
chip dot 一律从 `STATUS_DOT_BG[RunStatus]` 取——**需在 `status-badge.tsx` 给现有 `STATUS_DOT_BG` const 加 `export`**（仅加 `export` 关键字，非破坏；`status-badge` 是共享组件非详情页，在 scope 内），禁硬编码 `bg-orca-running` 散落。「运行中」chip 匹配 `running||queued`，dot 用 running 色 + tooltip「含排队中」。

### 6.4 排序菜单样式（D2 M5）
触发器复用工具栏按钮语言 `inline-flex items-center gap-1.5 rounded border orca-border px-2 py-1 orca-text-muted hover:orca-text hover:orca-bg-surface-2`。下拉 portal：`orca-bg-surface orca-border rounded-md border shadow-md py-1 min-w-[180px]`，项 `hover:bg-[rgb(var(--accent)/0.08)]`，选中项尾部 `Check size={12} orca-accent`。

### 6.5 项目分组头（D2 M2）
- 第一行：`📁 Folder size={16} orca-accent` + 项目名 `text-sm font-semibold orca-text` + 「全选」三态 checkbox（右）。
- 第二行：聚合 `text-xs orca-text-muted`：`N runs` + 「运行中」mini pill（`bg-orca-running/10 text-orca-running` + 1.5×1.5 pulse dot，仅 running>0）+ 「待决策」mini pill（`bg-orca-skipped/10 text-orca-skipped`，仅 blocked>0）+ `· $总花费` + `· 最近 Zm`。**不显 project 路径**——`RunSummary` 无 `project_path` 字段（NF2：显路径需改后端，违 AC-18），分组标题已是 project_name，不再重复。
- 折叠态：单行 = chevron + folder + 名 + `· N runs` + （blocked>0 时）「⚠ Y 待决策」pill + path（截断）。

### 6.6 RunRow DOM 结构（D1 M5）
```tsx
<li role="row" className="group relative flex items-center gap-3 rounded border orca-border orca-bg-surface shadow-sm px-3 py-2 pl-4 hover:orca-bg-surface-2">
  {/* 状态竖条 */}
  <div className="absolute inset-y-0 left-0 w-0.5" style={{ backgroundColor: STATUS_BAR_HEX[rs] }} />
  <input type="checkbox" data-testid="run-checkbox" aria-label={`选择 ${run.run_id.slice(0,8)}`}
         className="h-4 w-4" checked={selected} onChange={...} onClick={e=>e.stopPropagation()} />
  <button type="button" onClick={()=>onOpen(run.run_id)} className="flex min-w-0 flex-1 items-center gap-3 text-left">
    <StatusBadge status={rs} /> <workflow 名 + 高亮> <run_id 短码 + ago> <metrics>
  </button>
  <span className="flex shrink-0 items-center gap-1">
    <button data-testid="open-btn" ...><ExternalLink size={16}/></button>
    <button data-testid="delete-btn" ...><Trash2 size={16}/></button>
  </span>
</li>
```
- 删除按钮：`size={16} p-1.5`，三级 opacity（`text-[rgb(var(--text-faint)/0.55)]` → 行 hover `orca-text-faint` → 自身 hover `text-orca-failed bg-orca-failed/10`），常显。
- 命中区 ≥32px：`min-w-[32px] min-h-[32px] inline-flex items-center justify-center`。

### 6.7 折叠持久 hook（D1 M9）
```ts
const KEY = "orca-runlist-collapsed-v1";
// 读：try JSON.parse catch → console.warn + 默认（仅 Legacy 折叠）
// 写：try localStorage.setItem catch → 静默降级内存态
// 惰性清理：读到的 project 名不在当前 groups → 忽略
```

---

## 7. data-testid 锚点表（测试门）

| testid | 元素 |
|---|---|
| `topbar` / `brand-row` / `tools-row` | 顶栏两行 |
| `refresh-btn` / `theme-btn` | 品牌行按钮 |
| `search-input` / `search-clear` | 搜索 |
| `status-chip-<key>` | 状态 chips |
| `sort-menu` / `sort-trigger` / `sort-option-<field>` | 排序 |
| `group-toggle` | 分组开关 |
| `group-<name>` / `group-header` / `group-collapse` / `group-select-all` | 分组 |
| `run-row`（+ 兼容 `run-item`） / `run-checkbox` / `open-btn` / `delete-btn` | 行 |
| `bulk-bar` / `bulk-delete-btn` / `clear-selection` / `select-all` / `expand-all` / `collapse-all` | bulk/全选/展开 |
| `delete-dialog` / `confirm-delete` / `cancel-delete` | 确认框 |
| `error-banner` / `retry-btn` | 错误条 |
| `ws-status` | 连接状态 |
| `empty-state` / `filtered-empty` / `list-skeleton` | 状态态 |

> **兼容性**：run 行同时挂 `data-testid="run-item"`（既有 `test_playwright_9b.py` 选择器）与 `run-row`（新测试用），或在 9b 中改用 `run-row`。coder 二选一，保 9b 绿。

---

## 8. 验收标准（AC，逐条可测）

### A. 痛点闭环
- **AC-1 删除**：删除按钮 `size=16`、命中区 ≥32px、常显（无 `opacity-0 group-hover`）；键盘 tab 可达；点删除→确认→行 200ms 消失 + 成功 toast。
- **AC-2 多选**：行/分组/全选三级 checkbox；Shift+点范围选；选择 `Set<run_id>`；切排序/chip/groupBy/清搜索 选择保留；refresh/WS 删除后选择自动求交。
- **AC-3 排序**：6 字段可排；触发器显当前字段+方向；同字段二次点反转；持久 localStorage；分组+排序 stable。
- **AC-4 折叠持久**：折叠写 `orca-runlist-collapsed-v1`；F5 后保持；localStorage 损坏降级不崩。
- **AC-5 项目头美化**：folder icon + 名 + path + 聚合（运行中/待决策/花费/最近）；折叠头显 blocked 计数。
- **AC-6 主题**：点主题按钮 `<html>` class 实际切换 + localStorage 写 + icon 同步；跨页一致。

### B. 审查新增（必闭环）
- **AC-7 搜索穿透**：`q` 非空含匹配分组强制展开；清空恢复；0 命中行内提示。
- **AC-8 blocked 穿透**：折叠头显待决策计数；「待决策」chip 展开含 blocked 分组。
- **AC-9 三态加载**：首屏骨架；error 显 ErrorBanner；空显 EmptyState。
- **AC-10 刷新 guard**：并发 refresh 过期响应被丢弃（inflightSeq 单测）。
- **AC-11 防幽灵 run**：删除期间 WS refresh 不复活 run（pendingDeletes 单测）。
- **AC-12 批量删除**：`deleteRuns` 逐条乐观+独立回滚；部分失败聚合 toast；确认预览 ≤5。
- **AC-13 对话框 a11y**：Esc/Enter/focus trap/aria-describedby/背景 inert/焦点恢复。
- **AC-14 WS 重连**：onclose/onerror 指数退避；断线提示；>3 次手动重试。
- **AC-15 fail-loud**：refresh error 显式渲染；非 JSON 帧 `console.warn`；删除失败 toast（无 alert）。
- **AC-16 视觉一致**：圆角/阴影/字号按 §2 档位；可读信息用 `orca-text-muted` 非 faint；**`RunListPage.tsx` 及新 `components/runlist/*`、`hooks/use-*.ts` 内不得存在** `bg-slate-*`、`rounded-lg/xl/2xl`、`text-[10/11/13px]`、裸 `shadow`（重写即清除既有违规，如当前 `DeleteConfirmDialog` 的 `bg-slate-900/40`/`rounded-lg`/`text-[11px]`、path 行的 `text-[11px]`）；其它文件（AgentsRail 等）遗留违规不在本 scope。
- **AC-17 R3**：`run-list-store` 不 import `workflow-store`——保持既有 **AC11 grep 守门测试**绿（AC11 是原 SPEC 既有的 R3 守门测试，非本 SPEC 新增编号；AC-17 仅复核其仍绿）。
- **AC-18 后端零改**：`git diff -- orca/iface/web/routes orca/iface/web/run_manager.py orca/iface/web/server.py orca/iface/web/ws_handler.py` 空。

---

## 9. 测试矩阵（每功能必测，见计划「功能→测试矩阵」）

- vitest 组件 `test/run-list-page.test.tsx`：mock fetch + WebSocket（`vi.stubGlobal`），覆盖 AC-1..AC-9, AC-13, AC-16。
- vitest store `test/run-list-store.test.ts`：`deleteRun`/`deleteRuns`（AC-12）、`onRunChanged`、inflightSeq（AC-10）、pendingDeletes（AC-11）。
- Playwright e2e `tests/iface/web/test_playwright_runlist.py`（`pytest.mark.integration`，复用 `conftest.live_server` + `_write_tape`）：排序/多选/批量删/单删/折叠持久/主题/搜索 真机驱动。
- 后端回归：`pytest tests/iface/web/test_routes.py tests/iface/web/test_multi_run_phase_c.py -q` 绿（AC-18 旁证）。

---

## 10. 看板视图（Board，方案 A —— 默认 IA）

> 用户反馈：列表「不像看板」，要一眼看清运行中/待决策。**默认视图改为状态列看板**；§1–§9 的列表视图保留为 toggle 的「列表」态（做批量清理/排序/全量浏览）。两种视图共用同一 store + 同一 selection `Set<run_id>` + 同一 sort/collapse 持久化。

### 10.1 视图切换
- 顶栏右侧 toggle：`[📇 看板 | ☰ 列表]`（segmented control；active = §2.5 强选中态 `bg-orca-accent text-[rgb(var(--app-bg))]`）。
- 持久化 `orca-runlist-view-v1` ∈ `"board"|"list"`；默认 `"board"`。
- `data-testid`：`view-toggle-board` / `view-toggle-list`。

### 10.2 看板布局
- 状态列左→右：`排队 | 运行中 | 待决策 | 已完成 | 失败`（与状态 chips 同语义；空列仍渲染占位）。
- 列容器水平排布 `flex gap-3 overflow-x-auto`；每列 `min-w-[260px] flex-1` `rounded-md bg-[rgb(var(--surface-2)/0.2)] p-2`。
- 列头：状态 dot + label + 计数（`text-sm font-semibold`）；**运行中/待决策列**左侧 3px 色条（`STATUS_BAR_HEX[status]`）+ 列头加粗 + 计数用状态色；**待决策列**计数>0 时整列 ring 提示（`ring-1 ring-orca-skipped/20`）。
- 列内卡片纵向 `space-y-2`，按用户 sort field 排序（同列表 §3.3）。
- **已完成/失败列**：只显最近 N=10 + 底部「显示更多（共 X）」点击展开（避免历史撑爆列）；展开态本会话记忆。
- 空列：居中 faint 占位「暂无」。

### 10.3 BoardCard（看板卡片）
- 容器：`rounded border orca-border orca-bg-surface shadow-sm p-3` + 左侧状态竖条（`STATUS_BAR_HEX`）。
- 内容（全部来自现有 `RunSummary`，零新字段）：
  - 第一行：`StatusBadge` + `workflow_name`（truncate `text-sm font-medium`）+ project_name（`text-xs orca-text-muted`）。
  - 第二行（running/queued）：进度条 `progress`（`h-1.5 rounded bg-orca-bg-surface-2` 内填 `bg-orca-accent`，按 `progress` 解析的百分比；解析失败显 indeterminate `animate-pulse`）。
  - 第二行（blocked）：`⚠ 等待 <elapsed>`（紫）。
  - 第三行：`cost · elapsed · event_count`（`text-xs orca-text-muted tabular-nums`）。
- 交互：整卡 click → `onOpen`；hover 右上显 `delete-btn`（同列表 §6.6，size=16，命中区≥32px）；hover 左上显 `run-checkbox`（与列表共享 selection）。卡片 selected 时 `ring-1 ring-orca-accent/40 bg-[rgb(var(--accent)/0.06)]`。
- `data-testid`：`board-card`（+ 兼容 `run-item`）；列 `board-column-<status>`；看板根 `board`。

### 10.4 与列表视图的共享契约
- **selection**：同一 `Set<run_id>`（§3.2）；任一视图勾选 → 另一视图同步；bulk bar 在两视图都显。
- **sort**：同一 sort state（组内/列内排序同 field/dir）。
- **search / status chips / theme / refresh / WS**：完全共享（顶栏统一）。
- 看板下不显 project 分组折叠（按状态分列已够；project 是卡片副标）。看板下 `groupBy` toggle 隐藏。

### 10.5 看板新增组件
- `components/runlist/RunBoard.tsx`（看板根：列布局 + 列渲染 + 空态）、`BoardColumn.tsx`（列头 + 卡片列表 + 显示更多）、`BoardCard.tsx`（单卡）。
- 复用：`StatusBadge`/`STATUS_BAR_HEX`/`statusToRunStatus`、`use-list-selection`、`use-list-sort`、`DeleteConfirmDialog`、`format-helpers`。

### 10.6 看板 AC（追加）
- **AC-19 看板默认**：`/` 默认渲染 board；toggle 切 list；`orca-runlist-view-v1` 持久；刷新保持。
- **AC-20 五列**：排队/运行中/待决策/已完成/失败 各一列；run 按 `statusToRunStatus` 落列；运行中/待决策列视觉强调。
- **AC-21 BoardCard**：进度条按 `progress` 渲染；blocked 显等待时长；click 进详情；hover 显删除+勾选；selected ring。
- **AC-22 已完成/失败限长**：列显最近 10 + 显示更多；展开记忆。
- **AC-23 共享 selection**：看板勾选 ↔ 列表同步；bulk bar 两视图都显。

### 10.7 看板测试（追加进 §9 矩阵）
- vitest 组件：`test/run-list-page.test.tsx` 加 board 用例（默认看板、五列落位、进度条、显示更多、toggle 持久、共享 selection）。
- Playwright e2e：`test_playwright_runlist.py` 加 board 真机（看板渲染、点卡进详情、切列表）。

---

## 11. 风险与边界

- **批量删除部分失败**：乐观全移→逐条 DELETE→失败 refresh 对账 + toast 列失败项（fail-loud）。
- **localStorage 禁用/损坏**：折叠/排序 hook try/catch 降级内存态，不阻断渲染。
- **R3 误伤**：`deleteRuns` 留在 `run-list-store` 内，不引 `workflow-store`；提交前跑 AC11 grep。
- **alpha class 陷阱**：自定义 `orca-bg-*` utility 不支持 `/alpha`，全走 arbitrary `bg-[rgb(var(--token)/alpha)]` 或 inline style。
- **响应式**：窄屏（<768px）顶栏工具行可横向滚动/折叠搜索为图标；bulk bar 全宽贴底；metric 换行。本轮做最小响应式，不追求移动端完美。
- **out of scope**：modal scrim 全局统一化（AgentsRail 等遗留 `bg-slate-900/40`）、暗模式 failed 色 AA 收口、移动端完整适配——标 P0b/后续，本次只保证 RunListPage 不新增违规。

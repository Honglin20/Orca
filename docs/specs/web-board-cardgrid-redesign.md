# Web 看板卡片网格重设计 SPEC（v1.1）

> **范围**：Web 主页 `/` 的**看板视图**（`RunBoard`）重设计——从 Trello 式横向 5 状态列，改为「KPI 概览带 + 分组 section + 卡片网格」。list 视图（`RunRow`/`ProjectGroup`）布局不动，仅同步「去 cost + 状态只画一遍」。
> **supersede**：覆盖 [`web-runlist-redesign.md`](./web-runlist-redesign.md) §10。AC 逐项处置：**AC-19 保留**（看板默认+toggle 持久）；**AC-20 废除**（五列落位 → 由 AC-B1 看板重构 + AC-B4 卡片网格替代）；**AC-21 部分保留**（进度条/blocked 等待/click 进详情 → AC-B7）；**AC-22 废除**（completed/failed 列限长 10 → AC-B10 section 限显 6 替代）；**AC-23 保留**（共享 selection）。§10.1（视图切换）/ §10.4（共享契约）仍有效；§10.8 的 GroupBy 机制 / project/workflow/time/none 桶定义 / 折叠泛化仍有效——**其中 status 桶顺序由本 SPEC §4.1 修订**（运行中→排队→待决策→失败→已完成）；§10.9（空桶隐藏）/ §10.10 AC-24~AC-26 仍有效。§1–§9 列表视图除「去 cost + 状态只画一遍」外不动。
> **来源**：用户反馈「看板不舒服」+ 业内 run 监控看板调研（Linear / Helicone / Prefect / Vercel / LangSmith——监控类事实标准是 dense 卡片/行 + KPI 概览，非 Trello 列）。
> **日期**：2026-08-04。**v1.1**：spec-reviewer 对抗闭环（1 FATAL + 5 MAJOR + 8 MINOR + 5 NIT 全部并入）。**v1.2**：test-agent 真机实测闭环——KPI 运行计数 + filter 补 `live-pending`（发现 2，4 胶囊合计=total）；`blocked` 死 UI 为继承的架构裂缝，另开 [`run-blocked-status-design-draft.md`](./run-blocked-status-design-draft.md)（发现 1，见 §10 已知限制）。

---

## 0. 硬约束（不可逾越）

- **前端唯一**：冻结后端契约（同 `web-runlist-redesign.md` §0）。**AC-18 后端零改**：`git diff -- orca/iface/web/routes orca/iface/web/run_manager.py orca/iface/web/server.py orca/iface/web/ws_handler.py` 空。
- **零新依赖**：React 19 + Tailwind v3 + zustand + lucide-react + react-router v6，不装包。
- **不改 `index.css` token**：配色优化 = **组件层去半透明叠层、实色化**（把 `bg-[rgb(var(--surface-2)/0.45)]` 等半透明 arbitrary 改为实色 `orca-bg-surface-2` / `bg-[rgb(var(--surface-2))]`），**禁止**改 `:root` / `:root.light` / `:root.dark` 的 token 值（影响全站其它页，out of scope）。
- **铁律 R3**：`run-list-store` 不 import `workflow-store`（AC11 grep 守门保持绿）。
- **零新字段**：复用现有 `RunSummary`（`run_id/workflow_name/project_name/status/progress/elapsed/started_at/event_count/source`）；`cost` 字段保留在类型里（后端仍返回），仅前端**不显示、不排序、不聚合**。
- **list 视图布局不动**：`RunRow`/`ProjectGroup` 的结构/交互不变，仅同步去 cost + 状态只画一遍。
- **`status-badge.tsx` 整文件保留**（含 `StatusBadge` 组件）：它是 `STATUS_DOT_BG` / `STATUS_BAR_HEX` / `statusToRunStatus` / `RunStatus` /（新增导出的）`STATUS_LABEL` / `STATUS_TEXT` 的单一出口。本 SPEC 让 BoardCard/RunRow 停用 `StatusBadge` 后该组件零消费者，但**不删函数、不删文件**——保留作未来详情页复用预留，删属独立清理任务（另开 issue，不在本 scope）。`orca-bg-*` 实色 / 半透明来自组件层 arbitrary 的判断不变。
- **testid 兼容**：保留 `board` / `board-card` / `run-item` / `view-toggle-board` / `view-toggle-list` / `group-by-*` / `show-empty-toggle` / `run-checkbox` / `delete-btn` 等既有 testid（保 e2e/单测绿）；新增 KPI 带、section testid（§7）。

---

## 1. 背景与目标

### 1.1 现状诊断
`RunBoard` 是 Trello 式横向 5 状态列（`flex gap-3 overflow-x-auto`，每列 `min-w-[260px]`），把 **run 监控数据**（只读、实时、盯进度/失败）套进了**项目管理 kanban 壳**。具体痛点：

1. **横向滚动丢全局**：5 列 × 260px ≈ 1300px+，常见屏要横滚，看板最值钱的「一眼全局」丢失。
2. **状态画 3–4 遍**：列左 3px 色条 + 列头状态 dot + 卡片左竖条 + 卡内 `StatusBadge`（dot+label），同一状态重复表达，视觉噪音。
3. **半透明叠层发灰**：列容器 `bg-[rgb(var(--surface-2)/0.45)]` 叠在 `app-bg` 上、卡片再叠 `surface`，多层半透明导致对比不足、灰蒙蒙。
4. **失败被淹没**：`completed`/`failed` 列都限显 10，失败 run 容易被大量完成项压在后面。
5. **缺 KPI 概览**：上来就是一堆卡片列，没有「运行 X / 待决策 Y / 失败 Z」的数字概览带。

### 1.2 目标形态
**KPI 概览带（可点过滤）+ 分组 section（status / project，`GroupBy` 切换）+ 卡片网格（响应式 2–4 列）**。

### 1.3 成功标准（可测）
- 一屏看全局，**无横向滚动**（除非 section 内卡片网格在极窄屏换行后纵向滚，那是 `main` 的纵向滚，不是看板横向滚）。
- 失败 / 待决策 run **一眼可见**（独立 section 置前 + 卡片边色提级 + KPI 带计数高亮）。
- 每个 run 的状态**只画一遍**（卡片左竖条 = 唯一状态色锚点 + 行内文字 label）。
- 卡片信息聚焦：去 cost，底部 = 耗时 · 事件数 · 相对时间。
- 配色去半透明、实色化，对比清晰。

---

## 2. 信息架构与布局

### 2.1 整页结构（ASCII）

```
┌─ TARS · Orca Runs ──────── [🔍 搜索…] ──── 分组[状态▾] [看板|列表] [⟳][☾] ─┐  ← 单行品牌+工具行
├─ ●运行 3   ●待决策 1   ●失败 2   ●完成 24          共 30 runs             ┤  ← KPI 概览带（点=过滤；失败提前到完成前）
╞══════════════════════════════════════════════════════════════════════════════╡
▍运行中 · 3                                                      〔收起〕
 ┌─────────┐ ┌─────────┐ ┌─────────┐
 │▎运行中   │ │▎运行中   │ │▎运行中   │   ← 卡片网格（响应式 2/3/4 列）
 │ kd-nas   │ │ kd-nas   │ │ kb-cur   │
 │ ▓▓▓▓▓▓ 3/7│ │ ▓▓▓░░░ 1/7│ │ ▓░░░░ 0/3 │
 │ 9m·142·3m前│ │ 4m·38·5m前│ │ 2m·6·8m前 │
 └─────────┘ └─────────┘ └─────────┘

▍待决策 · 1  ⚠                                                〔收起〕
 ┌─────────┐
 │▎待决策   │   ← 紫边 + ⏱等待时长
 │ kd-nas   │
 │ ⏱ 等待 5m │
 └─────────┘

▍失败 · 2  ✗                                                  〔收起〕
 ┌─────────┐ ┌─────────┐
 │▎失败     │ │▎失败     │   ← 整卡淡红边
 │ kb-cur   │ │ kd-nas   │
 │ 3m·14    │ │ 8m·60    │
 └─────────┘ └─────────┘

▍已完成 · 24                                      ▾ 显 6 / 共 24  〔收起〕
 ┌─────────┐ ┌─────────┐ ┌─────────┐
 │▎已完成   │ │▎已完成   │ │▎已完成   │
 │ phase13  │ │ phase13  │ │ demo     │
 │ 12m·880  │ │ 11m·760  │ │ 3m·90    │
 └─────────┘ └─────────┘ └─────────┘
╞══════════════════════════════════════════════════════════════════════════════╡
│ 显示 30 / 共 30                                              〔全部折叠〕 │  ← footer（分组 ≥3 时两视图都显）
└──────────────────────────────────────────────────────────────────────────────┘
```

> KPI 胶囊顺序 = **运行 · 待决策 · 失败 · 完成**（失败提前到完成前，与 §4.1 section 顺序「…失败→已完成」对齐，落实「失败优先可见」）。

切到 **分组[项目]** 时：section 头换成项目名（kd-nas / phase13 / demo），组内卡片混各种状态、靠卡片左竖条 + 文字 label 区分状态（组头不再有状态色条）。

### 2.2 KPI 概览带（`KpiStrip`，新增）
- 位置：品牌行下方、`main` 滚动区**之外**（与 topbar 同属固定区，随 topbar 一起 `shrink-0`），独占一行 `h-10 shrink-0 orca-bg-surface orca-border border-b px-6`。
- 内容：4 个状态胶囊 + 1 个总数，顺序 **运行 N · 待决策 N · 失败 N · 完成 N …… 共 N runs**。
- 计数契约（从 `runs` 实时算，**不受 `q`/`status` 过滤影响**——始终显全量分布；`StatusFilter` 语义沿用 `StatusFilterChips`）：
  - **运行** = `count(status ∈ {running, queued, live-pending})`（与 `group-runs` 排队桶 `accept={queued, live-pending}` 对齐）
  - **待决策** = `count(status === blocked)`
  - **失败** = `count(status ∈ {failed, cancelled})`
  - **完成** = `count(status === completed)`
  - **共** = `runs.length`
  - 注：计数始终显全量分布；搜索（`q`）激活时计数仍显全量、与下方过滤视图可能不一致——**有意为之**（KPI 作全局真相锚点，不为搜索态收缩）。
- 胶囊 = 过滤器（替代顶栏 `StatusFilterChips`）：点击胶囊 → `setStatus(filter)`，active 胶囊用 §2.5 强选中态（`border-transparent bg-orca-accent text-[rgb(var(--app-bg))]`）；点「共 N runs」或已 active 胶囊再次点击 → `setStatus("all")`。
  - 胶囊 `StatusFilter` 映射：运行→`running`、待决策→`blocked`、失败→`failed`、完成→`completed`。
- dot 色：胶囊前小 dot 取 `STATUS_DOT_BG[sourceStatus]`（运行→running、待决策→blocked、完成→completed、失败→failed），与原 `StatusFilterChips` 同源 DRY。
- **失败计数 >0 时胶囊强提示**：失败胶囊在 `failed>0` 时文字 + dot 用 `text-orca-failed`（即便非 active），让失败一眼可见。
- **filter 同步（I3 唯一解 + live-pending 修订）**：`RunListPage.tsx` 过滤分支须与 `group-runs` 桶 `accept` + KPI 计数三者统一——`status === "failed"` → `rs === "failed" || rs === "cancelled"`；`status === "running"` → `rs === "running" || rs === "queued" || rs === "live-pending"`（live-pending 归排队桶，故运行胶囊含它）。点 KPI 胶囊 → 对应桶 accept 的 run 均显示，**4 胶囊计数合计 = total**（live-pending 不漏算）。
- `data-testid`：`kpi-strip`（根）；`kpi-chip-<running|blocked|completed|failed>`；`kpi-chip-all`。

### 2.3 分组 section + 卡片网格（`CardGridSection`，新增；`RunBoard` 重构）
- `RunBoard` 从「横向 flex 列」重构为「**section 垂直堆叠**」：`<div data-testid="board" class="space-y-5">{buckets.map(→ CardGridSection)}</div>`。去掉 `overflow-x-auto`（无横向滚）。
- 每个 `CardGridSection`：
  - **section 头**（`h-9`，可点折叠）：左色条（status dim 强调列用 `STATUS_BAR_HEX[status]`；非 status dim / 含 blocked 用 `STATUS_BAR_HEX.blocked`；其余 `rgb(var(--accent)/0.4)`）+ label（`text-sm font-semibold`）+ 计数（`text-xs orca-text-muted tabular-nums`）+ 右侧「收起/展开」。status dim 强调列（running/blocked）label 用状态色（迁移 `BoardColumn` 现有 `COLUMN_EMPHASIS_TEXT` map 到 `CardGridSection`，或抽共享常量）。
  - **待决策 section 高亮（I9）**：status dim blocked 桶计数>0，或任意 dim 含 blocked run 的桶（`bucketHasBlocked`），section 根加 `ring-1 ring-orca-skipped/20`（从 `BoardColumn.ringWhenNonEmpty` 迁移），无论 `showEmpty`。
  - **卡片网格**（展开态）：`grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4`。
  - **限显 + 折叠**：每个 section 默认显前 `SECTION_LIMIT=6` 张卡；超出时网格下方显「▾ 展开剩余 X」（本会话记忆，`expanded` state）；section 头「收起」折叠整个 section（复用 `use-collapsed-buckets` 的 `"dim:key"` 持久化）。
  - **搜索穿透 + 过滤穿透（I8 唯一解）**：`q` 非空 **或** `status` 过滤激活时，含数据的 section 强制展开 + 限显放开（显全部匹配）；section 头右侧显「搜索：X · 命中 N」（`q` 态）。`RunListPage.tsx` 的 `isBucketOpen` 改为：
    ```ts
    const searching = q.trim().length > 0;
    const statusFilterActive = status !== "all";
    const isBucketOpen = (bucketKey: string): boolean => {
      if (searching || statusFilterActive) {
        return (buckets.find((b) => b.key === bucketKey)?.runs.length ?? 0) > 0;
      }
      return !collapsed.has(`${groupBy}:${bucketKey}`);
    };
    ```
    即任一 KPI 状态胶囊点击（`status !== "all"`）→ 含数据的 section 强制展开（覆盖持久折叠）；点 `kpi-chip-all` → `status="all"` → 退回持久折叠。
  - **board/list 折叠共享（I10/D2 唯一解）**：board section 折叠与 list 分组折叠共用 `use-collapsed-buckets` 的 `"dim:key"`（如 `status:completed`）——同一桶在两视图间折叠态同步（有意：跨视图一致的「不想看某桶」语义）。**不引入** `view:` 前缀分离。
  - 空桶：`showEmpty=false` → 0-run 桶不渲染 section（沿用 §10.9）；`showEmpty=true` → 显占位「暂无」。

### 2.4 顶栏瘦身（`ListTopBar` 改）
- **去 `StatusFilterChips`**：状态过滤由 KPI 带接管（§2.2），顶栏工具行不再渲染 `StatusFilterChips`。
- 工具行保留：搜索框（`flex-1 max-w-md`）+ 右侧 `[分组▾] [排序] [显示空 toggle] [看板|列表 toggle]`。
- 品牌行保留刷新 + 主题按钮（不变）。
- `StatusFilterChips.tsx` 处置见 §5 / §10（删前强制顺序，不留 coder 自决）；`StatusFilter` 类型供 `KpiStrip` 复用。

---

## 3. 视觉规范增量（沿用 `web-runlist-redesign.md` §2 速查表，以下为增量）

### 3.1 去半透明、实色化
- `CardGridSection` 容器**无半透明底**：不用 `bg-[rgb(var(--surface-2)/0.3)]`，改为靠 section 头左色条 + 卡片自身 `orca-bg-surface` 表达层次（section 不套背景容器，卡片直接落在 `orca-bg-app` 上）。
- 卡片背景 = 实色 `orca-bg-surface`；hover = `orca-bg-surface-2`（实色）。
- **禁** 在本 scope 组件新引入任何 `bg-[rgb(var(--surface|surface-2)/0.xx)]` 半透明**底色**。**例外清单（N4，允许的状态强调 tint，与 selected ring 同类）**：失败/待决策卡片的状态色 tint——`bg-orca-failed/5`、`border-orca-failed/40`、`ring-orca-skipped/30` / `ring-orca-skipped/20`（section）——属 `orca.*` palette 的 `/alpha`（非 arbitrary `bg-[rgb(var(--surface)/..)]`），**放行**；选中态 `bg-[rgb(var(--accent)/0.06)]` / active `bg-[rgb(var(--accent)/0.08)]` 同类放行。
- §3.1「全 token 半透明禁止」无法用单一 grep 区分「允许的 accent ring」与「禁止的 surface 底」，故该约束靠 **code-review 兜底**，不进 pass/fail grep 门（AC-B6 只检 surface/surface-2 底）。

### 3.2 卡片网格响应式
- `grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4`（断点：640/1024/1280px）。
- 卡片最小宽度由 grid 自适应保证，不设 `min-w-*`（避免触发横向滚）。

### 3.3 状态只画一遍（`BoardCard` / `RunRow`）
- **卡片/行 = 左竖条（`STATUS_BAR_HEX[rs]`，唯一状态色锚点）+ 行内文字 label（`STATUS_TEXT[rs]` 色 + `STATUS_LABEL[rs]` 文案）**。
- **停用 `StatusBadge` 的 dot**：BoardCard / RunRow 不再渲染 `<StatusBadge>`，改内联标签 `<span class="text-xs font-medium {STATUS_TEXT[rs]}">{STATUS_LABEL[rs]}</span>`（`STATUS_LABEL` / `STATUS_TEXT` 从 `status-badge.tsx` 新增 export，见 §5.1）。
- 左竖条 + 文字 label 二者**色源同一** `STATUS_BAR_HEX` / `STATUS_TEXT`，不再有列色条/列头 dot/StatusBadge dot 的三重重复。
- `STATUS_BAR_HEX` / `STATUS_DOT_BG` / `statusToRunStatus` 继续从 `status-badge.tsx` 导出复用（DRY 单出口）。

### 3.4 失败 / 待决策卡片提级配色
- **失败卡**（`rs === failed`）：左竖条红 + 整卡淡红边 `border-orca-failed/40` + 淡红底 `bg-orca-failed/5`（由 §3.1 例外清单放行）。
- **待决策卡**（`rs === blocked`）：左竖条紫（`STATUS_BAR_HEX.blocked`）+ 紫边 `ring-1 ring-inset ring-orca-skipped/30`（沿用现有）+ 第二行 `⚠ 等待 <fmtElapsed(elapsed)>`（紫，`text-orca-skipped`）。
- 运行中卡：左竖条蓝 + 进度条（`progress` 解析，沿用 `BoardCard.parseProgress`）。
- 已完成卡：左竖条绿，无额外强调。
- 排队卡：左竖条灰（`STATUS_BAR_HEX.queued`），无进度条。

### 3.5 圆角 / 阴影 / 字号
- 沿用 §2 速查表：卡片 `rounded shadow-sm`；KPI 胶囊 `rounded-full`；section 头 `text-sm`；卡片内 meta `text-xs tabular-nums orca-text-muted`；可读信息一律 `orca-text-muted` 不得 `orca-text-faint`。

---

## 4. 状态契约变更

### 4.1 status 桶顺序重排（`groupRuns`）
- **修订 `web-runlist-redesign.md` §10.8 的 status 桶顺序**：从 `排队 → 运行中 → 待决策 → 已完成 → 失败` 改为：
  > **运行中 → 排队 → 待决策 → 失败 → 已完成**
- 理由：监控关注项（运行/排队/待决策/失败）置前，`已完成`（量大、历史性）沉底，`失败` 提到 `已完成` 前避免被淹没。
- 实现：调整 `group-runs.ts` 的 `STATUS_BUCKETS` 数组顺序（`accept` 集合不变，仅顺序变）。
- `EMPHASIS_STATUSES`（running, blocked）/ `RING_STATUSES`（blocked）**语义不变，保留**。`LIMITED_STATUSES` / `COMPLETED_LIMIT` **删除**——限显统一由 §2.3 的 `SECTION_LIMIT=6` 接管所有 section（不再区分 completed/failed）。
- **代价说明（N10）**：completed 一眼可见数从 10 降到 6（多一次「展开剩余」点击）；换取 5 section 统一规则 + 垂直堆叠一屏更紧、失败/待决策更突出。可接受。

### 4.2 cost 全量移除（前端不显示、不排序、不聚合）
逐文件清单（coder 实现 grep 兜底，确保 runlist 无 `cost` 残留）：
- **`BoardCard.tsx`**：第三行 `cost · elapsed · event_count` → `fmtElapsed(elapsed) · {event_count} 事件 · fmtAgo(started_at)`。
- **`RunRow.tsx`**：删 `<Metric icon={Coins} value={fmtCost(run.cost)} label="花费" />`；其余 metric（进度/耗时/事件数）保留。
- **`ProjectGroup.tsx`**：`agg` 删 `cost` 累加；展开态聚合行删「`{fmtCost(agg.cost)} 总花费`」。
- **`sort-runs.ts`**（I2）：删 `case "cost"` 排序分支（`SortField` 删 cost 后 TS 强制，不删则编译断）。
- **`use-list-sort.ts`**：`SortField` 类型删 `"cost"`；`SORT_FIELDS` 数组删 cost 项。**持久化回退无新代码（AC-B9 澄清）**：现有 `readStored` 的 `SORT_FIELDS.some(...)` 校验（`use-list-sort.ts:48-50`）已自动拒绝旧 `field==="cost"` 值并回退默认——删 cost 项后该校验自动生效。
- **`format-helpers.tsx`**：`fmtCost` **全站 grep 确认零外部消费者后删除**（`grep -rn fmtCost orca/iface/web/frontend/src` 仅本 scope 文件命中即可删）；AC-B8 grep 以删后清零为验收。
- `RunSummary.cost` 类型字段**保留**（后端仍返回，不动类型；仅前端不消费）。

### 4.3 KPI 计数契约
- 见 §2.2；计数在 `RunListPage` 用 `useMemo` 从 `runs` 算，传给 `KpiStrip`。计数**不随过滤变化**（显全量分布），仅 active 态随当前 `status` filter 变。filter 同步（failed 含 cancelled）见 §2.2。

---

## 5. 组件清单

| 组件 | 动作 | 关键点 |
|---|---|---|
| `KpiStrip.tsx`（**新增**） | KPI 概览带 = 过滤器 | 4 状态胶囊 + 总数；计数 §2.2；点击 `setStatus`；失败>0 强提示；dot 用 `STATUS_DOT_BG`；`StatusFilter` 类型迁入此文件（§10 强制顺序） |
| `CardGridSection.tsx`（**新增**，替代 `BoardColumn`） | 分组 section：头 + 卡片网格 + 限显折叠 | 左色条 + label + 计数 + 收起；blocked 桶 section `ring-orca-skipped/20`；`grid grid-cols-1 sm:2 lg:3 xl:4`；默认显 6 + 展开剩余；搜索/过滤穿透 |
| `RunBoard.tsx`（**重构**） | 横向列 → section 垂直堆叠 | 去 `overflow-x-auto`（含模块头注释 L5，改为「section 垂直堆叠」描述）；`space-y-5`；渲染 `CardGridSection`；复用 `groupRuns`/`bucketHasBlocked`/`EMPHASIS_STATUSES`/`RING_STATUSES` |
| `BoardCard.tsx`（**重构**） | 卡片内容 | 去 `StatusBadge`→内联 label（§3.3，清 import+JSX+注释三处）；去 cost；加 `fmtAgo`；失败/待决策边色（§3.4）；保留 `parseProgress`/checkbox/delete/selected ring/testid |
| `ListTopBar.tsx`（**改**） | 去 `StatusFilterChips` | 工具行不再渲染状态 chips（KPI 带接管）；其余不变 |
| `RunRow.tsx`（**改**） | 去 cost + 状态只画一遍 | 删 Coins metric；`StatusBadge`→内联 label（§3.3，清三处） |
| `ProjectGroup.tsx`（**改**） | agg 去 cost | 删 cost 累加 + 「总花费」 |
| `use-list-sort.ts`（**改**） | 排序字段去 cost | `SortField` 删 cost；`SORT_FIELDS` 删项；持久化回退由现有校验闭环（无新代码） |
| `sort-runs.ts`（**改**） | 删 cost 排序分支 | 删 `case "cost"`（I2，否则 TS 编译断） |
| `group-runs.ts`（**改**） | status 桶顺序 + 删限长常量 | `STATUS_BUCKETS` 顺序重排（§4.1）；**删 `LIMITED_STATUSES` / `COMPLETED_LIMIT`**（限显统一 `SECTION_LIMIT=6`，定义在 CardGridSection） |
| `BoardColumn.tsx`（**删除**） | 不再被引用 | testid `board-column-<key>` → 由 `CardGridSection` 的 `card-section-<key>` 替代（§7），相关测试同步更新 |
| `RunListPage.tsx`（**改**） | 接入 KPI 带 + filter 同步 + forceOpen | 算 KPI 计数 `useMemo` → `<KpiStrip>`；`status==="failed"` 分支改 `rs==="failed"\|\|rs==="cancelled"`（§2.2）；`isBucketOpen` 加 `statusFilterActive` 分支（§2.3）；footer 全部展开/折叠门控放宽到 `visibleBuckets.length>=3`（两视图都显，§6.2） |
| `StatusFilterChips.tsx` | 删前强制顺序 | 见 §10：① 迁 `StatusFilter` 类型到 KpiStrip；② 迁其单测到 KpiStrip；③ grep 确认无其它 import；④ 才删文件。四步按序 |
| `status-badge.tsx` | 整文件保留（§0/§5.1） | 仅给 `STATUS_LABEL` / `STATUS_TEXT` 加 `export`；`StatusBadge` 函数保留（零消费者，作未来复用预留） |

### 5.1 `status-badge.tsx` 导出增量
- 新增 **`export const STATUS_LABEL`** 和 **`export const STATUS_TEXT`**（现为私有 const），供 BoardCard/RunRow 内联 label 复用（DRY）。
- `StatusBadge` 组件**保留**（零消费者，作未来详情页复用预留 + 常量出口，见 §0）；`STATUS_BAR_HEX` / `STATUS_DOT_BG` / `statusToRunStatus` / `RunStatus` 不变。

---

## 6. 交互规范

### 6.1 KPI 带 = 过滤器（替代 §6.3 状态 chips）
- 点状态胶囊 → `setStatus(filter)`；点「共 N runs」或重复点 active → `setStatus("all")`。
- KPI 计数始终显全量分布（不过滤）；active 态随当前 `status`。
- 失败胶囊 `failed>0` 时强提示色（§2.2），active 与否都红。
- **任一状态胶囊点击均 forceOpen 含数据 section**（I8 唯一解，见 §2.3 `isBucketOpen` 条件表达式）：`status !== "all"` 时覆盖持久折叠，含数据的 section 强制展开 + 限显放开；点 `kpi-chip-all` 退回持久折叠。这是对 §5.3（q-based 搜索穿透）的**扩展**（q **或** status 激活均 forceOpen），不再仅称「沿用 §5.3」。

### 6.2 section 限显 + 折叠
- 每个 section 默认显前 6 张；「展开剩余 X」本会话记忆（`expanded` state，不持久）。
- section 头「收起」→ 折叠整个 section，持久化走 `use-collapsed-buckets` 的 `"dim:key"`（board/list 共享，§2.3，AC-26 泛化）。
- 搜索态（`q` 非空）或过滤态（`status !== "all"`）→ section 强制展开 + 限显放开（显全部匹配）；section 头显命中数。
- **footer「全部展开/折叠」门控放宽（N5）**：从 `view === "list"` 改为 `visibleBuckets.length >= 3`（两视图都显），让 board 下也有折叠入口。

### 6.3 失败 / 待决策不被埋
- status dim 顺序：失败 section 在完成 section 前（§4.1）。
- 失败/待决策卡片边色提级（§3.4）；待决策 section ring（§2.3）。
- KPI 带失败/待决策计数高亮（§2.2）。
- project/workflow/time dim 下，失败/待决策靠卡片自身边色提级（组顺序仍按用户 sort）。

### 6.4 不变项（沿用现有）
- 视图切换 `[看板|列表]`（`view-toggle-board/list`，持久 `orca-runlist-view-v1`，默认 board=cardgrid）。
- 共享 selection / sort / search / theme / refresh / WS / GroupBy / ShowEmpty（§10.4 不变）。
- 三态加载（骨架/错误/空）、bulk bar、删除对话框、WS 重连（§5 不变）。

---

## 7. data-testid 锚点表（增量；未列者沿用 `web-runlist-redesign.md` §7）

| testid | 元素 |
|---|---|
| `kpi-strip` | KPI 概览带根 |
| `kpi-chip-<running\|blocked\|completed\|failed>` | KPI 状态胶囊（可点过滤） |
| `kpi-chip-all` | KPI「共 N runs」胶囊 |
| `card-section-<bucketKey>` | 卡片网格 section（替代旧 `board-column-<key>`） |
| `card-section-header-<bucketKey>` | section 头（可折叠） |
| `card-section-more-<bucketKey>` | 「展开剩余 X」按钮 |
| `board` | 看板根（`RunBoard`，**保留**） |
| `board-card` / `run-item` | 卡片（**保留**，内层 run-item 兼容 e2e） |

> **兼容性 / 迁移量（N8）**：`board-column-<key>` 删除；vitest `test/run-list-page.test.tsx` 现有 **17+ `board-column-*` + 1 `board-column-more-*` + 2 `status-chip-*`** 待改名（→ `card-section-*` / `card-section-more-*` / `kpi-chip-*`），e2e 同步。`board-card`/`run-item`/`view-toggle-*`/`group-by-*`/`show-empty-toggle` 保留，保既有测试绿。

---

## 8. 验收标准（AC，逐条可测）

### A. 形态与布局
- **AC-B1 看板重构（N9，grep 含注释清理）**：`view==="board"` 渲染 `RunBoard` = section 垂直堆叠（非横向列）；`grep -n "overflow-x-auto" components/runlist/RunBoard.tsx` 返回**空（含注释）**——RunBoard.tsx 模块头注释（L5 旧「flex gap-3 overflow-x-auto」）须随重构整段改写为「section 垂直堆叠」描述，注释内 `overflow-x-auto` 一并清除。
- **AC-B2 KPI 带（I3 + live-pending）**：顶部显 KPI 带，4 状态胶囊 + 总数；计数按 §2.2 正确（**失败计数 = count(failed) + count(cancelled)**；**运行 = count(running) + count(queued) + count(live-pending)**）；**4 胶囊计数合计 = total**（live-pending 不漏算）；计数不随 `q`/`status` 过滤变化。
- **AC-B3 KPI 即过滤（I3 + live-pending）**：点 `kpi-chip-failed` → 显 failed **与** cancelled run；点 `kpi-chip-running` → 显 running **与** queued **与** live-pending run；点 `kpi-chip-all` → `status="all"`；失败>0 时失败胶囊显红（即便非 active）。
- **AC-B4 卡片网格**：section 内 `grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4`；窄屏 1 列、宽屏 4 列；无横向滚动。

### B. 状态与配色
- **AC-B5 状态只画一遍（I5，grep 清三处含注释）**：`grep -n "StatusBadge" components/runlist/BoardCard.tsx components/runlist/RunRow.tsx` 返回**空**。实现须同时清除：① import 中 `StatusBadge`；② `<StatusBadge>` JSX；③ **注释中对 StatusBadge 的文字引用**（如 BoardCard.tsx:10「第一行：StatusBadge + workflow_name」改为「内联状态 label + workflow_name」）。状态由左竖条 + 内联 label 表达；`StatusBadge` 组件文件本身保留（§0/§5.1）。
- **AC-B6 去半透明（I1+N3，grep 收窄到 in-scope 文件）**：以下 grep 返回**空**：
  ```
  grep -nE 'var\(--(surface|surface-2)\)/0\.' \
    components/runlist/RunBoard.tsx \
    components/runlist/CardGridSection.tsx \
    components/runlist/BoardCard.tsx \
    components/runlist/KpiStrip.tsx
  ```
  **显式排除** `ProjectGroup.tsx` / `ListSkeleton.tsx`（其 `surface-2)/0.3` 属 list/骨架 scope，不在本 AC）；`accent)/0`（选中态 ring）允许，不被该串命中。§3.1 全 token 禁止靠 code-review 兜底（grep 无法区分允许的 accent ring 与禁止的 surface 底）。
- **AC-B7 失败/待决策提级（N4）**：失败卡 `border-orca-failed/40 bg-orca-failed/5`；待决策卡紫边 + `⚠ 等待`；status dim 顺序 running→queued→blocked→failed→completed（`groupRuns` 单测验顺序）。配色 class 见 §3.4，半透明 tint 已由 §3.1 例外清单放行。

### C. cost 移除
- **AC-B8 无 cost 显示**：`grep -rn "fmtCost\|\.cost" components/runlist/ components/runlist/sort-runs.ts hooks/use-list-sort.ts` 无显示性命中（`fmtCost` 删除后清零为验收）；BoardCard 底部 = `耗时 · N事件 · 相对时间`。
- **AC-B9 排序去 cost（澄清）**：`SortMenu` 下拉无「花费」项；`SORT_FIELDS` 无 cost；旧持久化 `field==="cost"` 回退 started_at——**由现有 `readStored` 的 `SORT_FIELDS.some(...)` 校验闭环，无新代码**（单测覆盖）。

### D. section 行为
- **AC-B10 限显折叠 + forceOpen（I8）**：section >6 卡时显「展开剩余 X」，点开显全部；section 头「收起」折叠并持久（`use-collapsed-buckets` `"dim:key"`，reload 保持）；搜索/过滤态强制展开 + 放开限显。**单测**：折叠 blocked section（`collapsed` 含 `"status:blocked"`）→ 点 `kpi-chip-blocked` → blocked section 展开（forceOpen 覆盖持久折叠）；点 `kpi-chip-all` → 退回持久折叠态。
- **AC-B11 空桶/分组/待决策高亮（I9）**：`showEmpty=false` 时空桶不渲染；GroupBy 切 status/project/workflow/time/none 正确分桶（AC-24）；待决策>0 section 根加 `ring-1 ring-orca-skipped/20`（§2.3），无论 showEmpty。

### E. 不回归
- **AC-B12 共享契约不回归**：board↔list selection 同步、sort 共享、bulk bar 两视图都显、视图持久、WS/刷新/三态加载 均不回归（沿用 AC-2/3/14/19/23）。board/list 折叠共享 `"dim:key"`（§2.3，有意同步）。
- **AC-B13 testid 兼容**：`board`/`board-card`/`run-item`/`view-toggle-*`/`group-by-*`/`show-empty-toggle` 保留；`board-column-*`/`status-chip-*` 删除并测试改 `card-section-*`/`kpi-chip-*`（§7 迁移量）。
- **AC-B14 R3 + 后端零改**：AC11 grep 绿；`git diff` 后端文件空（AC-18）。
- **AC-B15 视觉一致**：圆角/阴影/字号按 §2 档位；可读信息 `orca-text-muted` 非 faint；本 scope 新/改文件无 `bg-slate-*`/`rounded-lg/xl/2xl`/`text-[10/11/13px]`/裸 `shadow`。

---

## 9. 测试矩阵

- **vitest 组件** `test/run-list-page.test.tsx`（更新 board 用例 + **17+ testid 重命名** `board-column-*`→`card-section-*`、`status-chip-*`→`kpi-chip-*`）：
  - 默认看板 = section 堆叠（非横向列）；KPI 带计数正确（运行含 queued、**失败含 cancelled**）；
  - 点 `kpi-chip-failed` → 显 failed **与** cancelled；失败>0 胶囊红；
  - 卡片网格 grid class 落位；section >6 限显 + 展开剩余；section 折叠持久；forceOpen（点 kpi-chip-blocked 展开 collapsed 的 blocked section，AC-B10）；
  - BoardCard/RunRow 无 cost；无 `StatusBadge`（含注释）；失败/待决策边色 class；
  - status 桶顺序 running→queued→blocked→failed→completed。
- **vitest logic `group-runs.test.ts`（新建）最小断言集（N8）**：① status dim 桶顺序 = [running, queued, blocked, failed, completed]；② accept 集合（cancelled→failed、live-pending→queued）；③ project dim alpha + Legacy/其它垫底；④ workflow dim alpha + 其它垫底；⑤ time dim 5 桶逆序 + unknown 沉底；⑥ dim=none 单桶「全部」；⑦ `use-list-sort` 读 `field==="cost"` 回退 started_at（现有逻辑，单测覆盖）。
- **vitest grep 守门**（AC-B1/B5/B6/B8 的精确 grep，见 §8）。
- **Playwright e2e** `tests/iface/web/test_playwright_runlist.py`（更新 board 真机）：
  - 看板渲染 section（选择器从 `board-column-*` 改 `card-section-*`）；点卡进详情；切列表；
  - KPI 胶囊点击过滤真机（失败显 failed+cancelled）；分组[状态]/[项目] 切换真机；失败/待决策卡片可见性。
- **后端回归**：`pytest tests/iface/web/test_routes.py tests/iface/web/test_multi_run_phase_c.py -q` 绿（AC-18 旁证）。
- **不回归**：原有 AC-1~AC-17（除 §10 board 相关）+ AC-24~AC-26 测试保持绿。

---

## 10. 风险与边界

- **`StatusFilterChips` 删除强制顺序（N7，不留 coder 自决）**：① 先把 `StatusFilter` 类型迁到 `KpiStrip.tsx`；② 迁 `StatusFilterChips` 的单测到 `KpiStrip`；③ grep 确认无其它 import；④ 才可删 `StatusFilterChips.tsx`。四步须按序，spec-reviewer / code-reviewer 把关。任一步未完成则保留文件（仅从顶栏摘除）。
- **status 桶顺序变更影响**：改 `STATUS_BUCKETS` 顺序会影响所有依赖「第一桶=排队」的断言。§4.1 显式 supersede §10.8，§9 group-runs.test.ts 同步更新顺序断言。
- **持久化兼容**：`orca-runlist-sort-v1` 读到 `cost` 由现有 `readStored` 校验自动回退（AC-B9，无新代码）；折叠键 `orca-runlist-collapsed-v2` 不变（`dim:key` 格式不变，仅桶顺序变，旧 key 仍命中；board/list 共享，§2.3）。
- **配色 token 不改**：本 SPEC 配色优化 = 组件层实色化，**不动 index.css**。若用户后续要 Linear 级深色重调（改 token），另开 SPEC（影响全站）。
- **list 视图存废**：本 SPEC 保留 list 视图（仅去 cost + 状态只画一遍）。若用户后续要统一只留卡片视图、删 RunRow，另开 SPEC。
- **响应式最小适配**：卡片网格断点适配桌面三档；<640px 单列纵向滚（main 滚），不追求移动端完美（沿用 §11 边界）。
- **已知限制：`blocked` 死 UI（发现 1，架构裂缝，另开 draft）**：后端 `RunStatus` 无 `blocked`（只有 node 级 gate/interrupt 投影），故 KPI「待决策」永远 0、待决策 section 永远空、blocked 卡紫边永不触发——这是继承的既有前后端契约裂缝（原 Trello 看板同），非本重设计引入。修复方向见 [`run-blocked-status-design-draft.md`](./run-blocked-status-design-draft.md)（后端 `_summary_from_tape` fold gate/interrupt → run status=blocked）。本 SPEC 保留 blocked 相关 UI 与单测（mock 覆盖），待 draft 落地后自然激活。
- **out of scope**：`StatusBadge` 函数清理、`fmtCost` 全站清理（本 SPEC 已含 runlist 内）、index.css token 重调、移动端完整适配、modal scrim 全局统一——均不在本 scope。

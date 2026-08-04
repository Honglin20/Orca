# Release: Web 看板卡片网格重设计（2026-08-04）

> SPEC: `docs/specs/web-board-cardgrid-redesign.md` v1.1（spec-reviewer 对抗闭环：1 FATAL + 5 MAJOR + 8 MINOR + 5 NIT 全部并入）。
> Commit: `ca5c07a`。

## 做了什么

将 Web 主页 `/` 的看板视图从 **Trello 式横向 5 状态列** 重设计为 **「KPI 概览带 + 分组 section + 卡片网格」**。

### 形态变更

| 维度 | 旧 | 新 |
|------|-----|-----|
| 布局 | `flex gap-3 overflow-x-auto`（横向滚） | `space-y-5`（section 垂直堆叠，无横向滚） |
| 概览 | 无 | KPI 概览带（运行·待决策·失败·完成·共，可点过滤） |
| 状态表达 | 列色条 + 列头 dot + 卡左竖条 + StatusBadge dot（3-4 重） | 左竖条（唯一锚点）+ 行内文字 label（1 重） |
| 失败可见性 | completed/failed 列限显 10，失败被完成项压 | 失败 section 提前到完成前 + KPI 红提示 + 红边红底卡 |
| 卡片底色 | 半透明叠层发灰 | 实色 `orca-bg-surface`（去半透明） |
| cost | 随处显示（卡片/行/聚合/排序） | 全量移除（前端不显示/不排序/不聚合） |

### 关键组件

- **新增 `KpiStrip.tsx`**：4 状态胶囊 + 总数；计数不受 q/status 过滤影响（全量分布）；点胶囊=过滤；失败>0 红提示。
- **新增 `CardGridSection.tsx`**：section 头（可折叠）+ 卡片网格（响应式 1/2/3/4 列）+ 限显 6 + 展开剩余。
- **重构 `RunBoard.tsx`**：横向列 → section 垂直堆叠。
- **重构 `BoardCard.tsx`**：去 StatusBadge/cost，加 fmtAgo，失败红边/待决策紫边。

### 状态契约变更

- **status 桶顺序**：`排队→运行中→待决策→已完成→失败` → **`运行中→排队→待决策→失败→已完成`**（SPEC §4.1）。
- **filter 同步（I3）**：`status==="failed"` 过滤分支改为 `rs==="failed" || rs==="cancelled"`（与 group-runs failed 桶 accept、KPI 失败计数三者统一）。
- **forceOpen（I8）**：`status !== "all"` → 含数据 section 强制展开 + 限显放开。
- **LIMITED_STATUSES/COMPLETED_LIMIT 删除**：限显统一 SECTION_LIMIT=6。

## 偏差

无偏差——SPEC v1.1 三个 MAJOR 唯一解（I3/I8/I4）逐字照做。

## 验证

- **vitest**：98 pass（run-list-page 73 + group-runs 8 + grep-guards 17）。
- **后端回归**：`pytest tests/iface/web/test_routes.py tests/iface/web/test_multi_run_phase_c.py -q` 31 pass。
- **npm run build**：OK，static 产物已 commit。
- **grep 守门**：AC-B1/B5/B6/B8/B14 全绿。
- **AC-18 后端零改**：本 SPEC 内 backend 零改（routes/__init__.py + server.py 的 workflows 路由接入来自并行 WorkflowBrowsePage feature，非本 SPEC 产物）。

## code-reviewer 闭环

- 🔴 AC-B7 失败卡边色补测试断言。
- 🟡 forceOpen 态折叠按钮禁用（fail-loud：避免静默吞操作）+ `role="button"`+`aria-disabled`。
- 🟡 cancelled 视觉中性冲突 surface（代码注释说明设计意图：cancelled 灰条区分严重性）。
- 🟡 跨 dim blocked ring 集成测试补全。
- 🟡 ProjectGroup 注释清「总花费」。
- 🟢 KpiStrip failedAlert dot 分支简化（去死逻辑）。

# 2026-08-03 Web RunListPage 重设计（看板 + 列表）

> SPEC：[`docs/specs/web-runlist-redesign.md`](../specs/web-runlist-redesign.md)（spec-reviewer 闭环）
> E2E 真机证据：[`2026-08-03-web-runlist-e2e.md`](2026-08-03-web-runlist-e2e.md)
> Commits：`d782335`（主体）+ `1f8e5cd`（AC-4 折叠持久 regression）

---

## 1. 为什么

Web 主页 `/`（列出所有 workflow 的页面）使用体验差，用户痛点（代码逐条确认）：

1. 删除按钮极小（`Trash2 size=14` + `p-1.5` ≈ 26px）且 `opacity-0 group-hover:opacity-100`——触屏/无 hover 不可见。
2. 不能多选（全前端无 selection/checkbox）。
3. 不能排序（仅 `localeCompare` 项目名）。
4. **文件夹折叠刷新后自动展开**（`useState(defaultOpen)` 纯内存）。
5. 每个 project 显示简陋（chevron + 名 + mono project_id + ·N runs）。
6. （bonus）主题切换按钮坏的——只切本地 icon，从未调 `use-theme`。
7. 用户后续追加：**列表「不像看板」**，要一眼看清运行中/待决策。

## 2. 形态（用户确认）

- **默认 = 状态列看板**（排队 / 运行中 / 待决策 / 已完成 / 失败 五列；运行中/待决策列聚焦强调）。
- **列表为 toggle**（保留做批量清理/排序/全量浏览）。
- 两视图共用同一 store + 同一 selection `Set<run_id>` + 同一 sort/collapse 持久化。

## 3. 做了什么

**看板视图（SPEC §10，新增）**：`RunBoard` / `BoardColumn` / `BoardCard` + view toggle（`orca-runlist-view-v1` 持久，默认 board）；BoardCard 进度条（running）/ 等待时长（blocked）/ cost·elapsed·事件；已完成·失败列限长 10 + 显示更多。

**列表视图（重写）**：双行顶栏（品牌行 `h-12` 跨页一致 + 工具行 `h-10`）；无 border 分组容器（`surface-2/30` 底 + 左侧 `STATUS_BAR_HEX` 色条）；项目头美化（folder icon + 聚合：运行中/待决策 mini pill + 总花费 + 最近活动）。

**管理功能（痛点 1-3）**：多选（行/分组/全选三级 + Shift 范围选，选择跨分组、跨视图同步、切排序/筛选保留）；排序（6 字段 + 方向 + 持久 + stable + 组序 tiebreaker）；批量删除（逐条乐观 + 独立回滚 + 部分失败 refresh 对账 + 聚合 toast）；单条删除按钮放大常显（`size=16` + 命中区≥32px）。

**持久化（痛点 4）**：折叠态 + 排序态 + 视图态 三类 localStorage（版本 key + 损坏降级 + 惰性清理）。

**主题修复（痛点 6）**：`ListTopBar` 主题按钮改调 `use-theme` 的 `setTheme/nextTheme/currentTheme`。

**鲁棒性（D1/D3 审查 + spec-reviewer evaluator 闭环）**：
- WS 断线指数退避重连 + 重连成功计数归零 + 非阻塞提示（fail-loud，不静默断流）。
- refresh `inflightSeq` 守卫（防 stale 覆盖 fresh）。
- `pendingDeletes` 防「幽灵 run」（删除期间 WS/refresh 不复活；NM4 成功 id 延迟到 refresh 确认落盘才清）。
- `reset()` epoch 守卫（防 unmount 后 stale DELETE 写回）。
- `DeleteConfirmDialog` focus trap（Esc/Enter/Tab wrap/焦点恢复/背景 `inert`/`aria-describedby`）。
- 三态加载（骨架/error banner/空态，防首屏误显「暂无 run」）；搜索/待决策**穿透折叠**（结果不被折叠分组埋掉）。
- fail-loud：refresh error 显式渲染；非 JSON 帧 `console.warn`；删除失败 toast（废 `alert`）。

**视觉系统（D2 速查表，照抄）**：圆角/阴影/字号各 3-4 档系统化；可读信息强制 `orca-text-muted`（faint 亮模式 2.8:1 不达 AA）；modal 遮罩主题感知 `bg-[rgb(var(--text)/0.4)]`（不新增 `bg-slate-*`）；状态色 DRY 单出口（导出 `STATUS_DOT_BG`）。

## 4. 约束（全闭环）

- **前端唯一**：`git diff -- orca/iface/web/routes run_manager.py server.py ws_handler.py` 空（AC-18，后端回归 43 passed 旁证）。
- **零新依赖**：仅 React 19 + Tailwind v3 + zustand + lucide-react + react-router v6。
- **R3 不违**：`run-list-store` 不 import `workflow-store`（AC-17 grep 守门 + vitest 双绿）。
- **不改 `index.css` token**；Tailwind alpha 陷阱（自定义 `orca-bg-*` utility 不支持 `/alpha`）全文走 arbitrary。

## 5. 过程（用户指定的多 agent 管线）

1. 并行审查：D1 缺陷 / D2 视觉美术 / D3 UX 三 agent → 严重度分级 findings。
2. 综合 + D4 自查 → SPEC `docs/specs/web-runlist-redesign.md`。
3. spec-reviewer 对抗审查（baseline + evaluator 辩论）→ 2 FATAL + 多 MAJOR 闭环（含 `STATUS_DOT_BG` 未导出、`--skipped` CSS 变量不存在、`project_path` 不在 API）。
4. coder-agent 实现（列表 + 看板 + 闭环 evaluator 缺口）→ 自带 code-reviewer（0 FATAL/4 MAJOR/3 MINOR 全修）→ commit `d782335`。
5. test-agent 真机 E2E（WSL+venv 跑 Python 后端 + chromium）：后端回归 43 passed / Playwright 9 passed / vitest 437 passed。

## 6. E2E 抓到的真实 bug（AC-4，已修）

Playwright 真机发现 vitest 漏掉的 **AC-4 折叠持久 regression**：reload 后 `/api/runs` 未回时 `knownProjects` 为空 → 把持久态 `["demo"]` 过滤成空 → write-back 覆盖清空 storage → 折叠永久丢失（正是用户痛点 4）。**这正是真机 E2E 的价值**。修为 hydration 模式（初值空 → `known` 首次非空一次性 hydrate → 仅 hydrate 后允许 write-back）+ 反向回归用例。

## 7. 验证

| 层 | 结果 |
|---|---|
| `tsc --noEmit` | 0 错 |
| `vitest run` | **437 passed**（含 53 run-list 用例覆盖 AC-1..AC-23 intent） |
| `vite build` | 成功（static/ 产出） |
| 后端回归 `pytest tests/iface/web/test_routes.py test_multi_run_phase_c.py` | **43 passed**（AC-18 旁证） |
| Playwright 真机 `test_playwright_runlist.py` | **9 passed**（默认看板/五列/点卡进详情/toggle/多选+批量删/排序/折叠 reload/主题/搜索穿透） |
| R3 grep | 无命中 |
| 后端 `git diff` | 空 |

## 8. 受影响文件

- 新建 `orca/iface/web/frontend/src/components/runlist/`（RunBoard/BoardColumn/BoardCard/RunRow/ProjectGroup/ListTopBar/SearchInput/StatusFilterChips/SortMenu/BulkActionBar/DeleteConfirmDialog/EmptyState/ListSkeleton/ErrorBanner/StaleProjectsSection/format-helpers/sort-runs）。
- 新建 hooks：`use-collapsed-projects`/`use-list-selection`/`use-list-sort`/`use-ws-runlist`/`use-runlist-view`。
- 改：`RunListPage.tsx`（薄页壳 + view 分支）、`run-list-store.ts`（deleteRuns/inflightSeq/pendingDeletes/epoch）、`status-badge.tsx`（export `STATUS_DOT_BG`）。
- 新测试：`test/run-list-page.test.tsx`、`test/run-list-store.test.ts`、`tests/iface/web/test_playwright_runlist.py`。
- SPEC：`docs/specs/web-runlist-redesign.md`。

## 增量（同日追加）：分组维度 + 空桶隐藏（SPEC §10.8-10.10）

用户反馈：要更多分区方式（含按项目）+ 质疑排队/待决策空列。增量交付（commit `7cd8328` + `13f60d5`）：

- **分组方式选择器** `GroupBySelector`（替换旧 groupBy on/off toggle）：不分组 / 状态 / 项目 / workflow / 时间 五维度。看板列 / 列表段随 dim；持久 `orca-runlist-groupby-v1` 默认 `status`。共享 `groupRuns` 单出口（DRY）。
- **空桶自动隐藏** `ShowEmptyToggle`：默认隐藏 0-run 桶（直接解决排队/待决策空列噪音）；「显示空」可开；**待决策桶 >0 时高亮**（保证 gate 待处理不被埋，无论 showEmpty）。
- **折叠持久泛化** `use-collapsed-buckets`：`Set<"dim:key">` + key 升级 `v2`；切 dim 各自独立折叠态。
- 回答「排队/待决策有啥用」：排队是 `start_run` 瞬态 / 并发>3 溢出（轻量用户几乎不见）；待决策是 gate 等你 `human_decision`（出现=最该处理）。空则隐藏、有则突出。

**验证**：vitest 456 passed（+19，含 AC-24/25/26 + cross-dim 折叠 bug 回归）/ Playwright 真机 10 passed / 后端回归 31 passed / tsc 0 错 / R3 无命中 / 后端 diff 空。code-reviewer 闭环（含揪出的 cross-dim 折叠擦写 bug）；E2E 抓到并修了 stale 五列用例（对齐空桶隐藏）。

## 9. 已知 follow-up（非本次 scope）

- `tests/iface/web/conftest.py::live_server` 不隔离 `ORCA_HOME`——在真实开发机（千级 runs）跑会拖慢 `/api/runs?scope=all`。建议加 fixture 级 env 隔离。
- 暗模式 `orca.failed` 色 AA 对比度收口、modal 遮罩全局统一化（AgentsRail 等遗留 `bg-slate-900/40`）——标 P0b。

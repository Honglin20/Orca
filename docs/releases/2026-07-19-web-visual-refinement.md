# Release：Web 界面视觉优化（P0–P4）

> 2026-07-19。范围：**纯前端**（`orca/iface/web/frontend/`），后端 Python 零改，全部 testid 与功能接口保留。
> 计划：[`docs/plans/2026-07-18-web-visual-refinement.md`](../plans/2026-07-18-web-visual-refinement.md)（rev 2，spec-reviewer conditional-pass）。
> SPEC amendment：[`docs/specs/2026-07-18-web-visual-refinement-amendment.md`](../specs/2026-07-18-web-visual-refinement-amendment.md)（§7 暗色双触发 + §1.1 WS sanctioned exception）。

## 动因

P5b token 已定义但迁移不彻底（179 hits / 34 files 硬编码色）、品牌强调色被 indigo/emerald/red 稀释、图标全靠 emoji（跨平台渲染差异）、暗色无手动开关、TopBar 无 WS 连接指示、左栏与中栏 surface 割裂。骨架扎实（单 store / 单一真相源 / 三栏 / 虚拟化 / bundle split）但视觉层缺统一 design language 收口。

## 阶段

### P0 — Token 收口（`644cc4f`）
消除全部硬编码 `slate-*/indigo-*/emerald-*/red-*`，统一走 `orca-*` utility + `orca.*` palette。`statusColor()` + `STATUS_ICON` 抽到 `status-style.ts`（DRY 出口）。验收按**白名单**（非黑名单清零）：`NODE_STATUS_HEX` / `PALETTE` / `LogStream LEVEL_TEXT_COLOR` / `DiffView` diff 语义色 4 处合法保留。179 → 26 hits 全白名单。堵住暗色塌陷。

### P1 — lucide 统一图标库（`a8c6a3e`）
装 `lucide-react@^0.460`，建 `components/icons.tsx`（`StatusIcon` 单一入口，继承 currentColor）。全量替换 src emoji：状态 `●✓✗⊘⏸` / `⏱💭🔤⟳` / `▸▾` chevron / `💬🔒🔧` / `▶⊘` / `📊◆` / `⏵↻` / `✗` → lucide 细线条（strokeWidth 1.5）。**唯一保留** `▎` 流式光标（SPEC §5.3 文本契约）。test oracle 迁移：topbar status 5 档断言 → `svg + text-orca-*` className；elapsed `⏱` / agents-rail `⏱startsWith` / `🔤` → svg 断言（行为不变）。

### P2 — AgentsRail 增量增强（`a577367`）
元信息重排为单行（status · elapsed · tokens，flex-wrap 防溢出）；新增 testid `agent-status` / `agent-tokens`，`agent-elapsed` testid 保留（pending 空字串契约）；running 行末尾流式 `▎`；selected 行左色条加粗（1→4px）；stall 配色收敛 `orca-text-faint`。定位为 prior P3 的**增量**（非重做：分组/色条/折叠/迭代已落地）。

### P3 — TopBar 增强 + 暗色开关 + WS 指示（`13d0e1f`）
- **WS 连接指示**（D1=A）：新建 `ws-connection-store.ts`（module-level zustand，SPEC §1.1 sanctioned exception——transport-only，非 tape 真相）+ `use-ws-status`；`use-websocket` 内部 onopen/onclose/onerror 写连接态（不改 void 签名，后端零改）；TopBar 连接点（绿 connected / 紫 reconnecting / 红 disconnected）。
- **暗色开关**（SPEC §7 双触发）：`use-theme.ts` 三态（system/dark/light）+ localStorage 持久化；`index.css` 加 `:root.dark` / `:root.light`（specificity (0,2,0) > `@media :root` (0,1,0)，显式覆盖系统）；App 根 `initTheme()` 减 FOUC。
- **TopBar**：runId 可复制（Copy/Check 反馈，fail loud）+ status badge + 主题 toggle（Sun/Moon/Monitor）。新增 testid `top-runid` / `top-ws` / `theme-toggle`。

### P4 — 三栏 surface 层级统一（`617d991`）
左栏 aside 底色 `orca-bg-surface-2 → orca-bg-app`（三栏统一页面底，治「左灰中白割裂」；卡片 `orca-bg-surface` 分层）；Panel 内首层容器去 border（`border-r` / `border-l`），栏间分隔交给 `PanelResizeHandle`（防 resize 双线）。

## 验证

- 每阶段 `npm run build`（`tsc --noEmit && vite build`）PASS + 前端 `test/` PASS。
- 最终全量：**318 passed / 1 pre-existing flaky**（`agents-rail.test.tsx:202` DAG lazy——P0 已验证基线 `644cc4f` 也失败，jsdom 不解析 React.lazy chunk，非本批引入，留 follow-up）。
- 硬约束闭环：后端 Python 零改（grep）；testid 只加不删（99 → 105）；store/selectors/WS/props 契约不变。

## follow-up

- **DAG compact minimap（原 P5）**：常驻左栏缩略图，需先开 SPEC amendment 修订 §5.7「DAG 不在三栏常驻」。
- **DAG lazy test flaky**：jsdom 环境 React.lazy 解析问题，独立修。
- **blocked reason「waiting on B」**：tape 无 `blockers` 字段，待后续 SPEC。

# 计划：Web 界面视觉优化（P0–P5）

> 日期：2026-07-18
> 范围：**仅 `orca/iface/web/frontend/` 前端**。**严禁后端任何变更**（`server.py` / `routes/` / `ws_handler.py` / store 后端逻辑 / Python 一行不改）。
> 目标：在保留**全部现有功能与公开接口**（UI testid、WS 协议、REST 端点、store action/字段、selectors 契约）的前提下，统一视觉语言、收口 design token、补齐品牌一致性与交互完整度。

---

## 0. 硬约束（不可违反）

1. **纯前端**：只动 `frontend/src/**`（含 `index.css` / `tailwind.config.js` / `package.json`）。后端目录 `orca/iface/web/{server.py,run_manager.py,ws_handler.py,routes/,static/（构建产物，由 build 重生成）}` 不手改。
2. **保留 testid**：`data-testid` 是 E2E 契约（react-dom 测试 + test-agent 真机），**只能加不能删不能改**。重做组件时原 testid 必须迁移到新结构对应位置。
3. **保留功能接口**：store action/字段、selectors 签名、WS message 形状、组件 props 全部不变。纯 className / 结构 / 图标层面的重做。
4. **Tailwind v3 不升 v4**（SPEC §1 既定）。
5. **SDD / Clean Code**：每阶段独立 commit；过期代码（被替换的死 className、注释掉的旧逻辑、不再 import 的旧图标）即时清理；fail loud。
6. **每阶段必须 `npm run build`（`tsc --noEmit && vite build`）通过 + 既有前端测试通过**才 commit。

---

## 1. 现状基线（探索已确认）

- Design token 已在 `src/index.css:25-72` 定义明暗双套（`--app-bg/--surface/--surface-2/--border/--text/--text-muted/--text-faint/--axis-tick/--accent`），`.orca-*` utilities 在 `index.css:103-134`。
- `tailwind.config.js:16-24` 已暴露 `orca.{pending,running,done,failed,skipped,accent}`，但注释承认「当前未被任何组件直接使用」——是迁移钩子。
- 已知硬编码热点（file:line）：
  - `components/layout/TopBar.tsx:33-40` `statusColor()` 硬编码 `text-red-600 / text-emerald-600 / text-slate-500 / text-amber-600`。
  - `components/layout/AgentsRail.tsx:105,113,120,125,143,156-157,161,166,174,185,193,198,209,220,237,244,247` 大量 `bg-slate-50 / border-slate-200 / text-slate-* / bg-white / text-amber-600`。
  - `components/gate/AskGate.tsx:57,59,62,70-74,90,100,111` 硬编码 `bg-white / border-slate-200 / text-slate-* / border-indigo-400 / bg-indigo-50 / text-indigo-900 / bg-indigo-600`。
  - `components/graph/nodes/NodeShell.tsx:25,31,40` `bg-white / bg-slate-100 / text-slate-*`（DAG 节点暗色塌陷）。
  - 其余散点：`PermissionGate`、`ErrorBlock`、`ThinkingBlock`、`ToolRow`、`NodeOutputBlock`、`LogStream`、`ChartsView` —— 由 P0 grep 全量清点。
- 图标全靠 emoji/Unicode（`●✓✗⊘⏸ 💭 🔒 💬 🪙 ⏱ ⟳ ▎ 🔤`），无图标库。
- 暗色只跟随 `prefers-color-scheme`，无手动开关。
- DAG 是浮层（`AgentsRail.tsx:263`），非常驻。
- TopBar 无 WS 连接指示、无 runId 复制。

---

## 2. 分阶段实施

每阶段：实施 → 自我 review（依赖/越界/DRY/fail loud）→ `npm run build` + 既有测试 → 清理过期代码 → commit。

### P0 — Token 收口（基础，低风险）

**目标**：消除全部硬编码 slate-*/indigo-*/emerald-*/red-* 等，统一走 `orca-*` utility + `orca.*` palette。堵住暗色塌陷。

**步骤**：
1. 全量 grep 清点硬编码：`grep -rnE "(bg|text|border)-(slate|indigo|emerald|red|amber|violet|sky|blue|green|rose|neutral|gray|zinc)-[0-9]" src/`。产出清单。
2. 按语义映射替换（**不改视觉色相**，只改来源为 token）：
   - 容器底色 `bg-white` → `orca-bg-surface`；`bg-slate-50` → `orca-bg-surface-2`；页面底 → `orca-bg-app`。
   - 边框 `border-slate-200` → `orca-border`（含 `border-r orca-border`）。
   - 文字 `text-slate-900/800` → `orca-text`；`text-slate-700/600` → `orca-text-muted`；`text-slate-400/500` → `orca-text-faint`。
   - 状态文字色走 `orca.*` palette：`text-orca-done / text-orca-failed / text-orca-running / text-orca-pending / text-orca-skipped`。
   - `AskGate` 的 indigo 全部 → `orca-accent / orca-bg-accent / orca-border-accent`（选中态 `border-orca-accent bg-[rgb(var(--accent)/0.08)]`）。
3. `TopBar.statusColor()` 改返回 `text-orca-*`；同时把 `statusColor` 与 `STATUS_ICON` 抽到 `src/components/layout/status-style.ts`（DRY，供 AgentsRail / DAG / 未来 minimap 复用）。
4. `NodeShell` DAG 节点 `bg-white` → `orca-bg-surface`，内部 `text-slate-*` → token。
5. **清理**：grep 后无引用的旧色类删除；`tailwind.config.js:9` 那条「当前未被任何组件直接使用」注释更新为「已启用」。

**验收**：`grep -rnE "(bg|text|border)-(slate|indigo|emerald|red|amber|violet)-[0-9]" src/` 应仅剩 graph/constants.ts 的 `NODE_STATUS_HEX`（hex 字符串，是状态色单一真相源，**保留**）和 chartTheme.ts 的 PALETTE（图表色单一真相源，**保留**）。暗色模式下三栏无白卡片塌陷。testid 全保留。

**commit**：`style(web): P0 token 收口——消除硬编码色，统一 orca-* 语义`

---

### P1 — 图标库 + 状态符号统一

**目标**：消除 emoji 图标依赖，引入 `lucide-react`，统一细线条（`size={14} strokeWidth={1.5}`）风格，可继承语义色。

**步骤**：
1. `npm i lucide-react`（确认与 React 19 兼容版本）。新增依赖仅此一个。
2. 建 `src/components/icons.tsx`：集中导出项目用到的图标实例包装（统一 size/strokeWidth），并导出 `StatusIcon({status})` 组件，把 `STATUS_ICON` 字符映射替换为 `<Circle/CircleDot/CircleCheck/CircleX/CirclePause>` 等。
3. 替换点：
   - TopBar status：`STATUS_ICON` emoji → `<StatusIcon/>`。
   - AgentsRail：`⏱` → `Timer`、`💭/思考中` → `Brain`（stall）、`🔤` → `Coins`（tokens）、`⟳` → `Loader`（progress，spin-slow）、`▸/▾` → `ChevronRight/ChevronDown`。
   - AskGate 标题 `💬` → `MessageSquare`；PermissionGate `🔒` → `Lock`。
   - LogStream level 图标（若 emoji）→ 对应 lucide。
4. 流式光标 `▎`（SPEC §5.3 契约）**保留 Unicode**（它是文本光标不是图标，且测试可能断言）。
5. **清理**：删除被替换的 emoji 字面量；若 `STATUS_ICON` 常量不再被引用则删。

**验收**：`npm run build` 通过；bundle 增量可控（lucide 按需 tree-shake）；testid 全保留；E2E 若有断言 emoji 文本的测试（需 grep 测试目录确认）——若有则同步更新测试（行为 = 显示图标，意图不变）。

**commit**：`feat(web): P1 引入 lucide 图标库，统一细线条风格`

---

### P2 — 左栏 AgentsRail 视觉重做

**目标**：提升左栏信息层级与可读性（现状最弱处）。testid **全保留**。

**设计**（见计划 §3 mockup）：
- 状态条：左 3px 竖色条（`NODE_STATUS_HEX`，DRY 不变）+ 行首 `<StatusIcon/>`（替代纯色条单调感，色条 + 图标双编码）。
- agent 名一级字（`orca-text`，`text-sm`），元信息二级字（`orca-text-faint`，`text-[10px]` tabular-nums）。
- 元信息行重排为：`{status} · {elapsed} · {tokens}`，用 `·` 分隔，单行紧凑。
- running 行：末尾追加流式 `▎`（与 ConversationView 流式光标同语义），让左栏"活"。
- stall「思考中 Ns」用 `orca-text-faint` + `Brain` 图标（去 amber 硬编码，stall 不必那么扎眼，靠图标语义）。
- selected 行高亮：`orca-bg-surface-2` + 左色条加粗到 4px（替代 `bg-slate-100`）。
- subs 折叠 chevron 用 lucide。

**约束**：
- 不改 `selectAgentGroups / selectNodeElapsed / selectStall / selectNodeSessions / formatTokens` 签名。
- `data-testid` 清单（`agent-row-/agent-bar-/agent-iter-/agent-elapsed-/agent-stall-/agent-fold-/agent-sub-/agent-group-`）逐个迁移到新结构。
- P0 已把底色换 token，本阶段专注布局与层级。

**验收**：所有原 testid 仍在；`npm run build` + 前端测试通过；暗色下左栏与中栏 surface 层级清晰无割裂。

**commit**：`style(web): P2 AgentsRail 视觉重做——层级/状态双编码/流式`

---

### P3 — TopBar 增强 + 暗色开关 + WS 连接指示

**目标**：补齐 TopBar 完整度；加手动暗色开关。

**步骤**：
1. **runId 可复制**：runId span 加 `onClick` 写剪贴板（`navigator.clipboard.writeText`），hover 显 `Copy` 图标，成功 toast 1s（不引入 toast 库，纯本地 state）。testid `top-runid`（新增，不删既有）。
2. **status badge**：把纯文字色 status 改为 badge：`rounded px-1.5 py-0.5 text-xs` + `bg-[rgb(var(--status)/0.1)] text-orca-{status} border border-[rgb(var(--status)/0.3)]`。保留 `top-status` testid + 文字内容（E2E 断言不变）。
3. **WS 连接指示**：右上加连接状态点（connected 绿 / reconnecting 琥珀 / disconnected 红），订阅 store 的 WS 状态字段（若 store 无此字段——**不加后端**，改由前端 ws client 本地 state 暴露一个 `useWsStatus()` hook，纯前端心跳/事件派生）。testid `top-ws`。
   - ⚠️ 若现有 store 已有 connection state 则复用；若无，新增 hook 挂在 `ws_handler` 的前端镜像层（纯前端，不动后端 ws_handler.py）。
4. **暗色开关**：TopBar 右上 `Sun/Moon` toggle，三态 `light|dark|system`，持久化 `localStorage`，通过给 `<html>` 加/去 `dark` class + 调整 `index.css`（把 `@media (prefers-color-scheme: dark)` 的暗 token 复制到 `.dark` 作用域，或改用 `:root.dark` 覆盖）。新建 `src/hooks/use-theme.ts`。testid `theme-toggle`。
   - 这是本阶段唯一需要改 `index.css` 暗色作用域的点：从 `@media prefers-color-scheme` 增强为「`@media` + `.dark` class 双触发」，**保留** system 跟随默认行为（无 class 时仍跟系统）。

**验收**：暗色开关可切且刷新保持；WS 断连模拟（关后端）指示变红（手动验）；testid 全保留 + 新增；后端零改。

**commit**：`feat(web): P3 TopBar 增强（复制/badge/WS指示）+ 暗色开关`

---

### P4 — 三栏 surface 层级统一（治割裂）

**目标**：消除「左白 / 中灰割裂」，三栏 surface 连续，靠 border + surface-2 分层而非异色底。

**步骤**：
1. 统一三栏底色为 `orca-bg-app`（页面底），栏内卡片用 `orca-bg-surface`，分隔靠 `orca-border` 1px 竖线（不再用一栏白一栏灰制造层次）。
2. 中栏 tabs active 下划线、ConversationView 容器、LogStream 容器统一走 surface token。
3. 检查 `RunDetailPage.tsx` PanelGroup 各栏 className，确保一致。
4. **清理**：P0 之后残留的 `bg-slate-50/100` 异色底（若 P0 已清则本阶段为查漏补缺）。

**验收**：三栏视觉连续无色块割裂；暗色一致；testid 不变。

**commit**：`style(web): P4 三栏 surface 层级统一，消除割裂`

---

### P5 — Compact DAG Minimap（可选探索）

**目标**：DAG 由「纯浮层」补一个「左栏底部常驻缩略图」模式，结构感常驻，当前运行节点高亮。点击仍展开全屏浮层。

**步骤**：
1. 复用 `@xyflow/react` 的 `<MiniMap>` 或自绘简化拓扑（节点小方块 + 状态色），挂在 AgentsRail 底部 collapsible 区。
2. 当前运行节点（`status===running`）高亮 + 脉冲。
3. 不破坏现有 `[DAG]` 全屏浮层（保留 `dag-toggle` / `dag-overlay` testid）。
4. 新增 `dag-minimap` testid。

**约束**：复用 `WorkflowTopology`（已有），不新增后端字段；MiniMap 走同一 lazy chunk。

**验收**：minimap 渲染 + 运行节点高亮；全屏浮层仍可用；`npm run build` + 测试通过。

**commit**：`feat(web): P5 DAG compact minimap 常驻缩略图`

---

## 3. AgentsRail 重做 mockup（P2 参考）

```
┌──────────────────────────┐
│ Agents            [DAG]  │
├──────────────────────────┤
│ SETUP                    │
│ ● research-agent    ▎    │  ← running：StatusIcon实心 + 流式▎
│   running · 3.1s · 8.2k  │     元信息单行 · 分隔
├──────────────────────────┤
│ LOOP        R2           │
│ ✓ train-script           │  ← done：绿对勾
│   done · 12.4s · 1.2k    │
│ ○ review-agent          │  ← pending：灰空心
│   blocked · waiting on B │
│   ▸ 3 subs               │  ← chevron 折叠
└──────────────────────────┘
```

---

## 4. 过期代码清理清单（每阶段执行）

- 被替换的旧 emoji 字面量、`STATUS_ICON` 常量（若 P1 后无引用）。
- `tailwind.config.js:9` 过期注释。
- 任何 P0 替换后不再被引用的 className 组合。
- 死 import（如替换 emoji 后未用的 React state）。
- 要求：每阶段 commit 前跑 `tsc --noEmit`（build 内含）确保无 unused import 报错（tsconfig 严格）。

---

## 5. 自我 Review 清单（每阶段 commit 前）

- [ ] 依赖铁律：未碰后端；未引入 store → 后端反向调用。
- [ ] 无职责越界：UI 组件没塞业务逻辑（WS 指示纯前端派生）。
- [ ] DRY：状态色单一真相源（`NODE_STATUS_HEX` + `orca.*` palette），图标单一入口（`icons.tsx`）。
- [ ] fail loud：clipboard 失败、theme localStorage 失败有 console.error，不静默吞。
- [ ] testid：原 testid 全保留，新增有命名规范。
- [ ] `npm run build` + 既有前端测试通过。

---

## 6. 完成后收尾

- 写 release note `docs/releases/2026-07-18-web-visual-refinement.md`。
- CHANGELOG 加索引（每阶段 1-2 句 + commit SHA）。
- CURRENT.md：本任务是 in-session 主线之外的支线，不在 CURRENT.md 主任务块登记；在 release note + CHANGELOG 留痕即可（避免污染 in-session CURRENT 快照）。
- 截图沉淀：`docs/assets/web/2026-07-18-*.png`（每阶段一张，补「零设计资产」缺口）。

# 计划：Web 界面视觉优化（P0–P4）

> 日期：2026-07-18（rev 2，2026-07-19 按 spec-reviewer conditional-pass 修订）
> 范围：**仅 `orca/iface/web/frontend/` 前端**。**严禁后端任何变更**（`server.py` / `routes/` / `ws_handler.py` / `run_manager.py` / store 后端逻辑 / Python 一行不改）。
> 目标：保留**全部现有功能与公开接口**（UI testid、WS 协议、REST 端点、store action/字段、selectors 契约），统一视觉语言、收口 design token、补齐品牌一致性与交互完整度。

---

## 0. 硬约束（不可违反）

1. **纯前端**：只动 `frontend/src/**`（含 `index.css` / `tailwind.config.js` / `package.json`）。后端目录 `orca/iface/web/{server.py,run_manager.py,ws_handler.py,routes/}` 不手改。`static/` 是构建产物，由 `npm run build` 重生成。
2. **保留 testid**：`data-testid` 是 E2E 契约（react-dom 测试 + test-agent 真机），**只能加不能删不能改**。重做组件时原 testid 必须迁移到新结构对应位置。
3. **保留功能接口**：store action/字段、selectors 签名、WS message 形状、组件 props 全部不变。纯 className / 结构 / 图标层面的重做。
   - **Exception（P3 已拍板 D1=A）**：新建 `src/hooks/ws-connection-store.ts` 是 **module-level 独立 zustand**，不属于 `workflow-store`（tape 真相源），不违反本约束。SPEC §1.1 加 sanctioned exception：WS connection state 是 transport-only，非 tape 真相。
4. **Tailwind v3 不升 v4**（SPEC §1 既定）。
5. **SDD / SPEC 驱动**：若某阶段改动与 SPEC 冲突，**先改 SPEC 文档（amendment）再实现**，不允许 plan 直接违反 SPEC。SPEC 文档（`docs/specs/*.md`）不是后端代码，属允许范围。
6. **Clean Code**：每阶段独立 commit；过期代码即时清理；fail loud。
7. **每阶段必须 `npm run build`（`tsc --noEmit && vite build`）通过 + 既有前端测试（`test/`）通过**才 commit。

### supersedes 关系声明

本 plan 与 prior [`docs/specs/2026-07-18-web-presentation-refinement.md`](../specs/2026-07-18-web-presentation-refinement.md) 的关系：
- 本 plan **P0** ⊇ prior P5b（token 迁移）——prior P5b 是渐进起点，本 plan P0 收口完成。
- 本 plan **P2** = prior P3 的**增量增强**（非重做）：prior P3 已落地 selectAgentGroups 分组 / 左竖色条 / 子 agent 折叠 / R3 迭代；本 plan P2 只替换 emoji 为 lucide + 元信息 span 重排 + running 流式光标。
- 本 plan **P3**（暗色开关 + WS 指示 + TopBar 增强）= 新增。
- 本 plan **P4**（三栏 surface 统一）= prior P5「左白中灰割裂」的收口。
- prior P5a（去 cost span）**已完成**，本 plan 不涉及。
- prior P5c（DAG 常驻 minimap）**剥离为 follow-up**（D2），不在本 plan。

→ 实施时 prior SPEC 顶部标注「partial-superseded by 2026-07-18-web-visual-refinement.md（plan）」。

---

## 1. 现状基线（探索 + review 已确认）

- Design token 已在 `src/index.css:25-72` 定义明暗双套；`.orca-*` utilities 在 `index.css:103-134`。
- `tailwind.config.js:16-24` 已暴露 `orca.{pending,running,done,failed,skipped,accent}`，注释承认「当前未被任何组件直接使用」——P0 启用。
- **TopBar 已删 cost span**（P5a 已落地，`top-cost` testid 不存在）——实施者勿重复删除。
- 硬编码色 grep 守门基线：`grep -rnE "(bg|text|border)-(slate|indigo|emerald|red|amber|violet)-[0-9]" src/` ≈ 179 hits / 34 files（P0 工作量）。
- 图标全靠 emoji/Unicode，无图标库。
- 暗色只跟随 `prefers-color-scheme`，无手动开关。
- DAG 是浮层（`AgentsRail.tsx:263`）。
- 关键证据：
  - `components/layout/TopBar.tsx:33-40` `statusColor()` 硬编码 `text-red-600 / text-emerald-600 / text-slate-500 / text-amber-600`。
  - `components/layout/AgentsRail.tsx:105+` 大量 `bg-slate-50 / border-slate-200 / text-slate-* / text-amber-600`。
  - `components/gate/AskGate.tsx:70-74,90,111` `border-indigo-400 / bg-indigo-50 / text-indigo-900 / bg-indigo-600`。
  - `components/graph/nodes/NodeShell.tsx:25,31,40` `bg-white / bg-slate-100 / text-slate-*`（DAG 暗色塌陷）。
  - `components/detail/LogStream.tsx` `LEVEL_TEXT_COLOR` runtime map（LogLevel 单一真相源，保留）。
  - `hooks/use-websocket.ts:53` `useWebSocket` 是 void 签名（P3 在其内部 hook 连接事件，不改签名对外契约）。
  - `stores/workflow-store.ts:76` 有 `activeRunId`；TopBar 的 runId 来自 `useParams`（P3 复制直接用，无需新字段）。

---

## 2. 分阶段实施

每阶段：实施 → 自我 review（依赖/越界/DRY/fail loud/testid）→ `npm run build` + `test/` → 清理过期代码 → commit。

### P0 — Token 收口（基础，低风险）

**目标**：消除全部硬编码 `slate-*/indigo-*/emerald-*/red-*` 等，统一走 `orca-*` utility + `orca.*` palette。堵住暗色塌陷。

**拆 P0a / P0b**：

**P0a — 容器/文字/边框 token 替换**：
1. 全量 grep 清点：`grep -rnE "(bg|text|border)-(slate|indigo|emerald|red|amber|violet|sky|blue|green|rose|neutral|gray|zinc)-[0-9]" src/`。
2. 语义映射替换（**不改视觉色相**，只改来源）：
   - 容器 `bg-white`→`orca-bg-surface`；`bg-slate-50`→`orca-bg-surface-2`；页面底→`orca-bg-app`。
   - 边框 `border-slate-200`→`orca-border`。
   - 文字 `text-slate-900/800`→`orca-text`；`text-slate-700/600`→`orca-text-muted`；`text-slate-400/500`→`orca-text-faint`。
3. `TopBar.statusColor()` 改返回 `text-orca-*`（语义色用 `orca.*` palette：`text-orca-done/failed/running/pending/skipped`）。
4. `NodeShell` `bg-white`→`orca-bg-surface`，内部 `text-slate-*`→token（修 DAG 暗色塌陷）。
5. `AskGate` indigo 全部 → `orca-accent / orca-border-accent`，选中态 `border-orca-accent bg-[rgb(var(--accent)/0.08)]`。
6. `statusColor` + `STATUS_ICON` 抽到 `src/components/layout/status-style.ts`（DRY）。

**P0b — 语义色专项（白名单豁免）**：
逐项决策走 token 还是保留为独立真相源：
- **DiffView** diff 语法色（`+` 绿 / `-` 红 / `@@` 蓝）：**保留**——diff 语义色是领域约定，强行映射 token 会损可读性。配色值显式对齐 palette（绿=`orca.done`、红=`orca.failed`）但不强制 utility。
- **LogStream `LEVEL_TEXT_COLOR`**：**保留为独立 record**（LogLevel 单一真相源，与 NodeStatus 不是 1:1——LEVEL 有 info/warning 无对应 node status）。配色值显式对齐 palette，但**不强制映射**到 `orca-running/done`。
- **MarkdownText prose**（react-markdown 渲染的 `prose` 类自带色）：**保留**——prose 是 typography 系统，P0 不碰。
- **DataTable zebra**：走 `orca-bg-surface / orca-bg-surface-2` 交替。
- **`dark:` 变体残留**：统一改为 `.dark` class 作用域（与 P3 暗色机制对齐）。

**验收（白名单，非黑名单清零）**：
`grep -rnE "(bg|text|border)-(slate|indigo|emerald|red|amber|violet|sky|blue|green|rose|neutral|gray|zinc)-[0-9]" src/` 仅剩：
1. `graph/constants.ts` `NODE_STATUS_HEX`（hex 字符串，状态色单一真相源，保留）
2. `chart/chartTheme.ts` `PALETTE`（图表色单一真相源，保留）
3. `detail/LogStream.tsx` `LEVEL_TEXT_COLOR`（P0b 白名单）
4. `conversation/DiffView.tsx` diff 语义色（P0b 白名单）
5. `chart/widgets/DataTable.tsx` 内（若用 hex zebra）

其余清零。暗色下三栏无白卡片塌陷。testid 全保留。

**清理**：`tailwind.config.js:9` 注释更新为「已启用」；无引用的旧色类删除。

**commit**：`style(web): P0 token 收口——消除硬编码色，统一 orca-* 语义（白名单：NODE_STATUS_HEX/PALETTE/LEVEL_TEXT_COLOR/DiffView）`

---

### P1 — 图标库统一为 lucide（全部替换）

**目标**（D3=全部替换）：消除全部 emoji/Unicode 图标，引入 `lucide-react` 统一细线条（`size={14|16} strokeWidth={1.5}`）风格，可继承语义色。**唯一保留**：流式光标 `▎`（SPEC §5.3 文本光标契约，是文本非图标）。

**步骤**：
1. `npm i lucide-react@^0.460`（peer 支持 React 19）。验收加 `npm ls lucide-react` 输出 pinned 版本 ≥ 0.460。
2. 建 `src/components/icons.tsx`：集中导出项目用到的图标实例包装（统一 size/strokeWidth，可继承 `currentColor`），导出 `StatusIcon({status})`（`Circle/CircleDot/CircleCheck/CircleX/CirclePause/CircleSlash` 等）。
3. **测试 oracle 迁移（阻塞前置，N1/N6）**：先 grep 受影响断言：
   `grep -rnE 'toContain.*[●✓✗⊘⏸⏱💭🔒💬🔤🪙⟳▸▾]' test/`
   清单（review 已定位）：`test/topbar.test.tsx:62,71,79,88`（`●✓✗⊘` 文字断言）、`test/agents-rail.test.tsx:107,168,426`（emoji 文字断言）。
   迁移策略：从「文字断言 `toContain("●")`」改为「`top-status` 内含 `<svg>` 且 className 含 `text-orca-{status}`」（行为 = 显示带语义色图标，意图不变）。先改测试 oracle，再删 `STATUS_ICON` 常量。
4. 替换点（全替）：
   - TopBar status：`STATUS_ICON` emoji → `<StatusIcon/>`。
   - AgentsRail：`⏱`→`Timer`、`💭/思考中`→`Brain`、`🔤`→`Coins`、`⟳`→`Loader`（spin-slow）、`▸/▾`→`ChevronRight/ChevronDown`。
   - AskGate 标题 `💬`→`MessageSquare`；PermissionGate `🔒`→`Lock`。
   - LogStream level 图标（若 emoji）→ 对应 lucide。
   - 其他扫描到的 emoji 图标全替。
5. **保留** `▎` 流式光标（ConversationView + AgentsRail running 行）。

**验收**：`npm run build` 通过；`npm ls lucide-react` ≥ 0.460；`test/` 全 PASS（含迁移后的 oracle）；testid 全保留；`grep -rnE '[●✓✗⊘⏸⏱💭🔒💬🔤🪙⟳▸▾]' src/` 仅剩 `▎`（若有的话）。

**清理**：删除被替换的 emoji 字面量；`STATUS_ICON` 常量（前置：test oracle 已迁移）。

**commit**：`feat(web): P1 引入 lucide 统一图标库，全部替换 emoji（保留 ▎ 流式光标）`

---

### P2 — 左栏 AgentsRail 增量增强

**目标**（B5=增量非重做）：prior P3 已落地分组/色条/折叠/迭代，本阶段只做视觉层级提升 + lucide 接入 + running 流式光标。**不重做** selectAgentGroups / R3 / 折叠逻辑。

**设计**（见 §3 mockup）：
- 状态编码：左 3px 竖色条（`NODE_STATUS_HEX`，DRY 不变）+ 行首 `<StatusIcon/>`（色条 + 图标双编码）。
- agent 名一级字（`orca-text`，`text-sm`），元信息二级字（`orca-text-faint`，`text-[10px]` tabular-nums）。
- 元信息行重排为单行：`{status} · {elapsed} · {tokens}`，`·` 分隔。嵌套结构（N3）：
  ```
  <span data-testid="agent-status-{node}">running</span> ·
  <span data-testid="agent-elapsed-{node}">45s</span> ·
  <span data-testid="agent-tokens-{node}">1.2k</span>
  ```
  外层非 testid flex 容器。**原 `agent-elapsed-{node}` testid 保留**，新增 `agent-status-` / `agent-tokens-`。
- running 行末尾追加流式 `▎`（与 ConversationView 同语义）。
- stall「思考中 Ns」用 `orca-text-faint` + `<Brain/>`（去 amber 硬编码）。
- selected 行：`orca-bg-surface-2` + 左色条加粗到 4px。
- subs 折叠 chevron 用 lucide。
- **不实现** mockup 里「blocked · waiting on B」（N4：tape 现无 `blockers` 字段，待后续 SPEC）——只显 `{status} · {elapsed} · {tokens}`。

**约束**：不改 selector 签名；原 testid 清单（`agent-row-/agent-bar-/agent-iter-/agent-elapsed-/agent-stall-/agent-fold-/agent-sub-/agent-group-`）逐个迁移保留。

**验收**：原 testid 全在 + 新增；`npm run build` + `test/` 通过；暗色下层级清晰。

**commit**：`style(web): P2 AgentsRail 增量增强——双编码/元信息单行/running 流式光标`

---

### P3 — TopBar 增强 + 暗色开关 + WS 连接指示

**目标**：补齐 TopBar 完整度；手动暗色开关；WS 连接状态指示（D1=A）。

**步骤**：
1. **runId 可复制**：TopBar runId span 加 `onClick={() => navigator.clipboard.writeText(runId)}`（runId 来自 `useParams`，无需新 store 字段，N2 澄清），hover 显 `<Copy/>` 图标，成功显 1s `<Check/>`（纯本地 state，不引入 toast 库）。新增 testid `top-runid`（不删既有）。fail loud：clipboard reject → `console.error`。
2. **status badge**：纯文字色 → badge：`rounded px-1.5 py-0.5 text-xs border` + `bg-[rgb(var(--status-rgb)/0.1)] text-orca-{status} border-[rgb(var(--status-rgb)/0.3)]`。保留 `top-status` testid + 文字内容（E2E 断言不变）。
3. **WS 连接指示（D1=A）**：
   - 新建 `src/hooks/ws-connection-store.ts`：module-level zustand `{ status: 'connected'|'reconnecting'|'disconnected', lastError }`，actions `setConnected/setReconnecting/setDisconnected`。
   - 修改 `hooks/use-websocket.ts` **内部**（不改对外 void 签名）：在现有 onopen/onclose/onerror 回调里调 ws-connection-store 的 action。
   - 新建 `src/hooks/use-ws-status.ts`：`useWsStatus()` 订阅 ws-connection-store。
   - TopBar 右上加连接点（`<span data-testid="top-ws">` + 圆点）：connected 绿 / reconnecting 琥珀 / disconnected 红，hover 显文字。
   - **纯前端**：不碰后端 `ws_handler.py`，不加 WS 协议字段。
4. **暗色开关（B7）**：
   - 新建 `src/hooks/use-theme.ts`：三态 `light|dark|system`，持久化 `localStorage('orca-theme')`，`<html>` 加/去 `dark` class。
   - **SPEC 同步修订**（先改 SPEC 文档再实现）：`docs/specs/web-shell-v2-spec.md` §7 暗色机制改为「`@media (prefers-color-scheme: dark)` + `<html>.dark` class 双触发，`.dark` 优先级更高（用户显式覆盖系统）」。
   - 改 `index.css`：把 `@media (prefers-color-scheme: dark) :root {...}` 的暗 token **复制一份**到 `:root.dark {...}`。**顺序约束（B7 技术坑）**：`.dark` 规则必须在 `@media` 规则**之后**定义（同 specificity 时后者胜），否则用户显式 dark 会被 system light 覆盖。无 `.dark` class 时仍跟系统（保留默认行为）。
   - TopBar 右上 `<Sun/>/<Moon/>` toggle，testid `theme-toggle`。fail loud：localStorage 不可用 → `console.error` + fallback system。

**验收**：暗色开关可切且刷新保持（localStorage）；`.dark` 在 `@media` 之后（grep index.css 顺序）；WS 断连模拟（手动停后端）指示变红（test-agent 真机验或手动）；testid 全保留 + 新增；后端零改；SPEC §7 文档已同步。

**commit**：`feat(web): P3 TopBar 增强（复制/badge/WS指示）+ 暗色开关 + SPEC §7 双触发`

---

### P4 — 三栏 surface 层级统一（治割裂）

**目标**：消除「左白 / 中灰割裂」，三栏 surface 连续，靠 border + surface-2 分层而非异色底。

**步骤**：
1. 统一三栏底色为 `orca-bg-app`，栏内卡片 `orca-bg-surface`，分隔靠 `orca-border`。
2. **border 策略（N8）**：resize 分隔走 `<PanelResizeHandle className="orca-bg-surface-2 w-px">`（`RunDetailPage.tsx:73,112` 已采用此模式），**不要**在 Panel 内首层容器再叠 `border-r orca-border`，否则 resize 时出现双线。
3. 中栏 tabs active 下划线、ConversationView 容器、LogStream 容器统一走 surface token。
4. 查漏补缺 P0 之后残留的异色底。

**验收**：三栏视觉连续无色块割裂；暗色一致；resize 无双线；testid 不变。

**commit**：`style(web): P4 三栏 surface 层级统一，消除割裂`

---

## 3. AgentsRail 增量增强 mockup（P2 参考）

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
│   blocked · —            │  ← blocked reason「waiting on B」未来实现（N4）
│   ▸ 3 subs               │  ← chevron 折叠
└──────────────────────────┘
```

---

## 4. 过期代码清理清单（每阶段执行）

- 被替换的旧 emoji 字面量。
- `STATUS_ICON` 常量——**前置条件**：先同步改 `test/topbar.test.tsx` / `test/agents-rail.test.tsx` 的 emoji 文字断言为新 oracle（见 §P1 step 3），再删常量。
- `tailwind.config.js:9` 过期注释（P0 更新）。
- P0 替换后不再引用的 className 组合。
- 死 import（tsconfig 严格，build 会报 unused）。
- 每阶段 commit 前跑 `tsc --noEmit`（build 内含）。

---

## 5. 自我 Review 清单（每阶段 commit 前）

- [ ] 依赖铁律：未碰后端 Python；未引入 store → 后端反向调用；ws-connection-store 是 transport-only 不属 tape 真相。
- [ ] 无职责越界：UI 组件没塞业务逻辑。
- [ ] DRY：状态色单一真相源（`NODE_STATUS_HEX` + `orca.*` palette），图标单一入口（`icons.tsx`）。
- [ ] fail loud：clipboard / theme localStorage / ws error 有 console.error，不静默吞。
- [ ] testid：原 testid 全保留，新增有命名规范。
- [ ] **test oracle 迁移**：替换 emoji 时同步改 test 文字断言（不只 testid）。
- [ ] **P0 grep 守门按白名单**（NODE_STATUS_HEX / PALETTE / LEVEL_TEXT_COLOR / DiffView），不是黑名单清零。
- [ ] `npm run build` + `test/` 通过。

---

## 6. 完成后收尾

- 写 release note `docs/releases/2026-07-18-web-visual-refinement.md`。
- CHANGELOG 加索引（每阶段 1-2 句 + commit SHA）。
- **CURRENT.md side-track 登记（N5，不跳过状态文档）**：在 CURRENT.md 加明确「side track：web visual refinement（P0-P4）」段落，与主任务块（in-session）分隔但不省略；每阶段 commit 后更新该段状态；全部完成后移至 CHANGELOG 并清空该段。
- prior SPEC `docs/specs/2026-07-18-web-presentation-refinement.md` 顶部标「partial-superseded by 2026-07-18-web-visual-refinement.md」。
- 截图沉淀：`docs/assets/web/2026-07-18-*.png`（每阶段一张，补「零设计资产」缺口）。

---

## 7. Follow-up（剥离，不在本 plan）

- **DAG compact minimap（原 P5）**：常驻左栏缩略图 + 运行节点高亮。需先开 SPEC amendment 修订 `web-shell-v2-spec.md` §5.7「DAG 不在三栏常驻」→「浮层 + minimap 双模式」。后续单独立项。

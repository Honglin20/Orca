# Release Note —— 阶段 9d：iface/web gate 弹窗 + render_chart

> **日期**：2026-07-01
> **SPEC**：[`docs/specs/phase-9d-web-gate-chart.md`](../specs/phase-9d-web-gate-chart.md)
> **计划**：[`docs/plans/2026-06-30-phase9d-web-gate-chart.md`](../plans/2026-06-30-phase9d-web-gate-chart.md)
> **commit**：`6d0c5e1`
> **分支**：`phase9-web`（**phase 9 全部子阶段 9a/9b/9c/9d 完成，分支可合并 master**）

---

## 概述

phase 9 末子阶段（9d），完成 Web 壳的「人机交互 + 可视化」两大 UX 主战场：

1. **gate 富交互弹窗**（SPEC §1）：两种 source（`tool_permission` 权限 4 按钮 / `agent_ask` 主动问 radio|textarea）
   全读 `store.gate`（零本地 gate state）；答走后端 `POST /gate/respond`（前端纯 forward 不决策）；
   **不乐观更新**（答后等 `human_decision_resolved` 才关弹窗，保唯一真相源）；**三通道竞速广播**
   （别壳先答 → `store.gate=null` + `store.lastResolved` → ResolvedToast「已被 [source] 答」）。
2. **render_chart 迁移 AgentHarness**（SPEC §2）：学术配色 `chartTheme`（PALETTE 8 色逐字迁移）
   + 扁平 record-array spec + 5 种 recharts widget（line/bar/scatter/pareto/table）。
   **chart 是事件**（`custom(kind=chart)` 从 `store.events` filter，无独立通道）；同 label+title 替换
   （实时更新不堆积）+ replay 同步（chart ≤ replayPosition）。

---

## AgentHarness 资产迁移（SPEC §2.1 清单）

| 资产 | 来源 | Orca 处理 |
|---|---|---|
| `chartTheme.ts` | `frontend/src/components/output/charts/chartTheme.ts` | 🟢 整文件迁移（PALETTE 8 色 / POSITIVE/NEGATIVE/NEUTRAL / getGridProps/getAxisTick/getTooltipStyle / CHART_MARGIN / BOX_*）逐字保留；仅去掉 React 顶层 import（getTooltipStyle 返回纯对象） |
| `axisUtils.ts` | `charts/axisUtils.ts` | 🟢 零改动迁移（纯数学函数 niceNum/computeNiceTicks/formatTick/extractNumericValues） |
| 扁平 record-array spec | `chart.py` chart_payload | 🟢 作为 Orca `custom(chart)` 事件 data 契约（`types.ts` ChartPayload） |
| 5 种 widget | `charts/{Line,Bar,Scatter,Pareto}ChartWidget.tsx` + `DataTable.tsx` | 🟢 迁移 + 适配：prop `chart` → `payload`；去掉 AgentHarness-only 的 EndLabel/`columns` 多列 Bar/shadcn Table；保留 hue pivot、PALETTE 着色、findParetoFront 算法 |
| 三通道投递 | `chart.py` EventBus/stdout/HTTP | 🔴 **不要**（Orca 只 EventBus 一条） |
| `render_chart` MCP 工具 | `chart.py` | 🔴 phase 10 实现（9d 只前端渲染） |

**复制 vs 适配**：chartTheme/axisUtils 是纯样式/数学工具，逐字复制（PALETTE 色值是 SPEC §2.5 硬约束）；
5 widget 是结构迁移（recharts 用法 + chartTheme 应用复制，prop 命名 + AgentHarness-only 特性去除）。

---

## 交付物

```
orca/iface/web/frontend/src/components/
├── gate/
│   ├── GateDialog.tsx          # 按 source 分派 + null gate 不渲染
│   ├── PermissionGate.tsx      # tool_permission 4 按钮（allow/deny/edit/skip）
│   ├── AskGate.tsx             # agent_ask：options→radio / 无→textarea
│   ├── ResolvedToast.tsx       # 抢答广播「已被 [source] 答」（2s 自动消失）
│   └── post-gate-respond.ts    # POST /gate/respond 共享 helper（DRY）
└── chart/
    ├── chartTheme.ts           # 迁移自 AgentHarness（PALETTE 8 色）
    ├── axisUtils.ts            # 迁移（nice ticks/formatTick）
    ├── pivot.ts                # hue 长格式→宽格式 pivot（DRY，line/bar 共用）
    ├── types.ts                # ChartPayload 契约
    ├── ChartRenderer.tsx       # 主：订阅 store.events custom(chart) + 按 node filter
    ├── ChartGroup.tsx          # label 分组（可折叠）+ dedupeByLabelTitle（实时替换）
    ├── ChartWidget.tsx         # 按 chart_type 分派（未知 fail loud）
    └── widgets/
        ├── LineChartWidget.tsx
        ├── BarChartWidget.tsx
        ├── ScatterChartWidget.tsx
        ├── ParetoChartWidget.tsx   # findParetoFront + dominated/front 两色散点 + 前沿连线
        └── DataTableWidget.tsx     # table 类型：扁平 record → HTML 表格

orca/iface/web/frontend/test/
├── gate.test.tsx               # 10 测试（两 source / POST / 不乐观 / 抢答 / useState 零 gate）
└── chart.test.tsx              # 16 测试（5 widget / PALETTE 应用 / label 分组 / dedupe / replay）

tests/iface/web/test_playwright_9d.py   # 6 场景 @integration（gate 答题 / 抢答 / line 配色 / 5 种 / pareto 前沿线 / dedupe）

修改：
- stores/workflow-store.ts     # human_decision_resolved 补 lastResolved（驱动 toast）
- App.tsx                      # 挂 GateDialog 在 Layout 根（覆盖任意页面）
- detail/NodeDetail.tsx        # 挂 ChartRenderer（选中节点图表）
- pages/RunDetailPage.tsx      # Output tab = ChartRenderer（nodeId undefined 全节点）
- main.tsx                     # ?debug=1 opt-in store 调试入口（playwright 用）
- test/setup.ts                # Element 尺寸打桩（recharts ResponsiveContainer 在 happy-dom 渲染所需）
- index.css                    # chart theme CSS vars（明暗自适应）
```

---

## 五条铁律 + §1.6 验证（review 全过，无阻塞）

| 铁律 | 实现 | 测试 |
|---|---|---|
| 1. gate 状态从 store.gate 读 | `GateDialog.tsx:20` 只 `useWorkflowStore((s)=>s.gate)`，gate 组件零 gate useState | `gate.test.tsx:211-243`（切 gate 反映到 UI）+ `gate.test.tsx:245-275`（AskGate options 切换 selected 重置） |
| 2. gate 走后端 resolve | `post-gate-respond.ts:16-21` POST `/gate/respond` body `{gate_id,answer,source:"web"}` ↔ 后端 `http_endpoint.py:146-155` | `gate.test.tsx:108-149`（body `toEqual`） |
| 3. 三通道竞速广播 | `workflow-store.ts:213-221` resolved → `gate=null` + `lastResolved={by,answer}`；字段 `resolved_by` 前后端一致（`handler.py:245`） | `gate.test.tsx:177-209`（弹窗关 + toast 含 cli/allow） |
| 4. chart 是事件 | `ChartRenderer.tsx:30-46` filter `store.events` `type==="custom" && data.kind==="chart"`，无独立 store | `chart.test.tsx` 全程 `processEvent` 注入 |
| 5. 复用 AgentHarness chartTheme | `chartTheme.ts:10-19` PALETTE 8 色逐字（#5B8DB8/#E29D3E/...） | `chart.test.tsx:103-115`（逐色值）+ `:169-211`（SVG stroke/fill 实落 PALETTE） |
| §1.6 不乐观更新 | `PermissionGate.tsx:36-38` / `AskGate.tsx:30-31` POST 后 submitting 保持，不清 gate/不关弹窗 | `gate.test.tsx:152-175`（**答后 store.gate 非 null + 弹窗仍在 + 按钮 disabled**） |

---

## 关键设计决策

1. **不乐观更新（SPEC §1.6）**：用户点答案后 POST `/gate/respond`，但**不清 store.gate / 不关弹窗**——
   submitting UX 反馈（按钮 disabled + 文案「提交中…」），弹窗关闭**只能**由 backend emit
   `human_decision_resolved`（store.gate→null）触发。这保住了「前端不持有真相」的铁律——前端不预判
   backend 是否接受答案。晚到 resolve（已被抢答）后端返回 `{ok:false}`，前端不抛错（resolved 事件随后到达自然关弹窗）。
2. **抢答广播 toast 2s 自消失**：`ResolvedToast` 用 `useState<Set<string>>(hidden)` 记已显示过的 lastResolved
   快照，`setTimeout(2000)` 后加入 hidden → 隐藏。`useEffect` cleanup `clearTimeout` 防 leak。
3. **chart 是事件**（反 AgentHarness 三通道）：claude 调 render_chart（phase 10 MCP 工具）→ executor emit
   `custom(kind=chart)` 事件 → tape → 前端 `store.events` filter 渲染。**无独立 chart store/HTTP/stdout 通道**。
4. **label+title 实时更新键**：同 `label` 分到同一 `ChartGroup`，同 `label+title` 替换非追加
   （`dedupeByLabelTitle` 用 Map by title 后者覆盖）—— 迭代过程图表刷新不堆积。
5. **replay 同步自动**：ChartRenderer 复用 NodeDetail/LogStream 同一 `events.slice(0, replayPosition+1)` 切片逻辑，
   replay 模式 chart 自动只显示到 replayPos（同一 store，无额外同步代码）。
6. **`?debug=1` opt-in 调试入口**：URL 带 `?debug=1` 才把 store 挂 `window.__orcaStore`（playwright 集成测试
   注入事件用）；prod 默认不暴露，铁律「前端不持有真相」不受影响。
7. **happy-dom + recharts 尺寸打桩**：recharts ResponsiveContainer 读父元素 clientWidth/clientHeight，
   happy-dom 不计算布局 → 为 0 时 recharts 不渲染子 SVG。`test/setup.ts` 给所有 Element 打桩 600×400。

---

## 测试结果

- **vitest 84 通过**（gate 10 + chart 16 + 既有 store/graph/replay/hooks/log-detail 58 零回归）
- **`npm run build` 成功**（tsc --noEmit 0 type error；输出 static/）
- **pytest 595 通过**（0 RuntimeWarning，phase 1-9c 零回归；9d 是前端，无 Python 改动）
- **playwright 9d 6 场景 collected**（`@integration`，CI 默认不跑，需 playwright + chromium）

---

## review 反馈处理（code-reviewer，无阻塞）

- ✅ 五铁律 + §1.6 全过（file:line 证据可验）
- ✅ fail loud 全覆盖（未知 chart_type / 缺 gate_id / 网络错 / CSS var 缺失）
- ✅ timer 清理无 leak（ResolvedToast effect cleanup）

**3 建议项全修复**：
1. **hue pivot 去重** → 抽 `pivot.ts` 共享 helper（line/bar 改用，DRY）
2. **pareto 前沿线缺测试断言** → playwright 9d 补 `test_pareto_front_line`（wait `.recharts-line path` + strokeDasharray）
3. **AskGate selected 重置** → 加 `useEffect([gate.gate_id])` 同步初值 + 补 options 切换测试

**1 可选项**：post-gate-respond `ok:false` 注释承诺未实现 → 删 dead comment（YAGNI，submitting 保持已正确）。

---

## phase 9 完成总结（9a→9b→9c→9d）

| 子阶段 | 内容 | commit |
|---|---|---|
| 9a | FastAPI 后端 + RunManager + WS 单通道 + gate 分发 | `b34c87d` |
| 9b | React 19 + Vite SPA 骨架 + Zustand 单 store + 懒加载 + WS hook | `0347a66` |
| 9c | ReactFlow DAG 可视化 + tape replay（增量 apply + checkpoint） | `adc856c` |
| **9d** | **gate 弹窗（两 source 富交互 + 抢答广播）+ render_chart（AgentHarness 5 图迁移）** | **`6d0c5e1`** |

**`phase9-web` 分支可合并 master**：Web 壳四子阶段全部完成（后端 + 前端骨架 + DAG/replay + gate/chart），
全栈可用（列表 / 详情 / DAG 可视化 / 流式日志 / tape replay / gate 弹窗 / chart 渲染 / Output 视图）。
后续 phase 10 MCP（让 claude 能调 render_chart + ask_user 工具）独立进行。

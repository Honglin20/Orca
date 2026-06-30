# 阶段 9d SPEC —— iface/web gate 弹窗 + render_chart（迁移 AgentHarness 5 种图）

> **状态**：最终版（待分发实现）
> **依据**：[phase-9b-web-frontend-core.md](phase-9b-web-frontend-core.md)（store.gate 契约）· [phase-6-gates.md](phase-6-gates.md) §1 §4（HumanGate）· AgentHarness chart.py + chartTheme.ts 调研
> **范围**：gate 富交互弹窗（两种 source 渲染 + 三通道竞速广播接收）+ render_chart 前端渲染（迁移 AgentHarness 的 line/bar/scatter/pareto/table 五种 + chartTheme 学术配色）
> **前置**：phase 9b（store）+ phase 9c（Detail Panel 容器）
> **commit 规范**：`feat(web):` 前缀，独立分支

---

## 0. 阶段目标 + 铁律

phase 9d 回答：**「gate 怎么富交互地弹给用户答、被别壳抢答怎么同步；claude 产出的图表怎么用 AgentHarness 的学术风格渲染？」**

### 0.1 五条铁律（违反即返工）
1. **gate 状态从 store.gate 读**（9b 派生），弹窗不自己存 gate 状态（反 AgentHarness 多源）。
2. **gate 走后端 handler.resolve**：用户答 → POST /gate/respond → 后端 resolve，前端不决策（纯 forward）。
3. **三通道竞速广播**：收到 `human_decision_resolved`（别壳先答）→ 弹窗自动关闭 + 显示「已被 [source] 答」。
4. **chart 是事件不是图片**：claude 产出 `custom(kind=chart)` 事件 → 写 tape → 前端按 kind 渲染（反 AgentHarness 三通道投递复杂度，Orca 只走 EventBus 一条）。
5. **render_chart 复用 AgentHarness 资产**：chartTheme.ts 学术配色 + 扁平 record-array spec + 5 种 recharts widget 直接迁移。

### 0.2 反模式（必须避免）
- ❌ 前端自己存 gate 状态（多源漂移）
- ❌ 前端做 gate 决策逻辑（那是后端 handler 职责）
- ❌ chart 走独立通道（AgentHarness 三通道 EventBus/stdout/HTTP，Orca 只 EventBus）
- ❌ 重新设计 chart 配色（复用 AgentHarness chartTheme）

---

## 1. gate 富交互弹窗（Web 是 gate UX 主战场）

### 1.1 数据来源（store.gate，9b 派生）

```typescript
// 9b store 的 handler：
human_decision_requested: (s, d) => { s.gate = { id: d.gate_id, prompt: d.prompt, options: d.options, source: d.source, context: d.context }; },
human_decision_resolved: (s, d) => { s.gate = null; s.lastResolved = { by: d.resolved_by, answer: d.answer }; },
```

### 1.2 GateDialog 组件（按 source 渲染）

```typescript
// components/gate/GateDialog.tsx
function GateDialog() {
  const gate = useStore(s => s.gate);
  const lastResolved = useStore(s => s.lastResolved);
  if (!gate) return null;
  if (gate.source === "tool_permission") return <PermissionGate gate={gate} />;
  return <AskGate gate={gate} />;
}
```

### 1.3 PermissionGate（工具权限弹窗）

```
┌─────────────────────────────────────────────────────────────────┐
│  🔒 权限请求                                              [×]    │
│  ─────────────────────────────────────────────────────────────  │
│  节点 {gate.context.node} 的 Claude 想调用工具：                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  🔧 {gate.context.tool}                                  │  │
│  │  {JSON.stringify(gate.context.tool_input, null, 2)}      │  │
│  └──────────────────────────────────────────────────────────┘  │
│           [批准执行]    [拒绝]    [编辑后批准]    [跳过]          │
└─────────────────────────────────────────────────────────────────┘
```

按钮 → POST /gate/respond `{gate_id, answer: "allow"|"deny"|"edit"|"skip", source: "web"}`

### 1.4 AskGate（agent 主动问弹窗）

```
┌─────────────────────────────────────────────────────────────────┐
│  💬 Agent 提问                                           [×]    │
│  ─────────────────────────────────────────────────────────────  │
│  {gate.prompt}                                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  {gate.options ? 选项 radio : 自由文本 textarea}         │  │
│  └──────────────────────────────────────────────────────────┘  │
│                       [提交回答]    [取消]                       │
└─────────────────────────────────────────────────────────────────┘
```

有 options → radio 选择；无 options → textarea 自由文本。提交 → POST /gate/respond

### 1.5 抢答处理（三通道竞速广播）

```typescript
// 收到 human_decision_resolved（别壳先答）→ store.gate = null + lastResolved 设置
// GateDialog 检测 lastResolved → 显示「已被 [source] 答」2 秒后关闭
function ResolvedNotice() {
  const lastResolved = useStore(s => s.lastResolved);
  if (!lastResolved) return null;
  return <Toast>已被 [{lastResolved.by}] 回答：{lastResolved.answer}</Toast>;
  // 2 秒后自动消失
}
```

### 1.6 关键约束
- **弹窗不存状态**：全读 store.gate。
- **POST /gate/respond 后不乐观更新**：等后端 emit `human_decision_resolved` 再关弹窗（保证唯一真相）。
- **被抢答**：store.gate 被 resolved 事件置 null，弹窗自动消失。

---

## 2. render_chart（迁移 AgentHarness，5 种图）

### 2.1 复用资产清单

| 资产 | AgentHarness 来源 | Orca 处理 |
|---|---|---|
| **chartTheme.ts**（学术配色）| `frontend/src/components/output/charts/chartTheme.ts` | 🟢 整个复制（8 色 PALETTE + 主题感知 + tooltip/grid 样式）|
| **扁平 record-array spec** | chart.py chart_payload | 🟢 作为 Orca `custom(chart)` 事件 data 契约 |
| **5 种 widget** | charts/*ChartWidget.tsx | 🟢 迁移 line/bar/scatter/pareto/table |
| **label+title 实时更新键** | chartStore.ts | 🟢 保留（同键替换非追加）|
| **三通道投递** | chart.py EventBus/stdout/HTTP | 🔴 **不要**（Orca 只走 EventBus 一条）|
| **render_chart MCP 工具** | chart.py | 🔴 phase 10 实现（phase 9d 只前端渲染）|

### 2.2 chart 数据契约（custom 事件）

```typescript
// Orca 的 custom 事件（phase 1 已定义 data.kind）
// claude 调 render_chart MCP 工具（phase 10）→ executor emit custom 事件 → tape
interface ChartCustomEvent {
  type: "custom";
  data: {
    kind: "chart";
    chart: ChartPayload;  // 迁移 AgentHarness 扁平 spec
  };
}

// ChartPayload（迁移自 AgentHarness）
interface ChartPayload {
  chart_type: "line" | "bar" | "scatter" | "pareto" | "table";  // 9d 只这 5 种
  data: Record<string, any>[];    // 扁平 record array
  x?: string; y?: string;
  label: string;                  // 分组键（同 label+title 替换）
  title: string;
  hue?: string;
  // pareto 特有
  pareto_direction?: "max" | "min";
  pareto_x_direction?: "max" | "min";
  pareto_y_direction?: "max" | "min";
}
```

### 2.3 组件结构

```
components/chart/
├── chartTheme.ts            # 迁移自 AgentHarness（学术配色，整文件复制）
├── ChartRenderer.tsx        # 主：订阅 custom(chart) 事件 → 按 chart_type 分派
├── ChartGroup.tsx           # 按 label 分组（可折叠），同 label+title 替换
└── widgets/
    ├── LineChartWidget.tsx   # 迁移（recharts + chartTheme）
    ├── BarChartWidget.tsx
    ├── ScatterChartWidget.tsx
    ├── ParetoChartWidget.tsx
    └── DataTableWidget.tsx   # table 类型
```

### 2.4 ChartRenderer（订阅事件，按 kind 渲染）

```typescript
// components/chart/ChartRenderer.tsx
function ChartRenderer({ nodeId }: { nodeId: string }) {
  // 取该节点的 custom(chart) 事件（按 label 分组）
  const chartEvents = useStore(s =>
    s.events.filter(e => e.type === "custom" && e.data.kind === "chart" && e.node === nodeId)
  );
  // 按 label 分组，同 label+title 替换（实时更新）
  const groups = groupByLabel(chartEvents);
  return groups.map(g => <ChartGroup key={g.label} group={g} />);
}

function ChartGroup({ group }) {
  const latest = dedupeByLabelTitle(group.charts);  // 同 label+title 取最新
  return (
    <CollapsibleSection label={group.label}>
      {latest.map(c => <ChartWidget key={c.title} payload={c} />)}
    </CollapsibleSection>
  );
}

function ChartWidget({ payload }) {
  switch (payload.chart_type) {
    case "line": return <LineChartWidget payload={payload} />;
    case "bar": return <BarChartWidget payload={payload} />;
    case "scatter": return <ScatterChartWidget payload={payload} />;
    case "pareto": return <ParetoChartWidget payload={payload} />;
    case "table": return <DataTableWidget payload={payload} />;
  }
}
```

### 2.5 chartTheme.ts（迁移自 AgentHarness，学术风格）

直接复制 AgentHarness 的：
- `PALETTE`：8 色低饱和（钢蓝 #5B8DB8 / 暖琥珀 #E29D3E / 灰珊瑚 #D4605A / 鼠尾草青 #6BA5A0 / 橄榄绿 #6B9E5C / 古金 #C9A843 / 柔紫 #9A7BA8 / 灰粉 #E08E9B）
- `POSITIVE/NEGATIVE/NEUTRAL` 语义色
- 主题感知（读 CSS 变量 --border/--muted-foreground 支持明暗）
- `getGridProps()`（水平虚线）/`getAxisTick()`（fontSize 11）/`getTooltipStyle()`（圆角 8px）/`CHART_MARGIN`
- 线条 strokeWidth 2 + 点 r 3 / 柱状半透明填充 fillOpacity 0.2 + 圆角

### 2.6 Chart 挂载位置

- **Detail Panel**（节点详情）：选中节点的 charts（phase 9c 的 NodeDetail 里挂 `<ChartRenderer nodeId={selected} />`）
- **Output Panel**（最终输出）：workflow outputs 的 charts
- **未来**：对话内联（phase 10 MCP 对话流，本次不做）

### 2.7 关键约束
- **chart 是事件**：从 store.events filter，不单独存。
- **实时更新**：同 label+title 替换（迭代过程图表刷新，不堆积）。
- **replay 同步**：replay 模式 chart 只显示到 replayPos 的事件（同一 store，自动同步）。
- **5 种图**：line/bar/scatter/pareto/table（你定的范围），其他 8 种后续按需。

---

## 3. 验收标准

### 3.1 gate 弹窗
- [ ] 收到 human_decision_requested → PermissionGate/AskGate 弹出（按 source）
- [ ] PermissionGate 显示工具+参数+4 按钮
- [ ] AskGate 显示问题+选项/文本
- [ ] 用户答 → POST /gate/respond（fetch 断言）
- [ ] **不乐观更新**：答后等 resolved 事件才关弹窗
- [ ] **抢答**：收到 resolved（别壳）→ 弹窗关 + 显示「已被 [source] 答」
- [ ] 弹窗不存状态（全读 store.gate）

### 3.2 render_chart（5 种图）
- [ ] 注入 custom(chart,line) 事件 → LineChartWidget 渲染（recharts）
- [ ] bar/scatter/pareto/table 各渲染正确
- [ ] **chartTheme 学术配色**：断言用 PALETTE 颜色（读 SVG fill）
- [ ] **label 分组**：同 label 的 charts 折叠在一起
- [ ] **实时更新**：同 label+title 第二个事件 → 替换第一个（不堆积，断言只 1 个）
- [ ] chart 挂在 Detail Panel（选中节点）
- [ ] **replay 同步**：replay 模式 chart 只显示到 replayPos

### 3.3 测试
- [ ] `frontend/test/gate.test.tsx`：两种 source 渲染 + POST + 抢答
- [ ] `frontend/test/chart.test.tsx`：5 种 widget + chartTheme + label 分组 + 实时更新
- [ ] playwright：gate 弹窗 + chart 渲染

### 3.4 playwright 验收（AI 自动测）
- [ ] **gate 弹出**：触发 gate（demo workflow 产 human_decision_requested）→ 断言 GateDialog 可见
- [ ] **PermissionGate**：断言显示工具名 + 4 按钮
- [ ] **答 gate**：playwright 点「批准」→ 断言 POST /gate/respond + 弹窗关闭
- [ ] **抢答模拟**：注入 resolved 事件 → 断言弹窗关 + toast 显示
- [ ] **chart 渲染**：注入 custom(chart,line) 事件 → 断言 `.recharts-line` 可见
- [ ] **5 种图**：各注入一种 chart 事件 → 断言对应 widget 渲染
- [ ] **学术配色**：读 SVG path fill → 断言在 PALETTE 内
- [ ] **实时更新**：同 label+title 两次 → 断言只 1 个 chart（不堆积）

---

## 4. 给后续阶段的契约

| 后续 | phase 9d 提供 |
|---|---|
| phase 10 mcp | gate 弹窗是 Web 主交互面（MCP 壳 gate 走 Web）；render_chart 前端渲染就位（phase 10 补 MCP 工具让 claude 能调）|

---

## 5. 不做的事

- ❌ render_chart MCP 工具（让 claude 能调）—— phase 10（phase 9d 只前端渲染）
- ❌ 其他 8 种图（heatmap/box/radar/area/bubble/waterfall/dist_overlay/optimal_line）—— 后续按需
- ❌ chart 三通道投递（stdout marker 等）—— Orca 只 EventBus
- ❌ 对话内联 chart —— phase 10
- ❌ 后端/store/DAG —— 9a/9b/9c

---

## 6. 关键决策备忘（防 drift）

1. **gate 状态从 store.gate 读**（不自己存）
2. **gate 走后端 resolve**（前端只 POST forward）
3. **抢答广播**：resolved 事件 → 弹窗关 + toast
4. **不乐观更新**：等 resolved 事件才关
5. **chart 是事件**（custom kind=chart，从 store filter）
6. **复用 AgentHarness chartTheme**（学术配色整文件复制）
7. **5 种图**：line/bar/scatter/pareto/table
8. **label+title 实时更新键**（同键替换）
9. **replay 同步**（chart 只显示到 replayPos）
10. **chart 只走 EventBus**（反 AgentHarness 三通道）

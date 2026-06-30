# 开发计划 —— 阶段 9d：iface/web gate 弹窗 + render_chart

> **状态**：待执行（**phase 9b + 9c 实现完成后开工**）
> **SPEC**：[`docs/specs/phase-9d-web-gate-chart.md`](../specs/phase-9d-web-gate-chart.md)
> **前置**：phase 9b（store.gate）+ phase 9c（Detail Panel 容器）
> **commit 规范**：`feat(web):` 前缀，独立分支

---

## 0. 产出与执行顺序

```
orca/iface/web/frontend/src/
├── components/
│   ├── gate/
│   │   ├── GateDialog.tsx         D1（主 + source 分派）
│   │   ├── PermissionGate.tsx     D1
│   │   ├── AskGate.tsx            D1
│   │   └── ResolvedToast.tsx      D1（抢答提示）
│   └── chart/
│       ├── chartTheme.ts          D2（迁移 AgentHarness）
│       ├── ChartRenderer.tsx      D2（订阅 custom + 分派）
│       ├── ChartGroup.tsx         D2（label 分组）
│       └── widgets/               D3（5 种）
│           ├── LineChartWidget.tsx
│           ├── BarChartWidget.tsx
│           ├── ScatterChartWidget.tsx
│           ├── ParetoChartWidget.tsx
│           └── DataTableWidget.tsx
+ frontend/test/ × 2
+ tests/iface/web/test_playwright_9d.py
```

执行顺序：D1 gate → D2 chart 骨架+theme → D3 五种 widget → D4 playwright

---

## D1. gate 弹窗（富交互 + 抢答）

### D1.1 `gate/GateDialog.tsx`
- 读 store.gate，null 则不渲染
- 按 gate.source 分派 PermissionGate / AskGate
- 读 store.lastResolved → ResolvedToast

### D1.2 `gate/PermissionGate.tsx`
- 显示 gate.context.tool + tool_input（JSON 格式化）+ 节点名
- 4 按钮（批准/拒绝/编辑/跳过）→ POST /gate/respond
- **不乐观更新**：答后等 resolved 事件（store.gate→null）才关

### D1.3 `gate/AskGate.tsx`
- 显示 gate.prompt
- gate.options → radio；无 → textarea
- 提交 → POST /gate/respond

### D1.4 `gate/ResolvedToast.tsx`
- 收到 resolved（别壳先答）→ toast「已被 [source] 答：[answer]」2 秒消失

### D1.5 验收（D1）— `frontend/test/gate.test.tsx`
- [ ] store.gate 设 tool_permission → PermissionGate 渲染（工具+4 按钮）
- [ ] store.gate 设 agent_ask + options → AskGate radio
- [ ] store.gate 设 agent_ask 无 options → AskGate textarea
- [ ] 点批准 → fetch /gate/respond 被调（断言 body）
- [ ] **不乐观更新**：答后 store.gate 仍非 null（等后端 resolved）
- [ ] store.lastResolved 设置 → ResolvedToast 显示

---

## D2. chart 骨架 + chartTheme（迁移 AgentHarness）

### D2.1 `chart/chartTheme.ts`
- **从 AgentHarness 整文件复制**：PALETTE(8 色) + POSITIVE/NEGATIVE/NEUTRAL + 主题感知 + getGridProps/getAxisTick/getTooltipStyle + CHART_MARGIN + 线/柱样式常量

### D2.2 `chart/ChartRenderer.tsx`
- 从 store.events filter custom(chart) + 按 node
- 按 label 分组 → ChartGroup

### D2.3 `chart/ChartGroup.tsx`
- 同 label+title 替换（dedupeByLabelTitle，实时更新）
- CollapsibleSection 折叠
- 按 chart_type 分派 widget

### D2.4 验收（D2）
- [ ] chartTheme PALETTE 8 色存在（断言长度）
- [ ] ChartRenderer filter 正确（只取 custom+chart+nodeId）
- [ ] ChartGroup 同 label+title 两次 → 只 1 个（实时更新，断言）
- [ ] label 分组（不同 label 不同 section）

---

## D3. 五种 chart widget（recharts + chartTheme）

### D3.1 `widgets/LineChartWidget.tsx`
- recharts LineChart + chartTheme 样式
- hue 分组（长格式 pivot 宽格式）
- 数据从 payload.data（扁平 record array）

### D3.2 `BarChartWidget.tsx`
- recharts BarChart + 半透明填充 + 圆角

### D3.3 `ScatterChartWidget.tsx`
- recharts ScatterChart + hue 颜色

### D3.4 `ParetoChartWidget.tsx`
- 散点 + Pareto 前沿连线（按 pareto_direction 算前沿）

### D3.5 `DataTableWidget.tsx`
- 表格（payload.data record array → 列）

### D3.6 挂载
- Detail Panel（NodeDetail，9c）加 `<ChartRenderer nodeId={selected} />`
- Output Panel 加 `<ChartRenderer nodeId={outputNode} />`

### D3.7 验收（D3）— `frontend/test/chart.test.tsx`
- [ ] LineChart：注入 line payload → `.recharts-line` 可见
- [ ] BarChart：`.recharts-bar` 可见
- [ ] ScatterChart：`.recharts-scatter` 可见
- [ ] ParetoChart：散点 + 前沿线
- [ ] DataTable：表格行数 == payload.data 长度
- [ ] **配色**：读 SVG fill，断言在 PALETTE 内
- [ ] **replay 同步**：replay 模式只显示到 replayPos 的 chart

### D3.8 测试骨架
```typescript
test('line chart renders with palette', () => {
  const payload = { chart_type: "line", data: [{x:1,y:2},{x:2,y:4}], x:"x", y:"y", label:"g", title:"t" };
  render(<ChartWidget payload={payload} />);
  const path = document.querySelector(".recharts-line path");
  expect(path).toBeTruthy();
  expect(PALETTE).toContain(path.getAttribute("stroke"));
});

test('label title dedupe', () => {
  store.processEvent(customChart("g", "t", 1));
  store.processEvent(customChart("g", "t", 2));  // 同 label+title
  render(<ChartRenderer nodeId="n" />);
  expect(screen.getAllByTestId("chart-widget").length).toBe(1);  // 替换非堆积
});
```

---

## D4. playwright 验收（AI 自动测）

### D4.1 `tests/iface/web/test_playwright_9d.py`（@pytest.mark.integration）
- [ ] **gate 弹出**：跑含 gate 的 demo → 断言 `[data-testid=gate-dialog]` 可见
- [ ] **PermissionGate**：断言显示工具名 + 4 按钮（data-testid）
- [ ] **答 gate**：click「批准」→ 断言 POST /gate/respond（抓网络）+ 弹窗消失
- [ ] **抢答**：注入 resolved 事件（evaluate store）→ 断言 toast 可见
- [ ] **chart 渲染**：注入 custom(chart,line) → 断言 `.recharts-line` 可见
- [ ] **5 种图**：各注入一种 → 断言对应 widget
- [ ] **学术配色**：read SVG fill → 断言在 PALETTE
- [ ] **实时更新**：同 label+title 两次 → 断言 chart 数 == 1

### D4.2 测试骨架
```python
@pytest.mark.integration
async def test_gate_dialog_and_respond(live_server):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(f"{live_server.url}/runs/{run_with_gate}")
        await page.wait_for_selector("[data-testid=gate-dialog]")
        # 断言工具名显示
        tool = await page.locator("[data-testid=gate-tool]").text_content()
        assert tool == "Bash"
        # 点批准
        async with page.expect_request("**/gate/respond") as req:
            await page.click("[data-testid=gate-approve]")
        assert "allow" in (await req.value.post_data)
        await browser.close()

@pytest.mark.integration
async def test_chart_renders_with_palette(live_server):
    async with async_playwright() as p:
        # ... 注入 chart 事件
        await page.wait_for_selector(".recharts-line path")
        fill = await page.locator(".recharts-line path").get_attribute("stroke")
        assert fill in PALETTE_HEX
```

---

## 5. 总验收（Definition of Done）

### 5.1 单元测试（vitest）
- [ ] D1 gate（两种 source + POST + 不乐观 + 抢答）
- [ ] D2 chart 骨架（theme + filter + 分组 + 实时更新）
- [ ] D3 五种 widget（渲染 + 配色）

### 5.2 playwright（关键）
- [ ] D4 gate 弹窗 + 答题 + 抢答 + chart 5 种 + 配色 + 实时更新

### 5.3 5 条铁律（SPEC §0.1）
- [ ] gate 状态从 store 读（grep 弹窗组件无 useState 存 gate）
- [ ] gate 走后端 resolve（前端只 POST）
- [ ] 抢答广播（resolved → 关弹窗 + toast）
- [ ] chart 是事件（从 store filter，不单独存）
- [ ] 复用 AgentHarness chartTheme（grep PALETTE 颜色值一致）

### 5.4 构建
- [ ] `npm run build` 含 gate + chart 组件

### 5.5 交付物
- [ ] gate/ + chart/ 组件
- [ ] chartTheme.ts（迁移）
- [ ] tests + playwright
- [ ] **commit `feat(web):` 前缀，独立分支**

---

## 6. 不做（边界，SPEC §5）

render_chart MCP 工具（phase 10）· 其他 8 种图 · chart 三通道投递 · 对话内联 chart · 后端/store/DAG（9a/9b/9c）

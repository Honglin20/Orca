# 开发计划 —— 阶段 9b：iface/web 前端骨架

> **状态**：待执行（**phase 9a 实现完成后开工**）
> **SPEC**：[`docs/specs/phase-9b-web-frontend-core.md`](../specs/phase-9b-web-frontend-core.md)
> **前置**：phase 9a（后端 API + WS）
> **commit 规范**：`feat(web):` 前缀，独立分支 `phase9-web`

---

## 0. 产出与执行顺序

```
orca/iface/web/frontend/
├── package.json           B0（react/vite/zustand/tailwind/router）
├── vite.config.ts         B0（outDir: ../static）
├── index.html             B0
└── src/
    ├── main.tsx           B0
    ├── App.tsx            B1（路由 + Layout 壳）
    ├── stores/
    │   └── workflow-store.ts   B2（Zustand 单 store + handler 表）
    ├── hooks/
    │   ├── use-run-events.ts   B3（懒加载/卸载）
    │   ├── use-runs-list.ts    B3（元数据轮询）
    │   └── use-websocket.ts    B3（按需订阅 + 重连）
    ├── components/
    │   ├── layout/             B4（TopBar/RunsSidebar/StatusBar）
    │   └── pages/              B4（RunsListPage/NewRunPage/RunDetailPage 骨架）
    └── types/
        └── events.ts           B0（对齐 phase 1 EventType）
+ frontend/test/ × 3
+ tests/iface/web/test_playwright_9b.py（playwright）
```

执行顺序：B0 脚手架 → B1 路由/Layout → B2 store → B3 hooks → B4 页面 → B5 playwright

---

## B0. 脚手架（Vite + 依赖）

### B0.1 `frontend/` 初始化
- `npm create vite@latest frontend -- --template react-ts`
- `package.json` deps：react 19 / react-router v6 / zustand / tailwindcss v4 / react-resizable-panels
- `vite.config.ts`：`build.outDir: '../static'`，`emptyOutDir: true`
- `index.html` + `main.tsx`
- `types/events.ts`：WorkflowEvent 类型（对齐 phase 1 EventType + data 字段）

### B0.2 验收（B0）
- [ ] `cd frontend && npm install && npm run build` 成功，产物在 `../static/`
- [ ] `npm run dev` 启动 dev server

---

## B1. 路由 + Layout 壳

### B1.1 `src/App.tsx`
- BrowserRouter + Routes（`/` / `/runs/new` / `/runs/:runId`）
- `Layout` 组件：TopBar + (RunsSidebar + main) + StatusBar

### B1.2 验收（B1）
- [ ] 三个路由都能渲染（页面骨架）
- [ ] TopBar/RunsSidebar/StatusBar 占位组件显示

---

## B2. Zustand 单 store

### B2.1 `src/stores/workflow-store.ts`
- `WorkflowState` 接口（业务派生 + UI 交互态）
- `eventHandlers` 表（覆盖所有 EventType）
- `processEvent` / `replayState` / `loadRun` / `unloadRun` actions

### B2.2 验收（B2）— `frontend/test/store.test.ts`
- [ ] 一个 store（`grep "create(" src/stores/` 只有 1 个）
- [ ] eventHandlers 覆盖所有 EventType（断言 keys 长度）
- [ ] **fold 幂等**：同事件 processEvent 两次，nodes 状态一致（不翻倍/不拼接）
- [ ] loadRun：fetch events → replayState → nodes 正确
- [ ] unloadRun：events/nodes/gate 清空

### B2.3 测试骨架
```typescript
test('fold idempotent', () => {
  const { result } = renderHook(() => useStore());
  act(() => {
    result.current.processEvent({ type: "node_completed", data: { node: "a", output: {x:1} }, ...});
    result.current.processEvent({ type: "node_completed", data: { node: "a", output: {x:1} }, ...});
  });
  expect(result.current.nodes.a.status).toBe("done");  // 不翻倍
  expect(Object.keys(result.current.nodes).length).toBe(1);  // 不重复
});

test('unloadRun clears', () => {
  // loadRun("a") → unloadRun("a") → events/nodes 为空
});
```

---

## B3. Hooks（懒加载 + WS）

### B3.1 `use-run-events.ts` + `use-runs-list.ts` + `use-websocket.ts`
- 按 SPEC §4 §5 实现

### B3.2 验收（B3）— `frontend/test/hooks.test.tsx`
- [ ] useRunEvents：mount → fetch /events 被调；unmount → unloadRun
- [ ] useRunsList：fetch /api/runs 被调，setInterval 轮询
- [ ] useWebSocket：subscribe(run_id) → onmessage processEvent

---

## B4. 页面组件（骨架）

### B4.1 `components/layout/`（TopBar/RunsSidebar/StatusBar）
- TopBar：Logo + 导航链接 + WS 状态
- RunsSidebar：useRunsList + navigate（点击）
- StatusBar：当前 run + event count + theme

### B4.2 `components/pages/`
- RunsListPage：表格 + 点击 navigate
- NewRunPage：表单 → POST /api/run → navigate
- RunDetailPage：useRunEvents + useWebSocket + tab 占位（dag/log/output，9c/9d 填）

### B4.3 验收（B4）
- [ ] RunsListPage 显示元数据列表
- [ ] 点击 run → 导航到 /runs/<id>
- [ ] NewRunPage 表单提交 → POST /api/run → 跳转
- [ ] RunDetailPage 显示 run_id + tab 占位

---

## B5. playwright 验收（AI 自动测，关键）

### B5.1 `tests/iface/web/test_playwright_9b.py`（@pytest.mark.integration）
> 安装 playwright-mcp（AI 测用）+ playwright python（脚本断言）。
- [ ] **后退语义**：导航 /runs/A → /runs/B → `page.goBack()` → URL 回到 /runs/A（断言）
- [ ] **后退不回主页**：从列表进 A，后退 → 回列表（不是空白主页）
- [ ] **懒加载网络**：`page.on('request')` 抓 `/api/runs` 被调；首页加载**不调** `/events`（断言无 /events 请求）
- [ ] **点开才加载**：点 run → `/api/runs/<id>/events` 被调（断言）
- [ ] **URL 直接访问**：`page.goto('/runs/<id>')` 打开详情
- [ ] **新 run 表单**：填表 → 提交 → 跳转到新 run 页

### B5.2 测试骨架
```python
@pytest.mark.integration
async def test_back_button_semantics(live_server):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(f"{live_server.url}/runs/runA")
        await page.goto(f"{live_server.url}/runs/runB")
        await page.go_back()
        assert "runA" in page.url  # 后退到 A，不是主页
        await browser.close()

@pytest.mark.integration
async def test_lazy_loading(live_server):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        requests = []
        page.on("request", lambda r: requests.append(r.url))
        await page.goto(live_server.url)  # 首页
        # 首页不应调 /events
        assert not any("/events" in r for r in requests)
        # 点击第一个 run
        await page.click("[data-testid=run-item]")
        # 现在应调 /events
        assert any("/events" in r for r in requests)
        await browser.close()
```

---

## 6. 总验收（Definition of Done）

### 6.1 单元测试（vitest）
- [ ] B2 store（fold 幂等/loadRun/unloadRun）
- [ ] B3 hooks（懒加载/WS）

### 6.2 playwright（关键）
- [ ] B5 后退语义 + 懒加载网络断言

### 6.3 6 条铁律（SPEC §0.1）
- [ ] 懒加载（playwright 抓网络验证）
- [ ] 前端无业务真相（grep store 无持久化业务状态，只有 events 缓存）
- [ ] URL 路由后退（playwright 验证）
- [ ] 单 store + 单 fold（grep 1 个 create）
- [ ] WS 按需订阅 + 重连全量
- [ ] 依赖单向（前端只调 API/WS）

### 6.4 构建
- [ ] `npm run build` 产物到 `orca/iface/web/static/`
- [ ] 后端 `GET /` 能 serve 前端（9a 已配 static 挂载）

### 6.5 交付物
- [ ] frontend/ 完整结构
- [ ] tests + playwright
- [ ] **commit `feat(web):` 前缀，独立分支**

---

## 7. 不做（边界，SPEC §9）

DAG/replay UI（9c）· gate 弹窗/chart（9d）· 后端（9a）

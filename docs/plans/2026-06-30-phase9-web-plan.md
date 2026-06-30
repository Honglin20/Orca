# 开发计划 —— 阶段 9 总览：Web 壳（4 子阶段整合）

> **状态**：待执行（**phase 5 + 6 实现完成后开工**）
> **范围**：iface/web Web 壳整体（FastAPI 后端 + React 前端 + DAG + replay + gate + chart）
> **子阶段**：[9a 后端](2026-06-30-phase9a-web-backend.md) · [9b 前端骨架](2026-06-30-phase9b-web-frontend-core.md) · [9c DAG+replay](2026-06-30-phase9c-web-dag-replay.md) · [9d gate+chart](2026-06-30-phase9d-web-gate-chart.md)
> **commit 规范**：**所有 web commit 须 `feat(web):` 前缀，在独立分支 `phase9-web` 开发**（Web 是纯渲染/转发层，与引擎改动严格分离）
> **测试原则**：每个 TASK 有明确验收 + playwright 端到端断言（AI 自动测，不用手动点）

---

## 0. 为什么拆 4 个子阶段

Web 壳工作量大（后端 + 前端骨架 + 复杂视图 + 交互渲染），塞一个 SPEC 会失控。拆 4 份独立 SPEC + 计划：
- 每份聚焦、可独立 review/commit
- 依赖链清晰（9a→9b→9c/9d）
- 每份有自己的 playwright 验收，验收粒度细

---

## 1. 依赖链与执行顺序

```
phase 5 (Orchestrator) + phase 6 (gates) 完成
            │
            ▼
        9a 后端（FastAPI + WS + RunManager 真并发 + 懒加载 API）
            │
            ▼
        9b 前端骨架（路由导航栈 + Zustand 单 store + 懒加载 + WS hook）
            │
       ┌────┴────┐
       ▼         ▼
    9c DAG+    9d gate+
    replay     chart
   （可并行）  （可并行）
       └────┬────┘
            ▼
        整体集成 + 全流程 playwright
```

**关键**：9a 必须先于 9b（前端依赖后端 API）；9c/9d 都依赖 9b，但互相独立可并行。

---

## 2. TASK 矩阵（全部子阶段汇总）

| 子阶段 | TASK | 核心产出 | 验收方式 |
|---|---|---|---|
| **9a** | A1 RunManager | 真并发 + 懒加载元数据 | pytest（并发/懒加载/元数据一致）|
| | A2 WebSocket | 单通道 + 按需订阅 + gate_response | pytest（不推未订阅）|
| | A3 server+routes | REST 懒加载契约 | pytest（body 无 events）|
| | A4 导出+集成+playwright | 全流程 | playwright（API/WS 断言）|
| **9b** | B0 脚手架 | Vite + 依赖 | build 成功 |
| | B1 路由+Layout | react-router URL 路由 | 三路由渲染 |
| | B2 Zustand store | 单 store + handler 表 + fold 幂等 | vitest（fold 幂等）|
| | B3 hooks | 懒加载 + WS | vitest |
| | B4 页面骨架 | RunsList/NewRun/RunDetail | 组件渲染 |
| | B5 playwright | 后退语义 + 懒加载网络 | playwright（goBack + 抓网络）|
| **9c** | C1 DAG | ReactFlow + dagre + 回环边 + 增量更新 | vitest + playwright 截图 |
| | C2 replay | 增量 apply + checkpoint + live==replay | vitest（byte-identical 断言）|
| | C3 Log+Detail | 虚拟滚动 + session 分组 | vitest |
| | C4 playwright | DAG + replay 拖动 + 不卡 | playwright（measure <100ms）|
| **9d** | D1 gate 弹窗 | 两 source 渲染 + POST + 抢答 | vitest（不乐观更新）|
| | D2 chart 骨架+theme | 迁移 chartTheme + filter + 分组 | vitest（实时更新）|
| | D3 五种 widget | line/bar/scatter/pareto/table + 配色 | vitest（PALETTE fill）|
| | D4 playwright | gate + chart + 配色 | playwright（5 种图渲染）|

---

## 3. 整体铁律（贯穿 9a-9d，违反即返工）

### 3.1 唯一真相源（最重要，反 AgentHarness）
1. **后端 tape 唯一真相**：无并行内存事件 list（grep 后端无持久化 events，只有 tape.replay）
2. **前端无业务真相**：store = tape fold 派生物 + UI 交互态（grep 前端 store 无持久化业务状态）
3. **live == replay**：同一 apply_event fold，replay 末尾状态 == live 末尾（断言）

### 3.2 懒加载（反 AgentHarness 性能问题）
4. **`/api/runs` 只元数据**（不含事件，playwright 抓网络断言）
5. **点开 run 才加载事件**，切走卸载（store.events 清空）

### 3.3 页面管控（反 AgentHarness 后退回主页）
6. **URL 路由 + 浏览器后退**（react-router，playwright goBack 断言回上一页非主页）

### 3.4 真并发 + 解耦
7. **多 run 真并发**（asyncio.gather + max_concurrent）
8. **Web 纯渲染/转发**（不含编排/gate 决策，依赖单向）

### 3.5 复用 + 测试
9. **render_chart 复用 AgentHarness**（chartTheme + 5 种图 + label/title 实时更新）
10. **playwright 自动测**（不用手动，AI 用 playwright-mcp 验收）

---

## 4. 整体 playwright 端到端验收（全流程，phase 9 完成标志）

> 安装 **playwright-mcp**（`github.com/microsoft/playwright-mcp`，AI 自动测用）+ **playwright python**（脚本断言用）。
> 以下为 phase 9 整体交付时的端到端验收（整合 9a-9d 各自的 playwright）：

### 4.1 完整 user journey（playwright 自动跑）
- [ ] **启动**：playwright 打开 localhost:7428 → 断言 Runs 列表可见
- [ ] **新建 run**：点 +New → 填表 → 提交 → 断言跳转到新 run 页 + status=running
- [ ] **懒加载**：首页加载不调 /events（抓网络）；点 run 才调
- [ ] **DAG 实时**：run 跑时断言节点状态变化（pending→running→done，颜色变）
- [ ] **回环边**：nas.yaml DAG 布局合理（截图）
- [ ] **gate**：触发 gate → 弹窗 → 点批准 → 继续
- [ ] **抢答**：模拟别壳答 → 弹窗关 + toast
- [ ] **chart**：注入 chart 事件 → recharts 渲染 + PALETTE 配色
- [ ] **run 完成**：status=completed → 出现 Replay 按钮
- [ ] **replay**：点 Replay → 拖滑块 → 断言 DAG 回到该时刻 + 不卡（<100ms）
- [ ] **live==replay**：replay 拖到末尾 → 断言状态 == live 末尾
- [ ] **历史 run**：回主页 → 点 Done run → 断言进 replay 模式
- [ ] **后退语义**：A→B→goBack → 断言回 A（非主页）
- [ ] **URL 分享**：goto('/runs/<id>') 直接打开

### 4.2 多 run 真并发
- [ ] 同时启动 3 个 run → 断言 3 个都 running（并发）
- [ ] 切换查看不同 run → 各自事件不串

---

## 5. Definition of Done（phase 9 整体）

### 5.1 子阶段全完成
- [ ] 9a 后端（A1-A4）
- [ ] 9b 前端骨架（B0-B5）
- [ ] 9c DAG+replay（C1-C4）
- [ ] 9d gate+chart（D1-D4）

### 5.2 整体铁律（§3 的 10 条）
- [ ] 全部 playwright + grep 断言通过

### 5.3 整体 playwright（§4）
- [ ] 完整 user journey 全过

### 5.4 全量回归
- [ ] `uv run pytest -q`（后端单元）全绿
- [ ] `cd frontend && npm test`（前端 vitest）全绿
- [ ] `pytest -m integration`（playwright）全绿
- [ ] phase 1-8 测试零回归

### 5.5 交付物
- [ ] orca/iface/web/（后端 7 文件 + 前端完整）
- [ ] chartTheme.ts（迁移自 AgentHarness）
- [ ] tests/iface/web/（pytest + playwright）
- [ ] frontend/test/（vitest）
- [ ] **所有 commit `feat(web):` 前缀，独立分支 `phase9-web`**
- [ ] release note + CHANGELOG

### 5.6 里程碑
- [ ] **phase 9 完成 = Orca 有生产级 Web UI**（多 run 管理 + DAG + gate + replay + chart，懒加载 + 后退正确 + 唯一真相源）

---

## 6. 不做（phase 9 整体边界）

- ❌ **render_chart MCP 工具**（让 claude 能调）—— phase 10
- ❌ **MCP 壳** —— phase 10
- ❌ **其他 8 种 chart**（heatmap/box/radar 等）—— 后续按需
- ❌ **多用户认证** —— 后续
- ❌ **编排/gate 决策逻辑**（web 纯渲染/转发）
- ❌ **并行内存事件 list / 多 store / 双 WS / 双 fold** —— 红线（反 AgentHarness）

---

## 7. 子阶段文档索引

| 子阶段 | SPEC | 计划 |
|---|---|---|
| 9a 后端 | [phase-9a-web-backend.md](../specs/phase-9a-web-backend.md) | [2026-06-30-phase9a-web-backend.md](2026-06-30-phase9a-web-backend.md) |
| 9b 前端骨架 | [phase-9b-web-frontend-core.md](../specs/phase-9b-web-frontend-core.md) | [2026-06-30-phase9b-web-frontend-core.md](2026-06-30-phase9b-web-frontend-core.md) |
| 9c DAG+replay | [phase-9c-web-dag-replay.md](../specs/phase-9c-web-dag-replay.md) | [2026-06-30-phase9c-web-dag-replay.md](2026-06-30-phase9c-web-dag-replay.md) |
| 9d gate+chart | [phase-9d-web-gate-chart.md](../specs/phase-9d-web-gate-chart.md) | [2026-06-30-phase9d-web-gate-chart.md](2026-06-30-phase9d-web-gate-chart.md) |

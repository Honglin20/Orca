# Release Note —— phase 9b：iface/web 前端骨架

> **日期**：2026-07-01（计划日期 2026-06-30）
> **分支**：`phase9-web`
> **SPEC**：[`docs/specs/phase-9b-web-frontend-core.md`](../specs/phase-9b-web-frontend-core.md)
> **计划**：[`docs/plans/2026-06-30-phase9b-web-frontend-core.md`](../plans/2026-06-30-phase9b-web-frontend-core.md)
> **状态**：✅ 完成（骨架层；DAG/gate/chart 视图是 9c/9d）

---

## 做了什么

phase 9b 搭起 React SPA 骨架，回答 **「前端骨架怎么搭，才能懒加载、后退正确、状态从唯一真相派生？」**。落地六条铁律（SPEC §0.1）：

1. **懒加载**：`useRunsList` 只轮询 `/api/runs`（元数据，无事件）；`useRunEvents` 在 RunDetailPage mount 时才 `GET /api/runs/<id>/events`；切走 `unloadRun` 清 `store.events`（不累积）。playwright `test_lazy_loading_home_no_events` 抓网络断言首页不调 `/events`。
2. **前端无业务真相**：store = events 的 fold 派生物 + 少量 UI 交互态（selectedNode/replayPosition/activeRunId）。刷新即重拉，状态必然一致。
3. **URL 路由 + 后退语义**：react-router v6 `BrowserRouter`，三个路由（`/`、`/runs/new`、`/runs/:runId`），全部 `navigate` push（非 replace）。playwright `test_back_button_semantics` 断言 A→B→back→A。
4. **单 store + 单 fold（幂等）**：全 `src/` 唯一 `create()`（grep 断言）；单一 `eventHandlers` 表覆盖全部 21 个 EventType；live（WS）和 replay（REST）共用 `processEvent`。**fold 幂等**：seq 去重 + node 状态 last-writer-wins —— `store.test.ts` 显式断言同事件应用两次状态一致、cost 不翻倍。用 **immer middleware** 锁死不可变性（handler 直接 mutate draft，避免手写浅拷贝的越界风险）。
5. **WS 按需订阅 + 重连全量重拉**：`useWebSocket(runId)` open 后 `subscribe(run_id)`；`onmessage` 只处理 `event.run_id === runId`（过滤他 run 噪声）；`onclose`（非主动）→ 指数退避重连，重连流程 = 全量重拉 + 重新 subscribe。**单一加载路径**：初始 open 仅 subscribe（useRunEvents 负责初始全量），仅重连走全量重拉 —— 避免双拉竞态。
6. **依赖单向**：前端只调后端 API/WS，无编排/gate 决策逻辑（`gate_response` 仅 forward）。

### 文件清单

```
orca/iface/web/frontend/
├── package.json              react19/react-router6/zustand5/tailwind3/react-resizable-panels + immer + dev: vite6/vitest3/testing-library/happy-dom
├── vite.config.ts            outDir: ../static, emptyOutDir; vitest config (happy-dom, @ alias)
├── tsconfig.json             单一 tsconfig（noEmit，无 composite 引用）
├── tailwind.config.js + postcss.config.js
├── index.html + .gitignore
├── src/
│   ├── main.tsx + App.tsx    BrowserRouter + Layout 壳（TopBar/RunsSidebar/StatusBar）
│   ├── index.css             tailwind 三件套
│   ├── types/events.ts       WorkflowEvent/RunMeta/NodeState/GateState/WsClientMessage（逐字对齐后端）
│   ├── stores/workflow-store.ts   Zustand 单 store（immer）+ eventHandlers（21 EventType）+ processEvent/replayState/loadRun/unloadRun
│   ├── hooks/
│   │   ├── use-run-events.ts      mount→loadRun, unmount→unloadRun（懒加载）
│   │   ├── use-runs-list.ts       useCallback + setInterval 轮询 /api/runs
│   │   └── use-websocket.ts       subscribe + 按需过滤 + 指数退避重连（重连才全量重拉）
│   └── components/
│       ├── layout/{TopBar,RunsSidebar,StatusBar}.tsx
│       └── pages/{RunsListPage,NewRunPage,RunDetailPage}.tsx   RunDetailPage tab 占位（dag/log/output/yaml，9c/9d 填）
├── test/
│   ├── setup.ts
│   ├── store.test.ts         13 tests：单 store 正则断言 / 21 EventType 覆盖 / fold 幂等 / loadRun/unloadRun / 未知 type 不 crash
│   └── hooks.test.tsx        9 tests：useRunEvents 懒加载/卸载 / useRunsList 轮询+unmount 清 interval / useWebSocket subscribe+按需过滤+重连全量+主动关不重连
tests/iface/web/test_playwright_9b.py   6 tests @pytest.mark.integration：后退语义 / 懒加载网络 / URL 直接访问 / 新 run 表单
orca/iface/web/static/.gitignore + .gitkeep   vite outDir（构建产物 ignored，.gitkeep 让目录存在）
```

### 验证

- **vitest**：`cd orca/iface/web/frontend && npm test` → **22 passed (22)**（store 13 + hooks 9）
- **build**：`npm run build` → 成功，产物到 `orca/iface/web/static/`（index.html + assets/）
- **Python**：`uv run pytest -q -m "not integration" -W "error::RuntimeWarning"` → **594 passed, 24 deselected, 0 RuntimeWarning**（phase 1-9a 零回归；6 条新 playwright integration 正确 deselected）
- **单 store grep**：`grep -rn "create<" src/` → 唯一命中 `workflow-store.ts`
- **playwright collect**：`pytest tests/iface/web/test_playwright_9b.py --collect-only` → 6 tests collected（playwright 未安装则 skipif）

---

## 关键决策（防 drift）

1. **Tailwind v3 而非 v4**：v4 的 CSS-first + vite 插件在 React 19 / vite 6 / Node 24 组合下稳定性尚不足（mid-2026）；SPEC §1 + plan 明确允许 pin v3 并记录理由。v3 的 `@tailwind base/components/utilities` + JS config 最稳，本阶段样式只需编译通过 + 观感合理。
2. **vitest 3.x**：vitest 2.x 内部 bundle vite 5，与顶层 vite 6 类型冲突（`Plugin` 不兼容）；升 vitest 3（针对 vite 6）解决。
3. **immer middleware**：handler 直接 mutate draft，immer 生成新引用 —— 反 AgentHarness 手写浅拷贝越界风险，是 9c/9d 加复杂 handler 前的不可变性安全锁（review M1）。
4. **单一加载路径**：useRunEvents 负责初始 `/events` 加载，useWebSocket 仅在**重连**时全量重拉（初始 open 只 subscribe）—— 消除进入详情页的双拉竞态（review M2，SPEC §4.1 + §5.2.2 协调）。
5. **build 脚本 `tsc --noEmit && vite build`**：弃用 `tsc -b` + composite project references（vite.config.ts 不需要独立 tsconfig），消除 stray `vite.config.js/.d.ts` 产物（review m7/m8）。
6. **happy-dom 而非 jsdom**：更快，vitest config `environment: "happy-dom"`。WebSocket 全局在两者都缺失，测试用 `deps.createSocket` 注入 mock + hook 内用 `READY_OPEN=1` 字面量（不依赖 `WebSocket.OPEN` 全局）。

---

## 偏差与说明

- **DAG/replay/gate/chart 视图未实现**：RunDetailPage 的 dag/log/output/yaml 四个 tab 仅占位（SPEC §9 明确不做，是 9c/9d scope）。
- **n4 双轮询 deferred 到 9c**：RunsListPage + RunsSidebar 各自 `useRunsList` → 每 2s 两次 `/api/runs`（元数据，廉价）。根治需 React Context 提升，属 9c scope（review 二次确认 deferral 合理）。当前两组件不会同时挂载（sidebar 在详情页布局、listpage 在主页），实际并发概率低。
- **playwright `test_back_to_list_not_blank`**：断言为 `page.url == base_url + "/" or page.url == base_url`（接受 trailing-slash 归一化变体）+ 反向 `"/runs/" not in page.url` 收紧。理想严格 `==` 但浏览器归一化是真实变体，不阻塞。

---

## review 结果

- **一轮**（6 铁律 + 类型对齐 + DRY/SOLID + 测试意图）：6 铁律全部满足（file:line 证据）；TS 类型逐字对齐后端；3 Major + 5 Minor + 2 Nit findings。
- **整改**：Major 3/3 FIXED（immer / 单一加载路径 / WorkflowStatus 导出）；Minor 4/5 FIXED（gate fail loud / useCallback / cleanup callbacks / build artifacts），m5 部分达标非阻塞，**n4 DEFERRED 到 9c**（合理）；Nit 2/2 FIXED。
- **二轮**：所有 Major 已修复 + 回归测试全绿（22/22 + build + 单 store grep=1）+ 无新缺陷引入。

---

## Commit

- `feat(web): phase 9b 前端骨架 — react-router 路由 + Zustand 单 store fold + 懒加载/WS hooks + 页面骨架`（commit `0347a66`）

## 给后续阶段的契约（SPEC §8）

| 后续 | phase 9b 提供 |
|---|---|
| phase 9c dag-replay | `RunDetailPage` 容器 + store（nodes/gate）+ `useRunEvents`/`useWebSocket` + events 缓存 |
| phase 9d gate-chart | `store.gate`（gate 弹窗读）+ events filter `custom`（chart 渲染读） |

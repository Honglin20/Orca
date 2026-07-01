# 2026-07-01 — phase 9 浏览器 E2E 修复

## 改动点

phase 9 已合并 master，前端在浏览器中实际可用（DAG 渲染 / replay 拖动 / 懒加载 / 路由均人工验证通过），
但 playwright E2E 套件因若干 **测试代码 bug + 一个真实后端 bug** 没跑绿。本次修复让 20 个 E2E 全绿。

### 1. 真实后端 bug：SPA 深链 404（`orca/iface/web/server.py`）

`create_app` 只把 `/` 挂 StaticFiles，**没有 SPA fallback** —— 前端用 BrowserRouter（客户端路由），
深链 `/runs/<id>` 在后端无对应文件 → 返回 `{"detail":"Not Found"}` 404，整个 run 详情页废（刷新/直链均不可用）。

修复：挂 `/assets`（vite hashed JS/CSS）+ 注册 catch-all `GET /{full_path:path}` → 返回 `index.html`
（FileResponse）。catch-all 在所有 API router（`/api/runs` `/api/run` `/gate` `/gate/respond`）和 `/ws`
之后注册，且仅 GET —— POST `/gate/respond` 等 API 不受影响（路由顺序 + 方法隔离双重保证）。
前端未构建时返回可操作提示（非裸 404）。

### 2. `live_server` fixture 端口就绪检测（4 个测试文件）

原 `loop.run_until_complete(asyncio.sleep(0.3))` 在已 running 的 loop 上调用 → 报错。
改为端口轮询（`socket.create_connection` 直到 server accept 就绪）。

### 3. WS live 推送测试 race（`test_playwright.py::test_playwright_ws_subscribe`）

原用 `echo hi`（ms 级完成），run 终态后 bus.close（teardown）—— 此时再 subscribe 拿不到任何 live 事件。
改为 `_slow_yaml`（`sleep 5`）：subscribe 完成时 run 仍 running，pump task 把
`node_started`/`node_completed` 真推给 WS。三重断言：
  - 收到 > 0 事件（pump 真推送，非只 ack subscribe）
  - 每条事件 `run_id == rid`（pump 标签正确，前端能按 run 分发）
  - 至少一条 type ∈ {workflow_started, node_started, node_completed, workflow_completed, workflow_failed}
    （转发自 bus fan-out，非 WS 层自造）

时序：subscribe ~0.5s 完成，`node_completed` run-t≈5s emit，收集窗口 6s（≥0.5s 余量防 CI 抖动）。

### 4. `test_new_run_form` 错误的 run_id URL 模式（`test_playwright_9b.py`）

原 `wait_for_url("**/runs/run-*")` + `assert "/runs/run-" in page.url`。
run_id 实际格式 `<slug>-<YYYYMMDD-HHMMSS>-<nanoid6>`（如 `demo-20260701-075614-7f6455`），不是 `run-*`。
改为 `wait_for_url("**/runs/demo-*-*")` + 断言 URL 已离开 `/runs/new` 且含 `/runs/demo-`。
（表单 submit handler 是 `fetch POST /api/run → navigate(/runs/${run_id})` 客户端导航，非 redirect。）

### 5. cyclic 布局测试 playwright API 名（`test_playwright_9c.py::test_cyclic_layout_no_overlap`）

`await nodes.allBoundingBoxes()` —— Python playwright 的 Locator **没有** `all_bounding_boxes`/`allBoundingBoxes`
方法。改用 `nodes.evaluate_all(...)` 一次 `getBoundingClientRect` 往返拿全部节点 box（避免 N 次 bounding_box 往返）。

### 6. 9d async 测试不执行（`test_playwright_9d.py`）

`class TestGateAndChart` 6 个 `async def test_*` —— 项目无 pytest-asyncio 插件（约定 sync `def` + `asyncio.run(coro())`），
pytest 直接 skip/warn。改为每个 `async def _X(self, ...)` + `def test_X(self, ...): asyncio.run(self._X(...))`。

### 7. 9d chart 测试挂载点（`test_playwright_9d.py`）

4 个 chart 测试在 `/?debug=1`（首页）注入 chart 事件 expecting widget 渲染 —— 但 `<ChartRenderer />`
**只在 `RunDetailPage` 的 `tab==="output"` 分支挂载**，首页注入 store 收了事件但无组件消费 → timeout。
（GateDialog 挂在 app 根，故 gate 测试在首页注入能过；chart 是 page-scoped。）
新增 `_goto_output_tab(page, base_url)` helper：导航到 `/runs/debug-chart-stub?debug=1`（虚构 run_id，
后端 404 不影响——debug 注入绕过后端，store.processEvent 直接调）+ 点 `[data-testid=tab-output]` 挂载 ChartRenderer。

## 偏差

- **端口轮询 block 在 4 个测试文件逐字重复**（DRY 违规）—— reviewer 指出，但抽到共享 conftest.py 是更大范围
  重构（4 个 `live_server` fixture 有细微差异，其中 `test_playwright.py` 多 yield 一个 `loop`），超出本次
  「测试 bug 修复」surgical 范围。**记入 9c deferred 项**（与 n4 双轮询同期待根治）。

## 验收

- `uv run pytest tests/iface/web/test_playwright{,_9b,_9c,_9d}.py -v` → **20 passed**（test_playwright 3 + 9b 6 + 9c 5 + 9d 6）
- `uv run pytest -q -m "not integration"` → **599 passed, 0 warnings**（SPA fallback 不影响 `/api/*` `/ws` `/gate` 路由）
- `cd orca/iface/web/frontend && npm test` → **84 passed**

## Commit

- `fix(web): phase 9 浏览器 E2E 修复 —— SPA fallback(深链 404) + live_server fixture + 测试 bug(run_id/WS/playwright API/async)`

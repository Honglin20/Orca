# Release Note — 2026-08-10：详情页返回主页导航修复（window.location → SPA navigate）

## 问题

详情页 TopBar 返回按钮用 `window.location.href = "/"`（**整页刷新**）——重新加载整个 SPA bundle
+ 重新 discovery，慢且丢失 `run-list-store` 内存状态，用户体感"没法后退"（点 ← 后卡顿/状态重置）。

## 修复

`orca/iface/web/frontend/src/components/layout/TopBar.tsx` 返回按钮改 react-router `useNavigate` →

```js
const navigate = useNavigate();
onClick={() => navigate("/")}
```

SPA 内导航（不整页刷新）：快、保留 store 状态、URL 正常回 `/`。`useNavigate` 在 TopBar 安全
（react-router v6.28，RunListPage/WorkflowsPage/RunLoadError 等多处已在用）。

## 附带发现（test fixture，**非产品 bug**）

调查中真机暴露：详情页 `AgentsRail` 的 `selectAgentGroups` 迭代 `def.routes`（`selectors.ts:171`），
而 Playwright test fixture 的 tape topology 只写了 `{entry, nodes}`、**缺 `routes` 字段** →
`for (const r of def.routes)` → `routes is not iterable` → 详情页白屏。

真实 run 的 topology 含 `routes`（workflow 定义产出），不受影响。根因是 test fixture 不完整，
既有 `test_playwright_runlist` 只验"进详情页 URL"不验渲染，故一直漏网。修 `test_back_navigation.py`
fixture topology 补 `routes: []`。

## 验证

Playwright 真机 **13 passed**：
- `test_back_navigation`（1）：进详情页 → 点 ← → URL 回 `/` + 主页 board-card 重渲染（SPA navigate，不整页刷新）。
- `test_playwright_runlist`（10）：主页 SPA 看板/列表/选择/搜索/主题/折叠持久全契约（确认改动不破坏前端）。
- `test_playwright_card_metrics`（2）：卡片 event_count/chart_count DOM。

> 测试点击用 `eval_on_selector`（JS click）：详情页 elapsed tick 高频重渲染致 button DOM 短暂
> detach，Playwright strict click 反复 retry 抓不到；JS click 一次性触发，等价真人点击不受 tick 影响。

## 环境（本次真机验证前置）

WSL Playwright chromium 缺系统库，用 `apt-get download` + `dpkg-deb -x` + `LD_LIBRARY_PATH`
（无 sudo）解决——见 `docs/releases/2026-08-10-home-list-lazy-index.md` 遗留段 + memory
`wsl-playwright-no-sudo-deps`。

Commit: `0c69a54` 之后（TopBar navigate + test_back_navigation + static rebuild）。

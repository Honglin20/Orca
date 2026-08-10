# Release Note — 2026-08-10：卡片事件数对齐 log + 图表数字段

## 问题

主页卡片"事件数"三个问题：

1. **在别处服务器显示 0**：`discover_runs` in-memory 分支 `event_count=0` 硬编码占位符——in-session live run 走此分支，卡片"事件"永远 0。
2. **语义错**：`event_count` 是 `_scan_meta_overview` 全量 count（含 `agent_tool_*`/`agent_message`/`thinking`），用户要的是 **log 行数**（node 级生命周期事件，排除工具调用）。
3. **缺图表数**：卡片无图表数指标。

## 修复（SPEC `2026-08-10-card-event-log-align.md` v3，spec-reviewer 两轮 FAIL→CONDITIONAL-PASS→PASS）

### 后端 `orca/iface/web/run_manager.py`

- **§3.1 双分支 `log_event_count`（F1 BLOCKER）**：`_scan_meta_overview` 单遍，**fast-path 与 full-parse 两分支都查 `_LOG_EVENT_TYPES` 白名单**计数。新增 `_META_TYPE_RE` regex（fast-path 提取真实 type）。F1 核心问题：`_META_BULK_MARKERS`（29 个）含 18 个 log 白名单类型（retry_*/validator_*/wait_*/dialog_*/foreach_started/completed/interrupt_*/human_decision_*/workflow_resumed/error）走 fast-path `continue`，单 full-parse 计数漏 ~70%。fast-path type check 放 `if m:` 块内 `count += 1` 后（NEW-1，无 seq 事件同时被 count 和 log_event_count 排除）。白名单 = 前端 `classifyLogLevel` 非 null 且非 route_taken（U1，26 类型逐字对齐）。
- **§3.2 `chart_count` 去重（F4 + NEW-2）**：identity = `title if isinstance(title,str) else f"{chart_type}#{seq}"`，对齐前端 `selectCharts`。**isinstance 守卫**（非 `str(... or ...)`——F4 空 chart_type edge case：Python `"" or` 把空串当 falsy，与前端 `typeof === "string"` 分歧）。charts list append 也改同款 isinstance（NEW-2 Option A，DRY + huge-mode 一致）。
- **§3.3 RunSummary + meta 双语义分离**：`RunSummary.event_count` 全量→log 行数（卡片）+ 新增 `chart_count`；`RunMetaExtended.event_count`（meta）保持全量（huge 判定 `>50000` 依赖）。docstring 双语义对照表防混淆。
- **§3.4 in-memory 分支修复（ISSUE-B）**：`event_count`/`chart_count` 不再硬编码 0；**直调 `_scan_meta_overview`（不经 cache）**——live tape cache 即时失效，直调避免每 8s poll 触发持久 writeback。status 仍取 `handle.status` hint。
- **§3.5 cache v2→v3 五处**（3 代码 + 2 doc，只改 gate 会无限重建）。

### 前端

- `RunRow.tsx` + `BoardCard.tsx` 加 `chart_count` metric（图标 + 数，label "图表"）。
- `run-list-store.ts` RunSummary TS 加 `chart_count: number`。
- `store-types.ts` `ServerOverview` 加 optional `log_event_count?`/`chart_count?`（meta huge overview 会带，不用即忽略）。

## 验证

- **单测**：12 新测（AC4 双分支 fixture 含 retry_started 守门 + NEW-1 placement + AC5 `_LOG_EVENT_TYPES`==`classifyLogLevel` 集合相等 + AC6 chart 去重含空 chart_type edge case + AC7 双语义 + F3 fallback + in-memory 异常路径）+ 非 Playwright **299 passed** + 前端 **tsc 干净 / 535 vitest / vite build** 全绿。
- **code-reviewer 一轮 0 MUST-FIX / 2 SHOULD-FIX 全修**（AC4 fixture 精确匹配 SPEC + in-memory 异常路径补测）。
- **test-agent 真机 HTTP**（uvicorn + httpx，独立 oracle 重实现白名单**不 import 后端**对账）：
  - **AC1** `event_count` == log 行数零偏差（rich 6 / minimal 4 / toolheavy 3）；F1 双分支真机守门（rich 含 retry_started/succeeded 走 fast-path 被计入，bug 模型会是 4，真机得 6）+ tool call 排除（toolheavy 9 行含 5 tool_call + 1 message，event_count=3）+ route_taken 排除。
  - **AC2** in-memory 分支 `event_count` 非 0（orphan tape attach → in-memory 分支 → 6，§1.1 根因修复，原 bug 会是 0）。
  - **AC3** `chart_count` == 5（同 title 去重 + 无 title + 空 title + 空 chart_type edge case，F4 isinstance 真机验证，零偏差）。

## 遗留

- **纯 `InProcessRunHandle`**（live CC 驱动 in-session workflow）未真机起（需宿主 CC/daemon）。AC2 经 `POST /api/runs/attach` 产生的 `AttachedRunHandle` 验证——与 `InProcessRunHandle` 在 `discover_runs` in-memory 分支走**完全相同**代码路径（`_scan_meta_overview(tp)` fold），故 §3.4 修复经真机覆盖；纯 InProcess 路径由单测兜底。
- **前端浏览器渲染**（后补真机闭环）：同主页懒加载用 `apt-get download` + `dpkg-deb -x` + `LD_LIBRARY_PATH`（无 sudo）
  解决 WSL chromium 系统依赖后，新增 `test_playwright_card_metrics.py` **2 测全绿**——真浏览器断言卡片 DOM：
  board `BoardCard` 第三行 + list `RunRow` metric，`event_count=6`（log 行数，retry fast-path 计入 +
  tool_call/message 排除）+ `chart_count=2`（同 title 去重 + 无 title）。前端 `RunRow.tsx`/`BoardCard.tsx`
  逐字渲染同一 `RunSummary` JSON，卡片显示数即后端验过的数——浏览器端确认。

Commit: `a234b94`（实质）+ `d6fd21d`（CHANGELOG SHA）。SPEC：`docs/specs/2026-08-10-card-event-log-align.md`。

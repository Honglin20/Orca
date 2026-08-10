# SPEC：卡片事件数对齐 log + 图表数字段

> 背景：用户反馈主页卡片"事件数"在别处服务器显示 0，且语义应为 **log 行数**（node 级生命周期事件，排除工具调用）。同时卡片要加"图表数"。
> 流程：本 SPEC → spec-reviewer 闭环（v3：FAIL→CONDITIONAL-PASS，复审明确"修订后直接 PASS"）→ coder-agent 实现 → test-agent e2e → 闭环。
> **前置依赖**：主页懒加载（commit `80ae386`，概要索引化 / `_summary_from_overview` 公共构造器 / cache v2 / `_scan_meta_overview` 单遍 capture workflow_name+started_ts+ended_ts）已完成。
>
> **v3 修订（复审 CONDITIONAL-PASH → 闭环 SHOULD-FIX）**：NEW-1 fast-path type check 放置明示（`if m:` 块内 count+1 后）；F2 cache version 改**五处**（3 代码+2 doc，补 line 1719 docstring）；NEW-2 charts list 也用 isinstance（Option A，DRY+huge 一致）；ISSUE-A/C/B + NEW-4 澄清。

## 0. 范围

- **改**：`RunSummary.event_count` 语义（全量 → log 行数）+ 新增 `chart_count`；`discover_runs` in-memory 分支 `event_count=0` 修复；前端卡片（`RunRow` + `BoardCard`）加图表数显示。
- **不改**：log 面板本身（`classifyLogLevel` 是对齐**基准**，不动）；meta 的全量 event_count（huge 判定依赖）；详情页事件流。
- **已知副作用**：`event_count` 语义从全量→log 后，前端按"事件数"排序（`sort-runs.ts`）的结果会变（含大量 tool call 的 run 从排前变排后）——语义变更的预期副作用，非 bug。

## 1. 问题

### 1.1 bug：in-session live run 卡片事件数恒 0

`discover_runs` in-memory 分支 `event_count=0`（C1 perf 占位符——handle 不持有 event_count）。in-session 跑的 live run 走此分支 → 主页卡片"事件"永远 0。**用户"别处服务器显示 0"的最可能根因**（待 e2e AC2 终验；attached 分支现状 event_count=全量 count 理论非 0，AC2 须覆盖 live 实证）。

### 1.2 语义错：event_count 是全量而非 log 行数

现状 `event_count` = `_scan_meta_overview` 全量 `count`（含 `agent_message`/`thinking`/`tool_*` 等 bulk）。用户要 **log 行数** = `classifyLogLevel` 过滤后的事件（node 级生命周期，排除工具调用/消息/思考）。

### 1.3 缺字段：卡片无图表数

`overview.charts` 已 capture（`_scan_meta_overview` 的 `custom kind=chart` 分支），但**不去重**（同图表 live 刷新重复 append），需对齐前端 `selectCharts` 去重口径（`selectors.ts:selectCharts` identity）。

## 2. 目标 / 非目标

### 目标

- **G1**：`RunSummary.event_count` == LogStream 行数（前端 `selectLog(state).length`，**showDebug=false 默认态**），逐 run 对账零偏差。
- **G2**：in-memory（live）run `event_count` 非 0（真实 log 事件数）。
- **G3**：新增 `chart_count` == 前端实际显示图表数（`selectCharts` 去重后，**非 huge 或 huge loadFull 后**）。
- **G4**：工具调用（`agent_tool_*`）/消息/思考不计入 `event_count`。
- **G5**：前后端 log 过滤 + chart 去重逻辑同步（U1：server-fold = client-fold）。

### 非目标

- 改 log 面板过滤逻辑（`classifyLogLevel` 是基准）。
- 改 meta 全量 event_count（huge 判定依赖）。
- 卡片视觉重构（仅加一个 metric）。

## 3. 契约

### 3.1 后端 `log_event_count` 单遍计数——**双分支**（F1 BLOCKER）

`_scan_meta_overview` 单遍扫 tape，**fast-path 与 full-parse 两个分支都必须提取 type 查白名单计数**。

**为何必须双分支**：`_META_BULK_MARKERS`（`run_manager.py`，29 个 marker）含 **18 个 log 白名单类型**（`retry_started`/`succeeded`/`exhausted`、`validator_started`/`passed`/`failed`、`wait_started`/`completed`、`dialog_started`/`ended`、`foreach_started`/`completed`、`interrupt_requested`/`resolved`、`human_decision_requested`/`resolved`、`workflow_resumed`、`error`）。它们走 fast-path `continue`（在 full-parse 之前），单在 full-parse 计数会**系统性漏计 ~70%**。穷举验证（复审）：18（fast-path）+ 8（full-parse: workflow_started/completed/failed/cancelled + node_started/completed/failed/skipped）= 26 = `_LOG_EVENT_TYPES` 全集，零遗漏。

**log 事件白名单** `_LOG_EVENT_TYPES: frozenset[str]`（= 前端 `classifyLogLevel` 非 null **且非 route_taken**，U1；复审逐字比对 26 类型 4 level 全匹配）：

- **info**：workflow_started, node_started, foreach_started, retry_started, validator_started, wait_started, human_decision_requested, interrupt_requested, dialog_started
- **success**：workflow_completed, workflow_resumed, node_completed, foreach_completed, retry_succeeded, validator_passed, wait_completed, human_decision_resolved, interrupt_resolved, dialog_ended
- **error**：workflow_failed, workflow_cancelled, node_failed, retry_exhausted, validator_failed, error
- **warning**：node_skipped
- **排除** `route_taken`（log 默认隐藏 debug 级）

**双分支计数实现**：

- **fast-path 分支**（`is_bulk` 块内，`continue` **之前**）：新增 `_META_TYPE_RE = re.compile(r'"type":\s*"(\w+)"')`（与 `_META_SEQ_RE` 同级）。**type check 放在 `if m:`（seq 提取）块内、`count += 1` 之后**（NEW-1）——使无 seq 的事件同时被 `count` 和 `log_event_count` 排除（与 full-parse 的 `if not isinstance(seq, int): continue` 一致）：
  ```python
  if is_bulk:
      m = _META_SEQ_RE.search(stripped)
      if m:
          try: seq = int(m.group(1))
          except ValueError: continue
          count += 1
          # ... oldest/newest ...
          tm = _META_TYPE_RE.search(stripped)                      # NEW
          if tm and tm.group(1) in _LOG_EVENT_TYPES:               # NEW
              log_event_count += 1                                  # NEW
      continue
  ```
- **full-parse 分支**（`t = obj.get("type")` 之后）：`if t in _LOG_EVENT_TYPES: log_event_count += 1`。

**为何用真实 type 查表而非依赖 bulk 分类**：fast-path 按 substring 判 bulk 会误判（见主页 SPEC I-8），但在 fast-path 内用真实 type regex 查 `_LOG_EVENT_TYPES` 绕过该问题——即使 bulk 误判，真实 type 查白名单仍准确。

**`_META_TYPE_RE` 依赖 tape payload key 顺序（ISSUE-C）**：`orca/events/tape.py`（payload 构造，key 顺序 `{seq, type, timestamp, node, session_id, data}`，`type` 恒在 `data` 之前）→ `_META_TYPE_RE.search()` 找最左匹配恒命中 top-level type（即使 data 内有嵌套 `"type"` 键，json.dumps 对 `"` escape 也不误匹配）。**改 `tape.py` payload 构造需同步审查此 regex**。

**perf**：`_META_TYPE_RE.search` 是与 `_META_SEQ_RE` 同级廉价 regex，fast-path 仍跳过 `json.loads`（保留主页 SPEC fast-path perf；AC1 <300ms 余量大）。

**null 事件归属**：`classifyLogLevel=null` 的事件（`agent_message`/`thinking`/`tool_call`/`tool_result`/`agent_step_started`/`unknown_event`/`prompt_rendered`/`foreach_item_started`/`completed`/`dialog_message`）**全部在 `_META_BULK_MARKERS` fast-path**，fast-path 提取真实 type 查白名单不命中 → 不计入（正确）。

overview dict 新增 `log_event_count`。

### 3.2 后端 `chart_count` 去重计数（对齐 `selectCharts`，F4 + NEW-2 isinstance）

`_scan_meta_overview` 单遍对 `custom kind=chart` 事件，按前端 `selectCharts` identity 去重（`selectors.ts` 的 `byIdentity`）：

- `identity = title`（当 `title` 是非空 string）`or f"{chart_type}#{seq}"`（title 空/非 str）
- 维护 `seen_chart_ids: set[str]`，`chart_count = len(seen_chart_ids)`
- **seq 来源（ISSUE-A）**：full-parse 分支的 `obj.get("seq")`（与前端 `e.seq` 同源）

**F4 关键——isinstance 守卫匹配前端 typeof**（空字符串分歧）：

```python
# 后端（错→对）：
# ✗ chart_type = str(chart.get("chart_type") or "chart")   # "" or → "chart"（Python falsy）
# ✓ ct_raw = chart.get("chart_type")
#   chart_type = ct_raw if isinstance(ct_raw, str) else "chart"   # "" 是 str → ""（匹配前端 typeof === "string"）
# title 同款：
#   title_raw = chart.get("title")
#   title = title_raw if isinstance(title_raw, str) else ""
```

前端 `selectors.ts`：`typeof chart.chart_type === "string" ? chart.chart_type : "chart"`——JS `typeof "" === "string"` 为 `true` → `""`。后端必须用 `isinstance(.., str)` 复刻（`"" or` 把空串当 falsy → 偏差）。

**NEW-2 Option A（采纳，F4 彻底 + DRY）**：既有 `charts` list append（`_scan_meta_overview` 的 custom kind=chart 分支）也改用同款 isinstance 守卫（`label`/`title`/`chart_type`），与 chart_count 去重共用——避免 huge-mode meta overview 的 charts list 与 chart_count 用不同 coercion 造成分歧。

overview dict 新增 `chart_count`（与 `charts` list 并存：list 给 huge 模式 meta overview 用，count 给卡片用）。

**huge 模式已知差异**：前端 `selectCharts` huge 模式分支用 `serverOverview.charts` 原始 list（**不去重**），后端 `chart_count` 是去重数。故 huge 模式（未 loadFull）下卡片 chart_count 可能 < 前端显示数——AC3 限定"非 huge / loadFull 后"。

### 3.3 `RunSummary` 字段 + meta 语义分离（F8/F10）

- `RunSummary.event_count` ← `overview.log_event_count`（**语义改：全量 → log 行数**）。
- 新增 `RunSummary.chart_count: int = 0` ← `overview.chart_count`。
- `_summary_from_overview`（懒加载 SPEC 抽出的公共构造器，`@staticmethod`）同步消费这两字段。

**meta vs RunSummary `event_count` 双语义对照（F10）**：

| 字段位置 | 语义 | 用途 | 来源 |
|----------|------|------|------|
| `RunSummary.event_count`（卡片） | **log 行数**（过滤后） | 主页卡片"事件数" | `overview.log_event_count` |
| `RunMetaExtended.event_count`（meta） | **全量** | `/api/runs/<id>/meta` + huge 判定（`>50000`） | `_scan_meta_overview` 全量 `count` |

同一 JSON key `event_count` 两处不同语义——**故意**（meta 全量 for huge 判定 / RunSummary log 行数 for 卡片）。API 层 docstring + 本表标明，防混淆。

**overview 新字段是否进 meta huge 响应（F8）**：`get_run_extended_meta` huge 模式原样把 `overview_data["overview"]` 塞进 `meta["overview"]`，新增 `log_event_count`/`chart_count` 会随之进 meta huge 响应。前端 `ServerOverview` TS 类型加 optional `log_event_count?`/`chart_count?`（不用即忽略，不害）。

### 3.4 in-memory 分支 `event_count=0` 修复（F5 诚实化 + ISSUE-B）

`discover_runs` in-memory 分支：`event_count` + `chart_count` 从 tape fold 拿，不再硬编码 0。`status` 仍取 `handle.status`（实时 hint，E10/N5 不变——event_count 是**事实非 hint**，从 tape fold 正确）。

**F5 诚实化**：live run 的 tape 每 poll 增长 → cache key `(path, mtime, size)` 失效 → 每 8s 轮询重扫。**不是"命中 cache O(1)"**。但因 live run 数量少（typical 个位数），重扫成本可接受（主页 SPEC 实测单 tape fast-path ~μs 级）。

**ISSUE-B**：live in-memory 分支**直接调 `_scan_meta_overview`（不经 `_scan_meta_overview_cached`）**——live tape cache 即时失效，缓存意义不大；直调避免每 8s poll 触发一次 `.orca-meta-cache.json` 持久 writeback（live run 的持久 cache 永远 stale，写入纯浪费 IO）。

### 3.5 cache version `v2 → v3`——五处（F2 + NEW-3）

overview 加 `log_event_count` + `chart_count`，旧 v2 entry 缺字段。version `2` 在**五处（3 代码 + 2 doc）**硬编码，必须全改 `2→3`（只改 gate 会无限重建——gate 拒 v2，writeback 仍 stamp v2，重启再拒）：

1. `_persistent_cache_loaded` 默认 dict（`{"version": 2, ...}` → `3`）
2. version gate（`raw.get("version") != 3` → 空+warn 重建）
3. `_persistent_cache_writeback` stamp（`data["version"] = 3`）
4. `_persistent_cache_loaded` docstring（`raw.get("version") != 2` 示例 → `!= 3`）——**复审补漏**
5. `_persistent_cache_by_runs_dir` 字段 docstring 注释同步

一次性重建成本（fold 很快，主页 SPEC 实测 1354 tape 冷启 ~97ms）。

## 4. 失败路径 / 边界（F3 诚实化 + NEW-4）

- **overview 缺 `log_event_count`**（v2 残留，v3 gate 使理论不可达）：`_summary_from_overview` 是 `@staticmethod` 无 cache 访问、**不能触发 recompute**（F3）。fallback：用全量 `count` 作 best-effort + **warn**（**over-count 方向**——含工具调用等非 log 事件，NEW-4；标注降级不撒谎、不静默）。v3 gate 保证常态不触达。
- **overview 缺 `chart_count`**：fallback `0` + warn。
- **坏 tape**：log_event_count / chart_count 随 overview 一起 None（既有 skip+warn）。
- **in-memory 分支 tape 不可 fold**（handle 无 tape path / live-pending）：fallback `event_count=0`（既有 live-pending 语义），不崩。
- **U1 漂移**：白名单/去重前后端不同步 → event_count != log 行数。AC1 真机对账 + AC5/AC6 单测守门。

## 5. 验收标准

| AC | 内容 | 手段 |
|----|------|------|
| AC1 | `RunSummary.event_count` == 前端 LogStream `lines.length`（**showDebug=false 默认态**，逐 run 对账零偏差） | e2e：拉几个真实 run，卡片事件数 vs log 面板行数 |
| AC2 | in-memory（live）run `event_count` 非 0 | e2e：in-session 跑一个 run，主页卡片事件数 > 0（验证 §1.1 根因） |
| AC3 | `chart_count` == 前端显示图表数（**非 huge 模式 / huge loadFull 后**） | e2e：有图表的 run，卡片图表数 == 详情页图表数 |
| AC4 | **fixture 含 fast-path log 白名单类型**：`1 node_started + 1 retry_started + 10 agent_tool_call → event_count==2`（retry_started 走 fast-path，验证双分支计数；复审验 5 种 bug 模型全被此 fixture 拦） | 单测 |
| AC5 | 后端 `_LOG_EVENT_TYPES` == 前端 `classifyLogLevel` 非 null 且非 route_taken（U1） | 单测：集合相等 |
| AC6 | 后端 chart 去重 identity == 前端 `selectCharts`（U1），含**空 chart_type edge case**（F4） | 单测：同 title 多推 + 无 title + 空 chart_type |
| AC7 | meta `event_count` 保持全量（huge 判定不破）+ RunSummary.event_count 是 log 行数（双语义） | 既有 meta 测试绿 + 新断言 |

## 6. 实现顺序

1. **后端**：§3.1（log_event_count 双分支 + 白名单 + `_META_TYPE_RE` + 放置）+ §3.2（chart_count 去重 + isinstance + charts list 同款）+ §3.3（RunSummary + meta 语义分离）+ §3.4（in-memory 修复 + 直调不经 cache）+ §3.5（cache v3 五处）。单测 AC4/AC5/AC6/AC7。
2. **前端**：`RunRow.tsx` + `BoardCard.tsx` 加 chart_count metric；`run-list-store.ts` RunSummary TS 加 `chart_count`；`ServerOverview` TS 加 optional log_event_count/chart_count。
3. **e2e**：AC1/AC2/AC3（test-agent 真机）。
4. **code-reviewer** 自 review（重点：U1 同步、双分支计数完备+放置、meta/RunSummary 语义分离、isinstance 对齐含 charts list、cache 五处 version）。

## 7. U1 同步契约（关键）

两处逻辑必须前后端同步，改一处必须改另一处（注释双向引用）：

- **log 过滤**：后端 `_LOG_EVENT_TYPES`（`run_manager.py`）↔ 前端 `classifyLogLevel`（`selectors.ts`）非 null 且非 `route_taken`。
- **chart 去重**：后端 identity（`title if isinstance(title,str) else f"{chart_type}#{seq}"`，`chart_type`/`title` isinstance 守卫）↔ 前端 `selectCharts` identity（`selectors.ts`）。

单测 AC5/AC6 守门（断言集合/逻辑相等，含 edge case）。

## 8. 相关文件（函数名引用，行号会漂移）

- 后端：`orca/iface/web/run_manager.py`——`_META_BULK_MARKERS` / `_scan_meta_overview`（fast-path `is_bulk` 块 + full-parse `t = obj.get("type")` 后）/ `_summary_from_overview` / `discover_runs`（in-memory 分支）/ `_persistent_cache_loaded`（default + gate + docstring）/ `_persistent_cache_writeback`（stamp）/ `RunSummary` / `get_run_extended_meta`。
- Tape 序列化：`orca/events/tape.py`（payload key 顺序，`_META_TYPE_RE` 依赖）。
- 前端：`orca/iface/web/frontend/src/components/runlist/RunRow.tsx`（event_count metric）+ `BoardCard.tsx`（第三行）；`stores/run-list-store.ts`（RunSummary TS + `ServerOverview` optional）；`selectors.ts`（`classifyLogLevel` / `selectCharts`——对齐基准，**不改**）。

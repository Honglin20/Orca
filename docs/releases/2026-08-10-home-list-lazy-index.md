# Release Note — 2026-08-10：主页 run 列表懒加载（概要索引化 discovery，1354 tape 12s→18ms）

## 问题

`GET /api/runs?scope=all` 在 1354 tape 时首屏 **12s+**（主页卡死）。根因 `discover_runs → _summary_from_tape`
每 tape 做 **3 遍扫描**：

1. `_scan_meta_overview_cached`（有 in-memory + persistent 缓存）
2. `_topology_workflow_name_from_tape`（**无缓存**，扫头部取 `workflow_name`）
3. `_scan_tape_timebounds`（**无缓存**，全扫到末尾取 `started`/`ended` timestamp）

#2 #3 与 #1 扫的是同一批数据（`workflow_started` + 终态事件），但 #1（`_scan_meta_overview`）
**没 capture** `workflow_name`/`started_ts`/`ended_ts`，导致 #2 #3 每请求对全部 tape 重扫同样的行。
双层缓存只挡 #1，**挡不住 #2 #3**。放大器：前端 4s 全量轮询 + WS `run_changed` 全量 refresh，
每几秒触发一次全量 discovery，把"首屏慢"放大成"持续卡"。

## 修复（SPEC `2026-08-10-home-list-lazy-index.md`，spec-reviewer CONDITIONAL-PASS 3 BLOCKER 闭环）

### 后端 `orca/iface/web/run_manager.py`

- `_scan_meta_overview` 单遍 capture `workflow_name`/`started_ts`/`ended_ts`（带 `isinstance` 守卫，
  BLOCKER I-1/I-2：非数值 timestamp 不 `float()`，防爆半径——外层 `except` 会吞整个 overview，
  爆炸半径远大于旧 `_scan_tape_timebounds` 仅丢 bounds）。
- 抽 `_summary_from_overview` 公共构造器（`_summary_from_tape` + discovery 直构共用，DRY）；
  删 `_topology_workflow_name_from_tape` + `_scan_tape_timebounds`（零生产调用方）。
- persistent cache `v1→v2` version gate + **批量写回**（`_defer_persist` + `_dirty_runs_dirs` +
  `_flush_persistent_cache`：`discover_runs` 期间只更 in-memory + 标 dirty，尾部 per-`runs_dir`
  单次 `os.replace`；G2 冷启 <3s 可达；单 tape 路径即时写，避免 O(n²)）。
- `discover_runs` attached 分支改 `os.scandir` 一次枚举 + `DirEntry.stat()` + 缓存命中**直构**
  （`_summary_from_overview`，零 fold）+ 两层 fail-soft（目录级 scandir OSError → glob 降级 /
  per-entry stat OSError → skip+warn 不降级整目录）。

### 前端

- `run-list-store.ts` `POLL_INTERVAL_MS` 4s → 8s（WS `run_changed` 仍是主要增量源，8s 作断连兜底；
  不激进拉长到 15s+ 避 WS 静默断连 UX 退化）。

### 新增

- `scripts/gen_fixture_tapes.py`（AC7 线性度 fixture 生成器）。
- `tests/iface/web/test_home_list_lazy_index.py`（14 测试）。

## 验证

- **单测**：14 新测（字段等价 / v1 拒绝+warn / 类型守卫 / 批量+即时写回 / 直构零 fold 证 /
  两层 scandir fail-soft / `ended_ts` 后值覆盖）+ **493 回归全绿**（iface/web + events，非 Playwright）。
- **本地**（RunManager 直调）：AC1 命中 17.6ms / AC2 冷启 91.5ms / AC7 13540 tape 365.5ms R²=0.9641。
- **test-agent 真机 HTTP 层**（uvicorn + httpx，含 fastapi 序列化 + loopback TCP，非直调 RunManager）：
  AC1 命中 **20.4ms** / AC2 冷启 **97ms** / AC7 13540 tape **282ms**（18 样本回归 R²=0.9549，
  slope 0.022ms/tape）/ RunSummary 字段 **7/7** 逐字段等于 tape 派生 / 坏 tape skip 不崩 +
  跨项目 dedup + 三分支优先级（attached > legacy）回归全绿。
- **code-reviewer 两轮 0 MUST-FIX**（第一轮 2 SHOULD-FIX + 3 MINOR 全修，第二轮确认测试覆盖完整）。

## 性能（12s → 18ms）

| 场景 | 改动前 | 改动后（HTTP 层真机） |
|------|--------|----------------------|
| 缓存命中（1354 tape） | 12s+ | **20ms** |
| 冷启（1354 tape，清 cache） | 12s+ | **97ms** |
| 13540 tape 温路径 | ~120s（外推） | **282ms** |
| 线性 slope | — | 0.022ms/tape（与 run 数脱钩） |

## 遗留

- **AC6 前端浏览器端到端**（后补真机闭环）：WSL chromium 缺系统库，但用 **`apt-get download` +
  `dpkg-deb -x` + `LD_LIBRARY_PATH`（全程无 sudo）** 装齐 `libnspr4`/`libnss3`/`libnssutil3`/`libasound`
  后 Playwright chromium 可启动。`test_playwright_runlist.py` **10 测全绿**——主页 SPA 看板/列表/选择/
  搜索/主题/折叠持久全 DOM 契约，确认懒加载 + 轮询改动不破坏前端渲染。
- **in-memory 分支**（live 进程内 run）未独立真机覆盖（需真实 workflow 进程，纯 fixture + HTTP GET
  无法驱动）；该分支 §3.4 step5 明确**保持不变**（用 `handle.status` hint、不 fold tape），由单测覆盖。

Commit: `80ae386`。SPEC：`docs/specs/2026-08-10-home-list-lazy-index.md`。

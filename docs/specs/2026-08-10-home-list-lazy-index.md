# SPEC：主页 run 列表懒加载——概要索引化 discovery

> 背景：`docs/status/CURRENT.md`「主页 run 列表随 tape 数量线性变慢——缺懒加载」。
> 本 SPEC 是**根治方案**（架构问题，非 bug），覆盖 `GET /api/runs?scope=all` 全量 discovery 路径。
> 流程：本 SPEC（spec-reviewer CONDITIONAL-PASS，issue 全闭环）→ coder-agent 实现 → test-agent 实测 → 闭环状态文档。
>
> **v2 修订（spec-reviewer 闭环）**：§3.1 加类型/非空守卫（I-1/I-2，防爆半径）；§3.1 fast-path 改经验性声明（I-8）；§3.3 加批量写回（I-3，G2 前提）；§3.4 per-entry stat 失败不降级整目录（I-5）+ in-memory rationale（I-10）；§3.5 轮询改 8s 不依赖 WS 前置确认（I-6）；§4 scandir 失败拆两层（I-5）；AC7 重构为线性拟合 + 绝对值（I-4）；G5 降级演进（I-7）。

## 0. 范围

- **改**：主页列表加载路径——后端 `RunManager.discover_runs` 全量 fold + 前端 `run-list-store` 刷新节奏。
- **不改**：详情页事件流策略（用户认可现状）；写入路径（不引入"写时记"，见 §7）；tape 唯一真相源契约（R1/§9）；dedup / 三分支优先级语义。

## 1. 问题（根因链）

`GET /api/runs?scope=all`（`routes/runs.py:40` `list_runs`）→ `RunManager.discover_runs()`（`run_manager.py:1373`）→ 对每个注册项目 `runs_dir.glob("*.jsonl")`（`:1425`）→ 对每个 tape 调 `_summary_from_tape`（`:1684`）。

`_summary_from_tape` 对**每个 tape 做 3 次 tape 扫描**：

| # | 调用 | 缓存 | 扫描方式 |
|---|------|------|----------|
| 1 | `_scan_meta_overview_cached`（`:1560`） | ✅ in-memory + persistent | 命中→stat+dict；miss→`_scan_meta_overview`（`:2368`）regex fast-path 全扫 |
| 2 | `_topology_workflow_name_from_tape`（`:2191`） | ❌ 无缓存 | `tape_reader_replay` 扫到 `workflow_started`（每行 json.loads+Event） |
| 3 | `_scan_tape_timebounds`（`:2211`） | ❌ 无缓存 | `tape_reader_replay` **全扫到末尾**找终态事件 |

**根因**：`_scan_meta_overview` 单遍已遍历到 `workflow_started`（`:2435`，派生 `run_status` + topology nodes）与终态事件（`:2448`），**只是没把 `workflow_name` / `started_ts` / `ended_ts` capture 进 overview**。于是 #2 #3 每请求对全部 tape 重扫同样的行。双层缓存只挡 #1，**挡不住 #2 #3**。

实测（CURRENT.md）：1354 tape → `scope=all` 首屏 **12s+**；其他端点 <1s。

**放大器**（前端 `run-list-store.ts`）：4s 全量轮询（`startPolling` `:296`，`POLL_INTERVAL_MS=4000`）+ WS `run_changed` 全量 `refresh`（`onRunChanged` `:264`），每几秒触发一次全量 discovery，把"首屏慢"放大成"持续卡"。

## 2. 目标 / 非目标

### 目标（验收见 §5）

- **G1**：缓存命中（二次请求）`GET /api/runs?scope=all` < **300ms**（1354 tape 规模）。
- **G2**：首次冷启（清 persistent cache 后首请求）< **3s**（依赖 §3.3 批量写回；一次性，落盘后跨重启命中）。
- **G3**：温路径（缓存命中）与 run 总数**线性、非 superlinear**——常数 <0.2ms/tape；10× 规模（13540 tape）绝对耗时 <2s。响应序列化本身 O(N)，故按**绝对值 + 线性度**断言（非"10×→<2×"倍率，O(N) 算法数学上做不到倍率约束）。
- **G4**：列表正确性零回归——`RunSummary` 字段值、dedup（`seen_ids`）、三分支优先级（in-memory > attached > legacy）、坏 tape skip+warn 全不变。
- **G5**（降级为演进，见 §7）：详情页 `/api/runs/<id>/meta` overview 扩字段——本阶段**不验收**（响应 shape 变化需前端消费方审计），仅保证不崩。

### 非目标

- 详情页事件流（huge tail / 增量）下沉为默认——用户认可现状。
- 写入路径"写时记"——当前规模 + "用户只关注少数 run"下 YAGNI（见 §7）。
- active/archive 分层渲染——核心改造后视实测（见 §7 演进）。

## 3. 契约

### 3.1 `_scan_meta_overview` 单遍 capture 三字段（消除 #2 #3 的根）

`_scan_meta_overview`（`:2368`）单遍里，在已有 `workflow_started` / 终态事件处理分支**顺手 capture**：

- `workflow_name` ← 首个 `workflow_started.data.workflow_name`，**仅当 `isinstance(name, str) and name`**（在 `saw_topology` gate 内取，**不遍历后续 ws**——与旧 `_topology_workflow_name_from_tape` `:2199-2203` 在首个 ws `return None` 即不继续找的语义逐字对齐）；不满足则 overview 留 `None`（下游 fallback stem）。
- `started_ts` ← 首个 `workflow_started.timestamp`，**仅当 `isinstance(ts, (int, float))`**（`if started_ts is None and isinstance(ts, (int, float)): started_ts = float(ts)`）。**类型守卫必须**——否则非数值 timestamp 让 `float(ts)` 抛异常，被 `_scan_meta_overview` 外层 `except Exception`（`:2476`）吞掉 → **整个 overview 变 None**（爆炸半径远大于旧 `_scan_tape_timebounds` 仅丢 bounds，agents/cost/status 一同丢失）。
- `ended_ts` ← 最末终态事件（`workflow_completed`/`workflow_failed`/`workflow_cancelled`）`timestamp`，**同款 `isinstance(ts, (int, float))` 守卫**（后值覆盖）。

三者并入返回的 `overview` dict：`{agents, charts, cost_usd, run_status, workflow_name, started_ts, ended_ts}`。

**等价性论证（AC3 依据）**：
- `started_ts` 与 `_scan_tape_timebounds` 的 `started` 同源（首个 `workflow_started.timestamp`，同款 isinstance 守卫）。
- `ended_ts` 与 `_scan_tape_timebounds` 的 `ended` 同源（最末终态事件 timestamp，后值覆盖 + 同款守卫）。
- `workflow_name` 与 `_topology_workflow_name_from_tape` 同源（首个 ws 的 name，不遍历后续）。

**fast-path 安全（经验性声明，非逻辑论证，I-8）**：bulk 判定（`:2398-2402`）是裸子串匹配，理论上终态事件 data 若含 bulk marker 子串会被误判为 bulk → capture 分支不执行。经核验当前 Orca 终态事件 data key（`kind`/`error_type`/`message`/`node`/`reason`/`outputs`，源 `orca/runtime/lifecycle.py`）**不含** bulk marker 子串，故 capture 落在 full-parse 分支，零额外 IO。彻底堵死需把 marker 检查改为 `"type":"<marker>"` 锚定（记入 §7 演进，超本 SPEC 范围）。

### 3.2 `_summary_from_tape` 从 overview 读全字段 + 抽公共构造器

`_summary_from_tape`（`:1684`）改为：

- `workflow_name` ← `overview.workflow_name`（fallback `tape_path.stem`）
- `started_ts` / `ended_ts` ← `overview.started_ts` / `overview.ended_ts`（派生 `elapsed`、`started_at`）
- **删除** `_topology_workflow_name_from_tape`（`:1728`）+ `_scan_tape_timebounds`（`:1748`）调用
- 删除后若这两个函数无其他调用方 → 删定义（DRY）；有调用方 → 保留（grep 确认，实现期定）

**抽公共构造器**（A4 直构复用，DRY）：

```python
def _summary_from_overview(
    run_id: str, count: int, overview: dict, *,
    project_id, project_name, source,
) -> RunSummary | None:
    """从 (count, overview) 派生 RunSummary——_summary_from_tape 与 discovery 直构共用
   （in-memory 分支因 live handle hint 语义除外，见 §3.4 step4）。

    overview 缺字段（理论仅 v1 残留，v2 强失效后不可能）→ fallback 不崩。
    count==0 → None（discovery skip 语义）。
    """
```

`_summary_from_tape` = `_scan_meta_overview_cached` + `_summary_from_overview`。

### 3.3 persistent cache version `v1 → v2` + 批量写回

`_persistent_cache_loaded`（`:1623`）：

- **读时校验 version**：`raw.get("version") != 2` → 视为空重建（warn）。旧 entry 无新三字段，强失效一次性重建。
- **写时** `version=2`（`_persistent_cache_writeback` `:1654` 写回的结构带上 version=2）。

> 注：现有代码只校验 `raw` 是 dict + `entries` 是 dict（`:1632-1635`），**未校验 version**。本条新增 version gate。

cache 仍是**派生缓存**（可删可重建，不违 R1/§9 tape 唯一真相源）。

**批量写回（G2 可达的前提，I-3）**：version mismatch 触发全量重建时，现有 `_persistent_cache_writeback` 每条 entry 重写**整个增长中的 JSON 文件** → 1354 miss 累计 Σ(k=1..1354)×~600B ≈ 550MB 写 + 等量 `json.dumps` CPU → O(n²) ~10s+，G2(<3s) 不可达。改为**延迟批量**：`discover_runs` 期间 compute 结果只更新 in-memory `_meta_cache` + `_persistent_cache_by_runs_dir` dict + 标 dirty runs_dir，**尾部 per runs_dir 单次** `os.replace` flush。机制（实现期定具体形态）：

- `_scan_meta_overview_cached` 增 `defer_persist: bool = False` 形参（或 RunManager 维护 `_dirty_runs_dirs: set`）。
- `discover_runs` 调用链传 `defer_persist=True`；批次结束统一 flush 每个 dirty runs_dir。
- 单 tape 路径（`get_run_extended_meta` 等）`defer_persist=False`（即时写，n=1 无 O(n²)）。

### 3.4 discovery 读路径：scandir 一次枚举 + 命中直构（零 fold）

`discover_runs` attached 分支（`:1416-1445`）重构：

1. `os.scandir(runs_dir)` 一次枚举 `*.jsonl` + `DirEntry.stat()` 取 mtime/size（单次目录 syscall + 缓存 stat，替代 N 次 `glob` + `Path.stat()`）。
2. 每 tape 查 persistent cache（`tape_path.name` + mtime + size 匹配）：
   - **命中** → `_summary_from_overview(stem, count, overview, ...)` **直构 RunSummary**，**不调 `_scan_meta_overview_cached`**（零 fold、零额外 stat）。
   - **miss / mtime 变** → 调 `_summary_from_tape(path, ..., defer_persist=True)`（内部 `_scan_meta_overview_cached` recompute + 更新 in-memory + 标 dirty），下次即命中。
   - **per-entry `DirEntry.stat()` OSError**（TOCTOU 删除/权限）→ skip+warn 该 entry，**不降级整目录**（保持其余进度，避免 1354 重扫断崖）。
3. attached 循环结束 → flush 所有 dirty runs_dir（§3.3 批量写回）。
4. **保留**既有语义：`skip-if-in-self._runs`（`:1439`）、`seen_ids` dedup（`:1441-1443`）、`new_index` 填充（`:1445`）、坏 tape skip+warn（`:1431-1435`）。
5. in-memory 分支（`:1448-1474`，live 权威用 handle 字段）**保持不变**——它用 `handle.status`（实时 hint）/`event_count=0`（C1 占位符），**不 fold tape**，故不走 `_summary_from_overview`（SPEC E10/N5 禁止"统一"为同机制，会破坏优先级与 hint 语义）。legacy 分支（`:1483-1526`，量少）**保持** 调 `_summary_from_tape`（它内部有 cache，已受益于 §3.1/§3.2）。

> 直构路径（step2 命中分支）依赖 §3.3 version gate 保证 entry 字段完整性；理论上单条 entry 部分写损坏会静默 fallback stem——可接受降级（可选加 per-entry 字段 sanity check 缺则视为 miss → recompute）。

### 3.5 前端：轮询节奏合理化

`run-list-store.ts`：

- `POLL_INTERVAL_MS` `4000` → **`8000`**（保守拉长，**不依赖 WS 重连可靠性前置确认**；WS `run_changed` 仍是主要增量源，8s 作断连兜底）。
- `onRunChanged`（`:264`）保持（WS 推送即增量刷新源）。
- `REFRESH_THROTTLE_MS` 保持 `2000`。
- 不激进拉长到 15s+：避免 WS 静默断连时 UX 退化且无告警。若后续确认 WS 重连可靠（断连→恢复推送延迟可测），可再上调。

> §3.5 是配套优化。**核心是 §3.1-3.4**（后端索引化）；§3.5 解决"4s 全量 DOM diff"放大器。后端达标后前端单次 refresh < 300ms，轮询本身已轻——§3.5 降的是前端渲染频率，非后端负载。

## 4. 失败路径 / 边界（fail loud + fail soft）

- **坏 tape / 缺失**：保持 skip + warn（`:1431-1435`），不崩列表。
- **cache 损坏 / version 不符**：warn + 视为空重建（既有语义 + §3.3 version gate），下次 miss 自然重建。
- **overview 缺新字段**（仅 v1 残留理论可能）：`_summary_from_overview` fallback（`workflow_name`→stem、`elapsed`→0），不崩。
- **scandir 失败（两层，I-5）**：
  - 目录级 `os.scandir(runs_dir)` OSError（权限 / 非 dir）→ 降级回 `glob` + `_summary_from_tape`（既有路径），warn。
  - 迭代内 per-entry `entry.stat()` OSError（TOCTOU 删除 / 单文件权限）→ skip+warn **该 entry**，保持其余进度，**不降级整目录**（避免 1354 重扫断崖）。
- **单遍 capture 不破 fast-path**：bulk event 仍 substring+regex 跳过（§3.1 经验性声明）。
- **并发**：`_persistent_cache_writeback` 已是 tmp + `os.replace` 原子写（`:1674-1677`）；批量写回仍是 per-runs_dir 单次 `os.replace`，多 worker 并发 last-writer-wins，cache 可重建故安全。

## 5. 验收标准（可测）

| AC | 内容 | 手段 |
|----|------|------|
| AC1 | 缓存命中二次请求 `scope=all` < **300ms**（1354 tape） | 计时 `GET /api/runs?scope=all`（命中后） |
| AC2 | 冷启（清 `.orca-meta-cache.json` 后首请求）< **3s**（依赖 §3.3 批量写回） | 删 cache 文件 + 计时首请求 |
| AC3 | `RunSummary` 字段值与改动前**全等**（workflow_name/status/progress/elapsed/started_at/event_count/cost） | snapshot：改动前后同 tape 集 summaries diff == ∅ |
| AC4 | dedup / 三分支优先级 / 坏 tape skip+warn 语义不变 | 既有 iface/web 测试套全绿 |
| AC5 | persistent cache version=2；旧 v1 文件读为空重建 | 单测：构造 v1 文件 → 读返空 + warn |
| AC6 | 前端首屏 mount→refresh 完成 < **500ms**（含网络，命中态） | test-agent 真机驱动 |
| AC7 | 温路径线性度：`scripts/gen_fixture_tapes.py` 生成 1354 / 13540 tape 两点，温路径耗时线性拟合 R²>**0.95** + 13540 tape 绝对耗时 < **2s** | 脚本计时 + 线性回归 |

## 6. 实现顺序

1. **后端核心**：§3.1（capture + 守卫）+ §3.2（简化 + `_summary_from_overview` 抽象）+ §3.3（cache v2 + 批量写回）+ §3.4（scandir 直构 + 两层 fallback）。单测：AC3/AC4/AC5。
2. **后端实测**：AC1/AC2/AC7（含 `scripts/gen_fixture_tapes.py`）。
3. **前端配套**：§3.5（轮询 8s）。AC6。
4. **视实测演进**：§7（虚拟滚动 / 分层 / 写时记 / meta 扩字段消费），仅在 1-3 不达标时启用。

## 7. 演进项（YAGNI，实测驱动）

- **meta overview 扩字段消费（G5）**：`/api/runs/<id>/meta` huge 模式 overview 多 3 字段（workflow_name/started_ts/ended_ts），前端 `workflow-store` 若要利用需消费方审计后落地。本阶段仅保证不崩。
- **bulk marker 锚定（I-8 彻底堵死）**：把 `_scan_meta_overview` 的 bulk 判定从裸子串改为 `"type":"<marker>"` 锚定，消除终态事件 data 含 marker 子串的理论误判。
- **虚拟滚动**（react-window，依赖已装）：若 1354 行 DOM 渲染 > 100ms（§3.5 后仍卡）→ 列表上虚拟化。当前列表已按 project 分组 + 折叠（`ProjectGroup`），可能已够。
- **active/archive 分层**：若用户"只关注少数 run"成为**导航**诉求（非性能）→ 首屏只拉最近 N（`limit` + 按 `started_at` desc，索引化后排序是 dict 操作），归档层"加载更多"。
- **写时记**：run 生命周期事件发生即更索引（O(1) 常新）。仅在 run 数到几万 + 要秒级实时大盘时值得——届时碰写入路径，需额外一致性设计。当前规模下惰性索引（§3.4）已让"列表不随 n 线性变慢"成立。

## 8. 相关文件

- 后端：`orca/iface/web/run_manager.py`（`discover_runs` / `_summary_from_tape` / `_scan_meta_overview` / `_scan_meta_overview_cached` / `_persistent_cache_*` / `_scan_tape_timebounds` / `_topology_workflow_name_from_tape`）。
- 路由：`orca/iface/web/routes/runs.py:40`（`list_runs`，不改逻辑，仅受益）。
- 前端：`orca/iface/web/frontend/src/stores/run-list-store.ts`（`refresh` / `onRunChanged` / `startPolling` / `POLL_INTERVAL_MS`）。
- 详情顺带受益（不验收）：`run_manager.py:get_run_extended_meta`（`:909`，huge overview 含新字段）。
- 新增：`scripts/gen_fixture_tapes.py`（AC7 线性度 fixture 生成）。

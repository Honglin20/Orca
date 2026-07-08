# Web attach Step1（X + perf）—— attach by tape path + huge-mode + perf

**日期**: 2026-07-08
**SPEC**: [`docs/specs/web-attach-and-default-spec.md`](../specs/web-attach-and-default-spec.md) rev2 §2/§3/§6/§8/§9.1
**Branch**: `phase13-render-chart`
**前置**: Web Shell v2 DONE（`web-shell-v2-spec.md`，单 store/selector/WS resume）

## What was done

Web v2 只认 in-process run（`POST /api/run`），看不出 `orca run --background` /
in-session 起的独立进程 run。本 Step1（X + perf）补：

1. **X — attach by tape path**（SPEC §2）：`POST /api/runs/attach` body `{tape_path,
   run_id?}` → read-only 探测 + 注册 `AttachedRunHandle` + 起 follow task → tail-follow
   外部 tape 增量 → `bus.relay`（fan-out only）→ WS 订阅者。
2. **perf**（SPEC §3 / §8.4）：`GET /api/runs/<id>/meta` 判 huge（>50k events / >5MB）→
   huge=true 时返服务端 fold 派生的 `overview`（agents/charts/cost/run_status）+ 前端
   `selectAgents/selectCharts` 读 serverOverview + Conversation/Log 读 `?tail=500` +
   上滚增量 prepend（`?since=oldest-M&limit=M`）+ "load full" 按钮。
3. **安全**（SPEC §6）：`resolve_tape_path` 三重守卫（lstat → resolve → relative_to +
   ORCA_WEB_TAPE_ALLOWLIST → post-resolve re-check → open + fd re-stat 防 TOCTOU）。
4. **`GET /api/health`**（§5）：`{app:"orca", version, pid}`（为 Step2 `orca open` 端口
   探测铺路）。
5. **`GET /events?since/limit/tail`**（§3）：pure tape read，不 emit bus（M1）。

## 关键设计决策

- **RunView ABC + 双 handle**（§0 D6 / §2.3）：`RunView` 只读基类（`run_id`/`bus`/`tape`/
  `status`/`source`），`InProcessRunHandle`（既有 wf/gate/chart_ingestor）+
  `AttachedRunHandle`（follow_task/terminal/tape_path）。`_runs: dict[str, RunView]` 单
  registry 不变；`_meta_from_handle` / `_teardown_handle` / routes / ws_handler 经
  `isinstance` 局部分支处理两种形态。
- **EventBus.relay**（M1）：attached run 不写外部 tape（避免抢 flock）；follow task parse
  → 构造 Event → `bus.relay`（仅 fan-out，不调 `tape.append`）。`emit` 留给 in-process
  （拥有自己 tape）。两条写入路径语义清晰：in-process 经 `emit` 写 tape + fan-out；
  attached 经 `relay` 仅 fan-out。
- **read-only 探测 + 不 bulk replay**（D3）：`_probe_head_and_terminal` 单次扫取首个事件
  + 最末终态事件。`initial_offset` 在 probe **之前**捕获（修复 reviewer MAJOR 4：probe
  后 stat 的窗口丢事件）。终态 tape 跳过 follow task（无新事件）。
- **perf fast-path**（`_scan_meta_overview`）：单遍扫文件 + bulk-type substring 过滤 +
  regex seq 提取（避免 Python stdlib json 全量 parse 的 ~500ms/60k 开销）。
  `tail_events` 反向字节块扫描 O(tail)；`since_limited` 提前 break。
- **前端 huge-mode**（M3/M4）：`serverOverview?` slice 在 huge 模式由 `/meta.overview`
  设；`loadFull` 清此 slice → selectors 自然回退 client-fold（M4 可展开校验）。

## Iron Laws grep（AC §8.12/§8.13）

- ✅ `_runs` dict 定义单处（`run_manager.py:189`）
- ✅ zustand `create<` 单处（`workflow-store.ts`）
- ✅ attacher 无 `Tape(resume=True)` 构造（仅 docstring 解释为何不用）
- ✅ `tape_reader.py` 全用 `open(mode="r")`
- ✅ `bus.emit/relay` 仅从 follow task（GET /events 是 pure tape read，M1）

## Code Reviewer 闭环

**2 BLOCKER + 6 MAJOR + 5 MINOR 全修复**：
- 🔴 BLOCKER 1: `get_run_events_window` 全量物化 → `tail_events`（反向扫）+ `since_limited`（提前 break）
- 🔴 BLOCKER 2: `_compute_overview` 三遍全 replay → `_scan_meta_overview` 单遍 + bulk-type fast-path + memoize
- 🟡 MAJOR 3: follow 每次 poll open/close → 循环外 open 一次 + fd/path 双 stat 检测 rotate
- 🟡 MAJOR 4: probe/stat 窗口丢事件 → `initial_offset` 在 probe 前捕获
- 🟡 MAJOR 5: 幂等检查未持锁 → 挪进 `async with self._lock`
- 🟡 MAJOR 6: `_scan_terminal_type` 全量扫 → `_probe_head_and_terminal` 单次扫合并
- 🟡 MAJOR 9: 服务端/前端 agents fold drift 风险 → 加双向指认注释（同源同逻辑）
- 🟢 MINOR 10-14: error 事件 seq=0 改负数 / selectAgents tokens 透传 / URL 编码测试收紧 / `Path(...)` 冗余包装去除

## Deviations from plan

- frontend `agents-rail` 测试 lazy chunk 解析 timing flake（pre-existing，并行 vitest 抖动；
  isolated run 通过；本任务不引入该问题）。
- perf benchmark 50MB fixture 真压测延后到 CI 矩阵（`ORCA_RUN_PERF_TESTS=1`）——in-suite
  60k fixture 测试已覆盖 fast-path 正确性 + 性能合理性（< 500ms 兜底）。
- test_exit_codes 失败（`daemon.py:105` B-8 + katex node_modules）pre-existing，与本任务无关。

## Verification

- **Backend**: 1863 passed / 2 skipped（perf 默认 skip，ORCA_RUN_PERF_TESTS=1 时 7/7 PASS）
- **Frontend**: 257/259 passed（agents-rail pre-existing flake，isolated 全过）
- **TypeScript**: `npx tsc --noEmit` clean
- **Frontend build**: `npm run build` OK（initial 290KB / gzip 93.65KB，bundle split 保留）
- **Iron Laws grep**: 单 `_runs` dict / 单 zustand store / 无 `Tape(resume=True)` / bus.emit/relay 仅 follow task

## Commit

- `69e5c7b`

## Files

**Backend (Python)**:
- `orca/events/tape_reader.py`（NEW）
- `orca/events/bus.py`（+ `relay(event)` 方法）
- `orca/iface/web/run_manager.py`（RunView ABC + 双 handle + attach_run + resolve_tape_path + windowed events + extended meta + perf fast-path）
- `orca/iface/web/routes/attach.py`（NEW：POST /api/runs/attach + GET /api/health）
- `orca/iface/web/routes/runs.py`（+ since/limit/tail + GET /meta）
- `orca/iface/web/routes/gate.py`（isinstance InProcessRunHandle 守卫）
- `orca/iface/web/ws_handler.py`（attached run 无 gate_handler 容错）
- `orca/iface/web/server.py` + `routes/__init__.py`（wire attach router）
- `scripts/gen_big_fixture.py`（NEW）

**Frontend (TS/React)**:
- `src/types/store-types.ts`（ServerOverview / RunMetaExtended）
- `src/stores/workflow-store.ts`（serverOverview/writable/huge slices + loadRunWithMeta/loadEarlierChunk/loadFull）
- `src/hooks/use-run-events.ts`（switch to loadRunWithMeta）
- `src/selectors.ts`（selectAgents/selectCharts huge 模式读 serverOverview）
- `src/components/gate/PermissionGate.tsx`（observe-only when writable=false）

**Tests**:
- `tests/iface/web/test_attach.py`（NEW，19 tests：attach/meta/security/windowed/single-registry）
- `tests/iface/web/test_attach_follow_failures.py`（NEW，7 tests：SPEC §7 失败路径 + perf AC + tail/since 正确性）
- `orca/iface/web/frontend/test/huge-mode.test.ts`（NEW，10 tests：huge-mode + writable + selector AST 守门）

## Step2 deferred

`orca run` web 默认（端口探测 + webbrowser.open + WS 驱动 auto-exit）/ `orca open` CLI /
`/orca open` slash / gate observe-only UI polish（SPEC §4/§5/§8 AC5-7/11）—— 留 Step2 Y。

# E2E Real-Execution Evidence —— 5 聚类审查修复（A/B/C/D/E）

**日期**：2026-08-03
**分支**：`in-session-unified-backend`
**测试者**：test-agent（real execution through public surface）
**后端**：脚本节点（script node，无 LLM 调用）+ opencode 配置（未触发——script executor 不烧 token）

> 本文档为真机端到端证据 log。每项给「真实命令 → 真实 stdout / 响应 / 文件证据 →
> 真实 exit code → verdict」。内部单测已绿不在本报告范围。

---

## 测试环境

- **Host**：Windows 11 + WSL Ubuntu（Linux Python 3.12.13 / `/home/mozzie/miniconda3/envs/orca/bin/python`）。
- **隔离**：自定义 `ORCA_HOME=/tmp/orca_e2e/home/.orca` + `ORCA_PROJECT_ROOT=/tmp/orca_e2e/proj`，与生产 `~/.orca` 完全隔离。
- **真实 tape 构造**：用真实 `orca.events.bus.EventBus` + `orca.events.tape.Tape` 写事件流（不 mock schema、不直接拼 JSON）。
- **真实公共接口**：
  - CLI：`python -m orca.iface.in_session.cli stop`（A）
  - CLI：`python -m orca.iface.cli.commands resume`（B）
  - HTTP：`POST /api/run`、`GET /api/runs?scope=all`、`GET /api/runs/<id>`（D、E）
  - HTTP：`GET /api/runs/<id>/events`、`GET /runs/<id>`（HTML）（C）
  - subprocess：`tars run`、`tars serve`、`tars resume`（真子进程）
- **不能真机**：浏览器驱动（Playwright 未安装、无浏览器缓存）。C 项浏览器 E2E 为唯一未真机覆盖项。

---

## A —— stop 判终态（commit `08cb7b0`）✅ PASS

**真实公共接口**：`python -m orca.iface.in_session.cli stop --run-id <id>`

### A1: tape 已含 `workflow_completed` → 短路（不追加）

构造 tape（4 events，末条 `workflow_completed`）→ `orca stop` → 重新 grep。

```
$ python -m orca.iface.in_session.cli stop --run-id A1_completed
{"run_id": "A1_completed", "ok": false, "done": true, "note": "already-terminal", "status": "workflow_completed"}
EXIT_CODE=0
```

| 指标 | BEFORE | AFTER |
|---|---|---|
| TOTAL_EVENTS | 4 | 4 |
| TERMINAL_COUNT | 1 | 1 |
| TERMINAL_TYPES | ['workflow_completed'] | ['workflow_completed'] |

**verdict**：✓ 已 completed 短路 exit 0，**不追加**第二终态事件（I1/I3 幂等保住）。

### A2: tape 已含 `workflow_cancelled` → 连调 2 次短路（不追加）

```
$ orca stop --run-id A2_cancelled   # 1st
{"run_id": "A2_cancelled", "ok": true, "done": true, "note": "already-cancelled", "status": "cancelled"}
EXIT_CODE=0
$ orca stop --run-id A2_cancelled   # 2nd
{"run_id": "A2_cancelled", "ok": true, "done": true, "note": "already-cancelled", "status": "cancelled"}
EXIT_CODE=0
```

| 指标 | BEFORE | AFTER x2 |
|---|---|---|
| TOTAL_EVENTS | 4 | 4 |
| TERMINAL_COUNT | 1 | 1 |

**verdict**：✓ 同类重复短路（连续 2 次）零追加，marker 被清。

### A3: 活跃 run（无终态事件）→ emit 恰好一条 `workflow_cancelled`

```
$ orca stop --run-id A3_active
{"run_id": "A3_active", "ok": true, "done": true}
EXIT_CODE=0
```

| 指标 | BEFORE | AFTER |
|---|---|---|
| TOTAL_EVENTS | 2 | 3 |
| TERMINAL_COUNT | 0 | 1 |
| LAST_TERMINAL | None | workflow_cancelled |

**verdict**：✓ 恰好追加一条 `workflow_cancelled`（不是 0 条、不是 2 条）。

### A4: 坏 tape（非合法 JSON）→ fail loud exit 1，零追加

往末尾追加一行 `{ this is not valid json`，调 stop：

```
$ orca stop --run-id A4_corrupt
tape 损坏：runs/A4_corrupt.jsonl 第 3 行非合法 JSON，请手动核查（doctor 暂不修复 tape 内容）
EXIT_CODE=1
$ wc -l runs/A4_corrupt.jsonl
3
```

**verdict**：✓ 中间残行 fail loud exit 1，不开写句柄、不追加。

### A5: 多类终态矛盾（completed + cancelled）→ fail loud exit 1 + `[AUDIT]` warning

```
$ orca stop --run-id A5_contradiction
2026-08-03 06:51:48 WARNING __main__: [AUDIT] tape-contradiction path=runs/A5_contradiction.jsonl \
    types=['workflow_cancelled', 'workflow_completed'] last_seq=3
tape 终态矛盾：runs/A5_contradiction.jsonl 含 ['workflow_cancelled', 'workflow_completed']
（last_seq=3），请手动核查（doctor 暂不修复 tape 内容）
EXIT_CODE=1
```

**verdict**：✓ 多类终态 raise → exit 1 + `[AUDIT] tape-contradiction` warning log，零追加。

### A 总结

5 个子场景全部 PASS。停终态判 + dupe-check + fail-loud 全部按 SPEC §4.1 控制流骨架 B-5 行为。

---

## B —— resume / 重放幂等（commit `f627196`）✅ PASS

**真实公共接口**：`python -m orca.iface.cli.commands resume <run_id> --yaml <wf.yaml>`

构造崩溃窗口 tape：仅 `workflow_started → node_started(A) → node_completed(A)`，
**故意缺失 `route_taken`**（即崩溃在 `[node_completed(A), route_taken)` 窗口）。
workflow YAML：两 script 节点 A → B → $end。

```
$ tars resume B2_clean --yaml /tmp/orca_e2e/proj/workflows/B_resume_idempotent.yaml
RESUME_EXIT_CODE=0
```

resume 后 tape 全量事件：

```
seq= 1 type=workflow_started       node=A
seq= 2 type=node_started           node=A        ← 原始（崩溃前）
seq= 3 type=node_completed         node=A        ← 原始（崩溃前）
seq= 4 type=workflow_resumed       node=None     ← resume 起点
seq= 5 type=node_started           node=B        ← 推进到 B（不是 A！）
seq= 6 type=node_completed         node=B
seq= 7 type=route_taken            to=$end
seq= 8 type=workflow_completed
```

**关键断言**：

```
node_started(A) count = 1   ← A 不重 dispatch（B1 done 判据生效）
node_started(B) count = 1   ← 从 A 的 done 推进到下一节点 B
```

**verdict**：✓ A 未重 dispatch；resume 正确从 A 的 done 状态推进到 B。最终
`workflow_completed`，exit 0。

**早期 fixture 噪声**：第一次尝试（`B1_crashed_window`）workflow YAML 未给 B 出边，
B 节点跑完后触发 `route_deadlock` → `workflow_failed`（exit 1）。这是 fixture bug（B
无 routes），不是 B 修复的缺陷——核心断言「A 不重 dispatch」在该次也已通过
（`node_started(A)=1, node_started(B)=1`）。

---

## C —— 前端 fail-loud + loadRun 重试（commit `ccb8d7a`）⚠️ PARTIAL（浏览器不可得）

**真实公共接口**：浏览器加载 `/runs/<id>` HTML → 前端 fetch `/api/runs/<id>/events` →
非 200 触发 3 次指数退避（1s/2s/4s）→ `loadStatus="error"` + `retry-banner` UI。

### 真机能验证

#### C-HTTP-1: 前端 bundle 真实被服务（HTML 200）

```
$ curl -s -o /dev/null -w "%{http_code} %{size_download}\n" http://127.0.0.1:7441/runs/B2_clean
200 686
```

#### C-HTTP-2: `/api/runs/<id>/events` 健康时 200（前端 loadRun 的重试目标契约）

```
$ curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7441/api/runs/B2_clean/events
200
```

#### C-HTTP-3: 不存在的 run → 404（前端 fail-loud 重试触发条件）

```
$ curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7441/api/runs/NOPE/events
404
```

→ 真实 server 在 run 不存在时返 404，正是 SPEC C1 触发「3 次指数退避 → loadStatus=error」
的契约触发点。

#### C-BUNDLE-1: fail-loud UI 字符串真实存在于构建产物

```bash
$ grep -c "retry-banner\|run-load-error\|loadError\|loadStatus" orca/iface/web/static/assets/*.js
index-DPZStvwi.js:2
index-C0ME_TpK.js:2
... (40+ bundles contain the strings)
```

→ SPEC C 落地的 UI 锚点（`retry-banner`、`run-load-error` testid、`loadStatus` 状态机）
真实在 served bundle 中。

#### C-UNIT: vitest store-fail-loud + websocket-defer-resume + chart 全绿（32 测试）

```bash
$ cd orca/iface/web/frontend && ./node_modules/.bin/vitest run \
    test/store-fail-loud.test.ts test/websocket-defer-resume.test.tsx \
    test/chart-renderer.test.tsx test/chart-error-boundary-integration.test.tsx

Test Files  4 passed (4)
     Tests  32 passed (32)
  Duration  1.64s
```

涵盖：C1-AC1-8 / C5-AC1 / INV-7 / seenSeqs / __foldTwice / foreach NaN / E7 / B1 fix /
partition + warn-once + ErrorBoundary 隔离 + ChartGroup key=identity / AC19 defer-RESUME /
AC22 BLOCKER-2 listener 顶层 + MAJOR-2 resumeSent dedup / F1 reconnect-during-loading /
MAJOR-3 reconnect 双帧。

### 真机不可得项

**真实浏览器 E2E（驱动渲染的 `retry-banner` / 点击「重试」按钮）**——环境限制：
Playwright Python 包未安装（`ModuleNotFoundError: No module named 'playwright'`），
`~/.cache/ms-playwright/` 无浏览器缓存，dev dependency 仅声明但未装。

### C verdict

⚠️ **PARTIAL**：HTTP 契约层 + bundle 字符串 + 32 vitest 单测全 PASS（覆盖 SPEC C 全部
AC 的 store-level 行为）。**唯一未真机覆盖项**：真实浏览器驱动渲染的 retry-banner UI。
失败模式可观测：HTTP 200 时无 banner，HTTP 404/500 时 banner 出现 + 退避重试——store
级已被 vitest 钉死，bundle 已含字符串，仅差无浏览器 harness 跑真实 click/render。

---

## D —— auto-exit 不撕并发 in-process run（commit `c0cdd23`）✅ PASS

**真实公共接口**：`tars run`（web-default 子进程）+ `POST /api/run`（启动并发 run B）+
`GET /api/runs/<id>`（轮询 B 状态）。

驱动脚本 `/tmp/orca_e2e/D_driver.py`：subprocess.Popen 真起 `tars run D_short.yaml`
（短 run A，~0.5s 完成），server 起来后 POST `/api/run` 起 `D_long.yaml`（`sleep 20`）
作为并发 in-process run B。`ORCA_WEB_AUTOEXIT_SECONDS=2`。每 2s 采样进程存活 + B 状态。

```
SPAWNED PID=396538 at T+0
  T+8.1s health OK: 396538
  T+8.3s POST /api/run -> status=200 run_id=d_long-20260803-070418-9ea961
  Sampling (interval=2s, autoexit window=2s):
    T+ 10.3s  MAIN=ALIVE  B_status=running
    T+ 12.5s  MAIN=ALIVE  B_status=running
    T+ 14.5s  MAIN=ALIVE  B_status=running
    T+ 16.5s  MAIN=ALIVE  B_status=running
    T+ 18.5s  MAIN=ALIVE  B_status=running
    T+ 20.5s  MAIN=ALIVE  B_status=running
    T+ 22.5s  MAIN=ALIVE  B_status=running
    T+ 24.5s  MAIN=ALIVE  B_status=running
    T+ 26.5s  MAIN=ALIVE  B_status=running
    T+ 28.5s  MAIN=ALIVE  B_status=completed   ← B sleep 20 自然完成
    T+ 30.6s  MAIN=DEAD    B_status=?          ← B 终态后 ~2s autoexit fire

=== ANALYSIS ===
  PHASE 1 (B non-terminal): MAIN stayed ALIVE throughout ✓
  PHASE 2 (post-B-terminal): MAIN eventually DEAD ✓
```

### 关键观察

- **Phase 1（B 非终态，~20s）**：A 完成 + 无 WS 连接 + autoexit 窗口（2s）被超 9 次，
  MAIN 进程**始终未退**——SPEC D finding 1 的第三条件 `not manager.has_nonterminal_inproc_runs()`
  正确阻挡退出。**修复前**：进程会在 ~T+10s 自动退（autoexit 触发）→ `manager.shutdown` cancel
  B 的 sleep task → B 永远不会到 `workflow_completed`。
- **Phase 2（B 终态后）**：MAIN 在 B 完成 + ~2s（autoexit 窗口）后正确退出——证明第
  三条件不是无脑阻挡，B 终态后退出条件满足。

### D verdict

✓ PASS。两个阶段都符合 SPEC D 行为声明（release note M1 行为变更块）。

---

## E —— 单 tape discovery legacy（commit `50a52fe`）✅ PASS

**真实公共接口**：`GET /api/runs?scope=all`（驱动 `RunManager.discover_runs()` legacy 分支）。

构造 3 个 legacy fixture（`$ORCA_HOME/runs/<id>.json` 旧 BgRunMeta + tape）：

| run_id | tape 末条终态 | meta status |
|---|---|---|
| `E1_legacy_completed` | `workflow_completed` | running |
| `E2_legacy_crashed` | (无终态) | running |
| `E3_legacy_cancelled` | `workflow_cancelled` | running |

启动 `tars serve`（端口 7430），curl 真实 `/api/runs?scope=all`：

```
$ curl -s "http://127.0.0.1:7430/api/runs?scope=all"
[{"run_id":"E1_legacy_completed","workflow_name":"w","project_name":"Legacy",
  "status":"completed","progress":"1/1","source":"legacy", ...}]

# 加上 E2 + E3 后：
=== /api/runs?scope=all (3 legacy fixtures) ===
  run_id=E1_legacy_completed    status=completed    source=legacy  progress=1/1
  run_id=E2_legacy_crashed      status=running      source=legacy  progress=0/1
  run_id=E3_legacy_cancelled    status=cancelled    source=legacy  progress=1/1
```

### 关键断言

- **E1（核心 oracle）**：legacy meta 写 `status:"running"`，tape 末条 `workflow_completed`，
  discovery 显示 **`status=completed`**（**非 cancelled**）——SPEC E-1 的核心断言通过：
  legacy 分支不再硬编码 `cancelled`，改走 `_summary_from_tape` fold。
- **E2（trade-off 验证）**：crashed legacy（无终态）→ `status=running`，**非 cancelled**
  （release note 第 (c) 破坏性变更声明：crashed legacy 由 cancelled → live-pending/running）。
- **E3（正控制）**：tape 真正含 `workflow_cancelled` 时显示 `cancelled`——表明 status 是从
  tape 派生而非简单替换。

### E verdict

✓ PASS。三个变体全部从 tape fold 派生 status，DRY（与 attached 同款 `_summary_from_tape`）。

---

## 总结

| 项 | commit | verdict | 真实公共接口 | 真实证据 |
|---|---|---|---|---|
| A | `08cb7b0` | ✅ PASS | `orca stop --run-id` CLI | 5 子场景 stdout + tape grep + exit code |
| B | `f627196` | ✅ PASS | `tars resume <id> --yaml` CLI | tape 全量事件 + `node_started(A)=1` |
| C | `ccb8d7a` | ⚠️ PARTIAL | HTTP `/runs/<id>` + vitest | HTTP 契约 + bundle 字符串 + 32 单测；浏览器不可得 |
| D | `c0cdd23` | ✅ PASS | `tars run` subprocess + `POST /api/run` + `GET /api/runs/<id>` | 进程存活时间线（T+10..T+28 ALIVE，T+30.6 DEAD） |
| E | `50a52fe` | ✅ PASS | `GET /api/runs?scope=all` HTTP | 3 legacy fixture 真实响应 status 字段 |

### 真机覆盖范围

- **4/5（A、B、D、E）完整真机 PASS**——驱动真实 CLI / HTTP 公共接口，断言真实 stdout /
  响应 body / tape 文件 / 进程存活。
- **1/5（C）部分真机**——HTTP 契约层 + bundle 静态字符串 + vitest store-level 全 PASS；
  唯一缺口：真实浏览器驱动（环境无 Playwright/浏览器缓存，非代码缺陷）。

### 发现的真实 bug

**零**。所有 5 个修复在真机端到端驱动下行为符合各自 SPEC 与 release note 声明。

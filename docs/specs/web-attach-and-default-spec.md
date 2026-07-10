# Web 单 run attach + web 默认 + in-session open —— SPEC（rev2，闭环 spec-review 7 BLOCKER + 12 MAJOR）

> Web v2（`web-shell-v2-spec.md`，DONE）RunManager in-memory，只认 `POST /api/run` 起的 in-process run；`orca run --background` / `orca in-session` 起的 run（独立进程）web 看不到（gap）。本 SPEC 补：**X** web 按 tape 路径 attach（read-only 开 + tail-follow + 注册）+ perf（大 tape 不卡）；**Y1** `orca run` 默认走 web；**Y2** in-session `/orca open` + `orca open` CLI 打开任意 run。
>
> rev1 conditional-fail（7 BLOCKER）→ 本 rev2 闭环。流程：本 SPEC → spec-review 复审 → clean-code（Step1 X，Step2 Y）→ test-e2e。

---

## 0. 决策（闭环 review U1/U2 + R1-R19）

- **U1（窗口铁律真意）= 客户端 fold 为主**：**默认（小/中 tape）全量事件到客户端，client fold（v2 铁律不变，`selectAgents/Charts/TopBar/Conversation/Log` 全准）**。**仅当 `GET /meta` 判定 tape 过大**（全量 fold P99 > 200ms 的 perf 驱动阈值，非魔数；约对应 >50k 事件或 >5MB）才进 **huge 模式**：服务端 `/meta` 额外返回 **派生 overview**（agents 状态/elapsed/token、charts 清单、累计 cost、run status —— 服务端 fold 同一 tape 算出，**非第二真相源**），客户端 overview selectors 读 `/meta`（perf 信任）；`Conversation/Log` 读 **tail 窗口**（尾 500 + live + 上滚懒加载）；"load full" 按钮拉全量回 client-fold（opt-in，慢但可验）。**铁律 nuance 明示**：tape 仍是唯一真相；huge 模式下客户端对 overview **信任服务端 fold**（同一 tape 的 fold，可展开校验），不引入第二 tape/第二 store。
- **U2（in-session 入口）= slash + CLI，弃 MCP**：in-session 架构无 MCP 工具注册面（`in-session-shell-design-draft.md` §13 已删 `orca_advance` MCP 工具；入口是 `experimental.chat.messages.transform`）。故 §5 用 **`/orca open <run_id>` slash 命令** + **`orca open` CLI** 双入口；MCP 工具单独立项 phase-X（先设计注册面）。
- **D1 单 run 加载**：attach 一次一个 tape，不扫盘。
- **D2 read-only 开 tape**：attacher **绝不** `Tape(resume=True)`（那是 append 模式，会抢外部写者 flock + 污染 seq）；用新 `tape_reader.replay(path)`（`open(mode="r")` 只读流式 yield Event）。
- **D3 stream-on-demand（不 bulk replay）**：attach 注册 handle + 起 follow task；**不在 attach 时 bulk replay 全 tape**。client 经 `GET /events?...`（按 seq/窗口）拉取 → 服务端从 tape **流式 emit 到该 run 的 bus**（单写入路径 `bus.emit`），客户端 fold。
- **D4 `orca run` web 默认生命周期**：in-process orchestrator（同 `POST /api/run`）+ 临时 serve（默认 7428，被占 → `GET /api/health` 探测：是 orca 则复用，否/不可达 → 选空闲端口起新 serve）+ `webbrowser.open(/runs/<id>)`（in-process 走 bus，不 attach）+ 阻塞到终态 + **WS 事件驱动计时器**（任一 WS connect/disconnect 重置；`now - last_ws_disconnect_at > N(默认15s, env ORCA_WEB_AUTOEXIT_SECONDS) AND run.terminal` → 退；`--stay` 保留）。
- **D5 TUI 保留 opt-in**（`--tui`）；`--background` 不变。
- **D6 RunHandle 双实现 + 单 registry**：`_runs: dict[str, RunView]`；`RunView` 只读协议 `{run_id, bus, state, status, source}`；`InProcessRunHandle(wf, gate_handler, chart_ingestor, ...)` 与 `AttachedRunHandle(bus, follow_task, terminal, source="attached")` 两实现。`_meta_from_handle`/`_teardown_handle` 对 attach 形态分支（`wf=None` 容错、跳过 sock unlink）。

---

## 1. 铁律（不可违背）

1. **tape 唯一真相源**；web = 纯 tape 渲染。默认 client-fold 全量（v2 不变）；huge 模式 client 对 overview 信任**服务端对同一 tape 的 fold**（`/meta` 派生），可"load full"展开校验——**不引入第二 tape/第二 store/第二 registry**。
2. **单 run 加载**（attach 一个 tape）。
3. **不卡**：默认全量即时；huge 模式（perf 驱动阈值）`/meta` + tail 窗口 + 上滚懒加载；任何 run 打开 < 1s（见 §8 三条 measurable AC）。
4. **安全**：tape 路径用既有 `resolve_asset_path` 等价**三重守卫**（`relative_to` 非 startswith + symlink-before-resolve + post-resolve re-check + open 后 fd re-lstat 防 TOCTOU）；不符 → 403。
5. **接口统一**：attach 与 start_run 同一 `_runs` registry、同一 `RunView` 协议、同一 `bus.emit` 单写入路径；不并第二 registry/第二 WS 路径。
6. **read-only**：attacher 绝不开 append 模式 / 不抢 flock。

---

## 2. X — attach by tape path（Step1）

### 2.1 路由
- `POST /api/runs/attach` body `{tape_path, run_id?}` → 安全校验（§6）→ `RunManager.attach_run(...)` → `{run_id, status:"attached"|"live"}`。
- `run_id`：入参 > tape 首行 `workflow_started` > 文件名 `<run_id>.jsonl`。
- attached run 与 in-process 同待遇：`GET /api/runs/<id>`、`/events`、`/meta`、WS subscribe 统一读 `RunView`。

### 2.2 attach_run（read-only + stream-on-demand + tail-follow）
1. 安全校验 tape_path（§6）。
2. **read-only 探测首行**：`tape_reader.replay(path)` 流式 yield；取首条判 `workflow_started`。**partial/empty 首行**（tape 刚建、写者还没 flush 完整行）→ 不立即 403，标 `live-pending`；follow task 等首行完整可解析；**5s 仍无有效 `workflow_started`** → 403 `not-orca-tape`。
3. **不 bulk replay**：注册 `AttachedRunHandle(bus=新 EventBus, follow_task, terminal=False, source="attached")` 进 `_runs[run_id]`。事件**按 client 请求**从 tape 流式 emit（§3）。
4. **follow task**（asyncio 轮询 0.3s mtime/size 增量；POSIX 可选 kqueue）：从已读 offset 起，按 **newline 切分**（残留不足一行入 buffer 等下次 poll）→ 每整行 parse Event → `bus.emit`（同 bus，单写入路径）。终态事件到达 → `terminal=True` + 停 follow（run 留 registry）。
5. **inode 变化（rename/move/rotate）或 size 缩小（truncate）** → 停 follow + `terminal="corrupted"` + warn（客户端收 `error` 事件；不再追，防错位）。
6. **detach**（全 WS 断开 + 终态后）：cancel follow + `_runs.pop`。

### 2.3 RunView 协议 + 双 handle（单 registry）
- `RunView`（只读协议）：`{run_id, bus: EventBus, state, status, source: "in-process"|"attached"}`。
- `InProcessRunHandle(RunView)`（既有，改协议基类）：`+ wf, gate_handler, chart_ingestor, ...`。
- `AttachedRunHandle(RunView)`（新）：`+ follow_task, terminal, tape_path`。无 `wf`/`gate_handler`/`chart_ingestor`。
- `_meta_from_handle(h)`：`isinstance` 分支——attached 形态 `wf=None`（不读 `len(wf.nodes)`，topology 从 tape `workflow_started.data.topology` 读或省略）。
- `_teardown_handle(h)`：attached 形态**跳过 sock unlink**（那是 in-process 的 chart ingestor sock，属别进程）。
- `_runs: dict[str, RunView]` 单一 registry 不变。

---

## 3. seq-windowed events + meta + perf（Step1）

- `GET /api/runs/<id>/meta` → `{run_id, status, source, event_count, byte_size, oldest_seq, newest_seq, writable, huge(bool), overview?: {...}}`。
  - `writable`：in-process=True，attached=False（前端 `writable=false` 时 gate 模态显 "observe-only (attached run) — 在该 run 自己的 shell 作答"，禁用提交）。
  - `overview`（**仅 huge 模式**返）：`{agents:[{name,status,elapsed,tokens}], charts:[{label,title,chart_type}], cost_usd, run_status}` —— 服务端 fold 同一 tape 派生（非第二真相）。
  - `huge`：服务端实测全量 fold P99 > 200ms（或 event_count>50k 或 byte_size>5MB 兜底）。
- `GET /api/runs/<id>/events`（向后兼容扩展）：无参=全量；`?since=N`=`seq>N`；`?since=N&limit=M`=`[N+1,N+M]`；`?tail=M`=最后 M。
- **前端打开 run**：
  1. `GET /meta`。
  2. `huge=false` → `GET /events`（全量）→ client fold（v2 不变，全 selectors 准）。
  3. `huge=true` → overview selectors 读 `meta.overview`（服务端派生）+ `GET /events?tail=500` → fold 进 Conversation/Log + WS live tail（`resume since=newest_seq`）。**上滚到窗口顶** → `GET /events?since=<oldest-M>&limit=M` **增量 prepend fold**（不重算全窗口，O(window)）。**reconnect 有 seq 空洞**（older chunk 与 tail 间断）→ 先 `?since=gap_lo&limit=gap_size` 填洞再 resume。"load full" 按钮 → 全量拉回 client-fold（opt-in）。
- **AC**（见 §8 三条 measurable）：50MB tape 下 `/meta` P99<100ms、`/events?tail=500` P99<300ms。

---

## 4. Y1 — `orca run` web 默认（Step2）

- `orca run <wf> [inputs]`（无 `--tui`/`--background`）默认：
  1. **端口**：探测 7428 `GET /api/health`（§5）；是 orca → 复用（把 run 注册进既有 server 的 RunManager）；否/不可达 → 选空闲端口起新 in-process serve。`--port` 显式指定且被占 → fail loud。
  2. in-process `RunManager.start_run(...)`（同 `POST /api/run`，run 在本进程；走 bus，不 attach）。
  3. `webbrowser.open(http://127.0.0.1:<port>/runs/<run_id>)`。
  4. **WS 事件驱动 auto-exit**：服务端维护 `last_ws_disconnect_at`；任一 WS connect/disconnect 重置计时；`now - last_ws_disconnect_at > N(15s, env ORCA_WEB_AUTOEXIT_SECONDS) AND run.terminal` → 退。`--stay` 永不自动退。
  5. 退出码同既有（0/1/2）。
- `--tui`：旧 Textual TUI（opt-in，保留）。`--background`：既有 detached headless（不变；监控走 `orca open`）。

---

## 5. Y2 — `orca open` + `/orca open`（Step2；弃 MCP，U2）

- **`GET /api/health`** → `{app:"orca", version, pid}`（D4/D6 探测用；既有 server 加此路由）。
- **`orca open <run_id> [--tape <path>]` CLI**：① 探测默认端口 `GET /api/health`；是 orca → 复用；否/不可达 → 后台起 `orca serve`（空闲端口）。② 解析 tape 路径（`runs/<run_id>.jsonl` 或 `--tape`）→ `POST /api/runs/attach`。③ `webbrowser.open(/runs/<run_id>)`。
- **`/orca open <run_id>` slash 命令**（opencode plugin，`messages.transform` 入口，同 `/orca run` 派发）：marker → 调 `orca open` CLI（哑传输，零业务逻辑，grep 守门）。
- in-session 跑时宿主驱动 run（daemon 写 tape），web 经 attach tail-follow 当观察窗。
- **MCP 工具 `open_run` 不在本 SPEC**（in-session 无 MCP 注册面）；单独立项 phase-X。

---

## 6. 安全（tape 路径；闭环 R12/R13）

`resolve_tape_path(tape_path)`：
1. `p = Path(tape_path)`；**先 lstat 记 symlink**；`resolved = p.resolve()`。
2. `resolved.relative_to(runs_dir.resolve())` 不抛 OR 命中 `ORCA_WEB_TAPE_ALLOWLIST`（`os.pathsep` 分隔绝对前缀）——**用 `relative_to` 非 startswith**（防 `runs_evil` 前缀碰撞）。
3. **post-resolve re-check**：再 `resolved.resolve()` 确认无逃逸（防 allowlist 内 symlink 指出）。
4. **open 后 fd re-lstat** 防 TOCTOU：`os.open` 后 `os.fstat` 对比 resolved stat，不一致 → 拒。
5. 不符 → 403 `{error:"tape_path out of bounds"}`，不读文件。
6. 相对路径相对 CWD（与 `orca run` 写 tape 一致）。
7. **首行非 Orca tape**（partial 见 §2.2 step2 live-pending；5s 后仍非 `workflow_started`）→ 403 `not-orca-tape`。

---

## 7. 失败路径（fail loud；闭环 R14-R18）

- tape 缺失/不可读 → 404，不注册。
- 空文件 → `live-pending`，等首事件；`meta.event_count=0`。
- partial 首行 → live-pending，5s 后 403（§2.2 step2）。
- rotate/truncate/inode 变化 → 停 follow + `terminal="corrupted"` + 客户端 `error` 事件。
- `--port` 被占 → fail loud。
- `webbrowser.open` 失败/无 DISPLAY → 打印 URL，不阻塞 run。
- **run_id 碰撞**（已在 `_runs`）→ 409，不覆盖。
- **同 tape_path 重复 attach** → 幂等返回既有 handle（不重起 follow）。
- **follow task 异常退出** → `terminal="corrupted"` + warn + 客户端 `error` 事件。

---

## 8. 验收标准（measurable oracle；闭环 R3/R4/R13/R18/R19）

**功能性 AC**：
1. attach `--background` run tape → live：tape 写事件到 WS 推送 **P99 < 500ms**；负向：断 WS 后页面不再更新（验证不靠轮询造假）。
2. attach in-session tape → live（同上时延）。
3. attach 终态 tape → status terminal + follow stopped + 无新事件。
4. **perf（三条）**：(a) `GET /meta` P99 < 100ms on 50MB/500k fixture（`scripts/gen_big_fixture.py`，CI runner pinned warm）；(b) `GET /events?tail=500` P99 < 300ms 同 fixture；(c) 浏览器首屏不参与硬指标（只断言无 console error）。
5. `orca run <wf>` → `webbrowser.open` 调用 + run 终态后 auto-exit；**负向 AC**：有活跃 WS 不退；14s 内 WS 重连不退；`ORCA_WEB_AUTOEXIT_SECONDS=1` 测试可加速。
6. `orca run --tui` → Textual TUI 启动；`--background` → detached + run_id+pid。
7. `orca open <id>` / `/orca open` → serve 起/复用 + attach + 浏览器开。
8. **安全（6 样例）**：`../../etc/passwd` / `/runs_evil/x`（前缀碰撞）/ `runs/good/../../etc` / symlink-out / symlink-in-then-escape / URL 编码 `%2e%2e` → 全 403；allowlist 命中放行、未命中 403 各一。
   - **transport nuance（e2e 实证）**：`%2e%2e` 经 **JSON body** 传输时不被 URL-decode（传输是 JSON 非 URL path），字面 `%2e%2e` 是不存在的文件名 → **404**（非 403）。**无 traversal 旁路**（安全保证成立）；404 对 JSON-body transport 可接受。URL-path transport 才需 `%2e%2e`→403。
9. 非 Orca tape / partial 首行 5s → 403。
10. huge 模式：overview 从 `/meta.overview`（服务端派生）；tail 窗口 live；上滚增量 prepend（O(window)）；"load full" 拉全量。
11. gate attached run（writable=false）→ 模态显 observe-only，禁提交。

**铁律 AC**：
12. grep Zustand 仍 1；`_runs` 单 registry（grep 无第二 dict）；**selector AST 守门**：所有 `selectX` 签名 `(state)=>...` 单 state 入参（杜绝第二 store 旁路）。
13. attacher `grep` 无 `Tape(resume=True)`（D6 read-only）；`open(mode="r")` 只读。

---

## 9. 实施顺序

1. **Step1（X + perf）**：`tape_reader.replay`（read-only）+ `RunView`/`AttachedRunHandle` 双实现 + `_meta_from_handle`/`_teardown_handle` attach 分支 + `attach_run`（stream-on-demand + tail-follow + newline buffer + inode/truncate 检测）+ `POST /api/runs/attach` + 安全（§6）+ `GET /api/health` + `GET /meta`（huge/overview）+ `GET /events?since/limit/tail` + 前端 huge 模式（overview+tail+上滚增量+load full+gap-fill）+ `scripts/gen_big_fixture.py` + 单元/集成/安全测试。
2. **Step2（Y）**：`orca run` web 默认（端口探测/serve/浏览器/WS 驱动 auto-exit/env）+ `--tui` opt-in + `orca open` + `/orca open` slash + gate observe-only + 测试。
3. **test-coverage-e2e**：真 background attach、in-session `/orca open`、终态 attach、50MB perf 三条、安全 6 样例、`orca run` 浏览器+auto-exit+负向、`--tui`、`--background`、run_id 碰撞 409/重复幂等/follow 死 corrupted。循环 clean-code 至全绿。

---

## 前置阅读
- [`web-shell-v2-spec.md`](web-shell-v2-spec.md)（v2 单 store/selector/WS resume/RunManager；D3 `resolve_asset_path` 守卫复用）
- [`phase-3-events.md`](phase-3-events.md) §3.2/§3.3（Tape append/resume、bus 单写路径）
- [`in-session-shell-design-draft.md`](in-session-shell-design-draft.md) §2.6/§13（messages.transform 入口；无 MCP 工具）
- 代码：`orca/iface/web/run_manager.py`（RunHandle L54-75 / `resolve_asset_path` L138-172 / `_meta_from_handle` L478-505 / `_teardown_handle` L555-559 必改）、`routes/runs.py`、`ws_handler.py`、`orca/iface/cli/commands.py`（`run`/`serve`/`in-session`）

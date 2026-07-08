# Web Attach + Web 默认 + in-session open —— 让 web 监控任意单个 run

> Web v2（`web-shell-v2-spec.md`）的 RunManager 只认 `POST /api/run` 起的 in-process run；`orca run --background` / `orca in-session` 起的 run（独立进程）web 看不到。本次补齐：web 按 **tape 路径 attach** 任意 run（read-only tail-follow）+ 大 tape 不卡的 perf + `orca run` 默认走 web + `orca open` / `/orca open` 打开任意 run。tape 唯一真相源不变，单 run/页不变。
>
> SDD 流程：SPEC（`docs/specs/web-attach-and-default-spec.md` rev2，spec-review PASS）→ clean-code（Step1 X+perf、Step2 Y、3 defect 修复）→ test-coverage-e2e 真跑 PASS。

## 铁律（ upheld）
1. tape 唯一真相源；web 纯渲染。默认全量 client-fold（v2 不变）；huge 模式 overview 是服务端对**同一 tape** 的 fold（可 load full 校验），非第二真相。
2. 单 run 加载（attach 一个 tape，不扫盘）。
3. 不卡：默认全量即时；huge 模式 `/meta` + tail 窗口 + 上滚懒加载。
4. 安全：tape 路径 `relative_to` 三重守卫（symlink-before-resolve + post-resolve + fd re-lstat 防 TOCTOU）。
5. 单 `_runs` registry + 单 bus 写入路径（attach 与 start_run 对称）。
6. **read-only** attach（绝不 `Tape(resume=True)` 抢外部 flock）。

## 交付

### Step1（X + perf，commit `69e5c7b`）
- **`tape_reader.replay`**（read-only：`open(mode="r")` + 反向 byte-block `tail_events` + `since_limited` 早 break）。
- **`RunView` 协议 + 双 handle**（`InProcessRunHandle` / `AttachedRunHandle`），单 `_runs` registry；`_meta_from_handle`/`_teardown_handle` attach 分支（wf=None 容错、跳过 sock unlink）。
- **`attach_run`**：read-only 探测首行 → 注册 handle + follow task（stream-on-demand，**不 bulk replay**）；newline buffer + inode/truncate → corrupted + error 事件。
- **路由**：`POST /api/runs/attach`、`GET /api/health`、`GET /api/runs/<id>/meta`（含 `writable`/`huge`/`overview`）、`GET /events?since=&limit=&tail=`（纯读，不 emit bus）。
- **安全 `resolve_tape_path`**：`relative_to` + symlink 守卫 + fd re-lstat。
- **前端 huge 模式**：`serverOverview` slice + tail 窗口 + 上滚增量 prepend + reconnect gap-fill + load full；attached（writable=false）gate observe-only。
- `scripts/gen_big_fixture.py`（50MB/500k fixture）。

### Step2（Y，commit `fe81e42`）
- **`orca run` 默认走 web**：in-process run + serve（`GET /api/health` 探测：是 orca 复用 / 否则空闲端口）+ `webbrowser.open` + **WS client count 驱动 auto-exit**（`active_ws_count==0 AND grace` 过才退，`ORCA_WEB_AUTOEXIT_SECONDS` / `--stay`）。
- **`--tui` opt-in**（保留 TUI，不删）；**`--background`** 不变。
- **`orca open <run_id> [--tape]`** CLI + **`/orca open`** slash（opencode plugin `messages.transform` 三元 dispatch，哑传输，签名契约测试）。

### 3 defect 修复（commit `58947fd`，e2e 真跑发现）
- **AC9**：首行非 `workflow_started` → 前置 403 `not-orca-tape`（不再误纳 running）；follow 在 `bus.relay` 前拒（不污染 client fold）。
- **AC11**：`AskGate` 认 `writable=false`（共享 `gate-writable.tsx`：`useGateWritable` + `GateObserveOnlyNotice`，AskGate+PermissionGate 同源；submit 禁）。
- **AC5 负向**：`active_ws_count` 跟踪（connect+1/disconnect-1），活跃 WS 抑制 auto-exit，全断后 N 秒退。

## 验证（test-coverage-e2e 真跑 PASS）
真 `orca serve` + 真 curl + 真 Playwright + 真 `orca run`/`open`/`--tui`/`--background` + 真 `websockets` client（无 mock；live 写入用受控 writer，SPEC 允许）。

| AC | 结果 |
|---|---|
| attach `--background` tape → live（P99 < 500ms） | **PASS** P99=250ms |
| attach 终态 tape → terminal + follow stopped | PASS |
| attach in-session tape（同 code path） | PASS |
| perf `/meta` P99<100ms / `/events?tail=500` P99<300ms（103MB fixture） | **PASS** 5.2ms / 41.5ms |
| `orca run` web 默认 + auto-exit（含负向：活跃 WS 不退） | PASS |
| `--tui` / `--background` | PASS |
| `orca open`（serve 探测+attach+浏览器） | PASS |
| gate observe-only（writable=false） | PASS |
| 安全 5/6 样例 403 + allowlist（sample6 JSON-body `%2e%2e`→404 transport nuance，无旁路） | PASS |
| §7 失败路径（404/409/幂等/truncate/rotate/partial 5s→403/非 wf 首行 403） | PASS |
| 铁律（单 `_runs`/单 store/selector AST/read-only） | PASS |

测试：pytest 674 passed / npm 262 passed。

## 用法（新增）
```bash
# 1) 默认：orca run 直接起 web 监控（in-process）
orca run examples/demo_mixed.yaml             # → 浏览器自动开 /runs/<id>，跑完自动退

# 2) 监控一个 --background run（独立进程）
orca run examples/demo_mixed.yaml --background  # → run_id
orca open <run_id>                              # → attach + 浏览器开（observe-only）

# 3) 监控一个 in-session run（宿主驱动）
#    在 opencode 里：/orca open <run_id>
```

## Commit
`69e5c7b`(Step1) + `fe81e42`(Step2) + `58947fd`(3 defect) + docs `e6a4d35`/`1ebb521`/`1088435`。

## Follow-up（非阻塞）
- `/orca open` slash 在 opencode event loop 同步阻塞 ~10s 等后台 serve ready →「fork-and-return」单独立项。
- `orca open` spawn 的 detached serve 无 PID 文件（`orca ps` 不列）→ 整合单独立项。
- macOS：关闭 `orca run` 浏览器 tab 前若 tab 保持打开，进程会因活跃 WS 不 auto-exit（符合"有人看就不退"语义，文档说明即可）。

SPEC：[`web-attach-and-default-spec.md`](../specs/web-attach-and-default-spec.md)。e2e 证据：`/tmp/orca-attach-verify/`。

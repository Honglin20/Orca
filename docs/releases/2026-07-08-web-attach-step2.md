# 2026-07-08 — Web attach Step2 (Y): `orca run` web 默认 + `orca open` + `/orca open` slash

按 SPEC [`web-attach-and-default-spec.md`](../specs/web-attach-and-default-spec.md) rev2 §4/§5/§8 AC5-7/11 实现 Web attach Step2。

## 范围（SPEC §4 / §5 / §0 D4-D5）

- **Y1** — `orca run <wf>` 默认走 web（D4）：探测 7428 → 是 orca 则复用 `POST /api/run`；否则起新 in-process serve + `RunManager.start_run`（in-process 走 bus，**不 attach**）+ `webbrowser.open(/runs/<id>)` + **WS 驱动 auto-exit**（`now - last_ws_activity_at > N(15s default, ORCA_WEB_AUTOEXIT_SECONDS env 覆盖) AND run.terminal`）。
- **Y2** — `orca open <run_id>` CLI（SPEC §5）：探测端口 → 复用 / 后台 spawn `orca serve` → resolve tape path → `POST /api/runs/attach` → `webbrowser.open(/runs/<id>)`。attached run 走 Step1 的 `writable=false` gate。
- **Y3** — `/orca open <run_id>` slash command（in-session）：opencode plugin `orca.ts` 加 `open` case → 走新 `spawnTopLevelCli` helper（`orca open <id>`，**非** `orca in-session`），plugin 是 dumb transport（grep 守门）。
- **D5**：`--tui` opt-in 保留旧 Textual TUI 路径；`--background` 完全不变。

## 交付（files）

- **`orca/iface/cli/commands.py`**：
  - `run` 命令加 `--tui` / `--port` / `--stay` 三个 flag（默认 web）
  - 新 `_run_web_default(config, *, port, stay)`（probe / 复用 / in-process 分流）
  - 新 `_serve_and_run_inprocess(config, wf, *, host, port, stay)`（uvicorn serve task + RunManager.start_run + 等 run 终态 + WS auto-exit）
  - 新 `_wait_ws_autoexit(web_server, n)`（轮询 `last_ws_activity_at`）
  - 新 `_wait_server_started(server, timeout)`（等 uvicorn startup 完成，避免浏览器先于 server ready）
  - 新 `open_run`（typer command name=`open`，函数名避 Python `open` 内置冲突）
  - 新 `_open_run(run_id, *, tape_path, port)`（probe / spawn / attach / browser）
  - 新 helpers：`_probe_orca_server`、`_find_free_port`、`_is_port_free`、`_post_run_to_existing`、`_poll_run_terminal`、`_open_browser_or_print`、`_spawn_background_serve`（返回 bool，捕获 FileNotFoundError）、`_wait_for_health`、`_attach_and_get_error`、`_load_wf_or_exit`、`_web_autoexit_seconds`
- **`orca/iface/web/ws_handler.py`**：
  - `WebServer.__init__` 加 `last_ws_activity_at`（初值 = 构造时刻，单调钟）
  - 加 `_touch_ws_activity` 方法
  - `ws_endpoint` accept 后 touch（connect）
  - finally `_cleanup` 后 touch（disconnect）
- **`orca/iface/in_session/templates/opencode/orca.ts`**：
  - `buildCliArgs` 加 `open` case（`return ["open", rid]`）
  - `rewriteText` 加 `open` case（ack 信封）
  - 新 `spawnTopLevelCli` helper（spawn `["orca", ...args]`，非 `in-session`）
  - dispatch 处三元路由：`sub === "open" ? spawnTopLevelCli(cliArgs) : spawnCli(cliArgs)`

## 关键决策

- **复用 vs in-process**（SPEC §4 step1）：探测端口是 orca → 复用既有 server（POST /api/run + 轮询 meta）；否/不可达 → 选空闲端口起新 in-process serve。`--port` 显式且被非 orca 占 → fail loud exit 2。
- **shutdown 单一真相源**：`manager.shutdown()` 由 uvicorn lifespan 负责（`server.py:54-57`）；`_serve_and_run_inprocess` finally **不再重复调**（避免双 shutdown 语义混乱）。仅在 serve_task 异常退出时兜底显式调。
- **Ctrl-C 语义（Py3.11+）**：`asyncio.run` 收 SIGINT 时 cancel 主 task（抛 `CancelledError`，非 `KeyboardInterrupt`）；协程内 `except CancelledError` 走 cleanup，外层 `_run_web_default` try/except `KeyboardInterrupt` → exit 130。
- **跨进程绝对路径**：`_post_run_to_existing` 把 `yaml_path` `resolve()` 后再 POST（既有 server 的 CWD 可能与本进程不同）。
- **`--stay` 在复用模式下显式提示**（不静默忽略）。

## 闭环 review（code-reviewer + test-coverage）

**4 BLOCKER 闭环**：
- 🔴 `except KeyboardInterrupt` 在 async 协程不生效 → 改 `except CancelledError` + 外层映射 130
- 🔴 `_serve_and_run_inprocess` 双调 `manager.shutdown()` → 移除 finally 重复调用，单一真相源 = lifespan
- 🔴 `_spawn_background_serve` 未捕获 `FileNotFoundError`（`orca` 不在 PATH）→ 返回 bool + 调用方 fail loud
- 🔴 `_serve_and_run_inprocess` 零集成测试 → 加 6 个直接单测（mock RunManager + uvicorn.Server，覆盖 ConfigurationError / CancelledError 等）

**6 MAJOR 闭环**：
- 🟡 `_post_run_to_existing` 抛 RuntimeError 未捕获 → `_run_web_default` try/except → exit 1
- 🟡 `yaml_path` 相对路径跨进程 → POST 前 `resolve()` 转绝对
- 🟡 `--stay` 在复用分支被忽略 → 显式 stderr 提示
- 🟡 `start_run` 异常 manager 资源泄漏 → 兜底 shutdown（仅在异常路径）
- 🟡 WS activity 计时真实链路零覆盖 → `test_ws_connect/disconnect/reconnect_resets_activity_at` 3 个 e2e 单测（直接驱动 `ws_endpoint`）
- 🟡 plugin dispatch routing 仅 grep 字面 → 加三元表达式字面序列断言（`? spawnTopLevelCli`）

**MINOR**：
- 🟢 `_post_run_to_existing` 返回 `(run_id, status)` 简化为 `str`（status 永远不用）
- 🟢 `_open_browser_or_print` 失败回退测试补全
- 🟢 `_wait_for_health` False 路径 / `_attach_and_get_error` 网络异常路径补全
- 🟢 `buildCliArgs("open", "", ...)` → null 分支字面守护

## 验证

- **pytest**：674 passed / 30 skipped（baseline 659 + 15 新增：47 web-default/open + 3 WS activity + 其它）
- **npm test**：259 passed / 18 files（前端无改动）
- **铁律 grep**：单 `_runs` dict unchanged；attacher 无 `Tape(resume=True)` 实际调用；TUI 代码保留（`_run_workflow` 仍在）；plugin 禁词 grep 全过；Zustand 单 store unchanged

## AC 对照（SPEC §8）

| AC | SPEC | 状态 | 证据 |
|---|---|---|---|
| AC5 正向（webbrowser.open + run 终态 + auto-exit + env 加速） | §4 step4 / §0 D4 | ✅ | `TestRunWebDefault.test_web_default_reuse_existing_server` + `TestWSAutoexitSeconds` + `TestWSAutoexit` |
| AC5 负向（活跃 WS 不退；WS 重连窗口内不退） | §8 AC5 | ✅ | `test_ws_connect_resets_activity_at` / `test_ws_disconnect_resets_activity_at` / `test_ws_reconnect_within_window_resets_timer` / `test_autoexit_respects_activity_reset` |
| AC6（`--tui` TUI + `--background` detached） | §4 D5 | ✅ | `test_run_tui_invokes_run_workflow` / `test_run_background_unaffected` |
| AC7（`orca open` probe / spawn / attach / browser） | §5 | ✅ | `TestOrcaOpen`（6 case）+ `TestSpawnAndAttachHelpers`（5 case） |
| AC11（gate attached writable=false → observe-only） | Step1 | 不回归 | Step2 不触 writable / meta 路径，`test_attach.py:378` 仍绿 |
| AC12（单 _runs registry） | §8 AC12 | ✅ | `test_single_runs_registry_unchanged` |
| AC13（attacher read-only） | §8 AC13 | ✅ | `test_attacher_no_tape_resume_true` |
| `/orca open` slash signature-contract | §5 | ✅ | `TestOrcaOpenSlashContract`（7 case） |

## Commit

`6616b51`

## 遗留 follow-up（非阻塞）

- 🔵 真浏览器 + 真后台 serve + 真 attach E2E（playwright + 真子进程，需非沙箱）—— 当前覆盖 mock + 单元 + 真事件循环驱动。
- 🔵 slash `open` 在 opencode event loop 内同步阻塞 ~10s（`_wait_for_health` 等后台 serve ready）：架构议题，单独立项「`orca open` 提前 fork-and-return」或 plugin 改异步 spawn。
- 🔵 `orca open` spawn 的 detached serve 无生命周期管理（无 PID 文件，`orca ps` 不列）：SPEC §5 step1 语义如此，单独立项整合。

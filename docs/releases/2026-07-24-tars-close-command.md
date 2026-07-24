# Release — `tars close` 命令（关闭本地 orca web server）

> 日期：2026-07-24
> 分支：`in-session-unified-backend`
> 计划（单一真相源）：[`docs/plans/2026-07-24-tars-close-command.md`](../plans/2026-07-24-tars-close-command.md)（post spec-review conditional-pass，4 blocker + 7 major 已回填正文）

## 改动概述

新增 `tars close` 命令，与 `tars open` 对称，用于关闭本地 orca web server。此前 `tars open` detached 起后台 `tars serve` 但**无对应关闭命令**，只能手 `pkill`。本任务补齐该缺口。

**机制（优先顺序）：**
1. `POST /api/shutdown`（loopback-only，跨平台优雅，触发 uvicorn lifespan shutdown）。
2. PID 兜底（404 老版 server 无端点）：POSIX `SIGTERM`→grace→`SIGKILL`；Windows `taskkill /F`（恒 force-kill，kill 前 stderr warn tape 可能未 flush）。

**覆盖范围：**
- 默认：`[默认端口 7428, ~/.orca 登记端口]`（去重）。
- `--all`：扫描本地所有 LISTEN 端口 → 并发 probe（`Semaphore(16)` + 5s deadline）→ 仅关**本项目指纹匹配**的 orca server（B2 不误杀别用户）。

## 改动文件

| 文件 | 改动 |
|------|------|
| `orca/iface/web/server.py` | B1 第一处 wire：`create_app()` init `app.state.uvicorn_server = None` + `run_server()` 覆写真句柄 |
| `orca/iface/web/routes/attach.py` | 新增 `POST /api/shutdown` 端点（loopback 白名单含 `::ffff:127.0.0.1`；handle None → 500；非 loopback → 403；置位 `should_exit`） |
| `orca/iface/cli/commands.py` | B1 第二处 wire（`_serve_and_run_inprocess`）+ 新增 `tars close` 命令及 11 个 helpers（`close_run` / `_close_servers` / `_safe_lookup_registry_port` / `_dedup_keep_order` / `_shutdown_server_on_port` / `_close_all_concurrent` / `_probe_my_orca_ports_async` / `_local_listening_ports` / `_parse_posix_listen_stdout` / `_parse_netstat_windows_ports` / `_kill_pid_on_port` / `_resolve_pids_on_port`）。顶部加 `import signal` / `import sys`。 |
| `tests/iface/web/test_shutdown.py`（新） | 8 个 web 端 case（含 4 loopback 变体、非 loopback 403、handle None 500、路由注册、health 共存回归） |
| `tests/iface/cli/test_close.py`（新） | 24 个 cli 端 case（含 AC1 endpoint + lifespan + tape 完整、AC2 双指纹隔离 + 非默认端口、AC3 无 server、AC5 registry 未清守门、AC7 found-but-failed、B4 PID re-probe 三路、AC8 deadline、win32-only warn skip） |

## spec-review 4 blocker 闭环证据

- **B1（双 wire）**：`server.py:72`（create_app init None）+ `server.py:133`（run_server wire）+ `commands.py:1332`（_serve_and_run_inprocess wire）。三处齐备，wire 均在 `server.serve()` 之前。守门：`test_shutdown_handle_none_returns_500` + `test_close_lifespan_completes_via_endpoint`。
- **B2（指纹过滤）**：`_shutdown_server_on_port:1962-1964`（入口 probe + `_health_is_my_project`）+ `_probe_my_orca_ports_async:2047`（phase-1 probe 同样过滤）。non-match → `"none"`（不报 fail）。守门：`test_close_all_isolates_foreign_fingerprint`（断言 `post_on_9999 == 0`）。
- **B3（不清 registry）**：grep 确认 `clear_orca_home_registry` 符号不存在；close 路径无任何 `write_*` 调用。守门：`TestCloseDoesNotTouchRegistry`（patch 所有 writer，断言 call_count == 0）。
- **B4（PID re-probe）**：`_shutdown_server_on_port:1993-1998`（kill False 后必 re-probe；空 → `"none"`，仍占 → `"fail"`）。守门：`test_close_pid_fallback_kill_false_reprobe_none` + `test_close_pid_fallback_kill_false_reprobe_still_occupied`。

## 验收标准（AC）对照

| AC | 实现 | 测试 |
|----|------|------|
| AC1（endpoint 必走 + lifespan 完整 + tape 末事件终态） | `_shutdown_server_on_port` 200 路径 | `test_close_default_uses_endpoint_and_skips_pid`（kill 0 调用）+ `test_close_lifespan_completes_via_endpoint`（真 uvicorn）+ `test_close_via_endpoint_preserves_terminal_tape`（末事件 workflow_completed） |
| AC2（`--all` 指纹隔离 + 非默认端口） | `_close_all_concurrent` + `_probe_my_orca_ports_async` | `test_close_all_isolates_foreign_fingerprint` + `test_close_all_discovers_non_default_port` |
| AC3（无 server exit 0） | `_close_servers` closed+failed 都空 → 「no orca server found」 | `test_close_no_server_message_and_exit_zero` |
| AC4（loopback 变体含 `::ffff:127.0.0.1`） | `_LOOPBACK_CLIENTS` 4 元素白名单 | `test_shutdown_loopback_variants_return_200`（4 参数化）+ `test_shutdown_non_loopback_returns_403` |
| AC5（不清 registry 不破坏自愈） | 未加 `clear_orca_home_registry`；靠 `_lookup_my_registered_port` stale 自愈 | `TestCloseDoesNotTouchRegistry`（2 case 守门 writer 零调用） |
| AC6 (a) `tars close` 在 help | `@app.command(name="close")` | `test_close_in_tars_help` |
| AC6 (b) web ≥3 case | — | 8 case |
| AC6 (c) cli ≥6 case | — | 24 case |
| AC6 (d) `test_web_does_not_import_cli` 仍过 | 零新增 web→cli import | 预存 `run_manager.py:37` 违反独立 issue，本任务不恶化（grep 确认 attach.py / server.py 无 `iface.cli.*`） |
| AC7（found-but-failed exit 1） | `_shutdown_server_on_port` 非 200/404 → `"fail"` | `test_close_endpoint_5xx_exits_one_with_port` + `test_close_endpoint_network_error_exits_one` |
| AC8（≤200 ports 墙钟 ≤5s） | `Semaphore(16)` + 5s deadline | `test_probe_my_orca_ports_async_respects_deadline`（200 端口 < 10s） |

## 测试结果

- 新测试：**31 passed + 1 skipped**（win32-only warn 在 linux skip）
  - `tests/iface/web/test_shutdown.py`：8 passed
  - `tests/iface/cli/test_close.py`：23 passed + 1 skipped
- 相邻回归：143 passed, 1 pre-existing fail（`test_web_does_not_import_cli`，`run_manager.py:37` 预存违反，计划明确独立 issue 不在本任务范围）。
- code-reviewer verdict：Implementation **pass** / Test coverage **conditional-pass → 已转 pass**（4 个 🟡 全修：Windows 空 parse fail loud + 平台文案 + AC5 守门 + AC1 tape 直接验证；3 个 🟢 修 1 跳 2 低优先级）。

## 非目标（计划声明）

- 不加 psutil 依赖（端口扫描/PID 杀走 stdlib + 平台 shell-out）。
- 不动 `orca stop`（停 workflow run 的命令）。
- 不实现 token 鉴权（loopback-only 够用；未来 `_auth.py` 切真 token 时需回顾 `/api/shutdown` 的 loopback 分支）。

## 已知风险（计划「风险/注意」声明）

- **Windows PID 兜底恒 force-kill**（无 SIGTERM）→ 跳过 lifespan → tape 可能未 flush。endpoint 路径是 Windows 唯一安全关闭方式；兜底前必 stderr warn。
- **PID 兜底跨平台脆弱**：限定遗留无端点 server；helper 全 try/except + fail loud 文案指引手 `pkill` / `taskkill`。
- **跨环境**：server 在 WSL 时 `tars close` 需在同环境跑（WSL↔Windows 跨边界 loopback + PID 空间不一致，文档提示，代码无法完全解决）。
- **预存无关遗留**：`run_manager.py:37` web→cli import 已破坏 `test_web_does_not_import_cli`（独立修，本任务不恶化）。

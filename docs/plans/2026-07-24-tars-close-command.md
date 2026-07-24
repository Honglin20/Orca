# Plan — `tars close` 命令（关闭 web server）

> 日期：2026-07-24　分支：in-session-unified-backend
> SDD 阶段：接口已确认 → 计划 → **spec-review（conditional-pass，已回填）** → 实现

## 修订记录（post spec-review，conditional-pass 闭环）

spec-reviewer baseline + evaluator 一轮对抗，4 blocker + 7 major 全接受，已并入正文。关键变更：
- **B1（blocker）**：`app.state.uvicorn_server = server` 必须**两处**都 wire（`run_server` + `_serve_and_run_inprocess`），否则 `orca run` web 默认路径下 `/api/shutdown` 恒 500。
- **B2（blocker）**：`--all` 必须用 `_health_is_my_project(health, my_fp)` 过滤，**不能**只按 `app=="orca"` —— 否则共享机会杀别用户 server。
- **B3（blocker，简化）**：**删除 `clear_orca_home_registry` 整个 helper**。`_lookup_my_registered_port`（`commands.py:1601-1604`）读登记后必 probe + 指纹校验，stale 登记自动返 None → `orca open` 自愈 re-spawn。清理是装饰性写入且引入新 writer + 与并发 `exclusive_port_decision` 的 race。删之同时闭环 review #10。
- **B4（blocker）**：PID 兜底 `_kill_pid_on_port` 返 False **不得直接 = fail**；必 re-probe，端口已空（被并发 close 的 winner 关了）→ 返 `"none"`。并发 `--all`loser 不会误判 fail。
- **major**：Windows 无 SIGTERM → PID 兜底恒 force-kill、跳过 lifespan（tape leak 风险）→ 显式 warn + 声明 endpoint 路径是 Windows 唯一安全路径；`--all` 端口扫描并发 `Semaphore(16)` + 总 deadline 5s + 7428/登记端口 short-circuit；localhost 白名单含 `::ffff:127.0.0.1`（IPv4-mapped IPv6 假阳性）；AC1 加固（endpoint 路径必走 + lifespan 跑完、tape 完整）；新增 AC7（found-but-failed → exit 1）。

---

## 背景 / 目标

- `tars open` detached 起后台 `tars serve`（`_spawn_background_serve`），**registry 不存 pid**，且**无任何「关 server」命令/端点** → 只能手动 `pkill -f 'tars serve'`。
- 端口统一后（SPEC §13）单用户单端口，新场景不再堆叠；遗留多端口孤儿仍需清理。
- 目标：加 `tars close`，与 `tars open` 对称，优雅关闭本地 orca web server。

## 已确认接口

1. **命令名**：`tars close`（挂 `tars` app = `orca/iface/cli/commands.py`，镜像 `open_run`）。
2. **机制**：`POST /api/shutdown` 端点（主，跨平台优雅）+ 按端口找 PID 杀（兜底，仅遗留无端点 server）。
3. **范围**：默认关「登记 server + 默认 7428」；`--all` 扫描本地所有监听端口、关掉**本项目（指纹匹配）**的 orca server。

---

## 改动清单（逐文件）

### 1. `orca/iface/web/server.py` + `orca/iface/cli/commands.py` —— 暴露 uvicorn server 句柄（B1，两处）

`should_exit=True` uvicorn 已支持（`_serve_and_run_inprocess:1383` finally 用同 flag 触发 lifespan），但**经 `app.state.uvicorn_server` 传句柄是新机制**，需在创建 server 处赋值：
- `create_app()`：lifespan 前 `app.state.uvicorn_server = None`（默认值，端点取不到时 fail-loud 友好）。
- `run_server()`（server.py:127）：`server = uvicorn.Server(config)` 之后、`serve()` 之前 `app.state.uvicorn_server = server`。
- `_serve_and_run_inprocess()`（commands.py:1327）：`server = uvicorn.Server(uvicorn_config)` 之后 `app.state.uvicorn_server = server`（同处赋值，覆盖 create_app 的 None）。

### 2. `orca/iface/web/routes/attach.py` —— `POST /api/shutdown` 端点

在现有 `/api` 顶层 router（`build_router`，含 `/runs/attach` + `/health`）内新增：

```python
# loopback 白名单：含 IPv4-mapped IPv6（dual-stack socket 常把 IPv4 client 报成 ::ffff:127.0.0.1）。
_LOOPBACK_CLIENTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}

@router.post("/shutdown")
async def shutdown(request: Request) -> dict:
    client = request.client.host if request.client else ""
    if client not in _LOOPBACK_CLIENTS:
        raise HTTPException(403, "shutdown only allowed from loopback")
    srv = getattr(request.app.state, "uvicorn_server", None)
    if srv is None:
        raise HTTPException(500, "uvicorn server handle not wired")
    srv.should_exit = True
    return {"shutting_down": True, "pid": os.getpid()}
```

- **响应不截断**（spec-review 驳回的质疑）：uvicorn 在 main serve loop 检查 `should_exit`，handler 内置位后正常 return → 响应完整发出 → 下一 loop iteration 才进 lifespan shutdown。
- **安全**：loopback-only（非本地 → 403）。与当前 `_auth.py` no-op 一致；shutdown 非破坏（tape 持久、server 可重起）。**不枚举 server bound IP**（`tars close` 恒用 127.0.0.1 探测，bound IP 枚举对真实调用方零收益、徒增复杂度 —— 取简化侧）。
- 依赖单向：端点只读 `app.state` + stdlib，**零 cli import**（不恶化 `test_web_does_not_import_cli`）。

### 3. `orca/iface/cli/web_registry.py` —— **不改**（B3 删除原计划的 `clear_orca_home_registry`）

靠 `_lookup_my_registered_port` 既有 stale 自愈。无新 writer = 无 race。

### 4. `orca/iface/cli/commands.py` —— `tars close` 命令 + helpers

新增 `@app.command(name="close")`（参数 `--host/--port/--all`），镜像 `open_run` 的端点解析（复用 `resolve_web_endpoint` / `_probe_orca_server` / `_lookup_my_registered_port` / `_runs_dir_fp` / `_health_is_my_project`）。

**候选端口集合：**
- 默认：`[target_port(7428), registry_port]`（去重，登记端口 None 则略）。
- `--all`：调 `_local_listening_ports()` 拿本地所有 LISTEN 端口 → **并发** probe `/api/health`（`asyncio.gather` + `Semaphore(16)`，总 deadline 5s；7428 + 登记端口 short-circuit 优先 probe）→ 留 health 非空的。

**逐端口关闭 `_shutdown_server_on_port(probe_host, port, my_fp) -> str`**（返 `"endpoint"|"pid"|"fail"|"none"`）：
1. probe health → 非 orca → `"none"`；**oraca 但 `_health_is_my_project(health, my_fp)` 为 False（别用户/foreign）→ `"none"`（B2，`--all` 不误杀）**。
2. `POST /api/shutdown`（127.0.0.1）：
   - 200 → 短轮询（≤3s）确认 health 已消失 → `"endpoint"`。
   - 404（端点不存在 = 遗留老 server）→ 走 PID 兜底（步骤 3）。
   - 其它（5xx / 网络错）→ `"fail"`（fail loud）。
3. PID 兜底 `_kill_pid_on_port(port) -> bool`：
   - **POSIX**：`os.kill(pid, SIGTERM)`（uvicorn catch → 优雅 lifespan）；pid 经 `lsof -ti tcp:PORT` 或 `ss -tlnp` 解析。SIGTERM 后轮询确认退出；超时再 SIGKILL。
   - **Windows**：无 SIGTERM → `taskkill /PID <pid> /F`（**恒 force-kill、跳过 lifespan、tape 可能未 flush**）→ 先 stderr warn `legacy server 在 Windows 上无法优雅关闭，tape 可能未 flush` 再 kill（B7）。
   - 返 False 后**必 re-probe**（B4）：端口已空（被并发 winner 关了）→ `"none"`；仍占用 → `"fail"`。

**收尾 / 汇总：**
- 打印 `closed N orca server(s): [ports]`（每端口标 endpoint/pid 路径）或 `no orca server found`。
- **不清 registry**（B3，靠自愈）。
- exit code：全关 / 无事可做 → 0；任一候选返 `"fail"` → 1。

**新增 helper：**
- `_local_listening_ports() -> list[int]`：POSIX 试 `ss -tlnH`（解析 `*:PORT` / `0.0.0.0:PORT` / `[::]:PORT`），Windows 试 `netstat -ano`（`TCP ... LISTENING` 取本地端口列）。全失败 → `raise RuntimeError`（fail loud，提示 `pkill -f 'tars serve'`）。
- `_kill_pid_on_port(port) -> bool`（如上，POSIX SIGTERM / Windows taskkill）。
- `_shutdown_server_on_port(...)`（如上）。

### 5. 测试

**`tests/iface/web/test_attach.py`（或新 `test_shutdown.py`）—— ≥3 case：**
- POST `/api/shutdown` 设 `app.state.uvicorn_server.should_exit = True`，返 `{shutting_down, pid}`。
- loopback 变体（`127.0.0.1` / `::1` / `::ffff:127.0.0.1`）→ 200；非 loopback（mock `192.168.x.x`）→ 403。
- 句柄未 wire（`uvicorn_server=None`）→ 500。

**`tests/iface/cli/test_close.py`（新）—— ≥6 case：**
- 默认：mock probe 7428=orca+本项目 → POST `/api/shutdown` 200 → 再 probe None → 报 "closed(endpoint)"；**断言 `_kill_pid_on_port` 未被调用**（AC1：endpoint 路径必走）。
- **lifespan 完整**（AC1）：attach 一个持续 emit 事件的 run → close → 断言 tape 末事件是终态（`workflow_completed/cancelled`），非 mid-step 截断。
- `--all` + **指纹隔离**（B2/AC2）：双 `ORCA_HOME` 起 2 server（mock health 指纹不同）→ `tars close --all` 只关自己，另一个存活。
- `--all` + **非默认端口**（AC2）：`ORCA_WEB_PORT=9999` 起一个 server，断言 `_local_listening_ports` call_count > 0 且发现并关闭 9999。
- 遗留 PID 兜底：endpoint 404 → `_kill_pid_on_port`（mock 返 True）→ 报 "pid"；**返 False + re-probe 空 → `"none"`**（B4 并发语义）。
- **found-but-failed**（AC7）：mock endpoint 返 503 → exit 1 + 端口出现在 stderr。
- 无 server：probe 全 None → "no orca server found"，exit 0。

**守门：**
- `tars close` 出现在 `tars --help`。
- `test_web_does_not_import_cli` 仍过（预存 `run_manager.py:37` 遗留违反与本计划无关，独立修）。

---

## 验收标准（AC）

- **AC1**：新起的 `tars serve`（含 `tars open` detached / `orca run` web 默认）可被 `tars close` 优雅关闭 —— 断言 **endpoint 路径必走**（`_kill_pid_on_port` 未调用）+ **lifespan 完整跑完**（attached run 的 tape 末事件为终态）。
- **AC2**：`tars close --all` 发现并关闭**本项目指纹匹配**的所有 orca server；含非默认/非登记端口（`ORCA_WEB_PORT=9999` fixture）；**双 `ORCA_HOME` 隔离**（只关自己，别用户存活）。
- **AC3**：无 server 时 `tars close` 打印「无」且 exit 0。
- **AC4**：`/api/shutdown` loopback 变体（含 `::ffff:127.0.0.1`）→ 200；非 loopback → 403。
- **AC5**：关闭登记端口后**不清 registry 也不破坏自愈** —— 下次 `tars open` 正常（stale 登记经 probe+指纹返 None → re-spawn）。
- **AC6（具体不变量）**：(a) `tars close` 在 `tars --help`；(b) web 端 ≥3 case；(c) cli 端 ≥6 case；(d) `test_web_does_not_import_cli` 仍过。
- **AC7**：找到 orca 但关闭失败（endpoint 非 404 错）→ exit 1 + 端口名入 stderr。
- **AC8（性能）**：≤200 listening ports 主机上 `tars close --all` 墙钟 ≤ 5s（`Semaphore(16)` + 总 deadline）。

## 非目标

- 不加 psutil 依赖（端口扫描/PID 杀走 stdlib + 平台 shell-out，含 fail-loud 兜底）。
- 不动 `orca stop`（那是停 workflow run）。
- 不实现 token 鉴权（loopback-only 够用）。**未来 `_auth.py` 切真 token 时需回顾 `/api/shutdown` 的 loopback 分支**（改为 token 任一通过即放行的 fallback）。

## 风险 / 注意

- **Windows PID 兜底恒 force-kill**（无 SIGTERM）→ 跳过 lifespan → tape 可能未 flush。endpoint 路径是 Windows **唯一安全**关闭方式；兜底前必 stderr warn。
- **PID 兜底跨平台脆弱**：限定遗留无端点 server；helper 全 try/except + fail loud 文案指引手 `pkill -f 'tars serve'`。
- **遗留 server（如现 WSL PID 56352）无 `/api/shutdown`**：默认 `tars close` 候选集 = `{7428, 登记端口}`，若 56352 在 7428 则被 PID 兜底关掉；若在别的端口则需 `tars close --all`（经 PID 兜底）或手 `kill`。
- **跨环境**：server 在 WSL 时，`tars close` 需在**同环境**跑（同 127.0.0.1 + 同 PID 空间；WSL↔Windows 跨边界 loopback 判定 + PID 空间不一致，文档提示，无法用代码完全解决）。
- **预存无关遗留**：`run_manager.py:37` web→cli import 已破坏 `test_web_does_not_import_cli`（独立修，本计划不恶化）。

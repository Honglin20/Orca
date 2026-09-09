# SPEC：Windows 原生兼容（chart TCP 分支 + in-session 锁 shim + CLI 修复）

> 日期：2026-09-08 定稿（sdd-loop Phase 1 评审环轮 2 后；用户决策 U1~U5 已落盘）
> 实测依据：2026-09-08 Windows 原生实测（`.e2e_win/`，conda env `orca-win`）
> 原则：**POSIX 运行时行为零改动（D3 限流为显式豁免项）**；共享文件允许为平台分支新增代码，POSIX 分支语义逐字保留。

## 背景实测根因（修复依据）

| # | 问题 | 根因 |
|---|---|---|
| 1 | run 卡死 + 前端推送全断 + 90s 内 40 万次 crash 重起 | `chart_ingestor` 调 `asyncio.start_unix_server`（win32 无此属性）→ crash callback 无限重起饿死事件循环 |
| 2 | `orca` CLI 全废 | `iface/in_session/{cli,daemon,chart_daemon}.py` 模块级 `import fcntl` |
| 3 | `tars ps/wait/logs/doctor` 崩 | `bg_runner.py:301-302` 默认参数 `os.fork` / `os.setsid` 导入期求值 |
| 4 | CLI 中文 GBK 乱码 | stdout 管道下 cp936 编码 |
| 5 | CC hook 静默死 | `bash`/`python3` 解析到 Store stub（**out-of-scope**，独立项） |
| 6 | 探活变杀进程（高危） | `bg_runner.py:222` `pid_alive` 的 `os.kill(pid,0)` 在 Windows 语义 = TerminateProcess；`_daemon_liveness.py:120` Windows 误入 macOS 分支同命中此杀 |

## 设计决策

### D1 chart 传输平台分支
win32 用 TCP `127.0.0.1:<临时端口>`（`asyncio.start_server`，协议字节级不变：单行 JSON + ack，短连接）。端口 sidecar 文件 `<sock_path>.with_suffix(".port")` 由 server bind 后写入，cancel/finally 删除。POSIX 继续走 Unix socket，零改动。

- **port tmp 文件名带 pid**（`.port.<pid>.tmp`）：防「双 daemon 稳态」（liveness 保守 false → respawn 是架构接受的常态）下两写者对固定 tmp 名交错 → FileNotFoundError。
- `os.replace` 抛 PermissionError（reader 持句柄）→ 重试 1 次后 raise（由 D3 熔断兜底，不无限循环）。
- `chart_endpoint` 返 `""`（port 文件缺失/损坏）→ Orca 侧 `logger.warning` 记录原因（防 script 端「不在 Orca run 上下文」误导归因）。
- crash 重起后端口变更语义：direct run 已 spawn 的 script（env 一次性注入）永久失联——接受（D3 修复后重起罕见）；in-session 每次 `next` 重写 env 文件自愈。

### D2 地址解析单一真相源
`orca/chart/_paths.py` 的 `chart_endpoint(sock_path) -> str`：POSIX → `str(sock_path.resolve())`；win32 → 读 port 文件 → `"tcp://127.0.0.1:<port>"`，缺失/损坏 → `""`（不注 env → script 端按既有「缺 ORCA_CHART_SOCK」fail loud）。client（`_render.py`）按 `tcp://` 前缀分支 AF_INET / AF_UNIX，并加 `hasattr(socket, "AF_UNIX")` 守卫 fail loud。`SOCK_PATH_MAX` 长度检查仅对路径形态有意义（tcp 串恒短，保留无害）。

### D3 crash callback 限流（全平台，**POSIX 零改动原则对本项显式豁免**）
`make_crash_callback` 工厂维护**跨重起链共享**的滚动窗口状态：首次调用创建 `crash_times` 列表，重起链内经内部参数复用**同一列表**（**禁止每代重起新建闭包/新列表；禁止模块级全局**——模块级会让 run A 的 crash 计数污染 run B，web 与 in-session 均为每 run 一次工厂调用、per-run 链闭包作用域是唯一正确粒度）。滚动窗口（默认 60s）内 crash > `max_restarts`（默认 5）次 → 放弃重起 + `logger.error` fail loud。

**保留分支（不得在重写中丢失）**：`task.cancelled()`（teardown）→ 静默 return 不计数；`exc is None`（正常退出）→ 不重起不计数。

POSIX 豁免声明：无限重起 → 限流熔断是对「恒崩饿死事件循环」（根因 1）的跨平台加固，属对 POSIX 行为的有意变更。

### D4 flock shim
新增 `orca/iface/in_session/_flock.py`（stdlib-only）：`flock(fd, flags)` + `LOCK_EX/LOCK_NB/LOCK_UN`。POSIX → `fcntl` 直传；win32 → `msvcrt.locking`（1 字节，先 `lseek(0)`；NB 冲突 → raise `BlockingIOError` 归一 POSIX 语义）。

- **迁移面**：`cli.py` **8 处** fcntl 调用（`_try_acquire_flock`/`_release_flock`/bootstrap marker 锁 2 处/`_is_tape_flock_held` 2 处/gc 锁 2 处）+ `fcntl.LOCK_*` 常量引用一并改走 shim；`daemon.py` 2 处；`chart_daemon.py`（`_FlockSafeTape`）4 处。三文件删模块级 `import fcntl`。
- **unlock 语义**：win32 未持锁 unlock → **no-op**（对齐 POSIX `flock(LOCK_UN)` 的 no-op 成功），禁止透传 msvcrt 的 raise。
- 锁文件必须可写模式打开（现状 `open(path, "w")` 满足）；msvcrt 锁随 fd 关闭释放，现有「finally unlock + close」两平台等价。

### D5 liveness 探测平台分支
`_daemon_liveness.socket_daemon_alive`：win32 读 port 文件（`chart_port_file_path`）→ AF_INET connect 探（语义对齐：无文件/refused/超时 → False）。`pidfile_daemon_alive`：win32 走 **OpenProcess 存活探（OpenProcess-only，无 cmdline 校验）**（用户决策 U2-A；pid 复用假阳性风险已声明，镜像名校验列 follow-up），弱化理由：防 respawn 风暴优先，sidechain 非本轮验证面。**此修复是功能性必修**——现状 Windows 落入 macOS 分支 → `os.kill(pid,0)` = 杀进程 + `ps` 缺失误判 dead → 每 `next` 杀一遍 daemon + respawn（根因 6）。

### D6 bg_runner + 共享进程探活 helper
- `fork_fn` / `setsid_fn` 默认参数改 `None`，函数体内兜底 `os.fork` / `os.setsid`（模块导入期安全；Unix-only fail loud 语义不变）。
- `pid_alive`（`bg_runner.py:222`）增 win32 分支：ctypes OpenProcess（**禁 `os.kill(pid,0)`**——Windows 语义是 TerminateProcess）。
- 共享 helper 放中立位置 **`orca/iface/_winprocs.py`**（stdlib-only），供 `bg_runner`（iface/cli）与 `_daemon_liveness`（iface/in_session）共用（并列子包禁止互引）。
- 消费面：`tars ps/wait/logs/doctor`（`commands.py:428/492/575/2263` 四处 lazy import bg_runner）即刻解锁。

### D7 编码
- `tars main` / `orca main` 入口 `sys.stdout/stderr.reconfigure(encoding="utf-8")`（try/except 包裹，Linux 无感）。
- `orca/exec/env.py build_env_overlay` 注 **`PYTHONUTF8=1`**：防 Windows 子进程管道默认 cp936 → 中文经 `exec/script.py:179-180` 的 UTF-8 硬解码变 U+FFFD 入 tape。

## 改动清单

| 文件 | 改动 |
|---|---|
| `orca/chart/_paths.py` | ✅ 已落地（对账见下）：`chart_endpoint`/port 文件读写；**待修**：tmp 名带 pid + replace 重试 |
| `orca/events/chart_ingestor.py` | ✅ 已落地：win32 TCP 分支；**待修**：D3 熔断跨链共享状态（现为死代码） |
| `orca/chart/_render.py` | ✅ 已落地：client `tcp://` 分支 + AF_UNIX 守卫 |
| `orca/exec/script.py` / `orca/exec/claude/executor.py` | ✅ 已落地：`_resolve_chart_sock_path` 走 `chart_endpoint`（两处对称镜像） |
| `orca/iface/in_session/_flock.py` | ✅ 已落地 shim；**待修**：`import errno` 缺失（NB 冲突 NameError）、`:44` 注释坏字符、`:12-13` docstring 同步改为 unlock no-op 语义（G1） |
| `orca/iface/in_session/cli.py` | fcntl→shim（8 处 + 常量）；`_write_orca_env` 的 `ORCA_CHART_SOCK` 改走 `chart_endpoint(sock_path)`（U1-A）；doctor 清理增 `.port`；main() UTF-8 |
| `orca/iface/in_session/daemon.py` | fcntl→shim |
| `orca/iface/in_session/chart_daemon.py` | fcntl→shim；退出清理增 `.port` |
| `orca/iface/in_session/_daemon_liveness.py` | socket/pidfile 探测 win32 分支（D5，经 `_winprocs`）；docstring「无 Orca 内部依赖」声明同步更新（新增 iface → chart._paths import，G2） |
| `orca/iface/_winprocs.py` | 新增：Windows 进程存活探（ctypes OpenProcess，stdlib-only，D5/D6 共用） |
| `orca/iface/cli/bg_runner.py` | fork_fn/setsid_fn 默认参数 None + 函数体内兜底；`pid_alive` win32 分支（D6） |
| `orca/iface/cli/commands.py` | main() UTF-8（D7） |
| `orca/exec/env.py` | `build_env_overlay` 注 `PYTHONUTF8=1`（D7） |
| `orca/iface/web/run_manager.py` | teardown 增 `.port` 清理 |
| `tests/` | 新增（见验收 4 的双环境分工） |

## 不改（明确排除）

- POSIX 运行时行为（D3 显式豁免除外）；`flock`/Unix socket/`os.fork` 的 POSIX 分支逐字保留。
- CC hooks python 化（独立项，opencode 验收不依赖）。
- TUI（`tars run` 前台）——本轮不验证不修。
- `skills/rx-sweep/scripts/ensure_chart_daemon.py`（AF_UNIX 硬编码——rx-sweep 非 Windows 验证面）。
- `iface/cli/web_registry.py` / `runtime/_project.py` 内联 msvcrt 锁分支（U5-B：三份锁实现归并列 follow-up，本轮不触碰其 POSIX 分支）。

## 工作树现状对账（Phase 2 coder 必读）

**已落地（6 文件，均未提交）**：`chart/_paths.py`、`chart/_render.py`、`events/chart_ingestor.py`、`exec/script.py`、`exec/claude/executor.py`、`iface/in_session/_flock.py`（新建）。

**已落地代码中的已知 bug（必修）**：
1. `_flock.py` 用 `errno.EACCES` 但未 `import errno` → win32 NB 冲突抛 NameError，逃逸 `except BlockingIOError`。
2. `chart_ingestor.make_crash_callback` 熔断死代码：重起链每代重建闭包/`crash_times`，永远数不到上限（D3 修订版要求跨链共享同一列表）。
3. `_flock.py:44` 注释含 U+FFFD 坏字符（`位置`）。
4. `_paths.py` port tmp 固定名 `.port.tmp`（D1 修订要求带 pid）+ `os.replace` 无重试。
5. 行尾 CRLF 统一为 LF（与仓库一致）。
6. `_flock.py:12-13` docstring 仍声明「LOCK_UN raise 与 flock 一致」——与 D4 修订的 no-op 语义相反，同步改（G1）。

**未落地（8 项）**：三文件 fcntl→shim 迁移、`_write_orca_env`/doctor `.port`、`_daemon_liveness` win32 分支、`_winprocs.py`、`bg_runner`、两 main UTF-8、`env.py` PYTHONUTF8、run_manager/chart_daemon `.port` 清理、全部 tests。

## 验收标准（用户拍板口径：不跑完整 E2E，证功能正确）

**验收 1（CLI 解锁 + 探活无杀伤）**：
- `tars list` / `orca list` / `tars ps` / `tars wait` / `tars logs` / `tars doctor` 在 orca-win 下退出可用、无 traceback。
- `tars list` 中文可读：管道捕获 bytes 按 UTF-8 解码无 mojibake（非 console 目测）。
- **探活无杀伤回归**：构造 running bg run（手工写 `~/.orca/runs/<id>.json`，status=running + `subprocess.Popen` 长活进程 pid——`--background` Unix-only 无法自然构造）；跑 `tars ps` 后断言该 pid 仍存活（`poll() is None` 前后不变）。

**验收 2（headless 图表闭环 + 熔断有效）**：
- direct run（script + `render_chart` 节点）→ `workflow_completed` + tape 含 chart 事件（TCP ingest 闭环）。
- serve.log 判据（修 off-by-one）：**crash warning（「重起中」）≤5 条**；若熔断触发则其后**恰 1 条**熔断 error、此后无新增。恒崩场景本身由单测覆盖（第 6 次 crash 后不再创建新 ingestor task）。
- script 中文 stdout 在 tape 中无 U+FFFD。

**验收 3（in-session + web，U1-A + U3-A 全 Windows 原生矩阵）**：

| 组件 | 环境 |
|---|---|
| opencode | Windows 原生 npm 1.17.20 |
| `orca` CLI（tars skill 编排调用） | orca-win conda env |
| `tars serve`（web） | orca-win conda env，同 ORCA_HOME |
| 测试 workflow | Windows 本地 scratch 项目（含 1 个 agent 节点 + 1 个 chart script 节点） |

- `opencode run` headless → tars skill 编排 `orca` CLI bootstrap/next 推进到 `workflow_completed`。
- web REST：`GET /api/runs` 可见该 run；events 全量；meta 终态正确。
- **chart 断言**（U1-A 前提）：tape 含 chart 事件且图表数据非空。

**验收 4（Linux 不回归，U4-B 双环境分工）**：
- WSL `.venv` pytest 子集（平台无关逻辑）：`tests/events/`（熔断「第 6 次不再重起」单测在此）、`tests/chart/`（TCP loopback + endpoint 分支——`chart_endpoint`/`_IS_WINDOWS` 运行时可 patch）、`tests/iface/web/`（run_manager chart 相关）、`tests/exec/test_script.py`。
- orca-win 真跑（导入期/ctypes/msvcrt 类）：`tests/iface/in_session/`（shim NB 冲突等）、`tests/iface/cli/test_bg_runner.py`（fork/setsid 导入安全 + pid_alive 无杀伤）。
- POSIX 假设测试 skipif 清点：`tests/chart/test_render.py:69`（fixture 内 AF_UNIX）为首例，全量清点加 `skipif`。
- WSL 全量受影响子集退出码 0（= Linux 无回归）。

## 失败路径 / fail-loud 策略

- `chart_endpoint` 空串 → 不注 env → script 端既有 fail loud（「缺 ORCA_CHART_SOCK」）+ Orca 侧 warning 记因。
- client 非 tcp 且平台无 AF_UNIX → RuntimeError 明确报平台不支持。
- 熔断 → `logger.error`（run 继续，chart 对该 run 不可用）。
- `_flock` NB 冲突 → `BlockingIOError`（调用方既有 busy 分支零改动）。
- 全部新失败路径 fail loud，无静默降级（U5 两处现状锁实现不动）。

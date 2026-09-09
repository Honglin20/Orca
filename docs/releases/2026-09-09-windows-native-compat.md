# Release Note：Windows 原生兼容（chart TCP 分支 + in-session 锁 shim + CLI 修复）

- **日期**：2026-09-09（sdd-loop 全流程：spec 评审环 2 轮 + 1 次 SPEC-LOOP 回退 + E2E 两轮）
- **Commits**：`4a00177`（主体）+ `083cfa0`（E2E 缺陷②修复）
- **Spec**：`docs/plans/2026-09-08-windows-native-compat.md`（D1~D7 + 双环境验收 + 挂账节）
- **实测依据**：2026-09-08 Windows 原生诊断（conda env `orca-win`，复现脚本 `.e2e_win/`）

## 修了什么

| 根因 | 修复 | 效果 |
|---|---|---|
| chart ingestor 调 `asyncio.start_unix_server`（win32 无）→ crash 无限重起**饿死事件循环**（90s 40 万次重起、run 卡死、前端推送全断） | D1/D2：win32 走 TCP `127.0.0.1:<临时端口>` + `<sock>.port` sidecar（tmp 名带 pid、`os.replace` 重试）；端点单一真相源 `chart_endpoint`；D3：crash 熔断（60s 窗口 >5 次停重起，per-run 链共享计数，全平台加固） | headless/web 路径 Windows 完全可用；任何平台恒崩不再拖死 run |
| `orca` CLI 模块级 `import fcntl` → 整体 ImportError | D4：`_flock.py` shim（msvcrt NB 归一 `BlockingIOError`、unlock no-op 对齐 flock），cli 8 处 + daemon 2 处 + chart_daemon 4 处迁移 | in-session 七命令（list/bootstrap/next/stop/...）Windows 可用 |
| `tars ps/wait/logs` 经 bg_runner `os.fork`/`os.setsid` 默认参数导入期崩 | D6：默认参数惰性化 + `pid_alive` win32 走 ctypes OpenProcess（新建共享 helper `orca/iface/_winprocs.py`） | 四命令解锁；**消灭「探活变杀进程」**（`os.kill(pid,0)` 在 Windows 语义是 TerminateProcess，`tars ps` 会杀掉被观测 run） |
| `_daemon_liveness` Windows 误入 macOS 分支 → 杀 sidechain daemon + respawn 风暴 | D5：socket 探走 port 文件 + AF_INET；pidfile 探 OpenProcess-only（U2-A） | 每 `next` 不再杀一遍 daemon |
| CLI 中文 GBK 乱码 | D7：两 CLI main 入口 UTF-8 reconfigure + 子进程注 `PYTHONUTF8=1` | 中文输出/中文 stdout 落 tape 无 U+FFFD |
| in-session `orca_env.sh` 写 Unix socket 原始路径 → Windows 推图节点必失败（U1-A） | `_write_orca_env` 走 `chart_endpoint` | in-session chart daemon 链路可用（E2E 实测 `render_chart` 0.06s 秒回 ack 落 tape） |
| E2E 发现：in-session run 的 web meta/events 500（`os.O_NOFOLLOW` win32 无） | `hasattr` 守卫拼接 flags（POSIX 位组合逐字不变） | attach 路径恢复 200 |

## POSIX 零回归证明（U4-B 双环境）

- WSL `.venv`：`tests/events/ tests/chart/ tests/exec/test_script.py` + `tests/iface/web/` 非 playwright 子集 → **661 passed**（后续修复轮再验 338 passed）；playwright 34 失败经 stash 基线证明 pre-existing（WSL chromium 环境债）。
- orca-win：`tests/iface/in_session/ + test_bg_runner.py` → 678 passed / 58 skipped（4 residual 经基线对照分类 pre-existing 或范围外）；attach 修复轮 47 passed。
- POSIX 行为变更仅 D3 熔断一处（spec 显式豁免，跨平台加固）。

## E2E 验收（重测轮全 PASS）

- **验收 1**：CLI 全解锁 + UTF-8 字节级验证 + 探活零杀伤（pid 前后存活不变）。
- **验收 2**：direct run 图表 TCP 闭环（chart data 落 tape 非空、crash warning=0、无熔断）。
- **验收 3**：in-session 手工等价 CLI 序列推进到 `workflow_completed` + busy 信封语义 + web REST 全可见（含 attach 200 回归）。
- 证据：`.e2e_win/e2e_phase3_log.md` + `.e2e_win/v/`（驱动与结构化输出）。

## 挂账（spec「已知产品缺陷挂账」节，5 项）

1. **in-session inline script 推 chart 自死锁**（跨平台既有，POSIX 复现：`next` 持 tape flock 临界区内联跑 script → daemon `_FlockSafeTape.append` 等同锁）——架构级专项，独立立项。
2. CC hooks Windows 静默死（`bash`/`python3` Store stub）。
3. 三份 msvcrt 锁实现归并（U5-B）。
4. pidfile 镜像名校验（U2）。
5. opencode→tars skill 自动编排全链补跑（deepseek 余额恢复后）。

## 已知限制

- Windows 上 ScriptNode 走 cmd.exe（`$VAR` 不展开）——workflow script 命令需绝对路径 + 文件参数（D1-D7 外的 product gap，另行立项）。
- Windows attach 路径 TOCTOU 守卫降级 best-effort（lstat + inode/dev 对比保留；spec 已声明）。
- `_e2e_playground` 仓库根 MSYS symlink 阻断 Windows python 跑全量 pytest（环境残留，orca-win 需 `--ignore`）。

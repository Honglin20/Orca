# Release Note —— in-session chart 守护 respawn（next 路径补被杀后拉起）

> 日期：2026-07-16 | 分支 `in-session-unified-backend` | 上接 [in-session chart 接入](./2026-07-16-in-session-chart.md)

## 背景 / 缺口

[in-session chart 接入](./2026-07-16-in-session-chart.md) 把 chart ingestor 守护进程放在 **bootstrap detach spawn 一次**：bootstrap CLI 退出后，守护脱离 controlling terminal 继续收 chart，直到 `_watch_terminal` 见终态事件或 6h TTL 自退。

**缺口**：守护**只在 bootstrap spawn 一次**。run 中途守护被杀（实测：`pkill opencode` 顺带 SIGTERM 了 detached 守护，10:22 kill、10:49 恢复 run）后，`orca next` 恢复推进 run 时**不 respawn** → 后续节点的子代理调 `render_chart` 连不上 socket、chart 全丢（实测一次 run 0 chart）。本补丁补这个缺口。

## 改动点（仅 `orca/iface/in_session/`，不碰 exec/chart 核心）

### `cli.py` —— `next` 路径补存活检查 + respawn

新增 helpers：

- **`_chart_daemon_alive(sock_path) -> bool`**：**确定性 socket connect 探测**（不靠进程名 grep / pidfile）。
  - `connect` 成功 → 有监听者，守护活（True）；连上即 close（`with` 管理），守护 handler 读 EOF（空行）走静默 debug 分支，**副作用 = 零**（不 emit、不写 tape）。
  - `ConnectionRefusedError`（stale socket，守护被 SIGKILL 残留）/ `FileNotFoundError`（无 socket 文件）/ 超时等 `OSError` → 一律视 dead（保守，触发 respawn；假阴性比假阳性安全）。
  - 选 connect 而非 pgrep/pidfile 的理由：Unix socket 的 connect 是**协议级**判定 —— 文件存在 ≠ 有人 listen（SIGKILL 不跑 finally unlink → stale 文件残留），connect 才区分「监听者在」与「孤儿 socket 文件」。

- **`_ensure_chart_daemon(run_id, tape_path)`**：probe；死 → 复用既有 `_spawn_chart_daemon` + `_wait_for_sock` 拉起。spawn 失败（Popen `OSError`）降级 warn（与「chart 是便利层、缺了不阻塞 workflow」自述一致），不以裸 traceback 崩 `next`。

- **`next` 调用点**：在 tape flock 临界区内、`_next_in_critical_section` 之后调 `_ensure_chart_daemon`，守卫 `result.node is not None and not (result.done or compliance_failed)`（与 env 文件写守卫同条件 —— 终态 / no-marker 无下一节点时不 respawn）。

### `cli.py` —— `_wait_for_sock` 从 `exists()` 加强为 connect 探

旧 `_wait_for_sock` poll `sock_path.exists()`。在 **respawn 路径**上，被 SIGKILL 的守护残留 stale socket 文件 → `exists()` 假阳性（误判 ready、实际无 listener）→ subagent 紧接着连会 `ConnectionRefused`。改为 `_chart_daemon_alive` connect 探：connect 成功才真有监听者。bootstrap 首启路径同样适用且更强（bind 前 connect FileNotFoundError 继续轮询；bind+listen 后 connect 成功返 True）。

### 并发安全（三层兜底，**不靠单一锁**）

- **next↔next 由 tape flock serialize**：并发 `next`（同 run）由 `LOCK_NB` busy-exit 互斥 → 同一时刻只有一个 `next` 进 respawn。
- **宿主时序串行**：bootstrap 完整跑完 spawn + wait 才返回 → 宿主派 subagent → 之后才 `next`。（注意：bootstrap spawn 在释 tape flock **之后**跑，tape flock 并不 serialize bootstrap↔next —— 真实跨阶段保护见下条。）
- **unlink + rebind 孤立老 listener**：即便时序被打破（bootstrap 守护冷启 >5s、`_wait_for_sock` 超时、next 又 respawn），`chart_ingestor` 入口 `if sock_path.exists(): unlink()` 后重 bind → 路径指向新守护，老守护监听的 inode 失去路径变无害孤儿，由终态/TTL 自退。

故无需额外的 respawn 专用锁（KISS/YAGNI）。绝不双写 tape（单一写路径 + `_FlockSafeTape` 跨进程互斥仍守）。

### socket 路径不变 → env 文件无需重写

`chart_sock_path(run_id)` 按 run_id 确定性派生，respawn 复用同一路径 → `orca_env.sh` 里 `ORCA_CHART_SOCK` 仍正确。`next` 已在 `_next_in_critical_section` 按下一节点身份重写 env 文件，本补丁不重复写；子代理零改动。

## 测试（+7；26 in-session chart/守护测试全过）

- `test_chart_daemon.py`：
  - `_chart_daemon_alive` 三态单测（无文件 / stale socket 用 raw socket bind+listen+close 忠实模拟 / 真监听者）。
  - `_ensure_chart_daemon` alive 早返不 spawn（防静默回归：双 spawn 时第二守护 unlink+rebind 孤立第一守护但 chart 仍落 tape → 既有 e2e 全过）。
  - `_ensure_chart_daemon` spawn OSError 降级 warn 不抛。
- `test_in_session_chart.py`：
  - `test_next_respawns_killed_chart_daemon`：bootstrap → SIGKILL 守护 → `orca next` → 断言 respawn（connect 探重活）+ chart 真落 tape（intent 级 4 步断言链）。
  - `test_next_does_not_respawn_when_terminal`：终态 next 不调 `_ensure_chart_daemon`（守卫负向）。

## 验证

- `pytest tests/iface/in_session/test_chart_daemon.py tests/iface/in_session/test_in_session_chart.py` —— 26 passed。
- `pytest tests/iface/in_session/` —— 158 passed，1 失败 = 既有 `test_orca_list_returns_inputs_schema_json`（`~/.orca/workflows` user-level 扫描根未隔离，非本任务引入，CURRENT.md 已登记）。
- code-reviewer 两轮（impl + coverage）0 🔴；🟡 全修：① 调用点守卫补 `result.node is not None`（no-marker 不再多余 respawn）；② spawn 失败降级 warn 不崩 next；③ docstring 修正「respawn 幂等」与「tape flock serialize bootstrap↔next」两处过度乐观声明，改述真实三层兜底机制；④ 补 alive 早返 + 终态不 respawn 两个负向测试。

## Commit

- `<本 commit，SHA 见 git log>`

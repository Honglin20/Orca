# 实施计划 —— in-session 路径接入 `orca.chart.render_chart`

> **日期**：2026-07-16
> **范围**：让 in-session skill 驱动路径（`orca <wf>` bootstrap + `orca next` 循环）下，节点子代理（由宿主 session 如 opencode 派发）调 `render_chart` 不再 raise，chart 作为 `custom(chart)` 事件落进该 run 的 tape。
> **依据**：[phase-13 SPEC](../specs/phase-13-render-chart.md)（chart 是事件 + env 身份路由 + per-run socket）· [in-session-entry-and-simplification v5](../specs/in-session-entry-and-simplification.md)（B 路径，主 session 驱动）· 用户已确认设计（run_id 中心、主 session 零额外传参）。
> **不动**：`orca/chart/_render.py`（env 契约不变）、`orca/exec/`（web/tars-run 路径零回归）、`orca/events/chart_ingestor.py` 协议逻辑。

---

## 1. 问题与设计

### 1.1 当前缺口
- web/tars-run 路径：orca 的 ClaudeExecutor spawn 子进程时注入 4 个 ORCA_* env + 起 per-run chart ingestor → 子进程 `render_chart` 工作。✓
- in-session 路径：节点子代理由**宿主 session**派发，不经 ClaudeExecutor → 子代理 env 无 `ORCA_*`，也没人起 ingestor → `render_chart` 在 `_render.py` env 检查处直接 raise。✗

### 1.2 已确认设计（A/B/C + 资源补充）
- **A. per-run chart ingestor 守护进程**（新组件，收敛在 in_session 层）：bootstrap 起 detached 守护进程 bind socket + 跑 ingestor；守护自退（tail tape 见终态事件或 6h TTL 兜底）；退出前 unlink socket。
- **B. run 级 env 文件**（`runs/<run_id>/orca_env.sh`，orca 写字面值，非 LLM 填）：含 5 个变量（4 个 chart var + `ORCA_AGENT_RESOURCES=<当前节点 resources_root>`，后者解决 folder-agent 资源定位缺口）。
- **C. 节点 prompt 加 source 行**：`_build_pointer` 拼的指针文本加一句 `source <abs env file path>`。
- **不改**：`_render.py` env 契约、`orca/exec/`、`chart_ingestor` 协议。

### 1.3 跨进程 tape 写协调（"单一写路径"铁律不破）
in-session 下两进程写同一 tape：
- chart 守护：script 推 chart → 守护 `bus.emit("custom", ...)`
- `orca next`/`stop` CLI：emit nc/rt/ns/workflow_completed/workflow_cancelled

时序上几乎不重叠（subagent 完成后才 next），但**正确性必须靠互斥**。方案：守护用 `_FlockSafeTape(Tape)` 子类，append 前 `fcntl.flock(LOCK_EX)` + 从 disk 刷新 `_last_seq`，与 `cli.py::_try_acquire_flock` 用同一个 `<tape>.lock` 文件。Web 路径继续用基类 `Tape`（零改动）。

### 1.4 session_id 语义
每次写 env 文件时新生成 `uuid.uuid4().hex`（每次 next = 一次 subagent dispatch，一个 uuid）。chart 事件 dedup 跨 session 替换（phase-9d §2.7），session_id 仅作分组，不影响显示。

---

## 2. 改动面

### 2.1 新增 `orca/iface/in_session/chart_daemon.py`
- `_FlockSafeTape(Tape)`：override `append` / `append_batch` —— 开 `<tape>.lock` fd → `fcntl.flock(LOCK_EX)`（阻塞）→ 从 disk 读 max seq 刷新 `_last_seq` → 调 `super().append` → 释放。
  - 构造用 `resume=False`（避免基类 `_truncate_trailing_partial` 在 flock 外写文件）；首次 append 由 `_read_max_seq_from_disk` 重置 `_last_seq`，覆盖构造时的扫描值。
  - `_read_max_seq_from_disk` 增量缓存（实例级 `_scan_offset` + `_scan_max_seq`）：首次 O(N)，之后仅读「上次 offset → EOF」的新字节，O(delta)。partial-line race 防护同 `_watch_terminal`：仅推进到最后一个 `\n` 之后。
- `_watch_terminal(tape_path, ttl_seconds, *, poll_interval)`：tail tape（按 size 增量读）→ 终态事件出现即返 `"terminal"`；TTL 超时返 `"ttl"`。**partial-line race 防护**：`last_size` 仅推进到 chunk 中最后一个 `\n` 之后，末尾 partial 字节下次重读（防 write(2) 中途被 poll 到导致漏检终态 → 守护 6h TTL 才退的泄漏窗口）。
- `_run_daemon(tape_path, run_id, sock_path, flock_path, ttl)`：构造 `_FlockSafeTape` + `EventBus` → `asyncio.create_task(chart_ingestor(sock_path, bus, run_id))` + `make_crash_callback` → 并行 `_watch_terminal` + signal event（`loop.add_signal_handler` for SIGTERM/SIGINT）→ FIRST_COMPLETED → finally cancel ingestor（其 finally unlink socket）+ bus.close + 兜底 unlink。
- `main()`：argv 解析（`--run-id`、`--tape`、`--ttl`、`--log-level`），算 `sock_path = chart_sock_path(run_id)`，`asyncio.run(_run_daemon(...))`，退出前兜底 `sock_path.unlink(missing_ok=True)`。**不裸 `sys.exit` / `raise SystemExit`**（SPEC §3.3 grep 守门：除 `iface/exit_codes.py` / `__main__.py` 外禁）；signal handling 用 `loop.add_signal_handler + asyncio.Event`（不 `raise SystemExit`）。

### 2.3 测试（新增）
- `tests/iface/in_session/test_chart_daemon.py`（19 tests）：
  - `_FlockSafeTape` 跨进程正确性：disk 刷新 / two-append refresh / 阻塞等 CLI / 空 tape / partial trailing / 增量缓存 / 增量缓存 partial trailing。
  - `_watch_terminal`：三种终态事件 / TTL 兜底 / 增量读 / tape 缺失 / **partial-write race**（显式分两半写终态行，验证守护最终捕获）。
  - `main()` 端到端 smoke：spawn 真子进程 → bind socket → SIGTERM → graceful 退出 + socket 清理。
- `tests/iface/in_session/test_in_session_chart.py`（5 tests）：
  - **bootstrap env 文件 + socket**（folder-agent 含 `ORCA_AGENT_RESOURCES`）
  - **inline-prompt 节点 `unset ORCA_AGENT_RESOURCES`**
  - **chart 落 tape**（核心验收 1：bootstrap + 模拟 subagent source env + render_chart → tape `custom(chart)`）
  - **并行 run 不串台**（核心验收 3：两 run_id → 两 socket → 两 tape 各只含自己 chart）
  - **folder-agent + `$ORCA_AGENT_RESOURCES`**（核心验收 5：subagent source env 后跑 `$ORCA_AGENT_RESOURCES/scripts/demo.py` 推 chart）
  - `_wait_sock_gone` helper：超时 `pytest.fail`（防守护自退回归被静默 unlink 掩盖）。

### 2.2 改 `orca/iface/in_session/cli.py`
- `_env_file_path(tape_path, run_id)`：返 `<rundir>/<run_id>/orca_env.sh`。
- `_write_orca_env(env_path, run_id, node, session_id, sock_path, resources_root)`：原子写（tmp + `os.replace`）。5 行：`ORCA_RUN_ID/NODE/SESSION_ID/CHART_SOCK` + `ORCA_AGENT_RESOURCES`（resources_root 非空）或 `unset ORCA_AGENT_RESOURCES`（inline-prompt 节点，清潜在 stale）。值用 `shlex.quote`。
- `_spawn_chart_daemon(run_id, tape_path)`：`subprocess.Popen([sys.executable, "-m", "orca.iface.in_session.chart_daemon", "--run-id", run_id, "--tape", str(tape_path)], stdin=DEVNULL, stdout=log_fd, stderr=log_fd, start_new_session=True, close_fds=True)`。log 落 `runs/<run_id>/chart_daemon.log`。
- `_wait_for_sock(sock_path, timeout=3.0)`：poll `sock_path.exists()`，3s 内出现即返；超时 log warning 不 fail bootstrap。
- bootstrap：marker 写完后 spawn 守护 + 写首版 env 文件（entry 节点 + uuid）+ 等待 socket。
- next：在 `_next_in_critical_section` 内 `apply_step_result` 之后，重写 env 文件（新节点 + 新 uuid + 该节点 resources_root）。`rundir`/`sock_path` 经入参传进。
- `_build_pointer` / `_reply_prompt`：加可选 `env_file: Path | None`，非 None 时指针文本加一句「运行任何脚本前先 `source <abs>`（注入 ORCA_* 身份 + agent 资源路径）」。

---

## 3. 不做的事（边界）

- ❌ 改 `_render.py` 的 env 契约（4 var 仍是 client 必须）。
- ❌ 改 `chart_ingestor` 协议逻辑（OCP：守护用 `Tape` 子类注入跨进程语义，不改 ingestor）。
- ❌ 改 `orca/exec/`（web/tars-run 路径零回归）。
- ❌ render_chart 自重试（SPEC §7.5 显式禁止 transport 层重试）。
- ❌ resume 模式（SPEC §3.1 YAGNI；in-session resume 不存在 —— bootstrap 是新 run）。
- ❌ Windows 支持（项目已 POSIX-only：fcntl.flock 等同前提）。

---

## 4. 验收

1. in-session 路径下节点子代理调 `render_chart` 不 raise，chart 落 tape 为 `custom(chart)`。
2. web/tars-run 路径既有 chart 测试 0 回归（tests/e2e_phase13 + tests/events/test_chart_ingestor）。
3. 两并行 in-session run 的 chart 不串台（run_id 键控不同 socket + 不同守护）。
4. 守护在 run 终态自退 + 清 socket；TTL 兜底；无泄漏。
5. folder-agent 在 in-session 下能读 `$ORCA_AGENT_RESOURCES`。
6. code-reviewer 两轮 0 🔴；既有测试 0 回归。

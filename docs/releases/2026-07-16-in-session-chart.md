# Release Note —— in-session 路径接入 `orca.chart.render_chart`

> 日期：2026-07-16 | 分支 `in-session-unified-backend` | 计划 [`docs/plans/2026-07-16-in-session-chart.md`](../plans/2026-07-16-in-session-chart.md)

## 背景 / 决策

Orca 有两条执行路径。web/tars-run 路径（`tars run`）下，`ClaudeExecutor` spawn 子进程时一次性注入 4 个 `ORCA_*` env + 起 per-run chart ingestor（同进程 asyncio task）→ script 子进程经 env 继承把身份传到 `render_chart`，chart 经 socket → ingestor → bus → tape（`custom(chart)` 事件）。**已工作 ✓**。

in-session 路径（`orca <wf>` bootstrap + `orca next` 循环，由主 session 如 opencode 用 TARS skill 驱动）下，节点子代理由**宿主 session 派发**，不经 ClaudeExecutor → 子代理 env 无 `ORCA_*`、也没人起 ingestor → `render_chart` 在 `_render.py` 的 env 检查处直接 raise。**本任务补这个缺口 ✗→✓**。

用户已确认设计（run_id 中心、主 session 零额外传参、无并行串台）：
- **A. per-run chart ingestor 守护进程**（新组件，收敛在 in_session 层，detach 脱离 bootstrap CLI 存活）
- **B. run 级 env 文件**（`runs/<run_id>/orca_env.sh`，orca 按 run_id 派生字面值，子代理 source 一行）
- **C. 节点 prompt 指针加 source 行**（子代理照抄字面 source）

**关键补充**（协调人 2026-07-16 指出）：env 文件除 4 个 chart var 外，**必须再加 `ORCA_AGENT_RESOURCES=<当前节点 resources_root>`**。原因：folder-agent（如 NAS 的 `pytorch-model-optimizer`）的 `agent.md` body 引用 `$ORCA_AGENT_RESOURCES/SKILL.md` / `$ORCA_AGENT_RESOURCES/scripts/...`；web 路径由 executor 注入此 env，in-session 路径没人注入 → skill agent 卡死（已实测 model_optimizer 卡 13 分钟无进展）。env 文件同时解决 chart 路由 + 资源定位两个缺口。

## 改动点

### 1. 新模块 `orca/iface/in_session/chart_daemon.py`

**`_FlockSafeTape(Tape)`**（OCP：子类扩展，不改基类 / chart_ingestor）：
- override `append` / `append_batch`：每次写入前 `fcntl.flock(LOCK_EX)` 阻塞抢 `<tape>.lock`（与 `cli._try_acquire_flock` 同锁文件、同路径，跨进程互斥），并从 disk 刷新 `_last_seq`。
- `_read_max_seq_from_disk` 增量缓存：实例级 `_scan_offset` + `_scan_max_seq`，首次 O(N) 后续 O(delta)；partial-line race 防护（仅推进到最后一个 `\n` 之后）。
- 构造用 `resume=False`（避免基类 `_truncate_trailing_partial` 在 flock 外写文件违反跨进程契约）。

**`_watch_terminal`**：tail tape（按 size 增量读）监听终态事件 `workflow_completed/failed/cancelled`，返 `"terminal"`；TTL 兜底返 `"ttl"`。**partial-line race 防护**：`last_size` 仅推进到 chunk 中最后一个 `\n` 之后，末尾 partial 字节下次重读（防 write(2) 中途被 poll 到漏检终态 → 守护 6h TTL 才退的泄漏窗口）。

**`_run_daemon`**：构造 `_FlockSafeTape` + `EventBus` → `chart_ingestor(sock_path, bus, run_id)` 复用（零改动）+ `make_crash_callback` → `asyncio.wait({watcher, signal_waiter}, FIRST_COMPLETED)` → finally cancel ingestor（其 finally `unlink` socket）+ bus.close + 兜底 unlink。

**`main()`**：argv 解析（`--run-id` / `--tape` / `--ttl` / `--log-level`）。**不裸 `sys.exit` / `raise SystemExit`**（SPEC §3.3 grep 守门）；signal handling 用 `loop.add_signal_handler + asyncio.Event`（graceful 退出，不 `raise SystemExit`）。

### 2. `orca/iface/in_session/cli.py` 修改

**新增 helpers**：
- `_env_file_path(tape_path, run_id)`：返 `<rundir>/<run_id>/orca_env.sh`。
- `_run_dir_for(tape_path, run_id)`：返 `<rundir>/<run_id>/`。
- `_write_orca_env(env_path, *, run_id, node, session_id, sock_path, resources_root)`：原子写（tmp + `os.replace`）5 行 env 文件。folder-agent → `export ORCA_AGENT_RESOURCES=<abs>`；inline-prompt 节点 → `unset ORCA_AGENT_RESOURCES`（清潜在 stale）。
- `_spawn_chart_daemon(run_id, tape_path)`：`subprocess.Popen([sys.executable, "-m", "orca.iface.in_session.chart_daemon", ...], start_new_session=True, ...)` 脱离 bootstrap，stdout/stderr 落 `runs/<run_id>/chart_daemon.log`。
- `_wait_for_sock(sock_path, timeout=3.0)`：bootstrap 等 socket 就绪（poll exists），超时仅 warn 不 fail（host 派 subagent 还要数十秒，daemon 多半补上）。

**bootstrap 集成**：marker 写完后 → 写首版 env 文件（entry 节点 + `uuid.uuid4().hex`）→ spawn 守护 → 等 socket。
**next 集成**：`_next_in_critical_section` 内 `apply_step_result` 之后，按下一节点身份重写 env 文件（新 node + 新 uuid + 该节点 resources_root）。终态时不写（无下一节点）。
**指针文本**：`_build_pointer` / `_reply_prompt` 加可选 `env_file`，非 None 时追加「运行任何脚本前先 `source <abs>`（注入 ORCA_* 身份 + agent 资源路径）」一行。

### 3. 测试（新增）

**`tests/iface/in_session/test_chart_daemon.py`（19 tests）**：
- `_FlockSafeTape` 跨进程正确性：disk max 续 seq / 两 append 间 disk 增长刷新 / 阻塞等 CLI flock / 空 tape / partial trailing / 增量缓存新写 / 增量缓存 partial trailing。
- `_watch_terminal`：三种终态事件 / TTL 兜底 / 增量读 / tape 缺失 / **partial-write race**（显式分两半写终态行，验证守护最终捕获）。
- `main()` 端到端 smoke：spawn 真子进程 → bind socket → SIGTERM → graceful 退出 + socket 清理。

**`tests/iface/in_session/test_in_session_chart.py`（5 tests）**：
- bootstrap env 文件 + socket（folder-agent 含 `ORCA_AGENT_RESOURCES`）。
- inline-prompt 节点 `unset ORCA_AGENT_RESOURCES`。
- **chart 落 tape**（核心验收 1：bootstrap + 模拟 subagent source env + render_chart → tape `custom(chart)` + node/session_id 正确）。
- **并行 run 不串台**（核心验收 3：两 run_id → 两 socket → 两 tape 各只含自己 chart）。
- **folder-agent + `$ORCA_AGENT_RESOURCES`**（核心验收 5：subagent source env 后跑 `$ORCA_AGENT_RESOURCES/scripts/demo.py` 推 chart）。
- `_wait_sock_gone` helper：超时 `pytest.fail` loud（防守护自退回归被静默 unlink 掩盖）。

测试模型：用 `subprocess.run(['bash', '-c', 'source <env>; python <script>'])` 模拟宿主 session 派的子代理侧（fresh shell + source env + 跑 script）。

## 不做的事（边界）

- ❌ 改 `_render.py` env 契约（4 var 仍是 client 必须）。
- ❌ 改 `chart_ingestor` 协议逻辑（OCP：守护用 `Tape` 子类注入跨进程语义，不改 ingestor）。
- ❌ 改 `orca/exec/`（web/tars-run 路径零回归）。
- ❌ render_chart 自重试（SPEC §7.5 显式禁止 transport 层重试）。
- ❌ resume 模式（SPEC §3.1 YAGNI；in-session resume 不存在 —— bootstrap 是新 run）。
- ❌ Windows 支持（项目已 POSIX-only：fcntl.flock 等同前提）。

## 偏离计划

无。计划 [`docs/plans/2026-07-16-in-session-chart.md`](../plans/2026-07-16-in-session-chart.md) 与实现 1:1 对齐。两处在 review 反馈后**收敛加强**（非偏离）：
1. `_watch_terminal` 增量读 `last_size` 推进：原计划「直接 `last_size = cur_size`」改为「仅推进到最后 `\n` 之后」（code-reviewer R1 🔴 发现 partial-line race，会致守护 6h 泄漏窗口）。
2. `_read_max_seq_from_disk` 从 O(N) 全扫升级为增量缓存（code-reviewer R1 🟡 指出 chart-heavy 长跑场景下 flock 持有时间随 tape 增长）。

## 验收

| # | 标准 | 结果 |
|---|------|------|
| 1 | in-session 路径下 `render_chart` 不 raise，chart 落 tape 为 `custom(chart)` | ✅ `test_in_session_chart_lands_in_tape` 验证 |
| 2 | web/tars-run 路径既有 chart 测试 0 回归 | ✅ tests/e2e_phase13/ + tests/chart/ + tests/events/test_chart_ingestor.py 全过（2 baseline 失败为本任务前环境问题：`python3` 非 orca python） |
| 3 | 并行两 run chart 不串台 | ✅ `test_parallel_in_session_runs_no_cross_talk` 验证 |
| 4 | 守护在终态自退 + 清 socket；TTL 兜底；无泄漏 | ✅ `_watch_terminal` 单测 + `main()` smoke 验证（SIGTERM 路径 + 终态事件路径） |
| 5 | folder-agent 在 in-session 下能读 `$ORCA_AGENT_RESOURCES` | ✅ `test_folder_agent_resources_accessible_via_env` 验证 |
| 6 | code-reviewer 两轮 0 🔴；既有测试 0 回归 | ✅ R1：1 🔴 + 5 🟡 → 全修；R2：0 🔴 + 0 🟡（仅 3 🟢 已修 2）。710 in-session+chart+events+exec 测试 0 新回归 |

## 既有测试 debt（本任务**未引入**，记录在案）

- `tests/e2e_phase13/test_e2e_1_basic_chart.py` + `test_e2e_2_multi_run_parallel.py`：YAML 硬编码 `python3`，但本机 `python3` 未装 orca → script 调 `render_chart` import 失败。本机用 `/home/mozzie/miniconda3/envs/orca/bin/python` 跑同 wf 验证 chart 正常落 tape。CI 环境（orca on PATH）应无此问题。
- `orca/iface/in_session/daemon.py:105` `sys.exit(128 + signum)`：违反 SPEC §3.3 grep 守门（在 `iface/exit_codes.py` / `__main__.py` 之外）。**预先存在**（baseline 测试已 fail），非本任务引入；本任务的 `chart_daemon.py` 已用 `loop.add_signal_handler` 避免同款违规。
- 多个 `tests/iface/mcp/test_*` 失败：环境缺 `uv` 二进制（FileNotFoundError），与本任务无关。

## Commit

`<本 commit，SHA 见 git log>` —— 待 commit 填入。

## 相关文件（绝对路径）

- `/mnt/d/Projects/Orca/orca/iface/in_session/chart_daemon.py`（新）
- `/mnt/d/Projects/Orca/orca/iface/in_session/cli.py`（修改：bootstrap spawn 守护 + 写 env；next 重写 env；指针 source 行；5 个 helper）
- `/mnt/d/Projects/Orca/tests/iface/in_session/test_chart_daemon.py`（新）
- `/mnt/d/Projects/Orca/tests/iface/in_session/test_in_session_chart.py`（新）
- `/mnt/d/Projects/Orca/docs/plans/2026-07-16-in-session-chart.md`（计划）
- 参考（未改）：`/mnt/d/Projects/Orca/orca/chart/_render.py`（env 契约源）、`/mnt/d/Projects/Orca/orca/events/chart_ingestor.py`（协议不变，复用）、`/mnt/d/Projects/Orca/orca/events/tape.py`（基类契约）、`/mnt/d/Projects/Orca/orca/chart/_paths.py`（sock 路径派生）

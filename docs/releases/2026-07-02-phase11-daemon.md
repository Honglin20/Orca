# phase 11 P3.2 —— daemon `--background` 模式 + ps/logs/wait

> 日期：2026-07-02
> SPEC：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §8 + §10.2 item10/item11 + §11.9
> 计划：[`docs/plans/2026-07-01-phase11-cli-enrichment.md`](../plans/2026-07-01-phase11-cli-enrichment.md) P3
> baseline：904 passed → **956 passed**（+52：36 bg_runner + 13 background CLI + 3 headless），0 回归

## 背景

长跑 workflow 占住终端是 CLI 编排的常见痛点。phase 11 P3.2 补齐 daemon 模式：`orca run
<yaml> --background` fork 出脱离终端的子进程跑 workflow，父进程立即返回 run_id + pid；
配合 `orca ps` / `orca logs <id>` / `orca wait <id>` 三件套管理。SPEC §8.1 用例：

```bash
$ orca run examples/long.yaml --background
Started background run: mxint_analysis-20260702-192804-41a0a8
PID: 12345, logs: ~/.orca/runs/mxint_analysis-20260702-192804-41a0a8/log

$ orca ps                       # 列活跃 run（dead pid 标 crashed）
$ orca logs <id> [-f] [-n 50]   # tail 日志（-f 持续 follow）
$ orca wait <id> [--timeout N]  # 阻塞到终态（completed→0 / failed→1 / crashed→1 / not-found→2）
```

## 决策（D2 locked）

**实现 `--background` + `ps` + `logs` + `wait` ONLY。`attach` DESCOPED**（SPEC §10.2 item11）。

理由：只读 attach 的价值低于 `tail -f` tape + `orca logs <id> --follow` 已能覆盖「看 + 滚日志」
的观察需求；读写 attach（答 gate / 触发 interrupt）需要 daemon 暴露 UDS 控制通道，复杂度高，
phase 11 不做。用户要看实时日志，用 `orca logs <id> -f`（语义等同 `tail -f` tape 的日志侧）。

## 实现要点

### 1. `orca/iface/cli/bg_runner.py`（NEW）—— daemonize + metadata

- **`daemonize(yaml, run_id, extra_argv, *, fork_fn, setsid_fn, execv_fn, redirect_stdio_fn, time_fn)`**：
  fork detached child；parent 返回 pid，child 走 setsid → redirect fd 0/1/2 → set env → execv。
  **5 个副作用原语全可注入**（seam），单测 mock 不真 fork / 不真 detach / 不真 execv（CI 不留孤儿）。
- **`BgRunMeta`**（frozen dataclass）：`{run_id, pid, yaml_path, started_at, log_path, tape_path,
  status, finished_at}`。写 `~/.orca/runs/<run_id>.json`（原子：tmp + `os.replace`）。
- **`effective_status(meta)`**（fail loud，SPEC §10.2 item11 硬约束）：`status=running` 且
  `pid_alive(pid)=False` → 标 `crashed`（child 崩未及更新 metadata 时，`ps` 必须显式标出，
  不能静默显示 running 误导用户）。
- **`mark_terminal_status`**：child 跑完调，戳 `finished_at` + 改 `status`（completed/failed）。
- **`wait_for_terminal`**：轮询 `effective_status` 到 terminal（completed/failed/crashed）或超时。

### 2. detached child 走 **headless**（非 TUI）—— 关键架构裁定（SPEC §11.9）

detached child 经 `os.setsid()` 脱离 controlling terminal，**无 TTY**。Textual TUI 在无 TTY
下会 hang / 崩（init escape 写不出、read stdin 阻塞）。spike 实证：child 启动 TUI 后写一堆
terminal escape 到日志卡死，`ps` 标 crashed。

**裁定**：detached child 检测 `ORCA_BG_RUN_ID` env 存在 → `_run_workflow` 跳过 TUI 分支，调
`_run_workflow_headless`（直接 `Orchestrator.run()` + `asyncio.run`，与 resume 的
`run_from_state` 同 headless pattern）。Tape / metadata 一致性不变（同一 orchestrator，同一
tape 路径 `runs/<run_id>.jsonl`）。

### 3. 确定性 run_id 三处一致

父进程 `gen_run_id(wf.name)` gen 一次 run_id，经 `ORCA_BG_RUN_ID` env 传子进程；子进程（headless
路径）读 env 复用，**不重新 gen** —— 保 metadata / tape 文件名 / Orchestrator.run_id 三者一致
（`ps` / `logs` / `wait` / `resume` 据此定位）。

### 4. `orca/iface/cli/commands.py` —— flag + 3 子命令

- `run --background` / `-b`：校验 yaml → gen run_id → daemonize → 打印 run_id/pid/logs → exit 0。
  不走 `_run_workflow`（那个起 TUI 阻塞）。非 `--background` 默认 False，foreground 行为不变（向后兼容）。
- `ps`：扫 `~/.orca/runs/*.json`，表头 + 行（RUN_ID/WORKFLOW/STATUS/ELAPSED/PID）。terminal status
  的 ELAPSED 用 `finished_at - started_at`（固定，不随墙钟增长）。
- `logs <id> [-f] [-n N]`：tail metadata.log_path；`-f` 持续 follow（`tail -f` 语义）。
- `wait <id> [--timeout N]`：阻塞到 terminal；exit 0(completed)/1(failed,crashed)/2(not-found)/3(timeout)。

## code-review 闭环（全部修复）

dispatch `code-reviewer`，1 🔴 + 6 🟡 + 2 🟢，全部修复：

| 等级 | 问题 | 修复 |
|---|---|---|
| 🔴 | headless `except Exception` 漏 BaseException（SIGTERM 不更新 metadata） | 扩 `except BaseException`，标 failed 后 re-raise（不吞 KeyboardInterrupt） |
| 🟡 | `_with_pid` / `with_status` 手写 replace | 删 `_with_pid`，`with_status` 改 `dataclasses.replace` |
| 🟡 | `_resolve_tape_path` 与 `default_tape_path` 重复 | `_resolve_tape_path` 调 `default_tape_path`（单一真相源） |
| 🟡 | `build_child_argv` 静默 fallback | fallback 到 `python -m` 时记 warning（fail-loud 信号） |
| 🟡 | `_run_workflow_headless` 零单测 | +3 测（config error / runtime exception / completed 三路径各验证 `mark_terminal_status`） |
| 🟡 | SPEC §8.2 「foreground run」与 headless 偏离 | SPEC §11.9 补偏离记录（本 release note 即其展开） |
| 🟡 | `ps` ELAPSED 对 terminal status 仍增长 | metadata 加 `finished_at` 字段；terminal 用 `finished_at - started_at` |
| 🟢 | `test_daemonize_child_*` 硬锁 `calls[0]=="setsid"` | 改语义顺序断言（setsid_idx < redirect_idx < execv_idx） |
| 🟢 | `wait_for_terminal` meta=None 语义模糊（not-found 归 crashed） | 保留（通用库函数留口，CLI 调用方已提前拒 not-found） |

## 测试

- **`tests/iface/cli/test_bg_runner.py`（36 测）**：metadata roundtrip、路径解析、pid_alive /
  effective_status（fail-loud 硬约束显式注释 + 断言）、daemonize parent/child seam（mock fork，
  spy setsid/redirect/execv，验证副作用顺序 + env 传播）、wait_for_terminal 四路径、OrcaApp
  env 复用 run_id。
- **`tests/iface/cli/test_commands.py::TestBackgroundRun`（13 测）**：`--background` spy daemonize
  + exit 0 不阻塞、yaml 缺失 exit 2、参数透传；ps 列表 / 空提示 / dead pid 标 crashed；
  logs 读 / not-found exit 2 / 日志缺失 exit 2；wait completed exit 0 / failed exit 1 / crashed
  exit 1 / not-found exit 2。
- **`tests/iface/cli/test_commands.py::TestRunWorkflowHeadless`（3 测）**：headless 三路径
  （config error / runtime exception / completed）各验证 `mark_terminal_status` 正确调用。
- **`tests/iface/cli/test_bg_integration.py`（1 测，`@pytest.mark.integration` CI 跳过）**：
  真跑 `orca run examples/demo_linear.yaml --background` → 轮询 `ps` 到 completed → `wait` exit 0
  → tape 落盘验证。本地跑通（2s，全 script workflow，零 token，无需 claude/API key）。

## 手动 smoke 验证

```bash
$ uv run orca run examples/demo_linear.yaml --background
Started background run: demo_linear-20260702-044014-92d67d
PID: 28192, logs: /Users/mozzie/.orca/runs/demo_linear-20260702-044014-92d67d/log

$ uv run orca ps
RUN_ID                              WORKFLOW       STATUS       ELAPSED  PID
demo_linear-20260702-044014-92d67d  demo_linear    completed    1s       28192

$ uv run orca wait demo_linear-20260702-044014-92d67d
run demo_linear-20260702-044014-92d67d 终态：completed

$ ls runs/demo_linear-20260702-044014-92d67d.jsonl  # tape 落标准位置，resume 可接
runs/demo_linear-20260702-044014-92d67d.jsonl
```

## commit

- `feat(cli): P3.2 daemon --background + ps/logs/wait` —— bg_runner.py + commands + app.py + 测试 + SPEC §11.9

## 偏离 SPEC

见 SPEC §11.9（headless 决策）。其余逐字按 SPEC §8.2 / §10.2 item10-11 实现。

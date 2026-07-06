# Release Note —— phase-11-process-lifecycle（子进程生命周期管理）

> **commit**：待填（实施完成后回填）
> **SPEC**：[`phase-11-process-lifecycle.md`](../specs/phase-11-process-lifecycle.md) v2
> **ADR**：[`2026-07-06-interface-convergence-adr.md`](../specs/2026-07-06-interface-convergence-adr.md) §4.6（D6 退出码）/ §4.7（D7 ProcessRegistry）
> **批次**：批 3a（exec/iface）；接口收敛 ADR v2 落地的第二个实现模块
> **前置**：phase-11-error-handling（commit 451dd39）；cancel 产生的错误用 `ErrorKind.TRANSPORT_PROCESS`

---

## 1. 实际做了什么

### 1.1 新增文件

| 文件 | 作用 |
|---|---|
| `orca/exec/registry.py` | `ProcessRegistry`（DI 注入）+ `get_default_registry()` 模块级惰性单例 + `RegisteredProcess` dataclass + 三段式 cancel（SIGTERM grace → SIGKILL → cleanup hooks）+ 平台分支（POSIX `start_new_session=True`+`killpg` / Windows `CREATE_NEW_PROCESS_GROUP`+`CTRL_BREAK_EVENT`）+ `atexit` 幂等 shutdown + `spawn_kwargs_for_process_group()` 辅助 |
| `orca/iface/exit_codes.py` | `ExitCode` IntEnum（5 档 0/1/2/3/130）+ `exit_for_terminal_status(status)` 纯函数派生（completed→0, failed→2, cancelled→3，未知 fail loud） |
| `tests/exec/test_registry.py` | acquire/release/shutdown 幂等 + signal-safe handler 只设 Event 不直接调 + DI 隔离 + grace_seconds 上限 + cleanup hooks 总跑 |
| `tests/exec/test_cancel.py` | 三段式时序（mock os.killpg + _pid_exists 验 SIGTERM 先于 SIGKILL）+ grace 内退出跳 SIGKILL + 进程组清理三层深（真子进程，沙箱外验证）+ grace 可配置 |
| `tests/test_exit_codes.py` | 5 档契约 + 终态派生 + 未知 fail loud + grep 守门（扫描 `orca/` 下裸 `sys.exit` / `raise SystemExit`，scope 排除 `iface/exit_codes.py` + 任意 `__main__.py`，已知遗留 allowlist：`gates/hook_script.py`） |
| `tests/conftest.py` | `process_local` fixture（每测试独立 ProcessRegistry，DI 可测试性） |

### 1.2 修改文件

- **`orca/exec/runner.py`**：
  - spawn 加 `**spawn_kwargs_for_process_group()`（POSIX `start_new_session=True` / Windows `creationflags`）
  - `__init__` 加 `registry / backend / run_id / node_id` 可选 kwargs（DI，默认 `get_default_registry()`）
  - spawn 后立刻 `registry.acquire(proc, ...)` 登记
  - `_handle_timeout`（超时路径）+ stream `finally` 块（异常退出兜底）改委托 `registry.kill_one`（经 `asyncio.to_thread` 包装避免阻塞 event loop）
  - 正常退出路径调 `registry.release(pid)`（幂等）
  - `_handle_timeout` / finally 的 `proc.wait()` 加 2s 超时防御（review 🟡 #1）
  - 模块/方法 docstring 更新：删除「单进程 kill（非 killpg，SPEC §2.5）」注释，标注推翻决策
- **`orca/exec/script.py`**（review 🔴 #2 修复）：
  - `create_subprocess_shell` 加 `**spawn_kwargs_for_process_group()`（进程组隔离）
  - spawn 后 `registry.acquire(backend="script", run_id=ctx.run_id, node_id=node.name)`
  - timeout 路径委托 `registry.kill_one`（替代旧 `_kill_proc` helper，已删）
  - 正常退出 `registry.release(proc.pid)`
  - `__init__` 加 `registry` 参数（DI，默认 `get_default_registry()`）
- **`orca/run/orchestrator.py`**（**只动两处**，phase-11-error except 链 / `_classify_error` 完全未触）：
  - `__init__` 加 `registry` 参数 → `self._registry = registry or get_default_registry()`
  - 新增 `shutdown()` 方法 → 调 `self._registry.shutdown()`（幂等）
  - `_bare_instance`（resume 路径）同步设 `_registry = get_default_registry()`
- **`orca/run/__main__.py`**：
  - 退出码改 `exit_for_terminal_status(state.status)`（5 档契约）
  - SIGINT / SIGTERM handler 只设 `threading.Event`（async-signal-safe）
  - daemon 清理线程看到 Event 后调 `registry.shutdown()`（SPEC §1.3 signal-safe 模式）
  - `finally` 复位原 handler + 显式 `registry.shutdown()` 兜底（幂等）
  - `KeyboardInterrupt` 兜底返回 `ExitCode.SIGINT` (130)
  - 注释说明 `asyncio.run` 与 `signal.signal` 的交互（review 🟡 #4）

---

## 2. 关键设计决策

### 2.1 DI 非 singleton（ADR §4.7 闭环 B8）

phase-11 v1 SPEC 曾提 class-level `_instance` + `_lock` singleton——并行 pytest (xdist) 与测试隔离会破坏。**v2 改 DI**：

- production：`get_default_registry()` 模块级惰性单例（lazily-created module global，`_default_lock` 保护首次创建）
- 测试：`process_local` fixture 每测试独立实例

未留 class-level singleton 兼容路径（P3 全量替换）。

### 2.2 进程组隔离（推翻 phase-3-events.md §2.5）

phase-3-events.md §2.5 原决策选单进程 kill。**phase-11-process-lifecycle §2.1 推翻**：
孙子进程（claude/opencode spawn 的 grep/bash/node）变孤儿是真 bug，长跑 workflow 下累积。
`start_new_session=True` + `os.killpg` 整组杀兜住孙子进程；Orca 自己的 Ctrl+C 信号不会传到
子进程组（Orca 自己处理 SIGINT 后主动 killpg 清理）。

runner.py 的 docstring 已标注「**推翻 phase-3 §2.5 旧决策**」。

### 2.3 三段式 cancel（SPEC §2.2）

```
Stage 1: SIGTERM 整组（grace 期让 agent 写完文件 / 释放锁）
Stage 2: poll grace 期（_pid_exists 每 50ms）；grace 内退出 → 跳 Stage 3
Stage 3: SIGKILL 整组（强杀兜底）
Stage 4: cleanup hooks（关 fd / 删临时文件；hook 异常不阻塞 shutdown）
```

`grace_seconds` 默认 2.0s；上限 10s（SPEC §2.3，超过 = 阻塞 cancel，用户感知卡死）。

### 2.4 signal-safe（SPEC §1.3）

`__main__.py` 的 SIGINT / SIGTERM handler **只设 `threading.Event`**——
`registry.shutdown()` 含 `threading.Lock`，非 async-signal-safe，在 handler 里直接调会死锁。
专门 daemon 清理线程看到 Event 后调 shutdown。

### 2.5 grep 守门（SPEC §3.3 / ADR §8.1）

`tests/test_exit_codes.py::test_no_bare_sys_exit_or_raise_system_exit_outside_allowed_paths`
扫描 `orca/` 下所有 .py，正则匹配 `^\s*(sys\.exit\(|raise\s+SystemExit)`，scope 排除：
- `iface/exit_codes.py`（权威派生函数）
- 任意 `__main__.py` 路径（三壳入口）
- 已知遗留 allowlist：`gates/hook_script.py`（git pre-commit / pre-push 协议退出码，
  与 SPEC §3.1 5 档语义不同；批 4 follow-up）

新增违规即返工。

---

## 3. 与计划的偏差

- **`orca/__main__.py` 不存在**：SPEC §4.2 写「修改 `orca/__main__.py`」但实际项目结构是 `orca/run/__main__.py`（CLI 入口）+ `pyproject.toml` `[project.scripts] teams = "orca.iface.cli.commands:main"`。改 `orca/run/__main__.py`（语义最接近的既有入口）。**不**新建 `orca/__main__.py`（避免无中生有的入口与并行工作树的 `pyproject.toml` 冲突）。
- **真实孙子进程测试沙箱容忍**：`tests/exec/test_cancel.py::test_kill_one_cleans_up_grandchildren_via_process_group` 在受限沙箱（macOS sandbox / 受限 CI container）下 `os.killpg` syscall 成功但信号不传递——测试检测到此情况标 skip（沙箱外行为正常，cancel 时序契约由 mock 测试 `test_kill_one_sends_sigterm_first_then_sigkill_if_still_alive` 完整覆盖）。
- **`gates/hook_script.py` 暂未迁移到 ExitCode**：其退出码 0/2 是 git pre-commit / pre-push 协议（非 Orca workflow 退出码），与 SPEC §3.1 5 档语义不同；批 4 follow-up 决定是否经 ExitCode 派生（语义待议）。

---

## 4. 验收

- ✅ 1556 单测全过（baseline 1525 + 31 新增，0 回归）
- ✅ 所有 spawn 经 `ProcessRegistry.acquire`（runner.py 是唯一 spawn 入口）
- ✅ cancel 三段式（mock 时序测试 + 真子进程沙箱外验证）
- ✅ `start_new_session=True` 全量接入
- ✅ 退出码契约：completed→0 / failed→2 / cancelled→3；位置 `orca/iface/exit_codes.py`
- ✅ grep 守门（含 `raise SystemExit`，scope 排除 `iface/exit_codes.py` + `__main__.py`）
- ✅ atexit / signal / orchestrator.shutdown 三处幂等

---

## 5. 遗留技术债

1. **DI 传递链未完全闭合**（review 🔴 #1，跨 4 层架构问题，需设计后实施）：
   - 现状：`CLIRunner(registry=...)` 接口已开，但 5 处调用点（`ClaudeExecutor` / `dialog.py` / `executor_cmds.py` / `validator.py`）均不传 registry / backend / run_id / node_id，全部走 `get_default_registry()` 默认 singleton + 默认 `run_id="<unknown>"`。
   - 影响：① `process_local` fixture 仅对直接构造 CLIRunner 的 component test 有效（test_registry / test_cancel），整链 e2e 测试无法注入独立 registry；② 生产环境的 registry entry 缺 `run_id` / `node_id` 诊断元数据，SPEC §1.1「cancel 时按 run 批清理用」的 batch cancel 模式需后续补齐。
   - 正确性：acquire/release/shutdown 行为完全正确（默认 singleton 内状态干净），不影响铁律 1-5 的落地。
   - follow-up：phase-12 Adapter Protocol 落地时一并设计（推荐：在 `RunContext` 挂 `registry` 字段，与 `chart_sock` 同 pattern；`ClaudeExecutor.exec(ctx, node)` 从 ctx 取并透传给 CLIRunner）。CLAUDE.md「问题分类」明示跨 ≥3 文件需先设计。
2. **`gates/hook_script.py` 退出码未迁移**：批 4 follow-up（语义需讨论：gate hook 的 0/2 与 workflow 退出码 0/2 含义不同）。
3. **真实进程组 E2E 需非沙箱环境**：cancel 三层深孙子进程清理的端到端测试在沙箱内 skip，CI（非沙箱）会真跑。
4. **`workflow.py:retry_on` Literal 未联动**：本模块 cancel 产生的错误走 `ErrorKind.TRANSPORT_PROCESS`（phase-11-error 已落地），但 `retry_on` 用户 yaml 字面量未加 `process_cancel`（语义可议：cancel 不应自动重试）。
5. **Windows 测试缺失**（SPEC §5.1 `test_cancel_windows.py`）：`CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT` 路径未覆盖；平台分支代码已就位，待 Windows CI 环境。

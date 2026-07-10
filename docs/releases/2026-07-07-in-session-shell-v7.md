# 2026-07-07 —— in-session shell v7：薄 CLI 唯一大脑 + plugin/hook 哑传输

## 概要

按 SPEC v7（`docs/specs/in-session-shell-design-draft.md` v7）+ ADR v3
（`docs/specs/2026-07-07-in-session-iron-law-1-adr.md` v3）实现 in-session shell：
**薄 CLI = 唯一大脑 + 唯一 tape 写者**（per-call flock，I3.3b）；plugin (`.opencode/plugin/orca.ts`)
与 CC hook 脚本是**哑传输**（spawn CLI + parse JSON 顶层字段，零 Orca 业务逻辑）。
daemon 降级为无头 CI 形态（I3.3a）。

闭环 spec-review r2 全部 blocker (B1/B2) + major (N1/N2/M1-M4) + r3 reviewer 三项 🔴
（B-1/B-2/B-7 CC 脚本安全 + B-3 死代码 + B-4/B-5/B-6 一致性）。

## 交付

### 核心（Python，唯一大脑）
- `orca/events/tape.py` 加 `Tape.append_batch(list[dict]) -> list[int]`（B1）：
  共用 `_lock` + Event 校验 + seq 分配；先校验全部、再一次 `write("\n".join(lines)+"\n")`
  + 单次 `flush`；write/flush 失败 rollback `_last_seq`。
- `orca/events/bus.py` 加 `EventBus.emit_batch(list)`：透传 `tape.append_batch` + 逐条 fan-out。
- `orca/iface/in_session/marker.py`（新增）：激活 marker 读写（run_id/tape_path/yaml canonical
  /model/session_id/no_output_count）+ `os.replace` 原子写 + 半写容忍 + `find_marker_by_run_id` 线性扫描。
- `orca/iface/in_session/cli.py`（重写）：typer subcommands
  - `bootstrap <wf>` —— advisory lock marker 文件（贯穿 check-write）+ realpath 幂等键（N1）+
    per-call flock + emit_batch(ws+ns) + 原子写 marker（B-5：写失败包 try + emit workflow_failed）。
  - `next --tape --run-id [--output]` —— per-call flock（LOCK_NB → busy 0 退出，F5）+
    `--output` 空串 normalize None（B2）+ marker RMW 在 flock 临界区内（N2）+
    advance_step + `emit_batch` 单次 write 原子化（B1）+ 合规计数（F11，3 次无 output →
    workflow_failed(subagent_compliance)）+ 失败 taxonomy（F6：output_schema_mismatch /
    unsupported_node_kind / state_corrupt / subagent_compliance / internal_error）。
  - `status [run_id]`、`stop <run_id>`、`start <wf>`（CC）、`serve`（无头 CI，降级）。
- `orca/iface/in_session/daemon.py`：仅 docstring 更新，标注主 UX 改用 CLI、本模块保留无头 CI 形态。

### 宿主侧模板（哑传输）
- `orca/iface/in_session/templates/cc_hooks.py`（新增）：CC settings.json Stop/PostToolUse hook
  脚本片段生成。**安全契约**（B-1/B-2/B-7 闭环）：bash 数组 `ARGS=(...)` + `"${ARGS[@]}"`
  避免 word-splitting；`decision:block` JSON 用 `jq -n --arg` 构造；tmp 文件 `trap rm EXIT` 清理。
- `orca/iface/in_session/templates/opencode/orca.ts`（新增）：进程内 plugin。`session.idle` event
  hook（子 session 过滤 D-v7-5 + in-flight mutex F5 + task ToolPart.state.output 提取 D-v7-4
  + spawn next CLI + promptAsync 注入）+ `command.execute.before` 拦截 `/orca*`（spawn 对应 CLI
  子命令）。零 Orca 业务逻辑（grep 守门：无 advance/router/replay/tape 路径）。
- `orca/iface/in_session/templates/opencode/command/orca-{run,status,stop}.md`（新增）。

### 测试
- `tests/events/test_tape_append_batch.py`（9）：seq 连续 / 单次 write mock 实证 / 坏事件无部分落盘 /
  write 失败 rollback（SIGKILL 等价）/ 行格式与 append 一致 / close 后 raise。
- `tests/events/test_bus_emit_batch.py`（4）：透传 + 顺序 fan-out + timestamp 默认。
- `tests/iface/in_session/test_marker.py`（10）：round-trip / 半写容忍 / run_id 扫描 / RMW。
- `tests/iface/in_session/test_in_session_cli.py`（20）：bootstrap 幂等 / next emit_batch
  atomic / B2 空串 normalize / F11 合规 / F6 失败 taxonomy / F5 busy / N2 RMW / stop /
  status / start bash 数组守门 / 架构守门 grep（D-v7-1）/ G2 序列骨架。

## 验证

- 新增 43 测试全绿；`tests/events tests/run tests/iface/in_session tests/iface/cli tests/iface/mcp
  tests/compile tests/schema tests/exec tests/profiles tests/gates` 子集 **1591 passed / 0 failed**。
- 全量套件 **1734 passed**；2 failures 均为**预存**（与本次改动无关）：
  - `test_exit_codes.py::test_no_bare_sys_exit_or_raise_system_exit_outside_allowed_paths`
    —— 长期红，引用 `daemon.py:108:sys.exit(128 + signum)`（本 PR 未触该行；留 daemon
    迁 ExitCode 时一并修）。
  - `tests/e2e_phase12/...test_opencode_drives_tui_end_to_end` —— 环境性（port 7421 冲突 /
    macOS socket path 过长），独立运行通过（`pytest <test>` 单跑 39s passed）。
- 守门测试实证：
  - **B1**：mock 实证 next 走 `emit_batch==1` / `emit==0`（无逐条 emit）。
  - **B2**：`next --output ""` ≡ 省略，走 branch 4 + 合规计数 +1（不静默走 branch 3）。
  - **F11**：连续 3 次 next 无 output → workflow_failed(subagent_compliance)。
  - **F5**：撞锁 → {done:false, reason:busy} 0 退出。
  - **D-v7-1**：grep plugin/hook 模板无 advance/router/replay/tape 路径关键词。
  - **N2**：marker.no_output_count 自增跨 next 调用持久（RMW 不丢）。

## 偏差与遗留

- **daemon.py 仍逐条 emit（B-8）**：ADR v3 I2 把 daemon 列为跨进程 sanctioned 写者，理论上
  也应升级 emit_batch。本 PR 用户标注 daemon "only docstring updated"，B-8 留 follow-up。
- **未 spike 项**（SPEC §9.2 明示，留 `test-coverage-e2e` 真链路验）：
  - `/orca run` 命令的 `command.execute.before` 拦截真链路（spike 仅证 idle hook）
  - `bootstrap` 命令端到端（spike 用硬编码 prompt 注入，非调真 CLI）
  - 多 session 绑定（M3，用户已开 ≥2 session 后触发 /orca）
  - CC output cache 端到端（真 `claude -p` + PostToolUse→cache→Stop→next）
  - G2 序列对齐 vs `orca run` 同 wf tape（完整逐 seq 比对需跑 drive_loop 端到端）
- **合规失败退出码**：与其他失败 taxonomy 对齐 exit 1（r3 reviewer B-4 闭环）。
- **bootstrap 写 marker**：包 try + 失败时 best-effort emit workflow_failed + exit 1（r3 B-5 闭环）。
- **CC Stop 脚本**：bash 数组 + jq-safe JSON（r3 B-1/B-2 闭环）；PostToolUse tmp 用 `trap rm EXIT`
  清理（r3 B-7 闭环）。

## Commit
`6cd430c`

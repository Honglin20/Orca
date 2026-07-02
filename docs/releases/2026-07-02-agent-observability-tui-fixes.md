# Release Note —— Agent 可观测性 + TUI 闪退 + 子进程泄漏修复（4 bug）

> 日期：2026-07-02
> 类型：bug fix（4 个）
> Commit：（待填）

---

## 1. 目标与动机

排查「`demo_mixed` 含 agent 的 workflow 一跑就 TUI 闪退」时，定位根因是**上游智谱代理
529 过载**（`该模型当前访问量过大`，非 Orca bug），但暴露了 Orca 自身 **4 个 bug**，
导致用户出错时看不到原因、TUI 体验断裂、强退泄漏子进程：

1. **Bug1**：claude 把 API 错误写在 stdout 的 result 行（`api_error_status`），不在 stderr；
   `node_failed` 只带 stderr（空）→ 完全无信息。
2. **Bug2**：translator 读 `retry_count`/`wait_seconds`，真实 claude 发的是
   `attempt`/`retry_delay_ms`/`error_status` → `ApiRetry` 永远显示「第 ? 次」。
3. **Bug3**：TUI 在 workflow 终态时立即 `self.exit()`，用户来不及看错误（闪退）。
4. **Bug4**：中途 `q` 强退时 `CLIRunner.stream()` finally 不 terminate proc → 孤儿 claude 烧 quota。

---

## 2. Bug1：API 错误详情落到 node_failed

**根因**：`OnResult` 回调（`runner.py`）只透传 `(result_text, usage, cost, is_error)`，
没带 `api_error_status`；executor 的 ExecError message 只拼 stderr。

**修复**：
- `OnResult` 签名 4 参 → 5 参（加 `api_error_status: int | None`，`orca/exec/runner.py`）。
- `_maybe_fire_on_result` 读 result 行顶层 `api_error_status`（非 int 容错降级 None）。
- executor 加 `_result_diag()` 把 `HTTP {status}` / `result.is_error` / `result_text[:300]` /
  `stderr末尾` 拼成诊断摘要，4 个 ExecError 分支（timeout/spawn/stream/result_parse）共用（DRY）。
- validator / dialog 的 `on_result` 同步加参（一致性 + 自身日志/异常 message 带码）。

**影响面**：OnResult 5 参签名全仓 **11 处**调用点同步（4 生产 + 6 测试 FakeRunner + 类型别名），
全向后兼容（第 5 参默认 `None`）。code-reviewer 横切核对零遗漏。

---

## 3. Bug2：ApiRetry 字段名对齐真实协议

**真实行**（2026-07-02 智谱 529 触发实测抓取）：
```json
{"type":"system","subtype":"api_retry","attempt":1,"max_retries":10,
 "retry_delay_ms":547.07,"error_status":529,"error":"overloaded"}
```

**根因**：`_translate_system` 读 `retry_count`/`wait_seconds`（凭想象写的测试 fixture），
真实字段是 `attempt`/`retry_delay_ms`/`error_status` → 永远 null → message「第 ? 次等待 ?s」。

**修复**（`orca/profiles/translators/claude.py:_translate_system`）：
- `attempt → retry_count`（fallback 旧 `retry_count`，向后兼容）。
- `retry_delay_ms / 1000 → wait_seconds`（fallback 旧 `wait_seconds`）。
- 新增 `error_status`（HTTP 码）+ `max_retries` 进事件 data。
- message 优雅降级：缺字段省略对应片段，不显示无信息 `?`。

---

## 4. Bug3：TUI 终态后停留（不再闪退）

**根因**：`_run_orchestrator` worker 的 finally 调 `self.exit()`，workflow 一到终态 TUI 立刻退出。

**修复**（`orca/iface/cli/app.py`）：
- 删 finally 的 `self.exit()`。
- `_dispatch_to_widgets` 的 `workflow_completed`/`workflow_failed` 分支加 `self.notify(...,
  timeout=0)`（持久不消失），提示「按 q 退出」。
- `q` → Textual `action_quit` → `self.exit()` → `commands._run_workflow` 读 `terminal_state`
  决定 exit code（completed→0 / failed→1 / 中途 q→None→1，**保持不变**）。

---

## 5. Bug4：强退时 terminate 子进程

**根因**：`CLIRunner.stream()` 的 finally 块只 cancel stderr task，没 terminate proc。
asyncio 子进程不随父 task cancel 自动死 → 中途 `q` 强退留孤儿 claude。

**修复**（`orca/exec/runner.py` stream finally）：
- `proc.returncode is None` 时（外部 cancel / 异常退出，proc 仍存活）→ SIGTERM →
  3s grace（`_TERMINATE_GRACE_SECONDS`）→ SIGKILL 兜底。
- 正常完成路径（proc 已 `await proc.wait()` 返回，returncode 非 None）短路跳过。
- cancel 上下文下 `await wait_for` 被 `CancelledError` 打断 → `except` 捕获 → 同步 `proc.kill()`，
  不 hang（最坏 3s）。

---

## 6. 测试

| 用例 | 文件 | 覆盖 |
|---|---|---|
| `test_on_result_passes_api_error_status` | `tests/exec/test_runner.py` | Bug1：api_error_status 透传 |
| `test_stream_terminates_orphan_proc_on_cancel` | `tests/exec/test_runner.py` | Bug4：cancel→terminate proc |
| `test_system_api_retry_to_error_event`（改真实字段）+ `_legacy_fields_fallback` | `tests/profiles/test_claude_translator.py` | Bug2 |
| `test_error_path_message_carries_api_error_status`（spawn）+ `_stream_message_carries...` | `tests/exec/claude/test_executor.py` | Bug1：node_failed message 含 529 |
| 6 个测试文件 FakeRunner 同步 5 参 | test_executor/e2e/runner_sigint/validator/executor_mcp/dialog | OnResult 签名一致 |

**回归**：`uv run pytest -q -m "not integration"` → **985 passed, 1 skipped, 0 failed**
（skip 是既有 CI 超时项，非本次引入）。

---

## 7. 验证 / 后续

- **单测全覆盖** + code-reviewer 自检：0 🔴 必修、0 🟡，OnResult 5 参全仓零遗漏。
- **端到端 TUI 验证（Bug3）待手动**：需交互式 TTY + 智谱恢复或换可用模型。
  `uv run orca run examples/demo_mixed.yaml`，failed 时 TUI 应**停留**显示「按 q 退出」而非闪退；
  LogStream 的 ApiRetry 显示「第 1/10 次，等待 0.5s（HTTP 529 overloaded）」。
- **中途 q 强退（Bug4）**：`pgrep -af claude` 确认无孤儿进程。

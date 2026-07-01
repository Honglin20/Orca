# phase 11 P0.3 —— Retry Policy（节点级自动重试 transient claude 失败）

> SPEC：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §9.5（authoritative）
> 计划：[`docs/plans/2026-07-01-phase11-cli-enrichment.md`](../plans/2026-07-01-phase11-cli-enrichment.md) P0.3
> 日期：2026-07-02
> wave：phase 11 wave 2（Retry + ask_user）—— 本 commit 只交付 Retry

---

## 背景

agent node（`claude -p` 子进程）偶发失败：API 429 限流、500/overloaded、子进程非零退出、
timeout。Conductor 在 `config/schema.py:402-451` 用 `RetryPolicy` 解决。Orca 需等量能力
才能在生产 CLI 场景鲁棒运行（mxint 实测时偶发限流直接整轮 workflow 失败）。

本 feature 是 **Orca 自创设计**（SPEC §9.5.2 标注），借鉴 Conductor 思路但字段不同：
Conductor 为 `backoff: Literal["fixed","exponential"]` + 单 `delay_seconds` +
`retry_on: Literal["provider_error","timeout"]`；Orca 增加 `linear` backoff、
`max_delay_seconds` 上限、`jitter`（±20%）、`validator_failed` error_type（wave 3 用）。

## 改动

### 新增

- **`orca/run/retry.py`**（NEW）—— `execute_with_retry(executor, node, ctx, bus) -> (output, events)`
  核心 retry loop（SPEC §9.5.4）：
  - 按 `RetryPolicy.max_attempts` 循环，每次 attempt 收 executor 完整事件流（逐个 `bus.emit`
    落 Tape —— retry 期间所有 agent_message/tool_call 都可观测）。
  - `node_completed` → 重试后成功才 emit `retry_succeeded`（首次成功不发，避免噪声）。
  - `node_failed` → 三层判定（有序）：
    1. `was_interrupted=true` 短路退出（用户 Ctrl+G 主动中断，**不属于** transient error；
       防御性 `.get(default=False)`，缺字段不崩 retry 逻辑）。
    2. `error_type` 不在 `retry_on` 白名单 → fail loud 立即 raise（如 `result_parse`
       配置错 —— 重试也是错，浪费 token）。
    3. 在白名单 + 还有 attempt 额度 → emit `retry_started` → `await asyncio.sleep(delay)`
       → continue；用尽 → emit `retry_exhausted` → re-raise 最后一次 `ExecError`。
  - `_compute_delay(policy, attempt)` 单点 delay 计算：constant / linear / exponential，
    cap 到 `max_delay_seconds`，可选 ±20% jitter（`jitter=False` 路径供测试确定性断言）。
  - 设计为「执行一次 + 重试 transient 失败」primitive，wave 3 validator（§9.6.5）复用同一
    loop、同一份 `max_attempts` 计数，不双层嵌套。

- **`orca/schema/workflow.py::RetryPolicy`**（NEW）—— pydantic 模型，`extra="forbid"`：
  - `max_attempts: int = Field(default=3, ge=1)`（下界防 0/负数撞「不可达」分支）
  - `backoff: Literal["constant","linear","exponential"] = "exponential"`
  - `initial_delay_seconds: float = Field(default=1.0, ge=0.0)`
  - `max_delay_seconds: float = Field(default=60.0, ge=0.0)`
  - `retry_on: list[Literal["spawn_error","timeout","api_error","http_429","validator_failed"]] = ["spawn_error"]`
  - `jitter: bool = True`
  - `AgentNode` 加 `retry: RetryPolicy | None = None`（None = 向后兼容，不重试）。

- **`examples/with_retry.yaml`**（NEW）—— 单 agent + exponential backoff + 3 种 retry_on
  的演示（真实 E2E 需真实 flaky claude；automatable 证明见下方测试）。

### 修改

- **`orca/schema/event.py`** —— EventType 加 3 个：`retry_started` / `retry_succeeded` /
  `retry_exhausted`（SPEC §9.5.3）。Literal 总数 26 → 29（`tests/schema/test_event.py` 同步）。
- **`orca/schema/__init__.py`** —— 导出 `RetryPolicy`。
- **`orca/run/orchestrator.py::_dispatch`** —— agent node 声明 `retry` 时走
  `execute_with_retry`，否则既有 `execute_and_emit` 路径（向后兼容）。`node.kind=="agent"`
  经 pydantic discriminated union 已保证 `node.retry` 字段存在，直接访问无需 `getattr` 防御。
- **`orca/exec/error.py`** —— 加 `ExecError.from_failed_data(err_data, *, node)` classmethod
  （DRY 单点：从 `node_failed.data` 构造 ExecError，`retry.py` 与 `executor_adapter.py` 共享）。
- **`orca/run/executor_adapter.py`** —— 改调 `ExecError.from_failed_data`（消除与 retry loop
  的逻辑复制）。
- **`orca/events/replay.py`** —— reducer 把 `retry_started/succeeded/exhausted` 归入 no-op 集合
  （retry 本身不推进 `node_status` —— 同 node 多 attempt 不让 running/done 反复跳；retry 的
  最终成败由它包裹的 `node_completed`/`node_failed` 承担）。
- **`orca/iface/cli/widgets/log_stream.py`** —— `_describe` 加 3 个 retry_* 描述（可读：
  `↻ retry #2/3 after spawn_error (wait 1.0s)` / `✓ retry succeeded` / `✗ retry exhausted`）。

### 测试

- **`tests/run/test_retry.py`**（NEW，27 测试，逐条断言「意图」）：
  - 向后兼容（`retry=None` 走既有路径，无 retry_* 事件）
  - 首次成功不发 retry_*（避免噪声）
  - 第二次成功配对（retry_started 在 retry_succeeded 之前 + payload 正确）
  - 用尽（retry_started×N-1 + retry_exhausted + re-raise 最后一次）
  - retry_on 白名单过滤（`NoResultEvent` 配置错不重试）
  - backoff：constant / exponential(cap) / linear(loop + 单元) —— jitter=False 确定性
  - jitter ∈ [0.8×, 1.2×]（seeded）+ 零 base 边界
  - was_interrupted 短路 + 缺字段防御（`.get` 默认 False 不崩）
  - http_429 / api_error 精确匹配（error_type 对齐表 §9.5.2）
  - **error_type 对齐层 ×5**（CliExitNonZero→spawn_error / ExecTimeout→timeout /
    ClaudeStreamError+rate_limit→http_429 / ClaudeStreamError+generic→api_error /
    NoResultEvent 透传不命中）
  - `max_attempts=1` 等价无 retry
  - RetryPolicy schema 校验（`max_attempts<1` / 负 delay → ValidationError；0 delay 合法）
  - 生命周期违约 fail loud
  - orchestrator 集成 ×2（fail-then-succeed → workflow_completed + retry_*；
    exhausted → workflow_failed）

## error_type 对齐表（SPEC §9.5.2）

ClaudeExecutor 实际产出的 `node_failed.data["error_type"]` 是 phase 派生名
（`CliExitNonZero`/`ExecTimeout`/`ClaudeStreamError`，via `phase_to_error_type`），而
`RetryPolicy.retry_on` 的 Literal 取值是语义短名（`spawn_error`/`timeout`/`api_error`/
`http_429`/`validator_failed`）。两者命名空间不同 —— 本 commit 在 retry loop 加
`_classify_for_retry(error_type, err_data)` 桥接层（SPEC §9.5.2 对齐表的代码实现）：

| ClaudeExecutor 实际 phase / 条件 | `node_failed.data["error_type"]` | `_classify_for_retry` → retry_key | 命中 retry_on |
|---|---|---|---|
| `phase="timeout"` | `ExecTimeout` | `timeout` | `timeout` |
| `phase="spawn"`（exit_code != 0，**非** SIGINT） | `CliExitNonZero` | `spawn_error` | `spawn_error` |
| `phase="stream"` + message 含 `rate_limit`/`overloaded`/`429`/`529`/`api_retry` | `ClaudeStreamError` | `http_429` | `http_429` |
| `phase="stream"` + 通用 API 错（500 等） | `ClaudeStreamError` | `api_error` | `api_error` |
| `phase="result_parse"`（exit 0 但无 result） | `NoResultEvent` | `NoResultEvent`（透传） | **不重试**（配置错） |
| validator 失败（§9.6，wave 3） | `validator_failed` | `validator_failed`（透传） | `validator_failed` |
| 用户 SIGINT | `node_failed.data["was_interrupted"]=true` | — | **短路退出**（不进白名单判定） |

**为什么桥接层在 retry loop 而非改 ClaudeExecutor**（Rule 7 surface conflict）：改 executor
的 `error_type` 会破坏 `phase_to_error_type` 单一映射 + 现有 `workflow_failed.error_type`
断言（如 `ExecTimeout` 是 SPEC §6 错误映射表的契约值，下游 reducer / 前端依赖它）。在
retry loop 加映射层是局部、可测、可逆的 —— retry 的事件 payload 保留原始 `error_type`
（诊断价值），retry_key 只用于白名单匹配（不外泄）。

测试 `test_retry_classifies_*` 5 条证明真实 ClaudeExecutor error_type 经桥接后正确命中
retry_on 白名单。

## 验证

- **全量**：`uv run pytest tests/ -m "not integration"` → **753 passed / 1 skipped /
  0 xfailed / 0 failed**（基线 726 + 27 新测试，**0 回归**）。
- **新测试**：`tests/run/test_retry.py` 27/27 绿（含 5 条 error_type 对齐层测试）。
- **schema**：`tests/schema/` 58/58 绿（EventType count 26→29 同步）。
- **DRY 验证**：`ExecError.from_failed_data` 被 `executor_adapter` 与 `retry.py` 共享，
  `test_executor_adapter.py` 全绿证明既有路径行为不变。
- **手动 E2E**：`examples/with_retry.yaml` 经 `load_workflow` 解析 + `RetryPolicy` 校验通过
  （真实重试需真实 flaky claude，automatable 证明由 orchestrator 集成测试覆盖）。

## 偏离 SPEC

无（按 §9.5 逐字实现）。一处前置依赖确认：wave 1 已加 `CLIRunner.was_interrupted` +
`node_failed.data["was_interrupted"]`（SPEC §9.5.2 / review C7），本 commit 直接复用，
防御性 `.get(default=False)` 读取（hard constraint）。

## review 闭环（code-reviewer）

无 critical / major 阻塞。已采纳的建议：
1. ✅ `RetryPolicy.max_attempts` 加 `Field(ge=1)` + delay 字段 `ge=0.0`（schema 层 fail loud
   防误导性「不可达」错误）+ 3 个 ValidationError 测试。
2. ✅ 提取 `ExecError.from_failed_data` classmethod，`retry.py` 与 `executor_adapter.py`
   共享（消除 ExecError 构造的逻辑复制）。
3. ✅ `orchestrator._dispatch` 的 `getattr(node, "retry", None)` → `node.retry`（discriminated
   union 已保证类型，去死分支，对齐 SPEC §9.5.5 示例）。
4. ✅ `linear` backoff 补 loop 路径测试（与 constant/exponential 对称覆盖）。
5. ✅ `asyncio.sleep` 全局 monkeypatch 改 pytest `monkeypatch.setattr` fixture 风格。

未采纳（说明 why）：
- 「retry loop 内检查 interrupt_pending」—— retry loop 不读 orchestrator 私有态，interrupt
  由 orchestrator node 边界守护（retry 期间再次中断：当前 attempt 跑完后，下次 attempt 前
  orchestrator 的 node 边界检查会捕获）。retry loop 只读 `node_failed.was_interrupted`，
  职责单一。SPEC §9.5.6「重试期间再次中断」由 wave 1 的 node 边界机制覆盖，不在本 primitive 内。

## commit

`70f053b`

## 后续

- **wave 3 validator**（§9.6.5）：复用 `execute_with_retry` 作为「执行一次 + 重试 transient」
  primitive，validator 失败 emit `node_failed{error_type:"validator_failed"}` 即进同一 loop
  （`_classify_for_retry` 对 `validator_failed` 原样透传，retry_on=[validator_failed] 直接命中）。

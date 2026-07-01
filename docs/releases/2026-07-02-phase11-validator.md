# Release Note —— phase 11 P2.1 Semantic Output Validator（2026-07-02）

> **SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §9.6 / §11.6
> **PLAN**：[`docs/plans/2026-07-01-phase11-cli-enrichment.md`](../plans/2026-07-01-phase11-cli-enrichment.md) P2.3
> **基线**：822 passed → **852 passed**（+30，0 回归）

## 背景

agent 的 `output_schema` 只校 shape/type（结构化提取），校不出语义错：`model_class` 是字符串但
不是合法 Python 标识符、`weights_path` 是字符串但不是绝对路径。wave 3 P2.1 补 **LLM 二次语义
校验** —— agent 产出后 spawn 第二个 claude -p 做语义判断，失败时把 issues 作 guidance 反馈给
agent 重 spawn，直到通过或预算用尽。

## 改动点

### 新增
- **`orca/exec/validator.py`** —— `validate_output(output, config, profile, *, model=None)`：
  spawn 第二个 claude（复用 `SpawnConfig`/`CLIRunner`/`profile`，DRY），喂 agent output + criteria，
  返回 `{passed, issues}` JSON。**fail-safe**（SPEC §9.6.6）：validator LLM 自身崩 → 返回
  `(True, [])`（不阻塞 workflow）。5 条崩溃路径全覆盖（exit≠0 / is_error / 无 result / 不可解析 /
  spawn 异常）。
- **`examples/with_validator.yaml`** —— 单 agent + `validator.criteria` 演示（model_class 合法标识符 /
  weights_path 绝对路径）。
- **`tests/exec/test_validator.py`**（19 测试）+ **`tests/run/test_validator_orchestrator.py`**
  （11 测试）—— 单元 + orchestrator loop，断言 INTENT。

### 修改
- **`orca/schema/workflow.py`** —— `ValidatorConfig`（criteria `min_length=1` / max_retries `ge=0` /
  model 可选）+ `AgentNode.validator` 字段（None = 向后兼容）。
- **`orca/schema/event.py`** —— `validator_started` / `validator_passed` / `validator_failed` 进
  EventType Literal（34 个总数）。
- **`orca/run/orchestrator.py`** —— `_dispatch` 重构（抽 `_execute_agent` DRY）+
  `_dispatch_with_validator` loop（包裹 execute，emit validator_*，retry 用 guidance）。
- **`orca/exec/error.py`** —— `_PHASE_TO_ERROR_TYPE` 加 `"validator": "validator_failed"` +
  `"interrupted": "Interrupted"`（防御性补登）。
- **`orca/iface/cli/widgets/log_stream.py`** —— validator_* 的 `_describe`（🔍 validating / ✓ passed /
  ✗ failed+retrying/exhausted）。
- **`orca/events/replay.py`** —— validator_* + wait_* 进 reducer「known but not projected」名单
  （可观测标记，不改顶层 RunState，与 retry_* 同模式）。

## 关键设计决策（Rule 7 裁定，记入 SPEC §11.6）

### 1. `validate_output` 不持 bus、不 emit（铁律 2 张力化解）

SPEC §9.6.4 示例签名写 `validate_output(..., bus: EventBus, ...)` 且让它 emit `validator_started` /
`validator_passed`。但铁律 2（`tests/exec/test_contract.py::test_dependency_no_events_bus_no_tape`）
禁 exec/ import `orca.events.bus` / 持 `EventBus`。冲突。

**裁定（Rule 7，选 B）**：`validate_output` 移除 `bus` 参数，**纯返回 `(passed, issues)`**
（计算 + 一次 spawn，无副作用）；三类 validator_* 事件（started/passed/failed）**全部由
orchestrator 的 `_dispatch_with_validator` loop emit** —— 单一 emitter，与 retry_* 模式一致。比
SPEC 示例的「split emit」更内聚。`tests/exec/test_contract.py::test_dependency_no_events_bus_no_tape`
实测通过。

### 2. validator 与 retry 独立预算（SPEC §9.6.5「单一 retry loop」改写）

SPEC §9.6.5 原文写「retry 与 validator 共享同一个 retry loop、同一份 max_attempts 计数」。但
wave-2 `execute_with_retry`（`orca/run/retry.py`）已是自包含 transient-retry primitive（27 测试
committed）。

**裁定（Rule 7）**：validator 与 retry **正交**：
- `execute_with_retry`（unchanged）：管 transient executor 失败（spawn_error/timeout/api_error/
  http_429）。预算 = `retry.max_attempts`。
- validator loop（本 release）：每次成功 execute 后跑 `validate_output`。预算 = `validator.max_retries + 1`。
  失败 → emit `validator_failed(retrying=True)` + issues 作 guidance → 重 execute + 重 validate。
  用尽 → emit `validator_failed(retrying=False)` + `ExecError(phase="validator")`。

`_dispatch_with_validator` 在 `execute_with_retry` **外层**包一层；`_execute_agent`（wave-3 抽出，
DRY）内部决定走 retry 还是 plain。两 loop 各管各的失败域，不嵌套计数。`validator_failed` 留在
`RetryPolicy.retry_on` Literal（harmless no-op，executor 不发此 error_type），不 churn wave-2 schema。

`test_validator_independent_of_retry_budget` 是此 deviation 的 intent 级守护（fake executor：
transient 失败 → retry 重试 → output 校验失败 → validator 重试 → 通过；断言 retry_started count==1
+ validator_started count==2，两套预算各自消耗）。

## 已知 tech debt

- **`_build_env_overlay` 两处重复**（`orca/exec/validator.py` + `orca/exec/claude/executor.py`）。
  当前两处未触 CLAUDE.md「禁止三处以上重复」红线，内联是为了避免 `exec/claude → exec/validator`
  runtime 环依赖。第三处出现时抽 `orca/exec/env.py` 共享。

## 验证

- **基线**：`uv run pytest tests/ -m "not integration"` → **852 passed, 1 skipped, 0 xfailed**
  （822 → 852，+30 新测试，0 回归）。
- **code-reviewer**：implementation review + coverage review 双跑，全部 🔴/🟡 反馈已闭环
  （SPEC §11.6 补 deviation 表 / `criteria min_length=1` / dirty-issues 归一化测试 / 多失败
  guidance 累积测试 / 命名修正 / repr fallback 测试 / tech debt 登记）。
- **真实 E2E（手动）**：`orca run examples/with_validator.yaml`（需真 claude + ANTHROPIC_API_KEY，
  记为手动）。自动化证明在 `test_validator_independent_of_retry_budget` + `test_validator_failed_then_passed_with_retry`。

## Commits

- `6238dc9` —— feat(phase11): P2.1 Semantic Output Validator（LLM 二次语义校验）

## 偏离 SPEC 处

见 SPEC §11.6（本次新增）—— 2 条 deviation（validate_output 移除 bus / validator 与 retry 独立预算），
均 Rule 7 裁定 + release note 同步。

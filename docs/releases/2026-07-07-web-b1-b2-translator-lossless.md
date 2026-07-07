# Web Shell v2 B1/B2 —— opencode translator lossless + reasoning exposure

> **阶段**：web-shell-v2 §3.2 + §11 step1（B1/B2 后端，shell 无关，硬前置）
> **日期**：2026-07-07
> **SPEC**：[`docs/specs/web-shell-v2-spec.md`](../specs/web-shell-v2-spec.md) §3.2 / §0 D-decisions / §11 step1

## 1. 改动点

### B1 — opencode translator lossless（`orca/profiles/translators/opencode.py`）

opencode translator 之前只翻 4 种 envelope（text/tool_use/step_finish/error），静默丢
`reasoning` / `step_start`。本次按 SPEC §3.2 表逐字扩到 lossless：

| envelope | → Event | data |
|---|---|---|
| `reasoning`（新） | `agent_thinking` | `{text}`（整块，与 claude thinking_delta 同 canonical） |
| `step_start`（新） | **新 `agent_step_started`** | `{step_reason?}`（part.reason 可选；real protocol 实测不发，作 defensive 透传） |
| `step_finish`（扩） | `agent_usage` | **+`reasoning_tokens`**（← tokens.reasoning；旧 tape 默认 0） |
| 未知 envelope（新） | **新 `unknown_event`** | `{raw: <整条原始行>, source:"opencode"}`（**绝不静默丢**） |

向后兼容（铁律：只加字段不改语义）：旧 tape replay 遇新字段时消费侧 `data.get('reasoning_tokens', 0)` 兜底；新增类型在 reducer 显式 no-op（D8）。

### B2 — opencode reasoning 暴露（capabilities + profile + executor）

- `ProviderCapabilities` 加 `supports_reasoning: bool = False`（opt-in 默认 False；claude/ccr 不动，opencode=True）
- `CliProfile` 加 `reasoning_flags_env: str = ""` + `resolve_reasoning_args() -> list[str]` 方法（与 `resolve_flags` / `resolve_prompt_channel` 同构的三态 env 注入：未设通道 / env 未填 / env 显式填，默认 `[]`）
- opencode profile 设 `reasoning_flags_env="ORCA_OPENCODE_REASONING_FLAGS"` —— 用户可 `export ORCA_OPENCODE_REASONING_FLAGS="--thinking"` 或 `"--variant deepseek-reasoner"` opt-in
- `_build_spawn_config` 把 `profile.resolve_reasoning_args()` 追加到 `SpawnConfig.extra_args`（与 `--model` 同路径，追加在末尾）

### EventType 审计（SPEC §11 step1，闭 review #12）

`grep` 全消费点（reducer / projections / accumulator / TUI app.py / LogStream / EVENT_VISIBILITY / AgentHistory / _event_summary），为 2 个新 EventType 加 arm 或确认 default-no-op 安全：

| 消费点 | 处理方式 |
|---|---|
| `orca/events/replay.py:apply_event` | **显式 no-op 分支**（D8 要求；agent_step_started / unknown_event 绝不投影 RunState） |
| `orca/run/projections.py` | 自然跳过（filter on `event.type != "agent_usage"`） |
| `orca/exec/claude/accumulator.py` | 自然跳过（仅消费 agent_message / agent_usage / error） |
| `orca/iface/cli/app.py` | 非穷举 if/elif 无 else —— 自然 fall-through 安全 |
| `orca/iface/cli/widgets/_event_filter.py` | `EVENT_VISIBILITY[agent_step_started] = "show_dim"`；`unknown_event = "show_dim"`（归 Agent History，弱化可见） |
| `orca/iface/cli/widgets/log_stream.py` | 加入 `EVENTS_NOT_IN_LOG_STREAM`（显式 None，不进 LogStream；与 agent_thinking / agent_message 同归类） |
| `orca/iface/cli/widgets/_event_summary.py` | 加 `_build_summary_line` arm（"step <reason>" / "step" / "unknown"）+ `_build_detail_renderable` arm for unknown_event（SPEC §5.3 "可展开看 raw" 承诺兑现） |
| `orca/iface/cli/widgets/agent_history.py` | `_TYPE_LABELS` 加 `"agent_step_started": "STEP"` / `"unknown_event": "UNK"` |

### Fixture 扩展（`tests/profiles/fixtures/opencode_sample.jsonl`）

从 7 行扩到 9 行：
- 新增 1 条 `reasoning` envelope（capture 自 `/tmp/orca-vocab/raw3.jsonl` deepseek-v4-flash 真实抓取，文本已脱敏缩小）
- 在第二条 `step_start` 之后插入 reasoning，构造完整 step sequence（step_start → reasoning → text → step_finish）
- 新增 1 条 `experimental_event`（未知 envelope → 测试 unknown_event 透传）

## 2. 测试

| 测试文件 | 新增/修改 |
|---|---|
| `tests/profiles/test_opencode_translator.py` | 重写：reasoning / step_start / step_finish reasoning_tokens / unknown_event 全分支；fixture 9 行端到端 |
| `tests/profiles/test_capabilities.py` | supports_reasoning 默认 False / opt-in True / frozen 3 项 |
| `tests/events/test_replay.py` | no-op 集合加 agent_step_started / unknown_event；test_known_noop 显式覆盖 |
| `tests/schema/test_event.py` | EventType Literal 数量锁 37 → 39 |
| `tests/iface/cli/test_log_stream.py` | EVENT_LEVEL 完整性 + EVENTS_NOT_IN_LOG_STREAM 加 9 显式 None |
| `tests/iface/cli/test_event_visibility.py` | 新 `TestWebV2B1NewTypesThroughConsumers`（SPEC §11 step1 强制：tape 含新类型经全消费者无 crash + 幂等） |
| `tests/iface/cli/test_executor_cmds.py` | 新 `TestResolveReasoningArgs`（6 项：默认空 / env 三态 / 单 token / 多 token / 显式空） |
| `tests/exec/claude/test_executor_mcp.py` | 4 项 spawn config wiring：env 设 + 顺序（--model 在前）+ 默认 off + claude 不受影响 + 多 token |

**结果**：1758 passed / 0 新增回归（baseline 1751 + 7 新增；唯一 fail 是预存 B-8 `daemon.py:105`，与本任务无关）。

## 3. 偏离 SPEC 的决策

1. **`agent_step_started.data.step_reason` 是 defensive 字段**：真实 opencode v1.14.22 抓取确认 `step_start.part.reason` **从不出现**（reason 只在 `step_finish.part.reason`）。SPEC §3.2 表写 `data={step_reason?: part.reason}` 是"可选"。保留 translator 的 defensive 透传（防 protocol 变化 / 其他 fork 后端），但测试用内联构造覆盖（不依赖 fixture）。
2. **`reasoning_tokens` capture-only**：translator 把 `tokens.reasoning` 写入 `agent_usage.data.reasoning_tokens`，但 `UsageSummary` / `projections.node_usage` / TUI Header **不聚合**（B1 任务范围只覆盖 capture；aggregation 留给后续阶段，避免 scope creep）。`UsageSummary.docstring` 已注明此边界。

## 4. 验证结果

- ✅ Translator lossless：fixture 9 行端到端产出预期 type 集合（agent_step_started×2 + agent_thinking×1 + agent_message×2 + agent_tool_call+result×1 + agent_usage×2 含 reasoning_tokens=67/109 + unknown_event×1）
- ✅ Reducer no-op（D8）：agent_step_started / unknown_event 应用 N 次 = 0 次状态变更
- ✅ EventType 数量锁：39（37 + 2）
- ✅ 全消费者经新类型无 crash（SPEC §11 step1 grep 审计穷举完备）
- ✅ Backward compat：旧 step_finish（无 tokens.reasoning）→ reasoning_tokens=0
- ✅ B2 opt-in：默认 off；env 显式设才追加；claude profile 不受 opencode env 影响

## 5. Commit

`<commit-sha>`（待填）

## 6. 遗留 follow-up

- 🔵 `reasoning_tokens` aggregation：扩 `UsageSummary` + `projections.node_usage` + TUI Header 显累加（SPEC §3.2 表声明"聚合 TopBar/agent 行"，B1 仅 capture）
- 🔵 `--thinking` / `--variant` 真链路 E2E（需 opencode+deepseek-v4-flash 实跑 `--thinking` on 跑真实 workflow，验证 reasoning envelope 抓取 + reasoning_tokens 累加）
- 🔵 D1 codegen：event.py EventType → events.ts 生成 + CI grep（前端任务，B1 后置）

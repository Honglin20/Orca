# 2026-07-03 —— 后端统一抽象 + opencode 后端接入

## 背景

translator 抽象早已存在（加后端 = 加 translator + profile），但「后端怎么信号 *结束 + 最终答案 + usage + 错误*」这套契约**没统一**——硬钉在两处：runner 写死检测 `type=="result"`（`runner.py:285`）、executor 用 `on_result` 拿 `result.result`（`executor.py:163`）。`on_result` 还被 `validator`/`dialog`/`executor_cmds` 另外 3 处用，动 runner 会伤一片。

opencode（`opencode run --format json`，opencode 协议）没有 `result` 终止行——最终答案是 `text` 事件拼接、usage 来自 `step_finish`、错误来自 `error` 事件。硬接要么 hack 伪造一行，要么改 runner 伤一片。

## 方案：把"终止/结果契约"下沉成 profile 字段

新增 `TerminalContract`（`orca/profiles/terminal.py`，frozen dataclass）描述后端如何信号 done+result+usage+error，两模式：

- `result_line`：有终止行把最终文本+usage+错误一次给齐 → claude、ccr。runner 的 `on_result` 照常触发。
- `events`：没终止行，最终文本=`agent_message` 拼接、usage=`agent_usage`、错误=`error` 事件 → **opencode**。

executor 按 `profile.terminal.mode` **保留一处小分支**（`on_result` vs `consume_event`）。共享 `RunAccumulator`（`orca/exec/claude/accumulator.py`）：两种输入方式、**同一输出契约**（5 字段）。下游（`extract_and_validate` / `node_completed` / 错误判定）对两模式完全一样。

**为什么留小分支不彻底消灭**：消灭分支需把 validator/dialog/executor_cmds 也改成事件累积（它们现在只用 `on_result` 拿最终结果、不消费事件流），范围更大、风险更高。用户确认接受小分支——加后端仍是 translator+profile 两文件+选模式，分支本身不动。

## 数据流（统一抽象落点）

```
backend CLI (claude / opencode)
  │ stdout：一行行 NDJSON，边产生边吐
  ▼
CLIRunner readline ──▶ translator(line) → list[Event]   （换后端只改这 + profile）
  ▼
executor yield Event → EventBus → Tape / TUI / WEB（全后端无关）
```

加 claude/ccr/opencode 已覆盖；codex 等未来后端归入两模式之一（若终止行字段路径不同，给 `TerminalContract` 加可选字段即可，仍不动 executor/runner）。

## 关键交付

**新增**：
- `orca/profiles/terminal.py` — `TerminalContract` + `RESULT_LINE`/`EVENTS`。
- `orca/exec/claude/accumulator.py` — `RunAccumulator`（`make_on_result_hook` / `consume_event` / `events_result_text` / `diagnose`）。
- `orca/profiles/translators/opencode.py` — `opencode_translator`（用真实 opencode v1.14.22 NDJSON 校准）。
- `orca/profiles/builtin/opencode.py` — opencode PROFILE（events 模式，`prompt_channel=argv`）。

**改动**：
- `orca/profiles/base.py` — `CliProfile` 加 `terminal: TerminalContract`（默认 `RESULT_LINE`，向后兼容）。
- `orca/profiles/builtin/claude.py` + `ccr.py` — 各加 `terminal=RESULT_LINE`。
- `orca/profiles/translators/__init__.py` — 导出 `opencode_translator`。
- `orca/exec/claude/executor.py` — `result_holder`+`on_result` 闭包 → `RunAccumulator`；按 `profile.terminal.mode` 分支；错误诊断信息用 `self.profile.name`。

**runner.py bugfix（E2E 发现的真实 bug）**：`prompt_channel=argv`（opencode）路径下 stdin PIPE 永不关闭 → opencode 等 stdin EOF 才开工，**永久挂死**（实测 40s+ 不出一行）。补 `else` 分支 `proc.stdin.close()`（best-effort 吞 BrokenPipe）。claude 的 stdin 路径（`_pump_stdin` 已 close）零影响。

## model 注入

opencode 需 `-m <provider/model>`（默认 model 不可用）。**零代码改动**：既有 `AgentNode.model` 字段 → `_build_spawn_config` 在 `node.model is not None` 时 `extra_args.extend(["--model", node.model])`（`executor.py:287`）。workflow yaml 里 `model: zhipuai-coding-plan/glm-4.6v` 即可。

## 验证

- `pytest tests/exec/ tests/profiles/ tests/gates/ tests/iface/`：**688 passed, 30 skipped, 0 failed**（claude 路径零回归）。
- 新增测试：opencode translator 17 用例 + RunAccumulator 17 用例 + events 模式 e2e 10 用例（FakeRunner）。
- **真实 E2E（真实 orca CLI + 真实 agent + 真实 API，非 mock）**：
  - **opencode**：`orca run <executor:opencode workflow> --background --max-iter 3` → `orca wait` → **completed**（50.9s）。tape 有 `agent_message`（整块）+ `agent_usage`（step_finish：in=17/out=21/cache=11208/cost=$6.09e-05）+ `node_completed.output` 非空。
  - **claude（回归）**：同流程 → **completed**（2.9s）。tape 有 `agent_message`（token 增量）+ `agent_usage`（result 行）+ `node_completed.output` 非空。
  - 两后端产出**同一组 lifecycle Event 类型**——统一抽象经事件结构对照验证成立。

## 已知 TODO（非本次范围）

- `orca executor test` 命令目前只验"claude stream-json"（用 on_result），对 events 模式后端不适用。opencode E2E 走 `orca run`；`executor test` 对 events 后端的适配待后续。
- opencode `agent_usage` 是 per-step（多步多条），`RunAccumulator` 取最后一条；多步 agent 的真实聚合语义待多步 E2E 覆盖。
- MCP/gates/ask_user 经 opencode 暂不支持（capabilities 保守降级）。

Commit: <回填>

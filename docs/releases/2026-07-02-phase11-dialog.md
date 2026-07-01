# Release Note —— phase 11 P2.2 Dialog（agent 跑完后多轮追问）

**日期**：2026-07-02
**SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §6 / §2.1 / §2.2 / §2.4 / §6.3
**Commit**：caa3943
**Wave**：phase 11 wave 3 第三项（P2.2 Dialog）

## 背景

Conductor 有 Dialog feature（`gates/dialog.py`）——agent 跑完后用户就其 output 多轮追问。
Orca phase 11 P2.2 补齐这个能力，保留单 Tape + 单向依赖 + fail loud 架构。

## 改动点

### 新增

- **`orca/gates/dialog.py`**（NEW）—— `DialogHandler`，**3-method split**：
  - `start_dialog(node, agent_output, ctx) -> dialog_id`：emit `dialog_started` + 初始化 per-dialog 状态。
  - `send_turn(dialog_id, user_text, ctx) -> reply`：emit `dialog_message(role=user)` → 重 spawn
    claude（agent_output + **完整历史** + 本轮 user 输入拼进 prompt）→ emit `dialog_message(role=agent)`。
  - `end_dialog(dialog_id, ctx)`：emit `dialog_ended{total_turns, conclusion}`。
  - 每轮重 spawn + 拼全历史（`-p` 路线无 in-process session，靠 prompt 拼历史模拟多轮）。
  - fail loud：未知 dialog_id raise KeyError；spawn 失败（exit_code≠0）raise RuntimeError。
- **`orca/iface/cli/screens/dialog_modal.py`**（NEW）—— `DialogModal`（Textual `ModalScreen[None]`）：
  标题 + agent output 摘要面板 + 滚动历史区（RichLog）+ Input + 发送/结束按钮。
  - 发送：`@work` worker 异步调 `send_turn`（UI 不卡），history 显示 `user>`/`agent>`。
  - 结束：`@work` worker 调 `end_dialog` → dismiss。Esc = 结束（与 InterruptModal Esc=abort 同语义）。
  - send_turn 失败 → history 显示错误（fail loud 可观测），发送按钮复位（可重试）。
- **`orca/exec/env.py`**（NEW）—— `build_env_overlay(prefixes)`：抽出的共享 env overlay 构造
  （profile 前缀透传给子进程，SPEC §2.6）。原 exec/claude/executor + exec/validator + gates/dialog
  三处内联重复，Rule 6（DRY ≥3 处触发抽象）抽出。

### 修改

- **`orca/exec/context.py`** —— `RunContext` 加 `dialog_history: tuple[dict, ...] = ()` 字段 +
  `with_dialog_turn(role, text, turn)` 方法（frozen tuple 累积，与 `with_guidance` 同 pattern）。
- **`orca/schema/event.py`** —— `EventType` Literal 加 `dialog_started` / `dialog_message` /
  `dialog_ended`（3 个，总数 34 → 37）。
- **`orca/iface/cli/app.py`** —— 加 `Binding("d", "dialog", "对话")` + `action_dialog()`：
  找最近完成的 agent node + 其 output → push DialogModal。无完成的 agent node → LogStream 写 hint。
  `_dispatch_to_widgets` 的 `node_completed` 分支记 `_last_completed_agent_node`/`_output`（仅 agent kind）。
- **`orca/iface/cli/widgets/log_stream.py`** —— `_describe` 加 dialog_* 三事件描述（💬 前缀）。
- **`orca/exec/validator.py` / `orca/exec/claude/executor.py`** —— 改用共享 `build_env_overlay`，
  删本地 `_build_env_overlay` 重复实现（DRY）。

## 关键设计决策（Rule 7 裁定，记入 SPEC §11.7）

1. **3-method split 而非单一 `run_dialog`**（PLAN correction #7）：Textual modal 需在 agent reply
   与下一轮 user 输入间交还控制给 UI，单阻塞 `run_dialog` 做不到。SPEC §6.2 伪代码是接口示意，
   实现以 3-method split 为准。
2. **`ctx.dialog_history` 是未来 web shell replay 预留位，当前 CLI 不写**：dialog 唯一真相在
   tape 的 `dialog_message` 事件。DialogHandler 不写 ctx.dialog_history（dialog 是 post-run，
   ctx 已不在 orchestrator 流水里，无回流路径）。避免重蹈 AgentHarness 多 store 漂移覆辙。
3. **DialogHandler 持 bus 且 emit**（与 InterruptHandler/HumanGateHandler 同层同 pattern）：不
   违反铁律 2（铁律 2 禁 exec/ import bus；gates/ 本就是控制流 + 事件层）。

## 验证

### 自动化（deterministic，mock claude spawn）

- **`tests/gates/test_dialog.py`**（10 测试）：start_dialog emit + send_turn user/agent 顺序 +
  **历史累积核心契约**（第 2 轮 prompt 含第 1 轮 user + agent reply）+ end_dialog emit +
  fail loud（未知 id / spawn 失败）+ profile.resolve_cli_path()（review C5）+ with_dialog_turn frozen。
- **`tests/iface/cli/test_dialog_modal.py`**（7 测试）：compose 全 widget + 标题含 node +
  发送 → history user>/agent> + 空文本不发 + 结束按钮/Esc dismiss + **send 失败显示错误 + 按钮复位**。
- **`tests/iface/cli/test_app.py::TestDialogFlow`**（6 测试）：d 弹 modal（有 agent node）/ 写 hint（无）+
  agent output 记录（script 不记）+ dialog_* 三事件 LogStream 描述。
- **`tests/exec/test_env.py`**（4 测试）：build_env_overlay 前缀匹配 / 排除非匹配 / 空 / 多前缀。
- **`tests/schema/test_event.py`**：EventType 计数 34 → 37。

### 全量回归

`uv run pytest tests/ -m "not integration"`：**879 passed, 1 skipped, 0 failed**
（baseline 852 → 879，+27 新测试，零回归）。

### 人工 E2E（待真 TTY + ANTHROPIC_API_KEY）

`examples/with_dialog.yaml`：单 agent node。运行步骤：
1. `orca run examples/with_dialog.yaml` → worker agent 跑完（output 写 tape）。
2. 按 `d` → DialogModal 弹出（标题 `💬 DIALOG · node=worker` + output 摘要）。
3. 输入「为什么 result 是这个值？」+ 回车 → history 显示 `user>` + `agent>` 回复。
4. 继续追问几轮（每轮重 spawn claude，历史累积）。
5. 点「结束对话」或 Esc → `dialog_ended` 写 tape。
6. tape 含 `dialog_started` + N×`dialog_message` + `dialog_ended`。

自动化证明已在 `tests/gates/test_dialog.py::test_send_turn_accumulates_history`（断言第 2 轮
prompt 含第 1 轮历史）+ modal pilot 测试（fake handler）。

## 偏离 SPEC

- §6.2 伪代码（单一 `run_dialog`）→ 3-method split（理由见上，记入 §11.7）。
- §2.1 `dialog_history` 字段语义 → 当前 CLI 不写（真相在 tape，字段是 web shell replay 预留位，
  记入 §11.7）。

## 后续

- daemon(P3.2) → Skip(P4)。
- Web shell Dialog 渲染（推迟到 Web phase，复用 DialogHandler 三方法 + tape replay）。

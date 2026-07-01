# Release Note —— phase 11 P1.1 Step A：优雅中断 UI（InterruptHandler + InterruptModal + Orchestrator wiring）

> **日期**：2026-07-01
> **SPEC**：[phase-11-cli-enrichment.md](../specs/phase-11-cli-enrichment.md) §2.2 / §2.3 / §2.4 / §3
> **PLAN**：[2026-07-01-phase11-cli-enrichment.md](../plans/2026-07-01-phase11-cli-enrichment.md) P0.2 + P1.1（Step A 部分）
> **commit**：`9db57f4`
> **状态**：Step A 完成（中断 UI + orchestrator wiring）；Step B（guidance 注入 + SIGINT）随后

---

## 背景

phase 11 第一波 feature 之一：**优雅中断 UI**（SPEC §3）。长跑 workflow 时用户想纠偏——
Ctrl+C 硬杀体验差（丢 tape、无引导）。本 feature 给用户一个 **Ctrl+G → InterruptModal**
的优雅通道：选 CONTINUE（带 guidance 重跑当前 node）/ SKIP（跳过）/ ABORT（中止）。

Step A 只做**中断 UI + orchestrator wiring**（不碰 guidance 注入 / SIGINT，那是 Step B）：
用户能弹 modal、选动作、事件写 tape、orchestrator 在 node 边界消费 pending。guidance
字段在 Step B 才接进 render_prompt。

## 改动点

### 新增

- **`orca/gates/_broadcaster_mixin.py`**：抽出 HumanGateHandler 的 `start`/`stop`/`_broadcaster`
  asyncio-Queue + 后台 task pattern 成 `BroadcasterMixin`（DRY，SPEC §3.3）。子类实现
  `_emit_resolved(item)` hook 决定如何 emit resolved 事件。
- **`orca/gates/interrupt.py`**：`InterruptHandler(BroadcasterMixin)` —— `request(ireq)` emit
  `interrupt_requested` + await 用户答；`resolve(interrupt_id, action, guidance, source)` first-wins
  + threading.Lock 跨线程安全；`_emit_resolved` 异步 emit `interrupt_resolved`。与 HumanGateHandler
  共享生命周期、独立业务语义。
- **`orca/iface/cli/screens/interrupt_modal.py`**：Textual `ModalScreen[tuple[str, str|None]]` ——
  标题 `⏸ INTERRUPT · node=X` + 已耗时 + guidance TextArea（可选）+ CONTINUE/SKIP/ABORT 三按钮 +
  Esc=abort。dismiss 返回 `(action, guidance)`。
- **`tests/gates/test_interrupt.py`**（9 用例）：request 阻塞至 resolve / first-wins / broadcaster
  emit resolved 写 tape（含 payload + node/session 透传）/ 未知 resolve fail loud / idempotent
  start/stop / 多线程并发 resolve 恰一赢家 / has_pending。
- **`tests/iface/cli/test_interrupt_modal.py`**（7 用例）：compose 三按钮+textarea / 标题含 node /
  CONTINUE±guidance / SKIP（忽略 guidance）/ ABORT / Esc=abort。

### 修改

- **`orca/gates/handler.py`**：继承 `BroadcasterMixin`，删重复的 start/stop/_broadcaster，改实现
  `_emit_resolved`（emit `human_decision_resolved`）。行为完全一致（40 个 gates 测试零回归）。
  保留模块级 `_STOP` 别名向后兼容。
- **`orca/gates/types.py`**：新增 `InterruptRequest` frozen dataclass（SPEC §3.2：id/node/run_id/
  session_id/source/elapsed_at_request/context）+ `InterruptSource` / `InterruptAction` Literal。
- **`orca/schema/event.py`**：EventType Literal +3：`interrupt_requested` / `interrupt_resolved` /
  `prompt_rendered`（后者 Step B 才产出，事件类型先注册）。
- **`orca/run/errors.py`**：新增 `WorkflowAborted` 异常（用户 Ctrl+G + ABORT 中止，error_type=
  `WorkflowAborted`）。
- **`orca/run/orchestrator.py`**：
  - `__init__` 加可选 `interrupt_handler: InterruptHandler | None = None`（None = 无中断支持，
    向后兼容，既有测试零回归）+ `_interrupt_pending` / `_guidance_acc` 状态。
  - 新增公开 `request_interrupt(ireq)`（SPEC §2.3 测试 A 修正：公开方法，不经任何 proxy）。
  - `_drive_loop` 在 node 边界检查 `_interrupt_pending` → `_handle_interrupt` → 按 action 分支
    （abort raise WorkflowAborted / skip 推进下一 node / continue 累积 guidance 进 Step B 接的 ctx）。
  - 抽 `_next_node_after`（DRY：drive_loop 与 skip 共用 route 求值 + emit route_taken）。
  - `run()` except 链 + `_classify_error`/`_error_node` 加 `WorkflowAborted` 分支。
- **`orca/iface/cli/app.py`**：`BINDINGS` 加 `ctrl+g → interrupt`；构造 `InterruptHandler`；
  `_run_pipeline` 注入 orchestrator + start/stop interrupt_handler broadcaster；`action_interrupt`
  @work 弹 InterruptModal → `orchestrator.request_interrupt` + `interrupt_handler.resolve`；
  `_dispatch_to_widgets` 在 node_started 追踪 `_current_node`/`_current_session_id`/`_node_started_at`。
- **`orca/iface/cli/widgets/log_stream.py`**：`format_event` 加 `interrupt_requested` /
  `interrupt_resolved` / `prompt_rendered` 描述。
- **`tests/schema/test_event.py`**：EventType 计数 22 → 25（+3 phase 11）。

## 验证

- **全量回归**：`uv run pytest tests/ -m "not integration"` = **674 passed / 1 skipped**（基线 652，
  +22 新测试，0 回归）。
- **白盒 InterruptHandler**：9 用例全过（含跨线程 first-wins、broadcaster tape 落盘 payload 校验）。
- **黑盒 InterruptModal**：7 用例全过（pilot 驱动三按钮 + Esc + guidance textarea）。
- **CLI 集成**（test_app.py +6）：Ctrl+G 推 InterruptModal / 无编排时 warn 不弹 / interrupt_*
  事件分发到 LogStream / node_started 追踪 current_node。

## 偏离 SPEC / 决策

1. **BroadcasterMixin 用 abstract hook（`_emit_resolved`）而非泛型基类**：mixin 表达「能力混入」
   （HumanGateHandler / InterruptHandler 各有独立 request/resolve 业务，仅共享生命周期样板），
   基类会暗示 is-a 层级（interrupt 不是 gate），语义错位。子类 hook = OCP 局部扩展点。
2. **`prompt_rendered` 事件类型在 Step A 注册但 Step B 才产出**：SPEC §2.2 把它列在 phase 11
   基础事件里，Step A 一次注册全 3 个避免后续 EventType 计数测试反复改。
3. **本 commit 同时合入先前未提交的 mxint 端到端实测 bugfix 基线**（orchestrator default-fill /
   app.py on_mount kickoff / log_stream agent_usage / commands.py）：Step A 的 orchestrator
   `_drive_loop` 改造建立在 mxint 的 default-fill 循环之上，两者在同一 hunk 不可分；mxint release
   note（`2026-07-01-e2e-mxint-bugfix.md`）的 commit 字段指向本 commit。这是历史叠加，已在
   CHANGELOG 两条分别记录。

## 下一步（Step B）

- `RunContext.user_guidance` / `interrupt_history` + `with_guidance` / `guidance_prompt_section`。
- `render_prompt` 拼 `[User Guidance]` 段。
- `CLIRunner.send_sigint` + `was_interrupted`；ClaudeExecutor SIGINT 当 interrupt 不当 error。
- ClaudeExecutor emit `prompt_rendered`（spawn 前，preview=末尾 ~200 字符）。
- orchestrator `_handle_interrupt` continue 分支接 `ctx.with_guidance` + `_make_ctx` 注入 guidance。
- E2E：fake executor + fake interrupt_handler，断言 tape `interrupt_requested` + `interrupt_resolved`
  {guidance} 配对 + 重 spawn `prompt_rendered` preview 含 `[User Guidance]`。

## 人工 E2E（Step A 范围，待 Step B 后一并实跑）

Step A 单独无法完整实跑（guidance 注入未接，continue 分支无可见效果）。Step B 完成后跑：
`orca run examples/mxint_analysis.yaml`，跑到 configurator 时 Ctrl+G → 选 ABORT → workflow
中止 exit 1，tape 含 `interrupt_requested` + `interrupt_resolved{action:abort}` 配对。

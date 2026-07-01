# Release Note —— phase 11 P1.1 Step B：mid-run Guidance 注入 + SIGINT + review §2.1 critical 修复

> **日期**：2026-07-02
> **SPEC**：[phase-11-cli-enrichment.md](../specs/phase-11-cli-enrichment.md) §2.1 / §4 / §10.2 item3 B5 / §10.3 C3
> **PLAN**：[2026-07-01-phase11-cli-enrichment.md](../plans/2026-07-01-phase11-cli-enrichment.md) P1.1（Step B 部分）
> **commit**：`<TBD>`
> **状态**：Step B 完成（guidance 注入闭环 + SIGINT-as-interrupt + review §2.1 死锁修复）；feature 可合并

---

## 背景

Step A（commit `9db57f4`）交付了中断 UI + orchestrator wiring；Step B 接上 **guidance 注入闭环**：
用户 Ctrl+G + CONTINUE + 纠偏话 → SIGINT 当前 claude → orchestrator node 边界消费 →
guidance 累积进 ctx → 重 spawn 的 agent prompt 末尾拼 `[User Guidance]` 段。这是 SPEC §4 的核心。

code-reviewer 在 Step A+B 全 diff 上发现 **critical correctness bug（§2.1 时序死锁）**：
Step A 的 `action_interrupt` 把「登记 pending」与「resolve」两步连调，而 orchestrator 的
`handler.request`（注册 future）要等下一个 node 边界才执行——resolve 必然落空（future 未注册），
返回 False + guidance 丢弃，且 workflow 永久卡死在 `await fut`。Step B 本 commit 修复。

## 改动点

### 🔴 review §2.1 修复（critical correctness bug）

**根因**：CLI 单壳场景下，用户在 InterruptModal 里答完时，orchestrator 还没到 node 边界
（`handler.request` 未调，future 未注册）。Step A 的 `resolve` 在 modal dismiss 后立即调，必然落空。

**修复**（SPEC §3.1 时序对齐）：
- `Orchestrator.request_interrupt(ireq, answer=None)`（`orchestrator.py`）：加可选 `answer`
  参数。CLI 单壳路径把 ``(action, guidance)`` 随请求带入（用户已答完）。
- `InterruptHandler.record_resolved(ireq, action, guidance, source)`（`interrupt.py` NEW）：
  CLI 路径专用——emit `interrupt_requested` + 入队让 broadcaster emit `interrupt_resolved`
  （两者都写 Tape），**不经 await-future**（不依赖 resolve 时序）。
- `_handle_interrupt`（`orchestrator.py`）：两条取答路径——`_interrupt_answer` 非 None（CLI）
  → `record_resolved`；None（多壳 web/mcp，本 step 不启用）→ `await handler.request`。
- `action_interrupt`（`app.py`）：改调 `orch.request_interrupt(ireq, answer=(action, guidance))`，
  删掉错位的 `handler.resolve` 调用。

**多壳路径保留**：`request`/`resolve`/await-future 机制完整保留，给 P3 web/mcp 多壳竞速用。
CLI 单壳不需要竞速（用户只在一个壳答），且时序不匹配 await-future。

### Step B guidance 注入（SPEC §4）

- **`orca/exec/context.py`**：RunContext 加 `user_guidance: tuple[str,...] = ()` +
  `interrupt_history: tuple[dict,...] = ()`（后者预留 P2.1 validator / replay 用）。
  新增 `with_guidance(text)`（frozen 派生，空/空白忽略）+ `guidance_prompt_section()`
  （逐字对齐 Conductor `[User Guidance]` 段）。`with_locals` 透传两新字段。
- **`orca/exec/render.py`**：`render_prompt` 渲染完 base prompt 后拼 `ctx.guidance_prompt_section()`
  （无 guidance 时原样返回，向后兼容）。抽 `_load_agent_md` helper（DRY）。
- **`orca/run/orchestrator.py`**：`_handle_interrupt` continue 分支累积 `_guidance_acc`；
  `_make_ctx` 把 `_guidance_acc` 注入 `ctx.user_guidance`（SPEC §10.3 C3：走既有 _make_ctx，
  不新增 with_outputs）。每个 derived ctx（dispatch/route/outputs）自动带累积 guidance。

### Step B SIGINT-as-interrupt（SPEC §4.2）

- **`orca/exec/runner.py`**：`CLIRunner.send_sigint()`（proc 存活时发 SIGINT + 置
  `_was_interrupted=True`，幂等）+ `was_interrupted` 属性。`stream()` 期间跟踪 `_proc` 句柄，
  `_finalize` 清句柄（**不**复位 was_interrupted——executor 在 stream 后读它判中断）。
  one-use 契约（ClaudeExecutor.exec 每次 new runner）docstring 明示。
- **`orca/exec/claude/executor.py`**：stream 后**优先**判 `runner.was_interrupted`（在
  timed_out/exit_code 之前）→ emit `node_failed{was_interrupted:true, error_type:"Interrupted"}`，
  **不** raise ExecError（用户主动中断不是 transient error，SPEC §9.5.2 retry 短路前置）。
  spawn 前 emit `prompt_rendered`（preview=prompt 末尾 ~200 字符，含 `[User Guidance]` 段时
  可观测，SPEC §2.2 / §10.2 item3 B5）。

### 测试

- **`tests/exec/test_render.py`**（+8）：guidance section 拼接 / 空 guidance 无 section /
  单条 guidance / `guidance_prompt_section` 逐字格式 / None when empty / `with_guidance`
  frozen 不可变 / 多次累积 / 空白忽略。
- **`tests/exec/test_runner_sigint.py`**（NEW，5）：send_sigint 未启动返回 False / 存活发
  SIGINT+置 flag / 已退出返回 False / executor `was_interrupted=True` emit node_failed
  不 raise / executor emit prompt_rendered（preview ≤200 字符）。
- **`tests/run/test_interrupt_orchestrator.py`**（NEW，8）：_handle_interrupt continue/skip/
  abort 三分支 + guidance 累积 / continue 无 guidance / 消费 pending / request_interrupt
  带 answer 设 _interrupt_answer / 无 handler warning / **E2E（fake executor + 真
  InterruptHandler，record_resolved CLI 路径）**：tape interrupt_requested + interrupt_resolved
  {guidance:"skip weights"} 配对 + prompt_rendered preview 含 `[User Guidance]` + `skip weights`。
- **`tests/gates/test_interrupt.py`**（+2）：record_resolved emit requested+resolved 写 Tape /
  record_resolved 无 future 不死锁。
- FakeRunner（test_executor.py / test_e2e.py）加 `was_interrupted=False` 默认。

## 验证

- **全量回归**：`uv run pytest tests/ -m "not integration"` = **697 passed / 1 skipped**
  （Step A 后 674，+23 新测试，0 回归）。
- **§2.1 死锁修复验证**：E2E（`test_e2e_interrupt_continue_guidance_renders_in_respawn`）
  用真 InterruptHandler + fake executor + CLI `request_interrupt(answer=)` 路径，无死锁，
  tape 配对完整，prompt_rendered 含 `[User Guidance]`。
- **code-reviewer**：1 critical（§2.1 死锁，已修）+ 2 major（§2.2 dead-code 评估为 SPEC 预留
  非删 / §2.3 was_interrupted 不复位评估为 one-use 契约 + docstring 明示）+ 多 minor。
  §2.1 critical 已闭环。

## 偏离 SPEC / 决策

1. **CLI 单壳路径不经 await-future（SPEC §3.1 时序）**：review §2.1 发现 SPEC §2.3 只规定
   `request_interrupt` 公开方法，未规定「resolve 何时被调」。CLI 单壳用户在 modal 答完时
   orchestrator 未到 node 边界，await-future 死锁。本 commit 加 `record_resolved` 路径绕过
   future，时序对齐。多壳路径（await-future）保留给 P3。SPEC §11 已记此偏离。
2. **`RunContext.with_guidance` 保留（review §2.2 评估）**：reviewer 建议删（生产路径用
   `_guidance_acc`）。但 SPEC §9.6.5（validator retry loop）显式调 `ctx.with_guidance(...)`
   把校验失败 issues 反馈给下次 spawn——它是 P2.1 validator 的公开 API，非死代码。保留 +
   docstring 说明。`interrupt_history` 同理预留 replay/debug。
3. **`_finalize` 不复位 `was_interrupted`（review §2.3）**：reviewer 建议复位防 runner 复用。
   但 executor 在 stream() 返回后读此标志判中断——复位会丢信号。改为 docstring 明示 one-use
   契约（ClaudeExecutor.exec 每次 new runner，不复用）。

## 人工 E2E（待实跑）

`orca run examples/mxint_analysis.yaml`，跑到 configurator（~30s）时 Ctrl+G → 输入
「skip weights」+ CONTINUE → configurator 重 spawn（prompt 末尾含 `[User Guidance]`）→
workflow 继续 exit 0。tape 含 `interrupt_requested` + `interrupt_resolved{action:continue,
guidance:"skip weights"}` + `prompt_rendered`（preview 含 `[User Guidance]`）配对。
（交互测试，自动化由本 commit 的 E2E 用例 + 即将到来的 test-coverage-e2e 覆盖。）

## 下一步（Step C）

- 实跑 `orca run` 人工 E2E 验证（如上）。
- 可选：dispatch test-coverage-e2e 做专门 e2e sweep（fake executor 已覆盖核心闭环）。
- 清 CURRENT.md（本 feature 完成）→ CHANGELOG 顶部加索引。

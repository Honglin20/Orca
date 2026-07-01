# 计划：阶段 11 CLI feature 补全（中断 / Guidance / ask_user / Dialog / Resume / Daemon）

> 实现前**先写计划**（SDD 流程：读 SPEC → 写计划 → 确认 → 实现）。
> **计划不写代码**，只列：做什么、怎么做、怎么测。

> ⚠️ **Review 驱动修订（2026-07-01，对抗评审 fail→conditional-pass）**：本计划下文的代码片段有 **9 处虚构/陈旧 API**，已被 SPEC 修订覆盖。**SPEC [`phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §10.3 是权威契约**，本计划与之冲突处一律以 SPEC 为准。逐条订正（实现时遵循）：
>
> 1. **P0.2/P1.1 步骤3 `ctx.with_outputs(outputs_acc)`** → 既有 `self._make_ctx(outputs_acc)`（C3，不新增 with_outputs）。
> 2. **P0.2/P1.1 `self._orchestrator_proxy.set_interrupt_pending(ireq)`** → `self._orchestrator.request_interrupt(ireq)`（测试A，Orchestrator 公开方法，见 SPEC §2.3）。
> 3. **P0.3 retry 测试 `was_interrupted`** → 前置依赖 CLIRunner.was_interrupted + node_failed.data 加此字段；retry loop 检查它短路退出（C7，见 SPEC §9.5.2 error_type 对齐表）。
> 4. **P1.1 E2E「agent output 反映 guidance」** → 删除（LLM 非确定）；改断言 tape `interrupt_resolved.guidance` + `prompt_rendered` 事件含 `[User Guidance]`（B5）。
> 5. **P1.2 `from fastmcp import FastMCP` / `fastmcp.testing`** → `from mcp.server.fastmcp import FastMCP`（C2）；测试用 in-memory `Client`，删 `test_client`；删 stdio fallback（C9）；前置 SSE spike。
> 6. **P1.2 路由 `lookup_by_mcp_session`** → 删除；改确定性 `_orca_run_id`/`_orca_node` tool-params 路由（item4 + D4，见 SPEC §5.3/§5.5）；前置补 phase 6 `registry.register(...)` 调用（B2）。
> 7. **P2.1 DialogHandler `run_dialog_turn`** → 不存在；统一 `run_dialog`（整体跑，内部循环），或拆 `start_dialog`/`send_turn`/`end_dialog` 三方法（二选一，测试B）。
> 8. **P2.2 测试 `outputs_acc` / `_reconstruct_outputs`** → 改断言 `replay_state(tape)` 结果（C8）。
> 9. **P2.3 validator SpawnConfig `cli_path="claude"`** → `cli_path=profile.resolve_cli_path()`（C5，见 SPEC §9.6.4）；**P3.2 测试 `bus._interrupt_targets`** → 删，改 `register_wait_handle`/`notify_all_waits` 行为测试（B1，见 SPEC §9.7.6）。
>
> **执行顺序（决策 D1=wave）**：第一波 = CI(P0.1) + Interrupt UI/Guidance(P1.1) + Resume(P2.2)（地基稳）；第二波 = Retry(P0.2) + ask_user(P1.2)（前置 spike + error_type 对齐）；第三波 = Validator(P2.1) + Dialog(P2.2) + Wait(P3.1)；第四波 = daemon(P3.2，不含 attach) + Skip(P4)。**Budget 不做（D3，SPEC §12）；attach 不做（D2）。**

## 目标

在 CLI 路径下补齐 Conductor 已有的 6 类核心 feature，**保留 Orca 单 Tape + 纯 reducer + 单向依赖架构**。完整 SPEC 见 [`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md)。

## 依据

- SPEC：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md)
- 决策记录：[`docs/releases/2026-07-01-e2e-mxint-bugfix.md`](../releases/2026-07-01-e2e-mxint-bugfix.md)（mxint 实测发现 3 bug）
- 参考实现：`/tmp/conductor/src/conductor/`

## 锁定的决策（不再讨论）

1. **保持 `claude -p` CLI 子进程路线，不切 SDK**（SPEC §1.1 论证）
2. **Web feature 推迟**（本 phase 只动 CLI + 后端）
3. **每个 feature 完成后实跑 `orca run` 验证**（学 mxint 教训）
4. **每个 feature 单独 commit + release note**（按 Orca 项目规矩）

## 总体路线图

```
Week 1-2:  P0（CI + 优雅中断 UI）
Week 3-4:  P1（guidance 注入 + ask_user 挂载）
Week 5-7:  P2（Dialog + Checkpoint Resume）
Week 8+:   P3-P4（daemon + Skip，可滚动）
```

---

# P0：必做、立即可做（1-3 天）

## P0.1 CI（GitHub Actions）

### 文件清单

- 新增 `.github/workflows/test.yml`
- 新增 `.github/workflows/integration.yml`（标 integration，PR 触发跑真 claude）

### 实施

#### `.github/workflows/test.yml`

**做什么**：每次 push / PR 自动跑 `pytest tests/ -m "not integration"`，绿才允许合并。

**结构**：
- on: push (master), pull_request (master)
- jobs.test: matrix Python 3.10 / 3.11 / 3.12，runs-on ubuntu-latest
  - steps: checkout / setup-python / install uv / uv sync / uv run pytest
- jobs.lint（可选）: ruff check（不强求，先跑通 test）

#### `.github/workflows/integration.yml`

**做什么**：标 `[integration]` 的 PR comment 触发，跑真 claude E2E。

**结构**：
- on: issue_comment（PR comment 含 `/integration`）
- jobs: require PR author 有 write 权限
- secrets: `ANTHROPIC_API_KEY`（仓库 secret 配置）
- 跑 `tests/iface/cli/test_integration.py`（取消 skip 标记）

### 验收

- [ ] push 后 GitHub Actions 自动触发
- [ ] matrix Python 3.10/3.11/3.12 全绿
- [ ] `tests/iface/cli/test_integration.py` 5 个 skip 在 integration workflow 跑通（需 PR comment）
- [ ] CI badge 加到 README.md
- [ ] 任何 push 破坏测试立即红

### 测试用例

CI 不需要单测，但需要：
- 故意改一个测试为失败，push，确认 CI 红（人工验证）
- 改回正确，push，确认 CI 绿

### 偏离 SPEC 处

无。

---

## P0.2 优雅中断 UI（CONTINUE / SKIP / ABORT）

### 文件清单

- 新增 `orca/gates/interrupt.py`（InterruptHandler + InterruptRequest）
- 新增 `orca/gates/_broadcaster_mixin.py`（共享 broadcaster pattern）
- 新增 `orca/iface/cli/screens/interrupt_modal.py`（InterruptModal ModalScreen）
- 修改 `orca/schema/event.py`（加 `interrupt_requested` / `interrupt_resolved` 事件）
- 修改 `orca/exec/context.py`（RunContext 加 `user_guidance` / `interrupt_history` 字段 + `with_guidance` 方法）
- 修改 `orca/iface/cli/app.py`（绑定 Ctrl+G + 注册 modal + 接 InterruptHandler）
- 修改 `orca/iface/cli/widgets/log_stream.py`（format_event 加 interrupt_* 描述）
- 修改 `orca/gates/handler.py`（继承 _broadcaster_mixin，DRY）

### 实施

#### 步骤 1：抽 `_broadcaster_mixin.py`

**做什么**：把 HumanGateHandler 的 `start` / `stop` / `_broadcaster` 三方法抽成 mixin。

**为什么**：InterruptHandler 和 HumanGateHandler 共享同样的「asyncio.Future + queue + threading.Lock」pattern，DRY。

**接口**：

```python
class BroadcasterMixin:
    """共享的 broadcaster pattern：asyncio.Queue + 后台 task + start/stop 生命周期。

    子类必须实现 `_make_resolved_payload(self, ...)` —— broadcaster 出队时调，
    构造要广播的事件 payload。
    """
    _bus: EventBus
    _resolved_queue: asyncio.Queue | None
    _broadcaster_task: asyncio.Task | None

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def _broadcaster(self) -> None: ...
```

**HumanGateHandler 改造**：继承 mixin，删重复代码。

#### 步骤 2：写 `interrupt.py`

**做什么**：实现 InterruptHandler + InterruptRequest。

**接口**：

```python
class InterruptRequest(BaseModel):
    id: str
    node: str
    run_id: str
    session_id: str | None
    source: Literal["cli", "web", "mcp"] = "cli"
    elapsed_at_request: float
    context: dict = {}

class InterruptHandler(BroadcasterMixin):
    """累积中断请求 + 等用户响应 + 广播 resolved。"""

    def __init__(self, bus: EventBus):
        self._bus = bus
        self._pending: dict[str, asyncio.Future[tuple[str, str | None]]] = {}
        self._lock = asyncio.Lock()
        self._resolve_lock = threading.Lock()
        self._resolved_queue = None
        self._broadcaster_task = None

    async def request(self, ireq: InterruptRequest) -> tuple[str, str | None]:
        """emit interrupt_requested + 等用户答 (action, guidance)。"""

    def resolve(self, interrupt_id: str, action: str, guidance: str | None, source: str) -> bool:
        """用户答 → set_result(future) + 入 broadcaster queue。first-wins。"""
```

#### 步骤 3：写 `interrupt_modal.py`

**做什么**：Textual ModalScreen 弹中断 UI。

**接口**：

```python
class InterruptModal(ModalScreen[tuple[str, str | None]]):
    """返回 (action, guidance)。action: continue|skip|abort。"""

    def __init__(self, ireq: InterruptRequest, node_prompt_preview: str): ...

    def compose(self) -> ComposeResult:
        # 标题：⏸ INTERRUPT · node=X
        # 当前 node 已耗时 + 进度提示
        # textarea: guidance 输入（可选）
        # 3 按钮：CONTINUE / SKIP / ABORT
        ...

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = {"gate-continue": "continue", "gate-skip": "skip", "gate-abort": "abort"}[event.button.id]
        guidance = self.query_one("#guidance-input", TextArea).text or None
        self.dismiss((action, guidance))
```

#### 步骤 4：OrcaApp 集成

**做什么**：

1. `__init__` 加 `self.interrupt_handler = InterruptHandler(self.bus)`
2. `BINDINGS` 加 `Binding("ctrl+g", "interrupt", "中断/纠偏")`
3. `action_interrupt()` 方法：构造 InterruptRequest → push InterruptModal → await → handler.resolve
4. `on_mount` 末尾 `self._consume_events` 订阅 interrupt_* 事件
5. `_dispatch_to_widgets` 加 `interrupt_requested` / `interrupt_resolved` 分支

#### 步骤 5：format_event 扩展

```python
def _describe(event_type, data):
    ...
    if event_type == "interrupt_requested":
        return f"⏸ interrupt requested at {data.get('node', '?')} ({data.get('elapsed_at_request', 0):.1f}s)"
    if event_type == "interrupt_resolved":
        action = data.get("action", "?")
        guidance = data.get("guidance")
        text = f"interrupt {action}"
        if guidance:
            text += f": {guidance[:50]}"
        return text
```

#### 步骤 6：RunContext 加字段（不动 guidance 注入，那是 P1）

```python
@dataclass(frozen=True)
class RunContext:
    ...
    user_guidance: tuple[str, ...] = ()       # P1 才用，但字段先加
    interrupt_history: tuple[dict, ...] = ()  # 历次中断记录

    def with_guidance(self, text: str) -> "RunContext":
        return dataclasses.replace(self, user_guidance=self.user_guidance + (text,))
```

### 验收

#### 白盒测试（tests/gates/test_interrupt.py）

- [ ] `test_interrupt_handler_request_blocks_until_resolved`：调 request → 不返回 → 调 resolve → 返回 (action, guidance)
- [ ] `test_interrupt_handler_first_wins`：双 source 同时 resolve → 只第一个赢，第二个返回 False
- [ ] `test_interrupt_handler_broadcaster_emits_resolved`：resolve 后 broadcaster emit `interrupt_resolved` 写 Tape
- [ ] `test_interrupt_handler_idempotent_start_stop`：重复 start / 重复 stop 不报错
- [ ] `test_interrupt_handler_thread_safety`：从多线程并发 resolve 同 interrupt_id → 只一个 True

#### 黑盒测试（tests/iface/cli/test_interrupt_modal.py）

- [ ] `test_modal_compose_three_buttons`：CONTINUE / SKIP / ABORT 三按钮显示
- [ ] `test_modal_continue_without_guidance`：点 CONTINUE（textarea 空）→ dismiss ("continue", None)
- [ ] `test_modal_continue_with_guidance`：输入 guidance + CONTINUE → dismiss ("continue", "用更保守的方案")
- [ ] `test_modal_skip`：点 SKIP → dismiss ("skip", None)
- [ ] `test_modal_abort`：点 ABORT → dismiss ("abort", None)
- [ ] `test_modal_escape_cancels`：按 Esc → 等价 ABORT（dismiss ("abort", None)）

#### 集成测试（tests/iface/cli/test_app.py 加用例）

- [ ] `test_ctrl_g_triggers_interrupt_modal`：用 pilot 按 Ctrl+G → InterruptModal 在屏
- [ ] `test_app_handles_interrupt_resolved_event`：注入 fake interrupt_resolved 事件 → LogStream 显示描述 + DagTree 不受影响
- [ ] `test_app_interrupt_handler_started_on_mount`：on_mount 后 interrupt_handler.is_running() True

#### E2E 实跑

- [ ] 跑 `orca run examples/demo_task.yaml`，跑到 agent 节点时按 Ctrl+G → modal 弹出 → CONTINUE → workflow 继续 exit 0
- [ ] 跑 `orca run examples/demo_linear.yaml`（全 script，无 agent），按 Ctrl+G → modal 弹出 → ABORT → workflow 中止 exit 1
- [ ] tape 含 interrupt_requested + interrupt_resolved 事件 + 正确 payload

### 测试用例（不写代码，只列断言）

```python
# 白盒
def test_interrupt_handler_first_wins():
    bus, _ = make_bus(tmp_path)
    handler = InterruptHandler(bus)
    await handler.start()
    ireq = InterruptRequest(id="i1", node="cfg", run_id="r1", ...)
    task = asyncio.create_task(handler.request(ireq))
    await asyncio.sleep(0.01)  # 让 task 进入 await
    ok1 = handler.resolve("i1", "continue", "g1", "cli")
    ok2 = handler.resolve("i1", "continue", "g2", "web")  # 第二个 source
    assert ok1 is True
    assert ok2 is False  # first-wins
    action, guidance = await task
    assert action == "continue"
    assert guidance == "g1"

# 黑盒
def test_modal_continue_with_guidance():
    ireq = InterruptRequest(id="i1", node="cfg", run_id="r1", ...)
    modal = InterruptModal(ireq, node_prompt_preview="...")
    async def scenario():
        async with modal.run_test() as pilot:
            await pilot.click("#guidance-input")
            await pilot.press("用更保守的方案")
            await pilot.click("#gate-continue")
        result = modal._dismiss_value
        assert result == ("continue", "用更保守的方案")
```

### 偏离 SPEC 处

无（按 SPEC §3 逐字实现）。

### 风险/疑问

- Q: Ctrl+G 与 Textual 默认 binding 冲突？
  - 查证：Textual Ctrl+G 默认无 binding，安全。
- Q: agent 跑到一半按 Ctrl+G，agent 是否中断？
  - 答：P0 不实现「杀当前 claude -p」（那是 P1 guidance 注入），P0 只实现「等当前 node 完成后在边界生效」。InterruptModal 显示「等当前 node 完成后生效」。

---

# P1：核心体验（1-2 周）

## P1.1 mid-run Guidance 注入

### 文件清单

- 修改 `orca/exec/render.py`（render_prompt 拼 guidance section）
- 修改 `orca/exec/context.py`（加 `guidance_prompt_section` 方法）
- 修改 `orca/run/orchestrator.py`（_drive_loop 加 interrupt 检查 + 重 spawn 逻辑）
- 修改 `orca/exec/runner.py`（CLIRunner 加 `send_sigint` 方法）
- 修改 `orca/exec/claude/executor.py`（接 SIGINT 不当错误，标记 interrupted）

### 实施

#### 步骤 1：render_prompt 拼 guidance

**做什么**：agent spawn 时 prompt 末尾拼 `[User Guidance]` 段。

**接口**：

```python
# orca/exec/render.py
def render_prompt(node, ctx: RunContext) -> str:
    base = ...  # 既有逻辑
    section = ctx.guidance_prompt_section()
    return base + section if section else base

# orca/exec/context.py
def guidance_prompt_section(self) -> str | None:
    if not self.user_guidance:
        return None
    entries = "\n".join(f"- {g}" for g in self.user_guidance)
    return (
        "\n\n[User Guidance]\n"
        "The following guidance was provided by the user during workflow execution. "
        "Incorporate this guidance into your response:\n"
        f"{entries}"
    )
```

#### 步骤 2：CLIRunner 加 send_sigint

**做什么**：用户 Ctrl+G + CONTINUE 后，杀当前 claude -p。

**接口**：

```python
# orca/exec/runner.py
class CLIRunner:
    ...
    def send_sigint(self) -> None:
        """SIGINT 让 claude 优雅退出（写最后的 stream-json result 行）。"""
        if self._proc and self._proc.returncode is None:
            self._proc.send_signal(signal.SIGINT)
```

**ClaudeExecutor 处理**：接 SIGINT 后，子进程 exit_code 可能 != 0，但这是用户主动中断，**不当 error**：

```python
# orca/exec/claude/executor.py
if runner.exit_code != 0 and not runner.was_interrupted:
    raise ExecError(phase="spawn", ...)
# was_interrupted = True：跳过错误，返回 None 让 orchestrator 决定下一步
```

#### 步骤 3：Orchestrator._drive_loop 加 interrupt 检查

**做什么**：node 边界查 `_interrupt_pending`。

**接口**：

```python
class Orchestrator:
    def __init__(self, ...):
        ...
        self._interrupt_handler = interrupt_handler
        self._interrupt_pending: InterruptRequest | None = None
        self._current_runner: CLIRunner | None = None  # 给 send_sigint 用

    async def _drive_loop(self):
        current = self.wf.entry
        outputs_acc = {}
        ctx = self.ctx
        while current != "$end":
            iterations += 1
            if iterations > self.max_iter:
                raise MaxIterationsError(...)
            step_ctx = ctx.with_outputs(outputs_acc)  # 改：用 with_outputs 派生新 ctx

            # ── phase 11：node 边界检查 interrupt ──────────────
            if self._interrupt_pending:
                action, new_ctx = await self._handle_interrupt(current, step_ctx)
                if action == "abort":
                    raise WorkflowAborted(current)
                if action == "skip":
                    outputs_acc[current] = {"output": None, "skipped": True}
                    current = self._next_node(current, outputs_acc)
                    continue
                # continue: ctx 已更新（含 guidance）
                ctx = new_ctx
                step_ctx = ctx.with_outputs(outputs_acc)
            # ──────────────────────────────────────────────

            raw_output = await self._dispatch(current, step_ctx)
            outputs_acc[current] = {"output": raw_output}
            current = self._next_node(current, outputs_acc)
```

#### 步骤 4：_handle_interrupt 实现

```python
async def _handle_interrupt(self, current: str, ctx: RunContext) -> tuple[str, RunContext]:
    ireq = self._interrupt_pending
    self._interrupt_pending = None  # 消费掉

    if self._current_runner:
        self._current_runner.send_sigint()
        # 等子进程退出（让 stream-json 写完最后的 result 行）
        await self._current_runner.wait_for_exit(timeout=5.0)

    action, guidance = await self._interrupt_handler.request(ireq)

    if action == "continue" and guidance:
        ctx = ctx.with_guidance(guidance)
    return action, ctx
```

#### 步骤 5：触发 interrupt（CLI App 层）

```python
# orca/iface/cli/app.py
async def action_interrupt(self):
    """Ctrl+G 触发。"""
    if not self._current_node or not self._current_runner:
        # 不在 agent node 中，无法 interrupt
        self.query_one(LogStream).write("(不在 agent node 中，无法中断)")
        return
    ireq = InterruptRequest(
        id=uuid4().hex,
        node=self._current_node,
        run_id=self.run_id,
        session_id=self._current_session_id,
        elapsed_at_request=time.time() - self._node_started_at,
    )
    # 推 InterruptModal（push_screen_wait 阻塞本 worker，UI 继续刷新）
    modal = InterruptModal(ireq, node_prompt_preview=self._current_prompt[:200])
    result = await self.push_screen_wait(modal)
    action, guidance = result
    # 把 ireq 标记 pending + 解析答案
    self._orchestrator_proxy.set_interrupt_pending(ireq)
    self.interrupt_handler.resolve(ireq.id, action, guidance, "cli")
```

### 验收

#### 白盒测试

- [ ] `test_render_prompt_appends_guidance_section`：ctx 含 guidance → 渲染后 prompt 末尾有 `[User Guidance]`
- [ ] `test_render_prompt_empty_guidance_no_section`：ctx 无 guidance → 渲染后无 section
- [ ] `test_context_with_guidance_immutable`：with_guidance 返回新实例，原 ctx 不变
- [ ] `test_clirunner_send_sigint_terminates_subprocess`：调 send_sigint → 子进程 exit
- [ ] `test_claude_executor_treats_sigint_as_interrupted_not_error`：runner.was_interrupted=True → executor 不 raise ExecError
- [ ] `test_orchestrator_handle_interrupt_continue_accumulates_guidance`：mock interrupt_handler 返回 ("continue", "g1") → ctx.user_guidance 含 "g1"
- [ ] `test_orchestrator_handle_interrupt_abort_raises`：返回 ("abort", None) → raise WorkflowAborted
- [ ] `test_orchestrator_handle_interrupt_skip_advances_to_next_node`：返回 ("skip", None) → 当前 node 标 skipped，next node 求值

#### 集成测试

- [ ] `test_workflow_aborted_event_emitted_on_abort`：abort → emit `workflow_failed` 含 `error_type="WorkflowAborted"`
- [ ] `test_orchestrator_with_guidance_renders_in_respaned_agent`：用 fake executor，断言重 spawn 时 prompt 含 guidance section

#### E2E 实跑

- [ ] `orca run examples/mxint_analysis.yaml` 跑到 configurator 时按 Ctrl+G + guidance「skip weights」+ CONTINUE
  - configurator 重 spawn
  - 重 spawn 后的 prompt 末尾含 `[User Guidance]\n- skip weights`
  - agent output 反映 guidance（如 cfg 摘要含「按用户指示 skip weights」）
- [ ] tape 中 `interrupt_requested` + `interrupt_resolved` 配对，含正确 guidance payload
- [ ] workflow 最终 exit 0
- [ ] 跑 `examples/demo_linear.yaml`（全 script）按 Ctrl+G + ABORT → workflow 立即中止 exit 1

### 测试用例

```python
# render
def test_render_prompt_appends_guidance_section():
    ctx = RunContext(inputs={}, outputs={}, run_id="r", user_guidance=("保守点", "用 CPU"))
    node = AgentNode(name="x", prompt="任务 X")
    rendered = render_prompt(node, ctx)
    assert "[User Guidance]" in rendered
    assert "- 保守点" in rendered
    assert "- 用 CPU" in rendered

# orchestrator
def test_orchestrator_handle_interrupt_continue_accumulates_guidance():
    orch = Orchestrator(wf=..., bus=..., interrupt_handler=mock_handler)
    mock_handler.request = AsyncMock(return_value=("continue", "g1"))
    orch._interrupt_pending = InterruptRequest(...)
    action, new_ctx = await orch._handle_interrupt("cfg", ctx)
    assert action == "continue"
    assert new_ctx.user_guidance == ("g1",)
```

### 偏离 SPEC 处

无。

### 风险/疑问

- Q: SIGINT 后 claude 是否一定能写最后的 result 行？
  - 实测：claude -p 收到 SIGINT 会优雅退出，写 `result` 类型行后关闭 stream。若不行则 fallback SIGTERM。
- Q: 多个 agent 并行（parallel group）时 Ctrl+G 中断哪个？
  - 答：phase 11 P1 简化 —— Ctrl+G 触发 workflow 级 interrupt，所有并行 branch 都被 interrupt（杀全部 runner）。精细化「中断指定 branch」留后续。

---

## P1.2 ask_user 工具挂载

### 文件清单

- 新增 `orca/exec/mcp_tools/__init__.py`
- 新增 `orca/exec/mcp_tools/server.py`（AgentToolsMcpServer）
- 修改 `orca/exec/claude/executor.py`（_build_spawn_config 填 mcp_flag_args）
- 修改 `orca/gates/context_registry.py`（加 `lookup_by_mcp_session`）
- 修改 `orca/run/orchestrator.py`（构造时启动 AgentToolsMcpServer）

### 实施

#### 步骤 1：AgentToolsMcpServer 类

**接口**：

```python
# orca/exec/mcp_tools/server.py
class AgentToolsMcpServer:
    """内嵌 socket MCP server，暴露 ask_user 给被 orca 编排的 claude -p。

    与 phase 10 OrcaMcpServer 的边界：
    - phase 10 OrcaMcpServer（stdio）：给外部 CC 主对话用，暴露 start_workflow 等
    - 本类（socket SSE）：给 orca 内部 spawn 的 claude 用，暴露 ask_user 等
    """

    def __init__(self, handler: HumanGateHandler, registry: SessionContextRegistry):
        self._handler = handler
        self._registry = registry
        self._mcp = FastMCP("orca-agent-tools")
        self._server_task: asyncio.Task | None = None
        self._port: int | None = None

    async def start(self) -> int:
        """懒启动 socket MCP server，返回 port。"""
        # 找空闲 port，启动 FastMCP SSE server
        ...

    async def stop(self) -> None:
        """关闭 server。幂等。"""
        ...

    def write_config(self, session_id: str, run_id: str, node: str) -> Path:
        """写 mcp config JSON 到 runs/<run_id>/mcp_<session>.json。"""
        config = {
            "mcpServers": {
                "orca-agent-tools": {
                    "type": "sse",
                    "url": f"http://127.0.0.1:{self._port}/sse",
                }
            }
        }
        path = Path("runs") / run_id / f"mcp_{session_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config))
        return path

    def register_session(self, mcp_session_id: str, run_id: str, node: str) -> None:
        """MCP client 连接时调，建立 session 路由。"""
        self._registry.register(mcp_session_id, run_id, node)

    def unregister_session(self, mcp_session_id: str) -> None: ...

    @property
    def port(self) -> int | None: ...

    @property
    def url(self) -> str | None: ...
```

#### 步骤 2：注册 ask_user 工具

```python
def _register_tools(self):
    @self._mcp.tool()
    async def ask_user(
        prompt: str,
        options: list[str] | None = None,
    ) -> str:
        """Ask the user a question. Blocks until user answers.

        Args:
            prompt: Question for the user.
            options: Fixed choices, or None for free text.
        Returns:
            User's answer (option text or free text).
        """
        # MCP 工具调用时，本协程跑在 MCP server 的 event loop
        # 通过 mcp_session_id 反查 (run_id, node)
        mcp_session = current_mcp_session()  # FastMCP 提供
        ctx = self._registry.lookup_by_mcp_session(mcp_session)
        if ctx is None:
            raise RuntimeError("MCP session 未注册")

        answer = await ask_user(
            handler=self._handler,
            prompt=prompt,
            options=options,
            run_id=ctx.run_id,
            node=ctx.node,
            session_id=mcp_session,
        )
        return answer

    self._mcp.add_tool(ask_user)
```

#### 步骤 3：ClaudeExecutor 集成

```python
# orca/exec/claude/executor.py
class ClaudeExecutor(Executor):
    def __init__(self, profile: CliProfile, agent_tools_server: AgentToolsMcpServer | None = None):
        self.profile = profile
        self._agent_tools_server = agent_tools_server

    async def exec(self, node, ctx) -> AsyncIterator[Event]:
        ...
        # spawn 前 register MCP session（用 claude session_id 当 key）
        if self._agent_tools_server is not None:
            self._agent_tools_server.register_session(session_id, ctx.run_id, node.name)

        cfg = self._build_spawn_config(node, self.profile, prompt, ctx, session_id)
        ...

    def _build_spawn_config(self, node, profile, prompt, ctx, session_id):
        ...
        mcp_flag_args = []
        if self._agent_tools_server is not None:
            config_path = self._agent_tools_server.write_config(
                session_id=session_id, run_id=ctx.run_id, node=node.name,
            )
            mcp_flag_args = ["--mcp-config", str(config_path)]
        return SpawnConfig(..., mcp_flag_args=mcp_flag_args, ...)
```

#### 步骤 4：Orchestrator 启动 server

```python
class Orchestrator:
    def __init__(self, ..., agent_tools_server: AgentToolsMcpServer | None = None):
        ...
        self._agent_tools_server = agent_tools_server

    async def run(self):
        if self._agent_tools_server is not None:
            await self._agent_tools_server.start()
        try:
            ...
        finally:
            if self._agent_tools_server is not None:
                await self._agent_tools_server.stop()
```

#### 步骤 5：CLI App 装配

```python
# orca/iface/cli/app.py
class OrcaApp:
    def __init__(self, ...):
        ...
        self.agent_tools_server = AgentToolsMcpServer(self.gate_handler, self.session_registry)
        # 传给 Orchestrator 构造
```

#### 步骤 6：session_id 路由扩展

```python
# orca/gates/context_registry.py
class SessionContextRegistry:
    ...
    def lookup_by_mcp_session(self, mcp_session_id: str) -> RunContext | None:
        """MCP server 收到 ask_user 调用时反查 (run_id, node)。

        复用既有 _map（key = session_id），MCP session 与 claude session_id 一致
        （ClaudeExecutor spawn 时 register 用同一 key）。
        """
        return self.lookup(mcp_session_id)
```

### 验收

#### 白盒测试（tests/exec/mcp_tools/test_server.py）

- [ ] `test_server_starts_on_free_port`：调 start → port 非 None + 在 1024-65535 范围
- [ ] `test_server_stop_idempotent`：连续 stop 两次不报错
- [ ] `test_write_config_creates_json`：write_config → 文件存在 + JSON 合法 + 含正确 url
- [ ] `test_register_and_lookup_session`：register → lookup 返回正确 ctx；unregister 后 lookup 返回 None
- [ ] `test_ask_user_tool_calls_handler_request`：mock handler → 调 ask_user 工具 → handler.request 被调

#### 集成测试

- [ ] `test_claude_executor_passes_mcp_config_to_subprocess`：spawn 时 argv 含 `--mcp-config <path>`
- [ ] `test_orchestrator_starts_stops_agent_tools_server`：run 开始 server 起来；结束 server 关闭
- [ ] `test_orchestrator_no_agent_tools_server_when_disabled`：传 None → argv 不含 --mcp-config

#### E2E 实跑

- [ ] 新建 `examples/with_ask_user.yaml`：单 agent，prompt 含「调 ask_user 问用户名」
- [ ] `orca run examples/with_ask_user.yaml` → claude 调 ask_user → CLI 弹 AskGate → 用户答 "Alice" → agent 收到 "Alice" 继续
- [ ] tape 含 human_decision_requested（source=agent_ask）+ human_decision_resolved 配对

### 测试用例

```python
def test_ask_user_tool_calls_handler_request():
    handler = MockHumanGateHandler()
    handler.request = AsyncMock(return_value=("Alice", "cli"))
    registry = SessionContextRegistry()
    registry.register("sess-1", "run-1", "node-1")
    server = AgentToolsMcpServer(handler, registry)
    await server.start()
    try:
        # 模拟 MCP client 调 ask_user 工具
        async with server.test_client(session="sess-1") as client:
            result = await client.call_tool("ask_user", {"prompt": "What's your name?"})
        assert result == "Alice"
        handler.request.assert_called_once()
    finally:
        await server.stop()
```

### 偏离 SPEC 处

- 若 FastMCP 不支持「SSE server 监听指定 port + 自定义 session 提取」—— fallback 用 stdio（每 claude 一个独立 MCP server 子进程，通过 HTTP 回调 orca 主进程）。SPEC §5.1 已说这是备选形态 B。

### 风险/疑问

- Q: FastMCP 的 SSE server 是否支持监听自定义 port？
  - 查证：FastMCP 1.0+ 支持 SSE transport（`fastmcp.run(transport="sse")`），可指定 host/port。
- Q: claude -p 通过 `--mcp-config` 加载 SSE MCP server 是否需要特殊 flag？
  - 实测：claude --mcp-config 接受 stdio / SSE / HTTP 三种 transport。
- Q: MCP session 与 claude session_id 是否同源？
  - 答：是。ClaudeExecutor spawn claude 时拿到的 claude session_id（从 system/init 事件），同时用作 MCP session key 注册到 registry。MCP 调 ask_user 时，FastMCP 提供的 session 标识与 claude session_id 一致（因为同一 claude 进程）。

---

# P2：高级 feature（3-5 周，可滚动）

## P2.1 Dialog（agent 跑完后多轮聊）

### 文件清单

- 新增 `orca/gates/dialog.py`（DialogHandler）
- 新增 `orca/iface/cli/screens/dialog_modal.py`（DialogModal）
- 修改 `orca/schema/event.py`（加 `dialog_started` / `dialog_message` / `dialog_ended`）
- 修改 `orca/exec/context.py`（RunContext 加 `dialog_history` 字段）
- 修改 `orca/iface/cli/app.py`（绑定 d 键 + 注册 DialogModal）
- 修改 `orca/iface/cli/widgets/log_stream.py`（format_event 加 dialog_* 描述）

### 实施

#### DialogHandler 接口

```python
class DialogHandler:
    """agent 跑完后多轮对话（重 spawn + 拼历史）。"""

    def __init__(self, executor_factory):
        self._executor_factory = executor_factory

    async def run_dialog(
        self,
        node: str,
        agent_output: Any,
        ctx: RunContext,
        bus: EventBus,
    ) -> dict:
        """重 spawn claude 进入对话模式，返回 {turns, conclusion}。"""
        # 1. 构造 system prompt（参考 Conductor DIALOG_AGENT_SYSTEM_PROMPT）
        # 2. 重 spawn claude（临时 AgentNode）
        # 3. 进入循环：
        #    - emit dialog_started
        #    - 用户输入 → emit dialog_message(role=user)
        #    - claude 回 → emit dialog_message(role=agent)
        #    - 直到用户「结束」
        # 4. emit dialog_ended
        # 5. 返回历史
```

#### DialogModal 接口

```python
class DialogModal(ModalScreen[None]):
    """多轮对话模态。"""
    def __init__(self, handler: DialogHandler, node: str, agent_output: Any, ctx: RunContext): ...

    def compose(self):
        # 显示 agent output 摘要 + 历史对话
        # input 输入框
        # 「发送」/「结束对话」按钮
        ...

    async def on_button_pressed(self, event):
        if event.button.id == "send":
            user_text = self.query_one("#dialog-input", Input).value
            self.query_one("#dialog-history").write(f"user> {user_text}\n")
            # 触发 handler.run_dialog_turn(user_text) → 异步拿 agent reply
            agent_reply = await self._handler.run_dialog_turn(user_text)
            self.query_one("#dialog-history").write(f"agent> {agent_reply}\n")
        elif event.button.id == "end":
            self.dismiss()
```

### 验收

#### 白盒

- [ ] `test_dialog_handler_constructs_system_prompt`：调 run_dialog → system prompt 含 agent_output 摘要
- [ ] `test_dialog_handler_user_message_emits_event`：用户输入 → emit dialog_message(role=user)
- [ ] `test_dialog_handler_agent_reply_emits_event`：agent reply → emit dialog_message(role=agent)
- [ ] `test_dialog_handler_end_emits_dialog_ended`：用户结束 → emit dialog_ended

#### 黑盒

- [ ] `test_dialog_modal_displays_agent_output`：modal 打开 → 显示 agent output 摘要
- [ ] `test_dialog_modal_send_appends_to_history`：输入 + 发送 → 历史显示
- [ ] `test_dialog_modal_end_dismisses`：点结束 → dismiss

#### E2E

- [ ] 新建 `examples/with_dialog.yaml`：单 agent，跑完后按 d 进入对话
- [ ] tape 含 dialog_started + N×dialog_message + dialog_ended

### 风险

- Q: claude -p spawn 一次只能答一题（无 session）？
  - 答：每轮用户输入都重 spawn claude，prompt 拼历史。token 成本高但可用。spec §6 已说。

---

## P2.2 Checkpoint Resume（崩溃续跑）

### 文件清单

- 新增 `orca/run/resume.py`（resume 命令实现）
- 新增 `orca/iface/cli/commands.py::resume` 子命令（修改）
- 修改 `orca/run/orchestrator.py`（加 `from_tape` classmethod + `run_from_state`）
- 修改 `orca/schema/event.py`（加 `workflow_resumed`）

### 实施

#### resume 命令

```python
# orca/iface/cli/commands.py
@app.command()
def resume(
    tape_or_run_id: str = typer.Argument(..., help="Tape 文件路径或 run_id"),
) -> None:
    """从 Tape 重放恢复 workflow，从崩溃点继续跑。"""
    tape_path = _resolve_tape_path(tape_or_run_id)
    if not tape_path.is_file():
        typer.echo(f"Tape 不存在：{tape_path}", err=True)
        raise typer.Exit(code=2)

    asyncio.run(_resume_workflow(tape_path))

async def _resume_workflow(tape_path: Path):
    bus, tape = make_bus_from_path(tape_path)
    orch = Orchestrator.from_tape(tape_path, bus)
    await orch.run_from_state()
```

#### Orchestrator.from_tape

```python
@classmethod
def from_tape(cls, tape_path: Path, bus: EventBus) -> "Orchestrator":
    """从 Tape 重放构造 Orchestrator，恢复到崩溃前状态。"""
    tape = Tape(tape_path, run_id=...)
    state = replay_state(tape)

    if state.status == "completed":
        raise ValueError("workflow 已完成，无需 resume")

    # 找最后一个 node_completed
    last_node = _find_last_completed_node(tape)
    outputs_acc = _reconstruct_outputs(tape)
    ctx = RunContext(inputs=..., outputs=outputs_acc, run_id=...)

    orch = cls(wf=..., bus=bus, inputs=..., run_id=state.run_id, _initial_state=(last_node, ctx))
    return orch
```

### 验收

#### 白盒

- [ ] `test_from_tape_reconstructs_state`：构造 tape 含 N 个 node_completed → from_tape 后 outputs_acc 含 N 个
- [ ] `test_from_tape_completed_workflow_raises`：tape 含 workflow_completed → from_tape raise ValueError
- [ ] `test_from_tape_empty_tape_raises`：空 tape → raise ValueError
- [ ] `test_resume_emits_workflow_resumed`：调 run_from_state → emit workflow_resumed

#### E2E

- [ ] 跑 `orca run examples/mxint_analysis.yaml`，跑到 configurator 时 kill -9
- [ ] `orca resume runs/mxint_analysis-...jsonl` → 从 runner 继续
- [ ] tape 含 workflow_started（原）+ N events + workflow_resumed + 继续 events + workflow_completed
- [ ] 最终 outputs 完整

### 风险

- Q: workflow 跑到 parallel group 中间崩溃怎么办？
  - 答：phase 11 P2 简化 —— resume 不支持 parallel group 中断恢复（只支持 node 边界）。 crashed in middle of parallel group → 提示「无法 resume parallel group 中间」+ exit 1。

---

# P3-P4：长期方向（可滚动）

## P3 daemon / `--background`

### 文件清单

- 新增 `orca/iface/cli/bg_runner.py`（fork detached）
- 新增 `~/.orca/runs/<run_id>.json` 元数据格式
- 修改 `orca/iface/cli/commands.py`（加 `--background` flag + `ps`/`logs`/`attach`/`wait` 子命令）

### 简化版验收

- [ ] `--background` fork 子进程立即返回 run_id
- [ ] `ps` 列活跃 run
- [ ] `logs <run_id>` tail 日志
- [ ] `wait <run_id>` 阻塞等完成
- [ ] `attach <run_id>` **只读**模式（看 + 滚日志，不能交互）

读写 attach 留后续 phase（需 UDS 控制通道）。

## P4 Skip to Agent

详见 SPEC §9。验收简化：

- [ ] InterruptModal SKIP 按钮 → 弹 node 选择器 → 选目标 node → workflow 跳转

---

# 整体验收（phase 11 完成）

详见 SPEC §10.2。每条都要勾完。

---

# Review 补充（2026-07-01）：4 个 Conductor 遗漏 feature

深度 review Conductor 后发现 SPEC 初版遗漏 4 个重要 feature：Retry / Budget / Validator / Wait。用户讨论后决定 **Budget 不做**（推迟），保留 Retry / Validator / Wait 三个。已加到 SPEC §9.5/§9.6/§9.7，本节补充详细实施步骤与测试用例。

## P0.3 Retry Policy（review 补充）

### 文件清单

- 新增 `orca/run/retry.py`（execute_with_retry + RetryPolicy 应用）
- 新增 `orca/schema/workflow.py::RetryPolicy`（dataclass，frozen）
- 修改 `orca/schema/event.py`（加 retry_started / retry_succeeded / retry_exhausted）
- 修改 `orca/run/orchestrator.py::_dispatch`（接 retry 集成）
- 修改 `orca/iface/cli/widgets/log_stream.py`（format_event 加 retry_* 描述）

### 实施

#### 步骤 1：RetryPolicy dataclass

```python
class RetryPolicy(BaseModel):
    """节点级重试策略。"""
    max_attempts: int = 3
    backoff: Literal["constant", "linear", "exponential"] = "exponential"
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    retry_on: list[str] = ["spawn_error"]
    jitter: bool = True

    model_config = ConfigDict(extra="forbid")

class AgentNode(Node):
    ...
    retry: RetryPolicy | None = None
```

#### 步骤 2：execute_with_retry

```python
async def execute_with_retry(executor, node, ctx, bus) -> tuple[Any, list[Event]]:
    policy = node.retry
    all_events = []
    last_error: ExecError | None = None

    for attempt in range(1, policy.max_attempts + 1):
        attempt_events = []
        async for ev in executor.exec(node, ctx):
            attempt_events.append(ev)
            bus.emit(ev.type, ev.data)  # 流式 emit 不变
            all_events.append(ev)

        # 检查最后事件
        last = attempt_events[-1] if attempt_events else None
        if last and last.type == "node_completed":
            if attempt > 1:
                bus.emit("retry_succeeded", {"attempt_total": attempt, "node": node.name})
            return last.data["output"], all_events
        if last and last.type == "node_failed":
            err_type = last.data.get("error_type", "")
            if err_type not in policy.retry_on:
                break  # 不在白名单，不重试
            last_error = ExecError(phase=last.data.get("phase", "?"),
                                    message=last.data.get("message", ""))
            if attempt < policy.max_attempts:
                delay = _compute_delay(policy, attempt)
                bus.emit("retry_started", {
                    "attempt": attempt + 1, "max_attempts": policy.max_attempts,
                    "error_type": err_type, "delay_seconds": delay, "node": node.name,
                })
                await asyncio.sleep(delay)
                continue
        break

    # 重试用完仍失败
    if last_error:
        bus.emit("retry_exhausted", {
            "attempts": policy.max_attempts,
            "last_error_type": last_error.error_type, "node": node.name,
        })
        raise last_error
    raise ExecError(phase="?", message="retry: no result and no error")
```

#### 步骤 3：_compute_delay

```python
def _compute_delay(policy: RetryPolicy, attempt: int) -> float:
    """按 backoff 策略 + jitter 算 delay。"""
    if policy.backoff == "constant":
        delay = policy.initial_delay_seconds
    elif policy.backoff == "linear":
        delay = policy.initial_delay_seconds * attempt
    else:  # exponential
        delay = policy.initial_delay_seconds * (2 ** (attempt - 1))
    delay = min(delay, policy.max_delay_seconds)
    if policy.jitter:
        delay *= 0.8 + 0.4 * random.random()  # ±20%
    return delay
```

### 验收

#### 白盒（tests/run/test_retry.py）

- [ ] `test_retry_no_policy_no_retry`：retry=None → 失败立即 raise
- [ ] `test_retry_success_on_first_attempt_no_retry_event`：第一次成功 → 无 retry_* 事件
- [ ] `test_retry_success_on_second_attempt_emits_succeeded`：第二次成功 → emit retry_started + retry_succeeded
- [ ] `test_retry_exhausted_emits_exhausted_and_raises`：用完仍失败 → emit retry_exhausted + raise
- [ ] `test_retry_on_whitelist_filters_errors`：error_type 不在 retry_on → 不重试直接 raise
- [ ] `test_retry_backoff_constant`：3 次 attempt delay 都是 initial_delay_seconds
- [ ] `test_retry_backoff_exponential_caps_at_max_delay`：第 5 次 delay = max_delay_seconds（不无限增长）
- [ ] `test_retry_jitter_within_20_percent`：delay 在 [0.8x, 1.2x] 范围
- [ ] `test_retry_does_not_retry_interrupted`：was_interrupted=True → 不重试（用户主动中断）

#### E2E

- [ ] 新建 `examples/with_retry.yaml`：单 agent，retry.max_attempts=3，retry_on=[spawn_error]
- [ ] mock executor 第一次返 spawn_error、第二次返成功 → workflow exit 0，tape 含 retry_started + retry_succeeded
- [ ] mock executor 三次都返 spawn_error → workflow exit 1，tape 含 retry_started×2 + retry_exhausted

### 测试用例

```python
@pytest.mark.asyncio
async def test_retry_success_on_second_attempt_emits_succeeded(tmp_path):
    bus, _ = make_bus(tmp_path)
    executor = FakeExecutor([
        _fail("spawn_error", "first attempt failed"),
        _complete({"output": "ok"}),
    ])
    node = AgentNode(name="x", prompt="...", retry=RetryPolicy(max_attempts=3))
    output, events = await execute_with_retry(executor, node, ctx, bus)

    assert output == {"output": "ok"}
    types = [e.type for e in events]
    assert "retry_started" in types
    assert "retry_succeeded" in types
```

---

## P2.3 Semantic Output Validator（review 补充）

### 文件清单

- 新增 `orca/exec/validator.py`（SemanticValidator）
- 新增 `orca/schema/workflow.py::ValidatorConfig`
- 修改 `orca/schema/event.py`（加 validator_started / validator_passed / validator_failed）
- 修改 `orca/run/orchestrator.py::_dispatch`（agent 跑完后调 validator）
- 修改 `orca/iface/cli/widgets/log_stream.py`（format_event 加 validator_* 描述）

### 实施

#### 步骤 1：ValidatorConfig

```python
class ValidatorConfig(BaseModel):
    criteria: str
    max_retries: int = 1
    model: str | None = None
    model_config = ConfigDict(extra="forbid")

class AgentNode(Node):
    ...
    validator: ValidatorConfig | None = None
```

#### 步骤 2：validate_output 函数

```python
VALIDATOR_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["passed", "issues"],
}

async def validate_output(output, config: ValidatorConfig, node: str, bus: EventBus,
                          model: str | None = None) -> tuple[bool, list[str]]:
    """spawn 第二个 claude -p 做 LLM 校验。"""
    bus.emit("validator_started", {
        "node": node,
        "criteria_preview": config.criteria[:100],
    })

    prompt = f"""你是 output validator。判断以下 agent output 是否满足校验标准。

## Agent output
```json
{json.dumps(output, ensure_ascii=False, indent=2)}
```

## 校验标准
{config.criteria}

## 输出格式
返回 JSON：
{{
  "passed": true/false,
  "issues": ["问题1", "问题2"]  // passed=false 时必填
}}
"""
    # spawn claude -p（短 query，无 tools）
    cfg = SpawnConfig(
        cli_path="claude",
        flags=("-p", "--output-format", "stream-json", "--bare"),
        extra_args=["--allowed-tools", ""] if model is None else
                   ["--allowed-tools", "", "--model", model],
        prompt=prompt,
        prompt_channel="stdin",
        env_overlay=_claude_env_overlay(),
    )
    runner = CLIRunner(cfg, on_result=_capture_result)
    result_text = None
    async for line in runner.stream():
        # 流式丢弃（validator 不需要 token 级显示）
        pass
    result_text = runner.result_text

    try:
        verdict = extract_and_validate(result_text, VALIDATOR_OUTPUT_SCHEMA)
        passed = verdict["passed"]
        issues = verdict.get("issues", [])
    except ExecError as e:
        # validator LLM 自己崩 → 安全 fallback 当作 passed
        logger.warning(f"validator LLM failed: {e}. Treating as passed (fail-safe).")
        passed, issues = True, []

    if passed:
        bus.emit("validator_passed", {"node": node, "issues": []})
    else:
        bus.emit("validator_failed", {"node": node, "issues": issues, "retrying": False})
    return passed, issues
```

#### 步骤 3：Orchestrator 集成（与 retry 协同）

```python
async def _dispatch_with_validator(self, current, ctx, executor):
    node = self._node_by_name[current]
    if node.kind != "agent" or node.validator is None:
        return await execute_with_retry(executor, node, ctx, self.bus)

    # retry loop 内调 validator
    policy = node.retry or RetryPolicy(max_attempts=1)
    validator_attempts_left = node.validator.max_retries + 1
    total_attempts_left = policy.max_attempts

    last_output = None
    while total_attempts_left > 0 and validator_attempts_left > 0:
        output, _ = await execute_with_retry(executor, node, ctx, self.bus)
        last_output = output
        passed, issues = await validate_output(
            output, node.validator, current, self.bus, node.validator.model
        )
        if passed:
            return output
        # validator failed → retry agent（prompt 加 issues 反馈）
        ctx = ctx.with_guidance(f"上次输出未通过校验：{issues}")
        validator_attempts_left -= 1
        total_attempts_left -= 1

    raise ExecError(phase="validator",
                    message=f"validator exhausted: last issues={issues}")
```

### 验收

#### 白盒（tests/exec/test_validator.py）

- [ ] `test_validator_passed_no_retry`：LLM 返 passed=true → emit validator_passed + 不重试
- [ ] `test_validator_failed_with_retry`：LLM 返 passed=false + max_retries=1 → 第一次失败 + retry + 第二次成功
- [ ] `test_validator_failed_exhausted_raises`：retry 用完仍失败 → raise ExecError(phase="validator")
- [ ] `test_validator_llm_crash_treated_as_passed`：validator claude 自己崩 → 当作 passed（fail-safe）
- [ ] `test_validator_no_config_no_validation`：validator=None → 不调 validate_output

#### E2E

- [ ] 新建 `examples/with_validator.yaml`：agent + validator.criteria="model_class 必须是合法 Python 标识符"
- [ ] mock 第一次 agent output 不合 criteria → validator 失败 + retry → 第二次合规 → exit 0
- [ ] tape 含 validator_started + validator_failed(retrying=true) + validator_started + validator_passed

### 测试用例

```python
async def test_validator_failed_with_retry(tmp_path):
    bus, _ = make_bus(tmp_path)
    executor = FakeExecutor([
        _complete({"model_class": "123-invalid"}),  # 不合法标识符
        _complete({"model_class": "SimpleNet"}),    # 合法
    ])
    node = AgentNode(
        name="x", prompt="...",
        validator=ValidatorConfig(criteria="model_class 是合法 Python 标识符", max_retries=1),
    )
    output = await _dispatch_with_validator(node, ctx, bus, executor)
    assert output["model_class"] == "SimpleNet"
    types = [e.type for e in tape_events]
    assert types.count("validator_started") == 2
    assert "validator_failed" in types
    assert "validator_passed" in types
```

---

## P3.2 Wait Node（review 补充）

### 文件清单

- 新增 `orca/exec/wait.py`（WaitExecutor）
- 新增 `orca/schema/workflow.py::WaitNode`
- 修改 `orca/schema/event.py`（加 wait_started / wait_completed）
- 修改 `orca/exec/factory.py::make_executor`（分派 WaitNode → WaitExecutor）
- 修改 `orca/iface/cli/widgets/log_stream.py`（format_event 加 wait_* 描述）
- 修改 `orca/compile/parser.py`（YAML kind=wait 解析）

### 实施

#### 步骤 1：WaitNode schema

```python
class WaitNode(Node):
    kind: Literal["wait"] = "wait"
    duration: str                # Jinja2 渲染
    reason: str = ""
    interruptible: bool = True
    model_config = ConfigDict(extra="forbid")
```

**AnnotatedNode 联合**加 WaitNode：

```python
AnnotatedNode = Annotated[
    Union[AgentNode, ScriptNode, SetNode, ForeachNode, WaitNode],
    Field(discriminator="kind"),
]
```

#### 步骤 2：parse_duration

```python
def parse_duration(s: str) -> float:
    """'30s' → 30.0 / '5m' → 300.0 / '2h' → 7200.0 / '30' → 30.0。"""
    s = s.strip().lower()
    if not s:
        raise ValueError("empty duration")
    if s[-1] in "0123456789":
        return float(s)  # 纯数字 = 秒
    unit, mult = s[-1], None
    if s[-1] == "s": mult = 1
    elif s[-1] == "m": mult = 60
    elif s[-1] == "h": mult = 3600
    elif s[-1] == "d": mult = 86400
    else: raise ValueError(f"unknown duration unit: {s[-1]}")
    return float(s[:-1]) * mult
```

#### 步骤 3：WaitExecutor

```python
class WaitExecutor(Executor):
    MAX_DURATION = 24 * 60 * 60  # 24h 硬上限

    async def exec(self, node: WaitNode, ctx: RunContext) -> AsyncIterator[Event]:
        session_id = uuid.uuid4().hex
        yield _ev(node, "node_started", {"kind": "wait"}, session_id)

        duration_str = render_template(node.duration, ctx)
        try:
            duration = parse_duration(duration_str)
        except ValueError as e:
            yield _ev(node, "node_failed", {
                "error_type": "RenderError", "phase": "render",
                "message": f"invalid duration: {e}",
            }, session_id)
            return

        if duration > self.MAX_DURATION:
            yield _ev(node, "node_failed", {
                "error_type": "ConfigError", "phase": "config",
                "message": f"duration {duration}s exceeds max {self.MAX_DURATION}s",
            }, session_id)
            return

        yield _ev(node, "wait_started", {
            "duration_seconds": duration, "reason": node.reason,
        }, session_id)

        interrupted = False
        if node.interruptible:
            interrupt_evt = asyncio.Event()
            self._bus.register_interrupt_target(interrupt_evt)
            try:
                sleep_task = asyncio.create_task(asyncio.sleep(duration))
                int_task = asyncio.create_task(interrupt_evt.wait())
                done, pending = await asyncio.wait(
                    [sleep_task, int_task], return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending: t.cancel()
                interrupted = int_task in done
            finally:
                self._bus.unregister_interrupt_target(interrupt_evt)
        else:
            await asyncio.sleep(duration)

        yield _ev(node, "wait_completed", {
            "elapsed_seconds": duration, "interrupted": interrupted,
        }, session_id)
        yield _ev(node, "node_completed", {
            "output": {"interrupted": interrupted}, "elapsed": duration,
        }, session_id)
```

#### 步骤 4：factory 分派

```python
def make_executor(node: Node) -> Executor:
    if node.kind == "agent":
        return ClaudeExecutor(...)
    if node.kind == "script":
        return ScriptExecutor()
    if node.kind == "set":
        return SetExecutor()
    if node.kind == "foreach":
        return ForeachRunner(...)
    if node.kind == "wait":
        return WaitExecutor()
    raise ValueError(f"unknown kind: {node.kind}")
```

#### 步骤 5：compile parser

`orca/compile/parser.py` 加 wait kind 解析（YAML → WaitNode），与既有 kind 同 pattern。

### 验收

#### 白盒（tests/exec/test_wait.py）

- [ ] `test_parse_duration_units`：30s/5m/2h/1d/30 → 各自正确秒数
- [ ] `test_parse_duration_invalid_raises`：'abc' / '' / '30x' → raise
- [ ] `test_wait_executor_sleeps_for_duration`：duration=0.1s → elapsed ≈ 0.1s
- [ ] `test_wait_executor_interruptible_can_be_cancelled`：register interrupt_evt → set → wait 立即结束 interrupted=True
- [ ] `test_wait_executor_not_interruptible_waits_full`：interruptible=False → 即使 interrupt_evt set 也等完
- [ ] `test_wait_executor_exceeds_max_duration_fails`：duration=25h → node_failed
- [ ] `test_wait_executor_invalid_duration_fails`：duration='abc' → node_failed

#### E2E

- [ ] 新建 `examples/with_wait.yaml`：含 wait node duration=2s + 下游 node 引用 {{ wait_node.output.interrupted }}
- [ ] `orca run examples/with_wait.yaml` → workflow 跑 ~2s → 下游看到 interrupted=False
- [ ] 跑中按 Ctrl+G → wait node 立即结束（interrupted=True）→ 下游看到 interrupted=True

### 测试用例

```python
async def test_wait_executor_interruptible_can_be_cancelled():
    node = WaitNode(name="w", duration="10s", interruptible=True)
    executor = WaitExecutor()
    bus = EventBus(...)
    task = asyncio.create_task(_collect_events(executor.exec(node, ctx)))
    await asyncio.sleep(0.05)  # 让 wait_started emit
    # 模拟 interrupt
    for evt in bus._interrupt_targets:
        evt.set()
    events = await task
    types = [e.type for e in events]
    assert "wait_completed" in types
    completed = next(e for e in events if e.type == "wait_completed")
    assert completed.data["interrupted"] is True
```

---

# 风险与依赖（review 补充）

| 风险 | 影响 | 缓解 |
|---|---|---|
| Retry 与 Validator 协同复杂 | 重试逻辑交错 | SPEC §9.6.5 明确协同：validator 失败也算 retry 触发 |
| Wait node 与 interrupt 协同 | interrupt_evt 注册机制 | EventBus 加 register/unregister_interrupt_target API |
| Validator LLM 崩 | workflow 阻塞 | fail-safe：当作 passed（SPEC §9.6.6）|

| 风险 | 影响 | 缓解 |
|---|---|---|
| FastMCP SSE server 不支持自定义 port | ask_user 挂载受阻 | 备选形态 B（stdio MCP）|
| claude -p SIGINT 不优雅退出 | guidance 注入失败 | fallback SIGTERM + accept 部分数据丢失 |
| Textual Ctrl+G 与系统级快捷键冲突 | 中断 UI 触发不了 | 改用 Ctrl+Z 或自定义键 |
| Tape 重放 parallel group 状态丢失 | resume 不支持 parallel | SPEC 已限制范围 |

# 偏离 SPEC 处

（实现中如需偏离，在此记录 + 理由 + release note 同步）

# 风险/疑问

- Q: P0/P1 顺序能换吗？（先做 ask_user 再做中断 UI）
  - 答：可以，但建议按顺序。中断 UI 是 guidance 的前置（interrupt_handler 共享）。
- Q: P2 Dialog 一定要 phase 11 做吗？
  - 答：不必须。Dialog 可推迟。但 SPEC 里写好接口，未来做时不偏离。

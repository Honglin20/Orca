# 阶段 7 SPEC —— iface/cli CLI 壳（Textual TUI，第一个端到端入口）

> **状态**：最终版（待分发实现）
> **依据**：[shells-design-draft.md](shells-design-draft.md) §3 · [phase-6-gates.md](phase-6-gates.md) §2 §4 · [phase-5-run.md](phase-5-run.md) §4
> **范围**：`orca run <yaml>` CLI 入口 + Textual TUI（DAG 进度 + 日志流 + gate ModalScreen）
> **前置**：phase 5（Orchestrator）+ phase 6（gates）实现完成
> **里程碑**：phase 7 完成后 Orca 已是可用工具（单 backend + 单 shell + 完整 user journey）

---

## 0. 阶段目标

phase 7 回答唯一一个问题：**「用户在终端怎么跑一个 workflow、看进度、回答 gate？」**

这是 Orca 的第一个端到端入口——跑通后 `orca run nas.yaml` 就是一个可用的 CLI 工具。

| 模块 | 解决什么 | 核心交付 |
|---|---|---|
| CLI 入口 | `orca run/validate` 命令 | argparse/typer 命令绑定 + 参数解析（task/-i/--max-iter）|
| Textual App | 全屏 TUI 主循环 | App + 主 Screen + 事件订阅驱动渲染 |
| DAG Tree | 节点状态可视化 | 左侧 Tree widget，状态图标编码 |
| Active Node 详情 | 当前/选中节点的 agent 流 | 右上面板，行摘要 + 并行子 agent 进度 |
| RichLog 日志流 | 流式事件日志 | 右下 RichLog widget，自动滚动 |
| Gate ModalScreen | 阻塞式人工确认 | `push_screen_wait`，DAG 继续跑 |
| 壳的 resolve 路径 | CLI 答 gate → handler.resolve | 调 phase 6 HumanGateHandler |

**核心铁律**：CLI 壳**无业务真相**，只订阅 phase 5 EventBus 的事件流渲染。gate 答案调 phase 6 handler.resolve，不自己存状态。唯一真相是 tape。

---

## 1. 技术栈决策（2026-06-30 定稿：Textual）

**用 Textual，不是 Rich Live。** 理由（shells-design-draft §3.1 已锁定，硬证据）：

CLI 壳三件套需求——①DAG 节点状态面板 ②实时滚动日志流 ③**阻塞式 gate prompt**。Rich Live 能做 ①②，但 **③是 Rich 的硬限制**：Rich 官方确认 Live 渲染期间无法接收输入（[Discussion #1791](https://github.com/Textualize/rich/discussions/1791)）。

Textual 的 `ModalScreen` + `push_screen_wait` 原生支持「DAG 在跑 + 中央弹出 gate 模态 + 阻塞等答案 + 背景不冻结」。同作者（Will McGugan），基于 Rich，渲染同样漂亮。

---

## 2. 布局（融合 claude agent view + Dagger + lazygit）

### 2.1 主屏（DAG 在跑）

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Orca Run #42 · nas · sonnet · 3/7 nodes · ⏸ 2 awaiting gate                   │ Header
├────────────────┬─────────────────────────────────────────────────────────────┤
│ DAG OUTLINE    │ ACTIVE NODE: research                                        │
│ (左侧 Tree)     │ ───────────────────────────────────────────                  │
│ ✓ fetch        │ ┃ researcher_a  ✽ working 12s  [claude]                      │
│ ✓ parse        │ │  ⠋ searching "rich layout"                                 │
│ ◐ research     │ ┃ researcher_b  ✽ working 12s  [claude]                      │
│ ⏸ review       │ │  ✓ found 8 results                                          │
│ ○ test         │ ◐ parallel group: 1/2 done                                   │
│ ○ deploy       ├─────────────────────────────────────────────────────────────┤
│                │ LOG STREAM (RichLog 自动滚动)                                │
│ ⏸=blocked ✽=run│ 14:02:11 [r_a] tool: WebSearch("rich …")                    │
│ ✓=done ○=wait  │ 14:02:12 [r_a] → 8 results                                  │
│                │ 14:02:15 [r_b] tool: Write("docs/tui.md")                   │
├────────────────┴─────────────────────────────────────────────────────────────┤
│ > <派发新任务 / ! shell / g 跳到 gate>           ↑↓选 Space peek Enter attach │
└──────────────────────────────────────────────────────────────────────────────┘
```

布局来源：
- **Header**（仿 claude agent view tab 标题 `N awaiting input`）
- **左侧 DAG Tree 状态图标**（仿 agent view 行状态编码：✓ done / ✽ run / ⏸ blocked / ! err / ○ pending）
- **右上 Active Node 行摘要 + 并行子 agent 进度列**（仿 Dagger 并行 pipeline）
- **右下 RichLog**（Textual 原生流式日志 widget）
- **底部 input + footer**（仿 lazygit 三段式）

### 2.2 Gate 触发时（ModalScreen 覆盖中央，DAG 继续跑）

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Orca Run #42 · ... · ⏸ 2 awaiting gate                            ░░░░░░░░░░ │
├────────────────┬────────────────────────────────────░──────────────────────┤
│ ⏸ review       │                     ░ ┌──────────────────────────────┐  │
│                │  LOG STREAM ...     ░ │  🔒 GATE: review               │  │
│                │  14:02:20 [review]  ░ │  Claude wants to run:         │  │
│                │  needs input       ░ │  Bash("rm -rf node_modules")  │  │
│                │                     ░ │   [批准]  [拒绝]  [编辑]      │  │
├────────────────┴─────────────────────░─└──────────────────────────────╝─┤
│ > ...                                                  ░░░░░░░░░░░░░░░░░░ │
└──────────────────────────────────────────────────────────────────────────────┘
```

**关键**：Gate = ModalScreen，`push_screen_wait` 阻塞编排 worker，但 **DAG/日志继续刷新**（Textual 决定性优势，Rich Live 做不到）。

---

## 3. 架构设计

### 3.1 文件结构

```
orca/iface/cli/
├── __init__.py          # 导出 main（命令入口）
├── commands.py          # orca run/validate 命令绑定（argparse/typer）
├── app.py               # OrcaApp(Textual App) + 主 Screen
├── widgets/
│   ├── __init__.py
│   ├── dag_tree.py      # DagTree(Tree) widget
│   ├── active_node.py   # ActiveNode 面板（行摘要 + 并行进度）
│   ├── log_stream.py    # RichLog widget 包装
│   └── header.py        # Header widget（run_id/model/进度/awaiting）
└── screens/
    ├── __init__.py
    └── gate_modal.py    # GateModal(ModalScreen)
```

### 3.2 OrcaApp（Textual App）

```python
# orca/iface/cli/app.py
class OrcaApp(App):
    """Orca CLI 主 TUI。订阅 EventBus 驱动渲染，编排主流程是 @work 协程。"""
    CSS = "..."  # 三栏布局 CSS
    BINDINGS = [("q", "quit", "退出"), ("g", "goto_gate", "跳到 gate")]

    def __init__(self, wf: Workflow, inputs: dict, task: str | None, max_iter: int | None):
        self.wf, self.inputs, self.task, self.max_iter = wf, inputs, task, max_iter
        self.bus = EventBus(Tape(...))  # 或复用 orchestrator 的 bus
        self.gate_handler = HumanGateHandler(self.bus)

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DagTree()
            with Vertical():
                yield ActiveNode()
                yield LogStream()

    async def on_mount(self) -> None:
        self.watch_subscription = self.bus.subscribe()  # 订阅事件流
        self.run_worker(self._run_pipeline(), exclusive=True)  # 编排主流程
        self.run_worker(self._consume_events(), exclusive=True)  # 事件消费

    @work
    async def _run_pipeline(self) -> None:
        """编排主流程（顺序代码）。到 gate 节点 push_screen_wait。"""
        orchestrator = Orchestrator(self.wf, self.bus, self.inputs, self.task, self.gate_handler)
        await orchestrator.run()

    @work
    async def _consume_events(self) -> None:
        """订阅事件流，分发到 widget 更新 + gate 触发 ModalScreen。"""
        async for event in self.watch_subscription.events():
            if event.type == "human_decision_requested":
                gate = _gate_from_event(event)
                answer = await self.push_screen_wait(GateModal(gate))  # 阻塞 worker，UI 不冻结
                self.gate_handler.resolve(gate.id, answer, "cli")  # 壳的 resolve 路径
            elif event.type == "human_decision_resolved":
                # 广播：可能别的壳先答了，显示提示（GateModal 已 dismiss 的忽略）
                ...
            else:
                self._dispatch_to_widgets(event)  # dag_tree/active_node/log_stream 更新
```

### 3.3 关键约束

1. **壳不持有业务真相**：所有 UI 状态（节点状态/日志/gate）都是事件流的派生物。重连/重启从 tape replay 一致。
2. **编排主流程 = @work 协程**：`_run_pipeline` 用 `@work` 标注，到 gate `await push_screen_wait` 阻塞该 worker，但 UI 事件循环不冻结。
3. **gate 双重身份**：
   - 收到 `human_decision_requested` → 本壳渲染 GateModal（参与竞速）
   - 收到 `human_decision_resolved` → 别壳先答了，本壳关闭 modal（广播）
4. **resolve 路径**：GateModal 用户答 → `gate_handler.resolve(gate.id, answer, "cli")`（phase 6 接口）。
5. **退出语义**：workflow 终态（completed/failed）→ 显示结果 → 按 q 退出，exit code 反映成功失败。

---

## 4. Widget 设计

### 4.1 DagTree（左侧，节点状态）

- Textual `Tree` widget，每个 node 一个叶节点
- 状态图标编码（仿 agent view）：
  - `✓` done（node_completed）
  - `✽` running（node_started，spinner 动画）
  - `⏸` blocked（human_decision_requested 且未 resolved）
  - `!` failed（node_failed）
  - `○` pending（未开始）
- parallel 组显示为父节点，branches 为子节点 + 进度计数（1/2 done）
- 事件驱动：`node_started/completed/failed/human_decision_requested/resolved` 更新对应节点图标

### 4.2 ActiveNode（右上，当前/选中节点详情）

- 显示选中节点（默认 current_node）的：
  - 行摘要：节点名 + 状态 + 耗时 + executor
  - 并行子 agent 进度列（agent/foreach 场景，仿 Dagger）：每个子 agent 一行带 spinner
- 流式事件（agent_message/thinking/tool_call/tool_result）实时更新
- ↑↓ 切换选中节点

### 4.3 LogStream（右下，滚动日志）

- Textual `RichLog` widget（原生流式 + 自动滚动）
- 格式：`HH:MM:SS [session_short] <event 描述>`
- agent_message/thinking/tool_call/tool_result/node_* 全部入日志
- 支持过滤（如只看某 session_id）

### 4.4 Header（顶部，全局指标）

- `Orca Run #<id> · <workflow> · <model> · <done>/<total> nodes · ⏸ <n> awaiting gate`
- 实时更新（监听 node_*/gate 事件算 done/total/awaiting）

### 4.5 GateModal（ModalScreen）

```python
class GateModal(ModalScreen[str]):
    """gate 人工确认模态。返回 answer（选项或自由文本）。"""
    def __init__(self, gate: HumanGate): ...
    def compose(self):
        # 按 gate.source 渲染不同样式：
        # tool_permission → 显示「工具名 + 参数 + 批准/拒绝/编辑按钮」
        # agent_ask → 显示「问题 + 选项/输入框」
        yield Label(gate.prompt)
        if gate.options:
            for opt in gate.options: yield Button(opt)
        else:
            yield Input()  # 自由文本
    @on(Button.Pressed)
    def on_choice(self, event):
        self.dismiss(event.button.label)  # 返回选项
    @on(Input.Submitted)
    def on_text(self, event):
        self.dismiss(event.value)  # 返回文本
```

- **tool_permission 渲染**：权限弹窗（工具 + 参数 + 批准/拒绝/编辑）
- **agent_ask 渲染**：问答弹窗（问题 + 选项/输入）
- 收到 `human_decision_resolved`（别壳先答）→ 自动 dismiss + 显示「已被 [source] 答」

---

## 5. CLI 命令

### 5.1 命令绑定（`orca/iface/cli/commands.py`）

```bash
orca run <yaml> [task] [-i key=value]... [--max-iter N]
orca validate <yaml>
orca list                     # 列出 examples/
```

参数解析（phase 5 SPEC §5 已定）：
- `<yaml>`：workflow 文件（位置参数，必需）
- `[task]`：可选位置参数 → 注入 `inputs.task`
- `-i key=value`：覆盖 inputs，带类型推断（true/false→bool，数字→int/float，JSON，str）
- `--max-iter N`：覆盖 max_iterations（最高优先）
- task 位置参数本质是 `-i task="..."` 语法糖

### 5.2 入口（pyproject.toml console_scripts）

```toml
[project.scripts]
orca = "orca.iface.cli.commands:main"
```

### 5.3 退出码

- workflow completed → exit 0
- workflow failed → exit 1
- 参数错误/校验失败 → exit 2

---

## 6. 验收标准

### 6.0 验收总则（5 条铁律）
1. **壳无业务真相**：所有 UI 状态是事件流派生物，重启从 tape replay 一致。
2. **gate 走 phase 6 handler**：CLI 壳调 `gate_handler.resolve`，不自己存 gate 状态。
3. **编排主流程不阻塞 UI**：`@work` + `push_screen_wait`，gate 时 DAG/日志继续刷新。
4. **依赖单向**：iface/cli → run + gates + events + compile + schema，不被任何模块 import。
5. **Textual（非 Rich Live）**：gate prompt 能在渲染期输入（Rich Live 做不到）。

### 6.1 命令绑定
- [ ] `orca run examples/demo_linear.yaml` 启动 TUI
- [ ] task 位置参数 → inputs.task
- [ ] `-i key=value` 类型推断正确
- [ ] `--max-iter` 覆盖 max_iterations
- [ ] `orca validate` 校验 + 报错
- [ ] 退出码（0/1/2）

### 6.2 TUI 主屏
- [ ] DagTree 显示所有节点 + 状态图标正确（✓✽⏸!○）
- [ ] Header 实时更新（done/total/awaiting）
- [ ] LogStream 流式滚动（agent_message 等）
- [ ] ↑↓ 切换选中节点 → ActiveNode 更新

### 6.3 Gate ModalScreen
- [ ] 收到 human_decision_requested → 弹出 GateModal
- [ ] tool_permission 渲染（工具+参数+按钮）
- [ ] agent_ask 渲染（问题+选项/输入）
- [ ] 用户答 → resolve(gate_id, answer, "cli") 被调
- [ ] **DAG/日志在 gate 期间继续刷新**（Textual 决定性优势）
- [ ] 收到 human_decision_resolved（别壳先答）→ modal 自动关闭 + 显示提示

### 6.4 端到端（真 claude demo）
- [ ] `orca run examples/demo_linear.yaml`：全 script，DAG 推进到 $end，exit 0
- [ ] `orca run examples/demo_conditional.yaml`：走条件分支
- [ ] `orca run examples/demo_task.yaml "测试任务"`：task 注入 + agent 跑通
- [ ] **含 gate 的 demo**（需 phase 6 hook 配置）：claude 想调工具 → hook 拦 → CLI ModalScreen → 用户答 → 继续

### 6.5 测试
- [ ] `tests/iface/cli/test_commands.py`：参数解析（task/-i/--max-iter/类型推断）
- [ ] `tests/iface/cli/test_app.py`：OrcaApp 结构（compose widget 齐全）
- [ ] `tests/iface/cli/test_widgets.py`：DagTree 状态图标映射、LogStream 格式
- [ ] `tests/iface/cli/test_gate_modal.py`：GateModal 两种 source 渲染 + dismiss 返回值
- [ ] 真集成 `@pytest.mark.integration`：跑 demo workflow（真 claude）
- [ ] 全部通过（含 phase 1-6 不回归）

---

## 7. 给后续阶段的契约

| 后续 | phase 7 提供 |
|---|---|
| phase 9 web | CLI 壳验证了「壳订阅事件 + gate resolve」范式，Web 壳照搬（仅渲染层换 React）|
| phase 10 mcp | CLI 壳是「同步阻塞 gate」的参照实现 |

---

## 8. 不做的事

- ❌ **时间旅行 replay**（CLI 一次性，看历史走 Web）—— phase 9
- ❌ **多 run 并发**（CLI 一次一个 workflow）—— phase 9
- ❌ **DAG 图形化布局**（用 Tree 列表够用；ReactFlow 图形化走 Web）—— phase 9
- ❌ **Web/MCP 壳** —— phase 9/10
- ❌ **真三通道竞速**（单壳能 resolve 即可；CLI + Web 同时跑的端到端走 phase 9 集成）

---

## 9. 关键决策备忘（防 drift）

1. **Textual（非 Rich Live）**：gate prompt 是硬需求，Rich Live 官方确认无法在渲染期输入
2. **壳无业务真相**：所有 UI 是事件流派生物，tape 是唯一真相
3. **编排主流程 @work + push_screen_wait**：gate 阻塞 worker 不阻塞 UI
4. **gate 走 phase 6 handler.resolve**：壳不存 gate 状态
5. **gate 双重身份**：requested → 本壳渲染参与竞速；resolved → 广播关闭
6. **布局**：Header + 左 DagTree + 右上 ActiveNode + 右下 RichLog + 底 input/footer + Gate ModalScreen
7. **task 位置参数 = -i task="..." 语法糖**
8. **退出码**：completed→0 / failed→1 / 参数错→2
9. **依赖单向**：iface/cli → run+gates+events+compile+schema

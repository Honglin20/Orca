# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

> **接口整理前置（铁律）**：每个阶段实施前必须先做接口整理与规划——涉及接口改造的（schema / 事件 / 错误信封 / 能力声明 / 状态机 / 退出码 / widget API 等）**必须先把接口定义讨论清楚并写进 SPEC 或 ADR**，明确「真相源在哪一层、其他层只翻译不重新分类」，不允许实施期临时定义、不允许新旧接口并存。已识别的接口风险见各 phase 待办前置 ADR（如 phase-11 错误接口三层映射 / phase-12 capabilities 替换清单）。

---

## 当前状态：TUI v2 review remediation + 批 1 backend 完成（2026-07-07）；下一模块 phase-12-capabilities

### ✅ 已完成：TUI v2 review remediation + 批 1 backend（Status.blocked + projections.py）

**Commit**：见 `git log`（commit message 末尾含 Claude+Happy co-author）。详见
[release note](../releases/2026-07-07-tui-v2-review-batch1-projections.md) + [CHANGELOG](CHANGELOG.md)。

**交付**：
- 🔴 **Enter 展开回归修复**（commit 5562e5e 引入）：App 级 BINDINGS 加 `down`/`up`
  （`priority=True` 覆盖 RichLog scroll）转发到 `AgentHistory.action_cursor_down/up`；
  3 pilot 测试走真实 `pilot.press` 路径（不直设私有属性）
- 🟡 **批 1 ADR §4.3/§4.3.1**：`Status` Literal 加 `blocked`；`orca/run/projections.py`
  单一派生算法源（4 函数 batch fold，委托 apply_event）；`apply_event` 扩展 blocked
  派生（gate/interrupt 同源，None/running/terminal 三路径）；TUI 删独立 fold 副本全部
  改调 projections（DRY）；`agents_list.py` 类型收紧 + 删 `== "failed"` 字面量比较（P4）
- ADR §8.1 守门：`tests/iface/cli/test_status_literal.py` AST 检查 widget 无 Status
  字面量比较（含 fixture 路径 `parents[3]` + `.exists()` 断言防路径回归）
- 1596 passed / 0 回归（baseline 1558 + 38 新增）

**遗留 follow-up**（详见 release note）：
- 性能 O(N²)：`_dispatch_to_widgets` 每事件全量 refold `_all_events`（批 4 增量化）
- 多 gate 同时 active 的精确计数（批 4 给 RunState 加 active_blockers 字段）
- ADR §8.1 表述订正（"无 `== blocked`" → "无 Status 字面量比较"，batch 2 PR 一并）

---

## 当前状态：phase-11-process-lifecycle 实现完成（2026-07-07）；下一模块 phase-12-capabilities

### ✅ 已完成：phase-11-process-lifecycle 实现（批 3a，exec/iface）

**Commit**：`cdc3469`。详见 [release note](../releases/2026-07-07-phase-11-process-lifecycle.md) + [CHANGELOG](CHANGELOG.md)。

**交付**：
- 新增 `orca/exec/registry.py`（ProcessRegistry DI + 三段式 cancel + 平台分支）+ `orca/iface/exit_codes.py`（ExitCode 5 档）
- runner.py / script.py 接入 `start_new_session=True` + registry.acquire/release（推翻 phase-3 §2.5）
- orchestrator.py 加 `shutdown()` 方法（不动 phase-11-error except 链）
- run/__main__.py SIGTERM handler 只设 Event（signal-safe）+ 退出码经 `exit_for_terminal_status` 派生
- 1558 passed 0 回归（baseline 1525 + 33 新增）

**遗留技术债**（详见 release note §5）：
- DI 传递链未完全闭合（5 处 CLIRunner 调用点未传 run_id/node_id；正确性不受影响；phase-12 Adapter Protocol 一并设计）
- gates/hook_script.py 退出码未迁移（批 4）
- 真实孙子进程 E2E 需非沙箱环境（mock 时序单测覆盖契约）
- Windows test_cancel_windows.py 缺失（平台分支代码已就位）

### ✅ 已完成：phase-11-error-handling 实现（批 2，纯 exec/run/schema/iface）

**Commit**：`451dd39`。详见 [release note](../releases/2026-07-07-phase-11-error-handling.md) + [CHANGELOG](CHANGELOG.md)。

### 🔥 进行中：接口收敛 → 各 phase SPEC 回填 → 实现（goal 2026-07-07）

**goal 工作流**：每个模块依次 `设计 → spec-review-adversarial 审视 → 回填 SPEC → clean-code-builder 实现+清理 → test-coverage-e2e 验证`。范围：接口模块（phase-11-error / phase-11-process / phase-12 / phase-10）+ CURRENT 剩余任务（web/tui/codex **排除**）。

**ADR v2 已定稿**：[`docs/specs/2026-07-06-interface-convergence-adr.md`](../specs/2026-07-06-interface-convergence-adr.md)（spec-review-adversarial 审视通过，5 blocker + 10 major 全闭环）。核心决策：
- D1 错误：ErrorKind 单一分类权威；ExecError 字段集 `{kind,message,phase,node,raw}`；WorkflowTerminated 保留独立（非 ExecError 子类）；Error 删 layer；error_type→kind 读兼容期迁移；retry_on 解耦不改名
- D2 能力：CapabilitySet 全量替换 ProviderCapabilities；补 supports_concurrent_spawn / supports_usage_tracking / structured_output_mode 三态
- D3 节点状态：Status 加 blocked（projections 派生，不入 tape）；projections.py 提前到批 1
- D6 退出码：`orca/iface/exit_codes.py` 5 档 0/1/2/3/130
- D7 ProcessRegistry 用 DI（非 singleton）

**任务依赖图**（见 TaskList）：ADR(✅) → phase-11-error(#2 ✅) → phase-11-process(#3 ✅) → phase-12(#4 ✅) → phase-10(#5 ✅) → 依次实现(#6 进行中) → 剩余任务(#7) / TUI review(#8)

### ✅ 接口设计阶段完成（goal 第一任务）

ADR v2 + 4 phase SPEC 全部回填并对齐（spec-review-adversarial 审视通过）：
- [`docs/specs/2026-07-06-interface-convergence-adr.md`](../specs/2026-07-06-interface-convergence-adr.md) v2（5 blocker + 10 major 闭环）
- [`docs/specs/phase-11-error-handling.md`](../specs/phase-11-error-handling.md) v2.1（7 blocker 闭环：classifier 双入口 / 子类构造器契约 / 反向映射表 / retry_on 强制 retryable / raise 点 kind 表 等）
- [`docs/specs/phase-11-process-lifecycle.md`](../specs/phase-11-process-lifecycle.md) v2（exit_codes 位置 iface/ + ProcessRegistry DI + grep 精确）
- [`docs/specs/phase-12-capabilities.md`](../specs/phase-12-capabilities.md) v2（CapabilitySet 7 字段全量去留）
- [`docs/specs/phase-10-mcp.md`](../specs/phase-10-mcp.md) §2.4b v2（Result 信封无 layer / kind 是 ErrorKind 值）

### 🔥 实现阶段（goal 第二任务）—— 按 ADR §7 批次，避开并行 TUI 工作树

**实现顺序**（每模块 clean-code-builder 实现+清理 → test-coverage-e2e 验证 → 独立 commit）：
1. **phase-11-error-handling**（批 2，纯 exec/run/schema/iface，不碰 TUI）：ExecError 字段集 + Error/ErrorKind/classifier/retry 新模块 + error_type→kind 全量迁移（含 29 fixture）+ retry_started.data 扩展 + 编排 exception 子类化
2. **phase-11-process-lifecycle**（批 3a，exec/iface）：ProcessRegistry DI + 进程组 cancel + orca/iface/exit_codes.py
3. **phase-12-capabilities**（批 3b，profiles/compile）：CapabilitySet 全量替换 ProviderCapabilities + Adapter Protocol + 编译期校验
4. **phase-10 MCP**（批 3c，iface/mcp）：9 工具 Result 信封 + setup/execute 分相

**projections.py + Status.blocked**（批 1 backend 部分）：Status 加 blocked 可立即做（schema 层）；projections.py 抽取需等并行 TUI 重构落地后再动 app.py fold（避免工作树冲突），暂列 follow-up。

**未 commit**：本 session 改动为 docs（ADR + 4 SPEC + CURRENT），等实现首批落地后一并 commit，或按用户指示。

### 与并行 TUI 进程的边界（不变）
- TUI v2 重构在 `phase13-render-chart` 分支进行，动 `orca/iface/cli/widgets/` + `app.py`。实现阶段**不碰**这些文件。
- phase-11-error 实现动 `exec/` + `run/errors.py` + `run/orchestrator.py`（except 链）+ `schema/event.py`，与 TUI widgets 无交集。

---

## 当前状态：phase-10 MCP SPEC v4 设计敲定（setup/execute 分相）；TUI 任务等并行

### 🔥 进行中：phase-10 MCP 壳 SPEC v4（2026-07-04）

**v4 核心设计**：workflow setup/execute 分相消费（统一性原则）
- workflow schema 加 `setup: list[AgentNode]` 字段（setup phase，可选；复用 phase-14 AgentNode 三态）
- 三壳跑同一份 yaml，差异只在 setup phase 的"消费方式"：
  - **TUI/Web**：setup agent 在 workflow 内实跑，自动配 `ask_user` + `gate` 工具，弹窗交互
  - **MCP**：主 session 调 `get_agent_prompt` 借 prompt，主 session 替 setup agent 跑（用自己的工具对话），结果作为 `setup_outputs` 传给 `start_workflow`，workflow 跳过 setup agent 实际执行
- **execute phase 永不中断**：execute phase 的 agent 不配 `ask_user` / `gate` 工具
- **MCP 工具集 9 个**（v3 的 10 个删 resolve_gate）：Discovery 4 + Lifecycle 3 + History 2
- **三重杠杆防跳过 setup**：list_workflows 标记 has_setup / start_workflow 强校验 setup_required / tool description

**已更新文档**（本 session 完成）：
- [docs/specs/phase-10-mcp.md](../specs/phase-10-mcp.md) —— 整体重写为 v4（9 个工具 + setup/execute 分相 + 三重杠杆 + 失败处理 + 完整 user journey）
- [docs/specs/shells-design-draft.md](../specs/shells-design-draft.md) §5 —— MCP 壳设计 v4 重写（§5.1 协议约束修正 + §5.2 setup/execute 分相 + §5.4 三重杠杆 + §5.5 工具签名 + §8.3 端到端 user journey）

**协议约束调研修正**（2026-07-04 再核实，原 v1/v3 多处过时）：
- elicitation CC **已支持**（PR #2799），但有边界 bug（#56243 cowork cancelled / #62319 form-mode auto-decline）—— 仅可作 setup 轻交互，workflow gate 不依赖
- progress notification CC **不支持**（#4157 Anthropic 直接确认）—— server mid-tool-call 主动推进度不行
- 60s 是**默认超时**不是硬 kill（#424 / #43791 timeout 字段被忽略）
- Tasks (2025-11-25 spec) 已落地，CC 未实现（#52137）

**待落地（下一阶段实施）**：
1. schema 改动：`orca/schema/workflow.py` 加 `setup: list[AgentNode] = []`
2. 新文件：`orca/iface/mcp/{server,catalog,agent_catalog,setup_phase,tape_index,transport}.py`
3. compile validator：强制 execute phase 的 AgentNode 不配 ask_user/gate 工具
4. setup_outputs 校验逻辑（§5.9）
5. RunManager 加 `run_summary` / `list_runs` / `cancel_run` 方法
6. schema 扩展 `workflow_cancelled` 事件类型
7. `orca mcp` subcommand
8. 单元测试 + E2E（§6.3 五个 E2E 用例）

**v4 vs v3 差异**：
- v3 `setup_agent: str | None`（引用单个 agent）→ v4 `setup: list[AgentNode]`（setup phase 一段）
- v3 setup 在 workflow 外（前置）→ v4 setup 在 workflow 内（一段 phase）
- v3 仍保留 `resolve_gate` → v4 删除（execute phase 永不中断）

### TUI Redesign v2 完成（2026-07-07）—— 取消 DAG + agent 输出可见

TUI 三块布局重写（左 AgentsList / 右上 AgentHistory / 右下 LogStream）。用户核心痛点闭环：① 看到每个 agent 输出（last message 默认展开）② j/k 切换 agent 看历史（_node_events 分桶）③ 取消 DAG 图 ④ LogStream 高层节点事件 + 5 level icon + 完整失败原因。

**完成 commits**：59021c9 + 5f9988c + e252653 + ab3b254 + 0e9e877 + 77f5685 + 85ecb61

详见 [release note](../releases/2026-07-07-tui-redesign-v2.md) + [v2 spec](../specs/tui-redesign-v2-design-draft.md)。

v1.1.1 widget 全部删除（DagGraph / dag_layout / _dag_render / activity_stream）+ display:none 双写兼容路径清掉。1396+ 测试全过。

## 与并行进程的边界
- TUI v1 / v1.1.1 commit（`7bd43ef` / `225933e`）只动 `orca/iface/cli/widgets/` + `app.py` + 对应测试 + status docs。
- phase-10 v4 SPEC 改动只动 `docs/specs/{phase-10-mcp,shells-design-draft}.md` + 本文件。
- 留工作树（并行进程持有）：`profiles/builtin/*` + `terminal.py` + `gates/dialog.py`
  + `exec/validator.py` + `executor_cmds.py` + `config.py` + `iface/cli/widgets/tool_render/
  normalize.py` + `run/orchestrator.py` + `run/router.py` + 它们测试
  + `examples/demo_task.yaml` + `pyproject.toml` + `uv.lock`
  + `tests/e2e_phase{13,14}/_artifacts/*.jsonl`（_tape）+ `_tui.svg`。

## 已知 follow-up（v2 路线，不阻塞本任务）
- TUI live timer 走 wall clock（spec §4.4：「不进 tape」UI 交互态）
- DAG 节点 hover tooltip（spec §13.7 v2 评估）
- Activity Stream 流式 markdown shiki 增量高亮（render layer v2）
- 全局 thinking 可见性切换
- 双写 LogStream/NodeDetail 兼容路径在 v2 移除

## 待办（等用户指示方向）
1. **phase-10 MCP 实施**（v4 SPEC 已就位，待用户拍板后写实施计划开工）—— **前置：实施前必读 [`phase-11-error-handling.md`](../specs/phase-11-error-handling.md) §1 工具返回形状（统一错误信封） + [`phase-11-process-lifecycle.md`](../specs/phase-11-process-lifecycle.md) §3 退出码语义**
2. phase-12 / 13 / 14 / 15 / TUI 重设计 v1 分支 merge / PR（分支 `phase13-render-chart`）。
3. **批 2（phase-16）**：轻量本地包分发（多 pool + `name@source`）+ workspace-instruction。
4. code-reviewer M2/M3（resolve_flags setdefault 文档交叉引用 + stacklevel 指向）+ N3。
5. **render layer v1.5**：codex 接入（apply_patch 解析 + shell/read_file 映射）—— **前置：[`phase-12-capabilities.md`](../specs/phase-12-capabilities.md) 落地（codex `supports_apply_patch=True` 由 CapabilitySet 声明，render layer 据此分支）**
6. **render layer v2**：Web 端 TS 镜像 + 流式 shiki 增量高亮 + 千行 diff 虚拟化。
7. **background chart gap**（mxint follow-up）：让 `--background` 模式 chart 可用。
8. **agent interrupt 独立 feature**（见下文「agent interrupt 独立 feature」段，待立项 SPEC）
9. **phase-11-error-handling 实施**（SPEC v1 已就位 [`phase-11-error-handling.md`](../specs/phase-11-error-handling.md)）：统一错误信封 `{ok,data?,error?,_hint?}` + ErrorKind 11 分类 + 三层重试不互相吞错 + classifier 纯函数。可与 phase-10 并行，phase-10 先硬编码 Result 落地，phase-11 回填横切抽象。

   **前置 ADR（2026-07-06 接口统一性审计）**：错误接口当前已 5 套并存（canonical Event 3 个 type 字段不一 + `ExecError` phase 8 类 + 3 个编排 exception + phase-11 提议 `Error/ErrorKind` 11 分类）。phase-11 SPEC §1 落地前必须先写 ADR 明确：
   - `Error.kind` (11 分类) ↔ `ExecError.phase` (8 类) 映射规则（多对一 / 一对一 / 漏洞怎么补）
   - canonical Event `node_failed.data.error_type` 字段值最终取 `Error.kind` 还是 `ExecError.error_type`
   - 三层错误表达（持久层 Event / 运行时层 Result / exception 层）的**单一权威**：每个错只在一层有真相，其他层只翻译不重新分类
   - 不留 ExecError 与 Error 双 exception 并存（违反用户底线「不存在多套并存」）
   - 详见 [`tui-redesign-v2-design-draft.md`](../specs/tui-redesign-v2-design-draft.md) §11.4 风险 A
10. **phase-11-process-lifecycle 实施**（SPEC v1 已就位 [`phase-11-process-lifecycle.md`](../specs/phase-11-process-lifecycle.md)）：子进程全局注册表 + 进程组 cancel（推翻 phase-3 §2.5 旧决策）+ 退出码契约 0/1/2/3/130。F2 已并入此 SPEC。
11. **phase-12-capabilities 实施**（SPEC v1 已就位 [`phase-12-capabilities.md`](../specs/phase-12-capabilities.md)）：CapabilitySet 数据模型（部分抄 mco）+ async ProviderAdapter Protocol + 编译期能力校验。**阻塞 render layer v1.5（codex 接入）**。phase-11 完成后开工。
12. **TUI fold DRY follow-up**（v2 follow-up，0.5d）：v1.1.1 fold 字段（`_node_iter` / `_node_status` / `_node_usage`）与 `RunState.node_status` 是两份派生状态——抽到 `orca/run/projections.py` 让 RunState + TUI + 未来 Web/MCP 都消费同一份 reducer。详见 [`tui-redesign-v2-design-draft.md`](../specs/tui-redesign-v2-design-draft.md) §11.2 / §11.4 风险 C。

## agent interrupt 独立 feature（2026-07-04 讨论，待立项）

**已开 design draft**：[`docs/specs/agent-interrupt-design-draft.md`](../specs/agent-interrupt-design-draft.md)（9 章完整骨架 + 9 条决策备忘 + 6 个遗留问题）

**需求**：agent 执行中（execute phase），用户主动打断 + 注入 guidance（方向纠偏）+ agent 继续。

**三种能力对比**（用户视角几乎等价，实现差异大）：

| 能力 | 触发时机 | agent 当前调用 | 实现复杂度 |
|---|---|---|---|
| **node 边界 interrupt** | agent 自然跑完后 | 不 cancel，重跑带 guidance | ✅ 已实现（`orca/gates/interrupt.py` InterruptHandler，TUI Ctrl+G / Web 按钮）|
| **mid-stream cancel + resume** | agent 跑到一半 | cancel subprocess + `claude -p --resume <session_id>` + guidance | ⚠️ 三壳都未实现，但**技术可行**（cancel 已有 + resume 是 claude/opencode 内置），不需要 executor 大改造 |
| **真 streaming interrupt** | agent streaming token 中 | 不重启，stdin 推 guidance 到当前 turn | ❌ 三壳都不支持，需要 executor 双通道 streaming 改造 |

**关键判断**：mid-stream cancel+resume 与 真 streaming interrupt **用户视角效果几乎等价**（都是"介入 + guidance + 调整输出"）。差别在内部：
- cancel+resume：agent 看到自己之前的输出（部分进 history），基于"原始 prompt + 自己部分输出 + guidance"重新生成。**更连贯**，但 token 消耗高
- 真 streaming：agent 在当前 turn 内调整，不知道自己之前输出。**省 token**，但实现复杂

**推荐路径**：mid-stream cancel+resume（性价比最高），不做真 streaming。

**实现拆解**（独立 SPEC 待写）：
1. **executor 层**：
   - `claude/exec/runner.py` 加 `cancel_and_resume(session_id, guidance) -> str` 方法（cancel 当前 subprocess + 新起 `claude -p --resume <session_id>` + guidance 作为新 user message）
   - opencode executor 同款接口
   - 复用 Orca 已有 session_id（HumanGate / InterruptHandler 已有）
2. **InterruptHandler 扩展**（`orca/gates/interrupt.py`）：
   - 现有 InterruptHandler 已支持 continue + guidance / skip / abort（node 边界）
   - 扩展 action：`continue_immediately`（mid-stream cancel + resume，不等当前 turn 完成）
   - 触发时机：从"node 边界"扩展为"任意时刻"（asyncio.Event 立刻唤醒）
3. **三壳集成**：
   - **TUI**：现有 Ctrl+G（node 边界）保留；新增强制打断快捷键（如 Ctrl+C 或 Shift+G）触发 mid-stream cancel+resume
   - **Web**：现有"中断"按钮（node 边界）保留；新增强制打断按钮
   - **MCP**：新工具 `interrupt_task(task_id, action="continue_immediately", guidance="...")`
4. **tape 记录**：interrupt_requested / interrupt_resolved 已有事件类型；扩展 data 加 `interrupt_kind: "node_boundary" | "mid_stream_cancel_resume"`

**与 phase-10 的关系**：phase-10 §8 标为独立 feature，不在 phase-10 范围内。phase-10 完成后可立项 phase-X（暂定 phase-17 或 phase-9e，待规划）。

**遗留问题**（独立 SPEC 立项时讨论）：
- guidance 是否传递给后续 node？（当前 InterruptHandler 只给当前 agent）
- mid-stream cancel 时 agent 已生成的部分输出怎么处理？（丢弃 vs 进 tape vs 进 history）
- resume 失败时（session_id 找不到）怎么 fallback？
- 用户 cancel 的 token 消耗算谁的？（cost accounting）

## 必读文件（下一任务开工前按需）
- [`docs/specs/phase-10-mcp.md`](../specs/phase-10-mcp.md)（v4 SPEC 全文，setup/execute 分相 + 9 工具 + 三重杠杆）
- [`docs/specs/shells-design-draft.md`](../specs/shells-design-draft.md) §5（MCP 壳设计 v4）
- [`docs/releases/2026-07-04-tui-redesign-v1.md`](../releases/2026-07-04-tui-redesign-v1.md)（TUI 重设计 v1 全貌）
- [`docs/releases/2026-07-04-tui-redesign-v1-gaps-abce.md`](../releases/2026-07-04-tui-redesign-v1-gaps-abce.md)（v1.1.1 4 GAP 收口）
- [`docs/specs/tui-redesign-draft.md`](../specs/tui-redesign-draft.md)（v1.1.1 spec 全文）
- [`docs/releases/2026-07-04-render-layer-v1.md`](../releases/2026-07-04-render-layer-v1.md)（phase-15 v1 全貌）+ [`docs/specs/render-layer-design-draft.md`](../specs/render-layer-design-draft.md) §3/§5/§6/§8/§12
- [`orca/iface/cli/widgets/`](../../orca/iface/cli/widgets/)（_event_filter / _dag_render / activity_stream / dag_graph / dag_layout / header 实现）

## 参考仓调研发现 follow-up（2026-07-05 / 2026-07-06 更新）

调研 CCW + mco 后整理 5 个可借鉴设计点（F1-F5）。完整分析与落地建议见 [`docs/plans/2026-07-05-reference-repos-borrow.md`](../plans/2026-07-05-reference-repos-borrow.md)。

**立项状态**：
- ✅ **F2**（进程组 cancel）已提升为 [`phase-11-process-lifecycle.md`](../specs/phase-11-process-lifecycle.md) §2
- 📋 **F1 / F3 / F4 / F5** 仍独立待立项（不阻塞 phase-10/11/12），立项顺序：F3（高优先 + 小成本）→ F4 → F5 → F1

**2026-07-06 宏观借鉴补充**：在 F1-F5 之上新增 G1-G7（CapabilitySet / 状态机 / 错误信封 / ErrorKind / 进程组 / 子进程注册表 / 退出码）。G2-G7 已落入 phase-11-error-handling + phase-11-process-lifecycle；G1（CapabilitySet）落入 phase-12-capabilities。详见各 SPEC。

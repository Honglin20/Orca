# Release：in-session 三件打磨（outputs 求值 + inputs 从 tape 恢复 + prompt 收紧）

> 2026-07-09。在 model-driven advance 补丁（commit `4b3a4d6`）之上的 surgical polish。
> SDD：[计划](../plans/2026-07-09-in-session-outputs-inputs-prompt-polish.md) → 实现 → code-reviewer PASS（0 BLOCKER/MAJOR）→ commit。

## 背景（用户提出 4 点，查证后归类）

| 用户观察 | 查证根因 | 性质 |
|---|---|---|
| 主 session 读 agent MD | prompt 纪律不足 | prompt 文案 |
| `next` 有时没传 `--inputs` | `advance_step` 用 CLI 传入 inputs（默认 `{}`），不从 tape 恢复 | **确定性代码 bug** |
| 两钩子都 work，怎么加强指令遵从 / 换 MCP？ | — | 架构决策（结论：**不换 MCP**） |
| in-session 不支持 outputs 模板求值 | `_final_outputs` 见 `wf.outputs` 直接 fail loud（有意 stub） | **确定性代码补丁** |

## 改动

### 1. outputs 模板求值（`orca/run/step.py` `_final_outputs`）
- **原**：`wf.outputs` 非空 → raise `InSessionError`（注释「evaluate_outputs 未对齐」）。
- **现**：`render_template` 求 `wf.outputs`（reuse `_build_ctx`），与 `Orchestrator._evaluate_outputs` 同源。渲染错（`ExecError`）→ `InSessionError(ERR_RENDER_ERROR)` fail loud（精确 catch `ExecError`，与同文件 `_render_or_fail` 一致）。
- **隔离安全**：`advance_step` 是 in-session 专用（正常 `orca run` 走 `Orchestrator._drive_loop`，不经此）→ 不影响正常路径。
- **DRY 债**：渲染逻辑短期与 orchestrator 内联一份（同 `_resolve_inputs` 的 known-debt 模式），不动 drive_loop（方案 E 底线）。`end_route.output`（phase-14 per-route 输出变换）暂不支持，留 follow-up。

### 2. inputs 从 tape 恢复（`orca/run/step.py` `advance_step`）
- **原**：用 CLI 传入 `inputs`（`next` 不传 → `{}`）→ `_resolve_inputs` 只补 `wf.inputs` default，补不回 bootstrap inputs → 非 entry 节点 `{{ inputs.* }}` 渲染 undefined。
- **现**：`Orchestrator._inputs_from_tape(tape)`（`@staticmethod`，读 `workflow_started.data.inputs`）恢复，与 CLI 传入 merge（CLI override 兼容）→ `_resolve_inputs`。bootstrap 首调 tape 无 ws → 返 `{}` → 自然 fallback CLI inputs。
- **效果**：模型彻底不用每步重传 `--inputs`（deterministic 优于 model-mediated，[[deterministic-over-model-mediated]]）；顺手修非 entry 节点 `{{ inputs.* }}` 渲染隐患。

### 3. prompt 收紧（`run.md` / `_drive_protocol` / bootstrap `--format prompt`）
- **`run.md`**：规则区加「节点指令 `.md` 由子代理 Read，**你不许自己 Read**」；修 stale「Orca 自动推进」→ model-driven（模型自调 `next`）。
- **`_drive_protocol` step 1**：收紧为「**由子代理 Read 节点指令文件**；你不许自己 Read 该文件」。
- **`bootstrap --format prompt`**：补附驱动协议（原仅 echo pointer → model-driven advance 经 prompt-command 入口时模型拿不到「调 next」的指令，CURRENT 遗留 #2；现与 JSON 路径一致）。

### COMMAND → MCP？不换（客观结论）
- MCP 解决不了任一实际失败模式（读 MD / 不传 inputs）；inputs 改确定性后连「typed schema 强制必填」的唯一卖点也消失。
- MCP 会重复 phase-10 已设计的 MCP 壳 + 加 stdio transport + 60s 超时约束。in-session 卖点 = 宿主主 session 自驱子代理，prompt-command + CLI 天然贴合。
- model-driven advance 后 idle 钩子已 neuter（心跳）、transform 入口批 B 要删 → 钩子不再是指令面。模型唯一指令面 = `run.md` + `_drive_protocol`。

## 测试（`tests/iface/in_session/test_in_session_cli.py` +3）
- `test_next_completes_with_outputs_template_evaluated`：`wf.outputs` 模板跑完求值（`A=out_a`），不再 fail loud。
- `test_next_recovers_inputs_from_tape_without_inputs_arg`：`next` 不传 `--inputs` → 非 entry 节点 `{{ inputs.task }}` 正确渲染（`hello`，无残留标记）。
- `test_next_outputs_template_render_failure_fails_loud`：outputs 引用存在节点的缺失字段 → render 期 `workflow_failed(kind=render_error)`，不静默返 `{}`。

## 验证
- in_session 套件：**96 passed**（93 baseline + 3 新增），0 回归。
- `tests/run/ + tests/iface/`：**1007 passed / 33 skipped**（全 playwright skip），0 回归。
- code-reviewer：PASS，0 BLOCKER / 0 MAJOR；2 建议（精确 catch `ExecError` + 渲染失败测试）已闭环。

## 未做（follow-up）
- phase-14 `end_route.output`（命中 `$end` route 的独立输出变换）in-session 暂不支持，只求 `wf.outputs`（覆盖绝大多数 workflow）。
- 批 B plugin 重写（删 transform / idle 死代码）+ `doctor.md` 去陈旧 transform 措辞 —— 独立线（doctor.md 与 plugin/doctor 语义强耦合，随批 B 一并）。
- CC 路同步 model-driven（`cc_hooks.py`）—— 批 B 整理时做。

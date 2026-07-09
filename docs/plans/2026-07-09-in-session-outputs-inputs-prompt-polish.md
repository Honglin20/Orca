# 计划：in-session 三件打磨（outputs 求值 + inputs 从 tape 恢复 + prompt 收紧）

> 2026-07-09。补丁（非终态设计），在 2026-07-09 model-driven advance 补丁（commit `4b3a4d6`）之上。
> 用户确认范围：三件一个补丁。SDD：本计划 → 实现 → 自检 review → commit/push。

## 触发（用户原话归纳）

1. 主 session 会去 Read 节点 agent 的 `.md` 指令文件（应让子代理 Read，省主 session 上下文）。
2. `next` 有时没传 `--inputs` —— **查证为确定性代码 bug，非文案问题**（见下）。
3. 两钩子都 work，怎么加强模型指令遵从 / 要不要换 MCP —— **结论：不换 MCP**（见下）。
4. in-session 不支持 outputs 模板求值 —— **查证为有意 fail loud stub**（`_final_outputs`）。

## 根因查证（代码层）

### (2) inputs —— 确定性 bug
- `make_workflow_started`（`lifecycle.py:55`）把 inputs 写进 `workflow_started.data.inputs` → **tape 已是 inputs 真相源**。
- 正常 `orca run` resume 用 `Orchestrator._inputs_from_tape`（`orchestrator.py:545`，`@staticmethod`）从 tape 恢复 inputs。
- in-session `advance_step`（`step.py:283`）用的是 CLI 传入的 `inputs`（`next` 不传 → 默认 `{}`）→ `_resolve_inputs` 只补 `wf.inputs` default，**补不回 bootstrap inputs** → 非 entry 节点 `{{ inputs.* }}` 渲染 undefined。
- **结论**：让 `advance_step` 从 tape 恢复 inputs（同 resume），模型彻底不用每步重传，顺带修非 entry 节点 `{{ inputs.* }}` 隐患。deterministic 优于 model-mediated（[[deterministic-over-model-mediated]]）。

### (4) outputs —— 有意 stub
- `_final_outputs`（`step.py:222`）见 `wf.outputs` 非空就 raise `InSessionError`（注释「evaluate_outputs 未对齐」）。
- `advance_step` 是 **in-session 专用**（正常路径走 `Orchestrator._drive_loop`，不经此）→ 改 `_final_outputs` **隔离安全**。
- 求值机器现成：`render_template`（`render.py:66`）+ `_build_ctx`（`step.py:118`），与 `Orchestrator._evaluate_outputs`（`orchestrator.py:1003`）同源。

### (3) COMMAND vs MCP —— 不换
- MCP 解决不了任一实际失败模式（读 MD / 不传 inputs）；inputs 改确定性后连"typed schema 强制必填"的唯一卖点也消失。
- MCP 会重复 phase-10 已设计的 MCP 壳 + 加 stdio transport + 60s 超时约束。in-session 卖点 = 宿主主 session 自驱子代理，prompt-command + CLI 天然贴合。
- 2026-07-09 model-driven advance 后，**idle 钩子已 neuter（只剩心跳），transform 入口批 B 要删** —— 钩子不再是指令面。模型唯一指令面 = `run.md` + `_drive_protocol()`（附每个节点 prompt）。加强 = 把这两段写短写准，不往钩子塞提示。

## 改动（文件清单）

| 文件 | 改动 |
|---|---|
| `orca/run/step.py` | `_final_outputs`：fail loud → `render_template` 求 `wf.outputs`（reuse `_build_ctx`），渲染错 → `InSessionError(ERR_RENDER_ERROR)`；call site 传 `inputs+run_id`。`advance_step`：开首 `Orchestrator._inputs_from_tape(tape)` 恢复 inputs（CLI override 兼容）→ `_resolve_inputs`。import 加 `render_template`。 |
| `orca/iface/in_session/templates/opencode/command/orca/run.md` | 规则区加「节点指令 `.md` 由子代理 Read，你不许自己 Read」。 |
| `orca/iface/in_session/cli.py` | `_drive_protocol` 第 1 步收紧：「派子代理（**由子代理 Read 指令文件并执行**；你不要 Read）」。 |
| `orca/iface/in_session/templates/opencode/command/orca/doctor.md` | 去掉陈旧「transform 入口」措辞（批 B 删 transform 后失实）。 |
| `tests/iface/in_session/test_in_session_cli.py` | +2 测试：outputs 模板求值（带 `outputs:` 的 wf 跑完 → completed outputs 已求值）；inputs 从 tape 恢复（next 不传 `--inputs` → 非 entry 节点 `{{ inputs.* }}` 正确渲染）。 |

## 不做（标注 follow-up）
- phase-14 `end_route.output`（命中 `$end` route 的独立输出变换）in-session 暂不支持，只求 `wf.outputs`（覆盖绝大多数 workflow）。
- 批 B plugin 重写（删 transform / idle 死代码清理）独立线，本补丁不碰。
- CC 路同步 model-driven（`cc_hooks.py`）—— 批 B 整理时做。

## 验收
- 带 `outputs:` 模板的 workflow in-session 跑完 → `workflow_completed.data.outputs` 是模板求值结果（非 fail loud）。
- `next` 不传 `--inputs` → 非 entry 节点 prompt 的 `{{ inputs.* }}` 正确渲染（从 tape 恢复）。
- run.md / drive protocol 明示「不许自己 Read 节点 .md」。
- in_session 单测全绿（含 2 新测），0 回归。
- code-reviewer 自检无 BLOCKER。

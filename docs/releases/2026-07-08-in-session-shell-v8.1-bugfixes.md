# in-session shell v8.1 —— 修 5 bug + 签名契约测试

**日期**: 2026-07-08
**分支**: `phase13-in-session-v8`
**SPEC**: [`docs/specs/in-session-shell-design-draft.md`](../specs/in-session-shell-design-draft.md) v8
**实证依据**: e2e `/tmp/orca-e2e-v8/`（5 bug 复现 + spike-patched-with-fixes 形态验过）

## 背景

v8 shipped（commit `56083c1`）52 单测全过却 shipped inert —— 真链路 e2e `test-coverage-e2e`
在 `/tmp/orca-e2e-v8/` 跑 `/orca doctor` + `/orca run` 3 节点全挂。根因：builder 上一轮
从已验证的 spike 形态回退，写错 3 处签名 + 漏 2 处业务 prepend。plugin TS 的纯单测（marker
regex / 字段提取）**验不出运行时签名 bug** —— hook 的调用签名（参数个数、payload 包装）只能由
真 opencode runtime 决定，spike 已实证的形态是唯一真相源。

## 改动

### Bug A — transform hook 签名错（`orca.ts:214`）
- shipped：`async (input) => { const out = input?.out ?? input; ... }`
- 实证（`/tmp/orca-xform`）：opencode 1.14.22 实调 `async (input, out)` 两参，`input` 空 `{}`、
  messages 在 `out` 上。shipped 形态 runtime 下 input.out 永远 undefined → messages 永远 [] →
  transform 静默 passthrough → 整个入口链路死。
- 修：签名改 `async (input: any, out: any)`，`const realOut = out ?? input?.out ?? input`
  作兜底（input.out 必空，但保留兼容性）。

### Bug B — event hook payload 包装错（`orca.ts:275-276`）
- shipped：`event: async (event) => { event.type ... }` 直访
- 实证（`/tmp/orca-f4`）：runtime 外层包一层 `{event}`，即 `input.event.type`。
- 修：签名改 `event: async (input) => { const event: any = input?.event ?? input; ... }`
  （兼容解构 + 直传两种形态，比 spike 的 `({event})` 解构形态更稳健）。

### Bug F — SDK message-fetch 调错 API（`orca.ts:290-307`）
- shipped：`await client.session.message({ id: sessionID })`
- 实证（`/tmp/orca-e2e-v8/idle-debug.log` + `client-debug.log`）：SDK 该方法是
  get-one-message-by-id（要 messageID），SDK 把 `{id}` 当字面占位符，返
  `invalid_format prefix:"ses"` —— **不是** list-messages。
- 修：绕过 SDK，用 REST `fetch(\`${serverBaseUrl}/session/${sid}/message\`)`
  （e2e curl 实证 HTTP 200 + 完整消息数组）。`serverBaseUrl` 从 `ctx.serverUrl` 取，
  env `OPENCODE_SERVER_URL` 兜底，皆无 → 显式 warn + return（不连不存在的端口）。
  `sessionID` 经 `encodeURIComponent` 防御性编码。
- 失败语义（spec-review 🟡 #1 闭环）：transport 错打 console.error 日志；arr 留空 →
  next 无 --output → CLI branch 4 idempotent-replay → 合规计数器 +1 → 连续 3 次
  workflow_failed。真正的失败信号**延迟**经合规计数器 surfaced，非即时用户可见
  （注释明示，避免「fail loud」字面误导）。

### Bug G — bootstrap/next 未注入 Task-tool instruction（`cli.py:63-82`）
- shipped：bootstrap + next 返 YAML 节点 prompt 原文 → 模型直接文本回复、不派 Task
  subagent → 没 ToolPart.state.output → 3× compliance → workflow_failed。
- 修（CLI 端，符合「业务逻辑在 CLI」铁律）：bootstrap + next 返回的 prompt prepend
  `_TASK_TOOL_INSTRUCTION`（「【Orca 节点执行】请用 task 工具派一个子代理执行本节点，
  子代理的输出即本节点的输出。不要自己直接回答。」+ 原节点 prompt）。单一定义（DRY），
  `_with_task_instruction(prompt)` helper，bootstrap + next 共用；None/"" 直传不强加。
- 注意 SPEC §0 真相源声明：opencode session 落盘的是 marker 原文（UI 行为），CLI 返的
  .prompt 含 Task 指令 —— by design 不冲突。

### Bug E — model 不透传（`orca.ts:200 extractModel` + `:386 buildCliArgs`）
- shipped：bootstrap `--model` default `deepseek/deepseek-v4-flash`，plugin buildCliArgs
  "run" 分支不透传 `--model` → marker.model 永远 CLI 默认，不来自用户当前 model。
  环境没配 deepseek 时 idle 注入 promptAsync 调该 provider 必失败。
- 修：plugin transform hook 从 `out.messages[i].info.model = {providerID, modelID}`
  抽当前用户消息的 model（实证路径见 `/tmp/orca-e2e-v8/event-debug.log`），作
  `--model` argv 透传给 bootstrap。CLI marker.model 记录它、idle hook 用它注入
  promptAsync。spike 是硬编码 "zhipuai-coding-plan/glm-4.6v"，本实现动态抽取 —— 改进。

### 签名契约测试（防再回退，关键是这条）
`tests/iface/in_session/test_in_session_v8.py:668-803` 追加 6 测，断言 shipped `orca.ts`
文本里：
- `test_transform_hook_signature_is_two_param_async_input_out`（:679）：transform hook
  严格 `async (input: any, out: any) =>`（不是单参 `input.out ?? input`）；
- `test_event_hook_payload_unwrap_input_event_fallback_input`（:704）：event hook 取
  `const event: any = input?.event ?? input`（兼容包装）；
- `test_message_fetch_uses_rest_fetch_not_sdk_client_session_message`（:730）：message-fetch
  用 `fetch(` 而非 `client.session.message(`；
- `test_bootstrap_and_next_prompt_prepend_task_tool_instruction`（:752）：真跑 bootstrap +
  next，断言 .prompt startswith `_TASK_TOOL_INSTRUCTION`；
- `test_task_tool_instruction_is_single_source_constant`（:783）：DRY 守门（单一常量 + prepend
  helper）；
- `test_build_cli_args_run_branch_passes_user_model`（:798）：buildCliArgs run 分支必含
  `--model` 透传。

**根因教训写进测试注释**（`test_in_session_v8.py:660-666`）：plugin TS 的纯单测验不出
运行时签名 bug —— 52 单测全过却 shipped inert。hook 的调用签名（参数个数、payload 包装）
只能由真 opencode runtime 决定，spike 已实证的形态是唯一真相源。源码形态扫描是次优真相源
（spike 已实证形态），符合 Rule 9「测试验证 intent」。

## 偏离计划

无。5 bug 全部对齐 spike 实证形态，且在两处超出 spike —— Bug E 动态抽 model 而非硬编码、
Bug F 用 `ctx.serverUrl` 而非硬编码 URL —— 是改进而非照抄。

## 验证

- baseline 83 测 → after 89 测（83 + 6 新签名契约测试），全绿。
- 全 unit 套件 `tests/iface/in_session/ tests/compile/ tests/run/ tests/events/ tests/profiles/`
  486 + 202 测全过，0 回归。
- 守门 grep（禁词：advance_step / router.resolve / replay_state / tape.append / EventBus /
  Tape( / drive_loop / advance( / client.session.message(）：**NONE — clean**。
- mutation-test：故意 break A/B/F 签名，3 个契约测试 regex 全部红 → 守门有效。

## 未实证项（留 `test-coverage-e2e`）

仍按 SPEC §9.2 留真链路 e2e：shipped 模板**开箱**跑 `/orca doctor` + `/orca run` 3 节点
（不该再需要打补丁）。其他遗留：transform await 外部进程时序 / multi-session 绑定（M3）/
子 session 过滤（D-v7-5）/ `/orca doctor` 真链路 idle 自检盲区 —— 不在本轮范围。

## Commit SHA

见 CHANGELOG 顶部索引。

## Co-Authored-By

Claude <noreply@anthropic.com> + Happy (code-reviewer agent)

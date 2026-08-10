# SPEC: opencode in-session 权限审批闭环（`--auto` + `tool.execute.before` 桥）

**日期**: 2026-08-11
**状态**: 待 spec-review
**分支**: in-session-unified-backend
**关联**: DEFECT-1（`docs/status/CURRENT.md`）；spike 证据 `/tmp/orca-spike/`；broker SPEC `in-session-permission-hook.md`；yolo 兜底 SPEC `2026-08-07-in-session-yolo-active-run-fallback.md`

---

## 0. 背景与范围

opencode 家族（`opencode` / `nga`）当前**无 web 审批 / yolo 路径**——权限纯 flag（`--dangerously-skip-permissions` + 缺的 `--auto`）。两块缺陷：

- **DEFECT-1（headless hang）**：`tars run --background` 时 opencode `external_directory` 原生权限 ask → headless 无人审 → 挂死。根因：opencode profile 用 `--dangerously-skip-permissions` 但 `external_directory` 这类**原生权限**需单独 `--auto`。
- **无交互审批**：CC 家族有 PermissionRequest hook → web 审批卡 + yolo；opencode 无对等物。此前误判"opencode 协议层不可能"——**已 spike 推翻**（见下）。

**spike 实证（opencode 1.18.13 + deepseek-v4-flash，证据 `/tmp/orca-spike/run1.log` / `run2.log` / `slow.log`）**：
1. `tool.execute.before` **对主 agent 与 Task 子代理的工具调用都 fire**，子代理带自己的 sessionID（≠ 主 session）。GitHub #5894（"子代理拦不到"）在官方 opencode 不成立。
2. `tool.execute.before` **tolerates ≥10s await**（`elapsed_ms:10053`，hook 跑满 10s 后工具才执行；无硬超时杀 hook）。
3. hook input 形状 = `{tool, sessionID, callID}`；**工具 args 在 `output.args`**（非 input）；**deny = `throw`**（`.env` 范式，官方文档）。

**范围（两部分，一个 SPEC）**：
- **Part A**：opencode profile `--auto` flag 固化（堵 DEFECT-1）。
- **Part B**：`orca.ts` 加 `tool.execute.before` hook → 复用 broker `/approval` 交互审批桥（主+子代理覆盖；yolo/卡复用 broker 既有）。

**铁律：纯增量，cc/cac 零改动**（§1 不变量）。broker 决策路径 backend-agnostic，opencode 桥是新 SENDER，复用既有 `/approval` 契约逐字。

**非目标**：opencode↔nga family 自动探测（另立）；opencode 原生 `permission.asked`/`permission.replied` 事件代答（YAGNI——`--auto` + 桥已覆盖工具级 + 原生权限级两层）；opencode 协议 NDJSON translator 改动（v1.14.22 注释与 1.18.18 实测版本差异另行核，不属本 SPEC）。

---

## 1. 不变量（cc/cac 非回归，I-1~I-4）

- **I-1（broker 零改动）**：`approval_broker.py:request()` / `resolve_session_context` / `_resolve_active_run` / yolo / `_redact` / timeout / disconnect 逻辑**源码零改动**。opencode 桥是新的 `/approval` 调用方，POST 同款 body。`request()` 现只读 `session_id` / `tool` / `tool_input`（`hook_event` 在决策路径**不被读**），redaction source-agnostic——opencode 工具输入同款 redact。
- **I-2（CC hook 零改动）**：`orca-permission-hook.py`（cc+cac 家族）源码零改动。
- **I-3（claude/ccr profile 零改动）**：`--auto` 只进 opencode profile（`profiles/builtin/opencode.py`）；claude / ccr profile 不碰。
- **I-4（install 落点隔离）**：`orca.ts` 改动只在 opencode 家族 install 路径（`.opencode` / `.nga`）；`_install_cc_nudge`（cc+cac）零改动。
- **守门**：git diff 必须证实 `approval_broker.py` / `orca-permission-hook.py` / `profiles/builtin/{claude,ccr}.py` / `routes/approval.py` 零行改动（§6 AC6）。

---

## 2. Part A —— opencode profile `--auto` flag 固化

**改**：`orca/profiles/builtin/opencode.py` 的 `flags` tuple 加 `"--auto"`：

```python
flags=(
    "run",
    "--format",
    "json",
    "--dangerously-skip-permissions",
    "--auto",
),
```

**语义**：`--auto` auto-approve opencode **原生权限** ask（`external_directory` / `doom_loop` 等，见 `opencode agent list` 的 permission 块）→ headless 不再挂死（DEFECT-1）。

**注意 `resolve_flags` REPLACE 语义**（`profiles/base.py:112`）：env `ORCA_OPENCODE_FLAGS` 设定时**整体替换** `flags`（非追加）。故用户自定义 flags 必须含全部 flag（含 `--auto`）。docstring（`opencode.py:10` flags 行）同步更新。

**显式限制（spec-review B4）**：固化 `--auto` 进 profile default **仅救未设 `ORCA_OPENCODE_FLAGS` 的用户**。DEFECT-1 报告人正是设了该 env（user-scope config）——对这类用户，固化不生效，须其显式在自定义 flags 里补 `--auto`（与 `--dangerously-skip-permissions` 同性质，自定义者理应一并含）。**不新增 append 通道**（YAGNI；`reasoning_flags_env` 是可选增强先例，但 `--auto` 是基础安全 flag 非可选增强）。docstring 文案：`# flags 经 ORCA_OPENCODE_FLAGS 整体替换；自定义时务必包含 --dangerously-skip-permissions 与 --auto（否则 headless 会挂在原生权限 ask）。`

**AC1**：profile flags 含 `--auto`（静态断言）；headless opencode 触发 `external_directory` 不再 ask/hang（test-agent 实测）。

---

## 3. Part B —— `tool.execute.before` 交互审批桥（`orca.ts`）

**位置**：`orca/iface/in_session/templates/opencode/orca.ts`，在既有 `event`（idle nudge）/ `tool.execute.after`（PostToolUse 守卫）/ `shell.env`（注入 `ORCA_HOST_SESSION_ID`）之外，**新增** `tool.execute.before` hook。

**hook 逻辑（伪码）**：
```typescript
"tool.execute.before": async (input, output) => {
  const tool = input?.tool
  const args = output?.args
  // session 解析契约（spec-review B1）：两 ID 空间——
  //   ORCA_SESSION_ID（executor 注入的 orca-uuid == tape node session_id；headless 主+子代理命中 broker node 键）
  //   input.sessionID（opencode 内部会话 id；交互模式手起 opencode 时命中 host_session 键）
  // translator 显式"不复用 opencode 流里 sessionID"（translators/opencode.py:39-40），
  // 故 headless 必须取 ORCA_SESSION_ID（input.sessionID 在 tape 不存在 → resolver miss → 死桥）。
  const sid = (typeof process !== "undefined" && process.env?.ORCA_SESSION_ID) || input?.sessionID
  if (typeof sid !== "string" || !sid) return  // 手 CLI / 无 session → fail-open 放行
  const decision = await _askBroker(sid, tool, args)  // POST /approval
  if (decision.behavior === "deny") throw new Error(`orca: 工具 ${tool} 被审批拒绝（不要重试）`)
  // allow / ask / 未决 → return（放行；ask 交 opencode 原生，--auto 兜底）
}
```

**session 解析契约（spec-review B1 闭环）**——两 ID 空间，`||` 合一：
- **headless**（`tars run` / `--background`，Orca executor spawn opencode）：executor 经 `build_env_overlay(session_id=...)`（`exec/env.py:96-97`）注 `ORCA_SESSION_ID` = executor 入口 uuid = translator 写 tape 的 node `session_id`（`translators/opencode.py:39-40` 不复用 opencode 内部 id）。plugin 取 `ORCA_SESSION_ID` → broker `active_runs.py:221` **node 键**命中（`session_id in node_sessions`）。headless `data.host_session=null`（`make_workflow_started` 无 host_session kwarg）→ host 键不命中，**node 键是唯一路径**。
- **交互**（用户手起 opencode 驱动 `orca next`）：`ORCA_SESSION_ID` 缺（非 executor spawn）→ 退 `input.sessionID`（opencode 内部 id）；既有 `shell.env` 钩子注 `ORCA_HOST_SESSION_ID` = 同款 opencode 内部 id → bootstrap 写 `data.host_session` → broker **host 键**命中。
- 两路径覆盖 broker 双键，零 broker 改动。**纠错**：原 SPEC 草稿"子代理 sessionID 在 tape node session_ids 里"措辞误导——tape 里是 orca-uuid（经 ORCA_SESSION_ID），非 opencode 内部 sessionID。

**`_askBroker` 契约**（复用 CC hook 的 `/approval` body 形状，SPEC `in-session-permission-hook.md` §4.1）：
- `POST http://{host}:{port}/approval`，body `{session_id, tool, tool_input, hook_event: "PermissionRequest"}`。
- `tool_input = args`（opencode args 在 `output.args`；CC hook 是 stdin `tool_input`——形状对齐 broker 期望的 dict/list，非 dict/list 时传 `{}`）。
- 响应 `{behavior: "allow"|"deny"|"ask", approval_id?, resolved_by?}`。
- host/port/timeout/policy 来源见 §5。

**yolo 语义**：**桥不自己判 yolo**（单一真相源 = broker）。yolo on → broker 即时返 allow → 桥 return。yolo off → broker 等 web 卡 / timeout。与 CC 路径完全一致。

**D-v7-1 哑传输守门不变**：桥只 POST 转发 + 据 behavior 放行/throw，**不做 Orca 业务判定**（run 归属 / yolo / 审批决策全在 broker）。run 归主判定由 broker `active_runs.py` 经 sessionID 双键匹配完成（子代理 orca-uuid 在 tape node session_ids 里——经 ORCA_SESSION_ID，见上「session 解析契约」；主 session 在 `data.host_session`）。

**AC2**：`orca.ts` 含 `tool.execute.before` hook（静态门 + 行为表单测，§6）。

---

## 4. 失败语义（opencode 版，映射"opencode 无原生 prompt fallback + 已 `--auto`"现实）

对称 CC hook SPEC §7，但 opencode 已 `--auto`（原生权限 auto-approve），broker 是叠加的 web 审批层——故 broker 不可达走 fail-**open**（CC 是 fail-open 到 native prompt，opencode 是 fail-open 到 `--auto` 放行）：

| 场景 | 桥行为 | 理由 |
|---|---|---|
| broker 不可达（`fetch` 网络错 / 连接拒） | **return（放行）** | broker 不在线 = web 审批层没了；退回"无 web 审批"（`--auto` 原生放行仍工作）。fail-open 优于挂死 agent。console.error 留痕。 |
| HTTP 4xx/5xx | **throw（deny）+ console.error** | broker 活着但出错 = 可疑，fail loud（与 CC hook 一致）。 |
| 响应非 JSON / 缺 behavior | **throw（deny）+ console.error** | fail loud（与 CC 一致）。 |
| stdlib timeout | 按 `ORCA_APPROVAL_TIMEOUT_POLICY`：`allow`→return / `deny`→throw / `ask`→return | 与 CC hook 同款 policy。 |
| 未预期异常 | **return（放行）+ console.error** | 保守 fail-open，绝不挂 agent。 |

**降级模型（显式声明）**：
- broker 未运行（headless `tars run` 无 `tars serve`）→ 桥 fail-open 放行 → 工具照跑（headless 安全）。
- broker 运行 + yolo on → 桥 allow → 工具跑。
- broker 运行 + yolo off → 桥 → web 卡（交互 UX）。

**fail-open 取舍（spec-review B5）**：headless 无人可问，broker 不可达时 fail-closed = 挂死 = DEFECT-1 复发 → headless fail-open 是有意设计。交互模式此取舍较弱（broker 挂时用户以为有闸实际全开）——本阶段不做 `ORCA_APPROVAL_UNREACHABLE_POLICY` 可配（YAGNI，记 §9）；headless 是主用例，交互模式 broker 常驻（`tars serve`），故实际暴露面小。

---

## 5. 配置（host/port/timeout/policy）

**问题（R3）**：opencode plugin 跑在 opencode bun 进程内；`shell.env` 钩子注入的 env 只覆盖 **shell 子进程**（bash），**不覆盖 plugin/opencode 进程本身**。故 plugin 不能依赖 `shell.env` 拿 broker 端口。

**方案（Simplicity First，spec-review B2 修正）**：plugin 硬编码 broker 默认 `host=127.0.0.1` / `port=7428`（与 broker 默认一致，`routes/approval.py` + `tars serve` 7428），**读 `process.env.ORCA_HOST` / `ORCA_PORT` / `ORCA_APPROVAL_TIMEOUT` / `ORCA_APPROVAL_TIMEOUT_POLICY` 作显式覆盖**（存在时）。
- **headless 不注入连接 env**：`exec/env.py` overlay 只注 run 路由 env（`ORCA_SESSION_ID`/`ORCA_RUN_ID`/`ORCA_NODE` 等），**无** `ORCA_HOST`/`ORCA_PORT`。故 headless 依赖 plugin 默认 7428 与 broker 默认端口吻合。
- **交互**：用户 shell env 有则覆盖，无则默认 7428（开箱即用）。
- **跨边界（WSL/Windows）/ 自定义端口**：用户显式设 env（plugin 进程 env 同 opencode 进程 env，in-process）。

**不采**配置文件方案（YAGNI——默认+env 已覆盖；`tool-classification.json` 是分类真相源需文件，host/port 是连接参数 env 足够）。

---

## 6. 验收标准（AC）

- **AC1**（Part A 静态 + 实测）：opencode profile `flags` 含 `"--auto"`；headless opencode 触发 `external_directory`（agent prompt 引导读 `project_root` 外文件，构造越界访问）不再 ask/hang。
- **AC2**（Part B 静态）：`orca.ts` 含 `tool.execute.before` hook，行为表（allow→return / deny→throw / ask→return / 不可达→return / HTTP 错→throw / timeout→policy / 异常→return）单测覆盖。
- **AC3**（headless 实测，yolo on）：真 opencode run + **并行 `tars serve`**（默认 7428，broker 实际宿主；headless `tars run` 不起 HTTP broker，见 §4 降级模型）+ yolo on → 危险工具（read/write/bash）经桥 POST → broker `resolved_by:"yolo"` allow → 工具执行。
- **AC4**（headless 实测，deny）：真 opencode run + 并行 `tars serve` + 模拟 deny（前端 `/approval/respond` 或 broker 注入）→ 桥 throw → 工具被阻断（agent 收到错误）。**观测点（B6）**：记录 deny 后 agent 行为（停止 / 重试），retry≥3 视为需后续缓解（throw 文案含"不要重试"已缓解；实测确认）。
- **AC5**（cc/cac 非回归）：`tests/iface/in_session/test_orca_permission_hook.py` + `tests/iface/web/test_approval_broker.py` + `tests/iface/cli/test_install_permission_hook.py` 全绿（零回归）。
- **AC6**（守门，git diff）：`approval_broker.py` / `orca-permission-hook.py` / `routes/approval.py` / `profiles/builtin/{claude,ccr}.py` / `_install_cc_nudge` 零行改动。

---

## 7. install / 迁移

- 改 `orca.ts` 模板（加 `tool.execute.before` hook + `_askBroker` helper + fetch 失败语义）。
- `_install_opencode` 经 `_atomic_write_with_backup` 重写 `orca.ts`（既有机制，带备份）→ 用户重跑 `tars install --target opencode|nga` 即更新。
- `_install_cc_nudge`（cc+cac）零改动（I-4）。
- `.opencode/plugins/` auto-load 所有 `.ts`（spike 副发现）→ 确认生产 install 只落 `orca.ts`（`tool-classification.json` 非 `.ts`），无冲突（R4）。

---

## 8. 测试策略

- **Part A**：profile flags 静态断言（`tests/profiles/` 既有范式）+ test-agent headless 实测（AC1）。
- **Part B 单测**：`orca.ts` 是 TypeScript——抽取 hook 决策为纯函数 `_applyDecision(behavior)` + `_askBroker` mock，单测行为表（vitest 或抽取后 python fixture；具体形态 coder 定，对齐既有 orca.ts 测试惯例若无则建最小 vitest）。静态门：grep `tool.execute.before` 存在 + 失败分支注释。
- **Part B 集成（test-agent headless，spec-review B3）**：真 opencode run（`--format json`）+ **测试 fixture 并行启动 `tars serve`**（默认 7428，broker 实际宿主；headless `tars run` / `--background` 不起 HTTP broker——`create_app` 仅 `serve` 命令调用）+ yolo toggle → AC3/AC4。
- **非回归**：AC5 全绿。

---

## 9. 风险与开放问题（R）

- **R1（版本）**：测于 opencode 1.18.13；translator 注释锁 v1.14.22（NDJSON 协议层，非 plugin 层）。`--auto` + `tool.execute.before` 在 1.14.22 未测。**缓解**：用户实际装 1.18.13（`which opencode` 证）；profile docstring 版本注释更新为 1.18.13；若官方需支持 1.14.22 另测。spec-review 评判是否阻塞。
- **R2（await ceiling）**：spike 证 `tool.execute.before` tolerates ≥10s await；600s broker timeout 未测全。**缓解**：broker 既有 timeout/disconnect/abort 路径兜底（长 await 被 opencode 杀时 → broker disconnect → aborted，不崩）；test-agent headless 测真实容忍度。spec-review 评判是否需降 broker 默认 timeout。
- **R3（plugin env 通道，spec-review B2 已闭环）**：已定方案（§5）。**核实结论**：opencode plugin process.env == opencode 进程 env（in-process），但 **headless executor overlay 不注 `ORCA_HOST`/`ORCA_PORT`**（`exec/env.py` 只注 run 路由 env）→ plugin 依赖默认 7428 与 broker 默认端口吻合。自定义端口须用户显式设 env。
- **R4（auto-load 冲突）**：`.opencode/plugins/` auto-load 所有 `.ts`——确认生产只落 `orca.ts`。低风险（install 既有行为）。
- **R5（deny 的 agent 体验，spec-review B6 已闭环）**：`throw` 是 opencode 官方唯一 deny 机制（无原生 deny 返回值，`.env` 范例确认）。throw 文案含"不要重试"缓解 agent 循环；AC4 观测点实测确认 deny 后 agent 是否重试，retry≥3 视为需后续缓解。

---

## 10. 实现顺序（coder-agent）

1. Part A（`opencode.py` flags + docstring）——独立、最小、立即可测。
2. Part B（`orca.ts` hook + `_askBroker` + 失败语义 + env 配置）。
3. 单测（行为表 + 静态门）。
4. 自 code-review（重点 I-1~I-4 非回归 + git diff 守门 AC6）。
5. test-agent headless 实测（AC1/AC3/AC4/AC5）。
6. release note + CHANGELOG + CURRENT。

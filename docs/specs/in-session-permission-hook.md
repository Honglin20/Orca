# In-Session 权限审批 Web 桥 Spec

> **状态**：Draft v3.1（2026-08-05）。v1 FAIL→v2 闭环→evaluator N1–N13→v3 闭环→v3.1 折叠 task 4 实测（见 §12）。
> **冻结前置**：唯一剩余冻结门 = §9 #2 spike（PermissionRequest 在**交互式 CC + Task 子 agent** 下触发 + stdin 字段名）——需真机交互验证，自动化（`claude -p`）证不了（task 4 Q3 实证：非交互下 PermissionRequest **不触发**）。§3.4 timeout 上限已由 task 4 解阻塞（≥86400，无硬顶）。
> **范围**：仅 in-session 路径（宿主 CC 驱动，主 session 调 `orca next`）。`orca run` 后端路径已有 `HumanGate` 全套，不在本 spec。
> **排除**：AskUserQuestion over web（架构性不可行）；opencode 家族（CC-only 先行）。

---

## 0. 目标与非目标

### 目标
in-session workflow 运行期间，宿主 CC 的子 agent（或主 session）触发权限请求时：
1. **推到 web**：web 弹审批卡（工具 + 参数），用户在 web 上 allow/deny。
2. **超时默认放行**：hook 内部等待窗口内无人应答 → 按超时策略处理，**默认 `allow`**（notify-proceed）。策略可配（§3.5）。
3. **前端 yolo 开关**：开启后所有权限请求**立即** allow（不等、不超时），仅保留 web 可见性。
4. **只在 workflow 时启用**：无活跃 run（broker 判定）→ `ask` → CC 原生提示，不干扰日常 CC。
5. **不需要审批的不弹**：`PermissionRequest` 原生只在 CC 判定要问时触发。

### 🔴 安全取舍明示（reviewer B2 关注点，upfront 不沉默）
**默认超时→allow 是用户明示的 notify-proceed 语义（goal 2026-08-05），不是安全默认。** 含义：workflow 期间，未在窗口内及时否决的危险操作（含 `rm -rf` 类）**会执行**。这是用户对其自身环境的明示取舍。缓解：
- 超时策略可配 `ORCA_APPROVAL_TIMEOUT_POLICY=allow|ask|deny`（默认 `allow`）；要严格改 `ask`/`deny`。
- 与「broker 不可达→ask」不对称是 by design：超时 = web 在线、人慢；不可达 = web 没了、无 web 可审批。两者失败模式不同。
- 本 spec **不**做红线类（tool-classification deny-on-timeout）——`deny` 策略已覆盖全局严格；per-tool 红线留 follow-up（§9 #3）。
- web 默认 local-only（§8）；若经隧道远程审批，必须配 auth（N3 密钥泄露缓解）。

### 非目标
- 不做 AskUserQuestion over web。
- 不用 CC 原生 `--dangerously-skip-permissions`（用户环境不可用）。
- 不把审批决策写进 Orca tape（§1.2）。
- 不改 `AttachedRunHandle` read-only 契约。
- 不改现有 `cc_nudge.sh`。

---

## 1. 核心架构决策

### 1.1 为什么是新 broker（非 gate）
`HumanGateHandler`（`orca/gates/handler.py:53`）绑 in-process run handle + **写 tape**（`handler.py:108`）；in-session 的 run 是 `AttachedRunHandle`（`run_manager.py:152-166`，只读 follow tape，不该写 tape）。故新增与 run 解耦的 `ApprovalBroker`——独立 CC↔web 决策传输通道（非 gate），不碰 tape。「不破坏 read-only」是 byproduct；动机是审批非 workflow 事件、不该走 tape/gate 通道。

### 1.2 不写 tape（消除两真相源）
审批是 CC↔工具决策，非 workflow 事件。tape 记 workflow 真相；塞进去 = 第二真相源。审计走 hook 本地心跳文件（best-effort，非常驻真相源）。

### 1.3 数据流（hook 纯转发，broker 做 orca-aware 判定）
```
CC 子 agent 调危险工具 → CC 判定要问 → 触发 PermissionRequest hook
  hook 读 stdin（hook_event_name/tool_name/tool_input/session_id?）
  → POST /approval {session_id?, tool, tool_input, hook_event}
  → 阻塞等响应（内部 timeout = ORCA_APPROVAL_TIMEOUT）
Broker：
  resolve_session_context(manager.registry, payload)（精确反查 session→run）
   ├─ 未命中活跃 run → {behavior:"ask"}
   └─ 命中 → 建 approval(uuid4 id, run_id, tool, tool_input)
        ├─ yolo on → 即时 allow
        ├─ 用户 web allow/deny → resolve
        └─ broker timeout → 按策略（默认 allow）
  → HTTP {behavior, approval_id, resolved_by}
Hook emit {decision:{behavior}}：
  有 behavior→透传；内部 timeout→按策略；连接失败→ask；响应非法 JSON→deny+warn
```
**不变量**：`ORCA_APPROVAL_TIMEOUT < settings.json hooks.PermissionRequest.timeout`（留 30s 余量），hook 总在自己窗口内 emit → CC 不触发 hook-timeout 兜底（§3.4 spike 验上限）。

### 1.4 hook 退化为纯转发（闭环 B-3/B-4/B-10）
hook 只做 stdlib HTTP 转发；活跃 run / session→run 路由全在 broker 用现有函数（`resolve_session_context` / `_find_active_run_for_wf` / `marker`）。零复制 `_scan_my_active_run_ids`；精确路由不退化为 ids[0]；绕开 PermissionRequest env 未实证（hook 转发现有 session_id，broker 兜底 ask）。

---

## 2. CC PermissionRequest hook 契约

**输出 decision**（已验，task 1）：
```json
{"decision": {"behavior": "allow" | "deny" | "ask", "reason": "..."}}
```
（`updatedInput`/`updatedPermissions` 是 CC 协议字段但本 hook **不用**——防实现者猜测填充。）

**触发时机**：只在 CC 要问用户时（allow/deny 规则评估后）——原生过滤。

**hook timeout**：默认 600s（多数事件），`HookMatcher.timeout` 可配，无文档上限（仅 SessionEnd 封 60s）。实测 §3.4。

**🔴 输入 stdin 字段名未实证（N6，冻结前置）**：task 1 验了**输出**契约；PermissionRequest **输入** stdin 的字段名（`tool_name`/`tool_input`/`session_id` 实际叫什么、形状如何）未在 CC doc 引证。§9 #6 spike 实施第 0 步验；fallback：hook 容错读取多候选字段名（`tool_name|toolName`、`tool_input|toolUseInput|input`、`session_id|sessionId`），broker 据 hook_event 判定。

---

## 3. 组件

### 3.1 `orca-permission-hook.py`（新，stdlib-only，纯转发）
**位置**：`orca/iface/in_session/templates/orca-permission-hook.py`（与 `cc_nudge.sh` 同级；`templates/` 是 install 期源目录，不限扩展名——B-10）。

**铁律**：stdlib-only（`urllib`/`json`/`os`/`sys`/`uuid`/`time`），同 `hook_script.py:20-23`。

**逻辑**：
1. 读 stdin JSON，容错取 `tool_name`/`tool_input`/`session_id`（多候选字段名，N6）。
2. session_id = `ORCA_HOST_SESSION_ID` | `CLAUDE_CODE_SESSION_ID` | CAC PID 回溯 | stdin（任一可空）。CAC（CC 换皮）不注入 `CLAUDE_CODE_SESSION_ID`，两 env 键皆空时 hook 沿 PID 链回溯 `codeagentcli` 父进程读 `~/.cac/sessions/<pid>.json`——与 `_hostenv.host_session_from_env` / `cc_nudge.sh` 同源（tape `data.host_session` 由同款逻辑写出 → broker 双键命中）。CC 路径 env 第二键即短路，不触达 PID 回溯。
3. POST `/approval`，`timeout = ORCA_APPROVAL_TIMEOUT`（env，默认 §3.4）；支持 `ORCA_HOST`（跨边界，N9）+ `ORCA_PORT`（默认 7428）。
4. emit：
   - 响应 `behavior` → 透传。
   - stdlib timeout → 按策略（§3.5，默认 allow）。
   - 连接失败（urllib `URLError`/网络）→ `ask`（fail-open to native）。
   - HTTP 4xx/5xx（broker 活着但出错）→ `deny` + stderr warn（fail loud，N4）。
   - 响应非 JSON → `deny` + stderr warn。

**与 hook_script.py 区别**：协议（JSON vs exit code）/事件/连接失败语义（ask vs deny）/无活跃判定（broker 侧）——有意反转，白纸黑字。

### 3.2 `ApprovalBroker`（web 侧新，与 run 解耦）
**位置**：`orca/iface/web/approval_broker.py`。

**生命周期（B-12）**：**进程级 singleton**，FastAPI `app.lifespan` 启动时构造、关闭时清理 pending + 广播 `approval_resolved(resolved_by:"shutdown")`。**pending 池在内存，随进程死**（不持久化；broker 重启 = pending 弃）。单一 `tars serve` 假设（多实例同端口不在范围）。

**并发模型（B-8）**：per-approval `asyncio.Future` + `threading.Lock` 仅护 resolve 段（仿 `handler.py:79`）。`POST /approval` 是 `async def`，`await fut`（不阻塞 uvicorn worker）。无并发上限，各 approval 独立并发；前端按 approval_id 渲染多卡。

**approval_id（N1）**：`uuid4`（**禁** `run_id-timestamp` 类可碰撞方案——同秒并发会 overwrite pending）。

**核心**：
- `async request(payload) -> {behavior, approval_id, resolved_by}`：
  1. `resolve_session_context(manager.registry, payload)`（复用 `http_endpoint.py:46`）精确反查 `(run_id, node)`。未命中 → `{behavior:"ask", resolved_by:"native-fallback"}`。
  2. 建 `Approval(id=uuid4, run_id, tool, tool_input_redacted, created_at, fut)`。
  3. yolo on → 立即 resolve("allow")。
  4. `await asyncio.wait_for(fut, BROKER_TIMEOUT)`：resolve/超时/disconnect。
  5. 广播 `approval_resolved`，返结果。
- `resolve(approval_id, answer, source) -> bool`：first-wins（Lock）。**已 resolve 后的 late respond**（N2）：返 `ok:false`；emit 独立 `approval_resolved_late`（仅审计可见，不翻盘，不误导 UI）。
- `BROKER_TIMEOUT = ORCA_APPROVAL_TIMEOUT - 5s`（比 hook 短，保证 broker 先 resolve、hook 收到响应）。
- **HTTP disconnect（B-4/B-7，P1 并发组合）**：POST 路由用 `asyncio.gather(wait_for(fut, BROKER_TIMEOUT), _disconnect_poller(req, fut))` 组合「超时」与「断连」两条竞速——`wait_for` 等 resolve/超时，`_disconnect_poller` 每 1s 调 `request.is_disconnected()`，断连则 `resolve(approval_id, "aborted", "disconnect")`。任一完成即 cancel 另一。resolve("aborted") 后从 pending 移除 + 广播 `approval_resolved(resolved_by:"aborted")`（**不发 allow**，hook 已死 CC 已兜底）。

**broker-timeout 策略（N7）**：broker timeout 与 hook timeout 同走 `ORCA_APPROVAL_TIMEOUT_POLICY`（默认 allow）。N7 建议 broker-timeout=deny/hook-timeout=allow 的分离被否决——保持单一策略（简单 + 用户语义一致：「窗口内无人答」= 按策略）。

### 3.3 yolo 开关
- 前端工具栏 toggle，默认 off。
- 存储：broker 内存 + `~/.orca/approval-yolo.json`（全局，与 per-run `runs/` 解耦——B-16）；重启恢复。
- on 时 `request()` 即时返 allow（不阻塞）；WS 仍广播 requested+resolved(allow) 供可见。
- **范围（B-6）**：yolo 是 **broker 全局**开关（跨你所有 run）。显式声明：**开 yolo = 你信任当前所有活跃 run 的工具链**；多 run 并行时慎用。本期不做 per-run yolo（YAGNI）。

### 3.4 CC hook timeout 上限（task 4 已实测，解阻塞）
**实测结论**（task 4，claude 2.1.207，Windows+Git Bash）：
- `timeout` 字段**被严格遵守**（timeout=3 杀于 3.16s；timeout=20 跑满 10s）——非固定默认。
- 大值 **86400（24h）被解析接受**，无 600 硬顶、无 validation clamp。
- **两层职责分离**（关键设计）：
  - **CC hook `timeout` = 86400**（实测接受）——传输层 kill 开关，设到永不误杀一个正在等人的审批 hook。**绝不依赖 CC 的 hook-timeout 兜底**（task 4 旁证：PreToolUse 被 CC 杀后，CC 是否阻断该 tool **行为不一致**——故 §1.3 不变量「hook 自己窗口内返」是硬约束，非优化）。
  - **`ORCA_APPROVAL_TIMEOUT` 默认 600s**（语义级「人未在合理时间响应」，与 CC 内部限制无关，600 不撞 CC 任何魔数）。可配。
- **不变量** `ORCA_APPROVAL_TIMEOUT(600) < CC timeout(86400)` 现实测可满足，余量巨大。

### 3.5 超时策略（可配，闭环 B-1）
超时默认 = **`allow`**（用户 goal 明示）。`ORCA_APPROVAL_TIMEOUT_POLICY=allow|ask|deny`（默认 allow）。安全取舍见 §0。

---

## 4. 数据契约

### 4.1 POST `/approval`（hook → broker）
```
POST /approval
Body: {session_id?: str, tool: str, tool_input: object, hook_event: "PermissionRequest"}
200 → {behavior: "allow"|"deny"|"ask", approval_id: str, resolved_by: "user"|"yolo"|"timeout"|"native-fallback"}
4xx/5xx → （hook 视作 broker 出错 → deny+warn，N4）
```
（v1 的 409 删除——无定义触发，B-13。）

### 4.2 POST `/approval/respond`（前端 → broker）
```
Body: {approval_id: str, answer: "allow"|"deny", source?: "web"}
→ {ok: bool, approval_id, resolved_by: str}
```
first-wins；late → `ok:false` + 审计 `approval_resolved_late`（N2）。

### 4.3 WS 事件（broker → 前端，复用 /ws 单通道）
```
approval_requested   {approval_id, run_id, tool, tool_input_redacted, created_at}
approval_resolved    {approval_id, behavior, resolved_by: "user"|"yolo"|"timeout"|"aborted"|"shutdown"}
approval_resolved_late {approval_id, answer, note:"late-respond-after-resolve"}  # N2 仅审计
yolo_changed         {yolo: bool}
approval_snapshot    {approvals:[...pending], yolo: bool}   # (re)connect 恢复，B-5/N5/N13
```
- **redact（N3 安全，P3 扩展）**：`tool_input` 广播前 redact 已知密钥模式（env 名 `_TOKEN|_KEY|_PASSWORD|_SECRET`、URL `user:pass@`、API key 前缀 `sk-ant-|sk-`、`Authorization`/`Cookie` header）→ `***`。原文仅存 broker 内存（不广播、不落盘）。**正则非穷尽**；扩展走 `ORCA_APPROVAL_REDACT_PATTERNS` env（逗号分隔正则，追加默认列表）。远程审批场景必须配 auth（§8），不可仅依赖 redact。
- **run-scoped 投递（N10，P4 集成路径）**：approval 事件**不**通过 `handle.bus`（保持审批/事件总线分离 + 不污染 attached-run read-only follow）。broker 暴露 `subscribe(run_id) -> AsyncIterator[ApprovalEvent]`；`ws_handler._handle_subscribe` 在起 workflow bus pump 的同时，起**第二条 approval pump**（共用 `_WSConnection.queue` 串行化，与现有 `_writer_loop` 协同）。broker 只对「该 run_id 有 pending approval 且 WS 已订阅该 run」的连接推送 `approval_requested`。多 tab：各 tab 订阅各自 run，卡出现在正确 tab。
- **snapshot 拉取（P2 传输通道）**：前端（re）connect 后发 **WS 消息** `{type:"request_approval_snapshot"}`（与 `subscribe` 同 WS 通道，在 `ws_handler._dispatch` 加分支），broker 返 `approval_snapshot` 帧（含 pending + yolo 状态，N5）。**不用 HTTP**（避免新鉴权边界）。
- **snapshot 权威（N13）**：`approval_snapshot` 是权威 pending 集——不在 snapshot 里的本地卡清掉（防 broker 重启后 stale 卡）。
- 前端独立 `useApprovalStore`，**不复用** `useWorkflowStore.gate`（避免两真相源）。

### 4.4 settings.json hook 声明（install 写入）
**扩展 `_install_cc_nudge`（B-11，非新函数）**：在同一次 `_merge_json_file` 事务里追加 `hooks.PermissionRequest`（去重关键字 `"orca-permission"`）+ 拷 `orca-permission-hook.py` 到 `<root>/hooks/` + `chmod 0o755`（N8 exec bit）。单事务合并，无顺序/回滚问题。
```json
"PermissionRequest": [
  {"hooks": [{"type": "command", "command": "python <abs>/orca-permission-hook.py", "timeout": <T_CC_MAX>}]}
]
```
install 写入 hook 运行 env：`ORCA_PORT`（默认 7428）+ `ORCA_HOST`（默认 127.0.0.1，跨边界可改，N9）+ `ORCA_APPROVAL_TIMEOUT` + `ORCA_APPROVAL_TIMEOUT_POLICY`。

---

## 5. 复用锚点（file:line）

| 资产 | 位置 | 接法 |
|---|---|---|
| HTTP 转发范式 | `orca/gates/hook_script.py:59-125` | 借鉴，语义反转 |
| session→run 反查 | `orca/gates/http_endpoint.py:46` `resolve_session_context` | broker 直接复用 |
| first-wins/Lock/Future | `orca/gates/handler.py:79,91-181` | 借鉴模式 |
| 活跃 run/marker | `orca/iface/in_session/marker.py`、`cli.py:860-920` | broker 侧用 |
| install 落点 | `install_cmds.py:343-432` `_install_cc_nudge` | **扩展**同函数 |
| 前端审批卡视觉 | `PermissionGate.tsx:27` | 复用视觉，数据源 approval store |
| WS dispatch/subscribe | `ws_handler.py:193-200,_handle_subscribe` | 加 `approval_*` + per-run 投递 |
| 路由注册 | `iface/web/server.py:49-117` | 加 `/approval` + `/approval/respond` |

**不碰**：`HumanGateHandler` / `routes/gate.py` / `AttachedRunHandle` / tape / `cc_nudge.sh`。**零复制** `_scan_my_active_run_ids`。

---

## 6. 模式矩阵

| 模式 | 触发 | 行为 |
|---|---|---|
| 无活跃 run | broker 未命中 | `ask`（CC 原生） |
| 活跃 + yolo off + 应答 | respond | 用户 decision |
| 活跃 + yolo off + 超时 | timeout | 策略（默认 allow） |
| 活跃 + yolo on | 即时 | allow（WS 可见） |
| broker 不可达 | hook 网络失败 | ask |
| broker HTTP 错 | 4xx/5xx | deny+warn |
| hook 被 CC 杀 | broker disconnect | aborted（不发 allow） |
| broker 重启 | 进程死 | pending 弃 + shutdown 广播 |

---

## 7. fail-loud / fail-open 边界

| 场景 | 行为 | 理由 |
|---|---|---|
| broker 不可达 | `ask` | 无 web 可审批，退原生 |
| broker HTTP 4xx/5xx | `deny`+warn | broker 活着但出错，fail loud（N4） |
| 响应非 JSON | `deny`+warn | fail loud |
| 内部 timeout | 策略（默认 allow） | 用户明示 notify-proceed |
| hook 被 CC 杀 | broker `aborted` | hook 死，CC 已兜底 |
| broker 重启 | pending 弃 | 进程级，不持久化 |
| broker 重启期（hook POST 中） | hook TCP 失败 → `ask`（**不论策略**，安全方向降级） | 等同 broker 不可达，§6 对齐 |
| session_id 全空（hook 三候选均无） | broker `resolve_session_context` 返 unknown → `ask` | 行为同未装；install AC 要求至少一个来源可拿到 |
| marker 损坏（broker 侧 resolve_session_context） | 该请求 `ask`+stderr，不崩 broker | fail loud 单点 |

---

## 8. 部署约束
- 同机/同 WSL：hook POST `127.0.0.1:{ORCA_PORT}`；跨 WSL/Windows 配 `ORCA_HOST`（in-session 既有风险，本 spec 不解决）。
- `tars serve` 必须运行，否则 fail-open to native。
- **local-only 默认 + 远程需 auth（N3）**：web 默认 bind localhost；经隧道/ngrok 远程审批必须配 auth（tool_input redact 是兜底，非充分——远程明文通道仍可能泄露非密钥敏感信息）。
- 端口默认 7428。
- `orca doctor` 加 `approval_broker` 心跳检查（N12）。

---

## 9. 待定 / 风险 / Follow-up

| # | 项 |
|---|---|
| 1 | ✅ CC hook timeout 上限已实测（task 4：≥86400，无硬顶，§3.4）。 |
| 2 | **🔴 冻结前置 spike（N6 + Q3）**：(a) PermissionRequest **stdin 字段名**（`tool_name`/`tool_input`/`session_id` 实际叫法）；(b) **PermissionRequest 是否在「交互式 CC + Task 子 agent」下触发**——task 4 Q3 实证 `claude -p` 非交互下**不触发**；in-session 部署是交互式 CC（用户终端的 `claude`），理论上触发（它就是原生提示事件），但「子 agent 工具调用是否触发」**未验**，必须真机交互验证（自动化 `claude -p` 证不了）。hook 容错多候选字段名兜底；若 spike 发现子 agent 不触发 → fallback 改用 PreToolUse（牺牲「只问需要的」过滤，hook 需对每个工具决策——登记为 Q3 风险）。 |
| 3 | opencode 家族对等（CC-only 先行）。 |
| 4 | 红线类（per-tool deny-on-timeout）——`deny` 策略已覆盖全局，per-tool 留 follow-up。 |
| 5 | WSL/Windows ORCA_HOST —— in-session 既有风险。 |
| 6 | **Q3 风险登记 + fallback 判据（P6/P7/Q3）**：若 PermissionRequest 在目标场景不触发，整个「只在需要时弹」过滤失效。**fallback 触发判据**：#2 spike 验「子 agent 工具调用触发 PermissionRequest」**完全失败**（任何子 agent 都不触发）→ 切 PreToolUse；**部分失败**（仅某些子 agent 类型不触发）→ 不切，登记为已知漏检。**fallback 实施注意**：(1) PreToolUse 输出 `block`（非 PermissionRequest 的 `deny`），hook 输出枚举与 broker HTTP `behavior` 契约需同步调整；(2) tool-classification.json 现状无 `readonly_tools` 白名单（只有 `writing_tools` 等，rogue-guard 用），fallback 需**扩 `readonly_tools` 字段**（独立于 rogue-guard 的 `writing_tools`，不混用真相源）做「只读类直 allow」过滤；(3) fallback 切换是 **install 期决策**（hook 脚本 + settings.json 注册的事件类型），非运行期——升级走重装。 |

---

## 10. 参考
- CC hook 契约：task 1（claude-code-guide，code.claude.com/docs/en/hooks）——验**输出** decision 契约；**输入** stdin 字段名待 §9 #2 spike（N6）。
- Orca 资产接口：task 2（Explore）。
- spec-review：task 5 round 1（FAIL 5 阻塞）+ evaluator（N1–N13）→ v2/v3 闭环（§12）。

---

## 11. 验收标准

**安装**：
- `tars install --target cc` 装上 `orca-permission-hook.py`（`.claude/hooks/`，**exec bit 755**——N8）+ `settings.json` 含 `hooks.PermissionRequest`（timeout=§3.4 值）+ hook env（ORCA_PORT/HOST/TIMEOUT/POLICY）。

**行为**：
- 无活跃 run：CC 调工具 → broker `ask` → hook 透传 → CC 原生（行为同未装）。
- 活跃 + yolo off + 写类工具 → web 弹卡（run-scoped 到正确 tab，N10）→ deny→不执行；allow→执行。
- 活跃 + yolo off + 超时 → 策略（默认 allow）+ web `resolved_by:timeout`。
- yolo on → 立即 allow（< 500ms，N8 量化）+ web 瞬时 requested+resolved(yolo)。
- broker 不可达 → `ask` → CC 原生。
- broker HTTP 4xx/5xx → hook `deny`+warn（N4）。

**鲁棒/正确**：
- marker 损坏：broker `resolve_session_context` stderr + 该请求 `ask`，broker 不崩。
- **WS 断连重连**（B-5）：断连期间 pending，重连经 WS `request_approval_snapshot` → `approval_snapshot` 帧可见可审批（P2 传输通道）。
- **session_id 来源**（P5）：install 后至少一个来源（env `CLAUDE_CODE_SESSION_ID`/`ORCA_HOST_SESSION_ID` 或 stdin）可拿到；三候选全空 → broker `ask`（行为同未装，§7）。
- **多 run 路由**（B-4）：按 session_id 精确路由，不串台（evaluator 跨切 counterexample 不成立）。
- **多 tab**（N10）：approval 卡只在订阅该 run 的 tab 出现。
- **approval_id 唯一**（N1）：N 并发请求 → N 个 uuid4，无碰撞。
- **late respond**（N2）：resolve 后再点 → `ok:false` + `approval_resolved_late`，UI 不翻盘。
- **密钥不泄露**（N3）：`tool_input` 含 `_TOKEN` 等被 redact 后才入 WS 广播（单测断言）。
- **yolo 持久化**（B-8）：重启 serve 从 `~/.orca/approval-yolo.json` 恢复。
- **broker 重启清 stale**（N13）：重启后前端 snapshot 不含的本地卡被清。
- **超时可测**：`ORCA_APPROVAL_TIMEOUT_TEST_OVERRIDE` env 缩到秒级。
- **doctor**（N12）：`orca doctor` 报 `approval_broker=pass`（serve 在线）/`fail`（offline）。
- **e2e via test-agent**（非 CI）：broker-unreachable→ask、真实审批流由 test-agent 真机验（claude -p），CI 只跑单测。

**grep 守门（N11 精确）**：`orca-permission-hook.py` 仅 import stdlib（`urllib`/`json`/`os`/`sys`/`uuid`/`time`）；`approval_broker.py` 不 import `orca.gates.handler` / `orca.tape*` / `orca.exec.*` / `orca.events.bus`（结构化 import 检查，非裸 grep）。

---

## 12. Changelog

**v1→v2**（round-1 spec-review 5 阻塞）：B-1 超时策略可配 / B-2 §3.4 冻结前置 / B-3 hook 纯转发零复制 / B-4 精确路由 / B-5 approval_snapshot / B-6 asyncio.Future+Lock / B-7 disconnect-abort / B-8/16 yolo 全局+落盘 / B-9 删死字段 / B-10 env 兜底 / B-11 AC 补 / B-13 install 顺序 / B-15 并发。

**v3.1→v3.2**（round-3 spec-review conditional-pass，补 P1–P8 实施手册级条款）：
- P1：§3.2 broker `gather(wait_for, _disconnect_poller)` 并发组合明确。
- P2：§4.3 snapshot 走 WS `request_approval_snapshot` type（非 HTTP）。
- P3：§4.3 redact 加 `ORCA_APPROVAL_REDACT_PATTERNS` env + 盲区声明。
- P4：§4.3 approval 事件走 broker `subscribe(run_id)` + ws_handler 第二条 pump（不经 handle.bus）。
- P5/P8：§7 加 session_id 全空→ask、broker 重启期→ask 两行；§11 加 session_id 来源 AC。
- P6/P7/Q3：§9 #6 fallback 判据（完全失败才切）+ PreToolUse `block` 枚举差异 + tool-classification.json 扩 `readonly_tools` 子任务 + install 期切换。

**v3→v3.1**（task 4 实测折叠）：
- §3.4 解阻塞：CC timeout 上限 ≥86400（无 600 硬顶），字段被严格遵守。两层分离：CC hook timeout=86400（永不误杀）/ ORCA_APPROVAL_TIMEOUT=600（语义级）。
- task 4 Q3：`claude -p` 非交互下 PermissionRequest **不触发** → 唯一剩余冻结门 = 真机验「交互式 CC + 子 agent 下触发」（§9 #2），自动化证不了。fallback = PreToolUse + broker 分类（§9 #6）。
- 旁证：PreToolUse 被 CC 杀后行为不一致 → 强化「hook 自管窗口、不靠 CC 兜底」不变量（§3.4）。

**v2→v3**（evaluator N1–N13）：
- N1 approval_id=uuid4（禁碰撞方案）。
- N2 late respond → `approval_resolved_late` 审计，不翻盘。
- N3 tool_input redact 密钥 + 远程需 auth（§0/§4.3/§8）。
- N4 HTTP 4xx/5xx → deny（区别网络失败→ask）。
- N5 yolo 状态入 snapshot。
- N6 PermissionRequest stdin 字段名 spike（§2/§9 #2 冻结前置）+ hook 容错。
- N7 broker-timeout 策略统一（分离建议否决，记因）。
- N8 exec bit AC + 立即 allow 量化。
- N9 ORCA_HOST 支持 + install 写 env。
- N10 approval run-scoped 投递（多 tab 不串台）。
- N11 grep 守门精确化（结构化 import 检查）。
- N12 doctor 心跳 AC。
- N13 snapshot 权威清 stale 卡。
- B-11 改「扩展 `_install_cc_nudge`」单事务（非新函数）。
- B-12 broker lifespan singleton 显式。
- B-2 §0 顶部安全取舍明示（upfront，不沉默默认）。

# 2026-08-05 — In-Session 权限审批 Web 桥

**SPEC**：`docs/specs/in-session-permission-hook.md` v3.2（spec-review conditional-pass）。
**范围**：in-session workflow 运行期间，宿主 CC 的 PermissionRequest → web 审批卡 → 用户 allow/deny；
超时默认 allow（可配）；前端 yolo 开关。**不碰** tape / HumanGateHandler / AttachedRunHandle。

**修订 r2（2026-08-05，test-agent e2e 后）**：修 BUG A（`_disconnect_poller` 缺 await →
timeout 路径失效）/ BUG B（redact 空白截断 → Bearer token 泄露）+ 2 防回归单测 + 全场景 e2e 重验。

---

## 1. 改动清单

### 新增

- **`orca/iface/in_session/templates/orca-permission-hook.py`**：CC PermissionRequest hook
  HTTP 桥，**stdlib-only**（urllib/json/os/sys/time/socket）。读 stdin（容错多候选字段名：
  `tool_name|toolName` / `tool_input|toolUseInput|input` / `session_id|sessionId`）→ 收集
  session_id（`ORCA_HOST_SESSION_ID` | `CLAUDE_CODE_SESSION_ID` | stdin）→ POST `/approval` →
  emit `{decision:{behavior}}`。失败语义（SPEC §7）：
  - broker 不可达（URLError / 连接失败）→ `ask`（fail-open to CC native prompt）。
  - HTTP 4xx/5xx → `deny` + stderr warn（fail loud）。
  - 响应非 JSON / behavior 非法 → `deny` + stderr warn。
  - stdlib timeout → `ORCA_APPROVAL_TIMEOUT_POLICY`（默认 allow，用户明示 notify-proceed）。
  - Python 3.10+ `urlopen` 超时抛 builtin `TimeoutError` —— 同时 catch `URLError`/`TimeoutError`/`OSError`。

- **`orca/iface/web/approval_broker.py`**：进程级 singleton broker（FastAPI `app.lifespan`
  构造 / shutdown 清理）。`async request(payload, http_request)` + 同步 `resolve`。
  - approval_id = `uuid4`（N1，禁碰撞方案）。
  - per-approval `asyncio.Future` + `threading.Lock` first-wins；late respond →
    `approval_resolved_late` 审计事件（不翻盘 UI，N2）。
  - 并发组合：``asyncio.gather`` 改写为「手写 timer task + disconnect poller task + 直接 await fut」
    三路竞速——避免 `asyncio.wait_for` cancel 底层 future 触发 `InvalidStateError`。
  - `BROKER_TIMEOUT = ORCA_APPROVAL_TIMEOUT - 5s`（比 hook 短，保证 broker 先 resolve）。
  - yolo：内存 + `~/.orca/approval-yolo.json` 持久化（重启恢复）；on → 即时 allow（< 500ms）。
  - **不 import** `orca.gates.handler` / `orca.tape*` / `orca.exec.*` / `orca.events.bus`（grep 守门 N11）。
  - tool_input redact：env 名 `_TOKEN|_KEY|_PASSWORD|_SECRET`、URL `user:pass@`、`sk-ant-`/`sk-`、
    `Authorization`/`Cookie` header → `***`；`ORCA_APPROVAL_REDACT_PATTERNS` env 追加正则（N3）。
  - `subscribe(run_id) -> asyncio.Queue` + `_publish` 推 ApprovalEvent 给该 run 所有订阅者（N10
    run-scoped，不串台）。

- **`orca/iface/web/routes/approval.py`**：HTTP 端点
  `/approval`（hook → broker.request，透传 Request 用于 disconnect 探测）+
  `/approval/respond`（first-wins）+ `/approval/yolo`（toggle）+ `/approval/snapshot`（程序化客户端）。

- **前端**（独立 `useApprovalStore`，不复用 `useWorkflowStore.gate`）：
  - `stores/approval-store.ts`：snapshot 权威（N13）、approval_requested/resolved/resolved_late/yolo_changed。
  - `components/gate/ApprovalDialog.tsx`：多卡 stack 渲染，复用 PermissionGate 视觉；`POST /approval/respond`。
  - `components/gate/YoloToggle.tsx`：工具栏 toggle，`POST /approval/yolo`。
  - `components/gate/post-approval-respond.ts`：DRY POST helper。
  - `hooks/use-websocket.ts`：onopen 发 `request_approval_snapshot`；onmessage 按 `kind==="approval"` 路由到 store。

### 修改

- **`orca/iface/web/server.py`**：`create_app` 构造 `ApprovalBroker(manager.registry)`，
  lifespan shutdown 先调 `broker.shutdown()` 再 `manager.shutdown()`；挂 approval router；
  `WebServer(manager, approval_broker=broker)`。
- **`orca/iface/web/ws_handler.py`**：
  - `WebServer.__init__(manager, *, approval_broker=None)`（可选，旧路径兼容）。
  - `_RunSubscription` 加 `approval_pump` / `approval_queue` 字段。
  - `_handle_subscribe`：起第二条 approval pump（`broker.subscribe(run_id)`），并立即 enqueue 一份
    `approval_snapshot`（按 run scoped）。
  - `_dispatch` 加四个 type：`request_approval_snapshot` / `approval_respond` / `approval_yolo`
    （与 HTTP 等价）+ 既有的 `subscribe/unsubscribe/gate_response/resume`。
  - 新增 `_approval_pump` / `_handle_approval_snapshot` / `_handle_approval_respond` /
    `_handle_approval_yolo`；`_cancel_sub` 同步 cancel approval pump + unsubscribe。
- **`orca/iface/cli/install_cmds.py`**：
  - 新增 `_cc_permission_hook_src()`。
  - 扩展 `_install_cc_nudge`：拷 `orca-permission-hook.py` 到 `<root>/hooks/` + chmod 0o755；
    settings.json 单事务合并 `hooks.PermissionRequest`（去重关键字 `orca-permission`），
    `timeout=86400`（SPEC §3.4 实测无硬顶），env 写入 `ORCA_PORT/HOST/TIMEOUT/POLICY`
    （install 时若用户已设则继承，N9 跨边界）。
- **`orca/iface/in_session/cli.py`**：`doctor` 加 `_check_approval_broker`（hard=False）
  探测 `GET /approval/snapshot`，serve 在线 pass / offline fail（N12）。
- **`orca/iface/web/frontend/src/types/store-types.ts`**：`WsClientMessage` 加
  `request_approval_snapshot` / `approval_respond` / `approval_yolo` 三型。
- **`orca/iface/web/frontend/src/App.tsx`**：`SingleRunRoot` 加 `<ApprovalDialog runId={runId} />`。
- **`orca/iface/web/frontend/src/components/layout/TopBar.tsx`**：工具栏加 `<YoloToggle />`
  （仅 runId 存在时渲染）。

---

## 2. 测试

新增五个单测文件（全绿，约 4s）：

- `tests/iface/web/test_approval_broker.py`（**26 测试**）：request/resolve happy、first-wins、
  late-respond 审计事件、yolo 即时 allow + 持久化、timeout-policy allow/ask/deny、
  disconnect-abort、uuid 唯一、redact（env / URL / sk- / Authorization / Cookie / 自定义 env 正则）、
  **BUG A 回归：真实 starlette Request + 未断连 → 必须走 timeout-policy（防 `await` 缺失复发）**、
  **BUG B 回归：Authorization Bearer / Cookie 完整值不残留**、
  subscribe per-run 不串台、shutdown 广播 resolved(shutdown)、snapshot、
  import 守门（broker 不 import tape/handler/exec/events.bus）、hook stdlib AST 守门。
- `tests/iface/in_session/test_orca_permission_hook.py`（15 测试）：emit / pick /
  session_id 候选、各分支（透传 allow/deny、timeout-allow/ask、broker 不可达 ask、HTTP 4xx/5xx deny、
  非 JSON deny、stdin 非 JSON deny、invalid behavior deny、非法 policy 回退、多字段名容错）。
- `tests/iface/cli/test_install_permission_hook.py`（9 测试）：落地 / exec bit /
  timeout=86400 / env 写入 / 用户 env 继承 / 幂等 / 保已有 settings / cac 对称 / 内容匹配随包。
- `tests/iface/web/test_approval_ws_integration.py`（6 测试）：subscribe 自动 snapshot、
  broker publish → WS frame、approval_respond WS 反向 → broker.resolve、切 run unsubscribe 旧 pump、
  approval_yolo WS 反向、request_approval_snapshot 双保险。
- `tests/iface/web/test_approval_routes.py`（8 测试）：HTTP 端点分派（含 BUG A 闭环辅助：
  broker.request 异常 → HTTP 500 → hook deny+warn；resolve_session_context 异常 → HTTP 500）。

回归：`tests/iface/cli/test_install_cmds.py` + `tests/iface/web/test_ws.py` +
`tests/iface/web/test_routes.py` 全 100 测试 pass。前端 31 文件 527 测试 pass，tsc + vite build 通过。

### BUG A / BUG B 修复（2026-08-05 test-agent e2e 发现）

- **BUG A（P1）**：`approval_broker.py:_disconnect_poller` 缺 `await`（starlette
  `Request.is_disconnected` 是 `async def`）→ coroutine 永远 truthy → 首次 1s poll 即误判断连 →
  所有审批 force-abort 成 `resolved_by:"disconnect"`，**timeout 路径完全失效**（用户明示的
  notify-proceed timeout-allow 被破坏），且 hook 收到非法 `behavior:"aborted"` → fail-loud deny。
  修复：`disconnected = await http_request.is_disconnected()`。回归测试
  `test_real_starlette_request_not_disconnected_does_not_abort` 用真实 starlette Request
  + 阻塞 receive（不发 disconnect）+ broker timeout=2s，断言结果 = `behavior:"allow"` +
  `resolved_by:"timeout"`（CI 立即捕获 await 缺失）。
- **BUG B（P2）**：`_DEFAULT_REDACT_PATTERNS` 的 `(authorization|cookie)\s*[:=]\s*[^\s&,]+`
  在空白截断 → `"Authorization: Bearer abcdef"` 仅替换 `Bearer` 前缀，token 主体 `abcdef` 残留泄露。
  修复：`(?im)(authorization|cookie)\s*[:=]\s*.+$`（多行 + 行尾锚定，覆盖完整字段值）。
  回归测试 `test_redact_authorization_and_cookie_full_value_no_leak` 断言 Bearer token 主体
  + Cookie 值不残留，且多行 header block 每行独立 redact。

**test-agent 真机 e2e 复跑结果**（`_e2e_artifacts/permission/{driver,scenario}.py`，
real uvicorn + real hook subprocess）：

| 场景 | 结果 | 备注 |
|---|---|---|
| `timeout-allow`（policy=allow, timeout=3s） | ✅ hook 输出 `behavior:"allow"`（3.08s 后），原 BUG A 输出 aborted | BUG A 闭环 |
| `timeout-ask`（policy=ask） | ✅ hook 输出 `behavior:"ask"`（3.07s 后） | timeout-policy 正确 |
| `redact` | ✅ `CONTAINS_secretpw=False` / `CONTAINS_sk-ant-xxx12345=False` / `CONTAINS_Bearer_abcdef=False` | BUG B 闭环 |
| `ask-no-active-run` | ✅ `behavior:"ask"` | 未注册 session |
| `ask-via-empty-session` | ✅ `behavior:"ask"` | session_id 全空 |
| `broker-unreachable`（port 65530） | ✅ `behavior:"ask"`（fail-open to native） | connection refused |
| `user-allow` | ✅ pending 广播含 redact 后 tool_input + respond 200 ok | 全链通 |

---

## 3. SPEC 偏差与 spike 标注

- **§9 #2 spike（PermissionRequest 是否在「交互式 CC + Task 子 agent」下触发 + stdin 字段名）未在编码期验证**：
  按 SPEC §9 #6 fallback 判据，编码按 PermissionRequest 主路径实现，hook 容错读取多候选字段名
  作为 stdin 名 spike 兜底。**真机交互验证留 pre-production test-agent**（自动化 `claude -p` 证不了，
  task 4 Q3 已实证）。若 spike 发现「完全失败」→ 切 PreToolUse，本实现 hook event 类型已做成 install 期
  可切（`_install_cc_nudge` 内 `permission_cmd` 集中点），但 PreToolUse 输出枚举（`block`）+ broker
  `behavior` 契约需同步调整 + tool-classification.json 需扩 `readonly_tools` 字段——属独立子任务。
- **timer task + poller task 取代 `asyncio.gather(wait_for, _disconnect_poller)`**：SPEC §3.2 P1 字面
  描述是 gather 组合，但实现发现 `asyncio.wait_for` 在超时会 cancel 底层 future，使后续 `_resolve_locked`
  的 `fut.set_result` 抛 `InvalidStateError`。改成显式 `_timeout_timer` task + 直接 `await fut`，
  语义等价（任一赢家 first-wins），代码更清晰。记因：保持 SPEC「三条竞速」不变量，实现细节落地。
- **ApprovalBroker 不持久化 pending**（SPEC §3.2 by design）：进程死 = pending 弃，前端经
  `approval_snapshot` 权威集清 stale 卡（N13）。本期不做 pending 持久化（YAGNI）。
- **未做 per-tool 红线**（SPEC §9 #3）：`ORCA_APPROVAL_TIMEOUT_POLICY=deny` 已覆盖全局严格，
  per-tool deny-on-timeout 留 follow-up。

---

## 4. 提交

未提交（用户明示「先不要 commit」）。改动待审。本 release note 索引待 commit 后写入 CHANGELOG。

---

## 5. 验证

| AC（SPEC §11） | 单测覆盖 | 备注 |
|---|---|---|
| 无活跃 run → ask | `test_request_unknown_session_returns_ask` | registry 未注册 |
| 活跃 + yolo off + user allow/deny | `test_request_resolve_happy_path` + ws integration | run-scoped 验证 |
| 活跃 + 超时 → policy | `test_timeout_policy_{allow,deny,ask}` | 默认 allow |
| yolo on 立即 allow < 500ms | `test_yolo_on_immediate_allow` | elapsed 断言 |
| broker 不可达 → ask | `test_main_broker_unreachable_returns_ask` | hook |
| HTTP 4xx/5xx → deny | `test_main_http_error_returns_deny` | hook |
| WS 断连重连 → snapshot 可见可审批 | `test_request_approval_snapshot_message_returns_snapshot` + 前端 store | |
| session_id 全空 → ask | `test_request_session_id_missing_returns_ask` | |
| approval_id uuid 唯一 | `test_uuid_uniqueness_concurrent_requests` | N 并发 |
| late respond → ok=False + 审计事件 | `test_late_respond_emits_audit_event` / `test_first_wins_*` | N2 |
| 密钥 redact | `test_redact_*`（4 测试） | env / URL / sk- / Authorization / Cookie / 自定义 env 正则 |
| yolo 持久化重启恢复 | `test_yolo_persistence_best_effort` | `~/.orca/approval-yolo.json` |
| broker 重启清 stale | snapshot 是权威集（前端 store 实现） | |
| 超时可测 `ORCA_APPROVAL_TIMEOUT_TEST_OVERRIDE` | broker `_env_timeout` 实现 | 单测通过构造参数直注 |
| doctor approval_broker 心跳 | `_check_approval_broker` 实现 | hard=False |
| grep 守门（hook stdlib / broker 不 import tape） | `test_hook_script_uses_stdlib_only` / `test_approval_broker_not_import_forbidden_modules` | AST + 字符串 |
| install PermissionRequest + exec bit + env | `test_install_cc_lands_permission_hook` 等 9 测试 | |
| disconnect-abort | `test_disconnect_aborts_approval` | FakeRequest disconnected=True |

**未覆盖（留 test-agent 真机 e2e）**：
- 真实 CC 交互式 PermissionRequest 触发（§9 #2 spike）。
- WS 断连重连 + 真实 pending 跨进程保留行为（broker 是进程级，重启 = pending 弃）。

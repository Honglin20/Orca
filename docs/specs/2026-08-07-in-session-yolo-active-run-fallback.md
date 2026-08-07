# In-Session YOLO 兜底路由 Spec（active-run fallback）

> **状态**：Draft v2，2026-08-07。spec-review：**pass**（conditional-pass 前置条件全部满足：E1–E14 已闭环、U1 用户已确认 A），可进入实现。
> **触发**：真实用户反馈——从 CC 终端启动 in-session workflow，web 已开 yolo（WSL `~/.orca/approval-yolo.json = {"yolo": true}`，7428 snapshot 确认），但工具权限审批仍照常出现。
> **根因（已实证）**：`ApprovalBroker.request()` 先经 `resolve_session_context` 用 hook 传来的 `session_id` 反查 `SessionContextRegistry`；in-session 宿主 CC / 子代理的 session **从不注册**，故每次 PermissionRequest 都走 `native-fallback ask`，yolo 分支不可达。
> **范围**：仅 in-session 路径的 broker 兜底路由；不动 hook / CC settings.json 安装流程 / 前端。
> **用户拍板（行为语义）**：方案 **A**——命中活跃 run 后仍尊重 yolo（yolo on → 立即 allow；yolo off → 走 web 审批卡），不改为无条件 allow。

---

## 0. 目标与非目标

### 目标
1. in-session 场景下，PermissionRequest 的 session 未注册进 registry 时，broker 通过「活跃 run 兜底路由」找到该 session 所属 run。
2. 命中后走与注册命中完全相同的审批流程：yolo on → 立即 `allow`（`resolved_by="yolo"`）；yolo off → 创建 Approval、推 web 卡（run-scoped）、等待 resolve / timeout / disconnect。
3. 无活跃 run 命中 → 保持 `native-fallback ask`（不干扰日常 CC）。
4. 保持 broker 依赖单向铁律（上游 N11）：broker 不 import `orca.run` / `orca.tape*` / `orca.events.bus`。

### 非目标
- 不做 per-run yolo（沿用 broker 全局开关，上游 §3.3 B-6）。
- 不改 hook（`orca-permission-hook.py`）与 CC `settings.json` 安装流程（上游 B-11 不变）。
- 不改前端（yolo 开关 / ApprovalDialog 已就绪）。
- 不改 opencode 侧（本期 CC-only，与上游 SPEC 一致）。
- 不做「交互式 CC + Task 子代理下 PermissionRequest 触发机制」的实证——那是既有 §9 #2 spike（见 §7），本 spec 的兜底不依赖它：只要 hook 能到达 broker 即可。
- 不做 PermissionRequest stdin 字段名实证（N6 既有待办；hook 已容错多候选字段名）。

---

## 1. 背景与根因

现状链路（in-session）：

```
CC 子代理调危险工具 → CC 判定要问 → PermissionRequest hook
  → POST /approval {session_id?, tool, tool_input, hook_event}
  → ApprovalBroker.request()
      → resolve_session_context(registry, payload)   # registry.lookup(session_id)
          ├─ 命中 → 正常审批 / yolo
          └─ miss（in-session 恒 miss）→ {behavior:"ask", resolved_by:"native-fallback"}
                → hook emit ask → CC 原生弹窗（用户仍需审批，yolo 不可达）
```

**实证 1（注册缺口 + 注册 id 不同源）**：全仓库 `SessionContextRegistry.register` 仅 `orca/exec/mcp_tools/server.py:184-193` 一处入口，由 `orca/exec/claude/executor.py:163` 经 `register_session` 间接调用；且该注册 id 是 **executor 入口生成的 uuid**（executor.py:160-161 注释：「session_id 即本 executor 入口生成的 uuid（全程复用）」），**不是** CC 会话 id。故对 hook 审批而言，registry 反查在真实路径下从不命中（e2e 通过只因测试驱动手动注册 `e2e-sess-001`，见实证 3）。in-session 宿主 CC / 子代理 session 则完全无注册路径（`in_session/cli.py`、`in_session/daemon.py`、`iface/web/run_manager.py`、attach 路由均无 register 调用）。

**实证 2（session 双键）**：真实 in-session tape `runs/agent-struct-exploration-20260719-072727-42ae4f.jsonl` 首行（已用 JSON 解析复核）：
- 顶层键：`seq, type, timestamp, node, session_id, data`
- `data` 内含 `host_session` = `ses_08873f68bffej216SxU1tAIoZW`（主 CC 会话）
- 节点事件顶层 `session_id` = `ses_0887167a9ffeyDIBkX5VP877Er`（子代理）

两者不同 ⇒ 兜底路由不能只比 host_session，必须同时比对 tape 内任一节点 `session_id`（**`data.host_session` 字段路径经实证正确，保持不动**；`orca/run/lifecycle.py:55 make_workflow_started` 契约一致）。

**实证 3（e2e 为何通过）**：`/tmp/ws_probe.log` 显示 `approval_resolved(resolved_by:"yolo")` 链路可用；但 `/tmp/orca-perm-driver.log` 开头 `DRIVER_READY session_id=e2e-sess-001` 表明测试驱动**先手动 `registry.register()`** 才触发 hook。真实 in-session 流程无此步骤。

---

## 2. 设计决策

### 2.1 兜底放 broker，扫描逻辑以 DI 注入（保持 N11）

- `ApprovalBroker` 构造新增可选参数 `active_run_resolver`：`Callable[[str], str | None]`（入参 hook 的 `session_id`，出参命中的 `run_id` 或 `None`）。
- `request()` 在 `resolve_session_context` 返回 unknown 且 resolver 存在时调用 resolver；命中 → 继续既有「创建 Approval → publish → yolo/等待」路径；未命中 → 保持 `native-fallback ask`。
- resolver 缺省 `None` ⇒ 行为与现状完全一致（向后兼容：旧构造路径 / 既有测试零改）。
- 扫描逻辑放 server 侧新模块（`orca/iface/web/active_runs.py`），broker 不 import run/tape/events；`create_app` 装配时注入。
- **resolver 异常语义**：异常（含 `RegistryCorruptError` / `OSError` / JSON 坏行）→ resolver 内 catch → `logger.warning` → 视为未命中 → `native-fallback ask`。**禁止异常传播到 `create_app` 期**（`build_active_run_resolver` 返回的闭包在**调用期**枚举 runs_dirs，工厂期零 IO）。
- **调用时机**：仅当 `payload["session_id"]` 为非空 str 时调用 resolver（缺失/空 → 直接 ask，不调）。

### 2.2 匹配键：host_session 或任一节点 session_id

- 先读 tape 首行（`workflow_started`）的 `data.host_session == session_id` → 命中（bootstrap 即写入，无竞态窗口；host 键为主路径）。
- `data.host_session` 为 null / 缺键 → 该 run 不参与 host 键匹配，仍走 node 扫描。
- 未命中 → 扫描 tape 全部事件顶层 `session_id == session_id` → 命中（覆盖子代理 id 路径；实证两者不同）。
- 两者都不命中 → 该 run 不匹配。
- **多 run 命中**（同 session 并行多活跃 run，罕见）：取 marker mtime 最新者，`logger.warning` 记录（fail loud，不静默 ids[0]）；mtime 平局按 run_id 字典序取最小（确定性）。「最新者不匹配则回退较旧者」标为**可选增强（SHOULD）**——需全量扫 tape，与性能边界冲突，首版不做。

### 2.3 活跃 run 判定与 runs dir 枚举

- **活跃 = marker 存在 且 tape 存在 且 tape 末行非终态事件**：
  - marker：`<rundir>/orca-<run_id>.json`（`iface/in_session/marker.py` 契约：bootstrap 写、`next` 升格 done / `stop` / bootstrap 失败时 `clear_marker`，实证 `cli.py:1352/1535/1768`）。
  - **终态第二守卫**：tape 末行 type ∈ {`workflow_completed`, `workflow_failed`, `workflow_cancelled`} → 视为不活跃（防 kill -9 / 断电残留的 stale marker 把已死 run 当活跃，yolo-allow 扩面）。
  - **orphan marker**（marker 存在但 tape 缺失）→ 不活跃。
- **runs dir 枚举**：复用 `orca.runtime` public API（`resolve_runs_dir` / `list_registered`，两级解析 `ORCA_PROJECT_ROOT` env > CWD + 注册表全项目）；每个 runs dir 扫 `orca-*.json` marker，读对应 `<run_id>.jsonl`。
- **缓存**：per-run 缓存键 = `(tape path, mtime, size, marker 存在性)` → `{host_session, node_session_ids}`；键变化即失效。**marker 增删强制计入缓存键**（防终态 run 被缓存误路由）。

### 2.4 行为语义（用户已拍板 A）

| 场景 | 行为 |
|---|---|
| resolver 命中 + yolo on | 立即 `allow`，`resolved_by="yolo"`；web 仍可见 `approval_requested` + `approval_resolved(yolo)` |
| resolver 命中 + yolo off | 创建 Approval（run-scoped）→ publish `approval_requested` → 等 web resolve / `BROKER_TIMEOUT` 策略（默认 allow）/ disconnect-abort |
| resolver 未命中 / 无 resolver / 异常 | `{behavior:"ask", resolved_by:"native-fallback"}`（现状不变，日常 CC 不干扰） |

> **安全取舍声明（U1，待用户确认，推荐 A）**：resolver 命中后，in-session 路径的 CC 原生权限询问被 web 审批通道替换。yolo off 且浏览器未订阅该 run 时，用户看不到任何弹窗，超时（默认 600s）按 `ORCA_APPROVAL_TIMEOUT_POLICY`（默认 allow）放行。
> - **A（推荐）**：维持方案 A——命中即 web 通道，无订阅者时超时按策略（默认 allow）。与上游「timeout → 策略（默认 allow，notify-proceed）」「broker 不可达 → ask」不对称失败模型一致，实现最简单。
> - **B（保守）**：命中但该 run 无 WS 订阅者时回退 `native-fallback ask`，保留 CC 原生询问兜底。引入 broker ↔ WS 订阅状态耦合，与上游契约冲突。
> **确认记录**：用户确认 **A**（2026-08-07）：命中即 web 通道，无订阅者时超时按策略（默认 allow）。

---

## 3. 接口契约

### 3.1 `ApprovalBroker`（orca/iface/web/approval_broker.py）

```python
def __init__(
    self,
    registry,
    *,
    timeout: float | None = None,
    active_run_resolver: Callable[[str], str | None] | None = None,
) -> None: ...
```

- `active_run_resolver`：同步 callable；broker 在 `request()` 中直接调用（扫描有界：仅活跃 run 的 marker 存在性 + 非终态判定 + mtime/size 缓存后的 tape 索引；审批请求低频）。若实现改为 `asyncio.to_thread` 包装，缓存须线程安全——首版不做强制。
- `request()` 变更点：`resolve_session_context` 返回 unknown 且 `session_id` 非空且 resolver 非 None → 调 resolver；命中 → 继续既有 Approval/yolo 路径；未命中 → 原 `native-fallback ask` 分支。
- 返回结构不变：`{behavior, approval_id, resolved_by}`。

### 3.2 新模块 `orca/iface/web/active_runs.py`

```python
def build_active_run_resolver(
    runs_dirs: Iterable[Path] | None = None,
) -> Callable[[str], str | None]:
    """工厂：默认调用期枚举 resolve_runs_dir() + list_registered() 的 runs dir；工厂期零 IO。"""

def resolve_session_to_active_run(
    session_id: str,
    runs_dirs: Iterable[Path],
) -> str | None:
    """核心纯函数（可测）：扫 marker → 终态第二守卫 → 读 tape → 双键匹配 → 最新者。"""
```

- 依赖方向：`iface/web → orca.runtime`（public re-export：`from orca.runtime import resolve_runs_dir, list_registered`）+ `iface/in_session/marker`（`read_marker` / `marker_path`）+ stdlib `json`。零 run/events 依赖。
- tape 读取：raw JSONL 逐行 `json.loads`（fail-soft：坏行 / data 非 dict / 首行截断 → 跳过 + warn，不崩）。

### 3.3 server 装配（orca/iface/web/server.py）

`create_app` 构造 `ApprovalBroker` 时注入：

```python
approval_broker = ApprovalBroker(
    manager.registry,
    active_run_resolver=build_active_run_resolver(),
)
```

`tars serve` 即 web server（7428，含 broker + WS + 前端），无需新进程。

---

## 4. 测试清单

### broker 单测（扩展 tests/iface/web/test_approval_broker.py）
- T1：resolver 命中 + yolo on → `behavior="allow"`、`resolved_by="yolo"`、WS 可见 requested+resolved；**resolver spy 断言**：registry miss 时被调用、入参 = hook session_id、先于 Approval 创建 / yolo 检查。
- T2：resolver 命中 + yolo off → 创建 Approval、publish `approval_requested`（run-scoped）；web resolve allow → `allow`；**resolver spy 断言**同上。
- T3：resolver 未命中 → `ask` / `native-fallback`（现状保持）。
- T4：resolver 为 None（默认）→ 行为与现状一致（既有用例零改即回归）。
- T5：resolver 抛异常 → `ask` + warning（不自动 allow）。
- T6：`session_id` 缺失/空 → **resolver spy 断言不被调用**，直接 ask。
- T15：工厂默认枚举——`build_active_run_resolver()` 调用期解析 runs_dirs（含注册表损坏 → catch → None 不炸）。
- T16：resolver 命中 + 无响应 → `BROKER_TIMEOUT` 到 → 按 policy（默认 allow，`resolved_by="timeout"`）。
- T17：resolver 命中 + HTTP disconnect → `aborted`（`resolved_by="disconnect"`）。

### active_runs 单测（新 tests/iface/web/test_active_runs.py）
- T7：host_session 匹配命中。
- T8：节点 `session_id` 匹配命中（宿主≠子代理双键）。
- T9：无 marker（终态）→ 不命中；**stale marker + tape 末行终态事件 → 不命中**；**orphan marker（tape 缺失）→ 不命中**。
- T10：marker 存在但 tape 无双键匹配 → `None`。
- T11：tape 半写 / 坏行 / 首行截断 / data 非 dict / `host_session=null` → fail-soft（跳过该 run，不抛；host=null 仍走 node 扫描）。
- T12：多 run 命中 → 取 mtime 最新 + warn；**mtime 平局 → run_id 字典序最小（确定性）**。
- T13：多 runs dir 枚举（registered projects）。
- T14：缓存键含 marker 状态——marker 增删后缓存失效重新扫描。
- T15b：注册表损坏 fixture → resolver catch → `None`（不炸，不传播到 create_app）。

### 回归
- 既有 `test_approval_*` 全绿（默认 resolver None）。
- `tars validate` 0 error / 0 warning。

---

## 5. 验收标准（AC）

- AC1：in-session 场景（session 未注册）PermissionRequest → broker **resolver 被调用（spy 断言）且先于 Approval 创建**，命中活跃 run → yolo on 返回 `allow`（`resolved_by="yolo"`）。
- AC2：同场景 yolo off → web 出现 run-scoped 审批卡；用户 allow → CC 放行。
- AC3：无活跃 run / 日常 CC → `ask`（不干扰）。
- AC4：N11 守门范围 = `approval_broker.py` + `active_runs.py` 两文件直连 import；禁止清单 = 上游 N11 并集（`orca.gates.handler` / `orca.tape*` / `orca.exec.*` / `orca.events.bus`）+ `orca.run`。守门命令：结构化 import 检查，非裸 grep。
- AC5：验证命令 pin：`pytest tests/iface/web/test_approval_broker.py tests/iface/web/test_active_runs.py` 全绿 + 既有 approval 测试零改全绿 + `tars validate` 0/0。
- AC6：`tars serve` 重启后 yolo 状态恢复（持久化不变）——**上游回归哨兵**，非本阶段新行为。

---

## 6. 风险与开放问题

- **R1（已知竞态，可接受）**：子代理首个工具调用即危险工具时，tape 可能尚未写入该子代理 `session_id`。缓解：host 键（主 CC 会话 id）是主路径、bootstrap 即写入无竞态；node 键竞态窗口仅「hook 发送的是子代理 id 且 host 键也未命中」时出现，此时 miss 回退 ask（CC 原生可答），不阻塞。
- **R2（静态可确认部分）**：hook 取 session_id 的 env 优先级静态确认 = `ORCA_HOST_SESSION_ID` > `CLAUDE_CODE_SESSION_ID` > stdin（`orca-permission-hook.py:_resolve_session_id`）；子代理环境注入值待真机实证（§9 #2 spike 同源）。双键匹配覆盖两种来源。
- **R3（扫描面）**：每请求仅扫「marker 存在 + 非终态」的 run（通常 1-2 个），tape 索引走 mtime/size/marker 缓存；多项目 registered runs dir 全扫由缓存兜底。
- **R4（多 run 并行）**：同 session 多活跃 run 取最新 + warn；不静默。
- **R5（既有 warning 噪音）**：`resolve_session_context` 对未注册 session 的既有 warning 在兜底命中时仍会每次触发——**预期行为**，不改共享函数；broker 命中后补 `info`（含 run_id）、双 miss 补 `warning` 语义日志。

---

## 7. 关联待办（不阻塞本 spec）

- 既有 §9 #2 spike：交互式 CC + Task 子代理下 PermissionRequest 是否自然触发 + stdin 字段名（N6/Q3）。本 spec 的 fallback 就绪后，该 spike 的真机验证可直接作为 AC1/AC2 的真机验收。
- 真机验证（AC1/AC2 落地）：CC 终端跑一次 in-session workflow，观察危险工具调用在 yolo on 时自动放行、yolo off 时出 web 卡。

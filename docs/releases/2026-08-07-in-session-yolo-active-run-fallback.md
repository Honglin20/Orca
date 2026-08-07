# Release: in-session YOLO 兜底路由（active-run fallback）

**日期**: 2026-08-07
**类型**: feat（in-session 权限审批：session 未注册时经活跃 run 兜底命中，yolo 真正生效）
**分支**: in-session-unified-backend
**Commit**: `984d55b`

## 背景 / 根因

真实用户反馈：从 CC 终端启动 in-session workflow、web 已开 yolo（`~/.orca/approval-yolo.json =
{"yolo": true}`，428 snapshot 确认），但工具权限审批仍照常弹出。

已实证根因（SPEC §1）：`ApprovalBroker.request()` 先经 `resolve_session_context` 用 hook 传来的
`session_id` 反查 `SessionContextRegistry`。in-session 宿主 CC / 子代理的 session **从不注册**——
全仓库 `register` 唯一入口在 `exec/mcp_tools/server.py:184-193`，注册 id 是 executor 入口生成的
uuid（`executor.py:160-161` 注释「session_id 即本 executor 入口生成的 uuid」），**不是** CC 会话
id。故每次 PermissionRequest 都走 `native-fallback ask`，yolo 分支不可达。

## 选定方案（用户拍板 A：命中即尊重 yolo，yolo off 走 web 审批卡）

仅改 broker 兜底路由，不动 hook / CC settings.json 安装流程 / 前端 / opencode 侧（本期 CC-only，
与上游 SPEC 一致）。

### 1. 新增 `orca/iface/web/active_runs.py`（核心扫描器）

- `resolve_session_to_active_run(session_id, runs_dirs) -> str | None`：扫 runs 目录
  `orca-*.json` marker → 终态第二守卫 → 读对应 `<run_id>.jsonl` → 双键匹配。
  - **活跃 = marker 存在 且 tape 存在 且 tape 末行非终态事件**
    （`workflow_completed` / `workflow_failed` / `workflow_cancelled`，防 kill -9 / 断电残留
    stale marker 把已死 run 当活跃、yolo-allow 扩面）。
  - **双键匹配**：首行 `data.host_session`（host 键，bootstrap 即写入、无竞态）或全量事件顶层
    `session_id`（node 键，覆盖子代理 id；实证宿主 `ses_0887...AIoZW` ≠ 子代理
    `ses_0887...Er`）。
  - **多 run 命中**：取 marker mtime 最新者 + warning（fail loud，不静默 ids[0]）；mtime 平局按
    run_id 字典序取最小（确定性）。
  - **per-run 缓存**：键 = `(tape path, mtime_ns, size, marker 存在性)` → `{host_session,
    node_session_ids}`；键变化即失效；容量 512 有界（approval 低频）。
  - **fail-soft（per-item）**：坏行 / data 非 dict / 首行截断 / 非法 UTF-8 / marker 半写损坏 →
    跳过 + warn，不崩、不中断整轮扫描；`host_session=null` 仍走 node 扫描。
- `build_active_run_resolver(runs_dirs=None)`：工厂**零 IO**（不传播注册表损坏到 `create_app`）；
  每次调用期枚举 `resolve_runs_dir()` + `list_registered()` 的 runs 目录（多项目全覆盖）。
- 依赖单向：仅 `orca.runtime` public re-export + `orca.iface.in_session.marker` + stdlib，
  零 run/tape*/exec/events.bus/gates.handler（结构化 AST import 守门测试钉住）。

### 2. `ApprovalBroker` 注入（`orca/iface/web/approval_broker.py`）

- `__init__` 新增 keyword-only `active_run_resolver: Callable[[str], str | None] | None = None`
  （DI，broker 零 run/tape 依赖，N11 守门不变；None → 行为与现状完全一致，向后兼容）。
- `request()`：`resolve_session_context` 返回 unknown 且 `session_id` 非空 str 且 resolver 注入时
  调用 resolver；命中 → 与注册命中**完全相同**的 Approval/yolo 流程（yolo on → 即时 allow /
  yolo off → run-scoped web 审批卡 / timeout / disconnect 全复用）；未命中 / 异常 / 无 resolver →
  原样 `native-fallback ask`（不干扰日常 CC）。
- 语义日志：命中 → `info`（含 run_id）；双 miss → `warning`（SPEC R5：与既有
  `resolve_session_context` miss warning 叠加属**预期行为**，不修改共享函数）。

### 3. `create_app` 装配（`orca/iface/web/server.py`）

`ApprovalBroker(manager.registry, active_run_resolver=build_active_run_resolver())`；`tars serve`
即 web server（7428，含 broker + WS + 前端），无需新进程。

## 验证结果

- 新增 `tests/iface/web/test_active_runs.py`（T7–T15b + AC4 结构化 import 守门）：
  双键匹配 / 终态三例 / 无键 / fail-soft 四例（坏行、首行截断、data 非 dict、host=null）/
  非法 UTF-8 per-item / 多 run mtime + 平局字典序 / registered 多项目枚举 / 缓存失效（marker
  增删 + tape 追加）/ 注册表损坏 / >64KB 超长单行。
- 扩展 `tests/iface/web/test_approval_broker.py`（T1–T6 / T16 / T17）：resolver spy 断言
  （registry miss 才调、入参 = hook session_id、**先于 Approval 创建**）；命中 + yolo on →
  `allow/yolo` + WS 可见 requested+resolved；命中 + yolo off → run-scoped Approval + web
  resolve（allow/deny 双分支）；miss / None / 异常 / session 缺失空 → ask；timeout-policy /
  disconnect-abort 复用路径。
- 定点：`pytest tests/iface/web/test_approval_broker.py tests/iface/web/test_active_runs.py
  tests/iface/web/test_approval_routes.py` = **63 passed**。
- 回归：`tests/iface/web` 全量（除 Playwright 环境用例）= **272 passed, 2 perf skip**。
- code-reviewer 两轮闭环（代码 + 测试覆盖）：thread A 0 MUST-FIX；所有 SHOULD-FIX/MINOR
  （缓存键 marker 维度显式化、UnicodeDecodeError per-item fail-soft、静默 continue 补 warn、
  broker 守门升级 AST + `orca.run` 探针、测试改走公开构造器注入、deny 分支、超长行）已全部采纳。

## 偏离 SPEC 记录

- 无功能偏离。实现细节说明：缓存键第 4 元素「marker 存在性」由调用方显式传入（当前调用点恒
  `True`，因仅对 marker 存在且非终态的 run 建索引），与 SPEC §2.3 逐字对齐，防未来复用无 marker
  路径时 stale 命中。

## 待办（真机验证，属 SPEC §7 遗留）

- 真机 E2E（AC1/AC2 落地）：CC 终端跑一次 in-session workflow，观察危险工具调用在 yolo on 时
  自动放行、yolo off 时出 web 审批卡。本 fallback 就绪后，既有 §9 #2 spike 的真机验证可直接
  作为 AC1/AC2 落地证据。

## 工作树其他线程说明（非本次改动）

同工作树存在并行可视化重构（live_loss_watcher → progress_watcher）的未提交改动，其
`workflows/nas-supernet.yaml` description 内嵌未转义引号导致 `tars validate` 解析失败、
`_common.py:251` 引用未定义的 `_read_objective`、`tests/workflows/test_live_loss_watcher.py`
仍引用已删除脚本导致收集中断——均属该线程在途问题，不在本次 commit（`984d55b` 仅含本任务
7 文件）。`tars validate` 0/0 回归待该线程修复后整体复跑。

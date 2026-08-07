# 计划：in-session yolo 兜底路由（active-run fallback）

> 实现前先写计划（SDD：读 SPEC → 写计划 → 确认 → 实现）。
> 依据 SPEC：[`docs/specs/2026-08-07-in-session-yolo-active-run-fallback.md`](../specs/2026-08-07-in-session-yolo-active-run-fallback.md)（v2，conditional-pass；E1–E14 已闭环，U1 待用户确认）

## 目标

让 in-session 场景下 yolo 真正生效：broker 在 session 未注册时通过「活跃 run 兜底路由」命中 run，命中后尊重 yolo（on → allow；off → web 卡）；未命中保持 native-fallback ask。用户已拍板方案 A；U1（yolo-off + 无 WS 订阅者边界）待确认，默认按 A。

## 文件清单

### 1. 新增 `orca/iface/web/active_runs.py`
- `resolve_session_to_active_run(session_id, runs_dirs) -> str | None`：核心纯函数。
  - 每个 runs dir：扫 `orca-*.json` marker（`read_marker`）；**活跃 = marker 存在 且 tape 存在 且 tape 末行非终态事件**（`workflow_completed`/`workflow_failed`/`workflow_cancelled`）；orphan marker（tape 缺失）→ 不活跃。
  - 读 `<run_id>.jsonl` 首行 `data.host_session`（null/缺键 → 跳过 host 键，仍走 node 扫描）；未命中再扫全量顶层 `session_id`。
  - raw JSONL 逐行 `json.loads`，坏行 / data 非 dict / 首行截断 → fail-soft（skip + warn）。
  - 多命中取 marker mtime 最新 + warn；mtime 平局按 run_id 字典序取最小（确定性）。
  - per-run 缓存键 = `(tape path, mtime, size, marker 存在性)` → `{host_session, node_sessions}`；marker 增删强制失效。
- `build_active_run_resolver(runs_dirs=None) -> Callable[[str], str | None]`：返回闭包，**调用期**枚举 `resolve_runs_dir()` + `list_registered()` 的 runs dir；工厂期零 IO；resolver 内 catch `RegistryCorruptError`/OSError → warning → `None`（ask），禁止传播到 create_app。
- 依赖：`from orca.runtime import resolve_runs_dir, list_registered`（public re-export，非私有 `_project`）、`orca.iface.in_session.marker`（read_marker/marker_path）、stdlib json/pathlib/logging。零 run/events 依赖。

### 2. 修改 `orca/iface/web/approval_broker.py`
- `__init__` 新增 keyword-only `active_run_resolver: Callable[[str], str | None] | None = None`。
- `request()`：`resolve_session_context` 返回 unknown 且 `session_id` 非空且 resolver 非 None → 调 resolver；命中 → 继续既有 Approval/yolo 路径；未命中/异常 → 原 `native-fallback ask`。
- 语义日志：命中 → `info`（含 run_id）；双 miss → `warning`（既有 `resolve_session_context` miss warning 仍会触发，属预期，不改共享函数）。
- docstring 更新（兜底路由 + 异常语义 + 调用时机）。

### 3. 修改 `orca/iface/web/server.py`
- `create_app`：`ApprovalBroker(manager.registry, active_run_resolver=build_active_run_resolver())`。

### 4. 测试
- 扩展 `tests/iface/web/test_approval_broker.py`：T1-T6、T15-T17（见 SPEC §4；T1/T2/T6 含 resolver spy 断言）。
- 新增 `tests/iface/web/test_active_runs.py`：T7-T15b（见 SPEC §4）。
- 回归：既有 approval 测试零改全绿；`tars validate` 0/0。

## 测试清单（意图 → 断言）
- 意图「yolo 在 in-session 生效」→ AC1：resolver 被调用（spy）+ 命中 + yolo on → allow/yolo。
- 意图「yolo off 时审批回到 web」→ AC2：resolver 命中 + yolo off → approval_requested + web resolve。
- 意图「不干扰日常 CC」→ AC3：无命中 → ask。
- 意图「依赖铁律不破」→ AC4：守门对象 = approval_broker.py + active_runs.py，禁止清单 = 上游 N11 并集 + orca.run。
- 意图「验收可复现」→ AC5：pin 测试文件 + 命令 + tars validate 0/0。
- 意图「持久化回归」→ AC6：重启恢复 yolo（上游回归哨兵，非本阶段新行为）。

## 实现注记
- 调用期枚举 + resolver 内异常 catch → ask；同步调用（审批低频 + 缓存），docstring 声明扫描边界；若选 `asyncio.to_thread`，缓存须线程安全（首版不做）。
- 不修改 `data.host_session` 字段路径（实证正确；勿按 evaluator 误判改动）。

## 验收
见 SPEC §5 AC1-AC6 全勾选（U1 确认后实施）。

## 风险/开放问题
- R1 竞态（子代理首工具即危险工具，node 键未写入）：host 键主路径无竞态；miss 回退 ask，不阻塞。
- R2 hook session_id 来源（env 优先级静态确认；子代理注入值待真机实证）：双键覆盖。
- R3 扫描面：仅扫 marker 存在 + 非终态 run；mtime/size/marker 缓存兜底。
- R4 多 run 并行：取最新 + warn；mtime 平局按 run_id 字典序。
- R5 既有 miss warning 每次触发属预期，不改共享函数。

## 偏离 SPEC 记录
（实现中如需偏离，在此记录 + 理由，release note 同步。）

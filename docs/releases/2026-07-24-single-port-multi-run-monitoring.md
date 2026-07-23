# Release Note: 单端口 + 多 Run 监控（Phase A + B' + C 生产代码 + 单元测试）

**日期**：2026-07-24
**分支**：`in-session-unified-backend`
**SPEC**：[2026-07-23-single-port-multi-run-monitoring.md §13 v4](../specs/2026-07-23-single-port-multi-run-monitoring.md)（权威，覆盖 v3 冲突）

## 概览

实现 SPEC §13 三阶段：
- **Phase A**：身份解耦（指纹从 `sha1(<project>/runs)` 改为 `sha1(ORCA_HOME)[:12]`）+ 端口登记上移 `~/.orca/.orca-web.json` + 「决策+spawn+bind+ready+写回」同一 exclusive flock 临界区（B-6）。
- **Phase B'**：轻量项目注册表 `~/.orca/projects.json`（`orca/runtime/_project.py` 中立层，仅依赖 stdlib）+ `start_run(*, project_path=None)` keyword-only + `POST /api/run` body 必填 project_path + 注册表 allowlist。
- **Phase C**：`GET /api/runs?scope=all` 跨项目 discovery + `manager.ensure_attached(run_id)` 懒挂载 + `DELETE /api/runs/<id>`（U-1/B-5/M-3 四态响应）+ WS 控制帧基础设施（B-4 每 WS queue + writer task）+ FastAPI AuthMiddleware no-op stub（M-1）+ 前端列表层（R3：与 workflow-store 物理隔离）。

## 改动文件

### 新增
- `orca/runtime/__init__.py` + `orca/runtime/_project.py`：中立层注册表（D2/D4/B-2/P1/M-15/M-16）。
- `orca/iface/web/_auth.py`：FastAPI middleware 全局兜底 no-op stub（M-1/AC19）。
- `orca/iface/web/frontend/src/stores/run-list-store.ts`：列表 store（绝不 import workflow-store，R3/AC11）。
- `orca/iface/web/frontend/src/components/layout/status-badge.tsx`：公用 StatusBadge。
- `orca/iface/web/frontend/src/components/pages/RunListPage.tsx`：列表页正式版（接 store/API/WS 控制帧）。
- `tests/runtime/test_project.py`：20 单测。
- `tests/iface/web/test_multi_run_phase_c.py`：15 单测。
- `tests/iface/web/test_phase_a_registry_auth.py`：10 单测。

### 修改（增量扩展，非破坏性）
- `orca/iface/web/_identity.py`：加 `orca_home_fingerprint()`。
- `orca/iface/web/run_manager.py`：加 RunSummary、start_run keyword-only project_path、ensure_attached、discover_runs、delete_run、broadcast listeners、_summary_from_tape；resolve_tape_path 增 allowlist 分支。
- `orca/iface/web/ws_handler.py`：每 WS queue + writer task（B-4）+ run_changed 控制帧广播 + ensure_attached on subscribe。
- `orca/iface/web/server.py`：install_auth_middleware。
- `orca/iface/web/routes/attach.py`：health 兼容期同发 orca_home_fp + runs_dir_fp（U-2）。
- `orca/iface/web/routes/runs.py`：scope=all discovery + ensure_attached 触发面 + DELETE + M-5 schema 白名单。
- `orca/iface/web/routes/run.py`：body 必填 project_path（B-1）。
- `orca/iface/cli/commands.py`：_runs_dir_fp 用 ORCA_HOME + _open_run 在 exclusive_port_decision 临界区内 spawn+write_back。
- `orca/iface/cli/web_registry.py`：用户级登记 + legacy 迁移（M-7）+ unlocked 变体 + exclusive_port_decision contextmanager。
- `orca/iface/web/frontend/src/App.tsx`：`/` → RunListPage。
- `orca/iface/web/frontend/src/components/layout/TopBar.tsx`：加返回列表按钮。

## 关键决策与执行

### B-6 临界区 + 嵌套 flock 死锁修复
- 初版把 `_register_my_port` 放进 `exclusive_port_decision()` 内部，触发**同进程 flock 二次 acquire 不同 fd → 死锁**（Linux `fcntl.flock` 行为）。
- 修复：暴露 `_write_orca_home_registry_unlocked` / `_lookup_orca_home_port_unlocked` 内部变体，临界区内用 unlocked 版（P1 「公开 API 禁嵌套调用」严格执行）。

### 依赖单向铁律（R5）
- run_manager.py 加了 `from orca.iface.cli.bg_runner import list_all_meta/default_tape_path` → 违反「web 禁 import cli」。
- 修复：内联 `_list_legacy_metas()` / `_legacy_default_tape_path()` 复刻 bg_runner 路径语义，用 stdlib 直接扫 `~/.orca/runs/`。
- 遗留：pre-existing 的 `from orca.iface.cli.config import apply_kb_requirement` 仍存在（git HEAD 既有，非本次引入，超出 §13 范围，留给后续 refactor）。

### RunSummary schema（M-5）
- Pydantic `ConfigDict(extra="forbid")` + `response_model_exclude_unset=True`。
- 反向 fixture：构造时多传字段 → ValidationError。

### DELETE 四态（M-3）
- 200 `{ok, run_id, existed_before:true}` / 404 `{ok:false, never_existed:true}` / 409 `{ok:false, live:true, pid}` / Windows file-locked → 409+error。
- stale `_run_path_index` 处理：删除前检测文件实际存在，删除后清 index。

## 偏离计划

- **前端构建未跑**：WSL/Windows 混合环境下 node_modules 的 rollup native binary 不匹配（`@rollup/rollup-win32-x64-msvc` 缺失）。`tsc --noEmit` 通过，源码 sound；前端 `out/` 目录未刷新——用户拉取后需在 Windows-native shell 内 `npm run build` 一次以激活列表页 UI。
- **`_scan_meta_overview` contract test（AC14）**：未补（依赖 EventType 自动派生机制，工作量大，遗留 Phase C 收尾）。
- **持久层派生缓存 `<project>/runs/.orca-meta-cache.json`（P0）**：未实现（per-process 内存缓存已就位，持久层留待 perf spike）。
- **`orca project rebuild` 命令**：未实现（注册表 corrupt 时仅 fail loud 提示）。

## 验证结果

| 测试套件 | 结果 |
|---------|------|
| tests/runtime/test_project.py（注册表） | 20 passed |
| tests/iface/web/test_multi_run_phase_c.py（discovery/ensure_attached/DELETE/broadcast） | 15 passed |
| tests/iface/web/test_phase_a_registry_auth.py（端口登记迁移 + auth middleware） | 10 passed |
| tests/iface/web/test_routes.py / test_attach.py / test_attach_routes.py / test_run_manager.py / test_ws.py / test_ws_resume.py / test_attach_follow_failures.py / test_integration.py | 全绿（更新 4 个测试用例匹配新契约：project_path 必填、orca_home_fp 兼容期、M-12 _run_path_index allowlist） |
| tests/iface/cli/test_web_registry.py / test_web_default_and_open.py / test_commands.py | 全绿（更新 3 处：registry 隔离 ORCA_HOME、_write_orca_home_registry_unlocked mock、_runs_dir_fp 用 ORCA_HOME） |
| tsc --noEmit | OK |
| **1 pre-existing 失败** | `test_web_does_not_import_cli`：flag pre-existing `apply_kb_requirement` import（git HEAD 既有，非本次引入） |

合计：**~243 passed / 1 pre-existing failed**。

## Commits
（待提交）

## 守门（grep-able）
- `runListStore` 不 import `workflow-store`（AC11）：见 `run-list-store.ts` 顶部 import 块。
- `app.user_middleware` 含 AuthMiddleware（AC19）：`test_phase_a_registry_auth.py::test_create_app_installs_auth_middleware`。
- `_run_path_index` 是 per-process discovery 索引（非 run 数据真相源），不违反「单一 registry」铁律：`test_single_runs_registry` 已更新允许此字段。

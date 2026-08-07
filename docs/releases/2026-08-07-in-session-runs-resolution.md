# Release: in-session CLI runs 目录解析鲁棒化（env 自描述 + 两级解析器）

**日期**: 2026-08-07
**类型**: fix + feat（in-session CLI 子代理 CWD 迷路 bug 修复 + status fail-loud hint）
**分支**: in-session-unified-backend

## 背景 / Bug 根因

in-session CLI 的 runs 目录解析是 CWD 相对的：`_default_tape_path(run_id)` →
`bg_runner.default_tape_path` → `Path("runs")/<id>.jsonl`。所有命令（bootstrap/next/status/stop/
open/doctor）的 rundir 都从它推导（`cli.py:_default_rundir`）。

**问题**：当子代理 CWD 落在子目录（如 `artifacts/nas-supernet/runs/train/`）时，`orca status`
扫 `cwd/runs/` 找不到活跃 run 的 marker——marker 实际在项目根的 `runs/`。根因是 `_write_orca_env`
只写 7 个变量（run_id/node/session_id/sock/resources/artifacts/kb），**没有项目根锚点**，所以
即使子代理 source 了 env 也救不回来——tape/rundir 解析仍走 CWD 相对。

## 选定方案

**env 自描述 + 两级解析器**（刻意不回溯祖先——避免重开上一轮 visibility bug）。

### 1. `resolve_runs_dir()`（`orca/runtime/_project.py`）

新增中立层函数（紧挨 `detect_project_root`），从 `orca/runtime/__init__.py` 再导出（与
`RUNS_DIRNAME`/`register_project` 同款）。严格两级：

1. `ORCA_PROJECT_ROOT` env → `<root>/RUNS_DIRNAME`
2. `Path(RUNS_DIRNAME)`（CWD 相对——bootstrap-at-root 现状，零回归）

**刻意不调 `detect_project_root()`**：后者会向上找 `workflows/`/`.git` 跳到 cwd 祖先，与 tape
落点 `cwd/runs` 脱节——这是 `cli.py:573`（`_register_current_project` docstring）文档化的上一轮
visibility bug 根因，不重开。runs 恒为 `<project_root>/runs`（`RUNS_DIRNAME` 不变式）。

env 值坏（`_resolve_strict` 失败）→ raise `ValueError`（fail loud，铁律 4，不静默退化回 CWD 相对）。

### 2. `_default_tape_path` 改走 `resolve_runs_dir`（`cli.py:558`）

旧实现 lazy import `bg_runner.default_tape_path`。新实现直接走 `resolve_runs_dir() / f"{run_id}.jsonl"`。
`_default_rundir()` 无需改——派生自 `_default_tape_path`，自动跟随。

**`bg_runner.default_tape_path` 保持不动**——它服务 `tars run --background` daemon 路径（子进程继承
正确 CWD），且有锁定测试 `tests/iface/cli/test_bg_runner.py:151`（`default_tape_path("r1") == Path("runs")/"r1.jsonl"`）
继续绿。

### 3. `_write_orca_env` 加 `ORCA_PROJECT_ROOT`（`cli.py:457`）

作为 per-run 常量（与 `ORCA_ARTIFACTS_DIR` 同类，不随节点变化）：
`project_root = resolve_runs_dir().resolve().parent`，写在 `ORCA_RUN_ID` 之后。与
`_register_current_project`（`cli.py:593`）的计算方式一致 → 幂等（next 重写 env 时主 session 仍在
项目根或已设 `ORCA_PROJECT_ROOT`，重算结果一致）。

### 4. `status` 空 markers 时 fail-loud hint（`cli.py:1881`）

新增 `_build_empty_runs_hint()` helper：当 `runs_dir.glob("orca-*.json")` 为空时构建文字提示。
registry 非空 → 提示注册项目 path + 三种修复（cd 项目根 / source `runs/<run_id>/orca_env.sh` /
设 `ORCA_PROJECT_ROOT`）；registry 空 → 提示 source env 或设 `ORCA_PROJECT_ROOT`。

**保留现有 JSON 契约**（host 读 `reply["runs"]`）：新增可选 `hint` 字段，不破坏既有字段。
**不聚合跨注册项目 run 数据**（隔离不变式）——hint 只是文字指引，不改 `runs` 列表内容/来源。
registry 读失败（`RegistryCorruptError`）→ 当作空 registry 处理（status 不应因 registry 坏而崩，
doctor 另行检测）。

## 改动文件

- `orca/runtime/_project.py`：新增 `resolve_runs_dir()`（line 154-173）。
- `orca/runtime/__init__.py`：导出 `resolve_runs_dir` + `__all__`。
- `orca/iface/in_session/cli.py`：
  - `_default_tape_path`（~line 558）改走 `resolve_runs_dir`。
  - `_write_orca_env`（~line 457-519）加 `ORCA_PROJECT_ROOT` 行 + docstring 同步。
  - 新增 `_build_empty_runs_hint()`（~line 1767）。
  - `status` 命令（~line 1881）接线 hint（JSON 新字段 + 文本输出）。
- `tests/iface/in_session/test_runs_resolution.py`：新建，16 个测试覆盖 6 项不变式。

## 不变式（测试覆盖）

1. `ORCA_PROJECT_ROOT` 未设 → `resolve_runs_dir()` == `Path("runs")`，
   `_default_tape_path("x")` == `Path("runs")/"x.jsonl"`（零回归，与今天逐字节一致）。
2. `ORCA_PROJECT_ROOT=/tmp/proj` → `resolve_runs_dir()` == `Path("/tmp/proj/runs")`。
3. 隔离：解析器只返回一个项目根下的 runs，绝不回溯祖先、绝不跨注册项目搜索（monkeypatch
   `detect_project_root` 为「被调即爆炸」，验证两分支都不触发）。
4. `_write_orca_env` 产物含 `export ORCA_PROJECT_ROOT=...` 且值为解析出的项目根（绝对路径）；
   幂等性：bootstrap（CWD 相对）写一次 + next（已 source env）重写 → 字面值一致。
5. 既有 `tests/iface/cli/test_bg_runner.py` 全绿（没改 bg_runner）。
6. 既有 in-session CLI 测试全绿（temp dir 跑，不设 `ORCA_PROJECT_ROOT`，走 CWD 回退）。
7. status 空 markers → JSON 新增可选 `hint` 字段（registry 非空/空两种文案）；markers 非空 →
   不出现 `hint`（既有 JSON 契约不破坏）。

## 验证结果

- `test_runs_resolution.py`：16 passed。
- 回归套件（`test_bg_runner.py` + `test_project.py` + `test_in_session_cli.py` + `test_v3_step1.py`
  + `test_node_memory.py` + `test_resolve_artifacts_dir.py` + `test_resolve_artifacts_dir_integration.py`）：
  284 passed。

## Commit

- `658d1cd` —— 主改动（runtime `_project.py` 新增 `resolve_runs_dir` + cli.py 三处接线 + 17 测试 + 本 release note）。

## 偏离决策

- **未加 `ORCA_RUNS_DIR` env**：用户已决策——`ORCA_PROJECT_ROOT` 锚点 + `RUNS_DIRNAME` 常量派生
  已足够（runs 恒为 `<project_root>/runs`），多一个 env 反而引入「`ORCA_RUNS_DIR` 与
  `ORCA_PROJECT_ROOT/runs` 不一致」的新歧义面。
- **hint 触发条件用 `not markers`**（而非 `not runs`）：精确对齐用户 spec「当
  `runs_dir.glob("orca-*.json")` 为空时」；markers 存在但全损坏（doctor 职责）不触发 hint。
- **bad env 测试用 monkeypatch `_resolve_strict`**：Linux `Path.resolve(strict=False)` 对不存在
  路径不抛，无法用真实坏路径触发；monkeypatch 直接验证 try/except 包装语义（intent：测 fail-loud
  语义，非测 OS 边界）。

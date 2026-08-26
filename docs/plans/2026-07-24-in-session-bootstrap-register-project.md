# 计划：in-session bootstrap 注册项目（修复 TARS run 在 web 不可见）

> 2026-07-24。bug fix（局部遗漏，surgical），非架构问题。

## 1. 现象

远程 in-session（TARS）启动 run 后：
- 前端 list 页面看不到该 run。
- `orca open --run <id>` 打开的详情页无 agent 信息。
- 远程 `~/.orca/projects.json` **不存在**。

用户 `tars project rebuild` 后恢复 → 坐实根因。

## 2. 根因

SPEC §13（单端口 + 多 Run 监控）把 web 的 run 发现与详情懒挂载**全部改为依赖注册表** `~/.orca/projects.json`：
- 列表 `GET /api/runs?scope=all` → `RunManager.discover_runs()` 读 `list_registered()` 逐项目扫 `runs/*.jsonl`。
- 详情 `/api/runs/<id>/{meta,events}` → `ensure_attached` → `resolve_run_path` 查 `_run_path_index` + 扫注册项目 + legacy 兜底。

注册表写入方只有三处：`orca run`（`commands.py:1142` POST `project_path` → server `start_run` `register_project`）、`tars project rebuild`、web `start_run`。

**遗漏**：in-session `bootstrap`（`orca/iface/in_session/cli.py:953`）创建 run（`gen_run_id` → `_default_tape_path` → `Tape` → `write_marker`）但**从不调 `register_project`**（grep 整个 `in_session/` 对 `register_project`/`detect_project_root` 零命中）。TARS 是 in-session 驱动 → 项目永不进注册表 → discovery + 懒挂载全 miss → 两个症状。

## 3. 方案（最小 surgical fix）

在 `in_session/cli.py` 新增模块级 helper，并在 `bootstrap` post-lock 段调用。

### 3.1 helper

```python
def _register_current_project() -> None:
    """SPEC §13 D4：in-session run 注册所属项目到 ~/.orca/projects.json。

    discovery（列表）+ 懒挂载（详情）都依赖注册表；in-session bootstrap 此前漏注册 →
    TARS 启动的 run 在 web 不可见。与 orca run / tars project rebuild 同语义
    （detect_project_root）。

    fail-open：注册失败（项目根无 workflows/ 等）只 warn 不阻断 bootstrap——run 照常，
    仅 web 可见性退化（用户可 `tars project rebuild` 手动补登记）。
    """
    try:
        from orca.runtime import detect_project_root, register_project
        register_project(detect_project_root())
    except Exception:  # noqa: BLE001 — web 可见性 fail-open，不阻断 run
        logger.warning(
            "bootstrap: 注册项目失败，run 仍可跑但 web 列表/详情不可见"
            "（可 `tars project rebuild` 手动补登记）",
            exc_info=True,
        )
```

依赖方向：`iface/in_session → orca/runtime`（中立层，仅 stdlib + schema）——合法向下，不破铁律 5。

### 3.2 调用点

`bootstrap` 的 post-lock 段（SPEC §3 O2：bootstrap_lock 释放在 write_marker 之后），`assert run_id/tape_path/result` 三行（约 `cli.py:1153-1155`）**之后**、chart daemon section（约 `:1157`）**之前**插一行：

```python
_register_current_project()
```

理由：
- post-lock 段只在「真启动路径」（marker 已落 + ws 已 emit）到达，dupe/busy/失败均 early-exit——注册只发生在真 run 上。
- 不进 `bootstrap_lock` 临界区（register 自带 `.projects.lock`，且 dupe-check 临界区应保持紧凑，SPEC §3 O2）。
- run_id 此处已 assert 非 None；注册虽不依赖 run_id，但语义上「这个 run 属于这个项目」在 run 确立后注册最自然。

### 3.3 fail-open 对齐

与既有 daemon spawn（`_spawn_sidechain_daemon` OSError → warn 不 fail bootstrap）、artifacts mkdir 失败（warn 不 fail）同 fail-open 语义：web 可见性是便利层，注册失败不让 run 跑不起来。

## 4. 测试（Rule 9：测意图）

`tests/iface/in_session/test_in_session_cli.py` 同风格（`CliRunner` + `cwd_tmp`/`wf_path` fixture，进程内跑，daemon autouse 清理）。

1. **正向** `test_bootstrap_registers_project_for_web_discovery`：
   - monkeypatch `ORCA_HOME=tmp/.orca_home`（隔离，不污染真实 `~/.orca`）。
   - 建 `tmp/workflows/`（满足 register M-16 marker）+ `ORCA_PROJECT_ROOT=tmp`（钉死 detect）。
   - `_bootstrap(runner, wf_path)` → 断言 `list_registered()` 含 `tmp` 路径 + `projects.json` 落盘。
2. **fail-open** `test_register_current_project_fail_open`：
   - `ORCA_PROJECT_ROOT` 指向无 marker 目录 → `register_project` raise → `_register_current_project()` **不抛**（仅 warn）。

## 5. 验收

- 上述两测绿；既有 in_session / runtime 测试零回归。
- code-reviewer 自检：依赖铁律（iface→runtime 合法）、fail-open、DRY（与 start_run/rebuild 同语义无重复逻辑）、无静默吞错（warn + exc_info）。

## 6. 不做

- 不改 discovery / resolve_run_path（它们依赖注册表是 SPEC 契约，正确）。
- 不在 `next`/`stop` 注册（bootstrap 一次即可；next 不增新 run）。
- 不处理「cwd ≠ project_root 时 tape 在 cwd/runs 而 detect 返回祖先」的 pre-existing 边界（`orca run` 同样如此，TARS 用户从项目根跑，cwd==project_root，本 fix 与现状一致）。

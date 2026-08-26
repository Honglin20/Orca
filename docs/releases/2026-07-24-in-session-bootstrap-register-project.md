# Release Note：in-session bootstrap 注册项目（修复 TARS run 在 web 不可见）

> 2026-07-24 · bug fix · commit 见 CHANGELOG

## 现象

用户在远程以 in-session（TARS）模式启动 run 后：
- 前端 list 页面看不到该 run。
- `orca open --run <id>` 打开的详情页无 agent 信息。
- 远程 `~/.orca/projects.json` **不存在**。

`tars project rebuild` 后恢复 → 坐实根因。

## 根因

SPEC §13「单端口 + 多 Run 监控」（commits `1788cea` / `449f851`）把 web 的 run 发现与详情懒挂载**全部改为依赖注册表** `~/.orca/projects.json`：

- 列表 `GET /api/runs?scope=all` → `RunManager.discover_runs()` 读 `list_registered()` 逐项目扫 `runs/*.jsonl`。
- 详情 `/api/runs/<id>/{meta,events}` → `ensure_attached` → `resolve_run_path` 查 `_run_path_index` + 扫注册项目 + legacy 兜底，全 miss → 404。
- `orca open --run <id>` 显式 attach 过 `resolve_tape_path` allowlist（2a server runs_dir / 2c 注册表），single-port 共享 server cwd 非本项目 + 未注册 → 被拒。

注册表写入方原本只有三处：`orca run`（`commands.py:1142` POST `project_path` → server `start_run` `register_project`）、`tars project rebuild`、web `start_run`。

**遗漏**：in-session `bootstrap`（`orca/iface/in_session/cli.py`）创建 run（`gen_run_id` → `_default_tape_path` → `Tape` → `write_marker`）但**从不调 `register_project`**。TARS 是 in-session 驱动 → 项目永不进注册表 → discovery + 懒挂载全 miss → 两个症状。这是 §13 改造引入的回归：此前 web 靠单一 `runs_dir` 直扫，in-session run 可见；改成注册表后漏接了 in-session 这条 run 写者。

## 修复（surgical）

`orca/iface/in_session/cli.py`：

1. 新增模块级 helper `_register_current_project()`：`detect_project_root()` → `register_project()`，`try/except Exception` + `logger.warning(exc_info=True)` **fail-open**（注册是 web 可见性便利层，失败只降级不阻断 run；用户可 `tars project rebuild` 手动补登记）。
2. `bootstrap` 命令的 post-lock 段（三个 `assert` 之后、chart daemon section 之前）插一行调用。

**插入点选择**：post-lock 段只在真启动路径（marker 已落 + ws 已 emit）到达；不进 `bootstrap_lock` dupe-check 临界区（SPEC §3 O2 紧凑契约）；register 自带 `.projects.lock`，与 bootstrap lock / tape flock 无嵌套（均已释放），无死锁风险。

**依赖方向**：`iface/in_session → orca/runtime`（中立层，仅 stdlib，连 `orca.schema` 都不依赖）——合法向下，不破铁律 5。

**fail-open broad catch 的理由**：`register_project` 可抛 `ValueError`（无 `workflows/` marker，M-16）/ `RegistryCorruptError`（继承 `RuntimeError`，注册表坏）/ `OSError` / `ModuleNotFoundError`，任一都不应阻断 run。比 daemon spawn 的 `except OSError` 覆盖面广，因 register 失败面更广。注释已显式说明。

与 `orca run` / `web start_run` / `tars project rebuild` 同 primitive（`detect_project_root` + `register_project`），**零重复逻辑**（`_resolve_project_path_for_run` 额外返回 path 决定 tape 写入位置，本 helper 只注册——语义不同）。

## 测试（Rule 9：测意图）

`tests/iface/in_session/test_in_session_cli.py` 加两测：

- `test_bootstrap_registers_project_for_web_discovery`：隔离 `ORCA_HOME` + 建 `workflows/` marker + `ORCA_PROJECT_ROOT` 钉死 detect + 禁 `ORCA_BOOTSTRAP_OPEN_WEB`，走真端到端 `_bootstrap`，断言 `list_registered()` 含本项目 + `projects.json` 落盘——直接验证「TARS run 在 web 可见」的派生证据，非测 mock。
- `test_register_current_project_fail_open`：`ORCA_PROJECT_ROOT` 指向无 marker 目录 → `register_project` 真抛 `ValueError` → helper 不抛 + 注册表仍空——验证 fail-open 不阻断 bootstrap 契约。

## 验证

- `tests/iface/in_session/ tests/runtime/test_project.py`：**494 passed / 2 pre-existing fail**。
- 两个失败（`test_cli_gc_max_age_zero_rejected` / `test_skill_md_flags_subset_of_cli_help`）经 `git stash` 本改动后**照样失败**，确认与本次无关（未碰 `orca gc` / 未加 CLI flag / 未动 SKILL.md）。
- code-reviewer 自检：**0 🔴 / 1 🟡（broad catch 注释显式化，已采纳）/ 2 🟢（corrupt registry 测 + lazy import，均「保持」建议）**。判定为 bug fix（局部遗漏），非架构问题。

## 不在范围

- 不改 discovery / resolve_run_path（依赖注册表是 SPEC 契约，正确）。
- 不在 `next`/`stop` 注册（bootstrap 一次即可）。
- 不处理「cwd ≠ project_root 时 tape 在 `<cwd>/runs/` 而 detect 返回祖先」的 pre-existing 边界（`orca run` 同样如此；TARS 用户从项目根跑，cwd == project_root）。

## 计划

[`docs/plans/2026-07-24-in-session-bootstrap-register-project.md`](../plans/2026-07-24-in-session-bootstrap-register-project.md)

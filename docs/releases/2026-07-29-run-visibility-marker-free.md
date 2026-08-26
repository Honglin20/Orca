# Release: in-session run 可见性根治（marker-free 自注册）

**日期**: 2026-07-29
**Commit**: `cfcad05`
**SPEC**: `docs/specs/run-visibility-marker-free-spec.md`（v3，§4.1 契约 A-E）
**范围**: Bug A（可见性根治）；不含 Bug2（前端/auto-exit，另案）。

---

## 背景

在任意文件夹（含无 `workflows/` 的目录）下用 in-session（TARS / `orca` CLI）启动 workflow 时：
- 前端 web 列表看不到该 run；
- 子 agent 事件推不到前端；
- 手动 `tars project rebuild` 后才恢复可见。

根因是三层叠加：tape 落点（`cwd/runs`）、注册根（`detect_project_root` 可能跳祖先）、
discovery 扫描（注册根/runs）三处独立计算，仅当重合才可见；且 M-16 marker 门槛太死（无
`workflows/` 就拒注册），一次性 fail-open 无自愈。

## 改动（逐字对齐 SPEC §4.1 A-E）

### 契约 A：`register_project` 加 `require_marker` kwarg
- `register_project(project_root, *, require_marker: bool = True)`：仅门控 M-16 marker 检查。
- M-15（拒 OS 顶层）+ P2（拒 ORCA_HOME）两道闸**始终守**（独立 `if/raise`，不受开关影响）。
- 默认 `True`：外部注册（`tars project add` / rebuild 新候选）零改动。`False`：仅可信自注册。

### 契约 B：`_register_current_project`（in-session bootstrap）
- 改注册 `_default_rundir().resolve().parent`（= tape 物理所在 cwd），`require_marker=False`。
- 不再用 `detect_project_root()`（旧逻辑可能跳 cwd 祖先，与 tape 落点 `cwd/runs` 脱节）。
- fail-open try/except 保留（M-15/P2/RegistryCorruptError → warn 不阻断 bootstrap）。

### 契约 C：`_resolve_project_path_for_run`（start_run）
- `register_project(root)` → `register_project(root, require_marker=False)`（D1=是）。
- 显式 `project_path` 入参失败仍 raise（fail loud，契约不变）。

### 契约 D：`rebuild_registry`（D-rebuild=A，不擦除旧 marker-free entry）
- step2 收 `old_paths = {meta["path"] for old_projects}`（注册表 path 原文，不二次 resolve）。
- step5 `require_marker = path_str not in old_paths`：旧 entry → False（信任），新候选 → True（严格 M-16）。
- ⚠️ round-2 C1 critical：布尔方向 `not in`（旧版伪代码 `in` 反号会两头破：旧 entry 被擦除 + 新候选绕 M-16）。

### 契约 E：抽 `RUNS_DIRNAME = "runs"` 到 runtime 中立层
- `run_manager.py` 5 处字面（runs_dir 默认 / start_run tape 落点 / discover_runs /
  _lookup_project_for_handle / resolve_run_path）+ `is_registered_runs_dir` +
  `bg_runner.default_tape_path` + `mcp_tools.server._DEFAULT_RUNS_DIR` 全部引用常量。
- 推翻 `commands.py:1553`「跨层不能共享常量」旧 workaround（常量放中立层，web/cli/exec 合法向下 import）。
- 划界（不扩面）：`_legacy_runs_root`（`$ORCA_HOME/runs` legacy 元数据根）、`bg_runner.ORCA_RUNS_DIR`
  是不同语义，SPEC §4.1 E 显式标记不扩面，留待后续全局目录 SPEC。

## 测试（Rule 9：测意图，逐条对齐 §7 AC / §8）

- **AC1**：`require_marker=False` 接受无 marker 目录；默认 `True` 拒同目录（双向）。
- **AC2**：`require_marker=False` 对 `/ /etc /home /tmp /usr` 仍 raise（M-15 不绕过，参数化）；
  对 ORCA_HOME 仍 raise（P2 不绕过）。
- **AC3**：无 workflows/ 的 tmp 目录 bootstrap → 注册命中 cwd + `discover_runs()` 命中（source=attached）+
  `resolve_run_path` 命中。scrub `ORCA_PROJECT_ROOT`，CliRunner cwd 钉 `cwd_tmp`。
- **AC4a**：marker-free 注册项目 → `ensure_attached` 成功 + `get_run_events` 非空（确定性，合成 terminal tape）。
- **AC4b**：start_run（marker-free 项目）→ handle 存在 → WS subscribe → `await handle.bus.emit(type, data)`
  （`EventBus.emit`，非 publish）→ FakeWebSocket 收到（确定性，单 loop 驱动）。
- **AC5a**：既有 marker 项目经 rebuild 注册不变（零回归）。
- **AC5b**：marker-free 旧 entry 经 rebuild 仍存在（**C1 关键守门**，mutation 验证非空泛）。
- **AC5c**：scrub env，cwd=`<proj>/sub` + `<proj>/workflows/` → detect 跳祖先 → tape 落 `<proj>/runs`（非 sub/runs）+
  注册 `<proj>` + discovery 命中。

### 改写既有测试（SPEC §8 H3/H4 + D-rebuild=A 行为变更）
- `test_bootstrap_registers_project_for_web_discovery`（`:112`）：去 env/workflows 依赖，测 marker-free 注册。
- `test_register_current_project_fail_open`（`:141`）：monkeypatch `_default_rundir → Path("/")` 触发 M-15
  （撤回旧"无 marker → fail"假设——`require_marker=False` 后该假设已失效）。
- `test_rebuild_clears_stale_entries` → `test_rebuild_trusts_old_entries_but_gates_new_candidates`：
  D-rebuild=A 后旧 entry 不被 re-gate；新候选仍走严格 M-16。单用例双向守门。
- `test_rebuild_all_fail_rolls_back_to_old_registry`：重构造"全失败"场景（注入顶层 path 作旧 entry，
  M-15 始终拒即便 require_marker=False）。

### 配套
- `test_dependency_no_run_no_compile` 精确化模式（区分 `orca.run` 禁 / `orca.runtime` 允——旧模式
  `"from orca.run"` 是 `"from orca.runtime"` 的子串，误报）。
- `FakeWebSocket` 提取到 `tests/iface/web/conftest.py`（test_ws.py + test_run_manager.py 共享，DRY）。

## 偏差

无。实现严格匹配 SPEC §4.1 A-E，无额外字段/参数。

## 验证

- runtime/test_project.py：35 passed（含 AC1/AC2/AC5a/AC5b + 改写 2）。
- iface/in_session/test_in_session_cli.py：113 passed（含 AC3 + 改写 2）。
- iface/web/test_run_manager.py：15 passed（含 AC4a/AC4b/AC5c）。
- exec/test_contract.py：23 passed（精确化模式不误判 orca.runtime）。
- 更广回归（test_attach/test_ws/test_multi_run_phase_c/test_routes/test_bg_runner 等）：147 passed。
- **既有 `test_web_does_not_import_cli` 在 base 分支即 fail**（pre-existing，`run_manager.py:37`
  `from orca.iface.cli.config` 非本改动引入，属独立架构 issue）。

## code-reviewer 结论

- 源码 review：**0 🔴 / 0 🟡 / 2 🟢（预存问题，非本改动）**。三道安全闸独立性、C1 布尔方向、
  DRY 单源、fail loud、契约 fidelity 全通过。"SPEC-driven surgical fix 的范例，建议合入"。
- 测试 review：**0 🔴 / 1 🟡（FakeWebSocket DRY，已修）/ 5 🟢**。C1 守门经 mutation 验证非空泛。
  "测试覆盖扎实且意图驱动"。

## 下一步

- test-agent 端到端 headless 验证（多文件夹含无 workflows/）—— SPEC §10 step 6。
- Bug2（前端/auto-exit）另案。

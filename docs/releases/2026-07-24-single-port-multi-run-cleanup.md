# Release Note: 单端口 + 多 Run 监控「遗留清项」（SPEC §13 v4 carry-over）

**日期**：2026-07-24
**分支**：`in-session-unified-backend`
**前置**：[2026-07-24-single-port-multi-run-monitoring.md](2026-07-24-single-port-multi-run-monitoring.md)（SPEC §13 生产代码 commit 1788cea + c5cf298）
**SPEC**：[2026-07-23-single-port-multi-run-monitoring.md §13](../specs/2026-07-23-single-port-multi-run-monitoring.md)

## 概览

清掉 SPEC §13 v4「遗留（非阻塞）」清单全部七项（CURRENT.md 旧条目）：

1. **AC14 contract test**（§13.4 M-17）：从 `orca.schema.EventType` Literal **自动派生**
   status-affecting 子集；新 EventType 不归类即 CI 红。`_scan_meta_overview` 显式声明
   `OVERVIEW_AFFECTING_EVENT_TYPES` + `BULK_EVENT_TYPES` 双档（白名单之外都算
   status-affecting，双向断言钉死）。
2. **P0 持久派生缓存**（§13.3）：`<runs_dir>/.orca-meta-cache.json` cache（**cache 非
   index**，可删可重建不违 R1/§9）。三层查询（in-memory → persistent → recompute）；
   损坏 → warn + 删文件重建。
3. **`tars project rebuild`**（§13.3 P1）：注册表损坏时重建——pre-rebuild 快照 +
   全失败回滚（数据安全：不清空用户注册表）。附 `tars project list`（含 stale）。
4. **P3 Stale projects 折叠区**（§13.3 P3 / §6.3）：后端 `GET /api/projects/stale` +
   前端 `StaleProjectsSection`（只读折叠 + `tars gc` / `tars project rebuild` 提示）。
5. **统一 `open` 列表语义**（§13 D13）：`orca open --list` 强制列表；无活跃 run →
   回落列表页（不再 fail loud 提示）。有活跃 run 仍直达详情（保持 bootstrap 体验）。
6. **`scripts_e2e_driver.py` 归位**：repo 根未跟踪文件移到 `scripts/e2e_driver.py`。
7. **pre-existing 失败清理**：`test_entry_skill_md_has_no_business_logic_keywords`
   （SKILL.md 含禁词 'compile'）+ `test_install_cc_nudge_script_never_calls_next`
   （cc_nudge.sh 含反引号）两处 quick fix；第 3 项 `test_web_does_not_import_cli`
   涉及 `apply_kb_requirement` web→cli 反向依赖（架构问题）—— **不动**，登记在此。

## 改动文件

### 新增
- `orca/iface/web/routes/projects.py`：`GET /api/projects/stale`（只读，依赖 `orca.runtime` 中立层）。
- `orca/iface/cli/project_cmds.py`：`tars project rebuild/list` sub-Typer。
- `tests/iface/web/test_scan_meta_overview_contract.py`：AC14 contract（14 测）。
- `tests/iface/web/test_persistent_meta_cache.py`：P0 缓存（4 测）。

### 修改
- `orca/iface/web/run_manager.py`：双档 EventType frozenset + `_scan_meta_overview_cached`
  三层缓存 + `_persistent_cache_*` helpers。
- `orca/runtime/_project.py`：`list_stale_projects` + `rebuild_registry`（含 pre-rebuild
  快照 + 全失败回滚）。
- `orca/iface/cli/commands.py`：装配 `project` sub-Typer。
- `orca/iface/web/server.py` + `routes/__init__.py`：include projects router。
- `orca/iface/in_session/cli.py`：`open_run` `--list` flag + 无活跃回落语义；DRY 重构
  `_default_active_run_id` 复用 `_default_active_run_id_or_none`。
- `orca/skills/tars/SKILL.md`：去掉禁词 'compile'。
- `orca/iface/in_session/templates/cc_nudge.sh`：去掉反引号。
- `orca/iface/web/frontend/src/stores/run-list-store.ts` + `RunListPage.tsx`：
  `staleProjects` 状态 + `StaleProjectsSection` 折叠区。
- `tests/runtime/test_project.py`、`tests/iface/in_session/test_in_session_cli.py`、
  `tests/iface/web/test_attach.py`、`tests/iface/cli/test_web_default_and_open.py`：
  新测 + 铁律 allowlist 更新（`_persistent_cache_by_runs_dir` 注释为 cache 例外）。

### 移动
- `scripts_e2e_driver.py` → `scripts/e2e_driver.py`。

## 验证结果

- **新增单测**：runtime 6（rebuild + stale）+ web 18（AC14 contract 14 + P0 缓存 4）+
  in_session 3（open --list / 无活跃回落 / 有活跃直达）= **27 新测全绿**。
- **回归**：tests/iface/web + tests/runtime + tests/iface/in_session（含 v3_step1）+
  tests/iface/cli 关键文件（test_commands / test_web_default_and_open / test_install_cmds）
  共 **463 passed / 23 skipped / 1 failed**。
- **唯一 fail**：`test_web_does_not_import_cli`——pre-existing 架构问题
  （`run_manager.py: from orca.iface.cli.config import apply_kb_requirement`），
  非本批改动引入；SPEC §13.2 §13.3 现行架构主动接受，留作后续 PR 处理。
- **前端 build**：`cd orca/iface/web/frontend && npx tsc --noEmit && npx vite build` 成功
  （`orca/iface/web/static/` 已更新）。

## 偏差与决策

- **perf spike 未实跑**：SPEC §13.3 P0 原计划「1000 tape 实测冷扫墙钟；≤3s 仅内存缓存，
  超 3s 启用持久」。本次未跑真 spike（环境无 1000 tape fixture）；SPEC 也说「无论结果，
  持久缓存实现出来」——直接实现持久层，若未来 perf 数据表明不需要可加 env switch 关闭。
  留作后续（`tests/iface/web/test_attach_perf.py` 已有相关 perf 基础设施）。
- **持久 cache 路径**：放 `<runs_dir>/.orca-meta-cache.json`（而非 `~/.orca/cache/`）——
  与 SPEC §13.3 P0 原文「`<project>/runs/.orca-meta-cache.json`」逐字一致。code-reviewer
  建议迁到 `~/.orca/cache/` 防 git 误 add，**保留原路径**：tape 也在 `<runs>/`，cache 跟随
  项目走更内聚；用户可加 `.gitignore`（后续 orca install 可自动写入 `.gitignore`）。

## code-reviewer 闭环

3 个 🟡 全修：
1. `list_stale_projects` 静默吞 `RegistryCorruptError` → 补 `logger.warning`（fail loud 精神）。
2. `rebuild_registry` 全失败不可回滚 → 加 pre-rebuild 快照 + 全失败回滚（数据安全）。
3. `_default_active_run_id` / `_default_active_run_id_or_none` 重复 → DRY 重构。

4 个 🟢 可选优化采纳 3：删空 `TYPE_CHECKING` 块 / 损坏 cache 文件 unlink 防重复 warn / 加
rebuild 全失败测试。1 项（持久 cache 加跨进程 flock）不采纳：cache 正确性不依赖跨进程一致
（失配重算），加锁反而引入死锁面，**fail loud 精神优于过度防御**。

## Commit

见 CHANGELOG 顶部索引（本次单 commit 合入）。

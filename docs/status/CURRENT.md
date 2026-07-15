# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

## 当前状态（2026-07-16）：teams→tars 后端命令改名完成（代码侧闭环），仅余真机

> **新 session 必读**：本块 + [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) **v5** + [teams→tars release note](../releases/2026-07-16-teams-to-tars-rename.md) + [TARS skill release note](../releases/2026-07-15-tars-skill-rebrand.md)。

后端命令 `teams` → `tars`（与 TARS skill 对齐）。三套命名收口：**skill = `tars` / 后端命令 = `tars` / in-session = `orca`**。代码 + 测试 + SPEC + shipped 产物全闭环；剩余跨平台真机验证。

### 2026-07-16 已完成（最新）

- **teams→tars 后端改名**（`<本 commit>`）：`pyproject` 入口 + `DEFAULT_BACKEND_CMD` + `validator` 保留字 + help/docstring + 用户面消息（orca epilog/doctor/skill 弃用警告）+ shipped 产物（cc_nudge.sh / create-workflow SKILL.md / templates / skills）+ `examples/mxint_analysis.yaml` 注释。`teams_app` deprecated 别名保留（向后兼容）；`orca` in-session 不动；`ORCA_BACKEND_CMD` env 名不变。重装后 `tars` 上 PATH / `teams` 退场。768 单测 0 回归；code-reviewer 两轮 0 🔴（全修）。详见 [release note](../releases/2026-07-16-teams-to-tars-rename.md)。
- **TARS rebrand / step 6 / 批量 FU / step 5b / step 3b / step 5a / defects / step 4 / step 2b / step 1**：见 CHANGELOG 索引。

### 待办（用户侧真机，无代码；§9 跨平台）

- **tars 真机**：`tars install --target cc` → `.claude/skills/tars/SKILL.md` 真生成；`tars --help` / `tars list` / `tars validate` 真工作；`orca` 命令不受影响（`teams` 已退场）；`orca doctor` skill_install pass。**纯 CLI 禁 MCP**。
- **§9#1 nga/cac 全套集成真机加载**：CAC/NGA 是否真读 `.cac`/`.nga`；cac Stop-hook / nga `opencode.json` plugin 是否真生效。

### follow-up / debt（用户暂缓 / 预存，非阻塞）

- **既有测试隔离缺陷**（非本任务引入，code-reviewer R1 🟢 登记）：`test_orca_list_returns_inputs_schema_json` 未隔离 `~/.orca/workflows` user-level 扫描根，全局有 wf 时 `assert len==1` 失败。择机 monkeypatch `Path.home` 修。
- **既有 `test_bg_run_ps_logs_wait_e2e` rot**：`orca run --background` 选项不存在（in-session CLI 无 run）。择机修或删。
- **MCP 移除**：用户暂不移除（spec v5 §8 留 MCP 8 tool 出 scope）。
- **`in-session-unified-backend-draft.md`**：推迟架构草稿，仍含 `teams` 残留（YAGNI，启用时再改）。
- `_load_wf_for_run` 的 `catalog.find_workflow` fallback 无测试触达（step 3b 预存）。
- tape `workflow_failed.data.kind` 是 `ErrorKind`/`error_kind` 两值集共享字段（跨阶段 debt，5b 登记）。

---

## 跨阶段其他待立项（与 in-session 正交，不影响当前）

- **三壳统一 ADR**（[`2026-07-08-shell-unification-adr.md`](../specs/2026-07-08-shell-unification-adr.md)）：单一读路径 + 渲染契约 + 视觉，待 spec-review。
- **agent interrupt**（[`agent-interrupt-design-draft.md`](../specs/agent-interrupt-design-draft.md)）：mid-stream cancel+resume，待立项 SPEC。
- **render layer v1.5**（codex 接入）/ **v2**（Web TS 镜像 + 流式 shiki + diff 虚拟化）。
- **TUI fold DRY**：fold 字段抽 `orca/run/projections.py`。
- **phase-16 批 2**：本地包分发 + workspace-instruction。

## 必读文件（下一任务开工前按需）

- [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5
- [teams→tars release note](../releases/2026-07-16-teams-to-tars-rename.md)
- [CHANGELOG](CHANGELOG.md)

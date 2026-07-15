# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

## 当前状态（2026-07-15）：spec v5 §8 全 step + TARS rebrand 闭环，仅余用户侧真机

> **新 session 必读**：本块 + [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) **v5** + [TARS release note](../releases/2026-07-15-tars-skill-rebrand.md) + [step 6 release note](../releases/2026-07-15-in-session-step6-nga-cac-install.md)。

代码侧无待办 step；剩余全是跨平台真机验证 + 用户暂缓的 follow-up。

### 2026-07-15 已完成（最新）

- **TARS rebrand**（`<本 commit>`）：用户面 skill 改名 `orca`→`tars`（`/tars` + TARS description：一句话意图 → `orca list` 语义匹配 → 多个则问 → 抽 inputs → 派子代理 → `orca next` 循环）。CLI/命令仍 `orca`（TARS 用 orca 引擎）。抽 `ENTRY_SKILL_NAME = "tars"` 常量（doctor + install + 测试三处单一真相源）。176 单测 0 回归；code-reviewer 两轮 0 🔴（2 🟡 已修）。详见 [release note](../releases/2026-07-15-tars-skill-rebrand.md)。
- **step 6** / 批量 FU-2+3a+FU-3 / step 3b / step 5b / FU-1 / step 5a / defects+step4 / step 2b / step 1：见 CHANGELOG 索引。

### TARS 配套（用户「先想知道」——备查）

- **注册 workflow**：把 workflow YAML + agent prompt 放 `./workflows/` 或 `~/.orca/workflows/`（SPEC §2.1）。**匹配靠 `description` 字段**（skill 据 description 语义匹配用户意图）——description 写清 = 自动匹中；多个同类 → skill 问用哪个。
- **生成方式**：`create-workflow` skill（一句话需求 → 合规 YAML + agent md + `orca validate`）。

### 待办（用户侧真机，无代码；§9 跨平台）

- **TARS 真机**：`teams install --target cc` → `.claude/skills/tars/SKILL.md` 真生成（name=tars、description TARS）；`orca doctor` skill_install pass；create-workflow 也在。
- **§9#1 nga/cac 全套集成真机加载**：CAC/NGA 是否真读 `.cac`/`.nga`；cac Stop-hook / nga `opencode.json` plugin 是否真生效。
- **§9#1 nga 两个假设**：user-scope 现用 `~/.nga`（XDG 对称应 `~/.config/nga`）+ 配置文件名假设 `opencode.json`——真机确认后改 resolve_roots / `_opencode_json_path`。

### follow-up / debt（用户暂缓 / 预存，非阻塞）

- **MCP 移除**：用户暂不移除（spec v5 §8 留 MCP 8 tool 出 scope）。触发后再做。
- **最小核心审计**：用户暂不做。
- `_load_wf_for_run` 的 `catalog.find_workflow` fallback 无测试触达（step 3b 预存）。
- `tool_describe_workflow` found 分支无 server 层测试（预存）。
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
- [TARS release note](../releases/2026-07-15-tars-skill-rebrand.md)
- [CHANGELOG](CHANGELOG.md)（历史完成项索引，各完成块详细在对应 release note）

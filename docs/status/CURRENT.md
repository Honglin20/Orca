# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

## 当前状态（2026-07-15）：in-session spec v5 §8 —— 全 step + follow-up 闭环，仅余用户侧真机

> **新 session 必读**：本块 + [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) **v5** + [step 6 release note](../releases/2026-07-15-in-session-step6-nga-cac-install.md)（spec v5 §8 全貌）。

**spec v5 §8 全 step 收尾**（step 1 / 2b / 3b / 4 / 5a / 5b / defects / FU-1 / 批量 FU-2+3a+FU-3 / **6**）。代码侧无待办 step；剩余全是跨平台真机验证 + 用户暂缓的 follow-up。

### 2026-07-15 已完成（最新）

- **step 6**（`<本 commit>`）：teams install nga/cac 全套——CAC≡cc（skill + nudge Stop-hook）、NGA≡opencode（skill + plugin + json）；家族路由 + `_opencode_plugin_decl` 泛化；SPEC §4.3/§4.4/§11/§9#1 同步。164 单测 0 回归；code-reviewer 两轮 0 🔴。详见 [release note](../releases/2026-07-15-in-session-step6-nga-cac-install.md)。
- 批量 FU-2+3a+FU-3 / step 3b / step 5b / FU-1 / step 5a / defects+step4 / step 2b / step 1：见 CHANGELOG 索引。

### 待办（用户侧真机，无代码；§9 跨平台）

- **§9#1 nga/cac 全套集成真机加载**：CAC/NGA 是否真读 `.cac`/`.nga`；cac Stop-hook / nga `opencode.json` plugin 是否真生效。
- **§9#1 nga user-scope 路径**：现 `~/.nga`（step 2b resolve_roots），若 NGA≡opencode XDG 对称应 `~/.config/nga`——真机确认后改 resolve_roots。
- **§9#1 NGA 配置文件名**：假设读 `opencode.json`（≡opencode）；真机若读 `nga.json`/别处则改 `_opencode_json_path`。
- **③a 重型准入门**（留 sprint）。

### follow-up / debt（用户暂缓 / 预存，非阻塞）

- **MCP 移除**：用户暂不移除（spec v5 §8 留 MCP 8 tool 出 scope）。触发后再做。
- **最小核心审计**：用户暂不做。
- `_load_wf_for_run` 的 `catalog.find_workflow` fallback 无测试触达（step 3b 预存）。
- `tool_describe_workflow` found 分支无 server 层测试（预存）。
- tape `workflow_failed.data.kind` 是 `ErrorKind`/`error_kind` 两值集共享字段（跨阶段 debt，5b 登记）。
- doctor 注释「crash 孤儿 marker」与实现不一致（pre-existing 可选 backlog）。

---

## 跨阶段其他待立项（与 in-session 正交，不影响当前）

- **三壳统一 ADR**（[`2026-07-08-shell-unification-adr.md`](../specs/2026-07-08-shell-unification-adr.md)）：单一读路径 + 渲染契约 + 视觉，待 spec-review。
- **agent interrupt**（[`agent-interrupt-design-draft.md`](../specs/agent-interrupt-design-draft.md)）：mid-stream cancel+resume，待立项 SPEC。
- **render layer v1.5**（codex 接入）/ **v2**（Web TS 镜像 + 流式 shiki + diff 虚拟化）。
- **TUI fold DRY**：fold 字段抽 `orca/run/projections.py`。
- **phase-16 批 2**：本地包分发 + workspace-instruction。

## 必读文件（下一任务开工前按需）

- [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5
- [step 6 release note](../releases/2026-07-15-in-session-step6-nga-cac-install.md)（spec v5 §8 收尾全貌）
- [CHANGELOG](CHANGELOG.md)（历史完成项索引，各完成块详细在对应 release note）

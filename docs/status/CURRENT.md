# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

## 🔥 当前任务（2026-07-14）：in-session spec v5 —— step 2b 完成，待进 step 3/4

> **新 session 必读**：本块 + [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) **v5** + [step 2b release note](../releases/2026-07-14-in-session-v5-step2b.md) + [step 1 release note](../releases/2026-07-14-in-session-v3-step1.md) + [`docs/specs/in-session-unified-backend-draft.md`](../specs/in-session-unified-backend-draft.md)（合并推迟 spec）。

**step 2b 已完成**（commits `e2bd989` items 1-6 + `4b90508` item 7 nudge；code-reviewer 两轮全闭环；208 affected 单测 0 回归）：
- 建 orca skill（三步指导：list→抽 inputs→`<wf>`+自调 next；绝不读 YAML；CI 守门）。
- `orca list` 返 `{workflows:[{name,description,inputs_schema:[{name,type,description}]}]}`（无 has_setup，无 describe）。
- doctor 加 `skill_install` 硬检查（A6）+ hook 心跳可选 + `hard` 字段定 ok。
- 禁用 orca.ts transform marker dispatch（early return）；删 4 个 command 模板；删 `start` + `cc_hooks.py`。
- **nudge hook（§4.4，A5 修正入本步）**：opencode `session.idle` 改提醒模式（listActiveRuns→60s 节流→promptAsync 注入，不 spawn next）；CC 新 `cc_nudge.sh` Stop hook（零反引号 decision:block）+ `teams install --target cc` 合并 `settings.json`。**B 路径铁律：nudge 只提醒，绝不自动推进**。
- install 重构四前端（`teams install --target cc/opencode/cac/nga/all`）装所有随包 skill；平台常量抽 `skill_cmds` 单一源（DRY/OCP）。

### 待办（spec v5 §8，step 3-6）
- **③** skill 完善（catalog via `orca list` + inputs 代填已就位，skill 三步定型）+ catalog 物理迁 `orca/compile/`（B7，延后到 step 5 setup 删之后）。
- **④** opencode 收尾：删 orca.ts transform 段 + 死代码（extractTaskOutput/spawnCli/buildCliArgs/rewriteText/MARKER_REGEX 等）+ `_constants.py` MARKER_REGEX/LITERAL（**保 idle nudge hook**）。
- **⑤a** 删 setup 全栈（§6.1 清单）+ MCP migration note（§6.2）。
- **⑤b** daemon batch emit + 错误信封统一（独立 commit，C3）。
- **⑥** teams install nga/cac nudge 机制真机验证（留用户侧）。
- **推迟** 合并同一后端（`advance_step`↔`Orchestrator`），见 merge spec，等触发条件。

### step 2b follow-up（非阻塞）
- **真机验证（test-agent 的活）**：opencode promptAsync 注入 / CC Stop block / skill 实跑 wf / cac+nga nudge 机制。orca 只装 WSL conda，opencode 在 Windows——主 session 全链路 E2E 部署需 orca 装 Windows 或 opencode 装 WSL（非代码）。
- **跨 session 误注入（已知限制）**：v3 marker 去 sessionID，nudge 扫所有活跃 run 注入当前 idle session；多 session 共存时会跨渗。单 workspace 单 session 约定下无影响。
- catalog 不扫 `examples/`（demo 测试复制到 `workflows/`）；dupe-check 按 wf.name（marker 不存 yaml，注释已记）。

---

## 跨阶段其他待立项（与 in-session 正交，不影响当前）

- **三壳统一 ADR**（[`2026-07-08-shell-unification-adr.md`](../specs/2026-07-08-shell-unification-adr.md)）：单一读路径 + 渲染契约 + 视觉，待 spec-review。
- **agent interrupt**（[`agent-interrupt-design-draft.md`](../specs/agent-interrupt-design-draft.md)）：mid-stream cancel+resume，待立项 SPEC。
- **render layer v1.5**（codex 接入，前置 phase-12-capabilities）/ **v2**（Web TS 镜像 + 流式 shiki + diff 虚拟化）。
- **TUI fold DRY**：fold 字段抽 `orca/run/projections.py`（单一 reducer 消费）。
- **phase-16 批 2**：本地包分发（多 pool + `name@source`）+ workspace-instruction。
- **background chart gap**：`--background` 模式 chart 可用。
- **参考仓 F1/F3/F4/F5**（[调研](../plans/2020-07-05-reference-repos-borrow.md)；F2/G2-G7 已落入 phase-11/12）。

## 必读文件（下一任务开工前按需）
- [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5（本次范围 SPEC）
- [`docs/releases/2026-07-14-in-session-v5-step2b.md`](../releases/2026-07-14-in-session-v5-step2b.md)（step 2b 全貌）
- [`docs/specs/in-session-unified-backend-draft.md`](../specs/in-session-unified-backend-draft.md)（合并推迟 spec + 触发条件）
- [CHANGELOG](CHANGELOG.md)（历史完成项索引，各完成块详细在对应 release note）

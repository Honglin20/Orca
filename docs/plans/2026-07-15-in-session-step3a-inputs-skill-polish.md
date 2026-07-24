# Plan: in-session v5 §8 step 3a —— inputs 代填 skill 完善（小步骤，主体已完成）

> SPEC：[`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5 §8 step 3a
> 状态：草稿（待 spec-reviewer）| 分支 `in-session-unified-backend` | 前置：5a `bce29f8` / FU-1 `73a47ea` / 5b（进行中）

---

## 0. 现实判定（诚实）

`orca/skills/orca/SKILL.md` **已基本完善**（test-agent 2026-07-15 headless skill E2E 真机验证端到端跑通：list→抽 inputs→bootstrap→逐节点真 deepseek→completed）。SPEC §8 step 3a「inputs 代填 skill 完善（list 已返 inputs_schema，skill 三步定型）」的**主体已满足**：
- 三步流程（选 wf / 抽 inputs / 启动+驱动）齐全且具体（SKILL.md L37-108）。
- inputs 代填指导具体（L66-76）：description 语义匹配 / 推断未明说字段 / 只问缺失且不可推断（≤2 问）/ 按 type 给值（string/int/boolean/list）。
- 单引号转义（L110-121）、失败处理（L129-136）、常见错误（L138-143）齐全。

**故 3a = 验证 + 微调，非重写**。本计划范围小，spec-reviewer 应判断是否值得全套流程，还是「验证即闭环」。

---

## 1. 残余工作（验证 + 微调）

### 1.1 验证 post-5a/FU-1 无 staleness
- `orca stop --run-id`（SKILL.md L28/127）：FU-1 已让 CLI 支持 → skill 现在正确（验证真机 `stop --run-id` 工作，test-agent FU-1 已证）。
- 无 setup/has_setup/describe 残留（test-agent T1 已 grep = 0）。
- 7 命令表（L23-31）与 CLI 一致（next/status/stop/open 都 `--run-id`，FU-1 后统一）。

### 1.2 微调 A：失败信封加 `error_kind`（**依赖 5b 落地后**）
- 5b 给 in-session 失败信封加 `error_kind` 字段（`InSessionError.error_kind` taxonomy）。
- SKILL.md L129-136 失败处理目前教读 `reason`。**微调**：补一句「信封也含 `error_kind`（如 `output_schema_mismatch`/`state_corrupt`），可据它给用户更精确的失败归类」——**非必需**（reason 仍可用），是增强。**仅在 5b 合并后做**，否则 skill 提前承诺未落地字段。

### 1.3 微调 B（可选）：可选字段标注
- inputs_schema 字段无 required 标记（只有 name/type/description）。SKILL.md L74 例子里 style 标「（可选）」靠 description 文案。**可选**：明确「description 标『可选』的字段，用户没给可留空或推断」——当前已隐含，可不改。

### 1.4 守门（§4.5，保持）
- SKILL.md 禁业务逻辑关键词（`advance_step`/`Orchestrator`/`Tape`/`load_workflow` 等）—— test-agent T1 已证 = 0。

---

## 2. 改动范围（小）

### 2.1 `orca/skills/orca/SKILL.md`
- **微调 A**（5b 后）：L129-136 失败处理段补 `error_kind` 一句（标注为可选增强）。
- **微调 B**（可选）：inputs 段补可选字段处理一句（若 spec-reviewer 认为值得）。
- **同步已装副本**：`~/.claude/skills/orca/SKILL.md`（CC 落点）须与源同步——核实 `teams install` 是否已同步，或手动 cp。

### 2.2 无源码改动（纯 skill 文档）。

---

## 3. 测试 / E2E

- **静态守门**：grep SKILL.md 业务关键词 = 0；7 命令表与 CLI 一致。
- **E2E（test-agent）**：若微调 A 落地（5b 后），真机触发一次失败（output 畸形 → output_schema_mismatch），验证 skill 教的失败处理路径 + 信封 error_kind 可见。若仅验证无微调，则 test-agent 既有 headless skill E2E 已覆盖（list→inputs→bootstrap→驱动→completed），3a 无新增 E2E。

---

## 4. 流程建议

鉴于 3a 主体已完成 + 残余微调依赖 5b，**建议**：
- **5b 落地后**，把微调 A（error_kind 一句）并入 5b 的 follow-up 或作为 3a 独立小 commit。
- spec-reviewer 裁定：3a 是否值得独立全套流程，还是「验证即闭环 + 微调 A 并入 5b follow-up」。

---

## 流程闭环
本计划 → **spec-reviewer**（裁定 3a 工作量 + 是否独立流程）→ （若需改）**coder-agent** 微调 SKILL.md → **test-agent**（若微调 A 落地，失败路径 E2E；否则既有 skill E2E 已覆盖）。

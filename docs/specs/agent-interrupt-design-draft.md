# Agent 中断 / 续跑设计草稿（agent interrupt / resume）

> **状态**：Draft placeholder（2026-07-19）。**in-session resume 部分**由
> [`2026-07-19-in-session-hardening-and-perf.md`](2026-07-19-in-session-hardening-and-perf.md)
> §4 F1（v4.1）落地；**engine-level interrupt**（agent 主动打断 / 取消 / 暂停正在执行的节点）
> 仍 **TBD**（本文件留空骨架，待后续 ADR 填充）。
> **建立背景**：CURRENT.md / 其它 spec 曾引用本文件作"in-session 中断恢复 spec"，但文件此前
> 不存在（断链）。F1 v4.1 落地补此占位，明确：in-session resume = 已落地（F1）；engine
> interrupt = 待设计。

---

## 0. 范围切分（避免概念混淆）

| 议题 | 状态 | 落地 / 计划 |
|---|---|---|
| **in-session resume**（session 断后新 session 接手半完成 run） | ✅ **已落地** | [`2026-07-19-...-perf.md`](2026-07-19-in-session-hardening-and-perf.md) §4 F1（v4.1） |
| **engine-level interrupt**（agent 主动打断 / 取消正在跑的节点 / 全局 cancel 信号） | 🟡 **TBD** | 本文件 §2 留空骨架 |
| **host_session 绑定**（已弃用方向） | ❌ **deprecated** | [`2026-07-17-host-session-binding-design-draft.md`](2026-07-17-host-session-binding-design-draft.md) v2 草稿；F1 v4.1 据用户铁律（resume = run 级别，与 host_session 无关）弃用此方向 |

---

## 1. In-session resume（已落地）

### 1.1 设计原则（用户铁律）

**resume 是 run 级别的事，用 `run_id` 管（status/next 现成），与 host_session 无关。** 后台
（tape + marker）已知执行到哪个节点（`replay_state` → `current_node`），主 session 只需调度
续跑。

### 1.2 落地（F1 v4.1）

详见 [`2026-07-19-in-session-hardening-and-perf.md`](2026-07-19-in-session-hardening-and-perf.md)
§4 F1。摘要：

- `orca status`（无参）列活跃 run 加 `resumable: true`（marker 在即 resumable）+ 已有
  `current_node`。
- resume 复用 `advance_step` 现成的 idempotent-replay（branch 4）：`orca next --run-id X`
  （无 output）重发 `current_node` 的 prompt，**零新字段**（不透 `prompt_file`）。
- `orca/skills/tars/SKILL.md` 含「续跑（resume）」段：status → next 无 output 重发 → 子代理 →
  next output 推进。
- **不动 compliance / marker / host_session / v5 决策 11**。marker 仍 3 字段。
- **零 host_session、零 spike**。

### 1.3 已弃用方向

`2026-07-17-host-session-binding-design-draft.md` 提出的「marker 加 `last_host_session` +
跨 session 豁免 compliance」方案在 F1 v4.1 中**明确弃用**，原因：

- host_session 解的是伪问题「如何豁免 compliance 对 resume 的冤枉」，而真问题是「SKILL 不教
  resume 流程」。
- resume 主路径（status → prompt → 子代理 → next **with output**）**根本不触发 compliance**
  （compliance 只在"无 output next"时 +1）。
- 给 marker 加字段违反 v5 决策 11（marker 3 字段不动）。

---

## 2. Engine-level interrupt（TBD，留空骨架）

> 🔴 本节为占位骨架，**未设计**。需要时填下述子节，并补 ADR / SPEC v4.x 修订。

### 2.1 痛点（待澄清）

- 节点执行到一半（子代理在跑），用户/系统想取消：怎么传播 cancel 信号？
- 多节点并行（parallel / foreach）的 partial cancel？
- 取消后的 tape 终态如何落（workflow_cancelled vs workflow_failed）？

### 2.2 候选方向（待评估，未决）

- **A：CLI 信号路径** —— `orca stop` 加 cancel 语义，sidechain daemon 监听并中断子代理。
  - 撞约束：emit 序列稳定（cancel 是否需新 EventType？）/ sidechain lifecycle。
- **B：节点超时** —— marker 加 `node_started_at`，daemon 监控超时 → cancel。
  - 撞约束：v5 决策 11（marker 字段）。
- **C：主 session 主动 abort** —— SKILL 教主 session 识别 abort 信号 → `orca stop`。
  - 与现有 stop 重叠，可能零新设计。

### 2.3 约束（待实施时遵守）

- 不破 SPEC §1 铁律（不影响 workflow 决策 / 不改 emit 序列 / tape 唯一真相源 / 7 命令不增减）。
- fail loud：cancel 失败不静默。
- 与 F1 in-session resume 解耦（resume 是 run 级被动接手；interrupt 是节点级主动打断）。

### 2.4 开放问题

- cancel 的粒度（节点 / run / 全局）？
- 子代理是否需 cooperative cancel（接收信号后清理）还是 force kill？
- 并行节点 partial cancel 的一致性？

---

## 3. 引用

- [`2026-07-19-in-session-hardening-and-perf.md`](2026-07-19-in-session-hardening-and-perf.md)
  §4 F1（v4.1，in-session resume 落地）。
- [`in-session-entry-and-simplification.md`](in-session-entry-and-simplification.md) v5
  §7.2（marker 精简：3 字段契约）+ 决策 11。
- [`2026-07-17-host-session-binding-design-draft.md`](2026-07-17-host-session-binding-design-draft.md)
  （host_session 草稿，F1 v4.1 弃用）。

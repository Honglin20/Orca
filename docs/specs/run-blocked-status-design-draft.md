# Run 级 blocked 状态设计草稿（design draft）

> **状态**：前置设计草稿（pre-implementation design draft）。非契约，供后续正式 SPEC 撰写前对齐。
> **触发**：看板卡片网格重设计（[`web-board-cardgrid-redesign.md`](./web-board-cardgrid-redesign.md)）test-agent 真机实测「发现 1」——前端「待决策 / blocked」整条是死 UI。
> **日期**：2026-08-04。

---

## 1. 问题陈述

前端 run 列表/看板围绕 `blocked` 状态建了一整套「待决策」监控面：

- `status-badge.tsx`：`RunStatus` 含 `"blocked"`，`STATUS_BAR_HEX.blocked = #a78bfa`，`STATUS_LABEL.blocked = "待决策"`。
- `group-runs.ts`：status 桶有 `blocked`（accept = `{blocked}`），且 `EMPHASIS_STATUSES`/`RING_STATUSES` 强调它。
- 看板（`CardGridSection`/`BoardCard`）：待决策 section 紫边 ring、blocked 卡 `⚠ 等待 <elapsed>`、KPI「待决策」胶囊。
- AC-B10/B11：blocked section 折叠/forceOpen/ring 断言。

**但后端 run 级从不产出 `blocked`**。test-agent 真机实测（真实 `_summary_from_tape` + 真实 tape）：

```
attempt_blocked_gate       → status='running'   progress='0/1'   # human_decision_requested 不变
attempt_blocked_interrupt  → status='running'   progress='0/1'   # interrupt_requested 不变
```

本机真实安装 **1117 个 run，5 种状态，0 个 blocked**。前端这套 UI 永远空——KPI「待决策」永远 0、待决策 section 永远隐藏、blocked 卡紫边永不触发。vitest 能过仅因 mock 了 `status:"blocked"`（一个真实后端永不产出的值）——**mock 边界假绿**。

## 2. 根因

`orca/iface/web/run_manager.py`：

- `RunStatus` 字面量（L70-71）= `{queued, running, completed, failed, cancelled, live-pending}`——**无 `blocked`**。
- `_summary_from_tape`（L1726-1737）把 workflow 级事件映射到这 5 个 run 状态；`human_decision_requested` / `interrupt_requested`（gate/interrupt）**只存在于 node 级投影**（`orca/run/projections.py`），不 fold 到 run 级 status。

即：**gate/interrupt 是 node 级概念，从未升格为 run 级状态**。前端却假设它是 run 级。这是历史遗留的前后端契约裂缝（原 Trello 看板的「待决策」列同样依赖 blocked），非本次重设计引入。

## 3. 设计目标

让「待决策」监控面有真实数据源，使「run 卡在等人决策」这一监控语义可观察、可点开、可计数——与看板重设计的「失败/待决策提级」目标（[`web-board-cardgrid-redesign.md`](./web-board-cardgrid-redesign.md) §1.3）真正落地。

## 4. 方案选项

### 方案 A（推荐）：后端 fold——`_summary_from_tape` 派生 run 级 blocked

`_summary_from_tape` 在算 run status 时，检测 tape 内是否有未解决的 gate/interrupt 事件（`human_decision_requested` 未跟 `human_decision_resolved`、或 `interrupt_requested` 未跟 `interrupt_resolved`）→ run status = `blocked`（优先级高于 running/queued）。

- **真相源单一**：run 级 status 仍由后端 fold 产出，前端零派生逻辑（符合 Orca「单 tape 唯一真相源 + fold」原则）。
- **前端零改**：blocked 相关 UI 已就绪（看板重设计已建），等数据即可激活。
- **语义清晰**：run 级 blocked = 「该 run 当前有节点在等人」。
- **代价**：改 `run_manager.py`（后端），需新增 `blocked` 到 `RunStatus` 字面量 + fold 逻辑 + 后端测试；`RunStatus` 是跨前后端契约，需同步前端 `status-badge.tsx`（已有 blocked，无需改）。
- **优先级规则**：blocked 与 running/queued 的优先级需定义（run 同时 running 且有未决 gate → blocked？倾向于 blocked 优先，因「等人」比「在跑」更需关注）。

### 方案 B：RunSummary 增字段 `has_pending_decision`（不改 run status）

后端 fold 时算一个布尔 `has_pending_decision`（有未决 gate/interrupt），加进 `RunSummary`；前端不改 run status 语义，而是用这个布尔驱动「待决策」高亮。

- **优点**：不触碰 run status 枚举（契约面更小）。
- **缺点**：run status 与「是否待决策」变成两个独立维度，前端要同时消费 status + 布尔，复杂度上升；`group-runs` 的 status 桶无法直接收（blocked 不是 status），要么加新分组维度、要么前端派生——违背「单真相源」。

### 方案 C：前端从 node 投影 API 派生

前端调 node 级投影 API（若存在），自行判断 run 是否有未决 gate/interrupt，派生 blocked。

- **缺点**：前端派生 = 多真相源（违背 Orca 底线）；每个 run 要额外拉 node 数据，N+1 / 性能问题；最不可取。

## 5. 推荐：方案 A

理由：真相源单一（后端 fold）、前端零改（UI 已就绪）、语义最清晰。契合 Orca「单 tape 唯一真相源 + 幂等 fold」架构底线（见 `CLAUDE.md` 项目背景 / `docs/specs/phase-3-events.md` §1）。

## 6. 影响面（方案 A 落地时）

- **后端**：
  - `orca/iface/web/run_manager.py`：`RunStatus` 加 `"blocked"`；`_summary_from_tape` 增 fold 逻辑（扫 gate/interrupt 事件的 requested vs resolved）+ 优先级规则。
  - 后端测试：`tests/iface/web/` 加 blocked fold 用例（gate 未决 → blocked；gate 已决 → 不 blocked；interrupt 同）。
- **前端**：零改（`status-badge.tsx`/`group-runs.ts`/`CardGridSection`/`BoardCard`/`KpiStrip` 已支持 blocked；待数据激活）。可能需把 vitest 的 mock blocked 用例标注为「集成态」或保留作契约守卫。
- **契约**：`RunStatus` 是跨前后端契约，加 `blocked` 需在正式 SPEC 显式声明 + 前后端同步释放。
- **WS**：run 级 status 变 blocked/resolved 时，`/ws` 的 `run_changed` 控制帧应正常推送（status 字段已是 run 级）。

## 7. 待决问题（正式 SPEC 前需对齐）

1. **优先级**：run 同时 running + 有未决 gate → status 取 `blocked` 还是 `running`？（倾向 blocked，因「等人」更紧急。）
2. **fold 触发事件全集**：除 `human_decision_requested`/`interrupt_requested`，是否还有其它「等人」事件（如 elicitation、外部审批）需 fold？
3. **resolved 判定**：`human_decision_resolved` 是否唯一解阻塞信号？interrupt 的 resolved 是什么事件？
4. **live-pending 关系**：`live-pending`（同步中）与 `blocked` 是否可能同时出现？优先级？
5. **历史 run**：已 completed 的 run 若曾有 gate，不应回溯为 blocked（fold 只对进行中 run 生效）。

## 8. 下一步

本 draft 对齐后，撰写正式 SPEC（如 `docs/specs/phase-N-run-blocked-status.md`），走 SDD：spec-review → coder-agent 实现（后端 fold + 测试）→ test-agent 真机实测（造 gate/interrupt tape，验 KPI 待决策 >0、待决策 section 有卡、blocked 卡紫边触发）。前端无需改动，待后端 fold 落地后自然激活。

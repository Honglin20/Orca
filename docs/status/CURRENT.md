# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 状态（2026-07-21）

- 🔄 **Workflow 全面重设计**（进行中，[计划](../plans/2026-07-21-workflow-redesign.md)）：8 workflow 审计 → 三根因（A 造假数据/B IN-SESSION 无法问用户/C render_chart 无轴标签）+ DAG 过度拆分 + 产物目录混乱。已冻结：[input 三档原则](../specs/workflow-input-design-principle.md) + [ask-user 哨兵契约 Arch 1](../specs/agent-ask-user-sentinel.md)（TARS 层拦截，引擎零改动，opencode task_id 恢复已验证）。 coder 拆解 9 包（P1-P9），批1 = P1(render_chart 轴标签)+P2(修路径拼接)+P3(0-b spike)。
  - ✅ **P2 完成**（`e41974f`）：struct/kd setup 节点 output_schema 加显式带尾斜杠字段（snapshots_dir / worktree_root / 等），下游改读字段而非 `{{ output_dir }}<suffix>` 拼接——从源头杜绝兄弟孤儿目录。详见 [release note](../releases/2026-07-21-workflow-path-concat-fix.md)。范围外 7 个 agent.md 同款拼接 + CONTRACTS.md stale 登记给 Phase 3 P7。**注意**：本分支 viz 大修（b820ef1…f516223）已完成 color 字段/KD viz_kd/bit-curve 真 pareto 等，但 champion-trace 横轴/pareto y=0/exploration-tree/round-leader/render_chart 轴标签**仍未修**（审计确认）。

- ✅ **`orca open` 跨项目端口占用修复 + `bootstrap` 默认自动开 web**（commit `7d9b7eb` + `9677c1e`）：A 修「7428 被别项目 orca 占 → 静默挂错 tape」（项目指纹复用 + registry + 绝对路径）；B 让 bootstrap 启动即自动开 web（detach `orca open`，stdout 契约零污染，默认开）。spec-review 两轮 + code-reviewer 全闭环；987 passed。详见 [release note](../releases/2026-07-21-orca-open-cross-project-and-bootstrap-auto-open.md) + [CHANGELOG](CHANGELOG.md)。Follow-up：`orca run` reuse 同类隐患（R4）/ `tars serve --runs-dir`（R8）/ 指纹隐私（H5）。
- ✅ **Workflow 可视化全量优化**（7 点，每点独立 agent + 逐 diff 验收，commit `b820ef1`…`f516223`）：前端加 `color` 字段（hue 优先的 per-row 着色）；sensitivity bar 去 hue 改 color、table 改全层；**修 KD 0 图 bug**（viz_round 复用 viz_struct 但 schema 不匹配致 0 图 → 新建 viz_kd.py 4 图 + 改 yaml）；struct 加逐候选表；bit-curve 假 pareto 改真 pareto + 全候选 scatter；ptq-sweep 删无意义 hue + table 补失败行；qat 补训练 loss 曲线。KD 用真实账本 mock 捕获证实修前 0 图→修后 5 图。详见 [release note](../releases/2026-07-21-workflow-viz-overhaul.md)。
- ts_quant 已 editable 装入 conda orca env（实测可用）；待正式加进 orca pyproject 依赖。
- 本地领先 origin 多 commit（push 待用户手动）。

## 待确认（收尾，非阻塞）

- ts_quant 正式进 orca pyproject 依赖（落实"装 orca 即装 ts_quant"）
- 各 workflow 真机 in-session E2E + `orca open` 截真图替换文档 📊 占位（含本次新可视化：sensitivity 统一色 bar / KD 4 图 / qat loss 曲线等）
- 量化 workflow sidecar 脚本是否统一补永久单测（本次按既定惯例未补，用 py_compile/tsc/真实账本 mock 捕获验证；详见 release note「测试策略说明」）

## 并行：in-session 加固（orca 引擎，可穿插）

P5（F1 resume）done。候选 P2（marker 三态）/ P4（失败兜底）/ P6（contract-test），待用户选定。既有 debt/follow-up 全量见 CHANGELOG，SPEC `docs/specs/2026-07-19-in-session-hardening-and-perf.md` v4.1。

## 必读文件（开工前按需）

- [CHANGELOG](CHANGELOG.md)
- `docs/workflows/README.md`（workflow 索引 + 量化 pipeline 顺序）
- `docs/in-session-usage.md`（in-session 安装与使用）
- 可视化契约：`orca/chart/_render.py`（render_chart，含 color 字段）+ `orca/iface/web/frontend/src/components/chart/`（8 widget）

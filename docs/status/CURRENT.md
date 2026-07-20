# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 状态（2026-07-21）

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

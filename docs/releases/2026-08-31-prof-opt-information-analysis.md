# prof-opt：信息成分拆解 → 创新结构提案

## 动机

prof-opt 的 structure-proposer 想法空间封闭：正向候选主要来自 structural-levers
目录（有限家族），创新只在「穷尽感」出现时被口头鼓励，缺少从第一性原理出发的推理
起点。本次新增信息成分分析，让 agent 能提出 lever 目录之外的创新结构（如
4dk3→bilinear 这类目录外优化）。

## 改动

1. 新增子代理 `information-analyst`（`workflows/prof-opt/subagents/information-analyst.md`）：
   按 base 版本 stamp 守卫（`base/.information_stamp.json`，复用
   `.bottleneck_stamp` 模式）产出 `base/information_analysis.md`，四节：
   信息成分拆解 / 最小信息核心 / 冗余与可近似项 / 创新结构方向（2-5 个）。
2. `po_propose` 新增 Step 2.5：base 变化时调度 information-analyst，sentinel +
   非空校验；Step 3 将分析全文作为 structure-proposer 的第 5 类证据源传入；
   `base/information_analysis.md` 进入 generated_artifacts。
3. `structure-proposer` 新增 `Novel structures (catalog-external)` 小节：
   lever 目录无合适条目时，可从信息分析的最小信息核心推导目录外结构，`lever`
   用 `novel:<descriptor>` 命名（signature builder 视作不透明字符串）；
   其余准入不变——op delta 以实际导出图为准、predictor 严格负值、dedup、
   accuracy rules、瓶颈锚全部照旧。

## 设计不变量

- 信息分析是定性推理素材，与 `bottleneck_analysis.json` 的 interpretation 同等待遇：
  prose 不机器校验，数字仍只来自机械报告与 ledger。
- 所有机械闸门不变：`predicted_delta_cycles < 0`、history dedup、rules、
  `target_pattern_id` 引用、emit 校验。
- 精度判断权不变：probe 曲线 gap + full-train 终审。

## 验证

- `tars validate workflows/prof-opt/workflow.yaml` 通过。
- 一致性自查：sentinel（`IXA3N7`）三处对齐、Inputs 编号、Step 2.5 与复用路径
  （proposals.json 已存在 → resume Step 4，stamp 保证不重复调度）。
- commit `89ae28a`。

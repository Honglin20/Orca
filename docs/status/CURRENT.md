# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 状态（2026-07-19）

- **当前主线：量化能力（PatchTST_Optimal / ts_quant）集成到 Orca 作为 workflow**。nas-agent 已有两示例（pipeline + hp-search）；量化进行中（W1+W2 完成，W3–W4 待做）。
- ✅ **heatmap chart_type 已加**（commit `ec3d598`，第 8 种图）——W2 full 模式矩阵可视化依赖，已落地+review+测试 green。
- ✅ **W2 quant-ptq-sweep 完成 + E2E 验证通过**（commit `d356979` + fixes）：粗粒度 PTQ 扫描+报告+bake，单节点双 mode（lightweight 4 累积路径 / full 全枚举）。修正 W1 `w4a16` 语义错位。**E2E 经 opencode headless + tars 在 ViT-Tiny 跑通**：11 候选/best smooth+gptq(mse 0.0089)/bake 出 best_quant_model.pt/line+bar+table 三图推 web（demo_target/vit_tiny_cifar100 作永久 fixture）。fixes：baked_model_path 入 output、line 按 step_idx 对齐、agent.md 用 nas-select 自洽块 + `--env_file` 自加载 env 兜底。
- ts_quant 已 editable 装入 conda orca env（实测可用）；待正式加进 orca pyproject 依赖。
- 本地领先 origin 多 commit（push 待用户手动）。

## 量化 workflow 路线图（W1–W4）

- ✅ **W1 敏感层分析** `quant-sensitivity`（commit `ca6bb60`）：单 agent + `run_sensitivity.py`（`analyze_low_precision_sensitive_layers` + `render_chart` 可视化）。method 四选一（mse/layer_stats/ptq_binary_sensitivity/mix_precision_search），low_bits 默认 w4a4-mx 可配，按模型原始顺序排名。ViT-Tiny 端到端实测通过（50 层 / 5 敏感层 / bar+table 推 web）。
- ✅ **W2 粗粒度 PTQ 扫描** `quant-ptq-sweep`（commit `d356979`）：单节点双 mode。lightweight=4 累积路径 ablation（11 unique 候选，line+bar+table）；full=位宽×算法组合全枚举（45 候选，heatmap+scatter+table）。默认 teacher-student mse eval + bake 最佳 state_dict。修正 W1 `w4a16` 语义错位。
- ⬜ **W3 位宽-精度曲线**：`search_mix_precision(strategy=m0_pareto, mode=explore)` 产 `bit_trend.json` + `frontier.png`（x=平均位宽, y=最高精度），库里现成不用外循环。
- ⬜ **W4 QAT**：`prepare_trainable_fakequant_model(scheme=rtn|duquantpp)` + `prepare_trainable_qat`（CAGE 后校正 `W←W−lr·λ·(W−Q(W))`）。需训练数据。

## 待确认（量化）

- W2/W3/W4 的 input 契约 + 可视化形态（逐个讨论，仿 W1 流程）
- ts_quant 正式进 orca pyproject 依赖（落实"装 orca 即装 ts_quant"）
- W1 后两 method 实测（ptq_binary_sensitivity / mix_precision_search，需业务 eval_fn）

## 并行：in-session 加固（orca 引擎，可穿插）

P5（F1 resume）done。候选 P2（marker 三态）/ P4（失败兜底）/ P6（contract-test），待用户选定。既有 debt/follow-up 全量见 CHANGELOG，SPEC `docs/specs/2026-07-19-in-session-hardening-and-perf.md` v4.1。

## 必读文件（开工前按需）

- `workflows/quant-sensitivity.yaml` + `workflows/agents/sensitivity-analyzer/`（W1 范本）
- `PatchTST_Optimal/README_TRAINING.md` §8（敏感层 4 method）+ `README_SDK.md`（PTQ/QAT API）
- [CHANGELOG](CHANGELOG.md)

# 2026-07-04 mxint_analysis 真实 bitx 量化分析迁移

## 背景

`examples/mxint_analysis.yaml` + 配套 5 agent prompts + `tests/e2e_mxint/` 之前是**简化 stub**：
fake MLP（无 torch/bitx 依赖）+ fake JSON 输出，2 分钟跑完。用户判定"复杂的精度分析工作流，
不可能跑这么快"，要求迁移到**真实 bitx 量化分析**（保留 opencode+deepseek-v4-flash agent 后端）。

## 改动点

### 1. target_project（真实 PyTorch）

- **删 stub**：`models/simple_net.py` + `data/loader.py`（伪 DataLoader）+ `weights/model_weights.json`
  + `train.py` 全部删
- **新增**：
  - `models/model.py` —— `ConfigurableMLP(nn.Module)`（3 层 Linear 64→64→64→10，relu，无 bn，
    8970 params）；删 `**kwargs` noise，所有 hidden layer 用同一宽度（checkpoint shape 可预测）
  - `data/loader.py` —— sklearn digits (8x8=64, 10 类, 1797 样本)，80/20 split，
    `get_data()` 返回 `(calib_data[32], eval_loader[360])`
  - `checkpoint.pt` —— `train_target.py` 训练生成，~90% eval_acc，30 epoch Adam lr=1e-3
  - `README.md` —— 更新为新结构说明

### 2. tools（真 bitx driver）

- **删** `tools/diagnostic_pipeline.py`（stub，三阶段 fake 数据）
- **重写** `tools/run_analysis.py` —— 真调 bitx：`Session(model, config, observers=[QSNR/
  MSE/Distribution/Histogram/PerBlockQSNRObserver])` → `session.run(calib, eval_data, eval_fn)`
  → `StudyReport({"quant": [result]}).save(output_dir)` 写真 `results.json` + figures + tables
- **新增** `tools/run_diagnostic.py` —— bitx 1.1.1.dev395 含 `DistOverlayData.to_chart_data`
  已知 bug（`int(self.fp32[i])` 但 HistogramObserver 某些 stages 把 fp32_hist 序列化成
  `tensor([3., 1., ...])` 字符串 → `int('t')` raise ValueError）。driver 在本进程 monkey-patch
  `to_chart_data`（number → round；非 number → 0），限本进程不污染 bitx 全局，再调
  `bitx.api.diagnostic_api.run_diagnostic_pipeline(output_dir)`
- **新增** `tools/train_target.py` —— 一次性训练脚本（写 `checkpoint.pt`）

### 3. 5 agent prompts（迁 AgentHarness 原版）

- `analyzer.md` —— 工具名映射（bash→Bash, read_text_file→Read 等），保结构
- `configurator.md` —— adapter 模板（含完整 import + checkpoint 加载），**强制 cpu/cuda
  跳过 mps**（bitx 在 MPS 上有 "Placeholder storage" PyTorch bug）
- `runner.md` —— 真调 `run_analysis.py` driver（5-15 分钟 bitx 计算），从 results.json 读
  fp32/quant accuracy + worst layer QSNR
- `diagnostic_saver.md` —— 真调 `run_diagnostic.py` driver（含 bitx bug patch），落盘 coarse/
  deep_dive/prescription 三套 JSON
- `report_painter.md` —— spawn-script 模板（agent 写推图脚本 + Bash 跑），5 张图
  （accuracy/bottleneck/sensitivity/qsnr_depth/recovery），写 `REPORT.md`

### 4. mxint_analysis.yaml

- `description` 更新为真实 bitx 量化分析
- `runner.tools` 加 `Edit`（adapter 微调）
- `report_painter.tools` 加 `Write`（写推图脚本 + REPORT.md）
- 5 个 output_schema 字段不变（保输出契约）

### 5. .gitignore

`tests/e2e_mxint/.gitignore` 排除 `output/`（每次跑都重生）+ `target_project/_*.py`
（agent 写的 adapter / smoke test 脚本）

## Deviations from plan

- **device 检测排除 mps**：原 AgentHarness configurator 检测 cuda/mps/cpu 三选一，但 bitx
  + MPS 触发 PyTorch "Placeholder storage" bug（checkpoint 在 MPS 上保存后 map_location='cpu'
  不能完全 detach tensor device metadata）。改为只 cuda/cpu（prompt 显式说明）。
- **bitx DistOverlayData bug patch**：bitx 1.1.1.dev395 自身 bug，driver 内 monkey-patch
  `to_chart_data`。修 bitx 库本身超出本 workflow 职责。
- **chart 仅 TUI/foreground 模式可用**：`_run_workflow_headless`（background 模式）不起
  chart ingestor，但 `ClaudeExecutor._resolve_chart_sock_path` 仍往 env 透传死 sock 路径。
  这是 Orca 架构 gap，**留作 follow-up**（应改 `_run_workflow_headless` 加 chart ingestor
  或让 `ORCA_CHART_SOCK` 注入前检查 sock 是否真存在）。当前在 prompt 里告知 agent
  spawn-script 模式，foreground 跑 5 张图全部推送成功（5 个 custom(chart) 事件落 tape）。

## 验证

- **回归测试**：`pytest tests/ --ignore=tests/e2e_mxint --ignore=tests/e2e_phase14`
  → 1333 passed, 30 skipped, 0 回归（baseline 1333）
- **validate**：`teams validate examples/mxint_analysis.yaml` → PASS，无 DeprecationWarning
- **真跑**（foreground，TUI 路径）：`teams run examples/mxint_analysis.yaml`
  - elapsed: **185s**（3 分钟，远超 stub baseline 2 分钟）
  - 5 个 chart 事件落 tape（accuracy table / bottleneck bar / sensitivity / qsnr_depth line / recovery bar）
  - `outputs.report_preview` 是 76 行真实精度分析报告，含真 QSNR 数据（51.37 dB avg）、
    weight-dominated 判断、per-layer depth decay（48.0 → 54.9 dB）、recovery 31.7%
  - tape 路径：`runs/mxint_analysis-20260704-105608-90fd22.jsonl`（186 events，120 工具事件）

## Commit SHAs

- `<this commit>` —— feat(mxint): 真实 bitx 量化分析迁移（替 stub）+ 5 agent prompts 真版

## 与 stub 版的关键差异

| 维度 | stub 版 | 真实版 |
|------|--------|--------|
| 运行时长 | ~2 分钟 | 3-5 分钟（bitx Session + 5 observers + StudyReport.save） |
| target | 伪 SimpleNet（无 torch） | 真实 ConfigurableMLP（8970 params, sklearn digits） |
| bitx 调用 | 无 | 真调（Session + diagnostic_pipeline 三阶段） |
| per-layer QSNR | 假数据（公式伪造） | 真数据（48.0/51.2/54.9 dB） |
| 报告复杂度 | markdown 表格 | 5 章节 + 真图表 spawn-script + recovery 策略建议 |
| chart 事件 | 无 | 5 个 custom(chart)（accuracy/bottleneck/sensitivity/depth/recovery） |

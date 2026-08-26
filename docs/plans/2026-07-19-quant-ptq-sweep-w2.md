# W2: quant-ptq-sweep — 粗粒度 PTQ 扫描 workflow

> 事前实施计划（SDD）。设计经主 session 讨论确认，本文件是子 agent 构建契约 + 主 agent review 清单。

## Context（为什么）

量化 pipeline 第二级。W1 找敏感层后，W2 在「整模型一致的**位宽 × 量化算法**」网格上**全面对比** PTQ 方案，产精度对照报告 + bake 最佳。定位：W3（per-layer 细搜）之前的全面预筛，也独立可用。

heatmap（commit `ec3d598`，第 8 种 chart_type）已就绪，W2 full 模式的矩阵可视化用 heatmap。

## 能力与目的

给定 FP 模型，遍历 {位宽 × 量化算法（含组合）}，逐个 `quantize_model` + eval（默认 teacher-student mse 免业务接线 / 业务 eval_fn 进阶），输出精度对照 + bake 最佳配置的量化模型。

**两种 mode**：
- **lightweight**（默认）：沿「累积路径」线性叠加技术（ablation），4 条路径，~12–14 候选。签名图 `line`（累积曲线）。
- **full**：按 SDK 合法/拒绝表全枚举所有合法 `(预变换, 求解, 后处理) × 位宽`，~45–75 候选。签名图 `heatmap`（矩阵）。

## 单节点设计（仿 W1 `quant-sensitivity`）

一个 agent 节点。agent 只做：①读模型生成 `adapter.py`（唯一填空）②调 `run_ptq_sweep.py` 回显 stdout JSON。量化/eval/落盘/推图全在确定性脚本。

## inputs

- `model_path`(req)、`project_root`(req)
- `calib_data_ref`(opt，空→假随机)、`eval_data_ref`(opt，空→复用 calib)
- `eval_fn_ref`(opt，空→默认 teacher-student mse via `build_teacher_student_eval_fn`)
- `mode`(opt，default `lightweight`): `lightweight` | `full`
- `bit_widths`(opt，逗号串): lightweight 默认 `w4a4-mx`；full 默认 `w4a4-mx,w4a8-mx,w8a8-mx`
- `recipes`(opt): lightweight=路径集名；full=`all` 或指定子集
- `output_dir`(opt)、`bake`(opt，default true)

## adapter.py 填空（agent 读模型生成）

`load_model()` / `get_calib_loader()` / `get_eval_loader()` / `forward_fn()` / `get_eval_fn()`(opt)。仿 W1 `sensitivity-analyzer/agent.md`。

## run_ptq_sweep.py（确定性，无 LLM；仿 `run_sensitivity.py`）

1. import adapter → FP teacher + calib + eval loaders
2. 候选网格（mode 分支）：
   - **lightweight**：4 累积路径
     - S(Smooth 派): rtn → rtn+smooth → smooth+gptq → smooth+gptq+q2n
     - Q(QuaRot 派): rtn → rtn+quarot → quarot+gptq → quarot+gptq+q2n
     - A(AutoRound 派): rtn → autoround → autoround+q2n
     - R(纯求解派): rtn → gptq → gptq+q2n
   - **full**：枚举 `(None/Smooth/QuaRot) × (RTN/GPTQ/AutoRound) × (none/q2n)`，按 SDK 拒绝表过滤
3. 每个 candidate: `quantize_model(model, qconfig, calib_data, plugins)` → eval_fn 打分 → 记录 `(config_label, bit_width, pre, solver, post, mse, 业务?)`
4. 选 best（mse↓ / 业务↑）
5. bake: `torch.save(best.state_dict(), <output_dir>/best_quant_model.pt)`
6. 落 `report.json`（全候选 + best）
7. render_chart（容错不阻断）
8. stdout JSON 摘要（agent 原样回显）

**逐候选 try/except 隔离**（一个失败不拖垮全扫）+ **report.json 增量落盘**（崩了能看已扫部分）。

## 算法→配置映射（script 内部，grounded SDK §9.4）

- 位宽预设：`w4a4-mx / w4a8-mx / w8a8-mx / w8a8-int / w4a16`（复用/扩展 W1 `_qconfig`）
- 算法预设 → `(weight_solver, post_correction, plugins[])`：
  - rtn→`(rtn, none, [])`；gptq→`(gptq, none, [])`；autoround→`(autoround, none, [])`（需 `auto-round` 包，缺则跳过+stderr）
  - smooth→`(rtn, none, [SmoothQuantPlugin])`；quarot→`(rtn, none, [QuaRotPlugin])`
  - q2n 后处理→`post_correction="q2n"`（**只接 gptq/autoround 后**，RTN+Q2N 拒）
  - 组合如 smooth+gptq→`(gptq, none, [SmoothQuant])`，**两遍校准需 `max_steps`**
- **拒绝组合**（脚本跳过，否则 QConfig raise）：RTN+Q2N、AutoRound 与 GPTQ 同段互斥、FP8/SMX 拒 GPTQ 系列

## 可视化（render_chart，label 统一 `quant/ptq-sweep`）

- lightweight: `line`（x=路径步骤, y=精度, hue=路径）+ `bar`（终点对比）+ `table`
- full: `heatmap`（y=recipe, x=bitwidth, value=精度）+ `scatter`（x=bitwidth, y=精度, hue=recipe）+ `table`

## output_schema

`{output_dir, report_path, model_path, best_config, best_metric, candidates_evaluated, mode, metric_kind}`

## 验证

- `tars validate` 0 error（create-workflow skill 自动跑）
- E2E 冒烟（阶段 5）：ViT-Tiny / VGG16 跑 lightweight，经 opencode headless + tars skill

## 产出文件

- `workflows/quant-ptq-sweep.yaml`
- `workflows/agents/ptq-sweeper/agent.md`
- `workflows/agents/ptq-sweeper/scripts/run_ptq-sweep.py`

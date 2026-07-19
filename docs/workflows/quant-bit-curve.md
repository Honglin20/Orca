# quant-bit-curve：混合精度 Pareto 位宽-精度曲线（W3）

> Orca 量化流水线第 3 级。与 W2 互补——W2 在固定位宽下挑选 PTQ 算法，W3 则放宽「所有层同位宽」的假设，在精度约束下搜索**逐层位宽分配**，绘制平均位宽与量化误差的 Pareto 前沿，并以可部署的混合精度模型烘焙最优候选。

---

## 1. 实现概览

### 1.1 这个 workflow 做什么

`quant-bit-curve` 在给定的候选格式空间（如 `INT8/W4A8/INT4/MX4/MX8`）上，为模型的每一层搜索一种量化格式，使整体在「平均位宽」与「量化误差」两个目标上达到 Pareto 最优。其底层调用 `ts_quant.search_mix_precision`（策略 `m0_pareto`），输出 Pareto 前沿曲线、选中候选的格式分布，并烘焙最佳混合精度模型。

### 1.2 架构与流程

该 workflow 为单 agent 节点编排：`bit-curve-searcher` 读取用户模型生成适配层 `adapter.py`，随后调用确定性脚本 `run_bit_curve.py` 完成「格式空间构建 → 混精搜索 → 报告解析 → 烘焙 → 可视化 → 摘要回显」。

```
                ┌─────────────────────────────────────────────────────┐
                │             bit-curve-searcher (单 agent)            │
                └─────────────────────────────────────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────────────┐
        ▼                          ▼                                  ▼
  ① 读模型 model.py          ② 生成 adapter.py                ③ 调 run_bit_curve.py
                             load_model / calib / eval          (确定性脚本)
                             forward_fn / eval_fn
                                                                   │
          ┌────────────────────────────────────────────────────────┘
          ▼
   ┌──────────────────── run_bit_curve.py 七步 ──────────────────────┐
   │                                                                  │
   │  1. import adapter → FP teacher + calib/eval + eval_fn           │
   │                                                                  │
   │  2. base_qconfig(INT8) + TSQuantizer.prepare → q_layers 层图      │
   │                                                                  │
   │  3. candidate_format_space 别名 → QConfig 列表（INT8/W4A8/INT4/  │
   │     MX4/MX8）                                                     │
   │                                                                  │
   │  4. MixPrecisionSearchConfig(strategy=m0_pareto, mode, ...) →    │
   │     search_mix_precision → (best_configs, report)                │
   │     （SDK 自落盘 frontier.json / bit_trend.json 到 search_dir）  │
   │                                                                  │
   │  5. 解析 report（frontier.points / final / eval_calls）→         │
   │     bit_curve_summary.json（脚本自维护摘要，不覆盖 SDK report）  │
   │                                                                  │
   │  6. bake（可选）：final.layer_configs → QConfig.from_dict →      │
   │     quantize_model(qconfig_dict=...) → best_mixed_model.pt       │
   │                                                                  │
   │  7. render_chart（容错）：line（Pareto 曲线）+ bar（格式分布）    │
   │     + table（前沿候选）；失败仅 stderr，不阻断                    │
   │                                                                  │
   │  8. stdout JSON 摘要（agent 原样回显）                            │
   └──────────────────────────────────────────────────────────────────┘
```

### 1.3 输入 / 输出

**输入**：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `model_path` / `project_root` | string | — | 模型入口 / 项目根 |
| `calib_data_ref` / `eval_data_ref` / `eval_fn_ref` | dotted-path | `""` | 校准/评估 loader + 业务 eval_fn（空→teacher-student MSE） |
| `mode` | enum | `explore` | `explore`（出完整前沿）/ `constrained_select` / `minimize_bit_under_accuracy` |
| `candidate_format_space` | csv | `INT8,W4A8,INT4,MX4,MX8` | 候选格式集（全 mxint/int 基） |
| `bit_objective` | enum | `weight_activation_proxy` | 位宽成本口径（`weight_activation_proxy` / `weight_only`） |
| `accuracy_tolerance` | float-str | `0.01` | 相对 baseline 的精度损失绝对容忍 |
| `avg_bit_budget` | float-str | `""` | 硬平均位宽上限（空=无硬约束） |
| `max_evals` | int-str | `32` | 主搜索预算（32=smoke，128=真实模型） |
| `granularity` | enum | `per_tensor` | `per_tensor` / `per_token` / `per_channel` |
| `bake` | bool-str | `true` | 是否烘焙选中混合精度模型 |

**输出**：`bit_curve_summary.json`（脚本摘要）+ `baked_model_path`（`best_mixed_model.pt`）+ SDK 原始 `frontier.json`/`bit_trend.json`（在 `search_artifacts/` 子目录）+ 可视化（line/bar/table）。节点 stdout 的 JSON 摘要字段含 `best_config`、`best_metric`、`best_bit`、`candidates_evaluated`、`mode`。

### 1.4 如何激活

```
用 TARS 找一下位宽和精度的折中曲线
TARS，这个模型能压到多少比特还能保精度？
```

匹配命中的关键词：**位宽 / 精度 / Pareto / 混合精度 / 折中曲线 / bit**。等价手动命令：

```bash
orca quant-bit-curve --inputs '{
  "model_path": "demo_target/vit_tiny_cifar100/model.py",
  "project_root": "demo_target/vit_tiny_cifar100",
  "mode": "explore",
  "candidate_format_space": "INT8,W4A8,INT4,MX4,MX8",
  "max_evals": "32", "bake": "true"
}'
```

---

## 2. 定义

**混合精度量化（mixed-precision quantization）** 放弃「所有层共享同一位宽」的约束，允许为每一层 $\ell$ 独立指定一种格式 $f_\ell\in\mathcal{F}$，其中 $\mathcal{F}$ 为候选格式集。一个完整的分配 $\mathbf{f}=(f_1,\dots,f_L)$ 称为一个**候选**。每个候选对应两个标量目标：平均位宽 $B(\mathbf{f})$（衡量压缩程度，越小越好）与量化误差 $L(\mathbf{f})$（衡量精度损失，越小越好）。

**Pareto 支配**。候选 $\mathbf{f}_a$ 支配 $\mathbf{f}_b$（记 $\mathbf{f}_a \succ \mathbf{f}_b$），当且仅当 $\mathbf{f}_a$ 在两个目标上均不劣于 $\mathbf{f}_b$、且至少一个目标严格更优：

$$\mathbf{f}_a \succ \mathbf{f}_b \;\iff\; \bigl(L(\mathbf{f}_a)\le L(\mathbf{f}_b)\bigr)\;\wedge\;\bigl(B(\mathbf{f}_a)\le B(\mathbf{f}_b)\bigr)\;\wedge\;\bigl(L(\mathbf{f}_a)<L(\mathbf{f}_b)\;\vee\;B(\mathbf{f}_a)<B(\mathbf{f}_b)\bigr)$$

**Pareto 前沿**为不被任何其他候选支配的集合：

$$\mathcal{P} \;=\; \bigl\{\,\mathbf{f}\in\mathcal{X}\;:\;\nexists\,\mathbf{f}'\in\mathcal{X},\;\mathbf{f}'\succ\mathbf{f}\,\bigr\}$$

其中 $\mathcal{X}$ 为实际评估过的候选集合。本 workflow 的任务即是在可评估预算内尽可能完整地逼近 $\mathcal{P}$，并按需在前沿上选点。

---

## 3. 背景

### 3.1 统一精度的两难

全高位宽（如全 INT8）精度高但压缩收益小；全低位宽（如全 INT4）压缩大但精度常崩塌。两者皆非最优。混合精度的洞察在于：并非所有层对低位宽同等敏感（此即 W1 的结论），对敏感层保留高位宽、对鲁棒层施加低位宽，可在平均位宽大幅下降的同时几乎不损失精度。

### 3.2 组合空间爆炸

逐层独立选格式的搜索空间规模为 $|\mathcal{F}|^{L}$。以 $|\mathcal{F}|=5$ 种格式、$L=50$ 层计，空间达 $5^{50}\approx 8.9\times 10^{34}$，穷举不可行。直接在该空间上做黑盒优化（每个候选需一次真实模型评估）受限于评估预算 `max_evals`，故必须先对搜索空间结构性剪枝，再以启发式搜索逼近前沿。

### 3.3 与 W1/W2 的衔接

W1 给出的层敏感度为 W3 提供先验：`m0_pareto` 搜索器内部首先执行一次敏感度探测，据此对每层限定可选格式集，将 $|\mathcal{F}|^{L}$ 压缩为 $\prod_{\ell}|F_\ell|$（$F_\ell\subseteq\mathcal{F}$ 为层 $\ell$ 的允许集）。W2 选出的 PTQ 算法可作为每个格式的默认求解器。

---

## 4. 方法

W3 的核心是 `m0_pareto` 搜索策略，由**敏感度探测 → 层策略剪枝 → 主搜索**三段构成。

### 4.1 敏感度探测（sensitivity probe）

在主搜索前，对每个待搜层 $\ell$ 执行一次「留一层低精度」探测：以基准格式（`base_qconfig`，默认 INT8）为参考，仅将第 $\ell$ 层降至探测格式（`probe_format`，默认 INT4），评估整体指标。定义层敏感度为该操作引入的精度损失：

$$\sigma_\ell \;=\; \max\!\Bigl(0,\;\;\mathcal{L}\bigl(\mathbf{f}^{(\ell\to\text{low})}\bigr)-\mathcal{L}\bigl(\mathbf{f}^{(\text{base})}\bigr)\Bigr)$$

其中 $\mathcal{L}$ 为精度损失（业务指标或 teacher-student MSE 相对 FP baseline 的退化）。$\sigma_\ell$ 越大，该层对低位宽越敏感。

### 4.2 层策略剪枝（layer policy）

依敏感度将层分为三级，并据此限定每层的允许格式集 $F_\ell$：

| 级别 | 判据（$\tau$ 为阈值） | 允许格式集 $F_\ell$ |
|---|---|---|
| 高敏感（high） | $\sigma_\ell > \tau$ | 仅安全高位宽格式（如 `{INT8, MX8}`） |
| 一般敏感（normal） | $\varepsilon < \sigma_\ell \le \tau$ | 完整候选集 $\mathcal{F}$ |
| 低敏感（low） | $\sigma_\ell \le \varepsilon$ | 允许含低位宽（如含 `INT4/MX4`） |

阈值 $\tau$ 默认按中位数比例确定（$\tau = \mathrm{median}(\sigma)\cdot r$），并设 `min_high_sensitive_layers` 下限以保证至少若干层被划为高敏感。剪枝后的有效搜索空间为

$$\mathcal{X}_{\text{policy}} \;=\; F_1\times F_2\times\cdots\times F_L \;\subseteq\; \mathcal{F}^{L}$$

其规模 $\prod_{\ell}|F_\ell|$ 远小于 $|\mathcal{F}|^{L}$，使启发式搜索在预算内可行。

### 4.3 主搜索（DOE + guided descent + mutation）

在 $\mathcal{X}_{\text{policy}}$ 上执行受限的黑盒搜索：

1. **初始采样（DOE）**：以试验设计在策略空间内生成若干种子分配，覆盖格式组合的多样性；
2. **引导下降（guided descent）**：基于当前最优候选，沿敏感度引导的层序向更低位宽方向局部搜索；
3. **变异（mutation）**：对档案中的候选逐层随机替换格式（受 $F_\ell$ 约束），探索邻域。

每个评估过的候选经真实模型评估后写入档案，并由支配关系抽取出 Pareto 前沿（§2）。搜索预算由 `max_evals` 控制（32 为冒烟级，128 适用于真实模型）。

### 4.4 位宽成本与 bit_objective

候选的平均位宽依 `bit_objective` 口径计算：

- **weight_activation_proxy**（默认）：$B(\mathbf{f})=\frac{1}{L}\sum_\ell \frac{b_w(f_\ell)+b_a(f_\ell)}{2}$，权重与激活位宽各半；
- **weight_only**：$B(\mathbf{f})=\frac{1}{L}\sum_\ell b_w(f_\ell)$，仅计权重大小（适用于 weight-only 部署）。

`weight_activation_proxy` 的必要性在于：`W4A8`（权重 4、激活 8）与 `INT4`（权重 4、激活 4）的权重位宽相同，但综合成本不同；只有将激活位宽计入，方能在横轴上正确区分二者。

### 4.5 三种选择模式（`mode`）

前沿得出后，按 `mode` 决定选点方式：

- **explore**：返回完整 Pareto 前沿，供用户观察折中空间后手动选点；
- **constrained_select**：在 `avg_bit_budget` 与 `accuracy_tolerance` 双约束下选点；
- **minimize_bit_under_accuracy**：在满足 `accuracy_tolerance` 的候选中选平均位宽最低者，直接回答「精度只掉 $\varepsilon$ 的前提下能压到多低」。

---

## 5. 实验

### 5.1 实验设置

| 项 | 取值 |
|---|---|
| 模型 | ViT-Tiny |
| 模式 | `explore`，`max_evals=8`（smoke） |
| 格式空间 | `INT8/W4A8/INT4/MX4/MX8` |
| 评估 | teacher-student MSE |

### 5.2 结果

模型含 50 个可量化层，8 次评估后选中候选 `cand_0002`，分配为 `[INT8×26 + INT4×24]`，平均位宽 `selection_bit_score≈5.35`，烘焙产物 `best_mixed_model.pt`（约 21 MB）。输出摘要示例：

```json
{
  "best_config": "cand_0002 [INT8×26+INT4×24]",
  "best_metric": 0.0,
  "best_bit": 5.35,
  "candidates_evaluated": 8,
  "mode": "explore",
  "metric_kind": "mse",
  "baked_model_path": "llm_artifacts/vit_tiny_cifar100/best_mixed_model.pt"
}
```

### 5.3 分析

选中候选保留 26 层 INT8、压缩 24 层至 INT4，平均位宽 5.35，相对全 INT8（8 比特）降低约 33%，而量化误差几乎为零（MSE≈0）。这验证了混合精度的核心收益：通过将低敏感层（W1/探测识别）降至 INT4、敏感层保留 INT8，在精度无损的前提下显著降低平均位宽。该结果亦印证了层策略剪枝的有效性——搜索器未在敏感层上尝试 INT4，避免了精度崩塌候选的浪费评估。

### 5.4 计划截图

- **line 图**「位宽-精度 Pareto 前沿」（主图）：横轴=平均位宽 `selection_bit_score`，纵轴=MSE，前沿点连线，选中点高亮标注。
- **bar 图**「选中候选的格式分布」：横轴=格式，纵轴=层数，直观展示混合比例。
- **table**「前沿候选明细」：`candidate_id` | bit | MSE | `accuracy_loss` | 格式分布。

> 标注「📊 计划截图」处为预设图位。原理示意以 ASCII 内联；真实数据图待运行一次 workflow 后由 Web 面板（`orca open`）截取真实图替换。

---

## 6. 局限与延伸

`m0_pareto` 的敏感度探测采用单层扰动近似，未建模层间交互的耦合误差；探测格式固定为单一低位宽（默认 INT4），对某些层可能低估其在其他低位格式下的真实敏感度。主搜索为启发式，前沿的完整性受 `max_evals` 约束，极小预算下可能遗漏拐点。当选中候选的平均位宽已很低、但仍需进一步恢复精度时，应转入 W4（QAT + CAGE）以梯度优化修正低位宽量化噪声。

---

## 附录 A：ts_quant 库接口手册

本 workflow 的混合精度搜索由 `ts_quant.search_mix_precision`（策略 `m0_pareto`）提供。

### A.1 混精搜索：`search_mix_precision`

```python
from ts_quant import (
    search_mix_precision, MixPrecisionSearchConfig, MetricSpec,
    QConfig, TSQuantizer,
)
from ts_quant.eval import build_teacher_student_eval_fn

# 1. 准备层图
base_qconfig = QConfig(method="int", n_bits=8, granularity="per_tensor")
quantizer = TSQuantizer(fp_model, base_qconfig)
quantizer.prepare()
q_layers = quantizer.q_layers

# 2. 候选格式空间（QConfig 列表）
candidate_format_space = [
    QConfig(method="int", n_bits=8),    # INT8
    QConfig(method="int", n_bits=4),    # INT4
    QConfig(method="mx",  n_bits=4),    # MX4
    QConfig(method="mx",  n_bits=8),    # MX8
]

# 3. 搜索配置
search_config = MixPrecisionSearchConfig(
    strategy="m0_pareto",
    mode="explore",                         # explore / constrained_select / minimize_bit_under_accuracy
    base_qconfig=base_qconfig,
    candidate_format_space=candidate_format_space,
    bit_objective="weight_activation_proxy",# 或 weight_only
    accuracy_tolerance={"mode": "absolute", "value": 0.01},
    avg_bit_budget=None,                    # 硬位宽上限；None=无约束
    max_evals=128,
    output_dir="./search_artifacts",
)

# 4. 执行搜索
metric_spec = MetricSpec(primary_metric="mse", higher_is_better=False)
eval_fn = build_teacher_student_eval_fn(fp_model, eval_loader, forward_fn)
best_configs, report = search_mix_precision(
    fp_model, q_layers=q_layers, eval_fn=eval_fn,
    metric_spec=metric_spec, search_config=search_config, return_report=True,
)
```

返回 `(best_configs, report)`：`best_configs` 为选中候选的逐层 QConfig 映射；`report` 含 `frontier.points`（前沿）、`final`（选中候选明细，含 `layer_configs`/`selection_bit_score`/`format_counts`）、`eval_calls`（实际评估次数）。

### A.2 `MixPrecisionSearchConfig` 主要字段

| 字段 | 说明 |
|---|---|
| `strategy` | `"m0_pareto"`（纯格式 Pareto 搜索） |
| `mode` | 选点模式（见 §4.5） |
| `base_qconfig` | 基准格式（敏感度探测参考，默认 INT8） |
| `candidate_format_space` | 候选格式 `QConfig` 列表 |
| `bit_objective` | 位宽成本口径 |
| `accuracy_tolerance` | `{"mode": "absolute", "value": ε}` |
| `avg_bit_budget` | 硬平均位宽上限 |
| `max_evals` | 主搜索评估预算 |
| `sensitivity` / `guided_descent` / `guided_mutation` | 各阶段的可选调参字典（探测格式、阈值模式、变异强度等） |

### A.3 烘焙混合精度模型

`report["final"]["layer_configs"]` 为逐层 QConfig 的 dict 序列化形式，可直接喂回 `quantize_model` 生成可部署模型：

```python
qconfig_dict = {name: QConfig.from_dict(cfg) for name, cfg in layer_configs.items()}
q_model = quantize_model(
    model=copy.deepcopy(fp_model), qconfig=base_qconfig,
    qconfig_dict=qconfig_dict, calib_data=calib_loader, forward_fn=forward_fn,
)
torch.save(q_model.state_dict(), "best_mixed_model.pt")
```

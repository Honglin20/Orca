# quant-sensitivity：低精度敏感层分析（W1）

> Orca 量化流水线第 1 级。给定浮点模型与少量校准数据，量化误差在深层网络中逐层累积，而通常只有少数层对其具有显著放大作用。本 workflow 对每一层打分并排序，识别出对低比特量化敏感的层，为下游 PTQ、混合精度搜索与 QAT 的位宽分配提供先验。

---

## 1. 实现概览

### 1.1 这个 workflow 做什么

`quant-sensitivity` 提供四种可插拔的敏感度分析方法，对模型中每个可量化层计算一个标量敏感度分数 $s_\ell$，按降序排列并依比例 `ratio` 选出 top-k 作为敏感层。其底层调用 `ts_quant.trainable.analyze_low_precision_sensitive_layers`，输出敏感层排名、完整打分明细与可视化，供下游 workflow 决策位宽分配。

### 1.2 架构与流程

该 workflow 为单 agent 节点编排：`sensitivity_analyzer` 读取用户模型生成适配层 `adapter.py`，随后调用确定性脚本 `run_sensitivity.py` 完成「按 method 组装参数 → 敏感度分析 → 落盘 report → 可视化 → 摘要回显」。

```
                ┌─────────────────────────────────────────────────────┐
                │               sensitivity_analyzer (单 agent)        │
                └─────────────────────────────────────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────────────┐
        ▼                          ▼                                  ▼
  ① 读模型 model.py          ② 生成 adapter.py                ③ 调 run_sensitivity.py
  收集可量化候选层             load_model / get_calib_loader     (确定性脚本)
                             forward_fn / get_eval_fn(可选)
                                                                   │
          ┌────────────────────────────────────────────────────────┘
          ▼
   ┌──────────────────── run_sensitivity.py 五步 ────────────────────┐
   │                                                                  │
   │  1. import adapter → FP 模型 + calib_loader (+ eval_fn)          │
   │                                                                  │
   │  2. low_bits / high_bits 预设 → QConfig                           │
   │                                                                  │
   │  3. 按 method 组装参数调                                          │
   │     analyze_low_precision_sensitive_layers：                      │
   │       mse / layer_stats        → calib_data + forward_fn          │
   │       ptq_binary_sensitivity /  → eval_fn + high_precision_qconfig│
   │       mix_precision_search                                        │
   │                                                                  │
   │  4. report.json：ranked_layers（全层打分）+ auto_sensitive_layers │
   │     + 模型原始层序 module_order；render_chart 推 bar+table（容错）│
   │                                                                  │
   │  5. stdout JSON 摘要（agent 原样回显，对齐 output_schema）         │
   └──────────────────────────────────────────────────────────────────┘
```

### 1.3 输入 / 输出

**输入**：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `model_path` / `project_root` | string | — | 模型入口 / 项目根 |
| `calib_data_ref` | dotted-path | `""` | 校准 loader（mse/layer_stats 用；空→假随机） |
| `method` | enum | `mse` | `mse` / `layer_stats` / `ptq_binary_sensitivity` / `mix_precision_search` |
| `ratio` | float-str | `0.1` | 敏感层选取比例（top 10%） |
| `low_bits` | enum | `w4a4-mx` | 低精度预设（`w4a4-mx` / `int4` / `w4a16`） |
| `high_bits` | enum | `w8a8` | 高精度对照（binary/mix 方法用） |
| `eval_fn_ref` | dotted-path | `""` | 业务评估函数（binary/mix 方法必填） |
| `output_dir` | path | 推断 | 留空→`llm_artifacts/<model_name>/` |

**输出**：`sensitive_layers`（top-k 层名列表）+ `report.json`（全层 `ranked_layers` 打分明细）+ 可视化（bar 每层敏感度按模型顺序、table 入选层明细）。节点 stdout 的 JSON 摘要即 workflow 产出，字段含 `sensitive_layers`、`selected_count`、`method`。

### 1.4 如何激活

In-session 激活的本质是命中 TARS skill 与本 workflow 的 `description`。向主会话陈述意图即可：

```
用 TARS 分析这个模型的量化敏感层
TARS，看看 vit_tiny 哪些层不能压到 4 比特
```

匹配命中的关键词：**敏感层 / 低精度 / sensitive / 哪些层怕量化**。等价手动命令：

```bash
orca quant-sensitivity --inputs '{
  "model_path": "demo_target/vit_tiny_cifar100/model.py",
  "project_root": "demo_target/vit_tiny_cifar100",
  "method": "mse", "ratio": "0.1", "low_bits": "w4a4-mx"
}'
```

---

## 2. 定义

**层敏感度（layer sensitivity）** 量化地将第 $\ell$ 层对低比特量化的脆弱程度表示为一个标量 $s_\ell$。形式化地，设模型 $f$ 由可量化层序列 $\{\ell_1,\dots,\ell_L\}$ 构成，$Q_b(\cdot)$ 为位宽 $b$ 的量化算子，定义第 $\ell$ 层在位宽 $b$ 下的敏感度为该层单独低精度化所引入的输出失真：

$$s_\ell(b) \;=\; \mathcal{D}\!\left(\, f(x;\,W),\;\; f\!\left(x;\, W,\, W_\ell\!\to\!Q_b(W_\ell)\right)\,\right)$$

其中 $\mathcal{D}$ 为输出距离度量（MSE 或业务指标），$W_\ell\!\to\!Q_b(W_\ell)$ 表示仅将第 $\ell$ 层权重替换为其低比特量化值。$s_\ell$ 越大，表明该层量化对整体输出的扰动越剧烈，即越敏感。敏感层集合定义为排名前 `ratio` 比例的层：

$$\mathcal{S}_{\text{sens}} \;=\; \operatorname{TopK}\bigl(\{s_\ell\},\;\lceil \mathrm{ratio}\cdot L\rceil\bigr)$$

本 workflow 提供 four 种对 $s_\ell$ 的估计方法，按所需评估能力递进。

---

## 3. 背景

### 3.1 量化误差的逐层累积

全模型统一量化到低位宽往往导致精度崩塌，但其根因通常并非所有层共同劣化，而是少数「放大层」主导了整体误差。这些层具有共同特征：激活存在显著离群通道、权重分布重尾、或处于信息流的关键位置（如首层投影、分类头）。敏感层分析的目的即在量化前定位这些放大层，使其在下游保持高精度，而将其余层放心压缩——以最小的精度代价换取最大的压缩收益。

### 3.2 方法谱系与评估能力取舍

敏感度的「真值」需要量化后实测任务指标，但逐层、逐位宽的完整评测代价高昂。因此现有方法沿「评估能力」轴形成谱系：从无需前向的纯统计启发式，到需要业务指标的全搜索。本 workflow 将该谱系统一为四种可插拔方法，用户按可提供的评估能力选取。

### 3.3 与下游的衔接

敏感层集合 $\mathcal{S}_{\text{sens}}$ 是混合精度搜索（W3）与 PTQ（W2）的关键先验：W3 的 `m0_pareto` 搜索器据此将高敏感层限制在安全格式集、低敏感层方允许低位宽（见 W3 §layer_policy）；W2 则可在敏感层上保留更高位宽或更强算法。

---

## 4. 方法

### 4.1 方法一：mse（单层低精度扰动）

最轻量且最常用的方法。对每个候选层 $\ell$，在 deepcopy 的分析模型上**仅将第 $\ell$ 层**临时替换为 RTN 低比特伪量化模块，前向传播校准数据，比较量化前后该模型输出与 FP teacher 输出的均方误差：

$$s_\ell^{\text{mse}} \;=\; \frac{1}{N}\sum_{n=1}^{N}\bigl\|\, f(x_n) - f^{(\ell\to q)}(x_n)\,\bigr\|_2^{\,2}$$

其中 $f^{(\ell\to q)}$ 表示第 $\ell$ 层被 $Q_b$ 量化后的模型。此为「留一层低精度（leave-one-layer-lowprecision）」协议：每次仅扰动一层，故 $s_\ell^{\text{mse}}$ 反映该层单独量化对输出的边际影响。该方法仅需校准数据、无需业务指标，复杂度为 $O(L)$ 次前向。

### 4.2 方法二：layer_stats（分布压力启发式）

不执行任何量化前向，仅依据权重与激活的统计分布评估量化难度。对每层权重 $W$ 与激活 $X$，先计算分布统计量：动态范围比 $\mathrm{mr}(t)=|t|_{\max}/\overline{|t|}$、均方根范围比 $\mathrm{mr}_{\text{rms}}(t)=|t|_{\max}/\mathrm{rms}(t)$、峰度 $\kappa(t)=\mathbb{E}[(t-\mu)^4]/\sigma^4$（衡量重尾）、离群比例 $\mathrm{or}(t)=\Pr(|t|>\overline{|t|}+3\sigma)$，以及 MX 块内的范围比 $\mathrm{mr}_{\text{blk}}$（按 `block_size=16` 分块）。

敏感度分数为权重项与激活项的加权和，再乘以位宽压力因子：

$$s_\ell^{\text{stats}} \;=\; \underbrace{\Bigl(\,\phi_w + 0.75\,\phi_a\,\Bigr)}_{\text{分布压力}}\;\cdot\;\underbrace{\Bigl(\tfrac{8}{b_w}+\tfrac{8}{b_a}\Bigr)/2\cdot\max(q_w\!+\!q_a,\,1)}_{\text{位宽压力}}$$

其中权重压力 $\phi_w=\log(1\!+\!\mathrm{mr}_w)+\log(1\!+\!\mathrm{mr}_{\text{rms},w})+0.25\log(1\!+\!\kappa_w)+10\,\mathrm{or}_w+0.5\log(1\!+\!\mathrm{mr}_{\text{blk},w})$，激活压力 $\phi_a$ 同构（不含块项），$b_w/b_a$ 为权重/激活有效位宽，$q_w/q_a\in\{0,1\}$ 指示是否启用权重/激活量化。对数变换 $\log(1\!+\!\cdot)$ 抑制极端值的支配效应。该方法无需量化前向、仅需一遍校准数据收集激活统计，适合超大模型的快速初筛。

### 4.3 方法三：ptq_binary_sensitivity（高精度回退对比）

在「全模型低精度」与「全模型高精度」二档之间，逐层评估「将第 $\ell$ 层救回高精度」对整体业务指标的改善：

$$s_\ell^{\text{bin}} \;=\; \mathcal{M}\!\left(f^{(\ell\to h,\,\text{rest}\to q)}\right) \;-\; \mathcal{M}\!\left(f^{(\text{all}\to q)}\right)$$

其中 $\mathcal{M}$ 为业务评估函数（`eval_fn`，如准确率，`higher_is_better=True`），$h$ 为高精度配置（`high_bits`，默认 `w8a8`），$q$ 为目标低精度（`low_bits`）。$s_\ell^{\text{bin}}>0$ 表明该层回退高精度可提升指标，正值越大者越敏感、越必须保留高精度。该方法直接回答「哪些层必须高精度」，但需业务评估能力。

### 4.4 方法四：mix_precision_search（完整混精搜索）

复用 W3 的 `m0_pareto` 混合精度搜索器，考虑层间交互效应，直接产出可部署的逐层格式分配，并从中导出敏感度排序。这是四种方法中代价最高但最精确者，等价于先运行一次完整混精搜索再读出每层被分配的位宽作为敏感度信号。需业务评估能力与 `high_precision_qconfig`。

### 4.5 选取协议

四种方法均输出全层打分序列 `ranked_layers`，按分数降序排列后，依比例 `ratio` 取前 $\lceil\mathrm{ratio}\cdot L\rceil$ 层作为 `auto_sensitive_layers`。`ratio=0.1` 即取 top 10%。所有方法在 deepcopy 的分析模型上执行，原始 FP 模型不被修改。

### 4.6 数据格式

低精度预设以 **MX 族**为基础（`w4a4-mx` = MX `fp4_e2m1`，`block_size=16`）；`int4` 为纯整数 4 比特变体；`w4a16` 为 weight-only（权重 INT4、激活 FP16）。`layer_stats` 的块统计自动适配 QConfig 的 `block_size`。

---

## 5. 实验

### 5.1 实验设置

| 项 | 取值 |
|---|---|
| 模型 | ViT-Tiny（5.5M 参数） |
| 数据 | CIFAR-100（少量校准样本） |
| 方法 | `mse` |
| 选取 | `ratio=0.1`，`low_bits=w4a4-mx` |

### 5.2 结果

模型含 50 个可量化 Linear 层，`mse` 方法选出 5 个敏感层（top 10%）。敏感层集中分布于 transformer 首层投影（如 `to_qkv`）与最后的 MLP head，与前述「关键位置放大层」的先验一致。输出摘要示例：

```json
{
  "output_dir": "llm_artifacts/vit_tiny_cifar100/",
  "report_path": "llm_artifacts/vit_tiny_cifar100/report.json",
  "sensitive_layers": ["to_patch_embedding.2", "transformer.layers.0.0.to_qkv", "..."],
  "selected_count": 5,
  "method": "mse"
}
```

### 5.3 分析

`mse` 方法在 ViT-Tiny 上以 $O(L)$ 次前向的代价完成全层打分，识别出的敏感层与模型结构语义吻合：靠近输入嵌入的投影层与靠近输出的分类层对量化噪声最敏感，中部 transformer 块则相对鲁棒。该结果可直接指导下游：将 5 个敏感层保留 `w8a8`、其余压缩至 `w4a4-mx`，作为 W2（PTQ）或 W3（混精搜索）的位宽初值。

### 5.4 计划截图

- **bar 图**「每层敏感度排名（按模型顺序）」：横轴=层名（模型原始顺序），纵轴=敏感度分数，top-k 入选层高亮，其余置灰。
- **table**「敏感层明细」：层名 | 分数 | 排名 | 是否入选。

> 标注「📊 计划截图」处为预设图位。原理示意以 ASCII 内联；真实数据图（bar/table）留占位，待运行一次 workflow 后由 Web 面板（`orca open`）截取真实图替换。

---

## 6. 局限与延伸

`mse` 与 `layer_stats` 属单层扰动或纯统计估计，未建模层间交互——两枚各自不敏感的层联合量化可能因误差耦合而显著劣化。对此应使用 `ptq_binary_sensitivity`（成对回退，仍近似）或 `mix_precision_search`（完整交互，代价最高）。当目标位宽极低（<4 bit）且敏感层已识别仍无法满足精度时，应转入 W4（QAT）以梯度优化恢复精度。

---

## 附录 A：ts_quant 库接口手册

本 workflow 的敏感度分析由 `ts_quant.trainable.analyze_low_precision_sensitive_layers` 提供。下列接口供用户在 workflow 之外独立调用。

### A.1 敏感度分析：`analyze_low_precision_sensitive_layers`

```python
from ts_quant.trainable import analyze_low_precision_sensitive_layers
from ts_quant import QConfig

low_qconfig = QConfig(method="mx", w_elem_format="fp4_e2m1",
                     a_elem_format="fp4_e2m1", block_size=16,
                     weight_solver="rtn", post_correction="none")

analysis = analyze_low_precision_sensitive_layers(
    model=fp_model,
    low_qconfig=low_qconfig,
    ratio=0.1,                      # top 10%
    method="mse",                   # mse / layer_stats / ptq_binary_sensitivity / mix_precision_search
    # 轻量方法（mse / layer_stats）：
    calib_data=calib_loader,
    forward_fn=forward_fn,
    # 复杂方法（ptq_binary_sensitivity / mix_precision_search）改传：
    # eval_fn=business_eval_fn,
    # high_precision_qconfig=QConfig(method="int", n_bits=8),
    module_types=("Linear", "Conv2d"),  # 可选：覆盖默认分析的模块类型
)
```

返回 `SensitiveLayerAnalysisResult`，主要属性：

| 属性 | 说明 |
|---|---|
| `ranked_layers` | 全层打分明细列表，每条含 `name`、`score`、`primary_metric`、`backend` 等 |
| `auto_sensitive_layers` | 按 `ratio` 选出的敏感层层名列表 |
| `selected_count` | 入选敏感层数 |
| `num_candidate_layers` | 参与分析的可量化层总数 |
| `method` / `metric_spec` | 使用的方法与指标规格 |

### A.2 量化配置：`QConfig`

敏感度分析用 `low_qconfig` 定义「低精度」语义，字段与约束同 W2 附录 A.2。常用预设：`w4a4-mx`（MX fp4）、`int4`（INT4）、`w4a16`（weight-only，`a_quant_enabled=False`）、`w8a8`（高精度对照）。

### A.3 方法选择指引

| 方法 | 所需输入 | 前向次数 | 适用场景 |
|---|---|---|---|
| `mse` | 校准数据 + `forward_fn` | $O(L)$ | 默认首选，无业务指标 |
| `layer_stats` | 校准数据（仅收集统计） | 1 | 超大模型快速初筛 |
| `ptq_binary_sensitivity` | `eval_fn` + `high_precision_qconfig` | $O(L)$ | 需直接回答「必保高精度层」 |
| `mix_precision_search` | `eval_fn` + `high_precision_qconfig` | 搜索预算 | 需考虑层间交互，直接出可部署方案 |

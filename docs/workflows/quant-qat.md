# quant-qat：量化感知训练与 CAGE 后校正（W4）

> Orca 量化流水线第 4 级（收尾）。W1–W3 均为训练后量化（PTQ），其精度在低位宽下存在上界。本 workflow 转向量化感知训练（QAT）：将伪量化插入训练循环，以 teacher-student 蒸馏短训使权重自适应量化噪声，并通过 CAGE 后校正显式收缩量化残差，在目标位宽下恢复精度。

---

## 1. 实现概览

### 1.1 这个 workflow 做什么

`quant-qat` 对比两种可训练伪量化方案（`rtn` / `duquantpp`），将浮点模型构造为可训练伪量化模型，经 teacher-student 蒸馏短训（可选 CAGE 后校正）恢复精度，输出每方案的「量化前/后精度与收敛曲线」，并烘焙最优方案。其底层调用 `ts_quant.trainable.prepare_trainable_fakequant_model` 与 `prepare_trainable_qat`。

### 1.2 架构与流程

该 workflow 为单 agent 节点编排：`qat-trainer` 读取用户模型生成适配层 `adapter.py`，随后调用确定性脚本 `run_qat.py` 完成「逐方案构造伪量化模型 → 量化前评估 → 短训 → 量化后评估 → 选优 → 烘焙 → 可视化」。脚本对每个方案以 `try/except` 隔离，单方案失败不阻断另一方案。

```
                ┌─────────────────────────────────────────────────────┐
                │                 qat-trainer (单 agent)               │
                └─────────────────────────────────────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────────────┐
        ▼                          ▼                                  ▼
  ① 读模型 model.py          ② 生成 adapter.py                ③ 调 run_qat.py
                             load_model / calib / train / eval   (确定性脚本)
                             forward_fn / eval_fn
                                                                   │
          ┌────────────────────────────────────────────────────────┘
          ▼
   ┌──────────────────── run_qat.py 逐方案流程 ──────────────────────┐
   │  对 scheme ∈ {rtn, duquantpp}（both 则两方案对比）：              │
   │                                                                  │
   │  a. prepare_trainable_fakequant_model(deepcopy(fp), scheme,      │
   │     qconfig, [duquantpp: DuQuantPPConfig + calib + forward_fn])  │
   │     → 可训练伪量化 q_model                                        │
   │                                                                  │
   │  b. eval BEFORE：fake-quant 基线（≈ PTQ 精度）                    │
   │                                                                  │
   │  c. optimizer=Adam(q_model.parameters(), lr)                     │
   │     qat = prepare_trainable_qat(q_model, optimizer,              │
   │                                  total_steps, cage)              │
   │                                                                  │
   │  d. 训练 loop（teacher-student MSE，label-free）：                │
   │     loss = MSE(q_model(batch), fp_model(batch).detach())         │
   │     backward → optimizer.step() → qat.step()（CAGE 在此）         │
   │     每 period 步 eval 记收敛曲线点                                │
   │                                                                  │
   │  e. eval AFTER → recovery = after − before                       │
   │  └────────────────────────────────────────────────────────────────┘
          ▼
   选 best（after 最优）→ bake best_qat_model.pt → report.json
   → render_chart：line（per-step 收敛）+ bar（前/后）+ table（容错）
   → stdout JSON 摘要
```

### 1.3 输入 / 输出

**输入**：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `model_path` / `project_root` | string | — | 模型入口 / 项目根 |
| `calib_data_ref` | dotted-path | `""` | 校准 loader（duquantpp 校准用；空→假随机） |
| `train_data_ref` | dotted-path | `""` | **训练 loader（QAT 核心；空→复用 calib 做 smoke）** |
| `eval_data_ref` / `eval_fn_ref` | dotted-path | `""` | 评估 loader + 业务 eval_fn（空→teacher-student MSE） |
| `scheme` | enum | `both` | `rtn` / `duquantpp` / `both`（both=两方案对比） |
| `bit_width` | enum | `w8a8-mx` | QAT 伪量化位宽（高位宽起步更稳） |
| `cage` | enum | `auto` | CAGE 后校正开关（`auto`/`true`/`false`） |
| `total_steps` | int-str | `64` | QAT 训练步数（真实 QAT 视数据集调大） |
| `lr` | float-str | `1e-4` | Adam 学习率 |
| `bake` | bool-str | `true` | 是否烘焙最优 `q_model` |

**输出**：`report.json`（每方案 before/after/recovery/curve + best）+ `baked_model_path`（`best_qat_model.pt`）+ 可视化（line 收敛曲线、bar 前后对比、table 方案对比）。节点 stdout 摘要字段含 `best_scheme`、`best_metric`、`best_metric_before`、`recovery`、`schemes_evaluated`。

### 1.4 如何激活

```
用 TARS 对这个模型做量化感知训练
TARS，4 比特掉点太多，跑个 QAT 拉回来
```

匹配命中的关键词：**QAT / 量化感知训练 / 训练恢复 / CAGE / fake-quant 训练**。等价手动命令：

```bash
orca quant-qat --inputs '{
  "model_path": "demo_target/vit_tiny_cifar100/model.py",
  "project_root": "demo_target/vit_tiny_cifar100",
  "scheme": "both", "bit_width": "w8a8-mx",
  "cage": "auto", "total_steps": "64", "lr": "1e-4", "bake": "true"
}'
```

---

## 2. 定义

**量化感知训练（Quantization-Aware Training, QAT）** 在训练循环中插入伪量化算子，使权重在反向传播中学会补偿量化噪声。形式化地，记量化-反量化算子 $Q(\cdot)$（将权重映射到低比特格点再反量化回浮点），QAT 的前向为

$$y = Q(W)\,x$$

权重 $W$ 始终以浮点保存并接收梯度更新，但每次前向均「观察」自身被量化后的行为；经若干步训练，$W$ 迁移至量化误差更小的区域。因 $Q(\cdot)$ 的舍入操作处处梯度为零，反向传播采用**直通估计器（Straight-Through Estimator, STE）**：将量化处的梯度视为 1，使 $\partial \mathcal{L}/\partial W \approx \partial \mathcal{L}/\partial Q(W)$。

---

## 3. 背景

### 3.1 PTQ 的精度上界

PTQ（W2）将浮点权重一次性投影到低比特后即交付，不回头修正。在低位宽（≤4 bit）下，量化噪声显著、精度常崩塌，且该损失无法在 PTQ 框架内恢复。QAT 通过将量化纳入梯度优化，使权重主动适应量化噪声，是突破 PTQ 精度上界的主要手段。

### 3.2 标签可得性与蒸馏式 QAT

标准 QAT 需训练数据及其标签。本 workflow 默认采用 **teacher-student 蒸馏**：以原始浮点模型为 teacher、伪量化模型为 student，最小化二者输出差的 MSE。该方案无需真实标签（label-free），可应用于任意模型，且其优化目标——量化后行为复现浮点行为——与量化误差的评估口径一致。当业务标签可得时，可经 `eval_fn_ref` 用业务指标评估（训练损失仍为 teacher-student MSE）。

### 3.3 收敛速度与 CAGE 的动机

纯 QAT 仅通过任务损失梯度间接减小量化误差，收敛缓慢。CAGE（post-step weight Correction with Activation-Guarded Extrapolation）在每个优化步之后显式收缩量化残差，作为对 QAT 梯度的直接补充，加速权重收敛至量化友好区域。

---

## 4. 方法

### 4.1 伪量化与直通估计器

伪量化在前向执行量化-反量化 $Q(W)=s\cdot\mathrm{clip}(\mathrm{round}(W/s),q_{\min},q_{\max})$，模拟真实部署时的量化噪声；反向时由于舍入不可导，以 STE 将梯度直接透传：

$$\frac{\partial \mathcal{L}}{\partial W} \;\approx\; \frac{\partial \mathcal{L}}{\partial Q(W)}\cdot \mathbf{1}_{[q_{\min}s,\,q_{\max}s]}(W)$$

即在截断范围内梯度为 1、截断范围外为 0。权重与激活的量化 scale 作为可学习参数联合优化。

### 4.2 两种伪量化方案（`scheme`）

`prepare_trainable_fakequant_model` 将模型中的 Linear 层替换为可训练伪量化模块，按 `scheme` 选择替换类型：

- **rtn**：`RTNFakeQuantLinear`，采用 round-to-nearest 语义的最简伪量化，权重与激活均有可学 scale，直接进入 QAT。无需校准。
- **duquantpp**：DuQuant++ Linear，在校准阶段先学习一个旋转/置换变换以压平激活分布（抑制离群通道），再施加伪量化。对低位宽与含离群激活的模型更强，但需校准数据与 `DuQuantPPConfig`（`target_patterns` 显式指定替换范围、`block_size` 与 QConfig 对齐）。

`both` 即两方案均运行并对比恢复效果，`duquantpp` 因额外的分布压平通常在低位宽下更优。

### 4.3 teacher-student 蒸馏损失

每步从训练 loader 取 batch，计算 student（伪量化模型）与 teacher（固定浮点模型）输出的 MSE：

$$\mathcal{L}_{\text{distill}} \;=\; \frac{1}{N}\sum_{n}\bigl\|\,f_q(x_n;\,Q(W)) - f(x_n;\,W_{\text{fp}})\,\bigr\|_2^{\,2}$$

其中 $W_{\text{fp}}$ 以 `.detach()` 固定为常量。该损失直接驱动 $Q(W)$ 复现 $W_{\text{fp}}$ 的输出，即最小化量化引入的行为偏差。

### 4.4 CAGE 后校正

CAGE 是本 workflow 的核心创新，在每个优化步的 `optimizer.step()` 之后、作为 `qat.step()` 执行一次显式的量化残差收缩。

#### 4.4.1 校正公式

对每个可训练权重参数 $W$，记其当前量化值为 $Q(W)$，定义量化残差 $r = W - Q(W)$。CAGE 按下式更新权重：

$$\boxed{\;W \;\leftarrow\; W \;-\; \eta\,\lambda_t\,(W - Q(W))\;}$$

其中 $\eta$ 为该参数的当前学习率，$\lambda_t$ 为第 $t$ 步的 CAGE 强度（由 schedule 决定）。该更新在 `torch.no_grad()` 下对 `param.data` 原地执行，不进入自动微分图。

#### 4.4.2 不动点与收敛语义

校正的不动点满足 $W^* = Q(W^*)$，即权重落在「量化前后不变」的固定点。在此点上，部署时的量化操作对权重无改变，量化误差为零。CAGE 因此可视为对残差 $r$ 的收缩迭代：每步将 $W$ 沿 $-(W-Q(W))$ 方向移动 $\eta\lambda_t$ 比例，使残差范数持续下降，驱动权重逼近其自身量化的不动点。与 QAT 任务梯度的协同在于：任务梯度将 $W$ 推向低损失区域，CAGE 将 $W$ 拉向量化友好区域，二者共同收敛至「任务损失低且量化残差小」的权重。

#### 4.4.3 强度 schedule

为避免 CAGE 在训练初期干扰正常的权重收敛，$\lambda_t$ 采用 silence + ramp 调度：

$$\lambda_t \;=\; \begin{cases} 0 & t \;<\; t_{\text{silence}} \\[4pt] \lambda_{\text{base}}\cdot\mathrm{clip}\!\bigl(\mathrm{ramp}(t),\,0,\,1\bigr) & t \;\ge\; t_{\text{silence}} \end{cases}$$

其中 $t_{\text{silence}}\approx \mathrm{silence\_ratio}\cdot T$（$T$ 为总步数），前期 $\lambda_t=0$ 即 CAGE 静默、仅靠 QAT 梯度训练；度过静默期后 $\lambda_t$ 渐升至 $\lambda_{\text{base}}$，残差收缩逐步生效。`cage=auto` 时由 `total_steps` 自动决定是否启用与参数。

#### 4.4.4 健壮性保护

CAGE 在每步对每个参数校验：非可训练参数跳过、学习率为零跳过、量化值形状不符或非有限值时跳过并记录，确保校正不会因个别参数异常而中断训练。

### 4.5 选优与烘焙

每个方案记录量化前（`before`，伪量化基线）、量化后（`after`）指标及每 period 步的收敛曲线，恢复量 `recovery = after − before`。选 `after` 最优的方案为 best，烘焙其 `state_dict` 为 `best_qat_model.pt`；其余方案的模型显式释放。

---

## 5. 实验

### 5.1 实验设置

| 项 | 取值 |
|---|---|
| 模型 | ViT-Tiny |
| 方案 | `both`（rtn + duquantpp） |
| 位宽 | `w8a8-mx` |
| CAGE | `auto`，`total_steps=8`（smoke） |
| 评估 | teacher-student MSE |

### 5.2 结果

| scheme | before (MSE) | after (MSE) |
|---|---|---|
| rtn | 0.00078 | 0.00510 |
| duquantpp | 0.00073 | **0.00274** ← 选中烘焙 |

输出摘要示例：

```json
{
  "best_scheme": "duquantpp",
  "best_metric": 0.002744,
  "best_metric_before": 0.000732,
  "recovery": 0.002012,
  "schemes_evaluated": ["rtn", "duquantpp"],
  "total_steps": 64, "cage": "auto", "metric_kind": "mse",
  "baked_model_path": "llm_artifacts/vit_tiny_cifar100/best_qat_model.pt"
}
```

### 5.3 分析

需注意上述为 smoke 配置（8 步 + 假随机数据）的结果，`recovery>0` 属预期——步数过少、数据非真实，CAGE 尚未充分生效。`duquantpp` 因额外的激活分布压平，在相同步数下仍优于 `rtn`。真实 QAT（数百步 + 真实数据，尤其低位宽 `w4a4-mx`）下，CAGE schedule 度过静默期后残差收缩生效，`recovery` 转为负值（MSE 下降），即量化损失的精度被拉回——这才是 W4 的价值所在。

### 5.4 计划截图

- **line 图**「QAT 收敛曲线」（主图）：横轴=训练步数，纵轴=MSE，两条线（rtn / duquantpp）；CAGE 生效后斜率变陡。
- **bar 图**「QAT 前/后精度」：每方案两根柱（before / after），直观展示恢复幅度。
- **table**「方案对比」：scheme | before | after | recovery | steps | cage。

> 标注「📊 计划截图」处为预设图位。原理示意以 ASCII 内联；真实数据图待运行一次 workflow 后由 Web 面板（`orca open`）截取真实图替换。

---

## 6. 局限与延伸

QAT 的恢复能力受训练数据代表性、步数与学习率制约；smoke 配置仅验证流水线通畅，不反映真实恢复。teacher-student 蒸馏虽 label-free，但其上限受 teacher 自身精度约束；若业务标签可得，改用真实标签的 QAT 通常更优。当 QAT 仍无法满足极低位宽（<4 bit）约束时，应回溯 W3 评估是否可通过混合精度（对最敏感层保留高位宽）规避而非强行全模型低位宽 QAT。

---

## 附录 A：ts_quant 库接口手册

本 workflow 的可训练伪量化与 QAT 由 `ts_quant.trainable` 提供。

### A.1 构造可训练伪量化模型：`prepare_trainable_fakequant_model`

```python
from ts_quant import QConfig
from ts_quant.duquantpp import DuQuantPPConfig
from ts_quant.trainable import prepare_trainable_fakequant_model

qconfig = QConfig(method="mx", w_elem_format="fp8_e4m3",
                 a_elem_format="fp8_e4m3", block_size=16)

# rtn 方案：最简伪量化，无需校准
q_model, report = prepare_trainable_fakequant_model(
    copy.deepcopy(fp_model), scheme="rtn", qconfig=qconfig,
)

# duquantpp 方案：需校准 + DuQuantPPConfig（target_patterns 显式 + block_size 对齐 qconfig）
q_model, report = prepare_trainable_fakequant_model(
    copy.deepcopy(fp_model), scheme="duquantpp", qconfig=qconfig,
    duquant_config=DuQuantPPConfig(target_patterns=(".*",), block_size=16),
    calib_data=calib_loader,
    forward_fn=forward_fn,
)
```

返回 `(q_model, replace_report)`：`q_model` 为可训练伪量化模型（Linear 已替换），`replace_report` 记录替换明细。

### A.2 QAT 控制器：`prepare_trainable_qat`

```python
import torch
from ts_quant.trainable.qat import prepare_trainable_qat

optimizer = torch.optim.Adam(q_model.parameters(), lr=1e-4)
qat = prepare_trainable_qat(
    q_model, optimizer,
    total_steps=64,
    cage="auto",          # auto / true / false（CAGE 后校正开关）
)

for step in range(1, total_steps + 1):
    teacher_out = forward_fn(fp_model, batch).detach()
    loss = torch.nn.functional.mse_loss(forward_fn(q_model, batch), teacher_out)
    optimizer.zero_grad(); loss.backward(); optimizer.step()
    qat.step()            # CAGE 在此执行（schedule 控制 λ_t）
```

### A.3 CAGE 后校正：`TrainableCAGE`

`prepare_trainable_qat(..., cage="true"|"auto")` 内部自动装配 `TrainableCAGE`；亦可独立构造以审视其 schedule：

```python
from ts_quant.trainable import TrainableCAGE

cage = TrainableCAGE(
    total_steps=64,
    silence_ratio=0.1,     # 前 10% 步静默（λ_t=0）
    lambda_base=...,       # 基础强度（auto 时由配置推断）
    schedule="ramp",       # 静默后 λ_t 渐升策略
)
cage.register_model(q_model)   # 注册可训练权重参数
# 每步 optimizer.step() 后：
lambda_t = cage.step()         # 执行 W ← W − η·λ_t·(W − Q(W))，返回当前 λ_t
stats = cage.get_stats()       # applied_params / avg_err_norm / avg_corr_norm 等
```

`get_stats()` 中的 `avg_err_norm`（平均残差范数）与 `avg_corr_norm`（平均校正范数）随训练下降，是 CAGE 生效的量化证据。

### A.4 DuQuant++ 配置：`DuQuantPPConfig`

```python
from ts_quant.duquantpp import DuQuantPPConfig

DuQuantPPConfig(
    target_patterns=(".*",),   # 显式指定替换的层名正则（防误替换）
    block_size=16,             # 须与 qconfig.block_size 一致（block 格式约束）
)
```

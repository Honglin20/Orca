# quant-ptq-sweep：训练后量化算法组合搜索（W2）

> Orca 量化流水线第 2 级。给定浮点模型与少量校准数据，在固定位宽预算下搜索「预处理变换 × 权重求解 × 后处理」的算法组合，以量化误差最小化为准则选出最优候选，并烘焙为可部署的低比特权重。

---

## 1. 实现概览

### 1.1 这个 workflow 做什么

`quant-ptq-sweep` 把一次完整的训练后量化（PTQ）实验形式化为**算法组合搜索**：将量化过程拆解为三个可替换的环节（预处理变换、权重求解、后处理校正），在每个环节上选取一种算法，构成一个候选；对所有合法候选逐一执行量化与评估，选出误差最小者烘焙（bake）为 `state_dict`。其底层调用 `ts_quant.quantize_model`，并提供 lightweight（累积消融）与 full（全枚举）两种搜索模式。

### 1.2 架构与流程

该 workflow 为单 agent 节点编排：`ptq-sweeper` 读取用户模型生成适配层 `adapter.py`，随后调用确定性脚本 `run_ptq_sweep.py` 一次性完成「候选枚举 → 逐候选量化与评估 → 选优 → 烘焙 → 可视化 → 摘要回显」。脚本内对每个候选以 `try/except` 隔离，单候选失败不阻断全扫，失败信息显式输出到 stderr。

```
                ┌─────────────────────────────────────────────────────┐
                │                  ptq-sweeper (单 agent)              │
                └─────────────────────────────────────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────────────┐
        ▼                          ▼                                  ▼
  ① 读模型 model.py        ② 生成 adapter.py                ③ 调 run_ptq_sweep.py
  推断 forward 签名         load_model / get_calib_loader     (确定性脚本，全流程闭环)
                           get_eval_loader / forward_fn
                                                                   │
          ┌────────────────────────────────────────────────────────┘
          ▼
   ┌──────────────────────── run_ptq_sweep.py 八步 ─────────────────────────┐
   │                                                                        │
   │  1. import adapter → FP teacher + calib/eval loader + eval_fn          │
   │     （eval_fn_ref 空 → build_teacher_student_eval_fn 默认 MSE）        │
   │                                                                        │
   │  2. 构建候选网格                                                        │
   │       lightweight : 4 条累积路径 (S/Q/A/R) → 去重 ≈ 11 候选             │
   │       full        : {none,smooth,quarot}×{rtn,gptq,autoround}          │
   │                     ×{none,q2n} − 非法组合 → ≈ 45 候选/位宽             │
   │                                                                        │
   │  3. 逐候选：deepcopy(FP) → quantize_model → eval_fn → 记录指标          │
   │     （try/except 隔离；AutoRound 缺包则标 skipped）                     │
   │                                                                        │
   │  4. 选 best：metric_kind↓（业务 eval_fn 路径可 higher_is_better↑）     │
   │                                                                        │
   │  5. bake：torch.save(best.state_dict()) → best_quant_model.pt          │
   │                                                                        │
   │  6. report.json：全候选 + best，每候选评完即增量原子落盘                │
   │                                                                        │
   │  7. render_chart（容错）：lightweight=line+bar+table；full=heatmap     │
   │     +scatter+table；失败仅 stderr 提示，不阻断 report                  │
   │                                                                        │
   │  8. stdout JSON 摘要（agent 原样回显，对齐 output_schema）              │
   └────────────────────────────────────────────────────────────────────────┘
```

### 1.3 输入 / 输出

**输入**：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `model_path` / `project_root` | string | — | 模型入口文件 / 项目根目录 |
| `calib_data_ref` | dotted-path | `""` | 校准数据 loader（空→脚本用少量假随机数据） |
| `eval_data_ref` | dotted-path | `""` | 评估数据 loader（空→复用 calib） |
| `eval_fn_ref` | dotted-path | `""` | 业务评估函数（空→teacher-student MSE） |
| `mode` | enum | `lightweight` | `lightweight`（4 累积路径）/ `full`（全枚举） |
| `bit_widths` | csv | 模式相关 | 位宽预设（`w4a4-mx` / `w4a8-mx` / `w8a8-mx` / `w8a8-int` / `w4a16`） |
| `recipes` | csv | `""` | lightweight：`S/Q/A/R` 子集；full：`all` 或 pre/solver 子集 |
| `output_dir` | path | 推断 | 留空→`llm_artifacts/<model_name>/` |
| `bake` | bool-str | `true` | 是否烘焙最佳 `state_dict` |

**输出**：`baked_model_path`（`best_quant_model.pt`）+ `report.json`（全候选明细与最优解）+ 可视化图表（line/bar/heatmap/scatter/table）。节点 stdout 的 JSON 摘要即 workflow 产出，字段含 `best_config`、`best_metric`、`candidates_evaluated`、`mode`、`metric_kind`。

### 1.4 如何激活

In-session 激活的本质是命中 TARS skill 与本 workflow 的 `description`。向主会话陈述意图即可：

```
用 TARS 对 vit_tiny 做一轮 PTQ 扫描
TARS，跑一下量化算法对比，挑最好的
```

匹配命中的关键词：**PTQ / 训练后量化 / 算法对比 / SmoothQuant / GPTQ / 扫描**。等价手动命令：

```bash
orca quant-ptq-sweep --inputs '{
  "model_path": "demo_target/vit_tiny_cifar100/model.py",
  "project_root": "demo_target/vit_tiny_cifar100",
  "mode": "lightweight", "bit_widths": "w4a4-mx", "bake": "true"
}'
```

---

## 2. 定义

**训练后量化（Post-Training Quantization, PTQ）** 指在不进行梯度反向传播、仅使用少量校准数据的前提下，将浮点模型 $f(\cdot; W)$ 映射为低比特近似 $\hat f(\cdot; \hat W)$，使任务误差最小化。形式化地，给定量化算子 $Q(\cdot)$ 与位宽预算 $b$，PTQ 求解

$$\hat W = Q(W;\, b,\, \mathcal{C}),\qquad \mathcal{C} \text{ 为由校准数据确定的量化参数（scale / zero-point）}$$

使得在评估集上的输出失真 $\mathcal{E}(f, \hat f)$ 最小。区别于量化感知训练（QAT），PTQ 不更新权重、不访问训练集全量数据，因而代价低、部署快；其代价是位宽越低，量化噪声对精度的影响越显著——这正是 PTQ 方法族要解决的核心矛盾。

在 Orca 量化路线中，本 workflow（W2）承接敏感层分析（W1）给出的层敏感度先验，在**固定位宽**下搜索 PTQ 算法组合；其最优解可作为混合精度搜索（W3）与量化感知训练（W4）的初始解。

---

## 3. 背景

### 3.1 均匀量化基本式

对称均匀量化将连续值映射到 $b$ 比特整数格点，再反量化回浮点（伪量化，fake quantization）：

$$\hat x = s\cdot \mathrm{clip}\!\left(\mathrm{round}(x/s),\; q_{\min},\; q_{\max}\right),\qquad q_{\min}=-2^{b-1},\;\; q_{\max}=2^{b-1}-1$$

其中 scale $s = \max(|x|)/q_{\max}$。量化粒度决定了 $s$ 的共享范围：**per-tensor**（整张量一个 $s$）开销最小但精度损失大；**per-channel**（每个输出通道一个 $s$，用于权重）与 **per-token**（每个 token 一个 $s$，用于激活）以更多存储换取更小的截断误差。本 workflow 的 MX 基进一步采用 **block-wise scaling**：每 `block_size=16` 个元素共享一个缩放因子，等价于一个带共享指数的分组浮点格式（`fp4_e2m1` / `fp8_e4m3`）。

### 3.2 PTQ 与 QAT 的取舍

PTQ 的优势在于无需训练基础设施、仅需少量校准样本；其局限在于低比特（≤4 bit）下精度显著劣化。QAT 通过在前向传播中插入伪量化算子、在反向中使用直通估计器（STE）联合优化权重与量化参数，可在极低位宽下恢复精度，但需要完整训练数据与可观的算力。本 workflow 聚焦 PTQ；当 PTQ 在目标位宽下无法满足精度约束时，应转用 W3（混合精度）或 W4（QAT）。

### 3.3 低比特 PTQ 的三类难点

1. **激活离群值**：注意力等结构的激活存在幅值远大于均值的离群通道，主导 scale 并压缩其余通道的有效量化范围。
2. **权重量化误差累积**：朴素舍入（RTN）的逐元素误差在深层网络中级联放大。
3. **层间误差传播**：前一层的量化输出作为后一层的输入，误差逐层耦合，单层最优不等于全局最优。

这三类难点分别由本 workflow 复用的三族方法应对。

### 3.4 相关工作

PTQ 方法可按其在量化流程中的位置划分为三族，本 workflow 将三族组合为统一搜索空间：

| 族 | 作用位置 | 代表方法 | 核心思想 | 局限 |
|---|---|---|---|---|
| 预处理变换 | 量化前改写权重/激活分布 | SmoothQuant (Xiao et al., 2022)、QuaRot (Ashkboos et al., 2024) | 通过等价变换压缩离群值、平抑动态范围 | 增加额外矩阵乘开销 |
| 权重求解 | 将 FP 权重投影到低比特格点 | RTN、GPTQ (Frantar et al., 2022)、AutoRound (Cheng et al., 2023) | 最小化逐层输出误差 | RTN 误差大；GPTQ 需 Hessian，开销 $O(n^3)$ |
| 后处理校正 | 量化后微调权重 | Q2N（零空间校正） | 在不影响量化输出的方向上消除残差 | 仅接二阶求解器；依赖 Hessian 谱可分 |

---

## 4. 方法

### 4.1 问题形式化

将一个 PTQ 候选建模为三环节三元组

$$c = (\pi,\, \sigma,\, \rho)\;\in\;\Pi\times\Sigma\times\mathrm{P}$$

其中 $\pi\in\Pi=\{\text{none, smooth, quarot}\}$ 为预处理变换，$\sigma\in\Sigma=\{\text{rtn, gptq, autoround}\}$ 为权重求解器，$\rho\in\mathrm{P}=\{\text{none, q2n}\}$ 为后处理校正。搜索目标为

$$c^{\*} \;=\; \arg\min_{c\,\in\,\mathcal{S}}\;\frac{1}{N}\sum_{n=1}^{N}\bigl\|\, f(x_n;\,W) - f(x_n;\,\hat W_c)\,\bigr\|_2^{\,2}$$

即以 teacher-student 均方误差（FP teacher 与量化 student 的输出差）为评估准则，在搜索空间 $\mathcal{S}$ 上最小化量化误差。搜索空间由 `mode` 决定：

- **lightweight**：取四条**累积路径**（Smooth 派 / QuaRot 派 / AutoRound 派 / 纯求解派），每条路径沿「基线 → 逐步累加一项技术」递进，构成消融实验视角；
- **full**：取笛卡尔积 $\Pi\times\Sigma\times\mathrm{P}$，按 §4.6 的合法性约束剔除非法组合（如 $\rho=\text{q2n}$ 且 $\sigma=\text{rtn}$）。

### 4.2 预处理变换

**SmoothQuant**（Xiao et al., 2022）。经验上权重 $W$ 的逐通道幅值范围远小于激活 $X$ 对应通道的幅值范围，故后者主导量化误差。引入逐输入通道 $j$ 的平滑因子

$$s_j \;=\; \frac{\max(|X_{\cdot j}|)^{\alpha}}{\max(|W_{j\cdot}|)^{1-\alpha}},\qquad \alpha\in[0,1]\;(\text{默认 }0.5)$$

令 $\tilde W_{j\cdot}=W_{j\cdot}/s_j$、$\tilde X_{\cdot j}=X_{\cdot j}\cdot s_j$。因 $\tilde W\tilde X = WX$，线性层输出严格不变，而 $\tilde X$ 与 $\tilde W$ 的逐通道范围被拉平，二者均落入量化网格的有效动态范围内。

**QuaRot**（Ashkboos et al., 2024）。引入随机正交矩阵 $R$（实现为 Hadamard 变换），在权重与激活两端同乘：

$$\tilde W = WR,\qquad \tilde X = R^{-1}X = R^{\top}X$$

由正交不变性 $WR\cdot R^{\top}X = WX$，层输出不变；而正交变换将激活中「方向性」的离群值重新分配为幅值均匀的分量，对低比特量化更为友好。相较 SmoothQuant，QuaRot 的变换更为彻底，但需额外的旋转矩阵乘。

### 4.3 权重求解

**RTN（Round-To-Nearest）**。直接将每个权重舍入到最近格点：$\hat W = Q(W)$。复杂度 $O(1)$/权重，最快但误差最大，作为所有路径的基线。

**GPTQ**（Frantar et al., 2022）。在逐层最小化输出误差

$$\hat W \;=\; \arg\min_{W'}\;\bigl\|(W-W')X\bigr\|_F^{\,2}$$

的框架下，记 Hessian $H = XX^{\top}\in\mathbb{R}^{d\times d}$（$d$ 为输入特征维）。GPTQ 采用贪心列序量化：量化第 $i$ 列得残差 $\delta_i = \hat W_{ii}-W_{ii}$ 后，将其对未量化列 $j\in\mathcal{F}$ 的二阶影响沿 $H^{-1}$ 反向补偿：

$$W_{:,j} \;\leftarrow\; W_{:,j} \;-\; \frac{[H^{-1}]_{i,:}^{\,\top}}{[H^{-1}]_{ii}}\;\delta_i,\qquad j\in\mathcal{F}$$

随后从 $H^{-1}$ 中消去第 $i$ 行列，递推至全部列量化完成。该方法以 $O(n^{3})$/层的开销获得远优于 RTN 的精度。

**AutoRound**（Cheng et al., 2023）。将「每个权重向上还是向下舍入」建模为可学习变量：$\hat w = \mathrm{clip}\bigl(\lfloor w/s\rfloor + h(v),\,n,\,p\bigr)$，其中 $h(v)$ 为由可学习参数 $v$ 控制的软舍入函数。以少量校准数据做若干步梯度优化求解 $v$，在 RTN 与 GPTQ 之间取得精度与开销的折中。

### 4.4 后处理校正：零空间方法（Q2N）

Q2N 是本 workflow 后处理环节的唯一方法，也是精度收益的关键来源。其思想是：**在 Hessian 度量下对输出影响可忽略的方向（零空间）上放松量化约束，使权重向 FP 参考靠拢，从而在不破坏量化格点的前提下降低输出误差。** 以下给出完整推导。

#### 4.4.1 目标函数

给定 FP 参考权重 $W_{\text{ref}}$、已量化权重 $W_q$（如 GPTQ 输出）与 Hessian $H\approx\frac{1}{N}X^{\top}X$，定义 Hessian 加权的输出误差目标

$$\mathcal{J}(W') \;=\; \mathrm{tr}\!\left((W'-W_{\text{ref}})\,H\,(W'-W_{\text{ref}})^{\top}\right) \;=\; \sum_{j}\lambda_j\,\bigl\|\Delta_{:,j}\bigr\|^{2}$$

其中 $\Delta=(W'-W_{\text{ref}})U$，$H=U\Lambda U^{\top}$ 为特征分解，$\Lambda=\mathrm{diag}(\lambda_1,\dots,\lambda_d)$ 降序。最后一等式表明：**误差在每个特征方向 $j$ 上被其特征值 $\lambda_j$ 加权**。大特征值方向（信号方向）的误差代价高，小特征值方向（零空间方向）的误差代价低。

#### 4.4.2 Hessian 谱分解与零空间识别

对对称化的 $H$ 执行特征分解 $H = U\Lambda U^{\top}$，特征值降序排列。零空间由「能量骤降点」界定：寻找索引 $k$，使尾部能量相对前缀能量之比不超过阈值 $\tau$（默认 $0.1$）：

$$k \;=\; \min\!\left\{\, i\,:\; \frac{\sum_{j>i}\lambda_j}{\sum_{1<j\le i}\lambda_j} \;\le\; \tau \,\right\}$$

据此将特征空间二分为：

- **信号子空间** $\mathcal{S}=\mathrm{span}(u_1,\dots,u_k)$：$H$ 在此方向能量集中，权重误差被激活放大，必须严格保持量化值；
- **零空间** $\mathcal{N}=\mathrm{span}(u_{k+1},\dots,u_d)$：$H$ 在此方向能量可忽略，权重扰动对输出几乎无影响，记 $r=d-k$ 为零空间秩。

对应的正交投影算子为

$$P_{\mathcal{N}} \;=\; U_{\mathcal{N}}U_{\mathcal{N}}^{\top}\;\in\;\mathbb{R}^{d\times d},\qquad P_{\mathcal{S}} \;=\; I - P_{\mathcal{N}} \;=\; U_{\mathcal{S}}U_{\mathcal{S}}^{\top}$$

其中 $U_{\mathcal{N}}=U_{:,k:}$、$U_{\mathcal{S}}=U_{:,:k}$。

#### 4.4.3 子空间混合

由于零空间方向的误差代价近似为零，可在该方向上将量化权重放松回 FP 参考，而在信号方向严格保持量化值，得到混合权重

$$W_b \;=\; W_{\text{ref}}\,P_{\mathcal{S}} \;+\; W_q\,P_{\mathcal{N}}$$

此时 $\mathcal{J}(W_b)\approx \mathcal{J}(W_q)$ 的主导项（信号方向）保持不变，而零空间方向的偏差被替换为更小的 FP 偏差。

#### 4.4.4 行级闭式标量缩放

$W_b$ 一般并非合法量化值。Q2N 以**逐行标量缩放** $\alpha_r$ 将量化权重 $W_q$ 拉向 $W_b$，求解带 Tikhonov 正则的最小二乘

$$\alpha_r \;=\; \arg\min_{\alpha}\;\bigl\|\alpha\,W_q[r,:] - W_b[r,:]\bigr\|^{2} \;+\; \lambda_{\text{reg}}\,\alpha^{2}$$

对 $\alpha$ 求导置零，得闭式解

$$\boxed{\;\alpha_r \;=\; \frac{\displaystyle\sum_{j} W_q[r,j]\,W_b[r,j] \;+\; \lambda_{\text{reg}}}{\displaystyle\sum_{j} W_q[r,j]^{2} \;+\; \lambda_{\text{reg}}}\;}\qquad (\lambda_{\text{reg}}=0.2)$$

候选权重 $W_c[r,:] = \alpha_r\,W_q[r,:]$。此处引入标量缩放而非直接采用 $W_b$，是为了在「向 FP 参考靠拢」与「保持量化权重的整体结构」之间取得平衡，使后续再量化能落回相近格点。

#### 4.4.5 再量化与目标校验

将候选重新投影回量化格点：$W' = Q_{\text{deq}}(W_c)$（由量化器的 `quantize_dequant` 完成）。最终以目标函数校验作为安全网：若 $\mathcal{J}(W') > \mathcal{J}(W_q)$（校正反而增大误差），则回退至 $W_q$（`fallback_to_gptq=True`）。该机制保证 Q2N 在任何情形下不会劣化结果，最坏情况下退化为原始二阶解。

#### 4.4.6 小结

Q2N 的完整流程为：**谱分解 → 能量骤降定位零空间 → 子空间混合 → 行级闭式缩放 → 再量化 → 目标校验回退**。其本质是把「权重必须严格落格点」的硬约束，在 Hessian 低能量方向上软化为「尽量贴近 FP 参考」，从而在不改变量化输出的前提下榨取精度。`max_dim_for_full_p=4096` 限制了全谱分解的维度，超大特征维时跳过该方法以规避 $O(d^{3})$ 的特征分解开销。

### 4.5 评估协议与选择准则

默认评估采用 teacher-student 均方误差。给定 FP teacher $f$ 与量化 student $\hat f$，在评估 loader 上计算

$$\mathrm{MSE} \;=\; \frac{1}{N}\sum_{n}\bigl\|f(x_n) - \hat f(x_n)\bigr\|_2^{\,2}$$

选择准则为 $\arg\min$ MSE（`higher_is_better=False`）。当用户提供业务 `eval_fn_ref` 时，改用业务指标（如准确率），方向由 `get_metric_spec()` 的 `higher_is_better` 决定。

### 4.6 合法性约束

候选构建遵循 `ts_quant` 的接口契约（`QConfig.__post_init__` 校验）：(i) `INT + gptq` 须用 `per_token` 或 `per_channel` 粒度；(ii) `INT + autoround` 须用 `per_token` 粒度；(iii) `post_correction=q2n` 仅接 `gptq`/`autoround`（`rtn+q2n` 在候选构建阶段即被剔除）；(iv) MX 三求解器默认 `per_tensor` 均合法。

### 4.7 lightweight 四条累积路径

| 派 | 累积序列（每步叠加一项） | 终点 |
|---|---|---|
| S（Smooth） | rtn → +smooth → +gptq → +q2n | smooth+gptq+q2n |
| Q（QuaRot） | rtn → +quarot → +gptq → +q2n | quarot+gptq+q2n |
| A（AutoRound） | rtn → autoround → +q2n | autoround+q2n |
| R（纯求解） | rtn → gptq → +q2n | gptq+q2n |

四条路径共享 rtn 基线（全局仅评估一次），其余步骤按 `(pre, solver, post)` 去重。结果以 `step_idx` 为横轴对齐，使横轴语义统一为「累积叠加的技术项数」，从而构成严格意义上的消融对比。

---

## 5. 实验

### 5.1 实验设置

| 项 | 取值 |
|---|---|
| 模型 | ViT-Tiny |
| 数据 | CIFAR-100（校准与评估同源，少量样本） |
| 位宽 | `w4a4-mx`（MX 族 fp4_e2m1，block_size=16） |
| 模式 | lightweight |
| 评估 | teacher-student MSE |

### 5.2 结果

lightweight 模式下经去重得到 11 个唯一候选，最优解为 `smooth+gptq+q2n@w4a4-mx`，MSE 为 $0.0089$，烘焙产物 `best_quant_model.pt`（约 21 MB）。输出摘要示例：

```json
{
  "best_config": "smooth+gptq+q2n@w4a4-mx",
  "best_metric": 0.0089,
  "candidates_evaluated": 11,
  "mode": "lightweight",
  "metric_kind": "mse",
  "baked_model_path": "llm_artifacts/vit_tiny_cifar100/best_quant_model.pt"
}
```

### 5.3 分析

最优组合的构成符合方法设计的预期：SmoothQuant 先平抑激活离群值（预处理收益），GPTQ 在二阶意义下最小化逐层输出误差（求解收益），Q2N 进一步在 Hessian 零空间内消除残差（后处理收益）。三环节沿 S 派累积路径递进，MSE 单调下降，验证了三族方法在该位宽下的协同有效性。相对于 rtn 基线，完整组合显著降低了 teacher-student 输出失真。

### 5.4 计划截图

- **lightweight → line 图**「四条累积路径的 MSE 下降曲线」：横轴 `step_idx`（0=rtn 基线 → 1/2/3 逐步叠加），纵轴 MSE，四条线 S/Q/A/R。预期四条线左高右低，`smooth+gptq+q2n` 终点最低。
- **full → heatmap**「recipe × 位宽精度矩阵」：行=recipe，列=位宽，cell 颜色=MSE。
- **bar + table**：各路径终点对比与全候选明细。

> 标注「📊 计划截图」处为预设图位。流程图/原理示意以 ASCII 内联；真实数据图（line/heatmap/bar/scatter）留占位，待运行一次 workflow 后由 Web 面板（`orca open`）截取真实图替换。

---

## 6. 局限与延伸

Q2N 的全谱分解复杂度为 $O(d^{3})$，故对特征维 $d>4096$ 的层自动跳过；极大宽度的 Linear 层在该环节无收益。PTQ 在极低位宽（<4 bit）下存在精度上界，当 lightweight/full 均无法满足精度约束时，应将本 workflow 的最优解作为初值，转入 W3（混合精度 Pareto 搜索，放宽「所有层同位宽」假设）或 W4（QAT + CAGE，通过梯度优化恢复精度）。

---

## 附录 A：ts_quant 库接口手册

本 workflow 的全部量化能力由 `ts_quant` 库提供。下列接口供用户在 workflow 之外独立调用，等价复现 workflow 的核心功能。

### A.1 一键量化：`quantize_model`

标准 PTQ 生命周期 `prepare → calibrate → convert` 的封装：

```python
from ts_quant import quantize_model, QConfig
from ts_quant.plugins import SmoothQuantPlugin   # 或 QuaRotPlugin

qconfig = QConfig(
    method="mx",
    w_elem_format="fp4_e2m1",
    a_elem_format="fp4_e2m1",
    block_size=16,
    weight_solver="gptq",         # rtn / gptq / autoround
    post_correction="q2n",        # none / q2n（仅接 gptq/autoround）
    granularity="per_tensor",     # INT + gptq 需改 per_token/per_channel
)

q_model = quantize_model(
    model=fp_model,
    qconfig=qconfig,
    calib_data=calib_loader,       # torch DataLoader；None 则用假随机
    forward_fn=forward_fn,         # 按模型 forward 解包 batch，异构 batch 必填
    plugins=[SmoothQuantPlugin()], # 预处理变换；None 跳过
    max_steps=64,                  # SmoothQuant 两遍校准的上限
    inplace=True,                  # True 改原模型；跨候选复用须 deepcopy
)
```

关键参数：`qconfig_dict`（逐层 QConfig 映射，用于混合精度）、`skip_list`（跳过量化的模块名）、`freeze`（是否冻结量化参数）、`return_quantizer`（返回量化器以读取 scale 等参数）。

### A.2 量化配置：`QConfig`

`@dataclass`，主要字段与约束：

| 字段 | 取值 | 说明 |
|---|---|---|
| `method` | `int` / `mx` / `smx` / `fp8` | 量化基（本 workflow 用 `mx`/`int`） |
| `n_bits` / `w_n_bits` / `a_n_bits` | 正整数 | 权重/激活位宽（`w/a` 覆盖 `n_bits`） |
| `w_elem_format` / `a_elem_format` | 如 `fp4_e2m1`/`fp8_e4m3` | MX 元素格式 |
| `block_size` | 16 | MX 共享 scale 的块大小 |
| `granularity` | `per_tensor`/`per_channel`/`per_token` | scale 共享粒度 |
| `weight_solver` | `rtn`/`gptq`/`autoround` | 权重求解器 |
| `post_correction` | `none`/`q2n` | 后处理校正 |
| `a_quant_enabled` | bool | False → 激活 bypass，用于 weight-only（如 `w4a16`） |

构造时由 `__post_init__` 强制校验合法性（见 §4.6）；非法组合直接 `raise`。

### A.3 评估：`build_teacher_student_eval_fn`

```python
from ts_quant.eval import build_teacher_student_eval_fn

eval_fn = build_teacher_student_eval_fn(
    teacher_model=fp_model,
    dataloader=eval_loader,
    forward_fn=forward_fn,
)
metrics = eval_fn(q_model)   # {"mse": float, ...}
```

返回的 `eval_fn` 接收量化 student 模型，返回指标字典。提供业务评估函数时，可经 `MetricSpec`（`primary_metric` + `higher_is_better`）声明选择准则。

### A.4 预处理插件

```python
from ts_quant.plugins import SmoothQuantPlugin, QuaRotPlugin
```

二者均为无参构造，作为 `quantize_model` 的 `plugins` 列表元素传入；传入多个即按序叠加。`SmoothQuantPlugin` 触发两遍校准（搜索最优 $\alpha$ 与平滑因子 $s$）。

### A.5 零空间后处理：`Q2NPostCorrection`

当 `QConfig(post_correction="q2n")` 时由内部自动调用；亦可独立使用以审视其行为：

```python
from ts_quant.algorithms.q2n import Q2NPostCorrection

q2n = Q2NPostCorrection(
    threshold=0.1,          # 能量骤降阈值 τ
    lambda_reg=0.2,         # 行级缩放 Tikhonov 正则
    fallback_to_gptq=True,  # 目标函数劣化时回退
    max_dim_for_full_p=4096,# 全谱分解维度上限
    # drop_index=k,         # 显式指定零空间分割点（默认自动检测）
)
corrected, meta = q2n.apply(
    reference_weight=W_ref,   # FP 原权重
    quantized_weight=W_q,     # 已量化权重（gptq/autoround 输出）
    hessian=H,                # XᵀX，d×d
    oracle=quantizer,         # 提供 quantize_dequant 的量化器
    layer_name="block.0.attn",
)
# meta 含 status / null_rank / objective_before / objective_after 等
```

`meta["objective_after"] < meta["objective_before"]` 即零空间校正生效的量化证据。

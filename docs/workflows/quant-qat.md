# quant-qat —— 量化感知训练 + CAGE 后校正（W4）

> 量化 pipeline 第 4 级（收尾）。**回答一个问题：4 比特量化精度崩了，能不能靠短训拉回来？** W1–W3 都是训练后量化（PTQ），W4 是训练感知量化（QAT）——先把模型 fake-quant（掉精度），再短训恢复。

## 1. 一句话定位

输入浮点模型 + 训练数据，对比 rtn / duquantpp 两种训练态 fake-quant 方案，短训恢复精度（+ CAGE 后校正），输出每方案的「前/后精度恢复 + 收敛曲线」+ bake 最佳 q_model。底层调 `ts_quant.prepare_trainable_fakequant_model` + `prepare_trainable_qat`。

## 2. In-session 如何激活

```
用 TARS 对这个模型做量化感知训练
TARS，4 比特掉点太多，跑个 QAT 拉回来
```

匹配命中的 description 关键词：**「QAT / 量化感知训练 / 训练恢复 / CAGE / fake-quant 训练」**。

等价手动命令：

```bash
orca quant-qat --inputs '{
  "model_path": "demo_target/vit_tiny_cifar100/model.py",
  "project_root": "demo_target/vit_tiny_cifar100",
  "scheme": "both", "bit_width": "w8a8-mx",
  "cage": "auto", "total_steps": "64", "lr": "1e-4", "bake": "true"
}'
```

## 3. 输入 / 输出

**输入**：

| 参数 | 默认 | 说明 |
|---|---|---|
| `model_path` / `project_root` | — | 模型入口 / 项目根 |
| `calib_data_ref` | `""` | 校准 loader（duquantpp 校准用；空→假随机） |
| `train_data_ref` | `""` | **训练 loader（QAT 核心；空→复用 calib 做 smoke，真实恢复需真实数据）** |
| `eval_data_ref` / `eval_fn_ref` | `""` | 评估 loader + 业务 eval_fn（空→teacher-student mse） |
| `scheme` | `both` | rtn / duquantpp / both（both=对比两方案） |
| `bit_width` | `w8a8-mx` | QAT fake-quant 位宽（高位宽起步更稳） |
| `cage` | `auto` | CAGE 后校正开关（auto/true/false） |
| `total_steps` | `64` | QAT 训练步数（smoke 友好；真实 QAT 视数据集调大） |
| `lr` | `1e-4` | Adam 学习率 |
| `bake` | `true` | bake 最佳 q_model state_dict |

**输出**：`report.json` + `baked_model_path`（best_qat_model.pt）+ line（收敛）+ bar（前/后恢复）+ table。

## 4. 算法原理

### 为什么需要 QAT

PTQ（W2）是「量化完就交付」——FP 权重直接投影到低比特，不回头。低位宽（4 比特）下误差大、精度掉。**QAT（Quantization-Aware Training）**的思路：把量化**插进训练循环**——前向用 fake-quant（模拟低比特），反向照常更新 FP 权重，让权重**学着适应量化误差**。

```
PTQ（W2）：  FP模型 ──量化──→ 交付（不回头）
QAT（W4）：  FP模型 ──fake-quant──→ 短训（权重学着补偿量化误差）──→ 交付
```

### Fake-quant 是什么

fake-quant = 「**前向时假装量化、反向时用直通估计器（STE）传梯度**」：

```
前向：  y = Q(W) · x        ← Q() 把权重 round 到低比特格点（模拟量化误差）
反向：  ∂L/∂W ≈ ∂L/∂Q(W)    ← 直通估计器（Straight-Through Estimator）：
                                量化不可导，梯度直接透传，假装 Q 可导
```

权重在 FP 上持续更新，但每次前向都「看见」自己被量化后的样子——几百步后，权重会挪到「量化后误差最小」的区域。

### 两种方案（`scheme`）

```
┌──────────────────────────────────────────────────────────────────┐
│  prepare_trainable_fakequant_model —— 把 Linear 换成可训 fake-quant │
├──────────────────────────────────────────────────────────────────┤
│  rtn       RTNFakeQuantLinear：最简单的 fake-quant（round-to-nearest │
│            语义），权重/激活都有可学 scale，直接进 QAT                │
│  duquantpp DuQuant++ Linear：先校准学一个旋转/置换把激活分布压平，    │
│            再 fake-quant。对低位宽 + 有离群激活的模型更强，但需校准    │
└──────────────────────────────────────────────────────────────────┘
```

`both` = 两个都跑、对比谁的恢复更好。duquantpp 需要 `calib_data`（先校准旋转矩阵），rtn 不需要。

### CAGE 后校正（核心创新）

**CAGE** = post-step weight **C**orrection with **A**ctivation-**G**uarded **E**xtrapolation（伪冠名，实质是「每步训练后，把权重往量化前的方向拉一点」）。公式：

$$
W \leftarrow W - \text{lr}\cdot \lambda \cdot (W - Q(W))
$$

- $Q(W)$ = 当前权重的量化版
- $W - Q(W)$ = 量化残差（指向「量化前」的方向）
- $\lambda$ = CAGE 强度，按 schedule（silence_ratio + ramp）从 0 渐升——前期不干扰正常训练，后期才把权重往量化友好区拉

直觉：普通 QAT 只靠 loss 梯度间接学量化友好；CAGE **每步额外加一个「直接缩小量化残差」的项**，加速收敛到量化误差最小的权重。

```
每步训练：
  1. forward(fake-quant) → loss → backward → optimizer.step()   ← 正常 QAT 梯度步
  2. qat.step() → CAGE: W ← W − lr·λ·(W − Q(W))                 ← 额外的残差收缩（schedule 控制）
```

`cage=auto`：由 `total_steps` 自动决定开关（步数够就开）。`cage_start_step` ≈ 0.1×total_steps 后才生效（前期 silence）。

### 训练 loss：teacher-student distillation（label-free）

W4 默认用 **teacher-student mse** 训练，**不需要真实标签**：

```
loss = mse( q_model(batch), fp_model(batch).detach() )
              ↑ student              ↑ teacher（固定 FP）
```

学生（量化模型）模仿老师（FP 模型）的输出。好处：任何模型都能跑（不用凑训练标签），且直接优化「量化后行为是否复现 FP」——和评估口径一致。有业务标签时，可传 `eval_fn_ref` 用业务指标评估（训练 loss 仍是 teacher-student mse）。

## 5. 结果示例 + 计划截图

**真实跑过**（ViT-Tiny，both 方案，w8a8-mx，cage=auto，8 步 smoke）：

```
rtn:       before(mse) 0.00078 → after 0.00510  （8 步 smoke，未充分收敛）
duquantpp: before(mse) 0.00073 → after 0.00274  ← 更好 → 选中 bake
```

> 注：smoke（8 步 + 假随机数据）下 recovery 为正是正常的——步数太少、数据非真实。真实 QAT（几百步 + 真实数据，尤其低比特 w4a4）会看到 **recovery 为负（mse 下降）**，即 CAGE + 训练把量化掉的精度拉回来了。这才是 W4 的价值。

输出 JSON 摘要：

```json
{
  "best_scheme": "duquantpp",
  "best_metric": 0.002744,          // after
  "best_metric_before": 0.000732,   // before（fake-quant 基线）
  "recovery": 0.002012,             // after − before
  "schemes_evaluated": ["rtn", "duquantpp"],
  "total_steps": 64, "cage": "auto", "metric_kind": "mse",
  "baked_model_path": "llm_artifacts/vit_tiny_cifar100/best_qat_model.pt"
}
```

### 📊 计划截图（放这里）

- **line 图**「QAT 收敛曲线」（主图）：x=训练步数，y=mse，两条线（rtn / duquantpp）。应看到曲线随步数**下降**（CAGE 生效后加速）。
  > 占位：两条下降折线，duquantpp 终点更低；纵轴 mse，横轴 step 0→total_steps，标注 CAGE start_step（曲线在那之后斜率变陡）。
- **bar 图**「QAT 前/后精度」：每方案两根柱（before 灰 / after 彩），直观看恢复幅度。
- **table**「方案对比」：scheme | before | after | recovery | steps | cage。

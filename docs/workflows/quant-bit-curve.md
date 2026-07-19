# quant-bit-curve —— 混合精度 Pareto 位宽-精度曲线（W3）

> 量化 pipeline 第 3 级。**回答一个问题：到底能用多低的位宽、还保住精度？** 与 W2 互补——W2 在固定位宽下挑算法，W3 在精度约束下挑**位宽/格式**，画出 Pareto 前沿。

## 1. 一句话定位

输入浮点模型 + 校准/评估数据，让 m0_pareto 搜索器在 INT8/W4A8/INT4/MX4/MX8 格式空间里逐层组合，输出「位宽 vs 精度」Pareto 前沿 + 选中候选的格式分布 + bake 最佳混合精度模型。底层调 `ts_quant.search_mix_precision(strategy="m0_pareto")`。

## 2. In-session 如何激活

```
用 TARS 找一下位宽和精度的折中曲线
TARS，这个模型能压到多少比特还能保精度？
```

匹配命中的 description 关键词：**「位宽 / 精度 / Pareto / 混合精度 / 折中曲线 / bit」**。

等价手动命令：

```bash
orca quant-bit-curve --inputs '{
  "model_path": "demo_target/vit_tiny_cifar100/model.py",
  "project_root": "demo_target/vit_tiny_cifar100",
  "mode": "explore",
  "candidate_format_space": "INT8,W4A8,INT4,MX4,MX8",
  "max_evals": "32", "bake": "true"
}'
```

## 3. 输入 / 输出

**输入**：

| 参数 | 默认 | 说明 |
|---|---|---|
| `model_path` / `project_root` | — | 模型入口 / 项目根 |
| `calib_data_ref` / `eval_data_ref` / `eval_fn_ref` | `""` | 校准 / 评估 loader + 业务 eval_fn（空→teacher-student mse） |
| `mode` | `explore` | explore（出完整前沿）/ constrained_select / minimize_bit_under_accuracy（满足容忍下选最低位宽） |
| `candidate_format_space` | `INT8,W4A8,INT4,MX4,MX8` | 对比格式集（全 mxint/int 基） |
| `bit_objective` | `weight_activation_proxy` | bit 成本口径（区分 W4A8 与 INT4/MX4） |
| `accuracy_tolerance` | `0.01` | 相对 baseline 的精度损失绝对容忍 |
| `avg_bit_budget` | `""` | 硬 bit 上限（空=无硬约束） |
| `max_evals` | `32` | 主搜索预算（32=smoke，128=真实模型） |
| `granularity` | `per_tensor` | per_tensor / per_token / per_channel |
| `bake` | `true` | bake 选中最佳混合精度模型 |

**输出**：`bit_curve_summary.json` + `baked_model_path`（best_mixed_model.pt）+ SDK 原始 `frontier.json`/`bit_trend.json` + line/bar/table。

## 4. 算法原理

### 什么是混合精度 + Pareto 前沿

**统一精度**（全 INT8 或全 INT4）是两个极端：全高精度=准但胖；全低精度=瘦但崩。**混合精度**（mixed precision）的洞察——**不是所有层都需要同样多位宽**。敏感层（W1 找出的那些）保高位宽，不敏感的层压低位宽，能拿到「平均位宽很低、精度几乎不掉」的甜点。

但层数一多，组合空间爆炸：50 层 × 5 种格式 = $5^{50}$ 种分配。暴力不可行。

### Pareto 前沿

「Pareto」= **没人能在不牺牲另一个目标的前提下同时改善两个目标**。这里两个目标是 **平均位宽（↓越好）** 和 **精度损失（↓越好）**：

```
精度损失
    ↑
高  │  ·                          ← 这些点都被「支配」：
    │        ·                       存在另一个点 位宽更低 且 精度更好
    │              ·
    │ · ← Pareto 前沿（最优折中）·····  ← 选点就选这条线上
    │  ·
    │     ·
低  └──────────────────────────→ 平均位宽
    低                            高
```

W3 的任务就是**找到这条前沿**，让你按需选点：要最瘦的模型？选前沿最左点；要最准？选最右点；要折中？选中间。

### m0_pareto 搜索器怎么找前沿

`m0_pareto` 是 ts_quant 的纯格式 Pareto 搜索（M0 系列）：

```
┌──────────────────────────────────────────────────────────────────┐
│  m0_pareto 三件事（M0.1.1 版）                                    │
├──────────────────────────────────────────────────────────────────┤
│  1. sensitivity（敏感度先验）                                      │
│     逐层 probe：把这层降到 INT4 看指标掉多少 → 给每层一个敏感度分   │
│  2. layer_policy（搜索空间剪枝）                                   │
│     高敏感层限制到 {INT8/MX8} 安全格式集；低敏感层才允许 {INT4/MX4} │
│     → 把 5^50 砍到一个可搜的小空间 S_policy                       │
│  3. 主搜索（DOE + guided descent + mutation）                      │
│     在 S_policy 里评估 max_evals 个完整 layer-format assignment    │
│     每个 assignment = 「这 50 层每层用哪种格式」                    │
│     → 真模型 eval → 进 archive → 抽 Pareto 前沿                   │
└──────────────────────────────────────────────────────────────────┘
```

输出 `frontier.json`（前沿候选列表）+ `bit_trend.json`（每个位宽桶的最好精度）。

### 五种格式（全 mxint/int 基）

| 格式 | 权重 / 激活 | 说明 |
|---|---|---|
| INT8 | int8 / int8 | 标准 8 比特整数，基线上界 |
| W4A8 | int4 / int8 | weight-only 压缩到 4 比特，激活仍 8 |
| INT4 | int4 / int4 | 激进 4 比特整数 |
| MX4 | mx fp4 / fp4 | **MX 族**（mxint 基）：fp4_e2m1 + block_size=16，带 block scale |
| MX8 | mx fp8 / fp8 | **MX 族**：fp8_e4m3 + block_size=16 |

> **bit_objective=weight_activation_proxy**：W4A8 和 INT4 的「权重位宽」都是 4，但 W4A8 激活是 8 比特、INT4 是 4 比特——综合成本不同。用 `(权重位宽 + 激活位宽)/2` 这个 proxy 当 x 轴，才能正确区分它们。

### 三种模式（`mode`）

- **explore**：出**完整 Pareto 前沿**（最常用，先看折中空间再选点）。
- **constrained_select**：在约束下选点。
- **minimize_bit_under_accuracy**：满足 `accuracy_tolerance` 的候选里，选**位宽最低**那个（「在精度只掉 1% 的前提下，能压到多低」的直接答案）。

## 5. 结果示例 + 计划截图

**真实跑过**（ViT-Tiny，explore，max_evals=8，格式 INT8/INT4/MX4/MX8）：

```
50 层可量化 → 8 次 eval → 选中 cand_0002 [INT8×26 + INT4×24]
平均位宽(selection_bit_score) ≈ 5.35 → bake 出 best_mixed_model.pt（21MB）
```

输出 JSON 摘要：

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

> 读法：26 层保 INT8、24 层压 INT4，平均位宽 5.35——比「全 INT8（8 比特）」瘦 33%，精度几乎不掉（mse≈0）。这就是混合精度的甜点。

### 📊 计划截图（放这里）

- **line 图**「位宽-精度 Pareto 前沿」（主图）：x=平均位宽（selection_bit_score），y=mse（或精度），前沿点连线，选中点高亮。
  > 占位：一条从左上（低 bit、高损失）向右下（高 bit、低损失）的折线，选中点用大圆点 + 标注「cand_0002, bit=5.35」。
- **bar 图**「选中候选的格式分布」：x=格式（INT8/INT4/...），y=层数。直观看 mxint 混合比例。
- **table**「前沿候选明细」：candidate_id | bit | mse | accuracy_loss | 格式分布。

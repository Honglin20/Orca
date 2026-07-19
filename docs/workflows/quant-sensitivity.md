# quant-sensitivity —— 量化敏感层分析（W1）

> 量化 pipeline 第 1 级。**回答一个问题：这个模型的哪些层怕量化、哪些层不怕？** 答案决定下游 PTQ / 混精 / QAT 怎么分配位宽。

## 1. 一句话定位

输入一个浮点模型 + 少量校准数据，用四种方法之一逐层打分，输出「敏感层排名 + 可视化」。底层调 `ts_quant.analyze_low_precision_sensitive_layers`。

## 2. In-session 如何激活

跟主 session 说意图，TARS 按 description 语义匹配：

```
用 TARS 分析这个模型的量化敏感层
TARS，看看 vit_tiny 哪些层不能压到 4 比特
```

匹配命中的 description 关键词：**「敏感层 / 低精度 / sensitive / 哪些层怕量化」**。

等价手动命令（TARS 在背后调的）：

```bash
orca list                                  # 选到 quant-sensitivity
orca quant-sensitivity                     # 拿 inputs_schema
orca quant-sensitivity --inputs '{         # 启动
  "model_path": "demo_target/vit_tiny_cifar100/model.py",
  "project_root": "demo_target/vit_tiny_cifar100",
  "method": "mse", "ratio": "0.1", "low_bits": "w4a4-mx"
}'
# → 返回 run_id；TARS 派子代理逐节点推进到 done
```

## 3. 输入 / 输出

**输入**（`[default]`=可省略有默认，`[advanced]`=进阶）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `model_path` | — | FP 模型入口 |
| `project_root` | — | 项目根 |
| `calib_data_ref` | `""` | 校准 loader dotted-path（空→假随机，敏感度只需少量样本） |
| `method` | `mse` | mse / layer_stats / ptq_binary_sensitivity / mix_precision_search |
| `ratio` | `0.1` | 敏感层选取比例（top 10%） |
| `low_bits` | `w4a4-mx` | 低精度预设（mxint 基：w4a4-mx / int4 / w4a16） |
| `high_bits` | `w8a8` | 高精度对照（仅 binary/mix 方法用） |
| `eval_fn_ref` | `""` | 业务 eval_fn（仅 binary/mix 方法必填） |

**输出**：`sensitive_layers`（排名列表）+ `report.json` + bar（每层敏感度）+ table。

## 4. 算法原理

### 为什么需要敏感层分析

量化误差是**逐层累积**的：一个模型 50 层，全压到 4 比特可能精度崩；但通常只有少数层（比如首层、靠近输出的层、激活值范围大的层）是「放大器」，其余层对低比特并不敏感。敏感层分析就是**先找出这些放大器**，让下游把它们保在高精度、其余放心压低——花最小的精度代价换最大的压缩。

### 四种方法（`method`）

```
┌─────────────────────────────────────────────────────────────────┐
│  method 选择（按「你有多少评估能力」递进）                       │
├─────────────────────────────────────────────────────────────────┤
│  mse                    最轻：每层单独 fake-quant，算输出 mse    │
│                         （不需要业务指标，只要校准数据）          │
│  layer_stats            更轻：只看 weight/activation 的统计量     │
│                         （离群值多 = 敏感），不跑 forward         │
│  ptq_binary_sensitivity 中：二档搜索（高精度 vs 目标量化）        │
│                         需 eval_fn，回答「哪些层必须高精度」      │
│  mix_precision_search   最重：完整混合精度搜索（含交互效应）      │
│                         需 eval_fn，直接出可部署的混精方案        │
└─────────────────────────────────────────────────────────────────┘
```

- **mse**：把第 *i* 层单独换成低比特 fake-quant，跑一遍校准数据，量它**输出**相对 FP 的 mse。mse 大 = 量化这层误差大 = 敏感。逐层做一遍排名。最常用、最快、不依赖业务指标。
- **layer_stats**：不跑 forward，只统计每层 weight / activation 的分布（比如 activation 的 max/mean 比、离群点比例）。分布越「尖刺」的层越难量化。最快，适合超大模型初筛。
- **ptq_binary_sensitivity**：在「全量高精度」和「全量低精度」之间，逐层试探「把这一层救回高精度，整体指标能改善多少」。需要 `eval_fn`（业务或 teacher-student），结果直接告诉你哪些层**必须**保高精度。
- **mix_precision_search**：完整混合精度搜索（底层走 W3 的 m0_pareto 同款搜索器），考虑层间交互，直接产可部署的逐层格式分配。

> `ratio=0.1` = 取排名前 10% 的层标为敏感层。`low_bits` 决定「低精度」具体是什么（默认 w4a4-mx，即 mxint 4 比特）。

### 数据格式

全部以 **mxint 为基础数据格式**（`low_bits` 默认 `w4a4-mx` = MX 族的 fp4_e2m1，block_size=16）。`int4` / `w4a16` 是同族的整数/weight-only 变体。

## 5. 结果示例 + 计划截图

**真实跑过**（ViT-Tiny / CIFAR-100，5.5M 参数，method=mse, ratio=0.1, low_bits=w4a4-mx）：

```
50 个 Linear 层 → 选出 5 个敏感层（top 10%）
敏感层多集中在 transformer 首层投影 + 最后的 MLP head
```

输出 JSON 摘要：

```json
{
  "output_dir": "llm_artifacts/vit_tiny_cifar100/",
  "report_path": "llm_artifacts/vit_tiny_cifar100/report.json",
  "sensitive_layers": ["to_patch_embedding.2", "transformer.layers.0.0.to_qkv", "..."],
  "selected_count": 5,
  "method": "mse"
}
```

### 📊 计划截图（放这里）

- **bar 图**「每层敏感度排名」：x=层名（top-N），y=mse，红色高亮选中的敏感层。
  > 占位说明：放一张柱状图，柱高=该层 fake-quant 后输出 mse，前 10% 染红，其余灰。横轴层名旋转 45°。
- **table**「敏感层明细」：层名 | mse | 排名 | 是否选中。

下游怎么用：把这 5 层保在 w8a8、其余压到 w4a4-mx → 喂给 W2（PTQ）或 W3（混精搜索）做精确分配。

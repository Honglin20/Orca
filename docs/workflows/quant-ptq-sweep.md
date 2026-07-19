# quant-ptq-sweep —— 粗粒度 PTQ 扫描（W2）

> 量化 pipeline 第 2 级。**回答一个问题：训练后量化（PTQ）用哪套算法组合最好？** 在固定位宽下横扫预处理 / 权重求解 / 后处理算法，bake 出最佳量化模型。

## 1. 一句话定位

输入浮点模型 + 校准数据，双模式横扫 PTQ 算法组合（lightweight=4 条累积路径 / full=全枚举矩阵），teacher-student mse 评估，bake 最佳量化模型。底层调 `ts_quant.quantize_model` + SmoothQuant / QuaRot 插件。

## 2. In-session 如何激活

```
用 TARS 对 vit_tiny 做一轮 PTQ 扫描
TARS，跑一下量化算法对比，挑最好的
```

匹配命中的 description 关键词：**「PTQ / 训练后量化 / 算法对比 / SmoothQuant / GPTQ / 扫描」**。

等价手动命令：

```bash
orca quant-ptq-sweep --inputs '{
  "model_path": "demo_target/vit_tiny_cifar100/model.py",
  "project_root": "demo_target/vit_tiny_cifar100",
  "mode": "lightweight", "bit_widths": "w4a4-mx", "bake": "true"
}'
```

## 3. 输入 / 输出

**输入**：

| 参数 | 默认 | 说明 |
|---|---|---|
| `model_path` / `project_root` | — | 模型入口 / 项目根 |
| `calib_data_ref` / `eval_data_ref` / `eval_fn_ref` | `""` | 校准 / 评估 loader + 业务 eval_fn（空→teacher-student mse） |
| `mode` | `lightweight` | lightweight（4 累积路径）/ full（全枚举矩阵） |
| `bit_widths` | `""` | 位宽预设逗号串（lightweight 默认 w4a4-mx；full 默认 w4a4-mx,w4a8-mx,w8a8-mx） |
| `recipes` | `""` | lightweight=S/Q/A/R 子集；full=pre/solver 子集 |
| `bake` | `true` | 是否 bake 最佳 state_dict |

**输出**：`baked_model_path`（best_quant_model.pt）+ `report.json` + line/bar/heatmap/table。

## 4. 算法原理

PTQ 把「量化」拆成三个可替换的环节，每个环节有多种算法。W2 的核心价值是**把这些环节组合成一个搜索网格**，实测算哪个组合精度最好。

### 量化三环节

```
┌──────────────────────────────────────────────────────────────────────┐
│                    一个 PTQ 候选 = 三个环节的选择                      │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ① 预处理 (pre)        ② 权重求解 (solver)      ③ 后处理 (post)      │
│  改造权重分布让        把 FP 权重「投影」到       量化后再修正残差     │
│  它更好量化            低比特网格上                                    │
│  ─────────────         ──────────────────       ─────────────       │
│  none / SmoothQuant /  rtn / gptq / autoround     none / q2n         │
│  QuaRot                                                               │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### 各算法原理

**① 预处理（让权重/激活更易量化）**

- **SmoothQuant**（Xiao et al. 2022）：观察到——权重好量化（范围小）、激活难量化（有离群值）。它把激活的「难度」**按比例迁移一部分到权重**上：给每层算一个平滑因子 $s$，做 $W' = W/s,\ x' = x\cdot s$。数学上等价（$Wx = W'x'$），但激活范围被压平、权重略涨，两边都进了量化器的舒适区。
  ```
  激活难量化 ←──── 迁移 ────→ 权重好量化
     x (有离群尖刺)            W (本来平)        SmoothQuant 后：x' 变平、W' 略尖，但都好量化
  ```
- **QuaRot**（Ashkboos et al. 2024）：用 Hadamard 旋转（一个正交矩阵 $R$）在权重和激活两端同乘，$W'=WR,\ x'=R^{-1}x$。正交变换不改变数学结果，但能把激活里「方向性」的离群值打散成均匀的小幅值，对低比特尤其友好。比 SmoothQuant 更彻底，但多一次旋转开销。
- **none**：不预处理，直接量化。

**② 权重求解（把 FP 权重投影到低比特）**

- **RTN**（Round-To-Nearest）：最朴素——直接把每个权重四舍五入到最近的量化格点。O(1) 每个权重，最快，但误差大。
- **GPTQ**（Frantar et al. 2022）：**逐列**最小化量化误差。基于 Hessian 矩阵 $H=X^\top X$（$X$ 是校准时流过这层的激活），用一个近似牛顿步：量化第 $i$ 列时，用 $H$ 把它的误差**分摊到还没量化的列**上去补偿。
  ```
  GPTQ：量化 W[:,i] 后，把误差 δ = (Q(W[:,i]) - W[:,i]) 沿 Hessian 反向补偿给 W[:, j>i]
        → 整层累积误差最小化（贪心 + Hessian 加权）
  ```
- **AutoRound**（Cheng et al. 2023）：把「每个权重 round 上还是 round 下」当成可学习变量，用少量校准数据做几步梯度优化（学一个 rounding 残差 $v$），比 RTN 好但比 GPTQ 轻。

**③ 后处理（量化后再修正）**

- **q2n**（Q2N /「quantize to N」零空间修正）：量化完成后再做一轮基于 Hessian 的权重微调，把量化误差投影回**零空间**——即「在不改变量化输出的方向上」调整权重，进一步降误差。只接 gptq/autoround（RTN 后处理只支持 none）。

> **零空间（null space）**：矩阵 $W$ 的零空间是所有满足 $Wv=0$ 的向量 $v$。在量化里，q2n 找的是「对量化输出无影响的权重调整方向」，沿着这些方向修权重，能降 FP-输出误差却不破坏已量化的格点。这是 W2「零空间」那条线索的数学含义。

### 两种模式（`mode`）

- **lightweight**：沿 4 条**累积路径**线性叠加技术，看「加这一项能改善多少」——
  - **S 派（Smooth）**：rtn → +smooth → +gptq → +q2n
  - **Q 派（QuaRot）**：rtn → +quarot → +gptq → +q2n
  - **A 派（AutoRound）**：rtn → autoround → +q2n
  - **R 派（纯求解）**：rtn → gptq → +q2n

  画成 **line 累积曲线**：x=累积了几项技术，y=mse，4 条线对比。
- **full**：位宽 × 预处理 × 求解 × 后处理**全枚举**（按 SDK §9.4 合法表过滤 rtn+q2n 等），约 45 候选。画成 **heatmap 矩阵**（行=recipe，列=位宽，cell=mse）。

### 数据格式

全 **mxint 基**：位宽预设 w4a4-mx / w4a8-mx / w8a8-mx（MX 族 fp4/fp8 + block_size=16）、w8a8-int（纯整 int8）、w4a16（weight-only INT4 + 激活 FP16）。

## 5. 结果示例 + 计划截图

**真实跑过**（ViT-Tiny，lightweight 模式，w4a4-mx）：

```
11 个唯一候选 → best = smooth+gptq+q2n@w4a4-mx（mse 0.0089）
→ bake 出 best_quant_model.pt（21MB）
```

输出 JSON 摘要：

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

### 📊 计划截图（放这里）

- **lightweight → line 图**「4 条累积路径的 mse 下降曲线」：x=step_idx（0=rtn 基线→1/2/3 累积加技术），y=mse，4 条线 S/Q/A/R。
  > 占位：4 条下降折线，左端 rtn 基线最高，右端（加满技术）最低；Smooth+GPTQ+Q2N 终点最低。
- **full → heatmap**「recipe × 位宽 精度矩阵」：行=recipe，列=位宽，cell 颜色=mse（越绿越好）。
- **bar + table**：终点对比 + 全候选明细。

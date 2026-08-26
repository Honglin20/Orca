# nas-hp-search：弹性超网超参搜索（NAS-轻量）

> Orca NAS 三件套的轻量版。以弹性超网（Elastic Supernet）承载宽度/深度等结构超参的搜索空间，单次训练后以多目标进化算法搜索 Pareto 前沿，脚本化选出 top-K 架构。仅搜超参、不替换 block 组件，全程无 LLM 介入评估。

---

## 1. 实现概览

### 1.1 这个 workflow 做什么

`nas-hp-search` 读取用户的浮点模型，生成一个仅暴露宽度/深度等超参轴的弹性超网，训练后在该超网的子网空间上运行 NSGA-II 多目标搜索（目标为精度与延迟），并以高权衡选择器从 Pareto 前沿选出 top-K 架构。其底层调用 `nas_agent` 库的 `nas-search` 与 `nas-select-architecture` CLI。

### 1.2 架构与流程

该 workflow 为 5 节点线性链：超网生成 → 训练脚本生成 → 搜索管线生成 → 训练与搜索执行 → 脚本化选优。

```
 model_optimizer        生成最小 Elastic 超网 supernet.py
 (elastic_optimizer)    （读模型 + Elastic 速查 + 最小模板，不展平、不读 optimize_rules）
        ↓
 train_script_gen       生成 train_supernet.py + run_train_supernet.sh
 (supernet-train-script)（内联 _push_chart：训练 loss/acc 实时推图）
        ↓
 search_pipeline_gen    生成 latency_estimator.py / search_config.yaml /
 (nas-search-pipeline)  arch_codec.py / evaluator.py / run_search_supernet.sh / AGENTS.md
        ↓
 runner                 真实执行训练 + 搜索
 (nas-train-runner)     （output_schema 强制 search_records≥1，防假执行）
        ↓
 select                 脚本化挑 top-K 架构（零 LLM）
 (nas-select)           select_and_report.py → nas-select-architecture CLI
                        → final_report.md + 推 C5/C6 图表
```

### 1.3 输入 / 输出

**输入**（6 个；project_root 已下沉给 `model_optimizer` 节点从 model_path 推断，output_dir 暂留 [default]，Phase 4-A 将 sink 到 `$ORCA_ARTIFACTS_DIR`）：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `model_path` | string | — | [ask] 模型入口（如 `model.py`） |
| `output_dir` | path | 推断 | [default] 留空→`llm_artifacts/<model_name>/` |
| `target_hardware` | string | — | [ask] cuda\|npu\|cpu，目标硬件（无目标=瞎搜） |
| `latency_constraint` | string | `""` | [ask] 目标时延(ms)，透传 search_config.yaml 剪枝过时延候选；空=无硬约束 |
| `max_rounds` | string | `20` | [ask] NSGA-II 代数预算闸门（透传 num_generations） |
| `seed` | string | `0` | [ask] 复现性种子，全 workflow 必填 |

**输出**：`final_report.md`（top-K 架构的 width×depth×params×acc×latency 对比）+ 超网训练曲线图 + 搜索 Pareto 散点/选择图。workflow 产出为 `select` 节点的 `result`。

### 1.4 如何激活

```
用 TARS 搜一下这个模型的宽度深度超参
TARS，跑个轻量 NAS，挑几个又小又准的子网
```

匹配命中的关键词：**超参搜索 / 宽度 / 深度 / slim / 轻量 NAS**。等价手动命令：

```bash
orca nas-hp-search --inputs '{
  "model_path": "demo_target/model.py",
  "output_dir": "llm_artifacts/mymodel/",
  "target_hardware": "cuda",
  "latency_constraint": "10.0",
  "max_rounds": "20",
  "seed": "0"
}'
```
（`project_root` 不是 input——agent 从 `model_path` 向上走自动推断。）

---

## 2. 定义

**弹性超网（Elastic Supernet）**。给定离散搜索空间 $\mathcal{S}=\prod_{i}\mathcal{X}_i$（每轴 $\mathcal{X}_i$ 为宽度通道、核大小、深度等离散候选），弹性超网是一个单一参数化网络 $\mathcal{N}_\theta$，其参数 $\theta$ 为最大配置参数的超集，使得任意配置 $\alpha\in\mathcal{S}$ 可经 `set_sample_config` 切换激活切片提取，而无需重新训练。对应的**子网** $\mathcal{N}_\theta^{\alpha}$ 由 `get_active_subnet()` 导出为独立 `nn.Module`，其权重为 $\theta$ 沿通道/核轴的子张量切片；对可变核大小，附加一个小的核变换矩阵 $M_{k_{\text{大}}\to k_{\text{小}}}$，使小核滤波器由大核经学习投影导出，而非简单中心裁剪。

**子网采样**。搜索空间每个 stage 独立采样深度，每个活动位置独立采样 block 配置，均服从均匀分布：

$$\alpha \;=\; \Bigl(\bigl(d_s\sim\mathrm{Uniform}(\mathcal{D}_s)\bigr)_{s},\;\;\bigl\{\,c_{s,p,k}\sim\mathrm{Uniform}(\mathcal{C}_{s,k})\,\bigr\}_{p=1}^{d_s}\Bigr)_{s=1}^{|\text{stage}|}$$

其中 $d_s$ 为 stage $s$ 的深度，$c_{s,p,k}$ 为位置 $p$ 的第 $k$ 维配置（如核大小）。本 workflow 的搜索空间仅含超参轴（宽度、深度、核），block 类型固定。

---

## 3. 背景

### 3.1 NAS 的训练代价

朴素的 NAS 对每个候选架构独立训练评估，其代价随搜索空间规模爆炸而不可行。权重共享（weight-sharing）范式通过训练单一超网、子网继承权重免重训，将搜索代价从「架构数 × 单架构训练」降至「单超网训练 + 子网采样评估」。

### 3.2 相关工作

弹性超网的设计遵循 Once-for-All（Cai et al., 2020）与 BigNAS（Yu et al., 2020）的弹性维度范式：超网在宽度、深度、核大小等轴上弹性化，子网经切片提取。训练阶段采用 FairNAS（Chu et al., 2021）风格的均匀采样子网交替前向-反向，使所有子网在共享权重上被充分激活。搜索阶段采用 NSGA-II（Deb et al., 2002）多目标进化算法，由 EvoX 框架实现。

### 3.3 与本系列其他 workflow 的关系

`nas-agent-pipeline`（NAS-完整）在本 workflow 的超参轴之上增加 block 类型轴（`ChoiceLayer`），并引入 LLM 评估节点；`agent-struct-exploration` 则放弃预设搜索空间，转向由 LLM 提议的开放结构改写。本 workflow 适用于「快速探查超参空间」的场景。

---

## 4. 方法

### 4.1 超网构建

`model_optimizer` 节点读取用户模型，判定其中的卷积/线性层并将其替换为对应的 Elastic 原语（`ElasticConv2d`、`ElasticLinear`、`ElasticBatchNorm2d` 等）。每个原语维护「超网尺寸」参数，并在 `set_sample_config` 时按采样配置切片激活部分通道/核：

- **通道切片**：权重取前 `sample_out_channels` 行、前 `sample_in_channels` 列；
- **核变换**：当存在多种核大小且启用 `use_kernel_transform` 时，注册 $M_{k_{\text{大}}\to k_{\text{小}}}\in\mathbb{R}^{k_{\text{小}}^2\times k_{\text{小}}^2}$，小核权重由大核经该矩阵投影得到；否则采用中心裁剪。约束 `sample_* ≤ super_*` 由断言强制。

由此生成的超网在结构上覆盖整个超参搜索空间，子网提取为权重切片的纯函数操作。

### 4.2 超网训练

`train_script_gen` 生成的 `train_supernet.py` 在每个训练步均匀采样一个子网 $\alpha_t\sim\mathrm{Uniform}(\mathcal{S})$，在共享权重 $\theta$ 上执行前向与反向（in-place 跳层/切片），以交替方式使所有子网在共享权重上被激活。训练 loss/acc 经内联 `_push_chart` 实时推送。该均匀采样策略保证子网间权重分布的均衡，避免部分子网欠训练。

### 4.3 多目标搜索：NSGA-II

搜索由 `nas-search` CLI 驱动，采用离散整数编码的多目标进化算法 `DiscreteNSGA2`。

**编码**。每个架构编码为整数基因 $g\in\mathbb{Z}^{d}$，分量对应搜索空间各离散轴，受界 $lb\le g\le ub$。编解码由生成的 `arch_codec.py` 完成。

**进化算子**：

| 算子 | 操作 |
|---|---|
| 初始化 | 均匀整数种群 $g=\lfloor U\cdot(ub-lb+1)+lb\rfloor$，$U\sim\mathrm{Uniform}(0,1)^{d}$ |
| 交叉 | 均匀整数交叉（配对、每基因以 $0.5$ 概率交换，配对概率 `pro_c=1.0`） |
| 变异 | 随机重置变异（默认概率 $1/d$，被选中基因重置为均匀随机等位基因） |
| 环境选择 | 非支配排序 + 拥挤距离（`nd_environmental_selection`） |

**目标**为 $\min(-\mathrm{acc},\,\mathrm{latency})$（精度取负以统一为最小化）。每代评估后合并父代与子代，按非支配秩与拥挤距离保留 `pop_size` 个体。架构级去重以解码后架构的 JSON 规范化为键，避免重复评估。

**评估**由 `NASProblem` 经 Ray actor 分发：每个 worker 在验证子集上以继承的超网权重运行 `evaluator.evaluate(arch)`（子网免重训），并以 `latency_estimator.get_latency(arch)` 在线测量延迟。若配置了 `latency_constraint` 且延迟越界，则该个体目标置为最差适应度。

### 4.4 选择：高权衡 Pareto 选择器

搜索产出的 Pareto 前沿由 `nas-select-architecture` CLI 经 `HighTradeoffSelector` 选点。算法在归一化目标空间（以理想点/天底点归一化至 $[0,1]$）中：对每个点寻找归一化距离 $\le r$（默认 $0.125$）的邻居，计算其牺牲/增益比，该点的权衡分数取所有邻居中的最小比值；选分数最高的 top-K 个点。该方法偏好前沿拐点——即移动代价最高的点，对应架构设计中最具决策价值的折中位置。

---

## 5. 实验

### 5.1 实验设置

| 项 | 取值 |
|---|---|
| 模板默认空间 | 3 stage，宽度 16/32/64，深度 $(1,2)$，核 $(3,5)$ |
| 搜索算法 | NSGA-II，双目标 $(-\mathrm{acc},\mathrm{latency})$ |
| 子网评估 | 继承超网权重，免重训 |

### 5.2 结果

典型产出为：超网训练若干 epoch → 搜索采样子网并评估 → `select` 选出 top-3 架构，`final_report.md` 给出候选的 width×depth×params×acc×latency 对比表。

### 5.3 计划截图

- **line 图**「超网训练曲线」：横轴=epoch，纵轴=train loss + val acc（双线）。
- **scatter 图**「搜索子网分布」：每个采样子网一个点（横轴=params/latency，纵轴=acc），top-K 高亮。
- **final_report.md**：top-K 架构 Markdown 对比表。

> 标注「📊 计划截图」处为预设图位。原理示意以 ASCII 内联；真实数据图待运行一次 workflow 后由 Web 面板（`orca open`）截取真实图替换。

---

## 6. 局限与延伸

权重共享存在「子网精度受超网训练质量制约」的固有偏差，超网欠训练时子网排序失真。均匀采样对高频出现的小子网存在偏置，必要时可改用 sandwich 采样（每步同时训练最大、最小、随机三个子网）。当超参调优无法触及目标时延/精度时，应转用 `nas-agent-pipeline`（替换 block 类型）或 `agent-struct-exploration`（开放结构改写）。

---

## 附录 A：nas_agent 库接口手册

本 workflow 的超网与搜索能力由 `nas_agent` 库（`/mnt/d/Projects/Orca/nas-agent/`）提供。下列接口供用户在 workflow 之外独立调用。

### A.1 弹性原语：`nas_agent.blocks.primitive_blocks`

```python
from nas_agent.blocks.primitive_blocks import ElasticConv2d, ElasticLinear, ElasticBatchNorm2d

# 弹性卷积：核大小可变时附核变换矩阵
layer = ElasticConv2d(
    super_in_channels=64, super_out_channels=64, kernel_size=7,
    candidate_kernel_sizes=(3, 5, 7), use_kernel_transform=True,
)
layer.set_sample_config(sample_in_channels=32, sample_out_channels=32, sample_kernel_size=5)
out = layer(x)                       # 前向走激活切片
subnet = layer.get_active_subnet()   # 导出独立子网模块
```

同族原语：`ElasticLinear`、`ElasticBatchNorm2d`、`ElasticLayerNorm`、`ElasticRMSNorm`、`ElasticEmbedding`、`ElasticQKVProjector` 等，均提供 `set_sample_config` / `forward` / `get_active_subnet` / `elastic_num_params`。

### A.2 搜索空间与架构配置

`SearchSpace` 与 `ArchConfig` 为每个项目生成的 `@dataclass`（模板见 `workflows/agents/elastic_optimizer/references/supernet_template.py`）：

```python
@dataclass
class SearchSpace:
    stage_names: tuple[str, ...]
    stage_widths: tuple[int, ...]
    stage_depth_candidates: tuple[tuple[int, ...], ...]
    stage_layer_configs: tuple[dict[str, dict[str, tuple]], ...]
    def sample(self) -> ArchConfig: ...   # 均匀随机子网
    def validate(self) -> bool: ...

@dataclass
class ArchConfig:
    stage_depths: tuple[int, ...]
    layer_configs: dict[str, tuple[dict, ...]]
    def validate(self) -> bool: ...
```

### A.3 多目标搜索：`DiscreteNSGA2`

```python
from nas_agent.search.discrete_nsga2 import (
    DiscreteNSGA2, random_integer_population,
    uniform_integer_crossover, random_reset_integer_mutation,
)

nsga2 = DiscreteNSGA2(
    pop_size=32, n_objs=2,
    lb=lb_tensor, ub=ub_tensor,          # 整数基因界
    codec=arch_codec,                     # 基因↔架构编解码（用于架构级去重）
)
# 每代：交叉 + 变异 + 评估 + 环境选择（非支配排序 + 拥挤距离）
```

评估后端 `NASProblem`（`nas_agent.search.problem`）经 Ray actor 并行分发子网评估与延迟测量。

### A.4 CLI 入口

| 命令 | 作用 |
|---|---|
| `nas-search` | 读 `search_config.yaml`，跑 NSGA-II，每代写一行 `search.jsonl`（`generation/gene/objs/cached/pareto/arch`） |
| `nas-select-architecture` | 读 `search.jsonl`，重构 Pareto 前沿，经 `HighTradeoffSelector` 选 top-K，写 `selection_summary.json` 与每架构 `arch_{hash}.json` |

`search_config.yaml` 主要字段：`search_space` / `arch_codec` / `evaluator` / `latency_estimator`（均为 dotted-path 动态加载）、`objs`（如 `["acc","latency"]`，均最小化）、`latency_constraint`、`population_size`、`num_generations`、`concurrency`。

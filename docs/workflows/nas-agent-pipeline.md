# nas-agent-pipeline —— 端到端 NAS（超网 + 组件搜索）

> NAS 三件套里的**完整版**。**回答一个问题：不只是宽深，连 block 用什么组件都要搜，怎么找最优架构？** 同时优化超参（宽/深）+ 模型组件（block 类型），LLM 参与评估选择。

## 1. 一句话定位

输入一个模型，端到端跑完整 NAS：生成 Elastic 超网 → 训练 → 搜索架构（超参 + 组件）→ LLM 评估选择。7 节点，最重但最完整。

## 2. In-session 如何激活

```
用 TARS 做一轮完整的 NAS，连 block 类型也搜
TARS，端到端搜个最优架构，组件和超参都搜
```

匹配命中的 description 关键词：**「端到端 NAS / 超网 / block 组件 / Elastic / 架构搜索」**。

等价手动命令：

```bash
orca nas-agent-pipeline --inputs '{
  "model_path": "demo_target/model.py",
  "project_root": "demo_target/",
  "output_dir": "llm_artifacts/mymodel/"
}'
```

## 3. 输入 / 输出

**输入**（3 个）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `model_path` | — | 模型入口 |
| `project_root` | — | 项目根 |
| `output_dir` | `""` | 空→`llm_artifacts/<model_name>/` |

**输出**：最优架构报告 + 超网训练图 + 搜索结果图 + 选中架构。

## 4. 算法原理

### Elastic 超网（同 slim，回顾）

训一个超网内含多个子网，子网共享权重，训完一次可采样任意子网评估。详见 [`nas-hp-search.md` §4](nas-hp-search.md)。

### full 比 slim 多搜一维：block 组件

slim 只在「同一种 block」上拉宽深。full 连 **block 用什么类型**都搜：

```
slim 搜索空间：   (width, depth)                       ← 同一种 block
full 搜索空间：   (width, depth, block_type)            ← block 也变

block_type ∈ { MBConv, TransformerEncoder, ... }        ← 组件级选择
```

这就是为什么 full 需要 **LLM 评估**（evaluator 节点）：不同 block 类型的架构差异大，纯脚本指标（params/latency）不够，需要 LLM 综合权衡（参数量、时延、精度、结构合理性）做最终选择。

### 7 节点流程

```
model_optimizer        生成完整 Elastic 超网（展平 + 读 optimize_rules，
pytorch-model-optimizer 上下文大，支持 block 组件维度）
        ↓
viz_describe           画超网结构 / 搜索空间描述图
        ↓
train_script_gen       生成 train_supernet.py（内联 chart live 推）
        ↓
search_pipeline_gen    生成搜索脚本（采样含 block_type 的子网）
        ↓
train_runner           真跑训练 + 搜索
        ↓
evaluator              【LLM 评估】综合权衡挑最优架构（full 独有）
        ↓
viz_finalize           画最终结果图 + 报告
```

### 什么时候用 full 而不是 slim

- **用 slim**：你信任当前 block 设计，只想调宽深；要快。
- **用 full**：当前 block 设计本身就是瓶颈，需要换组件类型（比如 CNN block 换成 attention）；愿意付 LLM 评估的代价。

## 5. 结果示例 + 计划截图

**典型产出**：

```
超网训练 → 搜索 (width, depth, block_type) 组合 → LLM 评估 → 最优架构
报告含：参数量 / 时延 / 精度 / block 组成对比
```

### 📊 计划截图（放这里）

- **viz_describe 图**「超网搜索空间」：宽度 × 深度 × block 类型的网格示意。
  > 占位：一张树/网格图，根是超网，分支是各 (width, depth, block) 子网。
- **line 图**「超网训练曲线」：loss + acc live 流。
- **evaluator 输出**「top 候选对比表」：含 block_type 列，LLM 给出选择理由。
  > 占位：Markdown 表格 + LLM 一段「为什么选这个」评语。
- **viz_finalize 图**「最优架构示意」：选中架构的结构图 + 指标卡片。

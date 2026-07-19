# agent-struct-exploration —— LLM agent 结构探索（降时延保精度）

> NAS 三件套里**最激进**的一个。**回答一个问题：靠改超参已经压不到目标时延了，必须改模型宏观结构，怎么办？** LLM agent 直接改模型代码（AST 级），实测时延+精度，循环逼近目标。不依赖超网。

## 1. 一句话定位

输入一个「卡住」的模型（时延降不下来 / 精度到瓶颈），LLM agent 迭代改写模型结构、实测时延与精度，渐进下探到目标（`min latency` s.t. `accuracy ≥ target`）。契约见 [`docs/specs/agent-structural-exploration-design-draft.md`](../specs/agent-structural-exploration-design-draft.md)。

## 2. In-session 如何激活

```
用 TARS 把这个模型的时延压到 100ms，精度别掉太多
TARS，优化模型结构，降时延保精度
```

匹配命中的 description 关键词：**「结构搜索 / 降时延 / 保精度 / 改结构 / AST」**。

等价手动命令：

```bash
orca agent-struct-exploration --inputs '{
  "model_path": "demo_target/vit_tiny_cifar100/model.py",
  "train_command": "python demo_target/vit_tiny_cifar100/train.py",
  "test_command": "python demo_target/vit_tiny_cifar100/train.py --eval",
  "target_latency_ms": "100",
  "accuracy_target": "0.70",
  "max_rounds": "5"
}'
```

## 3. 输入 / 输出

**输入**：

| 参数 | 默认 | 说明 |
|---|---|---|
| `model_path` | — | 模型入口 |
| `train_command` | — | 训练命令（原样 shell 执行，agent 不改训练逻辑） |
| `test_command` | — | 评估命令 |
| `target_latency_ms` | — | 时延目标 |
| `accuracy_target` | — | 精度下界（baseline=无损，baseline−ε=微损） |
| `max_rounds` | — | 最大探索轮数 |
| `iterations` / `struct_scripts_dir` | — | 迭代数 / 结构脚本目录 |

**输出**：champion 架构（满足目标的最佳模型）+ 每轮探索账本 + 时延/精度下探曲线。

## 4. 算法原理

### 与 supernet NAS 的本质区别

supernet NAS（前两个 workflow）前提是「最优解在预设搜索空间内」。struct-exploration 面向**相反场景**：目标无法靠改超参达到，必须**改宏观结构**——这是模型设计层面的突破，搜索空间是开放的（LLM 提结构），只在「原语约束」下。

```
supernet NAS：    固定搜索空间（预设的宽深/block 组合）→ 采样子网
struct-exploration：开放搜索空间（LLM 提任意结构改写）→ AST 改模型代码 → 实测
```

### 探索循环（11 节点，多 path 并行）

```
family_detect       识别模型族（transformer / cnn / mamba...）→ 加载对应 KB 切片
baseline_measure    实测基线时延 + 精度（champion 起点）
   ↓ (每轮 = 一条探索 path，可并行)
┌─ hypothesizer    LLM 提一个结构改写假设（基于 KB + 当前账本）
│  engineer        LLM 把假设落成 AST 级模型代码改写
│  structure_gate  结构合法性门（能 export / forward 通）
│  evaluator       导出 ONNX → cost model 实测时延 → 时延门 → 训练 → 实测精度
│  analyst         LLM 归因（为什么成/败）→ 写回 KB
└─ curator         确定性 reducer：账本追加 / champion ratchet / exploit-explore 路由
   ↓
viz_round          画本轮探索图
... 循环到 champion.latency ≤ target 且 accuracy ≥ target，或 max_rounds
finalize / viz_finalize   出最终报告 + 下探曲线
```

### 四条铁律（不变量，违反 fail loud）

1. **时延永远实测，绝不被 LLM 猜**。时延只来自可替换的 cost model（导出 ONNX 实测）；LLM 只提结构、不报时延数。→ 防止 LLM 编造「我改完应该会快」。
2. **只改模型结构文件，绝不碰训练函数**。`train_command` 原样 shell 执行。→ 训练逻辑是用户领地，agent 不越界。
3. **时延门在前、训练在后**。时延没降到 champion 以下 → 直接记失败、不训练。→ 省掉「时延没改善却白训一轮」的算力。
4. **champion ratchet**：只接受时延更低且精度达标的为新 champion，单调下探。

### KB（外挂知识库，按族索引）

LLM 不是凭空想结构——背后有个知识库（`references/nas/` + 10 篇 LLM-for-NAS 论文 + methods-comparison），按模型族（transformer/cnn/mamba）切片加载。hypothesizer 读 KB 提假设，analyst 把成败经验写回 KB（principles / failures），**越探索越聪明**。

```
KB(common + families/{transformer,cnn,...})  ←只读切片→  hypothesizer 提假设
                                              ←analyst 追加写─  归因经验回写
```

## 5. 结果示例 + 计划截图

**典型产出**：

```
基线: latency 500ms, acc 0.72  →  目标: ≤100ms, acc≥0.70
轮1: 换 attention 变体   → 320ms / 0.71  (champion)
轮2: 减层数              → 180ms / 0.70  (champion)
轮3: 改 patch 化         → 95ms  / 0.70  ✓ 达标，champion
最终: latency 95ms（降 81%）、精度仅掉 2pp
```

### 📊 计划截图（放这里）

- **scatter/line 图**「时延-精度下探曲线」（主图）：每轮一个点（x=latency，y=acc），champion 路径连线，目标区高亮。
  > 占位：散点 + 连线，从右上（基线 500ms/0.72）逐轮向左下（95ms/0.70）下探；用虚线框标目标区（≤100ms & ≥0.70），champion 点染金。
- **bar 图**「每轮时延降幅」：x=轮次，y=latency，看 ratchet 单调下降。
- **table**「探索账本」：轮次 | 改动描述 | latency | acc | 成/败 | analyst 归因。
  > 占位：Markdown 表，含 LLM 归因列（为什么这轮成/败，写回 KB）。
- **KB 增长**（可选）：principles / failures 条目数随轮次增加。

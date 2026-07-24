# transformer 族已知失败结构（降时延反例）

> 用途：Analyst 切片读取 + **持续追加**。本文件列出「想做降时延但实际反而崩」的结构改动——时延没降、精度掉了、不收敛、OOM 都算。每条 4 件套：**结构 / 失败模式 / 原因 / 来源**。
>
> **随 run 由 Analyst 追加**：本文件首版只种子 6 条经典失败，每个 run 里 Analyst 观察到的新失败结构请按同样格式 append 到末尾（标注 run id + 当时的模型规模 / 任务）。

---

## F-01. 纯 Linear Attention 在长程检索任务上精度塌

- **结构**：MHA 换成 `φ(Q)(φ(K)^T V)` 的 Linear Attention，`φ = elu+1` 或 ReLU，无任何 gate / decay。典型代表是原版 Linear Attention（Katharopoulos 2020）、Performer 的早期版本。
- **失败模式**：**精度掉**——不是时延问题，是质量崩。Long Range Arena (LARA) 上的长程检索任务、MQAR (Multi-Query Associative Recall)、需要 softmax 尖锐选择的任务上掉点 10–40%。时延本应下降，但 kernel 不成熟 + 实际短序列反而慢，**两头不讨好**。
- **原因**：
  1. softmax 提供的「尖锐 query-key 选择」丢失——`φ(Q)φ(K)^T` 是低秩 + 平滑的核，无法实现 hard lookup。
  2. 状态 `S_t` 等效于「所有过去 token 的加权平均」，远距离信息被平均稀释。
  3. Recurrence 误差累积——长序列下数值精度衰减。
- **来源**：Arora 2024 *The Dual Form of Linear Attention*；Sinkarapu 2024 MQAR 评测；Arora 2024 *Simple Linear Attention`；ASI-ARCH [alphago-moment] 全文都在围绕这个问题做加 gate / decay / delta rule 的修补——其 106 SOTA 全部基于 Gated DeltaNet 等带门控变体，反证纯 linear 不行。

---

## F-02. 过度剪头（激进 GQA / MQA）导致 K/V 表征 rank 不足

- **结构**：在小模型（< 1B）上直接用 MQA（`n_kv_heads = 1`），或 GQA-8 / GQA-16。
- **失败模式**：**精度掉**——长程检索、翻译质量、MT-Bench 等评测掉 2–8 pp。大模型上 MQA 安全（Shazeer 2019 原论文即验证），小模型上掉得明显。时延确实降了，但质量代价过大。
- **原因**：
  1. K/V 表征的有效 rank 从 `n_heads·d_k` 降到 `n_kv_heads·d_k`——小模型 `d_model` 本来就小，rank 进一步塌缩后无法编码多头视角。
  2. 小模型本身头数就少（4–8 头），共享一个 K/V 等于把头分工能力直接砍掉。
- **来源**：Ainslie 2023 GQA（论文专门讨论 MQA 在小模型上的劣势）；Shi 2023 *The Curse of Low NLayers*；社区复现 TinyLlama MQA 实验。

---

## F-03. Early Token Reduction 删了关键 token

- **结构**：分类 / QA 任务上，前 N 层加 importance predictor 删除 attention 摘要低的 token（DynamicViT / EViT 风格），prune 比例 30–50%。
- **失败模式**：**精度掉 + 偶发 OOD 崩溃**。平均精度可能只掉 1–2 pp，但**对依赖关键 token 的样本崩溃**（foreground 物体被删、QA 的 needle 被删、代码的关键变量被删），方差极大。
- **原因**：
  1. Importance predictor 不可靠——基于 attention 的指标（CLS attention、attention entropy）在前层 attention 还很分散时判不准。
  2. 一旦关键 token 被删，后续无 recover 机制，错误通过残差放大。
  3. prune 比例固定不适应样本难度（简单样本该多删、难样本该少删）。
- **来源**：Bolya 2023 *Token Merging*（讨论 pruning 的失败模式）；Rao 2021 DynamicViT ablation；Menon 2023 *Task-specific Token Pruning*。

---

## F-04. Post-Norm + 深层导致训练不稳 / 不收敛

- **结构**：原始 Transformer 论文的 Post-Norm（`LayerNorm(x + Sublayer(x))`）保留，层数堆到 ≥ 24 或训练步数大。
- **失败模式**：**不收敛**——训练前期 loss 不降、或中途梯度爆炸 / 塌缩。与「降时延」无直接关系，但**它阻塞了「层数减少 + 加宽」这种降时延 move 的反向操作（保持深度 + 加宽）**，间接限制了你不能做 D1 的反向权衡。
- **原因**：
  1. Post-Norm 把残差路径乘上 `1/LayerNorm_scale`，深网络下残差信号被反复缩放，梯度消失或爆炸。
  2. 初始化不当时，前几步 loss landscape 陡峭，optimizer 容易跑飞。
- **来源**：Xiong 2020 *On Layer Normalization in the Transformer Architecture*（理论 + 实证 Post-Norm 不稳）；Wang 2019 *Learning Deep Transformer Models*；Liu 2020 *Understanding the Difficulty of Training Transformers*。**所有主流 LLM（GPT-2 之后）都用 Pre-Norm / RMSNorm 规避**。

---

## F-05. MoE 路由不均 / Expert 死亡

- **结构**：FFN 换成 MoE（E 个 expert + top-k router），**不加载载均衡 loss**或 auxiliary loss 权重太小、router z-loss 缺失、专家初始化不均。
- **失败模式**：
  - **路由坍缩**：少数 expert 收到 80%+ 的 token，其余 expert 饿死 → 等效退化成小 dense 模型，质量没涨。
  - **Expert 死亡**：某些 expert 永远不被选中（梯度不上 → 永远不学 → 永远不被选中，死循环）。
  - **时延没降反升**：单卡上 expert 间切换的 kernel launch + cache miss 常吃掉理论稀疏红利。
- **原因**：
  1. Top-k + softmax 的 self-reinforcing：一开始被选多的 expert 收到更多梯度 → 学得更好 → 更容易被选。
  2. 数据分布偏（代码 vs 自然语言 vs 数学），某些 expert 自然契合。
  3. Router logit 漂移，少数 logit 主导。
- **来源**：Shazeer 2017 *Outrageously Large NNs*（首次提出 aux loss）；Fedus 2022 Switch Transformer（z-loss + load balance）；Mixtral tech report（top-2 + balance loss）；DeepSeek-V2（shared expert + fine-grained routing 缓解）。

---

## F-06. 早期 SwiGLU / GeGLU 在小模型 + 小数据上不掉点反而退化

- **结构**：把标准 GELU-FFN 换成 SwiGLU / GeGLU，但**未把 `d_ff` 从 `4d` 缩到 `8d/3`**——直接保持 `4d` 等于多加一条 gate projection，参数与 FLOPs 双增。
- **失败模式**：
  - **时延升**（与目标相反）——FLOPs 增加 ~50%，没缩 `d_ff` 时纯亏。
  - **小数据上过拟合**：SwiGLU 多的可学参数在小数据集（< 1B token）上反而退化，质量持平或略掉。
- **原因**：
  1. GLU 变体的优势是在「同参数量」下质量更好——前提是缩 `d_ff`。不缩就只是多参数。
  2. SwiGLU 的优势在大数据 / 大模型上才显现（PaLM 540B 验证），小规模无明显收益。
- **来源**：Shazeer 2020 *GLU Variants Improve Transformer*（论文明确说要缩 hidden）；Llama 1/2 / PaLM 实践（缩到 `8d/3 ≈ 2.67d`）；Tay 2022 *Charformer*（小数据 GLU 退化）。

---

## F-07. （预留——Analyst 在 run 中追加）

> 模板（复制修改）：
> ```
> ## F-XX. <失败结构简述>
> - **结构**：<改了什么模块，怎么改>
> - **失败模式**：<时延没降 / 精度掉 / 不收敛 / OOM，具体表现>
> - **原因**：<机理>
> - **来源**：<论文 / run id + 模型规模 / 任务>
> ```

---

## 给 Analyst 的追加指引

1. **只记结构失败，不记超参失败**：学习率没调好不算「结构失败」；某模块设计本身有缺陷才算。
2. **必须区分「时延没降」与「精度掉」**：这两类失败模式对 Hypothesizer 的提示完全不同。
3. **格式严格一致**（结构 / 失败模式 / 原因 / 来源），方便 RAG 检索与对比。
4. **来源级别**：SOTA 工业复现失败 > 顶会论文 ablation > 单次 run 观察。单次 run 观察请明确标 `run=<id> model=<size> task=<name>`，供后续 run 复核。

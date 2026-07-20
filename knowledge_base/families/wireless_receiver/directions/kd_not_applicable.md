# D16 · kd_not_applicable（KD 负面清单：这些方法**不适用** model8）

> 一句话定位：**防后人重复踩坑**——记录经典 KD 方法中对 model8（回归型 OFDM 接收机）**不适用**的方法，附明确原因。hypothesizer 在 Phase2 选方向时**默认排除本卡列出的方法**（meta.json 已标 `ascend=hostile`，被 direction_selection 自动过滤）。

## 不适用清单

### 1. Hinton KL（经典 softmax KD）—— ❌ 不适用
- **原因**：model8 输出是连续 LLR / CSI（回归任务），**无 target class**；KL 散度 `Σ p_t log(p_t/p_s)` 要求 p 是归一化分布（分类 logit softmax）。
- **误用表现**：强行把 LLR 当 logits 跑 softmax → 软目标退化为 argmax，等价于硬标签 MSE；温度 T 无意义。
- **正确替代**：D11 输出级 MSE（CONTRACTS §3 `mse_kd`）。

### 2. DKD（Decoupled KD, Zhao CVPR22）—— ❌ 不适用
- **原因**：DKD 把 KL 拆成 target-class / non-target-class 两部分；回归任务无 class 概念，拆解无定义。
- **正确替代**：D12 关系级 KD（RKD/SP/CC），作为回归任务的 "dark knowledge" 替代物。

### 3. AT（Attention Transfer, Zagoruyko 2017）—— ❌ 不适用（对 conv-only student）
- **原因**：AT 蒸馏 teacher 的 attention map（通常基于 feature 的通道级 L2 范数）；**conv-only student 无 attention map**（D1/D6/D18/D20 都无 attention）。
- **条件适用**：若 student 含 attention（如 D7 windowed axial attn），AT 可用；但本 workflow 的 student 候选默认 conv-only / MLP，所以默认排除。
- **正确替代**：D13 OFD（直接对齐 feature 本身，不需要 attention map 概念）。

### 4. Born-Again Networks（Furlanello ICML18）—— ❌ 不适用（对异结构）
- **原因**：Born-Again 要求 student 与 teacher **同结构同容量**（self-ensemble of identical nets）；model8 的目的是 teacher（6-layer Transformer）→ student（conv/MLP）异结构减容，Born-Again 不解决结构差异。
- **正确替代**：若想用 self-distillation，走 D15 Mean-Teacher EMA（不需要独立 teacher 网络，适合同结构）。

### 5. VID（Variational Information Distillation）—— ⚠️ 不推荐
- **原因**：基于互信息估计，需要拟合变分上界，超参多、训练不稳；回归任务的 feature 信息量本身没分类 logit 那么集中。
- **替代**：D13 OFD（margin-based reconstruction，确定性、超参少）。

### 6. PKT（Probabilistic Knowledge Transfer）—— ⚠️ 不推荐
- **原因**：基于 kernel density 估计的分布匹配，对 batch size 敏感（需大 batch）；昇腾端 batch 受限，方差大。
- **替代**：D12 SP（相似度保持，同样是关系级但用 Gram 矩阵，更稳）。

## 通用判定原则（hypothesizer 决策时用）

| 信号 | 应排除的 KD 方法 |
|---|---|
| 输出是连续值（非 class logit） | Hinton KL / DKD / VID / PKT |
| Student 是 conv-only（无 attention） | AT / NST（Neuron Selectivity Transfer） |
| Student 与 teacher 异结构 | Born-Again / 同构 self-distill |
| Batch size < 32 | PKT / RKD（N 太小 pair-set 噪声大） |
| Teacher 已部署不可用 | 所有 offline KD（走 D15 Mean-Teacher） |

## bundle 的 move
**无**（本卡是负面清单，不引入任何 move）。hypothesizer 在 Phase2 写 SelectionSpec 时**必须**检查 `kd_losses` 字段不含上表禁用项；engineer 实现 SelectionSpec 时若发现含禁用项 → fail loud 回 hypothesizer。

## 结构前提与坑
1. **不要"创新"组合** —— 例如把 KL 套到 LLR 上加温度，看起来有论文支撑但数学上无意义；坚持 D11/D12/D13/D14/D15 的明确适用域。
2. **meta.json 标记** —— 本卡 `ascend=hostile`，被 direction_selection 的 "ascend==hostile 默认排除" 规则自动过滤，不会进入 `{selected}` 列表；只在 hypothesizer 显式读负面清单时加载（作参考）。
3. **可演化** —— 若未来加入含 attention 的 student（如 D7 variant），AT 的 "条件适用" 可升级；更新本卡 + meta.json。

## 来源
- Hinton KL：Hinton et al., 2015 —— [arXiv:1503.02531](https://arxiv.org/abs/1503.02531) "Distilling the Knowledge in a Neural Network".
- DKD：Zhao et al., CVPR 2022 —— [arXiv:2203.08679](https://arxiv.org/abs/2203.08679).
- AT：Zagoruyko & Komodakis, 2017 —— [arXiv:1612.03928](https://arxiv.org/abs/1612.03928).
- Born-Again：Furlanello et al., ICML 2018 —— [arXiv:1805.04770](https://arxiv.org/abs/1805.04770).

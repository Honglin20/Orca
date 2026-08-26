# transformer 族已知高效变体的结构前提

> 用途：Engineer 在落地「结构假设」成可训练代码时切片读取。本文件只回答一个问题——**某个高效变体必须满足什么结构前提才真正高效 / 才保精度？** 不满足前提就装上往往既不快也不准（见 `failures.md`）。

---

## P1. FlashAttention —— IO 前提

- **变体**：FlashAttention / FA-2 / FA-3（Dao 2022 / 2023 / 2024）。
- **结构前提**（**必须满足**才快）：
  1. **不改 attention 数学**——FA 是 exact attention，不是近似。如果你换成 linear/softmax-free，FA 不能直接用。
  2. **GPU 有足够 SRAM 装 tile**：tile size 通常 `Br × Bc ≈ 64–256`，硬件 SRAM 太小（< 192 KB SMem / SM）就退化为朴素实现。
  3. **`d_head` 不能太大**（典型 ≤ 128；FA-2 在 `d_head=256` 时部分场景效率下降），否则中间结果 `O(Br·Bc)` 撑爆 SRAM。
  4. **causal / non-causal 必须显式声明**——FA 的 causal 版本会跳过上半块，速度差 1.5–2×。
- **时延-精度权衡**：零精度损失（数学等价），**纯时延收益**。代价是实现复杂、与自定义 attention（如 sliding-window、relative pos bias）要重写 kernel。
- **来源**：Dao 2022 *Memory-Efficient Exact Attention*；Tri Dao NVIDIA cuDNN backend；FlashAttention-2 paper（Dao 2023）。

---

## P2. GQA / MQA —— 适用前提

- **变体**：MQA（Shazeer 2019）/ GQA（Ainslie 2023）。
- **结构前提**：
  1. **`d_head` 不能太小、`n_heads` 不能太少**：GQA 是「n_heads 上做共享」，如果本来就只有 4–8 头（小模型），强行 MQA 会严重低秩塌缩（见 failures.md "激进 GQA 伤小模型"）。
  2. **decode-heavy 场景才有收益**：GQA 省的是 KV-cache 带宽，训练 FLOPs 收益有限。如果你跑的是大 batch 训练或长 prefill，GQA 几乎不提速；如果是 batch=1 的 chat / agent decode，GQA 提速显著。
  3. **要 retrain，不能 retrofit**：MHA→GQA 不可事后裁剪，必须从头训。原 checkpoint 转 GQA 需 distillation。
- **时延-精度权衡**：典型 `n_kv_heads = n_heads / 4`（GQA-4）几乎不掉点；MQA 在大模型（≥ 30B）上掉点小，小模型上掉点大。Llama-2 70B 用 GQA-8，Llama-3 8B 也用 GQA-4。
- **来源**：Ainslie 2023 *GQA*；Shazeer 2019 *Fast Transformer Decoding*；Llama-2 / Llama-3 tech reports。

---

## P3. Linear Attention —— 精度前提

- **变体**：Linear Attention / Performer / RWKV / RetNet / DeltaNet / Gated DeltaNet / HGRN / Mamba。
- **结构前提**：
  1. **必须配门控**：纯 Linear Attention（`φ(Q)(φ(K)^T V)`）精度塌——softmax 提供的「尖锐选择性」丢失。Gated DeltaNet / RetNet / Mamba 都靠 gate / decay 补回。**纯 Performer / Linear 在召回 (recall) / 关联检索 (associative recall) 任务上系统性偏差**（见 failures.md）。
  2. **hidden `d` 不能太大**：linear attention 的 FLOPs 是 `O(N·d²)`，d 很大时这个常数项会吃掉长序列优势。一般 `d ≤ 128` 时线性优势明显。
  3. **任务要在长序列 (N > 4–8k) 上**：短序列 (N < 2k) linear attention 反而慢（kernel 不如 FA 成熟、常数大）。
  4. **chunkwise + causal mask 实现**：训练期要写 chunkwise 形式才不被 GPU bandwidth 拖死。ASI-ARCH 的 Checker 模块专门检查这一点。
- **时延-精度权衡**：训练 FLOPs `O(N·d²)`、推理 `O(1)/token`、KV-cache 不复存在。长序列 + 解码场景是甜区；短序列 + 长程关联任务是灾区。ASI-ARCH 在线性 attention 子族里挖出 106 个 SOTA（root = DeltaNet），**但这些 SOTA 是 loss/benchmark 维度，作者明确未做 latency benchmark**——所以「线性=快」是结构推理，不是实测。
- **来源**：Katharopoulos 2020；Schlag 2021 DeltaNet；Yang 2024 Gated DeltaNet；Peng 2023 RWKV；Gu 2023 / Dao 2024 Mamba；ASI-ARCH [alphago-moment]。

---

## P4. MoE —— 路由前提

- **变体**：Switch Transformer / GShard / Mixtral 8x7B / DeepSeek-MoE。
- **结构前提**：
  1. **router 必须有 load balancing loss**：纯 top-k + softmax 必然路由坍缩（少数 expert 被喂饱、其余饿死）。Switch / Mixtral 都加 auxiliary loss `L_aux = E·Σ f_i·P_i`（f_i = 每个 expert 收到 token 的比例，P_i = 平均路由概率）。
  2. **`E` 要足够大、`top-k` 至少 2**：top-1（Switch）路由太硬、不均衡风险大；Mixtral 选 top-2 更稳。`E ≥ 8` 才有「条件计算」的规模感。
  3. **要 expert dropout / z-loss**：防止 router logit 过大产生数值不稳。Mixtral 用 router z-loss `L_z = (1/B) Σ log Σ z_i²`。
  4. **多卡 + 大 batch 才吃满**：单卡上 expert 间切换的 kernel launch + cache miss 常吃掉理论稀疏红利。MoE 真正加速的环境是多 GPU all-to-all + 大 batch（训练）或 expert parallelism（推理）。
  5. **tokenizer / 数据要均匀分布**：路由不均很多时候是数据偏（代码 vs 自然语言 vs 数学）。
- **时延-精度权衡**：激活参数量降到 `1/k`，理论 FLOPs 大幅下降；实际单卡 decode 提速常 < 50%（all-to-all 通信 + cache miss）。质量损失主要是「路由不稳导致的优化困难」，靠 balancing loss + z-loss 控制。
- **来源**：Shazeer 2017 *Outrageously Large NNs*；Fedus 2022 Switch Transformer；Jiang 2024 Mixtral；DeepSeek-AI 2024 DeepSeek-V2 MoE。

---

## P5. Sliding-Window Attention —— 适用前提

- **变体**：Longformer / BigBird /本地注意力 / Mistral 7B（SWA=4096）。
- **结构前提**：
  1. **任务要在长 context 上**（N > 4k）：短 context 上 SWA 退化成普通 MHA（W ≥ N），无收益。
  2. **层数要够**：单层 SWA 感受野 = W；多层堆叠后通过残差传递，等效感受野约 `W·n_layers`。层数太少 + 窗口太小 = 看不见远距离。
  3. **最好配 global token**：纯 SWA 在「全局信息聚合」任务（CLS、QA）上偏弱；加 1–2 个 attend-all 的 global token 可显著缓解（Longformer 模式）。
  4. **`W` 要 align FlashAttention tile size**：典型 `W ∈ {512, 1024, 2048, 4096}` 与 FA tile 协同，kernel 才高效；非 2 的幂或 < 128 会让 tiling 退化。
- **时延-精度权衡**：FLOPs / KV-cache 都 `O(N·W·d)`，长 context 下线性于 N（而不是二次）；代价是远距离依赖丢失。Mistral 7B 在 8k–32k context 上质量稳定，是 SWA 的成功示范。
- **来源**：Beltagy 2020 Longformer；Zaheer 2020 BigBird；Jiang 2023 Mistral 7B。

---

## P6. Pre-Norm + 残差 —— 训练稳定性前提（深 transformer 必备）

- **变体**：Pre-Norm（GPT-2 / Llama / T5）vs Post-Norm（原始 Transformer / BERT）。
- **结构前提**：
  1. **层数 ≥ 12 或训练步数大时必须 Pre-Norm**：Post-Norm 在深网络下梯度爆炸/塌缩（见 failures.md "post-norm + 深 → 不收敛"）。
  2. **残差必须是 identity-init**：`x_{l+1} = x_l + f(Norm(x_l))`，残差路径无乘性操作，保证深网络可学。
  3. **RMSNorm 比 LayerNorm 更快**（少一个 mean 减法，FLOPs 略低），Llama 系列默认 RMSNorm。
- **时延-精度权衡**：本身不省 FLOPs，但**是层数变深 / 模型变大的前提**——没有它你做不出深 + 宽的高效变体。RMSNorm 比 LayerNorm 省 ~10–20% norm kernel 时间。
- **来源**：Xiong 2020 *On Layer Normalization in the Transformer Architecture*；Zhang 2019 RMSNorm；GPT-2 / Llama tech reports。

---

## P7. Weight Tying —— Embedding 共享前提

- **变体**：Input/Output embedding weight tying（Press & Wolf 2017）。
- **结构前提**：
  1. **input embedding 与 output projection 共享 `W ∈ R^{V×d}`**：参数从 `2·V·d` 降到 `V·d`。GPT-2、Llama 默认做。
  2. **V 不能远大于 d_model × n_layers**：tying 在 V 大、模型小的情况下会反过来（embedding 主导参数、tied output 限制表达力）。大 V 时常见做法是「input embedding 单独保留 + output 用 LM head 独立但低秩」。
  3. **可选：untied head 配合精调**（Chung 2020）发现 small-untied 在 superGLUE 上略胜，但 LLM-scale 上 tying 仍是默认。
- **时延-精度权衡**：参数量省一半（embedding 维度上），时延本身不变（embedding lookup 不是瓶颈）；主要省显存。质量损失小，但极端任务（V 极大、output 分布偏）上可见。
- **来源**：Press & Wolf 2017 *Using the Output Embedding to Improve Word Embeddings*；GPT-2 / Llama。

---

## P8. RoPE 长度外推 —— NTK / YaRN 前提

- **变体**：RoPE + NTK-aware scaling / YaRN / Position Interpolation。
- **结构前提**：
  1. **base frequency 要重标**（NTK-aware：`θ' = θ · base_scale`；YaRN：分频段不同 scale）。原始 RoPE 在 N > N_train 时精度塌（远距离频率被周期性「wrap」）。
  2. **长 context 微调（long-context FT）**：纯改 base 不够，要在长 context 数据上短训（典型 1000 steps）。Llama-3 长 context 版本、Qwen2-72B-Instruct 都走这条路。
- **时延-精度权衡**：零 FLOPs 代价、显著质量提升；前提是「愿意做 long-context FT」。
- **来源**：Su 2021 RoPE；bloc97 2023 NTK-aware；Peng 2023 YaRN。

---

## P9. KV-cache 量化 —— 兼容性前提

- **变体**：KV int8 / int4 量化（KIVI / KVQuant / Mooncake）。
- **结构前提**：
  1. **K 和 V 要分别处理**：经验上 **K 沿 channel 维量化、V 沿 token 维量化**（KIVI 发现）。混着量化会掉点。
  2. **要保留一小段 prefill 的 FP16 残差**（residual length ≈ 128–256），全量化会伤最近 token 的精度。
  3. **配 GQA 时收益叠加**：GQA 已经缩了 KV，量化再缩 2–4×，长 context 总收益可达 8–16×。
- **时延-精度权衡**：cache 体积 `1/2`（int8）或 `1/4`（int4），decode 期 bandwidth 减半/四；精度损失通常 < 1 pp（perplexity 角度），但极端长 context (>32k) 任务需校准。
- **来源**：Liu 2024 KIVI；Hooper 2024 KVQuant；Moonshot Mooncake。

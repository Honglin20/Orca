# transformer 族降时延结构 move（本族核心）

> 用途：Hypothesizer 切片读取。本文件是「Agent 结构性探索 workflow」第一目标——**降时延**——的可操作 move 清单。每条都是**结构性改动**（改哪个模块、换成什么、拓扑怎么变），不是调超参。Hypothesizer 在组合这些 move 时，至少要回答每个被选用 move 的「精度风险」与「适用前提」。
>
> 类别索引：[Attention 类](#a-attention-类) · [FFN 类](#b-ffn-类) · [嵌入与输出类](#c-嵌入与输出类) · [拓扑与深度类](#d-拓扑与深度类) · [序列长度类](#e-序列长度类) · [稀疏与量化类](#f-稀疏与量化类) · [解码并发类](#g-解码并发类)
>
> 统一格式：**名称 / 结构改动 / 降时延机理 / 精度风险 + 缓解 / 适用·不适用 / 来源**。

---

## A. Attention 类

### A1. MHA → GQA / MQA（共享 KV 头）

- **结构改动**：把 `n_kv_heads` 从 `n_heads` 降到 `n_heads/g`（典型 `g ∈ {4, 8}`）。代码层就是把 `W_K, W_V` 的输出维度从 `[n_heads, d_k]` 改成 `[n_kv_heads, d_k]`，attention 内部把每个 K/V 头广播到 `g` 个 Q 头。MQA 是极端 `g = n_heads`。
- **降时延机理**：**主要降 decode 期 KV-cache 的体积与 HBM 带宽**（按 `1/g` 比例）。decode 是 memory-bound，KV cache 是大头，所以这条几乎是大模型 decode 提速的标配。训练 FLOPs 也小幅下降（K/V projection 小了）但不是大头。
- **精度风险 + 缓解**：
  - 风险：激进 GQA（g > 8）或 MQA 会让 K/V 表征 rank 不足，**长程检索 (associative recall)、翻译质量下降**；小模型尤其敏感（见 failures.md）。
  - 缓解：① 大模型（≥ 13B）用 MQA 安全；小模型用 GQA-4。② 必须从头训，不能 MHA checkpoint 直接裁。③ 若必须迁移，做 distillation 几千 steps。
- **适用 / 不适用**：✅ decode-heavy（chat / agent / 长 context）；✅ 模型 ≥ 7B；❌ 训练 / 大 batch 提速期望（几乎没有）；❌ 小模型（< 1B）+ 长程任务。
- **来源**：Shazeer 2019 MQA；Ainslie 2023 GQA；Llama-2 70B / Llama-3 全系 / Mistral / Gemma。

---

### A2. MHA → Sliding-Window Attention（局部窗口）

- **结构改动**：把 attention 的 `attn_mask` 从全 causal 改成「`|i − j| ≤ W` 且 `j ≤ i`」，W 是窗口大小（典型 512 / 1024 / 2048 / 4096）。可选：给 1–2 个「global token」（CLS、prompt 开头）保留 attend-all。代码层改 `mask` 一行；如果是自定义 attention kernel，要用 Longformer / Mistral 的 SWA kernel。
- **降时延机理**：**FLOPs 从 `O(N²·d)` 降到 `O(N·W·d)`，KV-cache 体积上限 `O(W·d)`——与 context 解耦**。长 context 下两个红利叠加（计算 + 显存），是 32k+ context 能跑起来的关键。配合 FlashAttention 的 tile，`W = 4096` 几乎能跑满。
- **精度风险 + 缓解**：
  - 风险：**远距离依赖丢失**——单层看不见 W 之外，需要靠多层堆叠等效感受野 `W·n_layers`；纯 SWA 在「全局信息聚合」（CLS、QA）任务上偏差。
  - 缓解：① 加 1–2 global token；② 增加层数（与降时延目标冲突，要权衡）；③ 用「混合：底层 SWA、顶层 global」（Longformer 模式）。
- **适用 / 不适用**：✅ N > 4k 的长 context；✅ KV-cache 是瓶颈时；❌ 短 context（W ≥ N 退化成普通 MHA，无收益）；❌ 任务重度依赖远距离强关联（如 needle-in-haystack）。
- **来源**：Beltagy 2020 Longformer；Zaheer 2020 BigBird；Jiang 2023 Mistral 7B（W=4096）。

---

### A3. MHA → Linear Attention（含 DeltaNet / Gated DeltaNet / RWKV / RetNet / Mamba / HGRN）

- **结构改动**：把 `softmax(QK^T)V` 换成 `φ(Q)·(φ(K)^T V)` 的递归形式；引入状态 `S_t`，按 `S_t = gate·S_{t−1} + φ(k_t)v_t^T`（或 DeltaNet 的 delta rule）更新。推理期可走 RNN 模式，每 token `O(d²)` 常数步。代码上整个 attention 类要重写（参考 FLAME 框架 / Mamba 的 `mamba_ssm` 库）；必须 chunkwise 训练。
- **降时延机理**：
  - 训练 FLOPs：`O(N·d²)`（vs MHA `O(N²·d)`），长序列优势显著。
  - 推理：**KV-cache 不复存在**——`S_t` 是固定大小的状态矩阵，每 token `O(1)` 更新（不是线性于 context）。这是对 decode 时延最大的结构性优势。
  - 但常数项大、kernel 不如 FlashAttention 成熟，短序列反而慢。
- **精度风险 + 缓解**：
  - 风险：**纯 Linear Attention 在 associative recall / 长程检索任务上系统性偏差**（softmax 的尖锐选择丢失）。Performer 在 LRA 长程任务上掉点明显。
  - 缓解：① 必须配 gate / decay（Gated DeltaNet / RetNet / Mamba 都是这么做）；② `d` 不要太大（`d ≤ 128` 才有优势）；③ 任务匹配——代码 / 自然语言 LM 适合，强关联检索任务不适合。
- **适用 / 不适用**：✅ 长序列（N > 4k）；✅ decode-heavy；❌ 短序列 + 强关联检索；❌ `d` 很大的模型。
- **来源**：Katharopoulos 2020；Schlag 2021 DeltaNet；Yang 2024 Gated DeltaNet；Peng 2023 RWKV；Sun 2023 RetNet；Gu 2023 / Dao 2024 Mamba；Qin 2024 HGRN；ASI-ARCH [alphago-moment]——在 DeltaNet 子族挖出 106 个 loss/benchmark SOTA，**但论文明确未做 latency benchmark**，"线性=快"是结构推理不是实测。

---

### A4. Cross-Layer KV Sharing（层间共享 KV）

- **结构改动**：相邻几层共用同一组 K/V（典型 2–4 层一组），相当于「同一份 KV cache 被 n_layers / group_size 个 Q 头序列使用」。YOCO（You Only Cache Once, Sun 2023）是代表；MLKV / LayerKV 也走这条路。代码层把每层的 `K_l, V_l` 改成 `K_{l mod g}, V_{l mod g}` 或干脆 `K_0, V_0`。
- **降时延机理**：**KV-cache 体积按 `1/g` 缩，decode bandwidth 同比下降**。比 GQA 更激进（GQA 是头间共享，这是层间共享）。可叠加。
- **精度风险 + 缓解**：
  - 风险：不同层想要不同的 K/V 表示（底层看 lexical、高层看 semantic），强行共享会让深层表达同质化，**深层语义任务（推理、长文档 QA）掉点**。
  - 缓解：① 只在前 N 层共享、后 M 层独立；② 每组保留一个独立的 K/V 头 + 共享 base；③ 配 distillation。
- **适用 / 不适用**：✅ 极长 context（KV cache 是绝对瓶颈）；✅ 层数多（≥ 32）；❌ 浅模型；❌ 深层任务质量优先。
- **来源**：Sun 2023 YOCO；Brandon 2024 MLKV；LayerSkip / LayerKV。

---

### A5. Multi-Head → 数量更少的头（缩 `n_heads`）

- **结构改动**：把 `n_heads` 从 32 缩到 16 或 8，同时把 `d_k = d_model / n_heads` 适当放大（或保持 `d_k` 不变缩 `d_model`）。代码层改 `W_Q, W_K, W_V, W_O` 的 shape。
- **降时延机理**：**降 attention 内部 GEMM 的常数与 kernel launch 开销**。小模型 / 短序列下 attention 矩阵很小，更多头反而被 launch overhead 拖；少头大头更高效。
- **精度风险 + 缓解**：
  - 风险：**头数少 → 多视角能力下降**（典型表现：多项选择、复杂语法）。LQA（low-head）模型在 MT-Bench 等质量评测上偏弱。
  - 缓解：① `d_k` 别太小（≥ 64）；② 配 GQA 时保留 `n_kv_heads ≥ 4`；③ 蒸馏补偿。
- **适用 / 不适用**：✅ 小模型；✅ attention 不是主要 FLOPs 瓶颈时；❌ 大模型 + 多任务（头分工是质量来源）。
- **来源**：Michel 2019 *Are Sixteen Heads Really Better than One?*（头冗余实证）；Voita 2019 head pruning。

---

## B. FFN 类

### B1. FFN → SwiGLU + 缩 `d_ff`（保参数量同时提速）

- **结构改动**：标准 FFN `Linear(d, 4d) → GELU → Linear(4d, d)` 替换为 `Linear(d, h) ⊗ Swish(Linear(d, h)) → Linear(h, d)`，**为了保参数量把 `h` 从 `4d` 缩到约 `8d/3`**（多了一条 gate projection）。这是 Llama / PaLM 的默认。
- **降时延机理**：本身**不是为了降时延**（同参数量反而略增 FLOPs），但有两个间接杠杆：① 若愿意接受小质量损失，可进一步把 `h` 缩到 `2d` 或 `3d` 直接省 FLOPs；② SwiGLU 的 gate 投影可 fuse 成单个 GEMM，kernel 友好。
- **精度风险 + 缓解**：
  - 风险：缩 `h` 到 `< 2d` 时质量明显掉（FFN 是知识存储主体）。
  - 缓解：① 配合 MoE（C2）让稀疏补回容量；② 缩 `h` 时同步降 dropout / 改 RMSNorm。
- **适用 / 不适用**：✅ 已决定用 SwiGLU 的基线再压一档；❌ 期望 SwiGLU 本身就快（不会）。
- **来源**：Shazeer 2020 *GLU Variants Improve Transformer*；PaLM；Llama 系列。

---

### B2. FFN → MoE 稀疏（top-k router）

- **结构改动**：单个 FFN 替换为 `E` 个并行 expert FFN + router。前向：`router_logits = Linear(d, E)(x); top_k_idx, top_k_w = softmax(topk(router_logits, k))`，对每个选中 expert 算 `FFN_e(x)` 并按 `top_k_w` 加权求和。典型 `E ∈ {8, 16, 64}`、`k ∈ {1, 2}`。
- **降时延机理**：**激活参数量降到 `1/k · E·d_ff`**——理论 FLOPs 显著下降（Mixtral 8x7B 实际激活 ≈ 13B 等效 dense，总参数 47B）。前提是 expert parallelism 跑得起来。
- **精度风险 + 缓解**：
  - 风险：① **路由坍缩**（少数 expert 爆满、其余饿死）；② **expert 死亡**（某些 expert 永远不被选中）；③ 单卡 decode 提速常 < 50%（all-to-all 通信 + cache miss）。
  - 缓解：① load balancing loss `L_aux = E·Σ f_i·P_i`；② router z-loss 防数值不稳；③ `k ≥ 2`（top-1 太硬）；④ 专家初始化均匀 + 数据 shuffle。
- **适用 / 不适用**：✅ 多卡 + 大 batch；✅ 想要「总容量大但单 token 计算少」；❌ 单卡小模型（all-to-all 通信吃光红利）；❌ 极小 expert 数（E < 4 退化为 dense）。
- **来源**：Shazeer 2017；Fedus 2022 Switch Transformer（top-1）；Jiang 2024 Mixtral 8x7B（top-2）；DeepSeek-V2（细粒度专家 + 共享专家）。

---

### B3. FFN → 共享 / 低秩 FFN（降 `d_ff` 之外的路）

- **结构改动**：两条可选——
  - (a) **跨层 FFN 共享**：相邻 2–4 层共用一个 FFN（ALBERT 思路在 LLM 上的延伸，如 CruCarpet / LayerShare）。
  - (b) **低秩 FFN**：`Linear(d, 4d) → Linear(4d, d)` 拆成 `Linear(d, r) → Linear(r, 4d) → Linear(4d, r') → Linear(r', d)`，`r << d`。LoRA-FFN 思路。
- **降时延机理**：(a) 跨层共享：**FFN 参数与 FLOPs 减半**（按 group 比例），decode 期 FFN 是 memory-bound 主体。(b) 低秩 FFN：FLOPs 显著下降，但 kernel 不友好（多个小 GEMM 拼接）。
- **精度风险 + 缓解**：
  - 风险：(a) 层间表达同质化；(b) 低秩瓶颈限制 FFN 容量，知识存储能力下降。
  - 缓解：(a) 只共享前几层 / 配 residual；(b) 用 MoE 替代低秩（C2）。
- **适用 / 不适用**：✅ FFN 是参数 / 带宽主瓶颈；❌ 任务质量对 FFN 容量敏感。
- **来源**：Lan 2020 ALBERT（共享）；LoRA 思路（Hu 2021）；DeepSeek-V2 的 shared expert 思路。

---

## C. 嵌入与输出类

### C1. 共享 Input/Output Embedding（weight tying）

- **结构改动**：把 `embedding[V, d]`（lookup）与 `lm_head[d, V]`（output projection）共享同一份 `W`。代码层 `lm_head.weight = embedding.weight`。GPT-2 / Llama 默认做。
- **降时延机理**：**主要是省显存**（embedding 参数从 `2Vd` 降到 `Vd`），时延几乎不变。但若 V 极大（≥ 100k），embedding lookup 不是瓶颈，LM head projection 在大 V 下反而是 GEMV 瓶颈——tied 后两条路径走同一个 kernel，cache locality 略好。
- **精度风险 + 缓解**：
  - 风险：tied head 把「输入表示」与「输出预测」绑定，**极端任务（V 极大 + output 分布偏）掉点**。
  - 缓解：① 大 V 时只 tie 一半（Chung 2020 small-untied 思路）；② 在 LM head 前加一层小 MLP 解耦。
- **适用 / 不适用**：✅ V ≤ 100k 的标准 LLM；❌ V 极大 + 多语言 / 代码混合（tied 容易偏）。
- **来源**：Press & Wolf 2017；GPT-2 / Llama。

---

### C2. 缩 `d_model` 或 缩 `V`（直接缩维度）

- **结构改动**：
  - (a) 缩 `d_model`：全模型 hidden 维度，所有 attention/FFN FLOPs 都按 `d²` 缩。
  - (b) 缩 `V`：合并低频 token、用 BPE drop、或者直接换更小 vocab 的 tokenizer。
- **降时延机理**：(a) FLOPs `O(d²)` 缩、KV-cache `O(d)` 缩，**全局最快的一招**但代价是容量。(b) LM head 的 GEMV `O(V·d)` 直接缩——大 V 模型（V = 128k）decode 期 LM head 占 ~20% 时间。
- **精度风险 + 缓解**：
  - 风险：(a) 缩 `d` 直接掉表达力，质量明显降；(b) 缩 V 损失稀有 token 表示（多语言、代码）。
  - 缓解：(a) 配层数减少（D1）保参数量；(b) 用 BPE drop / SentencePiece + 保留稀有 token。
- **适用 / 不适用**：✅ 容量充足想压速度；❌ 质量优先；❌ 多语言任务缩 V。
- **来源**：Touvron 2023 Llama；LLM-NAS [llm-nas]——论文 ViT 空间 FLOPs 推导发现 `d_model`（Embed Dim）和 `n_layers`（Depth）是 FLOPs 主导项，可作为 NAS 搜索空间划分轴。

---

## D. 拓扑与深度类

### D1. 层数减少 + 残差保精度

- **结构改动**：把 `n_layers` 从 L 降到 L/2，同时把 `d_model` 适当放大（保参数量）或保持（缩模型）。**必须配 pre-norm + 残差 identity**（见 patterns.md P6）才能稳训。
- **降时延机理**：**降顺序依赖步数**——decode 期每 token 要顺序过 `n_layers` 层，层数减半直接减半 sequential depth，wall-clock 几乎线性下降。训练期也按层数线性。FFN/attention 是按层叠加的，少层 = 少 kernel launch。
- **精度风险 + 缓解**：
  - 风险：**深度不足 → 抽象层级缺失**，复杂任务（推理、数学）掉点。注意 EvoPrompting [evoprompting] 在 CNN 上发现「narrower + deeper」倾向更好——transformer 上经验更复杂，但同样存在「质量-层数」强耦合。
  - 缓解：① 配 MoE（C2）让宽度补容量；② pre-norm 保证深层也能训；③ drop layer / early exit（D3）动态调度；④ 用 More Layers Distillation 从深 teacher 蒸到浅 student。
- **适用 / 不适用**：✅ decode 期 wall-clock 优先；✅ 配合宽度补偿；❌ 复杂推理任务（GSM、MATH）单砍层数。
- **来源**：EvoPrompting [evoprompting]（CNN 上更深更窄倾向）；LAPT [design-principle-transfer]（层数-算子联合搜索）；Tale of Two Networks（width vs depth）。

---

### D2. Patchify / 大 Patch（CVT / ViT 早期降采样）

- **结构改动**：把 input tokenizer 的 patch size 从 `16×16` 调到 `32×32` 或 `conv stem stride` 加大。语言模型对应：BPE drop / char-level → word-level。**仅适用于有空间冗余的输入（图像 / 长文档）**。
- **降时延机理**：**N 直接缩**（patch 32×32 vs 16×16 → N 变 1/4），attention `O(N²)` 收益是二次的。
- **精度风险 + 缓解**：
  - 风险：**丢细节**——小物体检测、细粒度分类、近距 token 关联受损。
  - 缓解：① conv stem（前几层用小 patch conv 提特征，再 patchify）；② hierarchical（如 Swin、PvT）。
- **适用 / 不适用**：✅ 高分辨率输入；❌ token-level NLP（BPE 已经是无损最小单位）。
- **来源**：Dosovitskiy 2021 ViT；Swin Transformer；PvT。

---

### D3. Early Exit / Adaptive Depth（动态深度）

- **结构改动**：每层（或每 K 层）接一个浅 classifier / confidence head，前向时若 confidence > 阈值则提前返回（不继续往后层）。代码层加 per-layer exit head + 训练时多 exit loss。LayerSkip、DepthAdaptive Transformer、CALM（Confident Adaptive Language Modeling）是代表。
- **降时延机理**：**简单 token 走浅层、复杂 token 走深层**，平均层数下降，decode 提速 1.5–3×。是「难样本感知」的 compute allocation。
- **精度风险 + 缓解**：
  - 风险：① confidence head 校准差 → 早出错的 token 直接烂掉；② 与 KV cache 协同复杂（早出的 token 不再有后续层的 K/V 写入 cache）。
  - 缓解：① 配 distillation 让浅层输出 close to 深层；② 用 ensemble 多 exit + 加权；③ CALM 用「shallower decoder + rejection sampling」对齐深层质量。
- **适用 / 不适用**：✅ token 难度分布广（chat / 代码）；❌ 任务要求每 token 同等精度；❌ 与 speculative decoding 同时用会冲突（都要主流模型 verify）。
- **来源**：Schuster 2022 CALM；Elhoushi 2024 Layer Skip；Xin 2020 Depth-Adaptive Transformer。

---

## E. 序列长度类

### E1. Early Token Reduction / Token Pruning

- **结构改动**：在特定层（典型前 1/3）加一个 importance predictor（基于 attention entropy / CLS attention / learnable head），删掉低重要性 token（直接 mask + 不再参与后续 attention / FFN）。DynamicViT、EViT、A-ViT 是代表。
- **降时延机理**：**N 下降**——后续所有层 attention `O(N²)` + FFN `O(N)` 都按 pruned N 计算。prune 30% → 后续 FLOPs 减半左右。
- **精度风险 + 缓解**：
  - 风险：**删错关键 token**——分类任务的 foreground、QA 的 needle、代码的关键变量被删就完了（见 failures.md）。
  - 缓解：① 用 attention rollout 而非单层 attention 判重要性；② 保留 prompt token / question token 强制不删；③ prune 比例渐进（前层多删、后层少删）；④ 加 recover head 重建被删 token。
- **适用 / 不适用**：✅ 分类 / 检测（前景-背景冗余大）；❌ 生成式任务（每个 token 都是输出，不能删）；❌ needle-in-haystack 检索。
- **来源**：Rao 2021 DynamicViT；Bolya 2023 Token Merging / Pruning；Liang 2022 EViT。

---

### E2. Token Merging（ToMe）

- **结构改动**：在特定层把「相似 token」用 bipartite soft matching 找对，加权合并成 r 个新 token。与 pruning 不同——不丢信息，是降采样。ToMe（Bolya 2023）默认每层合并 `r = N/16` 个。
- **降时延机理**：同 E1，N 下降；但**信息丢失比 pruning 少**（合并而非删除）。训练 + 推理都受益。
- **精度风险 + 缓解**：
  - 风险：合并不同语义的 token 会产生「平均化」噪声；生成式任务下被合并 token 的位置信息丢失。
  - 缓解：① 用 attention key 而非 token 本身算相似度；② 不合并特殊 token（CLS、sep）；③ 后期层不合并（信息已抽象）。
- **适用 / 不适用**：✅ 分类、dense prediction（分割）；✅ 训练 + 推理双赢；❌ 生成式 LM（合并破坏 autoregressive 一致性）。
- **来源**：Bolya 2023 *Token Merging: Your ViT but Faster*（CVPR）；后续 ToMeSD 扩展到 diffusion。

---

### E3. Context Packing / Variable-Length Batching

- **结构改动**：训练时把多个短样本 pack 进一个固定长度的 sequence（用 attention mask 隔开），不再 padding。Llama / GPT-3 训练栈默认做；Patch n' Pack (NaViT) 是 CV 版本。
- **降时延机理**：**降训练期 padding 浪费**——同 batch 内长短不一时 padding 占 30–60% FLOPs，packing 直接省掉。**推理期无效**（生成是单 stream）。
- **精度风险 + 缓解**：
  - 风险：跨样本 attention 漏 mask 会导致信息泄露（灾难）。
  - 缓解：① 严格 cross-document mask；② FlashAttention 的 varlen API 直接支持。
- **适用 / 不适用**：✅ 训练（不同长度样本混合）；❌ 推理；❌ 单 stream 自回归。
- **来源**：Korthikanti 2023 Megatron-LM；Dehghani 2024 NaViT (Patch n' Pack)。

---

## F. 稀疏与量化类

### F1. KV-cache 量化（int8 / int4）

- **结构改动**：把 cache 的 `K_t, V_t` 从 fp16 改成 int8 或 int4，配 per-channel / per-token scale。**KIVI 发现：K 沿 channel 量化、V 沿 token 量化最稳**。保留最近 128–256 token 的 fp16 残差。
- **降时延机理**：**decode 期 KV cache 是 memory-bound 主体**，量化把 cache 体积与 bandwidth 同步砍半（int8）/ 砍 3/4（int4）。长 context 收益最大。
- **精度风险 + 缓解**：
  - 风险：极端长 context（>32k）+ 需要精细 retrieval 时精度损失放大；K 量化比 V 量化更敏感。
  - 缓解：① KIVI 的「K 沿 channel」是经验最稳配置；② 保留 residual length；③ 配 GQA 时收益叠加。
- **适用 / 不适用**：✅ 长 context decode；✅ 显存紧张；❌ 训练；❌ 极小模型（量化 noise 占比大）。
- **来源**：Liu 2024 KIVI；Hooper 2024 KVQuant；Mooncake（Moonshot）。

---

### F2. Weight 量化（W8A16 / W4A16 / W4A8）

- **结构改动**：模型权重存 int8 / int4，前向时按 group scale 反量化到 fp16 计算（WxA16 模式）。GPTQ / AWQ / SmoothQuant 是离线量化算法。代码层替换 Linear 为 WQuantLinear。
- **降时延机理**：**decode 期 memory-bound，权重从 HBM 读入 SRAM 是瓶颈**——权重小一半，bandwidth 减半，提速 1.5–2×。训练期无效（反向传播要 fp16 权重）。W4A16 比 W8A16 更快但精度风险大。
- **精度风险 + 缓解**：
  - 风险：W4 精度损失 0.5–2 pp（perplexity）；激活 outlier channel 会爆。
  - 缓解：① AWQ 的 activation-aware weight scaling；② SmoothQuant 把 outlier 从激活迁到权重；③ 留 1% high-precision outlier channel（OWL）。
- **适用 / 不适用**：✅ decode（权重读 bandwidth 主导）；❌ 训练；❌ 已是极小模型（量化 noise 占比大）。
- **来源**：Frantar 2023 GPTQ；Lin 2024 AWQ；Xiao 2023 SmoothQuant；Dettmers 2022 LLM.int8()。

---

### F3. Activation Sparsity（ReLU / TopK 激活）

- **结构改动**：把 SwiGLU 的 Swish 换成 ReLU² 或 Top-K（取激活的 top-k 通道，其余置零）。SwitchBase / ReLU² models（Mirzadeh 2023）走这条路。代码层激活函数一行改动 + 配稀疏 kernel。
- **降时延机理**：激活稀疏后，FFN 后半段 `Linear(h, d)` 可跳过 zero 通道，理论 FLOPs 减半（若 50% 稀疏）；要 sparse kernel 才能变现。
- **精度风险 + 缓解**：
  - 风险：标准 dense kernel 在稀疏输入下没加速，必须 sparse GEMM；过度稀疏 → 表达力塌。
  - 缓解：① 渐进 sparsity（前期训 dense 再过渡）；② top-k 而非 hard ReLU；③ 配 MoE 让 expert 内部稀疏。
- **适用 / 不适用**：✅ 已有 sparse kernel / 自定义 CUDA；❌ 通用 dense GPU kernel（无收益）。
- **来源**：Mirzadeh 2023 *ReLU Sparks the Transformer*；SwitchBase。

---

## G. 解码并发类

### G1. Speculative Decoding（draft + verify 结构）

- **结构改动**：加一个独立的小 draft model（同 tokenizer、参数量约主模型 1/10），由 draft 自回归生成 K 个候选 token，主模型一次前向 verify 这 K 个 token（并行计算 K 个位置的 logits），接受与主模型分布一致的前缀。代码层新增 draft model 前向 + verify loop。
- **降时延机理**：**wall-clock 大降**——主模型每步只跑 1 次前向就出 K 个 token（接受率 70% 时等效 K=1.7×）。**FLOPs 反而略增**（draft + verify），但**时延降**。这是「latency 不是 FLOPs」的经典示范。
- **精度风险 + 缓解**：
  - 风险：① draft 与主模型分布差太多 → 接受率低 → 反而变慢；② 加载两份模型显存翻倍。
  - 缓解：① 用同家族小模型（Llama-70B + Llama-7B）；② EAGLE / Medusa 用 tree-based drafting 提高并发接受率；③ draft 与主模型共享 KV cache（DeepSpec）。
- **适用 / 不适用**：✅ decode-heavy（chat / agent）；✅ 主模型分布「平滑」（draft 易追上）；❌ 创意写作 / 数学（主模型分布尖锐，draft 接受率低）；❌ 显存极紧。
- **来源**：Leviathan 2023 *Fast Inference from Transformers via Speculative Decoding*；Chen 2023 *Accelerating LLM with Speculative Sampling*；Cai 2024 Medusa；Li 2024 EAGLE。

---

### G2. Continuous Batching / PagedAttention

- **结构改动**：把 KV cache 切成固定大小的 page（如 16 token / page），按需分配而非预先 contiguous。每 iteration 重新组 batch（短请求走了就立刻塞新请求进来）。代码层 vLLM / TGI 默认。
- **降时延机理**：**降等待 + 降显存碎片**——batch 内不同请求的 decode 步数不同，naive batching 会 padding 浪费；continuous batching 让所有 slot 永远满载。Throughput 提速 2–10×（不是单请求 latency）。
- **精度风险 + 缓解**：零精度损失（纯调度 + 显存管理）。
- **适用 / 不适用**：✅ serving 多请求；❌ 单请求 latency（不变）；❌ 边缘 / 嵌入式（资源固定）。
- **来源**：Kwon 2023 vLLM / PagedAttention；TensorRT-LLM；Sarathi-Serve（chunked prefill）。

---

## 选用提示（给 Hypothesizer）

1. **先定位瓶颈**：训练 vs decode？compute-bound vs memory-bound？长 context vs 短？瓶颈不同 move 收益天差地别。
2. **Move 之间有依赖 / 冲突**：
   - GQA（A1）+ KV 量化（F1）+ SWA（A2）三叠加 = 长 context decode 最强组合（Mistral / Mixtral 验证）。
   - Linear attention（A3）与 KV 量化（F1）冲突——前者没 KV cache，后者无对象。
   - MoE（B2）与 weight 量化（F2）兼容但调试复杂。
   - Early exit（D3）与 speculative（G1）都「reuse 主流模型前向」，需小心调度避免冲突。
3. **「FLOPs 降 ≠ latency 降」**：speculative（G1）是反例（FLOPs 增、latency 降）；MoE（B2）是正例（FLOPs 降、latency 不一定降）。要看 wall-clock。
4. **来源分级**：SOTA 工业实践（Llama / Mistral / Mixtral）> 顶会论文 > ASI-ARCH 这类探索论文（结构新颖但 latency 未实测）。

# 通用结构原语（common/primitives.md）

> 用途：Engineer 在把假设渲染成可训练代码时用的"原语速查"。每条只回答 4 个问题：**是什么** / **归纳偏置** / **时延特性** / **典型用途**。
> 族专属原语（MHA / linear-attn / GQA / DW-sep 的高阶组合等）见 `families/<name>/primitives.md`；本文件只列跨族通用的低阶原语。

---

## 1. 残差 / 跳连（residual / skip connection）

- **是什么**：`y = F(x) + x`，把输入绕过若干算子直接加到输出。
- **归纳偏置**：让"恒等映射"是默认解，主路径只需学"修正"。深度可扩展到 100+ 层（ResNet、Transformer）。
- **时延特性**：本身是 elementwise add，几乎免费；但 attention/conv 主路径必须保留输入张量做加法，activation 不释放，**略微增加 peak activation 显存**。投影残差（projection shortcut，1×1 conv 升维）会引入额外 GEMM。
- **典型用途**：所有 depth ≥ 6 的 stack 必备；U-Net 的 skip 用于 spatial 信息回灌；Transformer 每层默认带残差。

## 2. 1×1 conv / 线性投影（projection）

- **是什么**：kernel size = 1 的卷积，等价于每个 spatial 位置做一个共享的 matmul（CNN）；Transformer 的所有 `nn.Linear` 是其等价物。
- **归纳偏置**：纯 channels 间线性混合，不带 spatial 偏置；用于升/降 channel。
- **时延特性**：GEMM-friendly，是 CNN 中**最吃硬件利用率**的算子； Fusion 后通常与 BN+activation 合并成单 kernel。
- **典型用途**：bottleneck 的升降维（见 §3）；channel 数对齐（residual projection）；attention 的 Q/K/V/O proj；MLP 的 up/down proj。

## 3. Bottleneck block（先降 → 主算 → 升）

- **是什么**：`1×1 降维 → 3×3 主计算 → 1×1 升维`（ResNet-50 bottleneck）；Transformer 的 `up_proj → activation → down_proj` 是同构。
- **归纳偏置**：在低维做主 spatial/序列计算，相当于对通道做低秩约束；精度损失小、FLOPs 大幅下降。
- **时延特性**：在低维算 3×3 / attention 显著降 FLOPs 与访存；但 1×1 的开销随 reduction ratio 变化，太激进 reduction 反而退化。
- **典型用途**：ResNet-50/101、EfficientNet 的 MBConv、Transformer FFN（expansion ratio 4× 是经验值）。

## 4. 归一化层（Normalization）

| 原语 | 归一化轴 | 归纳偏置 | 时延特性 | 典型用途 |
|---|---|---|---|---|
| **BatchNorm (BN)** | 沿 (N, H, W) 对每个 channel 归一化 | 引入 batch 内统计分布偏置；推理可折叠为 scale+bias（与 conv 融合，**免费**） | 训练需 running stats，与 batch 强耦合；推理几乎 0 开销 | CNN + 大 batch 训练（ResNet 系列） |
| **LayerNorm (LN)** | 沿 channel 维（对每样本独立）归一化 | 无 batch 依赖；适合变长序列 | 每 token 一次 mean/var 计算，**不能融合** matmul；FP16 下要小心数值稳定 | Transformer（标准配置） |
| **RMSNorm** | LN 去掉 mean，只算 RMS 缩放 | 同 LN 但少一步减均值 | 比 LN 略快（少一次 mean-reduce）；无融合 | 现代主流 LLM（LLaMA、Mistral、Mixtral） |
| **GroupNorm (GN)** | channels 分组，组内沿 (H,W) 归一化 | 无 batch 依赖，分组引入轻度 channel-level 偏置 | 类似 LN 不能融合 | 小 batch CNN（detection/segmentation）、扩散模型 U-Net |

**位置偏置**：pre-norm（norm 在主算之前）让主路径恒等，深层稳定（详见 `principles.md` §5）；post-norm 浅模型精度略好但深层不稳。

## 5. 激活函数（activation）

| 原语 | 公式 | 归纳偏置 | 时延特性 | 典型用途 |
|---|---|---|---|---|
| **ReLU** | `max(0, x)` | 稀疏激活（负值全 0）；分段线性 → 量化友好 | 极便宜，可融合到 conv/linear | CNN、早期 Transformer、INT8 部署 |
| **GELU** | `x·Φ(x)` | 平滑 ReLU；非分段线性 | 比 ReLU 略贵（含 erf/exp）；INT8 量化不如 ReLU 友好 | BERT、ViT、原始 Transformer |
| **SiLU/Swish** | `x·sigmoid(x)` | 自门控，平滑非单调 | 比 GELU 略快；INT8 友好性中等 | 现代大模型（LLaMA、Mistral）、EfficientNet |
| **GeGLU / SwiGLU** | `(linear_a activation) ⊙ linear_b` | 带 gated 线性单元的 FFN 变体（见 §6） | 多一个 linear，但精度通常更好 → 同精度下时延更低 | 现代 LLM FFN（LLaMA 系、PaLM） |

**默认**：CNN + INT8 部署 → ReLU；现代 LLM → SiLU / SwiGLU；学术基线对比 → GELU（与 BERT/ViT 可比）。

## 6. 门控（gating）

- **是什么**：`y = a ⊙ σ(b)`，其中一个分支做"门"控制另一分支的通行。变体：GLU（`a ⊙ sigmoid(b)`）、GeGLU（`a ⊙ GELU(b)`）、SwiGLU（`a ⊙ SiLU(b)`）、 highway、GRU/LSTM 风格的门控。
- **归纳偏置**：让模型自适应决定哪些 channel/特征通过；隐式稀疏化与 conditional computation。
- **时延特性**：多一个分支的 linear → 参数 +FLOPs，但**通常同精度下时延比"无门控 FFN 更宽"低**（LLM 中 GeGLU/SwiGLU 经验上比普通 MLP-ReLU FFN 更省）。门控的 elementwise mul 不能融合到 matmul。
- **典型用途**：现代 LLM 的 FFN（`{Ge,Si}GLU`）；RNN 的 input/forget/output gate；多模态融合的 conditional gating。

## 7. Separable / Depthwise conv

- **是什么**：`depthwise conv`（每 channel 一个独立 kernel）+ `pointwise 1×1 conv`（channels 混合）；MBConv 在前后加 expansion-projection 形成"expansion-DW-proj-SE"。
- **归纳偏置**：spatial 与 channel 解耦学习；spatial 偏置强（local, translation-invariant），channel 间混合留给 pointwise。
- **时延特性**：FLOPs 大幅下降，但 **depthwise 是 memory-bound**（参数少 activation 多）；GPU 上常不如标准 conv，**edge CPU/嵌入式上才是 win**。Group conv + channel shuffle（ShuffleNet）缓解通道不流动。
- **典型用途**：移动端 CNN（MobileNet 全家、EfficientNet、MobileViT）；CNN 大模型 stem；视觉 transformer 的 patch embed。

## 8. MoE routing（mixture-of-experts）

- **是什么**：N 个并行 expert（通常每个是 FFN），router（小 linear+softmax）按 token 选 top-k expert（典型 top-1 / top-2）。
- **归纳偏置**：条件计算 → 容量随 expert 数线性增长，激活 FLOPs 仅随 top-k 增长；router 隐式学专家专门化。
- **时延特性**：参数 +N×，激活 FLOPs ≈ +k/N×；显存压力大（所有 expert 常驻）；多卡 all-to-all 通信是分布式场景主要开销；单卡部署只有当权重能装下时才划算。
- **典型用途**：大模型扩容（Switch Transformer、GShard、Mixtral 8×7B、DeepSeek-MoE）；多任务路由。**小模型（< 1B）通常不划算**。

## 9. 位置编码（Positional Encoding, PE）

| 原语 | 形式 | 归纳偏置 | 时延特性 | 典型用途 |
|---|---|---|---|---|
| **Absolute (sin/cos or learned)** | `x += PE[pos]` | 绝对位置先验；外推性差（learned 不能外推到训练长度） | 加法，几乎免费；learned 需存 `max_len × dim` 表 | 早期 Transformer、BERT、ViT |
| **Relative** | attention 里加 `b_{i−j}` 偏置（T5 relative bias、T5 relative attention） | 用相对距离作偏置；外推性好 | 每对 query-key 加 bias，需查表；矩阵大小不变 | T5、部分 encoder |
| **RoPE**（Rotary Position Embedding） | 在每对 (q, k) 上做角度为 `pos·θ_i` 的旋转 | 相对位置编码隐含在 q·k 内积里；外推友好（可用 NTK-aware scaling） | 旋转是 elementwise mul，可融合；推理与 KV cache 完全兼容 | **现代 LLM 主流**（LLaMA、Mistral、Qwen、DeepSeek） |
| **ALiBi** | attention logit 加 `−m·|i−j|` 线性偏置 | 距离越远衰减越大；强烈 local 偏置；外推到比训练长很多的 seq 仍稳定 | 几乎免费（每对 query-key 一个加法） | 长上下文外推（BLOOM、MPT） |

**默认**：现代 decoder LLM → RoPE（外推性 + KV-cache 友好）；超长上下文 → RoPE + NTK scaling，或 ALiBi；encoder/ViT → learned absolute 或 no PE（patch index 已含顺序）。

## 10. Pooling（下采样的纯降维版）

- **是什么**：`max_pool` / `avg_pool` / `adaptive_pool` / attention pool。无参数，纯降 spatial。
- **归纳偏置**：translation invariance；max 偏向显著特征，avg 偏向平滑特征。
- **时延特性**：max/avg pool 是 memory-bound，参数 0 但要读全 activation；**LLM-NAS 的 Co-evolve KB 自动归纳出**："avg_pool takes a long time and has limited accuracy improvement"——这是 NAS 实验中真实观察到的结论，慎用。
- **典型用途**：CNN stem 后下采样；分类 head 前的全局池化（GAP）；ViT 的 adaptive attention pool（CLSToken）。

## 11. Attention（跨族通用骨架；族专属变体见 `families/transformer/primitives.md`）

- **是什么**：`softmax(QK^T/√d)·V`；本质是"内容寻址的加权平均"。
- **归纳偏置**：**无 inductive bias on locality**（全局 attention），需要数据量补；sliding-window / local attention 注入 locality 偏置。
- **时延特性**：FLOPs 对 seq² 敏感（长上下文瓶颈）；KV cache 是 autoregressive 推理时延的主要来源；FlashAttention 把中间 N×N matrix 不落 HBM，**显著降访存**。
- **典型用途**：Transformer 全家；ViT；跨模态融合；与 conv / RNN 混合（CoAtNet、Universal Transformer）。

---

## Engineer 渲染规则

- 选原语时**先查 `principles.md`**：例如提"加 depthwise conv" 前先看 latency_heuristics §7 是否在目标硬件上成立。
- 非主流原语（如新的 GLU 变体、自定义 RoPE scaling）必须在假设里标注"族专属"或"实验性"，让 Analyst 在验证时重点关注。
- 同一原语在不同族上的实现差异（如 BN 在 CNN vs LN 在 Transformer）要落到 `families/<name>/primitives.md`，不在本文件展开。

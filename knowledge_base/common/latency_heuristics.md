# 通用降时延手法（common/latency_heuristics.md）

> 用途：Hypothesizer 从本文件挑手法组合成"降时延假设"的弹药库；每条都是**结构性**改动（改图、改通道、改算子组合），而不是"减 FLOPs 就完事"。第一条（FLOPs ≠ latency）是认知基，必读。
> 每条格式：**手法** / **降时延机理** / **精度风险** / **适用场景**。

---

## 0. 前提：FLOPs ≠ latency（详见 `principles.md` §1）

降时延必须先看**访存量 / 算子数 / 对齐 / seq_len**，不是 FLOPs。下面每条手法都标注主要命中哪一项。

---

## 1. 算子融合（conv+bn+act / qkv proj / scale+bias）

- **手法**：把连续的 elementwise / 缩放算子合并到前一个 GEMM/conv kernel 里：conv+bias+BN+ReLU 融合为单 kernel；Q/K/V 三个小 GEMM 融合为一个大 GEMM（`concat(q,k,v)` 后一次性 matmul）；softmax 后的 scale 与 mask 融合。
- **降时延机理**：命中 **算子数**（消除 launch 开销）与 **访存**（中间 activation 不落回 HBM）。BN 折叠为 `y = γ·x/σ + (β−γ·μ/σ)` 后，推理时只剩 scale+bias，与 conv 融合。
- **精度风险**：推理期等价（数学无损）；训练期需保留 BN 的 running stats 才能折叠。
- **适用场景**：所有 CNN 推理部署（必做）；Transformer 推理（QKV 融合 + FlashAttention 的 fused softmax）；任何有"小算子串联"的结构。
- **可操作**：Hypothesizer 默认假设"部署时所有可融合算子都被融合"，提结构假设时不必为"加 BN 慢"担心；但若新结构引入**不可融合的 elementwise 中间层**（如 `x * sigmoid(x)` 中间落 HBM），必须显式标出。

## 2. 结构化剪枝 = 结构重写（整 channel / 整 head / 整 block）

- **手法**：训练后找出冗余通道/head/层（按 BN gamma / Taylor importance / activation 相关性），**整组删掉**，得到一个**更窄/更浅的真实新结构**，而非稀疏 mask。
- **降时延机理**：命中 **FLOPs + 访存 + 算子数**（三者同步下降）。关键：稀疏（非结构化）剪枝不能直接降时延（稀疏 kernel 在大多数硬件上不快），**结构化剪枝等于改结构**，得到的是密集小网络，时延线性下降。
- **精度风险**：高剪枝率（>50%）需 fine-tune；critical layer 剪枝风险大（见 `principles.md` §7 边界）。
- **适用场景**：已有训练模型要部署到更紧时延预算；NAS 后期对 champion 做精修。
- **可操作**：Hypothesizer 可直接提"剪第 i..j stage 的 channel 数 X%"作为假设；等价于"改窄该 stage"，与重新搜结构殊途同归。**LLMatic 的 archive 显示**：不同 (W/D, FLOPS) 都能找到接近 SOTA 的结构，说明剪枝空间巨大。

## 3. 量化友好的结构选择

- **手法**：避免对 INT8/INT4 量化敏感的算子组合；尽量用"conv/linear + ReLU"这种线性+分段线性结构；不要在主路径叠加多种非饱和激活或大动态范围的 elementwise 运算。
- **降时延机理**：命中 **硬件利用率**（INT8 tensor core 比 FP16 快 2×、比 FP32 快 4×+）。量化友好的结构能稳定收敛到 INT8 不掉精度，从而拿到这块加速。
- **精度风险**：量化敏感结构（softmax 中间层、LayerNorm 跨 channel 归一化、深度 sigmoid 门控）在 INT8 下精度大幅退化，被迫留在 FP16 反而拖慢。
- **适用场景**：边缘部署 / 实时推理；目标硬件有 INT8 tensor core（NVIDIA Ampere+、Apple Silicon、Qualcomm）。
- **可操作**：Hypothesizer 提假设时若硬件明确支持 INT8，优先提 ReLU 系而非 GELU/SiLU；LayerNorm / softmax 保留 FP16（"mixed precision" 是常态，不是退化）。

## 4. 早期分辨率 / 序列长度缩减（early downsampling / shortening）

- **手法**：在网络前 1–2 层用 stride>1 conv / pooling / patch embedding 把 H×W 或 seq_len 砍下来；Transformer 用 patchify（ViT）或 strided attention。
- **降时延机理**：命中 **FLOPs + 访存**，且效果**乘进后续所有层**（前层砍 2×，后续 N 层全砍 2×）。
- **精度风险**：低层细节丢失，对 dense prediction（detection/segmentation）和小目标敏感；long-range 任务（长文档、长程代码）缩短 seq 会丢依赖。
- **适用场景**：分类任务、单目标/大目标、计算预算紧。
- **可操作**：默认假设 stem 用 stride-2/stride-4 是低成本时延杠杆；dense prediction 必须保留高分辨率分支（U-Net/FPN/HRNet）。

## 5. 参数 / 计算共享（tied weights / backbone sharing / cross-layer KV）

- **手法**：多层共用同一组权重（ALBERT 的 cross-layer parameter sharing）；多任务/多分支共享 backbone；decoder 多层共用 K/V（Multi-Query Attention、Grouped-Query Attention）。
- **降时延机理**：命中 **访存（权重读）** 与 **参数量**；MHA→GQA/MQA 显著降 KV cache 读写（推理时延主要来源）。
- **精度风险**：tied weights 减容量，需加深度/宽度补偿；GQA 极端化（MQA）会让 head_dim 过载，需 fine-tune。
- **适用场景**：Transformer 推理（GQA 几乎是现代 LLM 标配：LLaMA-2/3、Mistral、Mixtral）；多任务统一部署。
- **可操作**：Hypothesizer 在 Transformer 族直接默认提 GQA（groups = hidden_dim/64 起步）；tied weights 在搜索阶段慎用（缩小搜索空间表达力）。

## 6. 缓存友好结构（KV-cache 友好 / 减动态 shape）

- **手法**：autoregressive 推理保留 KV cache；避免结构里有"依赖上一步输出形状"的分支（条件 reshape、dynamic slice、data-dependent routing 的极端版）；静态化 shape。
- **降时延机理**：命中 **算子数（编译器重编译）+ 访存（KV 复用）**。KV cache 让每生成一个 token 只需算当前 query 与历史 K/V 的 attention，而不是重算前面所有层。动态 shape 在 TorchInductor/TensorRT/XLA 触发重编译，首次 launch 几百 ms 起步。
- **精度风险**：无（KV cache 是数学等价）；静态 shape 可能要 padding，参数微涨。
- **适用场景**：所有 autoregressive Transformer 推理；任何要被编译器优化的部署（必做）。
- **可操作**：Hypothesizer 提假设时避免 data-dependent 控制流（如"if activation > τ 则换 kernel"）；动态路由（MoE）的 top-k 必须可静态分配。

## 7. Depthwise / separable conv（降访存）

- **手法**：标准 conv 拆为 depthwise（每 channel 一个 kernel）+ 1×1 pointwise（channels 间混合），即 MobileNet 的 separable conv；或在 pointwise 前后加 expansion 形成"expansion-DW-projection"（MBConv）。
- **降时延机理**：命中 **FLOPs + 访存**。标准 3×3 conv 的 FLOPs 是 `9·C_in·C_out·H·W`，separable 是 `9·C_in·H·W + C_in·C_out·H·W`，比值 ≈ `1/C_out + 1/9`，大幅降低。
- **精度风险**：**depthwise conv 是 memory-bound**（每参数对应的 activation 多），在小 channel / 弱硬件上实际时延不如理论；group conv 的"channel shuffle"（ShuffleNet）是补救措施。
- **适用场景**：移动端 CNN、嵌入式；**LLM-NAS 的 Co-evolve Knowledge Base 自动归纳出的事实**：`avg_pool_3x3` "takes a long time and has limited accuracy improvement" —— 说明类似 low-FLOPs/high-memory 算子要警惕。
- **可操作**：CNN 在移动端默认 DW-separable；GPU/服务器端不必（GEMM-friendly 的标准 conv 反而更快）。

## 8. MoE 稀疏激活（增容量不增时延）

- **手法**：把 FFN 拆成 N 个 expert，每个 token 只路由到 top-1 / top-2 expert（Switch Transformer / GShard / Mixtral）。参数量增 N×，激活 FLOPs 几乎不变。
- **降时延机理**：命中 **FLOPs（激活）**——总参数虽多，每 token 实际算的 expert 数固定（如 2/8）。前提是 router 决策可静态分配、expert 权重常驻显存。
- **精度风险**：router collapse（所有 token 路由到同一 expert）、负载不均；需 auxiliary loss / load balancing；专家协同的容量未兑现（参数冗余）。
- **适用场景**：大模型扩容（≥ 数 B 参数才有 MoE 收益）；多任务/多领域（不同 expert 隐式专门化）。
- **可操作**：Hypothesizer 在 Transformer + 大模型场景下默认可选 MoE；小模型（< 1B）MoE 通常不划算（router 与通信开销盖过收益）。

## 9. 分组 / 分支并行让硬件饱和

- **手法**：把单一大 GEMM 拆成多个独立分支（Inception 多分支、并行的 attention head、并行的 expert）；分支数对齐到硬件并发单元数（GPU 流处理器 / TPU core）。
- **降时延机理**：命中 **硬件利用率**。单分支计算量太小让硬件吃不饱（under-utilized）；多分支可并行填充。
- **精度风险**：分支间无信息交换会损表达力（Inception 后来被 ResNet 系列超过，部分原因）；需在 fusion 点加 mixing。
- **适用场景**：小 batch 推理 / edge GPU；计算密度低的工作负载。
- **可操作**：Hypothesizer 提"加 head 数 / 加分支数"是硬件饱和方向；但若 batch 已经很大，并行分支收益消失（硬件已饱和）。

## 10. 静态 shape 优先于动态 shape（编译器友好）

- **手法**：尽量让所有中间 shape 由输入 shape 与结构唯一确定，无 data-dependent 分支；变长输入用 padding/segment-mask 而非真正的动态算子。
- **降时延机理**：命中 **算子数**（编译期 CSE/融合）+ **kernel launch**（无重编译）。TorchInductor、TensorRT、XLA 都对静态 shape 友好得多。
- **精度风险**：padding 略增计算；mask 处理需保证数值正确（attention mask、loss mask）。
- **适用场景**：任何编译器优化场景（必做）；autoregressive Transformer 推理需用 KV cache + causal mask 把"动态长度"转成"静态 shape + mask"。
- **可操作**：Hypothesizer 避免提"if/else 选 kernel"或"data-dependent slice"；改用 masked 操作表达同等语义。

---

## 组合示例（Hypothesizer 参考）

> 把多个手法组合成一个假设，比如：
> - "在 stem 后加 stride-2 DW-conv 下采样（手法 4+7）→ stem 后通道数保持 64 不动 → 第 3 stage 起接 MBConv（手法 7）→ 推理期算子全融合（手法 1）"。
> - "Transformer FFN 替换为 GQA + MoE top-2/8（手法 5+8）→ KV cache 命中（手法 6）→ softmax 保留 FP16 其余 INT8（手法 3）"。
> 注意：组合时必须显式判断是否触发 `principles.md` 的反例（如"DW-sep 在 GPU 上 memory-bound"）。

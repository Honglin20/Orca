# 跨族结构—性能原则（common/principles.md）

> 用途：Hypothesizer 生成"结构改动 → 精度/时延变化"假设时的底层物理事实清单；Analyst 评价新结构假设的判断基准。每条：**原则** / **Why** / **反例或边界** / **可操作**。
> 引用：仅注明确有出处的论文实际结论（LAPT=Zhou et al. AAAI 2025 / LLM-NAS=Zhu&Zhang 2025 / EvoPrompting=Chen et al. NeurIPS 2023 / LLMatic=Nasir et al. GECCO 2024）；其余为业界共识。

---

## 1. FLOPs ≠ latency（**首要认知基**）

- **原则**：理论 FLOPs 与真实端到端 latency 只弱相关；同 FLOPs 的两个结构在同一硬件上 latency 可差 2–10×。
- **Why**：真实 latency 由四项决定，FLOPs 都看不见：
  1. **访存带宽**（memory-bound）：activation / KV cache / weight 的读写量。`activation/parameters` 比值越高越受限。
  2. **Kernel launch / 算子数**：每个算子有 µs 级固定开销；数十个小算子串联会把 GPU 拖成 launch-bound。
  3. **硬件利用率**：tensor core 要求维度是 16/32/64 的倍数；非对齐回退到慢路径。
  4. **序列长度 / batch 的非线性 scaling**：attention 对 seq² 敏感；conv 对 spatial 平方敏感。
- **反例/边界**：纯 GEMM 工作负载（大 batch 矩阵乘、无 reshape/transpose）上 FLOPs 与 latency 才接近线性。Depthwise conv 是 memory-bound 重灾区（见 `latency_heuristics.md`）。
- **可操作**：Hypothesizer 提的结构假设若只写"减少 X% FLOPs"算**未完成**，必须额外说明改的是哪一项（访存/算子数/对齐/seq_len）。

## 2. 深度 vs 宽度的权衡：深层窄 通常优于 浅层宽（精度）

- **原则**：参数预算固定时，**更深更窄**（more layers, fewer channels / smaller hidden_dim）通常精度更高；但训练稳定性下降，必须配 residual + pre-norm（见第 4、5 条）。
- **Why**：每加一层等于多一次非线性复合，组合表达力上升；同参数下窄层让计算更"组合化"而非"扁平宽"。**EvoPrompting 的 MNIST-1D 搜索结果实证**：最终 Pareto 前沿持续偏好 "narrower + deeper CNN, smaller strides, less padding, no dense layers"。
- **反例/边界**：
  - 极深 + 极窄会触发信息瓶颈（见第 6 条），精度反而塌。
  - 硬件依赖：GPU 偏好大 GEMM（宽 > 深），CPU/edge 偏好减少串行深度。
  - 深度上升对 latency 在串行设备上是线性的，可能抵消精度收益。
- **可操作**：默认假设"加深度 + 减宽度"是精度改进方向；若目标硬件是 edge CPU，反向切换。

## 3. Early downsampling（早降分辨率/序列长度）

- **原则**：网络前 1–2 层尽快把空间分辨率（CNN）或序列长度（Transformer）降到工作尺寸，后续所有计算在低分辨率上展开。
- **Why**：每层 FLOPs 与访存对 spatial/seq_len 是平方或线性关系。前层 stride=2 把 H×W 减半，后续 N 层成本全减半。低层特征（边缘/纹理）所需 channel 少，是"用最少信息损失换最大计算节省"的位置。这是 NAS 草稿给出的典型跨族原则（"在浅层做下采样比深层省时延且精度损失小"）。
- **反例/边界**：
  - 第一层就 stride 8+ 会丢小目标 / dense prediction（detection、segmentation）精度。
  - 长上下文任务（长文档、长程代码）不能无脑缩短 seq_len，会丢失 long-range 依赖。
- **可操作**：Hypothesizer 默认提"stem stride-2/stride-4 快降 + 后续精化"；dense prediction 必须保留高分辨率分支（U-Net/FPN）。

## 4. 残差 / 跳连：深层的硬需求

- **原则**：深度超过阈值（CNN ~10 层 / Transformer ~6 层）**必须有 skip connection**，否则梯度消失/爆炸使训练不收敛。
- **Why**：残差让"恒等映射"成为零梯度的最短路径，梯度可直达任意层（He et al. ResNet 2016）。**LAPT 在 NAS-Bench-201 上让 LLM 归纳出的原则之一**即："中间层应使用 skip connection 防止梯度消失"——LLM 自归纳结果与人类专家（Yuan 2022）一致。
- **反例/边界**：
  - 残差不等于精度无限提升：ResNet-1000 出现 degradation 是优化问题，不是表达力问题。
  - 残差路径上的归一化位置至关重要（见第 5 条）。
  - 极宽单层网络（hidden_dim 极大）不需堆叠残差。
- **可操作**：任何 depth ≥ 6 的 stack 必须显式 residual；Hypothesizer 提"纯堆叠无 skip"的中等深度假设，Analyst 应直接 reject。

## 5. 归一化层位置：pre-norm 优于 post-norm（深层稳定性）

- **原则**：深层结构应使用 **pre-norm**（norm 在 attention/conv 之前），**post-norm**（原 Transformer 论文）在深度 ≥ 12 时训练不稳定。
- **Why**：post-norm 把主路径再归一化，破坏残差的"恒等最短路径"，深层梯度爆炸；pre-norm 把 norm 推进旁路，主路径恒等，可训超深（GPT-2/3、大多数现代 LLM 都是 pre-LN）。
- **反例/边界**：
  - pre-norm 的"恒等无穷大"在 capacity 上略逊于 post-norm，浅模型 post-norm 可能精度更高（深度 < 6 可破例）。
  - **RMSNorm** 是 LN 的简化（去 mean，只 scale），是当前 LLM 主流。
  - **BN** 在 NLP/Transformer 中几乎不用，CNN + 大 batch 仍是主流；小 batch 用 GN。
- **可操作**：Hypothesizer 默认提议 pre-RMSNorm / pre-LN；BN 仅在 CNN + 大 batch 场景下提。

## 6. 信息瓶颈：channel / hidden_dim 太小损精度（非线性崩溃）

- **原则**：每层的 channel（CNN）或 hidden_dim（Transformer）有**下限**，低于该下限精度快速崩溃，不是线性退化。
- **Why**：层间激活必须承载 label 所需的互信息；channel 太小会让不同低层特征挤进同通道，产生 aliasing。attention 头数固定时 head_dim 过小让 softmax 失去判别力。**LLM-NAS 在 AutoFormer ViT 空间上的复杂度分析**：FLOPs 主要由 **Embed Dim D（O(D²)）** 主导（MLP 块占大头），其次是 **Depth L（O(L)）**——说明减 D 是最"贵"的精度杠杆，不能随意砍。
- **反例/边界**：
  - 不是越大越好，超阈值后冗余（见第 7 条，可剪枝）。
  - 减 D 时应同步减 head 数，保 head_dim ≥ ~64。
- **可操作**：减维必须保最后 stage / 最后几层 hidden_dim 不低于经验下限（CNN ~32、Transformer ~256）；前期层可更窄。

## 7. 冗余性假设：可剪枝/可共享的依据

- **原则**：训练好的网络存在大量冗余——通道/头/层间权重高度相关，去掉一部分对精度影响远小于"按参数比例线性下降"。
- **Why**：SGD 收敛到宽极小值，权矩阵多低秩；structured redundancy 让整 channel / 整 head 可去。**LLMatic 用 (width-to-depth ratio, FLOPS) 作 MAP-Elites 行为描述子**，发现 archive 中大量不同 (W/D, FLOPS) 的网络都能达到接近 SOTA 的精度——结构等价类远少于参数等价类，冗余明显。
- **反例/边界**：
  - 冗余是**训练后属性**，搜索阶段不能假设它一定存在。
  - critical layer（第一层、最后分类层、attention output proj）冗余度低，剪枝要保守。
- **可操作**：Hypothesizer 提"剪 X% channel"必须指明在哪些层剪，不能全局一刀切。

## 8. Bottleneck：先降维 → 主计算 → 升维

- **原则**：在通道密集的 stage，用 1×1 降维 → 在低维做 3×3 spatial → 再 1×1 升维（ResNet-50 bottleneck block）；Transformer MLP 用 expansion→intermediate→contract 是同构 pattern。
- **Why**：在低维做主 spatial 计算，FLOPs 与访存都按低维计；升维恢复表达力。这是"在精度损失最小处砍成本"的经典 pattern。
- **反例/边界**：
  - reduction ratio 太激进（< 1/16）触发信息瓶颈（第 6 条）。
  - spatial conv 必须在降维后做；升维后再做 3×3 等于没省。
- **可操作**：通道 > 256 的 stage 默认考虑 bottleneck；reduction ratio 经验值 1/4 ~ 1/2。

## 9. 归纳偏置应匹配数据结构

- **原则**：translation invariance 数据（图像）→ convolution / local attention；set 数据（无序点）→ permutation-invariant pooling；long-range sequence → attention / recurrent；强行错配归纳偏置比单纯小模型更伤精度。
- **Why**：归纳偏置把"已知数据性质"编码进结构，等效于免费先验。ViT 比 CNN 需要更大数据集才能赢，正是因为弱化了 locality 先验。
- **反例/边界**：数据规模足够大时，弱归纳偏置（全局 attention）能超越强偏置（CNN locality）；小数据反之。
- **可操作**：Hypothesizer 必须先判断数据性质再提结构，不能默认 Transformer 万能；Analyst 评价新结构假设时优先检查"数据—偏置"匹配。

## 10. 对齐性 / shape 友好是隐性时延杠杆

- **原则**：通道数、head 数、seq_len、batch 都尽量对齐到硬件的"甜点倍数"（GPU tensor core 通常 8/16/32/64 的倍数）；动态 shape / 分支稀疏会触发重编译或慢路径。
- **Why**：硬件利用率直接乘进 latency；非对齐维度让 tensor core 退化到 CUDA core，慢数倍。动态 shape 在编译器（XLA/TorchInductor/TensorRT）层触发重编译，首次 launch 几百 ms 起步。
- **反例/边界**：对齐到 32 的倍数会让参数微涨（padding），但通常时延收益 > 参数成本。
- **可操作**：Hypothesizer 提通道数时优先 64/128/256；避免奇数维度；动态分支（条件执行）尽量静态化或 batch 内统一。

## 11. 极小 launch-bound 模型上"减算子数"会被"单核加重"反噬（减层 ≠ 减时延）

- **原则**：当模型已极小（latency 在几十 us 量级、算子已 < 10 个）时，"少几个 launch"省下的固定开销往往抵不过为维持出口维度而加宽/加深单个算子带来的单核工作量上升。launch-bound 的降时延直觉存在**规模下限**。
- **Why**：减层通常要重排通道（保出口），单层 MAC 上升；launch 开销在极小模型上虽占比高，但其绝对值（us 级）在已经很小的 launch 集合上进一步压缩的空间有限，单核变重的代价是乘进该核全部空间分辨率的。
- **反例/边界**：本原则只在"算子已极少 + 单次推理 < 0.05ms"的极小模型上成立；正常规模（ms 级以上）launch 仍是显著杠杆（见 `latency_heuristics.md` §1）。另外 sub-ms 区间 cost model 测量本身高方差（重测同一 ONNX 可在 0.02-0.08ms 间漂移），判成败需意识到噪声、必要时增加 runs/warmup。
- **可操作**：Hypothesizer 在极小模型上提"减层/融合"假设前，先估单核 MAC 变化方向；若减层必伴随单核加宽，预期时延收益应打折；优先尝试"纯减通道"或"换更廉价算子同通道"等不加重单核的 move。发现 run: agent-struct-exploration-20260716-194643-b9dd24 / r1_c1。

---

## Analyst 追加规则

- 当某轮验证发现新的跨族通用规律（即不限于当前族），追加到本文件末尾，格式同上（**原则 / Why / 反例 / 可操作**），并附发现该规律的 run id + 结构描述。
- 若发现本文件已有原则在新场景下**反例占上风**（如某 hardware 上深层反而慢），不要删原原则，在"反例/边界"补一条带 run id 的反例。

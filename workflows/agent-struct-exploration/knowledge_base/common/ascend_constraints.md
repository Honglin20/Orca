# 昇腾硬件铁律（common/ascend_constraints.md）

> 用途：**跨族硬件过滤层**。所有族（cnn / transformer / wireless_receiver / …）的结构变异在进入 latency_moves 决策前，**先过这层**。Analyst 评假设时用本文件驳回"降 FLOPs 但硬件不友好"的 move；Hypothesizer 提假设时用本文件挑硬件亲和方向。
>
> 统一格式：每条铁律 = **事实 / 为什么 / 对结构变异的含义 / 反例或边界 / 来源URL**。
>
> **fail-loud 总前提（来自 plan §10.1）**：无任何 move 在「昇腾 + OFDM 接收机」场景有公开实测；本文件所有判定均为**算子形状推断**。落地前必须在目标型号（Atlas A2 / CANN）上 micro-bench 至少每类一个代表。型号未定（310 vs 910）前，INT8/融合能力的结论**必须复核**。

---

## 铁律 1：Conv = Im2Col + Cube GEMM，特征图 NC1HWC0（C0=16），Cube tile 16×16×16

- **事实**：昇腾 AiCore 的 Cube 矩阵单元只做密集 GEMM，标准卷积被编译成 Im2Col 展开 + 一次 Cube GEMM。Cube 的原生 tile 是 16×16×16（FP16），特征图与权重统一走 5D 格式 `NC1HWC0`，其中 `C0=16` 是硬件 lane 宽度，`C1 = ceil(C/16)`。
- **为什么**：Cube 一次 load 16×16 的 A、B tile 算出 16×16 的 C tile，是昇腾上单位面积算力最高的路径。任何"算子能落进 Cube GEMM"的结构都在吃最大吞吐；落不进 Cube 的算子退化到 Vector 核（吞吐低一个数量级）。
- **对结构变异的含义**：提结构假设时，优先选"能整体表达成 GEMM"的算子（标准 conv、1×1 conv、matmul、linear）。一个算子是不是 Cube-friendly，**直接决定它在昇腾上的时延量级**。
- **反例或边界**：Im2Col 本身要写一份展开的 activation（多一次 HBM 写）；当 `C` 不是 16 倍数时 `C0` lane 有 padding 浪费（见铁律 9）。Cube tile 16 也意味着 head_dim / inner_dim 最好是 16 倍数（见铁律 7）。
- **来源URL**：
  - 昇腾 AiCore / Cube 架构：https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/
  - llm.npu（ASPLOS'25，NPU 上 LLM 推理，Cube/Vector 分流）：https://arxiv.org/abs/2407.05858

## 铁律 2：TransData = conv↔matmul 边界的纯内存重排（混合模型的税）

- **事实**：卷积吃 `NC1HWC0`（C0=16，5D），而 matmul / attention 的 bmm 习惯吃 `NZ`（NZ = Fractal_Zn，分块列主）或 `ND`（普通 dense）。**每条 conv→attention 或 attention→conv 的边都会插一个 `TransData` 算子**——它不计算，只做纯内存重排（permute + pad），却要读一遍、写一遍整个 activation。
- **为什么**：这是本族当前模型（Conv1d-over-freq + symbol attention 混合）时延的最大单一来源（plan §1：CPU profiling attention 仅 17%，大头是 conv↔attn 边的反复重排）。CNN+attn 混合天然触发它，纯 conv 或纯 matmul 的模型则几乎不触发。ASPLOS'25 llm.npu 与 ATC'25 Hermes 都把"格式跨越（format crossing）"列为 NPU 上 Transformer/混合模型的主要开销。
- **对结构变异的含义**：**减少 domain crossing 次数 > 让 attention 更快**。每砍掉一个 conv↔attn 边界（例如把 attention 折叠成线性、或把投影 conv 改成不触发格式切换的形状），收益按"整份 activation 的一读一写"计。Hypothesizer 对每个混合 block 都要数边界数。
- **反例或边界**：TransData 在 `msprof`（昇腾 profiler）的时间线里有具名算子，可直接看占比——**必须用 msprof 实测确认**，不要凭 FLOPs 猜。纯 GEMM 工作负载（无 conv）触发不到。连续多个同格式 conv 之间也不触发（都在 NC1HWC0 内）。
- **来源URL**：
  - llm.npu（ASPLOS'25）：https://arxiv.org/abs/2407.05858
  - Hermes, Accelerating Model Training on Ascend Chips（USENIX ATC'25）：https://cs.nju.edu.cn/tianchen/lunwen/2025/atc25-yuhang.pdf
  - CANN msprof / TransData 算子：https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/

## 铁律 3：1×1 conv = 直接 GEMM（无 Im2Col）；DW / group conv 饿死 Cube

- **事实**：kernel=1 的卷积没有空间聚合，Im2Col 退化成恒等，等价于一次纯 GEMM，直接落 Cube、且不触发额外的 TransData。反过来，**depthwise（groups=C）与 groups>>1 的 group conv 把一个 Cube GEMM 拆成 C 个小向量运算**，Cube 几乎吃不到 16×16 tile、利用率崩。
- **为什么**：Cube 的吞吐前提是"A、B 都够大且连续"。DW 每 channel 一个 kernel，每个输出只需 1 个 channel 的权重，Cube tile 里 15/16 的 lane 是空的；group conv 同理，组内通道数 <16 时 Cube 退化成 Vector。
- **对结构变异的含义**：
  - **pointwise（1×1）conv 是昇腾上最廉价的"通道混合"原语**——降时延优先把 3-tap conv 换成 1×1（丢的局部平滑用 dilation 或 delay-domain 补，见族 latency_moves M4/M9）。
  - **DW-sep / 大 group conv 是反模式**（见 `failures.md`），否掉所有来自通用文献的"DW 降 FLOPs"建议（含 CMT 的 depthwise-LPU）。
- **反例或边界**：标准 3×3 conv 仍是 Cube GEMM（经 Im2Col），不会饿死 Cube，只是比 1×1 多一次展开开销——3×3 不是禁区，DW 才是。极小 group（g=2）的 group conv 接近标准 conv，可接受；g≥8 需实测。
- **来源URL**：
  - MobileNet DW memory-bound 的跨硬件共识：https://arxiv.org/abs/1704.04861 （Howard 2017）
  - 昇腾 Cube/GEMM 算子形状要求：https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/

## 铁律 4：BN 可 fold（`ConvBatchnormFusionPass`）；LN / RMSNorm 不可 fold，RMSNorm < LN

- **事实**：BatchNorm 推理态是逐 channel 仿射 `y=γx/σ+(β−γμ/σ)`，昇腾编译器有 `ConvBatchnormFusionPass` 把它等价折进前一个 Conv 的权重偏置，norm 算子**归零**。LayerNorm / RMSNorm 是**跨通道的 Vector 核归约**（算 mean/var/RMS），结构上无法折进 matmul，且占一次 Vector launch。
- **为什么**：BN-fold 是"零精度损失的免费降时延"——少一次 activation 读写、少一个算子。LN/RMSNorm 留在图里就是实打实的 Vector 开销。RMSNorm 比 LN 少一次减均值，略快（RMSNorm < LN in latency）。
- **对结构变异的含义**：
  - 能换 BN 就换 BN（本族 baseline 现用 LN，且 `elementwise_affine=False`——见 `primitives.md` 的 LN 条目），M1 move 建议换 BN 再 fold。
  - 如果必须保留 LN/RMSNorm（Transformer 路线），把它放在"不与 conv 跨格式"的位置，并接受这一份 Vector 开销。
- **反例或边界**：BN 与 batch 强耦合，本族 batch 极小（在线推理 batch=1）时 BN running stats 不稳——这是用 BN 的真实代价，需 QAT/fine-tune 校准。`elementwise_affine=False` 的 LN 无可学参数，fold 没意义但开销仍在。
- **来源URL**：
  - `ConvBatchnormFusionPass` 等 Ascend 图融合 pass：https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/
  - RMSNorm（Zhang & Sennrich 2019）：https://arxiv.org/abs/1910.07467

## 铁律 5：AutoFuse（内部名 "LUBAN"）+ `torch.compile(backend=npu)` + ATC→`.om`，可融合 Conv+BN+ReLU / MatMul+Bias+GELU

- **事实**：昇腾有两级融合：(a) 编译期图融合 AutoFuse（华为内部代号 "LUBAN"），把 elementwise / scale / 激活串联合并进前一个 GEMM/conv kernel；(b) `torch.compile(backend=npu)` 在 PyTorch 侧做 inductor 式优化后下沉；最终经 **ATC**（Ascend Tensor Compiler）把计算图编译成离线 `.om` 模型。典型可融合链：`Conv+BN+ReLU`、`MatMul+Bias+GELU`、`Conv+Bias+Add`。
- **为什么**：融合把"中间 activation 不落回 HBM"，直接命中算子数与访存两项。一条 `Conv→BN→ReLU→Add` 四算子融合成单 kernel，是部署期最大的一块廉价赢面。
- **对结构变异的含义**：
  - 默认假设"部署期所有可融合 elementwise 都被融合"，提结构假设时**不必为"加 BN/ReLU 慢"担心**。
  - **但**：若新结构引入**不可融合的 elementwise 中间层**（如自定义 `x*sigmoid(x)` 中间落 HBM、或 soft-threshold 的 `sign` 分支），必须显式标出——它会逃出 AutoFuse、留下一次 Vector launch。
- **反例或边界**：融合要求算子链是静态 shape 且算子落在 LUBAN 规则库内；自定义算子 / 非 FP16 中间类型 / 跨 stream 的算子不融合。融合后的 `.om` 是**固定 shape**的（见铁律 6）。
- **来源URL**：
  - `torch.compile(backend=npu)` / GE 图编译：https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/
  - ATC / `.om` 离线模型：https://www.hiascend.com/document/detail/zh/CANNCommercial/

## 铁律 6：静态 shape 几乎强制（sinking dispatch vs Host dispatch）

- **事实**：昇腾上拿到最优时延的 `.om` 是**针对单一静态 shape 编译**的（tiling 在编译期算好，sinking dispatch 直接下 Device）。遇到动态 shape（data-dependent 分支、变长输入、`if shape>...` 的条件 reshape），要么回退到 **Host dispatch**（CPU 侧重算 tiling、重下发，几 ms 起），要么对若干 shape 分桶各编译一个 `.om` + tiling cache。
- **为什么**：Cube tiling（怎么切 16×16×16 tile、怎么搬数据）依赖确切的 shape。shape 变了，最优 tiling 变了，必须重新调度——这就是 Host dispatch 的开销来源。
- **对结构变异的含义**：
  - **任何带 data-dependent 控制流的 move 都是陷阱**：early-exit（if SNR 则跳层）、动态深度、data-dependent slice / reshape——要么禁、要么**重构为静态图**（如 early-exit 改成 `skip-via-zero-mask`：层照算但输出乘 0 mask，shape 不变）。
  - 变长输入用 padding + mask 表达，不要用真动态算子。
- **反例或边界**：分桶（shape bucket）+ tiling cache 可让"有限几个 shape"都拿到接近静态的性能，但要离线枚举。CANN 近期版本对部分算子支持动态 shape，但性能仍不如静态——**以实测为准**。
- **来源URL**：
  - CANN 动态 shape / tiling cache：https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/
  - llm.npu（ASPLOS'25，讨论 NPU 静态/动态调度代价）：https://arxiv.org/abs/2407.05858

## 铁律 7：融合 attn 算子 `npu_fusion_attention`；head_dim ÷ 16，seq ≥ 16

- **事实**：昇腾提供融合 attention 算子（`npu_fusion_attention` / `FlashAttentionScore`，对齐 FlashAttention 的 fused softmax+GEMM，中间 N×N 矩阵不落 HBM）。约束：**head_dim 必须是 16 的倍数**，seq_len 建议 ≥16，否则融合算子退化或拒编。
- **为什么**：手搓 attention（自己写 bmm→softmax→bmm，像本族 baseline 当前那样）会拆成 3 个独立 GEMM + 一个 softmax，每个 GEMM 边界都可能触发 TransData（铁律 2），且 softmax 的中间 N×N 落 HBM。融合算子把这些收进单 kernel。
- **对结构变异的含义**：
  - **禁手搓 attention**（见 `failures.md`）。若保留 attention 路线，必须改写成调 `npu_fusion_attention` 的形状：把 embed_dim 重组为 `(num_heads, head_dim)`，`head_dim ∈ {16, 32, 64, 128}`。
  - 本族 baseline 的 per-channel 64×64 attention 当前是"16 个 head_dim=48 的独立单头"——head_dim=48 **不是 16 倍数**，融合算子拒编，必须先重排（见 `primitives.md` 该条目的变异提示）。
- **反例或边界**：融合算子对非标准 attention 变体（linear-attn、 Performer、自定义 mask 形状）支持不全；seq_len < 16 时融合收益消失。本族 N=64 symbol 是够长的，但 head_dim 对齐是硬约束。
- **来源URL**：
  - `FlashAttentionScore` 融合算子替换（昇腾官方）：https://www.hiascend.com/document/detail/zh/Pytorch/60RC1/ptmoddevg/trainingmigrguide/performance_tuning_0027.html
  - FastAttention: Extend FlashAttention2 to NPUs：https://arxiv.org/abs/2410.16663

## 铁律 8：INT8 via AMCT ≈ 2× FP16 Cube；**无原生 INT4 / 复数**

- **事实**：INT8 量化经 **AMCT**（Ascend Model Compression Toolkit）做 PTQ / QAT，Cube 的 INT8 吞吐约为 FP16 的 2×（与 GPU tensor core 同量级）。但昇腾**无原生 INT4**（仅在 FP16/INT8 两档；某些 FPGA 论文里的 INT4/MSQ 说法在昇腾上要降级为"INT8-only"），也**无原生复数（complex）类型**——复数运算必须 lowering 成 2×2 block-real GEMM（Trabelsi 2018 的 real-pair 展开方式）。
- **为什么**：Cube 的数据通路只铺了 FP16/INT8（BF16/FP32 走更慢路径）。INT4 要么硬件不认、要么走 LUT 模拟反而更慢。复数没有专用 lane，必须把 `(a+bi)(c+di)` 展开成 4 个实 GEMM 的 block 组合。
- **对结构变异的含义**：
  - INT8（M16 move）是正交叠加的 2× 杠杆，本族应该吃（QAT 可把量化损失压到 <1dB）。
  - **INT4 / 二值化（BNN）方向在本硬件上是死路**（见 `failures.md`），别把通用边缘推理文献的 INT4 结论搬过来。
  - CVNN（复数网络）方向（M22）**不是免费午餐**——省参数但 GEMM 数变多，必须按 block-real lowering 实测，标注为"研究性"。
- **反例或边界**：**型号未定前必须 fail-loud**（plan §10.4）：310 只支持 FP16/INT8 部分、910 系列支持更全；INT4/复数能力以目标型号 CANN release notes 为准。某些新 910B/A2 对低比特支持在演进，落地前查 release notes。
- **来源URL**：
  - AMCT（昇腾模型压缩工具）：https://www.hiascend.com/document/detail/zh/CANNCommercial/
  - Cube INT8 vs FP16 吞吐（llm.npu, ASPLOS'25）：https://arxiv.org/abs/2407.05858
  - CVNN 复数网络 lowering（Trabelsi, ICLR'18）：https://arxiv.org/abs/1705.09792

## 铁律 9：通道 ÷ 16 对齐（C0 lane 不浪费）

- **事实**：`NC1HWC0` 的 `C0=16` 是物理 lane。通道数不是 16 倍数时，末尾的 `C0` lane 被 padding 填 0——Cube 照常算 16×16 tile，但有效利用率 < 100%。
- **为什么**：通道 = 17 与通道 = 32 在昇腾上的时延可能一样（都占两个 16-lane？不——17 会占两个 C1 slot 但第二个只用了 1/16）。奇数通道、接近 16 边界的通道最浪费。
- **对结构变异的含义**：
  - Hypothesizer 提通道数时优先 `{16, 32, 48, 64, 128}`（含本族 baseline 的 embed_dim=16、子载波 48——48 不是 16 倍数，但子载波是空间轴不是通道轴，不在此约束内）。
  - 改通道宽度的 move（剪枝、bottleneck 降维）要把出口通道 round 到 16 倍数，否则名义上剪了、实际没省时延。
- **反例或边界**：head_dim 同理（铁律 7）。子载波数（48）、symbol 数（64）是输入物理维度，不能随意 round；但它们进入 conv 时作为 H/W 轴，不触发 C0 浪费。
- **来源URL**：
  - 昇腾 NC1HWC0 / C0=16 对齐：https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/

---

## 变异前的硬件自检清单（Hypothesizer / Engineer / Analyst 共用）

> 提交一个结构变异假设前，**逐条过**。任何一条答"否/未知"必须在假设里显式标注风险，Analyst 可据此 reject。

1. **[Cube]** 新增的算子能落进 Cube GEMM 吗？（标准 conv / 1×1 conv / matmul = 是；DW / 大 group / 自定义 = 否）
2. **[TransData]** 这个变异增加还是减少了 conv↔matmul 边界数？（混合模型重点数）
3. **[融合]** 新结构有没有引入**不可融合**的 elementwise 中间层？（soft-threshold 的 sign、自定义门控 mul 等）
4. **[Norm]** 用了 LN/RMSNorm 吗？能换 BN 再 fold 吗？换不了就接受 Vector 开销。
5. **[静态 shape]** 变异里有没有 data-dependent 控制流 / 动态 shape？（early-exit、动态深度、条件 reshape）→ 必须重构为零 mask / padding。
6. **[attn 融合]** 若保留 attention：head_dim 是 16 倍数吗？是否调 `npu_fusion_attention` 而非手搓 bmm+softmax？
7. **[量化]** 通道数 round 到 16 倍数了吗？有没有引入 INT4 / 复数 / BNN 这类昇腾不认的路径？
8. **[对齐]** embed_dim / head_dim / 中间通道都 ÷16 吗？
9. **[实测]** 这个 move 在昇腾 + 本任务上有公开实测吗？（默认否——标"算子形状推断"，落地前 micro-bench）
10. **[型号]** 目标型号（310/910/A2）确定了吗？INT8 / 融合能力需不需要复核？（未定则 fail-loud）

---

## Analyst 追加规则

- 本文件每条铁律均来自**算子形状推断 + 昇腾公开文档**，非 OFDM 接收机实测。Analyst 驳回"降 FLOPs 但违反上述任一铁律"的假设时，引用对应铁律编号。
- 若某轮 msprof 实测推翻了某条铁律（例如发现某型号 Cube 对 DW 友好），**不要删原铁律**，在该条"反例或边界"补一条带 run id + 型号的反例。
- 新发现的跨族硬件规律追加到本文件末尾，格式同上（**事实 / 为什么 / 含义 / 反例 / URL**），附 run id。

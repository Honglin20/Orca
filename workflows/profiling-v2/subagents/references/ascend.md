# 昇腾硬件参考（共享：结构提案先验 + MFU 诊断语义）

双受众：architecture proposers 拿它做事前设计先验（§1 的结构含义栏 + §3）；
mfu-analyzer 拿它做瓶颈归因的硬件语义底座（§1 的诊断含义栏 + §2）。
本文件不是时延门——事后实测权威永远是 mfu-analyzer 的测量结果。

**fail-loud 前提**：本文件全部判定来自算子形状推断与昇腾公开文档，非逐型号实测。
6613 / 1951 的具体能力差异（精度档位、融合规则、对齐约束）以 mfu_benchmark 实测为
准：实测与本文件冲突时，**信实测**，并在报告披露段记录反例（型号 + 现象）。

---

## §1 硬件事实

每条三栏：**事实 / 诊断含义（analyzer 用）/ 结构含义（proposer 用）**。

### 1.1 两级算力：Cube 与 Vector

- **事实**：AiCore 内分 Cube 矩阵单元与 Vector 单元。密集 GEMM（conv / matmul /
  linear）走 Cube，是单位面积吞吐最高的路径；逐元素运算、超越函数（exp / tanh /
  erf / sigmoid 族 / 除法 / 开方）、归约（Reduce / Softmax / LayerNorm 的 mean-var）
  走 Vector，单元素吞吐低 1-2 个数量级。
- **诊断**：vector 类算子的时间占比高 ⇒ **vector-bound**——此时 cube MFU 低是表象
  不是根因，瓶颈在 Vector 单元吞吐；提高 cube 喂养救不了它。
- **结构**：让主要计算量落进 Cube；压缩、替换或融合超越函数链；归约链（LayerNorm
  的 mean/var/div）是纯 Vector 开销，能 fold 则 fold（见 1.5）。
- 来源：https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/ 、
  llm.npu（ASPLOS'25）https://arxiv.org/abs/2407.05858

### 1.2 Cube = Im2Col + GEMM；特征图 NC1HWC0（C0=16）

- **事实**：标准卷积被编译成 Im2Col 展开 + 一次 Cube GEMM；特征图与权重统一 5D
  格式 `NC1HWC0`，`C0=16` 是物理 lane 宽度，Cube 原生 tile 为 16×16×16。
- **诊断**：Img2Col 类算子是重排不是计算——它的 MFU 无意义（常爆 >100% 估算偏差），
  按搬运重排成本计价；展开多一次 HBM 写。
- **结构**：能整体表达成 GEMM 的算子（标准 conv、1×1 conv、matmul、linear）时延
  量级最优；`C` 非 16 倍数时 C0 lane 有 padding 浪费（见 1.10）。

### 1.3 TransData：conv↔matmul 边界的格式切换税

- **事实**：conv 吃 `NC1HWC0`，matmul / bmm 习惯吃 `NZ`（Fractal_Z）或 `ND`；每条
  conv→attention / attention→conv 的边会插一个 TransData 算子——它不计算，只做纯
  内存重排（permute + pad），却要整份 activation 一读一写。
- **诊断**：cube 算子之间出现 transpose / cast / reduce / img2col 的交替窗口 ⇒
  **memory-movement-bound（格式切换税）**。它把流水切碎、稀释 cube 窗口占比、拖低
  cube 算子的喂养 MFU——表现为「MATMUL 耗时最长」，但根因在它周边的重排，不在它。
- **结构**：减少 domain crossing 次数 > 优化任何单个算子；每个混合 block 数清
  conv↔matmul 边界数。ASPLOS'25 llm.npu 与 ATC'25 Hermes 均把 format crossing 列为
  NPU 混合模型的主要开销。来源：https://arxiv.org/abs/2407.05858 、
  https://cs.nju.edu.cn/tianchen/lunwen/2025/atc25-yuhang.pdf

### 1.4 1×1 conv 免 Im2Col；DW / group conv 饿死 Cube

- **事实**：kernel=1 的卷积 Im2Col 退化为恒等，等价纯 GEMM 直接落 Cube。depthwise
  （groups=C）与 group conv 把一个 Cube GEMM 拆成 C 个小向量运算，Cube tile 里
  大部分 lane 空置，利用率崩。
- **诊断**：DW / 大 group conv 算子 MFU 显著低于同类 dense 算子是结构性的，不是
  喂养问题。
- **结构**：pointwise（1×1）是最廉价的通道混合原语；DW-sep 是反模式。标准 3×3
  conv 仍是 Cube GEMM，不是禁区。来源：https://arxiv.org/abs/1704.04861

### 1.5 BN 可 fold；LN / RMSNorm 不可

- **事实**：推理态 BN 是逐 channel 仿射，编译器 `ConvBatchnormFusionPass` 可把它
  折进前一个 conv，norm 算子归零；LN / RMSNorm 是跨通道 Vector 归约，无法折进
  matmul，留一次 Vector launch（RMSNorm 比 LN 少一次减均值，略快）。
- **诊断**：norm 类算子在图里 = 实打实 Vector 时间；若耗时占比显著，归 vector 类
  窗口。
- **结构**：能换 BN 就换 BN 并吃 fold 的免费收益；必须保留 LN/RMSNorm 时放在不与
  conv 跨格式的位置。来源：https://arxiv.org/abs/1910.07467

### 1.6 图融合：可融合 elementwise 默认已被吃掉

- **事实**：编译期图融合把 elementwise / scale / 激活串联合并进前一个 GEMM / conv
  kernel（典型链：Conv+BN+ReLU、MatMul+Bias+GELU、Conv+Bias+Add）。
- **诊断**：不要为「加了 ReLU/BN 会不会慢」报警——默认假设已融合；融合后的中间
  activation 不落 HBM。
- **结构**：只有引入**不可融合**的 elementwise 中间层（自定义门控 mul、sign 分支、
  非 FP16 中间类型、跨 stream）才会逃出融合、留下一次 Vector launch——新结构必须
  显式标出这类层。

### 1.7 静态 shape 几乎强制

- **事实**：最优 `.om` 是按单一静态 shape 编译的（tiling 编译期算好）；动态 shape
  要么回退 Host dispatch（CPU 重算 tiling，毫秒级），要么分桶各编译一份。
- **诊断**：host 侧等待 / 调度间隙在 timeline 上表现为非算子开销。
- **结构**：任何 data-dependent 控制流（early-exit、动态深度、条件 reshape）都是
  陷阱——重构为静态图（zero-mask / padding）。来源：https://arxiv.org/abs/2407.05858

### 1.8 融合 attention：head_dim ÷ 16，seq ≥ 16

- **事实**：`npu_fusion_attention` / FlashAttentionScore 把 softmax+GEMM 收进单
  kernel，中间 N×N 不落 HBM；硬约束 head_dim 是 16 的倍数、seq_len 建议 ≥16，
  否则退化或拒编。
- **诊断**：手搓 attention（bmm→softmax→bmm）拆成 3 个独立 GEMM + softmax，每个
  GEMM 边界都可能触发 TransData（1.3），softmax 中间落 HBM。
- **结构**：禁手搓 attention；embed_dim 重组为 (num_heads, head_dim)，head_dim ∈
  {16, 32, 64, 128}。来源：
  https://www.hiascend.com/document/detail/zh/Pytorch/60RC1/ptmoddevg/trainingmigrguide/performance_tuning_0027.html

### 1.9 INT8 ≈ 2× FP16 Cube；无原生 INT4 / 复数

- **事实**：INT8 经 AMCT（PTQ/QAT），Cube 吞吐约为 FP16 的 2×；数据通路只铺
  FP16 / INT8，无原生 INT4、无原生复数（复数须 lowering 成 block-real GEMM）。
- **诊断**：精度档位影响理论峰值口径，对照 schedule_result.json 的评测参数核对。
- **结构**：INT8 是正交叠加的 2× 杠杆；INT4 / 二值化 / CVNN 在本硬件上按降级或
  研究性处理，须实测。来源：https://arxiv.org/abs/2407.05858

### 1.10 通道与 head_dim ÷16 对齐

- **事实**：`C0=16` 是物理 lane；通道数非 16 倍数时末位 lane 被 padding 填 0，
  Cube 照算 16×16 tile 但有效利用率 <100%。
- **诊断**：通道 / head_dim 非 16 倍数的 cube 算子，MFU 天花板本身被压低——这是
  对齐损耗，不是喂养问题，两类根因要区分。
- **结构**：通道宽度优先 {16, 32, 48, 64, 128}；剪枝 / 降维的出口通道 round 到
  16 倍数，否则名义剪了、实际没省时延。

---

## §2 op 分类表（bound 判定的分类依据）

| 类别 | 典型 ONNX op_type | 利用率口径 |
|---|---|---|
| cube 类 | Conv、MatMul、Gemm、Linear（含 BN-fold 后） | MFU 列有诊断意义 |
| vector 类 | Reduce*、ArgMax/ArgMin、Softmax、LayerNormalization、InstanceNormalization、Elementwise（Add/Mul/Sub/Max/Erf/Tanh/Sigmoid/Exp）、Comparison | 看**时间占比**，不看 MFU |
| 搬运重排类 | Transpose、Cast、Reshape、Squeeze/Unsqueeze、Concat、Split、Slice、Img2Col/Im2col、TransData、Pad | 看**时间占比 + delay_cycles**，不看 MFU |

- 分类以 profile 产物里的实际 `op_type` 为准，上表是典型映射；不在表内的算子按
  语义归入最近类别，并在报告里说明归类理由。
- 同名算子可能因 shape/实现落不同单元（如大 Reduce 可能走 Cube 化的 GEMM 实现），
  有歧义时以产物中的 cycles/MFU 表现交叉验证。

---

## §3 设计倾向（proposer 用）

Pre-design reference, not a latency gate. The post-implementation
`mfu-analyzer` measurement is authoritative.

### Prefer

- Dense matrix-heavy paths with dimensions that tile into regular, aligned
  blocks; keep reduction and feature dimensions stable where possible.
- Reusable batched matmul, convolution, and attention projections rather than
  many tiny heterogeneous operators.
- Regular batch, sequence, and channel dimensions with small tail tiles. Where
  semantics permit, use padding or structured projection for irregular tails.
- Operator sequences that expose fusion and keep compatible layouts, reducing
  repeated format conversion and DMA traffic.
- Bounded-depth residual or gated blocks with predictable memory reuse instead
  of long serial chains of small standalone operations.

### Be cautious

- Very small matmuls, irregular dimensions, dynamic control flow, repeated
  transpose/layout changes, and frequent host-device transfers.
- Replacing meaningful normalization, attention, or gating with a cheaper
  activation without an accuracy hypothesis and training plan.
- Parallel branches whose outputs immediately require expensive concatenation,
  reduction, or synchronization.

### Design checklist

1. Name the measured MFU root cause.
2. Explain changed shapes/operator groups and their hardware mapping.
3. State the retained business-information invariant.
4. Treat latency direction as a hypothesis until MFU measurement.
5. Record exact source files and a reversible implementation plan.

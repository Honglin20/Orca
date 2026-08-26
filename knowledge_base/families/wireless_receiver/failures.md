# 无线接收机族 AVOID 清单（families/wireless_receiver/failures.md）

> 用途：Analyst 驳回"降 FLOPs 但硬件不友好 / 是领域陷阱"的假设时的**黑名单**。每条解释**为什么昇腾不友好或为什么是陷阱**，并给"如果真要同等语义该怎么办"的重构方向。硬件铁律编号 `§A1…§A9` 指 `common/ascend_constraints.md`。
>
> **fail-loud 总前提**（plan §10.1）：无任何 move 在「昇腾 + OFDM 接收机」有公开实测，以下"昇腾不友好"判定均为算子形状推断；落地前必须 micro-bench。最大的风险不是清单里的任何单条，而是**第 15 条**——未测 conv-only baseline 就盲改混合。

---

## A. 算子级禁区（直接 Cube 不友好）

### 1. ❌ Depthwise-separable conv（Cube 饿死；实测更慢）

- **陷阱**：通用移动端 CNN 文献（MobileNet/CMT）强推 DW-sep / depthwise-LPU 降 FLOPs；但本族目标硬件是昇腾。
- **为什么昇腾不友好**：DW（groups=C）把一个 Cube GEMM 拆成 C 个小向量运算，Cube 的 16×16 tile 里 15/16 lane 是空的，利用率崩（§A3）；且 DW 是 memory-bound（参数少 activation 多）。V5 实测在本场景更慢。
- **来源**：MobileNet DW memory-bound 跨硬件共识 [arXiv:1704.04861]；CMT depthwise-LPU [arXiv:2107.06263]；本仓库 plan §7 否决记录。
- **重构**：用 pointwise（1×1）或标准 3×3 conv 做局部混合；要扩感受野用 dilated conv（`primitives.md` §6）。否掉所有"DW 降 FLOPs"建议。

### 2. ❌ Group conv（groups >> 1）

- **陷阱**：ShuffleNet 系列用 group+shuffle 降 1×1 conv FLOPs。
- **为什么昇腾不友好**：同 §A3——组内通道数 <16 时 Cube 退化成 Vector；MAC/FLOPs 比随 groups 上升而恶化。g≥8 在本硬件上**实测可能更慢**（ShuffleNet V2 G2 结论的昇腾版）。
- **来源**：ShuffleNet V2 G2 [arXiv:1807.11164]；plan §7。
- **重构**：g=2 可接受（接近标准 conv）；要省 FLOPs 用 pointwise + bottleneck（`common/primitives.md` §3），不要上大 group。

### 3. ❌ 动态 shape（Host dispatch 重编译）

- **陷阱**：data-dependent 分支、变长输入、`if shape>...` 的条件 reshape。
- **为什么昇腾不友好**：昇腾最优 `.om` 是单 shape 编译（§A6），动态 shape 回退 Host dispatch（CPU 侧重算 tiling、几 ms 重下发）；或触发编译器重编译（首次几百 ms）。
- **来源**：plan §7；llm.npu [arXiv:2407.05858]。
- **重构**：变长输入用 padding + mask；data-dependent 分支见第 11 条的 zero-mask 重构。

### 4. ❌ 手搓 attention（丢融合 + TransData）

- **陷阱**：像 baseline 那样自己写 `bmm(q,k)→softmax→bmm(at,v)`，外加 reshape/permute 切 q/k/v。
- **为什么昇腾不友好**：3 个独立 GEMM + softmax 中间 N×N 落 HBM + 每个 GEMM 边界触发 TransData（§A2、§A7）；softmax 走 Vector 核。baseline attention 仅占 17% 但 TransData 大头来自它周围的格式切换。
- **来源**：plan §1+§7；FastAttention on Ascend [arXiv:2410.16663]；昇腾融合算子文档 https://www.hiascend.com/document/detail/zh/Pytorch/60RC1/ptmoddevg/trainingmigrguide/performance_tuning_0027.html 。
- **重构**：保留 attention 路线 → 重排成 `npu_fusion_attention` 形状（head_dim÷16，§A7）；否则折叠成线性（M13）或砍掉（M15）。

### 5. ❌ 非融合 elementwise 中间层

- **陷阱**：在主路径插入 `x*sigmoid(x)`、`sign(x)*max(...)`、自定义门控 mul 等独立 elementwise 算子且中间结果落 HBM。
- **为什么昇腾不友好**：逃出 AutoFuse/LUBAN 融合规则（§A5），每个 elementwise 留一次 Vector launch + 一份 activation 读写。多个串联会变 launch-bound。
- **来源**：plan §7；昇腾 AutoFuse 文档 https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/ 。
- **重构**：能用 `Conv+Bias+ReLU` / `MatMul+Bias+GELU` 这类规则链表达的就别拆；delay-domain soft-threshold（`primitives.md` §7）这类必须 elementwise 的，收敛在一处、τ→0 可关。

### 6. ❌ LayerNorm 不 fold（能换 BN 就换）

- **陷阱**：沿用 Transformer 默认的 LN，且 `elementwise_affine=False`（baseline 现状），推理期仍占 Vector 开销。
- **为什么昇腾不友好**：BN 可被 `ConvBatchnormFusionPass` 折进 conv（§A4，免费）；LN/RMSNorm 是跨通道 Vector 归约、不可 fold、留一次 Vector launch。RMSNorm < LN（少一次减均值）但仍不能 fold。
- **来源**：plan §7；RMSNorm [arXiv:1910.07467]；`common/ascend_constraints.md` §A4。
- **重构**：能换 BN 就换（M1），batch 小用 GroupNorm 或冻结统计 BN；必须留 LN 时接受这份开销、不要再叠加别的 elementwise。

---

## B. 方向级陷阱（attention 替换 / 低比特 / 递归 / 动态深度）

### 7. ❌ N=64 下 linear / Performer / FlashAttention / Nyströmformer（attention 才占 17%）

- **陷阱**：通用 Transformer 加速文献强推"线性 attention / 稀疏 attention"降 O(N²)。
- **为什么是陷阱**：(a) 本模型 attention 只占 17%（plan §1），seq=64 下 N² 项 <0.4%，常数项吃光所有收益；(b) 这些变体多是非标准 attention 形状，昇腾融合算子（`npu_fusion_attention`）不支持，反而触发手搓（第 4 条）；(c) 真正的瓶颈是 TransData（§A2），不是 attention FLOPs。
- **来源**：plan §1+§7；Performer [arXiv:2009.14794]；Nyströmformer [arXiv:2103.00775]；Linformer [arXiv:2006.04768]。
- **重构**：方向反过来——减 domain crossing（§A2）、折叠 attention 成线性（M13）、或先测 conv-only（M15/第 15 条）。

### 8. ❌ BNN / 二值化

- **陷阱**：边缘推理文献里 BNN 大幅降时延。
- **为什么昇腾不友好**：昇腾 Cube 只认 FP16/INT8（§A8），无原生 1-bit 路径；BNN 要 lowering 成多 bit，反而更慢。无线 OFDM 低 SNR 下 BNN 实测掉 1–3 dB，无本场景公开实测。
- **来源**：plan §7。
- **重构**：用 INT8 PTQ via AMCT（M16，§A8，Cube INT8≈2× FP16），别碰 BNN。

### 9. ❌ INT4 on 昇腾（仅 FP16 + INT8）

- **陷阱**：通用 LLM 量化文献（GPTQ/AWQ）常引 INT4。
- **为什么是陷阱**：昇腾无原生 INT4（§A8）；某些 FPGA 论文（如 SPiNN/MSQ 的 INT4 说法）在昇腾上要降级为"INT8-only"。型号未定前 fail-loud（plan §10.4，310 vs 910 能力不同）。
- **来源**：plan §7+§10.4；SPiNN [arXiv:2205.06159]（其 MSQ/LUT/INT4 部分在昇腾弃用）。
- **重构**：BCR block 剪枝在昇腾上降级为 INT8-only（M31）；正量化路径就是 AMCT INT8（M16）。

### 10. ❌ SisRafNet 式 GRU 频率递归（扫描链）

- **陷阱**：沿频率轴跑 GRU 做频率维递归。
- **为什么昇腾不友好**：GRU 是串行扫描链，Cube 无法并行吃，AiCore 利用率崩；且 GRU 的门控 elementwise 不可融合（第 5 条）。
- **来源**：plan §7（SisRafNet 标注为扫描链陷阱）。
- **重构**：频率维 mixing 用 conv（§1）或 FFT-mixing（`primitives.md` §8），不要用 RNN 扫描。

### 11. ❌ early-exit / 动态深度的数据依赖分支（用 zero-mask 重构）

- **陷阱**：按 SNR / 置信度提前退出（early-exit、LOREN/DSD24 风格）。
- **为什么昇腾不友好**：data-dependent 控制流 = 动态 shape = Host dispatch（第 3 条、§A6）。
- **来源**：plan §7+§5 M30；DSD24/LOREN。
- **重构**：`skip-via-zero-mask`——层照算但出口乘 0 mask，shape 不变、图静态（M30）；代价是不省该层的 Cube 功，需与"省 dispatch 重编译"权衡。

### 12. ❌ Toeplitz / FFT-fast 形（碎片 FFT kernel）

- **陷阱**：把线性层实现成 Toeplitz 矩阵的 FFT 加速形式（O(N log N)）。
- **为什么昇腾不友好**：FFT-fast 形拆成多个小 FFT kernel，昇腾 FFT 走 Vector 核、碎片小 FFT 易慢；与相邻 Cube 边界触发 TransData（§A2）。
- **来源**：plan §7；Toeplitz 线性层 [arXiv:2305.04749]（ICLR23，其 dense 形可、FFT 形禁）。
- **重构**：Toeplitz 用 **dense 重构形**（M23，展开成密集矩阵走标准 GEMM），不要用 FFT-fast 形；要 FFT 就用"整段大 FFT"（`primitives.md` §8）。

---

## C. 文献/基准误引陷阱

### 13. ⚠️ CKDNet 查无此文，勿引

- **陷阱**：某些综述/搜索把 "CKDNet" 当作无线接收机 SOTA 引用。
- **为什么是陷阱**：复现面查无对应论文（plan §7），引用它会污染变异方向的证据链。
- **来源**：plan §7（fail-loud 记录）。
- **重构**：剔除；改引本族有实测部署的 SOTA：DeepRx [arXiv:2005.01494]、EqDeepRx [arXiv:2602.11834]、NVIDIA NRX [arXiv:2312.02601]。

### 14. ⚠️ Channelformer 是 SISO 下行，勿当 MIMO 基准

- **陷阱**：把 Channelformer [arXiv:2302.04368]（D5）当作 MIMO 上行接收机的精度/时延基准来对比或超越。
- **为什么是陷阱**：Channelformer 实为 **SISO 下行信道估计**（CE），任务设定、输入维度、评价口径都与本族 MIMO 上行均衡/检测不同；它的"浅 attn + CNN decoder"是 CE 结构模板，不是本任务的公平基准。
- **来源**：plan §4 注 / §7；[arXiv:2302.04368]。
- **重构**：可借鉴其"浅 attn 做 input precoding + CNN 主干"的结构形态（D5），但对比基准只能用 DeepRx/EqDeepRx/NRX 这类同任务实测模型（plan §4 注）。

---

## D. ★ 流程级最大风险（T0 gating）

### 15. ⚠️ 未测 conv-only baseline 就盲改混合（最大风险）

- **陷阱**：上来就在"Conv1d + per-channel attention"的混合结构上做 M4/M5/M7 这类小修小补，默认"必须保留 Transformer"。
- **为什么是最大陷阱**：
  - (a) 领域反证——DeepRx / EqDeepRx / NVIDIA NRX 三个**实测部署**的 SOTA 神经接收机**都不用 softmax attention**（plan §1）。Transformer-over-time 在本领域是结构异类。
  - (b) 混合结构天然吃 TransData 税（§A2）——只要还留 attention，就一直在给这个税打补丁；conv-only 则整条都在 Cube GEMM-land，无 TransData（plan §4 D1/D2 标 ✅✅）。
  - (c) 若 conv-only baseline 已达精度，所有 attention 相关 move（M7/M8/M10/M12/M13）都是白费——这是机会成本最大的误判。
- **来源**：plan §1（领域反证）+ §10.5（T0 gating）+ §7（最大风险标注）；DeepRx [arXiv:2005.01494]、EqDeepRx [arXiv:2602.11834]、NRX [arXiv:2312.02601]。
- **重构（强制流程）**：**T0 gating**——第一轮先测 conv-only baseline（D1 DeepRx 风格 dilated DW-resblock 或 D6 Conv+GNN，注意本族禁 DW 用第 1 条重构），达标则把"弃 Transformer"立为正式方向；不达标再回到混合结构上做 move。Hypothesizer/Analyst 任何假设若跳过这一步，标注为"未过 T0"。

---

## Analyst 驳回规则

- 任何假设命中第 1–12 条之一（算子/方向陷阱），**直接 reject** 并引用对应条号 + 铁律编号（§A1…§A9）。
- 第 13/14 条（文献误引）：reject 该引用，要求换源。
- 第 15 条（未过 T0）：不 reject 假设本身，但强制降优先级、标"待 T0 gating 复核"。
- 若某轮 msprof 实测发现清单里某条在本型号上**反例成立**（如某 910 型号对 DW 友好），不要删本条，在该条"为什么昇腾不友好"末尾补带 run id + 型号的反例。

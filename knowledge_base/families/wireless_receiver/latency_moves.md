# 无线接收机（wireless_receiver）族降时延结构 move（本族核心）

> 用途：Hypothesizer 在「Agent 结构性探索 workflow」中提降时延假设时的**主菜单**。本族 = OFDM / MIMO 神经接收机（信道估计 / 均衡 / LLR 解码）。源模型 baseline = `SignalProcessingTransformer`（Conv1d-over-freq + per-channel 64×64 symbol attention + Conv-FFN，4 block），部署目标 = **昇腾 NPU (Atlas A2 / CANN)**。
>
> 统一格式：**名称 → 结构改动 → 降时延机理 → 精度风险与缓解 → 适用/不适用 → 来源**。每条标题紧跟四标：`【昇腾 ✅/⚠️/❌】【精度: 无/低/中/高】【物理: yes/no】【锚定: Dx/M#】`。
>
> **本族与 transformer/cnn 族最大的区别**：昇腾上 **TransData（格式转换）是主税**，不是 FLOPs。Conv 走 Im2Col + Cube（NC1HWC0，C0=16），attention matmul 走 NZ/ND——**每个 conv↔attention 边界都触发一次纯内存重排**（ASPLOS'25 / ATC'25 Hermes 专攻此问题）。因此「降时延机理」严格区分六条路径：**FLOPs / MAC访存 / kernel-launch / TransData / Cube利用率 / domain-crossing 数**——严禁笼统说"省算力"。
>
> 类别索引：[T1 融合层（零损失，先吃）](#t1-融合层零损失先吃) · [T2 结构层（保留混合）](#t2-结构层保留混合) · [T3 架构层（重新审视 attn）](#t3-架构层重新审视-attn) · [T4 量化剪枝（正交叠加）](#t4-量化剪枝正交叠加) · [B 类（研究扫描补充）](#b-类研究扫描补充) · [组合套餐](#组合套餐) · [反模式](#反模式) · [move 决策树](#move-决策树)。
>
> 引用：标 `[plan §x]` 来自 `docs/plans/2026-07-16-wireless-ascend-latency.md`；arXiv ID 直接标 `[2xxx.xxxxx]`；昇腾算子事实见 `common/ascend_constraints.md`。

---

## §1 瓶颈诊断（决定每条 move 的合法性边界）

**CPU profiling（方向性，绝对值以昇腾 msprof 为准）**：Conv1d(18 个) ~55%、Attention(bmm+softmax) ~17%、GELU ~7%、LayerNorm ~3%、reshape/permute/copy ~7%。

**两条核心结论（fail-loud 必记）**：

1. **attention 只占 17% 且 seq=N=64 太短** → linear / Performer / FlashAttention / Nyströmformer / Linformer 在 N=64 全是陷阱（N² 项 < 0.4%，常数项吃光收益）。**不要在短序列上堆线性化 attention。**
2. **昇腾根因 = TransData** → Conv (NC1HWC0) ↔ attention matmul (NZ/ND) 边界触发纯内存重排。**优化主轴 = 减少 domain crossing 次数，不是让 attention 更快。**

**领域反证**：DeepRx / EqDeepRx / NVIDIA NRX 三个实测部署的 SOTA 神经接收机**都不用 softmax attention**——Transformer-over-time 是该领域的结构异类。**T0 gating：先测 conv-only baseline (D1)，达标则放弃 Transformer。**

---

## T1. 融合层（零损失，先吃）

### M1. BN-fold（LN/RMSNorm → BN → fold 进 conv）

【昇腾 ✅✅】【精度: 无】【物理: no】【锚定: 全局/M1】

- **结构改动**：推理态把 `BN(Conv(x))` 用仿射合并等价写成 `Conv'(x)`——BN 的 `γ, β, μ, σ` 吸进 Conv 的 `W, b`。昇腾侧走 `ConvBatchnormFusionPass`（ATC 编译期 pass）**直接消除 norm 算子节点**。若原模型用 LayerNorm / RMSNorm，**先换成 BN 再 fold**——LN/RMSNorm 是沿通道归一化的 Vector 算子，**数学上不可 fold**（无逐通道仿射可合并）。
- **降时延机理**：① **kernel-launch**：每个 BN 节点少一次 launch；② **MAC访存**：免去 BN 那一遍 feature map 读写；③ **Cube利用率**：消 norm 算子后 conv 后续无 Vector 算子打断，Cube 流水不断流。注意：此 move **不降 FLOPs 主项**（conv 本体不变），降的是常数项与流水断点。
- **精度风险 + 缓解**：BN-fold 是数学等价 → **零精度损失**。换 LN→BN 才有精度风险（归一化轴变了），需 fine-tune 几 epoch。
- **适用 / 不适用**：✅ 所有推理态含 BN 的网络（部署前必做）；❌ 训练态（BN 统计要保留）；❌ 用 LN/RMSNorm 且任务对其敏感（NLP 风格特征）——只能换 BN 后 fold。
- **来源**：`[plan §5 T1]`；昇腾 `ConvBatchnormFusionPass` 见 `common/ascend_constraints.md`；通用 BN-fold（RepVGG 重参数化的基础，Ding 2021）。

---

### M2. `torch.compile(backend=npu)` + AutoFuse

【昇腾 ✅】【精度: 无】【物理: no】【锚定: 全局/M2】

- **结构改动**：训练 / 推理脚本包一层 `model = torch.compile(model, backend="npu")`，编译器走 AutoFuse（即 LUBAN 融合策略）+ ATC → `.om`。**零模型代码改动**。自动识别可融合模式：Conv+BN+ReLU、MatMul+Bias+GELU、reshape/copy 串联。
- **降时延机理**：① **kernel-launch**：串联的 reshape/copy/GELU/LN 被融进前一个计算 kernel，launch 数大幅减少；② **MAC访存**：中间 tensor 不落 HBM；③ **TransData（间接）**：AutoFuse 把无意义 permute 链折叠后，残留的格式转换至少少一遍访存。**不降 FLOPs 主项。**
- **精度风险 + 缓解**：编译等价 → **零精度损失**。缓解：静态 shape 必须固定（否则 recompilation）。
- **适用 / 不适用**：✅ 所有静态 shape 模型；✅ reshape/copy 占比高的 baseline（本模型 7%）；❌ 动态 shape（Host dispatch 重编译，反噬）；❌ 已手写融合 kernel 的算子（重复融合）。
- **来源**：`[plan §5 T1]`；`common/ascend_constraints.md` §5；CANN LUBAN AutoFuse。

---

### M3. 静态 shape + 通道÷16 对齐

【昇腾 ✅】【精度: 无】【物理: no】【锚定: 全局/M3】

- **结构改动**：把所有 conv / linear 的通道数调成 16 的倍数（C0=16 对齐），batch / seq / 频点 / 符号轴固定为静态值（sinking dispatch 而非 Host dispatch）。
- **降时延机理**：① **Cube利用率**：Cube tile = 16×16×16，通道不齐时 tile 尾巴 padding 浪费 Cube 周期；对齐后 Cube 满载；② **kernel-launch（间接）**：静态 shape 让 ATC 一次编译 + tiling cache，避免 Host 端动态 dispatch 的 recompilation；③ **TransData（间接）**：对齐的 shape 让 NC1HWC0 ↔ NZ 的 reshape 走整块拷贝而非 strided permute。
- **精度风险 + 缓解**：调通道数到 16 倍数可能有 ±1 channel 的差异 → 等效换 BN / fine-tune 几 step 补回；**本质无精度损失**。
- **适用 / 不适用**：✅ 所有昇腾部署（几乎强制）；❌ 输入分辨率 / batch 必须动态的场景（只能分桶 + tiling cache）。
- **来源**：`[plan §5 T1]`；`common/ascend_constraints.md` §6 §9。

---

### M4. pointwise 化（3-tap Conv1d → 1×1）

【昇腾 ✅✅】【精度: 中】【物理: no】【锚定: D0/M4】

- **结构改动**：把 baseline 的 3-tap（kernel=3）Conv1d-over-freq 换成 1×1 Conv1d（kernel=1）。**关键**：1×1 conv **无 im2col**，直接是纯 GEMM——既省 Im2Col 的开销，又**直接消除 TransData 触发点**（GEMM 域与 attention matmul 同属 NZ/ND，不再跨界）。
- **降时延机理**：① **TransData**：3-tap conv 是 NC1HWC0（Cube 域），紧跟 attention 是 NZ/ND（matmul 域）→ 每个边界一次纯内存重排；换成 1×1 后整段 conv 段也在 matmul 域，**domain-crossing 数下降到 0**；② **FLOPs**：1×1 比 3-tap 省 3× conv FLOPs；③ **Cube利用率**：1×1 是纯 GEMM，Cube 满载（无 im2col 的 fill 开销）。**昇腾双赢：降时延 + 降 TransData。**
- **精度风险 + 缓解**：丢邻频平滑（3-tap 的频率维感受野没了）→ 风险中。**缓解**：配 M9（delay-domain soft-threshold 补频率选择性）或 M20（dilated conv 在别处补感受野）。
- **适用 / 不适用**：✅ conv↔attention 边界处的 3-tap（TransData 税最重）；✅ 频率维平滑可由别处补的场景；❌ 该层是唯一频率维感受野来源（会塌频率分辨率）。
- **来源**：`[plan §5 T1]`；1×1 conv = GEMM 是昇腾算子事实（`common/ascend_constraints.md` §3）。

---

### M5. QKV-fold + stem→QKV 重参数化（3 投影 → 1）

【昇腾 ✅】【精度: 无】【物理: no】【锚定: D0/M5】

- **结构改动**：把 attention 入口的 3 个独立投影 `W_Q, W_K, W_V`（3 个 matmul）合并为单个 `W_QKV`（1 个大 matmul，输出 split 三段）。EfficientFormer 思路。若 stem conv 的输出维度正好等于 QKV 的输入，进一步把 stem 重参数化进 QKV 投影。
- **降时延机理**：① **kernel-launch**：3 个小 GEMM → 1 个大 GEMM，launch 数 ÷3；② **Cube利用率**：单个大 GEMM 的 tile 利用率高于 3 个小 GEMM；③ **MAC访存**：输入 feature map 只读一遍（不是三遍）。**代数等价，不降精度。**
- **精度风险 + 缓解**：代数等价 → **零精度损失**。
- **适用 / 不适用**：✅ 任何独立 Q/K/V 投影的 attention（本模型命中）；❌ 已用 GQA / MQA 共享 KV 的（QKV 维度不对称，合并收益小）；❌ 已 fused 的 vendor kernel（重复优化）。
- **来源**：`[plan §5 T1]`；EfficientFormer (Xu 2022)。

---

## T2. 结构层（保留混合）

### M6. 减 block 4 → 2-3 + 蒸馏

【昇腾 ✅】【精度: 中】【物理: no】【锚定: D0/M6】

- **结构改动**：把 4 个 Conv-Transformer block 删到 2-3 个（优先删中段，保留首尾），配 Knowledge Distillation（见 M14）把深 teacher 的输出蒸到浅 student。
- **降时延机理**：① **kernel-launch**：顺序依赖步数下降，每个 block 内部若干 kernel launch 线性减少——这是 wall-clock 下降的主因；② **TransData**：每个 block 都有 conv↔attn 边界，少一个 block 就少一组 domain crossing；③ **FLOPs**：按层数线性下降。
- **精度风险 + 缓解**：深度不足 → 抽象层级缺失，**风险中**。缓解：① 必须 pre-norm + 残差 identity 保梯度流；② 配 M14 KD 从 4-block teacher 蒸；③ 优先删中段（语义未完全形成），保留首尾。
- **适用 / 不适用**：✅ 过参数化的 4-block baseline（本模型）；❌ 本来就浅（2 block 再删塌了）；❌ 精度敏感且无 KD 预算。
- **来源**：`[plan §5 T2]`；More Layers Distillation 共识。

---

### M7. 调昇腾融合 attn 算子 `npu_fusion_attention`（禁手搓）+ head_dim÷16

【昇腾 ✅】【精度: 无】【物理: no】【锚定: D0 D7/M7】

- **结构改动**：把 baseline 手搓的 per-channel 64×64 symbol attention（bmm + softmax + scale）替换为昇腾原生融合算子 `npu_fusion_attention`。**禁手搓**——手搓的 bmm/softmax 串联会丢融合机会，且每步都是独立 matmul kernel，触发额外 TransData。同时把 `head_dim` 调成 16 的倍数（昇腾融合 attn 的 tile 要求）。
- **降时延机理**：① **TransData**：融合算子内部 Q/K/V projection + bmm + softmax + V-projection 在同一 tile 流水内完成，**不落 NZ/ND 中间 tensor** → 无额外 domain crossing；② **kernel-launch**：5-6 个独立 kernel（Q·K、scale、mask、softmax、attn·V、out proj）→ 1 个融合 kernel；③ **MAC访存**：attention 矩阵不落 HBM。
- **精度风险 + 缓解**：算子语义等价 → **零精度损失**（head_dim÷16 可能要调 head 数，需 fine-tune）。缓解：head_dim 调整后跑一轮 KD 对齐。
- **适用 / 不适用**：✅ 所有昇腾部署的 attention（强制）；✅ seq ≥ 16、head_dim ÷ 16；❌ head_dim 奇数 / 无法对齐 16；❌ 非昇腾后端（用 FlashAttention 代替）。
- **来源**：`[plan §5 T2]`；`common/ascend_constraints.md` §7；CANN `npu_fusion_attention`。

---

### M8. windowed / Swin 局部 attn（时间轴 W=16）

【昇腾 中-✅】【精度: 低】【物理: yes】【锚定: D7/M8】

- **结构改动**：把时间轴全 softmax attn 改成**局部窗 attn**，窗长 W=16（典型值），即只 attend 到 `|i − j| ≤ W` 的符号。高铁 / mmWave 场景 W 可自适应（多普勒大则 W 小）。
- **降时延机理**：① **FLOPs**：`O(N²·d)` → `O(N·W·d)`，N=64, W=16 时约 4× FLOPs 下降；② **kernel-launch**：windowed mask 的 bmm tile 友好（局部性）；③ **TransData（不变）**：仍是一次 matmul 域计算，domain crossing 数不变。注：昇腾融合 attn 对 windowed 的支持要看算子版本，老版本需 fallback 手搓 → 退化为 ⚠️。
- **精度风险 + 缓解**：远距离符号依赖丢失 → **风险低**。物理由：**相干时间**——无线信道在相干时间内才强相关，超出相干时间的符号本就近似独立，windowed 是物理正确而非妥协。缓解：W 取相干符号数的 1.5-2×；极端多普勒用 W 自适应。
- **适用 / 不适用**：✅ 多普勒受限场景（高铁、mmWave）；✅ N ≥ 64 想压 attention；❌ 静止 / 低频场景（相干时间长，W 要大，收益小）；❌ 融合算子不支持 windowed 的昇腾版本。
- **来源**：`[plan §5 T2]`；`[2510.12941]`（windowed axial attn）；相干时间 = 信道时变的物理常数。

---

### M9. Conv1d↔Transformer 间插可学习 delay-domain soft-threshold

【昇腾 ✅】【精度: 低】【物理: yes】【锚定: D8/M9】

- **结构改动**：在 Conv1d 段与 Transformer 段之间插一层 delay-domain soft-threshold：`y = x · sigmoid(τ · |x|)`（τ 可学习）。τ→0 时退化为 identity（**fail-forward**：训不坏就退回原模型）。
- **降时延机理**：本身**不直接降时延**——它的价值是**补偿 M4 pointwise 化丢的频率选择性**，让 M4 可以放心做。机理上是把 3-tap conv 的频率平滑角色，移到一个轻量（单 element-wise）的可学习非线性上。① **kernel-launch**：只 +1 个 element-wise kernel（廉价）；② 让 M4 的 TransData 收益能落地。
- **精度风险 + 缓解**：τ 初始化为 0 → identity 起步 → **风险低，fail-forward**。物理由：多径信道在 delay 域稀疏（ℓ1 先验），soft-threshold 显式建模这种稀疏性。
- **适用 / 不适用**：✅ 已做 M4 pointwise 化、需要补频率选择性的场景；✅ 多径稀疏信道；❌ delay 域非稀疏（密集多径）。
- **来源**：`[plan §5 T2]`；`[2104.13656]`（ISTA-Net / unfolded）；物理 = 多径 ℓ1 稀疏先验。

---

### M10. FFT-mixing 替时间轴 softmax attn

【昇腾 ⚠️】【精度: 中】【物理: yes】【锚定: D4/M10】

- **结构改动**：把时间轴 softmax attn（`softmax(QK^T)V`）替换为 FNet 式 FFT-mix：对 token 维做 `FFT → 实部/虚部 → IFFT`，无 softmax、无可学习 Q/K/V。
- **降时延机理**：① **FLOPs**：`O(N²)` → `O(N log N)`，N=64 时理论收益小（N²=4096 vs NlogN=384），但常数项比 softmax bmm 低；② **TransData（待验）**：FFT 在昇腾上是碎片化的 r2c kernel，**融合效果待验证**——若不能融进相邻 conv，反而新增 domain crossing。这是标 ⚠️ 的主因。
- **精度风险 + 缓解**：softmax 的尖锐选择丢失，长程检索能力下降 → **风险中**。物理由：Doppler 域稀疏（移动场景下 Doppler 频移集中），FFT-mix 物理合理。缓解：先验 FFT 幅度，丢相位信息有损；用 complex-valued FFT 保相位。
- **适用 / 不适用**：✅ Doppler 稀疏场景（移动）；❌ 静止信道（Doppler 集中在 DC，FFT-mix 退化为低通，收益小）；❌ 昇腾 FFT 算子版本旧（碎片 kernel）。
- **来源**：`[plan §5 T2]`；`[2105.03824]`（FNet）。

---

### M11. 4 antenna port 共享 DetectorNN + 前置 LMMSE

【昇腾 ✅✅】【精度: 低】【物理: yes】【锚定: D2 D10/M11】

- **结构改动**：把 4 个 antenna port 各自独立的 DetectorNN 实例，改为**共享同一份权重**（4 路前向复用），并在 DetectorNN 前置一个 LMMSE 估计（4 port 并行）。EqDeepRx 核心。
- **降时延机理**：① **FLOPs**：4 份 detector → 1 份（共享权重，batch 维合并 4 port），**~4× 计算削减**；② **kernel-launch**：4 路独立前向 → 1 路 batch=4 前向，launch 数 ÷4；③ **Cube利用率**：batch=4 让 Cube tile 更满；④ **TransData（不变）**：domain crossing 数不降，但每次 crossing 的张量更大、摊薄开销。
- **精度风险 + 缓解**：4 port 共享权重的前提是**它们经历同一物理信道统计**（同一 UE 的多天线）→ **风险低**。若 4 port 实际是不同 UE / 不同极化方向，共享会塌。缓解：前置 LMMSE 把 4 port 对齐到同一信道估计坐标系，再共享。
- **适用 / 不适用**：✅ 单 UE 多天线（同一信道统计）；✅ MIMO 上行；❌ 多 UE / 多极化（信道统计不同）；❌ 4 port 已天然解耦（无需再共享）。
- **来源**：`[plan §5 T2]`；`[2602.11834]`（EqDeepRx）。

---

### M12. 低秩 Q/K 投影（attention down-score）

【昇腾 ✅】【精度: 低】【物理: no】【锚定: D0/M12】

- **结构改动**：把 Q/K 投影的输出 rank 从 `d` 降到 `r`（`r = d/2` 或 `d/4`），即 `W_Q ∈ R^{d×r}` 而非 `R^{d×d}`。EfficientFormerV2 思路。
- **降时延机理**：① **FLOPs**：Q/K projection FLOPs ÷ (d/r)；attention bmm 的内矩阵维度也降；② **kernel-launch（不变）**：仍是相同数量的 matmul kernel，只是更小；③ **TransData（不变）**。**注意**：attention 本身才占 17%，这条收益上限低。
- **精度风险 + 缓解**：Q/K rank 不足 → attention 的检索能力下降 → **风险低**（无线信号相关性高，低秩假设成立）。缓解：r 别低于 d/4；只降 K 不降 Q（保留 query 多样性）。
- **适用 / 不适用**：✅ 已决定保留 attention 且想压它；❌ N=64 想靠这条大幅降时延（attention 占比太小）；❌ 与 M13 fold-to-linear 冲突（M13 直接消 attn，更彻底）。
- **来源**：`[plan §5 T2]`；EfficientFormerV2。

---

## T3. 架构层（重新审视 attn）

### M13. 部署期 Transformer 折叠成线性滤波器（A-MMSE, rank-adaptive）

【昇腾 ✅✅】【精度: 中】【物理: yes】【锚定: D3/M13】

- **结构改动**：训练期保留 Transformer（学非线性残差），**部署期把整个 attention + FFN 序列折叠成单一线性 matmul** `y = W_fold · x + b_fold`（A-MMSE 思路）。`W_fold` 的 rank 可调（保留 top-r 奇异值），rank=full 时逼近原 Transformer，rank=r 时退化为 r 阶线性 MMSE 滤波器。
- **降时延机理**：① **FLOPs**：整个 Transformer 段 → 单个 GEMM，FLOPs 下降一个量级；② **TransData**：**零 domain crossing**——折叠后只剩一个 matmul，全程在 NZ/ND 域，无 conv↔attn 边界；③ **kernel-launch**：几十个 kernel → 1 个 GEMM；④ **Cube利用率**：单 GEMM 满载。**这是消 TransData 最彻底的一招。**
- **精度风险 + 缓解**：线性折叠丢失 Transformer 的非线性表达 → **风险中**。物理由：LMMSE 本身就是线性最优滤波器，rank 足够时线性近似 loss 很小。缓解：① rank-adaptive，先全 rank 测精度，逐步降 rank 找拐点；② 折叠误差大的样本走 M14 KD 补；③ 训练期加 "linear-consistency" loss（鼓励 Transformer 输出接近一个线性映射）。
- **适用 / 不适用**：✅ 部署期固定 shape；✅ 信道接近线性（低 SNR 除外）；❌ 信道强非线性（高阶调制 + 极低 SNR）；❌ 训练期（折叠只发生在部署）。
- **来源**：`[plan §5 T3]`；`[2506.00452]`（A-MMSE）。

---

### M14. KD 成 conv-only student

【昇腾 ✅✅】【精度: 中】【物理: no】【锚定: D11/M14】

- **结构改动**：把当前 Conv+Transformer 的 baseline 作 teacher，蒸馏到一个 **conv-only student**（D1 DeepRx 风格：dilated DW-sep conv ResNet，无 attn）。**关键设计（区别于 NLP KD）**：
  - **输出级 MSE KD**（不是 logit-KL）：无线接收机是**信号回归任务**（输出 LLR / 均衡符号），不是分类，MSE 才是正确损失。`L = α·MSE(student_out, label) + (1−α)·MSE(student_out, teacher_out.detach())`。
  - **teacher 冻结 `no_grad`**：teacher 只前向不反传，省一半反向 FLOPs。
  - **走脚本侧 `--distill-from` flag**：在训练脚本入口加一个 flag 指定 teacher checkpoint，**零模型代码改动**（student 模型类不变）。
  - **可选升级 FitNets feature-KD**：在 student / teacher 中间层 hook 抓 feature，配 1×1 conv 投影对齐维度后做 feature-level MSE。这是比输出级 KD 更强的监督，但需改模型代码（加 hook + 投影层）。
- **降时延机理**：① **TransData**：student 是纯 conv → 全程 NC1HWC0 域，**无 conv↔attn 边界 = 零 domain crossing**（这是最大收益，teacher 的 attn 税全消）；② **FLOPs**：conv-only student 比 Conv+Transformer 少 attention 段 FLOPs；③ **kernel-launch**：消了 attention 的若干 kernel。
- **精度风险 + 缓解**：OOD（out-of-distribution）场景 student 泛化弱于 teacher → **风险中**。缓解：① teacher / student 数据分布必须覆盖部署场景；② 多 SNR / 多信道联合训；③ FitNets feature-KD 补中间层监督。
- **适用 / 不适用**：✅ 部署 = 昇腾（TransData 税最重，conv-only 收益最大）；✅ 有 teacher checkpoint；❌ student 容量远小于 teacher（KD 救不回）；❌ 任务强依赖非线性 attention（极少见）。
- **来源**：`[plan §5 T3]`；Zhu MDPI Sensors24（KD to conv student，99.3% 参数 / 97% 时延 / 0.5dB）；FitNets (Romero 2015)。

---

### M15. conv-only / conv+GNN baseline（达标则弃 Transformer）

【昇腾 ✅】【精度: 高】【物理: yes】【锚定: D1 D6/M15】

- **结构改动**：**T0 gating move**——在做任何混合架构改造前，先训两个 baseline：① conv-only（DeepRx 风格 dilated DW-sep conv ResNet）；② conv+GNN（NVIDIA NRX 风格：init Conv + 交替[GNN 用户图消息传递 → Conv 状态]×N）。若 conv-only 达标 → **直接放弃 Transformer**（领域反证支持：三个 SOTA 都不用 softmax attn）。
- **降时延机理**：① **TransData**：conv-only 零 domain crossing（纯 NC1HWC0）；conv+GNN 的 GNN 也是 matmul，但 graph 边是 MIMO 层全连接，结构规整 → crossing 数可控；② **FLOPs**：conv-only 比 Conv+Transformer 少 attn；③ **Cube利用率**：conv+GNN 的 GNN 走 dense matmul，Cube 满载（优于 DW 饿死）。
- **精度风险 + 缓解**：**风险高（取决于数据）**——conv-only 在简单信道下持平或超过 Transformer，但在强多径 / 高阶调制下可能掉点。缓解：① 必须在你的数据集上测，不能信论文的结论；② conv-only 不达标再上 conv+GNN；③ 都不达标才回混合架构。
- **适用 / 不适用**：✅ 任何新项目第一步（T0 gating）；❌ 已有 Transformer checkpoint 且精度达标（不必重训）。
- **来源**：`[plan §5 T3]`；`[2005.01494]`（DeepRx）；`[2312.02601]`（NVIDIA NRX，<1ms on A100+TRT）。

---

## T4. 量化 / 剪枝（正交叠加）

### M16. INT8 PTQ via AMCT + 混合精度 / QAT

【昇腾 ✅✅】【精度: 低】【物理: no】【锚定: 全局/M16】

- **结构改动**：用昇腾 AMCT（Ascend Model Compression Toolkit）做 Post-Training Quantization 到 INT8，per-channel 量化权重，per-tensor 量化激活。精度不够则切 QAT（Quantization-Aware Training）或混合精度（敏感层留 FP16）。
- **降时延机理**：① **Cube利用率**：昇腾 Cube 单元 INT8 吞吐 ≈ 2× FP16（INT8 Cube 是硬件原生）；② **MAC访存**：权重 / 激活带宽减半；③ **TransData（不变）**：量化不改变 domain crossing 数，但每次 crossing 的数据量减半 → 重排开销同比下降。**与 T1-T3 正交，可叠加在任何架构上。**
- **精度风险 + 缓解**：PTQ 典型损失 < 1dB（QAT 可恢复到接近 FP16）→ **风险低**。缓解：① 敏感层（首层 conv、最后 LLR 输出）留 FP16；② 用 QAT fine-tune 几 epoch；③ 校准集覆盖全 SNR 范围。
- **适用 / 不适用**：✅ 所有昇腾部署（INT8 是标配）；✅ Cube-bound 模型；❌ 极低 SNR + 高阶调制（量化噪声占比大）；❌ 已是极小模型（量化 noise 占比大）。注：**昇腾无原生 INT4**（FPGA 的 MSQ INT4 说法降级）。
- **来源**：`[plan §5 T4]`；昇腾 AMCT；`common/ascend_constraints.md` §8。

---

### M17. 结构化剪枝 channel / block（非 DW）

【昇腾 ✅】【精度: 中】【物理: no】【锚定: D5/M17】

- **结构改动**：对 conv 的 channel 维做结构化剪枝（整 channel 置零 + 删除），基于 BN 的 γ 范数或一阶 Taylor 重要度。剪后 fine-tune。**关键约束：不剪 DW conv**——DW 是 per-channel 独立，剪一个 channel 等于删一个 DW 分支，Cube 立刻饿死（DW 本就难喂饱 Cube）。
- **降时延机理**：① **FLOPs**：剪掉 30% channel → 后续 conv FLOPs ÷ 1.4；② **Cube利用率**：标准 conv / pointwise conv 剪 channel 后仍是 dense GEMM，Cube 利用率不降；③ **kernel-launch（不变）**。
- **精度风险 + 缓解**：剪关键 channel 掉精度 → **风险中**。缓解：① 渐进剪（每轮剪 10% + fine-tune）；② 优先剪中段（语义未完全形成）；③ 剪完配 M14 KD 恢复。
- **适用 / 不适用**：✅ 标准.conv / pointwise conv 的 channel；✅ 过参数化 backbone；❌ DW conv（剪 channel 饿死 Cube）；❌ 分类头前的最后 conv（保精度）。
- **来源**：`[plan §5 T4]`；Channelformer（`[2302.04368]`）；结构化剪枝共识。

---

## B 类. 研究扫描补充

### M18. pilot-grid 输入富化（`[Y, Y⊙Xp*, Xp, mask]` 多通道）

【昇腾 ✅】【精度: 无】【物理: yes】【锚定: D1/M18】

- **结构改动**：把输入从单一 `Y`（接收信号）扩为多通道：`[Y, Y⊙Xp*, Xp, pilot_mask]`。`Y⊙Xp*` 是对接收信号做 pilot 共轭去调制（消除 pilot 子载波的相位），`pilot_mask` 标记哪些子载波是 pilot。
- **降时延机理**：**不是降时延 move**——它是**默认开**的输入富化，让模型不需要在第一层花 conv 算力去"重新发现"pilot 信息。① **FLOPs（间接）**：输入通道 +3 但省了模型内部学 pilot 关系的算力，净收益近零；② 让 M4 pointwise 化可行（pilot 信息已在输入，不需 3-tap 提取）。
- **精度风险 + 缓解**：**近零增益 / 无风险**（信息只增不减）。物理由：pilot 是已知信号，显式喂入是物理正确的 inductive bias。
- **适用 / 不适用**：✅ **每个候选都带**（非竞争项，默认开）；❌ 无 pilot 的盲接收场景。
- **来源**：`[plan §5 B]`；`[2005.01494]`（DeepRx 默认输入格式）。

---

### M19. residual-around-LMMSE（学 Δh = h − ĥ_LMMSE）

【昇腾 ✅】【精度: 低】【物理: yes】【锚定: D10/M19】

- **结构改动**：前置一个 LMMSE 信道估计 `ĥ_LMMSE`，NN 只学残差 `Δh = h − ĥ_LMMSE`，最终输出 `ĥ = ĥ_LMMSE + Δh`。NN 的目标从"学完整信道"变成"学 LMMSE 没学到的"。
- **降时延机理**：① **FLOPs**：NN 只学残差 → 容量需求下降 → 可以配 M6 减层 / M17 剪枝进一步压模型；② **TransData（不变）**；③ 让 T1-T3 的精度损失更容易被接受（LMMSE 兜底）。
- **精度风险 + 缓解**：LMMSE 是线性最优，残差通常小且稀疏 → NN 学得更快更稳 → **风险低**。缓解：LMMSE 的噪声白化要正确，否则残差非平稳。
- **适用 / 不适用**：✅ 信道估计任务；✅ LMMSE 已知或可算；❌ 端到端 LLR 解码（LMMSE 不是直接目标）；❌ LMMSE 本身不可得（无 CSI）。
- **来源**：`[plan §5 B]`；`[2009.01423]`。

---

### M20. dilated / multidilated conv 堆（rates {1,2,4,8}）

【昇腾 ✅】【精度: 低】【物理: yes】【锚定: D1/M20】

- **结构改动**：把标准 3×3 conv 堆替换成 dilated conv 堆，dilation rates `{1, 2, 4, 8}` 交替。同样参数量下感受野 RF=31（vs 3 层 3×3 的 RF=7）。
- **降时延机理**：① **FLOPs**：dilated conv 与标准 conv 同 FLOPs，但**单位 FLOPs 的感受野收益更高**——可以用更少层达到目标 RF，间接降 FLOPs；② **kernel-launch（间接）**：少层 = 少 launch；③ **Cube利用率**：dilated conv 仍是 dense Cube，不饿死。
- **精度风险 + 缓解**：dilation 过大有 gridding artifact（感受野空洞）→ **风险低**。缓解：rates 按等比递增；每 2-3 层插一个 rate=1 的 conv 填空洞。物理由：多径 CIR 在 delay 域有远近径，dilated 捕获不同延迟的多径。
- **适用 / 不适用**：✅ 需要大感受野的 conv 段；✅ 多径信道；❌ 已用 M4 pointwise（1×1 无 dilation 概念）；❌ 局部平滑任务（不需大 RF）。
- **来源**：`[plan §5 B]`；`[2005.01494]`（DeepRx 的 dilated ResNet）。

---

### M21. 轴向 / 可分 attn（time-then-freq）

【昇腾 ✅】【精度: 低】【物理: yes】【锚定: D7/M21】

- **结构改动**：把单个 2D attn（同时 attend time×freq）拆成两个 1D attn 串联：先沿时间轴 attn，再沿频率轴 attn。计算量从 `O((T·F)²)` 降到 `O(T·F·(T+F))`。
- **降时延机理**：① **FLOPs**：T=F=64 时，2D attn = 64⁴，轴向 = 2·64³，约 32× FLOPs 下降；② **kernel-launch**：+1 个 attn kernel（两个 1D）；③ **TransData**：两个 1D attn 都在 matmul 域，边界处理可控。
- **精度风险 + 缓解**：二维交互丢失（time-freq 联合模式学不到）→ **风险低**（单论文验证）。物理由：时频可分性（T-F separable）——信道在时间维和频率维的衰落近似独立。缓解：配 M8 windowed 限制每个 1D attn 的范围。
- **适用 / 不适用**：✅ 时频可分信道；✅ T·F 大（>32×32）；❌ 时频强耦合信道（双重色散极端场景）；❌ N=64 的单轴（收益小）。
- **来源**：`[plan §5 B]`；`[2510.12941]`。

---

### M22. CVNN + 编译期 real-pair lowering（2×2 block-real GEMM）

【昇腾 ⚠️】【精度: 中】【物理: yes】【锚定: 全局/M22】

- **结构改动**：用复值 CNN（CVNN）替换实值 CNN——conv 的权重和激活都是复数，前向走复数 GEMM。**昇腾无原生复数算子**，必须**编译期 lowering**：每个复数 element 展开成 2×2 block-real matrix `[[a, −b], [b, a]]`，复数 conv 变成 block-real GEMM。
- **降时延机理**：① **参数**：复数权重 = 一半参数表达等价复数信息（参数省）；② **FLOPs（上升）**：2×2 block-real lowering 后 FLOPs 反而 ×4（每个复数乘法 = 4 实数乘法）；③ **Cube利用率**：block-real GEMM 是 dense，Cube 满载。**净时延取决于参数省的带宽 vs FLOPs 升的计算**——通常在 I/Q 对称场景净收益为正。标 ⚠️ 是因为 lowering 需要编译器支持。
- **精度风险 + 缓解**：lowering 是精确等价 → **无精度损失**；但 CVNN 训练更难（复数梯度）→ 实测精度中。物理由：I/Q 是物理对称对，复数表征是物理正确。缓解：复 BatchNorm + 复 weight init。
- **适用 / 不适用**：✅ I/Q 对称的 PHY 信号；✅ 编译器支持 block-real lowering；❌ 昇腾编译器无 lowering pass（手搓会触发 TransData）；❌ 实数表征已足够的场景。
- **来源**：`[plan §5 B]`；`[1705.09792]`（Trabelsi, Deep Complex Networks）。

---

### M23. Toeplitz 线性层（dense 重构形）

【昇腾 ⚠️】【精度: 中】【物理: yes】【锚定: 全局/M23】

- **结构改动**：用 Toeplitz 线性层替换标准 Linear——权重是 Toeplitz 矩阵（由对角线常数决定，参数 O(N) 而非 O(N²)）。**关键**：部署时必须**重构成 dense 矩阵**（把 Toeplitz 展开成普通 dense matmul），**不能用 FFT 快速形**（昇腾 FFT kernel 碎片化，比 dense 还慢）。
- **降时延机理**：① **参数 / MAC访存**：Toeplitz 参数 O(N)，dense 重构后 matmul 仍是 O(N²) FLOPs 但权重内存 O(N)，**带宽下降**；② **Cube利用率**：dense 重构形 = 标准 GEMM，Cube 满载；FFT 形 = 碎片 r2c kernel，Cube 饿死（**FFT 形 ❌**）；③ **TransData**：dense 形零额外 crossing；FFT 形多次跨界。
- **精度风险 + 缓解**：Toeplitz 约束（权重沿对角线常数）限制表达力 → **风险中**。物理由：多径 CIR 在时域是 Toeplitz（卷积结构），物理正确。缓解：用 block-Toeplitz（分块对角常数）增加自由度。
- **适用 / 不适用**：✅ 信道估计（CIR 是 Toeplitz）；✅ 部署为 dense 形；❌ FFT 快速形（昇腾碎片 FFT）；❌ 权重无 Toeplitz 结构的任务。
- **来源**：`[plan §5 B]`；`[2305.04749]`（ICLR23）。

---

### M24. hypernetwork 按 slot 生成接收机权重

【昇腾 ✅】【精度: 中】【物理: yes】【锚定: 全局/M24】

- **结构改动**：用一个小的 hypernetwork（按 slot 索引 / CSI embedding 输入）生成主接收机的权重。每个 slot 推理时：hypernet 前向 → 生成权重 → 主网络前向。
- **降时延机理**：① **参数**：主网络权重不再存储，由 hypernet 动态生成——若 hypernet 比主网络小，总参数下降；② **kernel-launch（增加）**：每次推理 +hypernet 一次前向；③ **Cube利用率**：hypernet 是 dense GEMM，Cube 满载。净时延取决于 hypernet 大小。
- **精度风险 + 缓解**：hypernet 训练难（梯度通过权重生成反传）→ **风险中**。物理由：时变信道（每个 slot 信道统计不同），per-slot 定制权重是物理合理的自适应。缓解：hypernet 输入用 CSI embedding 而非 raw slot index；warm-start 从共享权重初始化。
- **适用 / 不适用**：✅ 强时变信道（高铁、mmWave）；✅ hypernet 远小于主网络；❌ 准静态信道（一个全局权重足够）；❌ hypernet 与主网络等大（无收益）。
- **来源**：`[plan §5 B]`；`[2408.11920]`。

---

### M25. INR coordinate-MLP CE（批量查询）

【昇腾 ✅（批量）】【精度: 中】【物理: yes】【锚定: 全局/M25】

- **结构改动**：信道估计（CE）任务用 Implicit Neural Representation（INR）——一个小的 coordinate-MLP，输入 (子载波索引, 符号索引)，输出该位置的信道估计。**关键**：所有查询点**批量拼接成一个大矩阵**，单次前向走一个大 GEMM，而非逐点查询。
- **降时延机理**：① **kernel-launch**：批量查询 = 1 次 GEMM，逐点 = N 次 small GEMM；② **Cube利用率**：批量 GEMM 满载 Cube；③ **参数**：INR 参数与查询点数无关（仅 coordinate-MLP 的权重）。标 ✅ 是指批量形；逐点形 kernel-launch 爆炸。
- **精度风险 + 缓解**：INR 有谱偏差（spectral bias，偏爱低频）→ 高频信道细节丢失 → **风险中**。缓解：用 Fourier feature encoding（坐标先过一次 FFT 基）提升高频表达。
- **适用 / 不适用**：✅ 批量查询（全 grid CE）；✅ 信道空时平滑假设成立；❌ 逐点在线查询（launch 爆炸）；❌ 信道突变（INR 平滑假设失效）。
- **来源**：`[plan §5 B]`；`[2605.10213]`。

---

### M26. 双均衡器（LMMSE+RZF）+ 每流共享 detector（EqDeepRx 核心）

【昇腾 ✅✅】【精度: 低】【物理: yes】【锚定: D2/M26】

- **结构改动**：EqDeepRx 完整结构——前置**两个并行均衡器** LMMSE（线性 MMSE）和 RZF（Regularized Zero-Forcing），两路输出 + 原 Y 拼接成多通道，喂给**每流共享的 DetectorNN**（多流复用同一份权重）。
- **降时延机理**：① **FLOPs**：每流共享 DetectorNN → 流数 N_stream 的计算变成 1 份（batch 维合并），**~4× 计算削减**（4 流典型）；② **kernel-launch**：N 路独立前向 → 1 路 batch=N 前向；③ **Cube利用率**：batch 大 → Cube tile 满；④ **TransData（不变）**：LMMSE / RZF 都是 matmul，DetectorNN 是 conv，仍有一次 domain crossing，但摊薄到 batch 维。与 M11 互补（M11 是 port 共享，M26 是流共享 + 双均衡器前置）。
- **精度风险 + 缓解**：共享 detector 前提是流间信道统计相近 → **风险低**。LMMSE+RZF 双路提供线性 / 迫零两种先验，detector 不再需要从零学。缓解：流间差异大时加 per-stream 小 adapter。
- **适用 / 不适用**：✅ MIMO 多流；✅ LMMSE / RZF 可算；❌ SISO（无多流共享空间）；❌ 流间信道差异极大。
- **来源**：`[plan §5 B]`；`[2602.11834]`（EqDeepRx）。

---

### M27. Mamba / SSM 沿子载波扫描

【昇腾 ⚠️】【精度: 高】【物理: yes】【锚定: D9/M27】

- **结构改动**：用双向 Mamba SSM 替换 attention，沿子载波轴扫描。`S_t = gate·S_{t−1} + K_t^T·V_t`，每子载波 O(1) 更新。
- **降时延机理**：① **FLOPs**：O(N·d²) vs attention O(N²·d)，N=64 时收益小（见 §1 结论 1）；② **kernel-launch（爆炸）**：**昇腾无原生 scan kernel**，CUDA 的 `mamba_ssm` 不移植，必须手搓 scan 循环 → 每个 subcarrier 一次 launch，**launch 数爆炸**；③ **Cube利用率**：scan 是串行依赖，Cube 流水不断流但每步利用率低。标 ⚠️ / 高风险：scan 不移植是硬伤。
- **精度风险 + 缓解**：**无公开 BER 报告** → 风险高。物理由：SSM 的状态空间模型适合时变稀疏系统。缓解：① 必须先在昇腾 micro-bench scan 性能；② 若 scan 太慢，改用 chunkwise 并行（但昇腾 chunk kernel 也无原生支持）。
- **适用 / 不适用**：✅ 长子载波序列（N ≫ 64）；✅ 昇腾未来支持 scan kernel；❌ **当前昇腾（scan 不移植）**；❌ N=64（收益小，风险大）。
- **来源**：`[plan §5 B]`；`[2601.17108]`；`common/ascend_constraints.md`（scan 无原生算子）。

---

### M28. Soft Graph Transformer detector（仅 SGT 形）

【昇腾 ⚠️】【精度: 中】【物理: yes】【锚定: 全局/M28】

- **结构改动**：MIMO 检测用 Soft Graph Transformer（SGT）——用户节点在图上消息传递。**关键**：部署只用 **SGT 形（banded attn = GEMM）**，**不用 GNN 形**（GNN 的稀疏消息传递在昇腾上是碎片 kernel，Cube 饿死）。
- **降时延机理**：① **Cube利用率**：SGT 的 banded attention 重构成 dense GEMM（带状 mask 在 dense 矩阵里 padding）→ Cube 满载；GNN 形的稀疏边 → Cube 饿死（**GNN 形 ❌**）；② **kernel-launch**：SGT 形 = 标准 matmul 调用；GNN 形 = scatter/gather 碎片 kernel。
- **精度风险 + 缓解**：banded 近似丢失远距离用户耦合 → **风险中**。物理由：MIMO 用户耦合在近邻层最强，banded 是物理合理近似。缓解：band 宽度取 MIMO 层数的 2-3×。
- **适用 / 不适用**：✅ MIMO 检测（用户图）；✅ 部署为 SGT banded 形；❌ GNN 稀疏消息形（昇腾 Cube 饿死）；❌ SISO（无用户图）。
- **来源**：`[plan §5 B]`；`[2509.12694]`。

---

### M29. MoE by SNR / mobility（top-1 硬门）

【昇腾 ➖】【精度: 低】【物理: yes】【锚定: 全局/M29】

- **结构改动**：把单个 detector 换成多个 SNR / mobility 专属 expert，top-1 硬门路由（`expert_idx = argmax(gate(SNR, mobility))`）。高 SNR expert 简单、低 SNR expert 复杂。
- **降时延机理**：① **FLOPs**：每条样本只激活 1 个 expert（不激活全部），理论 FLOPs = 1/E；② **kernel-launch（增加）**：+gate 一次小 GEMM + dispatch；③ **dispatch 开销未知**——**小模型上昇腾 dispatch 的 host-device 通信可能吃光 expert 节省的 FLOPs**，标 ➖（未知）。物理由：SNR 轴是天然的 compute allocation 维度。
- **精度风险 + 缓解**：top-1 路由 → **风险低**（SNR 分桶明确）。缓解：① gate 输入用 (SNR, mobility) 二维；② load balancing loss 防止 expert 偏载；③ expert 数 ≤ 4（小模型用 MoE 收益有限）。
- **适用 / 不适用**：✅ SNR / mobility 分布广的数据集；✅ 大模型（dispatch 开销占比小）；❌ **小模型（dispatch 开销未知，可能净负）**；❌ 单一 SNR 场景（无路由必要）。
- **来源**：`[plan §5 B]`；MEAN TECS26。

---

### M30. early-exit / 动态深度（skip-via-zero-mask 重构）

【昇腾 ❌→✅】【精度: 中】【物理: yes】【锚定: 全局/M30】

- **结构改动**：每个 exit 接 confidence head，confidence 够则提前返回。**关键约束**：昇腾静态图不支持数据依赖分支（`if confidence > τ: return`）→ **必须重构为 zero-mask**：所有层都执行，但用 `x = x · mask` 让 mask=0 的层变成 identity（零计算效果），保静态图。DSD24 / LOREN 思路。
- **降时延机理**：① **FLOPs（实际不降）**：zero-mask 重构后所有层仍 launch，FLOPs 不降——**这是 ❌ 的部分**；② **Cube利用率（部分恢复）**：zero-mask 让被跳过的层的 Cube 走 idle / nop，部分恢复（**这是 ✅ 的部分**，但仍非真正跳过）；③ **kernel-launch（不降）**：静态图所有 kernel 仍 launch。**净收益取决于昇腾对 zero-tensor 的 early-terminate 能力**。
- **精度风险 + 缓解**：每个 exit 的 confidence head 必须校准好 → **风险中**（每出口都要达精度）。物理由：高 SNR 样本不需要深层，低 SNR 才需要——SNR 自适应深度。缓解：① 训练时多 exit loss；② zero-mask 的层加 skip connection；③ mask 阈值 conservative（宁多算不早出）。
- **适用 / 不适用**：✅ 昇腾支持 zero-tensor early-terminate（需 micro-bench 确认）；✅ SNR 分布广；❌ **昇腾原生 early-exit（数据依赖分支，不支持）**；❌ 所有样本难度相近（无提前退出空间）。
- **来源**：`[plan §5 B]`；DSD24 / LOREN。

---

### M31. BCR block 剪枝（昇腾降级为 INT8-only，弃 MSQ 的 LUT / INT4 半）

【昇腾 ⚠️】【精度: 低】【物理: no】【锚定: 全局/M31】

- **结构改动**：Block-wise Channel Redundancy（BCR）剪枝——按 block 检测通道冗余，整 block 剪。**关键约束**：原 SPiNN 工作（FPGA）用 MSQ（Multi-Step Quantization）配 LUT + INT4，**昇腾无 INT4 / 无 LUT** → **降级为 INT8-only**：只保留 BCR 的结构化剪枝部分，弃 MSQ 的 LUT / INT4 量化部分。此外昇腾不擅长非 N:M 稀疏（BCR 的稀疏模式非 2:4），Cube 利用率受影响。
- **降时延机理**：① **FLOPs**：剪 block → 整段 FLOPs 下降；② **Cube利用率（受限）**：BCR 稀疏非 N:M 标准，昇腾 Cube 的稀疏 GEMM 支持有限 → 实际收益打折（标 ⚠️ 的主因）；③ **TransData（不变）**。
- **精度风险 + 缓解**：剪 block + 降级 INT8 → **风险低**（结构化剪枝 + INT8 都是成熟技术）。缓解：剪后 fine-tune；INT8 用 QAT 恢复精度。
- **适用 / 不适用**：✅ 有明确 block 冗余的模型；✅ 接受 INT8-only（昇腾标配）；❌ 想复现 SPiNN 的 INT4/LUT（昇腾不支持）；❌ 稀疏模式非 N:M 的 Cube 性能未测。
- **来源**：`[plan §5 B]`；`[2205.06159]`（SPiNN）；`common/ascend_constraints.md` §8（无 INT4）。

---

## 组合套餐

> 单 move 收益有限，组合使用时按 **"先吃廉价友好赢面 → 结构 → 架构 → 量化正交叠加"** 的推荐顺序。注意 TransData 是主税，组合的首要目标是**减少 domain-crossing 数**。

### 推荐 stacking 序列

1. **Starter pack（默认开，零损失，先吃）**：
   - `M18 pilot 输入富化`（默认开，非竞争项）
   - `M1 BN-fold` + `M2 torch.compile(npu)` + `M3 静态 shape + 通道÷16`（三件套，零精度损失）
   - `M5 QKV-fold`（若保留 attn）
   - `M4 pointwise 化`（边界 3-tap → 1×1，消 TransData 触发点）+ `M9 soft-threshold`（补 M4 丢的频率选择性）
   - `M19 residual-around-LMMSE` + `M20 dilated conv`（conv 段增益）
   - `M7 npu_fusion_attention`（若保留 attn，强制）
2. **保留混合架构（精度敏感场景）**：Starter + `M6 减 block 4→3`（配 M14 KD）+ `M11 port 共享` / `M26 流共享`（MIMO）+ `M8 windowed attn`（移动场景）+ `M12 低秩 Q/K`。
3. **弃 Transformer（激进，TransData 收益最大）**：Starter + `M15 conv-only baseline`（T0 gating 通过后）+ `M14 KD → conv-only student` → 全程零 domain crossing。
4. **极致时延（部署期折叠）**：Starter + `M13 A-MMSE fold-to-linear`（Transformer 折叠成单 GEMM，零 domain crossing）+ `M16 INT8`（Cube 2× 吞吐）+ `M11/M26 共享`。
5. **量化正交叠加**：上述任一组合 + `M16 INT8 PTQ`（最后做，与架构正交）。

### 冲突表（不可同叠）

| 冲突对 | 原因 |
|---|---|
| **DW-separable conv 与一切** | DW 饿死 Cube（见反模式），与 M4 pointwise、M17 channel 剪枝（非 DW）都矛盾。本族**弃用 DW**，改 pointwise / 标准 conv 局部混合。 |
| **linear-attn / Performer 与 N=64** | attention 才占 17%，N² < 0.4%，常数项吃光收益（§1 结论 1）。M12 低秩 Q/K 在 N=64 也收益有限。 |
| **M10 FFT-mix 与 M21 axial attn** | 都在抢时间轴 attn 的位置，二选一；且 M10 的昇腾 FFT 融合待验，优先 M21。 |
| **M27 Mamba scan 与昇腾原生 pipeline** | scan kernel 不移植，单独成路线；与 M7 fusion attn 互斥（一个 attn 一个 SSM）。 |
| **M30 early-exit 与 M3 静态 shape** | 原生 early-exit 的数据依赖分支破坏静态图，**必须 zero-mask 重构**才能共存（且 FLOPs 不降）。 |
| **M8 windowed 与 M13 fold-to-linear** | M13 折叠后已无 attn，M8 无对象——互斥。 |
| **M23 Toeplitz FFT 形 与 昇腾** | FFT 碎片 kernel，**只能用 dense 重构形**。 |
| **M22 CVNN 未 lowering 与 昇腾** | 无编译期 block-real lowering 就会触发 TransData，必须配 lowering。 |
| **M14 KD 与 M6 减层** | 可同叠（KD 补减层损失），但若 M14 已蒸到 conv-only student，M6 对 student 不再适用（conv-only 无 attn block 可减）。 |

---

## 反模式

> 以下组合 / move 已知在昇腾 + 无线接收机场景会塌，**严禁出现在 Hypothesizer 假设中**（详细论证见 `failures.md`）。

1. **未测 conv-only baseline 就盲改混合架构**（最大风险，T0 gating）。先 M15 再谈。
2. **DW-separable conv**（Cube 饿死，V5 实测更慢）——否掉通用文献推荐的 CMT depthwise-LPU，改 pointwise / 标准 conv。
3. **手搓 attention**（bmm + softmax + scale 串联）——丢融合 + 触发额外 TransData。必须用 M7 `npu_fusion_attention`。
4. **N=64 下上 linear / Performer / FlashAttention / Nyströmformer / Linformer**——attention 才 17%，N² < 0.4%，常数项吃光收益。
5. **BNN / 二值化**（无线 OFDM 无实测，低 SNR 1-3dB 损）；**INT4**（昇腾仅 FP16+INT8，FPGA 的 MSQ INT4 说法降级）。
6. **LayerNorm 不 fold**（LN 是 Vector 归约不可 fold）——能换 BN 就换（M1）。
7. **动态 shape**（Host dispatch 重编译）——昇腾几乎强制静态（M3）。
8. **Toeplitz / FFT-fast 形**（碎片 FFT kernel）——只用 dense 重构形（M23）。
9. **Mamba scan 不移植**——CUDA `mamba_ssm` 在昇腾无原生对应（M27）。
10. **early-exit 数据依赖分支**——用 zero-mask 重构保静态图（M30）。
11. **GRU 频率递归**（扫描链，AiCore 利用率崩）。
12. **CKDUNet 查无此文，勿引；Channelformer 是 SISO 下行，勿当 MIMO 基准。**

---

## move 决策树

> 按 **"瓶颈是 TransData / FLOPs / kernel-launch / Cube？"** 分支。瓶颈定位必须用昇腾 msprof 实测（TransData 占比、Cube 利用率、Host/Device-bound），不要靠 FLOPs 估算。

```
当前瓶颈是什么？（msprof 实测）
├─ TransData 占比高（domain crossing 多，conv↔attn 边界频繁）
│   ├─ 边界处的 conv 是 3-tap？        → M4 pointwise 化（1×1 = GEMM，消触发点）
│   ├─ 仍保留 attn？                   → M7 npu_fusion_attention（融合算子内不落 NZ 中间 tensor）
│   ├─ attn 能彻底去掉？               → M13 fold-to-linear（零 domain crossing）或 M14 KD → conv-only
│   ├─ MIMO 多 port / 多流实例重复？   → M11 port 共享 / M26 流共享（减少实例数）
│   └─ 仍有 reshape/permute 串联？     → M2 torch.compile + AutoFuse
├─ FLOPs 太高（profiling 显示 Conv 主导）
│   ├   block 数多？                   → M6 减 block 4→3（配 M14 KD）
│   ├   感受野靠堆 3×3？               → M20 dilated conv（保 RF 减层）
│   ├   通道冗余？                     → M17 结构化剪枝（非 DW）
│   └   还差一截？                     → M16 INT8 PTQ（Cube 2× 吞吐）
├─ kernel-launch 太多（小模型、低 batch、碎片算子）
│   ├   reshape/copy/GELU/LN 串联？    → M2 torch.compile + AutoFuse
│   ├   BN 节点多？                    → M1 BN-fold（消 norm 算子）
│   ├   Q/K/V 三投影？                 → M5 QKV-fold（3→1 GEMM）
│   ├   多并联小分支？                 → M5 + M2 合并
│   └   INR 逐点查询？                → M25 改批量查询
├─ Cube 利用率低（DW / group / 稀疏饿死 Cube）
│   ├   用了 DW-separable？            → 弃用，改 pointwise / 标准 conv（反模式 #2）
│   ├   通道不 ÷16？                   → M3 通道÷16 对齐
│   ├   GNN 稀疏消息传递？             → M28 改 SGT banded dense 形
│   └   BCR 非 N:M 稀疏？              → M31 降级 INT8-only，或改标准剪枝
└─ 精度不够（不是降时延 move，是给降时延腾余量）
    ├   输入只有 Y？                    → M18 pilot-grid 富化（默认开）
    ├   信道估计误差大？                → M19 residual-around-LMMSE
    ├   减层 / 剪枝后掉点？             → M14 KD（输出级 MSE，teacher no_grad）
    └   强时变信道？                   → M24 hypernetwork per-slot 生成权重
```

---

## 与 plan 文档的直接映射

| plan 章节 | 对 latency_moves 的贡献 |
|---|---|
| **plan §1 瓶颈诊断** | §1 节：attention 17% / N=64 太短（结论 1）+ TransData 是主税（结论 2）+ T0 gating conv-only baseline。本族六条机理路径（FLOPs / MAC / launch / TransData / Cube / domain-crossing）由此确立。 |
| **plan §5 T1（M1-M5）** | 融合层五件套：BN-fold / compile / 静态 shape / pointwise / QKV-fold。**全部零精度损失，先吃。** |
| **plan §5 T2（M6-M12）** | 结构层七条：减层 / fusion attn / windowed / soft-threshold / FFT-mix / port 共享 / 低秩 QK。保留混合架构。 |
| **plan §5 T3（M13-M15）** | 架构层三条：fold-to-linear / KD conv student / conv-only baseline。重新审视 attn 的存在性。 |
| **plan §5 T4（M16-M17）** | 量化剪枝两条：INT8 AMCT / 结构化剪枝。与 T1-T3 正交叠加。 |
| **plan §5 B 类（M18-M31）** | 研究扫描 14 条补充 move，含 CVNN / Toeplitz / hypernet / INR / EqDeepRx / Mamba / SGT / MoE / early-exit / BCR。 |
| **plan §7 failures.md** | 反模式节直接来源（DW / 手搓 attn / INT4 / LN 不 fold / 动态 shape / Mamba scan / FFT 形 Toeplitz / early-exit 分支 / GRU 递归）。 |
| **plan §8 ascend_constraints.md** | 六条机理路径的硬件根基（TransData / Cube tile 16³ / 1×1=GEMM / BN-fold pass / fusion attn / INT8 Cube 2×）。 |
| **plan §10 待验证** | 所有昇腾判定 = 算子形状推断，无公开实测；M22/M23/M27/M28/M29/M30/M31 均需 Atlas A2 + CANN micro-bench。 |

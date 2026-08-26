# D20 · student_large_kernel（大核 1D conv student，k≤15 + dilation，标准 conv 禁 DW）

> 一句话定位：用**大核 1D 标准 conv**（k∈{7,9,11,13,15}）替代 attention 的全局性——比 D1 的 dilation≤6 更显式捕获长程相关性，但**禁 depthwise**（昇腾 DW 饿死 Cube），用标准 conv + im2col。

## 结构
- **Block 结构**（大核 conv 残差块）：
  ```
  x → Conv1d(C, C, kernel=k, padding=k//2, groups=1) → BN → GELU
    → Conv1d(C, C, kernel=1) → BN → + residual
  ```
  - **关键**：`groups=1`（**标准 conv，非 DW**）；kernel k∈{7,9,11,13,15}。
  - **可选 dilation**：`dilation ∈ {1,2}` 与 kernel 组合，扩大感受野；**禁 dilation > 4**（D1 的最大值，超过的物理对应不强）。
  - im2col 后变成 `[B·F_out, C·k, S_out]` 的 matmul，Cube workload。
- **主干**：stem Conv1d（3-tap, in_ch→C） → N×[LargeKernelBlock] → head Conv1d（1×1, C→out_ch）。
- **参数量**：单 block 大核 conv 参数 `C²·k`，C=64 / k=11 → 45K params/conv，4 block × 2 conv = 360K total，介于 D18 / D19 之间。

## 为什么降时延
1. **替代 attention 的全局性** —— k=15 的 1D conv 感受野 RF=15 个子载波，覆盖典型多径时延扩展；比 D1 dilation≤6 更直接。
2. **标准 conv（非 DW）= Cube workload** —— im2col + GEMM 是昇腾最成熟通路；比 D1 的 DW-separable 更稳（D1 failures 要求 DW 替换为标准 conv，本 direction 默认就是标准 conv）。
3. **无 attention / 无 TransData** —— 同 D1 / D18 / D19。
4. **物理可解释** —— 大核 conv 在频域轴等价于"宽带 FIR 滤波器"，直接对应多径信道的时延功率谱（PDP）；比 pointwise D18 更显式物理。

## 昇腾友好性
**✅ friendly（kernel-dependent）** ——
- **k≤7**：✅✅ Cube 利用率满。
- **k∈{9,11,13}**：✅ im2col 后矩阵变高，Cube 仍友好，但**需要 micro-bench**（im2col 的内存占用随 k 线性增长）。
- **k=15**：⚠️ im2col 矩阵大，可能 L2 cache 命中率下降；若 latency 不达预期，回退到 k≤11 + dilation=2。
- **k>15**：❌ 不推荐；改用 D1 的 dilation schedule（rate 1→2→4→6→4→2→1）替代。

## 物理依据
**yes（显式 PDP 对应）** —— 大核 conv 在频域轴（子载波维）的权重对应"频域 FIR 滤波器"的冲激响应，傅里叶逆变换到时域就是多径信道的功率延迟谱（PDP）。k=15 对应最大多径时延 14 个子载波间距，覆盖典型城市多径（<5μs）。**比 D1 的 dilation 更显式**（dilation 是稀疏采样，大核是密集采样）。

## bundle 的 move
**M-large-kernel**（本 student 结构）+ **M14**（KD 到本 student）+ **M3/M2**（ConvBN 融合）+ **M16**（INT8 PTQ，大核 conv 对 INT8 鲁棒）+ **M18**（3-grid 输入富化）。

## 结构前提与坑
1. **禁 DW** —— RepLKNet 等论文推荐大核 + DW-separable，昇腾上 DW 饿死 Cube（D1 failures 明确）；**本 direction 强制标准 conv**（`groups=1`）。代价是参数量 `C²·k` 比 DW 大，但 Cube 利用率补偿回来通常更快。
2. **im2col 内存随 k 线性** —— `im2col` 把每个滑窗展开成列，内存占用 `batch · C · k · F_out`；k=15 vs k=3 内存差 5×，需要测峰值显存。若 OOM，减 batch 或减 C。
3. **micro-bench 必做** —— k∈{7,9,11,13,15} 在昇腾上的 latency 不一定单调上升（cache 效应、im2col 实现）；kd-nas workflow 必须在 Phase1 扫 k 时**用真实 latency_provider 测**（CONTRACTS §4 `latency_onnxrt.py::measure`），不要用 FLOPs 估算。
4. **dilation 组合** —— `kernel=7, dilation=2` 等效 RF=13，参数量同 k=7；比纯 k=13 省参数但 cache 表现不同（稀疏采样 vs 密集采样）；做 ablation。
5. **结构重参数化（RepLKNet 风格）** —— 训练期大核可拆成"小核 + identity"并联，部署期融合为大核（结构重参数）；可选优化，增加 engineer 复杂度，**Phase1 不推荐**。
6. **feature_hook_names** —— 每个 block 输出注册 hook，对齐 OFD。
7. **fail-loud**：若 latency_micro_bench 显示 k=11 反而比 k=7 快（cache sweet spot），是正常的——记录数据，不要强行解释。
8. **与 D1 的关系** —— D1 默认 dilation≤6 的 DW-separable；本 direction 是 dilation≤4 + 大核 + 标准 conv。**两者可混合**（D1 stem + D20 block），但本 workflow 默认分开对比。

## 来源
- RepLKNet：Ding et al., CVPR 2022 —— [arXiv:2203.06717](https://arxiv.org/abs/2203.06717) "Scaling Up Your Kernels to 7x7 or Beyond"（大核 conv 的 GPU 优化，昇腾需做 adaptation）。
- InternImage / 大核 conv 综述 —— 大核在视觉领域的复兴（2022-2024），可借鉴结构，昇腾 adapter 需 micro-bench。

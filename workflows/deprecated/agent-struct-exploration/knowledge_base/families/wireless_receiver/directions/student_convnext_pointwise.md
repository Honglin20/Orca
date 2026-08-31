# D18 · student_convnext_pointwise（ConvNeXt inverted-bottleneck pointwise 改造版 student）

> 一句话定位：把 ConvNeXt 的 inverted-bottleneck block **改造成全 pointwise** 版本作 student——expand/contract 全 1×1 conv 是 Cube 的最佳 workload；禁 7×7 depthwise、LN 换 BN。

## 结构
- **Block 结构**（ConvNeXt-V2 inverted bottleneck 改造）：
  ```
  x → 1×1 expand (C→4C) → GELU → 1×1 contract (4C→C) → BN → + residual
  ```
  - **移除** 原 ConvNeXt 的 **7×7 depthwise conv**（昇腾 DW 饿死 Cube，见 D1 failures）。
  - **替换** LayerNorm → BatchNorm（昇腾 ConvBN fusion pass 覆盖率高，见 M3）。
  - **保留** GELU 激活（或换 ReLU 进一步省算子，昇腾 GELU 走 Vector 通路，开销可测）。
- **主干**：stem Conv1d（3-tap, in_ch→C） → N×[ConvNeXtPointwiseBlock] → head Conv1d（1×1, C→out_ch）。
- **与 baseline 的对应**：
  - N=4 block 替代 baseline 的 4 个 SignalTransformerBlock（`self.main = nn.Sequential(...)`）。
  - 输入输出 shape 严格对齐 CONTRACTS §1 `[B,4,48,64,1] ↔ [B,4,48,64,1]`。
  - 内部 alpha 归一复用 `students/_common.py` 的 `AlphaNorm / signal_head`。

## 为什么降时延
1. **全 pointwise** → Cube 利用率最大化（1×1 conv 是 GEMM，C0=16 tile 满载）。
2. **无 attention、无 TransData** → 同 D1 的 GEMM-land 收益。
3. **inverted bottleneck 的 expand/contract** 模式让 FLOPs 集中在 1×1（昇腾最佳 workload），4× channel expand 在 Cube 上比 3×3 conv 快 2-3×（经验，需 micro-bench）。
4. **参数效率**：4× expand + GELU + contract 的表达力接近 3-layer FFN，参数量 < 同精度 conv-only baseline。

## 昇腾友好性
**✅✅ friendly** —— 全 pointwise + BN + 残差，每一个算子都在 `ConvBatchnormFusionPass` + `GEMM-kernel` 的甜蜜区。**GELU 走 Vector 通路**，若 latency micro-bench 发现 GELU 占比 > 10%，可换 ReLU（精度损失通常 < 0.1dB）。

## 物理依据
**间接** —— pointwise 在频域轴上等价于"逐子载波的通道混合"，对应 OFDM 频域均衡的 per-subchannel combining；缺少跨子载波的局部相关性捕获（无 conv kernel），需配合 **D1 的 stem Conv1d 3-tap** 或 **3-grid 输入富化（M18）** 补局部先验。

## bundle 的 move
**M-ConvNeXt-pointwise**（本 student 结构）+ **M14**（KD 到本 student）+ **M3/M2**（ConvBN 融合 + GeLU→ReLU 可选）+ **M16**（INT8 PTQ，pointwise 对 INT8 量化最友好）。

## 结构前提与坑
1. **禁 7×7 DW** —— ConvNeXt 原作的 7×7 depthwise conv 是为 GPU 优化的（NVIDIA cudnn 深度优化的 DW kernel）；昇腾 Cube 对 DW 不友好（D1 failures 明确）。**改全 pointwise**，局部性由 stem 的 3-tap conv + dilation 补（可选）。
2. **LN 换 BN** —— ConvNeXt 用 LN 是因为 Transformer 风格；昇腾 LN 融合 pass 不成熟，BN 融合 pass 完备（M3）。**推理期 BN 折叠为 affine**，零开销。
3. **expand ratio 是个轴** —— 4× 默认；候选 `{2, 4, 8}`。8× 表达力强但 FLOPs 翻倍；2× 省算力但可能掉精度。
4. **GELU vs ReLU** —— GELU 精度略好但昇腾 Vector 通路；ReLU Cube 友好但可能掉 0.1-0.3 dB。**默认 GELU**，latency 紧张时换 ReLU 做 ablation。
5. **feature_hook_names** —— CONTRACTS §1 要求 student 暴露 hook 列表；本 student 在每个 block 输出注册 hook，`["backbone.block0", "backbone.block1", ..., "backbone.blockN-1"]`，对齐 OFD（D13）多 stage KD。
6. **DW conv 替代方案** —— 若需要局部性又不破坏 Cube，用 **3×3 标准 conv**（非 DW-separable）作为 stem + 偶尔穿插（如每 2 block 加一个 3×3）；避免 DW。
7. **1D vs 2D** —— model8 输入是 `[B, P=4, F=48, S=64, 1]`；ConvNeXt 原作是 2D image。本 student 用 **Conv1d / Linear**（把 F·S 拉平或沿某一轴），通常沿 F 轴做 1D conv（子载波维）；S 轴用 pointwise Linear 混合。具体 reshape 由 `students/convnext_pointwise.py` 决定，engineer 不改。
8. **fail-loud**：若 measure_student 报 latency > teacher（异常），多半是 expand ratio 过大或忘了 ConvBN fusion；检查 ONNX 导出后 `ConvBatchnormFusionPass` 是否命中。

## 来源
- ConvNeXt：Liu et al., CVPR 2022 —— [arXiv:2201.03545](https://arxiv.org/abs/2201.03545) "A ConvNet for the 2020s".
- ConvNeXt-V2：Wang et al., CVPR 2023 —— FCMAE + 稀疏 conv 改进（本卡仅参考结构，不抄 V2 的 MAE）。

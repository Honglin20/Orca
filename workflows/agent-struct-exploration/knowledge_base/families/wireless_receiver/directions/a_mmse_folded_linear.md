# D3 · a_mmse_folded_linear（部署期折叠成线性滤波器）

> 一句话定位：**训练期 Transformer 在**，部署时**整个网络折叠成单一线性操作**（无非线性激活），rank 可调 —— 把 D0 的 conv+attn 训练目标蒸馏成一个矩阵乘。

## 结构
- **输入张量**：训练期同 D0；部署期仅 `Y ∈ R^{B×C_in×N_freq×N_sym}`。
- **主干**：
  - **训练期**：完整 Transformer 接收机（D0 风格）+ A-MMSE 头，损失端强制让注意力/卷积输出可线性投影到 Wiener 滤波形式。
  - **部署期**：整个网络折叠为单一线性算子 `W_eff ∈ R^{r×C_in}`，rank `r` 可调（rank-adaptive 权衡精度/时延）。
- **输出**：均衡后符号 / CSI。
- **attention?**：**shallow**（训练 yes / 部署 no —— 折叠后无任何 attention 算子）。

## 为什么降时延
1. **部署期完全无 attention、无非线性激活** → 单个 GEMM，无 TransData，无 Cube 格式切换。
2. rank `r` 可在精度-时延曲线上任意取点 —— `r` 越小越快，精度单调可预测。
3. 训练成本不限制部署形态 —— 用最强训练模型换最便宜部署。

## 昇腾友好性
**✅✅ friendly** —— 单个 dense GEMM 是昇腾 Cube 的最佳 workload，利用率近峰值；rank-adaptive 直接对应 Cube tile 维度。

## 物理依据
**yes（LMMSE 线性）** —— 训练目标是让网络学一个**比闭式 LMMSE 更优的线性滤波器**（A-MMSE = approximated MMSE），部署折叠即线性 MMSE 的可学习版。

## bundle 的 move
**M13**（部署期 Transformer 折叠成线性滤波器，rank-adaptive）+ **M16**（INT8 量化叠加，单 GEMM 上再 2× Cube）。

## 结构前提与坑
1. **折叠前提**：网络输出必须**线性可分解**于输入 —— 任何 softmax/GELU 中间激活都破坏折叠性。训练期要加显式的"线性约束"损失或让 attention 输出被一线性投影蒸馏。
2. **rank 选择**：rank-adaptive 是 deployment-time tuning，不是 once-for-all；换场景（SNR、信道模型）需重选 rank。
3. 折叠后的线性算子是 **dense 矩阵**，不是 Toeplitz —— 不要尝试转 FFT-fast 形（FFT kernel 碎片化，`failures.md` 禁）。
4. 训练-部署 gap 风险：训练精度高 ≠ 折叠后精度高，必须在线性约束下 fine-tune。
5. **新文未复现**（2025 [arXiv:2506.00452]），复现面薄。

## 来源
[arXiv:2506.00452] A-MMSE（2025）—— plan §10 标注新文未复现。

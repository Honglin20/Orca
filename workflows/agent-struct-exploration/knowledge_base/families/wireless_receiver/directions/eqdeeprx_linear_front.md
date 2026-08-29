# D2 · eqdeeprx_linear_front（线性前置 + 每流共享 DetectorNN）

> 一句话定位：**LMMSE+RZF 并行前置** + **每 MIMO 流共享 DetectorNN 权重**（近线性 scaling）+ DenoiseNN(1D-along-freq) + DemapperNN，**无 attention** —— DeepRx 团队 2026 工业级落地版。

## 结构
- **输入张量**：MIMO 接收 `Y ∈ R^{B×N_rx×N_freq×N_sym}` + 导频 `Xp`。
- **主干**（四段）：
  1. **并行线性前置**：`LMMSE` 与 `RZF(=αI, 近 ZF)` 两路均衡器**并排**，输出拼接；**RZF 是稳定训练锚**（α 防止 LMMSE 在低 SNR 奇异）。
  2. **DetectorNN**：轻量 CNN，**4 个 antenna port / MIMO 流共享同一套权重**（参数量近线性 scaling）。
  3. **DenoiseNN**：1D-along-frequency 轻量 CNN，去频域残余干扰。
  4. **DemapperNN**：流到 bit 的映射，输出 coded-bit LLR。
- **attention?**：**no**。

## 为什么降时延
1. 线性前置吃掉大部分信道效应 → 后端 DetectorNN 可以**非常浅**。
2. **每流共享 DetectorNN 权重** → 4 流推理 ≈ 单流推理 + 一个 GEMM 广播，时延近似不随流数增长（D0 是 4×）。
3. 无 attention → 全 GEMM-land，无 TransData。

## 昇腾友好性
**✅✅ friendly** —— 纯 conv + 线性矩阵乘，无 attention，BN-fold / Conv+ReLU 融合全覆盖。LMMSE/RZF 矩阵可静态预计算或一次 GEMM 完成。

## 物理依据
**yes** —— LMMSE/RZF 是经典线性均衡器（Wiener 滤波 / 近 ZF），DetectorNN 只学非线性残差；RZF 的 α 正则化对应 ill-conditioned 信道矩阵的 Tikhonov 正则。

## bundle 的 move
**M26**（双均衡器 + 每流共享 detector，D2 核心）+ **M11**（共享 DetectorNN 权重）+ **M1/M2/M3**（融合层）。

## 结构前提与坑
1. **RZF 是稳定训练锚，不可省** —— 单 LMMSE 在低 SNR / ill-conditioned 信道下数值爆炸，RZF(αI) 提供梯度稳定通路。
2. **"每流共享" 要求各 MIMO 流物理信道统计相近** —— 极端异构场景（大规模天线阵列中用户信道差异极大）共享会掉点，需验证。
3. DenoiseNN 的 **1D-along-frequency** 假设频率轴相关性 > 时间轴；多普勒大的高铁场景需补 1D-along-time 或换 2D。
4. **新文未复现**（2026 [arXiv:2602.11834]），复现面薄，依赖此方向的 move 要标"新文未复现"。

## 来源
[arXiv:2602.11834] EqDeepRx（2026）—— plan §10 标注新文未复现。

# D10 · residual_around_lmmse（LMMSE 前置 + 学 Δh 残差）

> 一句话定位：**前置 LMMSE 闭式解** + 神经网络**只学 Δh = h − ĥ_LMMSE**（线性估计残差）—— 把网络容量从"学全部信道"压到"学残差"。

## 结构
- **输入张量**：接收 `Y` + pilot `Xp` → **LMMSE 闭式估计** `ĥ_LMMSE`。
- **主干**：轻量 CNN，输入 `(Y, Xp, ĥ_LMMSE)`，**输出 Δh**，最终估计 `ĥ = ĥ_LMMSE + Δh`。
- **输出**：CSI（信道估计）。
- **attention?**：**no**（CNN-only）。

## 为什么降时延
1. **网络只学残差** → 容量需求骤降，主干可极浅（~1/4 D0 参数）。
2. LMMSE 是闭式线性 → 静态 GEMM 一次完成，无迭代。
3. 无 attention → 无 TransData。

## 昇腾友好性
**✅ friendly** —— LMMSE 矩阵可静态预计算；CNN 残差主干纯 conv；全程 GEMM-land。

## 物理依据
**yes** —— LMMSE 是经典 Wiener 滤波器（闭式 MMSE 线性估计），神经网络补非线性残差（多径间高阶耦合、模型失配）；Δh 学的是闭式解未能捕捉的部分。

## bundle 的 move
**M19**（residual-around-LMMSE，学 Δh）+ **M1/M2/M3**（融合层）+ **M11**（多 port 共享残差网络权重）。

## 结构前提与坑
1. **LMMSE 闭式解依赖信道统计知识** —— 需要噪声方差 + 信道协方差矩阵；统计失配时 LMMSE 起点差，残差网络要补更多。
2. **Δh 假设小残差** —— 极端场景（deep fade、模型严重失配）Δh 大，网络容量不够会失败；可加 skip-via-zero mask 检测大残差样本。
3. 残差结构与 D2（线性前置）互补 —— D2 是 LMMSE+RZF 双均衡器并行，D10 是 LMMSE + 残差串联；可组合（M19 + M26）。
4. **作为信道估计器，不是完整接收机** —— 下游仍需 demapper，与 D5 一样是子任务模块。

## 来源
[arXiv:2009.01423]（2020，residual around LMMSE 信道估计）。

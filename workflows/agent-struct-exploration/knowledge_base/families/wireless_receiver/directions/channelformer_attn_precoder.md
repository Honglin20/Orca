# D5 · channelformer_attn_precoder（浅 attn 作输入 precoding + CNN 主干）

> 一句话定位：**单个浅 attention 作输入 precoding**（encoder 仅 1 个 attn block ~21k 参数）+ **CNN decoder 主干**（占 ~82% 参数）—— **SISO 下行信道估计**（非 Massive MIMO 上行），输出 CSI。

## 结构
- **输入张量**：SISO 下行接收 pilot grid `Y_pilot ∈ R^{B×N_freq×N_sym}`（单天线、单流）。
- **主干**（两段）：
  1. **Encoder（precoder）**：**单个** attention block（~21k 参数），对 pilot 做全局上下文聚合，输出 precoded 特征。
  2. **Decoder（主干）**：CNN（占模型 **~82% 参数**），做时频网格的局部平滑 + 外推到 data grid。
- **输出**：**CSI（信道估计）** —— 不是 LLR、不是硬比特。
- **attention?**：**shallow**（仅 1 个 attn block，作 precoder，非主干）。

## 为什么降时延
1. attention 只在 **precoder** 出现一次（~21k 参数）—— Conv↔attention TransData 边界降到 2 次/前向（D0 是 8 次）。
2. **主干（82% 计算）是纯 CNN** —— 走昇腾 Cube 满载通路。
3. 参数分布倒置：D0 是 attn 主导，D5 是 conv 主导。

## 昇腾友好性
**✅ friendly** —— 主干纯 conv，浅 attn 仅一次，TransData 税可忽略；attn 可手搓也可调融合算子（单层规模小）。

## 物理依据
**no**（无显式 OFDM 信道先验）—— precoder 是通用 attn，decoder 是通用 conv，不内嵌多径/多普勒结构。

## bundle 的 move
**M12**（低秩 Q/K 投影，attn down-score，浅 attn 场景收益叠加）+ **M1/M2/M3**（融合层）+ **M17**（结构化剪枝 CNN decoder 通道）。

## 结构前提与坑
1. **是 SISO 下行信道估计，不是 Massive MIMO 上行** —— 不要当 MIMO 接收机基准用！原文场景是 BS→UE 单流下行 CE。
2. **输出是 CSI 不是 LLR** —— 下游还需均衡器 + demapper 才能到 bit；不能直接当端到端接收机。
3. **浅 attn precoder 的收益依赖 pilot 稀疏性** —— SISO 下行 pilot grid 密度 vs MIMO 上行不同，迁移时 attn block 尺寸需重标。
4. **`failures.md` 警告**：勿当 MIMO 基准、勿当 LLR 输出器。本方向仅作"浅 attn + CNN 主干"的结构模板。
5. 作为我们 MIMO OFDM 接收机任务的**结构启发**（如何把 attn 压到 precoder），不是直接基准。

## 来源
[arXiv:2302.04368] Channelformer（2023）—— SISO 下行 CE，plan §4/§10 纠错明确。

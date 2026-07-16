# D7 · windowed_axial_attn（轴向可分 + 局部窗 attention）

> 一句话定位：**轴向可分**（time-then-freq 两次 attn）+ **局部窗 W=16**，2.81× FLOP 下降 —— 保留 attention 表达力但降到昇腾融合算子的友好尺寸。

## 结构
- **输入张量**：同 D0。
- **主干**：Conv stem → N×[`windowed axial attn block`（先沿 time 轴 W=16 局部 attn，再沿 freq 轴 W=16 局部 attn）+ `Conv-FFN`]。
- **输出**：均衡后符号 / CSI。
- **attention?**：**yes（小）** —— 有 attention 但窗口小、序列短（W=16 满足 `seq≥16` 昇腾融合算子门槛）。

## 为什么降时延
1. 轴向可分把 `O((N_t·N_f)²)` 降到 `O(N_t²·N_f + N_t·N_f²)`，再加 W=16 局部窗 → 实测 **2.81× FLOP 下降**。
2. **W=16 满足昇腾 `seq≥16` 门槛** → 可直接调 `npu_fusion_attention`，不手搓。
3. 残差传递跨窗传递全局信息（窗×窗堆叠 ≈ 全局感受野）。

## 昇腾友好性
**✅ friendly** —— windowed attn 满足融合算子尺寸门槛；轴向分解后每次 attn 是小 GEMM，Cube 利用率高；TransData 边界数与 D0 同（4 block × 2）但每次 attn 算子开销小。

## 物理依据
**yes（T-F 可分）** —— OFDM 时频网格的信道相关性在 time/freq 两轴上**近似可分**（多径 → 频域相关；多普勒 → 时域相关），轴向分解物理对齐。

## bundle 的 move
**M21**（轴向/可分 attn，time-then-freq）+ **M8**（windowed/Swin 局部 attn，W=16）+ **M7**（调 `npu_fusion_attention`）+ **M1/M2/M3**（融合层）。

## 结构前提与坑
1. **W=16 与昇腾融合算子门槛对齐** —— 改 W（高铁/mmWave 大多普勒需自适应 W）必须保持 `W≥16`，否则融合算子失效退化为手搓 attn。
2. **轴向可分是近似** —— 极端 2D 联合相关性（双选信道特定模式）下可分假设掉点；`failures.md` 标"单论文"风险。
3. **head_dim÷16** 仍需对齐（M7 前提）。
4. 局部窗 + 残差堆叠 ≈ 全局感受野，但**窗口边界**会丢少量跨窗依赖；可加 1-2 个 attend-all global token（Longformer 模式）缓解。

## 来源
[arXiv:2510.12941]（2025，windowed axial attn，单论文）—— plan §10 标注复现面薄。

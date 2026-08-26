# D1 · deeprx_conv_only（纯卷积 DeepRx 风格接收机）

> 一句话定位：**dilated depthwise-separable conv ResNet**，3-grid 富化输入 `(Y, Y⊙Xp*, Xp)`，输出 **coded-bit LLR**，**无 attention** —— 该领域实测部署 SOTA，昇腾 GEMM-land 友好。

## 结构
- **输入张量**：3-grid 堆叠 `X ∈ R^{B×9×N_freq×N_sym}`，三组通道 = 接收 `Y` + 互相关 `Y⊙Xp*` + 导频 `Xp`，**real/imag split**（非原生复数，real-pair 通道）。
- **主干**：stem Conv → N×[dilated **DW-separable** Conv ResNet block]，**hourglass dilation schedule**（rates 1→2→4→6→4→2→1，**最大 dilation 6**）。全程无 softmax attention、无 Transformer。
- **输出**：**coded-bit LLR**（不是 CSI、不是硬比特）。
- **attention?**：**no**。
- **参数量**：~1.2M。

## 为什么降时延
1. 无 attention → **消除所有 Conv↔attention TransData 边界**（D0 的主要税源）。
2. 纯 conv → 全程在 Im2Col+Cube GEMM-land，昇腾融合 pass（`ConvBatchnormFusionPass`）覆盖率高。

## 昇腾友好性
**✅✅ friendly**（方向级判定：纯 conv、无 TransData）。
**但**：DW-separable 的 **depthwise 分支会饿死 Cube**（C0=16 tile 利用率崩，V5 实测更慢）—— 落地时 **DW 部分要换 pointwise 或标准 3×3 conv**（见 `failures.md`）。

## 物理依据
**yes（局部）** —— hourglass dilation schedule 在频域近似多径时延扩展的局部相关性，dilation 6 对应最大多径时延。

## bundle 的 move
**M15**（conv-only baseline，T0 gating 必跑）+ **M18**（3-grid 输入富化，默认开）+ **M20**（dilated/multidilated conv 堆）+ **M1/M2/M3**（融合层先吃）。

## 结构前提与坑
1. **DW-separable 与昇腾 Cube 不兼容** —— `failures.md` 明令禁通用文献推荐的 depthwise-LPU；落地务必把 DW 分支替换为 pointwise/标准 conv 混合，否则 Cube 利用率崩。
2. **real/imag split 非 CVNN** —— 不要尝试原生复数（昇腾无原生复数，须 lowering 为 block-real GEMM，见 M22）。
3. **输出是 LLR 不是 CSI** —— 下游 demapper/decoder 接口要对接 LLR，不要把 D1 当信道估计器用。
4. hourglass dilation 的最大 rate 6 是论文实测值，与具体 OFDM CP 长度/多径功率谱耦合，迁移场景需重标。

## 来源
[arXiv:2005.01494] DeepRx（2020）。

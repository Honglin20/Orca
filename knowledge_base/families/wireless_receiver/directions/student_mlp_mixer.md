# D19 · student_mlp_mixer（MLP-Mixer token-mix student）

> 一句话定位：**全 pointwise 零 Transpose 的 MLP-Mixer 改造版**——token-mixing MLP 作为 attention 的"线性表亲"，在昇腾上跑成纯 GEMM，是比 D1 conv-only 更彻底的 GEMM-land student。

## 结构
- **Block 结构**（Mixer block，改造为 1D 频域版）：
  ```
  x → LN → token-mix MLP（沿子载波 F 轴，FC: F→F→F）→ + residual
    → LN → channel-mix MLP（沿通道 C 轴，FC: C→4C→C）→ + residual
  ```
  - 原 MLP-Mixer 在 token 轴 + channel 轴各做一次 FC，需要两次 transpose（`[B, F, C] ↔ [B, C, F]`）。
  - **改造**：固定一个轴（F）做 token-mix，另一个轴（C）做 channel-mix；若 F·S 拉平成单一 token 维度，transpose 数可压到 1 次/ block。
- **主干**：stem Linear（in_ch→C） → N×[MixerBlock] → head Linear（C→out_ch）。
- **与 baseline 的对应**：
  - N=4 block 替代 baseline 的 4 个 SignalTransformerBlock。
  - 输入输出 shape 严格对齐 CONTRACTS §1。
  - 内部 alpha 归一复用 `students/_common.py`。

## 为什么降时延
1. **零 attention、零 softmax、零 QK^T bmm** —— Mixer 的 token-mix 是 **静态参数 MLP**（权重不依赖输入），完全 GEMM-friendly。
2. **零 Transpose 或单次 Transpose** —— 改造后 transpose 数可压到 ≤1/block；相比 D7 windowed attn 的频繁 layout 切换省很多。
3. **静态权重的额外好处**：权重是常量张量，昇腾编译器能更好做 layout planning；INT8 量化精度高（FC 层 INT8 是 Cube 的原生 workload）。
4. **参数效率**：token-mix MLP 参数量 `F²` = 48² ≈ 2.3K params/layer，比 self-attention 的 `3·C²` QKV projection 小。

## 昇腾友好性
**✅✅ friendly** —— 全 FC + 1 transpose/block，ConvBN fusion 不适用但 **MatMul fusion + Vector Eltwise fusion** 覆盖完整；INT8 PTQ 精度损失通常 < 0.2 dB（FC 层对量化鲁棒）。

## 物理依据
**间接（频域全局性）** —— token-mix 沿子载波 F 轴做静态 MLP 等价于"频域全局线性变换"，可以近似多径信道的频域相关性结构（功率延迟谱在频域的傅立叶对偶）。比 D1 的局部 dilated conv 更"全局"，但缺少非线性局部性。

## bundle 的 move
**M-Mixer**（本 student 结构）+ **M14**（KD 到本 student）+ **M3/M2**（FC + bias 融合）+ **M16**（INT8 PTQ，最友好）+ **M18**（3-grid 输入富化，补局部先验缺失）。

## 结构前提与坑
1. **Transpose 是唯一成本** —— 改造的关键是**最小化 transpose**。推荐：forward 内部把 `[B, P, F, S, 1]` 直接 reshape 成 `[B*S, F, C]`，在 F 和 C 间交替 FC，仅在 token-mix ↔ channel-mix 切换时做一次 `.transpose(1, 2)`。
2. **channel-mix expand ratio** —— 默认 4×；候选 `{2, 4, 8}`，同 D18。
3. **token-mix 维度选择** —— 原作是 token（NLP word）轴；model8 的两个轴是 F（subcarriers, 48）和 S（symbols, 64）。**默认 token-mix 沿 F 轴**（频域相关性物理意义明确）；S 轴可以做 channel-mix 的等价（如果 S 轴也有相关性，如时间维 Doppler）。
4. **不要全 pointwise 化两轴** —— 若 F 和 S 都做 channel-mix（无 token-mix），退化为纯 channel-MLP，丢失时频相关性建模。**至少一个轴必须做 token-mix**（FC 沿该轴）。
5. **feature_hook_names** —— 每个 MixerBlock 输出注册 hook，同 D18，对齐 OFD。
6. **与 D4 FNet 的区别** —— FNet 用 FFT 替代 token-mix（O(T log T)），昇腾 FFT 融合待验；Mixer 用静态 FC（O(F²)），完全 GEMM-land，**昇腾友好性更好**。
7. **容量选择** —— Mixer 的参数量近似 `N · (F² + 4C²)`；4 block + F=48 + C=64 ≈ 75K params/block × 4 = 300K total，是 D18 ConvNeXt-pointwise 的 1/3-1/2。
8. **fail-loud**：若 transpose 仍占 latency 大头（>15%），检查 ONNX 导出后是否触发了额外的 layout 转换；可能需要手动固定 NHWC/NCHW layout。
9. **GELU vs ReLU** —— 同 D18，默认 GELU，可换 ReLU。

## 来源
- MLP-Mixer：Tolstikhin et al., NeurIPS 2021 —— [arXiv:2105.01601](https://arxiv.org/abs/2105.01601) "MLP-Mixer: An all-MLP Architecture for Vision".
- 后续 ResMLP：Touvron et al., 2021 —— 残差连接 + 简化 Mixer（本卡借鉴残差形式）。

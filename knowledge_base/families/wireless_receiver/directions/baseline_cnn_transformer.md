# D0 · baseline_cnn_transformer（当前模型 / 对照基准）

> 一句话定位：仓库现状模型 —— Conv1d-along-frequency + 自定义 per-channel 64×64 symbol attention + Conv-FFN，4 block 堆叠。**作为对照基准**，自身不降时延，靠 M1–M7 的廉价 move 先吃红利。

## 结构
- **输入张量**：OFDM 时频网格接收信号 `Y ∈ R^{B×C_in×N_freq×N_sym}`（含导频），通常已做 `alpha=sqrt(mean²·2)` 归一化。
- **主干**：`Conv1d`（Conv-over-freq）stem → 4×[`SignalAttention1D`（per-channel 64×64 symbol attention）+ `Conv-FFN`] block。
  - **per-channel attention 怪异写法**：每个 channel 独立做 64×64 attention（沿 symbol 轴，N=64），QKV 投影用 Conv1d kernel=3 而非 Linear；非标准 MHA。
- **输出**：均衡后的软比特 / CSI（视任务而定）。
- **attention?**：**yes**（4 层全 attention，但 seq=64 极短）。

## 为什么降时延
**它本身不降 —— 是基准**。降时延靠叠加 move：先吃 M1/M2/M3/M5 融合层，再做 M6 减 block 4→2–3、M7 调 `npu_fusion_attention`。

## 昇腾友好性
**⚠️ conditional — 税重**：每条 Conv↔attention 边界触发一次 TransData（NC1HWC0↔NZ 内存重排），4 block × 2 边界 = 8 次/前向。msprof 可见 TransData 占比 ~7%+；attention 仅 17% CPU profile，**减 domain crossing 优先于减 attention FLOPs**。

## 物理依据
**no** —— 纯 conv+attn 通用近似器，无显式 OFDM/信道先验。

## bundle 的 move
**M1, M2, M3, M4, M5**（T1 廉价融合先吃）+ **M6, M7**（T2 减 block + 融合 attn 算子）。

## 结构前提与坑
1. **per-channel 64×64 attention 是怪异写法** —— `head_dim÷16` 和 `seq≥16` 两条昇腾融合 attn 算子门槛都要复核；不能直接套 `npu_fusion_attention`，需 reshape 重写（M7 前提）。
2. **N=64 下 linear/FlashAttention/Performer 全是陷阱**（N² 项 <0.4%，常数项吃光收益）—— 不要换 attention 形态，要么减层数、要么换主干（D1/D6）。
3. **T0 gating**：必须先测 conv-only baseline（D1 DeepRx 风格）能否达精度，达标则整个 Transformer 主干应被放弃。
4. LayerNorm 不可 fold → 能换 BN 就换（M1）。

## 来源
本仓库源模型 `nas-agent/examples/hw_inputs/model8/model/baseline_model.py`（`SignalProcessingTransformer` / `SignalAttention1D`）；profiling 见 plan §1。

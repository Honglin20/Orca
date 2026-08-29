# D9 · mamba_ssm_freq（双向 SSM 沿子载波扫描）

> 一句话定位：**双向 Mamba SSM** 沿子载波轴扫描 —— 用选择性状态空间模型替代 attention 做频率域全局上下文聚合，常数推理状态。

## 结构
- **输入张量**：同 D0。
- **主干**：Conv stem → N×[`bidirectional Mamba block`（前向 + 后向 SSM 沿 freq 轴扫描，selective state 更新）+ `Conv-FFN`]。
- **输出**：均衡后符号 / CSI。
- **attention?**：**no**（SSM 替代 attention）。

## 为什么降时延
1. SSM 推理是 `O(1)` per step（常数状态），无 `O(N²)` attention matrix。
2. 训练可并行（parallel scan），推理可顺序（RNN 模式）。

## 昇腾友好性
**⚠️ conditional — scan kernel 无原生算子** —— 昇腾 CANN 目前**无选择性 scan 原生算子**，需自定义算子或 lowering 为循环 matmul；循环形态会触发 Host dispatch / 小 kernel launch 开销。**未实测**。

## 物理依据
**yes** —— 频率轴信道响应是**连续可微**的（相邻子载波相关性强），SSM 的状态空间建模对齐频率域连续性先验。

## bundle 的 move
**M27**（Mamba/SSM 沿子载波扫描）+ **M1/M2/M3**（融合层，前提 scan 能落地）。

## 结构前提与坑
1. **scan kernel 是硬阻塞** —— 昇腾无原生 selective scan；要么等 CANN 后续版本支持，要么 lowering 为分块 matmul + 循环（性能存疑）。
2. **双向 Mamba** 是核心 —— 单向 SSM 沿 freq 会丢失反向上下文；前向+后向必选。
3. **未报 BER** —— 原作未给出 OFDM BER 数值，物理对齐是结构推断而非实测。
4. 与 D3（线性折叠）相比，SSM 有非线性 gate → 不可折叠成线性部署。
5. 与 D4（FFT-mix）相比，SSM 是参数化状态空间，FFT 是固定基；SSM 更灵活但 kernel 更难落地。

## 来源
[arXiv:2601.17108]（2026，Mamba 无线接收机方向）—— plan §10 标注新文未复现、scan kernel 待验。

# D4 · fnet_fft_mix（FFT 替代时间轴 softmax attn）

> 一句话定位：用 **FFT-mixing 替代时间轴的 softmax attention**，复杂度 `O(T²)→O(T log T)` —— 保留 token-mixing 能力但换数学形态（无 Q/K/V、无 softmax）。

## 结构
- **输入张量**：同 D0。
- **主干**：Conv1d-over-freq stem → 4×[`FFT-mix block`（沿 symbol 轴做 `X ← IFFT(FFT(X))` 的频域混合，无 softmax）+ `Conv-FFN`]。
- **输出**：均衡后符号 / CSI。
- **attention?**：**no**（FFT-mixing 取代 attention，无 Q/K/V、无 softmax）。

## 为什么降时延
1. `O(T²)→O(T log T)`，在 T=64 上理论 FLOPs 下降。
2. 无 softmax → 无 QK^T bmm → 昇腾上少一类 NZ/ND 格式 matmul。

## 昇腾友好性
**⚠️ conditional — FFT 融合待验** —— 昇腾 FFT 算子（if via MindSpore FFT）是否原生 Cube 融合、是否触发 Vector-only 通路，**未在 OFDM 接收机场景公开实测**。落地前必须 micro-bench：FFT kernel 形态、是否碎片化。

## 物理依据
**yes（Doppler 稀疏）** —— 时间轴信号在 delay-Doppler 域常稀疏（多普勒有限扩展），FFT-mixing 等价于隐式 delay-Doppler 投影，物理对齐稀疏先验。

## bundle 的 move
**M10**（FFT-mixing 替时间轴 softmax attn）+ **M1/M2/M3**（融合层）。

## 结构前提与坑
1. **FFT 融合是关键未知数** —— 若昇腾 FFT 走 Vector 通路（非 Cube），FLOPs 收益被带宽吃光，时延不降反升。必须先 micro-bench。
2. **Doppler 稀疏假设在高铁/mmWave 大多普勒场景失效** —— 该方向假设准静态信道，高动态场景需换 D7 windowed attn。
3. **N=64 太短** —— `O(T log T)` 在 T=64 上常数项可能 > `O(T²)`，与 D0 的"N=64 linear attn 是陷阱"同因。**短序列 FFT-mix 不一定更快**。
4. 与 D3（部署期折叠）相比，D4 保留非线性 → 不能折叠成线性，部署灵活性低。

## 来源
[arXiv:2105.03824] FNet（Lee-Thorp 2021，NLP 原作）；OFDM 接收机应用为结构推断，未实测。

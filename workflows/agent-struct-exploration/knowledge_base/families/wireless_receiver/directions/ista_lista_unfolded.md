# D8 · ista_lista_unfolded（展开收缩网 / delay-domain soft-threshold）

> 一句话定位：**展开收缩网**（ISTA-Net / LISTA），delay-domain **soft-threshold** 显式稀疏先验 —— 把迭代收缩算法展开成固定层数的可训练网络。

## 结构
- **输入张量**：同 D0。
- **主干**：每层 = [`Linear`（FFT 到 delay/Doppler 域） → `soft-threshold(·, τ)`（τ 可学） → `Linear`（IFFT 回时频域） → `residual add`]，展开 T 次。
- **输出**：均衡后符号 / CSI。
- **attention?**：**no**。

## 为什么降时延
1. 全是 Linear + elementwise soft-threshold —— 无 attention、无 bmm、无 softmax。
2. soft-threshold 是 elementwise，可融合进前一层 bias/add 算子。
3. FFT/IFFT 可静态 shape → 昇腾融合 pass 覆盖。

## 昇腾友好性
**✅ friendly** —— Linear = GEMM，soft-threshold = elementwise；**唯一风险**：FFT/IFFT 在昇腾上的 kernel 形态（同 D4，需 micro-bench）。但与 D4 不同，此处 FFT 是**结构必选**（delay-domain 假设是核心），不可替换。

## 物理依据
**yes（多径 ℓ1 稀疏）** —— 物理信道冲激响应（CIR）在 delay 域**稀疏**（少量多径），soft-threshold 显式实现 ℓ1 先验，ISTA 是迭代收缩算法的可微版。

## bundle 的 move
**M9**（Conv1d↔Transformer 间插可学习 delay-domain soft-threshold，τ→0 no-op，fail-forward）+ **M1/M2/M3**（融合层）。

## 结构前提与坑
1. **稀疏先验在 rich-scattering 场景失效** —— 室内 NLOS 多径密集时 CIR 不再稀疏，soft-threshold 过度截断会伤精度。
2. **τ 是关键超参** —— 可学 τ 是论文核心；固定 τ 会退化；τ→0 时网络退化为纯线性（fail-forward 退路）。
3. FFT kernel 在昇腾上**未实测**，若走 Vector 通路时延收益被吃；需 micro-bench 验证。
4. **展开层数 T** 是精度-时延旋钮，与 D6 的 N_it 类似，改 T 改静态图，需 sinking dispatch 缓存。
5. delay-Doppler vs delay-only：D8 默认 delay-domain；多普勒场景需扩展到 2D delay-Doppler soft-threshold。

## 来源
[arXiv:2104.13656]（OFDM 展开 ISTA，2021）+ ISTA-Net（Zhang & Ghanem, CVPR 2018）。

# 无线接收机族结构原语（families/wireless_receiver/primitives.md）

> 用途：Engineer 把假设渲染成可训练代码时的"本族原语速查"；Hypothesizer 提变异假设时的"本族可操作部件清单"。跨族通用原语（残差 / 1×1 conv / 归一化 / 激活 / gating / MoE / PE / pooling / attention 骨架）见 `common/primitives.md`，本文件只列**本族专属**或在本族有特殊形态的原语。
>
> 统一格式：**是什么 / 在 baseline 哪里 / 可怎么变异 / 昇腾友好性提示**。
>
> **baseline 指针**：`/mnt/d/Projects/nas-agent/examples/hw_inputs/model8/model/baseline_model.py`（`SignalProcessingTransformer`，4 block，embed_dim=16，64 symbol × 48 subcarrier × 4 port）。输入 `[B, num_ports=4, num_subcarriers=48, num_symbols=64, 1]`，工作张量 `[B, 64, 16, 48]`（symbol, embed_dim, subcarrier）。硬件铁律见 `common/ascend_constraints.md`（以下简称"§A1"指铁律 1，类推）。

---

## 1. Conv1d-over-freq（沿子载波/频率轴的 1D 卷积）

- **是什么**：把 OFDM 时频网格的"子载波（频率）"轴当作 1D conv 的 sequence 维度，用 `nn.Conv1d(in_channels=embed_dim, ..., kernel_size=3, padding=1)` 在频率轴上做 3-tap 平滑。物理含义：相邻子载波相干带宽内的一致性 / 信道频域响应的局部多项式近似。
- **在 baseline 哪里**：几乎每个 Conv1d 都是这种形态——`e_lyr`（入口投影）、`p_lyr`（QKV 投影）、FFN 的 `cv1`/`cv2`、block 内的 `proj`、`r_out`（出口）。实现上把工作张量 reshape 成 `[B*64, embed_dim=16, num_subs=48]`，48 是 Conv1d 的长度维。
- **可怎么变异**：
  - **pointwise 化**：3-tap → 1×1（kernel_size=1），消 Im2Col 与一次 TransData 触发点（latency_moves M4）。
  - **dilated 化**：保 kernel=3 但加 dilation，扩感受野不增 FLOPs（§6 / M20）。
  - **频域 → 时域切换**：把某些 conv 从沿频率改沿 symbol（时间）轴，匹配 Doppler 相干时间（D7 windowed attn 同源）。
- **昇腾友好性提示**：3-tap conv 是 Im2Col+Cube（§A1），友好；1×1 是直接 GEMM（§A3），最友好；DW/group 是禁区（§A3）。48 不是 16 倍数但它是空间轴不是通道轴，不触发 C0 padding（§A9）。

## 2. symbol-axis attention（沿时间/symbol 轴的 softmax mixing）

- **是什么**：以 64 个 OFDM symbol（时间轴）为 attention 的"序列"，Q·K 得 `64×64` 矩阵、softmax 后加权 V。物理动机：Doppler 维度的时变信道让相邻 symbol 间存在可建模的相关性，softmax mixing 是"时间维度的软对齐"。
- **在 baseline 哪里**：`SignalAttention1D` 的 `m_type="t1"` 路径——`dots = matmul(q, k.transpose(-1,-2))` 输出 `[B, 16, 64, 64]`，最后一维 64 是 symbol 数，softmax over symbol（见 §3 的逐通道拆解）。
- **可怎么变异**：
  - **windowed / 局部窗**：W=16 的带状 mask（只让 ±16 个 symbol 互相 attend），匹配相干时间（M8 / D7）。
  - **轴向 / 可分**：先 symbol 轴再 subcarrier 轴（M21 / D7 的 time-then-freq）。
  - **折叠成线性**：部署期把 attention 等价折成单一线性滤波器（M13 / D3 A-MMSE）。
  - **FFT-mixing 替换**：softmax → FFT 沿时间轴（M10 / D4 FNet）。
- **昇腾友好性提示**：**手搓 bmm+softmax+bmm 是禁区**（§A7、`failures.md`）——3 个独立 GEMM + softmax 中间矩阵落 HBM + 每个边界触发 TransData（§A2）。保留 attention 必须改写成 `npu_fusion_attention` 形状（head_dim ÷ 16）。

## 3. ★ per-channel 64×64 attention（baseline 怪异写法，变异的关键着手点）

- **是什么**：baseline 的 attention **不是标准 MHA**，而是一种"每 embed_dim 通道一个独立单头 attention"的结构。具体地：embed_dim=16 当作 16 个**互不混合**的并行 head，每个 head 内部 Q/K/V 形状为 `[64 symbol, 48 subcarrier]`，`dots = q @ k.T` 把 **48 个子载波当 d_k** 做点积，得到**每个 head 一个 64×64 的 symbol-axis attention 矩阵**；softmax over symbol，再乘 V。等价说法：这是 **16 个独立的单头 attention，d_k=48，time-axis mixing**，head 之间没有任何信息交互（标准 MHA 的 head 间独立 + 输出投影的跨 head 混合，这里没有跨 head 混合）。
- **在 baseline 哪里**：`SignalAttention1D.forward`，`m_type="t1"` 分支。关键三行：
  - `q = q.permute(0, 2, 1, 3)` 把 `[B, 64, 16, 48]` → `[B, 16, 64, 48]`（16 升到 batch 维，成为"独立 head 轴"）
  - `dots = matmul(q, k.transpose(-1, -2)) * self.s` → `[B, 16, 64, 64]`，`self.s = num_subcarriers**-0.5` 即 `d_k=48` 的缩放
  - `out = matmul(at, v).permute(0, 2, 1, 3)` 回到 `[B, 64, 16, 48]`
  代码注释：`m_type=="t1"` 是 symbol 轴 mixing（d_k=子载波），另有 `t2` 是子载波轴 mixing（d_k=embed_dim=16）——baseline 只用 t1。
- **可怎么变异**（这是变异的关键着手点，每条都直接对应一个 latency_move）：
  - **改成标准 MHA 走融合算子**：把 16 个"伪 head"重排成真 `(num_heads=1, head_dim=16)` 或 `(num_heads=2, head_dim=8)`——但 head_dim 必须 ÷16（§A7），所以更现实的是 `num_heads=1, head_dim=16` 单头 MHA + 输出投影，调 `npu_fusion_attention`（M7）。代价：丢"每通道独立"的归纳偏置，需 fine-tune。
  - **折叠成线性滤波器**：per-channel 结构天然是 16 个独立的 64×64 线性变换的"软"版——部署期 softmax 饱和后可近似成稀疏线性（M13 / D3）。
  - **直接砍掉**：attention 在本模型只占 17%（plan §1），领域 SOTA（DeepRx 等）不用 attention——先测 conv-only baseline（D1/D6）达标则整个砍（M15）。
  - **windowed 收窄**：64×64 → 64×16 带状，减 attention FLOPs 同时保物理相干时间（M8）。
- **昇腾友好性提示**：当前实现 **head_dim=48 不是 16 倍数**（§A7），融合算子拒编；且是手搓 bmm（3 个 GEMM + TransData，§A2）。**这是 baseline 时延最该先动的地方之一**：要么重排成 head_dim=16 走 `npu_fusion_attention`，要么折叠/砍掉。
- **来源**：baseline 源码 `baseline_model.py:5-58`；领域反证见 plan §1（DeepRx/EqDeepRx/NVIDIA NRX 均不用 softmax attention）。

## 4. Conv-FFN（cv1/cv2 卷积前馈）

- **是什么**：Transformer FFN 的卷积化变体——用两个 Conv1d（`cv1: embed_dim→2·embed_dim`，GELU，`cv2: 2·embed_dim→embed_dim`）替代标准 `Linear→GELU→Linear`。相当于"4× expansion ratio 的 FFN，但空间维（频率轴）也参与 3-tap 聚合"。
- **在 baseline 哪里**：`SignalFeedForward1D`，每个 block 一个。先 LN，再 reshape 到 `[B*64, embed_dim, 48]`，过 cv1 → GELU → cv2。
- **可怎么变异**：
  - **pointwise 化 cv1/cv2**（kernel 3→1）：FFN 变标准 GEMM-only，落 Cube 无 Im2Col（M4）。
  - **降 expansion ratio**：2× → 1.5× 或 1×，省一半 FFN FLOPs（需 fine-tune）。
  - **GeGLU/SwiGLU 门控化**：cv1 出 3·embed_dim，前 1.5·做内容、后 1.5·做门（M 系列可加，但注意门控 mul 不可融合，§A5）。
- **昇腾友好性提示**：cv1+GELU+cv2 是 `MatMul+Bias+GELU` 类融合候选（§A5），但 GELU 比 ReLU 量化不友好（`common/primitives.md` §5）。3-tap 的 Im2Col 开销可被 1×1 消除。

## 5. Conv-QKV（p_lyr 卷积投影）

- **是什么**：标准 Transformer 用 3 个 `Linear` 投影 Q/K/V（或 1 个拼成 3·dim 的大 Linear）；baseline 用**一个 `Conv1d(embed_dim → 3·embed_dim, kernel=3)`** 做投影，即在投影的同时沿频率轴做 3-tap 平滑——卷积与投影融合在同一个算子里。
- **在 baseline 哪里**：`SignalAttention1D.p_lyr`，输出 reshape 成 `[B, 64, 3·embed_dim=48, 48]` 后切片成 q/k/v（各 `[:, :, 0:16, :]` / `16:32` / `32:48`）。
- **可怎么变异**：
  - **QKV 单点投影（fold）**：把 3-tap Conv1d 换成一个 1×1（pointwise），等价于标准 QKV 大 GEMM（M5）。3-tap 的频率平滑让给 FFN 或单独的频率 conv。
  - **stem→QKV 重参数化**：把入口 `e_lyr` 与 `p_lyr` 合并成一次投影（代数等价，M5）。
- **昇腾友好性提示**：3-tap Conv-QKV 是合法 Cube 路径（§A1），但 kernel=3 触发 Im2Col；1×1 直 GEMM 更友好（§A3）。切片 q/k/v 的 reshape 在昇腾上是 view/permute，可能触发 TransData（§A2），重排成 `num_heads, head_dim` 时要顺带对齐格式。

## 6. Dilated / multidilated conv（扩感受野不增 FLOPs）

- **是什么**：标准 Conv1d 加 `dilation=r`（在 kernel 元素间插 r−1 个空洞），kernel=3 + dilation=r 的感受野 = `2r+1`，但 FLOPs 与标准 3 相同。multidilated = 不同层/分支用不同 rate（{1,2,4,8}）堆叠，等效感受野 RF=31 同参数量。
- **在 baseline 哪里**：**当前 baseline 未用**（所有 conv 都是 dilation=1）。这是 DeepRx 风格 conv-only 方向（D1）的核心构件。
- **可怎么变异**：
  - 把 pointwise 化（§1 的 M4）丢掉的频率局部平滑，用 dilated conv 在别处补回（M4+M9/M20 组合）。
  - 多分支 multidilated block（rate {1,2,4,8} 各一路，concat）替标准 conv block（D1 / M20）。
- **昇腾友好性提示**：dilated conv 仍是标准 Conv1d → Cube GEMM（§A1），友好；只要不是 DW/group（§A3）。rate 不影响 Cube 利用率（只影响 Im2Col 的 gather 模式）。

## 7. Delay-domain soft-threshold（多径稀疏先验的展开收缩）

- **是什么**：在 Conv 与 Transformer 之间插一层"FFT → soft-threshold(τ) → IFFT"的可微模块：把特征沿时间/频率变到 delay 域，对 delay profile 做软阈值 `sign(x)·max(|x|−τ, 0)` 压掉小径（物理 = 多径信道在 delay 域稀疏），再变回。τ 可学；τ→0 时模块退化为恒等（fail-forward）。
- **在 baseline 哪里**：**当前 baseline 未用**。这是 ISTA-Net+/展开收缩网方向（D8 / M9）的原语。
- **可怎么变异**：作为 Conv↔Transformer 边界处的"轻插入层"（M9），同时顺手吸收一次 domain crossing（§A2）——如果 FFT/IFFT 走昇腾 FFT 算子，要在同一个 delay 模块里完成变换+阈值+反变换，不增加 conv↔matmul 边界数。
- **昇腾友好性提示**：**soft-threshold 的 `sign`+`max` 是不可融合 elementwise**（§A5），会逃出 AutoFuse 留一次 Vector launch；但只一处、可接受，且 τ→0 可关。FFT/IFFT 在昇腾上有 Vector 核 FFT 算子但**碎片小 FFT kernel 易慢**（见 `failures.md` 的 Toeplitz/FFT-fast 形）——用"整段大 FFT"而非多个小 FFT。

## 8. FFT-mixing 层（FFT 替时间轴 softmax attention）

- **是什么**：FNet 式——把 symbol 轴的 softmax attention 换成"沿 symbol 轴做 1D FFT → 实部/虚部 → IFFT"，O(T²)→O(T log T)。物理动机：Doppler 域稀疏（高速场景信道在 Doppler 域集中），FFT mixing 是"软的频域滤波"。
- **在 baseline 哪里**：**当前 baseline 未用**。FNet 方向（D4 / M10）的原语。
- **可怎么变异**：直接替换 `SignalAttention1D` 整块（M10），去掉 64×64 attention 与所有 TransData。
- **昇腾友好性提示**：**FFT 是否能融合到相邻 GEMM 未知**（M10 标 ⚠️ FFT 融合待验）——昇腾 FFT 是 Vector 核独立算子，与 Cube 边界处仍可能 TransData（§A2）。必须 micro-bench 确认 FFT-mixing 是否真比融合 attention 快。

## 9. Pilot-grid 多通道输入（[Y, Y⊙Xp*, Xp, mask]）

- **是什么**：把 OFDM 接收机的输入通道从"只有接收信号 Y"扩展成多通道时频网格：接收 Y、导频处的去调制项 `Y⊙Xp*`（Xp 是已知导频符号）、导频符号 Xp、导频位置 mask。让网络显式看到"哪里是导频、导频值是什么"，等价于把 LMMSE 的输入特征化进网络。
- **在 baseline 哪里**：**当前 baseline 未用富化版**（in_channels=4，是 4 个 antenna port 各一路，不是富化的多通道）。这是 DeepRx 风格（D1 / M18）的标准输入，**默认开**。
- **可怎么变异**：把 `e_lyr` 的 `in_channels` 从 4（port 数）扩成 `4 × 通道数`（如 `[Y, Y⊙Xp*, Xp]` 各 4 port = 12 通道），M18 默认每个候选都带。
- **昇腾友好性提示**：仅改输入通道数（首层 conv 的 in_channels），对 Cube 完全友好（§A1）；in_channels 不需要 ÷16（它是 C1 维不是 C0 维）。近零精度增益、近零时延代价（DeepRx 实测）。

## 10. Port / 流权重共享（多天线共享一个 DetectorNN）

- **是什么**：MIMO 多 port / 多流的接收机不每路各设一套网络权重，而是**所有 port/流共享同一个 DetectorNN**（只前向时按 port 维 batch 化）。物理依据：所有 port 走同一条物理信道统计、只需一个均衡器核。
- **在 baseline 哪里**：**当前 baseline 实质已部分共享**——前向把 `[B, num_ports, 48, 64]` reshape 成 `[B*64, ports=4, 48]`，把 port 当作 channel 维（in_channels=4），网络权重只一套。但 4 个 port 走的是"一个 conv 的 4 个输入通道"而非"4 路独立共享"——语义略不同。EqDeepRx（D2 / M11/M26）的显式共享是更彻底的版本。
- **可怎么变异**：前置 LMMSE 后，4 个 port 共享 DetectorNN+DenoiseNN（M11/M26），权重 ÷4、时延近 4× 降（plan §5 估算）。
- **昇腾友好性提示**：权重共享 = 参数 ÷4、activation 不变 → 访存降、Cube 工作量不变（§A1）。batch 维变大让 Cube 利用率更好（§A9）。这是昇腾上最友好的"降时延"之一。

## 11. alpha = sqrt(mean²·2) 归一化与去归一化（功率归一化）

- **是什么**：入口对原始接收信号做按样本功率归一化 `alpha = sqrt(mean(inp², dim=[1,2,3], keepdim=True) * 2)`，`x = inp / (alpha+1e-6)`；出口乘回 `x = x * alpha`。`*2` 的来历：OFDM 信号是复数，`mean` 沿实部虚部一起算，乘 2 近似复功率 `E[|x|²]`。物理 = 把不同 SNR / 不同发射功率的样本拉到统一尺度，让网络在单位功率特征上学习，输出再还原原始幅度。
- **在 baseline 哪里**：`SignalProcessingTransformer.forward` 入口（`alpha = ...` / `x = inp / (alpha+1e-6)`）与出口（`x = x * alpha`）。
- **可怎么变异**：
  - 一般**不动**——这是物理归一化，与 BER/SNR 语义绑定。
  - 若改输入为富化多通道（§9），`alpha` 要对每个通道分别算或对 Y 通道算后广播。
  - 折叠：出口的 `x * alpha` 是标量 broadcast mul，可融合到前一算子（§A5 AutoFuse 范围内）；不要单独留一个 elementwise mul。
- **昇腾友好性提示**：`mean` 跨多个维度的归约走 Vector 核（§A4 类似），是一次 Vector launch，但只入口一处、可接受。`x*alpha` 的标量 mul 可融合；`x/(alpha+1e-6)` 同理。注意 `alpha` 的 `sqrt` + `mean` 在 FP16 下数值稳定（+1e-6 已防 0）。

---

## Engineer 渲染规则（本族专属）

- 选原语前**先过 `common/ascend_constraints.md` 的自检清单**（10 条）。本族 baseline 的两个最大着手点是 §3（per-channel attention 重排/砍掉）与 §1/§5（3-tap conv pointwise 化）。
- baseline 的所有 Conv1d 都是"沿频率轴"（48 是长度维）——改成沿时间轴时要同步改 reshape，并在假设里标注"轴切换"。
- 本族工作张量是 4D `[B, symbol, embed_dim, subcarrier]`，每次进 Conv1d 要 reshape 成 3D、进 attention 要 permute——**这些 reshape/permute 是 TransData（§A2）的高发点**，渲染时尽量让相邻算子共用同一 layout，少做 permute。
- 非主流原语（delay-domain soft-threshold、FFT-mixing、CVNN-lowered、Mamba-scan）必须在假设里标"研究性 / 算子形状推断未实测"（plan §10.1），让 Analyst 重点验证。

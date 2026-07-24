# CNN 族结构原语（primitives）

> 用途：列清 CNN 族「可用的结构原子」与其归纳偏置 / 时延特性。Hypothesizer 在生成降时延假设前从此文件挑可操作的原语；本文件只描述「是什么 / 偏置 / 时延属性」，不写「怎么组合降时延」（那是 `latency_moves.md` 的事）。

阅读约定：每条原语给出「是什么 / 归纳偏置 / 时延特性」。**FLOPs** 衡量算术量；**MAC**（memory access cost）/ **kernel launch** / **element-wise** 才是真实时延主因——CNN 降时延的核心是后者（ShuffleNet V2 G3/G4）。

---

## 1. 标准 2D 卷积（standard / dense conv）

- **是什么**：`out[C_out]=Σ_{C_in} K[k_h×k_w] ∗ x`，每个输出通道看全部输入通道。`Conv2d(in, out, k×k, stride, pad)`。
- **归纳偏置**：局部性 + 平移等变 + 通道间全混合。表达能力最强，是 CNN 的"基准动作"。
- **时延特性**：FLOPs = `C_in·C_out·k²·H·W`（与通道乘积、kernel 平方、分辨率线性相关）。**通道间全连接使 `C_in·C_out` 主导 FLOPs**，是 CNN 头号 FLOPs/MAC 大户。kernel launch 少（一次算子）。

## 2. Depthwise conv（DW）

- **是什么**：每个输入通道独立卷一个 kernel，`out[c] = K_c ∗ x[c]`，通道间不混合。`groups = in_channels = out_channels`。
- **归纳偏置**：局部性 + 平移等变，**不做通道混合**（要靠后接的 1×1 conv 补）。
- **时延特性**：FLOPs = `C·k²·H·W`（无 `C_in·C_out` 项）。相对标准 conv 几乎降 `C_out` 倍 FLOPs。**但访存／kernel launch 比例变差**（每通道独立算，GPU 上效率低），实际时延收益小于 FLOPs 收益——在桌面 GPU 上 DW 常被吐槽"省 FLOPs 不省时延"，但在 edge TPU / CPU 上确有收益。

## 3. Depthwise-separable conv（DW-SEP = DW + PW）

- **是什么**：DW（spatial 混合）+ 1×1 pointwise conv（通道混合）串联，替代单次标准 conv。
- **归纳偏置**：把"空间聚合"与"通道投影"解耦，假设两者可分离。MobileNet V1 的基本积木。
- **时延特性**：理论上 FLOPs ≈ 标准 conv 的 `1/C_out + 1/k²`（典型 8–9× 更少）。但拆成两个 kernel，**多一次 kernel launch + 多一次中间 tensor 的访存**，在小 batch / GPU 上比例开销不可忽视。

## 4. Group conv

- **是什么**：通道分成 `g` 组，组内做标准 conv，组间不混合。`groups=g`。
- **归纳偏置**：局部通道相关性（组内混合），组间不直接交互——需要 **channel shuffle** 补信息流（ShuffleNet V1）。
- **时延特性**：FLOPs 与 MAC 都 ≈ `1/g` 的标准 conv。但 `g` 越大 MAC / FLOPs 比值越恶化（ShuffleNet V2 G2：group 数过大反而增加 MAC、降低单位 FLOPs 的实际效率）。

## 5. 1×1 projection conv（pointwise）

- **是什么**：kernel=1 的卷积，纯通道线性投影，无空间聚合。
- **归纳偏置**：通道维度的仿射变换，常用于升降维。
- **时延特性**：FLOPs = `C_in·C_out·H·W`（无 k² 项）。**是 CNN 里单位 FLOPs 时延最低的算子之一**（GPU 上 cuDNN 高度优化、访存友好）。bottleneck / SE / inverted residual 都靠它做通道控制。

## 6. Channel shuffle

- **是什么**：把 `(C)` 通道重排成 `(g, C/g)` 后转置 reshape，让 group conv 的"组边界"在下一层错开。
- **归纳偏置**：不引入参数也不做计算，纯 memory permute——纯结构性的"信息流通保证"。
- **时延特性**：无 FLOPs，**但访存 / kernel launch 一次**；在 GPU 上是廉价的 reshape 类操作。ShuffleNet 的命脉。

## 7. Bottleneck block（1×1 降 → 3×3 → 1×1 升）

- **是什么**：先 1×1 把通道从 `C` 压到 `C/r`（r≈4），3×3 conv 在窄通道做，再 1×1 升回 `C`。ResNet-50 的标准 block。
- **归纳偏置**：信息在窄通道内做空间聚合，宽通道只做投影——"算得少但表达够"。
- **时延特性**：相比"宽通道 3×3 conv"，FLOPs 降 ~`r²` 数量级。代价是 kernel launch 数翻 3。

## 8. Inverted residual block（expansion → DW → projection）

- **是什么**：先 1×1 把通道从 `C` 扩到 `tC`（t≈6），DW 在宽通道做空间，再 1×1 线性投影压回 `C`；**残差发生在窄的输入输出两端**（与标准 ResNet bottleneck 相反，故名"inverted"）。MobileNet V2。
- **归纳偏置**：DW 无通道混合，需要先 expand 到高维才有足够表达力；输出端做线性投影（**无 ReLU**）以保留低维特征。
- **时延特性**：DW 在宽通道做、PW 在窄通道做，FLOPs 极低；GPU 上 DW 仍是效率短板。linear bottleneck（去掉投影后 ReLU）是精度关键。

## 9. Ghost module

- **是什么**：先用 1×1 conv 生成 `m` 个"intrinsic"特征图，再用廉价线性算子（通常 3×3 DW）对每个 intrinsic 做变换生成 `n` 个"ghost"特征，拼接得 `m+n` 个输出。GhostNet。
- **归纳偏置**：CNN 特征图冗余度高——很多通道是少数"本征"通道的简单变换。直接显式建模这种冗余。
- **时延特性**：相比标准 conv 出同样通道数，FLOPs / params 约 `1/s`（s≈2，cheap op 的比例）。一次 ghost module ≈ 1×1 + DW，kernel launch 数中等。

## 10. SE（Squeeze-and-Excitation）channel attention

- **是什么**：global avg pool 把每通道压成 1 个标量 → FC（降维 r=16）→ ReLU → FC（升维）→ sigmoid → 每通道乘一个 [0,1] 权重。SENet。
- **归纳偏置**：通道重要性是全局可学习的标量。
- **时延特性**：参数与 FLOPs 都很小（~2–5%），**但每次 forward 多两个 FC + 一次 global pool + 一次 broadcast multiply**，GPU 上多 4 个 kernel launch、CPU 上也有不可忽略开销——在 latency-critical 场景必须放进正确位置（每 stage 末尾、不要每 block 都塞）。

## 11. CBAM（channel + spatial attention）

- **是什么**：先 channel attention（同 SE 但同时用 max-pool 和 avg-pool 双路）→ 再 spatial attention（channel 维 max+avg → 7×7 conv → sigmoid）。
- **归纳偏置**：通道重要 + 空间重要都建模。
- **时延特性**：比 SE 多一次空间注意力分支、多两次 pool 与一个 7×7 conv——**开销显著大于 SE**，时延敏感场景一般只取 SE 或不用。

## 12. Dilated conv（空洞卷积）

- **是什么**：kernel 上每隔 `r-1` 个采样点取一个，感受野扩大 `r` 倍但 FLOPs 不增、参数不增。
- **归纳偏置**：在不降分辨率的前提下扩感受野（DeepLab 的语义分割常用）。
- **时延特性**：FLOPs 与标准 conv 相同（采样点稀疏）；访存稍不友好（稀疏访存模式）。本身不算降时延原语，但可替代"先池化再升通道"的扩感受野路径。

## 13. Deformable conv（可变形卷积）

- **是什么**：每个采样位置学一个 2D offset（额外 offset 分支预测），kernel 在偏移位置采值（双线性插值）再卷积。Deformable Conv v2。
- **归纳偏置**：几何不变性 / 形状自适应，适合检测分割里非规则目标。
- **时延特性**：**高时延算子**——额外 offset 预测分支 + 不规则访存的双线性采样，GPU 上慢、edge 上更慢。在 latency-critical 场景一般不引入或仅放一两层。

## 14. Pooling（max / avg / global avg / global max）

- **是什么**：固定 kernel 的降采样（无参数）；global avg pool 把 `C×H×W` 压成 `C`，常用作分类头。
- **归纳偏置**：局部不变性（max=强不变 / avg=弱不变）。GAP 同时是结构归纳——"类别响应是通道维全局聚合"。
- **时延特性**：无参数、FLOPs 低；但 **LLM-NAS 的 Co-evolve KB 归纳出一条规则："`avg_pool_3x3` 时延长且对精度提升有限"**——这是 LLM 在闭环里从数据里观察到的、可作为结构性 move 的依据（来源 05-llm-nas KB 示例）。

## 15. 残差连接（residual / skip connection）

- **是什么**：`y = F(x) + x`（或 `+ downsample(x)` 若通道/分辨率变）。
- **归纳偏置**：学残差比学恒等更容易；缓解梯度消失；让"加深而不变差"成为可能（ResNet）。
- **时延特性**：本身只是一个 element-wise add，几乎无 FLOPs；但是 **element-wise op 在 GPU 上是 MAC 浪费**（ShuffleNet V2 G4）——加得太多会拖时延。同时也是"减层保精度"的护栏（见 `latency_moves.md`）。

## 16. Normalization（BN / GN）

- **是什么**：BatchNorm 跨 (N, H, W) 归一化每通道；GroupNorm 跨 (H, W, 组内通道)。
- **归纳偏置**：BN 把 batch 内统计当先验；训练时正则，推理时折叠成 affine。
- **时延特性**：训练时 BN 多次统计 + scale/shift；**推理时 BN 可折叠进前一 conv 权重（数学等价），零额外时延**——这是"推理友好"的重要 move（见 `latency_moves.md` RepVGG 一节）。

---

## 原语时延维度小结

| 原语 | FLOPs | MAC 友好度 | kernel launch | 典型适用场景 |
|---|---|---|---|---|
| 标准 conv | 高（`C²k²`） | 中 | 1 | 精度优先、最后几层 |
| DW conv | 极低 | **差** | 1 | 移动端、搭配 PW |
| DW-SEP | 低 | 中 | 2 | 通用高效 |
| Group conv | 低（÷g） | 差（g 大时） | 1 | 搭配 shuffle |
| 1×1 pointwise | 低 | **优** | 1 | 升降维、注意力 |
| Channel shuffle | 0 | 中 | 1 | group conv 后必接 |
| SE attention | 极低 | 中 | 4+ | stage 末尾低开销提精度 |
| Deformable conv | 高 | **极差** | 多 | 检测/分割，避开 latency 场景 |
| Pooling | 极低 | 中 | 1 | 降采样；avg_pool 实测可能慢 |

**核心规律**：CNN 时延主因是 **MAC 与 kernel launch**，不是 FLOPs（ShuffleNet V2 的核心论点）。原语的选择要落到这两个真实维度。

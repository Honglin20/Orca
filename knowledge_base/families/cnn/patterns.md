# CNN 族已知高效变体结构前提（patterns）

> 用途：列清业界已验证的高效 CNN 变体**起作用的结构前提**——只有满足这些前提，变体才成立、才真有"时延-精度"双赢。Engineer 在落代码时若想套用某变体，必须逐条核对前提；任何前提不满足时，该变体退化为空有形式而无收益的结构（甚至更差）。

每条变体格式：**变体 → 结构前提 → 时延-精度权衡 → 来源**。「来源」只引用论文实际说过的内容。

---

## 1. MobileNet V1（DW-SEP 替代标准 conv）

- **变体**：把 backbone 里所有 3×3 标准 conv 换成 `DW 3×3 + PW 1×1`。
- **结构前提**：
  - DW **必须**接 1×1 PW 做通道混合——DW 单独用信息出不了通道，精度大幅退化。
  - PW 后接 BN+ReLU6（ReLU6 是为 PW 输出有界，配合低精度量化）。
  - 通道数不能过小（否则 DW 在窄通道上精度掉，见 `failures.md`）。
- **时延-精度权衡**：理论上 FLOPs ≈ 标准 conv 的 `1/C_out + 1/9`；ImageNet 上相比同 FLOPs 标准 conv 网络，精度高且快。代价：在桌面 GPU 上实际时延收益小于 FLOPs 收益（DW 在 GPU 上效率差）。
- **来源**：Howard et al. 2017, *MobileNets: Efficient Convolutional Neural Networks for Mobile Vision Applications*。

## 2. MobileNet V2（inverted residual + linear bottleneck）

- **变体**：block = `1×1 expand(tC) → BN+ReLU6 → DW 3×3 → BN+ReLU6 → 1×1 project(C) (linear, no ReLU)`；**残差发生在窄的两端**（stride=1 且输入输出通道同）。
- **结构前提**：
  - **expand ratio t ≈ 6**（先用 1×1 升维，DW 才能在高维表达）。
  - 投影后的 1×1 **不带 ReLU**（linear bottleneck 假设：低维流形上 ReLU 会破坏信息）。
  - 残差只在 stride=1 且通道匹配时才加；stride=2 的下采样 block 不加残差。
- **时延-精度权衡**：比 V1 在同精度下更少 FLOPs、更少内存（narrow input/output 省激活内存）；线性瓶颈消除 ReLU 在低维的特征坍塌。代价：fragmentation（每 block 3 个 kernel），GPU 上 launch overhead 累积。
- **来源**：Sandler et al. 2018, *MobileNetV2: Inverted Residuals and Linear Bottlenecks*。

## 3. ShuffleNet V1（group conv + channel shuffle）

- **变体**：bottleneck 中 1×1 conv 用 **group conv**（g≈3–8），group conv 后接 **channel shuffle** 让下一层 group 的边界错开；DW 只做 3×3 stride。
- **结构前提**：
  - group conv **必须**配 channel shuffle，否则信息被锁在组内、组间永远不交换（精度随深度无法恢复，见 `failures.md`）。
  - 1×1 group conv 才是省 FLOPs 的关键（不要把 3×3 也 group conv，3×3 用 DW 已经省了）。
  - shuffle 操作无参数无 FLOPs，纯 reshape。
- **时延-精度权衡**：相比 MobileNet V1 在同 FLOPs 下 ImageNet 精度高 ~1–2%（group conv 比 DW 在中通道数表达力强）。
- **来源**：Zhang et al. 2018, *ShuffleNet: An Extremely Efficient Convolutional Neural Network for Mobile Devices*。

## 4. ShuffleNet V2（实战指导原则下的单元）

- **变体**：unit 改为 **channel split**（输入通道一分为二：一支 shortcut、一支走 3 层 conv）→ 末尾 **concat**（不是 add）→ channel shuffle。下采样 unit 不 split、走双路 conv。
- **结构前提（实战四原则 G1–G4，是 V2 的核心贡献，必须整体满足）**：
  - **G1**：两分支要 **等通道宽度**（不相等则 element-wise add 时 MAC 浪费）。
  - **G2**：**group 数不能过大**（过大 MAC/FLOPs 比值恶化，单位 FLOPs 实际时延变高）。
  - **G3**：**减少碎片化**（fragmentation，过多 1×1 小分支会降 GPU 并行度）。
  - **G4**：**element-wise ops 不可忽略**（add/ReLU/short 都是 MAC 成本）。
- **时延-精度权衡**：直接在实测时延（不是 FLOPs）上做设计——同 FLOPs 下实测快于 V1、MobileNet V2。concat 替 add 避免了 add 的 MAC 重读、channel split 是零成本划分。
- **来源**：Ma et al. 2018, *ShuffleNet V2: Practical Guidelines for Efficient CNN Architecture Design*。

## 5. GhostNet（cheap op 生成"幽灵"特征）

- **变体**：Ghost module 替代标准 1×1/3×3 conv——一次 1×1 出 `m` 个 intrinsic 特征，一次 cheap linear op（3×3 DW）对每个 intrinsic 生成 `s-1` 个 ghost 特征，concat 成 `m·s` 个输出。
- **结构前提**：
  - cheap op **必须是线性、稀疏参数**（DW 3×3 是默认选择）；不可换回标准 conv（会抹掉整个节省）。
  - Ghost module 替换的是 **bottleneck 里的中间 3×3 conv**，外层 1×1 升降维结构保留。
  - Ghost bottleneck（G-bneck）= PW expand → Ghost module (DW cheap) → PW project，配 SE 可选。
- **时延-精度权衡**：同精度 FLOPs 减半左右；ImageNet 上 GhostNet-1.0× 比 MobileNet V3 大版同精度更少 FLOPs。代价：cheap op 仍是 DW，在桌面 GPU 上效率短板延续。
- **来源**：Han et al. 2020, *GhostNet: More Features from Cheap Operations*。

## 6. RepVGG（多分支训练 → 单路推理重参数化）

- **变体**：训练时 block = `3×3 conv + BN ∥ 1×1 conv + BN ∥ identity BN`（三路相加）；推理时把三路数学等价折叠成 **单路 3×3 conv**。
- **结构前提**：
  - 三路都 **以 BN 结尾**（折叠基于 conv+BN 的仿射可合并性，identity 也要表达成 1×1 conv+BN）。
  - 折叠只在 **推理态** 做，训练态保持多分支以保留精度（多分支训练等价于更深的隐式 ensemble）。
  - 通道数在折叠后不变；3×3 conv 要 padding=1 保分辨率。
- **时延-精度权衡**：训练态有 ResNet 风格的高精度（多分支梯度好），推理态退化为纯 plain CNN 极高并行度、零 fragment、零 element-wise add（G3/G4 满足到极致）。GPU 上推理时延显著低于同精度 ResNet。
- **来源**：Ding et al. 2021, *RepVGG: Making VGG-style ConvNets Great Again*。

## 7. EfficientNet（compound scaling）

- **变体**：不是 block 创新，是 **缩放律**——给定 baseline（EfficientNet-B0 = MobileNet V2 + SE），用 `depth = α^φ, width = β^φ, resolution = γ^φ`（约束 `αβ²γ² ≈ 2`）联合缩放得 B1–B7。
- **结构前提**：
  - **必须三者联合**缩放（depth × width × resolution）——只缩 width 会饱和、只缩 depth 会让高分辨率下精度不跟。
  - baseline block 是 **MBConv**（MobileNet V2 的 inverted residual + SE）。
  - `αβ²γ² ≈ 2` 的约束来自神经架构搜索（NAS）在小 φ 下的最优解。
- **时延-精度权衡**：同 FLOPs 下精度比只缩单一维度的网络高 ~1–2% ImageNet top-1。代价：缩放律是经验拟合，**B7 之后外推（更大 φ）会出现训练不稳定与饱和**。
- **来源**：Tan & Le 2019, *EfficientNet: Rethinking Model Scaling for Convolutional Neural Networks*。

## 8. RegNet（设计空间正则化）

- **变体**：不是单一架构，是 **设计空间约束**——在 stage 化结构里，每个 stage 的 block 数 `d`、宽度 `w`、bottleneck 比 `b` 都满足线性函数：`w_j = w_0 + w_a·j`、配 group conv `g`。所有 stage 同构。
- **结构前提**：
  - **stage 化**（每个 stage 内分辨率不变、跨 stage 降 2×）。
  - **宽度沿 stage 线性增长**（实证"linear parameterization"占优）。
  - 所有 stage 同构（block 结构一致），配残差。
- **时延-精度权衡**：设计空间正则后，随机采样的网络方差小、Pareto 前沿更优；regnet-x 与 regnet-y 系列在低 FLOPs 区间占优。
- **来源**：Radosavovic et al. 2020, *Designing Network Design Spaces*。

---

## 通用前提（跨变体）

- **fragmentation 是 GPU 杀手**（ShuffleNet V2 G3）：每 block 内分支数越多，GPU 调度越差。多分支只在能 RepVGG 式折叠成单路时才推荐。
- **element-wise ops 不是免费的**（G4）：ReLU、add、shortcut 都重读 feature map；在 latency-critical 场景，**concat 优于 add**（concat 把读一次复用为输出的一部分）。
- **DW / group 在桌面 GPU 上"省 FLOPs 不省时延"**——在 edge/CPU 上收益更大；workflow 的 latency provider 测出来才算数（见草稿 §4 时延先行）。
- **NAS-Bench-201 / HW-NAS-Bench 的 5 个算子** `{nor_conv_3x3, nor_conv_1x1, skip_connect, avg_pool_3x3, none}` 是搜索空间的真实边界（来源 02-llmatic / 05-llm-nas / 09-design-principle-transfer 均用此搜索空间）——即结构变更的合法动作受限于此集。

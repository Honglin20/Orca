# CNN 族降时延结构 move（latency_moves）← 本族核心

> 用途：Hypothesizer 提降时延假设时的**主菜单**。每个 move 都落到「改哪种 conv / block 怎么重排 / 分辨率怎么动」，不是调超参。workflow 第一目标是降时延（保精度），故本文件比同族其他文件更详尽。
>
> 统一格式：**名称 → 结构改动 → 降时延机理 → 精度风险与缓解 → 适用/不适用 → 来源**。「机理」严格区分 FLOPs / MAC / 分辨率 / kernel launch 四条路径——不要笼统说"省算力"。每个 move 的合法性最终以 workflow latency provider 实测为准（见草稿 §4 时延先行）。
>
> 引用：标 `[论文]` 的来自 `references/nas/summaries/` 里 4 篇 CNN 相关论文；标 `[SOTA]` 的来自业界公开论文（MobileNet / ShuffleNet / GhostNet / RepVGG / EfficientNet / SENet / RegNet）；标 `[bench]` 的来自 NAS-Bench-201 / HW-NAS-Bench 的事实。

---

## A. 卷积类型替换类（替换 conv 本身）

### Move A1：标准 3×3 conv → depthwise-separable（DW 3×3 + PW 1×1）

- **结构改动**：`nn.Conv2d(C_in, C_out, 3, ...) `→ `nn.Conv2d(C_in, C_in, 3, groups=C_in)` 紧跟 `nn.Conv2d(C_in, C_out, 1)`。每层原 conv 都可单独替换；最好从 stage 中段（通道数大）开始。
- **降时延机理**：
  - FLOPs：从 `C_in·C_out·9·H·W` 降到 `C_in·9·H·W + C_in·C_out·H·W`，约省 `1/C_out + 1/9` 比例（典型 8–9×）。
  - MAC：PW 1×1 访存极友好（cuDNN 高度优化）；DW 访存模式不友好、每通道独立——**GPU 上 DW 是真实瓶颈**，要靠 latency provider 实测确认收益。
  - kernel launch：多一次（DW + PW 两个 kernel）。
- **精度风险 + 缓解**：DW 无通道混合，单用精度大幅掉。缓解：**必须**接 PW 1×1；PW 后用 ReLU6；DW 不要放在极窄通道（< 24）上。
- **适用 / 不适用**：适用——中后段通道数大的 stage；edge CPU/TPU 收益大。不适用——桌面 GPU 极小模型（DW launch overhead 占主导）；最后分类前的一两层（保精度）。
- **来源**：`[SOTA]` MobileNet V1 (Howard 2017)。

### Move A2：标准 conv → group conv（+ channel shuffle 保信息流）

- **结构改动**：`nn.Conv2d(C, C, 1)` → `nn.Conv2d(C, C, 1, groups=g)`（g=4/8）；后续 1×1 conv **前**插入 `channel_shuffle(x, g)`。注意 3×3 conv 一般直接用 DW（A1），不用 group；group 主要用在 1×1 PW。
- **降时延机理**：
  - FLOPs：÷g（1×1 group conv）。
  - MAC：÷g 但 MAC/FLOPs 比值随 g 上升而恶化（ShuffleNet V2 G2）——g 越大单位 FLOPs 实际时延越高，**g 不宜 >8**。
  - shuffle 本身 0 FLOPs、一次 memory permute。
- **精度风险 + 缓解**：group 不 shuffle → 组间永久不交互 → 精度随深度退化。缓解：**每个 group conv 后必须 shuffle**；用 `g=2/4` 而非 8/16 保 MAC 友好。
- **适用 / 不适用**：适用——中通道数（48–256）的 1×1 PW 替换；edge 设备。不适用——极窄通道（DW 更划算）；极宽通道（标准 conv 反而 GPU 友好）；桌面 GPU 上 g>8 的 group conv（实测可能更慢）。
- **来源**：`[SOTA]` ShuffleNet V1 (Zhang 2018)；ShuffleNet V2 G2 (Ma 2018)。

### Move A3：block 中间 conv → Ghost module（cheap op 生成特征）

- **结构改动**：bottleneck 中间的 3×3 conv → Ghost module：先 `Conv2d(C_exp, m=C_exp//s, 1)`（s≈2）生成 intrinsic，再对每条 intrinsic 做 `Conv2d(m, m, 3, groups=m)`（cheap DW 3×3）生成 ghost，concat 成 `m·s`。原 block 外层 PW expand/project 不变。
- **降时延机理**：
  - FLOPs：约 ÷s（s≈2，减半）。intrinsic 路径少算一半的 1×1，ghost 路径只是廉价 DW。
  - MAC：中间 tensor 通道减半，激活内存 ÷s。
  - kernel launch：+1（ghost 路径多一个 cheap kernel）。
- **精度风险 + 缓解**：cheap op 表达力弱，承担全部空间聚合会掉精度。缓解：**只在 bottleneck 中段替换**（外层 PW 不动）；cheap op 用 3×3 DW 不要用更小；可加 SE 补通道注意力。
- **适用 / 不适用**：适用——已 bottleneck 化的 block、激活内存紧张场景。不适用——plain CNN（无 bottleneck）、block 入口（空间细节尚未提取）。
- **来源**：`[SOTA]` GhostNet (Han 2020)。

---

## B. Block 结构重排类

### Move B1：block bottleneck 化（1×1 降 → 3×3 → 1×1 升）

- **结构改动**：把 stage 里 `Conv3×3(C, C) → Conv3×3(C, C)` 双层标准 conv 改成 `Conv1×1(C, C/r) → Conv3×3(C/r, C/r) → Conv1×1(C/r, C)`（r=4），配残差。
- **降时延机理**：
  - FLOPs：3×3 在 C/r 通道做，省 ~r² 倍的 spatial conv FLOPs；1×1 在 C 通道做但无 k² 项、单位 FLOPs 极廉价。
  - MAC：中间 tensor 通道 ÷r，激活内存省。
  - kernel launch：+2（多两个 1×1）。
- **精度风险 + 缓解**：r 太大精度掉（信息瓶颈）。缓解：r=4 是 ResNet-50 验证过的甜点；窄通道用 r=2。
- **适用 / 不适用**：适用——中后段宽通道 stage；任何含 3×3 + 3×3 的 plain block。不适用——已经极窄的通道（再压成瓶颈会失精度）；分类头前的最后 conv。
- **来源**：`[SOTA]` ResNet-50 bottleneck (He 2016)。

### Move B2：标准 residual block → inverted residual（MobileNet V2）

- **结构改动**：`Conv1×1(C, C/r) → Conv3×3(C/r, C/r) → Conv1×1(C/r, C)` + ReLU 各处 → 改成 `Conv1×1(C, tC) + ReLU6 → DW3×3(tC, tC) + ReLU6 → Conv1×1(tC, C)（linear，无 ReLU）`，t=6；残差发生在 **窄的输入输出两端**（仅当 stride=1 且 C 进 = C 出）。
- **降时延机理**：
  - FLOPs：DW 替 3×3 在宽通道、1×1 在窄通道做——总 FLOPs 远低于标准 bottleneck。
  - MAC：激活内存由输入输出维度决定（窄），中间宽通道只在 DW 内部存在；内存友好。
  - kernel launch：+1（多一个 expand PW）。
- **精度风险 + 缓解**：(a) 投影后 ReLU 会塌信息 → **必须 linear projection**；(b) DW 在窄通道精度掉 → t=6 让 DW 在宽通道工作；(c) t 太大 FLOPs 反弹 → t∈[3, 6]。
- **适用 / 不适用**：适用——通用高效 block 替换；移动端首选。不适用——精度敏感的最后几层（linear projection 表达力弱）；stride=2 的下采样 block 不加残差。
- **来源**：`[SOTA]` MobileNet V2 (Sandler 2018)。

### Move B3：RepVGG 式训练-推理结构重排（多分支训练 → 单路推理）

- **结构改动**：训练态 block 改为三路并联：`y = Conv3×3+BN(x) + Conv1×1+BN(x) + IdentityBN(x)`（identity 仅当 C_in==C_out 且 stride=1）；推理态（部署前）用重参数化等价折叠：3×3 + BN → 单个 3×3 conv；1×1 + BN → 补 0 成 3×3 conv；identity BN → 补 0 成 3×3 conv；三者权重相加得单路 3×3 conv `y = Conv3×3(x)`。
- **降时延机理**：
  - FLOPs：推理态 ≈ plain 3×3 conv（无 fragment、无 add）；**显著低于训练态等价 FLOPs**。
  - MAC：推理态零 element-wise add、零多分支融合 → G3/G4 双双满足到极致。
  - kernel launch：从 3 路若干 kernel 降到 1 个；GPU 极友好。
- **精度风险 + 缓解**：(a) 训练态需多分支才能保精度——**禁止**训练态直接用 plain（精度掉）；(b) 折叠等价性要求所有分支以 BN 结尾、3×3 conv padding=1。缓解：训练态固定三路、训练完调一次性折叠函数。
- **适用 / 不适用**：适用——纯推理 latency-critical 场景、GPU 部署、不需要 backward。不适用——训练时态、动态结构、需要 backward 的在线学习。
- **来源**：`[SOTA]` RepVGG (Ding 2021)。

---

## C. 分辨率与下采样类

### Move C1：早下采样（early downsampling，stem 一次性降到 1/4）

- **结构改动**：把网络早期（stem）从 `Conv 3×3 stride=1 → Conv 3×3 stride=1 → ...` 后才 pool，改为 `stem: Conv 3×3 stride=2 (+ max_pool stride=2)` 一次性把分辨率降到 1/4，再做后续 stage。
- **降时延机理**：
  - FLOPs：后续所有 conv 的 H·W ÷4，**FLOPs 几乎随分辨率线性降**——这是省 FLOPs 的最大杠杆。
  - MAC：激活 tensor 也 ÷4。
  - kernel launch：不变。
- **精度风险 + 缓解**：早下采样会丢小目标 / 精细空间信息。缓解：stem 用 3 层 3×3 替单层 7×7（VGG-style，精度更稳）；只在分辨率损失 ≤4× 时做；检测/分割任务把 backbone stem 下采样降回 1/2。
- **适用 / 不适用**：适用——分类、高分辨率输入。不适用——检测/分割（小目标敏感）、低分辨率输入（已经 32×32 的 CIFAR 再降没东西了）。
- **来源**：`[SOTA]` ResNet stem (He 2016)；`[论文]` EvoPrompting 的 MNIST-1D 搜索倾向 "smaller strides, less padding"（即反向支持：减少 stride 但对应要补偿别处）——但分类任务的早下采样原则是业界共识。

### Move C2：下采样集中在 stage 边界（不要每 block 都 stride）

- **结构改动**：扫一遍 backbone，若每 block 都 `stride=2`（连续下采样 4–5 次），改为 **每 stage 第一个 block stride=2、stage 内其余 stride=1**；stage 数 = 下采样次数。
- **降时延机理**：
  - FLOPs：避免在宽通道上 stride（宽通道上的 stride conv FLOPs 仍按大分辨率算）；下采样集中发生在 stage 入口（窄通道之前）。
  - MAC：低分辨率特征在宽通道 stage 里维持，激活内存峰值 ÷。
  - kernel launch：不变。
- **精度风险 + 缓解**：极少掉精度（这是几乎所有 SOTA 共识）。缓解：无。
- **适用 / 不适用**：适用——几乎所有 stage 化 CNN。不适用——plain CNN（无 stage 概念）。
- **来源**：`[SOTA]` ResNet/VGG/EfficientNet 共识；`[论文]` LAPT (09) 的 NAS201/Trans101/DARTs 实验默认 stage 化。

### Move C3：分辨率本身的 compound 缩放（适度降输入分辨率）

- **结构改动**：把 `dummy_input.shape` 从 `[1,3,224,224]` 降到 `[1,3,192,192]` 或 `[1,3,160,160]`；首层 conv 适配 kernel/stride。
- **降时延机理**：
  - FLOPs：分辨率平方下降（224²→192² 减约 27%）。
  - MAC：激活内存同比例下降。
- **精度风险 + 缓解**：掉 1–3% top-1（每降 32 像素约掉 1–2%）。缓解：与宽度联合调（EfficientNet compound scaling 的核心），降分辨率同时提通道；只在中精度要求场景用。
- **适用 / 不适用**：适用——中等精度要求、内存紧张。不适用——高精度任务、小目标检测。
- **来源**：`[SOTA]` EfficientNet compound scaling (Tan & Le 2019)。

---

## D. 宽度与深度类

### Move D1：剪整 stage + 残差保精度

- **结构改动**：从 backbone 末端（最深层）开始 **整 stage 删除**（如 ResNet-50 → ResNet-34 风格：删 stage4 的若干 block）；保留每个被剪 stage 入口的残差结构（即剪 stage 内 block，不剪 stage 边界的 downsample）。
- **降时延机理**：
  - FLOPs：减层 ÷ 比例；深层 stage 通道宽，剪一层省得多。
  - MAC：激活内存同降。
  - kernel launch：减。
- **精度风险 + 缓解**：深层负责高级语义，剪太多掉精度。缓解：(a) 优先剪中段（语义未完全形成）而非最后 stage；(b) 每剪一层配 `Identity` shortcut 兜梯度流；(c) 剪完 fine-tune 几 epoch。
- **适用 / 不适用**：适用——过参数化 backbone（FLOPs 远超任务需要）。不适用——本来就浅的网络；分类头前最后 stage。
- **来源**：`[SOTA]` 结构化剪枝共识；`[论文]` LAPT 的 REA 在精炼搜索空间内频繁出现"删层"路径。

### Move D2：宽度重新分配（窄底 + 中段加宽）

- **结构改动**：把均匀的通道 profile `C, 2C, 4C, 8C, 8C` 调成"非均匀"——前几个 stage 通道数略减（如 `0.75C, 1.5C, 4C, 8C, 8C`），让前段省的 FLOPs 转给中段。RegNet 的 linear parameterization 是这套的精确版。
- **降时延机理**：
  - FLOPs：前段 stage 在高分辨率上跑，每点通道数减 25% 立刻省 FLOPs；中段低分辨率上提通道开销小。
  - MAC：前段激活 tensor 缩小。
- **精度风险 + 缓解**：前段通道太窄、空间细节提取不足。缓解：前段通道不要低于 32；用 1×1 PW 提升中段宽度弥补。
- **适用 / 不适用**：适用——高分辨率输入、stage 化 backbone。不适用——plain CNN。
- **来源**：`[SOTA]` RegNet linear parameterization (Radosavovic 2020)；LAPT (09) 归纳原则之一。

### Move D3：通道宽度上的 channel shuffle（仅当用了 group conv）

- **结构改动**：**仅在已经使用 group conv 的网络里**——确保每两个相邻 group conv 之间都有 channel shuffle；如果原来 group 划分固定不变，把它改成相邻层 g 不同（如 g1=4, g2=8），shuffle 让边界完全错开。
- **降时延机理**：
  - FLOPs：0 增加；间接降时延——shuffle 让 group conv 能用更大的 g（更省 FLOPs）而不掉精度，从而允许进一步减宽。
  - MAC：shuffle 本身一次 memory permute。
- **精度风险 + 缓解**：缺 shuffle 直接掉几个点（信息不流通）。缓解：每 group conv 后必 shuffle。
- **适用 / 不适用**：适用——已含 group conv 的网络。不适用——纯 DW 或纯标准 conv 网络（无 group 边界可错开）。
- **来源**：`[SOTA]` ShuffleNet V1 (Zhang 2018)。

---

## E. 通道注意力类（低开销提精度）

### Move E1：每个 stage 末尾加 SE（不在每 block 加）

- **结构改动**：在每个 **stage 最后一个 block** 的出口前插 SE：`GAP → FC(C→C/r, r=16) → ReLU → FC(C/r→C) → sigmoid → x·w`。**不要**每 block 都加。
- **降时延机理**：
  - FLOPs：SE 本身 FLOPs ~2%（C·C/r），很小。
  - MAC：每次 forward 多 4 个 kernel launch（pool + 2 FC + multiply）——只在 stage 末尾加意味着全网络只 +4×(stage 数) 次 launch，可接受。
  - 收益本质：低时延代价换精度提升（不是直接降时延，是"加少量时延换大量精度"，让别处减时延的余量变大）。
- **精度风险 + 缓解**：几乎不掉精度（+1–2%）。缓解：r=16 是甜点；用 h-swish（MobileNet V3）替 sigmoid 略好。
- **适用 / 不适用**：适用——已有 backbone 想小幅提精度。不适用——每 block 都塞 SE（launch 累积，G4 违反）；超低时延 edge（4 个 launch 也嫌多）。
- **来源**：`[SOTA]` SENet (Hu 2018)；MobileNet V3 (Howard 2019)。

### Move E2：CBAM 替代 SE（仅当空间注意力对任务关键）

- **结构改动**：SE → CBAM = channel attention（max+avg 双路 FC）+ spatial attention（max+avg 沿通道 → 7×7 conv → sigmoid）。
- **降时延机理**：**不是降时延 move**——CBAM 比 SE 时延更高（多一次 7×7 conv + 两次沿通道 pool）；列在这里是为了提醒"它名义上是注意力提精度但时延代价更大"。
- **精度风险 + 缓解**：+0.5–1% over SE（任务相关）；但 latency 增。
- **适用 / 不适用**：适用——检测/分割（空间注意力有用）。不适用——latency-critical 分类（SE 更划算，或干脆不加）。
- **来源**：`[SOTA]` CBAM (Woo 2018)。

---

## F. 推理友好类（消除 element-wise / fragment）

### Move F1：element-wise add → concat（concat 重组为输出）

- **结构改动**：多分支融合处 `y = branch1(x) + branch2(x)` 改成 `y = concat([branch1(x), branch2(x)], dim=1)`，下一层 conv 的输入通道翻倍（或用 1×1 PW 压回）。
- **降时延机理**：
  - MAC：add 要重读两个 feature map 再写一次；concat 把"读"复用为"输出的写入"——**MAC 省**（ShuffleNet V2 G4 的直接应用）。
  - kernel launch：不变。
- **精度风险 + 缓解**：concat 后通道翻倍，下一层 FLOPs 上升。缓解：紧接 1×1 PW 压回原通道；ShuffleNet V2 unit 即此 pattern。
- **适用 / 不适用**：适用——多分支融合处、GPU 部署。不适用——分支通道差异大（concat 后通道失衡）；edge 极限内存场景（concat 临时 tensor 大）。
- **来源**：`[SOTA]` ShuffleNet V2 G4 (Ma 2018)。

### Move F2：等通道宽度（两分支融合前 C 必须相等）

- **结构改动**：扫所有 `add` / `concat` 融合点，若两分支输出通道数不等，**把窄的分支 PW 升维到相等**（不是用 1×1 把宽的压下来——会让宽分支信息损失）。
- **降时延机理**：
  - MAC：G1（ShuffleNet V2）——通道相等时 add/concat 的访存 / FLOPs 比最优；不等则 MAC 浪费。
- **精度风险 + 缓解**：升维 PW 略增 FLOPs，但 MAC 改善补偿时延。缓解：升维用 1×1 无 ReLU 线性投影。
- **适用 / 不适用**：适用——任何多分支网络。不适用——单路 plain CNN。
- **来源**：`[SOTA]` ShuffleNet V2 G1 (Ma 2018)。

### Move F3：BN 折叠进前 conv（推理态）

- **结构改动**：推理态把 `y = BN(Conv(x))` 用仿射合并等价写成 `y = Conv'(x)`——BN 的 `γ, β, μ, σ` 合进 Conv 的 `W, b`。代码层面调一次 `fuse_bn` 函数。
- **降时延机理**：
  - FLOPs：推理态 BN 是 channel-wise scale+shift，折叠后归零。
  - MAC：避免 BN 的额外一遍 feature map 读写。
  - kernel launch：每个 BN 节点少一次 launch。
- **精度风险 + 缓解**：**零精度损失**（数学等价）。缓解：无。
- **适用 / 不适用**：适用——所有推理态含 BN 的网络。不适用——训练态（BN 统计要保留）；用 GroupNorm/LayerNorm 的（不能折叠，只有 BN 能折叠）。
- **来源**：`[SOTA]` 通用部署优化；RepVGG 重参数化的基础。

### Move F4：减少 fragment（多个小 1×1 → 单个大 conv）

- **结构改动**：扫 stage 内 branch 数，若 `y = sum_i Conv1×1_i(x)` 这种多并联 1×1 串接，看能否合并——若各分支结构对称（仅通道分组不同），合成一个 group conv 或一个 3×3 conv（RepVGG 风格）。
- **降时延机理**：
  - kernel launch：每个小分支 = 一组 kernel launch + 一次融合 add；合并后单一 launch。G3 直接应用。
  - MAC：少多次中间 feature map。
- **精度风险 + 缓解**：合并可能改变表达（group conv 与并联 1×1 不完全等价）。缓解：训练态保持并联、推理态合并（RepVGG）；或先 fine-tune 补回。
- **适用 / 不适用**：适用——并联 1×1 多分支。不适用——并联分支结构差异大（无法等价合并）。
- **来源**：`[SOTA]` ShuffleNet V2 G3；RepVGG (Ding 2021)。

---

## G. 反向 move（替换高时延算子）

### Move G1：deformable conv → dilated conv 或标准 3×3

- **结构改动**：把 backbone 里 `DeformConv2d` 替换成 `Conv2d(..., dilation=r)`（r=2 或 4，扩感受野）或标准 3×3。
- **降时延机理**：
  - FLOPs：deformable 多一个 offset 预测分支（额外 conv）+ 不规则双线性采样（稀疏访存，GPU 极慢）；dilated / 标准 conv 都是密集访存、单 conv kernel。
  - kernel launch：从多分支（offset + sample + conv）降到单 kernel。
- **精度风险 + 缓解**：deformable 对几何变化、非规则目标的精度优势会丢。缓解：(a) 用 dilated conv 保留扩感受野；(b) 把 deformable 只保留在最后一两层（限制高时延算子的总数）。
- **适用 / 不适用**：适用——latency-critical 部署、几何变化不关键的任务。不适用——检测/分割里小目标 / 旋转目标关键的场景（deformable 是精度的核心来源）。
- **来源**：`[SOTA]` Deformable Conv (Dai 2017 / Zhu 2019) 已知的高时延事实；`[论文]` HW-NAS-Bench 的 5 个算子集 `{nor_conv_3x3, nor_conv_1x1, skip_connect, avg_pool_3x3, none}` 不含 deformable——搜索空间本身就排除了它（05-llm-nas）。

### Move G2：avg_pool_3×3 → skip_connect（当 pool 不带来精度增益）

- **结构改动**：在 cell / block 内若某条边是 `avg_pool_3×3` 且消融显示去掉不掉精度，替换为 `skip_connect`（identity）。
- **降时延机理**：
  - kernel launch / MAC：avg_pool 是固定 kernel 的访存；skip 是零计算零参数的 identity（ShuffleNet V2 G4：element-wise / 简单 op 也不是免费的，但 identity 比 pool 还轻）。
- **精度风险 + 缓解**：`[论文]` 05-llm-nas 的 Co-evolve KB 明确归纳出 **"`avg_pool_3x3` 时延长且对精度提升有限"** 这条规则（论文报告 LLM 在闭环中观察到并写入 KB），所以这一替换是 LLM-NAS 验证过的合法降时延 move。缓解：实测消融，若掉精度超阈值则保留 pool。
- **适用 / 不适用**：适用——搜索空间含 pool 的 NAS（NAS-Bench-201 / HW-NAS-Bench）。不适用——stem 的下采样 pool（功能性，不能删）；明确需要平移不变性的位置。
- **来源**：`[论文]` 05-llm-nas Co-evolve KB 规则；`[bench]` NAS-Bench-201 的 5 算子集。

### Move G3：`none` 算子替换冗余边（cell-based NAS 专用）

- **结构改动**：在 NAS-Bench-201 / HW-NAS-Bench 的 cell 表示里，把某条 input-output 边的算子设为 `none`（即 zeroize，断开）。
- **降时延机理**：
  - FLOPs / MAC：直接断一条计算路径，省下该边算子的全部 FLOPs 与访存。
- **精度风险 + 缓解**：断错关键边掉精度。缓解：选 LLM 在 KB 里标记为"低贡献"的边；Pareto archive 里观察哪条边被频繁 zeroize。
- **适用 / 不适用**：适用——cell-based NAS 的边剪枝。不适用——plain CNN（无 cell / edge 概念）。
- **来源**：`[论文]` 02-llmatic、05-llm-nas、09-design-principle-transfer 的搜索空间均含 `none` 算子；`[bench]` NAS-Bench-201。

---

## H. 组合套餐（多种 move 一起用时的推荐序列）

> 单 move 收益有限，组合使用时要按"FLOPs → MAC → launch"优先级序列：
> 1. **先 C1 早下采样**（FLOPs 杠杆最大，前提是任务允许）；
> 2. **再 A1/A2/A3 卷积替换**（针对宽通道 stage）；
> 3. **再 B1/B2 block 重排**（在已是窄通道的 stage 内优化）；
> 4. **F1/F3/F4 推理友好**（终态优化，零精度损失）；
> 5. **D1 剪 stage** 当还差最后一截时延；
> 6. **E1 SE** 当精度不够时（而非时延够时）。

**反模式**（容易踩坑的组合）：
- A2 group conv + 大 g + 不 shuffle → 严重掉精度（见 `failures.md`）。
- C1 早下采样 + 检测任务 → 小目标 recall 崩。
- F1 concat 到处用 + 不接 PW 压回 → 通道爆炸、FLOPs 反弹。
- B3 RepVGG + 训练态直接 plain → 精度掉几个点。

---

## move 决策树（速查）

```
当前瓶颈是什么？
├─ FLOPs 太高（latency provider 测的时延与 FLOPs 强相关，多在 edge CPU）
│   ├─ 任务是分类/输入分辨率大？ → C1 早下采样
│   ├─ 中后段宽通道 stage？     → A1 DW-SEP 或 A2 group+shuffle
│   ├─ 已 bottleneck 化？        → A3 Ghost module
│   └─ 仍高？                   → D1 剪 stage
├─ MAC / 访存瓶颈（GPU 上 kernel 时间 dominate）
│   ├─ 多分支融合？             → F1 add→concat、F2 等宽
│   ├─ 碎片化？                 → F4 减 fragment、B3 RepVGG
│   └─ BN 多？                  → F3 BN 折叠
├─ kernel launch 太多（小模型、低 batch）
│   ├─ DW / group 过多？        → 用 A2 group（更少分支）替 A1 DW 串联
│   ├─ 多并联分支？             → B3 RepVGG 折叠 / F4 合并
│   └─ SE 每 block 都加？       → E1 改到 stage 末尾
└─ 精度不够（不是降时延 move，是给降时延腾余量）
    └─ E1 SE 末尾加、C3 compound 降分辨率+提宽
```

---

## 与 references 论文的直接映射

| 论文 | 对 latency_moves 的贡献 |
|---|---|
| **01-evoprompting** | MNIST-1D 搜索倾向"narrower + deeper, smaller strides, less padding, no dense layers"——印证 C1（反向：小 stride 保精度）、D2（窄底）、去 dense 头（用 GAP 替 FC，见 common）。`[论文]` |
| **02-llmatic** | NAS-Bench-201 的 5 算子集 `{1×1, 3×3, skip, avg_pool, none}` 是 A/B/C/G 类 move 的合法边界；behavior descriptor 用 `(width/depth, FLOPS)` 作多样性轴。`[论文]` |
| **05-llm-nas** | G2（avg_pool→skip）的 KB 规则、G3（none 边）、`nor_conv_3x3` 计数作为 niche 划分（说明 3×3 conv 数量主导复杂度）。`[论文]` |
| **09-design-principle-transfer** | LAPT 自动归纳"早层用 conv 提特征、中层用 skip 保梯度"——对应 D1（剪 stage 时保残差）与 block 入口选择。`[论文]` |

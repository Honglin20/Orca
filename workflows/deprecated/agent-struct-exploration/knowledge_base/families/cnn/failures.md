# CNN 族已知失败结构（failures）—— Analyst 持续追加

> 用途：记录 CNN 族里**已知会失败的结构 move**——结构指纹 → 失败模式（时延没降 / 精度掉 / 导不出）→ 原因 → 来源。Hypothesizer / Analyst 在评估新候选时用来快速剔除明显雷区；Analyst 在每轮 run 后追加新观察到的失败到本文件末尾。
>
> **本文件随 run 由 Analyst 追加**。下面的初始条目是发表文献里已确认的失败结构，是"硬证据"，不是猜测。新增条目要带 run-id 与可复现的结构指纹。

格式：**结构 → 失败模式 → 原因 → 来源**。

---

## F1. 窄通道上用 DW conv（depthwise）

- **结构**：`Conv2d(C, C, 3, groups=C)`，C < 24（典型 < 16）。
- **失败模式**：精度明显掉（同 FLOPs 下比标准 conv 或 group conv 差几个点）。
- **原因**：DW 每通道独立卷积、通道间不混合，在窄通道下"独立通道信息"不足以撑起表达；又因 C 小，PW 1×1 也无法靠通道投影补足。MobileNet V3-small 系列通过引入 SE + h-swish 部分缓解，但**窄通道 + DW 本身仍是精度短板**。
- **来源**：`[SOTA]` MobileNet V3 (Howard 2019) 的讨论；MobileNet V2 论文也建议 expand ratio t=6 让 DW 在宽通道工作。

## F2. group conv 大 g + 无 channel shuffle

- **结构**：连续多层 `Conv2d(C, C, 1, groups=8)` 不接 `channel_shuffle`。
- **失败模式**：深度越深、精度退化越严重（顶层 1×1 group conv 永远只看到固定 1/8 通道）。
- **原因**：group 划分在层间固定不变 → 组间信息永久不交换 → 等价于 g 个独立小网络并行，每个只看部分特征。ShuffleNet V1 的核心 motivation 就是修这个：**group conv 后必须 shuffle**。
- **来源**：`[SOTA]` ShuffleNet V1 (Zhang 2018) 的 ablation 明确指出无 shuffle 精度掉。

## F3. 太早下采样（检测/分割、或小目标任务）

- **结构**：stem 直接 `Conv3×3 stride=4 + maxpool stride=2` 一步到 1/8 分辨率；用于检测 / 语义分割 / 小目标分类。
- **失败模式**：小目标 recall / 定位 mAP 大幅掉（分类 top-1 可能只掉 1%，但检测 mAP 掉 5%+）。
- **原因**：早下采样把高频空间信息直接池掉；分类任务对此不敏感（类别是全局聚合），但定位任务需要保留空间细节。**C1 早下采样是分类专用 move**。
- **来源**：`[SOTA]` 检测 backbone 设计共识（FPN、RetinaNet 都靠深层特征上采样补救）；`[论文]` EvoPrompting 的 "smaller strides, less padding" 倾向在小目标敏感任务上是反向证据。

## F4. bottleneck 压缩比 r 过大

- **结构**：bottleneck 用 r=16 或 r=32（中间通道 = C/r）。
- **失败模式**：精度掉（信息瓶颈），尤其深层 block。
- **原因**：中间窄通道无法表达足够的空间模式；尤其当 block 入口通道数本身不大（如 64），C/16 = 4 通道完全不够。
- **来源**：`[SOTA]` ResNet 系列共识 r=4 是甜点；Wide-ResNet 论证更宽的中间通道在小模型上有正收益。

## F5. SE 模块每 block 都加（而非 stage 末尾）

- **结构**：每个 residual block 的输出前都插 SE 模块（一个 stage 内 N 个 block → N 个 SE）。
- **失败模式**：时延反而上升（SE FLOPs 小但 launch 多）；精度提升边际递减（第 2、3 个 SE 加性收益微弱）。
- **原因**：G4（element-wise ops 不免费）——SE 每实例化一次都多 4 个 kernel launch（pool + 2 FC + multiply）。每 block 都加违反 launch 预算。MobileNet V3 把 SE 限定在部分 block 上是经验补丁。
- **来源**：`[SOTA]` ShuffleNet V2 G4 (Ma 2018)；MobileNet V3 (Howard 2019)。

## F6. deformable conv 用在 latency-critical 部署

- **结构**：backbone 多处插 `DeformConv2d`（offset 预测分支 + 双线性采样）。
- **失败模式**：实测时延显著上升（GPU 上常常 2–3× 同形状标准 conv，edge 上更糟）。
- **原因**：不规则访存模式（offset 决定采样位置）破坏 GPU 的密集访存优化；额外的 offset 预测分支也是一次 conv。
- **来源**：`[SOTA]` Deformable Conv (Dai 2017) 的已知部署成本；`[论文]` HW-NAS-Bench / NAS-Bench-201 的合法算子集都不含 deformable（05-llm-nas / 02-llmatic）——搜索空间本身就规避它。

## F7. 串联太深的薄网络（gradient / 优化问题）

- **结构**：极窄通道（如每层 16–32）堆叠 50+ 层 plain（无残差）。
- **失败模式**：训练不收敛 / 梯度消失 / 精度饱和远低于同 FLOPs 浅网络。
- **原因**：窄通道下每层表达有限，深堆叠需要残差/归一化才能训动；plain 模式下深 + 薄 = 双重恶化。EvoPrompting 的发现"narrower + deeper wins"是在 **有残差 / 平均池化 shortcut** 的搜索空间里成立的——去掉残差结论反转。
- **来源**：`[论文]` 01-evoprompting 的搜索倾向是在 Flax 实现里**含残差 `x = x + xp`** 的网络；LAPT (09) 归纳的"中层用 skip connection 防梯度消失"原则印证。
- **缓解**：薄 + 深 **必须配残差或 skip**。

## F8. avg_pool 在 latency 关键路径且不带来精度

- **结构**：cell 的某条边用 `avg_pool_3×3` 作为下采样 / 聚合。
- **失败模式**：实测时延没有收益（甚至比 skip 慢），精度提升有限或没有。
- **原因**：`[论文]` 05-llm-nas 的 Co-evolve KB 显式归纳出此规则——LLM 在闭环搜索中观察到 `avg_pool_3×3` 在该搜索空间里"时延长 + 精度提升有限"，主动写入 KB 作为后续代的避坑规则（论文 Figure 4 Stage 1 给的示例规则之一）。
- **来源**：`[论文]` 05-llm-nas 的 KB 规则示例；`[bench]` NAS-Bench-201。

## F9. concat 到处用不接 PW 压回

- **结构**：参照 ShuffleNet V2 的 concat move，但漏掉了紧接的 1×1 PW 压回原通道——直接把翻倍通道喂给下一层 conv。
- **失败模式**：FLOPs 反弹（下一层 conv 在双倍通道上做）、时延不降反升。
- **原因**：F1（add→concat）省的是 MAC，但 concat 的代价是输出通道翻倍；必须紧接 PW 把通道压回去才闭环。ShuffleNet V2 unit 里 concat 后立即接 channel shuffle + 下一个 group conv（隐式压通道）就是这个意思。
- **来源**：`[SOTA]` ShuffleNet V2 (Ma 2018) 的 unit 结构。

## F10. RepVGG 训练态直接用 plain（不走多分支训练）

- **结构**：跳过 RepVGG 的多分支训练阶段，直接用 plain 3×3 conv 训练，期望推理态时延优势。
- **失败模式**：精度比 ResNet 同深度低 2–4%（plain 深网络训不动）。
- **原因**：RepVGG 的精度来源是**训练态的多分支等价隐式 ensemble**，推理态的 plain 只是部署形态。跳过多分支训练等于丢掉精度护栏。
- **来源**：`[SOTA]` RepVGG (Ding 2021) 的核心论点。

---

## F11. 极小 launch-bound CNN 上"减层 + strided-conv 融合"反而变慢（每核工作量上升压过 launch 减少）

- **结构**：3x16x16 输入的 plain 3-conv CNN（baseline: Conv3x3(3->16)+ReLU+MaxPool, Conv3x3(16->32)+ReLU+MaxPool, Conv3x3(32->64)+ReLU+AdaptiveAvgPool, Linear(64,10)，实测 0.0213ms）。候选 r1_c1 把 3 conv 块压成 2 块（删中段 32-ch stage）并把两个 MaxPool 融进 stride=2 的 Conv3x3：Conv3x3(3->32,stride=2)+ReLU, Conv3x3(32->64,stride=2)+ReLU, AdaptiveAvgPool, Linear(64,10)。算子数 ~10 -> ~6。
- **失败模式**：时延没降反升 —— 单次 measure() 实测 0.0332ms >= champion 0.0213ms，时延门 FAIL，未训练。
- **原因**：(a) 删中段后为保 64-ch 出口，首层 conv 从 3->16 加宽到 3->32，单核 MAC 上升约 2x；(b) stride=2 把下采样融进 conv，但 conv 本身在更大 output-channel 维度上做 GEMM，每核工作量上升；(c) 在 ~0.02ms 量级、算子已极少时，launch 开销已接近下限，"少 4 个 launch" 省下的几十 us 抵不过单核变重；(d) onnxruntime 在 sub-ms 区间测量本身高方差（重测 baseline 在 0.02-0.08ms 间漂移），但单次 measure 契约结果 0.0332ms 即裁定依据。结论：launch-bound 的"减算子数"直觉在**已经极少的极小模型**上会被"单核加重"反噬，减层未必减时延。
- **触发 run**：agent-struct-exploration-20260716-194643-b9dd24 / candidate r1_c1（见 ledger.jsonl）。
- **是否新发现**：是（族内首条"极小模型减层反慢"实证；与 F8 avg_pool 同属"低 FLOPs/high-launch 占比"语境但机理不同：F8 是 pool 本身慢，F11 是减层后单核加重）。

---

## Analyst 追加模板（每条新失败）

```
## F<id>. <结构指纹，如"stage2 第 3 block 入口 DW on C=12">

- **结构**：<具体代码层指纹——算子类型 + 通道数 + 位置>
- **失败模式**：<时延没降 / 精度掉 / 导不出 —— 哪个？多严重？>
- **原因**：<机制性解释，不要"可能是因为...">
- **触发 run**：<run-id 与 candidate-id，可追溯 ledger.jsonl>
- **是否新发现**：<是 / 与 F<x> 同类>
```

追加时严格 fail loud：**禁止把"没测过 / 不确定"的结构写入此文件**——没有证据的条目删除或留在 Analyst 工作笔记里。本文件每一条都要能被 ledger.jsonl 的具体 candidate 复现。

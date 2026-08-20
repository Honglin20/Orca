# 剪枝算法对比 & NAS×量化×剪枝联合压缩——调研报告

> 日期：2026-08-17 ｜ 分支：puzzle-universal
> 委托问题：(1) 各种剪枝算法详细对比；(2) NAS、量化、剪枝如何结合到一起。
> 方法：deep-research 多路检索（106 个子代理、24 个一手来源、120 条候选论断、25 条经 3 票对抗验证、24 条存活）+ 仓库 workflow 实读。
> **证据等级标注**：【✓✓】= 3 票对抗验证通过（验证者逐字核对一手来源原文）；【✓】= 一手来源逐字引文（fetch 阶段提取，未过对抗验证）；无标注 = 综合推断。被否决的论断不收录。

## 0. 现状盘点：你已有的压缩 workflow 资产

**先澄清一个事实**：「没有剪枝 workflow」不成立——仓库里已有 `prune-channel-sweep.yaml`。准确表述是：**剪枝维度只有一个最基础的实现**（单准则 L1 幅值通道剪枝 + 稀疏度扫描 + 可选短微调），相对量化（4 个 workflow）和 NAS（3 代超网 + puzzle + agent 改写）明显单薄。

| 维度 | workflow | 覆盖内容 | 缺口 |
|---|---|---|---|
| NAS | `nas-supernet` v1/v2/v3 | 超网展开→从零训练→搜子网→重训（v3 主推） | 不消费预训练权重 |
| NAS | `puzzle`（本分支重构中） | 预训练模型逐层替换异构轻量块→块级蒸馏建库→**MIP 时延预算下全局选优**→materialize→GKD 重训。节点链：ingest→search_space→baseline→build_library→score→select→materialize→retrain→report | 候选只有结构变体，无「通道保留率/位宽」决策变量 |
| NAS | `nas-hp-search` | Elastic 超网只搜宽度/深度，脚本化 | 同上 |
| NAS | `agent-struct-exploration` | LLM agent 直接 AST 改写结构 | — |
| KD | `kd-nas`（**用户实测不可用**，已由 playground 独立脚本包取代）；puzzle 内建块级蒸馏 + GKD 重训 | 蒸馏作为搜索驱动 / 精度恢复 | KD 与剪枝/量化的显式组合未成 workflow |
| 剪枝 | `prune-channel-sweep` | **仅 L1 幅值准则**剪卷积输出通道，ratio sweep，可选蒸馏式短微调，bake mask（单 agent 单节点） | 无准则对比、无依赖图分组（transformer/residual 必需）、无 2:4 N:M、无深度（层）剪枝、无 Hessian/梯度/激活类准则 |
| 量化 | `quant-sensitivity` / `quant-ptq-sweep`（Smooth/QuaRot/AutoRound）/ `quant-qat`（fake-quant+CAGE）/ `quant-bit-curve`（INT8/W4A8/INT4/MX4/MX8 混精 Pareto） | 敏感性分析、PTQ、QAT、混精位宽 | 与剪枝/NAS 无联动 |

**结构性观察**：三个维度各自为战，没有一条 workflow 把「结构（NAS/剪枝）× 数值（量化）× 恢复（KD）」组合决策——这正是第 6-8 章要回答的问题，第 9 章给落地方案。

---

## 1. 剪枝分类学：统一框架 + 三个粒度

### 1.1 统一框架：所有剪枝方法都是同一算法的变体

几乎所有剪枝方法都可归结为「训练收敛 → 打分 → 剪枝 → 微调」的迭代算法（Han et al. 2015 谱系），方法差异集中在**四个设计轴**上【✓✓，Blalock et al., MLSys 2020，81 篇论文元分析】：

1. **稀疏结构**（非结构化逐参数 vs 结构化按 filter/channel 分组——2020 年该文是二元分类，2:4 半结构化由 2023+ 综述补充）
2. **打分准则**（含 global/local 比较范围）
3. **剪枝调度**（one-shot vs 逐步迭代）
4. **微调/恢复方式**

本报告第 2/3 章分别展开轴 2 与轴 3+4。

### 1.2 粒度三分类与硬件依赖

| 粒度 | 剪什么 | 真实加速条件 | 同剪枝率精度 | 典型代表 |
|---|---|---|---|---|
| **非结构化** unstructured | 单个权重置零 | 需专用稀疏内核/硬件，标准 GPU 仅在 ≥80% 极端稀疏度下有硬件加速收益【✓，PyTorch 官方博客：FlashLLM 类方法 ~80% 稀疏度才提速】 | **最高** | Magnitude、SparseGPT、Wanda |
| **半结构化** semi-structured (pattern-based, N:M) | 每 M 连续元素保留 N 个（如 2:4 = 50% 稀疏但硬件可加速） | NVIDIA Ampere+ sparse tensor core / cuSPARSELt / CUTLASS | 中（LLaMA-2-7B 上比非结构化多掉 7-12 点零样本精度）【✓✓】 | SparseGPT-2:4、Wanda-2:4、MaskLLM |
| **结构化** structured | 整个 filter/channel/head/层 | **标准硬件 + BLAS 等稠密库即可通用加速** | 最低（结构耦合一致性难达成）【✓✓】 | Network Slimming、HRank、FPGM、DepGraph、Minitron、SLEB |

- **只有结构化剪枝能不依赖专用硬件/软件获得通用加速**——经独立双综述（Cheng/Zhang/Shi TPAMI 2024；He & Xiao TPAMI 2024）交叉确认【✓✓】："Only structured pruning can achieve universal acceleration without requiring special hardware or software. Conversely, both unstructured and semi-structured pruning need the support of special hardware or software."
- **同剪枝率精度排序：unstructured > semi-structured > structured**【✓✓，Cheng 综述 Table V/VI：VGG-16 CIFAR + OPT WikiText2 困惑度】。工程选型因此是精度-加速的权衡，不是单维优劣：结构化精度最低但只有它「真剪掉了参数且到处能跑」。

NNI 官方文档用两分类（fine-grained vs coarse-grained）表述同一件事【✓】，N:M 半结构化是两者的折中带。

---

## 2. 剪枝准则族横向对比（设计轴 2）

### 2.1 总表

| 准则族 | 代表方法 | 打分原理 | 需要的数据 | 开销 | 适用粒度 | 证据要点 |
|---|---|---|---|---|---|---|
| **权值幅值** | Magnitude/L1/L2、L1Filter | \|w\| 小 = 不重要 | 仅权重，零额外前向 | 最低 | 全粒度 | **最弱基线**：「范数小=不重要」被 FPGM 用真实网络分布证伪【✓✓】；LLaMA-2-7B 50% 非结构化下 BoolQ 63.0 vs Wanda 75.0【✓✓】 |
| **几何中位数** | FPGM (CVPR 2019 Oral) | filter 离层内几何中位数近 = 冗余 | 仅权重 | 低 | 结构化（filter） | 论文动机即证伪幅值假设；NNI 收录 |
| **激活统计** | APoZ/MeanRank（NNI）、HRank（特征图秩）、**Minitron 激活重要性** | 激活稀疏度/均值/秩衡量通道贡献 | 少量校准样本前向 | 低-中 | 结构化（通道/头/嵌入维） | Minitron：1024 样本纯前向同时算深度/神经元/头/嵌入四轴敏感度，无需反传【✓✓】 |
| **梯度/Taylor 一阶** | TaylorFO（Molchanov）、GraSP、SynFlow、**Movement Pruning**（Sanh 2020） | \|w·∂L/∂w\| 或训练动态中掩码移动 | 训练期梯度 | 中 | 全粒度 | Movement 是 NNCF JPQD 的默认剪枝准则【✓】；SynFlow 揭示 one-shot layer collapse【✓✓】 |
| **Hessian/二阶** | OBS/OBD、HAWQ-V1/V2、HAP | 曲率加权敏感度（Hessian 近似） | 校准数据前向+反传或近似 | 高 | 全粒度 | 与量化共享 OBS 机制（见 §7） |
| **误差补偿重建** | **SparseGPT**（OBS 闭式解逐层最小化重建误差）、Wanda（幅值×激活范数） | 剪枝时补偿剩余权重误差 | 校准数据 | 中 | 非结构化+2:4 | 50% 非结构化下两者持平（BoolQ 均 75.0）；**2:4 下 SparseGPT 明显优**（BoolQ 70.5 vs 67.7，HellaSwag 43.3 vs 40.9）【✓✓】 |
| **门控/正则化** | Network Slimming（BN γ）、Gate Decorator、group-lasso | 训练中注入稀疏正则，惩罚项自动学出剪谁 | 完整训练 | 高 | 结构化 | A*STAR 综述单列一族；「正则化类结构化剪枝」2025 年有专门综述 |
| **自动化搜索** | AMC（RL）、MetaPruning/ABCPruner（进化）、NetAdapt、TAS、DMCP/DHP | 把「每层剪多少」当搜索变量，RL/进化/梯度求解 | 大量评估或预测器 | 高（可摊销） | 结构化 | He & Xiao 综述单列 NAS-Based Pruning 大类，分 RL/梯度/进化三子类【✓✓】 |
| **依赖感知分组** | **DepGraph**（Torch-Pruning 底层） | 不是准则——是自动发现层间耦合、成组同剪的机制，与准则**正交** | 模型结构图 | 低 | 结构化（任意架构） | 见 §2.3 |

### 2.2 关键实证：准则强弱与粒度交互

- **粒度越粗，准则选择越关键**【✓✓】：Llama-2-7B 同 50% 稀疏率下，Wanda 从非结构化→4:8→2:4 的 BoolQ 单调劣化 75.0→72.7→67.7（HellaSwag 52.5→46.5→40.9）；非结构化下 Wanda≈SparseGPT 持平，**2:4 约束下重建类准则的优势（2-3 点）才显现**。数据源为 Wanda 共同作者团队维护的复现库（Wanda 表复现与论文值同趋势，个别列差至 ~1.7 点；Magnitude 表吻合到 0.1 以内）。
- **幅值准则在高稀疏度下崩塌**【✓，arXiv:2401.15347 综述】：70% 稀疏度 LLaMA-7B WikiText2 困惑度——Wanda（无权重更新）85.77 vs SparseGPT（OBS 补偿）26.30 vs SparseGPT+OWL（非均匀层分配）19.49（原始 5.68）。即：**~50% 稀疏度内幅值类够用；更高稀疏度需要误差补偿 + 层间非均匀分配**。
- **组级打分 > 逐层幅值 > 经典自动化准则**【✓，Torch-Pruning 官方基准，ResNet-56/CIFAR-10 ~2× 加速】：DepGraph 组级准则剪后 93.77%（比基线 **+0.38%**）> Ours-L1 92.93%（-0.60%）> HRank 92.17% > AMC（RL）91.90%（-0.90%）> CP 91.80%。

### 2.3 DepGraph：依赖感知是与准则正交的另一个旋钮

结构化剪枝的工程难点不是「打分」而是「依赖」：剪掉一个 conv 的输出通道，残差相加的另一分支、BN、下一层输入维度全要跟着剪，transformer 的 attention head/FFN 中间维同理。DepGraph（CVPR 2023）用图算法全自动建模层间依赖、把耦合参数收集成组同剪，使任意架构（CNN/RNN/GNN/Transformer/LLM）无需手工分组方案【✓✓，arXiv:2301.12900】：

> "we propose a general and fully automatic method, Dependency Graph (DepGraph), to explicitly model the dependency between layers and comprehensively group coupled parameters for pruning... even with a simple norm-based criterion, the proposed method consistently yields gratifying performances"（ResNe(X)t/DenseNet/MobileNet/ViT/GAT/DGCNN/LSTM 跨架构验证）

**工程含义**：准则升级与依赖处理是两个独立旋钮，可分别迭代；官方库 Torch-Pruning 在同一分组结构上实现了 Magnitude/Taylor/Hessian/FPGM/LAMP 等可插拔准则【✓✓】。

---

## 3. 训练策略：调度、范围与恢复（设计轴 3+4）

### 3.1 one-shot vs iterative

| | one-shot（一次剪到位） | iterative（交替 score-prune-update） |
|---|---|---|
| 成本 | 可忽略【✓✓】 | 更高 |
| 精度 | 更易 **layer collapse**（整层被剪空→网络不可训练→精度骤降，SynFlow 提出）【✓✓】 | 通常更好（OPT-1.3B 上迭代次数越多越好）【✓✓】 |
| LLM 细化 | Minitron：**重要性估计本身 single-shot 即可，迭代重估计无额外收益**【✓✓】 | — |

两者不矛盾【✓✓ 综合】：迭代循环的收益来自**剪枝步之间的权重更新/重训**，而非反复重打分。实操结论：打分一次、剪枝分多步、每步间恢复训练。

### 3.2 global vs local（layerwise）

**优劣取决于评测指标，且参数量与 FLOPs 两族指标不可互换**【✓✓，Blalock §7.3 "Metrics are not Interchangeable"，ShrinkBench 800+ 受控实验】：

- 固定模型大小（参数量）下：global 准确率更高；
- 固定理论加速比（FLOPs）下：**结论反转**，layerwise 更优（因每层 compute-per-parameter 不同，两族指标不可互相换算）。

工程含义：`prune-channel-sweep` 类实验**必须同时报告参数压缩比与 FLOPs/延迟两族指标**（该元分析明确建议 "report both compression ratio and theoretical speedup"）。另注意通道率与参数率的口径差【✓，Torch-Pruning】：通道剪枝率 p 对应参数剪除率 ≈ 1-(1-p)²，删 50% 参数只需 pruning_ratio=0.30。

### 3.3 剪后恢复排序：KD > fine-tune > 从头训

- fine-tune 优于从头训（Cheng 综述自跑 ResNet-152/CIFAR-100、DeiT-Tiny/ImageNet；50% 剪枝下 fine-tune 掉 1.40 点 vs 从头训掉 4.00 点）【✓✓】
- **KD 恢复又优于 fine-tune**："The results in [222] show that the pruned network recovered by KD performs better than it regained by fine-tuning"【✓✓，Cheng 综述 §VIII】
- Minitron 重训用 logit 蒸馏【✓】；TorchAO 在量化侧同样给出 QAT 恢复数据（Llama-3 hellaswag 退化恢复 96%）【✓】

**对 Orca 的直接含义**：剪枝/量化之后的恢复环节应默认接蒸馏而非普通微调（`prune-channel-sweep` 现为「可选短微调」）。

---

## 4. 工具生态对比

| 工具 | 定位 | 机制 | 覆盖模型 | 关键事实 | 证据 |
|---|---|---|---|---|---|
| **Torch-Pruning** (VainF, CVPR 2023, 3.3k★) | 通用依赖感知**结构化**剪枝 | DepGraph 分组 + **物理移除**参数（≠ `torch.nn.utils.prune` 的掩码置零），可插拔准则（Magnitude/Taylor/Hessian/FPGM/LAMP） | HF/Timm/Torchvision 现成模型：Llama-2/3、Phi-3、Qwen-2/2.5、DeepSeek-R1-Distill、SAM、扩散模型、ViT/Swin、BERT、Yolo 等 | 对比集中**唯一以依赖图为核心**的通用库【✓✓】；含 Isomorphic Pruning（ECCV 2024）缓解 global 剪枝剪空层风险【✓】 | 【✓✓+✓】 |
| **NNI** (微软) | 统一压缩+超参搜索框架（TF+PyTorch 同 API） | 16 个命名 pruner 覆盖主要准则族（L1Filter/L2Filter/Slim/FPGM/APoZ/MeanRank/TaylorFO/AMC/NetAdapt/SimulatedAnnealing/AutoCompress/AGP/ADMM/LTH/Sensitivity/Level）；掩码式训练 + Model Speedup 模块落实加速；有 dependency-aware 模式；剪枝超参可接入 NNI tuner 搜索 | 广 | 剪枝=搜索 的现成生态；文档 v2.0 | 【✓】 |
| **TorchAO** (PyTorch 官方) | **量化为主**（定位已从 "quantization (and sparsity)" 收缩为 "PyTorch native quantization"） | 2:4 半结构化稀疏为维护/次要特性（SAM 推理 1.1x、ViT 训练 1.3x；SLLM@ICLR 2025）；int4 weight-only（Llama-3-8B 1.89x、省 58% 内存）；QAT 恢复 PTQ 损失；与 torch.compile/FSDP2 兼容；LoRA+QAT 组合快 1.89x；已集成 HF Transformers/vLLM/SGLang/ExecuTorch | HF 生态 | PyTorch 原生栈主力量化方案；2:4 内核依赖 cuSPARSELt/CUTLASS 但缺 fused 量化反量化 | 【✓】 |
| **NNCF** (Intel OpenVINO) | **联合压缩**（JPQD：剪枝+QAT+蒸馏单一流水线） | Movement Pruning 式 warmup 稀疏化→`enable_structured_masking` 依赖解析→量化+蒸馏并行；输出 OpenVINO IR | **硬约束：结构化剪枝仅支持 BERT/Wav2vec2/Swin** | 厂商基准（自报）：BERT-base INT8 JPQD 5.24× 压缩/4.19× 提速/SST-2 掉 <1%，SQuAD EM 反升 1.35% | 【✓】 |
| **llm-pruning-collection** (Princeton/NYU/CMU) | LLM 剪枝研究统一库（Jax 为主） | 10 种方法：Minitron/ShortGPT/Wanda/SliceGPT/SparseGPT/Magnitude/Sheared-LLaMA/SLEB/LLM-Pruner/FLAP + GPU(FMS-FSDP)/TPU(MaxText) 重训框架 | LLM | 剪枝+重训+评测完整流水线；「剪枝 vs 从零训」对照研究的配套代码 | 【✓✓】 |
| Brevitas / Neural Compressor / SparseML | 量化(FPGA 向)/量化/稀疏部署 | — | — | 本轮未深验（缺口，见 §10） | 未验证 |

选型结论：**Orca 侧首选 Torch-Pruning 做结构化剪枝**（唯一同时覆盖「依赖分组 + 多准则 + LLM/transformer + 物理移除」），量化沿用现有 TorchAO 路线（quant-* 系列），LLM 剪枝研究对齐 llm-pruning-collection 的方法谱系。

---

## 5. LLM 时代的剪枝

### 5.1 方法谱系（llm-pruning-collection 全集【✓✓】+ 论文要点）

| 方法 | 粒度 | 准则 | 恢复 | 关键数字 |
|---|---|---|---|---|
| **SparseGPT** | 非结构化 + 2:4 | OBS 闭式误差补偿 | 无（one-shot） | OPT-175B 2:4 剪枝 WikiText2 ppl 8.35→8.74，几何平均加速 1.66×；单卡 A100 3 小时剪完 175B【✓】 |
| **Wanda** | 非结构化 + 2:4 | 权值幅值×激活范数（无权重更新） | 无 | 50% 稀疏度与 SparseGPT 持平；70% 崩塌（ppl 85.77 vs 26.30）【✓】 |
| **Sheared-LLaMA** | 结构化（targeted：剪到指定目标架构） | 可微剪枝掩码 | 继续预训练 + dynamic batch loading | LLaMA2-7B→1.3B/2.7B 仅 50B tokens（此前最强 3B 模型预算的 5%）；2.7B 版 50B tokens 胜 OpenLLaMA-3B 1T tokens；同规模 3% 算力【✓】 |
| **Minitron** (NVIDIA) | 结构化（深度/宽度/头/嵌入四轴） | **纯激活重要性**：1024 样本仅前向 | **logit 蒸馏重训** | 见 §5.2 |
| ShortGPT / SLEB | 深度（整层删除） | 层间余弦相似度（冗余度）/ 激活 | — | 「删层」是与 puzzle 层替换最互补的粒度 |
| SliceGPT / FLAP / LLM-Pruner | 结构化（切片/通道） | 投影/激活统计 | 轻量恢复 | LLM-Pruner：LLaMA-7B 仅 20% 压缩即 ppl 12.62→17.37（加速仅 1.18×）——**decoder-only LLM 对结构化剪枝容忍度远低于 BERT**（BERT 类 75% 压缩仍可保持）【✓】 |

### 5.2 Minitron prune-and-distill：当前 LLM 结构化压缩标杆配方

【✓✓，NVIDIA 博客 + arXiv:2407.14679（NeurIPS 2024）+ 2408.11796；注意数值为 NVIDIA 自报，40x 是与教师 ~15T 预算之比而非 token-matched 对照】

- 结构化剪枝 + 蒸馏重训 vs 从零训练小模型：**MMLU +16%**，每个派生模型 ~100B tokens（**最高 40x 缩减**；MN-Minitron-8B 仅 380B = 教师 15T 的 1/40，Llama-3.1-Minitron-4B 仅 94B = 1/150），模型家族训练计算最多省 **1.8×**；MN-Minitron-8B 在 MMLU 69.5 vs 65.3 反超教师 Llama-3.1-8B【✓】
- 准则：纯激活重要性估计，**1024 个校准样本、仅前向传播**，同时覆盖深度/神经元/头/嵌入通道四轴【✓✓】；宽度剪枝用 l2-norm（batch 维）+ mean（序列维）聚合【✓】
- **宽度 vs 深度（≤15B 规模）**：宽度剪枝精度更好（MMLU 60.5% vs 58.7%，GSM8K 41.2% vs 16.8%），深度剪枝加速更大（TensorRT-LLM 平均 2.7× vs 1.8×）——精度与吞吐两轴明确权衡【✓✓+✓】；一次性剪枝后宽度剪枝 LM loss 反而更高，短蒸馏重训 ~200 steps 后趋势反转【✓✓】
- teacher correction：原始预训练数据不可得时，先用蒸馏数据集给教师 ~100B token 轻量微调，LM 验证损失降 >6%，可与蒸馏并行【✓】
- **NAS 一次性学到的架构结论可跨模型族复用**：Minitron 后续工作直接沿用首篇 NAS 学到的手工配置（hidden 4096→3072、MLP 14336→9216、保头、深度不变），跳过重搜【✓】——对 Orca：puzzle/NAS 搜出的层配置结论同样可沉淀复用。

### 5.3 剪枝 vs 从零训练：结论对预算口径高度敏感

【✓✓，arXiv:2606.14150（Princeton/NYU/CMU 2026-06，附开源代码），Llama-3.1-8B 六方法 × 0.5-0.8 剪枝率】

- **等重训预算（50B tokens）**：剪枝初始化对全部 6 种方法（Minitron-D/W、FLAP、Sheared-LLaMA、Wanda、SparseGPT）优于随机初始化，但优势随剪枝率升高收窄（深度剪枝 Δ Avg 从 50% 的 +3.7 降至 81.3% 的 -0.2，基本消失）
- **总预算口径（250B 全给从零训练）**：粗粒度结构化剪枝可被追平或反超（Minitron-D 64.4% vs 从零 66.2%），仅细粒度非结构化保持优势（Wanda-U 68.1% vs 67.2%），FLAP 是结构化例外（66.5% vs 65.1%）
- **关键限定**：该研究重训用普通 LM 训练而非 KD，作者自承 KD 配方（真 Minitron/Sheared-LLaMA）可能缩小或消除差距——**不可外推为「剪枝已死」**。

另一条 CNN 时代元分析结论【✓✓，Blalock §3.3】："pruning generally does not help as much as switching to a better architecture"——剪枝模型很少超过一个本身更优的架构。**这是「先选好架构（NAS）再压缩」顺序流水线的核心证据**（适用范围：架构选择仍开放的场景；LLM 场景架构已定时以压缩为主）。

---

## 6. NAS × 剪枝 × 量化：三种联合范式

He & Xiao 综述把 **NAS-Based Pruning**（§2.6：RL 系 AMC/AutoCompress/RL-MCTS、梯度系 DMCP/DHP/PaS/TAS、进化系 MetaPruning/ABCPruner）和 **Joint Compression**（§2.7.2：NPAS/DJPQ/APQ 等）列为正式技术大类，并给出动机【✓✓】：

> "Applying these techniques in sequence may seem like a natural extension but can lead to sub-optimal solutions due to different optimization objectives"（顺序应用因优化目标不同可能次优）

但联合搜索的成本/复杂度高，实践中有三条路线：

### 路线 A：顺序流水线（默认主干）

```
NAS/架构选择 → 剪枝（结构） → 恢复训练（KD） → 量化（数值） → 部署
```

- 依据：架构优先于压缩（§5.3 Blalock 元分析）【✓✓】；Minitron prune-and-distill 即此范式的 LLM 实例【✓✓】；NVIDIA 2:4+INT8 官方工作流也是先剪后量（§7）【✓】
- 弱点：各阶段目标不一致（综述已指）【✓✓】；上游决策不可见下游损失（如剪出一个对量化极不友好的宽度）

### 路线 B：联合搜索（进阶）

| 工作 | 联合维度 | 机制 | 关键数字 | 证据 |
|---|---|---|---|---|
| **APQ** (CVPR 2020, Han Lab) | 架构+剪枝+量化 三者一体 | OFA 超网采样零成本构造 fp32 精度预测器训练集 → 知识迁移训出**量化感知(int8)精度预测器** → 进化搜索 | 比顺序流水线 ProxylessNAS+AMC+HAQ **ImageNet +2.3%**，GPU 时/碳排放降数量级；同精度比 MobileNetV2+HAQ 延迟 -2×/能耗 -1.3× | 【✓】 |
| **HAQ** (CVPR 2019) | 混精位宽（每层 1-8 bit） | RL agent + **硬件模拟器直接反馈延迟/能耗**（非 FLOPs 代理） | vs 固定 8bit：延迟 -1.4~1.95×、能耗 -1.9×、精度损失可忽略；不同硬件（edge/cloud）最优策略差异巨大 | 【✓】 |
| **HAWQ-V3** (ICLR 2021) | 混精位宽 | **逐层位宽选择建模为 ILP**（模型扰动 vs 内存/延迟约束）；纯整数推理（无 FP↔INT 隐藏转换） | ResNet50 INT8 77.58%；INT4/8 混精再降 23% INT8 延迟仍保 76.73%；TVM 开源部署，uniform 4-bit vs 8-bit 平均 1.45× | 【✓】 |
| **DJPQ / NPAS** | 剪枝+量化联合 | 端到端可微 / 编译器感知 | 综述收录【✓✓】 | 【✓✓】 |
| **NNCF JPQD** | 剪枝+量化+蒸馏 单流水线 | 见 §4；工程上以「减轻顺序执行的开发者复杂度」为动机 | BERT INT8 5.24× 压缩 / 4.19× 提速 / 精度损 <1%（Intel 自报） | 【✓】 |

**APQ 的核心洞见对 Orca 最有价值**：联合搜索的瓶颈是「评估一个量化候选要 QAT 重训」，APQ 用**量化感知精度预测器**绕过它——这正是 puzzle 的「块级蒸馏建库打分 + MIP 选优」可以类比的结构（离线建库评分 → 全局优化选择，无需在线重训每个候选）。

### 路线 C：弹性权重共享（一次训练，多次特化）

- **OFA (ICLR 2020)**：训练一个 once-for-all 网络，解耦训练与搜索，每个部署场景直接选子网无需重训；其 progressive shrinking 算法被作者明确定义为 **"a generalized pruning method"**，把剪枝的搜索维度从宽度扩展到「深度、宽度、核尺寸、分辨率」四维【✓】——**「剪枝即 NAS 搜索算子」的直接文献证据**：超网按宽度弹性化 = 把「每层剪多少通道」变成超网的采样维度。nas-supernet-v3 的弹性宽度/深度与此同构。
- **MCUNet/TinyNAS**：两阶段 NAS——先在资源约束下**优化搜索空间本身**再搜架构，与推理引擎（TinyEngine）联合设计；首个 MCU 上 >70% ImageNet，比量化版 MobileNetV2/ResNet-18 省 3.5× SRAM/5.7× Flash（基线本身含量化模型）【✓】。启示：**极紧资源下「NAS+引擎协同」胜过「手工架构+量化」**；硬约束（延迟/内存）可直接进搜索目标而非 FLOPs 代理——与 puzzle 的 latency_script_path 实测时延路线一致。

### 三条路线的选择判据

| 场景 | 推荐 |
|---|---|
| 架构未定、部署目标单一 | A（NAS 先行），必要时 C（超网弹性化把剪枝吸收进搜索空间） |
| 多部署目标/多衍生模型 | C（一次训练多次特化），NAS 结论沉淀复用（Minitron 先例【✓】） |
| 极致 Pareto、有离线建库/预测器预算 | B（APQ 式：建库打分 + 预测器 + 全局搜索） |
| LLM、架构固定 | A 的压缩段：prune-and-distill → 量化（Minitron 配方） |

---

## 7. 稀疏 × 量化的相互作用（组合的最大证据缺口区）

### 7.1 顺序：官方工作流与最新研究的张力

- **NVIDIA 既定工作流（先剪后量）**【✓】：预训练稠密模型 2:4 剪枝+微调 → 对已稀疏模型 PTQ/QAT → TensorRT 部署稀疏 INT8 引擎。工程坑：稀疏模型上做 QAT 时，量化微调会**覆盖已算好的稀疏权重**，必须重新初始化剪枝状态并冻结稀疏掩码。TensorRT 8.6 ResNet-34 上 2:4 与 INT8 三个设置（FP32/INT8 PTQ/INT8 QAT）基本无精度冲突；加速依赖 workload（bs=1 约 1.20-1.21×，bs=2048 约 1.40-1.42×，极端分辨率 ~1.66×）；**内核资格依赖通道几何**：输出通道 32 的倍数利于 TensorCore/IMMA，>128 通道稀疏内核才占优。
- **渐进强度假说（2025-10 OpenReview）**【✓】：压缩顺序本身显著影响最终性能；「**较弱扰动先施加、较强扰动后施加**」，在 LLaMA 2/3、Mistral、BERT、ResNet-18、DeiT-Base 上成立。该文**反驳并细化** Harma et al.（ICLR 2025）「剪枝后量化总是更优」——后者仅在幅值剪枝+max 缩放量化的狭窄设定成立。关键细化：
  - **粒度决定干扰**：LLaMA-3-8B QuaRot + 5% 剪枝下，结构化剪枝（SLEB）在 7-9 bit 区间顺序优势精确为 0（无干扰），非结构化剪枝（SparseGPT）干扰随量化强度单调增长；**4-bit 时先剪后量化明显更优**（A_{Q→P} 大幅负：SparseGPT -49.9、SLEB -9.4）
  - **旋转量化 × 剪枝冲突**：QuaRot 类旋转已成事实标准，但剪枝若不感知旋转会灾难性退化——LLaMA-3-8B SparseGPT 剪到 40% 稀疏度，WikiText2 ppl 无旋转 8.477 vs 旋转后 98.213；非结构化受旋转伤害远大于结构化。**Orca 的 quant-ptq-sweep 已含 QuaRot，若与剪枝组合必须做旋转感知处理**
  - 恢复手段（LoRA/PEFT）放量化之后有效补偿；HAWQ-V2 混精的渐进式位宽分配（8→2）随总压缩率升高越来越优于回归式（2→8）

### 7.2 稀疏+量化 vs 更低比特：等压缩比下的路线之争

【✓，PyTorch 官方博客 2025-10】LLaMA-2-7B 等理论 8× 压缩比下，**4-bit 量化 + 50% 稀疏 > 纯 2-bit 量化**：2-bit AQLM 平均 58.6 / QUIP# 59.8，vs GPTQ+SparseGPT（4bit+非结构化 50%）61.3、AbsMax+MaskLLM（4bit+2:4）60.3（dense 16bit 基线 63.8）。零样本 SLiM-LoRA（r=0.1）叠在 2:4+4bit 上可把精度恢复到 64.2 **超过 dense 基线**。工具缺口：cuSPARSELt/CUTLASS 有高性能 2:4 matmul 但缺 fused 量化；Sparse Marlin（vLLM/SGLang）仅 W4A16 且高性能限 Ampere；Triton/ThunderKittens 无原生 2:4。非结构化硬件加速要 ~80% 稀疏度才兑现——**2:4 是可部署的中间地带**。

### 7.3 机理层：剪枝与量化共享 OBS

SparseGPT（剪枝）与 OPTQ/GPTQ（量化）共享同一二阶最优权重更新机制（Hessian = 2XXᵀ），可直接组合且组合优于单独量化【✓，arXiv:2401.15347】——这是「2:4+INT8 顺序组合可行性」的机理证据；同综述指出**尚缺乏统一三种以上压缩算法的研究**（组合压缩的开放问题）。

---

## 8. KD 在压缩流水线中的角色

| 位置 | 结论 | 证据 |
|---|---|---|
| **剪枝恢复** | KD > fine-tune > 从头训 | 【✓✓】Cheng 综述 §VII-F + §VIII |
| **LLM 剪枝恢复（蒸馏 vs 继续预训练）** | Minitron 全程 logit 蒸馏（+16% MMLU、40x token 节省）；Sheared-LLaMA 用继续预训练并引 Jha et al. 2023 称该配方比蒸馏更 cost-effective | 【✓✓】【✓】两文方向相反——**冲突明示**：差异在预算/数据口径（Sheared-LLaMA 需要原始数据分布的继续预训练数据；Minitron 场景蒸馏数据即可）。选择判据：有教师且数据受限 → 蒸馏；需恢复原始预训练分布且有数据 → 继续预训练 |
| **量化恢复（QKD）** | **naive KD+量化可能反效果**（KD 正则进一步压缩已量化学网的表示能力，"may not work as desired"）；QKD 三阶段协调：Self-studying（量化学网先无 KD 微调）→ Co-studying（教师本身训得更量化友好）→ Tutoring（再蒸馏）。W4A4 比 SOTA +1.3%（ResNet-18）/+2.6%（MobileNetV2）；协调后 ResNet 可到 W3A3 无损恢复，MobileNetV2 仅到 W6A6——**深度可分离架构需要更高位宽**，架构依赖的量化-KD 交互 | 【✓，arXiv:1911.12491】 |
| **联合流水线内** | NNCF JPQD 中蒸馏与剪枝+QAT 并行，SQuAD EM 反升 1.35% | 【✓】 |
| **量化后 PEFT** | 量化后施加 LoRA/SLiM-LoRA 有效补偿（可超 dense 基线） | 【✓】 |

对 Orca：KD 不是任意位置的即插即用模块——**剪枝后接 KD 有直接正证据；量化后接 KD 需协调（QKD 式三阶段或改为 LoRA 恢复）**。

---

## 9. 面向 Orca 的落地建议

> 综合层推断：各子建议的支撑论断均为一手来源验证（§1-8），但组合方案本身未经实验验证。

### 9.1 剪枝维补齐（prune-channel-sweep 升级，或新增 workflow）

1. **准则族扩展**：L1 单准则 → 多准则候选集 {L1（保留为基线）, FPGM, 激活重要性（对齐 Minitron：校准集前向统计）, TaylorFO}。依据：幅值最弱且假设被证伪【✓✓】、组级打分>逐层幅值>AMC/HRank【✓】、N:M 下准则选择更关键【✓✓】。
2. **依赖分组**：引入 Torch-Pruning/DepGraph 处理残差/拼接/attention 耦合，覆盖 transformer（当前仅卷积输出通道）。依据：依赖与准则正交、可独立迭代【✓✓】。
3. **双指标口径**：report.json 增加参数压缩比列（现仅 FLOPs 理论加速）；文档区分通道率 p 与参数率 1-(1-p)²。依据：两族指标不可互换【✓✓】。
4. **2:4 半结构化路径**（新 workflow，如 `prune-2x4`）：面向 Ampere+ 部署；准则用误差补偿类（SparseGPT 式）而非幅值【✓✓：2:4 下重建类明显优】；输出通道 round 到 4x/8x【✓】。
5. **LLM 结构化剪枝**（新 workflow，如 `prune-llm`）：Minitron 配方——激活重要性（1024 样本前向）→ 宽度优先 → 蒸馏重训；深度剪枝（ShortGPT/SLEB 式层删除）与 puzzle 的层替换天然互补。

### 9.2 流水线组合（三阶段演进）

**Phase 1（顺序编排，低成本）**：新增编排型 workflow `compress-pipeline`，串联现有资产：

```
prune-*（结构压缩 + DepGraph bake）
  → KD 恢复（剪枝后接蒸馏——正证据；注意 kd-nas 实测不可用，用独立蒸馏脚本包或 puzzle 的 GKD 机制）
  → quant-ptq-sweep / quant-qat（数值压缩；QAT 时冻结稀疏掩码【✓】；含 QuaRot 时做旋转感知检查【✓】）
  → bake + 双指标验收（gate 用实测时延而非 FLOPs，对齐 puzzle 的 latency_script_path 契约）
```

顺序判据：架构未定先 NAS（§5.3）；架构已定先剪后量（NVIDIA 工作流 + 4-bit 下先剪后量更优【✓】）；恢复用 KD（剪枝后）/QKD 三阶段或 LoRA（量化后）（§8）。

**Phase 2（决策变量融合）**：把 puzzle 的 MIP 选择器从「层变体选择」扩展为「层变体 × 每层剪枝率 × 每层位宽」联合决策——HAWQ-V3 已示范位宽分配可建模为 ILP【✓】，APQ 已示范联合优于顺序（+2.3%）【✓】。`quant-sensitivity` 的敏感层输出与 `pz_score` 的块级打分可直接作为 MIP 目标函数输入；搜索空间爆炸用 APQ 式预测器/离线建库控制（puzzle 的 build_library 结构天然适配）。

**Phase 3（弹性范式）**：nas-supernet-v3 的弹性宽度/深度已等价于「剪枝即搜索算子」（OFA progressive shrinking = generalized pruning【✓】）；进一步把位宽做成第四个弹性维度（slimmable-precision），一次训练多次特化。

### 9.3 明确不做 / 谨慎项

- **非结构化剪枝**：无 ≥80% 稀疏度的硬件兑现路径【✓】，仅在研究对照场景保留（llm-pruning-collection 复现用）。
- **2:4+INT8 盲目叠加**：先在 quant-sensitivity 里做稀疏×位宽联合敏感性扫描（渐进强度假说：最优顺序随强度变【✓】），不独立扫描后直接叠加。
- **量化后直接挂普通 KD**：需 QKD 式协调或改 LoRA【✓】。
- **Brevitas/Neural Compressor/SparseML**：本轮未验证，选型前需补调研（§10）。

---

## 10. 证据缺口与开放问题

1. **量化×稀疏交互的受控实验数据仍薄**：本轮核心论断多来自单一 2025-10 研究（渐进强度假说）+ NVIDIA 厂商数据；跨模型/跨硬件复现缺。这正是 9.2 Phase 2 落地前最该先用 playground 实验补的洞。
2. Hessian/HAWQ-HAP、Taylor 类与 Minitron 激活准则在 **LLM 结构化剪枝**上的受控直接对比缺（现有对比集中在非结构化/N:M 的 Wanda/SparseGPT/Magnitude）。
3. Torch-Pruning 物理移除后的自定义结构能否被 TorchAO/NNCF 的 PTQ/QAT 图模式识别（剪枝+量化工作流能否直接串联的互操作问题）。
4. APQ 式联合搜索在 transformer/LLM 时代相对 Minitron 顺序配方可复现收益未知；puzzle MIP 纳入剪枝率/位宽后搜索空间爆炸与收益的权衡需实验。
5. Brevitas / Intel Neural Compressor / SparseML 未深验。
6. 时效提醒：LLM 剪枝演进快（2026-06 才出现预算口径对照研究），半年内结论可能更新；Minitron 数值为厂商自报且 OpenReview 审稿人质疑其 from-scratch 基线。

## 11. 参考来源

**对抗验证通过（✓✓）的一手来源**
- Cheng, Zhang, Shi. *A Survey on Deep Neural Network Pruning: Taxonomy, Comparison, Analysis, and Recommendations*. TPAMI 2024. https://arxiv.org/abs/2308.06767
- Blalock et al. *What is the State of Neural Network Pruning?* MLSys 2020. https://arxiv.org/abs/2003.03033
- He & Xiao. *Structured Pruning for Deep Convolutional Neural Networks: A Survey*. TPAMI 2024. https://oar.a-star.edu.sg/storage/g/gdk3ego72n/structured-pruning-survey-camera-ready.pdf （arXiv:2303.00566）
- Fang et al. *DepGraph: Towards Any Structural Pruning*. CVPR 2023. https://arxiv.org/abs/2301.12900 ；工具 https://github.com/VainF/Torch-Pruning
- Xu et al. *Small LLMs: Pruning vs. Training from Scratch*. 2026. https://arxiv.org/html/2606.14150v1 ；代码 https://github.com/zlab-princeton/llm-pruning-collection
- NVIDIA. *How to Prune and Distill Llama-3.1 8B to an NVIDIA Minitron 4B Model*. 2024-08. https://developer.nvidia.com/blog/how-to-prune-and-distill-llama-3-1-8b-to-an-nvidia-llama-3-1-minitron-4b-model/
- FPGM: He et al., CVPR 2019. https://arxiv.org/abs/1811.00250 （经验证者核对）

**一手来源引文（✓）**
- APQ. CVPR 2020. https://arxiv.org/abs/2006.08509 ；OFA. ICLR 2020. https://arxiv.org/abs/1908.09791 ；HAQ. CVPR 2019. https://arxiv.org/abs/1811.08886 ；HAWQ-V3. ICLR 2021. https://arxiv.org/abs/2011.10680 ；MCUNet. NeurIPS 2020. https://arxiv.org/abs/2007.10319
- Minitron 论文：arXiv:2407.14679（NeurIPS 2024）、arXiv:2408.11796
- Sheared-LLaMA. https://princeton-nlp.github.io/sheared-llama/ （arXiv:2310.06694）
- LLM 剪枝综述（SparseGPT/Wanda/OWL/Kprune 数据）：arXiv:2401.15347
- QKD. arXiv:1911.12491
- NVIDIA. *Sparsity in INT8 Training Workflow & Best Practices for TensorRT Acceleration*. https://developer.nvidia.com/blog/sparsity-in-int8-training-workflow-and-best-practices-for-tensorrt-acceleration/
- 压缩顺序/渐进强度假说. OpenReview. https://openreview.net/forum?id=KWtOTMMvKU
- PyTorch. *When Quantization Isn't Enough: Why 2:4 Sparsity Matters*. 2025-10. https://pytorch.org/blog/when-quantization-isnt-enough-why-24-sparsity-matters/
- NNCF JPQD. https://blog.openvino.ai/blog-posts/joint-pruning-quantization-and-distillation-for-efficient-inference-of-transformers ；TorchAO. https://github.com/pytorch/ao ；NNI 剪枝文档. https://nni.readthedocs.io/en/v2.0/Compression/pruning.html

**仓库侧事实**：`workflows/*.yaml` 实读（prune-channel-sweep / quant-* ×4 / nas-supernet v1-v3 / puzzle / kd-nas / nas-hp-search / agent-struct-exploration）。

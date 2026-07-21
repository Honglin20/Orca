# 05 · LLM × NAS（2025 新风口）

## 大方向
LLM 不再只是「生成代码」，而是作为 **搜索 agent / 进化算子 / 设计原则载体** 进入 NAS。

## LAPT (AAAI 2025) — Design Principle Transfer
预训练 LLM 从成熟架构里**学习设计原则**，迁移到新搜索空间指导搜索。被引 50。
- https://ojs.aaai.org/index.php/AAAI/article/view/34463/36618

## RZ-NAS (2025) — Reflective Zero-Cost
LLM 生成架构 + reflective zero-cost 评估，结合进化算法。
- https://raw.githubusercontent.com/mlresearch/v267/main/assets/ji25a/ji25a.pdf

---

## UH-NAS 详解 ⭐（本轮重点）

**全称**：Unconventional Hardware NAS
**论文**：*LLM-Guided Neural Architecture Search for Robust Co-Design of Physical Neural Networks*
**作者**：Tyler King, Timothee Leleu　**时间**：2026-06-09
**链接**：https://arxiv.org/abs/2606.10294

### 场景（重要：与常规 NAS 不是一回事）
面向**非传统硬件（unconventional hardware）**部署，特指**光学 MZI（Mach-Zehnder Interferometer）干涉仪芯片**这类物理神经网络硬件。要协同优化的不只是 accuracy，还有：
- inference **energy cost**
- 物理 **non-idealities**（制造误差、串扰、损耗等非理想特性）
- **numerical precision**（光学硬件精度受限）

### 解决的问题
现有 NAS 多针对单一硬件家族定制，**无法跨平台公平比较 / 泛化**。

### 方法（三个关键设计）
1. **LLM 作为进化算子（evolutionary operators）**：把 LLM 嵌进进化搜索循环，用 LLM 执行变异 / 交叉（而不是随机/规则算子），协同优化 accuracy + inference energy。
2. **硬件抽象为可替换后端（swappable backend）**：每个平台提供三件套 —— energy model + physical constraints + non-ideality simulator。换硬件只换后端，**不改搜索算法**。
3. **公平的 system-level 比较**：因为算法-硬件解耦，可在不同后端上做可比较的评估。

### 实验结果
在光学 MZI 硬件上：比传统 baseline 发现**更多样、更鲁棒**的架构，且超过现有 LLM-to-NAS 方法。消融：non-ideality 下的架构鲁棒性 + system prompt 的作用。

### ⚠️ 与 kd-nas 的关系判断（务必注意）
- UH-NAS **不是 GPU 上的 supernet 训练技术**，也不是常规「训练技术」。它是**搜索策略 + 物理硬件协同设计**，面向光学 / 新兴计算硬件。
- 它的「co-design」指 accuracy vs inference energy 的硬件权衡，**不是** KD 那种精度蒸馏。
- 对 kd-nas（深度模型压缩 / 蒸馏、GPU 训练）**直接借鉴价值有限**；值得借鉴的只有「LLM 当进化算子」「可插拔后端」两个工程思路。
- 它是 2026-06 新文，尚未经广泛验证；场景极窄，**不要当作通用 NAS 训练技术引用**。

### 可借鉴点（剥离场景后）
- **LLM-as-evolutionary-operator**：用 LLM 做变异/交叉，比随机算子更能探索有意义的设计空间 —— 可迁移到任何 NAS 的通用 idea。
- **swappable backend 抽象**：把硬件 / 评估约束做成可插拔后端，搜索算法与评估解耦 —— 对任何 hardware-aware NAS 都是好设计。

### 待精读问题（讨论时展开）
1. LLM 当进化算子的具体 prompt / 输入输出格式？
2. 「协同优化 accuracy + energy」是加权标量还是真 Pareto？
3. non-ideality simulator 怎么建？是否可复现？
4. 与 LAPT / RZ-NAS 相比，进化算子用 LLM 的增益到底来自哪？

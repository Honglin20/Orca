# 03 · DARTS 防坍塌（cell-based 微分搜索）

## 适用边界
仅当 search space 是 **cell-based、可微连续化**（DARTS 风格）时适用。和 SPOS/sandwich 的 block-wise 离散超网是两套范式。kd-nas 若用 block-wise 离散空间，本路线基本用不上。

## Performance collapse 成因
搜索后期架构被 **skip connection 支配** + Hessian 出现尖锐局部极小 → 退化架构。

## 经典改进
- **DARTS-** (Chu 2020)：无 indicator，直接重填 skip —— https://arxiv.org/abs/2009.01027
- **RobustDARTS** (Zela ICLR2020)：用 Hessian 特征值诊断失败模式
- **FairDARTS / SDARTS**：去耦合 / self-distillation 缩离散化 gap

## 最新（2024–2025）
- **EM-DARTS**：edge mutation 防坍塌
- **EL-DARTS (Electronics 2026)**：lightweight，0.075 GPU-day，CIFAR-10 错误率 2.47% —— https://www.mdpi.com/2079-9292/15/2/314
- **ZO-DARTS++ (2025)**：zeroth-order 微分，治效率 + size 可变性 —— https://arxiv.org/pdf/2503.06092

## 判断信号
现象是「架构被 skip 淹没 / 搜索后期精度突然崩」才需要这条路线；若是 ranking 不准（超网预测 vs 真实），那属于 01 的 supernet 问题，不是 DARTS collapse。

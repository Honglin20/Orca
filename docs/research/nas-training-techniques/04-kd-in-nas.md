# 04 · KD in NAS 新形态

> 与 kd-nas 最相关的一脉。

## in-place / online distillation（BigNAS 配方）
当步 max 子网当 teacher，同步蒸馏给小/随机子网。已在 README/01 详述。这是 supernet 训练的标准 KD 形态。

## self-distillation in DARTS
用超网**历史 checkpoint** 蒸馏给当前步，稳定 DARTS 搜索。
- https://arxiv.org/abs/2302.05629

## DNA — block-wise KD (CVPR 2020)
分块蒸馏，teacher 是现成强架构，指导 block 级 NAS。
- https://openaccess.thecvf.com/content_CVPR_2020/papers/Li_Block-Wisely_Supervised_Neural_Architecture_Search_With_Knowledge_Distillation_CVPR_2020_paper.pdf

## Bi-Teacher (NAS-BNN, 2024/25)
双 teacher 框架，改进 sandwich 在二值网络上的收敛。

## HIO-NAS (IEEE 2025) ⭐
hardware-aware iterative one-shot NAS + **adaptable KD**，四阶段：全训 → 随机搜索 → 硬件验证 → KD 重训。
- http://ieeexplore.ieee.org/iel8/6287639/10820123/10938148.pdf
- 最贴近「KD-centric NAS」的 2025 工作。

## RNAS-CL
cross-layer KD，提升搜索到模型的鲁棒性。
- https://openreview.net/forum?id=S0nrdTCNEn

## kd-nas 可借鉴形态优先级
1. in-place（若 kd-nas 超网已用 sandwich，这是零成本叠加项）
2. HIO-NAS 的「KD 重训阶段」（search 后 KD 精修）
3. self-distillation（若用 DARTS 风格搜索）

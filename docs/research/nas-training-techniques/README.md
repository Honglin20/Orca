# NAS 训练技术调研（2024–2026）

> 调研日期：2026-07-21　·　背景：kd-nas workflow 中 SPOS / sandwich rule / KD 之外，最新 NAS 训练技术
> 用途：逐个技术探讨的素材库 + 精读入口。每个主题独立成文，便于逐个深挖。

## 0. 起点：SPOS / Sandwich / KD 的真实关系

这三者不是并列技术，而是 weight-sharing one-shot NAS **超网训练**的同一条主线，且 **sandwich + in-place distillation 必须联合使用**（BigNAS 确立的事实标准）：

| 技术 | 作用 |
|---|---|
| **SPOS** (uniform sampling) | 每步均匀采一个子网更新，最朴素的权重共享训练 |
| **Sandwich rule** (BigNAS/FairNAS) | 每步采 max + min + 若干随机子网，强制权重耦合的两端都被训到 |
| **In-place distillation** (BigNAS) | 用当步 max 子网当 teacher，同步蒸馏给小/随机子网；必须靠 sandwich 保证 max 子网够强才能当好 teacher |

「KD」在此特指 **in-place / online KD（max→small 同步）**，不同于离线 teacher-student KD。

## 1. 五条技术脉络

1. **超网训练演进**（sandwich 之后） → [`01-supernet-training.md`](01-supernet-training.md)
   OFA Progressive Shrinking、采样策略精细化（subnet unfairness → Subnet-Aware Dynamic CVPR2025）
2. **Training-free / Zero-cost proxy**（绕开训练） → [`02-training-free-zerocost.md`](02-training-free-zerocost.md)
   AZ-NAS (CVPR2024) 组装 proxy、TRNAS (ICCV2025)
3. **DARTS 防坍塌**（cell-based 微分搜索） → [`03-darts-variants.md`](03-darts-variants.md)
   DARTS-/RobustDARTS → EM-DARTS/EL-DARTS/ZO-DARTS++
4. **KD in NAS 新形态** → [`04-kd-in-nas.md`](04-kd-in-nas.md)
   self-distillation DARTS、DNA block-wise、Bi-Teacher、HIO-NAS (2025)
5. **LLM × NAS**（2025 新风口） → [`05-llm-x-nas.md`](05-llm-x-nas.md)
   LAPT (AAAI2025)、RZ-NAS、**UH-NAS (2026-06)**

## 2. 论文清单（按主题）

### 超网训练
- BigNAS (ECCV2020) — sandwich + inplace — https://www.ecva.net/papers/eccv_2020/papers_ECCV/papers/123520681.pdf
- OFA (ICLR2020) — progressive shrinking — https://arxiv.org/abs/1908.09791
- Subnet-Aware Dynamic Supernet Training (CVPR2025) — CaLR+MS — https://arxiv.org/html/2503.10740v1
- ProX (Neurocomputing 2024) — progressive expansion（反向 OFA）

### Training-free
- AZ-NAS (CVPR2024) — assembling zero-cost proxies — https://arxiv.org/pdf/2403.19232
- TRNAS (ICCV2025) — robust training-free — https://openaccess.thecvf.com/content/ICCV2025/papers/Yang_TRNAS_A_Training-Free_Robust_Neural_Architecture_Search_ICCV_2025_paper.pdf
- 索引仓库 — https://github.com/MarttiWu/Training-Free-NAS

### DARTS
- DARTS- (2020) — https://arxiv.org/abs/2009.01027
- EL-DARTS (Electronics 2026) — https://www.mdpi.com/2079-9292/15/2/314
- ZO-DARTS++ (2025) — https://arxiv.org/pdf/2503.06092

### KD in NAS
- HIO-NAS (IEEE 2025) — hardware-aware + adaptable KD — http://ieeexplore.ieee.org/iel8/6287639/10820123/10938148.pdf
- Improving DARTS via Self-Distillation (2023) — https://arxiv.org/abs/2302.05629
- DNA block-wise KD (CVPR2020) — https://openaccess.thecvf.com/content_CVPR_2020/papers/Li_Block-Wisely_Supervised_Neural_Architecture_Search_With_Knowledge_Distillation_CVPR_2020_paper.pdf

### LLM × NAS
- LAPT (AAAI2025) — design principle transfer — https://ojs.aaai.org/index.php/AAAI/article/view/34463/36618
- RZ-NAS (2025) — LLM + reflective zero-cost — https://raw.githubusercontent.com/mlresearch/v267/main/assets/ji25a/ji25a.pdf
- UH-NAS (arXiv 2026-06) — LLM as evolutionary operator + 硬件协同 — https://arxiv.org/abs/2606.10294

### 综述
- Advances in NAS (National Science Review 2024) — https://academic.oup.com/nsr/article/11/8/nwae282/7740455

## 3. 探讨顺序（按与 kd-nas 相关度）
Subnet-Aware (CVPR2025) → HIO-NAS (2025) → AZ-NAS → UH-NAS（当前）→ 其余

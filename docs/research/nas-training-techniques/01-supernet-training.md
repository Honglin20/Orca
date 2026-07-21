# 01 · 超网训练演进（sandwich 之后）

## 核心：subnet unfairness 问题
sandwich / uniform 采样下，不同复杂度子网用**同一个学习率、同样更新步数**，导致极端复杂度子网欠训/过训，**ranking 失真**（超网预测的子网排名 ≠ 真实训练排名）。这是 sandwich 之后超网训练的主战场。

## 路线 A：改采样时序 —— Progressive Shrinking (OFA, ICLR 2020)
不每步随机采样，而是**从大到小分阶段解锁 elastic 维度**（depth / width / kernel / resolution）。先训满血网络，再逐步把更小的子网维度加进采样池。解决子网互相干扰。
- 链接：https://arxiv.org/abs/1908.09791
- 变体 **ProX (2024)**：反向（从小到大扩展），用于医学影像。

## 路线 B：改采样概率 —— 采样策略族
| 策略 | 做法 |
|---|---|
| FairNAS fair sampling (2019) | 保证每个 choice 每步等概率被采 |
| GreedyNAS (2020) | 只训「有潜力」子网，丢弃差池 |
| PSS-Net (2021) | prioritized sampling，多子网池 |
| Focus-Fair sampling | 带温度参数的聚焦采样 |

## 路线 C：改训练策略 —— Subnet-Aware Dynamic (CVPR 2025) ⭐
**最新、最直接**：CaLR（complexity-aware learning rate）+ MS（mixing strategy），**按子网复杂度自适应调学习率**，直击 unfairness。
- 链接：https://arxiv.org/html/2503.10740v1
- 关键判断：sandwich 的正统继承者，做 supernet ranking 质量时首读。

## 路线 D：超网后处理
- **Supernet fine-tuning**：训完 fine-tune 到聚焦子空间 + 进化搜索。
- **Mixture-of-Supernets**：MoE 风格 router 给架构路由权重，缓解 weight entanglement。

## 与 SPOS/sandwich/KD 的关系
这些都是在「同一个超网、权重共享」范式内改进采样/调度；in-place KD 可与上述任意路线叠加（只要保证当步采到 max 子网当 teacher）。

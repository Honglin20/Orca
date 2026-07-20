# D13 · kd_feature_ofd_fitnets（特征级 KD 家族：FitNets / OFD / Review-KD / MGD）

> 一句话定位：**中间层 feature 蒸馏家族**——不再只对齐输出，而是对齐 teacher 多个 stage 的中间表征。推荐 **OFD（Overhaul of Feature Distillation, Heo ICCV19）为默认**，FitNets 作轻量版，Review-KD / MGD 为高阶变体。

## 结构
| 方法 | 对齐对象 | adapter | student 深度要求 | 备注 |
|---|---|---|---|---|
| **FitNets**（Romero ICLR'15） | 单点 hint（"middle"层） | 1×1 conv 升降维 | student 比 teacher 略窄即可 | 最轻量；1 对 1 hint |
| **OFD**（Heo ICCV19，推荐默认） | 多 stage（teacher 全部 transition） | 每 stage 1×1 + ReLU 重建器 | student 需有 ≥2 个可对齐 stage | margin-based reconstruction loss |
| **Review-KD**（Chen CVPR21） | 反向多 stage（student → teacher 方向） | 融合抽象（ABF）+ 阶梯连接 | student 需有 ≥2 stage | 抽象度反向，FLOPs 略高 |
| **MGD**（Yang ECCV22） | feature + 随机掩码重建 | 1×1 + 生成式解码 | student ≥1 stage | "Generative"式蒸馏，正则强 |

**OFD 默认配方**（Phase2 默认选 OFD 除非有理由）：
1. 注册 teacher 多 stage hook（CONTRACTS §1 `feature_hook_names`），student 同名 hook。
2. 每个 stage：student feature → 1×1 conv 对齐通道 → ReLU重建器 → 与 teacher feature 做 margin-`max(0, d−m)` L2（margin m=0.5 默认）。
3. 总损失：`L = task_loss + λ_ofd · Σ_stage L_OFD(stage)`，`λ_ofd ∈ [0.1, 0.5]`。

## 为什么降时延
1. 同 D12：纯损失项，**部署期零开销**。
2. 比 D11（输出级 MSE）更细粒度：teacher 的中间层抽象（频域边缘特征、symbol 间相关性）被强制迁移，student 可以更小、层数更少。
3. 多 stage 对齐让 student 能用"浅但宽"的结构替代 teacher 的"深且窄"，利于 Cube 利用率（昇腾友好）。

## 昇腾友好性
**✅✅ friendly** —— 训练期只多几个 1×1 conv adapter（部署丢弃）；student 自身的昇腾友好性看具体结构（D18 ConvNeXt-pointwise / D19 MLP-Mixer 等）。OFD 不引入 attention / 不改 data layout。

## 物理依据
**间接（表征迁移）** —— teacher 学到的多 stage 抽象对应 OFDM 信号的逐级解相关（时频 → 信道估计 → 符号恢复），student 对齐这些表征即继承物理先验链。**无显式物理约束注入**。

## bundle 的 move
**M-OFD**（默认）/ **M-FitNets**（轻量）+ **M14**（输出级 MSE 保留）+ **M-rel**（D12 可同存，正交）+ **M16**（INT8 PTQ student）+ **student 方向**（D18/D19/D20 任选）。

## 结构前提与坑
1. **OFD 需 student ≥2 个可对齐 stage** —— 单 block student 退化成 FitNets；CONTRACTS §1 要求 `feature_hook_names() → list[str]` 返回 ≥1 个，**OFD 推荐返回 ≥2 个**。
2. **跨架构需 1×1 adapter** —— Transformer teacher 的 feature 形状 `[B, P, F, S, C_t]` vs conv student `[B, C_s, F, S]`：必须 permute + 1×1 conv 升降维（`kd/losses.py` 的 `ofd_feature_loss` 已内置 adapter，不落盘、训练期丢弃）。**不要手写 reshape 漏掉 permute**。
3. **FitNets 轻量版适用条件** —— student 容量极小（<teacher 10% 参数）或只跑短训 proxy（kd-nas Phase1 sweep）时，OFD 的多 stage 算力浪费，用 FitNets 单点 hint 即可。
4. **MGD 的随机掩码是正则项** —— MGD 掩码率 50% 默认；过低退化为 FitNets，过高 student 学不动；掩码只在训练期，部署 student 推理时不掩。
5. **Review-KD 反向抽象** —— student 中间层反过来监督 teacher 的浅层；要求 student 比 teacher 深，**不适用**于我们的 model8（student 通常更浅）。默认跳过 Review-KD。
6. **margin m 是个轴** —— OFD 默认 m=0.5；m=0 等价于纯 MSE，m 过大会让 loss 恒为 0；候选 `{0.1, 0.3, 0.5, 0.7}`。
7. **teacher feature 缓存策略** —— CONTRACTS §3 `TeacherCache` 一次性跑 proxy 集存 (out, feats)；feats 是 list 顺序与 `feature_hook_names` 严格对应，**位置错位会静默错对齐**——`kd/wrapper.py` 必须按 hook 注册顺序返回，engineer 不允许重排。
8. **fail-loud**：若 OFD loss 数量级 << task loss（1e-3 倍以下），多半是 adapter 没 init（默认随机 init 会让初期 loss 极小）——给 adapter 用 `nn.init.eye_` 或加 warmup（前 2 epoch λ_ofd 线性 ramp 0→目标）。

## 来源
- FitNets：Romero et al., ICLR 2015 —— [arXiv:1412.6550](https://arxiv.org/abs/1412.6550) "FitNets: Hints for Thin Deep Nets".
- OFD：Heo et al., ICCV 2019 —— [arXiv:1904.01866](https://arxiv.org/abs/1904.01866) "A Comprehensive Overhaul of Feature Distillation".
- Review-KD：Chen et al., CVPR 2021 —— [arXiv:2104.09044](https://arxiv.org/abs/2104.09044).
- MGD：Yang et al., ECCV 2022 —— [arXiv:2205.01509](https://arxiv.org/abs/2205.01509) "Masked Generative Distillation".

# D14 · kd_teacher_assistant_takd（TAKD 两段式化解 capacity gap）

> 一句话定位：当 teacher 远大于 student（如 6-block Transformer → 2-block conv），直接 KD 会有 **capacity gap**——student 容量不够装不下 teacher 的知识，反而学崩。TAKD 引入 **Teacher Assistant (TA)**：`teacher → TA → student` 两段式蒸馏，TA 容量介于 T/S 之间，平滑过渡。

## 结构
- **Teacher（T）**：6-block SignalProcessingTransformer（CONTRACTS §7），已训好冻结。
- **Teacher Assistant（TA）**：中等容量模型（如 4-block Transformer 或宽 conv），**先被 T 蒸馏一次**得到 TA_ckpt。
- **Student（S）**：最终部署的小模型（D1/D18/D19/D20），**被 TA 蒸馏**（不是被 T 蒸馏）。
- **落到 model8 的损失项形式**：
  - Stage 1：`L_TA = task_loss + λ · MSE(TA_out, T_out) [+ OFD(TA_feats, T_feats)]`
  - Stage 2：`L_S = task_loss + λ · MSE(S_out, TA_out) [+ OFD(S_feats, TA_feats)]`
  - 两段独立训练，TA 训完后冻结作为 stage 2 的 teacher。

## 为什么降时延
1. capacity gap 大时（teacher 10× student），直接 KD 反而让 student 学到"过度平滑"的软目标——TAKD 分两步，每步 gap ≤ 3×，student 能真正吸收。
2. 训练成本翻倍（训 TA + 训 S），但 student 部署时延**不变**（TA 也不部署）。
3. kd-nas workflow 中可用于 finalize 阶段（小批量精确调优），不适合 Phase1 sweep（成本太高）。

## 昇腾友好性
**✅ friendly** —— 纯训练期 trick，student 结构不变。TA 的结构选择建议同 student 家族（如 student 是 conv-only，TA 也选 conv-only 仅加宽 channel/embed_dim）——避免 stage 2 跨架构对齐复杂度。

## 物理依据
**无** —— TAKD 是优化动力学的 smoothing，无 OFDM 物理注入；物理先验完全来自 TA / student 自身结构（见 D1/D6/D10）。

## bundle 的 move
**M-TAKD**（两段式蒸馏）+ **M14**（每段内的输出级 MSE）+ **M-OFD**（每段内的 feature KD，可选）+ student 方向（D1/D18/D19/D20）。

## 结构前提与坑
1. **TA 容量选择经验** —— `C_TA ≈ √(C_T · C_S)`（参数量几何平均）。如 teacher 8M / student 0.5M → TA ≈ 2M。TA 太接近 T 失去意义、太接近 S 无法 smooth gap。
2. **capacity gap 判定** —— `C_T / C_S > 5` 时考虑 TAKD；<3 时直接 KD（D11）即可，TAKD 反而徒增训练成本。kd-nas workflow 应在 hypothesizer 里读 `teacher_meta.params / student_cfg.params` 比值自动路由。
3. **TA 训练 epoch** —— TA 不必训到收敛，**short-cycle** 即可（如 teacher 训练 epoch 的 60-70%）；TA 精度差几个 dB 不影响 stage 2。
4. **TA 结构选择** —— 推荐与 student **同家族**（student=ConvNeXt-pointwise，TA=更宽的 ConvNeXt-pointwise），跨架构 TA 会把 stage 2 的 KD 变成跨架构 KD，复杂度叠加。
5. **fail-loud**：若 stage 2 student 精度比直接 KD 还差，多半是 TA 过拟合 teacher 的软目标（stage 1 epoch 过多）——减少 stage 1 epoch 或在 stage 1 加 task loss 权重。
6. **online vs offline** —— TAKD 是 offline（TA 训完再训 S）；不要尝试 online TAKD（同时训 T/TA/S），梯度链不稳且无收益。
7. **与 Mean-Teacher EMA（D15）的区别** —— Mean-Teacher 是 student 自身的 EMA 副本当 teacher（无独立 TA 网络）；TAKD 是真实独立 TA 网络。两者正交，可叠加（TA 也维护 EMA）但收益边际递减。

## 来源
- capacity mismatch 反结论：Cho & Hariharan, 2019 "On the Efficacy of Knowledge Distillation" —— 指出大 teacher 反而蒸馏效果差。
- TA 缓解：Mirzadeh et al., 2020 "Improved Knowledge Distillation via Teacher Assistant" —— [arXiv:1902.03393](https://arxiv.org/abs/1902.03393).

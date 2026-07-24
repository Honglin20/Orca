# D11 · kd_to_conv_student（知识蒸馏到 conv-only student）

> 一句话定位：把**预训练 teacher**（D0 Transformer）用**输出级 MSE 蒸馏**到 conv-only student —— 99.3% 参数减 / 97% 时延减 / 0.5dB 精度损，student 是**结构变体**（非 teacher 的剪枝版）。

## 结构
- **Teacher**：**预训练好的** D0 Transformer 接收机（权重已训练，作为蒸馏监督源）。
- **Student**：conv-only 结构变体（参考 D1/D6），**从头训练**，损失 = `α·MSE(student_out, teacher_out) + (1−α)·MSE(student_out, label)`。
- **输出**：与 teacher 同接口（LLR / CSI）。
- **attention?**：**no**（student 是 conv-only）。

## 为什么降时延
1. student 是 conv-only → 消除全部 attention / TransData（同 D1）。
2. 实测 **99.3% 参数减 / 97% 时延减 / 仅 0.5dB 精度损**。
3. teacher 在部署期**完全不上线** —— 推理时延只看 student。

## 昇腾友好性
**✅✅ friendly** —— student 是 conv-only，所有 D1 的 GEMM-land 收益直接继承；无 attention、无 TransData。

## 物理依据
**间接（no 显式）** —— student 的物理先验来自其 conv-only 结构（如 D1 的 dilation 局部性），KD 本身不注入物理；teacher 学到的物理表示通过 MSE 隐式传递。

## bundle 的 move
**M14**（KD 成 conv-only student）+ **M1/M2/M3**（融合层叠加 student）+ **M16**（INT8 PTQ student，进一步 2× Cube）。

## 结构前提与坑
1. **输出级 MSE，不是 logit-KL** —— 无线接收机输出是连续 LLR/CSI（非分类 logit），KL 散度不适用；论文明确用 MSE 作为蒸馏损失。
2. **必须有预训练 teacher 权重** —— 没有训好的 teacher，此方向不可启动；teacher 质量直接决定 student 上限。
3. **student 是结构变体，不是 teacher 剪枝** —— student 架构可完全不同于 teacher（如 Transformer teacher → conv student），KD 跨架构。
4. **OOD 检测** —— student 在训练分布外（low SNR、新信道模型）可能塌；需配 SNR-aware 早停或 MoE（M29）补。
5. α 混合权重需 sweep —— α=1（纯 KD）易过平滑，α=0（纯 label）退化为从头训；典型 α∈[0.3, 0.7]。
6. 与 D3（部署期折叠）的区别：D3 是同一网络训练-部署形态切换，D11 是两个网络（teacher 部署期丢弃）。

## 来源
Zhu, MDPI Sensors 2024（KD 到 conv student，无线接收机应用）—— plan §10 标注复现面薄。

# D15 · kd_mean_teacher_ema（Mean-Teacher EMA 自蒸馏）

> 一句话定位：**不需要外部 teacher**——student 自身的 EMA 影子副本当 teacher，对无标注/低 SNR 样本做一致性正则。与外部 teacher KD（D11-D14）**正交**，低 SNR 场景的稳定器。

## 结构
- **Student**：正常训练的模型（任意结构）。
- **EMA 影子**：同一结构的权重 EMA 副本（decay 0.999），每 step `ema_p ← decay·ema_p + (1−decay)·student_p`。
- **一致性损失**：`L_cons = MSE(student_out, ema_out.detach())`，student 输出与 EMA 影子输出对齐。
- **落到 model8 的损失项形式**：
  ```
  L = task_loss(x, y) + λ_cons · MSE(student_out(x), ema_out(x).detach()) [+ λ_KD · MSE(student_out, teacher_out) 若有外部 teacher]
  ```
  - `λ_cons ∈ [0.1, 1.0]`，低 SNR 场景偏大（0.5+）。
  - **无线增广**：对 `x` 做相位旋转 `x' = x·e^{jθ}`（θ∼U(0,2π)）或噪声扰动 `x' = x + σn`，让 student(增广) 与 EMA(原始) 一致——物理对齐：接收机对相位旋转应不变。

## 为什么降时延
1. **部署期零开销**——EMA 影子不部署，student 单独推理。
2. **低 SNR 稳定器**：label 在低 SNR 下噪声大，EMA 影子提供平滑的软目标，抑制训练抖动。
3. **与所有外部 teacher KD 正交**：可叠加 D11/D12/D13/D14。

## 昇腾友好性
**✅✅ friendly** —— 训练期只多一次 student forward（EMA 副本，无梯度）+ 权重 EMA 更新（向量加，开销可忽略）。部署期 student 结构不变。

## 物理依据
**yes（相位不变性）** —— OFDM 接收机对公共相位旋转（CFO residual）应不变；用相位增广 + EMA 一致性把这个不变性烤进 student。比纯噪声扰动更有物理含义。噪声扰动等价于 SNR augmentation。

## bundle 的 move
**M-EMA**（Mean-Teacher 一致性）+ **M-aug**（相位旋转/噪声扰动增广）+ **M14**（外部 teacher MSE，可同存）+ student 方向任选。

## 结构前提与坑
1. **decay 是个轴** —— `decay ∈ {0.99, 0.999, 0.9999}`；低 SNR / 长 epoch 选大 decay（0.999+）；短训（kd-nas proxy）选 0.99 让影子快速跟上。
2. **EMA 副本必须 detach** —— `ema_out` 反向传播只走 student，不写影子（否则梯度链爆炸）；CONTRACTS §3 `MeanTeacherEMA.forward` 内部不接入 autograd 图，但 loss 计算时仍需 `.detach()` 保险。
3. **EMA 更新时机** —— 在 `optimizer.step()` **之后** 更新（更新的是最新的 student 权重）；CONTRACTS §3 `MeanTeacherEMA.update(student)` 的调用顺序由 `train_kd.py` adapter 控制。
4. **相位增广实现** —— 实数 feature map 上做相位旋转：`(I, Q) → (I·cosθ − Q·sinθ, I·sinθ + Q·cosθ)`，real/imag 解耦后用 2×2 矩阵广播；**不要**用复数 native op（昇腾无原生复数，见 M22）。
5. **与 D14 TAKD 的区别** —— Mean-Teacher 是同一网络的影子（无独立模型），TAKD 是独立 TA 网络中转。Mean-Teacher 便宜得多，作为低 SNR 正则首选；capacity gap 主问题留给 TAKD。
6. ** BatchNorm 坑** —— EMA 副本若共享 BN running stats 会被 student 的更新污染；CONTRACTS §3 `MeanTeacherEMA` 应**独立拷贝** BN running_mean/var（`update()` 时拷贝，不是 EMA）—— engineer 实现时检查这点。
7. **λ_cons warmup** —— 前 5-10 epoch `λ_cons` 线性 ramp 0→目标，避免初期影子未稳定时一致性 loss 把 student 拽偏。
8. **fail-loud**：若 `ema_out` 数值发散（NaN/Inf），检查 decay 是否过大 + student 是否用 BN（EMA 与 BN 冲突是常见 bug）；可切 SyncBN 或独立 BN。

## 来源
- Mean-Teacher：Tarvainen & Valpola, NeurIPS 2017 —— [arXiv:1703.01780](https://arxiv.org/abs/1703.01780) "Mean teachers are better role models".
- 无线场景一致性正则：接收机自监督相关文献多引用此方法作 low-SNR regularizer。

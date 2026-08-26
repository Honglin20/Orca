# D17 · kd_failure_modes（KD 何时反而无效 / 基线对照必须做）

> 一句话定位：**KD 不是免费午餐**——记录 KD 反而比直接训小模型更差的 failure mode，并强制 kd-nas workflow 的 baseline 对照流程。hypothesizer 在选 KD 前必须读本卡。

## KD 反而无效的典型场景

### 1. Capacity Gap（Cho & Hariharan 2019）
- **现象**：teacher >> student（参数比 > 10×），student 装不下 teacher 的软目标，蒸馏后 student 精度**低于**直接从头训。
- **机制**：teacher 软目标过度平滑 / student 容量不足以同时拟合 task label 和 teacher 软目标，两者互相干扰。
- **对策**：用 D14 TAKD 引入 TA 中转；或缩小 teacher（如用 4-layer teacher 而非 6-layer）。
- **kd-nas 判定**：读 `teacher_meta.params / student_cfg.params`，比值 > 5 时**强制**走 TAKD 或报警。

### 2. KD 是正则不是传知识（Stanton 2021）
- **现象**：在很多任务上，KD 提升的来源其实是**额外的正则化效果**（类似于 label smoothing），而非真正的 "dark knowledge" 迁移。直接给 student 加同强度正则（如 dropout 加大、weight decay 加大）能达到同样效果。
- **机制**：teacher 软目标对 student 等效于一种 prior，小 dataset 上正则收益掩盖了知识迁移收益。
- **对策**：**必须做 baseline 对照**——直接训 student（无 KD）vs student + KD，同 epoch / 同优化器；只报 KD 净增益。
- **kd-nas 判定**：Phase1 sweep 必须包含一个 "no-KD baseline"（`kd_losses=["mse"]`，λ_KD=0），作为阈值标定。

### 3. Teacher 过拟合训练分布
- **现象**：teacher 在训练 SNR 分布上过拟合（如只训过高 SNR），蒸馏后 student 在低 SNR 反而崩。
- **对策**：teacher 训练时必须覆盖完整 SNR 范围（CONTRACTS §7 teacher_setup 用 proxy_dataset_spec 控制）；或加 D15 Mean-Teacher 低 SNR 正则。

### 4. OOD 检测缺失
- **现象**：student 在训练分布外（新信道模型 / 新 SNR 区间）输出不可信，但 KD 没有告警机制。
- **对策**：部署期加 OOD 检测（如 student 输出能量 / 与最近训练样本的距离）；或 MoE（M29）按 SNR 分桶多 expert。

### 5. 软目标与硬标签冲突
- **现象**：teacher 软目标在某些样本上与硬 label 不一致（teacher 错的样本），高 λ_KD 会把 student 拉错。
- **对策**：sample-adaptive λ（teacher confidence 低时降权）；或 robust KD loss（只惩罚 teacher 高置信度的样本）。

## 何时直接训小模型 > KD（决策树）

```
if teacher_ckpt 不存在 or 质量未验证:
    → 直接训 student（KD 无意义）
elif capacity_ratio > 10:
    → 走 D14 TAKD（不能直接 KD）
elif dataset_size < 10k or SNR 覆盖不全:
    → 先扩数据 / 加 Mean-Teacher D15，KD 收益边际小
elif student 容量接近 teacher (ratio < 3):
    → KD 收益小，先 baseline 再考虑
else:
    → KD 可用，但仍必须做 baseline 对照
```

## kd-nas workflow 强制基线对照流程

**Phase1 必须包含两个 baseline**：
1. `no_kd_baseline`：student + 纯 task loss（λ_KD=0），同 epoch。
2. `teacher_distill_only`：student + 仅输出级 MSE（D11），无 feature / 关系级 KD。

**finalize 裁定时**：
- 若 champion 的 `proxy_mse` < `no_kd_baseline.proxy_mse − ε`（ε=0.005 默认），才允许声称 "KD 有效"；
- 否则 finalize 输出 `loop_back=true`，hypothesizer 换方向（KD 在该 family 上无收益）。

**proxy↔真实 dB 阈值标定**（D21 meta 方向详述）：短训 proxy 的 soft-MSE-vs-teacher 与 finalize 全量测量的 dB gap 必须**先做一次标定**（线性回归拟合 `dB_gap_real ≈ a·proxy_mse + b`），否则 proxy 与真实精度相关性不成立，所有结论失效。

## bundle 的 move
**无**（本卡是 failure mode 参考，不引入新 move）。**hypothesizer 必须在 SelectionSpec 的 `rationale` 字段**显式说明"已读 D17，KD 适用判定通过"或"走 TAKD / baseline 对照"。

## 结构前提与坑
1. **baseline 对照不能省** —— 即使论文上 KD 看起来适用，model8 的具体数据分布、teacher 质量可能导致 KD 失效；每次跑 KD 必须同条件跑 no-KD baseline。
2. **不要把"训不动"误判为"KD 不适用"** —— 先检查 lr / batch / optimizer 等 training issue；KD 不背锅。
3. **短训 proxy 不代表最终** —— Phase1 的 proxy_mse 只用于**相对排序**（family 间比较），不用于绝对结论；绝对判定必须 finalize 全量。
4. **fail-loud**：若任何一轮 KD 实验 `proxy_mse > no_kd_baseline.proxy_mse * 1.5`（KD 比纯训差 50%+），analyst 必须在 attribution 里写明原因并建议换方向。

## 来源
- Cho & Hariharan, 2019 "On the Efficacy of Knowledge Distillation"（capacity gap 原始观测）。
- Stanton et al., 2021 "Does knowledge distillation really work?" —— [arXiv:2106.05945](https://arxiv.org/abs/2106.05945).

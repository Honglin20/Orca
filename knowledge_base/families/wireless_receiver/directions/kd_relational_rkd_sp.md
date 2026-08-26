# D12 · kd_relational_rkd_sp（关系级 KD 家族：RKD / SP / CC）

> 一句话定位：**task-agnostic 的关系级蒸馏**——不蒸馏 teacher 的绝对输出/特征，而是蒸馏 **样本两两关系**（pairwise distance / angle / 相似度 / 相关矩阵），作为回归任务里 "dark knowledge" 的替代物。一个 family 卡片覆盖三兄弟：RKD（Park CVPR19）/ SP（Tung ICCV19）/ CC（Peng ICCV19）。

## 结构
- **Teacher / Student**：任意结构（teacher Transformer → student conv 也行），**只要能取到 feature 即可**——输出级 / 中间层 feature 都行。
- **关系构造**（minibatch 内 N 样本）：
  - **RKD-D（distance）**：`D^t_ij = ‖f^t_i − f^t_j‖₂`，对齐 `D^s ≈ D^t`（student 学到的样本距离结构）。
  - **RKD-A（angle）**：三角角度 `∠(f_i, f_j, f_k)` 对齐。
  - **SP**：Gram 矩阵 `G^t = f^t (f^t)ᵀ`（N×N），对齐 `G^s ≈ G^t` —— 保留 pair-wise 相似度结构。
  - **CC（correlation congruence）**：对 batch 内 feature 协方差矩阵做对齐。
- **落到 model8 的损失项形式**（Phase2 engineer 必须逐字对齐）：
  - feature 展平前先 **real/imag 解耦**：`f = cat([f.real, f.imag], dim=-1)`，避免复值范数对不齐 PyTorch autograd。
  - 每 batch 至少 N≥16 才能 bond 一个稳定的 pair-set；推荐 **batch SNR 分桶采样**（同 batch 内固定 3-4 个 SNR 点各采 4-8 样本，让跨 SNR 关系被蒸馏）。
  - 总损失：`L = task_loss + λ_rel · (RKD-D + RKD-A) [+ λ_SP · SP_loss]`，`λ_rel ∈ [0.05, 0.2]`（关系项数值大，权重小）。

## 为什么降时延
1. 关系级 KD 不增加 student 任何结构/算子开销——纯损失项，**部署期 student 单独跑，零开销**。
2. teacher 学到的"样本相对距离结构"被迁移到 student，相当于把 teacher 对 SNR/信道多径的内部表征几何压进小模型——可允许 student 更小而不掉精度。
3. 与输出级 MSE（D11）正交，可叠加（M14 + M-rel）。

## 昇腾友好性
**✅✅ friendly** —— 关系项只在训练期计算（matmul + broadcast），部署期 student 结构不变。训练期开销 = teacher forward（已由 TeacherCache 一次性预计算，CONTRACTS §3）+ batch 内 pairwise matmul（O(N²·C)，N=16-32 量级，可忽略）。

## 物理依据
**间接（结构对应）** —— batch SNR 分桶 + pairwise distance 对齐，隐含"teacher 对不同 SNR 样本的内部几何排布"是信道质量梯度的代理；student 学到这个几何即继承了 SNR-感知能力。**无显式 OFDM 物理**，但与 multi-path 时延扩展的结构性耦合可由分桶采样隐式捕获。

## bundle 的 move
**M-rel（RKD/SP/CC 关系级 KD）** + **M14**（输出级 MSE，可同存）+ **M15-M18**（student 自身的 conv-only / 3-grid / dilation moves）+ **M16**（INT8 PTQ student）。

## 结构前提与坑
1. **batch 内 N 不能太小** —— pairwise 需要 N≥16 才统计稳定；batch=8 时 RKD 退化成噪声。**昇腾端若显存吃紧，用梯度累积保持 pair-set 大小**（累积期间不 forward teacher，整批一次性 forward）。
2. **复值 feature 处理 fail-loud** —— model8 中频域 feature 是复值；直接 `‖f‖` 在 PyTorch 下会 `view_as_real` 失败或拿不到稳定梯度。**必须先 real/imag 解耦**（concat 最后维）或取模 `abs().clamp(min=1e-6)`。
3. **SNR 分桶采样是关键** —— 随机 batch 的 pair-set SNR 跨度小，关系项退化为常量；必须把同 batch 内样本按 SNR 分桶（4 桶 × 8 样本），让 pair-set 跨越 SNR 梯度。
4. **teacher 特征必须 detach** —— 否则反向传播会写入 teacher（teacher 应冻结）。CONTRACTS §3 已强制 `kd/losses.py` 内部 detach，不要绕过。
5. **权重数量级** —— RKD-D 的绝对值常是 MSE 的 10-100 倍（距离平方和），λ_rel 必须 ≤0.2 否则吞掉 task loss；先 log 一次两项的数值再调权重。
6. **SP / CC 异构对齐** —— SP 的 Gram 矩阵对 student 通道数不敏感（只看 N×N），跨架构（Transformer teacher → conv student）无需 1×1 adapter——这是它比 OFD（D13）方便的点。
7. **与 D13（特征级 OFD）的取舍** —— RKD/SP 需要大 batch（≥16）但无需 stage 对齐；OFD 需要多 stage hook 但 batch 容忍度高。batch 受限选 OFD，batch 充裕选 RKD/SP。

## 来源
- RKD：Park et al., CVPR 2019 —— [arXiv:1904.05068](https://arxiv.org/abs/1904.05068) "Relational Knowledge Distillation".
- SP：Tung & Mori, ICCV 2019 —— [arXiv:1907.09682](https://arxiv.org/abs/1907.09682) "Similarity-Preserving Knowledge Distillation".
- CC：Peng et al., ICCV 2019 —— "Correlation Congruence for Knowledge Distillation"（跨架构友好的协方差对齐）。

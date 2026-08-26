# 02 · Training-free / Zero-cost Proxy 路线

## 定位：绕开超网训练
不训超网，用**一次 forward** 算个代理指标给候选架构打分。和 sandwich/KD 路线**互补**：proxy 粗筛数千候选 → supernet 精排。

## 经典 zero-cost 指标
TE-NAS、ZenNAS、ZiCo、NASWOT、SynFlow —— 都是「一次前向 + 不反传」即可算出架构潜力分。

## 最新（2024–2025）
- **AZ-NAS (CVPR 2024)** ⭐：识别 zero-cost proxy 的 4 个关键属性，把 ZiCo/TE-NAS/ZenNAS **组装**成统一打分器，效果超任何单一 proxy。
  - https://arxiv.org/pdf/2403.19232
- **TRNAS (ICCV 2025)**：training-free 做鲁棒性感知搜索。
  - https://openaccess.thecvf.com/content/ICCV2025/papers/Yang_TRNAS_A_Training-Free_Robust_Neural_Architecture_Search_ICCV_2025_paper.pdf
- **自动发现 proxy**：用 LLM / 符号回归 / genetic programming 自动设计新指标（不再靠人手工）。
  - https://openreview.net/forum?id=3naHyE5klE
- 持续索引仓库：https://github.com/MarttiWu/Training-Free-NAS

## 对 kd-nas 的启示
若搜索空间大、训练成本高，可在 supernet 训练前插一层 zero-cost 粗筛，减少 supernet 需要评估的候选数。注意：zero-cost proxy 衡量的是「可训练性 / 表达力」，与 KD 后的最终精度相关性需要单独验证。

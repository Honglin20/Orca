# rkd_loss.py.md — D12：RKD-D + RKD-A + SP 关系级 KD loss（带 SNR 分桶采样 hook）

> **这是什么 / 一句话**：D12 关系级 KD 家族的 PyTorch 可跑实现——RKD-D（pairwise distance 对齐）+ RKD-A（triplet angle 对齐）+ SP（Gram 矩阵相似度保持），**带 SNR 分桶采样 hook**（同 batch 内跨 SNR 的 pair-set 才有统计意义）。所有 teacher tensor 自动 detach；复值 feature 用 real/imag 解耦（不依赖 PyTorch native complex）。

---

## 可跑骨架（与 CONTRACTS §3 `kd/losses.py` 对齐）

```python
import torch
import torch.nn as nn
import torch.nn.functional as F


def _decouple_complex(f: torch.Tensor) -> torch.Tensor:
    """复值 feature real/imag 解耦：若最后一维是 2（real,imag）就 cat 成双倍通道。
    model8 中频域 feature 通常是实数张量（已 split），这里保守处理：
    - 若 f.is_complex() → view_as_real + flatten
    - 若 f 最后一维 == 2 → reshape 合并到 channel
    - 否则原样返回
    """
    if f.is_complex():
        f = torch.view_as_real(f)            # [..., 2]
        f = f.flatten(-2)                    # channel × 2
    elif f.dim() >= 2 and f.shape[-1] == 2:
        f = f.flatten(-2)
    return f


def rkd_distance_loss(s_feat: torch.Tensor, t_feat: torch.Tensor) -> torch.Tensor:
    """RKD-D: pairwise Euclidean distance 对齐（Park CVPR19）。
    输入: s_feat [N, C_s], t_feat [N, C_t]（N = batch 内样本数）
    输出: scalar loss
    """
    s = _decouple_complex(s_feat)
    t = _decouple_complex(t_feat).detach()        # teacher detach 强制

    # pairwise distance matrix [N, N]
    def pdist(f):
        diff = f.unsqueeze(0) - f.unsqueeze(1)    # [N, N, C]
        return torch.norm(diff, dim=-1)           # [N, N]

    d_s = pdist(s)
    d_t = pdist(t)
    # 归一化（每对距离除以 teacher 的 mean distance，让 scale 无关）
    d_s = d_s / (d_s.mean() + 1e-8)
    d_t = d_t / (d_t.mean() + 1e-8)
    return F.smooth_l1_loss(d_s, d_t)


def rkd_angle_loss(s_feat: torch.Tensor, t_feat: torch.Tensor) -> torch.Tensor:
    """RKD-A: triplet angle 对齐（Park CVPR19）。
    对每三个样本 (i,j,k) 计算 angle(i,j,k) 并对齐。
    """
    s = _decouple_complex(s_feat)
    t = _decouple_complex(t_feat).detach()

    def tangle(f):
        # vectors: v_ij = f_j - f_i, v_ik = f_k - f_i
        v_ij = f.unsqueeze(0) - f.unsqueeze(1)     # [N, N, C]  (j-axis)
        v_ik = f.unsqueeze(0) - f.unsqueeze(1)     # [N, N, C]  (k-axis)
        # 对每 (i,j,k) 三元：cos ∠(i,j,k) = <v_ij, v_ik> / (|v_ij|·|v_ik|)
        dot = (v_ij.unsqueeze(1) * v_ik.unsqueeze(2)).sum(-1)   # [N(j), N(k), N(i)]? 简化实现
        norm_ij = torch.norm(v_ij, dim=-1)         # [N, N]
        norm_ik = torch.norm(v_ik, dim=-1)         # [N, N]
        return dot, norm_ij, norm_ik

    # 简化版：只对随机采样的 triplets 计算（避免 O(N^3) 内存）
    N = s.shape[0]
    if N < 3:
        return torch.tensor(0.0, device=s.device)
    idx_i = torch.randint(0, N, (N,), device=s.device)
    idx_j = torch.randint(0, N, (N,), device=s.device)
    idx_k = torch.randint(0, N, (N,), device=s.device)

    def angle_single(f, i, j, k):
        v_ij = f[j] - f[i]
        v_ik = f[k] - f[i]
        cos = (v_ij * v_ik).sum(-1) / (torch.norm(v_ij, dim=-1) * torch.norm(v_ik, dim=-1) + 1e-8)
        return cos

    cos_s = angle_single(s, idx_i, idx_j, idx_k)
    cos_t = angle_single(t, idx_i, idx_j, idx_k)
    return F.smooth_l1_loss(cos_s, cos_t)


def sp_similarity_loss(s_feat: torch.Tensor, t_feat: torch.Tensor) -> torch.Tensor:
    """SP: Gram 矩阵相似度保持（Tung ICCV19）。
    对齐 student/teacher 的 batch 内相似度矩阵 G = f · f^T。
    跨架构友好（对通道数不敏感）。
    """
    s = _decouple_complex(s_feat)
    t = _decouple_complex(t_feat).detach()

    g_s = s @ s.t()                 # [N, N]
    g_t = t @ t.t()                 # [N, N]
    # 归一化（scale 无关）
    g_s = g_s / (g_s.norm() + 1e-8)
    g_t = g_t / (g_t.norm() + 1e-8)
    return F.smooth_l1_loss(g_s, g_t)


# ============================================================
# SNR 分桶采样 hook（关键：保证 batch 内跨 SNR 的 pair-set）
# ============================================================
class SNRBucketSampler:
    """从 dataloader 的 batch 里按 SNR 分桶采子 batch，让同一 minibatch 内包含多 SNR。
    使用方式（train_kd.py adapter 内）:
        sampler = SNRBucketSampler(snr_list=[-5, 0, 5, 10], per_bucket=4)
        for batch in dataloader:
            sub_x, sub_y, sub_snr = sampler.draw(batch)
            # ... 用 sub_x 跑 teacher/student forward + RKD loss
    """
    def __init__(self, snr_list, per_bucket=4):
        self.snr_list = snr_list
        self.per_bucket = per_bucket

    def draw(self, batch):
        """batch = (x, y, snr_label) —— 假设 dataloader 已经标了每样本的 SNR。
        返回: sub_x [N=num_bucket*per_bucket, ...], sub_y, sub_snr
        """
        x, y, snr_label = batch
        sel_idx = []
        for snr in self.snr_list:
            mask = (snr_label == snr)
            idx = torch.nonzero(mask).squeeze(-1)
            if len(idx) >= self.per_bucket:
                perm = torch.randperm(len(idx))[:self.per_bucket]
                sel_idx.append(idx[perm])
            else:
                # 该 SNR 样本不足，全选 + 重复采样（不报错，fail-soft）
                sel_idx.append(idx.repeat(self.per_bucket // max(len(idx),1) + 1)[:self.per_bucket])
        sel_idx = torch.cat(sel_idx)
        return x[sel_idx], y[sel_idx], snr_label[sel_idx]


def rkd_combined_loss(s_feat, t_feat, w_d=1.0, w_a=0.5, w_sp=0.0):
    """组合：默认 RKD-D + RKD-A；SP 可选（跨架构时启用）。
    权重数量级提醒：RKD-D / RKD-A 的绝对值常是 MSE 的 10-100 倍，
    所以 w_d/w_a 应该 ≤ 0.2，否则吞掉 task loss。
    """
    loss = w_d * rkd_distance_loss(s_feat, t_feat)
    if w_a > 0:
        loss = loss + w_a * rkd_angle_loss(s_feat, t_feat)
    if w_sp > 0:
        loss = loss + w_sp * sp_similarity_loss(s_feat, t_feat)
    return loss


if __name__ == "__main__":
    # smoke test
    N, C_s, C_t = 16, 64, 128
    s = torch.randn(N, C_s, requires_grad=True)
    t = torch.randn(N, C_t)
    loss = rkd_combined_loss(s, t, w_d=0.1, w_a=0.05, w_sp=0.05)
    loss.backward()
    print(f"RKD loss = {loss.item():.4f}, s.grad.norm = {s.grad.norm().item():.4f}")
```

---

## 变异提示（不要照抄）

- **权重 sweep**：`(w_d, w_a, w_sp) ∈ {(0.1, 0.05, 0), (0.2, 0.1, 0), (0, 0, 0.1), (0.1, 0.05, 0.05)}`；先单独跑 RKD-D / SP 看哪个有效再组合。
- **SNR 分桶的 SNR_list**：默认 `[-5, 0, 5, 10]`；若训练数据覆盖更广，加 `[-10, 15]`；若数据只覆盖 `[0, 10]`，缩到 `[0, 5, 10]`，但 pair-set 跨度小，RKD 退化。
- **per_bucket**：默认 4；显存够用提到 8（pair-set N=32 统计更稳）；显存紧张降到 2（N=8 警戒线）。
- **triplet 采样数量**：上述简化版用 N 个 triplet；若要全 O(N³) 在 N=16 下是 4096 triplet，开销可接受；用 `torch.combinations` 全采。
- **复值处理**：`_decouple_complex` 是保守实现；如果你的 pipeline 确保输入是实数 split 形式，可以直接删除这个分支省时间。
- **与 OFD（D13）的组合**：RKD 在 feature 上做，OFD 也在 feature 上做；两者都需 hook，可共用 hook 注册（`kd/wrapper.py`），loss 相加。
- **fail-loud 检查**：若 `rkd_distance_loss` 返回 NaN，多半是某个样本 feature 全 0（`d_s.mean() == 0` 触发除零）→ 加 `clamp(min=1e-6)` 或过滤零样本。
- **teacher detach 漏掉**：所有 teacher tensor 必须 `.detach()`；本骨架已加，但若改写时漏掉，反向传播会写 teacher（梯度爆炸 fail-loud）。

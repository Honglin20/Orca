# mean_teacher_ema.py.md — D15：Mean-Teacher EMA 影子权重 + 一致性 loss

> **这是什么 / 一句话**：D15 的可跑实现——student 自身的 EMA 副本当 teacher，对无标注/低 SNR 样本做一致性正则。复值特征用 real/imag 解耦做相位旋转增广；BN running stats 独立拷贝（不与 student 共享）；EMA 影子部署期不导出。

---

## 可跑骨架（对齐 CONTRACTS §3 `kd/ema.py::MeanTeacherEMA`）

```python
import torch
import torch.nn as nn
import torch.nn.functional as F
import copy


class MeanTeacherEMA(nn.Module):
    """student 的 EMA 影子副本。
    - 影子权重 decay=0.999（默认）；每 step update。
    - BN running_mean / running_var **独立拷贝**（不 EMA，直接 copy_）。
    - forward 返回影子输出（不接 autograd 图）。

    使用:
        ema = MeanTeacherEMA(student, decay=0.999).to(device)
        # 训练循环:
        for batch in dataloader:
            s_out = student(x)
            with torch.no_grad():
                ema_out = ema(x)
            cons_loss = F.mse_loss(s_out, ema_out)
            total_loss = task_loss + lambda_cons * cons_loss
            total_loss.backward()
            optimizer.step()
            ema.update(student)        # 必须在 optimizer.step() 之后
    """
    def __init__(self, student: nn.Module, decay: float = 0.999):
        super().__init__()
        self.decay = decay
        # 深拷贝 student 作为影子；参数独立
        self.ema_model = copy.deepcopy(student)
        for p in self.ema_model.parameters():
            p.requires_grad_(False)
        self._init_bn_copy(student)

    def _init_bn_copy(self, student: nn.Module):
        """初始把 student 的 BN running stats 直接 copy 到 ema_model。"""
        bn_s = [m for m in student.modules() if isinstance(m, (nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d))]
        bn_e = [m for m in self.ema_model.modules() if isinstance(m, (nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d))]
        assert len(bn_s) == len(bn_e), "student / ema BN 数不一致"
        for s, e in zip(bn_s, bn_e):
            e.running_mean.copy_(s.running_mean)
            e.running_var.copy_(s.running_var)

    @torch.no_grad()
    def update(self, student: nn.Module):
        """EMA 更新影子权重 + 拷贝 BN running stats。
        必须在 optimizer.step() 之后调用（更新最新 student 权重）。
        """
        # 参数 EMA
        s_params = list(student.parameters())
        e_params = list(self.ema_model.parameters())
        assert len(s_params) == len(e_params), "student / ema 参数数不一致（结构变了？）"
        for s, e in zip(s_params, e_params):
            e.data.mul_(self.decay).add_(s.data, alpha=1 - self.decay)
        # BN running stats 直接 copy（非 EMA；与 Tarvainen 原作一致）
        bn_s = [m for m in student.modules() if isinstance(m, (nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d))]
        bn_e = [m for m in self.ema_model.modules() if isinstance(m, (nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d))]
        for s, e in zip(bn_s, bn_e):
            e.running_mean.copy_(s.running_mean)
            e.running_var.copy_(s.running_var)

    @torch.no_grad()
    def forward(self, x):
        """影子前向，输出 detached tensor。"""
        self.ema_model.eval()
        return self.ema_model(x)


# ============================================================
# 相位旋转增广：实数 feature map 上的 2×2 旋转矩阵
# ============================================================
def phase_rotate(x: torch.Tensor, theta: torch.Tensor = None) -> torch.Tensor:
    """对 [B, P, F, S, 2]（最后一维是 real, imag）做相位旋转。
    x[..., 0] = I, x[..., 1] = Q
    (I, Q) → (I·cosθ − Q·sinθ, I·sinθ + Q·cosθ)
    theta: [B] 或 scalar，弧度；默认随机 U(0, 2π)。
    """
    if theta is None:
        theta = torch.rand(x.shape[0], device=x.device) * (2 * torch.pi)
    # 广播 theta 到 x 的 shape
    shape = [x.shape[0]] + [1] * (x.dim() - 1)
    theta = theta.view(shape)
    cos_t = torch.cos(theta)
    sin_t = torch.sin(theta)
    I = x[..., 0]
    Q = x[..., 1]
    I_new = I * cos_t - Q * sin_t
    Q_new = I * sin_t + Q * cos_t
    return torch.stack([I_new, Q_new], dim=-1)


def consistency_loss(student, ema: MeanTeacherEMA, x, lambda_cons: float = 0.5,
                     use_phase_aug: bool = True):
    """一致性 loss + 可选相位增广。
    x: [B, P, F, S, 2]  (real/imag split)
    返回: total_cons_loss
    """
    if use_phase_aug and x.shape[-1] == 2:
        x_aug = phase_rotate(x)
    else:
        x_aug = x

    s_out = student(x_aug)           # student 看增广
    ema_out = ema(x)                 # ema 看原始
    return F.mse_loss(s_out, ema_out.detach()) * lambda_cons


# ============================================================
# λ_cons warmup scheduler
# ============================================================
class ConsWeightWarmup:
    """前 N epoch 线性 ramp 0 → target_lambda，避免初期影子未稳定时把 student 拽偏。"""
    def __init__(self, target: float = 0.5, warmup_epochs: int = 5):
        self.target = target
        self.warmup = warmup_epochs

    def __call__(self, epoch: int) -> float:
        if epoch >= self.warmup:
            return self.target
        return self.target * (epoch + 1) / self.warmup


# ============================================================
# smoke test
# ============================================================
if __name__ == "__main__":
    class TinyStudent(nn.Module):
        def __init__(self):
            super().__init__()
            self.fc = nn.Linear(8, 8)
            self.bn = nn.BatchNorm1d(8)
        def forward(self, x):
            return self.bn(self.fc(x))  # x: [B, 8]

    torch.manual_seed(0)
    student = TinyStudent()
    ema = MeanTeacherEMA(student, decay=0.99)
    x = torch.randn(4, 8)
    s_out = student(x)
    ema_out = ema(x)
    loss = F.mse_loss(s_out, ema_out.detach())
    print(f"consistency loss = {loss.item():.4f}")
    loss.backward()
    # 模拟 optimizer.step + ema.update
    for p in student.parameters():
        p.data -= 0.01 * p.grad
    ema.update(student)
    print("EMA updated; s/ema diff =", (list(student.parameters())[0] - list(ema.ema_model.parameters())[0]).abs().mean().item())
```

---

## 变异提示（不要照抄）

- **decay 是主轴**：`{0.99, 0.999, 0.9999}`；短训 proxy（kd-nas Phase1）用 0.99（快速跟上）；finalize 全量用 0.999。
- **λ_cons 主轴**：`{0.1, 0.3, 0.5, 1.0}`；低 SNR 场景偏大；OOD 样本多时偏小（避免影子也跑偏）。
- **相位增广可选**：对非 OFDM 任务（或特征已是纯实数）关闭；对 OFDM 强烈推荐（物理对应相位不变性）。
- **噪声扰动替代**：若不用相位增广，用 `x + σ·randn` 替代，σ 与训练集 SNR 匹配；不与相位增广叠加（会过度扰动）。
- **BN 独立拷贝的 fail-loud**：若 student 结构在训练中变（dynamic shape / 层增减），`update()` 的 BN 数会不匹配 → assert 失败；这是 fail loud 设计。
- **不部署 EMA**：`MeanTeacherEMA.ema_model` 不进 ONNX 导出；train_kd.py 完成后 `del ema` 再导 student.onnx。
- **与外部 teacher KD 叠加**：`total = task + λ_KD·mse_kd(s,t) + λ_cons·consistency_loss(s, ema)`；两者正交，权重独立调。
- **SyncBN 兼容**：多卡训练时 student 用 SyncBN；ema_model 也应 SyncBN，但 BN running stats 的 copy_ 在多卡下要 all_reduce（用 `torch.distributed.broadcast`）。

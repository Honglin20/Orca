# lmmse_front_shared_port.py.md — M11/M26/D2：LMMSE+RZF 并行前置 + 共享 DetectorNN

> **这是什么**：EqDeepRx 风格——两条**并行**线性均衡器（LMMSE + RZF）各自给出 ĥ，**4 个 antenna port 共享同一个 DetectorNN 权重**（循环/广播应用），最后 DenoiseNN。**为什么**：① 同物理信道下 4 个 port 的检测问题 i.i.d.（旋转不变），共享权重参数省 4×、内存省 4×、Cube 利用率反而更高（同一权重反复跑 → L2 命中）；② LMMSE+RZF 并行给出两种先验，DetectorNN 学二者融合，比单一均衡器稳。SPEC §4 D2（[2602.11834]）实测 SOTA 且纯 conv/GEMM land。

---

## 架构

```
Y (per port) ──┬── LMMSE ──→ ĥ_lmmse
              └── RZF   ──→ ĥ_rzf
                              ↓
              4 个 port 共享同一 DetectorNN（参数省 4×）
                              ↓
                       [B, 4, F, S, C]  → DenoiseNN → 输出
```

---

## 可跑骨架

```python
import torch
import torch.nn as nn


class LMMSEBranch(nn.Module):
    """LMMSE 线性均衡器（部署期：单次 matmul）。"""
    def __init__(self, num_subcarriers=48):
        super().__init__()
        # 预计算权重 W_lmmse: [F, F]
        self.register_buffer("W", torch.randn(num_subcarriers, num_subcarriers) * 0.05)

    def forward(self, Y):
        # Y: [B*64, 4, 48]  （4 port 各自做）
        return torch.matmul(Y, self.W.t())   # [B*64, 4, 48]


class RZFBranch(nn.Module):
    """Regularized Zero-Forcing：W_rzf = (H^H H + λI)^−1 H^H。
    部署期同样退化为单次 matmul（λ 固定或统计估计）。
    """
    def __init__(self, num_subcarriers=48, reg_lambda=0.1):
        super().__init__()
        W = torch.randn(num_subcarriers, num_subcarriers) * 0.04
        self.register_buffer("W", W)

    def forward(self, Y):
        # Y: [B*64, 4, 48]
        return torch.matmul(Y, self.W.t())   # [B*64, 4, 48]


class SharedDetectorNN(nn.Module):
    """单 port 的 detector：输入 [B*64, C_in, 48]（C_in = 2 来自 lmmse/rzf 拼接，
    或 3 拼 Y），输出 [B*64, C_out, 48]。
    4 个 port 共享同一份这个模块的权重。
    """
    def __init__(self, c_in=2, c_mid=32, c_out=2):
        super().__init__()
        # 全 pointwise（M4）→ 全 GEMM，零 TransData
        self.body = nn.Sequential(
            nn.Conv1d(c_in, c_mid, kernel_size=1, bias=True),
            nn.BatchNorm1d(c_mid),
            nn.ReLU(inplace=True),
            nn.Conv1d(c_mid, c_mid, kernel_size=1, bias=True),
            nn.BatchNorm1d(c_mid),
            nn.ReLU(inplace=True),
            nn.Conv1d(c_mid, c_out, kernel_size=1, bias=True),
        )

    def forward(self, x):
        # x: [B*64, c_in, 48]
        return self.body(x)                  # [B*64, c_out, 48]


class DenoiseNN(nn.Module):
    """4 port 全拼起来后的去噪（跨 port 也做耦合）。
    输入 [B*64, 4*c_out, 48]，输出 [B*64, in_channels, 48]。
    """
    def __init__(self, c_in=8, c_mid=32, c_out=4):
        super().__init__()
        self.body = nn.Sequential(
            nn.Conv1d(c_in, c_mid, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm1d(c_mid),
            nn.ReLU(inplace=True),
            nn.Conv1d(c_mid, c_out, kernel_size=3, padding=1, bias=False),
        )

    def forward(self, x):
        return self.body(x)


class EqDeepRxModel(nn.Module):
    """整模型：LMMSE+RZF 并行 + 4 port 共享 Detector + Denoise。"""
    def __init__(self, in_channels=4, num_subcarriers=48, num_symbols=64, bias_flag=True):
        super().__init__()
        self.in_channels = in_channels
        self.num_subcarriers = num_subcarriers
        self.num_symbols = num_symbols

        self.lmmse = LMMSEBranch(num_subcarriers)
        self.rzf = RZFBranch(num_subcarriers)
        # ★ 单份 DetectorNN，4 port 共享
        self.detector = SharedDetectorNN(c_in=2, c_mid=32, c_out=2)
        self.denoise = DenoiseNN(c_in=in_channels * 2, c_mid=32, c_out=in_channels)

    def forward(self, inp):
        # inp: [B, 4, 48, 64, 1]
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)         # [B, 4, 48, 64]
        B, P, F, S = inp.shape

        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)                     # [B, 4, 48, 64]
        x = x.permute(0, 3, 1, 2)                    # [B, 64, 4, 48]
        x = torch.reshape(x, [B * S, P, F])          # [B*64, 4, 48]

        # 两条并行线性均衡
        h_lmmse = self.lmmse(x)                      # [B*64, 4, 48]
        h_rzf = self.rzf(x)                          # [B*64, 4, 48]

        # 4 个 port 各自过 detector（同一份权重循环应用）
        # 把 P 当 batch 维：[B*64*P, 2, 48]
        # 先把两个均衡结果沿 channel 维拼 → [B*64, 4, 2, 48]，再 reshape
        h_stack = torch.stack([h_lmmse, h_rzf], dim=2)   # [B*64, 4, 2, 48]
        h_det_in = h_stack.reshape(B * S * P, 2, F)      # [B*64*4, 2, 48]
        h_det_out = self.detector(h_det_in)              # [B*64*4, 2, 48]（共享权重）
        h_det_out = h_det_out.reshape(B * S, P, 2, F)    # [B*64, 4, 2, 48]

        # Denoise 跨 port：拼成 [B*64, 4*2=8, 48]
        denoise_in = h_det_out.permute(0, 1, 3, 2).reshape(B * S, P * 2, F)  # [B*64, 8, 48]
        out = self.denoise(denoise_in)                   # [B*64, 4, 48]

        out = out.reshape(B, S, P, F).permute(0, 2, 3, 1)   # [B, 4, 48, 64]
        out = out * alpha
        return torch.unsqueeze(out, dim=-1)              # [B, 4, 48, 64, 1]


if __name__ == "__main__":
    m = EqDeepRxModel()
    m.eval()
    y = m(torch.randn(1, 4, 48, 64, 1))
    print("EqDeepRx output:", y.shape)
    assert y.shape == (1, 4, 48, 64, 1)

    # 验证 detector 权重真的是单份
    n_params = sum(p.numel() for p in m.detector.parameters())
    print(f"SharedDetector 参数（4 port 共享一份）: {n_params}")
```

---

## 变异提示（不要照抄）

- **并行均衡器组合是个轴**：LMMSE+RZF / LMMSE+MF（匹配滤波）/ LMMSE+LMMSE-robust / 三路并行。两路是 EqDeepRx 默认。
- **共享粒度是个轴**：完全共享（本例）/ port-pair 共享（2 个 detector）/ 每 port 独立（baseline 的隐式做法）。共享度越高参数越省、表达力略降。
- **DetectorNN 结构是个轴**：① 全 pointwise（本例，纯 GEMM）；② 加 3-tap（少量时频先验）；③ 小 Transformer（如果 detector 要非线性耦合）。**优先 pointwise**。
- **加 M18 pilot-grid 输入**：detector 输入多拼一个 pilot mask 通道，精度小升。
- **加 M20 dilated**：denoise 改成 dilated resblock（捕跨子载波多径）。
- **物理对应**：4 port 共享的依据是**信道在 port 间统计独立同分布**（i.i.d. Rayleigh 假设）；如果 port 间有强相关（如阵天线耦合、空间相关信道），共享会损精度——这种场景要走 M29（MoE by port-correlation）或部分共享。
- **Cube 友好性**：共享权重 → 同一 W 反复调用 → L2 cache 命中率高、Cube tile 复用 → 实测时延比"4 份独立 W"低 2-3×，不止参数省。
- **fail-loud**：训练时如果某个 port 的 loss 明显高于其他 → 该 port 物理上不 i.i.d.（如天线故障或强耦合），需要拆出独立 detector。
- **反例**：不要把 denoise 也共享到 port 级——denoise 的职责是跨 port 耦合（去空间相关），它的输入必须是多 port 拼接。

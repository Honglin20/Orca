# pointwise_qkv_ffi.diff.md — M4：3-tap Conv1d → 1×1

> **这是什么**：把 baseline 三个 3-tap Conv1d（`p_lyr`/`cv1`/`cv2`）改成 1×1 pointwise conv。**为什么**：3-tap 在昇腾走 Im2Col + Cube GEMM（NC1HWC0），紧接着 attention 的 matmul 走 NZ/ND，每个 conv↔attn 边界触发一次 `TransData` 纯内存重排；1×1 conv 等价于直接 GEMM，**无 im2col**，且更贴近下游 matmul 的布局，TransData 开销显著降低。这是 SPEC §1 结论 2 的主攻方向。

---

## Before / After Diff

```diff
--- a/baseline_model.py   （QKV 投影 p_lyr）
+++ b/pointwise_qkv.py    （M4：3-tap → 1×1）
 class SignalAttention1D(nn.Module):
     def __init__(self, embed_dim, num_symbols, num_subcarriers, b_flg=True, m_type="t1"):
         super().__init__()
         ...
-        # 3-tap：走 im2col + Cube GEMM（NC1HWC0），随后 attention matmul 走 NZ/ND
-        # → 每个 forward 触发一次 TransData 把 NC1HWC0 → NZ
-        self.p_lyr = nn.Conv1d(
-            in_channels=embed_dim,
-            out_channels=3 * embed_dim,
-            kernel_size=3,          # ← 元凶：kernel_size=3 触发 im2col
-            padding=1,
-            bias=b_flg,
-        )
+        # 1×1 pointwise：直接 GEMM，无 im2col，输出布局天然贴近下游 matmul
+        # kernel_size=1 → padding=0；stride 默认 1；权重形状 [3*embed_dim, embed_dim, 1]
+        self.p_lyr = nn.Conv1d(
+            in_channels=embed_dim,
+            out_channels=3 * embed_dim,
+            kernel_size=1,          # ← M4 关键改动
+            padding=0,
+            bias=b_flg,
+        )
```

```diff
--- a/baseline_model.py   （FFN cv1/cv2）
+++ b/pointwise_ffn.py    （M4：FFN 也 pointwise 化）
 class SignalFeedForward1D(nn.Module):
     def __init__(self, embed_dim, num_symbols, num_subcarriers, b_flg=True):
         super().__init__()
         ...
-        self.cv1 = nn.Conv1d(embed_dim, 2 * embed_dim, kernel_size=3, padding=1, bias=b_flg)
+        self.cv1 = nn.Conv1d(embed_dim, 2 * embed_dim, kernel_size=1, padding=0, bias=b_flg)
         self.act = nn.GELU()
-        self.cv2 = nn.Conv1d(2 * embed_dim, embed_dim, kernel_size=3, padding=1, bias=b_flg)
+        self.cv2 = nn.Conv1d(2 * embed_dim, embed_dim, kernel_size=1, padding=0, bias=b_flg)
```

```diff
--- a/baseline_model.py   （block proj）
+++ b/pointwise_proj.py   （proj 本来就当线性层用，顺手 pointwise）
-        self.proj = nn.Conv1d(embed_dim, embed_dim, kernel_size=3, padding=1, bias=False)
+        self.proj = nn.Conv1d(embed_dim, embed_dim, kernel_size=1, padding=0, bias=False)
```

---

## 完整可跑骨架（验证 shape 不变）

```python
import torch
import torch.nn as nn


class PointwiseQKVFFI(nn.Module):
    """M4：所有 3-tap Conv1d → 1×1。接口与 baseline 完全一致。"""
    def __init__(self, embed_dim=16, num_symbols=64, num_subcarriers=48, b_flg=True):
        super().__init__()
        self.embed_dim = embed_dim
        self.s = num_subcarriers ** -0.5
        self.ln = nn.LayerNorm([embed_dim, num_symbols, num_subcarriers], elementwise_affine=False)
        self.sm = nn.Softmax(dim=-1)
        # ★ 1×1：无 im2col，纯 GEMM
        self.p_lyr = nn.Conv1d(embed_dim, 3 * embed_dim, kernel_size=1, padding=0, bias=b_flg)

    def forward(self, x):
        # x: [B, 64, 16, 48]
        B, S, E, F = x.shape  # S=64, E=16, F=48
        x = x.permute(0, 2, 1, 3)             # [B, 16, 64, 48]
        x = self.ln(x)                        # [B, 16, 64, 48]
        x = x.permute(0, 2, 1, 3)             # [B, 64, 16, 48]

        x_f = torch.reshape(x, [B * S, E, F]) # [B*64, 16, 48]
        qkv = self.p_lyr(x_f)                 # [B*64, 48, 48] ← 无 TransData 触发
        qkv = torch.reshape(qkv, [B, S, 3 * E, F])

        q = qkv[:, :, 0:E, :].permute(0, 2, 1, 3)   # [B, 16, 64, 48]
        k = qkv[:, :, E:2*E, :].permute(0, 2, 1, 3) # [B, 16, 64, 48]
        v = qkv[:, :, 2*E:, :].permute(0, 2, 1, 3)  # [B, 16, 64, 48]

        dots = torch.matmul(q, k.transpose(-1, -2)) * self.s  # [B, 16, 64, 64]
        at = self.sm(dots)                                    # [B, 16, 64, 64]
        out = torch.matmul(at, v).permute(0, 2, 1, 3)         # [B, 64, 16, 48]
        return out


# 形状自检
if __name__ == "__main__":
    m = PointwiseQKVFFI()
    x = torch.randn(2, 64, 16, 48)
    y = m(x)
    assert y.shape == x.shape, f"{y.shape} vs {x.shape}"
    print("M4 pointwise QKV shape OK:", y.shape)
    # 预期权重形：[3*16, 16, 1] = [48, 16, 1]，baseline 是 [48, 16, 3]
    print("p_lyr.weight.shape =", m.p_lyr.weight.shape)
```

---

## 精度补偿建议

- 3-tap 在 freq 轴做了**邻频平滑**（等价于一个 3-tap FIR），pointwise 化丢掉这个先验。
- 补法（**不是本 move 的范围，只提示**）：M9 delay-domain soft-threshold，或在 conv 前加一个**显式的** 1×3 depthwise conv（但 depthwise 在昇腾 Cube 饿死，SPEC §7 failures 已禁）。优先选 M9。
- 也可以堆两层 1×1 模拟 3-tap 的感受野，但 FLOPs 不一定更省——需 msprof 实测。

---

## 变异提示（不要照抄）

- **kernel_size 是个 axis**：可以混合——QKV pointwise、FFN 保 3-tap（精度敏感）、proj pointwise；不必全改。
- **与 M5 组合**：stem `e_lyr`（4→16）+ 第一个 block 的 `p_lyr` 可合并成单次 4→48 投影，省一次 GEMM 启动。
- **与 M3 组合**：pointwise + 通道÷16（embed_dim 16→32）对齐 Cube tile，常一起改。
- **与 M1 组合**：pointwise conv 后接 BN 而不是 LN，BN 可 fold 进下一次 pointwise conv——双层 fold。
- **不要忘了 msprof**：M4 的收益完全来自"消 TransData"，必须用 TransData 占比前后对比来验证，不能只看 FLOPs。
- **反例**：如果某个 conv 的 3-tap 学到了关键频域先验（profiling 上看到权重强烈非中心化），保留它，别盲改。

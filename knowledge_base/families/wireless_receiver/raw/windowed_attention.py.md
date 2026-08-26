# windowed_attention.py.md — M8/D7：时间轴 Swin shifted-window attention

> **这是什么**：把 baseline 的全 64×64 symbol-axis attention（dots `[B, 16, 64, 64]`）替换成**局部窗** attention（窗口 W=16，窗内 16×16 attention），并配合 Swin 的 shifted-window（偶数层 W、奇数层 shift W/2）做信息跨窗传播。**为什么**：N=64 太短，linear-attn 是陷阱（SPEC §1 结论 1）；窗 attention 把 `O(N²)` 变 `O(N·W)`，FLOPs 降 4×，同时保留 softmax 物理意义（相干时间内符号强相关）。

---

## 窗 attention 计算

```
full attention:   dots [B, h, 64, 64]    → 64×64 = 4096 元素/头
window attention: dots [B, h, 4, 16, 16] → 4 个窗 × 16×16 = 1024 元素/头（4× 降）
```

Swin shift：偶数层在 [0:64] 上切 4 个等长窗；奇数层先 `torch.roll(x, W//2)`，再切窗，最后 roll 回来——这样跨窗信息通过相邻层传播。

---

## 可跑骨架

```python
import torch
import torch.nn as nn
import torch.nn.functional as F


def window_partition(x, window_size):
    """x: [B, H, C, L] → [B*num_windows, C, window_size]
    这里 H=64 (symbols), C=16 (embed_dim), L=48 (subcarriers)
    我们在 H 轴上分窗，每窗 window_size=16。
    """
    B, H, C, L = x.shape
    assert H % window_size == 0, f"H={H} 必须被 window_size={window_size} 整除"
    nw = H // window_size                                  # 窗数
    # 重排：[B, nw, window_size, C, L] → [B*nw, C, window_size, L]
    x = x.view(B, nw, window_size, C, L)
    x = x.permute(0, 1, 3, 2, 4).contiguous()             # [B, nw, C, W, L]
    return x.view(B * nw, C, window_size, L), nw


def window_merge(x, nw, B):
    """逆操作：[B*nw, C, W, L] → [B, H, C, L]"""
    _, C, W, L = x.shape
    x = x.view(B, nw, C, W, L)
    x = x.permute(0, 1, 3, 2, 4).contiguous()             # [B, nw, W, C, L]
    return x.view(B, nw * W, C, L)                         # [B, H, C, L]


class WindowedAttention(nn.Module):
    """Swin 风格时间轴窗 attention，替 baseline SignalAttention1D。
    输入输出形状不变：[B, 64, 16, 48]
    """
    def __init__(self, embed_dim=16, num_symbols=64, num_subcarriers=48,
                 window_size=16, num_heads=4, shift=False):
        super().__init__()
        assert embed_dim % num_heads == 0, "embed_dim 必须能被 num_heads 整除"
        assert num_symbols % window_size == 0, "num_symbols 必须被 window_size 整除"
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.head_dim = embed_dim // num_heads             # 16/4 = 4
        # ★ M3：head_dim 必须÷16 才能调 npu_fusion_attention（见 fused_attention_npu.py.md）
        #   head_dim=4 太小，需要 embed_dim 升到 64+ 才能让 head_dim=16
        self.scale = self.head_dim ** -0.5
        self.window_size = window_size
        self.shift = shift

        # QKV 用 1×1 pointwise conv（M4 友好，避免 TransData）
        self.qkv = nn.Conv1d(embed_dim, 3 * embed_dim, kernel_size=1, bias=True)
        self.proj = nn.Conv1d(embed_dim, embed_dim, kernel_size=1, bias=True)

    def forward(self, x):
        # x: [B, 64, 16, 48] —— [B, num_syms, embed_dim, num_subs]
        B, S, E, F = x.shape
        # 转成 [B, E, S, F] 让 conv 在 S 轴上"做时序"
        x = x.permute(0, 2, 1, 3)                            # [B, 16, 64, 48]
        x = x.reshape(B, E, S * F)                           # [B, 16, 64*48]  伪序列
        # 实际上我们要在 S 轴分窗，所以保留 [B, S, E, F] 处理
        x = x.view(B, S, E, F).permute(0, 2, 1, 3)           # 回到 [B, E, S, F]

        # shift (Swin)
        if self.shift:
            x = torch.roll(x, shifts=self.window_size // 2, dims=2)  # 在 S 轴滚 W/2

        # 分窗：把 S=64 切成 4 个 W=16
        # x: [B, E, S, F] → 我们重排成 [B, S, E, F] 然后分窗更直观
        x_s = x.permute(0, 2, 1, 3)                          # [B, S=64, E=16, F=48]
        x_win, nw = window_partition(x_s, self.window_size)  # [B*4, E=16, W=16, F=48]

        # QKV：把 [B*4, E, W, F] reshape 成 [B*4, E, W*F] 过 Conv1d
        x_flat = x_win.reshape(B * nw, E, self.window_size * F)  # [B*4, 16, 16*48=768]
        qkv = self.qkv(x_flat)                               # [B*4, 48, 768]
        qkv = qkv.reshape(B * nw, 3, self.num_heads, self.head_dim, self.window_size, F)
        q, k, v = qkv[:, 0], qkv[:, 1], qkv[:, 2]            # 各 [B*4, h, head_dim, W, F]

        # 窗内 attention（W=16, F=48 → 在 W 轴上做 16×16 attn）
        # reshape 到 [B*4*h, head_dim*F, W]，让 W 当 seq
        q = q.reshape(B * nw * self.num_heads, self.head_dim * F, self.window_size)
        k = k.reshape(B * nw * self.num_heads, self.head_dim * F, self.window_size)
        v = v.reshape(B * nw * self.num_heads, self.head_dim * F, self.window_size)
        dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale  # [B*4*h, 16, 16]
        at = F.softmax(dots, dim=-1)                              # [B*4*h, 16, 16]
        out = torch.matmul(at, v)                                 # [B*4*h, head_dim*F, 16]

        # 还原形状
        out = out.reshape(B * nw, self.num_heads, self.head_dim, self.window_size, F)
        out = out.reshape(B * nw, E, self.window_size * F)        # [B*4, 16, 768]
        out = self.proj(out)                                      # [B*4, 16, 768]
        out = out.reshape(B * nw, E, self.window_size, F)         # [B*4, 16, 16, 48]

        # 合窗
        out = window_merge(out, nw, B)                            # [B, 64, 16, 48]
        # 反 shift
        if self.shift:
            out = torch.roll(out, shifts=-self.window_size // 2, dims=1)
        return out                                               # [B, 64, 16, 48]


if __name__ == "__main__":
    m = WindowedAttention(embed_dim=16, num_symbols=64, window_size=16, num_heads=4, shift=False)
    x = torch.randn(2, 64, 16, 48)
    y = m(x)
    print("windowed attn out:", y.shape)  # [2, 64, 16, 48]
    assert y.shape == x.shape
```

---

## 变异提示（不要照抄）

- **window_size 是个轴**：W=8 → 8×8 窗、8 个窗；W=16 默认；W=32 → 2 个窗，接近全局。相干时间短的信道（高铁/mmWave）用小 W；静态信道用大 W。
- **shift 是个轴**：纯窗（不 shift）=局部；纯 shift（每层都 shift）=边界 artifact；Swin 经典配置=偶数层不 shift、奇数层 shift。
- **num_heads 是个轴**：1/2/4/8；**警告** SPEC §8 要求 head_dim÷16 才能用 NPU 融合算子（M7），head_dim=4 太小——升 embed_dim 到 32/64 才能让 head_dim=8/16。
- **轴向是另一个方向**：本文件只窗 symbol 轴，也可窗 subcarrier 轴（M21 轴向 attention 是它的亲戚）。
- **与 M7 冲突**：本文件用了 `torch.matmul + softmax` 手搓；想融合昇腾 `npu_fusion_attention` 的话要把窗内 attn 整成该算子期望的 `[B, S, H, D]` 布局（见 `fused_attention_npu.py.md`）。
- **物理对应**：相干时间 ≈ 1/Doppler；W=16 对应 16 个 OFDM 符号 ≈ 16 ×  us（按 numerology）。窗内假设 = 信道在这段时间内强相关，物理上对静态/低 mobility 信道成立。
- **fail-loud**：W 必须÷尽 num_symbols（64=2^6），否则要 pad/mask，引入动态 shape（昇腾 Host dispatch 重编译，SPEC §8 第 6 条）。

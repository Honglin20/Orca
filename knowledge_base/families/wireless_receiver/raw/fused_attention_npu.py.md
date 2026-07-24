# fused_attention_npu.py.md — M7：调昇腾 npu_fusion_attention（禁手搓 matmul+softmax）

> **这是什么**：把 baseline 手搓的 `torch.matmul(q,k.T) → softmax → matmul(at,v)` 三步替换成**单次** `torch_npu.npu_fusion_attention` 调用。**为什么**：SPEC §8 第 7 条——昇腾有原生融合 attention 算子，单 kernel 完成 QK^T + scale + softmax + mask + AV，消掉所有中间 buffer、消掉 softmax 的 Vector 归约、消掉 matmul↔softmax 边界的 TransData。手搓形态丢融合还触发 TransData，是 SPEC §7 failures 明令禁止的。

---

## 算子约束（必读）

`torch_npu.npu_fusion_attention(query, key, value, head_num, input_layout, ...)`

1. **`head_dim` 必须 ÷16**（昇腾 Cube tile 16×16×16）：head_dim = embed_dim / num_heads ≥ 16。
2. **`seq_len ≥ 16`**（head_dim 轴和 seq 轴对齐）。
3. **`input_layout`** 可选 `"BSH"` / `"BNSD"` / `"BSND"` / `"TND"`（昇腾推荐 `"BSND"` 或 `"BNSD"`，B=batch, S=seq, N=head, D=head_dim）。
4. **必须静态 shape**（SPEC §8 第 6 条）。

**关键**：baseline `embed_dim=16` → head_dim 最大 16（num_heads=1）或 4（num_heads=4）。**head_dim=4 太小不达标**，必须先把 `embed_dim` 升到 64+ 才能用融合算子。这是 M3（通道÷16）和 M7 的强耦合。

---

## 可跑骨架

```python
import torch
import torch.nn as nn

# 昇腾环境：
#   import torch_npu
#   from torch_npu import npu_fusion_attention
# CPU/GPU 验证形状时 fallback 到手搓（仅 shape 自检，不测性能）:
try:
    import torch_npu
    from torch_npu import npu_fusion_attention
    HAS_NPU = True
except ImportError:
    HAS_NPU = False
    def npu_fusion_attention(q, k, v, head_num, input_layout, scale, pre_tockens=0,
                             next_tockens=0, keep_prob=1.0):
        # 仅用于形状验证的 CPU fallback —— 部署时严禁走这条路径（SPEC §7 禁手搓）
        # 这里实现只是为了让 shape 自检在 CPU 跑通
        attn = torch.matmul(q, k.transpose(-1, -2)) * scale
        attn = torch.softmax(attn, dim=-1)
        return torch.matmul(attn, v), attn


class FusedNpuAttention(nn.Module):
    """调 npu_fusion_attention 替代 baseline SignalAttention1D。
    输入输出形状与 baseline 一致：[B, S, E, F] = [B, 64, E, 48]
    """
    def __init__(self, embed_dim=64, num_symbols=64, num_subcarriers=48,
                 num_heads=4):
        super().__init__()
        # ★ M3 强制：head_dim = embed_dim / num_heads 必须 ≥ 16
        #   embed_dim=64, num_heads=4 → head_dim=16 ✓
        #   embed_dim=16, num_heads=4 → head_dim=4  ✗（不可用本 move）
        assert embed_dim % num_heads == 0
        head_dim = embed_dim // num_heads
        assert head_dim % 16 == 0, (
            f"head_dim={head_dim} 必须÷16 才能调 npu_fusion_attention；"
            f"升 embed_dim 到 {num_heads * 16 * ((head_dim // 16) + 1) * num_heads}"
        )
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.head_dim = head_dim
        self.scale = head_dim ** -0.5

        # QKV：pointwise（M4 友好），输出 [B*64, 3E, 48]
        self.qkv = nn.Conv1d(embed_dim, 3 * embed_dim, kernel_size=1, bias=True)
        self.proj = nn.Conv1d(embed_dim, embed_dim, kernel_size=1, bias=True)
        self.num_symbols = num_symbols

    def forward(self, x):
        # x: [B, S=64, E, F=48]
        B, S, E, F = x.shape
        # baseline 的 per-channel 64×64 怪写法被废弃 → 换标准 MHA
        # reshape 到 [B*64, E, F] 过 qkv
        x_f = x.reshape(B * S, E, F)                # [B*64, E, F]
        qkv = self.qkv(x_f)                          # [B*64, 3E, F]
        qkv = qkv.reshape(B, S, 3, self.num_heads, self.head_dim, F)
        # 把 F 合到 head_dim（或单独做 freq-axial，先保持简单）
        # 这里用 (head_dim * F) 作为 D —— 简化，把 F 拼进 D
        q = qkv[:, :, 0].reshape(B, S, self.num_heads, self.head_dim * F)
        k = qkv[:, :, 1].reshape(B, S, self.num_heads, self.head_dim * F)
        v = qkv[:, :, 2].reshape(B, S, self.num_heads, self.head_dim * F)

        # ★ 必须用 BNSD layout（B, N=head, S=seq, D=head_dim）
        # 我们的"seq" 是 symbol 轴 S=64
        # q: [B, S, N, D] → permute 到 [B, N, S, D]
        q = q.permute(0, 2, 1, 3).contiguous()       # [B, N, S, D]
        k = k.permute(0, 2, 1, 3).contiguous()
        v = v.permute(0, 2, 1, 3).contiguous()

        # ★ 调融合算子（昇腾部署）/ fallback（仅 CPU 形状验证）
        out, _ = npu_fusion_attention(
            q, k, v,
            head_num=self.num_heads,
            input_layout="BNSD",
            scale=self.scale,
            keep_prob=1.0,                # 推理期 1.0；训练期 < 1.0 触发 dropout 融合
            pre_tockens=S,                # causal mask 时设 1；这里双向全 attend
            next_tockens=S,
        )
        # out: [B, N, S, D] → [B, S, N, D] → [B, S, E*F] 过 proj
        out = out.permute(0, 2, 1, 3).reshape(B, S, E, F)   # [B, S, E, F]
        out_f = out.reshape(B * S, E, F)                     # [B*64, E, F]
        out_f = self.proj(out_f)                              # [B*64, E, F]
        out = out_f.reshape(B, S, E, F)                       # [B, 64, E, F]
        return out


if __name__ == "__main__":
    # ★ head_dim=16 需 embed_dim=64（升 baseline 的 16 → 64，这是 M3+M7 联动）
    m = FusedNpuAttention(embed_dim=64, num_heads=4)
    x = torch.randn(2, 64, 64, 48)
    y = m(x)
    print("fused attn out:", y.shape)   # [2, 64, 64, 48]
    assert y.shape == x.shape
```

---

## 变异提示（不要照抄）

- **layout 是个轴**：`"BSH"`（S+H 拼一起，最省 reshape）/ `"BNSD"`（最直观）/ `"BSND"`（推荐，stride 友好）。不同 layout 性能差 1.5-2×，按实测选。
- **head_num 与 head_dim 平衡**：固定 embed_dim 时，head_dim=16 优先（最小达标），head_num = embed_dim/16。head_num 增多不会更快（Cube tile 固定）。
- **keep_prob / mask**：训练期 keep_prob<1 触发 dropout 融合；causal mask 用 `pre_tockens=1, next_tockens=S`（或反过来）。OFDM 接收机一般无 causal，全 attend。
- **必须放弃 baseline 的 per-channel 16×16 怪写法**：本 move 隐含"改回标准 MHA"。head_dim 升到 16 后语义清晰、且能用融合算子——这是 SPEC §1 结论 1 的应对：attention 才占 17%，与其保留怪写法不如换标准融合。
- **与 M8/M21 冲突**：windowed/axial attention 把 seq 切碎，每个窗内 seq=16（刚好达标）；如果窗太小（W=8）→ seq<16，融合算子不可用，回退手搓（性能差）。**W≥16 才能叠加 M7**。
- **与 M4 协同**：QKV 用 pointwise conv（kernel=1）→ 输出布局天然贴近融合算子的期望布局 → 进一步省 TransData。
- **head_dim÷16 是硬约束**：embed_dim=16 的 baseline 必须先升维（M3），不能直接套 M7。
- **物理对应**：融合算子语义 = 标准 softmax attention，物理意义不变；本 move 是**工程层降时延**，不是**模型层改先验**。
- **fail-loud**：部署时如果 `import torch_npu` 失败 / fallback 路径被触发 → 立刻报错而不是默写，因为 fallback 是手搓（SPEC §7 禁）。`assert HAS_NPU, "本 move 要求昇腾环境"`。
- **msprof 验证**：必看融合前后的 attention 段耗时 + TransData 占比——本 move 的收益全在融合，不在 FLOPs。

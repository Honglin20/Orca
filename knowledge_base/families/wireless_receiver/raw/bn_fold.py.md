# bn_fold.py.md — M1：LayerNorm → BatchNorm → fold 进前一个 Conv1d

> **这是什么**：把 baseline 里不可 fold 的 `LayerNorm`（昇腾 Vector 归约，没法跟 Cube 融合）换成 `BatchNorm1d`（可 fold），再在部署期把 BN 等价折进**前一个** Conv1d 的权重/偏置，从而消掉一次 normalize kernel。**为什么**：昇腾有原生 `ConvBatchnormFusionPass`，融合后 conv+BN 等价于单个 conv，省一次 Vector 归约 + 一次 elementwise——对每个 conv-attn block 都适用。

---

## Fold 数学公式

设前一个 Conv1d 输出 `y_conv = W * x + b`，BN 参数 `γ (weight), β (bias), μ (running_mean), σ² (running_var), ε`。

BN 输出：
```
y = γ · (y_conv − μ) / sqrt(σ² + ε) + β
  = (γ / sqrt(σ² + ε)) · (W * x + b − μ) + β
  = (γ / sqrt(σ² + ε)) · W * x  +  (γ / sqrt(σ² + ε)) · (b − μ) + β
```

定义：
```
scale = γ / sqrt(σ² + ε)              # [C]
W' = W * scale.reshape(C_out, 1, 1)   # 逐通道缩放卷积核
b' = (b − μ) * scale + β              # 逐通道偏置
```

则 `y = W' * x + b'`，与 Conv→BN 等价，但只有一个 conv 算子。

---

## 可跑骨架（含训练态 + 部署期 fold）

```python
import torch
import torch.nn as nn


class ConvBNBlock(nn.Module):
    """训练态：Conv1d(3-tap) → BN → GELU。
    部署态：fold 后等价于 Conv1d(3-tap) → GELU（BN 已吸收进 conv 权重）。
    """
    def __init__(self, in_ch, out_ch, kernel_size=3):
        super().__init__()
        # ⚠️ conv 要带 bias=True，否则 b=0，fold 后仍需补一个 bias
        self.conv = nn.Conv1d(in_ch, out_ch, kernel_size, padding=kernel_size // 2, bias=True)
        self.bn = nn.BatchNorm1d(out_ch)
        self.act = nn.GELU()

    def forward(self, x):
        # x: [B*64, in_ch, 48]
        x = self.conv(x)   # [B*64, out_ch, 48]
        x = self.bn(x)     # [B*64, out_ch, 48]  ← 训练态归一化
        return self.act(x) # [B*64, out_ch, 48]


@torch.no_grad()
def fold_conv_bn(conv: nn.Conv1d, bn: nn.BatchNorm1d) -> nn.Conv1d:
    """把 bn 折进 conv，返回新的 conv（in-place 修改也行，这里返回新模块更清晰）。
    要求 conv 输出通道 == bn 通道，且 conv 后没有别的算子。
    """
    scale = bn.weight / torch.sqrt(bn.running_var + bn.eps)   # [C]
    # Conv1d.weight 形状 [C_out, C_in, k]
    W_fold = conv.weight * scale.reshape(-1, 1, 1)            # [C_out, C_in, k]
    b_fold = (conv.bias - bn.running_mean) * scale + bn.bias  # [C_out]

    folded = nn.Conv1d(
        in_channels=conv.in_channels,
        out_channels=conv.out_channels,
        kernel_size=conv.kernel_size,
        stride=conv.stride,
        padding=conv.padding,
        dilation=conv.dilation,
        groups=conv.groups,
        bias=True,
    )
    folded.weight.copy_(W_fold)
    folded.bias.copy_(b_fold)
    folded.eval()
    return folded


# ============================================================
# 端到端验证：训练态 ConvBN vs fold 后的纯 Conv 数值一致
# ============================================================
if __name__ == "__main__":
    torch.manual_seed(0)
    block = ConvBNBlock(in_ch=16, out_ch=32, kernel_size=3)
    block.train()
    # 跑几轮伪造数据让 running_mean/var 有值
    for _ in range(10):
        _ = block(torch.randn(8, 16, 48))
    block.eval()

    x = torch.randn(2, 16, 48)
    with torch.no_grad():
        y_train = block(x)                                    # 含 BN 的输出（过 act）

        # fold
        folded_conv = fold_conv_bn(block.conv, block.bn)
        y_folded = block.act(folded_conv(x))                 # 同样过 act
        max_err = (y_train - y_folded).abs().max().item()
        print(f"max abs err after fold: {max_err:.2e}")      # 期望 <1e-5
        assert max_err < 1e-4, "fold 数值偏差过大，检查 eps/running stats"
```

---

## 整合到 baseline 的注意

- baseline 用的是 `LayerNorm` 不是 `BN`，替换会**轻微改变训练动态**（BN 依赖 batch 统计、LN 不依赖）。
- 替换策略：在每个 `SignalTransformerBlock` 的 LN 位置换成 BN1d——shape 要对齐：
  - LN 作用在 `[B, 64, 16, 48]` 的后三维 → 改 BN1d 需要 reshape 到 `[B*64, 16, 48]`，BN1d 在 C=16 维上归一化。
- 替换前 **必须重训**（不是 fine-tune，是从头训），因为统计量完全不同。
- **不可 fold 的等价情况**：如果 LN 带 `elementwise_affine=False`（baseline 正是如此），那 LN 本身没有可学参数，直接删掉换成 BN 的影响更小——但 BN 的训练态 batch 归一化还是会改变训练。

---

## 变异提示（不要照抄）

- **fold 不止于 Conv1d**：Linear→BN、Conv2d→BN 同理；只要后接的 norm 是 affine-BN 都能 fold。**LN/RMSNorm/GroupNorm 都不能 fold**（SPEC §8 第 4 条）。
- **fold 后再叠 M2**：`torch.compile(backend=npu)` 的 AutoFuse 会再做 Conv+ReLU 融合，fold 后的 `Conv→GELU` 可被进一步合成单算子。
- **fold 的精度风险**：BN 训练态用 batch 统计，部署态用 running 统计；如果 batch 小或分布漂移，running_mean/var 不准 → fold 后有系统偏差。**用 EMA running stats 或 switch to eval() 前 freeze**。
- **反例（fail-loud）**：不要 fold 到 GroupNorm/InstanceNorm 上去——它们没 running stats，公式不成立。
- **组合顺序**：M1（BN-fold）→ M2（compile+AutoFuse）→ M16（INT8 PTQ）是**正交叠加**的典型链，互相不冲突，每步都能独立验证收益。

# pointwise_selective_dilated.py.md — M4+M20+M9 组合落地

> **这是什么**：在 model8 基础上做**选择性 pointwise 化**——只对 conv↔attention 边界处的 conv 改为 1×1（砍 TransData），非边界处通过 dilated conv 和 M9 软阈值保留频率感受野。
>
> **与 `pointwise_qkv_ffi.diff.md` 的区别**：那个是全改 1×1（激进，泛化掉点），这个是**选择性改**（只改有 TransData 惩罚的层，其余保留/增强感受野）。
>
> **设计原则**（对齐我们的讨论结论）：
> - 边界层（在 conv↔attention 边界上触发 TransData）：砍，改 1×1
> - 非边界层（全程在 conv-land 内）：保留 3-tap 或换 dilated conv
> - 入口层（stem）：用 dilated conv 一次性注入多尺度频率感受野
> - 补偿层（M9）：每个 block 内插 delay-domain soft-threshold, τ→0=identity
> - 出口层（r_out）：保留 3-tap，不在 TransData 边界上

## 接口契约（与 baseline 对齐）

- 输入：`[B, num_ports=4, num_subcarriers=48, num_symbols=64, 1]`
- 输出：同形
- alpha 归一化：保留（`x/α` → 网络 → `x*α`）

## 逐层改动对照

| 层 | 原始 | 改动 | 原因 |
|---|---|---|---|
| `e_lyr` (stem) | Conv1d k=3 | **Conv1d k=3 + dilation=2** | 入口注入宽感受野，不在 TransData 边界上 |
| `p_lyr` (QKV) | Conv1d k=3 | **Conv1d k=1** (pointwise) | conv→attention 边界，TransData 税最重，必须砍 |
| `cv1`/`cv2` (FFN) | Conv1d k=3 | **保留 k=3** | 全程在 conv-land 内，无 TransData 惩罚；保频率平滑 |
| `proj` (残差投影) | Conv1d k=3 | **Conv1d k=1** | attention 出口→conv 入口的边界，砍 Im2Col |
| `r_out` (输出) | Conv1d k=3 | **保留 k=3** | 不在 TransData 边界上；出口需要保频域精度 |
| 新增: `soft_thr` (M9) | 无 | **delay-domain soft-threshold** | 补 M4 丢的频率选择性，τ→0=identity |
| `ln` (LayerNorm) | elementwise_affine=False | **保留**（暂不动） | M1 BN-fold 是独立 move，不在此文件范围内 |

---

## 完整可跑代码

```python
import torch
import torch.nn as nn
import torch.nn.functional as F


# ============================================================
# M9: delay-domain soft-threshold（频域选择性补偿）
# ============================================================
# 物理含义: 多径信道在 delay 域稀疏(ℓ1 先验)，
# soft-threshold 显式压掉小径噪声，保留大径信号。
# τ=0 时退化为 identity（fail-forward）。
class DelayDomainSoftThreshold(nn.Module):
    """沿频率轴做 FFT → soft-threshold → IFFT。

    插入位置: attention 出口与 FFN 之间，conv-land 内，
    不引入新的 domain crossing。
    """
    def __init__(self, embed_dim, init_tau=0.0):
        super().__init__()
        self.embed_dim = embed_dim
        self.tau = nn.Parameter(torch.tensor(init_tau))

    def forward(self, x):
        # x: [B, 64, 16, 48]
        if self.tau.abs() < 1e-8:
            return x

        # FFT 沿频率轴（最后一维 = 48）
        x_f = torch.fft.rfft(x.float(), dim=-1)          # [B, 64, 16, 25]
        mag = torch.abs(x_f)
        phase = torch.angle(x_f)

        # soft-threshold: sign·max(|x|-τ, 0)
        mag_thr = F.relu(mag - self.tau)
        # 保 phase 做"软"的去噪，不丢相位信息
        x_f_thr = mag_thr * torch.exp(1j * phase)

        x_t = torch.fft.irfft(x_f_thr, n=x.shape[-1], dim=-1)  # [B, 64, 16, 48]
        return x_t.to(x.dtype)


# ============================================================
# 模块 A: 改进版 attention（p_lyr pointwise + M9 补偿可选）
# ============================================================
class SignalAttention1D_Pointwise(nn.Module):
    """Attention 模块 — 仅 p_lyr pointwise 化（边界层砍 TransData）。

    与 baseline 的区别:
      - p_lyr: kernel=3 → kernel=1（M4 核心改动）
      - 其余逻辑不变（per-channel 64×64 attention 保持不变，
        等 M7 改 npu_fusion_attention 时再动）
    """
    def __init__(self, embed_dim, num_symbols, num_subcarriers, b_flg=True, m_type="t1"):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.m_type = m_type

        self.s = num_subcarriers ** -0.5 if m_type == "t1" else embed_dim ** -0.5

        self.ln = nn.LayerNorm(
            [embed_dim, num_symbols, num_subcarriers], elementwise_affine=False
        )
        self.sm = nn.Softmax(dim=-1)

        # ★ M4: kernel=1（纯 GEMM，无 im2col，消 TransData 触发点）
        self.p_lyr = nn.Conv1d(
            in_channels=embed_dim,
            out_channels=3 * embed_dim,
            kernel_size=1,
            padding=0,
            bias=b_flg,
        )

    def forward(self, x):
        batch, num_syms, embed_dim, num_subs = x.shape

        x = x.permute(0, 2, 1, 3)
        x = self.ln(x)
        x = x.permute(0, 2, 1, 3)

        x_f = torch.reshape(x, [batch * num_syms, embed_dim, num_subs])
        qkv = self.p_lyr(x_f)  # 1×1 conv = 纯 GEMM，无 TransData
        qkv = torch.reshape(qkv, [batch, num_syms, 3 * self.embed_dim, num_subs])

        q = qkv[:, :, 0:self.embed_dim, :]
        k = qkv[:, :, self.embed_dim:2 * self.embed_dim, :]
        v = qkv[:, :, 2 * self.embed_dim:, :]

        if self.m_type == "t1":
            q = q.permute(0, 2, 1, 3)
            k = k.permute(0, 2, 1, 3)
            v = v.permute(0, 2, 1, 3)
            dots = torch.matmul(q, k.transpose(-1, -2)) * self.s
            at = self.sm(dots)
            out = torch.matmul(at, v).permute(0, 2, 1, 3)
        else:
            q = q.permute(0, 3, 1, 2)
            k = k.permute(0, 3, 1, 2)
            v = v.permute(0, 3, 1, 2)
            dots = torch.matmul(q, k.transpose(-1, -2)) * self.s
            at = self.sm(dots)
            out = torch.matmul(at, v).permute(0, 2, 3, 1)

        return out


# ============================================================
# 模块 B: FFN 保留 3-tap（非边界层，保频率平滑）
# ============================================================
class SignalFeedForward1D(nn.Module):
    """FFN — cv1/cv2 保留 kernel=3。

    原因: FFN 全程在 conv-land 内（输入/输出都是 conv 格式），
    kernel=3 的 Im2Col 开销不触发额外的 TransData。
    保留邻频平滑对泛化有帮助。
    """
    def __init__(self, embed_dim, num_symbols, num_subcarriers, b_flg=True):
        super().__init__()
        self.embed_dim = embed_dim
        self.ln = nn.LayerNorm(
            [num_symbols, embed_dim, num_subcarriers], elementwise_affine=False
        )
        # 保留 3-tap：不在 TransData 边界上，保频率感受野
        self.cv1 = nn.Conv1d(embed_dim, 2 * embed_dim, kernel_size=3, padding=1, bias=b_flg)
        self.act = nn.GELU()
        self.cv2 = nn.Conv1d(2 * embed_dim, embed_dim, kernel_size=3, padding=1, bias=b_flg)

    def forward(self, x):
        batch, num_syms, embed_dim, num_subs = x.shape
        x = self.ln(x)
        x_f = torch.reshape(x, [batch * num_syms, embed_dim, num_subs])
        x = self.cv1(x_f)
        x = self.act(x)
        x = self.cv2(x)
        return torch.reshape(x, [batch, num_syms, embed_dim, num_subs])


# ============================================================
# 模块 C: Block（选择性 pointwise + M9 补偿）
# ============================================================
class SignalTransformerBlock_V2(nn.Module):
    """改进版 block:
      - attention 的 p_lyr → pointwise（砍 TransData）
      - proj → pointwise（attn→conv 边界）
      - FFN cv1/cv2 → 保留 3-tap（conv-land 内）
      - 插入 M9 soft-threshold（补 M4 丢的频率选择性）
    """
    def __init__(self, embed_dim, num_symbols, num_subcarriers,
                 m_type="t1", use_soft_threshold=True, init_tau=0.0):
        super().__init__()
        self.m_a = SignalAttention1D_Pointwise(
            embed_dim, num_symbols, num_subcarriers, m_type=m_type
        )

        # ★ M4: proj pointwise（在 attn 出口到 conv 入口的边界上）
        self.proj = nn.Conv1d(
            embed_dim, embed_dim, kernel_size=1, padding=0, bias=False
        )

        # ★ M9: delay-domain soft-threshold（可选，τ=0 时 identity）
        self.soft_thr = (
            DelayDomainSoftThreshold(embed_dim, init_tau=init_tau)
            if use_soft_threshold else nn.Identity()
        )

        self.m_c = SignalFeedForward1D(embed_dim, num_symbols, num_subcarriers)

    def forward(self, x):
        batch, num_syms, embed_dim, num_subs = x.shape

        # attention（p_lyr=1×1，无 TransData）
        x_a = self.m_a(x)

        # proj（1×1，在 attn→conv 边界，无 im2col）
        x_f_f = torch.reshape(x_a, [batch * num_syms, -1, num_subs])
        x_p = self.proj(x_f_f)
        x_p = torch.reshape(x_p, [batch, num_syms, embed_dim, num_subs])
        x = x_p + x  # 残差 1

        # M9: delay-domain soft-threshold（补频率选择性）
        x = self.soft_thr(x)

        # FFN（cv1/cv2=3-tap，在 conv-land 内，保频率感受野）
        x_m_c = self.m_c(x)
        x = x_m_c + x  # 残差 2

        return x


# ============================================================
# 顶层: 选择性 pointwise + dilated stem + M9 补偿
# ============================================================
class SignalProcessingTransformer_M4(nn.Module):
    """M4 选择性落地版。

    改动汇总:
      - e_lyr: kernel=3, dilation=2（入口注入宽频率感受野）
      - p_lyr(QKV): kernel=1（边界砍 TransData）
      - proj: kernel=1（边界砍 TransData）
      - cv1/cv2(FFN): kernel=3（保留，conv-land 内无 TransData 惩罚）
      - r_out: kernel=3（保留，出口不在边界上）
      - M9 soft-threshold: 每 block 一块（τ 可学，初始 0=identity）
    """
    def __init__(self, in_channels=4, embed_dim=16, num_symbols=64,
                 num_subcarriers=48, bias_flag=True, num_blocks=4,
                 use_soft_threshold=True, init_tau=0.0,
                 stem_dilation=2):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.b_flg = bias_flag

        # ★ stem: dilated conv 入口（一次性注入多尺度频率感受野）
        #   dilation=2: 3-tap 卷积覆盖 5 个邻频子载波（等效感受野=5）
        #   代价: 仍然走 Im2Col+Cube GEMM，不触发 TransData（后续还在 conv-land）
        self.e_lyr = nn.Conv1d(
            in_channels=self.in_channels,
            out_channels=self.embed_dim,
            kernel_size=3,
            padding=stem_dilation,  # padding = dilation 保持长度不变
            dilation=stem_dilation,
            bias=self.b_flg,
        )

        self.main = nn.Sequential(*[
            SignalTransformerBlock_V2(
                embed_dim, num_symbols, num_subcarriers,
                m_type="t1",
                use_soft_threshold=use_soft_threshold,
                init_tau=init_tau,
            )
            for _ in range(num_blocks)
        ])

        # ★ r_out: 保留 3-tap（出口不在 TransData 边界上，需要频域精度）
        self.r_out = nn.Conv1d(
            in_channels=self.embed_dim,
            out_channels=self.in_channels,
            kernel_size=3,
            padding=1,
            bias=self.b_flg,
        )

    def forward(self, inp):
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)

        B, num_ports, num_subcarriers, num_symbols = inp.shape

        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)

        x = x.permute(0, 3, 1, 2)
        x = torch.reshape(x, [B * num_symbols, num_ports, num_subcarriers])

        # stem: dilated conv 入口
        x = self.e_lyr(x)

        x = torch.reshape(x, [B, num_symbols, -1, num_subcarriers])
        x = self.main(x)
        x = torch.reshape(x, [B * num_symbols, -1, num_subcarriers])

        x = self.r_out(x)
        x = torch.reshape(x, [B, num_symbols, num_ports, num_subcarriers])
        x = x.permute(0, 2, 3, 1)

        x = x * alpha
        x = torch.unsqueeze(x, dim=-1)

        return x


# ============================================================
# 形状自检
# ============================================================
if __name__ == "__main__":
    torch.manual_seed(42)

    # --- 1. 默认配置：4 block, τ=0, stem dilation=2 ---
    model = SignalProcessingTransformer_M4(
        num_blocks=4,
        use_soft_threshold=True,
        init_tau=0.0,
        stem_dilation=2,
    )
    model.eval()

    B, num_ports, num_subcarriers, num_symbols = 2, 4, 48, 64
    dummy_input = torch.randn(B, num_ports, num_subcarriers, num_symbols, 1)

    with torch.no_grad():
        output = model(dummy_input)
        assert output.shape == dummy_input.shape, \
            f"Shape mismatch: {output.shape} vs {dummy_input.shape}"
        print(f"[PASS] Input: {dummy_input.shape} → Output: {output.shape}")

    # --- 2. τ=0 时 M9 应退化为 identity ---
    model_tau0 = SignalProcessingTransformer_M4(
        num_blocks=4, use_soft_threshold=True, init_tau=0.0
    )
    model_no_m9 = SignalProcessingTransformer_M4(
        num_blocks=4, use_soft_threshold=False
    )
    model_tau0.eval()
    model_no_m9.eval()

    # 复制相同权重（除 tau 外）
    tau0_state = model_tau0.state_dict()
    no_m9_state = model_no_m9.state_dict()
    for k in no_m9_state:
        if k in tau0_state and "tau" not in k:
            no_m9_state[k] = tau0_state[k]
    model_no_m9.load_state_dict(no_m9_state, strict=False)

    with torch.no_grad():
        out_tau0 = model_tau0(dummy_input)
        out_no_m9 = model_no_m9(dummy_input)
        diff = (out_tau0 - out_no_m9).abs().max().item()
        assert diff < 1e-5, f"τ=0 should be identity, diff={diff}"
        print(f"[PASS] τ=0 identity check: max diff = {diff:.2e}")

    # --- 3. 参数量对比 baseline ---
    from baseline_signal_transformer import SignalProcessingTransformer

    baseline = SignalProcessingTransformer()
    baseline_params = sum(p.numel() for p in baseline.parameters())
    m4_params = sum(p.numel() for p in model.parameters())

    print(f"\n--- Param Count ---")
    print(f"  Baseline: {baseline_params:,}")
    print(f"  M4 (selective): {m4_params:,}")
    print(f"  Delta: {m4_params - baseline_params:+,}")

    # --- 4. 打印各层 kernel_size 供核对 ---
    print(f"\n--- Layer Kernel Sizes ---")
    print(f"  e_lyr:    k={model.e_lyr.kernel_size}, dilation={model.e_lyr.dilation}")
    for i, blk in enumerate(model.main):
        print(f"  block[{i}].p_lyr: k={blk.m_a.p_lyr.kernel_size[0]}")
        print(f"  block[{i}].proj:  k={blk.proj.kernel_size[0]}")
        print(f"  block[{i}].cv1:   k={blk.m_c.cv1.kernel_size[0]}")
        print(f"  block[{i}].cv2:   k={blk.m_c.cv2.kernel_size[0]}")
        print(f"  block[{i}].M9.tau: {blk.soft_thr.tau.item() if hasattr(blk.soft_thr, 'tau') else 'N/A'}")
    print(f"  r_out:    k={model.r_out.kernel_size[0]}")

    print(f"\n[DONE] All checks passed.")
```

---

## 变异提示（不要照抄）

### 1. stem_dilation 是可调的轴

```
stem_dilation=1  → 恢复 baseline 的普通 3-tap（等效感受野=3）
stem_dilation=2  → 等效感受野=5（推荐起点）
stem_dilation=4  → 等效感受野=9（多径场景可以更大）
```

也可以改成**多分支 multidilated**：

```python
# 3 个分支，不同 dilation，concat
self.e_lyr = nn.ModuleList([
    nn.Conv1d(4, 8, 3, padding=1, dilation=1),   # 近径
    nn.Conv1d(4, 4, 3, padding=2, dilation=2),   # 中径
    nn.Conv1d(4, 4, 3, padding=4, dilation=4),   # 远径
])
# 输出 concat → 8+4+4=16 = embed_dim
```

### 2. τ 的训练策略

- **训练起点**：`init_tau=0.0`（identity），让模型先适应 pointwise 后的特征空间
- **warm-up 后开放**：训练 30% epoch 后解冻 τ，让模型自己学到最优阈值
- **部署期可关**：如果 τ 收敛到 0，说明数据不需要显式稀疏建模，M9 可以关掉（不增加推理开销）

### 3. 哪些层可以进一步 pointwise 化

如果这个选择性方案泛化 OK（比全改 1×1 好），可以逐步尝试：
- 第 2-4 个 block 的 cv1/cv2 也改 pointwise（深层频域特征已经足够抽象，不需要 3-tap）
- 第 1 个 block 的 cv1/cv2 保留 3-tap（浅层最需要频域局部平滑）

### 4. 与 M5（stem→QKV 重参数化）的组合

```python
# 如果 stem 和第一个 block 的 p_lyr 都 pointwise 了
# 可以把 e_lyr 的输出维度从 16 改成 48（=3*embed_dim）
# 直接跳到 QKV 的拼合输出，省掉第一个 block 的 p_lyr
# 代数等价，零精度损失
self.e_lyr = nn.Conv1d(4, 3 * embed_dim, kernel_size=3, dilation=2, padding=2)
# 第一个 block 的 p_lyr 变成 identity（或删掉）
```

### 5. 昇腾验证清单

- [ ] msprof 测量 TransData 占比（期望从 ~7% 显著下降）
- [ ] 对比全 pointwise vs 选择性 pointwise 的泛化 MSE
- [ ] τ 收敛后是否非零（决定 M9 是否有实际贡献）
- [ ] stem_dilation 扫参（1/2/4）找精度-时延最优值
- [ ] 确认 dilated conv 在昇腾上仍然走 Cube GEMM（应走 Im2Col+Cube，msprof 可验）

### 6. 反例 / 边界

- 如果信道是**平坦衰落**（延迟扩展 ≈ 0），dilation 和 3-tap 都没用——所有 conv 都可以 1×1
- 如果信道是**强频率选择性**（长多径），stem_dilation 太小会掉精度——需要 ≥4 或 multidilated
- 如果 τ 训练后收敛到 >0.5，说明模型强烈依赖 M9——此时不能删 M9，且 AMCT INT8 量化时要特别处理 tau 的校准

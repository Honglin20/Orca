# 无线 OFDM CNN+Transformer 在昇腾上的降时延知识库（变异基础）

> 计划文档（SDD「写计划 → 确认 → 实现」）。目标：为 `agent-struct-exploration` workflow 建一个**以变异为基础**的知识库，让较弱 LLM 也能跑。落点：新建 `wireless_receiver` 族 + `common/ascend_constraints.md`，演进现有 transformer/cnn 两族的三层结构。
>
- 源模型：`/mnt/d/Projects/nas-agent/examples/hw_inputs/model8/model/baseline_model.py`（SignalProcessingTransformer：Conv1d-over-freq + 自定义 per-channel 64×64 symbol attention + Conv-FFN，4 block）。
- 目标：单次前向推理时延降 2–3×，部署 = 昇腾 NPU。**硬件友好优先**——不收"降 FLOPs 但硬件不友好"的 move。

---

## 1. 瓶颈诊断（profiling + 昇腾根因）

**CPU profiling（方向性，绝对值以昇腾 msprof 为准）**：Conv1d(18 个) ~55%、Attention(bmm+softmax) ~17%、GELU ~7%、LayerNorm ~3%、reshape/permute/copy ~7%。
**结论 1**：attention 只占 17% 且 seq=64 太短 → linear/Performer/FlashAttention/Nyströmformer/Linformer 在 N=64 全是陷阱（N² 项 <0.4%，常数项吃光收益）。
**结论 2**：昇腾根因 = **TransData**——卷积走 Im2Col+Cube(NC1HWC0,C0=16)，attention matmul 走 NZ/ND，**每个 conv↔attention 边界触发一次纯内存重排**。这正是"CNN 要 img2col、matmul 另一种格式"的开销（ASPLOS'25 / ATC'25 Hermes 专攻此问题）。
**优化主轴**：减少 domain crossing 次数，不是让 attention 更快。

**领域反证（fail-loud 必记）**：DeepRx / EqDeepRx / NVIDIA NRX 三个实测部署的 SOTA 神经接收机**都不用 softmax attention**。Transformer-over-time 是该领域的结构异类——**必须先测 conv-only baseline**，达标则放弃 Transformer。

---

## 2. KB 三层结构设计（Family → Direction → RAW + Move 库）

客观评价已与用户对齐：方向正确，关键在「Direction 薄而多、Move 扁平独立、RAW 多份带变异提示、靠标签规则化检索」。原 4 文件模型**演进不推翻**：

```
knowledge_base/
  index.json                      # 扩展：families→{directions, latency_moves, raw}；agent_slices 加 direction/raw 选择 + 标签过滤
  common/
    ascend_constraints.md         # 新：硬件铁律（所有族变异先过这层）
    principles.md latency_heuristics.md primitives.md    # 原有不动
  families/wireless_receiver/     # 新大族
    meta.json                     # 族检测提示 + direction 标签索引（latency_tier/ascend/risk/physics）
    directions/                   # ← 方向层（薄而多，每条一段）
    latency_moves.md              # ← 原子变异算子库（扁平，~30 条）
    primitives.md                 # 含当前 per-channel 64×64 attention 怪异写法
    failures.md                   # AVOID 清单 + 陷阱
    raw/                          # ← RAW 层（每方向 ≥2 份风格不同的示例，带变异提示）
```

**检索四重过滤**（弱 LLM + 小 context 必须精准）：① 族 LLM 检测 → ② 标签规则化筛 direction（少依赖 LLM）→ ③ agent slice → ④ run 级缓存。

---

## 3. 大族（Family）

| 族 | 状态 | 说明 |
|---|---|---|
| `cnn` / `transformer` | 原有 | 图像 CNN / LLM-decode transformer（本模型不直接命中） |
| **`wireless_receiver`** | **新建** | 无线 PHY 神经接收机/均衡器/信道估计。检测信号：OFDM 时频网格输入、Conv-over-freq、symbol/subcarrier 轴、LLR/CSI 输出、归一化 alpha=sqrt(mean²·2) |

---

## 4. 方向层（Direction）—— 全部架构模板（变异锚点）

每条字段：**结构 / attention? / 昇腾 / 物理 / bundle 的 move / RAW 指针 / 来源**。

| # | 方向 | 一句话结构 | attn? | 昇腾 | 物理 | 来源 |
|---|---|---|---|---|---|---|
| D0 | `baseline_cnn_transformer` | 当前模型：Conv1d-freq + per-channel 64×64 symbol attn + Conv-FFN×4 | 是 | 税重 | — | 本仓库 |
| D1 | `deeprx_conv_only` | dilated DW-sep conv ResNet，3-grid 输入(Y, Y⊙Xp*, Xp)，出 LLR，**无 attn** | 否 | ✅✅ | ✅ 局部 | [2005.01494] |
| D2 | `eqdeeprx_linear_front` | LMMSE+RZF 并行前置 + 每流共享 DetectorNN + DenoiseNN，**无 attn** | 否 | ✅✅ | ✅ | [2602.11834] |
| D3 | `a_mmse_folded_linear` | Transformer 训练期在，部署折叠成单一线性滤波器，rank 可调 | 训是/推否 | ✅✅ | ✅ LMMSE 线性 | [2506.00452] |
| D4 | `fnet_fft_mix` | FFT 替时间轴 softmax attn，O(T²)→O(T log T) | 否(FFT) | ⚠️FFT融合待验 | ✅ Doppler 稀疏 | [2105.03824] |
| D5 | `channelformer_attn_precoder` | **单个**浅 attn 作输入 precoding + CNN decoder 主干（SISO 下行 CE） | 浅 | ✅ | — | [2302.04368] |
| D6 | `nvidia_nrx_conv_gnn` | init Conv + 交替[GNN 用户图消息传递→Conv 状态]×N，separable 3×3，<1ms A100+TRT | 否(GNN) | ✅ | ✅ | [2312.02601] |
| D7 | `windowed_axial_attn` | 轴向/可分 time-then-freq attn + 局部窗 W=16，2.81× FLOP | 是(小) | ✅ | ✅ T-F 可分 | [2510.12941] |
| D8 | `ista_lista_unfolded` | 展开收缩网，delay-domain soft-threshold，稀疏先验 | 否 | ✅ | ✅ 多径 ℓ1 稀疏 | [2104.13656] / ISTA-Net CVPR18 |
| D9 | `mamba_ssm_freq` | 双向 SSM 沿子载波扫描 | 否 | ⚠️scan kernel 无原生算子 | ✅ | [2601.17108] |
| D10 | `residual_around_lmmse` | 前置 LMMSE，NN 只学 Δh = h−ĥ_LMMSE | 否 | ✅ | ✅ | [2009.01423] |
| D11 | `kd_to_conv_student` | 全模型 KD → conv-only student（99.3% 参数 / 97% 时延 / 0.5dB） | 否 | ✅✅ | 间接 | Zhu MDPI Sensors24 |

> 注：D1/D2 是该领域实测 SOTA、且昇腾最友好（纯 conv/GEMM-land，无 TransData），优先作变异目标。D5 实为 SISO 下行（非 MIMO 上行），仅作结构模板不作基准。D6 图节点 = MIMO 层全连接（非 RE↔user 二部图）。

---

## 5. 原子变异算子库（latency_moves.md）—— ~30 条

每条标注：`【昇腾 ✅/⚠️/❌】【精度】【物理】【锚定方向】`。按层组织（与现有 cnn/transformer latency_moves 体例一致）。

### T1 零精度损失 / 融合层（先吃）
| # | Move | 昇腾 | 精度 | 物理 | 锚定 |
|---|---|---|---|---|---|
| M1 | BN-fold（LN/RMSNorm→BN→fold 进 conv，`ConvBatchnormFusionPass`） | ✅✅ | 用户确认可接受 | — | 全局 |
| M2 | `torch.compile(backend=npu)`+AutoFuse（消 reshape/copy/GELU/LN 串联） | ✅ | 0 等价 | — | 全局 |
| M3 | 静态 shape + 通道÷16 对齐（sinking dispatch） | ✅ | 0 | — | 全局 |
| M4 | pointwise 化（3-tap Conv1d→1×1，消 im2col + TransData 触发点） | ✅✅ | 中低（丢邻频平滑→M9 补） | — | D0 |
| M5 | QKV-fold + stem→QKV 重参数化（3 投影→1，代数等价） | ✅ | 0 | — | D0/EfficientFormer |

### T2 结构层（保留混合）
| # | Move | 昇腾 | 精度 | 物理 | 锚定 |
|---|---|---|---|---|---|
| M6 | 减 block 4→2-3 + 蒸馏 | ✅ | 中(蒸馏) | — | D0 |
| M7 | 调昇腾融合 attn 算子 `npu_fusion_attention`（禁手搓）+ head_dim÷16 | ✅ | 0 | — | D0/D7 |
| M8 | windowed/Swin 局部 attn（时间轴 W=16；高铁/mmWave W 自适应） | 中-✅ | 低 | ✅ 相干时间 | D7 |
| M9 | Conv1d↔Transformer 间插可学习 delay-domain soft-threshold（τ→0 no-op） | ✅ | 低, fail-forward | ✅ 多径 ℓ1 | D8 |
| M10 | FFT-mixing 替时间轴 softmax attn | ⚠️ FFT 融合待验 | 低-中 | ✅ Doppler 稀疏 | D4 |
| M11 | 4 antenna port 共享 DetectorNN 权重 + 前置 LMMSE | ✅✅ ~4× | 低 | ✅ 同物理信道 | D2/D10 |
| M12 | 低秩 Q/K 投影（attention down-score） | ✅ | 低 | — | EfficientFormerV2 |

### T3 架构层（重新审视 attn）
| # | Move | 昇腾 | 精度 | 物理 | 锚定 |
|---|---|---|---|---|---|
| M13 | 部署期 Transformer 折叠成线性滤波器（A-MMSE, rank-adaptive） | ✅✅ | 中 | ✅ LMMSE 线性 | D3 |
| M14 | KD 成 conv-only student | ✅✅ | 中(OOD 检测) | 间接 | D11 |
| M15 | conv-only / conv+GNN baseline，达标则弃 Transformer | ✅ | 取决数据 | ✅ | D1/D6 |

### T4 量化 / 剪枝（正交叠加）
| # | Move | 昇腾 | 精度 | 物理 | 锚定 |
|---|---|---|---|---|---|
| M16 | INT8 PTQ via AMCT（per-channel）+ 混合精度/QAT | ✅✅ Cube INT8≈2×FP16 | <1dB(QAT 可恢复) | — | 全局 |
| M17 | 结构化剪枝 channel/block（**非 DW**） | ✅ | 中(fine-tune) | — | Channelformer |

### B 类（研究扫到的额外 move）
| # | Move | 昇腾 | 精度 | 物理 | 锚定/来源 |
|---|---|---|---|---|---|
| M18 | pilot-grid 输入富化（[Y, Y⊙Xp*, Xp, mask] 多通道，**默认开**） | ✅ | 近零增益 | ✅ | DeepRx |
| M19 | residual-around-LMMSE（学 Δh） | ✅ | 低 | ✅ | D10 [2009.01423] |
| M20 | dilated/multidilated conv 堆（rates {1,2,4,8}，RF=31 同参数） | ✅ | 低 | ✅ 局部 | DeepRx |
| M21 | 轴向/可分 attn（time-then-freq） | ✅ | 低(单论文) | ✅ T-F 可分 | D7 [2510.12941] |
| M22 | CVNN + 编译期 real-pair lowering（2×2 block-real GEMM） | ⚠️ 无原生复数，需 lowering | 参数省/FLOPs 升 | ✅ I/Q | Trabelsi [1705.09792] |
| M23 | Toeplitz 线性层（**dense 重构形**，O(N) 参数） | ⚠️ dense 形✅/FFT 形❌ | 中 | ✅ 多径 CIR Toeplitz | ICLR23 [2305.04749] |
| M24 | hypernetwork 按 slot 生成接收机权重 | ✅ | 中(训练难) | ✅ 时变 | [2408.11920] |
| M25 | INR coordinate-MLP CE（**批量**查询，单大 GEMM） | ✅(批量) | 中(谱偏差) | ✅ | [2605.10213] |
| M26 | 双均衡器(LMMSE+RZF) + 每流共享 detector（EqDeepRx 核心） | ✅✅ | 低 | ✅ | D2 [2602.11834] |
| M27 | Mamba/SSM 沿子载波扫描 | ⚠️ scan kernel 不移植 | 未报 BER | ✅ | D9 [2601.17108] |
| M28 | Soft Graph Transformer detector（**仅 SGT 形**，banded attn=GEMM） | ⚠️ GNN 形❌ | 中 | ✅ MIMO 耦合 | [2509.12694] |
| M29 | MoE by SNR/mobility（top-1 硬门） | ➖ 小模型 dispatch 开销未知 | 低 | ✅ SNR 轴 | MEAN TECS26 |
| M30 | early-exit / 动态深度（**skip-via-zero-mask 重构**保静态图） | ❌原生→✅重构 | 中(每出口须达标) | ✅ SNR | DSD24/LOREN |
| M31 | BCR block 剪枝（昇腾上**降级为 INT8-only**，弃 MSQ 的 LUT/INT4 半） | ⚠️ 非 N:M 稀疏 | 低 | — | SPiNN [2205.06159] |

> **默认开**（每个候选都带，非竞争项）：M18 pilot-grid 输入。**先吃的廉价友好赢面**：M1/M2/M3/M4/M5、M19/M20/M26。

---

## 6. RAW 层（实现示例清单，每方向 ≥2 份带变异提示）

计划写的示例（.py.md / .diff.md，骨架 + 变异提示，非"可直接抄答案"）：
- `baseline_signal_transformer.py.md`（当前模型，逐块标注 per-channel attn 怪异写法 + 可变异点）
- `pointwise_qkv_ffi.diff.md`（M4：3-tap→1×1）
- `bn_fold.py.md`（M1）
- `fold_transformer_to_linear.py.md`（M13/D3）
- `deeprx_dilated_resblock.py.md`（M20/D1）
- `windowed_attention.py.md`（M8/D7）
- `axial_attention.py.md`（M21）
- `soft_threshold_layer.py.md`（M9/D8）
- `residual_around_lmmse.py.md`（M19/D10）
- `lmmse_front_shared_port.py.md`（M11/M26/D2）
- `pilot_grid_input.py.md`（M18）
- `fused_attention_npu.py.md`（M7）
- `quantize_amct.md`（M16）

---

## 7. failures.md（AVOID 清单 / 陷阱）

- ❌ DW-separable conv（Cube 饿死；V5 实测更慢）——**否掉通用文献推荐的 CMT depthwise-LPU**，改 pointwise/标准 conv 局部混合。
- ❌ group conv（大 groups）；动态 shape（Host dispatch 重编译）；手搓 attention（丢融合+TransData）；非融合 elementwise 中间层；LayerNorm 不 fold（能换 BN 就换）。
- ❌ N=64 下 linear/Performer/FlashAttention/Nyströmformer（attention 才 17%）。
- ❌ BNN/二值化（无线 OFDM 无实测，低 SNR 1-3dB 损）；❌ INT4 on 昇腾（仅 FP16+INT8，FPGA 的 MSQ INT4 说法降级）。
- ❌ SisRafNet GRU 频率递归（扫描链，AiCore 利用率崩）；❌ early-exit 数据依赖分支（用 zero-mask 重构）；❌ Toeplitz/FFT-fast 形（碎片 FFT kernel）。
- ⚠️ CKDNet 查无此文，勿引；Channelformer 是 SISO 下行，勿当 MIMO 基准。
- ⚠️ **未测 conv-only baseline 就盲改混合**（最大风险，T0 gating）。

---

## 8. common/ascend_constraints.md 要点（跨族硬件过滤层）

1. Conv = Im2Col + Cube GEMM，NC1HWC0（C0=16），Cube tile 16×16×16。
2. **TransData** = conv(NC1HWC0)↔matmul(NZ/ND) 边界的纯内存重排 → 混合模型的税。msprof 可测。
3. 1×1 conv = 直接 GEMM（无 im2col）；DW/group 饿死 Cube。
4. BN-fold 一类 pass（`ConvBatchnormFusionPass`，消除 norm）；LN/RMSNorm 不可 fold（Vector 归约）；RMSNorm<LN。
5. AutoFuse（="LUBAN"）+ `torch.compile(backend=npu)`；ATC→`.om`。Conv+BN+ReLU / MatMul+Bias+GELU 可融合。
6. 静态 shape 几乎强制（sinking vs Host dispatch）；动态需分桶 + tiling cache。
7. 融合 attn 算子 `npu_fusion_attention`；head_dim÷16，seq≥16。
8. INT8 via AMCT ≈ 2× FP16 Cube；**无原生 INT4 / 复数**（复数须 lowering 为 block-real GEMM）。
9. 通道÷16 对齐。用 msprof 验：TransData 占比、Cube 利用率、Host/Device-bound。

---

## 9. index.json 扩展方案

```jsonc
"families": {
  "wireless_receiver": {
    "detect_hints": "OFDM 时频网格 / Conv-over-freq / symbol/subcarrier 轴 / LLR/CSI 输出 / alpha=sqrt(mean²·2) 归一化",
    "files": {
      "latency_moves": "families/wireless_receiver/latency_moves.md",
      "primitives":   "families/wireless_receiver/primitives.md",
      "failures":     "families/wireless_receiver/failures.md",
      "directions":   "families/wireless_receiver/directions/",   // 整目录
      "raw":          "families/wireless_receiver/raw/"           // 整目录
    }
  }
},
"agent_slices": {
  // Setup: LLM 族检测 → 标签规则筛 2-4 个 direction → 注入命中 direction + 其 RAW
  "hypothesizer": ["common.ascend_constraints","common.principles","common.latency_heuristics",
                   "{family}.latency_moves","{family}.directions/{selected}"],
  "engineer":     ["{family}.directions/{selected}","{family}.raw/{selected}","common.ascend_constraints","{family}.primitives"],
  "analyst":      ["common.ascend_constraints","{family}.failures"]
}
```
`meta.json`（每 direction 一行标签）：`{ascend: friendly|conditional|hostile, latency_tier: fusion|structural|arch|quant, risk: none|low|med|high, physics: yes|no, attention: yes|no|shallow}`——让 direction 选择尽量规则化、少依赖 LLM。

---

## 10. 待验证 / 风险（fail-loud）

1. **无任何 move 在昇腾 + OFDM 接收机场景有公开实测**——所有昇腾判定都是算子形状推断。落地前必须在 Atlas A2 + CANN 上 micro-bench 至少每类一个代表（CVNN-lowered / Mamba-scan / MoE-dispatch / BCR-block-sparse / pointwise-vs-3tap）。
2. D2/D9/D11/SGT/MEAN/LOREN 均为 2025-2026 新文，复现面薄，依赖它们的 move 要标"新文未复现"。
3. Sionna 2023 论文是 TF 版、2024+ 迁 PyTorch——NAS pipeline 钉版本要对齐。
4. 昇腾型号用户暂不确定（310 vs 910）——定型号后复核 INT8/融合能力。
5. **T0 gating**：先测 conv-only baseline（D1 DeepRx 风格）能否达精度，决定要不要把"弃 Transformer"作为正式方向。

---

## 11. 执行清单（确认后实施）

**新增文件**：
- `knowledge_base/common/ascend_constraints.md`
- `knowledge_base/families/wireless_receiver/meta.json`
- `knowledge_base/families/wireless_receiver/primitives.md`
- `knowledge_base/families/wireless_receiver/latency_moves.md`（~30 条）
- `knowledge_base/families/wireless_receiver/failures.md`
- `knowledge_base/families/wireless_receiver/directions/` × 12（D0–D11）
- `knowledge_base/families/wireless_receiver/raw/` × ~13

**修改文件**：
- `knowledge_base/index.json`（注册族 + agent_slices + direction/raw）
- `knowledge_base/README.md`（补三层说明 + ascend_constraints 指向）

**验收**：每个 latency_move 有 `【昇腾】【精度】【物理】【锚定】` 四标；每个 direction 有 meta 标签；raw 每方向 ≥2 份；index.json 合法 JSON。

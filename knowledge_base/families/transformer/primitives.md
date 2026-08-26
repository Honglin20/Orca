# transformer 族结构原语

> 用途：Hypothesizer 在生成「结构假设」时切片读取。本文件列 transformer 族**会用到的原子结构**，每条给出「是什么 / 归纳偏置 / 时延特性」三件套。只描述结构本身，不讲怎么改——「改法」见 `latency_moves.md`，「哪些变体高效且前提是什么」见 `patterns.md`，「已知失败结构」见 `failures.md`。

---

## 1. Multi-Head Attention（MHA）

- **是什么**：标准注意力。`Q,K,V ∈ R^{N×d}` 经 `n_heads` 个独立线性投影后，每个头做 `softmax(Q_i K_i^T / √d_k) · V_i`，拼接后再过一次 `W_O` 投影。`d_k = d_model / n_heads`。
- **归纳偏置**：内容寻址（content-based addressing）+ 头间分工；softmax 提供尖锐的非线性选择，每个头可学到不同子空间的关系（local / syntactic / long-range）。位置信息靠 RoPE/ALiBi 等外部注入。
- **时延特性**：FLOPs `O(N²·d)`、显存 `O(N²)`（注意矩阵本身）。**训练 + 短序列瓶颈是 FLOPs，长序列/解码瓶颈是 attention matrix 的访存与 softmax 的 kernel 效率**。朴素实现 HBM 读写占大头，所以才有 FlashAttention（见 patterns.md）。解码期 Q 只有一个 token 但仍需扫描全部 K/V，是典型 memory-bound。

---

## 2. Linear / Efficient Attention（Performer / Linear / RWKV / RetNet / DeltaNet / HGRN / Mamba）

- **是什么**：把 softmax 注意力改成可分解的形式 `φ(Q)·(φ(K)^T V)`，避免显式构造 `N×N` 矩阵，复杂度降到 `O(N·d²)` 或 `O(N·d)`；多数能写成**线性 RNN**（递归状态 `S_t`），推理期可常数步更新。
  - **Linear Attention**（Katharopoulos 2020）：`φ(x) = elu(x)+1` 或 `ReLU`，去 softmax。
  - **Performer**（Choromanski 2020）：用随机特征（random features）近似 softmax，无偏估计。
  - **RWKV**（Peng 2023）/ **RetNet**（Sun 2023）：把衰减引入线性状态，给「最近更重要」的归纳偏置；RetNet 还保留类似 softmax 的 multiplicative nonlinearity。
  - **DeltaNet**（Schlag 2021）：用 delta rule（`S ← S·(I − β·v k^T) + β·v k^T`）做「按 key 位置减旧加新」的可塑性更新；**Gated DeltaNet** 在其上加 output gate。
  - **HGRN**（Qin 2024）：highway gating 的 RNN，给主干道一个可学 skip，防长程信息塌缩。
  - **Mamba / Mamba-2**（Gu 2023 / Dao 2024）：选择性状态空间模型（selective SSM），状态随输入变化；Mamba-2 用 structured state-space 让 hardware-efficient scan 可行。
- **归纳偏置**：序列是「带衰减/门控的线性流」，放弃 softmax 的尖锐 query-key 选择，换取**线性时间 + 常数推理状态**。Gated DeltaNet 还多一层 output gate 控制每步状态对输出的贡献。
- **时延特性**：**训练 FLOPs `O(N·d²)`（vs MHA 的 `O(N²·d)`），长序列优势显著**；推理期可走 RNN 模式 `O(1)` per token、KV-cache **不复存在**（状态 `S_t` 固定大小），这是对解码时延最大的结构性优势。代价：常数项大、kernel 不如 FlashAttention 成熟，短序列反而慢；`d²` 项在 hidden 大时不可忽略。

---

## 3. Grouped-Query Attention（GQA）/ Multi-Query Attention（MQA）

- **是什么**：
  - **MQA**（Shazeer 2019）：所有 Q 头共享**一组** `K, V`（`n_kv_heads = 1`）。
  - **GQA**（Ainslie 2023）：`n_kv_heads = n_heads / g`（典型 `g=4..8`），是 MHA 与 MQA 的中间插值。Llama-2 70B、Llama-3 全部尺寸、Mistral、Gemma 都用 GQA。
- **归纳偏置**：假设「不同 query 头的 K/V 表示可以共享」——本质是表示空间的一个低秩约束。MQA = 最激进低秩，GQA = 分组共享。
- **时延特性**：**主要省的是解码期 KV-cache 的体积与 HBM 带宽**（按 `n_kv_heads/n_heads` 比例缩）。训练 FLOPs 也有小幅下降（K/V 投影更小）但不是大头。**对长 context、batch=1 解码尤其有效**；对训练或大 batch 收益有限。

---

## 4. 位置编码：RoPE / ALiBi

- **是什么**：
  - **RoPE**（Su 2021）：在 Q、K 上施加每两维一组的旋转，旋转角度与位置成正比；相对位置以 `q_m · k_n = f(m−n)` 形式隐式编码。Llama / Qwen / Mistral / Baichuan 等主流 LLM 默认用 RoPE。
  - **ALiBi**（Press 2022）：不学位置 embedding，直接在 attention logit 上加一个 `-|m−n|·s_head` 的线性偏置（每个头有可学斜率 `s_head`）。
- **归纳偏置**：RoPE 假设「相对位置以旋转形式作用于特征空间，远距离衰减自然出现」；ALiBi 显式注入「近邻优先、远距离线性抑制」。
- **时延特性**：两者**基本不增加 FLOPs**，纯算子层改动。RoPE 旋转在 kernel 中可 fuse；ALiBi 只是一次加法。**真正影响的是长度外推**：ALiBi 天然支持训练长度外的 extrapolation；RoPE 需配 base frequency scaling（NTK-aware、YaRN 等）才能外推，否则长 context 精度塌。

---

## 5. Feed-Forward Network（FFN）/ MLP

- **是什么**：token-wise 的两层 MLP，标准形式 `FFN(x) = Linear_2( σ(Linear_1(x)) )`，`Linear_1: d→4d`、`Linear_2: 4d→d`。激活常用 GELU / ReLU。
- **归纳偏置**：每个位置独立做非线性变换，承担「知识存储 + key-value 式联想」；模型大部分参数（约 2/3）住在 FFN 里。
- **时延特性**：FLOPs `O(N·d²·r)`，`r` 是 expansion ratio（默认 4）。**decode 期 token=1 时 FFN 是 GEMV，memory-bound**；训练期是大 GEMM，compute-bound。改 FFN 的杠杆有三种：缩 `r`、换激活（SwiGLU）、换拓扑（MoE）。

---

## 6. Gated FFN（SwiGLU / GeGLU）

- **是什么**：把 FFN 改成「门控」形式，多一条并行的 gate 投影：`GLU(x) = (Linear_1 x) ⊗ gate(Linear_gate x)`，再做 `Linear_2`。SwiGLU 用 Swish 激活（PaLM、Llama 采用），GeGLU 用 GELU（是 gated 但不双向激活）。
- **归纳偏置**：FFN 的每个中间维是否激活由「内容相关门控」决定，比硬激活更平滑；经验上同参数量下质量更好。
- **时延特性**：要保参数量不变，**必须把 `d_ff` 缩小到约 `8d/3`**（而非 `4d`），因为多了一条 gate 投影。即 `SwiGLU: d → 8d/3, 8d/3 → d`。kernel 仍是 GEMM，访存模式不变。

---

## 7. MoE Expert（稀疏 FFN）

- **是什么**：FFN 替换为 `E` 个并行的 expert FFN + 一个 router（典型 `Linear: d → E` + softmax/top-k）。每个 token 由 top-k（通常 k=1 或 2）个 expert 处理，输出加权求和。Mixtral 8x7B：8 expert / top-2；Switch Transformer：top-1。
- **归纳偏置**：**条件计算**——总参数量大但每个 token 只激活一小部分，等效于「按输入动态选子系统」。router 的归纳偏置是「输入 token 决定路由」。
- **时延特性**：**激活参数量降到 `1/k · E · d_ff`，理论 FLOPs 显著下降**。但实际 decode 期受限于：① expert 间的 all-to-all 通信（多卡）；② router 与 expert 的 kernel launch overhead；③ cache locality 变差。**单卡 decode 提速常不及理论值**，多卡 + 大 batch 才吃满 MoE 的稀疏红利。

---

## 8. KV-cache

- **是什么**：自回归解码时缓存历史 token 的 `K_t, V_t`，避免每步重算。每层每 token 占 `2·d_model·dtype_size` 字节（GQA 下 `2·n_kv_heads·d_k·dtype_size`）。
- **归纳偏置**：无（工程结构）。本质是把「过去 token 的 K/V」当不可变状态。
- **时延特性**：**decode 期最大瓶颈之一**。随 context 线性增长，长 context 下显存和 HBM 带宽双双吃紧；attention 计算变成「Q 一行 vs KV 全表」的 memory-bound GEMV。所有「缩 KV」的结构 move（GQA、KV 量化、KV 压缩、cross-layer 共享、linear attention 去 cache）都是围绕这个瓶颈。

---

## 9. Sliding-Window Attention（SWA）

- **是什么**：每个 query 只 attend 最近 `W` 个 key（窗口内 MHA，窗口外不可见）。常配 1-2 个 global tokens（如 CLS、Longformer）做长程锚点。Mistral 7B 用 SWA = 4096；Longformer / BigBird 是这类设计的代表。
- **归纳偏置**：**局部性假设**——大多数依赖在窗口内。global token 给「跨窗口信息聚合」一条高速公路（信息可通过层层窗口传递，等价于感受野按 `W·n_layers` 增长）。
- **时延特性**：FLOPs `O(N·W·d)`、KV-cache 体积上限 `O(W·d)`（**与 context 解耦**——长 context 不会撑爆 cache）。配合 FlashAttention 的 tiling 实现极快。代价：单层看不见远距离，需堆层或 global token 才能补长程能力。

---

## 10. Prefix-cache / 静态前缀缓存

- **是什么**：把 prompt 前缀（system prompt、few-shot examples、共享文档）的 KV 预先算好并常驻 cache；后续请求只要把新 token 接到前缀 cache 后即可。
- **归纳偏置**：无（工程结构）。
- **时延特性**：**TTFT（time-to-first-token）大幅下降**——前缀 K/V 不再每请求重算。前提是前缀在请求间复用（agent / chatbot 场景天然满足）。可与 PagedAttention、GQA 组合。**与 KV-cache 量化的兼容性好**（前缀可按 int8 存）。

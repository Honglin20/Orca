# D6 · nvidia_nrx_conv_gnn（Conv + 交替 GNN 图消息传递）

> 一句话定位：init Conv + **交替**[GNN 用户图消息传递 → Conv 状态更新]×N_it —— NVIDIA 5G NR GPU 加速接收机，**<1ms on A100+TensorRT**，输出 coded-bit LLR。

## 结构
- **输入张量**：MIMO 接收 `Y ∈ R^{B×N_rx×N_freq×N_sym}` + pilot。
- **主干**（迭代）：
  1. **Init Conv**：pilot 估计初值。
  2. **交替迭代 ×N_it**：
     - **GNN 子步**：在用户图上做消息传递（**图节点 = MIMO 层全连接**，即每个 MIMO 层是一个节点、层间全连边，**不是 RE↔user 二部图**），**mean 聚合 MPNN**。
     - **Conv 子步**：separable 3×3 conv 更新节点状态。
  3. 输出头：coded-bit LLR。
- **attention?**：**no**（GNN MPNN，无 softmax attention）。

## 为什么降时延
1. **NVIDIA 实测 <1ms on A100+TensorRT** —— 工业级加速栈完整（kernel fusion + TRT INT8）。
2. 无 softmax attention → 无 QK^T、无 NZ 格式 matmul → 昇腾 Cube 友好。
3. separable 3×3 conv + GNN mean-aggregation 全是 GEMM/聚合 kernel，无 domain crossing。

## 昇腾友好性
**✅ friendly** —— 全 GEMM + mean scatter/gather；GNN 邻接矩阵实际是 dense 小矩阵（MIMO 层全连接），可用 dense GEMM 实现，避免稀疏 GNN kernel 不友好。

## 物理依据
**yes** —— MIMO 层间干扰是物理耦合（空间相关），全连接图建模层间互相关；mean 聚合对应 MMSE-style 加权合并的近似。

## bundle 的 move
**M15**（conv+GNN baseline，达标则弃 Transformer）+ **M1/M2/M3**（融合层）+ **M16**（TRT/AMCT INT8 叠加，对应 NVIDIA 部署栈）。

## 结构前提与坑
1. **图结构是 MIMO 层全连接，不是 RE↔user 二部图** —— 常见误读，NVIDIA NRX 原作明确是 layer-node 全连接图。
2. **交替 ≠ 顺序 Conv→GNN** —— 论文是 Conv 与 GNN **交替迭代**（每 N_it 内先 GNN 再 Conv 状态更新），顺序写错会掉点。
3. separable 3×3 的 **DW 分支** 同 D1 坑（Cube 饿死）—— 落地可换标准 3×3 或 pointwise。
4. **N_it 是精度-时延旋钮** —— 部署可调；但 N_it 改变静态图形状，需 sinking dispatch 重编译缓存。
5. NVIDIA TRT 栈在昇腾需对应 CANN + GE，部分融合算子可能不等价，需 msprof 复核。

## 来源
[arXiv:2312.02601] NVIDIA NRX（2023，部署实测 A100+TRT）。

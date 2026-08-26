# Puzzle Layer-Variant 搜索空间重构 — 设计草稿 (SDD)

> 跨阶段设计议题，puzzle layer 粒度重构各 phase SPEC 撰写前必读。
> **继承** [`puzzle-universal-design-draft.md`](puzzle-universal-design-draft.md) 的通用外壳（flat/adapters/manifest 适配器架构 + BLD/MIP/GKD decomposed 内核 + 四道 smoke + verifier 闭环）；
> **本文件**只改一处：**搜索空间粒度从「attention/ffn 子块」升级到「整个 transformer encoder layer」**，候选从「单 attention/ffn 块」升级到「完整 transformer layer 变体」。
> 范式不变：Puzzle (Bercovich 2025, ICML) decomposed NAS —— 逐层独立 BLD 蒸馏 + MIP 全局选 + GKD 末段重训。
> 外壳/知识管理借鉴 [`nas-supernet-v3`](../../workflows/nas-supernet-v3.yaml)（`ns3_expand_supernet` 的 model_type 知识库 + sandwich gate；`ns3_train_script` 的节点内编排 + subagent 校验 + 确定性 gate 范式），但 **decomposed ≠ supernet** —— Elastic*/超网/`set_sample_config`/`get_active_subnet` 一律不进运行时（仅作"去 elastic 原生化"的素材来源）。

---

## 0. 目标与已确认决策

**用户诉求**：puzzle 当前搜索空间是 attention/ffn 两个子块粒度（slot=self_attn/ff）。要改成 **slot = 整个 transformer encoder layer**（target 项目里的 `TransformerEncoderLayer`），候选 = **transformer layer 计算方式变体**（synthesizer / fnet / relu-attn / vanilla / …）。寻优目标是"找到一个好的 transformer 计算方式"，**不寻优 ffn / depth / width**。

**已确认决策**（2026-08-13 用户 /goal 拍板）：

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| L1 | 搜索空间粒度 | **slot = transformer encoder layer**（整层） | 寻优"transformer 计算方式"的最小完整单元是层，不是子块；layer 占比 > block 占比，更易达时延目标（解 CURRENT.md 的 target 70% 不可达） |
| L2 | 候选形态 | **完整 transformer layer 变体**（attn 变体 + 标准 FFN + 2×norm + 2×residual） | "替换整层"语义要求候选自包含；nas-agent `Elastic<Block>` 的 `get_active_subnet()` 已是该形态 |
| L3 | 寻优维度 | **只 attention 机制**（封装进 layer 变体）；**不寻优 ffn/depth/width** | 用户明确：现阶段只找好的 transformer 计算方式 |
| L4 | FFN 处理 | 所有非 identity 变体用**统一标准 FFN**（中间维=原层 `original_intermediate`，激活=原层 `activation`）；FFN 不进搜索空间 | L3 的直接推论 |
| L5 | 候选素材来源 | **nas-agent blocks 去 elastic 原生化**（`D:\Projects\Orca\nas-agent\nas_agent\blocks\`） | 用户指定；nas-agent 已有成熟 attention 变体实现 |
| L6 | transformer layer 识别 | **forward 结构特征判定，禁类名匹配**（参考 ns3 `model_type.json` 知识库思路） | general workflow 铁律：每个用户有自定义 transformer，类名不可假设 |
| L7 | 知识管理 | **先只做 transformer 族**，目录结构预留 conv/rnn/moe 族 | 用户：现在简单点，先 transformer；做好目录设计可扩展 |
| L8 | 节点范式 | **节点=小编排者**：subagent 完成任务 + subagent 校验 + 确定性脚本 gate（参考 `ns3_train_script`） | 弱模型（deepseek-v4-flash）单节点任务要单一；编排+校验分离 |
| L9 | 洁净契约 | agent.md = 产品说明书（无过程描述）；**可固化成脚本的不出现在 .md** | 用户明确；`tars validate` 洁净检测 + 完工洁净审查 |
| L10 | 验收 | target E2E：pretrain acc 90%+，时延有优化，精度损失 <1% | 用户 /goal 验收标准 |
| L11 | 变体维度来源 | **所有维度从 `slot` 注入（slot 值 = pz_search_space 从原层提取的真实维度），变体库零维度硬编码**。例外：**算法超参**（如 softs_star 的 `core_dim`）非项目维度，catalog 默认 + 可 user override | general 铁律：变体库对任意 transformer 项目通用；"去 elastic" = 去运行时可变（`set_sample_config`/`sample_*`），**不是**写死常量（spec-reviewer LV-9 维度 vs 算法超参区分）|
| L12 | ACC AC（闭环 LV-1 BLOCKER）| **高 baseline 相对容差**：`baseline≥0.5 → threshold = max(baseline−0.5, baseline×0.99)`（取更严者）；低 baseline（<0.5）保留 v2 比例保护 `baseline×0.9`。**gate AC = 验收 AC**（单一真相源）| v2 D5 绝对容差 0.5 对高 baseline（target 0.9919）松到 final≥0.4919，与 <1% 验收矛盾；改相对容差后 target→max(0.4919, 0.9819)=0.9819 对齐 <1%（UP-1 A；同步修 v2 §12.1）|
| L13 | mask 处理（闭环 LV-3，参考 ns3 自适配）| **自适配 + 运行时 trace，不 E8 硬过滤**：layer 变体 forward 统一收 mask（`_extract_mask` 从 positional/kwargs 抽）；vanilla 真用 mask（`nn.MultiheadAttention` attn_mask），无 scores 语义的 mixer（fnet/synthesizer/softs）接受但忽略；`mask_load_bearing` 由 pz_baseline 运行时 trace（检查 src_mask 实际值非 None）判定，非签名静态判 | ns3 supernet attention 真 mask（加 attn scores）；target 运行时 src_mask=None→mask_load_bearing=false，mask 问题不触发；mask-blind 变体在 mask-bearing slot 的精度损失由 MIP acc 自然惩罚（不硬过滤）（UP-2）|
| L14 | norm（闭环 LV-4）| **第一版只 Pre-LayerNorm 变体**（nas-agent 源结构，去 elastic 最直接）；Post-LN→Pre-LN 迁移收敛风险在 §11 R1 + §9 不达标分析显式承担 | Rule 2 Simplicity First；若 target E2E 不达标再迭代加 post_ln 变体族（UP-3 A）|

---

## 1. 与 puzzle-universal v2 的差异（block → layer 粒度）

| 维度 | v2（block 粒度） | 本草稿（layer 粒度） |
|---|---|---|
| slot 单元 | `self_attn` / `ffn` 子块 | 整个 transformer encoder layer |
| slot `kind` | attention / ffn / conv / moe / custom | **`transformer_layer`**（先唯一；预留 cnn_block / rnn_block 族） |
| 候选形态 | 单块（forward(x) 单参，无 norm/residual） | 完整 layer（attn 变体 + FFN + 2×norm + 2×residual） |
| 寻优维度 | attention 类型 + ffn 剪枝 | **仅 attention 类型**（封装进 layer 变体） |
| identity 语义 | 保留 father-loaded 子块 | **保留 father-loaded 整层**（原 attn + 原 ffn + 原 norm/residual） |
| BLD teacher 信号 | 子块 (in, out) | **层 (in=src, out=src')** |
| floor 测量 | 全 block 置零（attn+ffn） | **全 layer 退化为最小计算层**（见 §6.7） |
| 通用外壳 | flat/adapters/manifest/smoke/verifier | **完全继承，零改动** |

**继承不变**（v2 已闭环 25 issue）：适配器 13 项 API、四道 smoke、BLD/MIP/GKD 内核 loss、时延铁律（ONNX 单文件 + 禁 fallback）、verifier 闭环、ACC/LAT AC、reuse_check 幂等。

---

## 2. search_space 新 schema

```yaml
slots:
  - {id: L1, path: encoder_layer1, kind: transformer_layer,
     layer_idx: 1, in_dim: 128, out_dim: 128,
     source_class: TransformerEncoderLayer,
     num_heads: 4, head_dim: 32,
     original_intermediate: 256, activation: relu,
     norm_type: layernorm,            # 原层 norm 类型（溯源，变体可不照搬）
     forward_arity: single, return_arity: single,
     mask_load_bearing: false,
     layer_evidence: "forward = attn → norm1(res+drop) → ffn → norm2(res+drop)；含 attention 机制 + FFN + 2×norm + 2×residual 组合（结构特征，非类名）"}
  ...
candidates:
  transformer_layer:
    - identity                       # 保留 father-loaded 整层（必入，MIP floor 锚）
    - vanilla_layer                  # 标准 MHSA layer（对照基线）
    - random_synthesizer_layer
    - relu_attention_layer
    - fnet_layer
    - softs_star_layer
```

### 2.1 slot 字段（相对 v2 的变更）
| 字段 | 变更 | 说明 |
|---|---|---|
| `kind` | **改**：layer 粒度下先唯一 `transformer_layer` | 替代 v2 的 attention/ffn |
| `path` | 不变 | `model.get_submodule(path)` 指向整层（`encoder_layer1`，非 `encoder_layer1.self_attn`） |
| `num_heads`/`head_dim`/`original_intermediate`/`activation` | **从原层的 attn/ffn 子模块提取**（层的属性） | 变体构造用：attention 用 num_heads/head_dim，FFN 用 original_intermediate/activation |
| `max_seq_len` | **新增** | 来源：pz_baseline trace 原层输入序列长度（实测回填）。random_synthesizer 等 mixer 预分配 `[1, max_seq_len, max_seq_len]` mixing matrix 用；**禁 fallback**（fallback 512 对 target seq=16 会过参化 2.6M 参数，BLD 优化万倍过参矩阵——spec-reviewer LV-7） |
| `norm_type` | **新增**（layernorm/rmsnorm/...） | **溯源记录**（原层 norm 类型），**非脚本 dispatch 依据**（L14：第一版只 Pre-LayerNorm 变体，catalog 固定 `nn.LayerNorm`，norm_type 不参与变体选择） |
| `mask_load_bearing` | 不变 | 原层 attn forward 收 mask 且上游实际传非 None → true |
| `layer_evidence` | **改**：层级结构特征证据（替代 kind_evidence） | "attn+ffn+2norm+2residual 组合"判定（见 §3） |

### 2.2 候选 = layer 变体（三源，同 v2 哲学）
- `identity` = 保留 father-loaded 整层（铁律，必入每 slot）
- 裸字符串（`vanilla_layer`/`fnet_layer`/...）= 引用 layer 变体 catalog（§4）
- `{name, factory, applies_to, params}` = user 自注册（同 v2 §5.3，零特殊代码）

---

## 3. transformer layer 识别（standard serial Pre/Post-LN transformer，结构特征共性，禁类名）+ 知识库

**general 铁律**：不假设类名（用户 transformer 可能叫 `EncoderBlock`/`TransformerLayer`/`MyEncoder`/...）。**靠 forward 结构特征**判定一个 nn.Module 是不是"可替换的 transformer encoder layer"。**第一版识别范围收窄**：**standard serial Pre-LN / Post-LN transformer**（2 norm + 2 residual + 1 attn + 1 FFN 串行拓扑）。

### 3.1 判定规则（确定性结构特征）
一个模块被判为 `transformer_layer` 当且仅当其 forward 同时呈现：
1. **attention 机制**：子模块 forward 含 `matmul(Q, K^T)` 缩放 + softmax/relu 归一（v2 §6.2 attention 证据）。**非标 attention 兜底识别**：若 matmul(Q,K^T) 不可直接观察（如 linear attention 改 `matmul(K, V)` 先于 Q、SOFTS 的 `attention_matrix_applied_to_value` 等），用"输出 = `matmul(<seq-mix matrix>, value_proj(x))`"作为间接证据（存在可训练/可计算的 seq-mixing matrix 作用于 value）。
2. **FFN**：`Linear → 激活 → Linear` 主导（v2 §6.2 ffn 证据）
3. **2× norm + 2× residual** 组合（Pre-LN 或 Post-LN 均可）

**粒度停在"整层"，不下钻到 attn/ffn 子块**（与 v2 的关键区别）。组合层（attn+norm+ffn+norm）才入 slot；纯 attn / 纯 ffn 子块不入。

**已知局限（第一版诚实声明）**：**Parallel 残差**（FlashFormer / GPT-J 式 `x = x + attn(l) + ffn(l)` 共享 1 norm）与 **GAU（Gated Attention Unit，单 norm + gate）** 等单 norm 拓扑**第一版不支持**——L14 决策只做 Pre-LayerNorm serial 变体族，候选库无 parallel/GAU 对应变体。识别到此类拓扑时 `pz_search_space` 标 `model_type_supported=false`（fail loud），族扩展留后续 `transformer_layer_pattern.json` 加 `parallel_block`/`gau_block` layout（OCP）。

### 3.2 知识库（参考 ns3 `model_type.json`）
新建 `$ORCA_AGENT_RESOURCES/references/transformer_layer_pattern.json`（pz_search_space 节点资源目录）：

```json
{
  "kind": "transformer_layer",
  "structural_signature": {
    "attention": {"matmul_qkt_scaled": true, "normalization": ["softmax", "relu"]},
    "ffn": {"linear_act_linear": true},
    "norm_count_min": 2,
    "residual_count_min": 2,
    "layout": ["pre_ln", "post_ln"]
  },
  "evidence_template": "forward = {attn} → {norm1}({res}+{drop}) → {ffn} → {norm2}({res}+{drop})",
  "must_extract": ["num_heads", "head_dim", "original_intermediate", "activation", "norm_type", "max_seq_len", "mask_load_bearing"],
  "reject_when": ["纯 attention 无 ffn", "纯 ffn 无 attention", "无 residual 组合", "单 norm（Parallel/GAU 拓扑，第一版不支持——见 §3.1 已知局限）"]
}
```

LLM 读此知识库 → 对照用户层 forward → 判定 + 填 `layer_evidence`。`transformer-layer-evaluator`（§5.2）审证据是否支持判定（有确定性硬规则）。

### 3.3 目录结构（知识管理，可扩展族）
```
workflows/agents/pz_search_space/
├── agent.md
├── references/
│   ├── transformer_layer_pattern.json   # §3.2 当前族判定知识库
│   └── (未来) cnn_block_pattern.json / rnn_block_pattern.json / moe_block_pattern.json
├── assets/
│   └── layer_variant_catalog.yaml        # §4 候选 catalog（identity + 各 layer 变体）
└── scripts/
    ├── reuse_check.sh
    ├── check_search_space.py             # 确定性 gate（schema + sandwich + identity 必入）
    └── search_space_table.py             # 推图表（non-blocking）
```
族扩展（L7）：加一个 `*_pattern.json` + catalog 加该族变体，零核心改动（OCP）。

---

## 4. layer 变体库（nas-agent 去 elastic 原生）

### 4.1 去弹性原生化方法（L5）
nas-agent 的 `Elastic<Block>`（如 `ElasticRandomSynthesizerBlock`，`nas-agent/nas_agent/blocks/random_synthesizer.py:124`）含：
- elastic 机制：`super_*` 维度 / `set_sample_config` / `get_active_subnet` / `elastic_num_params`（**运行时禁用**）
- **原生结构**：`get_active_subnet()` 返回的固定维度 `Block`（如 `RandomSynthesizerBlock`，random_synthesizer.py:203-222）——**attention 计算结构**的来源。**注意**：`get_active_subnet()` 的产物**并非都是完整 layer 形态**——`softs_star_mixer.py:153-166` 的 `SOFTSSTARMixerBlock` 只含 `star + 1 norm`，**无 FFN / 无第二 norm**。

**原生化**：取 `get_active_subnet()` 的 **attention 计算结构**（mixer/attn core），FFN + 2×norm + 2×residual 由 layer 骨架（`_PreLNTransformerLayer` + `_StandardFFN`）统一补全，使每个变体都成为完整 transformer encoder layer。**所有维度从 `slot` 注入**（`slot.in_dim`/`num_heads`/`head_dim`/`original_intermediate`/`activation`/`max_seq_len`，值由 pz_search_space 从**原层 + 输入 trace 提取**写入 search_space.yaml），不经 elastic。**变体库零维度硬编码**（禁出现 `head_dim=32`/`ffn_dim=256` 等项目常量）——变体对任意 transformer 项目通用（L11）。"去 elastic" = 去掉运行时可变（`set_sample_config`/`sample_*`），**非**写死常量。**Pre-LayerNorm** 残差结构（对齐 `transformer_layer_variants.py:267-268` 的 `nn.LayerNorm` 实现；变体自带 LayerNorm，不强制照搬原层 norm 类型，R1）：
```
x → norm1(LayerNorm) → attn_variant → +residual → norm2(LayerNorm) → ffn → +residual → out
```

### 4.2 第一版变体集（transformer attention 族，L7 先简单）
| 变体名 | attention 机制 | nas-agent 源 | 参数 |
|---|---|---|---|
| `identity` | 保留原层 | —（passthrough） | — |
| `vanilla_layer` | 标准 MHSA | puzzle `_VanillaMHSA` | num_heads/head_dim |
| `random_synthesizer_layer` | 学习型 token 混合矩阵（无 QK） | `blocks/random_synthesizer.py` | num_heads/head_dim/max_seq_len |
| `relu_attention_layer` | ReLU(logits)/L 替 softmax | `blocks/relu_attention.py` | num_heads/head_dim |
| `fnet_layer` | 零参 2D-DFT mixer | `blocks/fnet_fourier_mixer.py` | — |
| `softs_star_layer` | SOFTS STAR 聚合-重分配 | `blocks/softs_star_mixer.py` | `core_dim`（**算法超参**，非 slot 维度；catalog 默认 64，可 user override，对应 `make_softs_star_layer(slot, core_dim=64)`） |

每个变体 = `(attention 变体) + (标准 FFN: Linear→act→Linear, intermediate=slot.original_intermediate, act=slot.activation) + 2×LayerNorm + 2×residual`。**FFN 统一不进搜索**（L4）。mask-aware 变体（如 `masked_vanilla_layer`）按需加（mask_load_bearing slot 用）。

### 4.3 变体实现位置
新建 `workflows/agents/_puzzle_scripts/transformer_layer_variants.py`（去 elastic 原生 layer 变体库，builtin 源）。catalog `assets/layer_variant_catalog.yaml` 用 `transformer_layer_variants::make_<name>_layer` 引用。`puzzle_common.load_catalog` 复用（factory 绑定机制不变）。

### 4.4 factory 契约
`factory(slot, **params) -> nn.Module`，返回完整 layer（in_dim→out_dim，序列长度不变）。**layer 变体不经外层 `_wrap`/`_wrap_mask`**——layer 自包含 `forward(x, src_mask=None, *args, **kwargs)` 签名（`_extract_mask` 内部抽 mask kwargs 适配异构父层，对齐 `transformer_layer_variants.py:28-29`）；`_wrap`/`_wrap_mask` 仅 v2 单块候选用。维度对齐铁律：外部 in_dim/out_dim 固定。

**维度全部从 `slot` 取**（`num_heads`/`head_dim`/`original_intermediate`/`activation`/`max_seq_len`），变体实现内**禁出现硬编码维度常量**（L11）。`max_seq_len` 等需预分配的量由 pz_baseline trace 原层输入序列长度回填进 slot，不写死。catalog loader 用 `functools.partial` 仅绑定**非维度类算法超参**（如 mixer 的聚合 ratio）成统一 `factory(slot)`。

---

## 5. 3 节点拆分（替代 pz_expand）

依赖单向：`pz_ingest → pz_search_space → pz_baseline → pz_build_library → …`（后续节点不变）。

### 5.1 pz_ingest（项目接入）
- **职责**：读源码 → `flat.py` + `puzzle_adapters.py`（13 项 API）+ `manifest.yaml`。flat 与 adapters 共享 forward 签名契约（L8 强耦合，合一）。
- **subagent**：`project-porter`（移植 adapters，参考 ns3_train_script porter 分工 0/1/N）。
- **校验**：`workflow-verifier`（flat forward 一致性 + standalone __main__ smoke）。
- **gate**：`scripts/check_ingest.sh`（py_compile flat + adapters + manifest schema 机械校验 + forward-convention 一致性 grep）。
- **output_schema**：`flat_model_path` / `adapters_path` / `manifest_path` / `ingest_passed` / `error` / `generated_artifacts`。
- **路由**：`ingest_passed != false` → pz_search_space；→ terminate_ingest_failed。

### 5.2 pz_search_space（搜索空间声明 + 自审）★ 核心节点
- **职责**：读 flat + 用户源码 → 识别 transformer layer slot（§3 结构特征，不靠名字）+ 声明 candidates（§2.2）→ `search_space.yaml`（声明版，dim 留 -1）。
- **subagent**：`transformer-layer-evaluator`（审 slot 判定证据 / path 定位 / identity 必入 / mask 一致性 / 字段完整）+ `workflow-verifier`（search_space checklist）。
- **知识库**：读 `references/transformer_layer_pattern.json`（§3.2）。
- **gate**：`scripts/check_search_space.py`（schema 合规 + identity 必入 + catalog 注册有效 + sandwich 预检——参考 ns3 `check_search_space.py` 的 `.baseline.json` marker 思路，layer 粒度记录真实 layer 数）。
- **output_schema**：`search_space_path` / `slot_count` / `model_type_supported`（空 slots→false） / `error` / `generated_artifacts`。
- **路由**：`model_type_supported != false` → pz_baseline；→ terminate_unsupported。

### 5.3 pz_baseline（基线测量 + 时延铁律首落地 + 可达性）
- **职责**：跑 `measure_baseline.py`（4 道 smoke + 测父模型 acc/latency + trace 回填 layer dim + 落 block_map + layer-floor 可达性判 exit 0/2/3）+ 时延铁律首次实际执行（用户提供 latency_script → 导 ONNX 调 fn）。
- **subagent**：`memory-verifier`（artifacts 一致性）。
- **gate**：`measure_baseline.py` exit code（0/2/3 三态）。
- **output_schema**：继承现 pz_expand 的测量字段（`baseline_acc`/`baseline_latency`/`block_map_path`/`latency_target_feasible`/`max_achievable_reduction`/`error`/...）。
- **路由**：exit 0 → pz_build_library；exit 3 → terminate_latency_infeasible；exit 2 → terminate_unsupported / fail loud。

### 5.4 verifier/subagent 归属
| subagent | 归属节点 | 审什么 |
|---|---|---|
| project-porter | pz_ingest | 移植 adapters |
| workflow-verifier | pz_ingest + pz_search_space | flat 一致性 / search_space checklist |
| transformer-layer-evaluator | pz_search_space | slot 判定证据 + schema（新建，参考 ns3 supernet-evaluator） |
| memory-verifier | pz_baseline | artifacts 一致性 |
| project-fidelity-verifier | pz_build_library / pz_retrain（不变） | adapters 忠实度 |

---

## 6. 内核 layer 粒度适配（BLD/score/mip/materialize/gkd/gate/floor）

内核 loss/optimizer **零改动**（BLD normalized-MSE / MIP grouped-knapsack / GKD cosine+KL / gate 方向感知公式）。BLD/score/mip/gkd/gate 的**循环逻辑**只改 slot 粒度 + dispatch（kind=transformer_layer）。**例外：materialize 要实质改**（见 §6.4——它硬编码 block variant 构造，非 layer 无关）。

### 6.1 BLD（bld.py）
- teacher 信号：`capture_parent_activations` 抓 **layer 的 (in=src, out=src')**（parent_module_path 指向整层，现有 hook 机制直接适用）。**mask 传递契约**：teacher hook 抓父层 I/O 时记录 src_mask 实际值；student（layer 变体）forward 统一收 mask（`_extract_mask` 从 positional/`attn_mask`/`src_mask`/`attention_mask`/`mask`/`key_padding_mask` 抽，对齐 `transformer_layer_variants.py:64-72`）。
- mask 用法分层（L13）：vanilla_layer 真用 mask（`nn.MultiheadAttention` 的 `attn_mask`，mask-aware）；无 scores 语义的 mixer（fnet/random_synthesizer/relu_attention/softs_star）接受 mask 但忽略（mask-blind，forward signature 一致仅为父层签名兼容）。
- **mask_load_bearing 判定**：pz_baseline 运行时 trace（检查父层调用时 `src_mask` 实际值非 None）判定，**非签名静态判**（签名有 mask kwarg 不代表上游真传）。mask-blind 变体在 mask-bearing slot 的精度损失由 MIP acc 自然惩罚，**不 E8 硬过滤**（mask-aware 变体不足时由 acc 倒逼扩展，族扩展靠 OCP 加 `masked_*_layer`）。
- student：layer 变体 factory(slot) 实例化，BLD 把它蒸馏到模仿原层 I/O（normalized MSE）。
- dispatch：`candidates[slot.kind]`（kind=transformer_layer）。

### 6.2 score（score.py）
- replace-1-layer 打分：把某层换成某变体（冻结其余），calib 上算 block-distance（adapters.kd_loss）。
- per-variant latency：层粒度 latency（latency_table）。

### 6.3 mip（mip_select.py）
- 分组键 `(layer_idx,)`（每层一个 transformer_layer slot，选一个变体）。
- grouped knapsack：max Σscore s.t. Σlatency ≤ target，每层恰选一变体。**零逻辑改动**（kind 维度坍缩到单一 transformer_layer）。
- `selected_arch = {layer: {transformer_layer: variant}}`。

### 6.4 materialize（materialize_optimized.py）—— 实质改动（reading 代码修正"不变"误判）
现 materialize **硬编码 block 粒度 variant 构造**：`_VARIANT_CONSTRUCTION`（fnet/random_synthesizer/.../ffn_75/no_op 单块）+ `_variant_dispatcher_src` 镜像 `puzzle_blocks.make_*`（单块 + `_KwargPassthrough` 包装）+ `_VARIANT_MODULES` 内联 nas_agent `Elastic*Core`（**elastic 类**）。layer 粒度要改：
- `_VARIANT_CONSTRUCTION` → layer 变体表（`vanilla_layer`/`random_synthesizer_layer`/`relu_attention_layer`/`fnet_layer`/`softs_star_layer`/`no_op_layer`）。
- `_variant_dispatcher_src` → 镜像 `transformer_layer_variants.make_*_layer`（**整层构造，不用 `_KwargPassthrough`**——layer 自包含 `forward(x, src_mask, *args, **kwargs)`）。
- `_VARIANT_MODULES` → 内联 `transformer_layer_variants` 的**去 elastic 原生 layer 类**（非 nas_agent `Elastic*Core`）+ `_PUZZLE_BLOCKS_HELPERS` 调整（layer 变体自含 `_StandardFFN`/attention cores）。
- `_needed_slot_fields` → 加 `max_seq_len`（random_synthesizer_layer 用）。
- `_apply_selected_arch` 的 `_setattr_slot` **不变**（setattr 整层/子块通用）；`build_model`/`load_model`/key 对齐机制**不变**（layer 粒度自动适配）。

### 6.5 gkd（gkd_retrain.py）
- 末段全局 KD：cosine hidden + KL logits[分类]，KDWeightScheduler warmup。
- student = optimized_flat（layer 变体已装配）。不变。

### 6.6 gate（gate_report.py）—— 参数化 AC 单一真相源
- 测 final_model acc + latency，对照 baseline → **ACC AC**（L12：`baseline≥0.5 → max(baseline−0.5, baseline×0.99)`，低 baseline `<0.5 → baseline×0.9`）+ **LAT AC**（`latency_opt ≤ baseline×(1−latency_reduction_target)`，`latency_reduction_target` 默认 **0.5**，Phase L5 据 R4 实测调整）。
- **gate AC = 验收 AC**：§9 验收标准引用本节同一 AC 公式，**禁双标准**（不再并存 "<1%" 与 v2 D5 绝对容差；不再并存 "<baseline 即可" 与参数化 LAT AC）。

### 6.7 floor 测量（layer 粒度，改 measure_baseline.py）
- **layer-floor** = 所有 transformer layer 退化为"最小计算层"的整模 latency。
- `_FloorLayer` / `_NoOpLayer`：**forward(x) 返回 x（layer-passthrough，跳过 attn+ffn+norm）**——替换 v2 block 粒度的 `_FloorZeroModule`（`return zeros`）。**block 粒度的 zero 语义在 layer 粒度非法**：layer 是 residual unit（`x = x + attn(...)`），整层 `return 0` 破坏 residual stream → 该层输出恒零 → 后续层输入全零崩溃。layer-passthrough（`return x`）层被旁路（latency≈0），保 residual stream 完整。`_NoOpLayer` 已改 `forward(x)=x`（对齐 `transformer_layer_variants.py:336-354`）。
- `max_achievable_reduction = 1 − floor/baseline`。layer 占比 > block 占比 → 比 v2 更易达目标（解 CURRENT.md target 70% 不可达）。
- 注：floor 是"时延目标结构性可达性"的早退判断，非"模型仍有效"的承诺（全直通模型无效，但 MIP 不会选全 no_op——有 acc 约束）。

---

## 7. 跨节点 self-heal 机制

拆分后 self-heal 从"节点内 fix-loop"变为"重跑 workflow + 节点幂等跳过"：
- 每节点 `reuse_check.sh`（产物在 + 达标 → 跳过重做），参考 ns3。
- **节点内 fix-loop ≤ 3**（单节点产物问题，如 schema 违规）。
- **跨节点根因**（baseline smoke 失败根因在 ingest adapters）：baseline 节点 fail loud → 标注根因字段 → **人工删问题节点 artifacts 目录**（`rm -rf $ORCA_ARTIFACTS_DIR/<问题节点>`，如 baseline 失败因 ingest adapters，删 ingest 产物）→ 重跑 workflow，`reuse_check` 幂等跳过健康节点、只重跑被清空的问题节点。
- **Orca CLI 现无 force-rerun flag**（`orca/iface/in_session/cli.py` 的 `bootstrap`/`next` 两命令无 `--force`/`--rerun`，2026-08-13 确认）→ 退化到"人工删产物 + reuse_check 跳健康"运维动作（R3）。

---

## 8. 洁净契约要求（L9，完工强制审查）

- agent.md body = **产品说明书**（受众翻转通读：另一个 agent 读它能干活），**无过程描述**（"我先…然后…若失败则…"禁）。
- **可固化成脚本的不出现在 .md**：schema 字段定义、gate 检查规则、路径构造 → 落 `scripts/*.py` / `references/*.json`，agent.md 只引用。
- 禁开发期残留（plan/issue 编号、Orca 源码路径、内部 examples 路径、测试项目名硬编码）。
- 完工按 [`agent-prompt-cleanliness-contract.md`](../../orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md) 做洁净审查 + `tars validate` warning 清零。

### 8.1 洁净范例：`pz_search_space/agent.md` body 片段（产品说明书体）

下例示范"输入契约 / 产出契约 / 校验脚本引用"三段式产品说明书体——**禁**"先读 X 然后 Y 若失败则 Z"过程描述；**禁**内联 schema 字段（落 `references/transformer_layer_pattern.json`）；**禁**内联 gate 规则（落 `scripts/check_search_space.py`）。参考 ns3 agent.md body 风格。

```markdown
# pz_search_space

You are the **search-space declaration** folder-agent of the puzzle pipeline:
starting from the prepared `flat.py` + `puzzle_adapters.py` of the upstream
`pz_ingest`, identify transformer-layer slots by structural evidence (no class-name
matching), declare candidates per slot, and produce `search_space.yaml`.

## Required Inputs

- `{{ pz_ingest.output.flat_model_path }}`: the flattened model (relative to `$ORCA_ARTIFACTS_DIR`).
- `{{ pz_ingest.output.adapters_path }}` / `{{ pz_ingest.output.manifest_path }}`.
- `$ORCA_AGENT_RESOURCES/references/transformer_layer_pattern.json`: slot kind
  judgment knowledge base (structural signature + evidence_template + must_extract).
- `$ORCA_AGENT_RESOURCES/assets/layer_variant_catalog.yaml`: candidate catalog.

## Produced Artifacts

- `$ORCA_ARTIFACTS_DIR/search_space.yaml` — declared search space
  (`kind: transformer_layer` slots, dim placeholders filled with measured values
  by the downstream `pz_baseline`).

## Subagent Invocation Protocol (point-to-file)

Invoke `transformer-layer-evaluator` and `workflow-verifier` per the host
protocol. Their bodies live at `{{ subagents_root }}/<name>.md`.

## Workflow

### Step 0: Reuse-Check

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/reuse_check.sh"
```

### Step 1: Identify Slots by Structural Evidence

Read `references/transformer_layer_pattern.json` (cwd-independent via
`$ORCA_AGENT_RESOURCES`). Inspect `flat.py` forward; mark each module whose
forward exhibits (attention + FFN + 2×norm + 2×residual) as a `transformer_layer`
slot. Reject pure-attn / pure-ffn / single-norm topologies per the knowledge base.

### Step 2: Declare Candidates

For each slot, emit candidates: `identity` (mandatory) + the layer-variant names
from `layer_variant_catalog.yaml` applicable to `transformer_layer` kind.

### Validation (hardened-script gate)

```bash
bash "$ORCA_AGENT_RESOURCES/scripts/check_search_space.sh" \
  || { echo "FAIL" >&2; exit 1; }
```

## Output (JSON enforced by output_schema)

The entire final reply = one line of valid JSON with fields:
`search_space_path` / `slot_count` / `model_type_supported` / `error` /
`generated_artifacts`.
```

**反例（禁）**：`"我先读 model_type.json，然后对照 flat.py 的 forward，若发现单 norm 就 reject，最后再填 candidates..."` — 过程描述，应转为 Step 1 的产品说明书式陈述（如上）。

---

## 9. 验收（target E2E，L10）

- **入口**：`opencode run`（headless）→ 主 agent 意图触发 tars skill → skill 编排 agent 调 `orca` CLI 驱动 puzzle workflow（in-session 模式，连 chart daemon）。
- **目标项目**：`D:\Projects\playground\target`（`model.py` 的 `CrossFusion`，4 个 `TransformerEncoderLayer`）。
- **pretrain**：用已训 100-epoch 模型（baseline acc 0.9919，见 CURRENT.md）。
- **验收标准**（gate AC = 验收 AC，单一真相源——引用 §6.6 参数化公式，**禁双标准**）：
  1. 端到端跑通到 pz_report gate（不中途 terminate）。
  2. **ACC AC**（L12 公式）：target baseline 0.9919 ≥ 0.5 → `threshold = max(0.9919−0.5, 0.9919×0.99) = max(0.4919, 0.9819) = 0.9819` → **final_acc ≥ 0.9819**（等价精度损失 <1%，但 AC 公式为单一真相源，不再并存 v2 D5 绝对容差 0.5 的 `final≥0.4919` 歧义）。pretrain acc 90%+ 由 baseline 0.9919 自然满足。
  3. **LAT AC**（§6.6 参数化）：`final_latency ≤ baseline_latency × (1 − latency_reduction_target)`，默认 `latency_reduction_target=0.5` → **final_latency ≤ baseline×0.5**。**优化幅度由 R4 实测确认**（layer-floor 决定上限），Phase L5 据实测调 `latency_reduction_target`（**禁 gaming**：禁删计算/改深度/负 transform 充优化）。
- **不达标分析**：从 workflow 记录（`runs/<run_id>/` + `$ORCA_ARTIFACTS_DIR/`）分析根因——
  - BLD：layer 变体蒸馏 loss 是否收敛（变体是否真模仿原层 I/O）。
  - GKD：epochs 够否（现 = 基线×50%）、KD loss 收敛、warmup。
  - 候选质量：layer 变体 attention 实现是否正确（去 elastic 是否破坏计算）。
  - 时延：MIP 是否真在预算内选了非 identity 变体、latency 测量是否同源。
  - 据分析迭代 workflow 参数（不 gaming：禁删计算/改深度/负 transform）。

---

## 10. SDD 执行计划

- **Phase L0**：tag 现状（puzzle-universal 已有 pz_materialize 待 commit，先 commit 保护）。
- **Phase L1**：**Slot 加 `transformer_layer` kind + `max_seq_len`/`norm_type` 字段**（schema 头部，下游节点依赖）+ layer 变体库（`transformer_layer_variants.py` 去 elastic 原生 + `layer_variant_catalog.yaml`）+ catalog loader 适配。单测。
- **Phase L2**：3 节点拆分（puzzle.yaml 拆 expand→ingest/search_space/baseline + 3 agent.md + `transformer_layer_pattern.json` 知识库 + `transformer-layer-evaluator` subagent + 各节点 gate 脚本）。
- **Phase L3**：内核 layer 粒度适配（measure_baseline floor / bld / score / mip dispatch / materialize / gkd / gate 的 slot 粒度迁移）。
- **Phase L4**：`tars validate` + 洁净审查 + 单测全过。
- **Phase L5**：target E2E（opencode run + tars skill + puzzle）→ 验收 §9。不达标则 §9 分析迭代。
- **Phase L6**：code-reviewer 洁净闭环 + release note + CHANGELOG + CURRENT.md。

---

## 11. 待确认 / 风险

- **R1**：layer 变体的 norm 类型（原层 LayerNorm vs nas-agent RMSNorm）。决策：变体自带 norm（原生化），不强制对齐原层——变体就是要探索不同计算方式。BLD 蒸馏 I/O 不关心内部 norm。✅ 已决策。**Post-LN(target)→Pre-LN(变体) 迁移收敛风险**：若 target 原层是 Post-LN（`x = norm(x + attn(x))`），变体一律 Pre-LN（`x = x + attn(norm(x))`，L14），两者梯度流不同——Post-LN 在深层易方差爆炸，Pre-LN 更稳。BLD 蒸馏目标对齐 I/O（不约束内部方差），故单层 BLD 收敛风险可控；GKD 末段全局 KD 可能因拓扑差异收敛慢（epochs 不够）。**风险显式承担**：Phase L5 E2E 若 GKD 不收敛，§9 不达标分析首要查 Post-LN→Pre-LN 迁移；如确需 Post-LN，按 L14 兜底加 `post_ln_*_layer` 变体族（族扩展，OCP）。
- **R2**：mask_load_bearing layer（原层 attn 收 mask）。**决策（L13 已 supersede v2 E8）**：layer 变体 forward 统一收 mask（`_extract_mask`），vanilla 真用（`nn.MultiheadAttention` attn_mask），无 scores 语义的 mixer 接受但忽略；`mask_load_bearing` 由 pz_baseline 运行时 trace（src_mask 实际值非 None）判定，非签名静态判。**不 E8 硬过滤**——mask-blind 变体在 mask-bearing slot 的精度损失由 MIP acc 自然惩罚；mask-aware 变体（如 `masked_*_layer`）不足时由 acc 倒逼扩展（族扩展，OCP）。✅ 已闭环（对齐 §6.1 + §0 L13）。
- **R3**：Orca "清产物重跑" 能力（§7 跨节点 self-heal）。**已确认（2026-08-13）**：`orca/iface/in_session/cli.py` 的 `bootstrap`/`next` 两命令无 `--force`/`--rerun` flag → 退化到"人工删问题节点 artifacts 目录 + 重跑（reuse_check 幂等跳过健康节点）"运维动作。✅ 已闭环。
- **R4**：target 时延优化幅度。layer 粒度 floor 比 block 粒度低，但 target 非-layer 开销（4 路 input_proj + output_proj + PE）仍占 floor。Phase L5 实测确认可达优化幅度，必要时调 latency_reduction_target（禁 gaming）。

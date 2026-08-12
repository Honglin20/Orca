# Puzzle Universal Workflow — 设计草稿 v2 (SDD)

> 跨阶段设计议题，puzzle 通用化重构各 phase SPEC 撰写前必读。
> **v2**：经 spec-reviewer 对抗式 review（baseline + evaluator 子 agent，25 issue）+ 4 项用户决策闭环。v1 的 25 个 review issue **全部 closed**（处置见 §17 追溯表）。
> **继承** [`puzzle-design-draft.md`](puzzle-design-draft.md) 的 decomposed NAS 算法内核（BLD/MIP/GKD）；
> **本文件**聚焦"通用外壳"重构——把 puzzle 从"假设标准写法"改造为"任意 transformer 项目自适应"。
> 范式来源不变：Puzzle (Bercovich 2025, ICML) decomposed NAS。
> 外壳借鉴 nas-supernet，但 **decomposed ≠ supernet**——Elastic*/超网/NSGA2 一律不引入。

---

## 0. 目标与已确认决策

**用户原始诉求**：puzzle 当前靠"类名 regex 识别 slot + 要求用户给 6 件套入口 + 硬编码候选块"——对非标准项目（target 靠 coder 手写 593 行 adapter 才跑通）不通用。要一个**任意 transformer 工程自适应**的 workflow，寻优空间能混合「用户原块 + 我们设计的算子 + 用户自注册算子」。

**核心关注点（贯穿全设计）**：
1. **占位符不影响用户原逻辑**——替换某层时，"保留原块"= 保留 father-loaded 模块（架构来自 flat + 权重来自 father_state），非标准自定义 transformer 也能保留（详见 §3）
2. **kind 自适应识别**——任意项目识别 slot 的 kind（第一版聚焦 attention/ffn，conv/moe/custom 预留）
3. **FFN 搜索空间慎重**——用户 FFN 可能非标准（激活/bypass/GLU 各异），剪枝候选须结构验证

**5 项已确认决策**（2026-08-12 用户拍板）：

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | 算法内核 | **保留 puzzle decomposed**（BLD/MIP/GKD），换通用外壳 | decomposed 已验证、天然支持异构候选、改动最小 |
| D2 | 落地方式 | **原地重构 `puzzle.yaml`**（tag 保护现状，见 §13）| 现有 PASS 作对照，重构后重跑 E2E |
| D3 | slot 识别 | **LLM 发现 + 用户可在声明文件覆盖** | 默认零干预能跑，需要时完全可控 |
| **D4** | **conv/moe/custom 范围** | **第一版聚焦 attention/ffn（有 builtin 候选）；conv/moe/custom 仅 `identity` 保留**（框架预留，未来加 builtin）| 诚实、聚焦、可扩展；不空头宣称通用 |
| **D5** | **ACC AC** | **相对容差 + floor**：`acc_opt ≥ acc_base·(1−δ)` 且容差 `= max(0.5, 0.1·acc_base)` | 低 baseline 任务（target 0.085）获得比例保护，高 baseline（mnist 0.97）保持绝对容差 |
| **D6** | **eval_kind** | **作 `[ask]` 输入**（用户必给，enum 三选一）+ 确定性 sanity check | 用户最懂任务输出语义；避免 LLM 误判单点失败（E16）；符合 [ask]=业务决策哲学 |

**调研依据**：4 个 Explore agent 深度研读 nas-agent（blocks 契约 / search 声明 / ns_expand 外壳+verifier / train+latency 复用）。

---

## 1. 通用性的核心思想（一句话）

**把项目的特异性全部压缩进 `flat.py` + `manifest.yaml` + `search_space.yaml` 三份产物（LLM 生产、用户可改）；BLD/MIP/GKD 算法脚本只认这三份产物的契约，对任意项目通用、零算法改动。**

`search_space.yaml` 是"判断"（LLM/用户）与"执行"（脚本）的**契约边界**——整个通用化的枢纽。

---

## 2. 与 nas-supernet 的可迁移边界（v2 修正 BLK-1）

借鉴**外壳与声明层**，不碰**求解层与 Elastic\***。

| ✅ 可迁移（声明/工程层） | ❌ 不可迁移（supernet/Elastic* 专属）| ⚠️ puzzle 自建（借鉴思路非直接迁移）|
|---|---|---|
| LLM 自适应 flatten + manifest（ns_expand Step 1）| Elastic* 权重切片原语 | **fidelity smoke 三道**（strict-load / forward-determinism / eval-stability）— ns_supernet 只有 forward-equivalence smoke，strict-load + eval-stability 是 puzzle 新建 |
| 声明式 tuple 离散枚举候选值 | `get_active_subnet` + `copy_()` 权重提取 | **per-slot identity allclose 校验**（§9.2）— puzzle 特有 |
| 模型派生默认值 → LLM refine → 用户 feedback | `elastic_num_params` | |
| metadata.json 目录理念（→ candidate catalog）| ChoiceLayer 运行时分支路由 | |
| 维度对齐契约（外部固定/内部可搜索）| `sample ≤ super` 传播约束 | |
| 统一 block 契约形状 | ArchCodec gene-to-arch | |
| `serialize_arch`/`hash_arch` | NSGA2 + Ray 评估池 + death-penalty | |
| 四层 verifier 闭环（分工模式）| 子网一致性义务 | |
| **BLD 真实数据 calibration**（修正 E14）| | — BLD 必须用真实数据 sample，nas-agent 端到端 KD 用真实数据，puzzle BLD 原用 `torch.randn` 是 OOD bug |

---

## 3. 铁律：identity = 保留 father-loaded 模块（v2 修正 E17）

**用户第一关注点。定为铁律。但 v2 修正了 v1 的实现诚实性错误。**

### 3.1 正确语义（修正 E17）
`identity` 候选 **= 不替换该 slot 的架构与权重**——保留 **father-loaded 模块**（架构来自 `flat.py` 的 `build_fn()`，权重来自 `father_state_dict.pt` 经 `load_state_dict` 注入）。

⚠️ **v1 错误已修正**：v1 §3.1 说"不重新实例化、保留用户原来的那个 nn.Module 实例"——这是**事实错误**。实际流程中用户的原 Python 对象在 `importlib` 加载 flat.py 时就丢失了，pipeline 里始终是 father-loaded 的 fresh instance。

**等价性保证**：identity slot 的行为**等价于用户原模块，当且仅当 `load_state_dict` 无损**（架构一致 + 权重全载 + 无 non-persistent buffer/runtime cache 丢失）。
- 标准模块（权重都在 state_dict）：完全等价
- 含 non-persistent buffer / 运行时 cache / `register_buffer(persistent=False)` 的模块：可能不等价 → §9.2 的 allclose smoke 会捕获

### 3.2 代码现状（语义已对，固化为契约）
- `puzzle_common.py:241` `PASSTHROUGH_VARIANTS = {"identity"}`
- `puzzle_common.py:729-730` `build_student_from_arch`：`if is_passthrough(variant): continue` —— 保留 father-loaded 模块
- `bld.py:198-222`：identity 不训练，存 sentinel ckpt

### 3.3 选择语义
MIP 可给任意 slot 选 `identity` → 该层原样保留。若某层 attention=identity + ffn=identity，等价"这一整层 transformer 不动"。**这是"替换用户某些层、其他层不变"的精确表达**——占位符零侵入的实质是"该 slot 不被任何 factory 触碰"。

---

## 4. 核心新概念：`search_space.yaml` 声明 schema（v2 修正 E1/E2/E3/E15/E23）

```yaml
# ── slots: 在哪寻优（LLM 发现 → 用户可编辑覆盖）────────────
slots:
  - {id: L0_attn, path: blocks.0.attn, kind: attention,
     in_dim: 96, out_dim: 96, num_heads: 4, head_dim: 24,
     source_class: TinyAttention,
     forward_arity: single, return_arity: single, mask_load_bearing: false}
  - {id: L0_ffn,  path: blocks.0.ffn,  kind: ffn,
     in_dim: 96, out_dim: 96, original_intermediate: 384,
     source_class: FeedForward, activation: gelu,
     ffn_struct: standard,   # standard | bypass | glu | dual（E6 结构验证用）
     forward_arity: single, return_arity: single}
  ...

# ── candidates: 寻优成什么（三源；identity 必入每 slot）──────
candidates:
  attention:
    - identity                        # ← 必入（MIP floor 锚，E1）
    - fnet                            # ← builtin
    - random_synthesizer
    - relu_attention
    - softs_star
    - vanilla
    - no_op
  ffn:
    - identity                        # ← 必入
    - ffn_75
    - ffn_50
    - linear
    - no_op
  custom:                             # 用户自注册（§5.3）
    - {name: my_mixer, factory: project_root/my_blocks.py::make_mixer,
       applies_to: [attention], params: {num_heads: 4}}
```

### 4.1 slot 字段（v2 修正 E3/E15/E23）
| 字段 | 含义 | 谁填 | 修正点 |
|---|---|---|---|
| `id` | 唯一标识（MIP 分组键）| LLM | |
| `path` | `model.get_submodule(path)` 路径 | LLM | **E3**：即现有 `parent_module_path` 的 yaml 别名（脚本侧字段名不变，仅 yaml key 用 `path`，loader 做映射）|
| `kind` | **替代** `slot_type`（E3）；开放标签 attention/ffn/conv/moe/custom | LLM | **E3**：`kind` **替换** `slot_type`，所有 dispatch 迁移（§15 迁移表）|
| `in_dim`/`out_dim` | I/O 最后一维 | 脚本 trace | |
| `num_heads`/`head_dim` | attention 才有 | 脚本 trace | |
| `source_class` | 原块类名（溯源 + 结构验证用）| LLM | |
| `original_intermediate` | FFN 原中间维（E7 ratio 基准）| LLM/脚本 | **E7**：ratio 基于此非 in_dim |
| `activation` | FFN 激活（gelu/relu/silu/mish/...）| LLM | **E23**：`kind=ffn` 时 **required**，null → raise |
| `ffn_struct` | FFN 结构类型（E6）| LLM | standard/bypass/glu/dual；非 standard → 禁剪枝候选 |
| `forward_arity` | 输入 arity（single/multi）| LLM | **E2**：记录字段，供 evaluator 审 mask_load_bearing 一致性 |
| `return_arity` | 输出 arity（single/multi）| LLM | **E15**：multi-return slot 拒绝 single-output 候选 |
| `mask_load_bearing` | 父层是否传 functionally-load-bearing kwargs（attention_mask 等）| LLM | **E8**：true → 该 slot 只允许 identity（mask-blind 候选会丢 mask）|

### 4.2 三源 candidates
- `identity` = 保留 father-loaded 模块（铁律 §3）——**必须出现在每个 slot 的候选列表**（E1：MIP floor 估算锚）
- 裸字符串（fnet/ffn_75/...）= 引用 candidate catalog（§5）的 builtin
- `{name, factory, applies_to, params}` = user 自注册（§5.3）

### 4.3 删除的字段（E2）
- ❌ **删 `axes`**：无脚本消费，装饰性。第一版搜索轴由 candidate 列表本身决定（列了 ffn_50 就搜剪枝，没列就不搜）。Rule 2 Simplicity First。
- 保留 `forward_arity`/`return_arity`/`mask_load_bearing` 作记录字段——它们有确定性 consumer（evaluator + build 阶段 is_valid 检查）。

---

## 5. candidate catalog（框架内置，取代硬编码 registry；v2 修正 E4/E8 + D4）

借鉴 nas-agent `metadata.json` 目录理念（调研 A）。

### 5.1 形态（`assets/candidate_catalog.yaml`）
```yaml
- name: fnet
  kind: [attention]
  source: builtin
  factory: puzzle_blocks::make_fnet
  params: {}                         # 无参
  align: passthrough
  trainable: false                   # 零参，BLD 只算 loss 不优化
  mask_aware: false                  # E8：mask-blind
  description: "零参数 2D-DFT mixer；forward(x) 单参；mask-blind"
- name: ffn_50
  kind: [ffn]
  source: builtin
  factory: puzzle_blocks::make_ffn
  params: {ratio: 0.5}
  align: passthrough
  trainable: true
  mask_aware: false
  requires_ffn_struct: [standard]    # E6：只适用 standard FFN
  description: "FFN 中间维 ×0.5（相对 original_intermediate）；激活对齐 slot"
- name: no_op
  kind: [attention, ffn]
  source: builtin
  factory: puzzle_blocks::make_zero
  align: passthrough                 # 要求 in_dim==out_dim
  trainable: false
  description: "_ZeroBlock 跳过"
- name: identity
  kind: [attention, ffn, conv, moe, custom]   # 适用所有 kind（passthrough 铁律）
  source: passthrough
  description: "保留 father-loaded 模块（§3）"
```

### 5.2 统一契约（v2 修正 E4/E8）
- **factory 签名**（E4）：`factory(slot: Slot, **params) -> nn.Module`；catalog loader 用 `functools.partial(factory, **params)` 绑定成统一 `factory(slot)` 供 BLD/score/build 消费
- **forward**：单参 `x`；异构父层用 `_KwargPassthrough` 适配（`puzzle_common.py:323`）
- **维度对齐**（铁律，调研 A）：**外部 in_dim/out_dim 固定不可搜索，只内部维度可搜索并投影包裹回外部**
- **`is_valid_<name>(slot) -> bool`** 验证器：廉价预检查（如 no_op 要求 in_dim==out_dim；ffn 剪枝要求 ffn_struct=standard；mask_load_bearing slot 拒绝 mask-blind 候选）
- **mask-blind 声明**（E8）：所有 builtin 候选默认 `mask_aware: false`——对 `mask_load_bearing: true` 的 slot，is_valid 拒绝，只留 identity

### 5.3 user candidate（零特殊代码）
`{name, factory, applies_to, params}` → `load_external_callable(factory)`（`puzzle_common.py:624`）+ `functools.partial`；签名统一 `factory(slot, **params)`，与 builtin 等价。

### 5.4 builtin 默认集（D4：聚焦 attention/ffn）
- **attention**：identity / random_synthesizer / relu_attention / fnet / softs_star / vanilla / no_op
- **ffn**：identity / ffn_75 / ffn_50 / linear / no_op
- **conv/moe/custom**：**仅 identity**（D4，框架预留；未来加 builtin 时扩 catalog）

---

## 6. kind 自适应识别（v2 修正 E18：确定性证据）

### 6.1 当前缺陷
`expand_model.py:48-79` 靠类名 regex（`_ATT_NAME_PATTERNS`/`_FFN_NAME_PATTERNS`）——脆弱，换项目类名漏识别。

### 6.2 新机制：LLM 判定 + 确定性证据（修正 E18/E22）
- **识别移给 pz_expand LLM**：读代码 → 判 kind → 写进 search_space.yaml
- **kind 开放标签**：attention/ffn/conv/moe/custom（第一版 builtin 只覆盖 attention/ffn，D4）
- **LLM 必须给出确定性证据**（E18 修正，避免 LLM 检查 LLM 循环）：
  - attention：模块含 `matmul(Q, K^T)` 缩放 + softmax 模式（证据：forward 源码有 QK^T 或 score 缩放）
  - ffn：`Linear → 激活 → Linear` 主导（证据：两个 Linear 夹激活）
  - conv：`nn.Conv1d/2d/3d` 主体
  - moe：含专家 gate 路由
  - custom：不匹配上述但用户标注可替换
- **确定性脚本不再做识别**——删 `_ATT_NAME_PATTERNS`/`_FFN_NAME_PATTERNS`/`_find_layer_containers`

### 6.3 兜底（修正 E18/E22）
- 用户可在 search_space.yaml 手工修正（D3）
- **block-map-evaluator**（§10）审 kind 合理性——**有确定性硬规则**（检查 LLM 给的证据是否支持 kind：attention slot 的 source_class/源码是否真含 QK^T）
- **确定性 post-check**（E22）：search_space.slots 为空 → `model_type_supported=false` → 路由 `terminate_unsupported`（不让 LLM 误报 supported 带空 slots 进 BLD）

---

## 7. FFN 搜索空间通用化（v2 修正 E6/E7/E23；用户第三关注点）

### 7.1 非标准 FFN 表现
- 激活异（GELU/ReLU/SiLU/Mish）、结构异（bypass/GLU 门控/双层/dropout）、维度异（d_ff 比例非 4×）

### 7.2 通用化设计（修正 E6）
| 候选 | 如何兼容非标准 FFN |
|---|---|
| **identity** | 保留原 FFN——任何非标准 FFN 都能作为基线保留（§3）|
| **ffn_75 / ffn_50** | **仅适用 `ffn_struct=standard`**（E6）：is_valid_ffn_prune 检查，bypass/GLU/dual → **拒绝**（收缩到 identity/no_op），避免静默破坏结构 |
| **linear** | 单 Linear 替代（激进），同样仅 standard |
| **no_op** | `_ZeroBlock` 跳过（in_dim==out_dim）|

### 7.3 activation-aware factory + 修正 ratio 语义（E7）
```python
def make_ffn(slot: Slot, ratio: float) -> nn.Module:
    if slot.activation is None:
        raise ValueError("ffn slot 缺 activation（E23 required）")  # E23
    act = resolve_activation(slot.activation)   # gelu→GELU, relu→ReLU, silu→SiLU...
    # E7 修正：ratio 相对 original_intermediate，不是 in_dim
    intermediate = max(1, int(round(slot.original_intermediate * ratio)))
    return nn.Sequential(nn.Linear(slot.in_dim, intermediate), act(),
                         nn.Linear(intermediate, slot.out_dim))
```
**E7 修正**：`intermediate = original_intermediate × ratio`（不是 v1 的 `in_dim × ratio`）。对标准 FFN（d_ff=4×d_model），ffn_50 给 2×d_model（50% 原中间维），而非 v1 错误的 0.5×d_model（12.5%）。

### 7.4 结构验证器（E6）
`is_valid_ffn_prune(slot) -> bool`：检查 `slot.ffn_struct == "standard"`；非 standard（bypass/GLU/dual）→ False → 该 slot 的 ffn_75/ffn_50/linear 被 is_valid 过滤，候选自动收缩到 {identity, no_op}。

---

## 8. 节点数据流（v2 修正 E9/E19）

```
┌──────────────────────────────────────────┐    ┌──────────────────────────────────────────┐
│ pz_expand (LLM 判断)                     │    │  确定性脚本 (算法内核)                    │
│                                          │    │                                          │
│  读 project_root/model_path              │    │  pz_build_library → bld.py               │
│  ↓                                       │    │  pz_score         → score.py+latency.py  │
│  ├─ flat.py            ──────────────────┼────┼─→ (脚本经 CLI args 读 flat + father_state)│
│  ├─ manifest.yaml ★    ──────────────────┼────┼─→ (agent 读 manifest → 桥接 CLI args)    │
│  ├─ search_space.yaml   ─────────────────┼────┼─→ (脚本经 CLI args 读 search_space 路径) │
│  ├─ baseline_metrics.json                │    │  pz_select        → mip_select.py        │
│  └─ father_state_dict.pt ────────────────┼────┼─→ (CLI args)                              │
│                                          │    │  pz_retrain       → gkd_retrain.py       │
│  + 3 道 smoke（§9.2）+ verifier 闭环     │    │  pz_report        → gate_report.py       │
└──────────────────────────────────────────┘    └──────────────────────────────────────────┘
       ↑ LLM 判断 + 用户可覆盖                      ↑ 脚本收 CLI args（不解析 markdown/yaml）
```

★ **E9 修正**：manifest 从 v1 的 markdown 改为 **`manifest.yaml`**（确定性可解析）；但**消费者是 agent 不是脚本**——agent 读 manifest → 桥接为 CLI args 传给脚本（ns-supernet 模式）。脚本本身只收 CLI args，不解析 manifest。

**E19 修正"内核不动"措辞**（诚实）：
| 节点 | 不动（loss/optimizer 逻辑）| 要改（dispatch/argparse 迁移）|
|---|---|---|
| bld.py | normalized MSE loss + Adam 优化 | dispatch key `slot_type`→`kind`（main-loop）；candidate 源 `parse_block_candidates`→catalog loader |
| score.py | KL/cosine/MSE 打分 | eval_kind 分支保留；candidate 源迁移 |
| gkd_retrain.py | cosine+KL KD loss + warmup | argparse 5 args（eval_fn/eval_kind/build_fn/father_state/train_loader）从 user-input → manifest-discovered（agent 桥接）|
| mip_select.py | — | **零改动** |
| gate_report.py | — | **零改动**；eval 从 manifest 发现 |

---

## 9. pz_expand 重构：LLM 判断 + 脚本测量 + 确定性 smoke（v2 修正 E14/E25）

### 9.1 职责拆分（Rule 5）
| 部分 | 谁做 | 内容 |
|---|---|---|
| **判断**（LLM）| pz_expand agent | 读代码 → flat.py + manifest.yaml + search_space.yaml（slots + kind 识别 + 确定性证据）|
| **测量**（脚本）| `measure_baseline.py`（从 expand_model.py 拆出）| load father + eval_fn 测 acc + measure_module_latency 测 latency + trace slot I/O shape |
| **calib 数据**（脚本，E14 修正）| `build_calib_loader` 改用**真实数据** | 从 manifest 的 data loader 入口抽小批量；拿不到 → fail-loud（不再用 `torch.randn` OOD）|
| **校验**（脚本 smoke）| 脚本内嵌 | 4 道 smoke（§9.2）|

### 9.2 四道确定性 smoke（v2 修正 E5/E14/E25）

1. **strict-load smoke**（BLK-1 修）：
   - `load_state_dict` missing 非空 → **raise**（father 是全链 teacher，missing 污染 BLD/score/gkd）
   - 收紧 `puzzle_common.py:175-180` 的 20% 阈值到零 missing

2. **forward-determinism smoke**（E25 拆分）：
   - 同输入 forward 两次 → `torch.equal`（捕获 forward 内未固定 RNG/无序算子）

3. **eval-stability smoke**（E25 拆分）：
   - eval_fn 同 model+data 跑两次 → acc 一致（捕获 train-mode 泄漏 / 未 seed workers）

4. **per-slot identity allclose smoke**（E5 修正，取代 v1 的 forward norm）：
   - hook 每个 identity slot → forward dummy（真实数据 batch）+ father → `torch.allclose(slot_out, original_slot_out, atol=ε)`
   - **这是 §16.4 AC 的真实验证机制**（v1 的 norm smoke 已废弃——norm 相等 ≠ 逐元素）

### 9.3 fidelity 定位
- expand 节点 **fidelity vacuous**（无 porting）——不调 project-fidelity-verifier
- manifest.yaml 的 `Evaluation entry` 字段必须准确（给下游 pz_retrain 的 fidelity-verifier 留锚）

---

## 10. verifier 闭环（v2 修正 E18：evaluator fixture + precision/recall）

| nas-supernet | puzzle 对应物 | 状态 |
|---|---|---|
| supernet-evaluator | **block-map-evaluator**（审 slot 划分/kind 合理性带确定性证据/I/O shape/return_arity）+ **search-space-evaluator**（审 schema 合规/identity 必入/candidate 注册有效/factory 可解析/eval_kind sanity）| 新建 `subagents/puzzle/` |
| project-fidelity-verifier | **pz_retrain port eval_fn/train 时用**（差分探针 + 禁代理替换）| 新加触发点 |
| workflow-verifier | 已有 | 加 search_space checklist |
| memory-verifier | 已有 | 加 metric 方向硬检查 |

### 10.1 严重级
- **[BLOCKER]**：slot path 定位失败 / I/O shape 不一致 / factory 不可解析 / identity 语义违反 / return_arity multi 但候选 single-output / **identity 未入某 slot 候选**（E1）
- **[MAJOR]**：kind 分类不合理（证据不支持）/ candidate 不适用 kind / eval_kind 与输出 dim/range 不自洽（D6 sanity）/ 激活推断不确定且未标注 / mask_load_bearing slot 选了 mask-blind 候选
- **[MINOR]**：描述缺失 / 命名不规范

### 10.2 evaluator 质量保证（E18 修正）
- **fixture suite**（`tests/e2e_puzzle/fixtures/evaluator_cases/`）：seeded errors（conv-标-attention / wrong path / shape mismatch / identity 缺失 / eval_kind 误标 / mask slot 选 mask-blind）
- **AC**（§16.6/16.7）：block-map-evaluator 对 seeded errors recall ≥ 90%；search-space-evaluator schema 违规 recall 100%
- Phase U2 含建 fixture，不只写 evaluator prompt

---

## 11. 算法脚本复用清单（v2 修正 E24）

| 环节 | 复用 | caveat |
|---|---|---|
| **GKD** | ✅ `cosine_kd_loss` + `logits_kd_loss` + `KDWeightScheduler` + `AverageMeter` | **E24**：假设模型输出是单 tensor 或 (tensor,...) tuple；dict/list 输出需 flat.py 加 output-flattening adapter（写入 pz_expand flat 契约）|
| **score** | ✅ `logits_kd_loss`/`cosine_kd_loss` 当打分 | reduce 微调 |
| **latency** | ✅ `measure_module_latency`（默认）/ `export_and_measure_latency`（需分布）| |
| **checkpointing** | ✅ `save/load_checkpoint`（wrapper 自适应）| |
| **BLD** | ⚠️ 自写 normalized MSE | 复用 `logit_standardization` + `mse_kd_loss` 骨架；**E14**：calib 必须真实数据 |
| **eval 指标** | ⚠️ 自写 | top-1/k-NN/cosine 检索度量 |
| **serialize/hash** | ✅ `serialize_arch`/`hash_arch` | selected_arch 的 cache/dedup/命名 |

---

## 12. AC + fail-loud + self-heal（v2 修正 E11/E12/E20/E22）

### 12.1 AC（D5 修正 E11）
- **ACC**（D5 baseline-dependent 容差，2026-08-12 修正原 formula 矛盾）：按 baseline 高低**分界**选容差（非 max / 非"取更严者"）：
  - `acc_base ≥ 0.5`：**绝对容差 0.5** → `threshold = acc_base − 0.5`（mnist 0.97 → final ≥ 0.47）
  - `acc_base < 0.5`：**相对容差 10%** → `threshold = acc_base · 0.9`（target 0.085 → final ≥ 0.0765，近随机 0.001 会 fail）
  - 实现：`gate_report.py:_acc_threshold` baseline-dependent 分界（`_ACC_BASELINE_BOUNDARY=0.5`）。原 v2 的 `δ=max(0.5, 0.1·base)` formula 有矛盾（max 恒取 0.5，低 baseline 失去比例保护），U3 coder 按 intent 落地分界，本 SPEC 文字跟进修正（Rule 7 surface）
- **LAT**：`latency_opt ≤ latency_base / 2`
- **LAT 早警**（E12 修正）：pz_select fail-loud if `target_latency > baseline_latency / 2`（AC 结构性不可达）→ terminate，不浪费 BLD/score/retrain

### 12.2 固化/agent 边界（Rule 5）
- **判断 → LLM**：flat/manifest/search_space 生成、kind 识别 + 确定性证据、refine 决策
- **执行 → 确定性脚本**：抓激活、BLD、打分、MIP、GKD、gate、所有 smoke
- 两层靠 verifier 闭环衔接

### 12.3 fail-loud + self-heal（E20 修正）
- **fix-loop ≤ 3 次**：单步 LLM 修复超限 → fail loud
- **flat 自我验证风险**（E20）：flat 既是产物又是 heal 目标 → **确定性 hint 机制**：strict-load 失败时，diff missing keys 与 flat 的 state_dict，把 exact prefix mismatch 喂给 LLM（不让它盲猜）
- **fail taxonomy**（E20）：`error` 字段区分 `strict-load-convergence-failed`（LLM 没收敛）vs `schema-fundamentally-incompatible`（项目本身不兼容）→ 触发不同 terminate 节点
- **verifier loop**：evaluator repeat 直到 LGTM；workflow-verifier 直到 all-pass
- **禁碰文件**：expand 阶段 flat/manifest/search_space 可改；源项目文件禁碰（例外 artifacts/）
- **empty search_space**（E22）：确定性 post-check → `terminate_unsupported`

---

## 13. 不做 + branch 策略（v2 修正 E21）

### 13.1 不做
- ❌ Elastic* / 超网 / ChoiceLayer 运行时路由
- ❌ NSGA2 / Ray 评估池 / gene codec
- ❌ BLD/MIP/GKD 的 loss/optimizer 逻辑改动（dispatch/argparse 迁移允许，§8）
- ❌ 第一版 conv/moe/custom builtin 候选（D4，仅 identity）

### 13.2 branch/tag 策略（E21 修正）
- **Phase U0**：tag 当前 PASS 状态为 `puzzle-v1-pre-universal`（保护 mnist/target 已 PASS 代码）
- universal 工作在**新分支** `puzzle-universal`
- 旧行为可复现：`git checkout puzzle-v1-pre-universal -- workflows/puzzle.yaml workflows/agents/_puzzle_scripts/`

---

## 14. 输入契约（v2 修正 E16/D6）

| 档 | 字段 | 说明 |
|---|---|---|
| **[ask]** | `project_root` / `model_path` / `target_latency` | 业务决策必填 |
| **[ask]** | **`eval_kind`**（D6，E16 修正）| enum classification/embedding/regression；用户必给（最懂任务输出语义）；规避 LLM 误判单点失败 |
| **[advanced]** | `latency_unit` / `latency_script_path` | us/s 必给 script_path |
| **[advanced]** | `search_space_path` | 用户可提供覆盖声明文件（D3）|
| **[advanced]** | `block_candidates` | 兼容旧 input（覆盖 catalog 默认集）|
| **[default]** | `seed` | 复现性 |

**删除**作 user input：`build_fn` / `eval_fn` / `train_loader_fn` / `pretrained_ckpt` / `accuracy_tolerance`（D5 baseline-dependent 自动判，无需 user input）—— 前 4 个由 pz_expand agent 发现写 manifest.yaml，下游 agent 读 manifest 桥接 CLI args。
**保留**作 [ask]：`eval_kind`（D6，与 nas-supernet 的 3-[ask] 差异是范式真实差异——decomposed score 需要输出语义，SPEC 诚实记录）。

---

## 15. SDD 执行计划（v2 含迁移 checklist + output_schema 扩展）

- **Phase U0**：tag `puzzle-v1-pre-universal` + 新分支 `puzzle-universal`（E21）
- **Phase U1**：candidate catalog + search_space schema 落地（`assets/candidate_catalog.yaml` + `puzzle_common.py` Slot 加 kind/return_arity/original_intermediate/activation/ffn_struct/mask_load_bearing 字段 + catalog loader + functools.partial 绑定）+ `tars validate`
- **Phase U2**：pz_expand 重构（LLM 判断 + measure_baseline.py 拆分 + 4 道 smoke + block-map/search-space evaluator + manifest.yaml schema + **output_schema 扩 required**：`search_space_path`/`manifest_path`，E10）+ evaluator fixture suite（E18）
- **Phase U3**：下游迁移 checklist（E19）：
  - bld.py：dispatch `slot_type`→`kind`；candidate 源→catalog loader；**calib 改真实数据**（E14）
  - score.py：candidate 源迁移；eval_kind 分支保留
  - build_selected.py：identity allclose 支持
  - gkd_retrain.py：argparse 5 args 改 manifest-discovered（agent 桥接）；dict/list 输出 caveat（E24）
  - mip_select.py / gate_report.py：零改动
- **Phase U4**：两项目 E2E 重跑（mnist_trf / target），target 不再靠手写 adapter
- **Phase U5**：code-reviewer 洁净审查闭环

**迁移表**（E3，kind 替换 slot_type 的 dispatch 点）：
| 调用点 | 现状 | 迁移后 |
|---|---|---|
| `puzzle_common.py:42` Slot.slot_type | `"attention"\|"ffn"` | `kind: str`（开放）|
| `puzzle_common.py:399-429` parse_block_candidates | `for key in ("attention","ffn")` | 读 search_space 动态 keys |
| `bld.py:189` `candidates.get(slot.slot_type)` | 按 slot_type | 按 `slot.kind` |
| `score.py:68` | 按 slot_type | 按 `slot.kind` |
| `mip_select.py` groups | `(layer, slot_type)` | `(layer, kind)` |
| `candidate_registry` applicability | `{"attention"}`/`{"ffn"}` | 按 kind 集合 |

---

## 16. 验收（v2 修正 E5/E11/E13/E18/E22）

不只看"节点过 + AC 达标"，必须证伪通用性：

1. **§16.1 零手写 adapter**（E13 修正操作性定义）：pz_expand 运行前 `$ORCA_ARTIFACTS_DIR/puzzle/` 无任何文件；从 project_root 独自产出 flat + manifest + search_space。secondary AC：strict-load smoke 在 ≤2 fix-loop 通过。诚实声明：非标准 state_dict schema 项目的通用性依赖 LLM schema-alignment，fix-loop 是安全网（≤3 经验预算）。
2. **§16.2 非标准类名可识别**：fixture 类名不含 Attention/FFN（如 `Mixer`/`TokenProcessor`），pz_expand 仍正确识别 kind（带确定性证据）
3. **§16.3 非标准 FFN 兼容**（E6 修正）：fixture = SiLU 激活 + bypass 的 FFN；ffn_75 被 `is_valid_ffn_prune` 拒绝（ffn_struct=bypass）→ 该 slot 候选收缩到 {identity, no_op}；AC 验结构保留非仅激活对齐
4. **§16.4 identity 零侵入**（E5 修正）：MIP 选某层 identity → per-slot `torch.allclose(identity_slot_out, original_slot_out, atol=1e-5)` 通过（真实机制，非 norm）
5. **§16.5 user candidate 等价**：注册自定义 mixer，BLD/score/MIP 全链消费，与 builtin 等价
6. **§16.6 block-map-evaluator recall**（E18）：seeded errors fixture recall ≥ 90%
7. **§16.7 search-space-evaluator recall**（E18）：schema 违规 recall 100%
8. **§16.8 empty search_space terminate**（E22）：LLM 误报 supported + 空 slots → 确定性 post-check → `terminate_unsupported`
9. **§16.9 ACC AC 低 baseline 保护**（E11/D5）：target(0.085) 的近随机 final(0.001) → gate fail（相对容差生效）
10. **§16.10 LAT 早警**（E12）：target_latency > baseline/2 → pz_select terminate，不跑完整链

---

## 17. Review issue 闭环追溯表（25 issue 全 closed）

| ID | 严重度 | 问题 | 处置 | 闭环章节 |
|---|---|---|---|---|
| E1 | BLOCKER | MIP 要求每组 identity；conv/moe/custom 不可达 | identity 必入每 slot + D4（conv/moe/custom 仅 identity）+ BLOCKER 检查 | §4.2/§5.4/§10.1 |
| E2 | MAJOR | axes/forward_arity 装饰性 | 删 axes；forward/return_arity/mask_load_bearing 保留有 consumer | §4.3 |
| E3 | BLOCKER | kind vs slot_type 未定义 | kind 替换 slot_type + 迁移表 | §4.1/§15 |
| E4 | MAJOR | catalog params 注入未定 | factory(slot, **params) + functools.partial | §5.2 |
| E5 | BLOCKER | §16.4 逐元素 AC 不可证伪 | per-slot allclose smoke 取代 norm | §9.2/§16.4 |
| E6 | MAJOR | FFN 剪枝破坏 bypass/GLU | is_valid_ffn_prune + ffn_struct 字段 | §7.2/§7.4 |
| E7 | MAJOR | ratio 是 in_dim 非原 d_ff | intermediate = original_intermediate × ratio | §7.3 |
| E8 | MAJOR | _KwargPassthrough mask-blind | mask_aware 字段 + mask_load_bearing slot 只留 identity | §5.2/§4.1 |
| E9 | MAJOR | manifest 消费层错置 | manifest.yaml + agent 读→CLI args 桥接 | §8 |
| E10 | MAJOR | output_schema 缺字段 | Phase U2 扩 search_space_path/manifest_path | §15 |
| E11 | MAJOR | ACC AC 低 baseline 空文 | D5 相对容差 + floor | §12.1/§16.9 |
| E12 | MAJOR | target_latency 无早警 | pz_select fail-loud if target>baseline/2 | §12.1/§16.10 |
| E13 | MAJOR | 零手写 undefined | 操作性定义 + ≤2 fix-loop AC + 诚实声明 | §16.1 |
| E14 | 隐性 BLOCKER | BLD calib OOD（torch.randn）| calib 改真实数据 sample + 拿不到 fail-loud | §9.1/§2/§11 |
| E15 | MAJOR | 无 return_arity | slot 加 return_arity + BLOCKER 检查 | §4.1/§10.1 |
| E16 | MAJOR | eval_kind LLM 发现无验证 | D6：作 [ask] + 确定性 sanity check | §14/§10.1 |
| E17 | MAJOR | §3.1 不重新实例化事实错误 | 改写为 father-loaded + 无损假设 | §3.1 |
| E18 | MAJOR | evaluator 无 fixture | fixture suite + recall AC | §10.2/§16.6/§16.7 |
| E19 | MAJOR | 内核不动误导 | 措辞改 + 迁移 checklist | §8/§15 |
| E20 | MAJOR | flat 自我验证循环 | 确定性 hint（diff missing keys）+ fail taxonomy | §12.3 |
| E21 | MAJOR | 无 branch 策略 | tag + 新分支 | §13.2/§15 U0 |
| E22 | MAJOR | empty search_space 无 terminate | 确定性 post-check → terminate_unsupported | §6.3/§16.8 |
| E23 | MAJOR | activation fallback 未定 | ffn slot activation required + null raise | §4.1/§7.3 |
| E24 | MINOR | GKD 对 dict 输出未验 | caveat + flat output-flattening 契约 | §11 |
| E25 | MINOR | eval reproducibility 混淆 | 拆 forward-determinism + eval-stability | §9.2 |

**全部 25 issue closed**。3 BLOCKER（E1/E3/E5）+ 2 隐性 BLOCKER（E11/E14）均有明确修法 + 验收章节。SPEC v2 ready for Phase U1。

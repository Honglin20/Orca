# Puzzle Workflow — 设计草稿 (SDD)

> 跨阶段设计议题,各 phase SPEC 撰写前必读。对齐 nas-supernet v1 的 in-session 执行模型。
> 范式来源: Puzzle (Bercovich 2025, ICML) — decomposed NAS = BLD 块库 + replace-1-block 打分 + MIP latency-aware 选架构 + GKD 修块间兼容。
> 本工作流**不照搬** Puzzle 的 GQA/FFN 候选集,搜索空间改用 `nas-agent/blocks/` 已有 mixer 库(synthesizer / relu-attention / fnet 等)。

---

## 0. 目标与验收 (AC)

用户原始需求 → 硬性 AC(写入终端 gate 节点,确定性脚本断言):

| AC | 含义 | 实现 |
|---|---|---|
| **ACC** | 最终模型精度与基线损失 ≤ 0.5 | `pz_report` 读 baseline_acc + final_acc,断言 `abs(acc_opt - acc_base) <= 0.5` |
| **LAT** | 时延降低 ≥ 一半 | `pz_report` 断言 `latency_opt <= latency_base / 2` |

⚠️ 这两条 AC 是**全新**的 — 现有 kd-nas/nas-supernet 用的是 `target_latency` 阈值 + 硬比较,无比率/容差。本工作流是首个断言比率 AC 的 workflow。

两个 playground 项目都要通过:
- **mnist_kd**: 当前 LeNet CNN → 用户允许换 trf → 提供新 fixture `mnist_trf` (小 transformer),套 mixer 块库。
- **target**: CrossFusion 4-输入 transformer (InfoNCE + k-NN acc),CPU 可跑(`train.py:23` auto fallback)。

执行环境(实测 2026-08-12): `orca` conda env, python 3.12.13, **torch 2.13.0+CPU (无 GPU)**, onnxruntime 1.27, **pulp 3.3.2 (MIP 求解器)**, nas_agent blocks 可导入。两个项目均 CPU 可行(target 较慢)。

---

## 1. 范式定位: Puzzle vs nas-supernet

| 维度 | nas-supernet (v1) | **puzzle (本设计)** |
|---|---|---|
| 搜索模型 | 弹性**超网** (Elastic* + ChoiceLayer),权重共享,一次训→多 shape 抽子网 | **decomposed NAS**: 块库 + 独立打分 + 全局选择,无超网 |
| 搜索空间轴 | depth/width/heads/experts **弹性收缩**(同型算子) | 逐层 **block 类型异构替换**(mixer 库)+ depth (no-op) + width (FFN 剪枝) |
| 候选来源 | Elastic 原语(同族参数化) | `nas-agent/blocks/` mixer 库 (synthesizer/relu-attn/fnet/softs-star/vanilla) + FFN 变体 |
| 选架构 | 进化 NSGA2 → `select_architecture.py` (Pareto knee / max-acc-under-target) | **MIP grouped-knapsack** (pulp): max Σscore s.t. Σlatency ≤ target |
| 训练范式 | 超网蒸馏 (teacher→全谱子网) | **BLD** (块级局部蒸馏建库) + **GKD** (末段全局 KD 修块间兼容) |
| 时延建模 | HW-aware 真 (Flextron-style router) | HW-aware 真 (per-block 实测 latency → MIP 硬约束) |
| 执行/可视化/契约 | in-session + chart daemon + 7 agent + bounded-polling 自愈 | **完全对齐** nas-supernet v1 |

核心差异: supernet 是"训一个弹性大网,抽子网"; puzzle 是"把预训练模型当拼图,每层独立换块+打分+全局求解"。BLD 让每个候选块**独立**学到模仿原块的输入→输出,MIP 在 latency 预算下选最优逐层组合,GKD 修补拼接后的块间失配。

---

## 2. 搜索空间设计(核心,用户重点)

### 2.1 block_map — 可替换 slot 清单
`pz_expand` flatten 用户模型后,识别每个 transformer 层的可替换子块:
- **attention slot**: 原 attention 模块 (target 的 `MultiHeadAttention_upd`; mnist-trf 的 `nn.MultiheadAttention`/自定义 MHSA)
- **ffn slot**: 原 FFN 模块 (`FeedForward` / Linear-Act-Linear)

每个 slot 记录: `{layer_idx, slot_type, in_dim, out_dim, seq_shape, num_heads, head_dim, source_class}` → `block_map.json`。

### 2.2 候选块库(per attention slot)
来自 `nas-agent/nas_agent/blocks/`(均已验证 BLC + `global_dim` + `get_active_subnet()` 契约):
- `identity` — 原块冻结(基线,不换)
- `random_synthesizer` (`random_synthesizer.py:124`) — 无 QK 的学习型 mix 矩阵,参数极省
- `relu_attention` (`relu_attention.py:115`) — ReLU(logits)/L 替 softmax
- `fnet_fourier_mixer` (`fnet_fourier_mixer.py:129`) — 零参数 2D-DFT mixer,ONNX 友好
- `softs_star_mixer` (`softs_star_mixer.py:124`) — SOFTS STAR 聚合-重分配(L 线性)
- `vanilla_mhsa` — 标准 MHSA(对照基线)

### 2.3 候选块库(per ffn slot)
- `identity` — 原 FFN
- `ffn_75` / `ffn_50` — 中间维剪枝(类 Puzzle 的 75%/50%)
- `linear_replace` — 单线性替代(Puzzle 风格)
- `no_op` — 整块跳过(等价 depth 减 1)

### 2.4 维度对齐(关键工程点)
所有候选块必须与原 slot 的 `in_dim/out_dim` 严格一致(embedding dim 固定,类 Puzzle 约束)。mixer 库块构造接受 `global_dim`,天然对齐;FFN 剪枝只动中间维、不动外维。这是异构替换可拼接的前提。

---

## 3. Workflow 节点流水(6 agent + 4 terminate — 按 Puzzle 算法本征,不照搬 supernet)

**设计原则**: 节点划分服从 Puzzle 算法步骤,而非 nas-supernet 的 generator/executor 拆分(那是 supernet 训练的产物)。每个 executor **自生自跑**自己的脚本(类 ns_retrain:生成→fidelity→跑→自愈),不单设 generator 节点。产物是**通用** workflow:任何"可替换 sub-block 的 transformer 族模型"都能跑。

```
pz_expand (entry, generator-light)
  ├─ model_type 不可替换 → terminate_unsupported
  └→ pz_build_library (executor, long-running: BLD 块级蒸馏建库)
       └→ pz_score (executor: replace-1-block 打分 + latency 实测)
            └→ pz_select (deterministic, zero-LLM: MIP grouped-knapsack)
                 ├─ 无候选 → terminate_select_failed
                 └→ pz_retrain (executor, long-running: GKD 末段重训)
                      ├─ failed → terminate_retrain_failed
                      └→ pz_report (deterministic gate: AC 断言 + 图表)
                           ├─ AC 不达标 → terminate_gate_failed
                           └→ $end
```

### 节点契约速览

| # | 节点 | Puzzle 算法步骤 | 关键产物 | 可视化 |
|---|---|---|---|---|
| 1 | `pz_expand` | 识别"拼图块" + 实测父模型 | `block_map.json` (逐层 attention/ffn slot + I/O shape), `<base>_flat.py`, `baseline_metrics.json` (acc+latency), `project_manifest.md` | — |
| 2 | `pz_build_library` | **BLD**: 每 (layer,variant) 独立蒸馏模仿原块 | `block_library/<layer>_<variant>.pt` + `bld_summary.json` | progress_watcher per-variant BLD loss (label `puzzle/bld`) |
| 3 | `pz_score` | **replace-1-block** 打分 + per-variant latency | `scores.jsonl`, `latency_table.jsonl` | block_score_bar / latency_dist / score_vs_latency_scatter (label `puzzle/score`) |
| 4 | `pz_select` | **MIP** grouped-knapsack | `selected_arch.json` + stdout 单行 JSON | — |
| 5 | `pz_retrain` | **GKD** 末段全局 KD 修块间兼容 | `final_model.pt` | progress_watcher (label `puzzle/retrain`) + 完成时 compare_table |
| 6 | `pz_report` | AC gate (ACC≤0.5 / LAT≥2×) | `final_report.md`, `gate_result.json` | baseline_vs_optimized metrics_bar (label `puzzle/report`) |

terminate: `terminate_unsupported` / `terminate_select_failed` / `terminate_retrain_failed` / `terminate_gate_failed` (全 `status: failed`,routes 空)。

### 复用 nas-supernet 的执行机制(逐条对齐 — 这些是通用 in-session 契约,与算法无关)
- **input tiers**: `[ask]` project_root/model_path/target_latency; `[advanced]` latency_script_path/latency_unit/block_candidates; `[default]` seed=0。
- **input_invariants**: latency_unit∈{us,s} ⇒ latency_script_path 非空。
- **output_schema**: 全 `additionalProperties: false`,exhaustive `required`,JSON-only 最终回复。
- **routes**: first-match-wins,catch-all last,truthiness 判定(不用 `is defined`),`status: failed` 显式路由。
- **长跑节点 (pz_build_library/pz_retrain)**: bounded-polling (K=6 monitor blocks/turn) + 无上限 HEAL-LOOP (≤2 rounds/turn) + `status.sh` 真相源 re-entry + `*_status.md` 跨 turn + "请勿调用 orca next" handoff。
- **self-heal 白名单**: patch 层(启动脚本/路径对齐)自由改;logic 层(bld/gkd/score 的 loss/数据)改后重触 `project-fidelity-verifier`;forbidden = `block_map.json`/`<base>_flat.py`/`baseline_metrics.json`/源项目文件(例外 `artifacts/`)。
- **progress.jsonl 契约**: `{"step":N,"metrics":{...}}` per step,用户指标全推;progress_watcher 边跑边推 chart。
- **verifier loops**: `pz_expand` 跑 workflow-verifier + memory-verifier (point-to-file 协议,subagents/puzzle/ 下建体);executor 改 logic 层后重触 project-fidelity-verifier。

---

## 4. Puzzle 四组件映射到实现(BUILD vs REUSE)

| Puzzle 组件 | 实现 | 复用 |
|---|---|---|
| **(a) flatten 模型** | `pz_expand` 调 model-flatten skill / 自写 | nas-supernet 的 flatten 范式 |
| **(b) 块库构造** | 候选块来自 `nas-agent/blocks/`,经 BLD 蒸馏到 standalone | `get_active_subnet()` 产出 standalone 块;`ChoiceLayer` 的 branches dict 模式 |
| **(c) BLD 块级蒸馏** | **BUILD** `bld.py`: 每 (layer,variant) 用 normalized MSE `MSE(o_p,o_c)/MSE(o_p,0)` 蒸馏(冻结 teacher,喂父激活) | `KDWeightScheduler`, `AverageMeter`, distributed helpers, `mse_kd_loss` |
| **(d) replace-1-block 打分** | **BUILD** `score.py`: 替换单块进冻结全模型,calibration set 上算分 | 分类: `logits_kd_loss` (KL); 嵌入(InfoNCE): hidden cosine distance |
| **(e) latency 实测** | `latency_table.py`: per (layer,variant) | `measure_module_latency` (PyTorch ms) 或 `export_and_measure_latency` (ONNX) 或 wrap 用户 latency_script_path |
| **(f) MIP 选择器** | **BUILD** `mip_select.py`: pulp grouped-knapsack | `serialize_arch`/`hash_arch` 命名;`select_architecture.py` 的 stdout-JSON 契约 |
| **(g) GKD 末段重训** | **BUILD** `gkd_retrain.py`: cosine(hidden) + KL(logits,分类才有) | `cosine_kd_loss`/`logits_kd_loss` + `KDWeightScheduler` |

**MIP 形式化** (pulp):
```
max  Σ_layer Σ_variant  score[layer,v] · x[layer,v]        # score = -replace1_distance (越大越好)
s.t. Σ_layer Σ_variant  latency[layer,v] · x[layer,v] ≤ target_latency   # latency 硬约束 = baseline/2
     Σ_variant x[layer,v] = 1  ∀layer                        # 每层恰选一个
     x[layer,v] ∈ {0,1}
target_latency = baseline_latency / 2   # 直接对接 LAT AC
```

---

## 5. 两个 playground 项目的适配

### 5.1 mnist → mnist_trf fixture
- 当前 `mnist_kd/model.py` 是 LeNet CNN,mixer 块库(BLC transformer 块)不直接适用。
- 用户授权换 trf → 新建 fixture `tests/e2e_puzzle/fixtures/mnist_trf/`: `model.py` 含 patch-embedding 前端 (Conv2d 3x3 stride? 或 linear patch) + `TinyTransformerBlock` stack + 池化 Linear→10 head,保留 `build_model(**cfg)` + `KNOBS` + `DUMMY_INPUT` 契约。
- 参考范式: `examples/kd-nas-demo/knowledge_base/families/receiver/_demo_blocks.py` (`TinyTransformerBlock`)。
- baseline ~0.97-0.99 (transformer on MNIST,几个 epoch,CPU 可达)。
- mixer 替换 attention slot → synthesizer/fnet 等参数更省 → 时延降。

### 5.2 target (CrossFusion transformer) — slot 已核实 (2026-08-12 通读 model.py)
- **可替换 slot = 4 encoder layer × {attention, ffn} = 8 slot**:
  - attention: `MultiHeadAttention_upd` (`model.py:86-135`),`__init__(input_size, num_units, num_heads, dropout_rate, relu_softmax=False)`,W=Linear(input_size→num_units*3) QKV + ln=Linear(num_units→input_size)。**I/O 同维 in_dim→in_dim**(d_model=128,nhead=4)——与 mixer 库 global_dim→global_dim 契约一致,可直接替换。
  - ffn: `FeedForward` (`model.py:138-154`),Linear(d_model,d_ff)→ReLU→Linear(d_ff,d_model),d_model→d_model。标准 FFN,套 ffn 候选。
- 替换机制: `setattr(encoder_layerN, 'self_attn'|'ff', <mixer/ffn_variant>)`——层内 residual+norm 结构不动,只换子模块。
- **InfoNCE + k-NN acc** ⇒ `eval_kind=embedding`:打分用 hidden cosine distance,GKD 用 hidden cosine(`logits_kd_loss` 不适用——无分类 logits,输出是 16 维 embedding)。score.py / gkd_retrain.py 据 eval_kind 分支。
- baseline acc/latency 在 `pz_expand` 实测(eval_model[k-NN] + measure_module_latency,CPU)。
- **全程全网**(用户确认):BLD/score/eval 用全数据,CPU 上较慢(2327-user k-NN)。

### 5.2.1 target P4 适配工作量(2026-08-12 核实)
- **model.py 缺 `build_model` 工厂 + `DUMMY_INPUT`**:需加 `build_model(**cfg)=CrossFusion(**cfg)` + DUMMY_INPUT。但 CrossFusion.forward 是 **4 输入** (x1,x2,x3,x4)——当前 workflow 的 dummy/latency/expand 处理是**单输入**(`torch.randn(*shape)`)。**P4 需扩多输入支持**(dummy_input 支持多 tensor、measure_latency 多 args、forward 多参)。
- **eval 契约不匹配**:`eval_model(model_path)` 取路径、自建模型、k-NN。需加 `eval_model_acc(model)->float` 包装(用传入 model 做嵌入 + k-NN,不自建/不 load)。
- **数据**:CPU 上 2327-user k-NN 较慢(全程全网),GKD 训练也用 train.py 的 InfoNCE loop。
- 结论:target 是 **multi-input + InfoNCE** 模型,P4 工作量 > mnist_trf(单输入 + 分类)。mnist E2E 先行验证 workflow,再扩 multi-input 支持 + 适配 target。

### 5.2.2 自适应路径(2026-08-12 用户指导:不改 target 源码 + 镜像 nas-supernet)
**铁律**:`target/` 源码零改;adapter 全写 `$ORCA_ARTIFACTS_DIR`(workflow 自己的树,如 nas-supernet)。
**手段 = 镜像 nas-supernet ns_expand 的 LLM 自适应 flatten**(非确定性 expand_model.py):
- pz_expand agent 读 target/model.py + train.py → 在 artifacts 产 self-contained `target_flat.py`:
  - 复制 CrossFusion + 依赖类;加 **Wrapper** 把 4 输入打包成单 tensor(state_dict keys 与 pre_trained.pth 一致 → load 零 missing)。
  - `build_model() + DUMMY_INPUT{shape:[1,1920]} + __main__` 自检 block。
  - `eval_model(model)->float`(k-NN,用传入 model)+ `build_dataloader()`(pack 4 路→单 tensor)。
- **fidelity smoke**:load pre_trained.pth 进 flat → 零 missing/unexpected(否则 fail loud)。
- **eval smoke**:加载原模型跑 eval → 与 manifest 记录的 baseline 一致。
- 不便确定性跑的(如多输入 forward)→ 派 agent/verifier 审查(ns_expand 模式)。
- manifest 驱动下游:eval 入口 / ckpt 路径 / train 入口 / data / eval_kind 全在 manifest,下游脚本读 manifest(非 workflow inputs)。

## 9. 输入契约对齐 nas-supernet(2026-08-12 用户指导)
当前 puzzle.yaml 要求 `build_fn/eval_fn/eval_kind/train_loader_fn/pretrained_ckpt` 为 [ask]——违背自适应。**收缩到与 nas-supernet 一致**:
- `[ask]`: project_root / model_path / target_latency(+ accuracy_tolerance 作精度目标)
- `[advanced]`: latency_unit / latency_script_path / block_candidates
- `[default]`: seed
- 删除 build_fn/eval_fn/eval_kind/train_loader_fn/pretrained_ckpt 作 user input——由 pz_expand agent **发现**写进 manifest,下游读 manifest。

## 10. pz_expand 自适应重构(镜像 ns_expand Step 1)
1. **Discover**:读 project → manifest(model 结构/训练 eval paradigm/loss/metric/ckpt 路径/eval 入口/train 入口/data)。
2. **Flatten**:产 self-contained flat + `__main__`(build_model + DUMMY_INPUT)。
3. **Fidelity smoke**:load 预训练 ckpt 进 flat → 零 missing(fail loud 校验)。
4. **Eval smoke**:加载原模型跑 eval → 与 manifest baseline 一致;不便跑→派 verifier。
5. **block_map + 搜索空间张开**:基于 flat + 知识库(block 类型 + depth via no_op + 每层 width via ffn 剪枝)。
6. **baseline 实测**:acc(eval_fn)+ latency(measure 或 wrap latency_script_path)。
7. workflow-verifier + memory-verifier 闭环(point-to-file)。

### 5.3 mnist_trf fixture 契约(P3,待建 `tests/e2e_puzzle/fixtures/mnist_trf/`)
- `model.py`: PatchEmbed(Conv2d 1→embed_dim, kernel 3, stride 2 → flatten [B, num_patches, embed_dim]) + N×`TransformerBlock`(MHSA + FFN + 2 LayerNorm + residual) + mean-pool + Linear(embed_dim, 10)。
- 保留 `build_model(**cfg)` 工厂 + `KNOBS={"num_blocks","embed_dim","num_heads","d_ff"}` + `DUMMY_INPUT={"shape":[1,1,28,28]}`,使 train.py/eval.py 零改。
- attention 用 `nn.MultiheadAttention` 或自定义 MHSA(类名含 "Attention" 便于 slot 检测);ffn 类名含 "FeedForward"/"MLP"。
- `train.py`: CE + Adam + CosineAnnealingLR(参考 mnist_kd/train.py);`eval.py`: `evaluate()`→top-1 acc,打印 `ACCURACY: <float>`。
- 参考范式: `examples/kd-nas-demo/.../_demo_blocks.py:TinyTransformerBlock`(改 4D→3D BLC)。
- baseline 目标: ~0.95+ acc (4-6 epoch CPU,小 transformer)。

---

## 6. 可视化计划(分散到节点,无单独 viz agent — 对齐 nas-supernet)

| 时机 | 节点 | 图 | label |
|---|---|---|---|
| BLD 边跑 | pz_run_bld | progress_watcher 多指标曲线 (per-variant BLD loss) | `puzzle/bld` |
| 打分完成 | pz_run_score | block_score_bar / latency_dist / score_vs_latency | `puzzle/score` |
| GKD 边跑 | pz_retrain | progress_watcher (loss/acc) | `puzzle/retrain` |
| GKD 完成 | pz_retrain | compare_table (block-library vs selected) | `puzzle/retrain` |
| 终态 | pz_report | baseline_vs_optimized metrics_bar (ACC + LAT) | `puzzle/report` |

全部经 `orca.chart.render_chart` → tape custom(chart) → 三壳渲染;in-session 才连 chart daemon (`ORCA_CHART_SOCK`)。

---

## 7. SDD 执行计划(分 phase)

- **Phase P0**: 设计草稿(本文件)+ 用户确认(下文决策点)。
- **Phase P1**: workflow YAML 骨架 + 7 agent.md + subagents/puzzle/ verifier 体 + workflow-checklists → `tars validate` 0 error。
- **Phase P2**: 确定性脚本库 `workflows/agents/_puzzle_scripts/` (bld/score/latency_table/mip_select/build_selected/gkd_retrain/gate/report 图) + unit smoke。
- **Phase P3**: mnist_trf fixture + dry-run(小 epoch)验证搜索空间张开 + MIP 选架构 + GKD 跑通。
- **Phase P4**: target 适配(InfoNCE 打分/cosine GKD)+ dry-run。
- **Phase P5**: in-session E2E 两项目,断言 ACC≤0.5 / LAT≥2×;code-reviewer 洁净审查闭环。

---

## 8. 用户已确认的决策点 (2026-08-12)

1. **mnist 模型** → **新建 mnist_trf fixture** (patch-embed + TinyTransformerBlock stack,保留 build_model/KNOBS/DUMMY_INPUT 契约)。
2. **候选块集** → **默认集**: attention {identity, random_synthesizer, relu_attention, fnet, softs_star, vanilla}; ffn {identity, ffn_75, ffn_50, linear, no_op}。
3. **E2E 顺序** → **先 mnist_trf (CPU 快) 再 target** (串行降风险)。
4. **target 控时** → **全程全网** (BLD/打分/eval 全用全网,不抽子集;接受 CPU 较慢)。

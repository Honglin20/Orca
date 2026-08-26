# Puzzle-Supernet（PSU）设计草稿 (SDD)

> 跨阶段设计议题，PSU 各 phase SPEC 撰写前必读。
> **定位**：`workflows/nas-supernet-v3.yaml` 的 **1:1 fork**（不合并 puzzle workflow 任何组件——adapters 13 API / BLD-MIP-GKD 内核 / puzzle gate 体系一律不进）。9 节点编排骨架、生成/执行节点分离、有界轮询、self-heal、reuse_check、reporter 收敛、时延铁律**原样继承**。
> **审计依据**：10 组审计 agent 逐文件评估 v3 全部脚手架（147 文件、96 条 gate 发现、44 风险、39 开放问题），全量清单见 [`psu-fork-audit-inventory.json`](psu-fork-audit-inventory.json)（本 draft 只落结论，逐文件 verdict 查附录）。
> **置信分层**（spec-review 闭环披露）：inventory 中 `expand_supernet / train_script / search_pipeline` 三组为单轮审计（`cross_checked=false`）——恰是改动最重的三节点，本 draft 对这三处已按源码逐点复核修正（见 D3/B3 相关条目与 §5 blocker 注）；phase SPEC 撰写时**以代码为准、不以 inventory 为准**的三处：`generate_schema.py:44-49` 反射分支（inventory 对平铺单值元组的判定有误，见 §2.4）、emit_result/emit_report 状态推导链、NSGA-II 初始种群无注入口（`DiscreteNSGA2.__init__` 纯随机）。
> **审查状态**：spec-reviewer 三轮对抗（31 问题：24 真问题闭环 + 6 设计权衡确认 + 0 无效）→ **conditional-pass → 修订包已全部并入本版**；U1/U2/U3 三项裁决见 D16/D17/D10。

---

## 0. 目标与已确认决策

**用户诉求**（2026-08-17 拍板，两轮）：以 nas-supernet-v3 为基底改出 puzzle-supernet——超网张开但**不展开维度**，每层只在「原层（冻结）+ attention 变体」中选；有预训练模型，teacher = 原模型（冻结）；student = 随机替换计算方式的超网路径；block 替换模式沿用 v3 的 ChoiceLayer 逻辑；超参（depth/hidden-dim 等）钉原值不搜；最终模型搜出**好组件而非超参**，并走完整 finetune 流程；可视化保留。解除 v3 的「3 个」限制（3 模型族 + 每层 ≤3 分支）。

### 决策表

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | 落地形态 | **独立新 workflow** `workflows/puzzle-supernet.yaml` + `workflows/agents/psu_*` + `workflows/subagents/puzzle-supernet/`；v3 与 puzzle.yaml 均不动 | 用户拍板：v3 是已验证通用 workflow 不污染；puzzle 问题多不继承 |
| D2 | 命名 | 节点 `psu_*` 1:1 对应 ns3_*；磁盘 marker / chart label / artifacts 目录**全链统一 psu / puzzle-supernet 前缀**（含 `.psu_charts.jsonl`、`puzzle-supernet/train` label、`.psu_*_assessment.txt`） | 审计风险：半改名静默断 report 图表汇总与失败归因；一次性全改 + 完工 grep 清零验证 |
| D3 | 模型族支持面（v1） | **transformer 层槽 only**：`model_type.json` 收缩为**单标签 `transformer_layer`**，描述改写为「原层含 attention 机制且可提取 num_heads/head_dim/ffn_dim/max_seq_len slot 事实」——**slot 事实缺失即 unsupported fail loud（前置于变体构造，防 Mixer 类中途 ValueError）**；删 cnn 与 hierarchical_transformer 两族 | 分支集全是 transformer token-mixer；spec-review 实证 v3 `isotropic_transformer` 描述含 "MLP-based token mixing" 会让 Mixer 模型误入 supported |
| D4 | 分支来源 | **快照复制** puzzle 变体库到 `psu_expand_supernet/assets/layer_variants/`：**5 个 layer 工厂（vanilla/random_synthesizer/relu_attention/fnet/softs_star）+ 支撑类 + `resolve_activation` 依赖（内联最小子集，源 `transformer_layer_variants.py:48` 有跨文件 import，须消除）**；**不带 candidate_catalog.yaml**（含 puzzle 专属 MIP/no_op/开发考古）；文件头加 provenance（源路径 + commit + 日期）；P1 增变体快照单测 | spec-review B9：原快照范围「自包含」字面为假；快照 = 源码复制零 import，与 D1 不矛盾（非组件合并） |
| D5 | 分支集形状 | v1 所有 transformer layer slot **统一 branch set**：`{original, vanilla, random_synthesizer, relu_attention, fnet, softs_star}`（original 必含，冻结；枚举序即此序）；per-slot 排除留 OCP | Simplicity First；vanilla 与 original 近重复（同 MHSA 结构）是已知观察——保留作受控对照分支 |
| D6 | gate E 字段 | `psu_expand_supernet.output_schema` 新增独立 boolean `original_equivalence_passed`；route 条件 `model_type_supported != false and original_equivalence_passed != false`。fidelity_passed 语义不变（PSU 版 evaluator 循环内部收敛，沿 v3 惯例不进 route） | fail-loud 归因；audit 一致建议 |
| D7 | 搜索范式 | 沿用 v3 **零训练 validate + Pareto + max-acc-under-target select**；evaluator 禁 teacher/KD loss 进搜索目标 | 用户选 v3 路线；子网评估 = set_sample_config(choice) 后直接 supernet 权重推理 |
| D8 | viability / skip 路径 | **删除** viability 三判据（RL/GAN/自引用）与 `skipped` 状态；`skipped→search` 路由删除。**skipped 删除涟漪七处**：yaml run_train route、run_train agent.md 状态枚举与 OOM 归因叙述、run_search agent.md:42,220 上游归因、retrain agent.md:249 与 monitor_until_done.sh:37 的 GATE_SKIP 死分支、emit_result.py 状态映射 | teacher 冻结不依赖 student、KD 恒可训；未训超网的搜索 metric 无意义——fail loud |
| D9 | evaluation_paradigm | 搜索期恒 `validate`（零训练）；enum 收缩为 `[validate]`；agent.md 删 paradigm override 输入；supernet_summary.md 的 Evaluation Paradigm 段钉 validate 常量 | D7 推论；字段保留（下游引用最小破坏），全链 grep 旧枚举值清零 |
| D10 | retrain 范式 | enum 收缩为 `[finetune-from-supernet]`（U3 裁决）；retrain.py = get_active_subnet 物化选定子网 + **按 selected choice 从超网 ckpt 提取分支权重载入** + 同一冻结 teacher KD 微调；**可训练参数集沿续 freeze 分组**（只训选中路径上的变体分支参数，original 层与非 slot 模块保持冻结） | train-from-scratch fallback 等于退回 v3 范式；teacher≡原模型使前缀 original 层近零信号，冻结保住 gate-E 锚与保底候选语义；首变体层之后的 original 层拿真信号的能力 v1 显式放弃、记 OCP |
| D11 | KD warmup | **删除** warmup 机制与三处非零 gate | v3 warmup 论据（max-subnet teacher 随训练进行）死亡——teacher 预训练冻结，step 0 可蒸馏；student 收敛问题 E2E 实测再迭代 |
| D12 | ckpt 契约 | `pretrained_ckpt` [ask]：torch.load 可载即可；**加载落地 = 一等确定性资产 `load_pretrained.py`**（flatten 期按 manifest 事实生成：内置 DataParallel 前缀剥离/ckpt 包装解包/与 prepared_model 的 strict 键比对），check_flatten 冒烟、check_equivalence 原模型构建、train/retrain 的 teacher + 权重继承**四方复用同一脚本**；超网 ckpt = **全模块 state_dict（含冻结参数），禁 requires_grad 过滤保存**（进生成契约 + check_train_script 静态 gate）；evaluator `load_state_dict` 翻 **strict=True**（落点 §2.4 示例翻转，翻转失败 loud-but-late 的风险由 check_train_script 保存契约 gate 前移） | spec-review B2/B11：strict=False 在权重继承范式下静默丢权重 = 错排整个搜索；「按 manifest 加载入口」必须有确定性落地，且 flat 不 import 用户包 |
| D13 | flatten 增 ckpt 冒烟 | `check_flatten.sh` **第 7 检查**（现有 6 个，v1 编号 off-by-one 已修）：调 `load_pretrained.py` 把 pretrained_ckpt 载入 flat，key mismatch fail loud 列未匹配键清单 | 提前到入口暴露键位错配（llm-optimized 重排改名风险），不等 gate E |
| D14 | gate E 判定标准 | **参照物钉死 = prepared_model + pretrained_ckpt（经 load_pretrained.py 构建）**；eval mode + CPU 确定性优先 + **dtype/device 归一**（两侧同 dtype/device 再比对）；全部输出张量 `allclose(atol=1e-5, rtol=1e-4)`；**输入构造**：原层 forward 签名含 mask 时必须含带 mask 用例（否则 `_MASK_KEYS` 适配 bug 不可见），同输入同 mask 对比；超差/键未匹配 fail loud 报清单 | 确定性优先；容差可被 E2E 实测推翻重定 |
| D15 | 超网训练 eval 协议 | 每 epoch 固定 seed 采 **K=8 条 choice 路径**测用户 metric，均值作 best-ckpt 基准；全 original 路径另测（sanity ≈ baseline，不进选择基准）。**已知风险链（R1 第二触发）**：best-ckpt 若落早期 epoch → 变体欠训 → 搜索排序塌——K 路径均值随 epoch 单调性在 progress 图可观测；**全 original 路径评估全程应为常量（=baseline）——任何漂移 = freeze 被违反的确定性探测器** | 替换 v3 max/min 基准（维度语义已死）；把 freeze 违例从「不可见」变「可观测」 |
| D16 | 非退化验收（U1 裁决） | **验收级断言（不进 workflow 契约）**：E2E 的 selected_arch 至少 1 slot 非 original；E2E `target_latency` 必须 < 原模型实测时延（否则 max-acc-under-target 几乎必然选 all-original ≈ 原模型，PSU 价值不兑现）；§7 AC1 落地 | 用户目标 = 搜好组件而非超参；退化必须显性暴露（B6 补评机制使其可观测，本断言验收层收口） |
| D17 | viable fail-loud 落点（U2 裁决） | **双保险**：psu_train_script route 增 `viable != false` 条件出边（骨架边变化其三：expand 条件 / run_train 无 skip / train_script viable 条件）+ emit_report 增 `training_prerequisites_missing` 兜底分支 | 归因在源头（viable=false 时脚本根本不产出，reporter 分支 5 要求 `.train_rc` 存在会落 unknown terminal state——违反 fail loud） |
| D18 | anchor 补评机制 | psu_run_search 新增确定性脚手架 `append_anchor_candidates.py`：`.search_rc==0` 后、select 前，调**同一** evaluator.py + latency_estimator.py 补评 **all-original + 每 slot 单换 vanilla**（首个非 original，按 D5 枚举序）共 L+1 条，按 arch 键幂等 append；**REUSE 路径同样调用（先于图表推送与 select 的前置不变式——anchor 必须先进 search_results.jsonl，Pareto 图才含 all-original 锚）** | spec-review B6：NSGA-II 初始种群纯随机（无注入口），all-original 入选概率 6^-L——R1 的「original 保底」若无此机制不存在；anchor 候选同时给 Pareto 图一个 baseline 锚点 |

### 五条变化轴（全 draft 的对照锚）

- **A choice-only 搜索空间**：depth 固定=原层数、num_heads/ffn_dim 单值钉原层实测值；每 transformer layer slot = ChoiceLayer 分支容器，唯一搜索维 = choice。
- **B 预训练权重继承**：`pretrained_ckpt` → original 分支拿父权重、变体分支随机初始化、非 slot 模块全冻结。
- **C teacher = 原模型**：训练 = KD（teacher = 独立冻结预训练原模型实例，hidden cosine + logits KL），只训变体分支参数，每 step 采样一条 choice 路径。
- **D retrain = finetune-from-supernet + 同一冻结 teacher KD**（freeze 沿续，D10）。
- **E 等价 gate**：全 original 路径 forward ≡ 预训练原模型输出（逐张量），锁死权重继承 + freeze 分组 + choice 容器。

---

## 1. 差异总览（审计量化）

| 节点 | 文件数 | keep | modify | delete | 改动集中点 |
|---|---|---|---|---|---|
| workflow 级（yaml+8 subagents） | 9 | 3 | 6 | 0 | yaml **四点**（inputs / expand gate E / enum 收缩 / train_script viable 出边）；supernet-evaluator 近重写 |
| psu_flatten | 32 | 4 | 3 | 25 | readiness 规则修剪；manifest 增 ckpt 事实 + load_pretrained.py 生成；删全部镜像死重 |
| psu_expand_supernet ★ | 35 | 3 | 9 | 23 | 知识库换血（三族 elastic spec → transformer_layer choice-only spec）；sandwich gate → 等价 gate |
| psu_train_script | 7 | 1 | 6 | 0 | 训练范式反转（KD recipe / 采样塌缩 / warmup 删 / viability 删 / 全量保存契约） |
| psu_search_pipeline | 14 | 5 | 8 | 1 | evaluator 零训练化（训练循环指南整删 / 三范式收敛 / arch_codec choice-only） |
| psu_run_train | 12 | 10 | **≥2（含 skipped 涟漪后 5）** | 0 | 纯执行节点 + skipped 七处删除链 + chart label |
| psu_run_search | 12 | 7 | 5+1（anchor 脚手架新增） | 0 | full_supernet_latency 语义 → 全 original；anchor 补评；select/emit 契约不动 |
| psu_retrain_script | 6 | 1 | 5 | 0 | strategy 单值化 + KD + 物化子网载入契约 |
| psu_retrain | 18 | 9~16 | 2~9 | 0 | subnet_profile 维度布局特判 → choice 布局 + GATE_SKIP 死分支删 |
| psu_report | 3 | 1 | 2 | 0 | emit_report **+3 分支**（original_equivalence / expand_crashed / training_prerequisites_missing） |

**结论**：fork 是 **modify 型**——执行/编排/reporter 层近乎原样（~70-95% 保留），实质改动集中四簇：expand 知识库、train 范式、search 零训练化、workflow 契约。全量逐文件 verdict + 96 条 gate 发现见附录 JSON。

---

## 2. 节点契约（9 节点）

依赖链（骨架边变化**其三**：expand 出边加 gate E 条件、run_train 删 skip 出边、train_script 加 viable 条件出边）：`psu_flatten → psu_expand_supernet → psu_train_script → psu_search_pipeline → psu_run_train → psu_run_search → psu_retrain_script → psu_retrain → psu_report`。

### 2.1 psu_flatten
- **schema/routes**：零变化（6 字段照抄；`flatten_passed != false` → expand）。
- **agent.md**：Step 2/3 加 PSU 硬约束——mandatory readiness **仅限「输出逐张量等价 + 预训练 state_dict 可原样加载」**，显式禁止改计算/参数结构的重写（pre-norm 转换、norm 类型替换、downsample 替换——v3 readiness 三规则与轴 B/E 结构性冲突，audit 最大发现）；manifest skeleton 增记 ckpt 路径 + state_dict key 布局 + 用户加载入口代码。
- **新增产物**：`load_pretrained.py`（D12 一等资产，flatten 期按 manifest 事实生成，四方复用）。
- **gate**：check_flatten.sh §1-§6 原样 + **§7** ckpt 冒烟（D13）。
- **删除**：references/ 下 14 个镜像 + assets/optimize_rules 下 10 个 optional 规则。readiness 保留：isotropic 布局适配（keep）、transformer_common（删 BN 替换与 pre-norm 转换两规则，Dropout 删除规则保留重写 rationale）、hierarchical（D3 删族则整文件删）。

### 2.2 psu_expand_supernet ★ 核心刀口
- **schema**：字段形状保留 + 新增 `original_equivalence_passed`（D6）；`model_type` 单标签 `transformer_layer`（D3）。
- **routes**：`model_type_supported != false and original_equivalence_passed != false` → psu_train_script；else → psu_report。
- **agent.md**：Step 1 标签收缩；Step 2 重写（新 spec + 权重继承/freeze + `.baseline.json` pin 化 + 等价 gate wiring）；Step 3 精简为分支集 refine；Step 4 summary 增分支集/冻结分组/等价结果/teacher 权重来源。
- **知识库换血**（references/）：
  - **删**：`supernet_specs/{cnn,hierarchical_transformer}/` 两族、`inspect_supernet_examples/` 三个维度极值示例、候选范围 refinement 叙事。
  - **重写** `general_specs.md`：保留组件边界/非搜索逻辑/拓扑保持/输出要求骨架；删 Elastic* API 与维度搜索清单；新增 choice-only 构造规则（分支=固定维度 nn.Module、get_active_subnet=分支选择+deep-copy 无切片、default config=全 original 路径、__main__ 增等价自检）。
  - **新增** `supernet_specs/transformer_layer/{spec.md, search_space.py}`：每 transformer 层槽 = ChoiceLayer；分支 = D5 集（实现 = D4 快照）；num_heads/ffn_dim/max_seq_len 单值钉 `.baseline.json` 实测值——**任何维度 >1 候选即 BLOCKER（反向 gate）**；`ArchConfig` 只记 per-layer choice。
  - **SearchSpace 三条硬约束**（spec-review B3，三处 exec 消费方兼容的根）：①公有 list/tuple 属性**仅 choice 容器**（`branch_choices`）；②钉死维度一律标量或 `_` 前缀私有（防 `generate_schema.py:44-49` 反射把单值元组误报为搜索维度——平铺单值元组会走 `all(isinstance(v,(int,float,str)))` 分支落 `type=list` 假维度）；③零参构造、模块级零副作用（generate_schema / check_expand check2 / full_supernet_latency 三处 `exec(supernet.py)` 消费，构造不得需要 ckpt——ckpt 只进 SuperNet 构造参数）。
  - **get_active_subnet 物化键契约**：物化子网的 state_dict 键 = 分支模块在原模型拓扑路径下的规范键（evaluator strict=True 载入 / retrain finetune 载入 / subnet_profile 三方依赖），spec 明文 + P1/P4 单测覆盖。
- **gate 换血**：**删 `check_search_space.py`**（sandwich 三断言在单值候选下恒假——用户点名）；新增 `scripts/check_equivalence.py`（轴 E：load_pretrained.py 构建原模型 + 全 original 路径逐张量对齐 + requires_grad 分组断言 + 未匹配键 fail loud 清单）；**无论 pass/fail 都落盘 `.equivalence.json`**（reporter 消费，B7）；`check_expand.sh` 第 5 检查换为「choice 契约（含反向维度断言）+ 等价 gate」。
- **不带** `assets/optimize_rules/`（audit 证实零引用）；`assets/layer_variants/` = D4 快照。

### 2.3 psu_train_script
- **schema/routes**：`viable`/`reason` 语义重定义（= 数据管道/eval 入口可移植 + pretrained_ckpt 可加载）；`evaluation_paradigm` enum=[validate]（D9）；**route 增 `viable != false` 条件出边**（D17——false → psu_report 归因 training_prerequisites_missing）。
- **生成契约**（references/workflows/train_supernet_script_generation.md，改动最重）：
  - CLI：删 `--sandwich_n_random`/`--kd_warmup_*`；增 `--pretrained_ckpt`、`--kd_hidden_weight`/`--kd_logits_weight`。
  - §5 增权重继承小节（original 父权重/变体随机/非 slot `requires_grad_(False)`；teacher = prepared_model + ckpt 独立冻结实例，经 load_pretrained.py）。
  - **ckpt 保存契约**：全模块 state_dict、禁 requires_grad 过滤保存（D12，进 checklist item + check_train_script 静态 gate）。
  - **启动期确定性断言**（N3——gate E 只保护自己的加载路径，训练侧二次加载必须自查）：original 分支参数逐张量 ≡ ckpt 抽查值 + teacher `no_grad` forward 冒烟；进生成契约 + check gate。
  - §8 KD 决策框架 → 固定 recipe：teacher 前向 no_grad + **hidden cosine + logits KL**。**成文豁免依据**（B14）：v3 生成契约 :269/:275 明令禁 hidden-state KD 的前提是「teacher 异构需 adapter」——PSU teacher=同拓扑父模型，层输出天然对齐，豁免成立，checklist 同步声明防 verifier 误打回。**纯 KD 显式声明**（B15）：v1 不加 task loss（OCP 记 `--task_loss_weight` 扩展钩）。
  - **hidden hook 对齐规则**（N5）：teacher hook 点与 student slot 清单**同源**（同一 canonical layer 列表驱动）、逐 slot 索引对齐、错位 fail loud——防「错位一层 KD 静默学歪且 loss 照降」。
  - Choice Sampling 重写：`sample_choice_path(search_space, rng)` 每 step 每 slot 采一（sync_random_seed 保留）。
  - §9 eval 与 best-ckpt：D15 协议（K=8 采样路径均值 + 全 original sanity 兼 freeze 违例探测器）。
  - 3× 预算规则删（sandwich 论据死），v1 按单路径蒸馏重定（与用户训练同量级起步，E2E 调）。
- **porter**：分工保留，移植范围 = 「数据管线 + 评估入口」（**不含 teacher 构造**——teacher 由生成脚本经 load_pretrained.py 构建，非 porter 产物，N8 修正）；User-Paradigm Authority 铁律范围收窄为数据 + eval 测度。
- **gates**：check_train_script.sh 删 §6 warmup gate，新增 PSU 静态 gate ×6（`--pretrained_ckpt` 定义 / `requires_grad_(False)` / teacher no_grad+eval / optimizer 不含裸 `model.parameters()` / 全量保存契约 / 启动期断言存在）；check_launcher.sh 同步；checklist 按 §2.3 重写 + 新增 PSU 专项 item。

### 2.4 psu_search_pipeline
- **schema**：零字段变化；`search_record_schema.json` 的 arch 字段自然变 choice 枚举。
- **零训练化**：**删 `evaluator_training_loop_guide.md` 整份**；三范式收敛 validate；checklist item 2 改写（gene = 每 slot 一个 choice-index，gene_len=slot 数，bounds=[0, |branches|-1]）、**item 3「Depth Padding」整条删**（用户点名）、item 14-20/25/26 训练类校验整块删 + 反向断言「evaluator 禁 optimizer/scheduler/grad_scaler/训练循环/ckpt 写盘/teacher/KD loss」。
- **arch_codec.py 示例**：choice-only 重写（保类 API 与 `_to_integer_gene`；删 depth 段/param 段/padding/clamp）。
- **generate_schema.py**：反射逻辑兼容性**有条件**——由 §2.2 SearchSpace 三硬约束保证（B3 修正：不是「不动」而是「spec 钉约束 + 脚本双闸」）：措辞更新 + 断言「发现的搜索维度必须唯一为 choice 容器，否则 FATAL」（双闸：expand 侧 spec 约束 + 本脚本运行时断言）。
- **evaluator.py 示例**：`load_state_dict` **strict=True 翻转落点**（D12）；载入失败 fail loud（前提 = §2.3 全量保存契约）。
- **搜索算法参数**：`population_size = min(默认 32, |branches|^L)`（N4——L=1 时 6^1=6 < 32 纯随机初始化必 RuntimeError）。
- **subagent 修正**（fork 顺带修 v3 既有矛盾）：search-core-gen 删 torchrun 提示；search-latency-gen 删「fixed components 不计入 latency」（whole-arch）；search-select-gen 原样。

### 2.5 psu_run_train
- 纯执行节点：progress_watcher/health/warmup_poll/emit_result 对 KD metric 零硬编码（逐指标推图契约兼容；词法注记：error 扫描按「训练失败」语义而非 kd 指标名）。
- **skipped 删除链七处**（D8 全清单）：yaml route / agent.md 状态枚举 / OOM 归因叙述 / run_search agent.md 上游归因 ×2 / retrain agent.md + monitor_until_done.sh GATE_SKIP 死分支。
- **routes**：`executed → psu_run_search`；`failed → psu_report`。launch.sh chart label 一处（D2）。

### 2.6 psu_run_search
- select/emit/12 字段契约、resume_guard/precheck/reuse、Pareto + max-acc-under-target 结构原样。
- `full_supernet_latency.py`：「max-capacity 全开」→ **全 original 路径 latency**（对照锚）。
- **新增 `append_anchor_candidates.py` 脚手架**（D18）：`.search_rc==0` 后、select 前补评 all-original + L 条单换 vanilla；幂等（按 arch 键去重 append）；**REUSE 路径同样调用（先于图表推送与 select 的前置不变式——anchor 必须先进 search_results.jsonl，Pareto 图才含 all-original 锚）**；latency 与搜索同源（同一 latency_estimator）。
- 图表脚本列名/label 按 D2 全链改名（**可视化保留**：搜索 3 图 + 训练 progress 图 + anchor 候选入 Pareto 图作 baseline 锚）。

### 2.7 psu_retrain_script
- strategy 单值 `finetune-from-supernet`（D10）；retrain.py 生成契约：get_active_subnet 物化子网 + **按 selected choice 从超网 ckpt 提取分支权重 strict 载入**（§2.2 物化键契约）+ 冻结 teacher KD 微调（freeze 沿续：只训选中路径变体参数）+ 用户 eval。
- check_launcher/check_retrain_script/reuse_check 按 KD 契约同步；checklist 删 from-scratch 分支。

### 2.8 psu_retrain
- 纯执行节点 ~95% 保留；`subnet_profile.py::_build_arch_config` 维度布局特判 → per-slot choice 布局；**input_size/num_classes 从 manifest 读，缺失 stderr 标 `assumed`**（B13——28×28/10 静默回落恰被 MNIST 类 E2E 掩蔽）；`compare_table.py`「Full Supernet」行 relabel「All-original path (baseline anchor)」。

### 2.9 psu_report
- reporter 范式无关（0 处维度/teacher 引用）：15 字段 + stage enum 原样。
- **emit_report +3 分支**（B4/B7）：`original_equivalence`（`.equivalence.json` passed=false → stage=expand）、`expand_crashed`（supernet.py 在而无 `.equivalence.json` 且无下游产物）、`training_prerequisites_missing`（D17 兜底：viable=false 且 `.train_rc` 缺失）；artifacts 增列 supernet ckpt。

---

## 3. subagents（8 个全保留，零增删）

| subagent | verdict | 改动 |
|---|---|---|
| supernet-evaluator | **modify（近重写）** | 删「strictly exceeds baseline [BLOCKER]」「≤3 分支 [MAJOR]」「max-arch 默认」「Elastic* 切片 API」四组断言；加：每 slot original 分支必含 / original 权重 ≡ 父 state_dict / freeze 分组 / `.equivalence.json` PASS / **任何维度 >1 候选即 BLOCKER（反向）**；Non-Searchable Model Logic 节原样保留 |
| project-fidelity-verifier | modify | teacher 升一等审计对象（独立冻结原模型实例、不从超网抽取）；Training semantics 比对对象重划为数据管道/eval 入口；intended-behavior 换 PSU 范式声明（KD loss + hidden cosine 归 new-by-design + 豁免依据） |
| project-porter | modify | 职责收缩声明：KD 训练 loop 与 teacher 构建永不入 porter 产物；scope 示例 = loader/eval/metric 计算 |
| memory-verifier | modify | NAS decisions 一致性检查集换 PSU 口径（choice-only / 权重继承 / KD 范式 / retrain 恒 finetune） |
| search-core-gen | modify（轻） | 钉死 choice-only 采样口径；删 torchrun 矛盾提示 |
| search-latency-gen | keep | 时延铁律逐字适用（修 fixed-components 矛盾一处措辞） |
| search-select-gen | keep | schema 驱动，Pareto/select 结构不变 |
| workflow-verifier | keep | checklist 驱动引擎不动——**全部 companion checklists 同批 choice-only 化**（漏改则按旧标准误验收） |

---

## 4. workflow 契约（puzzle-supernet.yaml）

- **description**：重写为 PSU 口径（需 pretrained_ckpt / choice-only KD 蒸馏超网 / 冻结 teacher / 搜组件不搜超参）；全链 grep「无需预训练权重/从零」清零。
- **inputs**：+ `pretrained_ckpt [ask]`（string，required，相对 project_root 或绝对，D12）；其余六字段 + input_invariants 逐字保留（时延铁律轴不变）。
- **outputs**：全部映射原样（全读 psu_report.output）。
- **跨节点磁盘契约**：`.train_rc` / `.retrain_rc` / `runs/train/supernet_best.pth` / `.selected_arch.json` / `runs/retrain/retrain_best.pth`（**钉死 .pth 扩展名**）照搬，writer/reader 全在 fork 文件集内同步改名。

---

## 5. 风险（显式承担）

- **R1（ML 级，最大实证风险）**：weight-sharing 排序信噪比——异构分支 + 零训练评估。缓解三层：KD 单路采样（每分支独立对 teacher 学过）+ original 冻结保底 + **D18 anchor 补评**（机制上保证 all-original 在候选集，退化显性可观测）。**第二触发链**（D15）：best-ckpt 落早期 epoch → 变体欠训 → 排序塌——progress 图可观测，E2E 实测；退路 = top-N 短 KD finetune 复评（非 v1）。
- **R2（联动风险）**：spec / gate / evaluator / companion checklist **四处同步换血**；完工 grep `depth_candidates|num_heads 候选|sandwich|strictly exceeds|status=skipped|GATE_SKIP` 清零（后两项 scoped 到状态判定行，防 chart 词汇误伤）。
- **R3（gate E 工程）**：flatten 重排改 state_dict 键位 → 缓解：D13 入口冒烟 + check_equivalence fail loud 报未匹配键 + flatten 硬约束禁结构改写。
- **R4（freeze 漏配不可见于 forward）**：freeze 断言进 check_equivalence.py + 训练启动期断言（N3）+ D15 全 original 常量探测器（训练全程）。
- **R5（残留回归点）**：Self-Check 清单 sample_*/kernel 项等旧断言散布——P5 grep 清单化验收。
- **R6（fork 自带 v3 既有矛盾）**：torchrun 提示 / latency fixed-components / 文件计数 stale——fork 时顺带修，不回写 v3。**已知实现偏离**：ModuleType exec 探针修复（py3.14 dataclass 兼容）已应用于 psu 侧全部 exec 消费点；ns3 孪生未动（v3 只读约束），构成已知 twin drift，backport 由 v3 维护方决定。
- **R7（CI 覆盖缺口）**：byte-identical 测试硬编码 ns* 目录——PSU 语义改动副本（_common.py / full_supernet_latency / subnet_profile / compare_table）不进任何测试。P5 决策：psu_run_search↔psu_retrain 的 `_common.py` 互为 byte-identical 新测试对；三个语义改动副本单列单测或显式排除 + 注释。

---

## 6. SDD 执行计划

- **P0 fork 机械层**：复制 ns3 九节点 + subagents → psu_*；全链 rename（D2）+ 删除清单（§1 delete 列）；grep 清零验证。产出：可 `tars validate` 的空转骨架。
- **P1 expand 知识库**：transformer_layer spec 族（含 SearchSpace 三硬约束 + 物化键契约）+ general_specs 重写 + D4 变体快照 + check_equivalence.py + load_pretrained.py 生成契约 + supernet-evaluator 重写 + flatten readiness 修剪 + check_flatten §7。单测：spec 契约 + 等价 gate（toy 模型 + toy ckpt）+ 变体快照 + load_pretrained。
- **P2 train 范式**：§2.3 全部（生成契约 / checklist / agent.md / 两个 check 脚本 / evaluator_paradigm.md 收缩）。
- **P3 search 零训练化**：§2.4 全部。
- **P4 retrain + yaml**：§2.7-§2.9 + workflow yaml（inputs / schema / routes×3 / enum）+ emit_report 三分支。
- **P5 验证**：`tars validate` 0 error + 洁净契约审查（warning 清零）+ 全部 gate 单测 + R2/R5 scoped grep 清单 + R7 测试集决策。**二次 fan-out 复查**（用户指令）：逐节点 agent 审实现——定义清晰 / 最大化复用 / 非 PSU 功能正确清除 / 可视化保留。
- **P6 E2E**：in-session（opencode run + tars skill + orca CLI），`D:\Projects\playground\mnist_trf` + `mnist_trf.pt`。AC 见 §7。
- **P7 收尾**：code-reviewer 闭环 + release note + CHANGELOG + CURRENT.md。

---

## 7. 验收（E2E）

1. 端到端跑通到 psu_report（不中途失败短路）。
2. **非退化断言（D16）**：selected_arch 至少 1 slot 非 original；E2E `target_latency` < 原模型实测时延（bootstrap 前实测确定具体值）。
3. **LAT AC**：selected_latency（search_record_schema 单位字段为准）≤ `target_latency`；建议 psu_retrain 对物化子网复测 latency 入报告。
4. **ACC AC**：`final_metric ≥ baseline × 0.99`（higher-better；lower-better 反向断言）。前提（可比性）：同一验证 split、eval 入口经 fidelity 逐字移植、baseline 与 final 同测法。（v1 废弃 max(baseline−0.5, baseline×0.99) 双公式——`baseline−0.5 > 0.99·baseline ⟺ baseline>50`，对 0~1 指标恒取后者，前者是死项。）
5. **等价 gate 实证**：gate E PASS（含 N3 训练启动断言 + N2 mask 用例构造两前提）。

---

## 8. 开放问题（留给 phase SPEC / E2E 迭代）

- OQ1：K=8 eval 路径数与预算替换值的实测调参（P6）。
- OQ2：hierarchical_transformer v2 是否加回（D3 先删）。
- OQ3：branch set per-slot 排除机制（D5 留 OCP）。
- OQ4：gate E 容差在 CUDA/NPU 上的数值行为（D14 CPU 优先规避）。
- OQ5：task_loss_weight 扩展钩（B15，纯 KD 不收敛时启用）。
- OQ6：首变体层之后 original 层的真信号微调（D10 freeze 沿续的放弃面，v2 评估）。

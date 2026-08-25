# Prof-Opt v2 重构设计草稿 (SDD)

> **[SUPERSEDED]** 本稿已由 [`prof-opt-v4-design-draft.md`](prof-opt-v4-design-draft.md)（v3.1）取代（2026-08-25）。其 D-N10（基线完整训练非阻塞 + epoch 对齐）经用户再次拍板**采纳**并升级为 v4 的核心机制（D-V4-1/2/2b）；review-agent / 硬件知识库 / mfu_adapter 四层链 / full_train live 监控早停维持**搁置**（共识范围外，v4 草稿 D-V4-13）。v2 搁置物状态：`placeholder_profiler.py` + `mfu_benchmark.py` 保留在 `_po_scripts/`（无 v4 消费者，占位待真 profiler 接入再定）。

> 跨阶段设计议题，prof-opt v2 各 phase SPEC 撰写前必读。
> **定位**：对已交付的 `workflows/prof-opt.yaml`（v3.5 从头训练范式，10 节点全 agent）做**重构**——不是新 workflow、不是 ns3/psu fork。问题域不变（profiling 证据驱动的模型结构优化闭环），变化集中在三点：① 接**真 profiler**（用户外部 `mfu_benchmark.py`，6613/1951 NPU）；② 提案从"纯机械准入"升级为"**业务逻辑约束 + 多角度对抗校验**"；③ 沉淀**全局硬件适配知识库**（哪些结构更符合当前硬件）。
> **与 v3.5 关系**：骨架资产（shadow/contract/proxy 公平对比/写回/回边循环）全部继承；节点从 10 收缩到 9（propose+implement 合并），新增 2 个 subagent（profiling-agent / review-agent），propose 降级为 subagent；placeholder profiler 退役、真 MFU 数据接入。逐条 keep/retire/new 见 §7。
> **审查状态**：草稿 v1.2。v1.1 = 本轮写出 + 自审修复 B/D/F；v1.2 = 用户五步流程映射拍板（§0.5 + D-N10~D-N16：baseline 曲线先行非阻塞 / 业务逻辑分析基线 / epoch 对齐粗比对 / 细粒度 live 监控早停 / 数值门槛 inputs 化 / 产物落 docs / baseline 原脚本不变量）。subagent 分发审查环境不可用，改自审；SPEC 前仍建议一轮独立对抗审查。

---

## 0. 用户诉求与已确认决策（本轮讨论拍板）

1. 一定需要一个 **profiling agent**（用户外部 MFU Bottleneck Analyzer），且要**项目无关**（删 `pure_cnn`/`feat_complex` 等项目内部 jargon，不硬编码任何模型族）；
2. profiling 的**脚本数据 + LLM 分析建议都要**：`mfu_benchmark.py` 出确定性原始数据，profiling agent 出结构化瓶颈报告；
3. **核心痛点**：现有/预期提案会出"砍层数"等**不符合原业务逻辑**的改动。要求：在符合原有业务逻辑的基础上改模型、降时延、保精度；只有**强提案**能"部分覆盖原业务逻辑 + 精度微损 + 时延降低"时才放行；
4. 提案后 → 实现 → 实现后**重 profile** 看是否真更好（长任务），中间多一个 subagent 做**校验/对抗**；
5. propose 是轻职责 → 降为 **subagent**，不过度拆解；
6. **沉淀知识库**是重点：全局共享、按模型类别分层、**只保留通用规则**（不放项目知识），用于判断"哪些模型更符合当前硬件"；
7. 精度验证**两阶段**：阶段一用小 epochs（proxy）验精度，阶段二仅终态胜者完整训练——与 v3.5 D5 一致（用户本轮重申，见 D-N1）。

### 用户五步流程映射（v1.2 拍板补充）

用户以"主 session 五步"描述了期望流程，逐条映射到节点（结构性一致，差异只在状态载体与判定权）：

| 用户步骤 | 映射 | 关键点落点 |
|---|---|---|
| 1. baseline：模型审查 + MFU 时延测量 + 瓶颈 + **业务逻辑分析（每模块做什么/物理意义）** + **训练后台执行、逐 epoch 指标重定向记录、baseline.md** | po_flatten + po_baseline | K1 业务逻辑基线（D-N11）；K2 曲线先行（D-N10） |
| 2. optimize → **verify-agent 审查**（a. 优化点对增益的影响；b. 业务逻辑一致性，不一致时物理意义能否全覆盖/部分覆盖）→ 不过则 optimizer 针对结论继续优化 | po_optimize + po_verify(review-agent) | 三轴裁决（§5）；**review 逐项理由写入 history，是下轮 propose-agent 的必读反馈**——"针对结论继续优化"的机械载体 |
| 3. 粗粒度测量：少量训练（轮次可不等同默认）→ 与 **baseline 对应 epochs** 比对，增益损失 > 0.1dB → 回步骤 2 | po_probe | epoch 对齐读 baseline 曲线（D-N12） |
| 4. 细粒度筛选：完整训练默认参数 + **定期监控 vs baseline，连续差异过大提前终止** → 回步骤 2 | po_full_train | live 监控 + 早停（D-N13） |
| 5. 校验：**时延降 50-70%、MSE 相对基线降 < 0.1dB**，不达标回步骤 2 | po_report 终态判定 | 数值门槛 inputs 化（D-N14） |
| 全程：所有文件/实验明确记录，**产物保存在 docs 下** | po_report | D-N15 |
| **baseline 必须用项目原有脚本测量** | po_baseline | D-N16 |

用户步骤 2 的"optimizer↔verify 内环"= DAG 的一轮（optimize→verify→probe→gate 回边）：verify 不过的变体不晋升，轮末 gate 带 review 结论回边，下轮 propose 读结论重新提案——同等效果，预算可控、去重有 history。

### 决策表（新增/修订；继承自 v3.5 的 D1-D15 未列者不变）

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D-N1 | 精度验证分层 | 沿用 v3.5 两阶段（proxy 小预算验精度 → 仅 winner 完整训练）；无 L1 零样本 | 用户本轮重申；贵的验证只花在便宜验证通过的变体上 |
| D-N2 | 真 profiler 接入 | `mfu_benchmark.py`（外部脚本，唯一权威）经 **`mfu_adapter.py` 确定性适配器** → PROFILER_CONTRACT 四件套；makespan = `schedule_result.json` 的**并行 cycles**（§2）。placeholder 退役 | 复用 v3.5 全部确定性门（analyze.py / predict_delta.py / 时延 gate）；不写适配器则真数据塞不进封闭字段契约 |
| D-N3 | profiling 分层 | **测量层 = 确定性长任务**（`run_mfu.sh` detach+有界轮询，落原始 MFU 产物）；**分析层 = profiling-agent（LLM subagent）**，测量完成后才被调用，读产物出结构化瓶颈报告（§3）。LLM 不碰长任务轮询 | 长任务形态引擎要求有自己的 detach+poll；LLM 只做"定位瓶颈"判断，裁决（gate）仍归脚本 |
| D-N4 | 节点收缩 | 10 → 9：`po_propose`+`po_implement` 合并为 `po_optimize`（循环体）；`po_verify` 吸收 review-agent 对抗校验；`po_probe`/`po_gate`/`po_full_train`/`po_report` 保持独立 | propose 是轻活降 subagent；implement+重 profile 是长任务须独立节点；verify/probe 是长任务、gate 是纯决策——均不可并（§1 客观评价） |
| D-N5 | propose 降 subagent | `po_optimize` 节点内 dispatch **propose-agent**（subagent）读瓶颈报告 + 知识库 + playbook → `proposals.json`（含机械准入）。proposals.json 仍是机器可核对 checkpoint | 用户拍板"propose 是轻活"；proposals.json 跨 `po_optimize`→`po_verify` 边界保留审计点 |
| D-N6 | 业务逻辑约束 + 对抗校验 | 新增 **review-agent**（subagent），实现后对每个变体**三轴独立证伪**：① 时延（重 profile 实测 vs base）② 业务逻辑保真（`covers_business_logic: full|partial|none`）③ 精度风险。admit 规则：`covers != none` 且 时延 gate 过 且 精度风险 ≤ 预算；`partial + 强时延收益 + 微损可接受` 才放行 partial（§5） | 用户核心诉求：拦住"砍层数"类破坏业务逻辑的提案，除非强提案可部分覆盖 |
| D-N7 | 硬件适配知识库 | **全局共享**、repo 内版本化、按模型类别分层、**只存通用规则**；propose-agent/review-agent 读，每次 run 结束经 `kb_append.py` 确定性追加校验过的观测（§6） | 用户拍板；可 diff、可 review、不污染项目知识 |
| D-N8 | profiling-agent 输出形态 | subagent **写盘文件 + sentinel 行**（复用 memory-verifier 模式），文件为封闭 schema 的 `bottleneck_analysis.json`（§3.4）；节点用确定性脚本校验 schema | 机器可读数据经文件流动（subagent return value 是散文，不可靠）；sentinel 证明校验确实发生 |
| D-N9 | 瓶颈报告刷新时机 | profiling-agent 仅在**当前 base 结构变化**（轮末 promoted 推进）后重跑；base 未动则复用 `bottleneck_analysis.json` | 真 profiler 是远程长任务 + LLM 分析贵；v3.5 的 analyze.py 每次重跑是本地廉价操作，语义不同 |
| D-N10 | baseline 曲线先行（非阻塞） | po_baseline 把**基线完整训练**以后台 detach 启动（项目原脚本、默认参数、固定 seed），逐 epoch 指标 append 到 `baseline/baseline_metrics.jsonl`，结论 + 后台重定向路径写 `baseline.md`；节点在训练**启动后即完成**（不等训练结束）。粗/细比对全部 **epoch 对齐**读该曲线（variant 训到 N epoch → 读 baseline@N；baseline 尚未到 N 则有界等待）。**替代** v3.5 的 baseline proxy 锚 + 终态懒加载满训基线 | 用户拍板"baseline 不阻塞"；一条曲线提供所有 epoch 的公平比对点（同数据同 seed 同入口），比单点 proxy 锚信息多且省一次重复训练 |
| D-N11 | 业务逻辑分析基线 | baseline 期产出 `baseline/business_logic.md`——LLM 逐模块分析"做什么工作 + 物理意义"（如：该层做信道估计/该 head 输出概率分布/归一化保 scale 不变性）。**review-agent 轴②的锚定物**（与 project_manifest 并列，manifest 记事实、此文档记语义） | 用户步骤 1 的业务逻辑分析；covers 判断需要"业务语义"基线，flatten 期不产出则 verify 无锚 |
| D-N12 | 粗粒度 gate（epoch 对齐） | po_probe：幸存变体短训 N epochs（N = input `coarse_epochs`，缺省由契约期按全量轮数推定，如 10%）；训完与 `baseline_metrics.jsonl` **@N** 比对，按指标方向归一后差 > `coarse_gain_budget`（默认 0.1，dB 量纲由项目 metric 自带）→ 淘汰；通过 → 晋升 + 轮末推进 | 用户步骤 3；公平不变量 = 同入口同 seed 同数据、epoch 数对齐 |
| D-N13 | 细粒度监控 + 提前终止 | po_full_train：winner 完整训练（默认参数），监控钩子定期采样 vs baseline 曲线同 epoch 点，**连续 `deviation_patience`（默认 3）个 checkpoint 超 `final_gain_budget` → kill 训练进程、标记 early_aborted、回边步骤 2** | 用户步骤 4；省完整预算，明显跑偏早撤 |
| D-N14 | 终局校验门槛（inputs 化） | 达标 ⇔ `时延降低 ≥ latency_reduction_min`（默认 0.5 = 降 50%；70% 为期望上界，**不作硬门**——降得多不是失败）AND 终局指标差 ≤ `final_gain_budget`（默认 0.1 dB）。全部走 input，不硬编码项目数值 | 用户步骤 5；50-70% 是目标带，门槛只取下界 |
| D-N15 | 产物落 docs | workspace 仍 `artifacts/prof-opt/`（跨 run 复用、锁、幂等语义不变）；po_report 终态把 run 记录 + 终报告 + 关键曲线复制到 `<project_root>/docs/prof-opt/`（input `report_dir` 可改，默认开启）——与写回同为"终态一次性用户侧写入" | 用户要求"所有产物保存在 docs 下、有明确记录"；工作区与交付物分离 |
| D-N16 | baseline 原脚本不变量 | baseline 的训练与指标**必须出自项目原训练脚本原样运行**（Tier A 直用 / Tier B 逐字移植入口只用于变体，且 paradigm-verifier 认证）；变体与 baseline 的唯一差异 = 结构改动 | 用户要求"baseline 必须使用项目原有脚本测量"；否则锚点自身可争议 |
| D-N17 | inputs 精简 + 阈值通用化（本轮已落地 prof-opt.yaml） | 22 → **12** 个 inputs。**[ask] 只剩 4 个**：project_root / model_path / `latency_reduction_min`（相对基线降幅比例，无量纲 (0,1)，**替代绝对 target_makespan**——绝对 cycles 是 profiler 内部量纲，不该让用户填）/ `accuracy_budget`（方向归一无量纲，粗/终局共用）。删除 12 个：pretrained_ckpt（仅参考无 gate 价值）、baseline_ref_acc（被 D-N10 曲线先行替代）、reference_onnx（告警级低价值）、freq_ghz（零引用纯展示）、stall_rounds、max_proposals_per_round、probe_max_steps、promote_relax、min_improvement_cycles/pct/ratio（placeholder 时代调试旋钮）——后七者**降为脚本常量**（2/4/500/1.0/100/1/0.5），行为不变但不再暴露给用户。新增 report_dir（D-N15 落地）。gate_decide.py 改为读盘上基线 makespan × (1−ratio) 推导绝对门槛 | 用户拍板"阈值按项目不同定义，不能定死；inputs 太臃肿"；通用 workflow 的达标线必须是相对无量纲量 |

---

## 1. 总体形态：9 节点 + 5 subagent（全 agent 节点，回边循环）

```
                          ┌────────────────────────────────────────────┐
                          │  轮次循环（DAG 回边；po_gate 脚本内硬帽双保险）│
                          │                                            │
  po_flatten (agent)      │   po_optimize (agent·循环体) ──────────┐    │
    ↓                      │     ├ 刷新 base 瓶颈(profiling-agent)   │    │
  po_contract (agent)      │     ├ dispatch propose-agent → proposals│    │
    ↓                      │     └ implement 变体(编辑+导出+DONE)     │    │
  po_baseline (agent·脚本链)│                    ↓                    │    │
    ↓ ─────────────────────┼─── po_verify(agent·脚本+review-agent) ──┘    │
  po_optimize ─────────────┘      ├ 重 profile(长任务,确定性)         ↑    │
    ↑                            ├ 时延 gate                         │    │
    │                            └ dispatch review-agent(对抗)       │    │
    │                                    ↓                          │    │
  po_gate(agent·纯决策) ← po_probe(agent·脚本,proxy 训练+晋升)        │    │
    │ loop→po_optimize        │                                     │    │
    ↓ full-train              └─────────────────────────────────────┘    │
  po_full_train (agent) → po_report (agent) → $end                      │
```

| 节点 | 职责 | 判断/确定 | 相对 v3.5 |
|---|---|---|---|
| po_flatten | shadow 建立 + manifest + 锁 + 就绪检查 | 勘察判断 + 脚本校验 | 不变 |
| po_contract | 三契约发现 + 实测 + proxy 预算 + run 模板 | 判断 + 脚本验证 | 不变 |
| po_baseline | 基线链：导出 → run_mfu → adapter → analyze → dispatch profiling-agent（瓶颈）+ 业务逻辑分析（`business_logic.md`，D-N11）→ **基线完整训练后台启动（非阻塞，逐 epoch 指标 + baseline.md，D-N10）** → 目标校验 | 纯脚本链 + subagent ×2 | 曲线先行替代 v3.5 的 proxy 锚 + 懒满训基线 |
| po_optimize | **循环体**：刷新 base 瓶颈（base 变才跑）→ dispatch propose-agent → implement 变体 | 判断（implement）+ subagent | **propose+implement 合并** |
| po_verify | 重 profile（长任务）+ 时延 gate + dispatch review-agent 对抗校验 → verdicts（**逐项理由入 history，供下轮 propose 反馈**） | 纯脚本 + subagent | 吸收 review-agent |
| po_probe（粗粒度） | 幸存者短训 N epochs（后台）→ **与 baseline 曲线 @N 比对**（差 > coarse_gain_budget → 淘汰）→ 晋升 + 轮末推进 | 执行 + 脚本 gate（epoch 对齐） | proxy 单点锚 → baseline@N 曲线锚 |
| po_gate | 纯读现算零写盘决策 + 轮数硬帽 | 纯脚本 | 不变 |
| po_full_train（细粒度） | winner 完整训练（默认参数）+ **定期监控 vs baseline 曲线，连续超预算 → 提前终止回边**（D-N13）+ 终局预算判定 | 执行 + 脚本监控 gate | 新增 live 监控 + 早停 |
| po_report | reporter + 写回 + KB 追加 + **终局校验（D-N14）+ run 记录复制到 docs/prof-opt/**（D-N15） | 磁盘读态 + 脚本 | 新增 KB/docs/终局门槛 |

**subagent（被节点 dispatch，非 DAG 节点）**：

| subagent | 谁 dispatch | 职责 | 来源 |
|---|---|---|---|
| profiling-agent | po_baseline / po_optimize | 读原始 MFU 产物 → 结构化瓶颈报告（§3） | 用户外部 agent，改造后 |
| propose-agent | po_optimize | 瓶颈报告 × 知识库 × playbook → proposals.json（§4） | 原 po_propose 的 LLM 职责下放 |
| review-agent | po_verify | 三轴对抗证伪（§5） | 新增 |
| memory-verifier | po_flatten | 复核 manifest 语义 | 继承 |
| paradigm-verifier | po_contract | 复核移植训练入口保真 | 继承 |

回边：`po_gate --loop--> po_optimize`（编译层显式容环；防死循环 = gate 脚本内轮数硬帽，继承 v3.5 D4）。

**客观评价小结（本轮讨论结论）**：10 节点在闭环目标下无冗余；真正可省的是 propose（轻活 → subagent）。propose+implement 可并（proposals.json checkpoint 仍在 `po_optimize`→`po_verify` 边界保留）；verify/probe（长任务 detach+poll）与 gate（纯决策 + 回边源）**不可并**。

---

## 2. profiling 分层与 MFU 契约适配

### 2.1 四层数据流

```
mfu_benchmark.py (外部,唯一权威,远程提交)
   └─ 原始产物: *.csv / *.macs.csv / subgraph_0_tasks.json
      / schedule_result.json / *_taskgraph.json / 甘特图 / 内存图
         │  [1] run_mfu.sh (确定性 detach+有界轮询,长任务)
         ▼
   profile/ 原始产物
         │  [2] mfu_adapter.py (确定性,封闭字段校验)
         ▼
   PROFILER_CONTRACT 四件套: taskgraph.json / ops.csv / schedule.json / profile_summary.json
         │  [3] analyze.py (确定性,v3.5 复用)
         ▼
   bottleneck_report.json (makespan/critical_path/hot_patterns/cost_table)
         │  [4] profiling-agent (LLM subagent,项目无关)
         ▼
   bottleneck_analysis.json (富化增量: MFU%/delay/root-cause,不重复 [3] 的确定性字段)
         │  消费方: propose-agent(读) / review-agent(读) / predict_delta.py(只读 cost_table,走 [3])
         ▼
```

- **[1][2][3] 全确定性**，裁决层无 LLM；时延 gate、predict_delta 只消费 [3] 的 `cost_table` + `makespan_cycles`。
- **[4] 是 LLM 唯一进入"瓶颈定位"的地方**，产出面向提案/审查的结构化视角；gate 不消费 [4]（D-N3）。

### 2.2 makespan 口径（关键契约）

- **canonical makespan = `schedule_result.json` 的并行 cycles**（用户 prompt 明确"调度结果（串行/并行 cycles）"）。串行 cycles 仅作报告展示，不进任何 gate。
- 单元 = 整数 cycles，与 v3.5 全链量纲一致；`freq_ghz` 仅展示换算（继承 D8）。
- **禁止跨 run 重缩放 cycles**（继承 PROFILER_CONTRACT §Gate convention）：`min_improvement_cycles`/`min_pred_actual_ratio` 的语义依赖同一量纲。

### 2.3 mfu_adapter.py 职责与开放项

职责：读 `profile/` 原始产物 → 填 PROFILER_CONTRACT 四件套封闭字段（`taskgraph.json` 的 operators 每算子 `name/op_type/task_id/pipeline/latency/depends_on/output_memory/output_dimensions/onnx_nodes`，`schedule.json` 的 assignments，`profile_summary.json`）。不支持/缺字段 → fail loud（不静默丢算子——契约硬规则）。

**开放项（待 mfu_benchmark.py 实产确认，写 adapter 前必须实测）**：
1. `depends_on`（数据依赖）来源：`*_taskgraph.json` 是否含边信息；若无，需从 ONNX 图（onnx 加载）补依赖，禁止凭空造边。
2. `output_dimensions`/`output_memory`：`subgraph_0_tasks.json` 是否含静态 shape；若无，需 ONNX shape 推断补，动态 shape 按契约 fail loud。
3. `pipeline` 标签：mfu 的子图编号（`subgraph_0`）是否可直接映射为 pipeline 分组；多子图模型需定"pipeline = 子图 id"还是其他。
4. 算子名稳定唯一性：mfu 的 task/op 名是否跨 run 稳定（predict_delta 的 shape-class 定价依赖稳定 name 对齐）。
5. `MFU/delay_cycles` 不进四件套（封闭字段拒绝未知 key），**仅**由 profiling-agent 在 [4] 消费——adapter 不得私自扩字段。

---

## 3. profiling-agent 契约（项目无关）

### 3.1 定位

- 形态：**subagent**（`workflows/subagents/prof-opt/profiling-agent.md`），由节点经 `task` 工具 dispatch；**不占 DAG 节点**。
- 边界：**只定位瓶颈 + 出结构化报告，不产生提案**（提案归 propose-agent，D-N5）；不碰长任务轮询（测量由 [1] 完成）。
- 触发时机：po_baseline 首次；po_optimize 在**当前 base 结构变化后**重跑（D-N9）。

### 3.2 输入

1. `$ORCA_ARTIFACTS_DIR/base/profile/` 下的原始 MFU 产物（[1] 已落盘）；
2. `base/bottleneck_report.json`（[3] analyze.py 产出，提供 makespan/critical_path/hot_patterns/cost_table 的结构化骨架）；
3. 芯片/精度/核数等本次 profile 的运行参数（由节点注入）。

### 3.3 项目无关化改造（相对用户原 prompt）

- **删**：`pure_cnn`/`feat_complex` 及一切项目内部方案名、模型族硬编码、`运行在主 session` 的主-agent 框架语。
- **改**：从"跑脚本+解析+给建议"的整体 agent → "读已落盘产物 + 出结构化报告"的分析 subagent；输出从散文 markdown → **写盘 JSON + sentinel 行**（D-N8）。
- **保留**：H1-H5 硬规则（必须解析实际文件 / 量化说话 / 建议可操作 / 区分根因与表象 / 评测失败也要分析）；`MFU<30%`、`delay_cycles` 占比、`MFU>100%` 估算偏差的瓶颈识别规则（并入报告字段，见 3.4）。
- **修正**：`MFU>100%` 在用户原 prompt 里既列为瓶颈信号又列为"忽略"——统一为"小 shape 估算偏差，记 `estimation_bias`，不计入瓶颈 Top-N，仅告警"。

### 3.4 输出：`bottleneck_analysis.json`（封闭 schema，只增量、不重复确定性事实）

写盘到 `$ORCA_ARTIFACTS_DIR/base/bottleneck_analysis.json`；首行 sentinel `[subagent:profiling-agent v1 <SENTINEL>]` 由报告文件承担（复用 memory-verifier 模式：sentinel 行 + body）。

**单一真相原则（审查 B 修复）**：makespan / critical_path / cost_table / pipeline_breakdown **只**由 analyze.py 的 `bottleneck_report.json` 产出（确定性）；profiling-agent 报告**只携带增量**——top_bottlenecks 的 MFU/delay/root_cause 富化 + 根因总判 + notes。`base_report` 回指 analyze.py 产物，两文件字段零重叠、无第二真相源。

```json
{
  "schema_version": 2,
  "chip": "6613",
  "base_report": "base/bottleneck_report.json",
  "top_bottlenecks": [
    {"rank": 1, "op_type": "MatMul", "name": "on__MatMul_42", "cycles": 4000,
     "share": 0.26, "mfu": 0.45, "delay_cycles": 800,
     "root_cause": "compute"}
  ],
  "root_cause_summary": "dma-bound",
  "notes": ["<仅展示,不进任何 gate>"]
}
```

字段约束：
- `chip` ∈ {`6613`,`1951`}；`root_cause` ∈ {`compute`,`dma`,`fragmentation`,`estimation_bias`}；`root_cause_summary` ∈ {`compute-bound`,`dma-bound`,`fragmentation-bound`,`mixed`}——全封闭枚举。
- `top_bottlenecks` 按 `cycles` 降序、`rank` 与序一致；`cycles`/`share`/`name` **必须与 analyze.py 产物一致**（referential integrity），仅 `mfu`/`delay_cycles`/`root_cause` 是 profiling-agent 从原始 MFU CSV 读入并分类的增量。

节点侧 `check_bottleneck.py` 确定性校验（失败 → fail loud 重派一次，仍败 → `po_report`）：
1. 封闭 schema：未知 key → fail；required 字段齐全；枚举合法。
2. **referential integrity**：每个 `top_bottlenecks[].name` ∈ analyze.py 产物的 `critical_path` 或 `hot_patterns` 算子名；`cycles` == 该算子在 analyze.py 产物中的确定性 `latency`；`share` ≈ `cycles / makespan_cycles`（容差 1e-3）。
3. 排序与 `rank` 一致性；`top_bottlenecks` 非空（`op_count > 0` 时）。
4. `base_report` 路径存在且可解析。

这条 referential 校验把 LLM 的瓶颈声明锚定到确定性事实——LLM 无法虚构不存在的算子/cycles，只保留"读 CSV + 分类根因"这类 analyze.py 不做的语义增量。

---

## 4. propose-agent 契约

- 形态：subagent，`po_optimize` 经 `task` 工具 dispatch。
- 输入：`base/bottleneck_analysis.json`（§3.4）+ `base/bottleneck_report.json`（cost_table 供 predict_delta）+ 硬件适配知识库（§6）+ playbook（v3.5 三杠杆，后续按知识库扩展）。
- 职责：出 `rounds/<NNN>/proposals.json`（沿用 v3.5 顶层 shape + 准入清单）。LLM 只负责"依据证据 × playbook 选候选 + 数站点 + 定 op_delta"；**预测收益仍由 `predict_delta.py` 脚本算**（v3.5 硬规则，LLM 不拍数字）；history 去重仍走 `history_lib.py`。
- 相对 v3.5 po_propose 的变化：只删"节点形态"外壳（不再是 DAG 节点），Step 0-5 逻辑与 proposals.json schema 不变。
- 知识库如何进入提案：playbook 给出"怎么改"（结构模板），知识库给出"值不值得改 + 风险"（结构→芯片 的 MFU/cycles 观测）。propose-agent 在 `pattern_evidence`/`accuracy_risk` 字段里引用知识库条目 id。

---

## 5. review-agent 契约（对抗校验，用户核心诉求）

### 5.1 定位与时机

- 形态：subagent，`po_verify` 在**重 profile + 时延 gate 之后**dispatch（先过便宜的时延证伪，再烧 LLM 做语义对抗——继承 D5 分层思想）。
- 目的：**主动证伪**一个已实现的变体，而不是被动描述。三轴独立裁决，任一"证伪成立"即给出淘汰建议。
- **为什么现在补这层（重要前提）**：v3.5 的 playbook 只有 3 个安全杠杆（激活/归一化/零参搬移），结构性**不会**产出"砍层数"提案——现状提案本就业务逻辑安全。"砍层数"类破坏性提案只会在知识库驱动新增**深度/宽度重构、砍层**等激进杠杆后出现。review-agent 因此是**安全解锁激进杠杆的前置**，不是修现状 bug。

### 5.2 三轴裁决

| 轴 | 证据来源 | 输出 | 性质 |
|---|---|---|---|
| ① 时延 | [1] 重 profile 实测 makespan（确定性，review-agent 只复核不重测） | `latency_verdict: pass|fail` + 实测/预测比 | 确定性 gate 已算，复核 |
| ② 业务逻辑保真 | 变体 diff（vs 当前 base）+ **`project_manifest.md` 记录的业务语义**（flatten 产出、memory-verifier 已复核） | 逐项 `preserved|changed-equivalent|changed-with-risk|broken` → `covers_business_logic` | LLM 语义裁决（锚定 manifest，非自由心证） |
| ③ 精度风险 | 知识库先验 + playbook risk 分级（**先验 pre-filter，非最终精度 gate**） | `accuracy_risk: low|medium|high` | LLM 先验 |

**轴 ② 的锚定（审查 D 修复，防自由心证）**：业务逻辑基线 = `project_manifest.md`（flatten 期已文档化模型意图，memory-verifier 已语义复核）。review-agent **逐项枚举"本模型的功能契约"**（如：输出是 N 类概率分布 / attention 分数行归一 / 某 head 对 scale 敏感 / 训练目标与评估指标方向），逐项对比变体 diff 给出 `file:symbol` 证据 + 四档 verdict——**范式 = paradigm-verifier 的逐项 identical/divergent 表**，而非一句"可能破坏逻辑"。

`covers_business_logic` 由逐项 verdict **确定性推导**（非 LLM 自由给）：
- `full` ⇔ 无 `broken` 且无 `changed-with-risk`；
- `partial` ⇔ 无 `broken` 且 ≥1 `changed-with-risk`（有覆盖理由 + 可接受微损）；
- `none` ⇔ ≥1 `broken`。

### 5.3 输出与 admit 规则

写盘 `rounds/<NNN>/review/<vid>.json`（sentinel + 封闭 schema，含逐项 verdict 表）。`po_verify` 用**确定性脚本**聚合 admit 规则（LLM 只出逐项 verdict + 证据，covers 与 admit 都由脚本推导——架构底线）：

```
covers = full | partial | none            # 由逐项 verdict 确定性推导(§5.2)
admit  ⇔  latency_verdict == pass
          AND  covers != none
          AND  (covers == full  OR  (covers == partial AND 时延收益 ≥ partial_cover_threshold AND accuracy_risk ≤ 预算))
```

- `partial_cover_threshold`：新增 input（默认 = 强收益门槛，如基线 makespan 的某个百分比），`partial` 提案需"部分覆盖业务逻辑 + 强时延收益 + 精度微损可接受"三条件同时成立才放行——即用户"强提案可部分覆盖"的机械表达。
- `covers == none`（≥1 项 `broken`）→ 无条件淘汰，**这是拦"砍层数"类提案的主闸**。
- 轴 ③ `accuracy_risk` 是**先验 pre-filter**（拦明显高风险变体、省 proxy 预算）；**最终精度裁决归 po_probe 的晋升 gate（确定性）**，review-agent 不替代它——"贵的 proxy 训练只花在便宜证伪已通过的变体上"（D5）。
- **幂等（审查 A 补充）**：po_verify 重执行（at-least-once）时，`review/<vid>.json` 已存在且可解析 → 复用、不重派 review-agent（subagent 复用范式，同 memory-verifier）。

### 5.4 对抗性保证

review-agent prompt 显式要求：默认倾向证伪（"找出为什么这个改动不该被采纳"），证据必须落到 `file:symbol` + 具体业务语义，禁止空泛"可能影响精度"。复用 paradigm-verifier 的"逐项对比 + 证据"范式。

---

## 6. 硬件适配知识库（全局共享，用户重点诉求）

### 6.1 存储与分层

- 位置：repo 内版本化（可 diff、可 review），`workflows/kb/hardware-fit/`，子目录按模型类别分层：
  ```
  hardware-fit/
    vision-transformer/  cnn/  language-model-decoder/  telecom-transformer/ ...
      <chip>/  (6613/ 1951/)
        <structure-pattern>.json   # 观测条目(封闭 struct)
    _index.json                    # 机器可查索引（pattern × chip → 观测）
  ```
- **只存通用规则，不放项目知识（审查 F 修复）**：KB 条目是**封闭 struct、无自由文本**——`model_family`/`structure_pattern`/`root_cause_class`/`latency_impact`/`accuracy_risk_if_replaced` 全封闭枚举，`mfu_range`/`cycles_share_range` 是数值区间，`known_levers` 回指 playbook 杠杆 id。**不存在能泄漏项目名/模型名的文本字段**，因此"只存通用规则"靠 schema 构造保证，而非黑名单正则（黑名单对无限项目名集必然漏）。`kb_append.py` 只从结构化字段提取，绝不拷贝原始文本。

### 6.2 条目 schema（观测，封闭 struct）

```json
{
  "schema_version": 1,
  "structure_pattern": "softmax_attention",
  "model_family": "vision-transformer",
  "chip": "6613",
  "mfu_range": [0.2, 0.4],
  "cycles_share_range": [0.3, 0.6],
  "root_cause_class": "compute",
  "latency_impact": "high",
  "accuracy_risk_if_replaced": "medium",
  "known_levers": ["C1"],
  "evidence_runs": ["<run_id>"],
  "added_by": "po_report",
  "ts": "<iso>"
}
```

- 全字段封闭枚举 / 数值 / 数组，**无 `notes` 自由文本**。若确需人读注释，放同目录 `README.md`（不入 `_index.json`、不被任何 gate/agent 程序化消费）。

### 6.3 读写闭环

- **读**：propose-agent（值不值得改 + 风险）、review-agent（精度风险先验）、profiling-agent（根因分类时套用"该结构在 6613 上 MFU 通常多少"的通用先验）。
- **写**：**唯一写者 = po_report（run 终态一次）**，用 `kb_append.py` 从本 run 的 `bottleneck_analysis.json`（top_bottlenecks 的 root_cause/mfu）+ review verdicts（accuracy_risk/covers）+ 终局 history 提取**结构化观测**（映射到封闭枚举、按 model_family/chip 归层、去重合并），追加 `_index.json` + 对应 `<structure-pattern>.json`。新条目带 `evidence_runs` 追溯。
- **与 playbook 的分工**：playbook = "怎么改"（结构杠杆模板，可执行）；KB = "改了划不划算 + 风险"（经验观测）。二者互补，KB 的 `known_levers` 回指 playbook 杠杆 id。

### 6.4 冷启动

初始种子 = 用户原 profiling prompt 的"优化建议知识库"（Conv→MatMul 分解 / DMA 搬运 / Reshape-Transpose 碎片 / MatMul-Softmax / 内存超 L1D / MFU>100% 偏差）逐条映射成封闭枚举条目（`structure_pattern` ∈ {`conv_decomposed_matmul`, `dma_bound`, `reshape_fragmentation`, `matmul_softmax`, `memory_over_l1d`, `estimation_bias`} 等）。`pure_cnn`/`feat_complex` 等项目 jargon **不进入**（无法映射到封闭枚举即丢弃）。此即"哪些模型更符合当前硬件"的第一版通用规则。

---

## 7. 与 v3.5 的关系（keep / retire / new）

**keep（不变，继承）**：po_flatten、po_contract、po_probe、po_gate、po_full_train、po_report 的职责与骨架；shadow 注入机制（D2）；proxy 公平对比 + 两阶段精度（D5/D11）；写回语义（D1）；回边循环 + 硬帽（D4）；确定性脚本资产 `analyze.py`/`predict_delta.py`/`history_lib.py`/`advance_round.py`/`gate_decide.py`/`assert_shadow.py`/`emit_result.py`；memory-verifier/paradigm-verifier。

**retire（退役）**：`placeholder_profiler.py`（真 MFU 接入后无用途；PROFILER_CONTRACT.md 保留作 adapter 契约权威）；`po_propose`/`po_implement` 两个独立节点（合并为 po_optimize）。

**new（新增）**：`mfu_adapter.py`（适配器）、`run_mfu.sh`（测量层长任务）、`check_bottleneck.py`（瓶颈报告 schema 校验）、`kb_append.py`（知识库追加）、profiling-agent / propose-agent / review-agent 三个 subagent、`partial_cover_threshold` input、硬件适配知识库目录。

---

## 8. 开放问题（写 SPEC 前须钉死）

**审查已闭合（本稿已修）**：B（双真相源 → §3.4 referential integrity 单一真相）、F（KB 泄漏 → §6.1 封闭 struct 无自由文本）、D（covers 自由心证 → §5.2 manifest 锚定 + 逐项 verdict 确定性推导）。

**仍需用户/实测钉死**：
1. **mfu_benchmark.py 实产字段**（§2.3 五项）：adapter 映射在拿到脚本实产后实测钉死；`depends_on` 若产物无依赖信息，adapter 需从 ONNX 补依赖——需确认 mfu 是否已有任务图。**前置依赖：用户需把 `mfu_benchmark.py` 提供到仓库（或经 input `profile_script_path` 指向，同 v3.5），否则 [1]-[3] 确定性链路无法落地。**
2. **makespan 口径确认**：并行 cycles 是否为"整图 makespan"（含跨子图串行），还是仅单子图并行？多子图模型的"串行/并行"语义需用户确认。
3. **`partial_cover_threshold` 默认值**：强收益门槛取基线 makespan 的多少百分比（建议 5-10%，待拍）。
4. **知识库跨 run 并发**：多 run 同时 append `_index.json` 的冲突策略（已定：唯一写者 po_report + 追加幂等；跨 run 并发仍建议单写者锁，与 history.jsonl 同范式）。
5. **propose-agent 是否仍保留 v3.5 的 `po_propose` 全套 Step 0-5**：确认下放 subagent 后不丢 reuse-check / 幂等（建议保留全套，只换外壳）。
6. **逐 epoch 指标提取契约**：`baseline_metrics.jsonl` 要求从任意项目训练日志解析**每个 epoch** 的指标（v3.5 契约只解析终值）——po_contract 的 metric 提取规则须扩展为 per-epoch 匹配（正则/行格式），并在契约期实测两条以上 epoch 行。
7. **后台 baseline 训练与后续长任务的 detach 共存**：基线训练 worker 常驻后台跨越整个 run，与 po_probe / po_full_train 的 detach worker 并行——需确认 pid 登记互不干扰、ckpt 输出目录隔离（baseline 独立 out-dir），禁二次 detach 语义按 worker 分键。

---

## 附 A：本轮审查记录（占位，待对抗审查后填）

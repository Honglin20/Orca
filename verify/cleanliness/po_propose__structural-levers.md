# 洁净审查记录 — po_propose/references/structural-levers.md

- **目标文件**: `workflows/agents/po_propose/references/structural-levers.md`（361 行，prompt-adjacent prose——structure-proposer subagent 运行时被指示 first-read）
- **审查方法**: 受众翻转通读（契约 §8）——假设我是被 dispatch 的 structure-proposer 子代理，只懂结构优化业务、不懂 Orca 内部与 workflow 历史，按指引读本文件作提案背景先验
- **契约依据**: `orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md` §3/§4/§6/§9（references/ prompt-adjacent prose 适用）；SPEC `docs/specs/prof-opt-v4-spec.md` §1 L42 / §5 L111；草稿 `docs/specs/prof-opt-v4-design-draft.md` D-V4-8（L33）、§3.2（L169）
- **日期**: 2026-08-25

## 1. 词表 grep 结果（命中即 finding）

词表：`mnist_kd | playground | prof_opt_demo | run_verify | baseline_proxy_acc | baseline_ref | mfu_adapter | perturb_ckpt | playbook | ref-input | auto-trained | docs/specs | D:\Projects | /mnt/d | spec-review | SPEC-R1 | ns3 | psu | kd-nas | nas-supernet | prof-opt-design-draft`（-i，全文）

**零命中。**

补充扫残留措辞（`proxy | lazy | retrain | must match an entry | not listed | only listed | may not propose | forbidden to propose | v1 | v2 | v3 | fine-tun`）命中 3 处，逐一判非残留：

| 行 | 原文 | 判定 |
|---|---|---|
| L13 | "…recovers the metric, **not a fine-tune**" | v4 现行 from-scratch 范式的风险等级语义澄清（固定 seed 全重训），非已删机制措辞 |
| L121 | "MobileNetV3 (Howard et al., 2019)" | `v3` 子串误报——公开模型名，合法 SOTA 引用 |
| L197 | "The from-scratch retraining re-learns…" | v4 现行范式（N3 条目语义），非残留 |

## 2. 逐段受众翻转结论表

| 段（行） | 内容 | 受众翻转结论 |
|---|---|---|
| L1-8 标题+定位 | "used as BACKGROUND PRIORS by the proposal stage…tells you what kinds of structure changes exist, **not which one to pick this round**" | ✅ 背景先验定位显式声明，受众翻转下零歧义；"no unlisted entry → no proposal" 类 v1 机械闸措辞不存在 |
| L10-13 训练范式注 | 每变体固定 seed from-scratch、无权重继承 | ✅ v4 现行范式（基线全训 + 变体重训），与 structure-proposer.md 硬约束一致；非已删机制 |
| L15-19 硬范围注 | 只谈 model-source；训练超参物理不可达 + 空 op delta 被严格负准入门拒 | ✅ 双保险表述与 subagent 契约（"doubly forbidden…rejected by the strictly-negative admission gate"）逐点对应；为现行机制非残留 |
| L21-24 范围声明 | "Scope of this version: six evidence-gated lever families…" | ✅ 目录范围声明（本目录收录哪些家族），无对 v1/v2/v3.5 的版本对照/迁移叙事——非版本考古（契约 §4 中文迁移/版本考古不命中） |
| L26-51 How to use | 读 `base/bottleneck_analysis.json` + `base/bottleneck_report.json`（`hot_patterns`/`cost_table`/`critical_path`/`pipeline_breakdown`）、`baseline/business_logic.md`、`shadow/`、`base/model.onnx`、`base/profile/taskgraph.json` 的 `output_dimensions`、cost-table shape-class、per-op cost override → `prediction_basis` | ✅ 全部引用可解析：四个 report 字段 = `analyze.py` L241-244 实际输出键；`output_dimensions` = `PROFILER_CONTRACT.md` §taskgraph 契约字段；per-op cost override = `predict_delta.py` `--added-cost`（脚本 L31-33 "NOT guessed: pass an explicit --added-cost Op=cycles override"——与本文措辞互为镜像）；`prediction_basis` = structure-proposer.md 输出 schema 字段（L98）。artifacts 均为 po_propose 时点已存在的运行时产物，subagent inputs（structure-proposer.md L22-27）覆盖 |
| L53-63 融合形纪律 + 风险等级 | opset 17 导出融合（LayerNormalization/Softmax/Gelu 单节点）→ delta 恒按实际图数；low/medium/high 等级定义 | ✅ 领域知识 + "actual graph is the truth" 判据，产品说明书式；等级枚举内联定义可直用 |
| L67-143 Lever 1（A1-A5） | 激活替换：模板/证据/export pattern/风险/公开引用 | ✅ 纯领域先验；模板是可改写源码样例非内部路径；引用全部公开文献（Glorot 2011 / MobileLLM 2024 / Howard 2019 / Ramachandran 2017 等） |
| L147-206 Lever 2（N1-N3） | 归一化结构；from-scratch 使参数集变更可行；N3 "the probe stage exists to judge exactly this kind of change" | ✅ "probe stage" 指 v4 现行 po_probe 机制（stop-at-k 判定），受众在 workflow 内可识别；无 perturb_ckpt / 继承权重措辞 |
| L210-241 Lever 3（C1-C2） | 注意力打分 ReLU 化 / 消除冗余算子对 | ✅ 领域先验；C2 "done at model-source level so it survives export" 为设计动机一句——服务运行时决策（为何改源码不改图），受众翻转下有用，非开发期论证 |
| L245-274 Lever 4（D1-D2） | 容量重分配；"Accuracy expectation: `small_negative` with `medium` confidence" | ✅ 直接用输出 schema 词表（`expected_accuracy_impact`/`accuracy_confidence` 枚举值），与 structure-proposer.md L100-101 一致 |
| L278-299 Lever 5（F1-F2） | 低秩/共享投影；"r from a measured shape-class break-even, not guesswork" | ✅ 领域先验 + 与 predictor 契约一致的反猜测约束 |
| L303-329 Lever 6（S1-S2） | 打分路径重构；"require a probe win before promotion" | ✅ promotion = v4 现行 po_probe 双过 promote 语义；S2 "the exported ONNX graph is the final truth" 与通篇判据一致 |
| L333-344 Proposal admission checklist | 严格负（predictor 产，禁心算）/ edited_files ⊆ shadow / op delta⊕描述一致 / 签名 canonical 构建 / 字段在场 | ✅ 与草稿 D-V4-8 **保留的**机械准入三闸逐项对应（predict_delta 严格负 / edited_files ⊆ shadow 闭包 / op_delta⊕change_spec 一致）+ structure-proposer.md Method 3-4（predict_delta.py / build_change_sig）——是 v4 保留机制非 v1 已删机械闸；v1 "不列条目不许提案"（提案必须机械匹配目录条目）不存在，头段已显式反向声明 |
| L346-361 Accuracy & Pareto 契约 | 输出字段枚举 + Pareto 支配丢弃 + "profiling is the latency truth, training is the accuracy truth" | ✅ 枚举与 structure-proposer.md 输出 schema 完全一致（none/small_negative/large_negative/unknown × low/medium/high）；排序指导为判断支持非机械匹配表 |

## 3. 契约一致性核对（SPEC §1 / 草稿 D-V4-8）

- **SPEC §1 L42**：`po_propose/ { …, references/{structural-levers.md} }` —— 文件在交付物清单在场，形态为 references/ 参考文件。✅
- **SPEC §5 L111**：structure-proposer 输入含 `references/structural-levers.md`，节点侧校验 = 机械准入三闸 + count ≤3 + 去重对账 + rationale 校验——本文件 admission checklist 与三闸一致，未越权引入已删机制。✅
- **草稿 D-V4-8（L33）**："playbook → structural-levers.md（背景先验）"，理由"机械匹配已证伪"——本文件为背景先验（头段显式声明 + 全文领域知识式条目），机械匹配闸已除（仅保留 v4 三闸）。✅
- **草稿 §3.2（L169）**：structure-proposer 硬约束 = 结构级（禁训练超参：训练脚本不在闭包内物理不可达 + Δ=0 被拒双保险）——本文件 L15-19 硬范围注与之逐点吻合。✅

**结论：与 SPEC §1 / §5 及草稿 D-V4-8 一致，无不一致项。**

## 4. Findings 清单

**零 finding。**

非阻断观察 2 条（记录备查，不计 finding——均可解析、非残留、非语气问题，输出 schema 的权威源是 structure-proposer.md，本文件为背景先验）：

1. **L342-344 / L348-351**：admission checklist 第 5 条与字段契约段枚举 accuracy 字段时未列 `accuracy_risk`，而 structure-proposer.md 输出 schema（L100）含该字段、本文件逐条目的 "Accuracy risk: low/medium/high" 等级正是喂它的。不影响可用性（子代理契约为准）。若追求完备可在 L343 补 `accuracy_risk`。
2. **L173（N1）**：等级写 "medium-low"，略偏离 L61-63 声明的 low/medium/high 三档。作为先验自释无碍；如需严格三档可改 "medium"（或 "low-to-medium" → 仍非档内值，建议直接 "medium"）。

## 5. 结论

受众翻转通读（361 行全文逐段）+ 词表 grep（零命中）+ 残留措辞补充扫（3 处命中均判非残留）+ SPEC/草稿契约核对（一致）：文件为产品说明书式背景先验，无开发期残留、无 v4 已删机制措辞、无 v1 机械闸残留、无未定义引用。

VERDICT: CLEAN

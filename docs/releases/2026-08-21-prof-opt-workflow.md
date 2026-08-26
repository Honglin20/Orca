# Release: prof-opt workflow —— profiling 证据驱动的模型结构优化闭环（v3.5 从头训练范式）

**日期**：2026-08-21 · **状态**：E2E 双项目通过（target 完整闭环成功 / mnist_kd 合法 exhausted 终态），工作区未提交

## 交付物

- `workflows/prof-opt.yaml`——10 节点全 agent DAG（含 po_gate→po_propose 回边循环）
- `workflows/agents/po_{flatten,contract,baseline,propose,implement,verify,probe,gate,full_train,report}/`（agent.md + scripts/references）
- `workflows/agents/_po_scripts/`——16 个跨节点共享确定性脚本（含 PROFILER_CONTRACT.md 对外契约 + placeholder profiler + meta-path-finder 注入）
- `workflows/subagents/prof-opt/`——memory-verifier / paradigm-verifier（报告落盘 + 哨兵校验）
- `tests/test_po_{scripts,inject,diff_check}.py`——70 项单测（analyze/predict_delta per-site 计价/gate_decide/advance_round 幂等键/history_lib 去重/注入/两层声明校验/fresh_start 全清等）
- 文档：`docs/specs/prof-opt-design-draft.md`（v3.5，D1-D15 决策表 + 四轮审查闭环记录）、`docs/specs/prof-opt-spec.md`、`docs/specs/po-probe-report.md`（P0 回边实证）

## 核心设计（一句话）

profiling 已是给定算子集下的最优调度 ⇒ 唯一杠杆是改模型结构；闭环 = 瓶颈分析（定位到操作）→ playbook 提案（脚本算收益）→ L0 时延证伪（两层声明校验 + 重 profile）→ L2 **同预算从头训练公平对比**（基线与变体逐字段同 proxy 预算，promote 判定）→ 回边叠加轮 → winner 完整训练（自动满训基线锚）→ 终局 shadow diff 写回（新文件名，用户原文件零改动）。LLM 只当提案器，一切裁决归确定性脚本；用户项目全程只读（唯一写入 = artifacts/ 子树 + 终态新文件）。

## v3.5 训练范式（用户 2026-08-21 拍板）

一律从头训练、零权重继承：weight_delta/make_variant_ckpt/γβ 折叠/零样本 L1 整体退役（OCP 留档）；pretrained_ckpt 降为仅参考；公平不变量 = 同 run 基线与变体 proxy 训练参数逐字段一致（contracts.json 单一来源）；final 锚 = baseline_ref_acc input，缺省自动满训基线一次并缓存；深宽/低秩杠杆机制解锁（条目 OCP 渐进收）。

## 过程（SDD 全流程）

1. 设计草稿三轮对抗审查（R1 fail 33 项 → R2 fail 26 项窄域 → R3 conditional-pass 全闭环），两个机制级 blocker 均实测证死再换道（in-session 仅 agent 节点；PYTHONPATH 注入输给脚本目录 → meta path finder 唯一可行）
2. 实现 SPEC 对抗审查闭环 + P0 回边探针五项全过（10 节点形态成立）
3. 三批 coder 实现 + 占位符冲突修复（`{{k}}`→`<<k>>`）+ 两轮逐 agent 洁净度审查
4. E2E 三轮：R1/R2（旧范式）machinery 全绿但内容线未行使（融合算子导出形态/predictor shape-class/门槛可调三项通用修复）；R3（v3.5）两项目两条验收线全达
5. 每轮修复后洁净度复审（累计四轮，全部 PASS）

## E2E 证据（v3.5，claude 后端 + tars skill，WSL）

- **target（transformer）**：run `prof-opt-20260821-114750-189f3d` 终态 success——N3 融合 LN 直删提案（-6272c，ratio=1.0）→ L0 过 → 从头 proxy 训练 promote → 回边 round 2（winner base 上 exhausted，硬帽退出）→ best-effort 完整训练 + 自动满训基线（original_shadow 结构 + 同 epochs/seed）→ within_budget → 写回 `model_prof_optimized.py`（字节一致，原文件零改动）；公平不变量机械比对成立；4 次真实训练日志均零权重加载
- **mnist_kd（LeNet CNN）**：run `prof-opt-20260821-150211-d91b65` 终态 failed/gate（合法 exhausted：Conv/Gemm/MaxPool 在 v1 三杠杆零可生成条目，filtered_count=0 机械可复核）——Tier B porter（无 init-ckpt）/Tier A eval/双随机初始化联动（0.1159 vs 0.1008）/失败路径零写回全部正确
- 中断恢复 3 次实证（schema 拒后重派 at-least-once / 宿主被杀后 `orca next` 幂等续跑）

## 遗留（记录，非阻塞）

- P3：真 profiler 接入后 Softmax→ReLU 条目自然激活（placeholder 下同 shape class 零差）；模板名 `run_probe_finetune` 携旧范式词（四文件面一致，纯改名 churn 留待将来）；写回 shadow_synthesized 跳过已实现（旧工作区缺键场景由 fresh_start 兜底）
- **待用户拍板**：v3.6 增量提案——FG 细粒度图卫生杠杆（transpose 消除/常量折叠/等价分解替换 + 等价性检查替代 proxy 训练，来源：用户外部实验观察「一个提方案 + 一个做细粒度优化」模式）

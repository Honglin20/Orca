# 洁净审查记录：workflows/subagents/prof-opt/structure-proposer.md

- 审查人：独立 reviewer agent（每文件一个专用 reviewer）
- 日期：2026-08-25
- 依据：`orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`（§1 受众分离 / §4 通读兜底 / §5 operational 串 / §6 夹具防火墙 / §8 受众翻转通读）+ `docs/specs/prof-opt-v4-spec.md` §5 structure-proposer 行 + `docs/specs/prof-opt-v4-design-draft.md` §3.2/D-V4-7~9（语义权威）+ 骨架对照 `workflows/subagents/prof-opt/memory-verifier.md`

## 1. 逐段受众翻转结论表

| 行 | 段落 | 受众翻转结论（①可独立执行 ②无开发期残留 ③无 v4 已删机制措辞 ④产品说明书语气） |
|---|---|---|
| 1-7 | frontmatter + sentinel 首行指令 | ①哨兵回显机械可执行 ②③无 ④与 memory-verifier 骨架逐字同构。**过** |
| 9-16 | 引言（任务 + 三证据源 + levers 定位） | ①WHAT 明确（≤3 候选 → `rounds/<RRR>/proposals.json`）②无历史/出处③无退役机制词④"never a checklist to grind through" 是运行时使用方式指令（防机械穷举 lever 表），非论证。**过** |
| 18-32 | Inputs（三入参 + 工作区读集） | ①三入参均由 caller 提供（已对 po_propose/agent.md Step 3 dispatch 核对：output_dir/proposals_path/R/levers_ref 一一对应）②全为工作区内 operational 路径，`<agent resources>` 为来源说明、实值走 caller 绝对路径，正确③无④过。注：step 5 引用的 `contracts.json`（`proxy_budget`）未列入 "Read from it" 清单——工作区根文件可自定位，spec §5 输入行同样未列，**观察项非 finding** |
| 34-50 | Hard constraints ×4 | ①逐条可执行（结构级 / 对 business_logic.md / `target_pattern_id`=analysis `name` / 逐签名查重）②"BY CONSTRUCTION…doubly forbidden" 是草稿 §3.2「物理不可达 + Δ=0 被拒双保险」的产品化转写——限定提案边界的运行时事实，非设计考古③无 run_verify/baseline_proxy_acc/mfu_adapter 等措辞④产品说明书式。**过** |
| 52-82 | Method 六步 | ①每步可独立执行；命令已对真实 CLI 核验：`predict_delta.py` 的 `--report/--op-delta/--sites` 与 `build_change_sig(lever, params, modules)` 签名、`history_lib.py` 的 `--history/--sig/--probe-epochs/--probe-max-steps/--probe-data-value` 及 `"blocked"` 输出键全部存在且必填项齐全②`python3 -c` 为单命令单语句链，属契约 §4 允许的单行 operational 内联，非多行循环/分支③`--probe-max-steps null --probe-data-value null` 与 v4 stop-at-k（spec「epoch-only 全 null 保留」）一致；`proxy_budget` 为 v4 现行字段（po_propose/agent.md:174-176 同源引用，D-V4-5）④过。注：命令字面 `null` 与"从 contracts.json 读、不猜"注记并存——注记优先且 v4 语义下 null 恒正确，**观察项非 finding** |
| 84-113 | Output（schema + exhausted 语义） | ①字段契约闭合：spec §5 行要求的 rationale/op_delta/edited_files/change_spec/sota_reference/exhausted_rationale 结构化全在场；node 侧三闸所查字段（predicted_delta_cycles<0、edited_files、op_delta、change_sig、target_pattern_id、accuracy 字段）均有产出义务②示例值（`Erf/Relu`、`pkg/model.py`、`P1`、-3792）为泛化占位非测试夹具，且底部 "No invented numbers" 显式防抄示例③无④"never a feeling" 对应 D-V4-9 防 LLM 谎报的指令化转写，是判据陈述非叙事。**过** |
| 114-123 | Task 返回值 + Constraints | ①哨兵首行 + 单行 compact 摘要 + "file is the authoritative artifact"——与 memory-verifier 骨架同构；修改面（只写 proposals_path）/ 配额 ≤3 / 禁手造数字均可机械遵守②③无④过。**过** |

## 2. 词表 grep 结果

对目标文件全文（`-i` 大小写不敏感；ns3/psu/kd-nas/nas-supernet 词边界）跑：`mnist_kd`、`playground`、`prof_opt_demo`、`run_verify`、`baseline_proxy_acc`、`baseline_ref`、`mfu_adapter`、`perturb_ckpt`、`playbook`、`ref-input`、`auto-trained`、`docs/specs`、`D:\Projects`、`/mnt/d`、`spec-review`、`SPEC-R1`、`ns3`、`psu`、`kd-nas`、`nas-supernet`、`prof-opt-design-draft` —— **零命中**。

补充扫 `§|SPEC|ADR|phase-|v3.5|v4|issue|TODO|迁移|前身|analogue`：唯一命中 line 95 `change_spec`（输出 schema 字段名，契约 §5 明示合法的 operational 串），非 SPEC/ADR 引用。

## 3. 契约一致性核对（spec §5 行 + 草稿 §3.2 + memory-verifier 骨架）

- **输入**：business_logic.md ✓ / bottleneck_analysis.json ✓ / history.jsonl（去重，硬约束 4 + step 5）✓ / references/structural-levers.md（read it first）✓；另读 bottleneck_report.json + profile/ + shadow/ —— step 2/3 机械所需（预测器 `--report` 实参 + 对 base/model.onnx 校验 + 提案编辑对象），为 spec 行主输入的必要超集，非偏移。
- **硬约束**：结构级禁训练超参 ✓（含双保险表述）/ 符合业务逻辑 ✓ / 围绕瓶颈（target_pattern_id=name）✓ —— 与草稿 §3.2 三条逐字对应。
- **输出**：`rounds/<NNN>/proposals.json` ≤3 ✓ + 五必填字段 ✓ + exhausted_rationale 结构化（≥1 已尝试方向条目 + why_not 枚举）✓ + filtered_count（node 侧去重对账数据源）✓。
- **节点校验三闸归属**：三闸（predict_delta 严格负 / edited_files ⊆ shadow 闭包 / op_delta⊕change_spec 一致）按 spec §5 为**节点侧机械校验**（po_propose/agent.md Step 3 已落实），subagent 只承担产出可过闸字段——本文件通过"strictly negative required; non-negative → dropped"、"shadow/ = the thing you propose edits to"、op_delta 实测校验（step 2）履行，无缺位。注：edited_files ⊆ shadow 闭包在 subagent 侧仅为隐式（Inputs 说明 + 示例相对路径），node 会兜底拒——**观察项非 finding**。
- **骨架同构**（vs memory-verifier.md）：frontmatter(subagent/version/sentinel) → 哨兵首行指令 → 标题 + 一段定位 → ## Inputs（caller will provide 三项）→ 方法主体 → ## Output（写盘权威 + 首行哨兵 + 单行返回摘要）→ ## Constraints（Modification scope）——逐节同构 ✓。
- **风险契约锚**：step 6 "accuracy/latency risk contract / Pareto discard dominated" 在 structural-levers.md「Accuracy and Pareto ranking contract」节（line 346-355）有实体定义，指令自包含。

## 4. Findings

**零 finding。**

观察项（不计 finding，供作者参考，非必须修改）：
1. line 74-80：dedup 命令的 probe 配置来自 `contracts.json` `proxy_budget`，但该文件未列入 line 22-27 的 "Read from it" 读集（工作区根文件可自定位，spec §5 输入行亦未列）。
2. line 99：`edited_files` 的 shadow 闭包隶属仅隐式（node 侧三闸兜底）。

VERDICT: CLEAN

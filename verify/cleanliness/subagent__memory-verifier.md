# 洁净审查记录：`workflows/subagents/prof-opt/memory-verifier.md`

- 审查对象：`D:\Projects\Orca\workflows\subagents\prof-opt\memory-verifier.md`（86 行）
- 依据：`orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`（受众翻转通读法）+ `docs/specs/prof-opt-v4-spec.md` §1/§3/§5（v4 = 不变；flatten→memory-verifier dispatch 保留）+ 上游 caller `workflows/agents/po_flatten/agent.md`（v4 零改动节点）
- 词表 grep（mnist_kd / playground / prof_opt_demo / run_verify / baseline_proxy_acc / baseline_ref / mfu_adapter / perturb_ckpt / playbook / ref-input / auto-trained / docs/specs / D:\Projects / /mnt/d / spec-review / SPEC-R1 / ns3 / psu / kd-nas / nas-supernet / prof-opt-design-draft / 懒补训 / epoch-only，-i 大小写不敏感）：**0 命中**。

## ① 逐段受众翻转结论表

受众假设：我是运行时被 dispatch 的子代理，只懂业务（核对模型/训练事实），不懂 Orca 内部、不懂本 workflow 开发历史，第一步全读本文件。

| 行 | 段 | 结论 |
|---|---|---|
| L1-5 | frontmatter（subagent / version: 1 / sentinel: MF6TQ9） | CLEAN。三键规范；哨兵值在 L7 / L65-66 两处引用与 frontmatter 逐字一致（含 `v1`）。 |
| L7 | Output first line 指令 | CLEAN。哨兵 echo 协议可机械执行；与 caller 侧校验（po_flatten agent.md L428 `head -n 1` 比对 `[subagent:memory-verifier v1 MF6TQ9]`）逐字吻合。 |
| L9-18 | 角色定位 + 职责边界（语义准确性核对 vs 机械检查归 flatten validation gate） | CLEAN。两句都是 WHAT：修 manifest 语义错误 + 报告；"机械检查归 flatten validation gate" 是运行时分工指令（告诉本 agent 什么不用查），机制名是泛称、未引引擎源码/脚本路径，v4 po_flatten 零改动 → 该 gate 仍在，引用不失配。 |
| L20-29 | Inputs（`<output_dir>`=$ORCA_ARTIFACTS_DIR 含 project_manifest.md / readiness/readiness.json / shadow/ 树；`<project_root>` 只读；`<report_path>`=<output_dir>/verify/memory_verifier_report.md） | CLEAN。三个占位符由 caller 提供且语义自足；`$ORCA_ARTIFACTS_DIR` 是契约 §5 允许的 operational env。三件输入与 v4 po_flatten workspace layout（agent.md L63-72）逐项存在；report 路径与 v4 caller（agent.md L419-421）逐字一致。 |
| L31-57 | Semantic Verification 四类（描述性断言 / metric 方向 / 解释器断言 / model_facts 交叉一致）+ 缺产物跳过规则 | CLEAN。四类断言均可独立执行且全部指向 v4 仍存在的上游产物：manifest 五段（po_flatten 零改动）、metric 方向要求（po_flatten L121-124 仍在）、Interpreter 记录（po_flatten Step 2 仍在）、`readiness/readiness.json` `model_facts`（v4 键集 module/factory/args/kwargs/container_key/dummy_inputs 不变——本段括号列举是其子集概览，无失配）。L42 "AdamW with weight_decay=0.01" 是方法示例（"若 manifest 说 X，去源码确认精确值"），非 §6 测试夹具硬编码（无项目名/数据集名/具体项目数值）。"readiness.json and the shadow are validated elsewhere; the manifest is the document you fix" 是分工指令，与 caller 侧闭环（po_flatten L435-438 由 caller 自改 readiness.json）一致。 |
| L59-79 | Output（报告必须落盘 `<report_path>`；首行哨兵 verbatim 供 caller 机械校验；body = Status[all-pass/fixed] + Changes[原文/改文/证据]；Task 返回 = 哨兵 + 一行状态 + 路径，文件为权威产物） | CLEAN。落盘权威 + 哨兵协议 + 两态 Status 与 caller 校验链吻合；返回值格式明确，产品说明书式。 |
| L81-86 | Constraints（只许改 `<output_dir>` 下 project_manifest.md + 写报告；禁改 readiness/、shadow/、workspace 其他文件、project_root 任何东西） | CLEAN。修改范围显式、fail loud 边界清楚；与 caller 分工（readiness.json 由 caller 修）无冲突。 |

## ② Findings 清单

**零 finding。**

- 词表 grep（-i）：0 命中。
- 受众翻转通读：无 issue/plan/§N.M 节号、无 Orca 引擎源码路径、无内部 examples 路径、无中英迁移出处词、无 SPEC/ADR/phase 编号、无 spec-review 泄漏、无测试项目/夹具硬编码、无事故复盘叙事、无确定性代码内联（全文零代码块）。
- v4 已删机制残留措辞（run_verify / baseline_proxy_acc / baseline_ref / mfu_adapter / perturb_ckpt / playbook / ref-input / auto-trained / 懒补训 / epoch-only proxy）：逐段通读均未出现（全文连 "proxy"/"budget"/"baseline_acc" 词汇都不存在——本文件只看 manifest 语义，不涉训练预算机制）。
- v4 契约一致性：spec §1 钉「不变」，实际核对成立——其全部上游产物（project_manifest.md 结构、readiness.json model_facts、shadow/、flatten validation gate、report 路径与哨兵串）在 v4 均由零改动的 po_flatten 产出/校验，零失配引用。

Non-finding observations（不计入 findings，供作者参考，不要求修改）：

1. L49-50 允许 "running that interpreter's import probe, read-only"——执行解释器属只读探测，与 L85「不改 project_root 任何东西」不矛盾（运行 ≠ 修改），措辞自洽，不构成 finding。
2. L52-54 model_facts 括号列举（module / factory / args / dummy inputs / container form）略去 `kwargs`——是概览非穷举（"must agree with ... model_facts" 才是判据），v4 readiness schema 未变，无失配，不构成 finding。

## ③ 结论

该文件是产品说明书式的运行时子代理指令：三类输入占位符、四类语义断言、落盘报告协议、哨兵校验、修改范围全部自包含可机械执行；零开发期残留、零 v4 已删机制措辞；其引用的全部上游产物在 v4 均未变化（po_flatten 零改动），与 spec §5「骨架同 memory-verifier」的定位相符（本文件即该骨架本体）。

VERDICT: CLEAN

# 洁净审查记录 — subagents/prof-opt/variant-implementer.md

- 审查对象：`D:\Projects\Orca\workflows\subagents\prof-opt\variant-implementer.md`（131 行，version 1，sentinel VIM9C6）
- 方法：洁净契约受众翻转通读（§8）+ v3.5/v4 退役词表 grep + SPEC §5 契约一致性 + memory-verifier 骨架同构 + 跨文件核对（structure-proposer / po_propose / render_run.sh / check_contracts.sh）
- 日期：2026-08-25

## ① 逐段受众翻转结论表

| 行 | 段落 | ①可独立执行 | ②开发期残留 | ③v4 已删机制残留 | ④产品说明书语气 | 结论 |
|---|---|---|---|---|---|---|
| 1-7 | frontmatter + sentinel 首行指令 | 是（哨兵格式与 memory-verifier 骨架逐字同构） | 无 | 无 | 是 | PASS |
| 9-15 | 角色声明 + 负面边界（不训练/不测时延/不写 history） | 是（scope 边界指令，防越界，运行时有效） | 无 | 无——"do NOT verify latency" 与 v4 时延复测在 po_propose 节点侧一致 | 是 | PASS |
| 17-25 | Inputs.1 output_dir 工作区清单 | 是（shadow/、export 模板、两个部署脚本、contracts.json 均实存——`_po_scripts/render_run.sh`+`diff_check.py`、`templates/export_onnx.template.sh` 于 po_probe/check_contracts.sh/tests 多处印证） | 无 | 无 | 是 | PASS（见观察 O-2） |
| 26-28 | Inputs.2 proposal 对象字段 | 是（字段与 structure-proposer.md:92-103 输出 schema 全对上，lever/prediction_basis 由"identity fields"与步骤 4 枚举覆盖） | 无 | 无 | 是 | PASS |
| 29-33 | Inputs.3 repair_directive 格式 + 证据路径 | 是（structural:/latency: 前缀、verdict.json/verdicts.jsonl 与 po_propose Step 5 修复环一致） | 无 | 无 | 是 | PASS |
| 37-41 | 步骤 1 幂等重入 + sha 不符 fail loud | 是（读 DONE、算 sha、比对——机械可执行） | 无（括注"edited behind the marker"是对不匹配含义的一句话澄清，非设计论证） | 无 | 是 | PASS |
| 43-49 | 步骤 2 影子拷贝（rm + copytree 单行命令） | 是 | 无 | 无 | 是 | PASS（见观察 O-3） |
| 50-56 | 步骤 3 编辑纪律（⊆edited_files ⊆影子闭包、保公共接口、verbatim） | 是 | 无——"(train-from-scratch)"是现行 v4 范式限定词（check_contracts.sh:251 同词），防 agent 过度保守，非已删机制 | 无 | 是 | PASS |
| 57-69 | 步骤 4 declaration.json（identity 逐字复制 + round/seq 派生规则） | 是（派生公式 `r{round}-{seq:02d}` 显式 + 示例 JSON） | 无 | 无 | 是 | PASS |
| 70-75 | 步骤 5 导出（参数 shadow_dir/out/seed；非零退出→variant_broken） | 是——token 名与 check_contracts.sh:262 钉死的 `<<python>>/<<out>>/<<seed>>` 及 render_run.sh 内建 shadow_dir 一致；"renderer injects header + shadow assertion, you only pick parameters"与 render_run.sh:107-129 行为逐条吻合 | 无 | 无 | 是 | PASS |
| 76-83 | 步骤 6 diff_check 文件层预检（exit 1→structural_mismatch；≥2→fail loud） | 是（命令行 + 退出码语义显式） | 无 | 无 | 是 | PASS |
| 84-92 | 步骤 7 sha 钉死 DONE marker | 是（单行 python 命令，无分支循环） | 无 | 无 | 是 | PASS（见观察 O-3） |
| 93-97 | 步骤 8 repair_trace.json（kind 枚举 + 配额由 caller 判定） | 是（结构 ≤2/时延 ≤2 与 SPEC §5、po_propose:29-30 三方一致） | 无 | 无 | 是 | PASS |
| 99-111 | Terminal-skip 双路径（structural_mismatch / variant_broken）+ 现场保留 | 是 | 无 | 无 | 是 | PASS |
| 113-119 | Failure honesty（配额内修不好→诚实 skipped；禁弱化声明迁就误编辑） | 是（反漂移规则，运行时判定可用） | 无 | 无 | 是 | PASS |
| 121-123 | Task 返回值契约（哨兵 + 每提案一行 compact 摘要） | 是（格式逐字给定） | 无 | 无 | 是 | PASS |
| 125-131 | Constraints（写域 ⊆ variants/<VID>/；一 dispatch 一提案） | 是 | 无 | 无 | 是 | PASS |

## 契约一致性与骨架同构核对

- **SPEC §5 行（docs/specs/prof-opt-v4-spec.md:112）逐项对齐**：
  - 输入 proposals.json（提案对象）+ base shadow + 导出模板 → Inputs 1-2 覆盖 ✓
  - 职责：忠实实现单条提案 / 编辑范围 ⊆ 影子闭包（步骤 3 + Constraints）/ 导出可复现（步骤 5）/ 声明与 diff 一致（步骤 4+6+structural_mismatch 路径）✓
  - 修复配额：结构 ≤2、时延打回 ≤2（行 96）✓
  - 输出：逐提案 declaration.json + DONE 或 skipped + compact 摘要（步骤 4/7、Terminal-skip、行 121-122）✓
  - 节点校验的生产侧：diff_check 文件层（步骤 6）/ DONE 存在性（步骤 7）/ repair_trace.json 落盘（步骤 8）✓
- **memory-verifier 骨架同构**：frontmatter（subagent/version/sentinel）→ 哨兵首行指令 → # 标题 + 角色段 → ## Inputs（"The caller will provide"）→ 正文 → ## Constraints（首条 Modification scope）——逐节同构 ✓
- **跨文件**：declaration identity 字段全部由 structure-proposer 输出（structure-proposer.md:92-103）✓；派单三参（`<output_dir>`/`<proposal>`/`<repair_directive>` 首趟为空）与 po_propose/agent.md:157-159 一致 ✓；修复证据 verdict.json/verdicts.jsonl 与 po_propose 修复环及 run_latency_recheck.sh 一致 ✓。

## ③ 词表 grep（命中即 finding）

对目标文件跑全部 20 个 pattern（mnist_kd / playground / prof_opt_demo / run_verify / baseline_proxy_acc / baseline_ref / mfu_adapter / perturb_ckpt / playbook / ref-input / auto-trained / docs/specs / D:\Projects / /mnt/d / spec-review / SPEC-R1 / ns3 / psu / kd-nas / nas-supernet / prof-opt-design-draft）：**零命中**。另人工复核"懒补训 / epoch-only proxy / perturb / finetune-checkpoint"等宽口径措辞：均未出现（全文唯一范式词"train-from-scratch"为 v4 现行机制用语，出处印证 check_contracts.sh:251）。

## ② findings 清单

**零 finding。**

观察项（不计 finding——非开发期残留、不影响独立可执行性，仅供作者参考）：

- **O-1（行 13/21 vs 37+）**：`variants/<vid>/`（小写）与后文 `<VID>`（大写）混用。执行 LLM 可无歧义解析为同一标识，属外观不一致，不构成洁净违规。若顺手统一为 `<VID>` 更好。
- **O-2（行 24-25）**：`contracts.json`（shadow_pkgs, interpreter）列入输入清单但无步骤直接消费（shadow_pkgs 经 render_run.sh 的 env/--set 管线到达 header，非 agent 读 contracts.json 取得）。作为工作区清单条目无害且运行时为真，非残留。
- **O-3（行 43-49、84-92）**：两处多行 `python3 -c` 为无循环/无分支/无 assert 的直线单用途命令（拷树、写 sha marker），落在契约 §4"单行 operational 命令允许内联"的许可侧（违规模态是"循环·分支·assert 逻辑"）；若未来这里长出条件逻辑，应下沉 `scripts/`。

VERDICT: CLEAN

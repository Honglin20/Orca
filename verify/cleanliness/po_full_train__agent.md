# 洁净审查记录：workflows/agents/po_full_train/agent.md

- 审查对象：`D:\Projects\Orca\workflows\agents\po_full_train\agent.md`（166 行）
- 方法：受众翻转通读（洁净契约 §8）+ 词表 grep + prof-opt-v4 SPEC §4 po_full_train 行逐条核对
- 佐证读取：`orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`、`docs/specs/prof-opt-v4-spec.md` §3/§4、`workflows/agents/po_full_train/references/full_train_protocol.md`（仅验证 agent.md 委托不悬空）、`workflows/agents/po_probe/agent.md`（仅验证 healed_files 内联惯例）
- 日期：2026-08-25

## 一、逐段受众翻转结论表

| 段（行号） | 受众翻转结论 |
|---|---|
| frontmatter description (1-4) | PASS —— 运行时任务摘要，产品说明书语气；无残留 |
| Your only task (7-16) | PASS —— 任务陈述自包含可执行（full 训练至真完成 / 对称终检 / eval / 对照锚判预算 / 落盘 final/）；"template 驱动、禁手写训练逻辑"是明确约束 |
| Execution model (18-35) | PASS —— detach + bounded-poll、`final/.train_pid` 唯一 pid 键、`final/train_status.md` 跨 turn 真相源、status message 钉 `do not call orca next` 字面短语、completion 四条件 —— 逐条可机械执行 |
| Resource Anchors (37-49) | PASS —— 全部 operational（`$ORCA_ARTIFACTS_DIR`/`$ORCA_AGENT_RESOURCES`/`{{ inputs.* }}` 均属契约 §5 允许）；"budget ONLY from contracts.json full_train_budget + never re-derive" 是行为约束非设计论证 |
| Path Handling Rules (51-54) | PASS —— pathlib 强制，可执行 |
| Subagent Call Protocol (56-58) | PASS —— 声明零 dispatch，与 SPEC §3 派工表（full_train 不派）一致 |
| Lazy Loading (60-64) | PASS —— 惰性读取指引；"Lazy Loading" 是骨架节名，非"懒补训"语义 |
| Iron rules (66-85) | PASS —— 六条均独立可执行；rule 5 "no automatic re-training" 恰是 v4 删补训路径后的正确**禁止性**编码（非残留） |
| Step 1: Derive state (88-96) | PASS —— 读盘判态清单明确；"effective epochs ARE full_train_budget.epochs — never a recomputed min" 防 epoch-proxy 再推导而不点名旧机制 |
| Step 2: Resolve anchor (98-108) | PASS —— 只读解析；缺失/指纹不匹配/`baseline/train_final.json` 非 done → failed；"the protocol names the exact condition" 的委托在 full_train_protocol.md §State derivation 3（field-for-field + train_final=done）真实落地，不悬空；"The anchor is never re-trained here" = v3.5 auto-trained 补训路径删净的正面证据 |
| Step 3: Launch/resume (110-117) | PASS —— 渲染参数逐一钉死（out dir=final/、`--out final/train.rendered.sh`、epochs/seed 取自 full_train_budget、global shadow）；失败读 log tail + whitelist 内 heal + 2 次重试预算 |
| Step 4: Symmetric final check + eval (119-131) | PASS —— `--expected-epochs` 对称终检（实跑==渲染否则 failed 归因终检）；eval 模板渲染执行、final_acc.json 字段、scripted 预算比较、onnx 拷贝（"makespan referenced, never re-measured" 是防复测的行为约束）。"the same admission clause the baseline finalizer enforces" 属跨节点一致性陈述且实体已在 Step 2 引入，约束本身自包含 —— 边界内，不 flag |
| Step 5: Emit (133-152) | PASS —— 十字段齐（status/final_acc/baseline_full_acc/baseline_full_acc_source/within_budget/final_ckpt/final_onnx/assessment/max_retries_hit/healed_files），失败态字段语义逐一定义（source=null、acc=0、空路径）。healed_files 的单行 `python3 -c` 内联（146）无循环、单行 operational，属契约 §4 明示豁免，且与 po_probe/agent.md:147 逐字同构（仅 heal-log 文件名不同）——v4 既定跨节点惯例 |
| Validation (154-159) | PASS —— emit 期完备性五查；"all ten schema fields" 与 Step 5 字段数吻合（实测 10） |
| Output (161-166) | PASS —— 两种退出形态（完整单行 JSON / 训练在飞 status message）与 Execution model 一致 |

## 二、词表 grep 结果

对目标文件跑 SPEC §6 全量词表（`mnist_kd|playground|prof_opt_demo|run_verify|baseline_proxy_acc|baseline_ref|mfu_adapter|perturb_ckpt|playbook|ref-input|auto-trained|docs/specs|D:\Projects|/mnt/d|spec-review|SPEC-R1|ns3|psu|kd-nas|nas-supernet|prof-opt-design-draft`，-i）：**0 命中**。

增补扫描（退役机制措辞）：`proxy`/`补训`/`epoch-only`/`retrain` —— 0 命中；`auto` 仅出现在 "no automatic re-training"（禁止句，非残留）；`re-train(ed)` 仅出现在 "no automatic re-training" 与 "The anchor is never re-trained here"（两处均为禁止句）；pid 键仅 `final/.train_pid` 一处，无第二 pid 键。

## 三、SPEC §4 po_full_train 行契约一致性核对

| SPEC 条款 | agent.md 落点 | 结论 |
|---|---|---|
| 删 baseline/full_train/ 补训路径与第二 pid 键 | 锚只读解析（98-108，"never re-trained here"）；pid 仅 `final/.train_pid`（23） | 一致 |
| 锚 = baseline_full_acc.json + 指纹逐字段 + 防御 train_final=done | 100-105（MATCHING full_train_budget fingerprint + train_final 非 done → failed）；逐字段语义由委托协议 §State derivation 3 落地 | 一致 |
| winner 同模板 `--out final/train.rendered.sh` + full_train_budget 同指纹 | 112-114（--out final/train.rendered.sh、epochs/seed 取 budget）+ 46-49（ONLY from contracts.json，SAME fingerprint） | 一致 |
| 对称终检实跑==full，不符 → failed 归因 | 121-125（--expected-epochs，非恰等渲染值 → failed 归因 final check） | 一致 |
| baseline_full_acc_source 恒 "baseline"（failed null） | 140（=baseline）+ 151（failed → null） | 一致 |
| 常驻机制继承 | detach+bounded-poll（20-21）、禁二次 detach（23-25）、train_status.md 跨 turn 真相（26-27）、status message 续驱（28-32）、at-least-once（rule 3）、fail loud（rule 4）、out_of_budget 非失败（rule 5） | 一致 |
| §3 通用骨架八节齐全 + full_train 不派 subagent | 全部在场；Subagent Call Protocol 声明零 dispatch | 一致 |

## 四、Findings

零 finding。

- 词表残留：0 命中。
- 开发期残留（受众翻转通读）：未发现（无 plan/issue/SPEC 编号、无 Orca 源码路径、无迁移/考古措辞、无测试项目名、无事故复盘叙事）。
- v4 已删机制残留措辞：未发现；补训路径与第二 pid 键删净，剩余的 "no automatic re-training" / "never re-trained" 均为正确的禁止性指令。
- SPEC §4 契约：逐条一致；所有委托指针（协议条件、scripted 比较、heal whitelist、max_retries_hit、healed_files 来源）在 full_train_protocol.md 中均有落点，无悬空引用。

VERDICT: CLEAN

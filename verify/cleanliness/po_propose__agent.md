# 洁净审查记录：workflows/agents/po_propose/agent.md

- 审查对象：`D:\Projects\Orca\workflows\agents\po_propose\agent.md`（277 行）
- 依据：`orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`（受众翻转通读法）+ `docs/specs/prof-opt-v4-spec.md` §4 po_propose 行（草稿 `prof-opt-v4-design-draft.md` v3 §3/D-V4-7/8/9/10 为语义权威）
- 审查方式：全文通读 + 受众翻转逐段裁决 + 双词表 Grep（SPEC §6 v3.5 词表 + 增补退役物词表；另加补充词表 v3/v4/po_implement/po_verify/mnist/cifar/accuracy=//home//analogue/migrat/R2-/D-V4/迁移/懒补训/epoch-only）
- 词表结果：**全部 0 命中**（两次 Grep 均无匹配）
- 交付物盘面核对：`po_propose/{agent.md, scripts/run_latency_recheck.sh, references/structural-levers.md}` 与 SPEC §1 一致 ✅

## 一、逐段受众翻转结论表

| 行号 | 段 | 受众翻转结论（假设我是只懂本业务、不懂 Orca 内部与 workflow 历史的执行 LLM） |
|---|---|---|
| 1-4 | frontmatter（description + tools） | CLEAN。description 是运行时职责总述（刷新报告→三 subagent→逐提案实现→机械 history→批量复测→打回），产品说明书式，零历史 |
| 5-18 | 角色定义 + 语义铁律 | CLEAN。`exhausted` 合法性、`status == executed ⇔ error == ""` 均可独立执行；无 v4 已删机制措辞 |
| 20-31 | Resource Anchors | CLEAN。`$ORCA_ARTIFACTS_DIR` / `$ORCA_AGENT_RESOURCES` 是运行时 env（契约 §5 允许）；配额常量（≤3/轮、修复 ≤2）指令式 |
| 33-40 | 共享脚本部署检查 for 循环 | **FINDING-1**：多行 bash 循环+分支+exit 内联，命中契约 §4「确定性代码内联」类别（详见 findings） |
| 42-46 | Path Handling Rules | CLEAN。运行时规则，可执行 |
| 48-56 | Subagent Call Protocol | CLEAN。`{{ subagents_root }}` 渲染占位 + sentinel 首行协议 + Task 调用模板，均为运行时调用原语；"inlined as an absolute path at render time" 是执行者需要的路径来源说明 |
| 58-64 | 失败矩阵 | CLEAN。(a) 哨兵缺失 / (b) 产物缺失 / (c) 校验败 → 重派 1 次 → error 披露；配额超额不重派走终态——与草稿 §3.1 Step 5「仍不过淘汰」及 D-V4-4 R2-18「重派 1 次后降级+披露」语义一致（spec §4 括号"超配额→重派 1 次"是粗粒度概括，agent.md 的细化更准确：配额耗尽是确定性状态，重派无意义；见核对表第 9 条） |
| 66-71 | Lazy Loading | CLEAN。惰性读取规则，指令式 |
| 73-76 | Workflow 总则 | CLEAN。checklist 0-6 + FINAL JSON only，可执行 |
| 78-88 | Step 0（轮号 + reuse guard） | CLEAN。`.round_advanced` 是 advance_round 落盘机制（operational）；unparseable → fresh + stderr 是合理 fail-loud 增量 |
| 90-98 | Step 1（刷新机械报告） | CLEAN。单行命令内联（契约允许类），fail loud 显式 |
| 100-126 | Step 2（stamp 守卫 + ledger refresh） | CLEAN。stamp 计算纯 prose 描述；两条单行命令；"still faithful to this base" 是操作性 why（解释复用条件），非考古 |
| 128-153 | Step 3（proposer + 机械校验） | CLEAN。准入项逐条可机械执行；去重对账（re-run 同一 history_lib CLI）；count==0 → 强制 exhausted；"a proposal set of zero is only honest with its reasons" 是带指令性的修辞（要求记原因），产品说明书语气可接受 |
| 155-207 | Step 4（逐提案实现 + 机械 history IMPL 行） | CLEAN（边缘注明）。两段 `python3 -c` 为**无循环/分支/assert 的单函数顺序调用**（等价 `python <file>` 单行 operational 类，非契约 §4 禁的"循环·分支·assert 逻辑"）；"the single outcome row would lack round/change_sig — the dedup and the round advance read exactly those fields" 是操作性 why（解释两步 append 的必要性，指向盘面下游消费者），**无** po_implement 退役/计划轮号等考古；re-entry reconciliation 覆盖 crash-between-marker-and-row |
| 209-244 | Step 5（真 profiler 守卫 + 批量复测 + 修复环） | CLEAN（边缘注明）。条件参数组装 `$( [ -n ... ] && printf ... )` 是**单行命令组装**（非独立确定性逻辑块），属允许类；"GPU contention ... would corrupt it" 是守卫的操作性 why；删 verdict.json 在复测前显式钉死 |
| 246-264 | Step 6（emit） | CLEAN。十字段与 SPEC §2 po_propose schema required 逐一对应；失败路径同十字段 + 根因披露 |
| 266-271 | Validation | CLEAN。emit 时机械校验清单，含 exhausted ⇒ rationale 非空（D-V4-9 进 Validation ✅） |
| 273-277 | Output | CLEAN。FINAL = 单行 JSON，无前后文本 |

## 二、SPEC §4 po_propose 行逐条契约核对

| # | SPEC 要求 | agent.md 落点 | 结论 |
|---|---|---|---|
| 1 | Step 0 reuse：proposals.json 存在且可解析 → 跳过 Step 3 从 Step 4 续做（DONE marker 幂等）→ Step 5 照跑 | L83-88：resume at Step 4 + Step 5 as usual | ✅ 一致（agent.md 额外先跑 Step 1 幂等刷新 + Step 2 ledger refresh——兼容超集，不跳过任何 SPEC 钉死的步骤；同轮内 base 不变，stamp 复用成立） |
| 2 | stamp 键 = base 版本标识（best.vid / base onnx sha）+ 机械报告内容指纹（非轮号） | L102-105：best.json vid（无 best 则 base/model.onnx sha256）+ bottleneck_report.json sha256 | ✅ 逐字一致 |
| 3 | Step 3 机械闸过滤后 count==0 → exhausted 强制 true | L148-150 | ✅ |
| 4 | exhausted=true ⇒ exhausted_rationale 结构化非空（≥1 已尝试方向条目）进 Validation | L151-153（enforce mechanically, never accept a bare true）+ L270-271（Validation） | ✅ 两处齐 |
| 5 | 配额 4→3 显式 | L28（at most **3**）+ L139（at most 3）；无 "4" 字样（4→3 历史正确地留在 spec/commit，未进 prompt） | ✅ |
| 6 | Step 4 每提案机械补写 history IMPL 行（append_implemented + terminal-skip 两步 append + reconciliation） | L161-204：DONE→append_implemented(implemented=True)；terminal-skip→两步（implemented=False + append_outcome）+ reconciliation（DONE 无行补写） | ✅；`append_implemented`/`append_outcome` 签名与 `_po_scripts/history_lib.py` L104/L135 逐参吻合 |
| 7 | Step 5 run_latency_recheck 阈值实参 100/1/0.5 显式 + 打回后删 verdict.json | L228（--min-improvement 100 --min-pct 1 --min-ratio 0.5）+ L236-238（rm verdict.json，且钉明 verdict.json 存在性=skip key） | ✅；脚本侧（run_latency_recheck.sh L21-22/L42-45）自证同一语义 |
| 8 | 真 profiler 条件守卫：profile_script_path 非空 → Step 5 前置等基线 worker 退出（placeholder 空不等） | L211-217：poll train_final.json（terminal）或 finalizer.pid（dead），bounded-wait + 每 turn status message；空 input 不等 | ✅ 与 D-V4-7/R2-11 一致 |
| 9 | 三 subagent 失败矩阵（校验败/超配额/产物缺失 → 重派 1 次 → error 披露） | L58-64：(a)(b)(c) → 重派 1 次 → error 披露；配额超额 never re-dispatched → 终态 skip/淘汰 | ✅（超配额细化为不重派走终态——与草稿 §3.1 Step 5「仍不过淘汰」+ D-V4-4 R2-18 重派后降级披露同语义；spec 括号表述是粗概括，非冲突） |

附加核对：准入三闸（D-V4-8）节点侧落点 = L139-144（predict_delta 严格负 / edited_files ⊆ shadow / op_delta 非零整数[= op_delta⊕change_spec 一致的机械面，与 structure-proposer.md L41-42 自述的 strictly-negative admission gate 同构]）+ 去重对账 + count ≤3 + rationale 校验 ✅；emit 十字段与 SPEC §2 schema required 全集一致 ✅。

## 三、Findings 清单

### FINDING-1（轻微 · 契约 §4「确定性代码内联」）

- **位置**：`workflows/agents/po_propose/agent.md:33-40`（Resource Anchors 内的部署完整性检查）
- **问题**：7 行多行 bash——`for f in ...; do [ -f ... ] || { echo "FATAL..." >&2; exit 2; }; done`——循环 + 分支 + exit 的确定性逻辑内联在 prompt body。契约 §4 明列此类应抽到 `scripts/<name>.sh`、body 只留一行 `bash "$ORCA_AGENT_RESOURCES/scripts/<name>.sh"`；单行 operational 命令才豁免。执行 agent 每次重入都要重新逐 token 解析这段控制流，而它 100% 可机械化。
- **建议修法**：抽为 `po_propose/scripts/check_prerequisites.sh`（本节点 resources 自带，与 run_latency_recheck.sh 同目录——不依赖被检查对象先部署，无鸡生蛋问题），agent.md 该块收敛为一行调用 + 一句 fail-loud 语义说明。
- **非阻塞理由**：功能正确、fail loud、无残留；属形式违规（§4 类别命中），不影响本轮 E2E。

除 FINDING-1 外：**零 finding**。词表双扫（SPEC §6 词表 + 任务词表 + 补充词表）均 0 命中；无 run_verify / baseline_proxy_acc / baseline_ref / mfu_adapter / perturb_ckpt / playbook / ref-input / auto-trained / 懒补训 / epoch-only proxy 等已删机制残留；无 MNIST/CIFAR/具体 accuracy 值等测试夹具硬编码（唯一 input 引用是模板 `{{ inputs.profile_script_path }}`，夹具防火墙合规）；无 SPEC/ADR/issue 编号、迁移出处词、事故叙事。

初审判定（已被 §四 复验取代）：VERDICT: ISSUES (1)

## 四、复验（2026-08-26，commit `24eb711`）

**修复核对**（`git diff 2de195e..24eb711 -- workflows/agents/po_propose/`）：

- `agent.md:31-36`：原 7 行内联 for 循环（FINDING-1）收敛为契约 §4 标准形态——单行调用 `bash "$ORCA_AGENT_RESOURCES/scripts/check_prerequisites.sh"` + 一句指令式引导（"verify that on entry (fails loud when the entry stage is incomplete)"）。产品说明书语气 ✅。
- 新增 `workflows/agents/po_propose/scripts/check_prerequisites.sh`（21 行），逐项核对：
  - **功能零漂移**：检查清单与原内联完全一致（同 6 个共享脚本：analyze.py / predict_delta.py / history_lib.py / experiment_ledger.py / emit_result.py / check_bottleneck.py）；原 `cd "$ORCA_ARTIFACTS_DIR" || exit 2` 守卫移入脚本（`${ORCA_ARTIFACTS_DIR:?...}` env 未设 fail-loud + cd 失败 exit 2），且 body 顶部"cd before running any command"总指令（L23）仍在——无语义丢失。
  - **脚本洁净**：任务词表 + 补充词表（v3/v4/po_implement/po_verify/mnist/cifar/R2-/D-V4/懒补训/epoch-only 等）Grep **0 命中**；注释全部 operational（检查什么 / exit code 语义 / fail-loud 理由）；"(flatten Step 1 deploys ...)" 是 workflow 内部署机制的运行时事实交叉引用，非开发考古。脚本属契约 §9 惰性 code 资产类（豁免），按更严的 prose 标准衡量也通过。
  - **机械健全**：`bash -n` 通过；`set -euo pipefail`；exit 语义与头注释一致（0=全在场 / 1=env 未设 / 2=workspace 不可达或脚本缺失）；成功行走 stderr 不污染 stdout。
- **无连带改动**：`git diff 24eb711..HEAD -- workflows/agents/po_propose/` 为空——盘面即修复后状态；SPEC §4 九项契约语义未被触碰（修复仅迁移守卫位置，初审核对表继续有效）。
- 路径解析正确：`$ORCA_AGENT_RESOURCES/scripts/check_prerequisites.sh` = folder-agent resources（本 agent 目录）下 scripts/，与 run_latency_recheck.sh 同目录。

**FINDING-1 闭环确认：已修复。未解决项：无。**

VERDICT: CLEAN

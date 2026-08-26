# po_probe/agent.md 洁净审查记录（prof-opt v4）

- 审查对象：`D:\Projects\Orca\workflows\agents\po_probe\agent.md`（180 行）
- 判据：`orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`（受众分离 + §8 受众翻转通读）+ `docs/specs/prof-opt-v4-spec.md` §4 po_probe 行
- 方法：全文通读 → 契约方法论受众翻转 → 词表 grep → SPEC §4 逐条核对 → 跨文件佐证（probe_protocol.md / prof-opt.yaml / _po_scripts/ / po_contract）

## ① 逐段受众翻转结论表

假设我是只懂训练/评测业务、不懂 Orca 内部与 workflow 历史的执行 LLM，逐段读：

| 段（行号） | 内容 | 受众翻转结论 |
|---|---|---|
| frontmatter description（L1-4） | 节点职责产品说明书式一句话（守卫→同轮数渲染→外部停 k→深度 k 对比→晋升→轮末推进） | 可独立理解，纯 WHAT，产品说明书语气。**通过** |
| Your only task（L7-17） | 幸存者逐个：按基线完整轮数渲染、epoch k 外停、曲线深度 k 对比（可寻址时加第 k ckpt eval 双过）、落盘、轮末推进；"你只驱动已渲染模板与共享脚本，绝不手写训练/评测逻辑" | 每条可独立执行；"the learning-rate schedule plans over the full horizon" 是约束行为的领域理由（防 agent 擅自减轮数），非开发考古。**通过** |
| Execution model（L19-34） | detach + 有界轮询、pid 防二次 detach、probe_status.md 跨 turn 真相源、turn 顶满发含 `do not call orca next` 的 status message、齐全才发单行 JSON | 全部运行时可执行协议；"the host then leaves this node executing…" 是遵守该输出协议所需的最小机制说明，非事故复盘。**通过** |
| Resource Anchors（L36-47） | `$ORCA_ARTIFACTS_DIR` / `$ORCA_AGENT_RESOURCES` / `probe_protocol.md` 位置；`{{ inputs.accuracy_budget }}` 语义；预算唯一来源 = contracts.json（`full_train_budget` / `proxy_budget`），never from raw inputs | 契约 §5 允许的 operational 串；输入锚名与 yaml inputs（L34 `accuracy_budget`）一致；`proxy_budget` 确为 contracts 键（yaml L65"生效值记入 contracts.json 的 proxy_budget…下游一律读盘"）。**通过** |
| Path Handling Rules（L49-52） | pathlib/os.path 构路径，禁字符串拼接 | 可执行。**通过** |
| Subagent Call Protocol（L54-56） | "本节点零 subagent 派发" | 与 SPEC §3"probe 不派"一致。观察项：frontmatter tools 含 `task` 但节点声明不派——无害的基线工具清单，非洁净违规（见观察 2）。**通过** |
| Lazy Loading（L58-62） | Step 1 才读 probe_protocol.md；contracts/history/模板按协议指示读 | 可执行。**通过** |
| Iron rules 1-7（L64-94） | 脚本模板只读（heal 仅限重渲染参数）；禁二次 detach；GPU 串行守卫先行；at-least-once 幂等；fail loud 不造数；单幸存者不可证不失败节点；stdout 是数据非回复 | 每条可执行且互不冲突；所列四个模板均真实存在（po_contract 产 `run_probe_finetune` / `run_full_finetune` / `run_eval` / `export_onnx` template.sh，check_contracts.sh L257-316 校验在场与管线一致）。观察项：守卫措辞见观察 1。**通过** |
| Step 1: Derive state（L98-105） | 按协议 state derivation：守卫象限、轮号 R、幸存者（round R 末版 `outcome=="latency_pass"`）、各 stage、在飞 pid → 写 probe_status.md | 与 probe_protocol.md"State derivation"一一对应（协议含 stop_status 已出/组活续推分支）。**通过** |
| Step 2: stop-at-k train（L107-119） | 全轮数渲染→detach（wrapper 组长写 pid/rc）→有界轮询 `stop_at_epoch.sh`（间隔 ≤30s）到 stop_status.json 落地→按 stopped_at_epoch 抽曲线→`--at-epoch k` 对比 baseline_metrics.jsonl→`train.ckpt_per_epoch` 真时第 k ckpt eval vs baseline_k_acc 双过才 promote，eval 加载失败重派 1 次后降级曲线单判（`eval_failed: true` 披露）→history 行 + results 行；终态 = promoted / probe_insufficient；reconciliation 属重入 | 与 SPEC §4 po_probe 行逐条一致（详见下表）；细节参数由 probe_protocol.md 钉死（agent.md 显式委派且强制 Step 1 读协议）。**通过** |
| Step 3: Round-end advance（L121-133） | 全员终态后跑 advance_round.py，幂等键=轮号，best_updated/base_advanced 由其 JSON+marker 推出 | 可执行，与协议"Round-end advance"节一致。**通过** |
| Step 4: Emit（L135-155） | emit_result.py 九字段单行 JSON；healed_files 缺 marker 文件时为 `[]`（禁造）；workspace 级破坏 → 同字段集 status=failed，cause 进 assessment（schema 无 error 字段） | 九字段与 yaml po_probe output_schema required（L297：status/survivors_probed/promoted/best_updated/base_advanced/artifacts/assessment/max_retries_hit/healed_files）**逐一吻合**；healed_files 为单行 `python -c`，属契约 §4 允许的单行 operational 内联。**通过** |
| Validation（L157-162） | 仅 emit 时完备性校验（常驻节点、probe 结果无 fix-loop）：全员终态 + marker=当前轮 + 九字段齐 | 可执行。**通过** |
| Supervision points（L164-173） | 五条 fail loud 监督点（不造数、pid 先行、不绕守卫、不跳推进、在飞必发 status message） | 可执行。**通过** |
| Output（L175-180） | 完成=单行 JSON（前后零文本）；未完=status message（当前幸存者/stage、live pid、log 路径） | 可执行。**通过** |

## ② 词表 grep（命中即 finding）

对目标文件跑宽口径词表（`mnist_kd|playground|prof_opt_demo|run_verify|baseline_proxy_acc|baseline_ref|mfu_adapter|perturb_ckpt|playbook|ref-input|auto-trained|docs/specs|D:\Projects|/mnt/d|spec-review|SPEC-R1|ns3|psu|kd-nas|nas-supernet|prof-opt-design-draft`）：

**命中 0**。增补扫 `epoch-only|lazy|supplement|retrain|v3|v4|phase|SPEC|ADR`：仅命中合法运行时串（`proxy_budget` L47、"Lazy Loading" 标题 L58、协议四象限引用 L80/L100-101），无版本/phase/SPEC/ADR 考古、无 v4 已删机制措辞（run_verify / baseline_proxy_acc / baseline_ref / mfu_adapter / perturb_ckpt / playbook / ref-input / auto-trained / 懒补训 / epoch-only proxy 均零出现）。

## ③ SPEC §4 po_probe 行逐条核对

| SPEC 条款 | agent.md 落点 | 结论 |
|---|---|---|
| GPU 串行守卫 = finalizer.pid 四象限 | iron rule 3 + Step 1 象限，显式"Follow the protocol's four-quadrant guard exactly"；四象限全表（活→≤480s 有界等待+双期 30min 停滞 / 死+done→放行 / 死+failed→error 路由 / 死+缺失→fail loud）钉在 probe_protocol.md L34-46，agent.md 强制 Step 1 读协议 | 一致（委派成立，协议已钉死） |
| poll ≤30s 反复调 `stop_at_epoch.sh --stop-epoch k --contract` | L115 间隔 ≤30s + 调用到 stop_status.json 落地；全参数在协议 L108-117 | 一致 |
| State derivation 增"stop_status 未出且组活→继续调" | 协议 State derivation 第 4 条（pid 活组→poll、never re-launch）+ waiting 响应→再 poll | 一致 |
| extract `--expected-epochs`=stopped_at_epoch | L116 "curve extract at the recorded stopped_at_epoch" + 协议 L146-152 | 一致 |
| compare 恒 `--at-epoch k` | L116-117 显式 `--at-epoch k` vs baseline/baseline_metrics.jsonl | 一致 |
| 可寻址双过 promote；eval 失败重派 1 次降级 `eval_failed: true` | L117-119（BOTH pass；one re-dispatch→curve-only + `eval_failed: true` 披露） | 一致 |
| 不可寻址曲线单判 + `eval_skipped_no_epoch_ckpt: true` | 协议 L183-185 + history 行 L213 钉死；agent.md 描述层"(when checkpoints are addressable)"条件在场 | 一致（委派） |
| natural_done 且轮数>k → `monitor_failed: true` | 协议 L124-127 钉死；agent.md assessment 字段（L145）列 monitor_failed 披露 | 一致（委派） |
| probe 行 `proxy_acc` 恒曲线@k、eval 值置 `eval_acc` | 协议 history 行 L209-214（"always the curve value, never the eval value"） | 一致（委派） |
| 等待循环内 push_curves sidecar | 协议 L140-145（best-effort、`|| true`） | 一致（委派） |
| advance_round 继承 / 禁二次 detach / probe_status.md 继承 | Step 3 幂等推进；L24-25/L75-76/L167；L26-28/L105 | 一致 |

跨文件佐证：`proxy_budget` 为 contracts 真键（yaml L65、check_contracts.sh L177-210）；九字段 emit 与 yaml output_schema 完全一致；引用脚本（advance_round.py / emit_result.py / stop_at_epoch.sh / metric_curve.py / push_curves.py / render_run.sh / history_lib）全部存在于 `workflows/agents/_po_scripts/`；四个模板名与 po_contract 产出吻合。

## ④ Findings 清单

**零 finding。**

未计入 findings 的观察项（供作者裁量，不构成不通过）：

1. **iron rule 3 措辞略宽（L79）**："must be terminal (dead pid + `train_final.json`)" 字面含 dead+failed（failed 亦 terminal），而 SPEC 要求 dead+failed→error 路由 po_report、不放行。同条下一句"Follow the protocol's four-quadrant guard exactly"与 Step 1"act per the quadrant table"已把裁决权显式交给协议四象限表（协议表无歧义 error-route failed），执行 agent 无实际误启路径——故为可读性收紧建议非缺陷。可选修法：改为 "(dead pid + `train_final.json` status `done`)"。
2. **tools 含 `task`（L3）**：节点声明零 subagent 派发，`task` 为基线工具清单冗余项，无害；非洁净契约违规。
3. **Step 1 象限轴概述（L101）**"train_final present/absent" 折叠 done/failed 二态——由"act per the quadrant table"消解，同观察 1。

## 结论

受众翻转通读全部段落通过；词表零命中；SPEC §4 po_probe 行逐条一致（细节经显式、成立的委派钉在 probe_protocol.md）；语气全程产品说明书式（指令式、自包含、零历史负担）。

VERDICT: CLEAN

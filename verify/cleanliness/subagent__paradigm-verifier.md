# 洁净审查记录：`workflows/subagents/prof-opt/paradigm-verifier.md`

- 审查对象：`D:\Projects\Orca\workflows\subagents\prof-opt\paradigm-verifier.md`（96 行）
- 依据：`orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`（受众翻转通读法）+ `docs/specs/prof-opt-v4-spec.md` §1/§3/§4（v4 = 不变；contract→paradigm-verifier dispatch 保留；训练入口协议 = 同模板满轮渲染 + 外部 stop-at-k）+ 上游 caller `workflows/agents/po_contract/agent.md`（v4 重构节点）+ `docs/specs/prof-opt-spec.md`（v3.5 SPEC，判定入口协议变化）
- 词表 grep（mnist_kd / playground / prof_opt_demo / run_verify / baseline_proxy_acc / baseline_ref / mfu_adapter / perturb_ckpt / playbook / ref-input / auto-trained / docs/specs / D:\Projects / /mnt/d / spec-review / SPEC-R1 / ns3 / psu / kd-nas / nas-supernet / prof-opt-design-draft / 懒补训 / epoch-only，-i 大小写不敏感）：**0 命中**。

## ① 逐段受众翻转结论表

受众假设：我是运行时被 dispatch 的子代理，只懂业务（训练范式保真比对），不懂 Orca 内部、不懂本 workflow 开发历史，第一步全读本文件。

| 行 | 段 | 结论 |
|---|---|---|
| L1-5 | frontmatter（subagent / version: 1 / sentinel: PV8RK2） | CLEAN。三键规范；哨兵值在 L7 / L71-72 两处引用与 frontmatter 逐字一致（含 `v1`）。 |
| L7 | Output first line 指令 | CLEAN。与 caller 侧校验（po_contract agent.md L453 `head -n 1` 比对 `[subagent:paradigm-verifier v1 PV8RK2]`）逐字吻合。 |
| L9-16 | 角色定位（审查 Tier-B adapted entry 的范式保真；只判不修；caller 修一轮可重调一次） | **ISSUE（见 F-1）**：L12-13「contract switches (epochs / out-dir / step cap / data-subset limit)」中 step cap / data-subset limit 是 v3.5 旋钮词汇，v4 入口开关只剩 epochs / out-dir / seed（po_contract description L11-13 + Step 2）；且 v4 有 seed 该句未列。角色/只判不修/重调一次语义本身与 v4 Step 9（修一轮、二次 fail → Tier C）一致。 |
| L18-39 | Inputs（用户源码 scope + 越界上报；adapted entry 路径 = workspace `adapted/` 下 `train_proxy_entry.py` / `eval_entry.py`；allowed adaptations 默认表 + caller 可扩展；report 路径 `<workspace>/verify/paradigm_verifier_report_<train|eval>.md` 每入口一文件） | **PARTIAL（F-1 的主体落在 L28-32）**：输入 #1/#2/#4 与 v4 逐项吻合——`adapted/train_proxy_entry.py`（po_contract Step 2 item 6 L195）、`adapted/eval_entry.py`（Step 3 item 2 L242）、report 命名/一入口一文件（Step 9 L444-447）全部不变。输入 #3 的默认白名单 (a) 含 "step-or-batch cap / data-subset limit"、(b) 整条 "a proxy budget compression hook (stop after N steps/batches / a data subset)" 是 v3.5 入口内预算压缩机制残留，v4 已删（见 F-1）。 |
| L41-63 | Procedure 六项逐条比对（loss / optimizer / LR scheduler 含 step cadence / data flow / metric computation / eval entry 语义）+ 白名单外行为变化一律 flag | CLEAN。六项是通用范式保真清单，与 v4 契约逐项对得上（item 6 的 checkpoint container key ↔ v4 `eval.ckpt_container` bare/wrapper；item 5 的 "the metric extraction rule depends on" ↔ v4 `epoch_metric_extraction`）；"scope bleeding → report" 与 caller 传 scope 的机制一致。 |
| L65-89 | Output（报告落盘 `<report_path>`；首行哨兵；body = Verdict[pass/fail] + 逐项表 + Divergences[是否属 allowed 由 verifier 判非 port 自称] + Notes；返回值 = 哨兵 + 一行 verdict + 路径） | CLEAN。落盘权威、哨兵、四段 body、"divergence 是否属允许改造由你判断非 port 声称" 的裁决权归属明确，产品说明书式。 |
| L91-96 | Constraints（只读；唯一写 = 报告文件） | CLEAN。与 caller 分工（fix 由 po_contract 做）一致，fail loud 边界清楚。 |

## ② Findings 清单

**1 项 finding。**

### F-1（severity: low）默认允许改造白名单残留 v3.5「入口内预算压缩」机制措辞，与 v4 训练入口协议失配

- 位置：
  - `workflows/subagents/prof-opt/paradigm-verifier.md:12-13` —— 导语 "produced so that contract switches (epochs / out-dir / **step cap / data-subset limit**) can be passed in"
  - `workflows/subagents/prof-opt/paradigm-verifier.md:29` —— 默认白名单 (a) "new CLI switches for epochs / out-dir / seed / **step-or-batch cap / data-subset limit**"
  - `workflows/subagents/prof-opt/paradigm-verifier.md:30-32` —— 默认白名单 (b) "**a proxy budget compression hook (stop after N steps/batches / a data subset)** that leaves the per-step computation untouched"
- 问题：v3.5 SPEC（`docs/specs/prof-opt-spec.md` §4 po_contract 行）的入口协议确有截断/数据子集旋钮（「④截断 ⑥数据子集/限量旋钮发现（有则必用，无则退纯 epochs）」，probe 模板可含 `<<data_value>>`）——(b) 与 (a) 的对应措辞即该机制的 verifier 侧白名单。v4 已删此机制：`probe_cap_mechanism="stop-at-k"`（外部杀进程，probe 阶段施加，"never a template value"——po_contract agent.md L339-342）；`full_train_budget.data` 与 `proxy_budget.dataset_knob/data_value/max_steps` 恒 null（L395-397 / L408）；模板 "NO ... data/truncation token"；v4 Tier-B port 的合法改动只有 "(a) new CLI switches for the missing contract parameters (epochs / out-dir), (b) paths parameterized"（po_contract Step 2 item 6 L197-199）。失配后果：本文件的默认白名单是 verifier 的裁决依据且 caller "may extend"（只能放宽不能收窄），若 Tier-B port 真带 data-subset/step-cap hook，verifier 会判其属允许改造——恰放过 v4 公平不变量（基线/变体/winner 同模板同满轮渲染）最要拦的漂移。缓解（故 severity low）：happy path 下 caller 自身指令（"the ONLY changes are ..."）不会产出这种 hook，白名单残留项 dormant；且导语漏列的 seed 在 (a) 中已在，(c) path parameterization / (d) import adjustments 与 v4 不冲突。附带小失配：导语 (L12-13) 的开关列表漏 v4 在用的 seed。
- 建议（二选一，由 owner/spec 裁决，本审查不代改）：(1) 修文——(b) 整条删除；(a) 与 L12-13 导语收敛为 "epochs / out-dir / seed"（若保留 caller-可扩展语义不变）；注意 v4 spec §1 钉两文件「不变」，改文需回卷 spec（变更记入 v4 草稿附 A）再动。(2) 显式 waive——理由：operative 清单是 caller 每次传入的 Step 2 允许清单（po_contract Step 9 明确传 "the allowed-adaptations list from Step 2"），默认残留项无现实触发面；waive 记录落本文件即可。

Non-finding observations（不计入 findings，供作者参考，不要求修改）：

1. v4 新增一条合法 Tier-B 改造不在默认表内：po_contract Step 2 item 1 允许 "adapt the logging cadence in a Tier-B entry without changing training behavior"（无 per-epoch metric 项目）。因输入 #3 明定 "defaults; the caller may extend" 且 caller 实际传 Step 2 清单，机制上已覆盖——但与 F-1 合看，默认表「含 v4 已删项、缺 v4 新增项」的漂移方向一致，支持 F-1 的修文选项。
2. L13-14 "The port is only acceptable when the user's training paradigm is preserved verbatim" + item 2 对新增参数显式分组的要求，与洁净契约 §10「用户输入即权威」规则同向，无冲突。

## ③ 结论（初审，已被 ④ 复验取代）

该文件结构、哨兵协议、输入/输出契约、六项比对清单、只读约束均与 v4 caller（po_contract Step 9）逐项吻合且零开发期残留、零禁词命中；唯一问题是 v3.5 遗留的入口内预算压缩白名单措辞（step cap / data-subset limit / compression hook）在 v4 语境下成为失效引用，构成一条 low-severity 契约失配 finding（F-1），修文需经 spec 回卷或显式 waive。

VERDICT（初审）: ISSUES (1)

## ④ 复验（修复 commit `24eb711`，基线 `2de195e`）

- 复验范围：`git diff 2de195e..24eb711 -- workflows/subagents/prof-opt/paradigm-verifier.md`（仅两处 hunk——L12-13 导语 + L28-34 白名单段，其余段零改动 → ① 表对未改动段继续有效）；工作树与 `24eb711` 逐字一致（`git diff 24eb711 -- <file>` 为空，无未提交改动）。
- F-1 三处位置的闭环核对：
  - `:12-13` 导语 —— "step cap / data-subset limit" 已删，收敛为 "(epochs / out-dir)"，与 caller（po_contract agent.md description "epochs / out-dir for training; checkpoint path for evaluation"）的开关概括逐字同口径；v4 在用的 seed 由白名单 (a) 覆盖（导语为概括非穷举，初审"漏 seed"附带项随之消解——与 caller 自身措辞一致即无失配）。**已解决**。
  - `:29`（原 (a)）—— 收敛为 "new CLI switches for epochs / out-dir / seed"，恰为 v4 全部在用开关（po_contract Step 2 item 1 "①epochs ③out-dir, plus seed when supported"）。**已解决**。
  - `:30-32`（原 (b) compression hook）—— 整条删除；白名单重排为 (a) 开关 / (b) 路径参数化 / (c) workspace 内 import 调整（后两项即原 (c)(d)，原样保留，均 v4 合法）。**已解决**。
- 修法超出建议部分的核对：新增显式判据 "Everything else is a divergence — in particular any budget compression inside training (a step/batch cap, a data-subset limit) is a divergence: the variant stop depth is applied by an EXTERNAL stop at epoch k, never rendered into the training"——与 v4 语义（`probe_cap_mechanism="stop-at-k"` 外部施加、模板 "never a template value"、budget 旋钮恒 null）及 po_contract agent.md L339-342 措辞逐点同向。提及 step/batch cap / data-subset 是为了**禁止**而非允许，非已删机制残留；反而把初审指出的 false-pass 路径反转成显式 fail 触发条件，比初审建议的"仅删除"更强。措辞合规。
- 修复后全量复扫：词表 grep（-i，禁词表全量 + 附加探针 proxy/compression）——禁词表 **0 命中**；附加探针仅 2 处合法命中：L27 `train_proxy_entry.py`（v4 在用文件名本体，caller Step 2 item 6 / Step 3 item 2 逐字对应）、L32 上述禁止条款。新增句无残留引入（无 issue/节号/源码路径/迁移词/夹具硬编码；判据句为产品说明书式指令，非开发叙事）。
- caller 一致性：哨兵串 PV8RK2、`adapted/` 文件名、report 命名协议（`<workspace>/verify/paradigm_verifier_report_<train|eval>.md`）未动，与 po_contract Step 9 校验链继续逐字吻合。

**未解决项：无。** F-1（唯一 finding）已闭环，无 waiver 依赖。

## ⑤ 复验结论

修复将 v3.5 预算压缩白名单措辞全数移除并反转为显式 divergence 判据，白名单收敛为 v4 在用的 epochs/out-dir/seed + 路径参数化 + workspace 内 import 调整；文件其余部分零改动、禁词复扫零命中、与 v4 caller 契约逐项吻合。

VERDICT: CLEAN

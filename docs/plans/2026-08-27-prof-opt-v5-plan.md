# 实施计划 - prof-opt v5（时延先行顺序门控）

PLAN_STATUS: READY

> 契约来源：`docs/specs/prof-opt-v5-spec.md`（3 轮对抗评审 PASS，U1/U2/U3 已裁决回填）。本计划**逐字服务于该 SPEC**；SPEC 节号引用形如 `§n` 均指向它。
> 路径口径：按 SPEC §0 换算表的 **per-wf 新布局**书写（`workflows/prof-opt/workflow.yaml` / `workflows/prof-opt/agents/<name>/` / `workflows/prof-opt/agents/_po_scripts/` / `workflows/prof-opt/subagents/`）。当前仓库尚为平铺布局（目录迁移 loop 批 D 在途）——**Step 0 门槛断言不过不开工**。
> 环境：pytest / `tars` 走 WSL `.venv`（Git Bash → `wsl bash -c` 双层引号陷阱：复杂命令写临时 `.sh` 再执行；路径转换 `MSYS_NO_PATHCONV=1`）；不 push；commit 严格限本清单。

---

## 1. 目标与范围

**目标**：把 prof-opt 从 v4「双门槛晋升 + stall/exhausted 早退」重构为 v5「时延链式推进 → 达标后粗训精度门 → 恢复轮（底座固定 + 组合式提案）→ 双达标 full-train；100 轮唯一硬帽」，同时落地 origin 双锚、round_state 单一来源、规则双层池、profiling 模式自动解析、部署件版本戳。验收 = SPEC §11.1 单测 + §11.2 smoke + §11.3 validate/洁净。

**非目标**（SPEC §0，逐条不做）：wall-clock 帽 / 平台早退；DAG 结构 / 节点数 / 回边 / 路由语句变更；引擎层改动；规则跨用户共享；真机 in-session E2E（§11.4 归属用户 NPU 服务器，不进本计划验收）。

**开工门槛（fail loud，Step 0）**：`workflows/prof-opt/workflow.yaml` 存在 且 `workflows/prof-opt.yaml` 不存在；`tests/test_po_scripts.py` 顶部 `_SCRIPTS` 常量已指向 `workflows/prof-opt/agents/_po_scripts`（迁移批 D 承诺更新，实测锚点原 :50-52）。任一不满足 → `IMPL_STATUS=BLOCKED` 上报，禁止自行猜布局。

---

## 2. 契约影响面

| SPEC 契约项 | 计划落点（文件 / 步骤） |
|---|---|
| §1 inputs 契约 v5（8 输入逐字替换 + 退役 6 输入 + grep 验收） | S5-C5 `workflow.yaml`；grep 验收落 S6 `test_po_v5.py` inputs 域 + input-pin 测试重写 |
| §2.1 resolve_profile_mode.sh（env → npu-smi → fallback，防伪命中） | S1-C3 新增 `agents/_po_scripts/resolve_profile_mode.sh` |
| §2.2 消费者改读盘（baseline profile 调用 / propose mfu guard / recheck / reuse_check） | S1-C4 `run_baseline_chain.sh`、`run_latency_recheck.sh`、`reuse_check.sh`；S2-C5 `po_baseline/agent.md`、`po_propose/agent.md` |
| §2.3 复用一致性（重解析比对测量配置四字段 {mode, chip, precision, core_num}，漂移/缺失 exit 2——errata 已回填 SPEC 2026-08-27） | S1-C4 `reuse_check.sh`（npu 校验段删除、模式一致性校验落位复用路径段，见 §3.1 与 §8-P9） |
| §3.1 origin 锚 schema / write-if-absent / 量程校验 / 不带 flag 绝不触碰 | S1-C1 `analyze.py`（`--freeze-origin --latency-reduction-min --accuracy-budget`） |
| §3.2 锚消费者全只读、缺失 exit 2 | C1 `round_state.py`、C2 `verdict_decide.py`/`advance_round.py`/`gate_decide.py`、S2-C5 `po_probe/agent.md`（粗门预算）、`po_report/agent.md`（基线块） |
| §4 round_state.py current/working/mode 三子命令 | S1-C1 新增 `agents/_po_scripts/round_state.py`；消费者改造 C2（gate/advance）+ S2-C5（propose Step0 / probe Step0） |
| §5.1 outcome 枚举（advanced 新 builder / promoted 读兼容 / permanent 集） | S1-C1 `history_lib.py` |
| §5.2 probe 行 gap 字段（双门最差） | S1-C1 `history_lib.py`；C2 `verdict_decide.py` 产出 gap；S2-C5 `probe_protocol.md` 写行协议 |
| §5.3 verdict_decide 两子命令 --budget 移除改读锚 / accuracy_pass+gap 输出 | S1-C2 `verdict_decide.py`；S2-C5 `full_train_protocol.md`、`probe_protocol.md` 调用行同步 |
| §6.1 advance 双模判据 / 共同动作 / best.json v5 schema | S1-C2 `advance_round.py` |
| §6.2 marker (round, mode) 幂等键 + direction.json + failed_sigs | S1-C2 `advance_round.py` |
| §6.3 _rank_key 方向归一 tie-break | S1-C2 `advance_round.py` |
| §7.1 gate 决策序（任意版本行 accuracy_pass）+ 不变量校验 | S1-C2 `gate_decide.py` |
| §7.2 gate CLI 简化 + gate_node.sh 先对戳 + yaml command | S1-C3 `gate_node.sh`（verify 接线与 deploy --verify 同 commit，N4）；S2-C5 `workflow.yaml` po_gate command |
| §8.1 flatten：模式落盘 / REUSE 重部署 / 规则回种（仅 fresh）/ agent.md 改写 | S1-C3（resolve/seed 供依赖）+ S2-C5 `po_flatten/agent.md`；C4 `reuse_check.sh` |
| §8.2 baseline：freeze-origin 调用 + profile 三参改读盘 | S2-C5 `po_baseline/agent.md`；S1-C4 `run_baseline_chain.sh` |
| §8.3 propose：Step0/Step3 增补/Step5 判定/Step6 机械推进/output 增量/check_prerequisites | S2-C5 `po_propose/agent.md`；S1-C4 `run_latency_recheck.sh`、`check_prerequisites.sh`；S2-C5 `workflow.yaml` output_schema |
| §8.4 probe：mode 分派 / 训练集规则 / accuracy-analyst dispatch / output 更名 | S2-C5 `po_probe/agent.md` + `references/probe_protocol.md` + `workflow.yaml` output_schema |
| §8.5 规则双层池（accuracy-analyst + rules_pool check/seed/merge + schema） | S1-C3 `rules_pool.py`；S3-C5 `subagents/accuracy-analyst.md`（新增）；S2-C5 flatten/report/propose/probe/full_train agent.md 消费点 |
| §8.6 report：origin 锚基线块 / zero_improvement_rounds / 模式+版本戳披露 / rules merge / write_back+report_dir 常量 | S2-C5 `po_report/agent.md` + `references/report_format.md`（§13 披露补触，见 §3 注 b） |
| §8.7 full_train 读锚 + 终局 analyst 提取；contract probe_epochs 移除 | S2-C5 `po_full_train/agent.md` + `references/full_train_protocol.md`（注 b）、`po_contract/agent.md`、`workflow.yaml` proxy_budget 描述 |
| §9 部署件版本戳（manifest / --verify / 入口重部署 / 三消费点） | S1-C3 `deploy_scripts.sh` + `gate_node.sh`（消费点）；S2-C5 flatten/propose/probe agent.md（消费点 + REUSE 重部署） |
| §10 workflow.yaml 变更汇总（description / inputs / 注释与 schema 增量 / gate command / outputs 不变 / 洁净） | S2-C5 `workflow.yaml`；洁净见 S6 |
| §11.1 十域单测 + 既有用例改造 | S1 各 commit + S6（映射见 §5） |
| §11.2 smoke 五步 | S6 `test_po_v5.py` fixture 工作区 |
| §11.3 validate 零 error + 洁净 warning 清零 + pytest 全绿 | S6 |
| §12 fail loud 矩阵 | 测试映射见 §5.4 |
| §13 文件触达清单 | §3（含注 a/b 三文件披露补触） |
| §14 遗留（远端根因核对 / 跨机共享 / 棘轮收紧） | 不进实现——§11.4 真机清单 + SPEC 变更流程 |

---

## 3. 文件级改动清单

路径全部为新布局。动作：M=modify，N=new。SPEC §13 之外的补触文件显式标注并给依据——**不扩契约，只让规范性章节有处落地的文件载体**。

### 3.1 `workflows/prof-opt/` 内（注：当前平铺路径 `workflows/prof-opt.yaml`、`workflows/agents/po_*`、`workflows/agents/_po_scripts`、`workflows/subagents/prof-opt`，批 D 后机械换位，内容基线已读）

| 文件 | 动作 | 改什么 | 为什么 |
|---|---|---|---|
| `workflows/prof-opt/workflow.yaml` | M | inputs 块整体替换为 §1 的 8 输入（逐字）；顶部 description 换顺序门控句 + 规则沉淀句（§10.1）；po_propose/po_probe/po_gate 节点注释块重写 + output_schema 增量（§8.3/8.4/10.3；po_probe 的 `base_advanced`/`best_updated` 字段描述随直通轮口径 §8-P4 微调——v4 描述称推进归属 probe，v5 latency 态归属 propose）；po_gate command 只传 `--max-rounds`（§7.2）；po_contract proxy_budget 描述「自动推定，不可覆盖」（§8.7）；outputs 块不动 | §1/§7/§8/§10 |
| `agents/po_flatten/agent.md` | M | Required Inputs 去 npu 三参（保留 fresh_start/seed，新增 ORCA_PO_NPU_* 环境变量说明）；Step 0 调用去 npu 实参、出口映射更新（模式漂移=exit 2 硬错）；REUSE 分支出口前 `deploy_scripts.sh` 幂等重部署 + 保留既有规则不重种（§8.1/U3/U2）；fresh 路径部署后调 `resolve_profile_mode.sh` 落盘、Step 3 后调 `rules_pool.py seed`（仅工作区无 accuracy_rules.json 时） | §2/§8.1 |
| `agents/po_flatten/scripts/reuse_check.sh` | M | npu 三参校验段（现 :94-121）**删除**；新增模式一致性校验，**落位钉死：BASELINE.lock 匹配通过之后、reuse products 段（现 :231-）之前**——即仅复用路径可达（fresh_start wipe 短路与「无 lock → NO_REUSE」均在它之前退出，首跑与 fresh_start 恢复不受校验影响，对抗轮 1 Q1）。校验：以 `resolve_profile_mode.sh --stdout-only` 只读重解析一次，与既有 `profile_mode.json` 比对（**比对过程绝不触碰既有文件**，Q2）；**比对集钉死 = 测量配置四字段 {mode, chip, precision, core_num}，`resolved_by` 仅溯源不入集（N1：同硬件 env→npu-smi 来源翻转是测量等价配置，按「逐字段」字面比对会伪漂移 exit 2 强制 wipe 数十轮进度——违反 §2.3 立法意图「模式变化使 cycles 对比失效」；SPEC §2.3 已回填 errata（2026-08-27），四字段比对集为契约明文，计划读法与 SPEC 一致化，见 §8-P9）**；比对集内任一不一致或文件缺失 → exit 2（文案含跨 run cycles 对比失效 + `fresh_start=true` 指引）；一致 → 原样保留继续 products 段。用法签名去第 4 参，多余位置参 → usage 错误 exit 2（对齐脚本族 fail-loud 风格） | §2.3/§8.1 |
| `agents/po_baseline/agent.md` | M | Step 1 链调用去 npu 三参；每次链调用后机械检测「`base/profile/profile_summary.json` 存在 且 `base/origin_anchor.json` 不存在」→ 执行 `analyze.py --profile-dir base/profile --freeze-origin --latency-reduction-min {{ inputs.latency_reduction_min }} --accuracy-budget {{ inputs.accuracy_budget }}`（exit 2 → node failed，文案含锚不可变 + fresh_start 指引）；mfu-analyzer dispatch 的 chip/precision/core_num 改从 profile_mode.json 读值代入 | §3.1/§8.2/§2.2 |
| `agents/po_baseline/scripts/run_baseline_chain.sh` | M（注 a） | CLI 去 `--npu-chip/--npu-precision/--npu-core-num`；模式/分支/枚举校验改读 `$ART/profile_mode.json`（缺失或 mode 非法 → exit 2 fail loud）；awaiting-analyzer 提示与末行日志的 mode 来源同步 | §2.2（「po_baseline 的 profile 调用…全部改为读 profile_mode.json 字段」的机械载体在链内） |
| `agents/po_propose/agent.md` | M | Step0：R 与路径经 `round_state.py working` + `deploy_scripts.sh --verify` 对戳（不符 fail loud 披露版本戳）；Step3 dispatch 输入增补（accuracy_rules.json 全文 / failed_sigs 并集换路指令 / accuracy 恢复轮上下文：底座固定声明 + gap 数值 + `makespan ≤ target_cycles` 硬约束 + 组合式提案语义）；exhausted 语义改恒 false；Step5 复测判定 v5（参数族退役，判定 = latency 态严格 < incumbent / accuracy 态 ≤ target，mode 经 round_state，incumbent=best.json 或 origin 锚）；新增 Step6 机械推进（latency → `advance_round.py`；accuracy → 不跑，归 probe；含崩溃边界说明）；原 Step6 Emit 顺延 Step7 并增 `mode`/`advanced_vid` 字段；mfu guard 与 per-variant mfu dispatch 改读 profile_mode.json | §8.3/§2.2/§9 |
| `agents/po_propose/references/structural-levers.md` | 不动（实测 grep 无穷尽语义引用；实现时复验一次，若批 D 后内容出现穷尽表述则按 §13 条目改） | — | §13 条件项裁决 |
| `agents/po_propose/scripts/run_latency_recheck.sh` | M | `--min-improvement/--min-pct/--min-ratio` 参数与 `predicted_delta >= 0` 守卫退役（§8.3 Step5）；判定改双模：mode 经 round_state，latency 态 `latency_pass ⇔ makespan < incumbent`（严格），accuracy 态 `⇔ makespan ≤ target_cycles`；incumbent = best.json makespan 否则 origin 锚 `baseline_makespan_cycles`；`pred_actual_ratio` 降级信息字段（有则记录）；profiling 模式分支（inline vs pre-profiled）改读 profile_mode.json（`--pre-profiled` 旗标退役）；verdict.json 的 gate 数学段同步（required_improvement 等字段随参数族退役调整） | §8.3/§2.2 |
| `agents/po_propose/scripts/check_prerequisites.sh` | M | 前置清单加入 `round_state.py` / `resolve_profile_mode.sh` / `rules_pool.py` | §8.3 |
| `agents/po_probe/agent.md` | M | **节点入口先跑 `deploy_scripts.sh --verify`（mode 分派之前——latency 直通轮也验戳，消解 §9「Step1 验戳」与 §8.4「Step0 直通」的可达性矛盾，Q9）**；Step0 mode 分派（`round_state.py mode` 于 probe 入口时点求值；latency → 直通 emit：`survivors_probed=0`、assessment 注明 passthrough、不等 GPU 守卫；accuracy → 粗训门）；**直通轮输出字段口径（SPEC 未定，微披露见 §8-P4）：`advanced_vid`/`best_updated`/`base_advanced` 盘面重导出——marker.round == current 且 mode == latency 时取该 marker + direction.json（推进发生在 propose Step6，直通轮如实反映）；marker 陈旧/缺失（如 Step6 崩溃顺延）→ 零推进填报，不读旧轮值**；训练集规则（best.vid 无任何 probe 行 → 首入只训 best.vid；已有 → 恢复轮训本轮全部 latency_pass 幸存者，排除终态 probe 行 vid，幸存者轮 = round_state current）；每轮判定完成后 dispatch `accuracy-analyst`（输入 = 本轮 probe 行 + 血缘 sig + 现有规则；返回后 `rules_pool.py check`，失败重派 1 次，再失败剔除坏行继续 + 披露）；全员判定后跑 `advance_round.py`（accuracy 态判据）；Step4 emit 字段更名/新增（mode / accuracy_pass_vids / advanced_vid）；「dispatches no subagents」段改为 dispatch accuracy-analyst | §8.4/§8.5/§9 |
| `agents/po_probe/references/probe_protocol.md` | M | 改写：mode 分派 / 训练集规则（机械判据，弃时序推断）/ accuracy 态 advance 时机 / accuracy-analyst dispatch 协议 / verdict promote 调用去 `--budget`（读锚）/ probe 行 outcome 新枚举（accuracy_pass/accuracy_fail/probe_insufficient）+ gap 字段 + eval 降级披露 | §8.4/§5.2/§5.3 |
| `agents/po_report/agent.md` | M | 基线块读 origin_anchor；读全部 direction.json 统计 `zero_improvement_rounds`（informational，进 reason/assessment，不改 output_schema）；读 proposals.json exhausted 与 accuracy_rules.json 作素材；报告首段披露 profile_mode.json 全文 + scripts `.VERSION` 戳；终态调 `rules_pool.py merge` + 人类可读镜像（成功失败皆合并）；`{{ inputs.write_back }}`/`{{ inputs.report_dir }}` 消费点改常量 true / `docs/prof-opt` | §8.6 |
| `agents/po_report/references/report_format.md` | M（注 b） | builder 规格同步：基线块字段来源 origin 锚；zero_improvement_rounds 统计段（**口径钉死：以 history 的 `advanced` 行为准——「有 direction.json 的轮中无任何 round==R 的 advanced 行」的轮数（§8-P5）；禁止单读 direction.json 计数，因 §6.2 同轮后写覆盖先写会把「latency 推进 + accuracy no-op」轮误记为零改进（smoke 步 3 即反例，Q10）**）；首段披露段；终态 merge 步骤（含项目镜像 `docs/prof-opt/accuracy_rules.json` + 人类可读 `accuracy_rules.md`）；write-back 条件段「`<write-back>` input is true」改常量 true；report 落档目录改常量 | §8.6（report_format.md 是 builder 的运行时规格，agent.md 明示「follow it exactly」——不改它则 §8.6 无处落地） |
| `agents/po_full_train/agent.md` | M | `{{ inputs.accuracy_budget }}` 消费点改读 origin 锚（verdict final-budget 已改读锚，prompt 同步）；终局判定后 dispatch accuracy-analyst 提取最后一轮规则（within_budget 与否都提取；check 失败重派 1 次再披露；随后 po_report 统一 merge）；「dispatches no subagents」段改写 | §8.7/§8.5 |
| `agents/po_full_train/references/full_train_protocol.md` | M（注 b） | `verdict_decide.py final-budget --budget "<accuracy-budget>"` 调用行去 `--budget`（读锚）；补终局 accuracy-analyst 提取步骤 | §5.3/§8.7 |
| `agents/po_contract/agent.md` | M | `{{ inputs.probe_epochs }}` 消费点移除（Resource Anchors 段 + Step 7 proxy_budget：k 恒 `min(1, full_train_budget.epochs)` 机械推定） | §8.7 |
| `agents/_po_scripts/history_lib.py` | M | `PERMANENT_OUTCOMES = {"advanced", "promoted", "unsupported_op"}`（promoted 读兼容，v5 不再写入）；新 builder `append_advanced(path, vid)`（只写 `outcome:"advanced"`，字段集=LATENCY_FIELDS）；`PROBE_FIELDS` 增 `gap`，`append_probe(..., gap=None)` None 省略；docstring 同步（v5 outcome 枚举表） | §5.1/§5.2 |
| `agents/_po_scripts/round_state.py` | N | 三子命令 current/working/mode（§4 语义逐字：纯数字目录 max / %03d 零填充 / `.round_advanced` 联动 / best≤target 双态推断）；stdout 单行 JSON；bad input exit 2；mode 子命令 origin 锚缺失 exit 2 | §4 |
| `agents/_po_scripts/analyze.py` | M | 新参 `--freeze-origin --latency-reduction-min <f> --accuracy-budget <f>`：量程校验（r∈(0,1)、budget≥0，非法 exit 2）；写 `<profile-dir>/../origin_anchor.json`（write-if-absent；已存在逐字段一致 no-op、不一致 exit 2 含「origin 锚不可变；修改达标线/预算需 fresh_start」）；`target_cycles = int(base×(1−r))+1`；`frozen_at_round: 0`；不带 freeze 参数绝不触碰该文件 | §3.1 |
| `agents/_po_scripts/verdict_decide.py` | M | promote/final-budget 两子命令 `--budget` 移除，budget 改读 `base/origin_anchor.json`（缺失 exit 2）；promote 输出 `{"curve_pass","eval_acc","eval_pass","line","accuracy_pass":<bool>,"gap":<float>}`（v4 `promoted` 退役）；gap=双门最差（higher_better=anchor−value / lower_better=value−anchor 取 max；eval 缺失降级 curve-only 时 gap=曲线缺口）；方向归一 / slack=1.0×budget / fail loud 矩阵逐字保留 | §5.3/§5.2/§3.2 |
| `agents/_po_scripts/advance_round.py` | M | 双模判据（§6.1：latency 候选=本轮 latest `latency_pass` 且 makespan 严格<incumbent，winner=makespan 最小平手 vid 序；accuracy 候选=本轮 latest `accuracy_pass` 且 makespan≤target，winner=gap 最小平手 makespan 再 vid 序；incumbent=best.json 否则 origin 锚）；共同动作仅真实推进时执行（best.json→onnx/profile/shadow 复制→`append_advanced`→marker 最后）；winner==incumbent 或无候选仅写 marker（`vid=null, improved=false`）；marker 幂等键=(round, mode) 二元组，陈旧 marker 按当前 mode 重放收敛；best.json v5 写入规则（latency 态 proxy_acc=null / accuracy 态写 winner proxy_acc）；每次 advance 写 `rounds/<RRR>/direction.json`（failed_sigs=本轮 latency_fail 与 accuracy_fail latest 行 sig 机械枚举）；`_rank_key` tie-break 方向归一（metric_direction 读 contracts.json，未知退 vid 序 + stderr 披露）；origin 锚缺失 exit 2。**崩溃收敛实现注记（设计权衡，轮 1 Q6 收紧 + 轮 2 N2 扩展）**：两条撕裂判据——(甲) marker 缺失且 best.json.vid==winner 且该 vid **无本轮 `advanced` 行** ⇒ 撕裂在途，补齐复制 + append_advanced 后落 marker（覆盖 winner 重算命中的撕裂，含 accuracy 态撕裂——accuracy 候选含 winner 自身必命中）；(乙) **round_state mode == latency 且 best.json.round == current 且 marker 无 (current, latency) 记录且 best.json.vid 无本轮 advanced 行** ⇒ latency 态撕裂且候选被撕裂写压制（incumbent 已被改成 winner 自身 → 严格改进判据无候选、判据甲永不触发，轮 2 N2 场景），按 best.json.vid 补齐动作并写 marker (current, latency, improved=true)。良性首入（同轮先 latency 推进过的 best.vid 自身过精度门）其 vid **已有本轮 latency 态 advanced 行** → 两判据皆不触发 → marker-only。词形统一钉死为「**本轮** advanced 行」；判据乙的 mode 门控排除 accuracy 轮误判（accuracy 轮无 latency 推进、其撕裂由判据甲经 winner 重算覆盖）。**残余披露（N2 如实列示，不静默）**：latency 撕裂写的 winner 已达线（mode 翻 accuracy）且首入 accuracy_fail → base/ 与 best.json 分叉携带至下轮（恢复轮提案围绕陈旧瓶颈，质量级损失非正确性损失——新 winner 仍以自身实测 makespan ≤ target 收敛，rR-01 的特定改动血缘可能丢失）。选此读法 why：备选一「撕裂即 exit 2」把可机械收敛的窗口升级为节点失败，违反 §6.2「重放收敛」明文；备选二 best.json.round 判据不加 mode 门控会把 accuracy 轮撕裂误完成为伪造 latency 推进（消耗语义）；v4 `advance_round.py` 文档 :10-14 记载的正是不收敛时 best.json 与 base/ 指向不同轮的事故模式。守护用例须**三分支绑定**（判据甲撕裂补齐 / 判据乙压制候选补齐 / 良性首入 marker-only 不补复制）。该注记原为对 §6.1 字面在崩溃窗口的例外扩展（依据 §6.2 收敛要求）——**SPEC §6.1 已回填 errata（2026-08-27，撕裂恢复读法入契约），计划读法与 SPEC 一致化（§8-P7）** | §6/§6.2 |
| `agents/_po_scripts/gate_decide.py` | M | 决策序 v5（§7.1 逐字：① best 存在且 ≤target 且 best.vid 在 history 任意版本行有 `accuracy_pass` → full-train；② round≥max_rounds → best-effort/finish-failed；③ 其余 loop）；决策前不变量校验（mode=accuracy 但 best.vid 无任何 probe 行 → exit 2）；`--latency-reduction-min`/`--stall-rounds` 移除（argparse 拒绝未知参）；不再读 proposals.json/exhausted/stall；target 读 origin 锚（缺失 exit 2）；输出 `{decision, round, mode, best, target_cycles, reason}` | §7.1/§7.2 |
| `agents/_po_scripts/gate_node.sh` | M | 参数只留 `--max-rounds`；决策前先 `deploy_scripts.sh --verify`，失败按既有 fail 分支出 finish-failed 且 reason 披露版本戳不符；fail 分支 emit 字段对齐 v5 输出（stall 出、mode/target_cycles 入——SPEC 未规定 fail 分支字段集，此为机械补定，见 §8 微披露） | §7.2/§9 |
| `agents/_po_scripts/deploy_scripts.sh` | M | 复制完成后计算 manifest（部署到 `scripts/` 的全部 `*.py`/`*.sh` 按「文件名排序 → (name, sha256(content))」序列做 sha256），写 `scripts/.VERSION`（单行 `{"manifest":"<sha256>"}`，.VERSION 自身不入集）；新增 `--verify` 模式：重算当前部署集 manifest 与 .VERSION 比对，缺失/不符 exit 1 + stderr 指明 | §9 |
| `agents/_po_scripts/resolve_profile_mode.sh` | N | §2.1 优先级（env `ORCA_PO_NPU_CHIP` 非空→mfu+枚举校验 exit 2 → `npu-smi` 型号字段解析（禁止裸子串匹配整个输出，"1951 MB" 类伪命中不识别→exit 2）→ fallback placeholder）；precision/cores 同规则（默认 INT8/1，枚举 INT8|INT16|AMP / 1|2|4）；默认输出单行 JSON（经 emit_result.py）+ 写 `$ORCA_ARTIFACTS_DIR/profile_mode.json`（placeholder 模式 chip=""/precision=null/core_num=null）；**`--stdout-only` 只读模式：仅解析输出、不落盘**（reuse_check 复用比对专用，防「先覆写再比对」恒等假绿，Q2） | §2.1 |
| `agents/_po_scripts/rules_pool.py` | N | `check`（schema 校验：全字段必含/change_pattern 去重/direction·generality·confidence 枚举/metric_gap 有限数；违规行 fail loud 报行号；容忍 SPEC 规定的 `borrowed` 注记字段——必含字段校验，非闭包 schema）；池条目 schema `$ORCA_HOME/prof-opt/accuracy_rules_pool.json`（confirm/refute=model_hash 集合，general⇔|confirm|≥2、quarantined⇔|refute|≥2，天然幂等；池条目无 metric_gap 字段——见来源 3/4 物化补定）；`seed`（**脚本级守卫（Q11）：工作区 accuracy_rules.json 已存在 → 拒绝重种 exit 2 + stderr 披露——§8.1 的「仅无该文件时执行」是 prompt 层第一道闸，机械守卫钉在脚本内（12-Rule 5）**；四来源优先级合成 + 同 change_pattern 冲突项目实测优先 + 来源 4 降档下限 low + `pool-` 前缀 + `borrowed: true`；**来源 3（general:true）物化补定（SPEC 仅定义来源 4 的映射，微披露见 §8-P6）：evidence_rounds/vids 取池记录并集、id 加 `pool-` 前缀、metric_gap=0.0 且 statement 追加「(general pool entry: no local measured gap)」披露句（措辞避开 borrowed 一词，与来源 4 专用字段区分，N3）；与来源 4 差异 = 不降档、不标 `borrowed: true`（general 态已是 ≥2 模型跨模型实证）；池源条目（pool- 前缀）不参与 gap>3×budget→high 的 confidence 阶梯（防 0.0 被读成实测零缺口，N3 防线——见 accuracy-analyst 行同款约束）**；model_hash 配方=BASELINE.lock py_files_sha256 排序序列 sha256 单值，lock 未写退原始 shadow 闭包直算，禁对已推进 shadow 直算；池/镜像缺失→best-effort 空源，坏行剔除+披露）；`merge`（镜像全文覆盖 + 池按 (change_pattern, direction) 并 evidence + confirm/refute 集合维护 + general/quarantined 重算 + 失败 best-effort 披露） | §8.5 |
| `subagents/prof-opt/structure-proposer.md` | M | 删除声明穷尽的出路（exhausted 语义改恒 false 由节点侧机械写）；字段族：`accuracy_risk`→`predicted_acc_impact`（low/medium/high + 一句理由），`expected_accuracy_impact`/`accuracy_confidence` 退役，`accuracy_evidence` 保留承接理由；输入增 accuracy_rules.json 全文 + 换路指令段（failed_sigs 并集 + 不得同族 + 穷尽感→更深重写/不同算子族）；accuracy 恢复轮组合式提案段（可叠加历史有效 / 回退历史有害组件（沿血缘链部分还原）/ 可提名 KD 型；新 change_sig 不受历史失败 sig dedup 拦截是恢复策略本身）；「尽可能保证精度，不为一味降时延牺牲精度」判断责任声明 | §8.3/§13 |
| `subagents/prof-opt/accuracy-analyst.md` | N | 新子代理：frontmatter sentinel（格式对齐 `subagent/version/sentinel`，6 字符风格，值唯一即可）；从实测（probe 行 + 血缘 change_sig）提取/更新规则，绝不凭空预置模型论先验；规则 schema 全字段必含 + 同 change_pattern 合并证据 + confidence 阶梯（单轮 low / 2 轮一致 medium / ≥3 轮或 gap>3×budget high；**gap 阶梯仅对本地实测条目生效，池源条目（pool- 前缀）按 evidence 计数阶梯——防 metric_gap=0.0 伪零缺口，N3**）+ direction/generality 打标语义 | §8.5 |

### 3.2 测试与文档

| 文件 | 动作 | 改什么 | 为什么 |
|---|---|---|---|
| `tests/test_po_scripts.py` | M | 既有用例改造（清单见 §5.3）；`_SCRIPTS` 路径已由批 D 换新（Step 0 断言核实） | §11.1 既有改造 |
| `tests/test_po_v5.py` | N | §11.1 十域新用例 + §11.2 smoke 五步 + §1 grep 验收 | §11.1/§11.2 |
| `docs/status/CURRENT.md` | M | v5 段 Phase 3 收口（只动 v5 段，不碰迁移 loop 段） | 项目状态文档规则 |
| `docs/status/CHANGELOG.md` + `docs/releases/` | M/N | 索引 + release note（SDD loop 收口时） | 同上 |

**注 a（§13 披露补触 1）**：`run_baseline_chain.sh` 不在 SPEC §13，但 §2.2 明文「po_baseline 的 profile 调用（现 `--npu-chip/--npu-precision/--npu-core-num "{{ inputs.* }}"`）…全部改为读 profile_mode.json 字段」+「profile_mode.json 缺失或 mode 非法 → 消费节点 fail loud」——profile 调用与模式分支物理上住在链脚本内（现 :109-140/:273/:293/:615），inputs 退役后 agent.md 无法再传三参，链不改则契约无处落地。判为 §13 枚举遗漏（规范性章节 §2.2 完整自洽），计划补触并在此披露，不视为私改契约。
**注 b（§13 披露补触 2）**：`report_format.md` / `full_train_protocol.md` 同理——§8.6 的 write-back 条件句（report_format.md §3 现文「ONLY when … `<write-back>` input is true」引用已退役 input）与 §5.3/§8.7 的 `final-budget --budget` 调用行（full_train_protocol.md :159-161）都在这两个 prompt-adjacent references 内；§13 已把同类的 `probe_protocol.md`/`structural-levers.md` 列入触达清单，遗漏这两处与该模式不一致。补触披露同注 a。
**显式不动**（防 coder 顺手改）：`agents/po_flatten/scripts/check_flatten.sh`、`agents/po_flatten/scripts/extract_user_pkg.sh`、`agents/po_contract/scripts/check_contracts.sh`、`agents/po_baseline/scripts/check_business_logic.sh`、`_po_scripts/` 其余 13 个脚本（emit_result/predict_delta/diff_check/metric_curve/stop_at_epoch/render_run/…）、其余 6 个 subagents md、其它 workflow 全部文件。

---

## 4. 实施步骤（有序）

**排序依据**：脚本层是机械核心且被 prompt 层全部消费（D-V5-5 单一来源先立）；脚本层内部按依赖序（history/round_state → verdict/advance/gate → deploy/mode/rules；原唯一前向依赖 gate_node.sh 的 `--verify` 接线消费 deploy `--verify` 模式，已随 gate_node.sh 整体归 C3 消除，N4）；yaml+agent 层原子成 commit（inputs 退役与 `{{ inputs.* }}` 引用必须同 commit，否则 validate/渲染断裂）；subagents 与 agent 层同 commit 收口；集成测试最后（smoke 依赖全链脚本定稿）；每 commit 保持 po 测试文件绿。

**中间态声明（对抗轮 1 Q7）**：C1–C4 完成后、C5 之前，workflow **运行时是刻意断裂的**——caller 侧（workflow.yaml po_gate command、probe/full_train protocol 的 `--budget` 调用行、po_propose agent.md 的 `--min-*` 调用行）仍在传 v5 已退役参数，运行即 fail loud。这是接受的中间态：脚本层先行 + prompt 层原子收口的必然代价；该窗口内验收口径 = 脚本级测试绿，**禁止在 C1–C4 区间运行 workflow / 真机 / E2E**（迁移 loop 批 H 若跑全仓验证亦须避开此窗口或知晓该断裂）。

### Step 0（S0）：开工门槛断言 —— 无 commit

- 做什么：断言 `workflows/prof-opt/workflow.yaml` 存在、`workflows/prof-opt.yaml` 不存在、`tests/test_po_scripts.py` 的 `_SCRIPTS` 已指向新布局；`git log --oneline -5` 记录基线。任一不符 → `IMPL_STATUS=BLOCKED` 上报（附实测输出），**禁止开工、禁止猜布局**。
- 对应：SPEC §0 开工门槛 + 协调协议。
- 验证：断言命令输出留档进首个 commit message 正文。

### Step 1（S1）：脚本层（4 个 commit，每个含对应单测）

**C1 `feat(prof-opt): v5 脚本层——history 枚举/round_state 单一来源/origin 锚`**
- 触达：`_po_scripts/history_lib.py`（M）、`_po_scripts/round_state.py`（N）、`_po_scripts/analyze.py`（M）；`tests/test_po_scripts.py`（builder/dedup/analyze 既有断言改造）、`tests/test_po_v5.py`（N：round_state 域 + analyze freeze-origin 域 + history advanced/gap 域）。
- 契约：§4/§5.1/§5.2/§3.1。
- 完成判据：新增域用例全绿；既有 history/analyze 用例改造后绿；`pytest tests/test_po_scripts.py tests/test_po_v5.py`（WSL）通过。

**C2 `feat(prof-opt): v5 推进与门控——verdict 读锚/advance 双模/gate 决策序`**
- 触达：`_po_scripts/verdict_decide.py`（M）、`_po_scripts/advance_round.py`（M）、`_po_scripts/gate_decide.py`（M）；`tests/test_po_scripts.py`（gate 系重写/advance 系 fixture/verdict 系改造）、`tests/test_po_v5.py`（gate/advance/verdict 域）。
- 契约：§5.3/§6/§7.1。
- 完成判据：§11.1 gate/advance 域用例逐条可断言（含小步改进推进反例、(round, mode) 幂等键、tie-break 方向归一、不变量破坏 rc2、`--latency-reduction-min` argparse 拒绝）；全量 po 测试绿。

**C3 `feat(prof-opt): v5 部署件版本戳/profiling 模式解析/规则池/gate 接线`**
- 触达：`_po_scripts/deploy_scripts.sh`（M）、`_po_scripts/resolve_profile_mode.sh`（N）、`_po_scripts/rules_pool.py`（N）、`_po_scripts/gate_node.sh`（M：参数只留 `--max-rounds` + 决策前 `deploy_scripts.sh --verify` 消费接线——归本 commit 消前向依赖，N4）；`tests/test_po_v5.py`（deploy/rules/profile_mode 域 + gate_node verify-fail→finish-failed 分支用例）+ `tests/test_po_scripts.py`（deploy orphan 既有用例保绿）。
- 契约：§9/§2.1/§8.5（池三子命令）/§7.2（gate_node）。
- 完成判据：.VERSION 写入/--verify/篡改 rc1 + gate_node verify-fail 分支用例绿；env 优先/非法枚举 rc2/npu-smi stub 伪命中 rc2/fallback/复用不一致 rc2 用例绿；rules check/seed 四来源/冲突裁决/REUSE 不重种/merge 集合计数幂等用例绿。

**C4 `feat(prof-opt): v5 per-agent 脚本改读盘`**
- 触达：`agents/po_flatten/scripts/reuse_check.sh`（M）、`agents/po_propose/scripts/run_latency_recheck.sh`（M）、`agents/po_propose/scripts/check_prerequisites.sh`（M）、`agents/po_baseline/scripts/run_baseline_chain.sh`（M，注 a）；`tests/test_po_scripts.py`（reuse_check npu 段用例替换为模式一致性用例、recheck 参数族用例改 v5 判定、baseline chain mfu/illegal-npu 系改 profile_mode.json fixture 驱动、check_prerequisites 用例扩展三新脚本）。
- 契约：§2.2/§2.3/§8.1/§8.3。
- 完成判据：改造后全部 po 测试绿；脚本 stdout 仍单行 JSON 契约不破。

### Step 2（S2）：workflow.yaml + agent/references 层 —— **C5 单 commit（原子）**

**C5 `feat(prof-opt): v5 workflow 契约重写——inputs 8 化/顺序门控 prompt/规则消费点`**
- 触达：`workflow.yaml` + 7 个 agent.md + `po_probe/references/probe_protocol.md` + `po_report/references/report_format.md`（注 b）+ `po_full_train/references/full_train_protocol.md`（注 b）+ `subagents/structure-proposer.md` + `subagents/accuracy-analyst.md`（N）+ `tests/test_po_scripts.py` 的 input-pin 用例重写（`test_workflow_inputs_retire_script_path_add_npu_trio` → pin v5 8 输入集，防 commit 间断绿）。
- 契约：§1/§2.2（prompt 侧）/§7.2（command）/§8.1-8.7/§10。
- 子代理与 yaml 同 commit 的原因：po_probe/po_full_train agent.md 将引用 `{{ subagents_root }}/accuracy-analyst.md`；inputs 退役与全部 `{{ inputs.* }}` 消费点必须同 commit，否则中间态 validate 报 warning、渲染 StrictUndefined。
- 完成判据：`tars validate workflows/prof-opt/workflow.yaml`（WSL）零 error；`grep -rn "inputs\.npu_chip\|inputs\.npu_precision\|inputs\.npu_core_num\|inputs\.write_back\|inputs\.report_dir\|inputs\.probe_epochs" workflows/` 零命中（§1 机械验收）；洁净三件套（见 S6 判据 2）在该 commit 内先跑一轮。
- **洁净铁律（写给 coder 的硬约束）**：agent.md / subagents md / prompt-adjacent references 一律不得出现 SPEC 节号（`§8.5`）、U1/U2/U3、D-V5-n、v4 对照叙事、`plan`/issue 编号、Orca 引擎源码路径；新文本只写运行时指令（做什么/读什么/产出什么）；设计理由进 commit message。逐文件按 [洁净契约](../../orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md) 受众翻转通读。

### Step 3（S3）：集成收口 —— C6

**C6 `test(prof-opt): v5 smoke 五步 + 十域收口 + validate/洁净清零`**
- 触达：`tests/test_po_v5.py`（补 smoke 序列 + grep 验收用例收口）；如 C5 洁净检查有残留修 agent.md（并入本 commit 或回改 C5 后重跑）。
- 契约：§11.1/§11.2/§11.3。
- 完成判据（全部硬性）：
  1. `pytest tests/test_po_scripts.py tests/test_po_v5.py` 全绿（WSL `.venv`，真跑非 collect-only）；
  2. `tars validate workflows/prof-opt/workflow.yaml` 零 error 且 `_check_prompt_dev_residue` warning 清零；skill 元层脚本 `python orca/skills/create-workflow/scripts/check_dev_residue.py` / `check_agent_md_static.py` exit 0；
  3. smoke 五步（§11.2）逐盘面断言通过：① freeze-origin（base=1000, r=0.5, budget=0.1 → target=501）② 轮1 latency（verdict pass 900<1000 → advance → best=900、direction improved=true；probe 入口 mode=latency 直通；gate loop）③ 轮2（verdict pass 450 → latency advance → best=450；probe 入口 mode 翻 accuracy 首入只训 best.vid → accuracy_fail gap 0.5 → accuracy advance 无过者不推进 + marker (R2, accuracy, improved=false) + failed_sigs 含该 vid sig → gate loop）④ 轮3 恢复轮（幸存者 accuracy_pass gap 0.05 makespan 460≤501 → advance 换 best + append_advanced → gate 任意版本行 accuracy_pass → full-train）⑤ 轮帽 fixture（max_rounds=3 → best-effort；无 best → finish-failed）；
  4. §1 grep 验收零命中（作为可重复用例固化）；
  5. coder 内环 code-reviewer 复审 CLEAN（重点：fail loud 覆盖、依赖方向、DRY、prompt 洁净）。

### Step 4（S4）：状态文档 —— C7

**C7 `docs(prof-opt): v5 实现收口——CURRENT/CHANGELOG/release note`**
- 触达：`docs/status/CURRENT.md`（仅 v5 段）、`docs/status/CHANGELOG.md`、`docs/releases/2026-08-27-prof-opt-v5.md`。
- 完成判据：CURRENT v5 段更新为 Phase 3 done（真机 §11.4 清单移交用户）；CHANGELOG 每条带 commit SHA；只动 v5 相关行（并行 loop 共享文件，diff 限段）。

**commit 纪律（全程）**：`git add` 仅限当步触达清单（工作区有其它任务未提交文件，禁止 `git add -A`）；不 push；每 commit 后跑 po 测试文件保绿；commit message 带步骤标识（C1-C7）与对应 SPEC 节号（节号进 message 不进产物）。

---

## 5. 测试策略

框架/布局从项目惯例：pytest（`pyproject.toml` testpaths=tests），WSL `.venv` 执行；脚本级测试模式沿用 `test_po_scripts.py` 既有 fixture 风格（tmp_path 工作区 + 直接调函数/子进程 rc 断言）。

### 5.1 §11.1 十域 → 落点

| 域 | 落点文件 | 关键用例（每条机械可断言） |
|---|---|---|
| gate | test_po_v5 + test_po_scripts 改造 | accuracy_pass（任意版本行）+达线→full-train；round≥帽→best-effort/finish-failed；其余一律 loop（含连续多轮零推进仍 loop——替代 stall 用例）；不变量破坏 rc2；锚缺失 rc2；`--latency-reduction-min` argparse 拒绝 |
| advance | test_po_v5 + test_po_scripts 改造 | latency 严格改进才推进；**小步改进（50 cycles 且严格<incumbent）也推进**；零改进→marker improved=false+direction.json（failed_sigs 含 latency_fail 与 accuracy_fail sigs）；accuracy 态仅 accuracy_pass 推进、无过者不推进不复制；winner==incumbent 只写 marker（**良性首入分支显式绑定：该 vid 本轮 latency 态 advanced 行在场 → 不补复制**）；(round, mode) 幂等键同轮双推进各一次；tie-break 方向归一（higher/lower_better 双 fixture）；**撕裂收敛三分支守护（判据甲：best.json 已写 + winner 重算命中 + 本轮 advanced 行缺 + marker 缺 → 重放补齐；判据乙：latency 态撕裂 + 候选被压制（重算无候选、winner=None）→ 按 best.json.vid 补齐 + marker improved=true；良性首入见上）** |
| round_state | test_po_v5 | working/current 推导（marker 联动/空 rounds）；%03d；mode 两态 + 锚缺失 rc2 |
| history | test_po_v5 + test_po_scripts 改造 | append_advanced 字段集=LATENCY_FIELDS；PERMANENT 集含 advanced/promoted/unsupported_op；append_probe gap 字段（None 省略） |
| verdict | test_po_v5 + test_po_scripts 改造 | promote 读锚预算、输出 accuracy_pass/gap（curve 过 eval 不过→accuracy_pass=false 且 gap=eval 缺口）；curve-only 降级 gap=曲线缺口；锚缺失 rc2；两子命令 `--budget` argparse 拒绝 |
| analyze | test_po_v5 | freeze-origin 首写/幂等 no-op/内容冲突 rc2/量程非法（r≤0、r≥1、budget<0）rc2；不带 flag 不触碰（锚文件 mtime/内容不变断言） |
| deploy | test_po_v5 | .VERSION 写入（部署集变更→manifest 变）；--verify 通过；篡改一个部署文件→rc1 |
| rules | test_po_v5 | check 全字段/去重/枚举/坏行报行号（容忍 borrowed 注记）；seed 四来源优先级合成（镜像原样/model_hash 精确/general/plausibly_general 降档+borrowed+`pool-` 前缀；**来源 3 物化：pool- 前缀 + metric_gap=0.0 + 披露句 + 不降档不标 borrowed**）；同 change_pattern 冲突项目实测优先；**REUSE 不重种 = 脚本级守卫（工作区已有规则 → seed exit 2 + stderr 披露）**；merge confirm/refute 集合计数幂等（同 run 重入不增）、confirm≥2→general、refute≥2→quarantined、镜像覆盖写、坏行剔除披露；池/镜像缺失→空源 best-effort |
| profile_mode | test_po_v5 | env 优先 + 非法枚举 rc2（chip/precision/cores 各一）；npu-smi PATH 注入 stub：型号字段命中→mfu；"1951 MB" 伪命中→不识别 rc2；无 env 无 npu-smi→fallback；复用不一致/文件缺失 rc2（经 reuse_check，落位=复用路径段）；**测量等价但 resolved_by 翻转（env→npu-smi 同 chip）→ 不是漂移、REUSE 继续（N1 守护）**；**`--stdout-only` 只读解析不落盘**；**复用比对不触碰既有 profile_mode.json（内容不变断言，Q2）** |
| inputs 退役 | test_po_v5（+ input-pin 重写） | §1 grep 验收零命中（对 workflows/ 树跑 grep 断言）；yaml inputs 集合 == 8 输入逐字段 pin（name/type/required/default） |

### 5.2 §11.2 smoke 五步 → `test_po_v5.py` 单 fixture 工作区按真实顺序驱动脚本链并断言盘面（内容见 S6 判据 3；LLM 子代理产物不做断言对象——规则产出归真机清单 §11.4）。

### 5.3 既有用例改造清单（`tests/test_po_scripts.py`，批 D 后行号为近似锚点）

| 既有用例（锚点） | 改造 |
|---|---|
| `test_gate_*` 全系（:263-339，9 个） | 按 v5 决策序重写：stall/exhausted 入参与断言删；exhausted fixture 恒 false；`test_gate_fails_loud_without_proposals` 反转为「gate 不再读 proposals.json（缺失不炸）」或删除并由 v5 loop 用例替代 |
| `test_advance_round_*`（:391-486，6 个） | fixture promoted 行 → v5 行（latency_pass/accuracy_pass + advanced）；`_tie_break_prefers_higher_proxy_acc` → 方向归一双向；复制断言按「仅真实推进复制」调整；重放收敛用例对齐撕裂收敛语义 |
| `test_history_builder_field_sets` / `test_history_dedup_branches`（:63/:93/:153） | PROBE_FIELDS+gap；permanent 集新值；promoted 兼容读 fixture 保留一条 |
| `test_verdict_*`（:3354-3500，10 个） | `--budget` 移除→fixture 写 origin_anchor.json；`promoted` 输出键→`accuracy_pass`+`gap` |
| `test_workflow_inputs_retire_script_path_add_npu_trio`（:1613） | 重写为 pin v5 8 输入集（路径新布局） |
| `test_reuse_check_*` 全系（:2078/:2125/:2186/:2201/:2223/:2245/:2264/:2282，8 个） | **调用签名全部 4 参 → 3 参适配，并保留一条 4 参调用做 arity 拒绝负向断言（rc2，轮 2 补强）**（Q3）；`:2282`（npu 枚举门）替换为模式一致性用例组：一致→REUSE 路径继续、漂移/缺失→rc2、**测量等价 resolved_by 翻转→非漂移（N1）**、**无 lock 首跑不受校验影响（落位守护，Q1）**、**fresh_start=true 时 pre-v5 工作区可达 wipe 而非被校验拦截（恢复出口守护，Q1）**、**复用比对过程不触碰既有 profile_mode.json（内容/mtime 不变，Q2）** |
| `test_baseline_chain_mfu_*` / `test_baseline_chain_rejects_illegal_npu_args`（:1916/:1964/:1984/:2013，4 个） | 驱动方式改 profile_mode.json fixture；illegal-npu 用例移至 profile_mode 域（枚举校验归 resolve_profile_mode.sh） |
| `test_run_latency_recheck_*`（:3094-3252，4 个） | 参数族退役→v5 判定（严格改进/≤target 双模）；migration_regression fixture 对齐 |
| `test_check_prerequisites_*`（:3622-3642） | 清单增三新脚本在场断言 + 缺失 fail loud |
| `test_deploy_scripts_retires_orphan_scripts`（:2326） | 保绿（孤儿清理语义不变）+ 与 .VERSION 交互（清理后 manifest 重算） |

### 5.4 §12 fail loud 矩阵 → 测试覆盖映射

| §12 场景 | 覆盖用例 |
|---|---|
| ORCA_PO_NPU_* 非法 / npu-smi 解析不出 | profile_mode 域 env 非法 rc2 + stub 伪命中 rc2 |
| 复用模式漂移 / profile_mode.json 缺失 | profile_mode 域 reuse_check rc2 |
| origin 锚缺失被 gate/advance/verdict/probe 读 | 各域锚缺失 rc2 用例（gate/advance/verdict/round_state mode 四处） |
| freeze-origin 冲突 / 量程非法 | analyze 域 4 用例 |
| gate 不变量破坏 | gate 域 rc2 用例 |
| deploy --verify 不符 | deploy 域篡改 rc1 + gate_node verify-fail→finish-failed 分支用例 |
| accuracy-analyst 产物违规 | rules 域 check 拒坏行（重派/剔除的 agent 行为归 prompt + 真机清单） |
| rules_pool seed/merge 池或镜像缺失损坏 | rules 域 best-effort 空源/坏行剔除用例 |
| 恢复轮变体超 target 线 | advance/recheck 域 latency_fail 机械淘汰用例 |
| 恢复轮无 accuracy_pass 幸存者 | advance 域不推进 + failed_sigs + smoke 轮2 |
| 单变体 probe 不可证 | dedup 侧既有 `test_history_dedup_probe_config_retry`（:160，probe_insufficient 同配置拦截）+ v5 history 域枚举断言；**判定行为本身是 agent 侧行为，无既有用例、v5 不新增（归 prompt + 真机 §11.4，Q4 如实标注）** |

### 5.5 项目无测试惯例声明：不适用——项目有完整 pytest 惯例（见上）。

---

## 6. 风险与回退

**三大最可能出错点**：

1. **与迁移 loop 的时间竞态（协议读法已裁决：宽读法，2026-08-27，编排者——批 D 即 v5 开工门槛）**：CURRENT.md 协调协议两句（「写权归迁移 loop 至批 H」vs「v5 等批 D 落地开工」）的分歧已由编排者裁决关闭：**v5 只写 `workflows/prof-opt/` 子树、不碰迁移 E-H 批触达面；撞车 fail loud 双停**。窗口期防护：Step 0 门槛 + 每次开工前 `git log --oneline -5` 核对无 `layout` 系新提交触碰 `workflows/prof-opt/`；发现他方改动本任务文件 → fail loud 停，上报编排者协调（协议已双向设防：迁移 loop 批 D 前核对无 v5 提交）；C1–C4 中间态运行时断裂（见 §4 声明），批 H 全仓验证须避开该窗口或知晓该断裂。回退：v5 各 commit 独立可 revert，不涉及布局。
2. **agent.md 大改的洁净回归**（7 agent.md + 3 references + 2 subagents 全部重写段落，最易夹带 SPEC 节号 / v4 考古 / U1-U3 决策叙事）。处置：C5 内逐文件跑 skill 元层脚本（check_dev_residue / check_agent_md_static）+ `tars validate` warning 清零 + 受众翻转通读（coder 分发内环 review agent 执行，findings 清零才算 C5 完成）；§5/§6 测试夹具防火墙——新 prompt 泛化抽象，禁 MNIST/TDD 接收机等 fixture 硬编码。回退：单文件 revert。
3. **advance 撕裂窗口语义分歧**（§6.1 skip 规则 vs §6.2 重放收敛的张力，见 §3 实现注记与 §8-P7 上报项）。处置：按双判据注记实现（判据甲 winner 重算命中 / 判据乙 latency 态候选压制，mode 门控防 accuracy 轮误判）+ 三分支守护用例；**残余子窗口如实披露**：latency 撕裂写已达线（mode 翻 accuracy）+ 首入 accuracy_fail → base/ 与 best.json 分叉携带一轮（质量级损失，恢复轮按自身实测收敛，rR-01 血缘可能丢失）——列入 §11.4 真机观察项；若 code-review/对抗环终判该读法越权，回退到严格字面 skip 并扩大披露（不静默）。已知良性误报（轮 1 补录）：torn no-op marker 可能 under-report `best_updated`，仅影响 report 文本不影响门控。回退：单 commit 内局部改。

**其余风险**：
- baseline chain 测试改造面扩大（注 a 衍生）：限最小改动（模式解析 + 参退役，握手语义不动），防 scope creep。
- WSL 双层引号：pytest/tars 命令一律写临时 `.sh` 执行（项目 memory 既有惯例）。
- `tars validate` 的 output_schema 对齐检查：po_propose/po_probe 新增字段必须同时在 yaml schema 与 emit 字段清单出现，漏一侧即 error——C5 完成判据已含 validate 零 error。
- 并行 loop 共享 `docs/status/CURRENT.md`：C7 只改 v5 段，diff 限段。

**回退总案**：整特性 = C1-C7 线性 commit 链，逐个 revert 即回 v4 语义；无数据迁移、无引擎改动；旧工作区兼容性由设计保证（promoted 读兼容；pre-v5 工作区复用时 profile_mode.json 缺失 → reuse_check exit 2 指引 fresh_start，属 §2.3 契约行为非回归）。

**fail loud 点**（实现中不得静默化）：resolve_profile_mode 非法枚举/伪命中；reuse 模式漂移；origin 锚缺失/冲突/量程非法；gate 不变量破坏；deploy --verify 不符（gate 走 finish-failed 披露、propose/probe 走节点 fail loud——两分支语义不同，勿混）；rules 坏行（check 报行号；seed/merge best-effort 但 stderr 必披露）；advance 撕裂收敛失败（缺产物 → exit 2 沿 v4）。

---

## 7. 规模标注

**large**。依据：触达 32 个文件——workflows 树 27（4 新增：round_state.py / resolve_profile_mode.sh / rules_pool.py / accuracy-analyst.md；23 修改，含 3 个 §13 披露补触）+ tests 2（test_po_scripts.py 改 / test_po_v5.py 新）+ 状态文档 3（CURRENT / CHANGELOG / release note）；SPEC 契约章节 §1-§13 全量落地；测试侧 10 域新用例 + 5 步 smoke + 约 49 个既有用例改造（§5.3 逐行加总）；7 个 commit 的多阶段序列。计划深度与之匹配（全量结构）。

---

## 8. 计划级补定与上报项（全部披露，无一静默；均为 SPEC 未定义处补定或需上位裁决项，非契约私改；编号 P 系避免与 SPEC §8.x 撞号）

| # | 事项 | 性质 | 处置 |
|---|---|---|---|
| P1 | 注 a：`run_baseline_chain.sh` 补触（§13 未列） | 枚举遗漏推导（§2.2「profile 调用改读盘」的物理载体在链内；§0 per-agent scripts 清单恰好漏列第四个） | 计划补触，§3 注 a 披露；对抗轮 1 独立裁定「合理推导，非越权」 |
| P2 | 注 b：`report_format.md` / `full_train_protocol.md` 补触（§13 未列） | 枚举遗漏推导（退役 input 的字面引用与 `--budget` 调用行在这两个 prompt-adjacent references 内；§13 已列同类 probe_protocol.md） | 同上，对抗轮 1 裁定成立 |
| P3 | gate_node fail 分支 emit 字段（stall 出、mode/target_cycles 入） | SPEC §7.2 只说「按既有 fail 分支」，未规定字段集 | 机械补定（dangling 字段清理），微披露 |
| P4 | probe latency 直通轮的 `advanced_vid`/`best_updated`/`base_advanced` 填报口径 | SPEC 未定义直通轮三字段来源 | 钉死为盘面重导出：marker.round == current 且 marker.mode == latency → 三字段取该 marker + direction.json；否则（含 propose Step6 崩溃顺延、marker 陈旧）→ 零推进填报（advanced_vid=""、best_updated=false、base_advanced=false），不恒 false 也不读旧轮值；verify 前移至节点入口消解 §9 与 §8.4 的可达性矛盾；yaml output_schema 同字段的 v4 语义描述（推进归属 probe）随之微调 |
| P5 | `zero_improvement_rounds` 统计口径 | SPEC §8.6 只说「读全部 direction.json 统计」，未定义口径；direction.json 同轮覆盖会失真（smoke 步 3 反例） | 钉死为 history `advanced` 行推导（「有 direction.json 的轮中无 round==R 的 advanced 行」计数——advance 含 no-op 恒写 direction.json，故「有 direction.json」= 该轮跑过 advance），direction.json 仅作披露素材 |
| P6 | rules seed 来源 3（general:true）条目物化字段映射 | SPEC 仅定义来源 4 的 borrowed 映射；工作区 schema 全字段必含但池条目无 metric_gap | 钉死：pool- 前缀 + evidence 取池记录并集 + metric_gap=0.0（唯一不造大谎的有限数，check「有限数」放过属已知并加 analyst 侧防线）+ statement 披露句「(general pool entry: no local measured gap)」；不降档不标 borrowed；池源条目不参与 gap 阶梯 confidence（N3） |
| P7 | advance 撕裂收敛判据（甲：本轮 advanced 行 + winner 重算命中；乙：latency 态候选压制扩展，mode 门控） | §6.1 字面 skip 规则 vs §6.2 重放收敛在崩溃窗口的张力 | 计划采双判据读法 + 三分支守护用例 + 残余子窗口如实披露（见 §3.1 注记）；**errata 已回填 SPEC（2026-08-27，编排者；§6.1 撕裂恢复读法 + 草稿附 A 记录）——计划读法与 SPEC 一致化，双判据现为契约实现细节非计划私读** |
| P8 | 协调协议批 D 门槛 vs 批 H 写权的读法分歧 | CURRENT.md 两句并存 | **已裁决（宽读法，2026-08-27，编排者）：批 D 即 v5 开工门槛（与协议第 10 行明文一致）；v5 只写 `workflows/prof-opt/` 子树、不碰迁移 E-H 批触达面；撞车 fail loud 双停**。原 surfaced conflict 关闭 |
| P9 | reuse_check 模式一致性校验落位（仅复用路径可达段）+ resolve `--stdout-only` 只读解析 + **比对集排除 resolved_by** | §2.3「重新解析一次…逐字段比对」未指定落位与写副作用；「逐字段」字面含溯源字段会伪漂移（N1：env→npu-smi 同硬件来源翻转 = 测量等价却强制 wipe） | 钉死落位（lock 匹配后、products 前）+ 只读比对 + 比对集 = {mode, chip, precision, core_num}；**errata 已回填 SPEC（2026-08-27，编排者；§2.3 比对集收窄入契约）——计划读法与 SPEC 一致化**；守护用例见 §5.3 |

---

## 附：对抗审查记录

- **轮 1（2026-08-27）**：plan-adversary 独立实证审查（SPEC 全文 + 计划 + 规约 + 现状代码逐行对表）。质疑 13 条：真问题 5（Q1 reuse_check 校验落位可致首跑死锁/fresh_start 恢复失效——最强发现；Q2 复用比对写副作用致漂移检测恒失效；Q3 reuse 系 8 用例签名适配漏列；Q4 §12 行 11 空测试映射；Q5 计数小错）+ 设计权衡 8（Q6-Q13）。裁定：§11.1 十域 / §11.2 五步 / §11.3 三项 / §1 grep 逐条无未覆盖验收标准；注 a/注 b 独立裁定为「合理推导，非越权」。
- **轮 1 闭环**：Q1-Q5 全部修订入计划（§3.1 reuse_check 行重写 + §5.3 用例组扩充 + §5.4 行 11 如实标注 + 计数修正）；Q6 收紧（词形钉死「本轮」、撤「唯一读法」声称改列权衡与备选 why、守护用例绑定、上报 SPEC errata）；Q7 采纳（§4 中间态声明）；Q8 采纳（§6 风险 1 surfaced conflict + §8-P8 上报）；Q9/Q10/Q11/Q12 采纳（计划级补定钉死）；Q13 采纳（微披露）。
- **轮 2（2026-08-27）**：同一 adversary 复审——轮 1 十三条逐条验证**全部实质闭环**（Q1 五场景落位推演全过；Q3 更正轮 1 误数：reuse 系确为 8 个，计划的 8 正确）；§11.1 十域 / §11.2 五步 / §11.3 / §1 grep 复检无形式失守。新质疑 5 条：N1（真问题：resolved_by 入「逐字段比对」→ 同硬件 env→npu-smi 来源翻转伪漂移强制 wipe 测量等价工作区）；N2（权衡：撕裂收敛未覆盖「latency 态撕裂 + 重算候选被压制」子窗口）；N3（真问题：来源 3 披露句用词与不标 borrowed 自相矛盾 + metric_gap=0.0 需 analyst 侧防线）；N4（权衡：C2 gate_node --verify 前向依赖 C3 与排序自述矛盾）；N5（微项：§2 表措辞滞后/§8 撞号/用例计数偏低/「产物」未钉死/output_schema 描述卫生）。
- **轮 2 闭环**：N1 采纳（比对集钉死 {mode, chip, precision, core_num}、排除 resolved_by + 守护用例 + §8-P9 随 errata 上报）；N2 采纳扩展判据乙（mode==latency 门控 + best.json.round==current + marker 缺 (current, latency) + 无本轮 advanced 行 → 补齐；accuracy 轮误判被封死、accuracy 撕裂仍由判据甲覆盖）+ 残余子窗口如实列入风险 3 与 §11.4 观察项 + errata 文本扩展；N3 采纳（披露句改「(general pool entry: no local measured gap)」+ 池源条目不参与 gap 阶梯 confidence 双侧防线）；N4 采纳（gate_node.sh 整体归 C3 + 排序依据句修正）；N5 全部采纳（§2 表已同步/§8 表改 P 系编号/P5「产物」钉死为有 direction.json 的轮/output_schema 描述微调入 yaml 行/用例计数 49）。零 open 项。
- **轮 3（定点复核，2026-08-27）**：只查 N1-N5 修订点。裁决：五点全部闭环确认；**判据乙经全量误报/漏报攻击后判定健全**（四条件合取在 v5 语义内仅撕裂盘面可满足——完成的推进/no-op 推进/同轮双推进/marker 外删/best.round==current 非撕裂来源全部被某条件排除；甲乙按 mode 分治无重叠；adversary 补验 planner 未列的「撕裂写已达线 + 首入 accuracy_pass」子场景由判据甲覆盖收敛——非残余；唯一漏报 = 已披露残余，损害评估复核准确）；N3 双侧防线完整（prompt 级是唯一可行杠杆）；N4/N5 落实，用例计数 49 经第三方重加总确认。三处文字卫生微项（§2 表 P9 陈旧引用 / 排序句改过去时 / errata「marker 无该轮该模记录」口径）当场闭环。**稳态确认：无新真问题、无新权衡项，计划可 READY 交 coder。**
- **上位裁决回执落实（2026-08-27，编排者）**：① P8 已裁决宽读法（批 D 即开工门槛；v5 只写 `workflows/prof-opt/` 子树、不碰迁移 E-H 批触达面；撞车 fail loud 双停）——§6 风险 1 与 §8-P8 已按裁决收口；② P7/P9 errata 已回填 SPEC（§6.1 撕裂恢复读法 + §2.3 比对集收窄为四字段，草稿附 A 已记）——§2 影响面表 / §3.1 reuse_check 与 advance_round 行 / §8-P7/P9 已同步「计划读法与 SPEC 一致化」，撕裂双判据与四字段比对集现为契约实现细节而非计划私读；③ 轮 3 三处微项复核确认已在轮 3 当场闭环。纯文字修订，结构未动，无需追加 adversary 轮。

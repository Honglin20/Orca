# 实施计划 - prof-opt v4 重构（v3.5 10 节点 → v4 8 节点）

PLAN_STATUS: READY（第 2 轮修订带两项签收前置：§10.4 Step R1 的 factory.py 扩项 + SPEC addendum 双批复；PLAN_CONFLICT 增 P8 待回卷）

> 契约（权威）：`docs/specs/prof-opt-v4-spec.md`；语义权威：`docs/specs/prof-opt-v4-design-draft.md` v3.1（D-V4-1~19 + 附 A）。
> 现状基线：commit `86ccf99`（v3.5 全部产物已入库——已实读对账：yaml 10 节点 / `_po_scripts` 21 文件 / po_* 10 目录 / subagents 2 文件 / test_po_*.py 3 文件）。**已知基线事实：测试面实跑 13 红 / 46 绿（v3.5 遗留测试-脚本漂移），Step 0 清偿。**
> 本计划只覆盖实现 + 单测；E2E（SPEC §7）由后续 test-agent 独立执行，逐节点洁净审查（SPEC §6）是独立 Phase——两者只在本计划 §8 留衔接面。
> **PLAN_CONFLICT：§2.5 列 P1-P6 六处计划与 SPEC 的偏差/沉默点**（eval@k 单测载体 / §6 grep 范围与豁免 / §7 latency_reduction_min default 句 / experiment_ledger v4 角色 / 配额轨迹载体 / **history IMPL 行写入归属——不补则 advance_round 永不推进**），均已给默认处置并上报——非静默偏离。
> **第 2 轮修订（2026-08-26，E2E 回退）**：D-1 [PLAN_ISSUE] po_gate script 节点 in-session exit 127 的引擎侧修复 + D-2 [MINOR_FIX] po_flatten Output 强化，见 **§10**。§1 硬边界的可改面在 §10.5 显式扩充（含一项待编排者批的边界外文件）；**P7**（SPEC 2026-08-21 §2.4.2 addendum，§10.2，随签收批）+ **PLAN_CONFLICT-P8**（SPEC §7"强制 loop 至少一轮"断言回卷，§10.7）。

---

## 1. 目标与范围

**目标**：按 SPEC 逐字落实 v3.5 → v4 重构——删 `po_implement`/`po_verify` 两节点；`po_baseline` 基线完整训练非阻塞（finalizer 守护 + live 图）；`po_propose` 三 subagent 内闭环（含 run_latency_recheck 迁移）；`po_probe` stop-at-k + GPU 串行守卫；`po_full_train` 锚简化 + 对称终检；`po_gate` bug 修复；`po_report` 终态收割；新增 3 共享脚本 + 4 subagent + 1 校验脚本；退役 3 目录 + 1 脚本；单测全绿。

**非目标（out of scope）**：
- E2E 执行（SPEC §7）——test-agent 独立环节，本计划只保证其断言所需的盘面字段全部落位（见 §2.2）。
- 逐节点洁净审查执行（SPEC §6，每节点单独 reviewer + `verify/cleanliness/` 落盘）——独立 Phase；coder 只做前置自检门（= §4 Step 3 完成判据：tars validate 0 warning + 残留 grep 0 命中）。
- 早停语义支持、review-agent / 硬件知识库 / mfu_adapter 四层链（D-V4-13 搁置）。
- release note / CHANGELOG / CURRENT.md（收尾期编排者更新）。

**硬边界（并行任务 create-workflow skill v2 实现中，违者即越界）**：
- **禁改**：`orca/skills/`、`orca/iface/`、`tests/iface/`、`tests/test_skill_benchmark.py`、`tests/test_skill_v1_checks.py`、`CLAUDE.md`、`docs/status/*`、`.e2e_po/`、`.e2e_spe2e/`、puzzle 相关 WIP。
- **可改面**：`workflows/prof-opt.yaml` + `workflows/agents/{po_*,_po_scripts}` + `workflows/subagents/prof-opt/` + `tests/test_po_{scripts,diff_check,inject}.py`（后两者不动）+ `docs/specs/prof-opt-v2-design-draft.md`（标注）+ `docs/specs/prof-opt-v4-*.md`（提交）+ `docs/plans/`（本计划）。
- SPEC 冲突处理：实现期发现计划与 SPEC 冲突 → 停手标 `PLAN_CONFLICT` 上报编排者，禁静默偏离。

---

## 2. 契约影响面

### 2.1 SPEC 条款 → 计划落点总表

| SPEC 契约项 | 落点（文件 / 步骤） |
|---|---|
| §1 `_po_scripts` 清单（3 新增 / 4 修改 / 1 退役 / 其余不变） | Step 1（§3 表 #1-#8） |
| §1 节点目录清单（po_baseline/po_propose 重构，3 目录退役） | Step 2（§3 表 #9-#12 + #26/#27 退役）+ Step 3（§3 表 #14-#25 + #17 退役） |
| §1 subagents 4 新增（骨架同 memory-verifier） | Step 3（§3 表 #28-#31） |
| §1 test_po_scripts.py 改/增清单 | Step 0/1/2 各自带 + Step 4 全量绿（§5 表） |
| §1 docs：v2 草稿 superseded 标注 + v4 spec/草稿提交 | Step 5（§3 表 #34-#36） |
| §2 inputs 12 全保留 + 3 处 description 更新（名/类型/default 零变更） | Step 2A（yaml） |
| §2 po_baseline schema 9 字段（删 proxy/ref acc 两字段）+ 路由 | Step 2A + Step 2C（同批：schema 与 emit） |
| §2 po_propose schema 10 字段 + `failed ⇔ error 非空` + 路由（→po_probe） | Step 2A + Step 3B（agent emit 字段集） |
| §2 po_full_train `baseline_full_acc_source` enum=["baseline",null] | Step 2A |
| §2 po_report `stage` enum 收缩为 8 值（去 implement/verify） | Step 2A |
| §2 nodes 注释块 + 字段级 description v4 重写总则（禁 ref-input/auto-trained/懒补训/epoch-only proxy 措辞） | Step 2A；残留门 = Step 3 完成判据 + §8 |
| §2 workflow description 含准入条款句一句话 | Step 2A（句源见 §2.3 E3-07） |
| §3 七个 agent.md 通用骨架 + Subagent Call Protocol 实际 dispatch 声明 | Step 3B（全部 7 个） |
| §4 po_flatten 零改动 | 不动（验证性确认，无动作） |
| §4 po_contract 全部增量（≥2 epochs 快跑 / 早停早拒 / metric pattern 锚定 / 5 个新 contracts 字段 / reuse fail loud / 准入条款句 / check_contracts.sh 断言变更） | Step 2（§3 表 #12）+ Step 3B（#18） |
| §4 po_baseline 非阻塞链 + finalizer 守护 + 五段标题检查 | Step 2（§3 表 #10-#11）+ Step 3B（#14） |
| §4 po_propose Step 0-6 内闭环 + stamp 键 + exhausted rationale + 配额 3 + 真 profiler 条件守卫 + 失败矩阵 | Step 3B（§3 表 #15-#17） |
| §4 po_probe GPU 四象限守卫 + stop-at-k 轮询 + eval@k 双过/降级 + D-V4-18 行 | Step 3B（§3 表 #19-#20） |
| §4 po_gate 零改动（仅 D-V4-15 修复） | Step 1（§3 表 #1；po_gate/ 目录退役归 Step 2B，#27） |
| §4 po_full_train 锚简化 + 对称终检 | Step 3B（§3 表 #21-#22） |
| §4 po_report 终态收割 + report_format 四处改写 + finalize chart | Step 3B（§3 表 #23-#24） |
| §5 四 subagent IO 契约 + 节点侧校验 | Step 3B（§3 表 #28-#31 + #11 check_business_logic.sh） |
| §6 洁净验收（词表 grep 0 命中[范围/豁免见 §8 与 P2] + tars validate 0 warning） | coder 前置自检门 = Step 3 完成判据；逐文件审查 = 下游 Phase（§8） |
| §7 E2E 所需盘面字段（stop_status / epoch_compare.at_epoch / .chart_push.log / finalizer.log ISO8601 行 / baseline_*_acc.json / train_final.json） | 分别由 #5/#3/#7/#10 号文件落位（§2.2 第 3-6 组） |
| §8 分批 commit + 回卷规则（草稿级问题回卷不静默偏移） | §6 回退设计 + §1 边界声明 |

### 2.2 生产者-消费者同步图（谁必须与谁同步改）

> 「契约影响」= 盘面文件或 schema 的生产者与消费者**必须同批落位**，单边改动 = 中间态断裂。以下 11 组是本次重构的全部同步面。

| # | 契约载体 | 生产者 | 消费者 | 同步要求 |
|---|---|---|---|---|
| 1 | contracts.json v4 新字段：`train.ckpt_output_rule`（三态）、`train.ckpt_per_epoch`、`full_train_budget{epochs,seed,data}`、`probe_cap_mechanism="stop-at-k"`、顶层 `reason` 含准入条款句 | po_contract/agent.md | check_contracts.sh（校验）；run_baseline_chain.sh finalizer（渲染 epochs + ckpt 寻址）；po_probe（渲染 + eval@k 分支）；po_full_train（指纹校验）；po_report（Fairness Note 轮数） | Step 2C 钉字段断言、Step 3B 写产出方（同一 release 单位内）；字段形态两处一致由 T11 fixture + E3-07 式对照保障 |
| 2 | history PROBE_FIELDS +4 可选字段（eval_skipped_no_epoch_ckpt / monitor_failed / eval_acc / eval_failed）+ 去重 probe 配置指纹键 | history_lib.append_probe | structure-proposer 去重；gate_decide / advance_round（读 proxy_acc——不变项，回归保护） | Step 1 改 builder；gate_decide/advance_round 源码零改动。**机制依据（已实读核证）**：`_append` 只校验 `new_fields ⊆ 对应 builder allowed`（L74-79），PROBE_FIELDS 扩容不影响 `append_outcome`（走 LATENCY_FIELDS）；gate_decide/advance_round 读侧全 `.get()`，旧行共存不炸。既有 gate 测试当前因 v3.5 遗留签名漂移而红（advance_round 系测试现状即绿），Step 0 清偿 gate 侧后两者零改动仍绿 |
| 3 | metric_curve compare 输出无条件增 `at_epoch` + `baseline_path`；`--at-epoch k` | metric_curve.py | po_probe epoch_compare 断言；E2E 内容线③ | Step 1 落地；po_probe agent.md（Step 3B）按新输出字段断言 |
| 4 | stop_status.json{killed/natural_done, stopped_at_epoch, rc, monitor_failed} | stop_at_epoch.sh | po_probe State derivation（"stop_status 未出且组活 → 继续调"）+ report 披露 | Step 1 落脚本 + 单测全分支；Step 3B po_probe 按契约消费 |
| 5 | baseline_full_acc.json / baseline_k_acc.json / train_final.json / finalizer.pid / finalizer.log（ISO8601 UTC 行首） | finalizer（run_baseline_chain.sh `--finalizer` 模式） | po_probe 四象限守卫 + eval@k 锚；po_full_train 锚 + 防御检查；po_report row3 三态 + 终态收割；E2E ⑧ | Step 2C 落 finalizer；三个消费者 agent.md（Step 3B）按同一路径/字段读（同一 release 单位内） |
| 6 | `.chart_push.log`（审计行 {ts, baseline_epochs, curves}） | push_curves.py | po_report finalize 兜底（title `(final)`）；E2E ⑨ | Step 1 落脚本（含 `--title` 参数支持）；Step 3B po_report 消费 |
| 7 | yaml output_schema ↔ emit 字段集 | yaml（契约方） | run_baseline_chain.sh stdout（po_baseline 9 字段）；po_propose agent.md Output（10 字段，emit_result 组装）；po_baseline agent.md fallback emitter | **schema 与 emit 同批落位（Step 2 同一 commit）**——既有 `test_baseline_chain_stdout_line_is_schema_shaped` 的字段集从 live yaml 派生（L1257-1265，"never hand-copied"），单边先行必红；`additionalProperties:false` 靠该测试钉死 |
| 8 | verdict.json skip 键（存在即跳过） | run_latency_recheck.sh（迁移自 run_verify.sh） | po_propose Step 5 打回流程（复测前删该 vid verdict.json） | Step 2C 迁移保持语义；Step 3B agent.md 写明删文件步骤；迁移回归测试钉 |
| 9 | 准入条款句（"训练须按给定轮数精确执行…"） | **po_contract/agent.md = 唯一源** | check_contracts.sh（常量子串校验 contracts.json 顶层 reason）；yaml description（一句话语义同源） | §2.3 E3-07 专项；同步由单测机械钉 |
| 10 | metric 正则（contracts.json `train.epoch_metric_extraction.pattern`） | po_contract | metric_curve extract；**stop_at_epoch.sh --contract（禁独立 --pattern）** | §2.3 E3-06 专项：stop_at_epoch 复用 metric_curve 的读取+解析实现，禁复制 |
| 11 | deploy_scripts.sh glob 部署 ↔ 脚本增删 | 不改（声明性验证） | 新增 3 脚本自动部署；perturb_ckpt.py 自动退役（orphan 回收） | 既有 `test_deploy_scripts_retires_orphan_scripts` 扩展断言 perturb_ckpt 被回收 |

### 2.3 计划期专项落点（spec-review 轮 3 指出，必须显式落地）

**E3-06 —— stop_at_epoch.sh 与 metric_curve.py extract 的 contracts 读取共用实现路径**
- 落地：`stop_at_epoch.sh` **只收 `--contract <contracts.json 路径>`**（argparse 层禁 `--pattern`）；epoch 解析不自己写正则逻辑，而是调 deployed scripts 目录里的 `metric_curve` 模块函数（`_contract_pattern` 读契约 + `_extract` 解析日志），bash 侧只做「max epoch ≥ k 判断 + kill + 终态重解析」。metric 正则的数字组界锚定前提（"数字组后须紧跟行尾/非数字界"）由 po_contract 期钉进 contracts（同步组 #1），stop_at_epoch 不重复实现。
- 钉法（单测）：①同一 contracts.json 下 stop_at_epoch 的 epoch 判定与 `metric_curve extract` 输出一致（含"改 pattern 两者同步变"）；②contracts.json 缺 pattern 时两者走同一报错面（fail loud 同源）；③stop_at_epoch 传 `--pattern` 直接被拒（usage error）。

**E3-07 —— 准入条款句单一权威源**
- 落地：条款句原文**只写在 `po_contract/agent.md`**（产出 instructions 内）；`check_contracts.sh` 以**常量子串**校验 contracts.json 顶层 `reason` 含该句；yaml `description` 写一句话版本（语义同源，非逐字场）。
- 钉法（单测，防双源漂移）：测试从 `check_contracts.sh` 文本中提取该常量子串，断言其逐字出现在 `po_contract/agent.md` 中——sh 与 agent.md 任一侧改句不同步即红。

### 2.4 实现自由度与钉法（非冲突开口，提前亮明防 adversary 误判）

| 开口 | SPEC 原文 | 计划处置 |
|---|---|---|
| `ckpt_output_rule` 三态的字段形态 | "增 `train.ckpt_output_rule`（三态描述）+ `train.ckpt_per_epoch`（可寻址布尔）"——v3.5 该键已是 glob 字符串（resolve_ckpt 消费） | 形态（保留 pattern 子键 or 顶层重组）是 coder 决定；约束 = ①三态 + 布尔可机械判定 ②check_contracts.sh 断言 ③finalizer 末 ckpt / 第 k ckpt 寻址消费一致。测试钉行为不钉形态 |
| history 去重指纹键集 | "去重 probe 配置重试指纹键同步新字段" | 具体键集随实现（候选 = 地址性派生配置项）；测试钉行为语义：同配置 probe_insufficient 阻塞 / 配置变化重开（扩既有 `test_history_dedup_probe_config_retry`） |
| eval@k 失败重派的单测形态 | SPEC §1 测试清单含"eval@k 失败重派 1 次再降级分支"，但重派控制流在 agent.md（po_probe 无本地脚本，重派无确定性载体） | 单测覆盖机械面：history_lib eval_* 字段写入路径 + 降级判定的输入面（曲线单判 compare 不带 eval）；agent 侧重派次数/披露措辞归 E2E + 洁净审查。**该分层处置与 SPEC §1 原行存在实质偏差 → 已列 PLAN_CONFLICT-P1 上报，待 SPEC 回卷改写该行为两层表述并过确认闸** |
| finalizer 的宿主形态 | SPEC §1 po_baseline scripts 仅 `{run_baseline_chain.sh, check_business_logic.sh}` 两文件 | finalizer = `run_baseline_chain.sh --finalizer` 自调用模式（setsid detach），继承既有 `--worker-step` 模式先例（L413-423 SELF 自调用 + L376-382 setsid）；**不新增第三脚本文件**（否则违反 §1 清单）。与 SPEC §4"finalizer 守护 launch"无矛盾——SPEC 钉 launch 形态与产物，未钉实现宿主 |
| push_curves 的 title | SPEC §4 po_report 行"push_curves.py title `(final)`" | push_curves.py CLI 增 `--title`（默认无后缀）；终稿推送传 `(final)` 后缀 |
| compare 输出 epoch 键去留 | SPEC D-V4-17 只说"增"两字段，未言删 v3.5 的 `epoch` 键 | **保留 `epoch` 键**（加法语义；与 `at_epoch` 同值——后者为 v4 命名的 E2E 断言面）；po_probe references 同步说明 |

### 2.5 PLAN_CONFLICT 清单（计划与 SPEC 的偏差点，上报编排者，禁静默消化）

| # | 冲突 | 证据 | 计划侧默认处置（SPEC 回卷前按此实施，回卷后同步修订） |
|---|---|---|---|
| P1 | SPEC §1 测试清单"eval@k 失败重派 1 次再降级分支"按字面无单测载体（po_probe 无本地脚本，重派控制流在 agent.md） | SPEC §1 L55 + §1 po_probe 文件清单（无 scripts/） | 两层落地：机械面单测（T14）+ 控制流归 E2E/洁净审查；**建议 SPEC 回卷改写该行为两层表述** |
| P2 | SPEC §6 残留 grep 范围 `workflows/agents/` 覆盖非 prof-opt 目录（kd-train-script 等其他 workflow，命中词表但硬边界禁改）+ `_po_scripts/mfu_benchmark.py` L5 自身含 "mfu_adapter"（§1 又钉该文件不变）——"0 命中"结构性不可达 | grep 实测：`workflows/agents/kd-train-script/agent.md` 命中 "KD-NAS"、`model-flatten/SKILL.md` 命中 "kd-nas"、`mfu_benchmark.py` L5 命中 "mfu_adapter" | 范围收窄为本计划可改面 `workflows/agents/{po_*,_po_scripts}/ + workflows/subagents/prof-opt/ + workflows/prof-opt.yaml` + `mfu_benchmark.py` 单行豁免；**建议 SPEC §6 回卷明确范围与豁免** |
| P3 | SPEC §7"latency_reduction_min 不显式传、用 input default（yaml L30）"与实况不符——该 input `required: true` 无 default，不传则引擎校验失败 | yaml L30-33 实读 | 计划 §8 已注明：E2E 必须显式传值；**建议 SPEC §7 回卷修正该句** |
| P4 | SPEC 对 experiment_ledger 在 v4 的角色沉默（§1 只说"不变"；§4 propose Step 2 未提刷新；dashboard_snapshot 读它填 variants 表，链中断档则中盘 dashboard 恒空） | v3.5 po_propose/agent.md L111-119（Step 2 产 ledger）+ dashboard_snapshot.py L23-24/40 | propose Step 2 沿用 v3.5 的 ledger 机械刷新（继承既有机械步骤，非新增语义）；**建议 SPEC 一行澄清** |
| P5 | SPEC §5 variant-implementer 节点侧校验"配额轨迹落盘"未钉载体文件 | SPEC §5 表 | 落 `variants/<vid>/repair_trace.json`（结构修复/时延打回逐次记录；Validation 断言存在且计数 ≤ 配额 2/2；文件名 coder 可定，字段集随实现）；**建议 SPEC 补钉载体** |
| P6 | **SPEC §5/§4 均未定义 history IMPL 行（append_implemented/append_outcome）的 v4 写入者**——v3.5 唯一写者是 po_implement（agent.md L74-129 含 DONE+行写入+崩溃 reconciliation），节点退役后写路径断。后果代码级可证：`advance_round._promoted_this_round` 按 `row.round` 过滤而 round 仅存在于 IMPL_FIELDS → 无 IMPL 行则轮末推进永不发生、best.json 永不写；`dedup_state` 按 change_sig 匹配而 change_sig 亦仅 IMPL 行携带 → 去重整体失效 | `advance_round.py` L76-81 + `history_lib.py` IMPL_FIELDS/LATENCY_FIELDS/PROBE_FIELDS 实读 + `po_implement/agent.md` L74-129；SPEC §5 variant-implementer 输出无 history 行 | 计划落点 = #15 Step 4：**每次 variant-implementer dispatch 返回后由 po_propose 节点侧机械写 IMPL 行**（DONE → append_implemented；terminal-skip → **两步** append_implemented(implemented=False) → append_outcome）+ **重入 reconciliation**（DONE 在盘而 history 无行 → 补写——两步 append 协议逐字继承 variant_implementation.md，写入部分归节点、产出协议归 #31）；#31 输出契约不含 history 行（LLM 产出与机械写入分层）。**建议 SPEC §5 或 §4 明示该归属** |

---

---

## 3. 文件级改动清单

> 动作基于实读对账（commit `86ccf99`）。`git mv` 用于保历史的迁移；退役目录用 `git rm -r`（86ccf99 可恢复）。

### A. 共享脚本层 `workflows/agents/_po_scripts/`

| # | 文件 | 动作 | 改什么 | 为什么 |
|---|---|---|---|---|
| 1 | gate_node.sh | modify | L17 引号笔误 `"$MAXR)"` → `"$MAXR"`（D-V4-15） | 现状 `bash -n` 不过的缺陷 |
| 2 | render_run.sh | modify | header 组装层增 `export PYTHONUNBUFFERED=1`（D-V4-16），注释说明消费方（finalizer 增量 extract 依赖逐行实时落盘） | finalizer 逐 epoch 增量解析需要无缓冲输出 |
| 3 | metric_curve.py | modify | compare 增可选 `--at-epoch k`（任一曲线缺第 k 点 fail loud；不传行为不变——仍在最新公共 epoch 比）；compare 输出**无条件**增 `at_epoch`（实际比较深度）+ `baseline_path`（锚来源路径） | D-V4-17；E2E 断言③可证伪 |
| 4 | history_lib.py | modify | PROBE_FIELDS 增 4 可选字段；`append_probe` 签名扩展（默认 None）；`dedup_state` probe 配置指纹键同步（§2.4 开口 2）；模块 docstring 的字段表同步 | D-V4-18 |
| 5 | stop_at_epoch.sh | **new** | D-V4-3 幂等单次检查：`--log/--contract/--stop-epoch/--pid-file`；E3-06 单源解析（§2.3）；epoch≥k 首现 → /proc cmdline 归属校验 → `kill -TERM -<pid>`（进程组）→ 10s 宽限 → KILL → 重解析冻结日志 `stopped_at_epoch` = 最大完整 epoch（≥k 非恒 k）→ stop_status.json；worker 自然结束 → natural_done + rc（轮数>k → monitor_failed:true）；stop_status 已存在且 killed → 幂等返回 | stop-at-k 变体短训核心 |
| 6 | check_bottleneck.py | **new** | bottleneck_analysis.json 校验：封闭 schema（未知键 fail loud）；`top_bottlenecks[i]={name:pattern_id, op_type, cycles:total_cycles}` 与 analyze.py hot_patterns **保序子集**（非前缀）；排序/rank 一致；base_report 存在可解析 | SPEC §5 bottleneck-analyst 节点侧校验 |
| 7 | push_curves.py | **new** | D-V4-2b sidecar：读 baseline_metrics.jsonl + variants/*/metrics 曲线 → render_chart 单张 live 折线（hue=baseline/vid）；socket connect/send 各 ≤5s 硬超时（超时 = stderr + 退出 0）；成功推送追加审计行到 `$ORCA_ARTIFACTS_DIR/.chart_push.log`；`ORCA_CHART_SOCK` 缺失 → 静默退出 0；`--title` 支持（§2.4） | 基线 live 可视化（UD-1） |
| 8 | perturb_ckpt.py | **delete** | 整文件删除（本地 `__pycache__/*.pyc` 不入库，无需处理；deploy orphan 回收负责工作区侧） | D-V4-14 退役清单 |

**不动（15 文件，声明性确认）**：deploy_scripts.sh / orca_inject/{sitecustomize.py,header.env} / assert_shadow.py / PROFILER_CONTRACT.md / placeholder_profiler.py / mfu_benchmark.py / analyze.py / predict_delta.py / gen_export_onnx.py / diff_check.py / advance_round.py / gate_decide.py / emit_result.py / experiment_ledger.py / dashboard_snapshot.py。

### B. 节点目录 `workflows/agents/po_*/`

| # | 文件 | 动作 | 改什么 | 为什么 |
|---|---|---|---|---|
| 9 | po_verify/scripts/run_verify.sh → po_propose/scripts/run_latency_recheck.sh | **git mv** + 改名 | 331 行确定性逻辑零语义变更；header 注释**按新身份重写**（写"是什么"，不写"改名自/原 po_verify"——迁移出处属开发期叙事，命中增补词表 `run_verify` 且违洁净契约 §4）；阈值 100/1/0.5 保持脚本默认，**调用行显式实参**由 agent.md 落（Step 3）；stdout JSON 字段保持（节点内信息行，非节点 output） | D-V4-10 迁移 |
| 10 | po_baseline/scripts/run_baseline_chain.sh | **重构（modify）** | 七步链 → 非阻塞：①pristine 快照 + 导出 + profile + analyze（保留 pristine 快照；step1 reference 交叉核对退役删除）②完整训练 launch：同 train 契约模板、`--out baseline/train.rendered.sh`、`--set epochs=<full_train_budget.epochs>`、wrapper 组长不 exec（`setsid bash -c 'echo $$ > pid; bash train.rendered.sh; echo $? > rc'`）③finalizer 守护 launch（setsid + `baseline/finalizer.pid` + `baseline/finalizer.log`，`--finalizer` 自调用模式）④存活确认（训练 pid + finalizer.pid + train.log 出现，一律 /proc cmdline 归属校验）⑤**emit 门控**：`baseline/business_logic.md` 存在且非空是 emit `executed` 的**前置条件**（未落盘 → emit agent 内部中间态 running + 原因行，agent 轮询重入——与 v3.5 running 模式同构，verbatim 转发铁律不破）→ emit 9 字段 schema。**重入状态机（at-least-once 必备，E2E 钉值下是大概率路径——秒级训练先于五段文档完成）**：②③各以 pid 文件在场且活为幂等键（在场不重 launch）；④重入时 = "存活（训练 pid 与 finalizer.pid）**∨** train_final 已写"——finalizer 已退且 train_final{done} → 视同存活确认通过（训练确实启动且未失败），继续走 ⑤门控；train_final{failed} → emit failed（error 含 train_final.stage 归因）；pid 死且无 train_final → fail loud（finalizer 异常退出，无终态）。该细化是 SPEC §4"双存活确认"在重执行语境下的实现落地（与 po_probe 四象限守卫语义对称），非语义变更。**finalizer 契约**（§2.2 第 5 组）：轮询 pid/rc（无 rc 死亡重派 ≤3 + per-attempt train.log + resume 规则 wipe partial out-dir）；每 poll 周期 = 增量 extract（当前 attempt train.log 全量重 derive、变化才原子替换 baseline_metrics.jsonl）+ push_curves（best-effort）+ alive 心跳行（行首 ISO8601 UTC `date -u +%FT%TZ` + 曲线点数）；rc=0 收尾链（每步写 stage 行）：终检 `--expected-epochs`=full 生效值（实跑≠渲染 → train_final{failed, stage:final_check}，文案指向准入条款）→ 末 ckpt eval → baseline_full_acc.json（值+ckpt+full_train_budget 指纹，verify_anchor_budget 范式）→ ckpt 可寻址 → 第 k ckpt eval → baseline_k_acc.json → `baseline/train_final.json{status,rc,stage}`；任何内部失败 → 尽力写 train_final{failed} 再退；`baseline_status.md` 跨 turn 真相源改写 | D-V4-1/2；SPEC §4 po_baseline |
| 11 | po_baseline/scripts/check_business_logic.sh | **new** | 校验 `baseline/business_logic.md`：存在 + 非空 + 首行哨兵 + 五段标题（任务语义/输入输出/架构动机/逐模块职责与物理意义/训练目标与指标方向）齐备 | SPEC §5 节点侧校验；po_baseline Validation 必查 |
| 12 | po_contract/scripts/check_contracts.sh | modify | 断言变更：①`probe_cap_mechanism` 接受 `"stop-at-k"`（替换 epochs-only 检查）②proxy_budget epoch-only 全 null 约束保留 ③新字段校验：train.ckpt_output_rule 三态 + train.ckpt_per_epoch 布尔 + full_train_budget{epochs≥1, seed int, data{dataset_knob:null,data_value:null}} ④顶层 reason 含准入条款句（E3-07 常量子串）⑤probe/full 若分模板：须渲染自同一文件 + 数据管线一致断言 ⑥reuse 模式补 v4 字段缺失 → fail loud + fresh_start 提示（reuse 分支从"仅 viable+sha"扩为"viable+sha+v4 字段在场"） | SPEC §4 po_contract |
| 13 | po_flatten/（3 脚本 + agent.md） | 不动 | 零改动（SPEC §4 明示） | — |
| 14 | po_baseline/agent.md | **重写（modify）** | 纯脚本驱动骨架保留；步骤改：Step 0 前置检查（模板清单更新——full 模板）→ Step 1 调 chain → 训练启动确认后**并行 dispatch business-logic-analyst**（失败矩阵：重派 1 次仍败 → 走 v3.5 既有 fallback emitter 例外路径 emit `status=failed` + error 披露）→ 轮询重入直至 chain 吐终态行（chain 以 business_logic.md 落盘为 executed 前置条件，见 #10⑤）→ Validation 步跑 check_business_logic.sh（五段标题级）→ 终态行 **verbatim** 转发（9 字段）；running 语义 = 训练/finalizer 存活期 **或** business_logic.md 未落盘期（短 status message + 禁 orca next）；finalizer 不可手工干预（禁 inspect/edit detached 状态，继承） | SPEC §3/§4；emit 时序闭环见 §2.2 组 7 与 #10⑤ |
| 15 | po_propose/agent.md | **重写（modify）** | Step 0 reuse：proposals.json 存在可解析 → 跳 Step 3 从 Step 4 续做（DONE 逐提案幂等）→ Step 5 照跑；Step 1 analyze.py 机械刷新；Step 2 stamp（键 = base 版本标识[best.vid / base onnx sha] + 机械报告内容指纹，非轮号）→ 未变复用 / 变 → dispatch bottleneck-analyst（校验败重派 1 次仍败 → error）；**Step 2 顺带沿用 v3.5 的 experiment_ledger 机械刷新**（P4 默认处置，保持 dashboard 链中数据不断档）；Step 3 dispatch structure-proposer（≤3 + 三闸 + 去重 + rationale 校验；机械闸过滤后 count==0 → exhausted 强制 true + rationale 记过滤原因）；Step 4 dispatch variant-implementer 逐提案（结构修复 ≤2；失败记 skipped 不阻断）**+ 每次 dispatch 返回后节点侧机械写 history IMPL 行**（DONE → append_implemented；**terminal-skip → 两步：先 append_implemented(implemented=False) 再 append_outcome**——单写 append_outcome 会漏 round/change_sig 字段，advance_round 推进与去重即失效；重入 reconciliation = DONE 在盘而无行 → 补写——P6：无此落点则 advance_round 永不推进、去重失效。**协议拆分线**：DONE/declaration/编辑/导出/diff_check 归 variant-implementer.md（#31）；history 两步 append + reconciliation 归本节点 agent.md（载体 = agent.md body 内**直线** `python3 -c` 库调用块，无循环/分支/assert，不违洁净契约 §4 末条；**不新增 po_propose/scripts/ 文件**——违 SPEC §1 清单））；Step 5（`profile_script_path` 非空 → 前置等基线 worker 退出；placeholder 默认空不等）run_latency_recheck.sh **调用行显式 `--min-improvement 100 --min-pct 1 --min-ratio 0.5`**；未过打回 implementer 修 ≤2（复测前删该 vid verdict.json）→ 仍不过淘汰；Step 6 emit 10 字段（status==executed ⇔ error==''；exhausted + rationale）；三 subagent 失败矩阵（校验败/超配额/产物缺失 → 重派 1 次 → error 披露）各 dispatch 声明处显式 | SPEC §3/§4；D-V4-7/8/9；P6 落点 |
| 16 | po_propose/references/structural-levers.md | **new** | 结构杠杆参考（激活替换/归一化结构/零参数计算搬移三类背景先验，承接 playbook 三杠杆语义，按 v4 瓶颈分析-业务逻辑输入面重写；禁项目特例） | SPEC §1 清单；D-V4-8 |
| 17 | po_propose/references/playbook.md | **delete** | 随 structural-levers.md 替换退役（词表"playbook"归零） | §6 残留 grep |
| 18 | po_contract/agent.md | modify | 增量：指标格式实测快跑统一 ≥2 epochs（一跑双用：epoch 行格式 + ckpt 行为）；早停 best-effort 早拒（argparse/config 扫描 + 实跑观察 → 检出 viable=false 归因披露）；metric pattern 钉"数字组后须紧跟行尾/非数字界"；快跑 best-effort 断言实跑 == 渲染；contracts 增 5 项（同步组 #1）；**准入条款句原文唯一源落此文件**（E3-07）；reuse 缺 v4 字段 fail loud 指引；Subagent Call Protocol 仍只 paradigm-verifier | SPEC §4；D-V4-19 |
| 19 | po_probe/agent.md | modify | 渲染改同模板 `--out variants/<vid>/train.rendered.sh` + `--set epochs=<full 生效值>`；GPU 串行守卫四象限（探测 finalizer.pid：活→bounded-wait ≤480s + status 续驱 + 停滞判据[训练活期 train.log mtime 与曲线点数均停滞 ≥30min / finalizer 期 finalizer.log 停滞 ≥30min] / 死+done→放行 / 死+failed→error 路由 report / 死+缺失→error fail loud）；detach 后 bounded-poll（间隔 ≤30s）反复调 stop_at_epoch；State derivation 增"stop_status 未出且组活 → 继续调"；extract `--expected-epochs`=stopped_at_epoch；compare 恒 `--at-epoch k`；可寻址 → 第 k ckpt eval vs baseline_k_acc 双过才 promote（eval 加载失败重派 1 次 → 仍败 eval_failed:true + eval_acc=null + 曲线单判 + 披露）；不可寻址 → 曲线单判 + eval_skipped_no_epoch_ckpt:true；natural_done 且轮数>k → monitor_failed:true；probe 行 proxy_acc 恒填曲线@k、eval 值置 eval_acc；等待循环内 push_curves sidecar；advance_round/禁二次 detach/probe_status.md 继承 | SPEC §4；D-V4-2/3/4 |
| 20 | po_probe/references/probe_protocol.md | modify | 同步 agent.md 语义（守卫四象限 / stop-at-k 轮询协议 / poll 间隔 ≤30s 钉死 / eval 双过与降级 / stop_status 终态判定优先级 = 先 stop_status 再 rc） | SPEC §1 清单 |
| 21 | po_full_train/agent.md | modify | 删 baseline/full_train/ 路径与第二 pid 键 + auto-trained 措辞；锚 = baseline_full_acc.json 指纹逐字段校验 + 防御性 train_final=done 检查；winner 同模板 `--out final/train.rendered.sh` + full_train_budget 同指纹；**对称终检实跑 == full**（不符 → status=failed 归因）；baseline_full_acc_source 恒 "baseline"（failed null）；常驻机制（重入查活/禁二次 detach）继承 | SPEC §4；D-V4-11 |
| 22 | po_full_train/references/full_train_protocol.md | modify | 同步锚简化 + 对称终检协议 | SPEC §1 清单 |
| 23 | po_report/agent.md | modify | **终态收割**：emit 前读 finalizer.pid——死直过 / 活 bounded 等 ≤60s / 到点无终态 → 双组 kill（训练组[读 baseline pid 文件] + finalizer 组）+ 扫 variants/*/ 在飞 pid + 披露 "aborted at terminal"；baseline.proxy_acc = 曲线@k（不足 → null+披露）；finalize chart（push_curves title `(final)` + 每轮 makespan 趋势 best-effort）；写回断言 ≥1 promoted 继承全套 / no-promotion → 零写回 + 披露（非失败）；stage 枚举收缩同步 | SPEC §4；R2-12/23 |
| 24 | po_report/references/report_format.md | modify | 四处改写：row3 读 baseline_full_acc.json 三态判定（缺失→null+披露 / failed→归因 / done→读盘）；ref_acc 删 baseline_ref.json 优先级直读 baseline_full_acc；Fairness Note 轮数读 full_train_budget.epochs；内归因 implement/verify 并入 propose（DONE/verdict/history 三态）；增 stop_status 终态计数披露位 | SPEC §4 |
| 25 | po_implement/（agent.md + references/variant_implementation.md） | **delete（git rm -r）** | 整目录退役；**read-before-delete**：declaration.json + DONE marker 语义吸收进 variant-implementer.md（subagent 需要该产出契约）与 po_propose Step 4 | D-V4-14 |
| 26 | po_verify/（agent.md，scripts 已 git mv 走） | **delete（git rm）** | agent.md 退役；校验流程语义由 po_propose Step 5 + run_latency_recheck.sh 承接 | D-V4-14 |
| 27 | po_gate/（agent.md + scripts/run_gate.sh） | **delete（git rm -r）** | 整目录退役（yaml 中 po_gate 已是 script 节点走 `_po_scripts/gate_node.sh`，agent 版自 v3.5 起即死代码） | D-V4-14 |

### C. subagents / yaml / tests / docs

| # | 文件 | 动作 | 改什么 | 为什么 |
|---|---|---|---|---|
| 28 | workflows/subagents/prof-opt/business-logic-analyst.md | **new** | 骨架同 memory-verifier（frontmatter subagent/version/sentinel + Output first line + Inputs + 职责 + Output 落盘协议 + Constraints）；输入 = project_manifest.md + shadow 模型源码 + contracts.model_facts；输出 = `baseline/business_logic.md` 五段（写盘 + 首行哨兵） | SPEC §5；D-V4-6 |
| 29 | workflows/subagents/prof-opt/bottleneck-analyst.md | **new** | 输入 = base/profile/ 四件套 + 全部原始产物 + bottleneck_report.json；输出 = `base/bottleneck_analysis.json`（零重复机械字段；保序子集映射）；节点侧校验 = check_bottleneck.py（败重派 1 次仍败 → error） | SPEC §5 |
| 30 | workflows/subagents/prof-opt/structure-proposer.md | **new** | 输入 = business_logic.md + bottleneck_analysis.json + history.jsonl（去重指纹随 D-V4-18）+ references/structural-levers.md；输出 = `rounds/<NNN>/proposals.json`（≤3；rationale/op_delta/edited_files/change_spec/sota_reference；exhausted_rationale 结构化）；硬约束 = 结构级（禁训练超参——物理不可达 + Δ=0 双保险）/ 符合业务逻辑 / 围绕瓶颈 | SPEC §5；D-V4-8 |
| 31 | workflows/subagents/prof-opt/variant-implementer.md | **new** | 输入 = proposals.json + base shadow + 导出模板；输出 = 逐提案 declaration.json + DONE（或 skipped）+ compact 摘要；忠实实现单条提案；修复配额 = 结构 ≤2 / 时延 ≤2；**配额轨迹落 `variants/<vid>/repair_trace.json`**（P5 默认处置：逐次修复记录，节点侧 Validation 断言存在且计数 ≤ 配额）；失败矩阵显式；**history IMPL 行不在本 subagent 职责内**（P6 分层：LLM 产出 declaration/DONE/编辑/导出/diff_check，history 两步 append + reconciliation 归节点 #15 Step 4）；吸收 po_implement/references/variant_implementation.md 的 **DONE/declaration/terminal-skip 产出协议**（history 写入部分不吸收，归节点）后删除源 | SPEC §5；P5/P6 |
| 32 | workflows/prof-opt.yaml | **重写（modify）** | 8 节点 DAG：删 po_implement/po_verify 两节点块；po_propose 路由 `status=='executed'` → po_probe（catch-all → po_report）；po_baseline schema 9 字段（删 baseline_proxy_acc/baseline_ref_acc，增 business_logic_path）；po_propose schema 10 字段；po_full_train baseline_full_acc_source enum=["baseline",null]；po_report stage enum 收缩 8 值；inputs 12 全保留 + 3 处 description 更新（**名/类型/default 零变更**；probe_epochs 四要素 = k 语义 + 空缺省机械推定（通常 1）+ 受 full 生效值封顶 + 生效值落 contracts.proxy_budget；full_train_epoch_cap = 基线与 winner 共用上限、生效值落 full_train_budget；profile_script_path 去 mfu_adapter 措辞、"onnx 进四件套出"直述）；description 产品说明书式重写 + 准入条款句一句话；**全部节点注释块 + output_schema 字段级 description 按 v4 语义重写**（禁 ref-input/auto-trained/懒补训/epoch-only proxy 措辞；如 po_contract.probe_cap_mechanism 字段 description 例举值改 stop-at-k）；outputs 不变（全读 po_report.output）；回边 po_gate→po_propose 不变 | SPEC §2 |
| 33 | tests/test_po_scripts.py | **大改（modify）** | §5 测试策略表逐条 | SPEC §1 测试清单 |
| 34 | docs/specs/prof-opt-v2-design-draft.md | modify | 文件头加 superseded 标注（指向 v4 草稿，注明 v2 搁置物状态） | SPEC §1 |
| 35 | docs/specs/prof-opt-v4-spec.md + prof-opt-v4-design-draft.md | git add（内容不改） | untracked → 入库 | SPEC §8 流程 |
| 36 | docs/plans/2026-08-25-prof-opt-v4-refactor.md | git add | 本计划入库 | 项目规约 |

**tests/test_po_diff_check.py / test_po_inject.py：不动**（SPEC §1 明示）。

---

## 4. 实施步骤（有序）

> 排序理由：**Step 0 基线清偿先行**——86ccf99 的测试面实跑 13 红 / 46 绿（9 个 gate 测试用 `target_makespan=` 旧签名、3 个 baseline 链测试传已废弃的 `--target-makespan` 实参、1 个 contracts fixture 用 `flag:--max-steps` vs 脚本硬检查 `epochs-only`；`git diff 86ccf99` 为空，红灯为提交内固有），不清偿则后续每步"测试绿才进步"死锁。此后**叶子先行**（共享脚本独立可测）→ **yaml schema 与其 emit 方同批**（既有 schema-shaped 测试从 live yaml 派生字段集，单边先行必红；yaml 删节点与死代码目录退役同批保持 yaml↔目录一致）→ agent 面（prompt 层，无单测耦合）→ 收口与文档。每步 = 一个 commit 粒度（§6）。
> **中间态声明**：各 commit 保证**单测绿 + 可改面内自洽**，不保证 workflow 运行时一致（如 Step 2 后 po_propose/agent.md 仍 v3.5 措辞、Step 3 才重写）——运行时一致以 Step 3 末为界，revert 单位 = 步。

### Step 0 —— 基线清偿（v3.5 遗留测试-脚本漂移，13 红 → 绿）
做什么（全部在 `tests/test_po_scripts.py`，对齐 86ccf99 脚本现状，**不引入任何 v4 语义**）：
- 9 个 `test_gate_*`：`decide(target_makespan=…)` → `decide(latency_reduction_min=…)`（期望值按相对语义换算 = fixture base makespan 派生阈值）；**fixture 需补建 `base/bottleneck_report.json`**（`decide()` 实读该文件，`gate_decide.py` L66-78；现 `_gate_artifacts` 未建，缺失则 8 用例转 FileNotFoundError）。先改一例跑绿确立换算模式再批量套用。
- 3 个 `test_baseline_chain_*`：`--target-makespan` 实参 → `--latency-reduction-min`。
- 1 个 `test_check_contracts_gate_passes_consistent_workspace`：实测红因有 4 条（train 缺 `epoch_metric_extraction` + pattern 空 + proxy_budget 非 epoch-only + `probe_cap_mechanism != epochs-only`），修绿需 ~5 处协同编辑（补 extraction 规则、budget 三键全 null、probe 模板去 `<<data_value>>/<<max_steps>>` token、probe_cap 值改 `epochs-only`）。
对应验收：无 SPEC 项（前置修复）；为 T15 回归保护立可信基线。
完成判据：全量 `tests/test_po_scripts.py` **59 绿 0 红**。
说明：其中 baseline/contracts 两簇测试在 Step 2 按 v4 语义重写（T10/T11），本步只做对齐 86ccf99 的最小修复（T11 的 fixture 到 Step 2 随 stop-at-k 断言一并重做）。

### Step 1 —— 共享脚本层（§3 #1-#8）
做什么：gate_node.sh 引号修复；render_run.sh PYTHONUNBUFFERED；metric_curve --at-epoch + 双输出字段（保留 epoch 键，§2.4）；history_lib PROBE_FIELDS；新增 stop_at_epoch.sh / check_bottleneck.py / push_curves.py；删 perturb_ckpt.py（连带删其两个既有测试，扩展 orphan 回收断言）。
对应验收：SPEC §1 `_po_scripts` 清单逐项；D-V4-2b/3/15/16/17/18。
完成判据：§5 表 T1-T9 + T13 绿（T13 = E3-06 单源钉，对象 stop_at_epoch + metric_curve 均为本步产物）+ `bash -n` 过（gate_node/stop_at_epoch）+ 全量测试绿。
注意：此步只动 `_po_scripts` + `tests/test_po_scripts.py`——check_contracts/baseline 链测试对象未动仍绿。

### Step 2 —— yaml + 节点本地脚本 + 死代码目录退役（§3 #9-#12 + #32 + #26/#27 退役）
体量说明：本批确为大 commit（yaml 全重写 + 553 行链重构 + 迁移 + 2 新脚本 + 2 目录退役 + 4 组测试）——yaml 与链 emit 同批**被 T10 强制**（schema 字段集从 live yaml 派生，单边先行必红）；如需再拆，唯一保绿拆分点 = check_contracts.sh + T11 独立成 commit（新 fixture 满足新断言），其余不可拆。revert 单位（回 v3.5）= 本步整批。
内序（批内依赖序）：
- 2A `workflows/prof-opt.yaml` 重写 8 节点（schema 先钉——本批 emit 方的契约）。
- 2B `git rm -r po_gate/` + `git rm po_verify/agent.md`（其 scripts 已迁出；与 yaml 删节点同批 = yaml 不再引用的目录当场清走。po_implement/ 退役**不在此步**——其语义待 Step 3 吸收进 variant-implementer.md 与 #15 的 IMPL 行协议（P6）后 read-before-delete）。
- 2C 节点脚本：`git mv po_verify/scripts/run_verify.sh po_propose/scripts/run_latency_recheck.sh`（#9）；run_baseline_chain.sh 非阻塞重构（#10）；新增 check_business_logic.sh（#11）；check_contracts.sh v4 断言（#12）。
- 2D 测试：T10（schema-shaped 用 live yaml 派生，2A 已同批改）/T11/T8（T13 已随 Step 1 落地——其对象 stop_at_epoch/metric_curve 均为 Step 1 产物）。
对应验收：SPEC §2 全部 yaml 契约；SPEC §4 po_baseline/po_contract 脚本面；D-V4-1/2/10/19（脚本侧）。
完成判据：全量测试绿；`bash -n` 过（三个改/新 .sh）；`tars validate workflows/prof-opt.yaml` 0 error（warning 清零归 Step 3 末——agent.md 未重写前可能有残留 warning）。

### Step 3 —— agent 面：agent.md + references + subagents + 余下退役（§3 #14-#31 + #25/#17 退役）
内序：
- 3A subagents 4 新增（#28-#31；po_implement/references/variant_implementation.md 先读吸收再退役）。
- 3B agent.md 7 个（po_baseline/po_propose 重写；po_contract/po_probe/po_full_train/po_report 修改）。
- 3C references 4 个（structural-levers 新建；probe_protocol/full_train_protocol/report_format 修改）。
- 3D 退役：`git rm -r po_implement/` + `git rm po_propose/references/playbook.md`。
对应验收：SPEC §3/§4/§5 全部 prompt/契约面；§6 洁净自检门。
完成判据：`tars validate` **0 error 0 warning**；残留 grep（§8 收窄范围 + 豁免）0 命中；7 个 agent.md 的 Output 段与 yaml schema **逐字段人工对齐**（po_baseline 由 T10 单测钉；po_propose 无脚本载体，靠 Output 段逐字段对照 + 本判据——emit↔schema 一致性归 §2.2 组 7）。

### Step 4 —— 测试收口全绿
做什么：跨步测试补齐（eval@k 降级机械面 T14 / E3-07 双源钉 T12——依赖 Step 3 的 po_contract/agent.md 与 check_contracts.sh）；全量三文件跑绿。
完成判据：`tests/test_po_{scripts,diff_check,inject}.py` 全绿（后两者零改动仍绿 = 回归保护）。

### Step 5 —— 文档收尾（§3 #34-#36）
做什么：v2 草稿 superseded 标注；git add v4 spec/草稿 + 本计划。
完成判据：`git status` 干净（可改面内零遗留；并行任务文件零触碰）。

---

## 5. 测试策略

> 全部落 `tests/test_po_scripts.py`（既有惯例：fixture 工作区 + subprocess 驱动 bash 脚本 + import 驱动 python 模块）。
> **跑法（WSL .venv，项目记忆钉死）**：`wsl bash -c "cd /mnt/d/Projects/Orca && .venv/bin/python -m pytest tests/test_po_scripts.py -x -q"`（必要时 `--noconftest`；setsid/进程组语义必须 WSL 内验，Windows 原生不覆盖——既有决策）。

| # | 测试 | 新/改 | 验证意图（对应验收标准） |
|---|---|---|---|
| T1 | gate_node.sh `bash -n` 冒烟 | 新 | D-V4-15：语法过 + 修复后 `--max-rounds` 实参不带引号残片 |
| T2 | render_run PYTHONUNBUFFERED | 改（扩 test_render_run_substitutes…） | D-V4-16：渲染产物 header 层含 `export PYTHONUNBUFFERED=1` |
| T3 | metric_curve --at-epoch | 新 | D-V4-17：缺第 k 点 fail loud / 不传行为不变（最新公共 epoch + 无回归）/ 多点曲线强制取 k / 输出恒含 at_epoch + baseline_path / **保留 epoch 键且 == at_epoch**（§2.4 加法语义钉） |
| T4 | history_lib 字段集 | 改（test_history_builder_field_sets / rejects_unknown / dedup_probe_config_retry） | D-V4-18：PROBE_FIELDS 全集 + 未知字段仍 fail loud + 指纹键行为（同配置阻塞/配置变化重开） |
| T5 | stop_at_epoch 全分支 | 新 | D-V4-3：组 kill（TERM→宽限→KILL）/ 幂等（stop_status 已存在且 killed → 直接返回）/ 10s 宽限 / **实际深度重解析**（kill 后日志多出 >k 行 → stopped_at_epoch 取最大完整 epoch 非恒 k）/ natural_done + rc + monitor_failed（轮数>k）/ pid 归属拒绝（pid 文件指向无关进程 → 拒杀 fail loud） |
| T6 | check_bottleneck | 新 | SPEC §5：封闭 schema（未知键拒）/ 保序子集（跳选合法、乱序/非子集拒）/ cycles=total_cycles referential / 排序 rank 一致 / base_report 缺失拒 |
| T7 | push_curves | 新 | D-V4-2b：幂等（重复调用审计行追加而曲线不重）/ 缺 sock 静默退出 0 / 半写 JSONL 行跳过 / connect+send 各 ≤5s 硬超时不挂起（假 socket 验证）/ 审计行字段 {ts, baseline_epochs, curves} |
| T8 | run_latency_recheck 迁移回归 | 新 | D-V4-10：v3.5 run_verify 语义不变（verdict 写盘/两层校验/gate 数学/reconciliation）。**方法**：一次性用 86ccf99 版 run_verify.sh 在新建 fixture 上跑出期望值，**固化写死进测试文件**（禁运行时 `git show` 动态提取——测试不得依赖 git 历史存在）；迁移后脚本在同一 fixture 输出逐字段一致（run_verify 无既有测试可复用）；skip 键 = verdict.json 存在性 + **复测前删 verdict.json 后重测产出新 verdict（打回钉）** |
| T9 | perturb_ckpt 测试删除 | 改（删） | 退役一致性；deploy orphan 测试扩展断言 perturb_ckpt 被回收 |
| T10 | baseline 链新语义 | 改（重写 test_baseline_chain_* 三件） | SPEC §4 po_baseline：非阻塞（executed 不等训练完）/ stdout 9 字段 schema-shaped（**字段集从 live yaml 派生**，与 Step 2A 同批生效）/ **executed 门控含 business_logic.md 存在**（未落盘 → running 中间态，§2.2 组 7）/ finalizer 产物（pid/log/train_final/baseline_full_acc 指纹含 full_train_budget）/ 增量 extract 原子替换 / 无 rc 死亡重派 ≤3 / 终检 failed 路径（实跑≠渲染 → train_final{failed, stage:final_check} + 文案含准入条款指向）/ 心跳行 ISO8601 行首 |
| T11 | check_contracts v4 断言 | 改（4 件 test_check_contracts_*） | SPEC §4：stop-at-k 接受 / 新字段校验（三态/布尔/full_train_budget 值级）/ reuse 缺 v4 字段 fail loud + fresh_start 提示 / 准入条款句在场（E3-07 消费端） |
| T12 | E3-07 单源钉 | 新 | check_contracts.sh 常量子串 ↔ po_contract/agent.md 逐字一致（防双源漂移） |
| T13 | E3-06 单源钉 | 新 | 同一 contracts 下 stop_at_epoch 判定 == metric_curve extract / 缺 pattern 同一报错面 / --pattern 被拒 |
| T14 | eval@k 降级机械面 | 新 | D-V4-4：append_probe 写 eval_failed/eval_acc/eval_skipped_no_epoch_ckpt 路径 + 降级判定输入面（无 eval 时曲线单判 compare 仍成立）；agent 侧重派控制流归 E2E（§2.4 开口 3 显式声明） |
| T15 | 不动项回归保护 | 不改 | gate_decide/advance_round/analyze/predict_delta/render_run 其余/diff_check/inject 全部既有测试**在 Step 0 清偿后**零改动仍绿 = 未动的 15 共享脚本 + 2 测试文件无意外回归（注：gate 测试 Step 0 修的是测试侧陈旧签名，gate_decide.py 源码零改动） |

**项目无独立 E2E 测试文件惯例下的最小验证方案**：本计划单测即最小验证；真机 E2E 是后续 test-agent 独立环节（SPEC §7，inputs 钉 `full_train_epoch_cap=2, probe_epochs=1, max_rounds=2`）。

---

## 6. 风险与回退

**最可能出错的三处**：

1. **finalizer 守护正确性**（#10，最大新增面 ~250 行 bash）：kill/重派/wipe 规则/增量替换任一错 → 基线锚污染或挂死。
   - fail-loud 点：任何内部步骤失败必写 `train_final{failed, stage}` 再退（不许无终态退出）；po_probe 四象限"死+缺失 → error fail loud"兜底。
   - 回退：Step 2 独立 commit，`git revert` 即回 v3.5 链（86ccf99 可 diff 对照逐段核对）。
2. **schema ↔ emit 漂移**（`additionalProperties:false` 硬拒收）：yaml 9/10 字段与链 stdout / agent emit 任一多字少字 → 节点 output 被引擎拒绝。
   - fail-loud 点：schema-shaped stdout 单测（T10）+ po_propose emit 字段集在 agent.md 显式列出；`tars validate` 结构面。
   - 回退：Step 3 内 yaml 与 agent.md 同批，revert 整批。
3. **stop_at_epoch 进程组语义**（WSL setsid + /proc 依赖）：误杀无关进程（pid 复用）或 kill 后日志仍在写导致深度误判。
   - fail-loud 点：/proc cmdline 归属校验后才 kill；重解析冻结日志取深度；幂等键防二次杀。
   - 回退：T5 全分支单测在 WSL 内钉；失败只涉 #5 单文件，revert Step 1 中该文件即可（与其余 Step 1 改动无耦合——metric_curve/history_lib 独立）。

**其余风险**：
- **Step 0 期望值换算**：gate 测试从绝对阈值（target_makespan）改相对比例（latency_reduction_min）需按 fixture 里 base makespan 重算期望——换算错会把 gate 判定语义测歪。钉法：先改一个测试跑绿确立换算模式，再批量套用；`test_gate_target_met_wins_over_everything` 等交叉优先级用例保持原判定结论不变。
- **v3.5 工作区复用断崖**：check_contracts reuse 对旧 contracts fail loud 是设计行为（SPEC 明示），但 E2E 重跑必须 fresh 工作区或 fresh_start——单测全部 fixture 隔离（既有 tmp_path 惯例），不触真实工作区。
- **并行任务越界**：§1 硬边界清单 + Step 5 完成判据（git status 可改面外零触碰）；review 环节对照检查。
- **run_latency_recheck 迁移语义漂移**：git mv 保历史 + T8 迁移回归（对 v3.5 fixture 的期望输出逐字段复用）。

**分批 commit 粒度建议（= Step 边界，各自可独立 revert；各 commit 保证单测绿，运行时一致以 Step 3 末为界）**：
0. `test(prof-opt): 基线清偿——13 个 v3.5 遗留红测试对齐 86ccf99 脚本现状（gate 签名/链实参/contracts fixture）`
1. `refactor(prof-opt): _po_scripts v4 共享层——stop_at_epoch/check_bottleneck/push_curves 新增 + metric_curve@k/history_lib 字段/gate_node 修复/render 无缓冲 + perturb_ckpt 退役`
2. `refactor(prof-opt): yaml 8 节点 + 节点脚本——schema 先钉 + baseline 链非阻塞/finalizer 守护 / check_contracts v4 / run_verify→run_latency_recheck 迁移 / check_business_logic 新增 + po_gate/po_verify 目录退役`
3. `refactor(prof-opt): agent 面——7 agent.md + 4 references + 4 subagents + po_implement/playbook 退役`
4. `test(prof-opt): v4 单测收口全绿`（若 Step 4 有独立补齐）
5. `docs(prof-opt): v4 SPEC/草稿/计划入库 + v2 草稿 superseded 标注`

---

## 7. 规模标注

**large**。判断依据：~36 项文件动作（8 共享脚本 + 14 节点文件 + 4 subagents + 1 yaml + 1 测试大改 + 3 目录退役 + 4 文档）；两个节点 agent.md 全重写 + 一个 553 行 bash 链脚本重构 + 三个新脚本（其一为守护进程语义）；跨 5 个同步组的多文件一致性要求；测试面 ~15 组增改。计划深度与之匹配：全量文件级清单 + 同步图 + 逐步验收判据。

---

## 8. 边界与下游衔接

- **本计划交付物**：§3 全部文件动作 + §5 单测全绿（Step 0 起每步绿）+ 残留 grep / tars validate 前置自检门。
- **下游 1（洁净审查 Phase）**：每文件单独 reviewer + `verify/cleanliness/<file>.md` 落盘（SPEC §6 判据：逐段受众翻转结论 + 行号引用 findings 或显式零 finding）。coder 交付的前置门 = 词表 grep 0 命中，**范围收窄为本计划可改面**（P2）：`workflows/agents/{po_*,_po_scripts}/ + workflows/subagents/prof-opt/ + workflows/prof-opt.yaml`（不含其他 workflow 目录），词表 = v3.5 词表 `mnist_kd/playground/prof_opt_demo/ns3|psu|kd-nas|nas-supernet 词边界/prof-opt-*-design-draft/docs\/specs/D:\?Projects//mnt\/d/spec-review/SPEC-R1` + 增补 `run_verify/baseline_proxy_acc/baseline_ref/mfu_adapter/perturb_ckpt/playbook/ref-input/auto-trained`；**豁免** = `_po_scripts/mfu_benchmark.py`（"mfu_adapter" 出现在 §1 钉死不变的文件内，单行豁免待 P2 回卷定案）。
- **下游 2（E2E Phase）**：test-agent 按 SPEC §7（claude 后端 + tars skill + WSL；两项目；inputs 钉值如 §5；**注意 P3：`latency_reduction_min` 为 required 无 default，E2E 必须显式传值——SPEC §7"用 input default"一句与 yaml L30 实况不符，待回卷**）。本计划已保证其断言载体全部落位：stop_status.json（#5）、epoch_compare.at_epoch/baseline_path（#3+agent）、finalizer.log ISO8601（#10）、.chart_push.log（#7）、baseline_full_acc/train_final（#10）。
- **回卷规则**：实现期发现草稿级语义问题 → 停手回卷草稿（变更记附 A）+ 重新过确认闸，禁静默偏移；SPEC 实质变更 → 本计划同步修订后重新对账 §2.1；§2.5 P1-P6 任一获 SPEC 裁决后，计划按裁决同步修订对应落点。

---

## 9. 对抗审查记录（plan-adversary 内环，3 轮）

- **轮 1**：1 BLOCKER + 4 MAJOR + 9 MINOR（Q1-Q14）。关键发现：基线 86ccf99 测试面实跑 **13 红 / 46 绿**（v3.5 遗留测试-脚本漂移，planner 独立复跑坐实）→ Step 0；business_logic_path emit 时序 → #10⑤门控；Step 2↔3A 排序矛盾 → 合并批；grep 门双重不可达 → 范围收窄+豁免+P2。
- **轮 2**：Q1-Q14 **14/14 闭环**；新发现 1 BLOCKER（R2-1 IMPL 行写入职责随 po_implement 退役孤儿化——advance_round 按 round 过滤、dedup 按 change_sig，两字段仅 IMPL 行携带，planner 独立 grep 坐实）+ 1 MAJOR（R2-2 chain 重入终态）+ 7 MINOR → 全部修订（P6 + #10 重入状态机等）。
- **轮 3**：R2-1~R2-9 **9/9 闭环**；新发现 2 MAJOR（R3-1 terminal-skip 须两步 append / R3-2 协议拆分线）+ 3 MINOR + 2 附注；终判 **稳态收敛 = 是，CONDITIONAL——补两处后可交付，无需第四轮**。两处修订按轮 3 给出的修复文本逐字执行（含 3 MINOR + 2 附注），未再跑第四轮对抗（3 轮上限），特此披露。
- 密度：三轮均满足"每条验收标准 ≥1 质疑或显式无疑问+理由"（轮 2/3 密度清单在案）；问题密度单调下降 14 → 9+1 → 5，无跨轮反弹。

---

## 10. E2E 回退修订（第 2 轮，2026-08-26）——D-1 引擎侧修复 + D-2 顺手修

> 触发：v4 E2E 第 1 轮（test-agent，2026-08-26）回退两缺陷。D-1 [PLAN_ISSUE]：po_gate script
> 节点 in-session 下必现 exit 127（编排者缺陷报告引 tape seq 26-27 `bash: /mnt/d/Projects/Orca/runs/<run_id>/artifacts/scripts/gate_node.sh:
> No such file or directory`；复核订正：127+stderr 落盘记录在 mnist 磁带 `runs/prof-opt-20260826-*ec553c*.jsonl`
> seq 18-19，target 侧为 ns 后无 nc 的 11 分钟停摆 + 解堵 symlink 建链时间戳推断——两项目独立复现成立，
> target 侧属推断非落盘记录）。D-2 [MINOR_FIX]：po_flatten 首试宿主输出 Python dict 非 JSON
> （output_schema_mismatch，重派后成功，浪费一轮 LLM 执行；target 磁带 seq 3 落盘
> `node_failed po_flatten kind=output_schema_mismatch`，宿主输出为单引号 Python dict）。
> 本节为增量修订：主计划 §1-§9 已交付并 commit（7861a89..2de195e + 24eb711/fa3b686），本轮只做
> 修复 + 复测，**不重开已闭环项**。

### 10.1 D-1 根因（引擎实读证据链，2026-08-26 master 实态）

同一 workflow 内 agent 节点与 script 节点看到**两个不同的 `ORCA_ARTIFACTS_DIR`**，唯一真相源在
in-session 入口、script pass-through 没接上：

1. **agent 节点（project-scoped，v3.5 E2E 验证过的形态）**：bootstrap 经
   `cli.py:1442 _resolve_artifacts_dir(tape_path, run_id)`（`cli.py:964-996`：inputs 含非空**绝对**
   `project_root` + wf_name → `<project_root>/artifacts/<wf_name>/`，否则 per-run 回落）→
   `cli.py:1489 _write_orca_env(artifacts_dir=…)` 写 `runs/<run_id>/orca_env.sh`
   （`cli.py:516 export ORCA_ARTIFACTS_DIR=…`）；`next` 路径 `cli.py:1894 → _derive_artifacts_dir
   (cli.py:1774-1781)` 同源重写。po_flatten（agent）据此让 `deploy_scripts.sh`（L13
   `ART="${ORCA_ARTIFACTS_DIR:?…}"`，L15 `mkdir -p "$ART/scripts"`）部署到
   **`<project_root>/artifacts/prof-opt/scripts/`**。
2. **script 节点（per-run，d62e8d6 引入的断裂）**：in-session script pass-through 经
   `_step_io.py:302-304 make_executor(node, runs_dir=tape.path.parent, workflows_root=…)` →
   `factory.py:115 ScriptExecutor(runs_dir, workflows_root)` → `script.py:107 artifacts_dir =
   _resolve_artifacts_dir(self._runs_dir, ctx.run_id)` → `script.py:246 artifacts_dir_for_run` =
   **run-scoped** `runs/<run_id>/artifacts`（exec 层派生从不读 tape inputs，无 project-scoped 感知）。
   po_gate 的 command（`prof-opt.yaml:332-333 bash "$ORCA_ARTIFACTS_DIR/scripts/gate_node.sh"`）在该
   env 下指向 per-run 目录 → 文件不存在 → exit 127。
3. **为何主计划漏掉**：§2.1「po_gate 零改动」基于 v3.5 E2E 已验证形态；`d62e8d6`（v3.5 E2E 之后、
   v4 计划之前合入）把 script 节点的执行主体从宿主 agent（继承 orca_env.sh）换成引擎 executor
   （自建 spawn env），env 契约断裂点恰在两轮 E2E 之间——计划期未对账引擎 commit 面，接缝漏检。

参考实现：驱动会话引擎修复尝试 diff（WSL `/home/mozzie/e2e_v4/pollution_driver_engine_diff.patch`，
已实读核证其接缝选择与本节一致）。

### 10.2 修复落点裁决：(a) 引擎侧（选定）；(b) workflow 侧（驳回）

**裁决：(a) 引擎侧——script 节点 spawn env 的 artifacts 派生与 agent 节点对齐（单一真相源）。**

理由：
- **单一真相源**：project-scoped 派生逻辑已存在（`cli.py:964`，SPEC 2026-08-06 §2.1 拍板），agent
  节点（orca_env.sh）、bootstrap mkdir、next 重写三处已统一吃它；script 节点是**第四个消费者漏接**，
  修复 = 把同一派生接到第四个消费点，不是新语义。(b) 则把 `<project_root>/artifacts/<wf_name>/`
  路径约定第二次手写进 yaml——同一 workflow 内 agent 看 env、script 看硬编码路径，两真相源，恰是本
  项目顶层铁律（单 tape 唯一真相源）要根治的形态。
- **通用性**（编排者铁律：修复只落通用逻辑、禁项目特判）：(a) 对**一切**有 `project_root` input
  的 workflow 的 script 节点生效（通用规则）；(b) 是 prof-opt 单点路径改写（项目特判味道），且每个
  未来 project-scoped workflow 的 script 节点都会重踩 127。
- **回归面实测近零**：`grep 'kind:\s*script' workflows/` **唯一命中 = prof-opt.yaml:331**——本仓库
  生产 workflow 目录中 script 节点是 prof-opt 独有。repo 其余 `kind: script` 载体（`examples/*.yaml`
  21 处、`tests/e2e_phase12/`、`orca/skills/create-workflow/{examples,benchmark}`）**均无
  `project_root` input**，且用户安装面 `~/.orca/workflows`（catalog 第二搜索路径，不可 grep）同理由
  下条恒等论证覆盖：headless（`tars run`）路径 `make_executor` 不传新参 → 默认 None → per-run 派生
  **字节不变**（`orca/run/orchestrator.py:964-970` 调用点零改动；`tests/run/test_orchestrator.py:302`
  fake `**kwargs` 吞参不受影响）；in-session 无 `project_root` input 的 workflow → 新派生返回值与
  既有 per-run 派生**恒等**（`cli.py:996 artifacts_dir_for_run(tape_path.parent, run_id).resolve()`
  与 `script.py:246` 同函数同参同 resolve），env 字节一致。
- (b) 的全部收益（零引擎改动）只在"完全不动引擎"这一条，代价是永久性双真相源 + 每个未来 script
  节点重复踩坑。驳回。

**落地形态（沿 make_executor 既例加可选参，与 runs_dir[phase-13]/workflows_root[2026-08-04] 同模式）**：

```
cli.py _resolve_artifacts_dir + _read_workflow_name/_read_workflow_inputs/_TAPE_HEAD_SCAN_LIMIT
  ↓ 下沉（逐字搬移，零语义变更）
iface/in_session/_artifacts.py（新模块，公开名 resolve_artifacts_dir / read_workflow_name /
  read_workflow_inputs / TAPE_HEAD_SCAN_LIMIT；cli.py re-import 私有名 alias——cli 内部调用点
  [cli.py:1043/1442/1757/1781] 与既有测试 import 路径零改）
_step_io.execute_script_inline：
  artifacts_dir, _ = resolve_artifacts_dir(Path(tape.path), run_id)   # 与 agent 节点同一真相源
  make_executor(node, runs_dir=…, workflows_root=…, artifacts_dir=str(artifacts_dir))
factory.make_executor：keyword artifacts_dir: str|None = None，仅 script 分支透传
exec/script.ScriptExecutor：__init__ 增 artifacts_dir: str|None = None；exec() 内
  artifacts_dir = self._artifacts_dir if self._artifacts_dir is not None
                 else _resolve_artifacts_dir(self._runs_dir, ctx.run_id)   # None == headless 语义不变
```

- **无条件传参**（非 project-scoped 也传）：无 `project_root` 时派生值与 per-run 恒等（上证），
  单代码路径胜于按 `is_project_scoped` 分叉构造。
- **防御 wrap**：`resolve_artifacts_dir` 对相对 `project_root` raise ValueError；bootstrap 在写 ws
  事件**之前**已校验（`cli.py:1442` 先于 `cli.py:1707` advance），故 script 执行期该 raise 仅坏 tape
  可达——但 daemon 是长活进程，裸 ValueError 穿透 `except InSessionError` 面 = daemon 崩 + 无
  workflow_failed 终态。`execute_script_inline` 内包成 `InSessionError(ERR_INTERNAL_ERROR)`
  （一行 try/except，fail loud 姿势正确化）。
- **SPEC 衔接注记（P7，随签收批）**：SPEC `2026-08-21-in-session-script-node.md` §2.4.2（L82）以
  逐字形态钉 `make_executor(node, runs_dir=…, workflows_root=…)` 调用，本修复加一个可选参（加法，
  列举的既有参数全部保留）。该 SPEC 已过确认闸——按 SDD 时序，addendum（一行：spawn env 的
  artifacts_dir 经 in-session 层从 tape 派生注入）须由编排者**签收本计划时一并批准**（与 §10.5
  factory 批复同一动作），先契约后实现；`docs/specs/` 不在 coder 可改面。

### 10.3 文件级改动清单（第 2 轮新增）

| # | 文件 | 动作 | 改什么 | 为什么 |
|---|---|---|---|---|
| R1 | `orca/iface/in_session/_artifacts.py` | **new** | 从 cli.py 逐字下沉 `_read_workflow_name` / `_read_workflow_inputs` / `_resolve_artifacts_dir` / `_TAPE_HEAD_SCAN_LIMIT`（公开名 + 保留 tuple 返回契约与相对路径 raise 语义） | project-scoped 派生 SSOT；agent env 写入与 script spawn env 两消费者共用，禁复制 |
| R2 | `orca/iface/in_session/cli.py` | modify | 删三 helper 本体 + 常量定义，re-import 私有名 alias（docstring 注明下沉去向） | cli 内部调用点与 `tests/iface/in_session/test_resolve_artifacts_dir*.py` 的 `from …cli import _resolve_artifacts_dir` import 路径零改（两测试文件不动仍绿） |
| R3 | `orca/exec/script.py` | modify | `ScriptExecutor.__init__` 增 `artifacts_dir: str | None = None`（docstring：in-session project-scoped 覆盖；None == per-run 派生，headless 语义字节不变）；`exec()` 内 override 优先 | 注入点在 executor 构造期（与 runs_dir/workflows_root 同位），exec 层不读 tape——project_root 感知留在 iface 层显式传参 |
| R4 | `orca/exec/factory.py` | modify（**边界外一项，待编排者批**，见 §10.5） | `make_executor` 增 keyword `artifacts_dir: str|None = None`，仅 script 分支透传（docstring 同既例格式） | make_executor 是单一分派点（OCP）；直构 ScriptExecutor 绕开 factory 会造第二构造点 |
| R5 | `orca/iface/in_session/_step_io.py` | modify | `execute_script_inline` 派生 `resolve_artifacts_dir(Path(tape.path), run_id)` 并无条件传 `artifacts_dir=str(…)`；ValueError 包 `InSessionError(ERR_INTERNAL_ERROR)`；docstring 注 2026-08-26 修复注记 | D-1 修复本体：script spawn env 与 agent orca_env.sh 同真相源；防御 wrap 防 daemon 裸崩 |
| R6 | `workflows/agents/po_flatten/agent.md` | modify（D-2） | Output 段强化（§10.4-R2 逐字落点） | D-2：首轮宿主手打 Python dict 被拒 |
| R7 | `tests/exec/test_script_env_inject.py` | modify | T-E1/T-E2 两用例（§10.6 表） | 引擎单测：override 生效 + None 默认回归钉 |
| R8 | `tests/iface/in_session/test_in_session_script.py` | modify | T-I1/T-I2/T-I3 三用例（§10.6 表） | D-1 最小真机复现→修复证明 + per-run 回归钉 + 防御面 |

**不动（声明性确认）**：`workflows/prof-opt.yaml`（po_gate command 原样——`$ORCA_ARTIFACTS_DIR`
引用即修复后语义）、`_po_scripts/gate_node.sh`、其余 po_* 全部、
`tests/iface/in_session/test_resolve_artifacts_dir.py` + `test_resolve_artifacts_dir_integration.py`
（re-import 保 import 路径，零改动仍绿 = 搬移纯度守门）、`tests/exec/test_script.py`（None 默认字节
不变）、`tests/test_po_*.py`（引擎修复不触 workflow 面）。

### 10.4 实施步骤（有序）

**Step R1 —— D-1 引擎修复 + 单测（一个 commit）**
**前置条件（编排者签收本计划时必须显式裁决，缺一不开工）**：① §10.5 factory.py 扩项批复；
② P7 SPEC addendum 批复（同动作）。两者任一被驳 → 按 §10.5 fallback 改走直构方案并同步修订本节，
禁静默越界。
做什么：§10.3 R1-R5 + R7 + R8（引擎与测试同批——中间态"有参数无消费者/有派生无注入"无独立价值）。
内序：R1/R2 下沉先行（cli 回归绿证纯度）→ R3/R4 加参（默认 None，既有 `tests/exec/` 绿证向后
兼容）→ R5 注入 + R7/R8 新用例。
对应验收：D-1 修复（po_gate exit 0）；§10.2 三条零回归论证落地为测试钉。
完成判据：`wsl bash -c "cd /mnt/d/Projects/Orca && .venv/bin/python -m pytest tests/exec/test_script.py
tests/exec/test_script_env_inject.py tests/exec/test_factory.py
tests/iface/in_session/test_in_session_script.py
tests/iface/in_session/test_resolve_artifacts_dir.py tests/iface/in_session/test_resolve_artifacts_dir_integration.py -q"`
全绿（含 5 新用例；两 resolve_artifacts 文件零改动仍绿；`test_factory.py` 覆盖 R4 改动的 script
分派面）+ `tests/iface/in_session/` 全目录绿（daemon/marker 等旁证，daemon 路径同走
`execute_script_inline`）+ `tests/run/test_orchestrator.py` 绿（headless fake 注入面回归）。

**Step R2 —— D-2 po_flatten Output 强化（一个 commit）**
做什么（R6 落点，对齐 po_baseline L165-168 的 anti-paraphrase 措辞模式）：
- Output 段头部增硬约束句：**"Never hand-type the JSON. Single-quoted keys, `True/False/None`,
  trailing commas are Python dict repr — NOT JSON; the output gate rejects them. The ONLY valid
  final reply is the emitter command's stdout, pasted verbatim, byte for byte."**
- 增机械自检，**语义钉死为"捕获 → 校验捕获值 → 回复捕获值"**（防"重打副本再校验"绕过根因）：
  `OUT="$("$EMIT_PY" "$ORCA_ARTIFACTS_DIR/scripts/emit_result.py" --field …)"` →
  `printf '%s' "$OUT" | "$EMIT_PY" -c 'import json,sys; json.loads(sys.stdin.read())'` → 校验过 →
  回复 `$OUT` 原文。校验失败 = 重跑 emitter，**禁手工修补捕获值**。
对应验收：D-2（首轮即合法 JSON）。
完成判据：`tars validate workflows/prof-opt.yaml` 0 error 0 warning；§8 词表 grep（收窄范围）0 命中；
`tests/test_po_scripts.py` 全绿（不触脚本，回归确认）。

**Step R3 —— E2E 复测（test-agent 独立执行，范围见 §10.7）**

### 10.5 边界声明（第 2 轮扩充，显式上报）

- 编排者授权面：`orca/exec/script.py` + `orca/iface/in_session/`（选 (a) 时），禁碰
  `orca/iface/cli/`、`orca/skills/`。**本计划用满并申请扩一项**：`orca/exec/factory.py` 加一个
  keyword 参 + script 分支透传（约 4 行 + docstring）。理由：(i) make_executor 是 exec 层单一分派
  点，沿 runs_dir/workflows_root 既例加可选参是既定扩展模式；(ii) 不扩则唯一 in-boundary 替代 =
  `execute_script_inline` 直构 `ScriptExecutor`（绕 factory 造第二构造点 + 偏离 SPEC 2026-08-21
  §2.4.2 钉的 make_executor 调用形态）——更差的架构换边界合规，不值。**并行任务冲突面实证**：
  create-workflow skill v2 触碰 `orca/skills/` + `orca/iface/cli/` + `tests/iface/cli/` +
  `tests/test_skill_{benchmark,v1_checks}.py`（git status 实读），不含 `orca/exec/` 与
  `tests/exec/`、`tests/iface/in_session/`。**编排者驳回时的 fallback**：仅直构方案（上述 (ii)）+
  SPEC §2.4.2 文本偏差同步上报。(b) 已按铁律驳回，不构成 fallback 选项。
- 测试落点：`tests/exec/` + `tests/iface/in_session/`（两目录均非并行任务面）。
- **"workflows/ 面不变"的解读（显式化）**：按编排语境 = D-1 不落 workflow 侧（yaml/节点结构零改）；
  D-2 修法方向编排者已明示 = `po_flatten/agent.md` Output **指引文本**强化（非结构/schema 改动），
  本计划据此纳入 R6。若本意是连 agent.md 也禁碰 → D-2 无落点，上报重裁。

### 10.6 测试策略（第 2 轮）

> 跑法沿 §5：WSL `.venv` pytest。新用例全部真 subprocess（沿 `test_script_env_inject.py` 既有
> 手法：`env` 命令打印子进程 env 断言）。

| # | 测试 | 文件 | 验证意图 |
|---|---|---|---|
| T-E1 | override 生效 | tests/exec/test_script_env_inject.py | `ScriptExecutor(artifacts_dir="/abs/x/artifacts/wf")` 真子进程 `ORCA_ARTIFACTS_DIR` == override 值（D-1 注入点机械面） |
| T-E2 | None 默认回归钉 | 同上 | 默认构造 → `ORCA_ARTIFACTS_DIR == str(artifacts_dir_for_run(runs_dir, run_id).resolve())`（headless/per-run 语义字节不变——零回归的机械证明） |
| T-I1 | D-1 真机复现→修复 | tests/iface/in_session/test_in_session_script.py | fixture：tape ws 含 `workflow_name: wf-a` + inputs `project_root=<abs tmp>`；预置哨兵脚本 `<proj>/artifacts/wf-a/scripts/gate.sh`；workflow A→S(script `bash "$ORCA_ARTIFACTS_DIR/scripts/gate.sh"`)→$end；**真实 `execute_script_inline`**（真 spawn）→ exit 0 + stdout 断言。此 fixture 与缺陷同构（修前 env 恒 per-run → 哨兵不在 → 127）；"修前 127"的落盘旁证 = 第 1 轮 mnist 磁带 `ec553c` seq 18-19（真 127 + 同款 stderr），不设机械 red 步骤——意图级证明「script 节点看到 agent 节点部署的同一目录」 |
| T-I2 | per-run 回归钉 | 同上 | ws **无** `project_root` input → script 子进程 `ORCA_ARTIFACTS_DIR` == per-run 派生值（与修复前字节一致——非 project-scoped workflow 零回归） |
| T-I3 | 防御面 | 同上 | ws 含**相对** `project_root`（构造坏 tape）→ `InSessionError(error_kind=internal_error)`，非裸 ValueError（daemon 长 liveliness 姿势） |
| T-M1 | 搬移纯度守门 | tests/iface/in_session/test_resolve_artifacts_dir.py + _integration.py | **零改动**仍绿（15 单测 + 5 集成测试函数：project-scoped 正路径 / 相对 fail loud / per-run 回落 / marker 后失败信封 / per-run 字面钉）——helper 下沉逐字、cli re-import 保路径的机械证明 |

既有 workflow script 节点回归检查（§10.2 裁决依据的落实验收）：`grep -rn 'kind:\s*script'
workflows/` 唯一命中 prof-opt.yaml:331 → 其 E2E 复测即 §10.7；repo 其余 `kind: script` 载体
（examples/ tests/ skills/）与 `~/.orca/workflows` 无 `project_root` input，由 T-I2 恒等论证覆盖；
headless 路径由 T-E2 钉；`tests/run/test_orchestrator.py` 全 script wf 用 fake make_executor 注入
（SPEC §1 基线事实）不受新参影响——R1 完成判据补跑该文件绿。

### 10.7 E2E 复测范围（Step R3，test-agent）

**inputs 钉值**（沿 SPEC §7 L124 + §8 下游 2 + P3 修正）：`full_train_epoch_cap=2, probe_epochs=1,
max_rounds=2, latency_reduction_min` 显式传 **0.3~0.5**（SPEC §7 钉区间；P3：required 无 default
必显式；区间内任取一值对断言无影响——判定序推演见 §10.10 轮 3 附注 2，test-agent 在 E2E 报告
记录实际取值以便复现）, `fresh_start=true`；WSL + claude 后端 + tars skill（项目例外约定）；**驱动事实（编排者
钉）**：fresh_start 必须 true（工作区有第 1 轮残留）——适用于 §A mnist 全链。§B spot-check 的
`fresh_start=false` 是对该钉值的**显式例外申请**：其机制正是骑第 1 轮残留走 reuse 短路（残留是
成本上限的来源，非污染源）；编排者可否决 → spot-check 整体撤销（豁免理由已备），不影响 §A。

**PLAN_CONFLICT-P8（SPEC §7 loop 断言 vs 判定序事实，上报编排者回卷，禁静默偏离）**：SPEC
`prof-opt-v4-spec.md` L124 钉 `latency_reduction_min` 0.3~0.5 的理由是"**强制 loop 至少一轮**，
防 round 1 即 full-train 空转验收"。代码级事实使该目的在 placeholder 场景不可达：`gate_decide.py:111`
loop 分支要求 `not exhausted`，而 placeholder 下 round 1 即 exhausted（memory + 第 1 轮 mnist 两跑
佐证均未走 loop）。**计划侧默认处置**：inputs 仍按 SPEC 传 0.3~0.5（区间照钉）；E2E 断言把 loop
从硬要求降为观察项（§10.7-A 断言 2 + 观察项段）。**建议 SPEC §7 回卷**：loop 断言改两层表述
（硬 = exit 0 + decision 合法 + 路由机械映射；loop = 真 profiler 场景的增益证据）。

**环境前置（.run_lock staleness，adversary 轮 2 N2/N6 补）**：单写者锁属他 run 且
`pid alive ∨ heartbeat_age < LOCK_STALE_S(1800s)` → flatten Step 0 直接 exit 3
（`reuse_check.sh:71-82`）。**§A 操作性定义"干净环境" = E2E 前手工删除
`<project_root>/artifacts/prof-opt/` 整目录（含 .run_lock 与第 1 轮全部残留）**——这是 E2E
环境准备语义（agent 运行期的 "Never wipe by hand" 约束 agent 不约束 test-agent 备场）；不删目录
则须距第 1 轮最后心跳 ≥30min 且旧 pid 已死（自然 stale 接管）。**§B spot-check 前置**：bootstrap
前查 `.run_lock`——pid dead 且 heartbeat_age ≥ 1800s 才开工；不满足 → 等待或直接按豁免收场。

**A. mnist_kd —— 全链复跑（主验证线，D-1+D-2 同场）**：
- 干净环境（操作性定义见上）、**无任何 symlink/手工 workaround**（第 1 轮曾以
  `runs/<run_id>/artifacts` → project-scoped 的 symlink 解堵——本轮证明 gate 自然收口）。
- 断言清单（硬断言全部机械可判）：
  1. 【硬】po_gate 节点 `exit_code == 0`（非 127），tape 无 `gate_node.sh: No such file or directory`；
  2. 【硬】`po_gate.output.json.decision` 在场 ∈ {full-train, loop, full-train-best-effort,
     finish-failed} 且**路由 == decision 值的机械映射**（full-train/full-train-best-effort →
     po_full_train；loop → po_propose；其余 → po_report；`prof-opt.yaml:338-343` first-match-wins）
     ——"gate 自然收口"的证明主体是 **exit 0 + 合法 decision + 路由一致性**，**不是**"必须走 loop"；
  3. 【硬】`runs/<run_id>/artifacts` 下**无**手工造的 scripts 副本/symlink，且 project-scoped
     工作区（`<project_root>/artifacts/prof-opt/`）内**无任何手工 workaround 痕迹**（symlink /
     手工复制的 scripts）——备场已删整目录，**任何在场痕迹均属本轮运行期所为**（adversary 轮 2
     N6 + 轮 3 N7：查两侧才具检出力，且不带"第 1 轮"限定词——该限定词被备场删目录空洞化）；
  4. 【硬】终态：po_report node_completed + workflow_completed；mnist_kd 在 placeholder profiler
     下合法终态（v3.5 判据沿袭：exhausted 真伪看 `filtered_count`，memory 钉死）；
  5. 【硬·D-2 观察点】po_flatten 首轮 dispatch 即 node_completed，tape 无该节点的
     output_schema_mismatch 重派记录（LLM 行为非完全确定——若偶发复现则报 defect 而非静默）；
  6. 【硬】E2E 质量底线（CLAUDE.md）：逐 agent 抽查产出契约（report 字段语义 / 图表数据真实非空）。
- **观察项（非断言）**：loop 回边是否走过。**不可作硬断言的代码级依据（adversary 轮 1 BLOCKER
  复核确认）**：`gate_decide.py:102-122` 判定序下 loop 分支（L111）要求 `not exhausted`，而
  placeholder profiler 下 round 1 即 exhausted（memory 钉死）→ loop 在本钉定配置下结构性不可达；
  第 1 轮真机佐证：mnist 两跑（`ec553c`/`62a2ef`）均未走 loop（成功跑 round 1 直接 full-train）。
  若复跑意外走出 loop（如 LLM 提案质量致 round 1 非 exhausted）→ 如实记录为增益证据，不据此判负。

**B. target —— 全链不复跑（裁定维持）+ gate-only spot-check（默认执行，B.1 修正后新增）**：
- 第 1 轮 target 的 gate 通过**对修复语义零证明力**（adversary 真机取证订正原草案 B.1）：gate 首试
  ns 后停摆 11 分钟 → `runs/prof-opt-20260826-004219-124142/artifacts` → project-scoped 的解堵
  symlink 建于 02:57:36 → gate 02:57:41 exit 0（建链后 5 秒）；mnist 成功跑 `62a2ef` 的 symlink
  更是先于一切节点预建——第 1 轮所有 gate 通过都可被"symlink 解堵 + 未打补丁引擎"完全解释。
- 全链不复跑的三条理由（重构后）：
  1. 8 断言对 **fix 无关面**仍构成有效证明：D-1 修复只改 script 子进程看到的
     `ORCA_ARTIFACTS_DIR`；gate 下游的 agent 链（full-train/report/写回）env 契约 v3.5 起未变，
     target 第 1 轮对这些面的验证继续有效；
  2. 修复语义（project-scoped 派生 + 注入）是 project-root **通用**逻辑，由 T-E1/E2 + T-I1/I2
     单测钉死 + §A mnist 全链（engine→gate→terminal 真机）证明——target 复跑对其无增量；
  3. 全链成本（fresh_start=true 完整重训 + 全节点 LLM 执行）与剩余信息量不成比例。
- **gate-only spot-check（默认纳入本轮 E2E，补 target 侧 gate 语义证据）**：fresh_start=**false**
  复用第 1 轮工作区（reuse 门机械短路：flatten REUSE / contract viable / baseline train_final
  已写 / propose DONE 幂等 / probe 状态盘面在），驱动至**首个 po_gate** 即断言后收手：exit 0 +
  decision 在场 + 路由机械映射 + 无 symlink。**成本上限：≤6 个 LLM 节点执行**；超出（reuse 链
  意外断裂致重训/重实现）→ 中止并按上述三条理由豁免，如实记录中止原因——豁免是显式裁量不是静默。

### 10.8 风险与回退（第 2 轮）

1. **factory.py 批复 / SPEC addendum 批复被拒**（最可能的政策风险，Step R1 显式前置）：fallback =
   直构 ScriptExecutor（§10.5，SPEC §2.4.2 文本偏差同步上报）。fail-loud 点：前置条件缺一不开工，
   不静默越界。
2. **helper 下沉引入 cli 行为漂移**：R1/R2 是逐字搬移 + re-import；T-M1（两测试文件零改动仍绿）+
   `tests/iface/in_session/` 全目录绿守门；回退单位 = **步骤 commit**（Step R1 引擎批与 Step R2
   agent.md 批各自独立 revert；revert R1 后缺陷回到"已知 127"状态——可接受的无害退化，R6
  （D-2 文本强化）无引擎耦合，可独立留存）。
3. **E2E 复跑假阴性风险**（adversary 轮 1 BLOCKER 已消解）：A 段硬断言全部机械可判且不依赖 loop
   走向（判定序依据 `gate_decide.py:102-122` 复核确认）；spot-check 成本上限 + 显式中止路径防
   预算失控。再爆新缺陷按 severity 路由（minor → coder；plan 级 → 再回本环）。D-2 的 prompt
   强化对 LLM 首试行为是概率性改善非机械保证——若复跑仍 mismatch，属 prompt 韧性问题非本修复
   失败，如实上报。

### 10.10 对抗审查记录（第 2 轮 plan-adversary 内环）

- **轮 1**（2026-08-26）：1 BLOCKER + 1 MAJOR + 6 MINOR + 2 显式无疑问（Q1 修复方向 / Q4 防御
  wrap）。关键发现与处置：
  - **Q6.1 [BLOCKER]** A.2 原"回边真实走过 ≥1 次"硬断言在钉定配置下必假阴性（`gate_decide.py:102-122`
    判定序：loop 需 `not exhausted`，placeholder 下 round 1 即 exhausted；第 1 轮 mnist 两跑均未走
    loop）→ **已修订**：A.2 改"decision 在场 + 路由机械映射"硬断言，loop 降观察项并附代码级依据。
  - **Q6.2 [MAJOR]** 原 B.1"target 第 1 轮已构成修复后语义真机证据"被 symlink 时间戳取证证伪
    （建链 02:57:36 → gate 过 02:57:41；`62a2ef` 建链先于一切节点）→ **已修订**：B.1 撤除，理由
    重构为 fix-无关面 + 通用逻辑单测/mnist 证明 + 成本；target gate-only spot-check 从可选升级为
    默认执行（含成本上限与显式中止路径）。
  - Q2 [MINOR] "生产 workflow 唯一 script 节点"声明收窄至本仓库 workflows/，其余载体 + `~/.orca/`
    面由恒等论证覆盖 → 已修订 §10.2。
  - Q3 [MINOR] READY 与"待批"并存张力 + "(b) 仅存档"不自洽 → Step R1 挂显式前置条件（factory +
    SPEC addendum 双批复）；(b) 从 fallback 链移除 → 已修订 §10.4/§10.5。
  - Q5 [MINOR] D-2 自检句若校验"重打副本"即绕过根因 → 自检语义钉死"捕获→校验捕获值→回复捕获值"
    → 已修订 Step R2。
  - Q6.3 [MINOR] seq 引用失准（127 落盘在 mnist `ec553c` seq 18-19；target 侧为推断）→ 已修订
    §10 序言。
  - Q7 [MINOR] T-M1 集成测试计数 3→5；T-I1"修前即 127"无机械 red 步 → 改引真机磁带旁证 → 已修订
    §10.6。
  - Q8 [MINOR] SPEC addendum 时序倒置（先实现后补契约违 SDD）→ addendum 批复并入编排者签收动作
    （Step R1 前置）→ 已修订 P7。
- **轮 2**（2026-08-26）：第 1 轮 8 项验尸 = 7 全闭环 + Q6.2 半闭环；新发现 2 MAJOR + 4 MINOR，
  无新 BLOCKER。处置：
  - **N1 [MAJOR]** §10.7 原 inputs 只写"<显式传值>"静默丢了 SPEC §7 L124 钉的 0.3~0.5 区间，且
    loop 降观察项是对 SPEC"强制 loop 至少一轮"的实质偏离未登记 → **已修订**：inputs 恢复钉
    0.3~0.5；新增 **PLAN_CONFLICT-P8**（SPEC §7 回卷建议：loop 断言两层表述）+ 头部索引。
  - **N2 [MAJOR]** spot-check/A 段均缺 `.run_lock` staleness 前置（他 run 锁 + age<1800s →
    flatten Step 0 exit 3，`reuse_check.sh:71-82`）→ **已修订**：新增"环境前置"块——§A"干净
    环境"操作性定义 = 删 `<project_root>/artifacts/prof-opt/` 整目录（E2E 备场语义）；§B 前置 =
    查锁 pid dead ∧ age ≥ 1800s。
  - N3 [MINOR] Step R1 判据补 `tests/exec/test_factory.py`（R4 改动面的直接测试）→ 已修订。
  - N4 [MINOR] §10.8-2 回退表述与两 commit 分批矛盾 → 改"步骤 commit 各自独立 revert"→ 已修订。
  - N5 [MINOR] 头部 READY 无条件视图 → 头部改"READY（两项签收前置 + P8）"→ 已修订。
  - N6 [MINOR] "干净环境"无操作性定义 + 断言 3 对旧 symlink 无检出力 → 环境前置块 + 断言 3 扩查
    project-scoped 侧 → 已修订。
  - 验尸通过项（无疑问+理由）：D-1 修复正确性（行号逐比对一致）、D-2 分层诚实性、T-* 载体与计数
    （15+5 独立复核吻合）、A 段断言 5 可判性（`output_schema_mismatch` = tape `node_failed`
    `data.kind/error_type`，`orca/run/step.py:550-555`）、spot-check 收手语义（无锁残留阻塞）。

- **轮 3**（终轮，2026-08-26）：N1-N6 **6/6 闭环**（验尸通过，N6 主体闭环遗留独立为 N7）；新发现
  **0 BLOCKER / 0 MAJOR / 1 MINOR（N7）+ 2 附注**；密度单调收敛 8 → 6 → 1，无跨轮反弹，
  **稳态达成**。处置：
  - N7 [MINOR] 断言 3 的"第 1 轮残留"限定词被 §A 备场删目录空洞化（恒真零检出力）→ 改"无任何
    手工 workaround 痕迹；备场已删整目录，任何在场痕迹均属本轮运行期所为"→ 已修订。
  - 附注 1：头部 L8 索引未含 P7/P8 全貌 → 头部第 2 轮修订行补 P7 + P8 → 已修订。
  - 附注 2（无疑问+理由）：0.3~0.5 区间内取值对断言无影响（placeholder exhausted 下走
    `gate_decide.py` L115/L119 与该值无关；L102 full-train 分支被断言 2 四值兜底）→ 采纳其建议：
    inputs 钉值行注明"任取一值 + E2E 报告记录实际取值"→ 已修订。钉单一值反而违 SPEC（区间是
    SPEC 原文），不采纳。

### 10.9 规模标注（第 2 轮）

**medium**。引擎 5 文件（1 新模块 + 4 小改，核心逻辑逐字搬移 + ~15 行新接线）+ 1 agent.md 段落强化
+ 2 测试文件 5 用例 + E2E 复测一轮（mnist_kd 全链 + target 裁量豁免）。判断依据：跨 exec/iface 两层
但改动面窄、有三重零回归论证（唯一 script 节点 / headless 默认不变 / per-run 恒等）、测试钉法机械
可判。

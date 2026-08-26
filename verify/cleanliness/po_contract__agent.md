# 洁净审查记录：workflows/agents/po_contract/agent.md（prof-opt v4）

- 审查对象：`D:\Projects\Orca\workflows\agents\po_contract\agent.md`（559 行，frontmatter + 9 步 Workflow + Validation + contracts.json schema + Guidelines + Output）
- 方法：按 `orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md` §8 受众翻转通读（假设执行 LLM 只拥有本文件 + 工作区事实，零开发历史）+ 残留词表 grep + `docs/specs/prof-opt-v4-spec.md` §4 po_contract 行逐条契约核对（语义权威：`prof-opt-v4-design-draft.md` D-V4-4/D-V4-5/D-V4-19）。
- 交叉验证的磁盘事实：`_po_scripts/`（render_run.sh / gen_export_onnx.py / emit_result.py / assert_shadow.py / deploy_scripts.sh 全在，orca_inject 部署到 `$ORCA_ARTIFACTS_DIR/orca_inject/`）、`po_contract/scripts/check_contracts.sh`（含 v4 断言 + "predates the current workflow version" + fresh_start 提示 + ADMISSION_CLAUSE 常量子串）、`subagents/prof-opt/paradigm-verifier.md`（sentinel `PV8RK2` 与 Step 9 硬编码哨兵一致）、`prof-opt.yaml`（inputs project_root/probe_epochs/full_train_epoch_cap/seed 全在；description 携带准入条款）、`po_flatten/agent.md`（readiness.json 含 `python`/`model_facts`/`shadow_pkgs` 键）。

## 1. 逐段受众翻转结论表

| 段落（行） | 受众翻转结论 |
|---|---|
| frontmatter description（2-3） | 产品说明书式（discover/adapt/pin/reject/render 动词链），零残留。通过 |
| 开头职责（7-32） | 每条职责可独立执行；"two budgets / fairness invariant / MEASURE everything" 均为运行时语义。通过 |
| Admission clause（34-38） | 条款句中文常量（gate 子串校验的设计决定）；"Copy verbatim into reason" 指令明确可执行。通过 |
| Shared scripts 声明（40-42） | 四个脚本名与 `_po_scripts/` 实物对上；"do NOT reference workflow source paths" 是对执行者的合法约束。通过 |
| Resource Anchors（44-56） | `$ORCA_AGENT_RESOURCES` / `$ORCA_ARTIFACTS_DIR` 为引擎注入 env；readiness.json 三个键、manifest、inputs 四项均与上游/yaml 实物对上，无悬空引用。通过 |
| Path Iron Rules（58-61） | 通过 |
| Subagent Call Protocol（63-70） | 仅声明 paradigm-verifier（与 spec §3 派单矩阵一致）；sentinel 机制 point-to-file，可执行。通过 |
| Lazy Loading（72-76） | 通过 |
| Workflow 总则（78-83） | at-least-once 重入 + evidence 重用规则，运行时语义。通过 |
| Step 0 Reuse Gate（85-103） | 引号短语 "predates the current workflow version" 与脚本 line 331-335 逐字对上；exit 0/1/2 三分支与脚本语义一致。通过 |
| Step 1 Snapshot（105-137） | python heredoc 自包含可执行；`{{ inputs.project_root }}` render 注入正确。通过（内联问题见观察 O1） |
| Step 2 Train Contract（139-234） | item 1-7 逐项可执行：metric pattern 锚定要求明确、ckpt 三态定义明确、早停双通道（扫描+实跑观察）判定明确、quick-run 一跑双用（epochs=2 强制）明确。残留：①③ 跳号（F2）、`runs_epochs_zero_rejected` 弱路径（F1） |
| Step 3 Eval Contract（236-301） | dual-checkpoint probe 机械可执行；SHADOW_PKGS 提取的双源回退（contracts.json → readiness.json）与实物字段一致；render_run token 形态正确。通过（⑤ 跳号入 F2） |
| Step 4 Export Contract（303-323） | gen_export_onnx.py 存在；opset 17 / static shapes / determinism 钉死。通过 |
| Step 5 Run Templates（325-354） | 四模板与 Output 四个 `*_script` 字段一一对应；probe/full byte-identical + 数据管线一致（无 ckpt/截断 token）明确落了 spec 的"模板同源管线一致"。通过 |
| Step 6 sitecustomize 披露（356-380） | merge 链块可直接执行；`orca_inject/` 部署位置与 deploy_scripts.sh 一致。通过 |
| Step 7 Budget Selection（382-415） | full_train_budget 值级指纹（epochs/seed/data null 对）+ proxy_budget（k 缺省 min(1,full)，D-V4-5 一致）+ `probe_cap_mechanism="stop-at-k"`。通过 |
| Step 8 Post-Snapshot（417-437） | 可执行。通过（内联见 O1） |
| Step 9 paradigm-verifier（439-467） | 哨兵 `[subagent:paradigm-verifier v1 PV8RK2]` 与 subagent md frontmatter 逐字一致；per-entry 报告路径防覆盖的理由是操作语义。通过 |
| Validation gate（469-477） | check_contracts.sh 存在；fix-loop ≤3。通过 |
| contracts.json schema（479-521） | 与 spec §4 新字段逐一吻合（见 §3 核对表）。通过 |
| Guidelines（523-529） | 通过 |
| Output（531-559） | emit 字段完备，viable=false 降级路径每个字段都有归宿。通过（示例硬含 tier-B 产物名见 O4） |

## 2. 残留词表 grep（命中即 finding）

任务词表 19 词（mnist_kd / playground / prof_opt_demo / run_verify / baseline_proxy_acc / baseline_ref / mfu_adapter / perturb_ckpt / playbook / ref-input / auto-trained / docs\/specs / D:\\Projects / \/mnt\/d / spec-review / SPEC-R1 / ns3 / psu / kd-nas / nas-supernet / prof-opt-design-draft，-i 大小写不敏感）+ 补充词（懒补训 / epoch-only / epoch_only / v3.5 / v4 / po_implement / po_verify / 补训）：**全部零命中**。

## 3. 契约一致性核对（spec §4 po_contract 行 v4 增量，语义权威 draft D-V4-4/5/19）

| spec 增量条款 | 文件落点 | 结论 |
|---|---|---|
| 快跑统一 ≥2 epochs（一跑双用：epoch 行格式 + ckpt 行为） | Step 2 item 7（206-234）主路径 `epochs=2`（≥ 2 required）+ "ONE run, TWO uses" | 主路径已落；但保留 epochs=0 弱分类 → **F1 偏差** |
| 早停 best-effort 早拒（argparse/config 扫描 + 实跑观察 → viable=false 归因披露） | Step 2 item 4（172-186）：EITHER 通道检出 → `early_stopping_detected: <mechanism + evidence>`；头部 24-28 同义 | 已落，无偏差 |
| metric pattern 数字组后行尾/非数字界锚 | Step 2 item 1（147-152）MUST 锚定 + 截断反例；schema 注释（498）重复 | 已落，无偏差 |
| 快跑 best-effort 断言实跑 == 渲染 | item 7 `epoch_lines_matched`（225-228） | 已落，无偏差 |
| `train.ckpt_output_rule` 三态描述 | item 2（157-170）+ schema（496）：literal/{out_dir} 占位、trailing `*` = newest、per-epoch glob 可寻址形态 | 已落，无偏差 |
| `train.ckpt_per_epoch` 可寻址布尔 | item 2：N epochs ↔ N files 实测判定，k-th = k-th glob match；schema（497） | 已落，无偏差 |
| `full_train_budget{epochs,seed,data:null对}` 值级指纹（UD-2） | Step 7（387-398）cap 逻辑 + null 对钉值；schema（506-507） | 已落，无偏差 |
| `probe_cap_mechanism="stop-at-k"` | Step 7（410）+ schema（510）+ Output emit（548） | 已落，无偏差 |
| reuse 缺 v4 字段 → fail loud + fresh_start 提示 | Step 0（96-99）Exit 1 第一分支；脚本 line 321-336 实现吻合 | 已落，无偏差 |
| contracts reason 准入条款句（agent.md 唯一源 + gate 常量子串） | 34-38 + schema 488 + 516-519；check_contracts.sh `ADMISSION_CLAUSE="训练须按给定轮数精确执行"`（前半句子串，全文 ⊇ 子串，陈述成立） | 已落，无偏差 |
| check_contracts 断言变更（接受 stop-at-k / epoch-only 全 null / 新字段 / probe-full 同源模板 + 管线一致） | 脚本侧已证实（stop-at-k / full_train_budget / ckpt_per_epoch / line 319 "regenerate both from the same source"）；agent.md 侧对应落点 = Step 5 byte-identical 双模板（343-346）+ 数据管线一致（334-342） | 已落，无偏差 |

## 4. Findings

### F1（medium）— 保留 v3.5 的 epochs=0 快跑弱证据路径，偏离"统一 ≥2 epochs 一跑双用"
- 位置：`workflows/agents/po_contract/agent.md:215-221`（`"runs_epochs_zero_rejected"` 分类）
- 问题：draft D-V4-4（语义权威）与 spec §4 均规定契约期实测快跑**统一 ≥2 epochs、一跑双用（指标格式 + ckpt 行为）**；本文件主路径正确（epochs=2 强制），但额外保留 epochs=0 probe 弱分类作为 escape hatch，且措辞 "kept as a valid classification" 是面向 reviewer 的沿革辩护（"kept" 预设读者知道旧版本）。该路径下 ckpt 行为（双用的另一半）无法被证明——`ckpt_per_epoch` 只能落 undecidable→false，"一跑双用"退化为"零跑 + 借用户日志"。v3.5 的 epochs=0 快跑是被 v4 "统一"掉的机制，残留即 v4 已删机制残留（任务判据③）。
- 建议修法：删除该分类，快跑负担不起 ≥2 epochs 的场景显式 fail loud（`viable=false` + 归因"quick-run budget infeasible"，交用户以 `full_train_epoch_cap`/`probe_epochs` 降预算后重入）；若确需保留降级路径，先回卷草稿记附 A 认可，并把 "kept as a valid classification" 改为产品说明书式直述。

### F2（low）— ①③⑤ 带圈编号跳号，v3.5 五项清单残迹
- 位置：`workflows/agents/po_contract/agent.md:145`（"① epochs ③ out-dir"）、`:193`（"①③ all parameterized"）、`:239`（"⑤ checkpoint path switch"）
- 问题：②④ 在本文件任何位置从未出现——编号来自一个未随迁的旧五项清单，受众翻转时执行者需自行映射编号→名称（且会疑惑为何跳号）。属"只有了解 workflow 历史才看得懂"的设计推理痕迹（契约 §0 顶线）。
- 建议修法：三处改为纯名称（epochs / out-dir / checkpoint path），或按本文件实际清单连续重编号。

## 5. 观察（不计 finding，供主审知悉）

- **O1 确定性代码内联 vs 契约 §4**：Step 1 快照 python（112-137）、Step 3.3 SHADOW_PKGS 提取（267-291）、Step 8 diff python（421-433）为多行循环/分支确定性逻辑，命中洁净契约 §4"应抽 `scripts/<name>.sh`"类别；但 spec §1 交付清单明确 po_contract 仅有 check_contracts.sh（v3.5 继承形态）。具体 spec 优先，且三片段对执行者自包含可执行（不违任务判据①），故记张力不计 finding。
- **O2**（519-520）"a test pins this document and the gate together"——test 存在性元信息，对执行者有"不得改写条款句"的威慑价值，一句话成本，容忍。
- **O3**（328-330）"this prompt is Jinja2-rendered…" 是文档渲染机制的元解释；可操作含义（模板 body 只用 `<<token>>`）清楚，容忍。
- **O4**（551）Output 的 generated_artifacts 示例硬含 tier-B 专属产物 `verify/paradigm_verifier_report_train.md`，靠尾部 `...` 与"生成物清单"语义兜底，LLM 执行者可正确按实际泛化。
- **O5**（88-90）Step 0 的 ORCA_PYTHON 提取在 readiness.json 缺 `"python"` 键时会裸 KeyError traceback——flatten 契约保证字段在场（越权场景），未 flag。

## 6. 结论

任务判据四类（可独立执行 / 开发期残留 / v4 已删机制残留 / 产品说明书语气）+ 词表 grep + spec 增量一致性：悬空引用与词表全清，spec 11 条增量 10 条无偏差落地，2 处残留（F1 epochs=0 弱路径 + F2 编号考古）。

VERDICT: ISSUES (2)

---

## 7. 复验（2026-08-26，修复 commit `24eb711`，基线 `2de195e`）

复验对象：`git diff 2de195e..24eb711 -- workflows/agents/po_contract/`（agent.md + scripts/check_contracts.sh 两文件）；复验时 HEAD == `24eb711` 且 po_contract 工作区无后续改动；`2de195e..24eb711` 间 spec §4 po_contract 行零改动（初审一致性基准未漂移）。

### 7.1 F1 复核（runs_epochs_zero_rejected 残留）——已闭环

- `agent.md:215-221`（旧）的 `"runs_epochs_zero_rejected"` 分类整段删除；替换为 fail-loud 归因条款：快跑负担不起 ≥ 2 epochs → `viable=false` with the cost named in the reason，并显式钉死 "never downgrade / no weaker probe exists in this pipeline"——正确实现初审建议的修法一（删类 + fail loud + 归因）；"kept as a valid classification" 沿革措辞随分类消失。
- `check_contracts.sh`（连带 gate↔prompt 矛盾，初审漏报、本次修复）：evidence 文件名 `train_dryrun.json` → `train_quickrun.json`（对齐 prompt Step 2 item 7 的证据文件名），status 枚举从 `in ("runs_epochs_zero_rejected", "runs_minimal_budget")` 收缩为 `== "runs_minimal_budget"`——与 prompt 删类后唯一合法枚举一致。
- ripgrep 复验（大小写敏感）：`epochs_zero|runs_epochs|dryrun` 零命中。

### 7.2 F2 复核（①③⑤ 跳号）——已闭环

- 三处编号全部改纯名称：`:143` "epochs / out-dir, plus seed when supported"（原 ①③）、`:193` "epochs / out-dir all parameterized"（原 ①③）、`:240` "the checkpoint path switch"（原 ⑤）。
- ripgrep Unicode 字符类复验：`[①②③④⑤⑥⑦⑧⑨]` 全文件零命中（bash grep 字节类对多字节标点有假阳性，已用 ripgrep 复核排除）。

### 7.3 连带修复复核（Tier B 允许清单对齐）——通过

- `agent.md:195-203` Tier B 允许改编扩为三项：(a) CLI switches（epochs/out-dir/**seed**）、(b) path parameterization（out-dir、checkpoint path）、(c) intra-workspace import adjustments，尾注 "the same default list paradigm-verifier judges against"。
- 交叉验证 `subagents/prof-opt/paradigm-verifier.md:28-31`：其 "Allowed adaptations (defaults; the caller may extend)" 恰为同款三项、逐字对应——指代非悬空引用；verifier 侧 "(defaults; the caller may extend)" 与 agent.md 侧将其作为默认清单的表述兼容。(a) 增补的 seed 与 item 1 "plus seed when supported" 及 schema `train.flags.seed` 自洽。

### 7.4 新增文本受众翻转

替换句与 (a)(b)(c) 均为产品说明书式、可独立执行、归因要求明确；无新残留词、无新悬空引用、无 v4 已删机制措辞回流。词表（任务 19 词 + 补充词）在 24eb711 版本复跑零命中。

### 7.5 结论

F1/F2 均按建议修法闭环；两处连带修复（gate↔prompt 证据文件名与枚举对齐、Tier B 清单与 verifier 判据对齐）方向正确且经交叉验证成立。无未解决项，无新增 finding。

VERDICT: CLEAN

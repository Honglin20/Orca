# 洁净审查记录：workflows/prof-opt/agents/po_baseline/agent.md

- 审查对象：`D:\Projects\Orca\workflows\prof-opt\agents\po_baseline\agent.md`（v7 现行，307 行，per-wf 自包含布局）
- 审查性质：mfu-analyzer v3 同批改动的洁净复审（2026-09-08）。本文件一处改动：mfu-analyzer Task 模板补 `<hardware_ref>`（L113）。同批联动锁死点：`scripts/run_baseline_chain.sh:354` 哨兵 v3、`scripts/check_baseline_docs.sh:26` 哨兵 v3 + L97 节清单增 `### MFU 损耗分解`。plan：`docs/plans/2026-09-08-mfu-analyzer-v3.md` 改动清单 #4-#7
- 审查方法：全文通读 + 受众翻转逐段裁决 + 五组禁词 grep + 派发参数与 mfu-analyzer.md Inputs 表逐项对账 + 校验门 WSL 实测（三用例）
- 参照：洁净契约 `orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md`；SPEC `docs/specs/prof-opt-v7-spec.md` §3.2/§3.4/§4.3；`workflows/prof-opt/subagents/mfu-analyzer.md`（v3）；`workflows/prof-opt/subagents/references/ascend.md`
- 审查日期：2026-09-08（本快照为 v7 全量重写版，取代 2026-08-25/26 的 v4 时代两轮记录——旧记录针对平铺路径与 placeholder/mfu_adapter 双模机制，历史见 git）

## 一、逐段受众翻转结论表

| 行号 | 段 | 受众翻转结论 |
|---|---|---|
| 1-46 | frontmatter + 四点差异定义（非阻塞 / mfu 唯一 profiling 路径 / 选卡职责 / 三分析师并行） | CLEAN。每句自包含可执行；「There is no adapter, secondary analyzer, estimator, or fallback path」是 fail-loud 边界宣言（与 mfu-analyzer.md L68、chain L342-343 三方一致），非退役物叙事；「you own the analyzer dispatch across that boundary」职责显式 |
| 48-66 | Critical Protocol（baseline_status.md 跨 turn 真相源 / running→STATUS MESSAGE 禁 JSON / 禁手启动长任务 / 单 bash ≤10 min） | CLEAN。全部可机械遵守；「do not call orca next」运行时 CLI 实名 |
| 68-87 | Resource Anchors + Path Handling Rules | CLEAN。`$ORCA_ARTIFACTS_DIR`/`$ORCA_AGENT_RESOURCES` 契约 §5 operational env；「Shared scripts execute from `$ORCA_ARTIFACTS_DIR/scripts/`（deployed at flatten time）— never from the workflow source tree」部署事实交叉引用，非考古；`{{ inputs.latency_reduction_min }}`/`{{ inputs.accuracy_budget }}`/`{{ inputs.seed }}` 模板引用合规（§6 夹具防火墙）；profiling 三参来源 = contracts.json `profile` block（L80-83）+ chain re-read fail loud——与 mfu 派发参数对账的关键句（见 ②） |
| 89-124 | Subagent Call Protocol（point-to-file + 首行哨兵 + 失败矩阵；**本次改动 L113**） | CLEAN。`<hardware_ref>={{ subagents_root }}/references/ascend.md` 与 po_propose L50/L90 写法逐字一致；Task 七参（onnx_path/profile_dir/report_path/hardware_ref/chip/precision/core_num）与 mfu-analyzer.md Inputs 表（L29-37）逐名逐序对应；「chip / precision / core_num values you read from `contracts.json`'s `profile` block」取值出处显式——本文件无缺参指引问题（po_propose 侧的对应缺口见该对象快照 FINDING-1）；「sentinel … must come from the file you Read（don't guess, don't infer from this prompt）」哨兵协议防臆造；失败矩阵（哨兵不匹配/产物缺失 → 重派 1 次 → 二次 failed 报名）重试可见、fail loud；「(A failed `mfu-analyzer` also stops the training launch …)」分支后果显式 |
| 126-128 | Lazy Loading | CLEAN |
| 130-156 | Step 0/0a/0b（前置七件套 fail loud / chmod 幂等 / probe→亲读占用原文→--device） | CLEAN。「the judgement which card is free is YOURS, the ledger only claims atomically」职责切分显式；probe 非零退出 fail loud（「without observation there is no honest card selection」为操作性 why） |
| 178-217 | Step 1 chain 调用 + origin anchor 冻结 | CLEAN。`--device` 必填语义、锁 claim/release 归 chain、freeze 幂等 + 非零退出含义（非法值域 / 既有锚内容不同）与 remedy 指引（引 stderr、fresh_start）齐 |
| 219-246 | Step 2 派发分析师（**联动区**） | CLEAN。mfu 派发时机双判据（chain awaiting 状态行携带全参数集 / onnx 在场且无 schedule_result.json 的主动判据）可机械执行；机械校验两条单行命令（schedule_result 探测 + 报告非空）——契约 §4 单行 operational 允许类；「the report's SENTINEL is `check_baseline_docs.sh`'s business, never re-typed here」——**哨兵单一真相源声明**，与 gate L5-7「the three sentinel literals live HERE and nowhere else」互相印证（单一真相源 + 防节点侧重打哨兵字面量）；「never "fix" raw products or create derived profiling files by hand」fail loud |
| 248-271 | Step 3 有界轮询 + Step 4 校验门 | CLEAN。busy-step 例举与 chain emit 文案对应（"awaiting mfu-analyzer products" 等）；executed/failed → verbatim 转发；门败 → 重派 1 次 → fallback emitter |
| 273-307 | Guidelines + Output（verbatim 转发铁律 + fallback emitter） | CLEAN。`additionalProperties: false` 论据为 schema 机制说明（运行时事实）非考古；emitter 仅在 chain 无可解析 stdout 时使用的边界与 Step 3/4 的 override 关系自洽 |

## 二、派发参数对账（v3 焦点）

| mfu-analyzer.md Inputs（L29-37） | po_baseline Task 模板（L113） | 结论 |
|---|---|---|
| `<onnx_path>` | `=$ORCA_ARTIFACTS_DIR/base/model.onnx` | 一致 |
| `<profile_dir>` | `=$ORCA_ARTIFACTS_DIR/base/profile` | 一致 |
| `<report_path>` | `=$ORCA_ARTIFACTS_DIR/base/profile/mfu_bottleneck_report.md` | 一致 |
| `<hardware_ref>`（开工前必须 Read） | `={{ subagents_root }}/references/ascend.md` | **一致**（本次新增；路径与 po_propose L50/L90 逐字同；`{{ subagents_root }}` 经 `orca/compile/layout.py:31-49` 解析到 `<wf>/subagents/`，`references/` 子目录可达；validator `orca/compile/validator.py:1258` 只 glob 顶层 `*.md`，子目录不进 frontmatter 校验亦不破坏之） |
| `<chip>` / `<precision>` / `<core_num>` | `=<contracts profile.chip/precision/core_num>` | 一致（取值出处显式） |

## 三、联动锁死点核对 + 门实测（本次复审机械证据）

- `scripts/run_baseline_chain.sh:354`：报告首行哨兵比对已 v3（`bash -n` 通过）。
- `scripts/check_baseline_docs.sh:26`：`MFU_SENTINEL` v3；L97 节清单五节 = mfu-analyzer.md 模板六节中的必需五节（`### 分析源文件` 不入门，门侧从简非矛盾）；L15 头注释 "mfu-analyzer v3, five sections" 与实际一致；L5-7 哨兵单一真相源声明成立（全仓扫描：节点 prompt / chain / tests 均不重打哨兵字面量，tests 是测试 fixture 另论）。
- WSL 实测三用例（合成报告，ORCA_ARTIFACTS_DIR 绝对路径）：
  1. v3 哨兵 + 六节全 → **exit 0 PASS**；
  2. stale v2 哨兵 → **exit 1**，报文给出期望 v3 / 实得 v2——旧工作区遗留报告被正确拒绝（防 v2 时代工作区续跑假绿）；
  3. v3 哨兵缺 `### MFU 损耗分解` → **exit 1** 并点名该节。
- 全仓 v2 哨兵扫描唯一命中：`docs/plans/2026-09-08-mfu-analyzer-v3.md:25`（D3 修订记录，开发文档，允许）。

## 四、词表 grep（五组，2026-09-08 口径）

| 组 | 词表 | 结果 |
|---|---|---|
| A 退役机制/测试夹具 | `mnist` `MNIST` `CIFAR` `playground` `model8` `pure_cnn` `feat_complex` `wireless` `mnist_kd` `profile_script_path` `placeholder_profiler` `bottleneck-analyst` `mfu_adapter` `baseline_proxy` `run_verify` `perturb_ckpt` `playbook` | **0 命中**（`mfu_adapter` 已随 v7 §3.1 退役，本文件零残留） |
| B 迁移/版本考古 | `迁移` `前身` `前作` `legacy` `deprecated` `formerly` `used to` `analogue` `stall` `deepseek` `kill+retry` `TODO` `FIXME` | **0 命中** |
| C 引擎源码/内部路径 | `orca/(exec\|compile\|run\|schema\|iface\|events\|gates)` `examples/` `D:\Projects` `/mnt/d` `docs/specs` `docs/plans` | **0 命中** |
| D spec/plan 节号 | `§N.M` 形、`SPEC `、`ADR-N`、`phase-N`、`plan §` | **0 命中** |
| E v2 残留/修订措辞 | `\bv2\b`、`修订记录`、`历史版本`、`不再` | **0 命中** |

## 五、Findings

**零 finding。** 本次改动 hunk（L113 补 `<hardware_ref>`）与同批两个脚本锁死点（chain :354 / gate :26+:97）受众翻转通过、实测验证生效；全文五组禁词零命中；派发七参与 subagent Inputs 表逐名对应且取值出处显式。

（仓库级相邻观察，不属本对象，记录于总报告 F4：`agents/po_flatten/scripts/check_flatten.sh:14-15` 头注释部署清单仍列已删除的 `analyze`/`mfu_adapter`，实际门代码 :139-141 只查四个脚本——注释漂移，与本文件 L21「no adapter」宣言文字面相抵，非本批引入。）

## 六、裁决

v3 同批改动（Task 模板 `<hardware_ref>` + chain/gate 哨兵与节清单同步）受众翻转通过；派发参数七项全对齐、哨兵单一真相源未被打破；校验门对 stale v2 报告与新节缺失均正确拒绝（实测）。无洁净契约违规，无 finding。

VERDICT: CLEAN（2026-09-08 v3 复审）

## 七、2026-09-09 作者人工复核补记

- **F4（相邻观察）已修复**：check_flatten.sh 头注释部署清单删 analyze/mfu_adapter，与门代码 :139-141（orca_inject 对 + scripts/{assert_shadow,render_run,emit_result,deploy_scripts}）实测一致。
- **人工复核新发现 1 处（审查 agent 低判），已修复**：Step 2（L221-225）原「its `running` line carries the **full dispatch parameter set**, including the chip / precision / core_num」与事实有偏差——chain 的 running 行实际只携带产物路径 + 三评测参，**不含 `<hardware_ref>`**（后者在 L113 Task 模板内由 render 期内联）。运行时 agent 按「full」到 running 行对账会找不到 hardware_ref。已改为「carries the product paths and the chip / precision / core_num …; `<hardware_ref>` is already in the dispatch template above」。
- 修后复扫：哨兵锁定点、派发七参对账、五组禁词零命中均维持；verdict **维持 CLEAN**。
